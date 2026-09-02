"""The run loop: `dradar go` / `dradar resume`.

Runs the volunteer's held batch of cells serially — free-pick instances let
them claim up to a handful at once on the web, and the CLI works through
them one at a time. Menu-mode instances (no web claim) still claim a single
task from the menu. Quota is the volunteer's own to manage — dradar shows
the server's per-task estimate and lets them decide whether to proceed; if a
run doesn't finish before its lease expires, the cell just reopens for
someone else with nothing counted. Split out of cli.py to separate this from
identity (login/register) and doctor (environment checks) concerns.
"""

from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from . import (
    __version__, artifact_staging, assignment_boundary, assignment_lock, egress,
    failure_circuit, image_cache, local_jobs, pending, refill as refill_plan,
)
from .api_client import ApiClient, ApiError, normalize_batch_id
from .codebuddy_provider import (
    CODEBUDDY_AGENT,
    CODEBUDDY_NATIVE_EFFORTS,
    CODEBUDDY_RUN_CONFIG_VERSION,
    CODEBUDDY_RUNTIME_PROFILE,
)
from .claude_usage import claude_usage_facts
from .identity import _client
from .local_config import (
    DEFAULT_BENCHMARK, HOME, _load_config, runtime_config,
    tasks_root_from_config,
)
from .machine import acquire_run_lock, sweep_orphan_compose
from .patch_guard import check_pompeii_patch, format_patch_guard_report
from .providers import (
    ANTIGRAVITY_AGENT,
    ANTIGRAVITY_ARTIFACT_CAPTURE,
    ANTIGRAVITY_CAPABILITY,
    ANTIGRAVITY_PROVIDER,
    ANTIGRAVITY_RUN_CONFIG_VERSION,
    ANTIGRAVITY_RUNTIME_PROFILE,
    CLAUDE_AGENT,
    CLAUDE_CLI_VERSION,
    CLAUDE_RUN_CONFIG_VERSION,
    CLAUDE_RUNTIME_PROFILE,
    DEEPSEEK_CATALOG_SHA256,
    DEEPSEEK_PROVIDER,
    DEEPSEEK_RUN_CONFIG_VERSION,
    DEEPSEEK_RUNTIME_PROFILE,
    DSH_AGENT,
    DSH_RUN_CONFIG_VERSION,
    DSH_RUNTIME_PROFILE,
    GROK_AGENT,
    GROK_MODEL,
    GROK_PROVIDER,
    GROK_RUN_CONFIG_VERSION,
    GROK_RUNTIME_PROFILE,
    KIMI_AGENT,
    KIMI_RUN_CONFIG_VERSION,
    KIMI_RUNTIME_PROFILE,
    HONEY_CHILD_AGENT_ACCESS,
    HONEY_EXECUTION_SECURITY_PROFILE,
    HONEY_INNER_PERMISSION_MODE,
    HONEY_OUTER_ISOLATION,
    HONEY_SECURITY_AGENTS,
    ZCODE_AGENT,
    ZCODE_RUN_CONFIG_VERSION,
    ZCODE_RUNTIME_PROFILE,
    PAID_API_REFILL_AGENTS,
    SUBSCRIPTION_REFILL_AGENTS,
    assignment_codex_provider,
    prepare_antigravity_auth,
    validate_refill_scope,
)
from .runner import (
    CODEX_TRAJECTORY_BUNDLE_SCHEMA, DIAG_ADVICE,
    BuildDiskFullError, BuildFlakeError, BuildSnapshotterPermissionError,
    RunnerError,
    RunnerCleanupUnconfirmedError, RunnerTaskRetryableError,
    POMPEII_BENCHMARK_ID,
    POMPEII_FINALIZATION_RESERVE_SEC, POMPEII_SOFT_BUDGET_SEC,
    POMPEII_TERMINAL_HEAVY_TIMEOUT_SEC,
    build_codex_trajectory_bundle, build_kimi_trajectory_bundle,
    check_task_content_hash, classify_exception_message,
    codex_trajectory_bundle_usage,
    diagnose_exception, ensure_pier, ensure_tasks_root,
    local_deep_swe_commit,
    prepare_pinned_deep_swe_tasks,
    pompeii_agent_timeout_sec, run_trial,
    resolve_environment_build_timeout_multiplier,
    summarize_result, task_content_mismatch_diagnostic,
    trial_artifact_paths,
)
from .scrub import (
    patch_structure_is_valid, redact_patch_secrets, scan_secrets,
    scrub_json_bytes,
)
from .session_archive import archive_after_submit
from .submission_intent import (
    LEGACY_UPLOAD_INTENT_VERSION,
    UPLOAD_INTENT_VERSION,
    submission_payload_manifest,
    upload_intent_id,
)
from .telemetry import RunnerTelemetry
from .taskpacks import TaskPackError, ensure_benchmark_task_pack


# Quota is the user-facing campaign limit. Keep a deliberately high internal
# count ceiling as a last-resort guard against corrupt estimates or a logic
# regression; normal quota-bounded plans should never reach it.
DEFAULT_REFILL_TASK_SAFETY_CAP = 1000
_TERMINAL_LOCAL_OUTCOMES = {
    "assignment-isolated", "assignment-reopened", "not-uploaded", "rejected",
    "task-content-mismatch",
}
_NON_FAULT_RUNNER_OUTCOMES = {
    "submitted", "interrupted", "expired", "assignment-isolated",
    "assignment-reopened",
}
_ACCOUNT_TERMINAL_OUTCOMES = {
    "auth-failure", "insufficient-balance", "quota-exhausted",
    "runtime-incompatible", "provider-preflight-failed",
    "repeat-agent-failure",
}
_POOL_ABORT_ENV = "DRADAR_POOL_ABORT_FILE"
_POOL_TARGET_FILE_ENV = "DRADAR_POOL_TARGET_FILE"
_POOL_FAILURE_CUTOFF_ENV = "DRADAR_POOL_FAILURE_CUTOFF_FILE"
_POOL_RETURNED_ASSIGNMENTS_ENV = "DRADAR_POOL_RETURNED_ASSIGNMENTS_FILE"
_POOL_RETURNED_ASSIGNMENTS_SNAPSHOT_ENV = (
    "DRADAR_POOL_RETURNED_ASSIGNMENTS_SNAPSHOT"
)
_POOL_WORKER_ACTIVITY_ENV = "DRADAR_POOL_WORKER_ACTIVITY_FILE"
_POOL_CAPABILITIES_ENV = "DRADAR_POOL_CAPABILITIES_V1"
_POOL_BACKFILL_V2_ENV = "DRADAR_POOL_BACKFILL_V2"
_REPEAT_FAILURE_STATE_ENV = "DRADAR_REPEAT_FAILURE_STATE_FILE"
_ASSIGNMENT_BOUNDARY_ENV = "DRADAR_ASSIGNMENT_BOUNDARY_FILE"
_PINNED_TASKS_ROOT_ENV = "DRADAR_PINNED_TASKS_ROOT"
_POOL_DRAIN_PREFIX = "drain:"
# EX_TEMPFAIL: a supervised child uses this to distinguish one unsafe slot
# from a generic failure that must freeze the pool's shared waiting queue.
_WORKER_SLOT_QUARANTINED_EXIT_CODE = 75
_POOL_SUPERVISOR_POLL_SECONDS = 0.2
_POOL_BACKFILL_REFRESH_SECONDS = 2.0
_POOL_BACKFILL_ERROR_RETRY_SECONDS = 10.0
_POOL_BACKFILL_MAX_ATTEMPTS = 3
_POOL_BACKFILL_RETRY_BASE_SECONDS = 2.0
_POOL_BACKFILL_RETRY_MAX_SECONDS = 30.0
_SCOPED_REFILL_WAIT_SECONDS = 30.0
# A runner-session admission conflict is different from a broken executable or
# provider preflight: another fresh/stale session may temporarily occupy the
# batch's observation capacity while the held assignment is still safe and
# waiting. Recheck after the server freshness window instead of exhausting the
# generic three-attempt startup budget and permanently serializing the pool.
_POOL_SESSION_CAPACITY_RETRY_SECONDS = 10 * 60
_POOL_IMAGE_CACHE_MAINTENANCE_SECONDS = 15 * 60
_POOL_TARGET_CACHE: dict[Path, int] = {}
_ZCODE_NETWORK_RETRY_DELAY_SECONDS = 2.0


def _retryable_zcode_network_failure(assignment: dict, exc: RunnerError) -> bool:
    diagnostic = exc.failure_diagnostic
    return bool(
        assignment.get("agent") == ZCODE_AGENT
        and isinstance(diagnostic, dict)
        and diagnostic.get("schema") == "dradar-runner-failure-v1"
        and diagnostic.get("zcode_provider_failure_reason") == "network_error"
    )


# Cloudflare's common request-body ceiling is 100 MB. Keep enough headroom
# for multipart boundaries and form fields so an optional trajectory bundle
# cannot strand an otherwise valid patch/result at the proxy edge.
_UPLOAD_BODY_BUDGET_BYTES = 95_000_000
_MULTIPART_OVERHEAD_BUDGET_BYTES = 64 * 1024

_GROK_PREFLIGHT_STDOUT_RE = re.compile(
    r"(?:^|\n)stdout: DRADAR_GROK_PREFLIGHT_FAILURE="
    r"(auth|network|catalog|unknown)(?:\n|$)"
)
_GROK_PREFLIGHT_ADVICE = {
    "auth": (
        "Grok subscription authentication is no longer usable. Run "
        "`dradar provider setup grok`, then `dradar provider status grok` "
        "before resuming."
    ),
    "network": (
        "Grok's live catalog could not be reached. Check this machine's "
        "network/proxy, then run `dradar provider status grok` before resuming."
    ),
    "catalog": (
        "This Grok subscription session cannot currently see grok-4.6. Run "
        "`dradar provider status grok`; reauthenticate if that check confirms "
        "the model is unavailable."
    ),
    "unknown": (
        "Grok's live catalog check failed without a safe diagnostic. Run "
        "`dradar provider status grok` before resuming."
    ),
}

_TERMINAL_FAILURE_OUTCOMES = {
    "auth": ("auth-failure", "agent authentication failed"),
    "insufficient-balance": (
        "insufficient-balance", "paid API balance exhausted",
    ),
    "quota-limit": ("quota-exhausted", "account quota exhausted"),
    "stale-agent": (
        "runtime-incompatible", "agent runtime is incompatible",
    ),
}


def _ensure_selected_tasks_root(tasks_root: Path, benchmark_id: str) -> None:
    # Keep the legacy one-argument seam for DeepSWE tests and third-party
    # wrappers while giving non-default task packs an explicit error path.
    if benchmark_id == DEFAULT_BENCHMARK:
        ensure_tasks_root(tasks_root)
    else:
        ensure_tasks_root(tasks_root, benchmark_id)


def _ensure_egress_runtime(*, probe_connectivity: bool = True) -> None:
    """Finish the only shared image pull before any automatic CLI claim."""

    try:
        if probe_connectivity:
            egress.ensure_egress_runtime_ready(announce=True)
        else:
            egress.prepare_egress_proxy_runtime(announce=True)
    except egress.EgressProxyError as exc:
        raise RunnerError(
            f"Pier egress environment is not ready: {exc}; no task was started"
        ) from exc


def _selected_tasks_root(cfg: dict) -> Path:
    pinned_override = os.environ.get(_PINNED_TASKS_ROOT_ENV)
    if pinned_override:
        return Path(pinned_override)
    benchmark = cfg.get("benchmark") or DEFAULT_BENCHMARK
    if benchmark == DEFAULT_BENCHMARK:
        return tasks_root_from_config(cfg)
    return tasks_root_from_config(cfg, benchmark)


def _run_config(args) -> dict:
    credentials_file = getattr(args, "credentials_file", None)
    try:
        cfg = (
            runtime_config(credentials_file)
            if credentials_file else _load_config()
        )
    except ValueError as exc:
        sys.exit(str(exc))
    scoped_batch = cfg.get("run_plan_batch_id")
    normalized_scope = (
        scoped_batch.replace("-", "").lower()
        if isinstance(scoped_batch, str) else scoped_batch
    )
    if normalized_scope and normalized_scope != getattr(args, "batch_id", None):
        sys.exit("run-plan credentials do not match the exact selected batch")
    if getattr(args, "worker_child", False):
        raw_capabilities = os.environ.get(_POOL_CAPABILITIES_ENV)
        if raw_capabilities is not None:
            try:
                parsed = json.loads(raw_capabilities)
            except json.JSONDecodeError:
                sys.exit("invalid internal worker capability snapshot")
            if (
                not isinstance(parsed, list)
                or len(parsed) > 64
                or any(not isinstance(value, str) for value in parsed)
            ):
                sys.exit("invalid internal worker capability snapshot")
            from .providers import normalize_capabilities

            normalized = normalize_capabilities(parsed)
            if len(normalized) != len(parsed):
                sys.exit("invalid internal worker capability snapshot")
            cfg["client_capabilities"] = normalized
    try:
        args._environment_build_timeout_multiplier = (
            resolve_environment_build_timeout_multiplier(
                getattr(args, "environment_build_timeout_multiplier", None)
                if getattr(args, "environment_build_timeout_multiplier", None)
                is not None
                else cfg.get("environment_build_timeout_multiplier")
            )
        )
    except RunnerError as exc:
        sys.exit(str(exc))
    args._build_cache_mode = _resolve_build_cache_mode(args, cfg)
    return cfg


def _resolve_build_cache_mode(args, cfg: dict) -> str:
    """Select a safe cache policy for this invocation.

    Explicit CLI/config values always win.  If neither is present, a pool
    with more than one local worker uses the host-user-scoped shared BuildKit
    cache so immutable base/install layers are downloaded once; a single
    worker keeps the historical assignment-isolated default.  ``auto`` is
    resolved again after capacity inspection, because its final worker count
    is not known when ``_run_config`` first runs.
    """
    cli_value = getattr(args, "build_cache_mode", None)
    if cli_value is not None:
        return image_cache.normalize_build_cache_mode(cli_value)
    if "build_cache_mode" in cfg:
        return image_cache.configured_build_cache_mode(cfg)
    workers = getattr(args, "workers", 1)
    if workers != "auto":
        try:
            if int(workers) > 1:
                return "shared"
        except (TypeError, ValueError):
            pass
    return image_cache.DEFAULT_BUILD_CACHE_MODE


def _refresh_build_cache_mode(args, cfg: dict) -> None:
    """Re-evaluate the dynamic multi-worker default after ``auto`` sizing."""
    args._build_cache_mode = _resolve_build_cache_mode(args, cfg)


def _is_trajectory_bundle_rejection(exc: ApiError) -> bool:
    """Whether retrying once without the optional bundle is safe.

    A proxy-generated 413 cannot identify which multipart field crossed its
    whole-request limit. The caller invokes this helper only while a bundle
    is present, so removing that optional field is a safe one-shot downgrade;
    a second 413 is still terminal. For application 422s, retain the narrow
    bundle-specific code/text checks so unrelated validation is never bypassed.
    """
    if exc.status_code == 413:
        return True
    if exc.status_code != 422:
        return False
    if exc.code and exc.code.startswith("trajectory_bundle_"):
        return True
    detail = str(exc).lower()
    return "trajectory_bundle" in detail or "trajectory bundle" in detail


def _is_trajectory_bundle_required(exc: ApiError) -> bool:
    """Whether the server rejected a reduced upload for lacking the bundle.

    This is a terminal protocol mismatch for the current payload: the client
    already tried the bundle (or omitted it because it exceeded the safe body
    budget), while the server refuses an answer without one.  Keeping such an
    entry in the pending ledger causes every public worker to retry it forever
    and gradually consumes all real-concurrency slots.
    """
    if exc.status_code != 422:
        return False
    if exc.code == "trajectory_bundle_required":
        return True
    detail = str(exc).lower()
    return (
        "complete trajectory_bundle.json is required" in detail
        or ("trajectory bundle" in detail and "required" in detail)
    )


def _estimated_upload_body_bytes(
    patch: Path,
    trajectory: Path | None,
    result: Path | None,
    trajectory_bundle: Path | None,
    client_meta: dict,
) -> int:
    """Conservatively estimate the multipart request body before submission.

    httpx adds a random boundary plus small per-part headers. A fixed 64 KiB
    allowance is intentionally much larger than that framing for this handful
    of fields, while the 5 MB gap below the proxy ceiling absorbs remaining
    implementation differences.
    """
    total = _MULTIPART_OVERHEAD_BUDGET_BYTES
    total += len(json.dumps(client_meta).encode("utf-8"))
    for artifact in (patch, trajectory, result, trajectory_bundle):
        if artifact is not None and artifact.exists():
            total += artifact.stat().st_size
    return total


def _is_patch_secret_rejection(exc: ApiError) -> bool:
    """A server-side secret rejection must never be bypassed automatically."""
    if exc.status_code != 422:
        return False
    if exc.code in {"patch_secret_detected", "patch_contains_secret"}:
        return True
    return "patch appears to contain secrets" in str(exc).lower()


def _pool_abort_path() -> Path | None:
    raw = os.environ.get(_POOL_ABORT_ENV)
    return Path(raw) if raw else None


def _signal_pool_abort(reason: str, *, interrupt_siblings: bool = True) -> None:
    """Stop this supervised pool, optionally without interrupting paid work.

    The historical marker contained only a reason and meant "interrupt every
    child now".  Keep that interpretation for explicit/legacy stop files, but
    let account-wide terminal conditions request a graceful drain: no worker
    may check out another task and the supervisor may not backfill a vacant
    slot, while siblings that already own a model run are left alone to finish.
    """
    path = _pool_abort_path()
    if path is None:
        return
    try:
        payload = reason if interrupt_siblings else f"{_POOL_DRAIN_PREFIX}{reason}"
        path.write_text(payload)
    except OSError:
        # The worker that observed the terminal condition still stops locally.
        # Never turn a best-effort sibling signal into another task failure.
        pass


def _pool_stop_directive(path: Path | None = None) -> tuple[bool, str] | None:
    """Return ``(interrupt_siblings, reason)`` for the shared stop marker."""
    path = path or _pool_abort_path()
    if path is None or not path.is_file():
        return None
    try:
        value = path.read_text().strip()
    except OSError:
        return True, "another worker stopped the pool"
    if value.startswith(_POOL_DRAIN_PREFIX):
        return False, value.removeprefix(_POOL_DRAIN_PREFIX).strip() or "account stop"
    return True, value or "another worker stopped the pool"


def _pool_abort_reason() -> str | None:
    directive = _pool_stop_directive()
    return directive[1] if directive is not None else None


def _pool_target_file(args=None) -> Path | None:
    raw = (
        getattr(args, "worker_target_file", None) if args is not None else None
    ) or os.environ.get(_POOL_TARGET_FILE_ENV)
    return Path(raw).expanduser() if raw else None


def _read_pool_target(path: Path | None, *, default: int, maximum: int) -> int:
    """Read a live worker target without ever guessing past safe bounds."""
    if path is None:
        return default
    try:
        value = int(path.read_text().strip())
    except (OSError, ValueError):
        print(
            f"worker target file {path} is unavailable or invalid; "
            f"keeping {default} worker(s)", file=sys.stderr,
        )
        return default
    if not 0 <= value <= maximum:
        print(
            f"worker target {value} is outside 0..{maximum}; "
            f"keeping {default} worker(s)", file=sys.stderr,
        )
        return default
    return value


def _worker_slot_is_enabled() -> bool:
    """Whether this child may check out another task after a live resize."""
    path = _pool_target_file()
    if path is None:
        return True
    try:
        slot = int(os.environ.get("DRADAR_WORKER_INDEX", "1"))
        maximum = int(os.environ.get("DRADAR_POOL_MAX_SIZE", "40"))
        previous = int(os.environ.get("DRADAR_POOL_SIZE", str(maximum)))
    except ValueError:
        return False
    previous = _POOL_TARGET_CACHE.get(path, previous)
    target = _read_pool_target(path, default=previous, maximum=maximum)
    _POOL_TARGET_CACHE[path] = target
    return slot <= target


def _sync_worker_refill_target() -> int | None:
    """Make a worker's shared refill plan follow the latest live pool size."""

    path = _pool_target_file()
    if path is None:
        return None
    try:
        maximum = int(os.environ.get("DRADAR_POOL_MAX_SIZE", "40"))
        previous = int(os.environ.get("DRADAR_POOL_SIZE", str(maximum)))
    except ValueError as exc:
        raise refill_plan.RefillError(
            "worker pool size environment is invalid"
        ) from exc
    previous = _POOL_TARGET_CACHE.get(path, previous)
    target = _read_pool_target(path, default=previous, maximum=maximum)
    _POOL_TARGET_CACHE[path] = target
    refill_plan.resize_target(HOME, target)
    return target


def _announce_account_stop(outcome: str) -> None:
    messages = {
        "auth-failure": "agent authentication failed",
        "insufficient-balance": "paid API balance is exhausted",
        "quota-exhausted": "the account quota window is exhausted",
        "runtime-incompatible": "the agent runtime is incompatible",
        "provider-preflight-failed": (
            "the selected subscription provider failed its live check"
        ),
        "repeat-agent-failure": (
            "the same zero-progress agent command failure repeated"
        ),
    }
    reason = messages.get(outcome, "an account-wide stop condition was detected")
    print(
        f"{reason} — stopping this worker before the next task or model run. "
        "The supervised pool will not check out or backfill new work, but "
        "siblings with model runs already in flight are allowed to finish. "
        "Unstarted leases remain untouched; fix or wait for "
        "the condition, then explicitly start `dradar resume` again."
    )


def _repeat_failure_state_path() -> Path | None:
    raw = os.environ.get(_REPEAT_FAILURE_STATE_ENV)
    return Path(raw) if raw else None


def _agent_exit_code(diag: dict) -> int | None:
    structured = diag.get("exit_code")
    if (
        isinstance(structured, int)
        and not isinstance(structured, bool)
        and structured != 0
    ):
        return structured
    text = "\n".join(str(line) for line in diag.get("tail", ()))
    match = re.search(
        r"(?:command\s+failed\s*\(\s*exit|exit(?:ed)?(?:\s+with)?"
        r"(?:\s+(?:status|code))?|return\s*code|returncode|\brc)"
        r"\s*[:=()]?\s*(-?\d+)\b",
        text,
        flags=re.IGNORECASE,
    )
    return int(match.group(1)) if match is not None else None


def _repeat_failure_scope(assignment: dict, codex_cli_version=None) -> str:
    return json.dumps({
        "batch_id": assignment.get("batch_id"),
        "agent": assignment.get("agent") or "codex",
        "provider": assignment.get("provider"),
        "model": assignment.get("model"),
        "agent_version": assignment.get("agent_version"),
        "codex_cli_version": codex_cli_version,
    }, sort_keys=True, separators=(",", ":"))


def _repeat_failure_signature(
    assignment: dict, stats: dict, diag: dict, art,
) -> tuple[str, str] | None:
    """Identify only proven task-independent, zero-progress command exits."""
    if assignment.get("agent") not in {None, "codex"}:
        return None
    if diag.get("type") != "NonZeroAgentExitCodeError":
        return None
    exit_code = _agent_exit_code(diag)
    if exit_code in (None, 0):
        return None
    progress = (
        stats.get("n_agent_steps"), stats.get("n_input_tokens"),
        stats.get("n_cache_tokens"), stats.get("n_output_tokens"),
        stats.get("cost_usd"),
    )
    if any(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and value > 0 for value in progress
    ):
        return None
    # Pier's compact result can be empty even when a partially parsed Codex
    # session proves a model request consumed tokens. Treat that evidence as
    # progress too; the circuit is for truly empty command exits, not merely
    # incomplete aggregate accounting.
    bundle = build_codex_trajectory_bundle(art.trial_dir)
    aggregate = bundle.get("aggregate_usage") if isinstance(bundle, dict) else None
    if isinstance(aggregate, dict) and any(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and value > 0 for value in aggregate.values()
    ):
        return None
    scope = _repeat_failure_scope(
        assignment, getattr(art, "codex_cli_version", None),
    )
    signature = json.dumps({
        "failure_kind": "agent-command-failed",
        "exception_type": diag["type"],
        "exit_code": exit_code,
    }, sort_keys=True, separators=(",", ":"))
    return scope, signature


def _observe_repeat_failure(
    assignment: dict, signature: tuple[str, str] | None, *, success: bool,
    codex_cli_version=None, invocation_id: str | None = None,
) -> bool:
    if signature is None and not success:
        return False
    scope = (
        signature[0] if signature is not None
        else _repeat_failure_scope(assignment, codex_cli_version)
    )
    if _repeat_failure_state_path() is None and invocation_id is not None:
        scope = f"{invocation_id}:{scope}"
    count, opened = failure_circuit.observe(
        scope=scope,
        signature=signature[1] if signature is not None else None,
        state_path=_repeat_failure_state_path(),
    )
    if not opened:
        return False
    reason = f"repeated zero-progress agent command failure ({count} consecutive)"
    _signal_pool_abort(reason, interrupt_siblings=False)
    print(
        f"safety circuit opened after {count} consecutive identical "
        "zero-progress agent command failures; no later waiting task or "
        "automatic refill will start. Existing sibling runs are left alone. "
        "Inspect the local agent login/runtime, then explicitly run "
        "`dradar resume` after it is fixed."
    )
    return True


def _grok_preflight_failure(result_path: Path | None) -> str | None:
    """Read only the adapter's bounded preflight class from Pier output.

    The command text contains the marker template, so accepting an arbitrary
    substring would misclassify every later Grok exception. Pier's explicit
    ``stdout:`` field is the only trusted transport; its value is restricted
    to four non-secret recovery classes.
    """

    if result_path is None or not result_path.is_file():
        return None
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    info = data.get("exception_info")
    if not isinstance(info, dict):
        return None
    message = info.get("exception_message")
    if not isinstance(message, str):
        return None
    match = _GROK_PREFLIGHT_STDOUT_RE.search(message)
    return match.group(1) if match is not None else None


def _terminal_failure_outcome(kind: str | None) -> str | None:
    policy = _TERMINAL_FAILURE_OUTCOMES.get(kind)
    if policy is None:
        return None
    outcome, abort_reason = policy
    _signal_pool_abort(abort_reason, interrupt_siblings=False)
    return outcome


def _fmt_pct(pct: float) -> str:
    """Adaptive precision, mirroring the radar page's price tags exactly so
    the CLI and the cell a volunteer just clicked always show the same
    number."""
    if pct >= 9.95:
        return str(round(pct))
    if pct >= 0.95:
        return f"{pct:.1f}"
    if pct >= 0.005:
        return f"{pct:.2f}"
    return "<0.01"


def _quota_share_line(a: dict) -> str:
    """The estimate's weekly-quota share, per subscription tier. The server's
    est_quota_pct is Plus-denominated; printing it bare made a 20x Pro
    volunteer read a 20x-overstated cost (their web tag said 0.01%, the CLI
    said 0.3% — same dollars, different denominator). When the assignment
    carries the tier windows, convert and show all three so everyone reads
    their own column; otherwise label the denomination instead of implying
    it's universal."""
    if a.get("billing_mode") == "api":
        pricing = a.get("token_pricing") or {}
        model = str(a.get("model") or "").removeprefix("dsh-")
        current = (pricing.get("current") or {}).get(model) or {}
        rates = current.get("usd_per_million") or {}
        band = current.get("band")
        if band in {"peak", "off_peak"} and all(
            key in rates for key in ("input", "cached_input", "output")
        ):
            label = "peak" if band == "peak" else "off-peak (50% of peak)"
            return (
                "pay-as-you-go DeepSeek API billing; current Beijing band: "
                f"{label}; per 1M tokens ${rates['input']} uncached input / "
                f"${rates['cached_input']} cached input / ${rates['output']} output "
                "(peak 09:00–12:00 and 14:00–18:00; not ChatGPT quota)"
            )
        return "pay-as-you-go DeepSeek API billing (not ChatGPT subscription quota)"
    pct = a.get("est_quota_pct")
    if pct is None:
        return "?"
    windows = a.get("tier_windows_usd") or {}
    plus = windows.get("plus")
    if not plus:
        return f"~{pct}% of a weekly (7d) Plus quota window (less on Pro tiers)"
    parts = [f"{label} ~{_fmt_pct(pct * plus / windows[key])}%"
             for key, label in (("plus", "Plus"), ("pro-5x", "5x Pro"),
                                ("pro-20x", "20x Pro")) if windows.get(key)]
    return "share of your weekly (7d) quota: " + " / ".join(parts)


def _print_assignment(a: dict) -> None:
    print(f"assignment {a['assignment_id']}: {a['task_id']}")
    provider = f" provider={a['provider']}" if a.get("provider") else ""
    print(
        f"  model={a['model']} effort={a['effort']} "
        f"agent={a['agent']}{provider}"
    )
    if a.get("est_minutes"):
        # Denominated in the weekly window: Codex removed the 5h rolling
        # limit (2026-07), the 7d quota is the only constraint left.
        print(f"  estimated: ~{a['est_minutes']} min, {_quota_share_line(a)}")
    print(f"  lease expires: {a['expires_at']}")


def _print_menu(menu: list[dict]) -> None:
    for i, m in enumerate(menu, 1):
        est = (
            f"~{m['est_minutes']} min, {_quota_share_line(m)}"
            if m.get("est_minutes") else "?"
        )
        provider = f" provider={m['provider']}" if m.get("provider") else ""
        print(
            f"  {i}. {m['task_id']}  model={m['model']} "
            f"effort={m['effort']}{provider}  est={est}"
        )


def _choose_menu_entry(menu: list[dict], yes: bool) -> dict:
    """Pick an entry from a non-empty menu. Non-interactive (-y) always takes
    the first (hungriest) pick with zero prompting, to keep automation stable.
    Empty input takes the top pick. Invalid input gets one announced re-prompt
    (the claim leases the cell immediately, so a silent fallback would point
    the volunteer's quota at a task they never chose), then falls back to the
    top pick so garbage-piping automation still terminates."""
    if yes:
        return menu[0]
    _print_menu(menu)
    for attempt in range(2):
        raw = input(f"pick a task 1 to {len(menu)}, or press enter for the top pick: ").strip()
        if not raw:
            return menu[0]
        try:
            idx = int(raw)
        except ValueError:
            idx = 0
        if 1 <= idx <= len(menu):
            return menu[idx - 1]
        if attempt == 0:
            print(f"invalid choice '{raw}'")
    print(f"taking the top pick ({menu[0]['task_id']})")
    return menu[0]


def _claim_from_menu(client: ApiClient, menu: list[dict], yes: bool) -> dict | None:
    """Claim a menu entry, retrying once with a fresh menu if it went stale.
    Returns the claimed assignment (or an already-held one, when a 409 meant
    "you already hold an active lease" and get_assignment self-heals), or
    None when no work is available."""
    for attempt in range(2):
        choice = _choose_menu_entry(menu, yes)
        try:
            data = client.claim_assignment(choice["task_id"], choice["model"], choice["effort"])
            return data.get("assignment")
        except ApiError as exc:
            if exc.status_code != 409:
                raise
            if attempt == 1:
                print("no work available right now — thank you, check back later")
                return None
            print(f"that cell went stale ({exc}); fetching a fresh menu...")
            retry = client.get_assignment()
            menu = retry.get("menu")
            if not menu:
                return retry.get("assignment")
    return None


def _parse_pick(spec: str) -> tuple[str, str, str]:
    parts = spec.split(":")
    if len(parts) != 3:
        sys.exit(f"--pick expects task_id:model:effort, got {spec!r}")
    return parts[0], parts[1], parts[2]


class _ConcurrentCapHit(Exception):
    """Raised by _claim_cell when a 409 means the volunteer's own concurrent-
    hold cap, not a stale/taken cell -- every further claim in the same batch
    would fail identically, so callers stop instead of repeating the same
    line N times."""


def _claim_cell(client: ApiClient, task_id: str, model: str, effort: str) -> dict | None:
    """Claim one cell, printing a clear per-cell success/failure line (the
    acceptance bar from volunteer issue #1: an Agent driving this headlessly
    needs to know exactly what landed, not just an aggregate count). A stale/
    taken cell (409) is reported and swallowed -- the caller keeps trying the
    rest of the batch. Everything else (401, the concurrent-hold cap, a
    validation error) propagates: _ConcurrentCapHit to the batch loop,
    anything else to _exit_for."""
    try:
        data = client.claim_assignment(task_id, model, effort)
    except ApiError as exc:
        if exc.status_code != 409:
            raise
        if (exc.code == "claim_limit_reached"
                or (exc.code is None and "already holding" in str(exc))):
            raise _ConcurrentCapHit(str(exc)) from exc
        print(f"  {task_id}/{model}@{effort}: not claimed ({exc})")
        return None
    a = data.get("assignment")
    if a:
        print(f"  {task_id}/{model}@{effort}: claimed")
    return a


def _claim_picks(client: ApiClient, specs: list[str]) -> list[dict]:
    """`dradar go --pick task:model:effort` (repeatable): claim exact cells by
    ID instead of picking from the web or auto-suggesting."""
    claimed = []
    try:
        for task_id, model, effort in (_parse_pick(s) for s in specs):
            a = _claim_cell(client, task_id, model, effort)
            if a is not None:
                claimed.append(a)
    except _ConcurrentCapHit as exc:
        print(f"  stopping — {exc}")
    return claimed


def _top_up_picks(
    client: ApiClient, active: list[dict], specs: list[str],
) -> list[dict]:
    """Claim exact requested cells that are not already held.

    The server remains authoritative for availability and account caps. This
    local filter only prevents a running/held cell or a duplicate CLI flag
    from turning a valid top-up request into an avoidable 409.
    """
    seen = {
        (a.get("task_id"), a.get("model"), a.get("effort"))
        for a in active
    }
    missing = []
    for spec in specs:
        cell = _parse_pick(spec)
        if cell in seen:
            task_id, model, effort = cell
            print(f"  {task_id}/{model}@{effort}: already held; skipping")
            continue
        seen.add(cell)
        missing.append(spec)
    return active + _claim_picks(client, missing)


def _claim_auto(client: ApiClient, n: int) -> list[dict]:
    """`dradar go --auto [N]`: auto-pick + claim up to N cells via the
    server's weighted-random suggester (/api/v1/suggest — the same primitive
    behind the web's 雷达随机推荐 button), so a headless/Agent run never needs
    a prior web claim (volunteer issue #1, 2026-07-15). A suggested cell that
    went stale between suggest and claim (someone else grabbed it first) is
    just skipped, not treated as a failure."""
    cells = client.suggest(n).get("cells") or []
    if not cells:
        print("no eligible cells to auto-pick right now")
        return []
    claimed = []
    try:
        for c in cells:
            a = _claim_cell(client, c["task_id"], c["model"], c["effort"])
            if a is not None:
                claimed.append(a)
    except _ConcurrentCapHit as exc:
        print(f"  stopping — {exc}")
    return claimed


def _exit_for(exc: ApiError) -> None:
    """Exit on a dead-end ApiError in the run flow with a next step, not just
    the raw server error. 401 means the token was reset/clobbered — recoverable
    without support. status_code None means the request never reached the
    server (DNS/connect/timeout), so any held leases are untouched. Everything
    else (e.g. 403 account suspended) carries the server's own explanation
    verbatim."""
    if exc.status_code == 401:
        _signal_pool_abort(
            "DRadar account authentication failed", interrupt_siblings=False,
        )
        sys.exit(f"{exc}\nyour token was rejected — `dradar login --github` recovers a "
                 "linked identity, otherwise grab a fresh token on the radar page")
    if exc.status_code in (402, 403):
        _signal_pool_abort(
            f"DRadar account stopped with HTTP {exc.status_code}",
            interrupt_siblings=False,
        )
        sys.exit(str(exc))
    if exc.status_code == 429:
        _signal_pool_abort(
            "DRadar service rate limit persisted after bounded retries",
            interrupt_siblings=False,
        )
        sys.exit(f"{exc}\nthe server rate limit persisted after bounded retries; "
                 "the supervised pool will stop new checkout/backfill while "
                 "already-running siblings finish")
    if exc.code == "runner_session_capacity_reached":
        _record_worker_precheckout_failure(exc.code)
        sys.exit(f"{exc}\nserver error code: {exc.code}")
    if (
        exc.status_code == 426
        and exc.required_capability == ANTIGRAVITY_CAPABILITY
    ):
        _signal_pool_abort(
            "Antigravity provider is not ready for this batch",
            interrupt_siblings=False,
        )
        _record_worker_precheckout_failure("provider_capability_required")
        sys.exit(
            f"{exc}\nAntigravity is not ready in this DRadar home; run "
            "`dradar provider setup antigravity`, then explicitly rearm any "
            "saved campaign with `dradar refill stop`"
        )
    if exc.status_code is None:
        sys.exit(f"{exc}\ncheck your connection — held leases stay active, and "
                 "`dradar resume` continues where you left off")
    sys.exit(str(exc))


def _check_version_pin(
    pinned: str | None,
    tasks_root: Path,
    allow_drift: bool,
    *,
    return_tasks_root: bool = False,
) -> str | None | tuple[Path, str | None]:
    """Refuse to burn real quota on a checkout the server won't grade the same
    way. The lease stays active across the exit."""
    local_commit = local_deep_swe_commit(tasks_root)
    if pinned and local_commit and local_commit != pinned:
        # Never mutate the volunteer's configured checkout. It may contain their
        # own edits, and another concurrently running batch may need a different
        # grading commit. Build/reuse one immutable DRadar-owned snapshot instead.
        print(f"deep-swe drifted (local {local_commit[:12]} != server {pinned[:12]}); "
              "preparing an isolated verified task snapshot...")
        snapshot_error = None
        try:
            pinned_root = prepare_pinned_deep_swe_tasks(HOME, pinned)
        except RunnerError as exc:
            pinned_root = None
            snapshot_error = str(exc)
        if not allow_drift:
            if pinned_root is None:
                sys.exit(
                    "couldn't prepare a verified task environment for this run; "
                    "your configured task files were left unchanged. Check Git, "
                    "network access, and free disk space, then retry"
                    + (f" ({snapshot_error})" if snapshot_error else "")
                )
        if pinned_root is not None:
            print(f"  isolated task snapshot ready at {pinned[:12]}")
            return (pinned_root, pinned) if return_tasks_root else pinned
        print(
            f"warning: proceeding with task drift (local {local_commit[:12]} != "
            f"server {pinned[:12]}); the submission will be flagged for review"
        )
    return (tasks_root, local_commit) if return_tasks_root else local_commit


def _version_pinned_tasks_root(
    pinned: str | None, tasks_root: Path, allow_drift: bool,
) -> tuple[Path, str | None]:
    """Internal adapter that preserves the historical pin-check test seam."""

    result = _check_version_pin(
        pinned, tasks_root, allow_drift, return_tasks_root=True,
    )
    if isinstance(result, tuple):
        return result
    # Older embedders/tests may replace the historical helper with a callable
    # returning only the commit (or None). Keep their configured root intact.
    return tasks_root, result


# The trial-dir artifact layout is owned by runner (pier writes it); retry
# reconstructs paths from a bare trial_dir via the same single source of truth.
_artifacts_from_trial_dir = trial_artifact_paths


def _apply_usage_to_result(result_path: Path, usage: dict) -> None:
    """Replace Pier's missing/arbitrary token totals in an upload copy.

    The raw result remains untouched for retry/debugging.  Clearing cost_usd
    is intentional: the server owns the model price table and recomputes the
    cost from these normalized aggregate token counters.
    """
    try:
        payload = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    agent_result = payload.get("agent_result")
    if not isinstance(agent_result, dict):
        agent_result = {}
        payload["agent_result"] = agent_result
    agent_result["cost_usd"] = None
    complete = bool(usage.get("complete"))
    for result_key, usage_key in (
        ("n_input_tokens", "n_input_tokens"),
        ("n_cache_tokens", "n_cache_tokens"),
        ("n_output_tokens", "n_output_tokens"),
    ):
        agent_result[result_key] = usage.get(usage_key) if complete else None
    metadata = agent_result.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        agent_result["metadata"] = metadata
    metadata["provider_usage"] = usage
    if "codex" in str(usage.get("schema", "")):
        metadata["codex_session_usage"] = usage
    result_path.write_text(json.dumps(payload, ensure_ascii=False))


# Kept for compatibility with callers/tests that used the earlier narrow name.
_apply_codex_usage_to_result = _apply_usage_to_result


def _dsh_trial_usage(trial_dir: Path) -> dict | None:
    """Read DSH's provider-reported, per-request de-duplicated usage sidecar.

    DSH defines ``inputTokens`` as uncached input and exposes cache reads and
    writes as disjoint buckets. DRadar's upload contract uses total prompt
    tokens plus the cache-read subset, so cache writes remain billed as normal
    input while cache reads receive the server's cached-input price.
    """
    path = trial_dir / "agent" / "dsh-home" / "dsh-usage.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema") != "dsh-provider-usage-v2":
        return None
    names = (
        "uncachedInputTokens", "cacheReadTokens", "cacheWriteTokens",
        "outputTokens", "requestCount",
    )
    if any(
        not isinstance(value.get(name), int)
        or isinstance(value.get(name), bool)
        or value[name] < 0
        for name in names
    ) or value["requestCount"] < 1:
        return None
    n_input = (
        value["uncachedInputTokens"]
        + value["cacheReadTokens"]
        + value["cacheWriteTokens"]
    )
    requests = value.get("requests")
    if (not isinstance(requests, list)
            or len(requests) != value["requestCount"]):
        return None
    token_usage_events = []
    request_totals = {name: 0 for name in names[:-1]}
    for request in requests:
        if not isinstance(request, dict):
            return None
        occurred_at = request.get("occurredAt")
        try:
            instant = datetime.fromisoformat(
                occurred_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return None
        if instant.tzinfo is None:
            return None
        if any(
            not isinstance(request.get(name), int)
            or isinstance(request.get(name), bool)
            or request[name] < 0
            for name in names[:-1]
        ):
            return None
        for name in names[:-1]:
            request_totals[name] += request[name]
        token_usage_events.append({
            "occurred_at": occurred_at,
            "n_input_tokens": (
                request["uncachedInputTokens"]
                + request["cacheReadTokens"]
                + request["cacheWriteTokens"]
            ),
            "n_cache_tokens": request["cacheReadTokens"],
            "n_output_tokens": request["outputTokens"],
        })
    if any(request_totals[name] != value[name] for name in names[:-1]):
        return None
    return {
        "schema": value["schema"],
        "complete": True,
        "model": value.get("model"),
        "request_count": value["requestCount"],
        "uncached_input_tokens": value["uncachedInputTokens"],
        "cache_read_tokens": value["cacheReadTokens"],
        "cache_write_tokens": value["cacheWriteTokens"],
        "n_input_tokens": n_input,
        "n_cache_tokens": value["cacheReadTokens"],
        "n_output_tokens": value["outputTokens"],
        "token_usage_events": token_usage_events,
        "timed_usage_complete": True,
    }


def _subscription_trial_usage(trial_dir: Path, meta: dict) -> dict | None:
    """Read normalized usage or a structurally checked observed ledger."""

    path = trial_dir / "agent" / "provider-usage.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    expected_provider = (
        "claude-code" if meta.get("claude_cli_version")
        else "antigravity" if meta.get("antigravity_cli_version")
        else "zcode" if meta.get("zcode_cli_version")
        else "kimi-code" if meta.get("kimi_cli_version")
        else "grok" if meta.get("grok_cli_version")
        else "codebuddy" if meta.get("codebuddy_cli_version")
        else None
    )
    if (
        expected_provider is None
        or not isinstance(value, dict)
        or value.get("schema") != "dradar-subscription-provider-usage-v1"
        or value.get("provider") != expected_provider
    ):
        return None
    complete = value.get("complete") is True
    incomplete_reason = value.get("usage_incomplete_reason")
    allowed_incomplete_reasons = {
        "claude-code": {"request_ledger_unavailable_or_invalid"},
        "antigravity": {
            "terminal_aggregate_missing_or_inconsistent",
            "request_ledger_unavailable_or_invalid",
        },
        "zcode": {"provider_aggregate_missing_or_invalid"},
        "grok": {
            "terminal_aggregate_missing_or_inconsistent",
            "request_ledger_unavailable_or_invalid",
        },
        "kimi-code": {
            "turn_completion_ledger_mismatch",
            "request_ledger_unavailable_or_invalid",
        },
        "codebuddy": {
            "terminal_aggregate_missing_or_inconsistent",
            "request_ledger_unavailable_or_invalid",
        },
    }
    if (not complete and (
            value.get("complete") is not False
            or incomplete_reason not in allowed_incomplete_reasons[expected_provider])):
        return None
    names = ("n_input_tokens", "n_cache_tokens", "n_output_tokens")
    if any(
        not isinstance(value.get(name), int)
        or isinstance(value.get(name), bool)
        or value[name] < 0
        for name in names
    ) or value["n_cache_tokens"] > value["n_input_tokens"]:
        return None
    request_count = value.get("request_count")
    if (not isinstance(request_count, int) or isinstance(request_count, bool)
            or request_count < (1 if complete else 0)):
        return None
    timed = value.get("timed_usage_complete") is True
    if not complete and timed:
        return None
    observed = value.get("request_usage_observed") is True
    if observed and value.get("usage_evidence_tier") not in {
        "complete_reconciled", "observed_unreconciled",
    }:
        return None
    if complete and not observed:
        # Current adapters always attest that a complete reconciled ledger was
        # observed. Retain compatibility with the immediately preceding
        # sidecar schema, which did not carry the explicit flag.
        observed = "request_usage_observed" not in value
    request_complete = (
        value.get("request_usage_complete") is True
        or ("request_usage_complete" not in value and timed)
    )
    events = value.get("token_usage_events")
    if request_complete or observed:
        if not isinstance(events, list) or len(events) != request_count:
            return None
        totals = {name: 0 for name in names}
        for event in events:
            if not isinstance(event, dict):
                return None
            if timed:
                try:
                    instant = datetime.fromisoformat(
                        event["occurred_at"].replace("Z", "+00:00")
                    )
                except (AttributeError, KeyError, TypeError, ValueError):
                    return None
                if instant.tzinfo is None:
                    return None
            if any(
                not isinstance(event.get(name), int)
                or isinstance(event.get(name), bool)
                or event[name] < 0
                for name in names
            ) or event["n_cache_tokens"] > event["n_input_tokens"]:
                return None
            for name in names:
                totals[name] += event[name]
        if any(totals[name] != value[name] for name in names):
            return None
    else:
        events = []
    return {
        **value,
        "token_usage_events": events,
        "request_usage_complete": request_complete,
        "request_usage_observed": observed,
        "timed_usage_complete": timed,
    }


def _claude_trial_usage_from_trajectory(
    trial_dir: Path, expected_model: str, *, attempts: int = 10,
    retry_delay: float = 0.2,
) -> dict | None:
    """Rebuild Claude usage from the exact ATIF artifact being uploaded.

    The Pier adapter normally writes ``provider-usage.json`` next to the
    trajectory.  That sidecar is a convenience, not the authority: retry and
    cleanup paths are required to remain able to derive the same fail-closed
    ledger from the preserved trajectory itself.
    """

    if not expected_model:
        return None
    _patch, trajectory_path, _result = trial_artifact_paths(trial_dir)
    if trajectory_path is None:
        return None
    for attempt in range(max(1, attempts)):
        try:
            trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            trajectory = None
        if trajectory is not None:
            usage = claude_usage_facts(trajectory, expected_model)
            if usage is not None:
                return usage
            # A complete JSON document with the wrong model or irreconcilable
            # totals will not become trustworthy by waiting. Keep that case
            # fail-closed rather than masking it as a filesystem race.
            return None
        if attempt + 1 < attempts and retry_delay > 0:
            time.sleep(retry_delay)
    return None


def _dsh_completed_outcome(
    trial_dir: Path, patch: Path, result: Path | None,
) -> dict | None:
    """Return pinned DSH completion evidence after a Pier post-run failure.

    Pier's process can fail after the in-container agent has completed and the
    task hook has harvested a valid patch.  That outer rc must not discard paid
    work, but a non-empty patch alone is insufficient: API failures can leave a
    partial or empty artifact too.  The pinned headless runner therefore writes
    a minimal terminal sidecar before Pier starts post-run collection.  Recover
    only when that sidecar, result timestamps, exception state, and git diff all
    independently agree that the agent completed.
    """
    outcome_path = trial_dir / "agent" / "dsh-home" / "dsh-outcome.json"
    try:
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        result_value = json.loads(result.read_text(encoding="utf-8")) if result else None
        patch_bytes = patch.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(outcome, dict) or not isinstance(result_value, dict):
        return None
    request_count = outcome.get("requestCount")
    if (
        outcome.get("schema") != "dradar-dsh-outcome-v1"
        or outcome.get("terminalKind") != "completed"
        or outcome.get("agentCompleted") is not True
        or outcome.get("errorCode") is not None
        or not isinstance(request_count, int)
        or isinstance(request_count, bool)
        or request_count < 1
    ):
        return None
    agent_execution = result_value.get("agent_execution")
    if (
        result_value.get("exception_info")
        or not result_value.get("started_at")
        or not result_value.get("finished_at")
        or not isinstance(agent_execution, dict)
        or not agent_execution.get("started_at")
        or not agent_execution.get("finished_at")
        or not patch_bytes
        or not patch_structure_is_valid(patch_bytes)
    ):
        return None
    return {
        "schema": outcome["schema"],
        "terminal_kind": outcome["terminalKind"],
        "request_count": request_count,
    }


def _grok_completed_outcome(
    assignment: dict, trial_dir: Path, patch: Path, result: Path | None,
) -> dict | None:
    """Return strict Grok completion evidence after a nonzero outer rc.

    Grok's adapter writes three independent views before Pier post-processing:
    the task result, an ATIF trajectory, and the reconciled provider ledger.
    Require all three to agree with the leased identity and the harvested diff;
    a nonzero rc by itself is never evidence that paid work completed.
    """

    if (
        assignment.get("agent") != GROK_AGENT
        or assignment.get("provider") != GROK_PROVIDER
        or assignment.get("model") != GROK_MODEL
    ):
        return None
    trajectory_path = trial_dir / "agent" / "trajectory.json"
    try:
        result_value = json.loads(result.read_text(encoding="utf-8")) if result else None
        trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
        patch_bytes = patch.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(result_value, dict) or not isinstance(trajectory, dict):
        return None

    def parse_instant(value: object) -> datetime | None:
        try:
            instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return instant if instant.tzinfo is not None else None

    overall_start = parse_instant(result_value.get("started_at"))
    overall_finish = parse_instant(result_value.get("finished_at"))
    phases = []
    for name in ("environment_setup", "agent_setup", "agent_execution"):
        phase = result_value.get(name)
        if not isinstance(phase, dict):
            return None
        started = parse_instant(phase.get("started_at"))
        finished = parse_instant(phase.get("finished_at"))
        if started is None or finished is None or finished < started:
            return None
        phases.append((started, finished))
    if (
        result_value.get("exception_info")
        or overall_start is None
        or overall_finish is None
        or overall_finish < overall_start
        or phases[0][0] < overall_start
        or any(phases[index][0] < phases[index - 1][1] for index in range(1, 3))
        or phases[-1][1] > overall_finish
        or not patch_bytes
        or not patch_structure_is_valid(patch_bytes)
        or scan_secrets(patch_bytes)
    ):
        return None

    expected_version = assignment.get("agent_version")
    agent = trajectory.get("agent")
    agent_extra = agent.get("extra") if isinstance(agent, dict) else None
    steps = trajectory.get("steps")
    metrics = trajectory.get("final_metrics")
    if (
        trajectory.get("schema_version") != "ATIF-v1.7"
        or not isinstance(trajectory.get("session_id"), str)
        or not trajectory["session_id"]
        or not isinstance(agent, dict)
        or agent.get("name") != GROK_AGENT
        or agent.get("model_name") != assignment["model"]
        or not isinstance(expected_version, str)
        or not expected_version
        or agent.get("version") != expected_version
        or not isinstance(agent_extra, dict)
        or agent_extra.get("provider") != assignment["provider"]
        or agent_extra.get("oauth") is not True
        or not isinstance(steps, list)
        or not steps
        or not isinstance(metrics, dict)
    ):
        return None
    for index, step in enumerate(steps, start=1):
        if (
            not isinstance(step, dict)
            or step.get("step_id") != index
            or step.get("source") not in {"agent", "user"}
            or not isinstance(step.get("message"), str)
            or not step["message"]
        ):
            return None
        if step["source"] == "agent" and (
            step.get("model_name") != assignment["model"]
            or step.get("reasoning_effort") != assignment.get("effort")
            or step.get("llm_call_count") != 1
        ):
            return None
    if not any(step["source"] == "agent" for step in steps):
        return None

    usage = _subscription_trial_usage(
        trial_dir, {"grok_cli_version": expected_version},
    )
    names = ("n_input_tokens", "n_cache_tokens", "n_output_tokens")
    metric_names = {
        "n_input_tokens": "total_prompt_tokens",
        "n_cache_tokens": "total_cached_tokens",
        "n_output_tokens": "total_completion_tokens",
    }
    agent_result = result_value.get("agent_result")
    embedded_usage = (
        (agent_result.get("metadata") or {}).get("provider_usage")
        if isinstance(agent_result, dict) else None
    )
    if (
        usage is None
        or usage.get("schema") != "dradar-subscription-provider-usage-v1"
        or usage.get("provider") != "grok"
        or usage.get("model") != assignment["model"]
        or usage.get("complete") is not True
        or usage.get("request_usage_complete") is not True
        or usage.get("request_usage_observed") is not True
        or not isinstance(usage.get("request_count"), int)
        or isinstance(usage.get("request_count"), bool)
        or usage["request_count"] < 1
        or metrics.get("total_steps") != len(steps)
        or not isinstance(agent_result, dict)
        or agent_result.get("n_agent_steps") != len(steps)
        or not isinstance(embedded_usage, dict)
        or any(
            metrics.get(metric_names[name]) != usage.get(name)
            or agent_result.get(name) != usage.get(name)
            or embedded_usage.get(name) != usage.get(name)
            for name in names
        )
        or embedded_usage.get("schema") != usage.get("schema")
        or embedded_usage.get("provider") != usage.get("provider")
        or embedded_usage.get("model") != usage.get("model")
        or embedded_usage.get("complete") is not True
        or embedded_usage.get("request_count") != usage.get("request_count")
    ):
        return None
    return {
        "schema": "dradar-grok-completion-v1",
        "provider": usage["provider"],
        "model": usage["model"],
        "agent_version": expected_version,
        "trajectory_schema": trajectory["schema_version"],
        "session_id": trajectory["session_id"],
        "request_count": usage["request_count"],
        "n_agent_steps": len(steps),
    }


def _bundled_completed_outcome(
    assignment: dict, trial_dir: Path, patch: Path, result: Path | None,
) -> dict | None:
    """Return independent completion evidence for a Codex-family rc failure."""
    if assignment.get("agent") in {
        DSH_AGENT, GROK_AGENT, KIMI_AGENT, CODEBUDDY_AGENT,
    }:
        return None
    try:
        result_value = json.loads(result.read_text(encoding="utf-8")) if result else None
        patch_bytes = patch.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(result_value, dict):
        return None
    phases = (
        result_value.get("environment_setup"),
        result_value.get("agent_setup"),
        result_value.get("agent_execution"),
    )
    if (
        result_value.get("exception_info")
        or not result_value.get("started_at")
        or not result_value.get("finished_at")
        or not isinstance(result_value.get("agent_result"), dict)
        or not all(
            isinstance(phase, dict)
            and phase.get("started_at")
            and phase.get("finished_at")
            for phase in phases
        )
        or not patch_bytes
        or not patch_structure_is_valid(patch_bytes)
    ):
        return None
    bundle = build_codex_trajectory_bundle(trial_dir)
    usage = codex_trajectory_bundle_usage(bundle) if bundle is not None else None
    if usage is None:
        return None
    if usage.get("complete") is True:
        return {
            "schema": "dradar-bundled-completion-v1",
            "usage_schema": usage.get("schema"),
            "agent_session_count": usage.get("agent_session_count"),
            "root_session_count": usage.get("root_session_count"),
            "subagent_session_count": usage.get("subagent_session_count"),
        }
    if bundle.get("parse_degraded_completion_eligible") is not True:
        return None
    parse_error_count = bundle.get("parse_error_count")
    if (
        not isinstance(parse_error_count, int)
        or isinstance(parse_error_count, bool)
        or parse_error_count < 1
    ):
        return None
    return {
        "schema": "dradar-bundled-completion-v2",
        "evidence_mode": "single-root-terminal-parse-degraded",
        "usage_schema": usage.get("schema"),
        "agent_session_count": usage.get("agent_session_count"),
        "root_session_count": usage.get("root_session_count"),
        "subagent_session_count": usage.get("subagent_session_count"),
        "parse_error_count": parse_error_count,
    }


def _upload_trial(
    client: ApiClient, entry: dict, *, ask_cleanup: bool = False,
    request_salvage: bool = False,
) -> str:
    """Scrub + upload one trial's artifacts, described by a pending-ledger
    entry dict (assignment_id/nonce/task_id/trial_dir/meta/outcome/job_dir/
    keep) — the same shape the ledger round-trips, so what persists on failure
    is identical by construction to what was attempted. Shared by the normal
    post-run path and by `dradar retry-upload` (which passes loaded ledger
    entries straight through). Never exits — returns an outcome tag so
    callers (the held-batch loop, a retry scan) can carry on with the next
    item.

    The entry is recorded in the local pending-upload ledger BEFORE artifact
    staging or the submit attempt, so a process death during either handoff
    can't orphan a completed, quota-burning trial.
    Every exit settles it: success, 409 "already submitted", and 410 remove
    the entry; fencing conflicts and transient errors keep it for retry. The
    raw source patch is preserved separately until server acknowledgement;
    scrubbing writes to a fresh tempdir, so a later retry re-scrubs from the
    same byte-verified original."""
    entry = dict(entry)
    assignment_id = entry["assignment_id"]
    task_id = entry.get("task_id", "?")
    blocked_reason = entry.get("upload_blocked")
    salvage_requested = request_salvage and blocked_reason == "owner_superseded"
    if request_salvage and not salvage_requested:
        pending.record(HOME, entry)
        print(
            f"  {task_id}: explicit salvage only applies to an "
            "owner_superseded completed upload; no state was changed"
        )
        return "upload-blocked"
    if blocked_reason and not salvage_requested:
        # A persisted block is a terminal *automatic* recovery decision, not
        # a transient upload error.  Keep both the ledger row and artifacts so
        # the paid result can be inspected explicitly, but never restage it or
        # contact the server again.  pending.assignment_ids() deliberately
        # continues to fence every model-start entry point for this assignment.
        pending.record(HOME, entry)
        print(
            f"  {task_id}: upload is blocked ({blocked_reason}); local evidence "
            "was kept, and this assignment will neither be retried nor run "
            "again automatically"
        )
        return "upload-blocked"
    outcome = entry.get("outcome", "completed")
    trial_dir = Path(entry["trial_dir"])
    job_dir = Path(entry["job_dir"]) if entry.get("job_dir") else trial_dir.parent
    jobs_root = (HOME / "work" / "jobs").resolve()
    if not entry.get("job_dir"):
        inferred = job_dir.resolve()
        if inferred == jobs_root or jobs_root not in inferred.parents:
            # Old ledgers may omit job_dir. Only infer it from the canonical
            # jobs tree; never let a crafted trial_dir turn its parent into a
            # cleanup target.
            job_dir = None

    def cleanup_settled() -> None:
        # During an interactive completed run, keep the current directory
        # just long enough to ask the volunteer. Older duplicate job trees
        # are removed immediately.
        keep_dir = job_dir if (entry.get("keep", False) or ask_cleanup) else None
        try:
            local_jobs.cleanup_assignment(
                HOME, assignment_id, keep_job_dir=keep_dir,
            )
        except ValueError:
            # External developer/test job roots are never cleanup authority.
            pass

    def settle_terminal_local_failure() -> None:
        """Keep evidence but make a non-retryable local result runnable again."""
        _mark_stopped_quietly(client, entry)
        if job_dir and job_dir.is_dir():
            try:
                local_jobs.mark_kept(HOME, job_dir, terminal=True)
            except ValueError:
                pass

    # Persist intent before touching either artifact copy. If this process is
    # interrupted while making the durable source or atomic staged copy, the
    # next go/resume/retry-upload still knows exactly what must be recovered.
    pending.record(HOME, entry)
    try:
        staged = artifact_staging.ensure_staged_patch(trial_dir, entry)
    except artifact_staging.PatchStagingError as exc:
        entry["artifact_staging_failure"] = exc.telemetry()
        pending.record(HOME, entry)
        print(
            f"  {task_id}: model.patch staging blocked ({exc}); both local copies "
            "were left untouched and the upload was kept for retry"
        )
        return "artifact-staging-failed"

    entry.update(staged.ledger_fields)
    entry.pop("artifact_staging_failure", None)
    recovery = staged.recovery_telemetry
    if recovery is not None:
        entry["artifact_staging_recovery"] = recovery
        print(
            f"  {task_id}: recovered model.patch staging "
            f"({recovery['reason']}, {staged.size} bytes)"
        )
    pending.record(HOME, entry)

    patch, trajectory, result = trial_artifact_paths(trial_dir)
    patch = staged.staged

    # Use the byte snapshot verified while the staging lock was held. The
    # multipart request below gets its own temporary file, so a concurrent
    # pause/cleanup cannot change or remove the bytes mid-upload.
    raw_patch = staged.data
    leaked = scan_secrets(raw_patch)
    redacted_patch: bytes | None = None
    redacted_labels: list[str] = []
    if leaked:
        redacted_patch, redacted_labels, unsafe_labels = redact_patch_secrets(raw_patch)
        still_leaked = scan_secrets(redacted_patch)
        if (unsafe_labels or still_leaked
                or not redacted_labels or not patch_structure_is_valid(redacted_patch)):
            labels = sorted(set(unsafe_labels or still_leaked or leaked))
            print(f"patch contains secret-shaped content ({', '.join(labels)}) "
                  "outside safely redactable added lines, or redaction made the diff "
                  f"invalid; not uploaded. Raw evidence kept at {patch}")
            pending.remove(HOME, assignment_id)
            settle_terminal_local_failure()
            return "not-uploaded"
        print(f"patch contained secret-shaped content "
              f"({', '.join(redacted_labels)}); uploading a structurally validated "
              "redacted copy. The raw patch stays local.")

    entry_meta = entry.get("meta") or {}
    is_zcode_pompeii = (
        task_id.startswith(f"{POMPEII_BENCHMARK_ID}-")
        and (
            entry_meta.get("model_runtime_profile") == ZCODE_RUNTIME_PROFILE
            or entry_meta.get("zcode_protocol_version") == 1
        )
    )
    if is_zcode_pompeii:
        guard = check_pompeii_patch(raw_patch)
        if not guard.accepted:
            print(
                f"  {task_id}: ZCode patch preflight blocked upload; the "
                "declared deliverable boundary was violated"
            )
            for line in format_patch_guard_report(guard):
                print(f"    {line}")
            print(f"    raw artifact preserved at: {patch}")
            print(
                "    assignment reopened for an independent ZCode re-solve; "
                "no prior model answer is reused"
            )
            pending.remove(HOME, assignment_id)
            settle_terminal_local_failure()
            # This guard is assignment-local: the model completed normally,
            # but did not produce the one allowed deliverable.  The server has
            # reopened this cell for an independent attempt, so a supervised
            # worker may safely exclude it for this session and continue with
            # another waiting cell.  Keep transport, auth, secret, and payload
            # rejections on the fail-closed ``rejected`` path below.
            return "assignment-reopened"

    upload_meta = dict(entry.get("meta") or {})
    trial_dir = Path(entry["trial_dir"])
    # Claude Code emits an ATIF trajectory that is intentionally useful for
    # audit, but it is not a Codex session tree. Prefer the adapter's strictly
    # reconciled provider sidecar so the generic Codex bundle parser cannot
    # shadow complete Claude request usage with an incomplete session bundle.
    claude_usage = (
        _subscription_trial_usage(trial_dir, upload_meta)
        if upload_meta.get("claude_cli_version")
        else None
    )
    claude_usage_source = "provider-sidecar" if claude_usage is not None else None
    trajectory_bundle = None
    if not upload_meta.get("codebuddy_cli_version"):
        trajectory_bundle = build_codex_trajectory_bundle(trial_dir)
        if trajectory_bundle is None:
            trajectory_bundle = build_kimi_trajectory_bundle(trial_dir)
    # Pier finalizes Claude's ATIF file in a post-run hook. On some Docker
    # filesystems the directory entry becomes visible a fraction before the
    # final JSON bytes do. Build the audit bundle first, then make a bounded
    # stable read of the exact trajectory that will be uploaded.
    if claude_usage is None and upload_meta.get("claude_cli_version"):
        claude_usage = _claude_trial_usage_from_trajectory(
            trial_dir, str(upload_meta.get("claude_model") or ""),
        )
        if claude_usage is not None:
            claude_usage_source = "uploaded-trajectory"
    usage = claude_usage or (
        codex_trajectory_bundle_usage(trajectory_bundle)
        if (
            trajectory_bundle is not None
            and trajectory_bundle.get("schema_version")
            == CODEX_TRAJECTORY_BUNDLE_SCHEMA
        )
        else None
    )
    if upload_meta.get("dsh_version") and usage is None:
        usage = _dsh_trial_usage(Path(entry["trial_dir"]))
    if usage is None:
        usage = _subscription_trial_usage(Path(entry["trial_dir"]), upload_meta)
    if entry.get("artifact_staging_recovery"):
        upload_meta["artifact_staging_recovery"] = entry["artifact_staging_recovery"]
    if redacted_patch is not None:
        upload_meta["patch_redacted"] = True
        upload_meta["patch_redaction_labels"] = redacted_labels
    if usage is not None:
        upload_meta["cost_usd"] = None
        upload_meta["usage_aggregation"] = usage["schema"]
        upload_meta["usage_aggregation_complete"] = usage["complete"]
        if claude_usage_source is not None:
            upload_meta["claude_usage_source"] = claude_usage_source
        for key in (
            "agent_session_count", "root_session_count",
            "subagent_session_count", "agent_session_usage", "request_count",
            "uncached_input_tokens", "cache_read_tokens", "cache_write_tokens",
            "token_usage_events", "timed_usage_complete",
            "request_usage_complete", "request_usage_observed",
            "timed_usage_incomplete_reason", "usage_aggregate_source",
            "usage_incomplete_reason", "usage_evidence_tier",
            "session_usage_model_request_count", "request_ledger_duplicate_count",
            "request_ledger_source", "session_usage_request_attempt_count",
            "session_usage_request_retry_count",
            "session_usage_compaction_request_count",
            "usage_counters_valid", "session_identity_valid",
            "request_ledger_valid", "turn_ledger_valid",
            "timed_usage_valid", "wire_metadata_count",
            "provider_actual_cost_observed", "cost_semantics",
            "completed_turn_count", "turn_prompt_count",
            "cache_creation_tokens", "subscription_reported_cost_usd",
            "subscription_reported_cost_basis", "resume_attempts",
            "thinking_tokens", "provider_runtime_model", "terminal_status",
            "terminal_recovery",
        ):
            source_key = "sessions" if key == "agent_session_usage" else key
            if source_key in usage:
                upload_meta[key] = usage[source_key]
        for key in ("n_input_tokens", "n_cache_tokens", "n_output_tokens"):
            upload_meta[key] = (
                usage[key]
                if (
                    usage["complete"]
                    or (
                        usage.get("provider") != "kimi-code"
                        and usage.get("request_usage_observed") is True
                    )
                )
                else None
            )
    elif upload_meta.get("claude_cli_version"):
        upload_meta["claude_usage_diagnostic"] = (
            "provider-sidecar-and-trajectory-unavailable-or-invalid"
        )

    with tempfile.TemporaryDirectory() as td:
        scrubbed = Path(td)
        upload_patch = scrubbed / "model.patch"
        upload_patch.write_bytes(
            redacted_patch if redacted_patch is not None else raw_patch
        )
        trajectory_bundle_scrubbed = None
        if trajectory_bundle is not None:
            trajectory_bundle_scrubbed = scrubbed / "trajectory_bundle.json"
            serialized = json.dumps(
                trajectory_bundle, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
            try:
                scrubbed_bundle = scrub_json_bytes(serialized)
                json.loads(scrubbed_bundle)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                # The bundle is optional display data.  A redaction bug must
                # not strand an otherwise valid patch/result or make every
                # later `go` retry the same broken local upload forever.
                print(f"  {task_id}: redaction produced a malformed optional "
                      f"trajectory bundle ({exc}); uploading the verified "
                      "result without it")
                trajectory_bundle_scrubbed = None
            else:
                trajectory_bundle_scrubbed.write_bytes(scrubbed_bundle)
        traj_scrubbed = None
        if trajectory:
            traj_scrubbed = scrubbed / "trajectory.json"
            try:
                scrubbed_trajectory = scrub_json_bytes(trajectory.read_bytes())
                value = json.loads(scrubbed_trajectory)
                if not isinstance(value, dict):
                    raise ValueError("top level is not an object")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                print(f"  {task_id}: Pier produced a malformed optional trajectory "
                      f"({exc}); uploading the verified result without it")
                traj_scrubbed = None
            else:
                traj_scrubbed.write_bytes(scrubbed_trajectory)
        result_scrubbed = None
        if result:
            result_scrubbed = scrubbed / "result.json"
            result_scrubbed.write_bytes(scrub_json_bytes(result.read_bytes()))
            if usage is not None:
                _apply_usage_to_result(result_scrubbed, usage)
        # Refresh before submitting: from here on an unacked completed trial
        # has the canonical paths + digest in its ledger entry. The server
        # dedupes replays (409 "already submitted"), so duplicates are safe.
        pending.record(HOME, entry)
        submit_bundle = (
            None if entry.get("omit_trajectory_bundle")
            else trajectory_bundle_scrubbed
        )
        projected_bytes = _estimated_upload_body_bytes(
            upload_patch, traj_scrubbed, result_scrubbed,
            submit_bundle, upload_meta,
        )
        if projected_bytes > _UPLOAD_BODY_BUDGET_BYTES:
            if submit_bundle is not None:
                # Persist before submitting so a crash or later transport
                # failure cannot rebuild and resend the same oversized body.
                entry["omit_trajectory_bundle"] = True
                entry.pop("upload_intent", None)
                pending.record(HOME, entry)
                submit_bundle = None
                print(
                    f"  {task_id}: projected upload is "
                    f"{projected_bytes / 1_000_000:.1f} MB, above the safe "
                    f"{_UPLOAD_BODY_BUDGET_BYTES / 1_000_000:.0f} MB request "
                    "budget; omitting the optional trajectory bundle"
                )
                projected_bytes = _estimated_upload_body_bytes(
                    upload_patch, traj_scrubbed, result_scrubbed,
                    None, upload_meta,
                )
            if projected_bytes > _UPLOAD_BODY_BUDGET_BYTES:
                print(
                    f"  {task_id}: required upload body is "
                    f"{projected_bytes / 1_000_000:.1f} MB, above the safe "
                    f"{_UPLOAD_BODY_BUDGET_BYTES / 1_000_000:.0f} MB request "
                    "budget; kept for retry without allocating the body"
                )
                return "upload-failed"

        if salvage_requested:
            # A salvage owner may itself be reclaimed before its saved upload
            # lands.  Always rebind from the original completed runner, not
            # from that synthetic upload-only session, whose audit event is
            # intentionally insufficient to prove a model run.
            original_source = entry.get("salvaged_from")
            if original_source is None:
                source_session_id = entry.get("runner_session_id")
                source_owner_epoch = entry.get("owner_epoch")
            elif isinstance(original_source, dict):
                source_session_id = original_source.get("runner_session_id")
                source_owner_epoch = original_source.get("owner_epoch")
            else:
                source_session_id = None
                source_owner_epoch = None
            if (
                not isinstance(source_session_id, str)
                or not source_session_id
                or not isinstance(source_owner_epoch, int)
                or isinstance(source_owner_epoch, bool)
                or source_owner_epoch < 0
            ):
                print(
                    f"  {task_id}: saved upload lacks a trustworthy source "
                    "runner identity; explicit salvage refused"
                )
                return "upload-blocked"

            salvage = entry.get("salvage_rebind")
            if salvage is None:
                assignment_response = client.get_assignment()
                active = assignment_response.get("active")
                if active is None:
                    one = assignment_response.get("assignment")
                    active = [one] if one else []
                assignment = next(
                    (
                        item for item in active
                        if item and item.get("assignment_id") == assignment_id
                    ),
                    None,
                )
                if assignment is None:
                    print(
                        f"  {task_id}: assignment is no longer an active lease; "
                        "explicit salvage refused"
                    )
                    return "upload-blocked"
                expected_owner_epoch = assignment.get("owner_epoch")
                if (
                    not isinstance(expected_owner_epoch, int)
                    or isinstance(expected_owner_epoch, bool)
                    or expected_owner_epoch <= source_owner_epoch
                ):
                    print(
                        f"  {task_id}: server did not expose a newer idle owner "
                        "epoch; explicit salvage refused"
                    )
                    return "upload-blocked"
                if assignment.get("started_at") is not None:
                    print(
                        f"  {task_id}: another runner currently owns this "
                        "assignment; explicit salvage refused"
                    )
                    return "upload-blocked"
                salvage = {
                    "source_session_id": source_session_id,
                    "source_owner_epoch": source_owner_epoch,
                    "expected_owner_epoch": expected_owner_epoch,
                    "salvage_session_id": f"salvage-{uuid.uuid4().hex}",
                }
                # Crash safety: persist the exact idempotency identity before
                # asking the server to issue an upload-only owner.
                entry["salvage_rebind"] = salvage
                pending.record(HOME, entry)
            elif not isinstance(salvage, dict):
                print(
                    f"  {task_id}: malformed saved salvage identity; no state "
                    "was changed"
                )
                return "upload-blocked"

            try:
                rebound = client.rebind_submission_upload_salvage(
                    assignment_id,
                    entry["nonce"],
                    str(salvage["source_session_id"]),
                    int(salvage["source_owner_epoch"]),
                    int(salvage["expected_owner_epoch"]),
                    str(salvage["salvage_session_id"]),
                )
                new_owner_epoch = int(rebound["owner_epoch"])
            except ApiError as exc:
                if (
                    exc.status_code == 409
                    and exc.code in {
                        "upload_salvage_owner_changed",
                        "upload_salvage_identity_unavailable",
                    }
                ):
                    # These structured conflicts prove that this exact
                    # one-shot identity cannot become a usable owner.  Forget
                    # only the attempted identity so the user can explicitly
                    # request a fresh one later; keep the paid artifacts and
                    # the terminal automatic-upload block intact.
                    entry.pop("salvage_rebind", None)
                    pending.record(HOME, entry)
                    print(
                        f"  {task_id}: the saved salvage request is no longer "
                        "usable; request salvage explicitly again after "
                        "checking that the assignment is idle"
                    )
                    return "upload-blocked"
                print(
                    f"  {task_id}: explicit upload salvage was refused or "
                    f"could not complete ({exc}); local evidence remains blocked"
                )
                return "upload-blocked"
            except (KeyError, TypeError, ValueError) as exc:
                print(
                    f"  {task_id}: explicit upload salvage was refused or "
                    f"could not complete ({exc}); local evidence remains blocked"
                )
                return "upload-blocked"

            entry["salvaged_from"] = {
                "runner_session_id": source_session_id,
                "owner_epoch": source_owner_epoch,
            }
            entry["runner_session_id"] = str(salvage["salvage_session_id"])
            entry["owner_epoch"] = new_owner_epoch
            entry["ledger_version"] = 3
            entry.pop("upload_blocked", None)
            entry.pop("upload_intent", None)
            upload_meta["upload_salvage_recovery"] = {
                "schema": "dradar-upload-salvage-v1",
                "source_owner_epoch": source_owner_epoch,
                "rebound_owner_epoch": new_owner_epoch,
            }
            entry["meta"] = upload_meta
            pending.record(HOME, entry)
            print(
                f"  {task_id}: server authorized an upload-only salvage owner; "
                "submitting the saved result without rerunning the model"
            )

        while True:
            submit_kwargs = {
                "outcome": outcome,
            }
            if submit_bundle is not None:
                submit_kwargs["trajectory_bundle"] = submit_bundle
            runner_session_id = entry.get("runner_session_id")
            if runner_session_id:
                manifest_kwargs = {
                    "assignment_id": assignment_id,
                    "session_id": runner_session_id,
                    "outcome": outcome,
                    "meta": upload_meta,
                    "patch": upload_patch,
                    "trajectory": traj_scrubbed,
                    "result": result_scrubbed,
                    "trajectory_bundle": submit_bundle,
                }
                saved_intent = entry.get("upload_intent")
                legacy_entry = "owner_epoch" not in entry
                intent_already_registered = False
                legacy_manifest = submission_payload_manifest(
                    **manifest_kwargs,
                    resume_generation=int(entry.get("resume_generation", 0)),
                )
                legacy_intent_id = upload_intent_id(legacy_manifest)
                if legacy_entry:
                    # Existing ledgers are migrated without guessing whether
                    # the pre-upgrade registration response was lost. Replaying
                    # the deterministic v2 identity first is authoritative:
                    # success means it already exists/is current; a structured
                    # stale-owner response means the server proved it does not
                    # exist and only then may v3 reconciliation begin.
                    if saved_intent is not None and (
                        not isinstance(saved_intent, dict)
                        or saved_intent.get("id") != legacy_intent_id
                        or saved_intent.get("manifest") != legacy_manifest
                    ):
                        print(
                            f"  {task_id}: saved legacy upload identity no longer "
                            "matches its durable artifacts; kept for explicit review"
                        )
                        entry["upload_blocked"] = "legacy_content_identity_changed"
                        pending.record(HOME, entry)
                        return "upload-blocked"
                    try:
                        client.register_submission_upload_intent(
                            assignment_id,
                            entry["nonce"],
                            runner_session_id,
                            None,
                            legacy_intent_id,
                            resume_generation=int(entry.get("resume_generation", 0)),
                            intent_version=LEGACY_UPLOAD_INTENT_VERSION,
                        )
                    except ApiError as legacy_exc:
                        migratable = (
                            legacy_exc.status_code == 409
                            and (
                                legacy_exc.code == "owner_protocol_upgrade_required"
                                or "stale recovery generation" in str(legacy_exc).lower()
                            )
                        )
                        if not migratable:
                            if legacy_exc.status_code == 410:
                                print(
                                    f"  {task_id}: lease expired before its saved "
                                    "upload could be reconciled; local evidence kept"
                                )
                                pending.remove(HOME, assignment_id)
                                cleanup_settled()
                                return "expired"
                            print(
                                f"  {task_id}: legacy upload reconciliation failed "
                                f"({legacy_exc}); kept for retry"
                            )
                            return "upload-failed"
                        entry["owner_epoch"] = int(
                            entry.get("resume_generation", 0)
                        )
                        entry["ledger_version"] = 3
                        entry["migrated_from"] = LEGACY_UPLOAD_INTENT_VERSION
                        entry.pop("upload_intent", None)
                        pending.record(HOME, entry)
                        legacy_entry = False
                    else:
                        entry["upload_intent"] = {
                            "id": legacy_intent_id,
                            "manifest": legacy_manifest,
                        }
                        pending.record(HOME, entry)
                        submit_kwargs.update({
                            "session_id": runner_session_id,
                            "resume_generation": int(
                                entry.get("resume_generation", 0)
                            ),
                            "upload_intent_id": legacy_intent_id,
                        })
                        calculated_intent_id = legacy_intent_id
                        manifest = legacy_manifest
                        owner_epoch = None
                        intent_already_registered = True
                if not legacy_entry:
                    owner_epoch = int(entry["owner_epoch"])
                    manifest = submission_payload_manifest(
                        **manifest_kwargs,
                        owner_epoch=owner_epoch,
                    )
                    calculated_intent_id = upload_intent_id(manifest)
                    saved_intent = entry.get("upload_intent")
                    if saved_intent is not None and (
                        not isinstance(saved_intent, dict)
                        or saved_intent.get("id") != calculated_intent_id
                        or saved_intent.get("manifest") != manifest
                    ):
                        print(
                            f"  {task_id}: prepared upload changed after its "
                            "content-bound intent was saved; kept for explicit "
                            "recovery instead of changing the pending result"
                        )
                        entry["upload_blocked"] = "content_identity_changed"
                        pending.record(HOME, entry)
                        return "upload-blocked"
                try:
                    registered_intent_id = (
                        calculated_intent_id
                        if intent_already_registered
                        else client.register_submission_upload_intent(
                            assignment_id,
                            entry["nonce"],
                            runner_session_id,
                            owner_epoch,
                            calculated_intent_id,
                        )
                    )
                except ApiError as exc:
                    if exc.status_code == 410:
                        # The assignment (or its claim batch) expired before
                        # the content-bound recovery fence could be registered.
                        # This is terminal for this one completed run, exactly
                        # like a 410 returned by submit below.  Treating it as
                        # a generic upload transport failure makes supervised
                        # worker pools open a shared failure cutoff and freezes
                        # unrelated work from a newer healthy batch.
                        print(
                            f"  {task_id}: lease or claim batch expired before "
                            "upload recovery could be registered — the cell "
                            "reopened, dropping it"
                        )
                        pending.remove(HOME, assignment_id)
                        cleanup_settled()
                        return "expired"
                    if exc.status_code == 409 and exc.code == "upload_owner_superseded":
                        entry["upload_blocked"] = "owner_superseded"
                        # A previously issued upload-only owner has itself
                        # been superseded.  Its one-shot identity can no
                        # longer be replayed, so a later explicit salvage must
                        # start with a fresh identity and current owner epoch.
                        entry.pop("salvage_rebind", None)
                        pending.record(HOME, entry)
                        print(
                            f"  {task_id}: completed result belongs to an owner "
                            "that was later replaced; it was kept locally and will "
                            "not be retried or run again automatically"
                        )
                        return "upload-blocked"
                    if exc.status_code != 404:
                        print(
                            f"  {task_id}: could not register the content-bound "
                            f"upload recovery intent ({exc}) — kept for retry "
                            "without sending an unfenced submission"
                        )
                        return "upload-failed"
                else:
                    if registered_intent_id != calculated_intent_id:
                        print(
                            f"  {task_id}: server returned a mismatched upload "
                            "intent identity; kept for retry"
                        )
                        return "upload-failed"
                    entry["upload_intent"] = {
                        "id": calculated_intent_id,
                        "manifest": manifest,
                    }
                    pending.record(HOME, entry)
                    submit_kwargs.update({
                        "session_id": runner_session_id,
                        "owner_epoch": owner_epoch,
                        "upload_intent_id": calculated_intent_id,
                    })
            try:
                ack = client.submit(
                    assignment_id, entry["nonce"], upload_patch, traj_scrubbed,
                    result_scrubbed, upload_meta, **submit_kwargs,
                )
                break
            except ApiError as exc:
                if (submit_bundle is not None
                        and _is_trajectory_bundle_rejection(exc)):
                    # The bundle is optional. Persist the downgrade before the
                    # second request so a crash/transport failure cannot make
                    # the next retry rebuild and resend the rejected artifact.
                    entry["omit_trajectory_bundle"] = True
                    entry.pop("upload_intent", None)
                    pending.record(HOME, entry)
                    submit_bundle = None
                    print(
                        f"  {task_id}: upload was rejected while it included the "
                        "optional trajectory bundle; retrying the completed "
                        "result without it"
                    )
                    continue

                if (submit_bundle is None
                        and _is_trajectory_bundle_required(exc)):
                    # An older/strict server can reject the optional bundle
                    # and then reject the one-shot reduced request for not
                    # carrying that same bundle.  No retry can change this
                    # completed payload.  Reopen the cell instead of pinning
                    # a paid worker slot behind an immortal pending entry.
                    print(
                        f"  {task_id}: the server requires a complete trajectory "
                        "bundle after rejecting/omitting this run's bundle; "
                        "releasing the incompatible assignment instead of "
                        "retrying forever"
                    )
                    pending.remove(HOME, assignment_id)
                    settle_terminal_local_failure()
                    print(
                        "  rejected artifacts kept for diagnosis: "
                        f"{patch.parent.parent}"
                    )
                    return "rejected"

                if exc.status_code == 409 and "already submitted" in str(exc).lower():
                    # Some earlier attempt actually landed server-side even
                    # though THIS process never saw the response — good news.
                    print(f"  {task_id}: already submitted (an earlier attempt landed) — clearing it")
                    if not ask_cleanup:
                        archive_after_submit(HOME, entry)
                    pending.remove(HOME, assignment_id)
                    cleanup_settled()
                    return "submitted"
                if exc.status_code == 410:
                    print(f"  {task_id}: lease expired, unsalvageable — the cell reopened "
                          "for someone else, dropping it")
                    pending.remove(HOME, assignment_id)
                    cleanup_settled()
                    return "expired"
                if (exc.status_code in (404, 413)
                        or _is_patch_secret_rejection(exc)):
                    # These are genuinely terminal for the current payload:
                    # assignment unknown, the reduced request is still too
                    # large, or a patch the server still considers unsafe.
                    # Never bypass the secret gate by retrying a reduced
                    # optional-artifact set.
                    print(f"  {task_id}: the server rejected this upload for good ({exc}) — "
                          "retrying can't fix it, dropping it from the retry queue "
                          f"(local artifact path: {patch.parent.parent})")
                    pending.remove(HOME, assignment_id)
                    settle_terminal_local_failure()
                    print(f"  rejected artifacts kept for diagnosis: {patch.parent.parent}")
                    return "rejected"
                # Unknown 422 responses are not proof that the completed work
                # is unrecoverable. Keep the ledger instead of silently losing
                # a paid run; future CLI/server versions may understand it.
                print(f"  {task_id}: upload failed ({exc}) — kept for retry "
                      "(`dradar retry-upload`)")
                return "upload-failed"

    if not ask_cleanup:
        archive_after_submit(HOME, entry)
    pending.remove(HOME, assignment_id)
    cleanup_settled()
    if ack.get("grade_status") == "invalid":
        # Neutral by design: the cause (printed by _run_and_submit's
        # diagnosis) may be anything from a stale agent image to a real rate
        # limit — claiming "wait for your quota to reset" here misled a real
        # volunteer whose quota was fine.
        print(f"recorded as invalid (not graded): {ack['submission_id']} — "
              "no points lost, the cell reopens for a fresh attempt")
    else:
        print(f"submitted: {ack['submission_id']} (grading happens server-side)")
    if job_dir and entry.get("keep", False):
        try:
            local_jobs.mark_kept(HOME, job_dir)
        except ValueError:
            pass
        print(f"  local artifacts kept by --keep: {job_dir}")
    elif job_dir:
        if outcome == "interrupted":
            # Always keep a failure's artifacts (result.json, agent logs):
            # deleting them made the first volunteer bug report undiagnosable
            # client-side. Completed runs stay tidy-by-default as before.
            if Path(job_dir).is_dir():
                print(f"  failure artifacts kept for diagnosis: {job_dir}")
        elif ask_cleanup and Path(job_dir).is_dir():
            answer = input("  delete this task's local files now? [Y/n] ").strip().lower()
            if answer in ("", "y", "yes"):
                archive_after_submit(HOME, entry)
                shutil.rmtree(job_dir, ignore_errors=True)
                print("  local task files cleaned")
            else:
                try:
                    local_jobs.mark_kept(HOME, job_dir)
                except ValueError:
                    pass
                print(f"  local artifacts kept: {job_dir}  "
                      "(`dradar cleanup --include-kept` removes them later)")
        else:
            shutil.rmtree(job_dir, ignore_errors=True)
    return "interrupted" if outcome == "interrupted" else "submitted"


def _mark_stopped_quietly(
    client: ApiClient,
    assignment: dict | str,
    *,
    defer_seconds: int = 300,
    failure_kind: str | None = None,
    failure_diagnostic: dict[str, object] | None = None,
) -> bool:
    """Best-effort checkout cleanup with bounded retry and visible failure.

    The endpoint is idempotent, so retrying transport/5xx failures is safe.
    Client errors are not retried because they need a refresh or upgrade, but
    they are still surfaced: silently losing this transition can strand a
    lease as apparently resumable after its runner disappeared.
    """
    assignment_id = (
        assignment if isinstance(assignment, str) else assignment["assignment_id"]
    )
    resume_generation = (
        None if isinstance(assignment, str) else assignment.get("resume_generation")
    )
    owner_epoch = (
        None if isinstance(assignment, str) else assignment.get("owner_epoch")
    )
    runner_session_id = (
        None if isinstance(assignment, str)
        else assignment.get("_runner_session_id")
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            stop_kwargs = {"defer_seconds": defer_seconds}
            if owner_epoch is not None:
                stop_kwargs["owner_epoch"] = owner_epoch
                stop_kwargs["session_id"] = runner_session_id
            elif resume_generation is not None:
                stop_kwargs["resume_generation"] = resume_generation
            if failure_kind is not None:
                stop_kwargs["failure_kind"] = failure_kind
            if failure_diagnostic is not None:
                stop_kwargs["failure_diagnostic"] = failure_diagnostic
            client.mark_stopped(assignment_id, **stop_kwargs)
            # A supervised child may have checked out paid work before this
            # failure. Publish the server-acknowledged return only after the
            # endpoint confirms the lease is still active and its running
            # ownership stamp was cleared. The parent combines this local
            # process-exit proof with a fresh authoritative inventory read.
            _record_worker_returned_assignment(assignment_id)
            return True
        except ApiError as exc:
            last_error = exc
            if (
                failure_diagnostic is not None
                and exc.status_code == 422
                and "failure_diagnostic" in str(exc)
            ):
                # A rolling/older server may reject only the optional
                # diagnostic schema. Retry cleanup once without telemetry;
                # never generalize this to unrelated 4xx responses.
                failure_diagnostic = None
                continue
            if (
                failure_kind is not None
                and exc.status_code == 422
                and "failure_kind" in str(exc)
            ):
                # A pre-failure_kind server can still perform the essential
                # idempotent checkout cleanup. Drop only the unsupported
                # observability field; never downgrade unrelated 4xx errors.
                failure_kind = None
                failure_diagnostic = None
                continue
            if exc.status_code is not None and exc.status_code < 500:
                break
        except Exception as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(0.25 * (attempt + 1))
    print(
        f"warning: could not confirm checkout cleanup for {assignment_id} "
        f"after {attempt + 1} attempt(s): {last_error}. The lease may appear "
        "stuck until the server detects the stale runner; keep this message "
        "and retry `dradar resume` after connectivity or the CLI upgrade is fixed."
    )
    return False


def _run_and_submit(client: ApiClient, assignment: dict, tasks_root: Path,
                    args, local_commit: str | None,
                    telemetry: RunnerTelemetry | None = None,
                    _assignment_lock_held: bool = False) -> str:
    """Run one assignment and upload the artifacts. Returns an outcome tag —
    never exits, so the held-batch loop can carry on with the next item."""
    if (
        _repeat_failure_state_path() is None
        and getattr(args, "_repeat_failure_invocation_id", None) is None
    ):
        args._repeat_failure_invocation_id = (
            f"{os.getpid()}-{time.time_ns()}-{id(args)}"
        )
    # The assignment lock covers the whole quota-consuming lifetime. A second
    # local `dradar resume` must never start a duplicate paid model process.
    if not _assignment_lock_held:
        try:
            with assignment_lock.lock(HOME, assignment["assignment_id"]):
                return _run_and_submit(
                    client, assignment, tasks_root, args, local_commit,
                    telemetry=telemetry, _assignment_lock_held=True,
                )
        except assignment_lock.AssignmentBusy:
            print(
                f"assignment {assignment['assignment_id']} is already running on this "
                "machine; refusing to start a duplicate model session"
            )
            return "busy"
    # This flag belongs to exactly one checked-out task. A cleanup failure
    # stops this worker before another checkout; a prior task must never poison
    # a later explicit invocation after the user has repaired Docker.
    args._docker_cleanup_blocked = None
    hash_match = check_task_content_hash(assignment, tasks_root)
    if hash_match is False and not getattr(args, "allow_task_drift", False):
        print(
            "refusing to start: the selected benchmark task differs from the "
            "server's published task package. No model process was started and "
            "no model quota was consumed. Update the CLI, restore local task "
            "changes, or refresh the task repo, then run `dradar resume`. Do not "
            "use `--allow-task-drift` for an ordinary retry; that override is only "
            "for an intentional non-comparable run."
        )
        _mark_stopped_quietly(
            client,
            assignment,
            failure_kind="task_content_mismatch",
            failure_diagnostic=task_content_mismatch_diagnostic(
                assignment, tasks_root, local_commit,
            ),
        )
        return "task-content-mismatch"
    if assignment["assignment_id"] in pending.assignment_ids(HOME):
        print(
            "refusing to start: this assignment already has a durable completed "
            "result pending upload; run `dradar retry-upload`"
        )
        return "pending-upload"
    work_dir = HOME / "work"
    print("running trial (this can take a while)...")

    def bind_owner() -> None:
        try:
            response = client.mark_started(
                assignment["assignment_id"],
                session_id=telemetry.session_id if telemetry else None,
            )
        except ApiError as exc:
            raise RunnerError(
                f"server ownership bind failed before model start: {exc}"
            ) from exc
        if response.get("owner_epoch") is not None:
            assignment["owner_epoch"] = int(response["owner_epoch"])
        if telemetry is not None:
            assignment["_runner_session_id"] = telemetry.session_id
            telemetry.set_phase(
                "running", assignment["assignment_id"],
                assignment.get("owner_epoch"),
            )
            telemetry.flush()

    allow_isolated_builder = True
    for attempt in (1, 2):
        try:
            try:
                art = run_trial(
                    assignment, tasks_root, work_dir, dev_agent=args.dev_agent,
                    on_started=bind_owner,
                    environment_build_timeout_multiplier=(
                        getattr(
                            args, "_environment_build_timeout_multiplier", None,
                        )
                    ),
                    build_cache_mode=(
                        getattr(args, "_build_cache_mode", None)
                        or getattr(args, "build_cache_mode", None)
                        or image_cache.DEFAULT_BUILD_CACHE_MODE
                    ),
                    allow_isolated_builder=allow_isolated_builder,
                )
            finally:
                # Assignment-scoped builders own only this trial's BuildKit
                # state and are removed on every exit path. A shared builder
                # is deliberately retained so its immutable layers serve the
                # next worker; image/runtime objects are still cleaned below.
                cache_mode = (
                    getattr(args, "_build_cache_mode", None)
                    or getattr(args, "build_cache_mode", None)
                    or image_cache.DEFAULT_BUILD_CACHE_MODE
                )
                if cache_mode == "shared":
                    builder_removed, builder_note = (
                        image_cache.remove_trial_builder(
                            HOME, assignment["assignment_id"], mode="shared",
                        )
                    )
                else:
                    builder_removed, builder_note = image_cache.remove_trial_builder(
                        HOME, assignment["assignment_id"],
                    )
                if not builder_removed:
                    args._docker_cleanup_blocked = (
                        "临时构建空间未能删除："
                        + (builder_note or "Docker 未返回具体原因")
                    )
            break
        except BuildDiskFullError as exc:
            print(
                f"trial failed: {exc}\n"
                "the build ran out of disk space — free space and re-run "
                "`dradar resume` (still free: the agent never started), or "
                "use `dradar release` if you do not want to keep the cell"
            )
            _mark_stopped_quietly(
                client, assignment, failure_kind="environment_build_failed",
            )
            return "environment-build-failed"
        except BuildSnapshotterPermissionError as exc:
            if attempt == 1 and allow_isolated_builder:
                print(
                    f"environment build failed ({exc})\n"
                    "isolated overlay is not permitted here — retrying with "
                    "the host default builder (no quota was consumed)..."
                )
                allow_isolated_builder = False
                continue
            print(
                f"trial failed: {exc}\n"
                "the image build cannot write overlay whiteouts on this host; "
                "run on a machine with a full Linux kernel, or `dradar release` "
                "if you do not want to keep the cell"
            )
            _mark_stopped_quietly(
                client, assignment, failure_kind="environment_build_failed",
            )
            return "environment-build-failed"
        except BuildFlakeError as exc:
            # The image build died before the agent ran — a free failure
            # (zero quota), and mirror flakes usually pass on the second
            # attempt, so retry once automatically instead of bouncing the
            # volunteer. A second flake in a row is likely a real network
            # problem worth a human look.
            if attempt == 1:
                print(f"environment build failed ({exc})\n"
                      "no quota was consumed — retrying once automatically...")
                continue
            print(f"trial failed: {exc}\n"
                  "the build failed twice — check your network/proxy and re-run "
                  "`dradar resume` (still free: the agent never started), or "
                  "use `dradar release` if you do not want to keep the cell")
            _mark_stopped_quietly(
                client, assignment, failure_kind="environment_build_failed",
            )
            return "environment-build-failed"
        except RunnerCleanupUnconfirmedError as exc:
            print(
                f"trial stopped: {exc}\n"
                "the lease remains running because local cleanup was not proven; "
                "this worker slot is quarantined to prevent a duplicate agent"
            )
            return "cleanup-unconfirmed"
        except RunnerTaskRetryableError as exc:
            stopped = _mark_stopped_quietly(
                client,
                assignment,
                failure_kind="runner_failed",
                failure_diagnostic=exc.failure_diagnostic,
            )
            retry_state = (
                "the assignment was returned for a later retry"
                if stopped
                else "the server will recover the isolated lease after it goes stale"
            )
            print(
                f"trial isolated: {exc}\n{retry_state}; other worker slots may continue"
            )
            return "assignment-isolated"
        except RunnerError as exc:
            failure_kind = classify_exception_message(str(exc))
            terminal_outcome = _terminal_failure_outcome(failure_kind)
            if attempt == 1 and _retryable_zcode_network_failure(assignment, exc):
                stopped = _mark_stopped_quietly(
                    client,
                    assignment,
                    defer_seconds=0,
                    failure_kind="provider-transport",
                    failure_diagnostic=exc.failure_diagnostic,
                )
                if stopped:
                    print(
                        "ZCode reported a structured transient network failure; "
                        "retrying this assignment once in the same runner..."
                    )
                    time.sleep(_ZCODE_NETWORK_RETRY_DELAY_SECONDS)
                    continue
                print(
                    "ZCode reported a transient network failure, but checkout "
                    "cleanup was not confirmed; refusing an unsafe retry"
                )
                return "failed"
            print(f"trial failed: {exc}\n"
                  "use `dradar resume` to retry later, or `dradar release` to "
                  "give the cell back")
            _mark_stopped_quietly(
                client,
                assignment,
                failure_kind=failure_kind or "runner_failed",
                failure_diagnostic=(
                    exc.failure_diagnostic
                    if (failure_kind or "runner_failed") == "runner_failed"
                    else None
                ),
            )
            return terminal_outcome or "failed"
        except (KeyboardInterrupt, EOFError):
            _mark_stopped_quietly(
                client, assignment, defer_seconds=0,
                failure_kind="user_interrupted",
            )
            raise

    if assignment.get("agent") == GROK_AGENT:
        preflight_kind = _grok_preflight_failure(art.result)
        if preflight_kind is not None:
            # No prompt/model request occurred. Keep this out of the
            # submission table, return the same assignment to a deferred
            # retryable state, and drain (not kill) already-running siblings.
            _signal_pool_abort(
                f"Grok provider preflight failed ({preflight_kind})",
                interrupt_siblings=False,
            )
            stopped = _mark_stopped_quietly(
                client,
                assignment,
                defer_seconds=900,
                failure_kind="auth" if preflight_kind == "auth" else None,
            )
            print(
                f"Grok provider preflight failed ({preflight_kind}); no model "
                "request was made and no invalid submission was created."
            )
            print(f"  -> {_GROK_PREFLIGHT_ADVICE[preflight_kind]}")
            print(f"  local diagnostic kept: {art.result or art.log_path}")
            if not stopped:
                print(
                    "  checkout cleanup was not confirmed; this worker is still "
                    "stopping and will not take another task."
                )
            return "provider-preflight-failed"

    # Make the authoritative source copy immediately after Pier returns,
    # before result parsing, image bookkeeping, pause handling, or upload can
    # be interrupted. _upload_trial repeats this idempotently and persists the
    # same paths/digest in the pending ledger.
    try:
        artifact_staging.ensure_staged_patch(art.trial_dir)
    except artifact_staging.PatchStagingError:
        # _upload_trial below records the exact structured failure and keeps it
        # retryable. Do not turn an artifact handoff fault into a model failure.
        pass

    # Pier normally removes compose images on a clean stop, but Docker/Pier
    # failures in the wild leave the task image tagged. Record only exact
    # label-validated references now, before upload cleanup removes job_dir.
    # This is best-effort bookkeeping and must never invalidate real work.
    image_cache.record_trial_images(
        HOME,
        assignment_id=assignment["assignment_id"],
        task_id=assignment["task_id"],
        trial_name=art.trial_dir.name,
    )
    cleanup = image_cache.cleanup_trial_resources(
        HOME,
        assignment_id=assignment["assignment_id"],
        job_dir=art.job_dir,
        trial_name=art.trial_dir.name,
        builder_isolated=art.builder_isolated,
        builder_reusable=art.builder_reusable,
        builder_name=art.builder_name,
        builder_expected=art.builder_expected,
        keep_images=bool(args.keep),
    )
    removed_objects = (
        cleanup.removed_containers + cleanup.removed_networks
        + cleanup.removed_volumes + cleanup.removed_images
    )
    if cleanup.success:
        reclaimed = _format_size(cleanup.estimated_reclaimed)
        print(
            f"  本题运行环境已清理（{removed_objects} 项，"
            f"预计释放 {reclaimed}）"
        )
    else:
        args._docker_cleanup_blocked = cleanup.note or "题目运行环境未能完整清理"
        print(
            "  提示：本题运行环境没有清理完整；这一路运行不会继续下一题"
        )
        print(f"  -> {args._docker_cleanup_blocked}")

    stats = summarize_result(art.result)
    # A recorded agent exception is always interrupted. A nonzero outer Pier
    # rc may happen after the agent completed and the task hook harvested its
    # patch. Accept that paid work only with independent DSH terminal evidence
    # or a complete server-verifiable Codex trajectory bundle.
    dsh_completion = None
    if (
        assignment.get("agent") == DSH_AGENT
        and art.returncode != 0
        and not stats.get("exception_info")
    ):
        dsh_completion = _dsh_completed_outcome(
            art.trial_dir, art.patch, art.result,
        )
    grok_completion = None
    if (
        assignment.get("agent") == GROK_AGENT
        and art.returncode != 0
        and not stats.get("exception_info")
    ):
        grok_completion = _grok_completed_outcome(
            assignment, art.trial_dir, art.patch, art.result,
        )
    bundled_completion = None
    if (
        art.returncode != 0
        and not stats.get("exception_info")
        and dsh_completion is None
        and grok_completion is None
    ):
        bundled_completion = _bundled_completed_outcome(
            assignment, art.trial_dir, art.patch, art.result,
        )
    postrun_completion = dsh_completion or grok_completion or bundled_completion
    interrupted = bool(stats.get("exception_info")) or (
        art.returncode != 0 and postrun_completion is None
    )
    diag = diagnose_exception(art.result) if interrupted else {}
    failure_kind = diag.get("kind")
    if (
        failure_kind is None
        and diag.get("type") == "NonZeroAgentExitCodeError"
        and _agent_exit_code(diag) is not None
    ):
        failure_kind = "agent-command-failed"
    repeat_failure = (
        _repeat_failure_signature(assignment, stats, diag, art)
        if interrupted and failure_kind == "agent-command-failed"
        else None
    )
    terminal_outcome = _terminal_failure_outcome(failure_kind)
    outcome = "interrupted" if interrupted else "completed"
    if telemetry:
        telemetry.set_phase(
            "uploading", assignment["assignment_id"],
            assignment.get("owner_epoch"),
        )
    display_outcome = "invalid/not-graded" if interrupted else "completed"
    print(f"trial finished in {art.duration_sec/60:.1f} min (pier rc={art.returncode}, "
          f"outcome={display_outcome}); uploading...")
    if postrun_completion is not None:
        print(
            "agent completed and produced independently verified artifacts; treating the "
            "nonzero Pier return code as a post-run infrastructure warning"
        )
    if interrupted:
        # Say what ACTUALLY failed. pier's rc=0 covers only its own process;
        # the recorded exception carries the in-container agent's real error
        # (exit code, API rejection) — hiding it sent a volunteer chasing a
        # quota problem that was a version problem.
        if diag:
            print(f"the agent failed inside the container: {diag.get('type') or 'unknown error'}")
            for ln in diag.get("tail", []):
                print(f"  | {ln[:300]}")
            advice = DIAG_ADVICE.get(diag.get("kind"))
            if advice:
                print(f"  -> {advice}")
        else:
            print(f"no exception recorded; see the pier log: {art.log_path}")

    meta = {
        "dradar_version": __version__,
        "duration_sec": round(art.duration_sec, 1),
        "pier_returncode": art.returncode,
        "dev_agent": args.dev_agent,
        "task_content_hash_match": hash_match,
        "deep_swe_commit": local_commit,
        "codex_cli_version": art.codex_cli_version,
        "grok_cli_version": art.grok_cli_version,
        "kimi_cli_version": art.kimi_cli_version,
        "antigravity_cli_version": art.antigravity_cli_version,
        "zcode_cli_version": art.zcode_cli_version,
        "zcode_cli_sha256": art.zcode_cli_sha256,
        "dsh_version": art.dsh_version,
        "codebuddy_cli_version": art.codebuddy_cli_version,
        **stats,
    }
    if failure_kind:
        # Keep Pier's raw exception_type for debugging, but also upload the
        # recovery-oriented cause that the CLI already derived from the
        # provider error.  The authenticated status view can then say
        # "insufficient-balance" instead of the opaque wrapper exception.
        meta["failure_kind"] = failure_kind
        meta["failure_layer"] = {
            "provider-transport": "provider_transport",
            "provider-temporary": "provider_transport",
            "rate-limit": "provider_transport",
            "model-capacity": "provider_transport",
            "agent-deadline": "agent_deadline",
        }.get(failure_kind, "agent_process")
        meta["failure_code"] = {
            "provider-transport": "provider_stream_failed",
            "provider-temporary": "provider_temporary",
            "rate-limit": "provider_rate_limited",
            "model-capacity": "provider_capacity",
            "agent-deadline": "agent_hard_deadline",
        }.get(failure_kind, failure_kind)
    if assignment.get("benchmark_id") == POMPEII_BENCHMARK_ID:
        meta.update({
            "soft_budget_sec": POMPEII_SOFT_BUDGET_SEC,
            "hard_budget_sec": pompeii_agent_timeout_sec(assignment),
            "terminal_tool_timeout_cap_sec": POMPEII_TERMINAL_HEAVY_TIMEOUT_SEC,
            "finalization_reserve_sec": POMPEII_FINALIZATION_RESERVE_SEC,
        })
    if (assignment.get("agent") or "codex") in HONEY_SECURITY_AGENTS:
        meta.update({
            "honey_execution_security_profile": HONEY_EXECUTION_SECURITY_PROFILE,
            "honey_inner_permission_mode": HONEY_INNER_PERMISSION_MODE,
            "honey_child_agent_access": HONEY_CHILD_AGENT_ACCESS,
            "honey_outer_isolation": HONEY_OUTER_ISOLATION,
        })
    if assignment_codex_provider(assignment) == DEEPSEEK_PROVIDER:
        # Server-side audit/gating can distinguish corrected official-catalog
        # runs from the earlier fallback-metadata benchmark without deleting
        # either history.
        meta.update({
            "model_config_version": DEEPSEEK_RUN_CONFIG_VERSION,
            "model_catalog_sha256": DEEPSEEK_CATALOG_SHA256,
            "model_runtime_profile": DEEPSEEK_RUNTIME_PROFILE,
        })
    if assignment.get("agent") == CLAUDE_AGENT:
        meta.update({
            "model_config_version": CLAUDE_RUN_CONFIG_VERSION,
            "model_runtime_profile": CLAUDE_RUNTIME_PROFILE,
            "subscription_oauth": True,
            "subscription_concurrency": (
                telemetry.target_workers if telemetry is not None else 1
            ),
            "subscription_oauth_coordination": "shared-setup-token-v1",
            "claude_cli_version": CLAUDE_CLI_VERSION,
            "claude_model": assignment["model"],
            "claude_native_efforts": ["low", "medium", "high", "xhigh", "max"],
            "claude_credential_mode": "private-file-process-env-v1",
            "claude_customizations": "disabled-isolated-config-v1",
        })
    if assignment.get("agent") == GROK_AGENT:
        meta.update({
            "model_config_version": GROK_RUN_CONFIG_VERSION,
            "model_runtime_profile": GROK_RUNTIME_PROFILE,
            "subscription_oauth": True,
            "subscription_concurrency": (
                telemetry.target_workers if telemetry is not None else 1
            ),
            "subscription_oauth_coordination": "native-shared-lock-v1",
        })
        if grok_completion is not None:
            meta.update({
                "grok_completion_evidence": grok_completion,
                "pier_postrun_warning": True,
                "pier_failure_phase": "post_agent",
            })
    if assignment.get("agent") == KIMI_AGENT:
        meta.update({
            "model_config_version": KIMI_RUN_CONFIG_VERSION,
            "model_runtime_profile": KIMI_RUNTIME_PROFILE,
            "subscription_oauth": True,
            "subscription_concurrency": (
                telemetry.target_workers if telemetry is not None else 1
            ),
            "subscription_oauth_coordination": "native-shared-lock-v1",
            "kimi_native_efforts": ["low", "high", "max"],
        })
    if assignment.get("agent") == ANTIGRAVITY_AGENT:
        meta.update({
            "model_config_version": ANTIGRAVITY_RUN_CONFIG_VERSION,
            "model_runtime_profile": ANTIGRAVITY_RUNTIME_PROFILE,
            "subscription_oauth": True,
            "subscription_concurrency": (
                telemetry.target_workers if telemetry is not None else 1
            ),
            "subscription_oauth_coordination": "shared-file-session-v1",
            "antigravity_native_efforts": ["low", "medium", "high"],
            "antigravity_terminal_sandbox": False,
            "antigravity_artifact_capture": ANTIGRAVITY_ARTIFACT_CAPTURE,
        })
    if assignment.get("agent") == ZCODE_AGENT:
        meta.update({
            "model_config_version": ZCODE_RUN_CONFIG_VERSION,
            "model_runtime_profile": ZCODE_RUNTIME_PROFILE,
            "coding_plan_api_key": True,
            "zcode_protocol_version": 1,
            "zcode_native_efforts": ["low", "high", "max"],
        })
    if assignment.get("agent") == DSH_AGENT:
        meta.update({
            "model_config_version": DSH_RUN_CONFIG_VERSION,
            "model_runtime_profile": DSH_RUNTIME_PROFILE,
            "dsh_minimal_tools": ["bash", "str_replace_editor"],
            "dsh_native_efforts": ["off", "high", "max"],
            "dsh_artifact_binding": art.dsh_artifact_binding,
        })
        if dsh_completion is not None:
            meta.update({
                "dsh_completion_evidence": dsh_completion,
                "pier_postrun_warning": True,
                "pier_failure_phase": "post_agent",
            })
    if assignment.get("agent") == CODEBUDDY_AGENT:
        meta.update({
            "model_config_version": CODEBUDDY_RUN_CONFIG_VERSION,
            "model_runtime_profile": CODEBUDDY_RUNTIME_PROFILE,
            "subscription_oauth": True,
            "subscription_concurrency": (
                telemetry.target_workers if telemetry is not None else 1
            ),
            "subscription_oauth_coordination": "host-monotonic-merge-v2",
            "codebuddy_native_efforts": list(CODEBUDDY_NATIVE_EFFORTS),
            "codebuddy_credential_mode": "isolated-run-copy-concurrent-v2",
            "codebuddy_mcp_mode": "strict-empty-v1",
            "codebuddy_tools": ["Bash", "Edit", "Read", "Write", "Glob", "Grep"],
        })
    elif bundled_completion is not None:
        meta.update({
            "bundled_completion_evidence": bundled_completion,
            "pier_postrun_warning": True,
            "pier_failure_phase": "post_agent",
        })

    if art.job_dir is not None:
        try:
            local_jobs.cleanup_assignment(
                HOME, assignment["assignment_id"], keep_job_dir=art.job_dir,
            )
        except ValueError:
            # Test/developer adapters may return a job outside the managed
            # jobs root. Never widen cleanup authority to accommodate it.
            pass

    upload_outcome = _upload_trial(client, {
        "assignment_id": assignment["assignment_id"], "nonce": assignment["nonce"],
        "task_id": assignment["task_id"], "trial_dir": str(art.trial_dir),
        # A private run-plan credential may replay only its exact claim batch.
        # Persist this public scope with the durable result so a fresh plan-only
        # machine can retry the upload without re-running the model or touching
        # another concurrently active Honeypot.
        "batch_id": assignment.get("batch_id"),
        "meta": meta, "outcome": outcome,
        "job_dir": str(art.job_dir) if art.job_dir else None, "keep": args.keep,
        "archive_session": getattr(args, "archive_session", False),
        "ledger_version": 3,
        "owner_epoch": assignment.get("owner_epoch", 0),
        "resume_generation": assignment.get("resume_generation", 0),
        "runner_session_id": telemetry.session_id if telemetry is not None else None,
    }, ask_cleanup=(
        outcome == "completed"
        and not args.keep
        and not getattr(args, "yes", False)
        and not getattr(args, "parallel", False)
    ))
    circuit_opened = _observe_repeat_failure(
        assignment,
        repeat_failure,
        # Any settled non-candidate result breaks consecutiveness. This
        # includes a real success and a task-level failure with observed
        # progress; neither may bridge two empty command exits into a streak.
        success=(
            repeat_failure is None
            and upload_outcome in {"submitted", "interrupted"}
        ),
        codex_cli_version=art.codex_cli_version,
        invocation_id=getattr(args, "_repeat_failure_invocation_id", None),
    )
    if circuit_opened:
        return "repeat-agent-failure"
    return terminal_outcome or upload_outcome


def _pending_uploads_for_batch(batch_id: str) -> list[dict]:
    try:
        exact_batch = normalize_batch_id(batch_id)
    except ValueError:
        return []

    def belongs(entry: dict) -> bool:
        try:
            return normalize_batch_id(entry.get("batch_id")) == exact_batch
        except ValueError:
            return False

    return [entry for entry in pending.load(HOME) if belongs(entry)]


def _retry_pending_uploads(
    client: ApiClient, *, batch_id: str | None = None,
) -> list[str]:
    """Auto-heal at the top of every `dradar go`/`resume`: flush anything a
    previous run couldn't upload before doing anything else. Silent no-op
    when the ledger is empty — this must never surprise a volunteer who has
    nothing pending."""
    entries = pending.load(HOME)
    if batch_id is not None:
        entries = _pending_uploads_for_batch(batch_id)
    if not entries:
        return []
    print(f"checking {len(entries)} pending upload(s) left over from a previous run...")
    outcomes = []
    for e in entries:
        # _upload_trial handles the gone-artifacts case (drops the entry).
        outcomes.append(_upload_trial(client, e))
    print()
    return outcomes


def cmd_retry_upload(args) -> int:
    """Standalone entry point: flush the pending-upload ledger without
    grabbing any new work (e.g. you're back online and just want to clear
    the backlog before deciding whether to run more)."""
    cfg = _load_config()
    client = _client(cfg)
    entries = pending.load(HOME)
    if not entries:
        print("nothing pending — every trial you've run has been uploaded")
        return 0
    salvage_assignment_id = getattr(args, "request_salvage", None)
    if salvage_assignment_id:
        matches = [
            entry for entry in entries
            if entry.get("assignment_id") == salvage_assignment_id
        ]
        if not matches:
            print(
                "no saved pending upload matches that assignment id; "
                "no state was changed"
            )
            return 2
        entry = matches[0]
        if entry.get("upload_blocked") != "owner_superseded":
            print(
                "that saved result is not blocked by owner_superseded; "
                "ordinary retry-upload remains the safe path"
            )
            return 2
        if not getattr(args, "yes", False):
            answer = input(
                "request a fresh upload-only owner for this exact saved result? "
                "The model will NOT run. [y/N] "
            ).strip().lower()
            if answer not in {"y", "yes"}:
                print("cancelled — no server or local state was changed")
                return 1
        outcome = _upload_trial(client, entry, request_salvage=True)
        return 0 if outcome in {"submitted", "interrupted"} else 1
    _retry_pending_uploads(client)
    remaining = pending.load(HOME)
    if remaining:
        blocked = [entry for entry in remaining if entry.get("upload_blocked")]
        retryable = [entry for entry in remaining if not entry.get("upload_blocked")]
        if blocked:
            reasons = ", ".join(
                f"{entry.get('task_id', entry.get('assignment_id', '?'))}:"
                f"{entry['upload_blocked']}"
                for entry in blocked
            )
            print(
                f"{len(blocked)} upload(s) are blocked and require explicit "
                f"review; they will not be retried automatically ({reasons})"
            )
        if retryable:
            print(
                f"{len(retryable)} still pending and retryable (will retry "
                "again on the next `dradar go`/`retry-upload`)"
            )
        return 1
    print("all clear")
    return 0


def _active_by_id(client: ApiClient) -> dict[str, dict]:
    data = client.get_assignment()
    active = data.get("active")
    if active is None:
        one = data.get("assignment")
        active = [one] if one else []
    return {a["assignment_id"]: a for a in active if a}


def _assignment_boundary_path(args) -> Path | None:
    value = getattr(args, "_assignment_boundary_path", None)
    if value:
        return Path(value)
    value = os.environ.get(_ASSIGNMENT_BOUNDARY_ENV)
    return Path(value) if value else None


def _prepare_assignment_boundary(
    args,
    client: ApiClient,
    benchmark_id: str,
    active: list[dict] | None = None,
) -> Path | None:
    """Validate/create the immutable non-refill assignment campaign set."""
    inherited = _assignment_boundary_path(args)
    if inherited is not None:
        args._assignment_boundary_path = str(inherited)
        return inherited
    if getattr(args, "refill", False):
        return None
    if active is None:
        try:
            active = list(_active_by_id(client).values())
        except ApiError as exc:
            _exit_for(exc)
    try:
        path = assignment_boundary.prepare(
            HOME,
            benchmark_id,
            active,
            # Preserve the pre-Fleet boundary path for legacy invocations so
            # an upgrade cannot silently bypass unresolved local state. Fleet
            # parents need a per-batch path because several exact batches are
            # intentionally live under one machine coordinator.
            batch_id=(
                getattr(client, "batch_id", None)
                if getattr(args, "fleet_pool", False) else None
            ),
            expected_ids=getattr(args, "expect_assignment", None),
            forget_existing=getattr(args, "forget_assignment_boundary", False),
        )
    except (assignment_boundary.BoundaryError, OSError) as exc:
        sys.exit(
            f"assignment boundary check failed: {exc}. No model was started. "
            "Inspect `dradar leases`; use --forget-assignment-boundary only "
            "after intentionally accepting the missing assignment(s)."
        )
    if path is not None:
        args._assignment_boundary_path = str(path)
    return path


def _record_assignment_boundary(args, assignment: dict, outcome: str) -> bool:
    path = _assignment_boundary_path(args)
    if path is None:
        return True
    try:
        assignment_boundary.record_outcome(path, assignment, outcome)
    except (assignment_boundary.BoundaryError, OSError) as exc:
        # The model has already stopped at this point. Fail closed on the next
        # checkout by opening the same pool drain used for other local faults.
        _signal_pool_abort(
            f"assignment boundary state could not be updated ({exc})",
            interrupt_siblings=False,
        )
        print(f"assignment boundary update failed ({exc}); no later task will start")
        return False
    return True


def _finish_assignment_boundary(
    client: ApiClient, path: Path | None,
) -> bool:
    """Reconcile after a run; False means the campaign lost an assignment."""
    if path is None:
        return True
    try:
        active = list(_active_by_id(client).values())
    except ApiError as exc:
        if _explicit_batch_finished(client, exc):
            # Exact-batch reads intentionally become 404 after the final
            # assignment settles.  The boundary ledger already records that
            # submitted/terminal outcome, so an empty active inventory is the
            # authoritative clean-drain state rather than a reconciliation
            # failure.
            active = []
        else:
            print(
                f"could not verify the assignment boundary after the run ({exc}); "
                "the boundary was kept and the next resume will re-check it"
            )
            return False
    try:
        report = assignment_boundary.reconcile(path, active)
    except (assignment_boundary.BoundaryError, OSError) as exc:
        print(
            f"could not verify the assignment boundary after the run ({exc}); "
            "the boundary was kept and the next resume will re-check it"
        )
        return False
    if report is None:
        return True
    if report.missing_ids:
        print("assignment boundary violation: unresolved assignment(s) disappeared:")
        for assignment_id in sorted(report.missing_ids):
            print(f"  {assignment_id}")
        print("no replacement task was claimed or started")
        return False
    if report.unexpected_ids:
        print("assignment boundary violation: active assignment(s) are outside the campaign:")
        for assignment_id in sorted(report.unexpected_ids):
            print(f"  {assignment_id}")
        print("no out-of-bound assignment was started")
        return False
    if report.complete:
        try:
            assignment_boundary.finish_if_complete(path, report)
        except OSError as exc:
            print(
                f"could not remove the completed assignment boundary ({exc}); "
                "the next resume will verify it again"
            )
            return False
    return True


def _finish_invocation_assignment_boundary(
    args, client: ApiClient, path: Path | None,
) -> bool:
    """Reconcile a boundary only in the process that owns its inventory.

    A worker-pool parent passes one shared boundary to its children so they
    can atomically record the outcome of the assignment they checked out.
    Each child can subsequently be scoped by the server to that assignment's
    provider/batch, however, and therefore does *not* have an authoritative
    view of the other assignments in the shared campaign. Reconciling from
    that partial view falsely reports healthy sibling assignments as missing
    and makes a replacement worker exit before checkout.

    The parent retains responsibility for final reconciliation. A standalone
    worker child without an inherited boundary remains conservative and
    reconciles normally.
    """
    inherited = os.environ.get(_ASSIGNMENT_BOUNDARY_ENV)
    if (
        getattr(args, "worker_child", False)
        and path is not None
        and inherited == str(path)
    ):
        return True
    return _finish_assignment_boundary(client, path)


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def cmd_refill_status(args) -> int:
    plan = refill_plan.load(HOME)
    if not plan:
        print("no local refill plan")
        return 0
    quota = plan.get("max_estimated_quota_pct")
    reserved = sum(
        float(item.get("estimated_quota_pct") or 0)
        for item in plan.get("assignments", {}).values()
    )
    quota_text = (f"{reserved:.2f}% / {quota}% {plan.get('quota_tier', 'plus')}"
                  if quota is not None else "not set")
    print(f"refill plan {plan.get('plan_id', '?')}  status={plan.get('status', '?')}")
    print(f"  queue target: {plan.get('refill_to', '?')}")
    print(f"  task budget: {len(plan.get('assignments', {}))}/{plan.get('max_tasks', '?')}")
    if plan.get("refill_harness"):
        scope = plan["refill_harness"]
        if plan.get("refill_model"):
            scope += f"/{plan['refill_model']}"
        if plan.get("refill_effort"):
            scope += f"@{plan['refill_effort']}"
        print(f"  exact scope: {scope} (no fallback)")
    print(f"  estimated quota cap: {quota_text}")
    seed_ids = plan.get("seed_assignment_ids")
    if seed_ids is not None:
        submitted = set(plan.get("submitted_seed_assignment_ids", []))
        complete = sum(assignment_id in submitted for assignment_id in seed_ids)
        print(f"  selected batch: {complete}/{len(seed_ids)} submitted before auto-refill")
    if plan.get("stop_reason"):
        print(f"  note: {plan['stop_reason']}")
    circuit = plan.get("circuit")
    if isinstance(circuit, dict) and circuit.get("state") == "open":
        scope = "/".join(
            str(value) for value in (
                circuit.get("harness"), circuit.get("provider"),
            ) if value
        ) or "saved scope"
        print(
            f"  circuit: open  scope={scope}  "
            f"family={circuit.get('failure_family', '?')}  "
            f"observations={circuit.get('observation_count', 1)}"
        )
    return 0


def cmd_refill_stop(args) -> int:
    plan = refill_plan.stop(HOME, "stopped by user", discard=True)
    if not plan:
        print("no local refill plan")
        return 0
    print("continuous refill stopped — no more tasks will be claimed; "
          "already held/running tasks were left untouched")
    return 0


def cmd_cleanup(args) -> int:
    """Remove only local jobs/images proven safe by current server state.

    A network failure aborts the whole sweep: without an authoritative active
    lease list, a local job may be a finished trial that crashed immediately
    before its upload ledger was recorded.
    """
    shared_cache_requested = bool(getattr(args, "shared_build_cache", False))
    docker_requested = bool(
        getattr(args, "docker", False) or getattr(args, "all_task_images", False)
    )
    if getattr(args, "all_task_images", False) and not getattr(args, "docker", False):
        print("--all-task-images requires --docker")
        return 1
    if shared_cache_requested and not getattr(args, "docker", False):
        print("--shared-build-cache requires --docker")
        return 1
    cfg = _load_config()
    client = _client(cfg)
    try:
        active_ids = set(_active_by_id(client))
    except ApiError as exc:
        print(f"cleanup stopped: couldn't verify active assignments ({exc})")
        print("nothing was deleted")
        return 1

    pending_ids = {
        entry.get("assignment_id") for entry in pending.load(HOME)
        if entry.get("assignment_id")
    }
    candidates: list[local_jobs.LocalJob] = []
    protected_active = protected_pending = protected_kept = 0
    seen_jobs: set[Path] = set()
    for item in local_jobs.scan(HOME):
        job = item.job_dir.resolve()
        if job in seen_jobs:
            continue
        seen_jobs.add(job)
        if item.assignment_id in pending_ids:
            protected_pending += 1
            continue
        if item.assignment_id in active_ids:
            protected_active += 1
            continue
        if local_jobs.is_kept(HOME, item.job_dir) and not args.include_kept:
            protected_kept += 1
            continue
        candidates.append(item)

    total = sum(item.size_bytes for item in candidates)
    if not candidates:
        if docker_requested:
            print("local task files: nothing safe to clean")
        else:
            print("nothing safe to clean")
    else:
        action = "would remove" if args.dry_run else "ready to remove"
        print(f"{action} {len(candidates)} settled local task(s), {_format_size(total)}")
        for item in candidates:
            kept = " [kept]" if local_jobs.is_kept(HOME, item.job_dir) else ""
            print(f"  {item.task_id or '?'}  assignment={item.assignment_id or '?'}  "
                  f"{_format_size(item.size_bytes)}{kept}")

    if protected_active or protected_pending or protected_kept:
        print("protected: "
              f"{protected_active} active/resumable, "
              f"{protected_pending} pending upload, "
              f"{protected_kept} explicitly kept")

    image_plan = None
    if docker_requested:
        image_plan = image_cache.plan_cleanup(
            HOME,
            protected_assignment_ids=active_ids,
            include_kept=args.include_kept,
            include_legacy=getattr(args, "all_task_images", False),
        )
        print("Docker task images:")
        if not image_plan.docker_available:
            print(f"  couldn't inspect Docker safely: {image_plan.note}")
        elif not image_plan.candidates:
            print("  nothing safe to clean")
        else:
            qualifier = "would remove" if args.dry_run else "ready to remove"
            print(f"  {qualifier} {len(image_plan.candidates)} image tag(s), "
                  f"estimated {_format_size(image_plan.estimated_reclaimable)} reclaimable")
            for image in image_plan.candidates:
                ownership = ("recorded" if image.reference in image_plan.owned_references
                             else "legacy Pier")
                print(f"    {image.reference}  {_format_size(image.unique_size)}  [{ownership}]")
        if image_plan.protected:
            print(f"  protected {image_plan.protected} image tag(s) used by a "
                  "container, active/pending task, or kept job")

    shared_cache_limit = image_cache.effective_policy(HOME, cfg).limit_bytes
    if shared_cache_requested:
        shared_name = image_cache.shared_builder_name(HOME)
        print("Shared BuildKit cache:")
        if args.dry_run:
            print(
                f"  would prune builder {shared_name} to "
                f"{_format_size(shared_cache_limit)} maximum"
            )
        elif active_ids:
            print(
                f"  protected while {len(active_ids)} active/resumable assignment(s) "
                "exist; no shared cache was pruned"
            )
        else:
            print(
                f"  ready to prune builder {shared_name} to "
                f"{_format_size(shared_cache_limit)} maximum"
            )

    has_images = bool(
        image_plan and image_plan.docker_available and image_plan.candidates
    )
    if (not candidates and not has_images and not shared_cache_requested) or args.dry_run:
        return 1 if docker_requested and image_plan and not image_plan.docker_available else 0
    if not args.yes:
        subjects = []
        if candidates:
            subjects.append("settled local files")
        if has_images:
            subjects.append("Docker task images")
        if shared_cache_requested:
            subjects.append("the DRadar shared BuildKit cache")
        subject = " and ".join(subjects)
        answer = input(f"remove these {subject}? [Y/n] ").strip().lower()
        if answer not in ("", "y", "yes"):
            print("nothing was deleted")
            return 0
    for item in candidates:
        local_jobs.remove(HOME, item)
    if candidates:
        print(f"cleaned {len(candidates)} task(s); freed {_format_size(total)}")
    image_failed = bool(image_plan and not image_plan.docker_available)
    if has_images:
        removed, reclaimed = image_cache.remove_images(HOME, image_plan.candidates)
        print(f"removed {removed}/{len(image_plan.candidates)} Docker image tag(s); "
              f"estimated {_format_size(reclaimed)} reclaimable")
        if removed != len(image_plan.candidates):
            image_failed = True
            print("some images changed or became active during cleanup and were safely skipped")
    shared_failed = False
    if shared_cache_requested and not active_ids:
        shared_ok, shared_note = image_cache.prune_shared_build_cache(
            HOME, max_used_bytes=shared_cache_limit,
        )
        print(f"  {shared_note or 'shared BuildKit cache cleanup finished'}")
        shared_failed = not shared_ok
    elif shared_cache_requested:
        shared_failed = True
    return 1 if image_failed or shared_failed else 0


def _maintain_image_cache(client: ApiClient, cfg: dict, *, phase: str) -> bool:
    """Run bounded, ledger-only GC with fail-closed ownership checks.

    A server read failure makes cleanup a no-op: without the active lease set
    we cannot prove an image is disposable.  The return value controls only
    NEW claims; existing leases are still allowed to run. During
    a worker pool, active assignments and Docker container references remain
    protected and every removal revalidates the exact image ID and labels.
    """
    try:
        active_ids = set(_active_by_id(client))
    except Exception as exc:
        print(f"image-cache maintenance skipped ({exc}); no Docker image was deleted")
        allow_new_claims = _disk_allows_refill(cfg)
        if not allow_new_claims:
            print("disk space is below the 25 GiB safety floor; existing work may "
                  "continue, but no new task will be claimed")
        return allow_new_claims
    result = image_cache.automatic_maintenance(
        HOME, cfg, protected_assignment_ids=active_ids,
    )
    if result.removed:
        print(f"image-cache {phase}: removed {result.removed} old DRadar image tag(s), "
              f"estimated {_format_size(result.estimated_reclaimed)} reclaimable")
    if result.note:
        print(f"image-cache {phase}: {result.note}")
    if phase.startswith("before") and result.legacy_count:
        print(f"image-cache {phase}: found {result.legacy_count} legacy Pier image "
              f"tag(s), estimated {_format_size(result.legacy_bytes)}. They are never "
              "auto-deleted; inspect once with `dradar cleanup --docker "
              "--all-task-images --dry-run`.")
    if (phase == "before run" and image_cache.proxy_detected()
            and "image_cache_mode" not in cfg):
        print("proxy environment detected: balanced image caching stays enabled to "
              "reduce repeat downloads. If proxy traffic is billed, run "
              "`dradar config set image-cache-mode metered`.")
    return result.allow_new_claims


def _disk_allows_refill(cfg: dict) -> bool:
    policy = image_cache.effective_policy(HOME, cfg)
    allowed, _reason = image_cache.disk_allows_new_tasks(
        HOME, min_free_bytes=policy.min_free_bytes,
    )
    return allowed


def _scope_client_to_batch(client: ApiClient, batch_id: str | None) -> None:
    """Apply one validated batch scope without burdening lightweight tests."""
    if hasattr(client, "set_batch_id"):
        client.set_batch_id(batch_id)
    elif batch_id is not None:
        # Production clients always expose set_batch_id. This compatibility
        # seam is only for small injected clients used by embedders/tests.
        setattr(client, "batch_id", batch_id)


def _redact_provider_preflight_issue(issue: str) -> str:
    """Keep setup diagnostics useful without printing account-local paths."""

    value = " ".join(str(issue).split())
    for path, replacement in (
        (str(HOME), "$DRADAR_HOME"),
        (str(Path.home()), "~"),
    ):
        if path:
            value = value.replace(path, replacement)
    return value[:500]


def _preflight_scoped_provider(args) -> None:
    """Fail before telemetry/session creation when a paid lane is unusable."""

    if getattr(args, "refill_harness", None) != ANTIGRAVITY_AGENT:
        return
    issue = prepare_antigravity_auth()
    if issue is None:
        return
    safe_issue = _redact_provider_preflight_issue(issue)
    refill_plan.open_circuit(
        HOME,
        {
            "agent": ANTIGRAVITY_AGENT,
            "provider": ANTIGRAVITY_PROVIDER,
            "batch_id": getattr(args, "batch_id", None),
        },
        "provider_not_ready",
    )
    sys.exit(
        "Antigravity provider preflight failed before any runner session or "
        f"assignment checkout: {safe_issue}\n"
        "Run `dradar provider setup antigravity` to repair/login, then run "
        "`dradar refill stop` to explicitly rearm this saved campaign."
    )


def _publish_fleet_startup_failure(args, reason: object) -> None:
    """Best-effort structured failure for the Agent-facing startup contract."""

    if not getattr(args, "fleet_pool", False):
        return
    from . import fleet

    detail = " ".join(str(reason).split()).lower()
    if "deep-swe" in detail or "task snapshot" in detail or "version pin" in detail:
        code = "task_environment_update_failed"
        message = (
            "这台设备未能准备与判分一致的题目环境；已有本地文件没有被修改。"
            "请检查网络和磁盘空间后，再次使用原运行说明。"
        )
    elif any(word in detail for word in ("docker", "pier", "egress", "disk space")):
        code = "local_environment_not_ready"
        message = (
            "这台设备的运行环境尚未准备好，题目没有开始执行。"
            "请检查 Docker、网络和磁盘空间后，再次使用原运行说明。"
        )
    elif "auth" in detail or "provider" in detail or "login" in detail:
        code = "runtime_tool_not_ready"
        message = (
            "这台设备上的运行工具尚未准备好，题目没有开始执行。"
            "请先完成该运行工具的登录或修复，再次使用原运行说明。"
        )
    elif reason == "pool ended before startup acknowledgement":
        code = "run_state_changed_before_start"
        message = (
            "题目状态在本机准备期间发生了变化，没有题目开始执行。"
            "请重新检查网页状态后，再次使用原运行说明。"
        )
    else:
        code = "local_start_failed"
        message = (
            "这台设备未能完成运行准备，题目没有开始执行。"
            "请检查本机状态后，再次使用原运行说明。"
        )
    try:
        fleet.publish_pool_startup_failure(
            HOME,
            args.batch_id,
            error_code=code,
            user_message=message,
            retryable=True,
        )
    except (fleet.FleetError, OSError, ValueError):
        # The original startup result remains authoritative. A dead coordinator
        # or an already-ready pool must never be replaced by reporting cleanup.
        pass


def _ack_fleet_startup_ready(args) -> None:
    if not getattr(args, "fleet_pool", False):
        return
    from . import fleet

    if not os.environ.get(fleet.POOL_STARTUP_FILE_ENV):
        return
    try:
        fleet.publish_pool_startup_ready(HOME, args.batch_id)
    except (fleet.FleetError, OSError, ValueError) as exc:
        raise SystemExit(
            "couldn't confirm that the local run finished preparing; no task "
            "will be started"
        ) from exc


def cmd_go(args) -> int:
    try:
        args.batch_id = normalize_batch_id(getattr(args, "batch_id", None))
    except ValueError as exc:
        sys.exit(f"invalid --batch-id: {exc}")
    if getattr(args, "pick", None) and getattr(args, "auto", None):
        sys.exit("--auto and --pick are two different ways to choose cells; pass only one")
    if getattr(args, "auto", None) is not None and args.auto < 1:
        sys.exit("--auto N requires N >= 1")
    workers = getattr(args, "workers", 1)
    auto_workers = workers == "auto"
    fleet_pool = bool(getattr(args, "fleet_pool", False))
    if not auto_workers and (workers < 1 or workers > 40):
        sys.exit("--workers N requires 1 <= N <= 40")
    if getattr(args, "worker_child", False) and (
        workers != 1
        or not getattr(args, "parallel", False)
        or not getattr(args, "resume", False)
        or getattr(args, "expect_assignment", None)
        or getattr(args, "forget_assignment_boundary", False)
    ):
        sys.exit("invalid internal worker invocation")
    if fleet_pool:
        controller_id = os.environ.get("DRADAR_FLEET_CONTROLLER_ID")
        env_batch_id = os.environ.get("DRADAR_FLEET_BATCH_ID")
        if (
            not controller_id
            or not getattr(args, "resume", False)
            or getattr(args, "worker_child", False)
            or not getattr(args, "batch_id", None)
            or env_batch_id != getattr(args, "batch_id", None)
            or auto_workers
            or getattr(args, "parallel", False)
        ):
            sys.exit("invalid internal Fleet pool invocation")
        from . import fleet

        if not fleet.controller_matches(controller_id, HOME):
            sys.exit("invalid internal Fleet pool invocation")
        try:
            fleet.acquire_pool_lock(HOME, args.batch_id, controller_id)
        except fleet.FleetError as exc:
            sys.exit(str(exc))
        fleet.start_pool_watchdog(HOME, controller_id)
        args.yes = True
    elif (
        getattr(args, "parallel", False)
        and not getattr(args, "worker_child", False)
    ):
        from . import fleet

        if fleet.controller_is_active(HOME):
            suffix = (
                f" --batch-id {args.batch_id}" if getattr(args, "batch_id", None)
                else " --batch-id <BATCH_ID>"
            )
            sys.exit(
                "a machine-local DRadar Fleet is already active. Do not start an "
                "unmanaged parallel session; add the exact Honeypot batch with "
                f"`dradar fleet add{suffix} --workers auto`"
            )
    if (auto_workers or workers > 1) and getattr(args, "parallel", False):
        sys.exit("--workers already manages parallel sessions; do not combine it with --parallel")
    if getattr(args, "refill_to", None) is not None:
        args.refill = True
    target_file = _pool_target_file(args)
    if (target_file is not None
            and not getattr(args, "worker_child", False)
            and (auto_workers or workers <= 1)):
        sys.exit("--worker-target-file requires a fixed --workers N greater than 1")
    refill_options = (
        getattr(args, "max_tasks", None),
        getattr(args, "max_estimated_quota_pct", None),
        getattr(args, "refill_harness", None),
        getattr(args, "refill_model", None),
        getattr(args, "refill_effort", None),
        getattr(args, "refill_order", None),
    )
    if any(value is not None for value in refill_options) and not getattr(args, "refill", False):
        sys.exit("refill limits and scope filters require --refill")
    if getattr(args, "refill", False):
        if (
            getattr(args, "expect_assignment", None)
            or getattr(args, "forget_assignment_boundary", False)
        ):
            sys.exit("assignment boundary options cannot be combined with --refill")
        if getattr(args, "assignment", None):
            sys.exit("continuous refill cannot be combined with --assignment")
        if (getattr(args, "refill_harness", None) is None
                and (getattr(args, "refill_model", None) is not None
                     or getattr(args, "refill_effort", None) is not None
                     or getattr(args, "refill_order", None) is not None)):
            sys.exit("--refill-model/--refill-effort/--refill-order require "
                     "--refill-harness")
        if getattr(args, "refill_harness", None) is not None:
            try:
                (args.refill_harness, args.refill_model,
                 args.refill_effort) = validate_refill_scope(
                    args.refill_harness,
                    getattr(args, "refill_model", None),
                    getattr(args, "refill_effort", None),
                )
            except ValueError as exc:
                sys.exit(str(exc))
        if getattr(args, "refill_harness", None) in PAID_API_REFILL_AGENTS:
            sys.exit("paid-API Harness assignments remain one-off and cannot use "
                     "continuous refill")
        if (getattr(args, "refill_harness", None) in SUBSCRIPTION_REFILL_AGENTS
                and args.max_tasks is None):
            sys.exit("subscription Harness refill requires an explicit "
                     "--max-tasks N total-task stop limit")
        if args.max_tasks is None and args.max_estimated_quota_pct is None:
            sys.exit("--refill requires --max-estimated-quota-pct PCT "
                     "(or the advanced --max-tasks N limit)")
        if args.max_tasks is None:
            args.max_tasks = DEFAULT_REFILL_TASK_SAFETY_CAP
        elif args.max_tasks < 1:
            sys.exit("--max-tasks N requires N >= 1")
        if args.refill_to is not None and args.refill_to < 1:
            sys.exit("--refill-to N requires N >= 1")
        if (args.max_estimated_quota_pct is not None
                and args.max_estimated_quota_pct <= 0):
            sys.exit("--max-estimated-quota-pct must be greater than 0")
    if not auto_workers:
        _align_refill_target_with_workers(args)
    if (auto_workers or workers > 1) and not getattr(args, "worker_child", False):
        try:
            _preflight_scoped_provider(args)
            result = _run_worker_pool(args)
        except BaseException as exc:
            _publish_fleet_startup_failure(args, exc)
            raise
        _publish_fleet_startup_failure(
            args, "pool ended before startup acknowledgement",
        )
        return result
    try:
        _preflight_scoped_provider(args)
    except BaseException as exc:
        _publish_fleet_startup_failure(args, exc)
        raise
    try:
        cfg = _run_config(args)
        cfg["benchmark"] = (
            getattr(args, "benchmark", None)
            or cfg.get("benchmark")
            or DEFAULT_BENCHMARK
        )
        client = _client(cfg, auto_register=True)
        _scope_client_to_batch(client, args.batch_id)
    except BaseException as exc:
        _publish_fleet_startup_failure(args, exc)
        raise
    # Pre-default configs may not carry tasks_root at all.  They now get the
    # same hidden checkout as a fresh login, while any explicit legacy path
    # remains authoritative.
    tasks_root = _selected_tasks_root(cfg)
    try:
        target_workers = int(os.environ.get("DRADAR_POOL_SIZE", "1"))
    except ValueError:
        target_workers = 1
    if not 1 <= target_workers <= 40:
        target_workers = 1
    telemetry = RunnerTelemetry(client, target_workers=target_workers)
    telemetry.bind_batch(args.batch_id)
    telemetry.start()
    close_reason = "error"

    try:
        # One runner per machine by default, THEN sweep containers stranded by
        # dead runs — the lock is what makes "a pier-shaped compose project
        # exists right now" mean "nobody alive owns it" (see machine.py).
        if fleet_pool:
            args.yes = True
            # Fixed Fleet entries never claim. A refill entry may claim only
            # through its exact server-authoritative campaign after the seed
            # barrier; it still cannot use go/auto/pick or cross batch scope.
            args.allow_new_claims = bool(getattr(args, "refill", False))
            print(
                f"Fleet batch {args.batch_id}: running under the machine-local "
                "coordinator; "
                + (
                    "refill is limited to this server-budgeted campaign."
                    if getattr(args, "refill", False)
                    else "no new assignments or refill will be requested."
                )
            )
        elif getattr(args, "parallel", False):
            args.yes = True  # a dispenser that stamps at checkout can't prompt
            print("--parallel: running alongside other dradar sessions on this "
                  "machine. Cells are split safely server-side, but the sessions "
                  "share this machine's CPU/RAM — expect slower individual runs.")
        else:
            acquire_run_lock(HOME)
            sweep_orphan_compose(HOME, args.yes)
            args.allow_new_claims = _maintain_image_cache(
                client, cfg, phase="before run",
            )

        # Preparing is a real phase: cloning the task repo and installing pier
        # can take minutes on a fresh machine. The heartbeat lets operators
        # distinguish that from an abandoned claim without inspecting the host.
        preparation = (
            fleet.preparation_lock(HOME) if fleet_pool else nullcontext()
        )
        try:
            with preparation:
                if cfg["benchmark"] != DEFAULT_BENCHMARK:
                    try:
                        ensure_benchmark_task_pack(
                            client, cfg["benchmark"], tasks_root)
                    except TaskPackError as exc:
                        raise RunnerError(str(exc)) from exc
                _ensure_selected_tasks_root(tasks_root, cfg["benchmark"])
                ensure_pier()
                _ensure_egress_runtime(
                    probe_connectivity=not getattr(args, "worker_child", False),
                )
        except RunnerError as exc:
            _publish_fleet_startup_failure(args, exc)
            sys.exit(str(exc))

        telemetry.set_phase("queued")
        # Self-heal before anything else: a trial from a previous run that ran
        # but failed to upload must not just sit on disk forever. The worker
        # pool parent already drains this shared ledger before spawning its
        # children; letting every child replay the same entries creates a
        # duplicate-upload herd precisely when the server asks us to slow down.
        if not getattr(args, "worker_child", False) and not fleet_pool:
            _retry_pending_uploads(client)

        boundary_path = _prepare_assignment_boundary(
            args, client, cfg["benchmark"],
        )

        # Completed paid work is represented only by the durable pending-upload
        # ledger above; old retired state directories can never start a model.

        try:
            rc = _go_menu(args, cfg, client, tasks_root, telemetry=telemetry)
        except BaseException as exc:
            _publish_fleet_startup_failure(args, exc)
            raise
        _publish_fleet_startup_failure(
            args, "pool ended before startup acknowledgement",
        )
        if not getattr(args, "parallel", False) and not fleet_pool:
            _maintain_image_cache(client, cfg, phase="after run")
        if not _finish_invocation_assignment_boundary(
            args, client, _assignment_boundary_path(args),
        ):
            rc = 1
        close_reason = "completed" if rc == 0 else "paused"
        return rc
    except (KeyboardInterrupt, EOFError):
        if getattr(args, "refill", False):
            refill_plan.stop(HOME, "interrupted by user")
        close_reason = "interrupted"
        raise
    finally:
        if getattr(args, "refill", False) and close_reason == "error":
            refill_plan.stop(HOME, "CLI exited unexpectedly")
        telemetry.close(close_reason)


def _worker_command(args) -> list[str]:
    """Build one internal resume worker without forwarding selection flags.

    The supervisor is the only process allowed to auto-claim or configure a
    refill plan. Children merely attach to that prepared batch and use the
    server's atomic checkout endpoint, which prevents duplicate model runs.
    """
    command = [
        sys.executable, "-m", "dradar.cli", "resume", "-y", "--parallel",
        "--workers", "1", "--worker-child",
    ]
    if args.keep:
        command.append("--keep")
    if getattr(args, "archive_session", False):
        command.append("--archive-session")
    if args.allow_task_drift:
        command.append("--allow-task-drift")
    if args.dev_agent:
        command.extend(("--dev-agent", args.dev_agent))
    if getattr(args, "benchmark", None):
        command.extend(("--benchmark", args.benchmark))
    if getattr(args, "batch_id", None):
        command.extend(("--batch-id", args.batch_id))
    build_timeout = getattr(
        args, "_environment_build_timeout_multiplier", None,
    )
    if build_timeout is not None:
        command.extend((
            "--environment-build-timeout-multiplier", f"{build_timeout:g}",
        ))
    build_cache_mode = getattr(args, "_build_cache_mode", None)
    if build_cache_mode:
        command.extend(("--build-cache-mode", build_cache_mode))
    if getattr(args, "credentials_file", None):
        command.extend(("--credentials-file", args.credentials_file))
    if getattr(args, "refill", False):
        command.extend(("--refill", "--max-tasks", str(args.max_tasks)))
        if args.refill_to is not None:
            command.extend(("--refill-to", str(args.refill_to)))
        if args.max_estimated_quota_pct is not None:
            command.extend((
                "--max-estimated-quota-pct", str(args.max_estimated_quota_pct),
            ))
        command.extend(("--quota-tier", args.quota_tier))
        if getattr(args, "refill_harness", None):
            command.extend(("--refill-harness", args.refill_harness))
        if getattr(args, "refill_model", None):
            command.extend(("--refill-model", args.refill_model))
        if getattr(args, "refill_effort", None):
            command.extend(("--refill-effort", args.refill_effort))
        if getattr(args, "refill_order", None):
            command.extend(("--refill-order", args.refill_order))
    return command


def _signal_workers(processes: list[subprocess.Popen]) -> None:
    """Ask children to stop cleanly, then bound escalation to dead processes."""
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(p.poll() is None for p in processes):
        time.sleep(0.05)
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(p.poll() is None for p in processes):
        time.sleep(0.05)
    for process in processes:
        if process.poll() is None:
            process.kill()


def _assignment_is_ready_for_checkout(
    assignment: dict, *, now: datetime | None = None,
    claimed_after: datetime | None = None,
    returned_assignment_ids: set[str] | None = None,
) -> bool:
    """Whether a held cell is genuinely waiting for a worker right now.

    Historical paused rows deliberately stay out of automatic pool backfill.
    Fresh controller claims have no ``started_at`` value and are safe for the
    server's atomic checkout endpoint to assign.
    """
    assignment_id = assignment.get("assignment_id")
    confirmed_return = bool(
        assignment_id
        and returned_assignment_ids
        and assignment_id in returned_assignment_ids
        and assignment.get("execution_state") == "waiting"
        and assignment.get("runner_state") == "waiting"
        and assignment.get("heartbeat_running") is False
        and assignment.get("runner_phase") is None
    )
    if (assignment.get("started_at")
            or assignment.get("execution_state") == "paused"
            or assignment.get("checkpoint_id")):
        return False
    if claimed_after is not None:
        if confirmed_return:
            # This exact child checked the assignment out, received a
            # successful assignment/stopped acknowledgement, and has now
            # exited. The server independently confirms that the still-leased
            # row is waiting, unowned and has no retired state marker. It is therefore
            # safe to bypass only the older leased_at cutoff for this ID.
            pass
        else:
            leased_at = assignment.get("leased_at")
            try:
                claimed_at = datetime.fromisoformat(
                    str(leased_at).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                # Old servers do not expose leased_at. A degraded pool must
                # fail closed rather than rotate a local fault through an
                # assignment it cannot prove was claimed after the failure.
                return False
            if claimed_at.tzinfo is None:
                claimed_at = claimed_at.replace(tzinfo=timezone.utc)
            if claimed_at <= claimed_after:
                return False
    retry_after = assignment.get("retry_after")
    if not retry_after:
        return True
    try:
        ready_at = datetime.fromisoformat(str(retry_after).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if ready_at.tzinfo is None:
        ready_at = ready_at.replace(tzinfo=timezone.utc)
    return ready_at <= (now or datetime.now(timezone.utc))


def _pool_ready_work_count(
    client: ApiClient, *, claimed_after: datetime | None = None,
    desired_workers: int | None = None,
    returned_assignment_ids: set[str] | None = None,
) -> int | None:
    """Read work eligible for a vacant desired-worker slot.

    ``None`` means the safety check itself failed. The supervisor then keeps
    current workers but fails closed instead of guessing and overspawning.
    New servers expose authoritative per-assignment heartbeat state; when a
    desired target is supplied, never create replacements if the batch already
    has that many live assignment owners. Atomic checkout still prevents two
    racing parents from consuming the same waiting assignment.
    """
    try:
        data = client.get_assignment()
    except ApiError as exc:
        if _explicit_batch_finished(client, exc):
            # An exact active-batch inventory becomes 404 when its last lease
            # settles. At this point the pool has already existed and is only
            # reconciling vacant slots, so this is authoritative clean drain,
            # not a failed inventory read. Initial acquisition still routes
            # the same 404 through _acquire_batch/_exit_for and fails closed.
            return 0
        print(f"worker backfill check failed ({exc}); keeping current workers only")
        return None
    active = data.get("active")
    if active is None:
        one = data.get("assignment")
        active = [one] if one else []
    pending_ids = pending.assignment_ids(HOME)
    waiting = sum(
        _assignment_is_ready_for_checkout(
            assignment, claimed_after=claimed_after,
            returned_assignment_ids=returned_assignment_ids,
        )
        for assignment in active
        if assignment and assignment.get("assignment_id") not in pending_ids
    )
    ready = waiting
    if desired_workers is None:
        return ready
    live = sum(
        bool(assignment.get("heartbeat_running"))
        for assignment in active
        if assignment
    )
    return min(ready, max(0, desired_workers - live))


def _explicit_batch_finished(client: ApiClient, exc: ApiError) -> bool:
    """Whether one scoped supervisor read proves its batch is now exhausted."""
    if not getattr(client, "batch_id", None) or exc.status_code != 404:
        return False
    if exc.code == "claim_batch_not_found":
        return True
    detail = str(exc).lower()
    return any(message in detail for message in (
        "active claim batch not found",
        "active batch not found",
        "claim batch not found",
    ))


def _pool_failure_cutoff_path() -> Path | None:
    raw = os.environ.get(_POOL_FAILURE_CUTOFF_ENV)
    return Path(raw) if raw else None


def _pool_returned_assignments_path() -> Path | None:
    raw = os.environ.get(_POOL_RETURNED_ASSIGNMENTS_ENV)
    return Path(raw) if raw else None


def _pool_backfill_v2_enabled() -> bool:
    """Whether the bounded desired-worker reconciler is enabled.

    The default is on so ordinary users get the fix. Operators can restore
    the previous drain-on-last-exit behavior without downgrading the CLI while
    a rollout is being investigated.
    """
    return os.environ.get(_POOL_BACKFILL_V2_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _worker_activity_path() -> Path | None:
    raw = os.environ.get(_POOL_WORKER_ACTIVITY_ENV)
    return Path(raw) if raw else None


def _write_worker_activity_state(value: str) -> bool:
    """Atomically publish one bounded child state to its local supervisor."""
    path = _worker_activity_path()
    if path is None:
        return True
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError:
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _record_worker_precheckout_failure(code: str) -> bool:
    """Classify a known safe pre-checkout gate without claiming paid work."""
    if code not in {
        "runner_session_capacity_reached", "provider_capability_required",
    }:
        return False
    return _write_worker_activity_state(f"preparing:{code}")


def _record_worker_returned_assignment(assignment_id: str) -> bool:
    """Record a server-confirmed return to waiting before this child exits."""
    return _write_worker_activity_state(f"waiting:{assignment_id}")


def _record_worker_checkout(assignment_id: str) -> bool:
    """Tell the local supervisor that this child consumed a real assignment.

    This local marker lets the parent distinguish a pre-checkout startup
    failure from a task/runtime failure. The latter keeps the existing
    anti-cascade cutoff; the former may receive a bounded replacement.
    """
    return _write_worker_activity_state(str(assignment_id))


def _pool_backfill_delay(attempt: int) -> float:
    """Bounded exponential delay for a repeatedly vacant worker slot."""
    exponent = max(0, attempt - 1)
    return min(
        _POOL_BACKFILL_RETRY_MAX_SECONDS,
        _POOL_BACKFILL_RETRY_BASE_SECONDS * (2 ** exponent),
    )


def _read_pool_failure_cutoff(path: Path | None = None) -> datetime | None:
    path = path or _pool_failure_cutoff_path()
    if path is None or not path.is_file():
        return None
    try:
        cutoff = datetime.fromisoformat(
            path.read_text(encoding="utf-8").strip().replace("Z", "+00:00")
        )
    except (OSError, TypeError, ValueError):
        return datetime.max.replace(tzinfo=timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return cutoff


def _write_pool_failure_cutoff(path: Path, cutoff: datetime) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(cutoff.isoformat(), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _decode_pool_returned_assignments(raw: str | None) -> set[str]:
    if not raw:
        return set()
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(values, list):
        return set()
    returned = set()
    for value in values:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 128
            or any(
                char not in (
                    "abcdefghijklmnopqrstuvwxyz"
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
                )
                for char in value
            )
        ):
            return set()
        returned.add(value)
    return returned


def _read_pool_returned_assignments(path: Path | None = None) -> set[str]:
    """Read exact server-acknowledged returns shared by the pool parent.

    Missing, unreadable, or malformed state fails closed to no proofs. The
    authoritative assignment inventory is still checked separately before a
    returned ID can bypass the degraded-pool claim cutoff.
    """
    # A freshly spawned replacement receives the parent's exact proof set in
    # its environment. This avoids depending on Windows sharing/AV semantics
    # merely to reach its first checkout. The file remains the update channel
    # for siblings that were already alive when another child returned work.
    snapshot = _decode_pool_returned_assignments(
        os.environ.pop(_POOL_RETURNED_ASSIGNMENTS_SNAPSHOT_ENV, None)
    )
    path = path or _pool_returned_assignments_path()
    try:
        if path is None or not path.is_file():
            return snapshot
        file_proofs = _decode_pool_returned_assignments(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError):
        return snapshot
    return snapshot | file_proofs


def _write_pool_returned_assignments(
    path: Path, assignment_ids: set[str],
) -> bool:
    """Atomically publish the parent's bounded exact-return proof set."""
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(sorted(assignment_ids), separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return True
    except OSError:
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _pool_degraded_exclusions(client: ApiClient) -> set[str] | None:
    """IDs held before the pool's latest child failure.

    ``None`` means the authoritative inventory could not be read; callers
    stop before checkout instead of guessing. The exclusion is refreshed on
    every checkout so assignments explicitly claimed after the failure stay
    eligible under the original parent process.
    """
    cutoff = _read_pool_failure_cutoff()
    if cutoff is None:
        return set()
    try:
        data = client.get_assignment()
    except ApiError as exc:
        print(
            f"degraded pool inventory check failed ({exc}); stopping this "
            "worker before checkout"
        )
        return None
    active = data.get("active")
    if active is None:
        one = data.get("assignment")
        active = [one] if one else []
    returned_assignment_ids = _read_pool_returned_assignments()
    return {
        assignment["assignment_id"]
        for assignment in active
        if assignment and not _assignment_is_ready_for_checkout(
            assignment, claimed_after=cutoff,
            returned_assignment_ids=returned_assignment_ids,
        )
        and assignment.get("assignment_id")
    }


def _run_worker_pool(args) -> int:
    """Prepare one batch, then supervise several ordinary resume processes."""
    configured_abort_file = _pool_abort_path()
    if configured_abort_file is not None and configured_abort_file.is_file():
        print(
            f"worker pool is circuit-broken: {_pool_abort_reason() or 'account stop'}; "
            "not claiming or starting model workers"
        )
        return 0
    cfg = client = None
    fleet_pool = bool(getattr(args, "fleet_pool", False))
    if fleet_pool:
        from . import fleet
    if args.workers == "auto":
        from .capacity import AUTO_WORKER_CAP, inspect_capacity, print_report

        cfg = _run_config(args)
        cfg["benchmark"] = (
            getattr(args, "benchmark", None)
            or cfg.get("benchmark")
            or DEFAULT_BENCHMARK
        )
        client = _client(cfg, auto_register=True)
        _scope_client_to_batch(client, getattr(args, "batch_id", None))
        requested_options = [
            value for value in (
                getattr(args, "refill_to", None), getattr(args, "auto", None),
                AUTO_WORKER_CAP if getattr(args, "refill", False) else None,
            ) if value is not None
        ]
        requested = max(requested_options) if requested_options else None
        if requested is not None and getattr(args, "max_tasks", None) is not None:
            requested = min(requested, args.max_tasks)
        try:
            report = inspect_capacity(client, requested_tasks=requested)
        except ApiError as exc:
            _exit_for(exc)
        print_report(report)
        args.workers = report.recommended_workers
        _align_refill_target_with_workers(args)
        # ``auto`` did not know the final pool size when _run_config first
        # resolved defaults. Re-evaluate now so a multi-worker pool shares
        # immutable BuildKit layers unless the user/config explicitly chose a
        # different policy.
        _refresh_build_cache_mode(args, cfg)
    elif not fleet_pool:
        from .capacity import docker_resources, worker_resource_warnings

        cpus, memory_gib, probe_warnings = docker_resources()
        resource_warnings = worker_resource_warnings(
            args.workers, cpus, memory_gib,
        )
        for warning in (*probe_warnings, *resource_warnings):
            print(f"warning: {warning}")
        if resource_warnings:
            print(
                "  CPU/memory pressure can change agent retry paths and "
                "benchmark results; use `--workers auto`, reduce N, or "
                "increase the Docker VM resources before continuing."
            )
            if sys.platform == "darwin":
                print(
                    "  Colima users should prefer a dedicated DRadar profile "
                    "instead of resizing another project's VM."
                )
    if not args.yes:
        answer = input(
            f"start {args.workers} local workers? They share this machine's "
            "CPU/RAM and may use model quota concurrently. [y/N] "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("not started; no new tasks were claimed")
            return 1
    args.yes = True

    if cfg is None or client is None:
        cfg = _run_config(args)
        cfg["benchmark"] = (
            getattr(args, "benchmark", None)
            or cfg.get("benchmark")
            or DEFAULT_BENCHMARK
        )
        client = _client(cfg, auto_register=True)
        _scope_client_to_batch(client, getattr(args, "batch_id", None))
    tasks_root = _selected_tasks_root(cfg)
    if fleet_pool:
        args.allow_new_claims = bool(getattr(args, "refill", False))
    else:
        acquire_run_lock(HOME)
        sweep_orphan_compose(HOME, True)
        args.allow_new_claims = _maintain_image_cache(
            client, cfg, phase="before worker pool",
        )
    preparation = (
        fleet.preparation_lock(HOME) if fleet_pool else nullcontext()
    )
    try:
        with preparation:
            if cfg["benchmark"] != DEFAULT_BENCHMARK:
                try:
                    ensure_benchmark_task_pack(client, cfg["benchmark"], tasks_root)
                except TaskPackError as exc:
                    raise RunnerError(str(exc)) from exc
            _ensure_selected_tasks_root(tasks_root, cfg["benchmark"])
            ensure_pier()
            _ensure_egress_runtime()
    except RunnerError as exc:
        sys.exit(str(exc))
    if fleet_pool:
        _retry_pending_uploads(client, batch_id=args.batch_id)
    else:
        _retry_pending_uploads(client)

    maximum = args.workers
    target_file = _pool_target_file(args)
    target = _read_pool_target(target_file, default=maximum, maximum=maximum)
    active, _free_pick = _prepare_batch(args, client)
    if _scoped_fleet_refill(args):
        pending_now = _pending_uploads_for_batch(args.batch_id)
        ready_now = (
            _pool_ready_work_count(client)
            if active else 0
        )
        if pending_now or not active or not ready_now:
            active = _wait_for_scoped_refill_work(
                args, client, desired_workers=target,
            )
    if not active:
        return 0
    worker_tasks_root = tasks_root
    if active[0].get("deep_swe_commit"):
        worker_tasks_root, _worker_task_commit = _version_pinned_tasks_root(
            active[0].get("deep_swe_commit"), tasks_root,
            args.allow_task_drift,
        )
    boundary_path = _assignment_boundary_path(args)
    if cfg.get("benchmark"):
        boundary_path = _prepare_assignment_boundary(
            args, client, cfg["benchmark"], active,
        )
    ready_now = (
        _pool_ready_work_count(client)
        if _scoped_fleet_refill(args) else len(active)
    )
    count = min(target, len(active), int(ready_now or 0))
    if count == 0 and _scoped_fleet_refill(args):
        active = _wait_for_scoped_refill_work(
            args, client, desired_workers=target,
        )
        if not active:
            return 0
        count = min(
            target,
            len(active),
            int(_pool_ready_work_count(client) or 0),
        )
    if count == 0:
        return 0
    if count < target:
        print(f"only {len(active)} task(s) are currently held; starting {count} worker(s)")
    print(f"starting {count} worker(s); server-side checkout assigns each task exactly once")
    if target_file is not None:
        print(f"live worker target: {target_file} (range 0..{maximum})")
    command = _worker_command(args)
    parent_capabilities = tuple(getattr(client, "capabilities", ()) or ())
    pool_abort_file = configured_abort_file or (
        Path(tempfile.gettempdir())
        / f"dradar-pool-abort-{os.getpid()}-{time.time_ns()}"
    )
    repeat_failure_state_file = (
        Path(tempfile.gettempdir())
        / f"dradar-repeat-failure-{os.getpid()}-{time.time_ns()}.json"
    )
    failure_cutoff_file = (
        Path(tempfile.gettempdir())
        / f"dradar-pool-failure-cutoff-{os.getpid()}-{time.time_ns()}"
    )
    returned_assignments_file = (
        Path(tempfile.gettempdir())
        / f"dradar-pool-returned-{os.getpid()}-{time.time_ns()}.json"
    )
    worker_activity_prefix = (
        Path(tempfile.gettempdir())
        / f"dradar-pool-worker-{os.getpid()}-{time.time_ns()}"
    )
    owns_abort_file = configured_abort_file is None
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    processes: list[subprocess.Popen] = []
    active_processes: dict[int, subprocess.Popen] = {}
    returncodes: list[tuple[int, int]] = []
    quarantined_slots: set[int] = set()
    suppressed_slots: set[int] = set()
    returned_assignment_ids: set[str] = set()
    worker_activity_files: dict[int, Path] = {}
    vacant_attempts: dict[int, int] = {}
    retry_not_before: dict[int, float] = {}
    zero_live_inventory_failures = 0
    backfill_error: str | None = None
    backfill_exhausted = False
    backfill_disabled = False
    failure_cutoff: datetime | None = None
    abort_reason: str | None = None
    abort_interrupts_siblings = False

    def cleanup_abort_file() -> None:
        if owns_abort_file:
            pool_abort_file.unlink(missing_ok=True)
        repeat_failure_state_file.unlink(missing_ok=True)
        repeat_failure_state_file.with_name(
            f"{repeat_failure_state_file.name}.lock"
        ).unlink(missing_ok=True)
        failure_cutoff_file.unlink(missing_ok=True)
        returned_assignments_file.unlink(missing_ok=True)
        for path in worker_activity_files.values():
            path.unlink(missing_ok=True)

    def spawn_worker(slot: int) -> None:
        activity_file = worker_activity_files.setdefault(
            slot,
            worker_activity_prefix.with_name(
                f"{worker_activity_prefix.name}-slot-{slot}.started"
            ),
        )
        # The parent writes the initial state before spawning. Missing or
        # unreadable state later is treated fail-closed; only this exact value
        # proves that the child exited before checkout.
        activity_file.write_text("preparing", encoding="utf-8")
        env = os.environ.copy()
        env["DRADAR_WORKER_INDEX"] = str(slot)
        env["DRADAR_POOL_SIZE"] = str(target)
        env["DRADAR_POOL_MAX_SIZE"] = str(maximum)
        if target_file is not None:
            env[_POOL_TARGET_FILE_ENV] = str(target_file)
        env[_POOL_ABORT_ENV] = str(pool_abort_file)
        env[_REPEAT_FAILURE_STATE_ENV] = str(repeat_failure_state_file)
        env[_POOL_FAILURE_CUTOFF_ENV] = str(failure_cutoff_file)
        env[_POOL_RETURNED_ASSIGNMENTS_ENV] = str(returned_assignments_file)
        env[_POOL_RETURNED_ASSIGNMENTS_SNAPSHOT_ENV] = json.dumps(
            sorted(returned_assignment_ids), separators=(",", ":"),
        )
        env[_POOL_WORKER_ACTIVITY_ENV] = str(activity_file)
        env[_PINNED_TASKS_ROOT_ENV] = str(worker_tasks_root)
        # The parent has already evaluated provider readiness and used this
        # exact capability set for its successful batch inventory request.
        # Children inherit that snapshot instead of concurrently probing
        # shared provider state and accidentally dropping one capability.
        env[_POOL_CAPABILITIES_ENV] = json.dumps(
            list(parent_capabilities), separators=(",", ":"),
        )
        if boundary_path is not None:
            env[_ASSIGNMENT_BOUNDARY_ENV] = str(boundary_path)
        process = subprocess.Popen(command, env=env, **popen_kwargs)
        processes.append(process)
        active_processes[slot] = process
        print(f"  worker {slot}/{count}: pid {process.pid}")

    try:
        for index in range(1, count + 1):
            spawn_worker(index)
        if fleet_pool and os.environ.get(fleet.POOL_STARTUP_FILE_ENV):
            try:
                fleet.publish_pool_startup_ready(HOME, args.batch_id)
            except (fleet.FleetError, OSError, ValueError):
                print(
                    "couldn't confirm that the local run finished preparing; "
                    "stopping the newly started workers safely"
                )
                _signal_workers(processes)
                cleanup_abort_file()
                return 1

        next_backfill_check = 0.0
        while True:
            for slot, process in list(active_processes.items()):
                returncode = process.poll()
                if returncode is None:
                    continue
                returncodes.append((slot, returncode))
                del active_processes[slot]
                activity_file = worker_activity_files.get(slot)
                try:
                    activity_state = (
                        activity_file.read_text(encoding="utf-8")
                        if activity_file is not None else None
                    )
                except OSError:
                    activity_state = None
                capacity_blocked = (
                    activity_state
                    == "preparing:runner_session_capacity_reached"
                )
                returned_assignment_id = (
                    activity_state.removeprefix("waiting:")
                    if activity_state and activity_state.startswith("waiting:")
                    else None
                )
                if returned_assignment_id:
                    updated_returned = returned_assignment_ids | {
                        returned_assignment_id,
                    }
                    if _write_pool_returned_assignments(
                        returned_assignments_file, updated_returned,
                    ):
                        returned_assignment_ids = updated_returned
                    else:
                        # Without a proof readable by the replacement child,
                        # fail closed and leave this old lease out of backfill.
                        returned_assignment_ids.discard(returned_assignment_id)
                elif activity_state not in {
                    None, "preparing",
                    "preparing:runner_session_capacity_reached",
                }:
                    # A later checkout consumed this exact assignment's
                    # one-shot safe-return proof. If the child did not return
                    # it again, never let an old marker authorize another run.
                    returned_assignment_ids.discard(activity_state)
                    _write_pool_returned_assignments(
                        returned_assignments_file, returned_assignment_ids,
                    )
                precheckout_exit = (
                    activity_state == "preparing" or capacity_blocked
                )
                # Unknown marker state is safety-significant: never turn a
                # lost checkout signal into automatic retry of paid work.
                consumed_assignment = not precheckout_exit
                if returncode == _WORKER_SLOT_QUARANTINED_EXIT_CODE:
                    quarantined_slots.add(slot)
                    print(
                        f"worker {slot} quarantined after cleanup could not be "
                        "confirmed; sibling workers will continue and this slot "
                        "will not be reused"
                    )
                elif capacity_blocked:
                    attempt = vacant_attempts.get(slot, 0) + 1
                    if attempt < _POOL_BACKFILL_MAX_ATTEMPTS:
                        vacant_attempts[slot] = attempt
                        retry_not_before[slot] = (
                            time.monotonic() + _pool_backfill_delay(attempt)
                        )
                        print(
                            f"worker {slot} was deferred by runner session "
                            "capacity; retrying this vacant slot after bounded "
                            f"backoff ({attempt}/{_POOL_BACKFILL_MAX_ATTEMPTS})"
                        )
                    else:
                        # A genuine concurrent session usually clears in one
                        # of the short attempts above. At the normal retry
                        # ceiling, preserve the waiting assignment but reset
                        # the attempt budget before the generic suppression
                        # pass can permanently retire this slot. The longer
                        # delay lets an unclosed session cross the server's
                        # freshness boundary without creating a spawn storm.
                        vacant_attempts[slot] = 0
                        retry_not_before[slot] = (
                            time.monotonic()
                            + _POOL_SESSION_CAPACITY_RETRY_SECONDS
                        )
                        print(
                            f"worker {slot} remained blocked by runner session "
                            "capacity after bounded retries; the assignment "
                            "remains waiting and this slot will be retried after "
                            "the server freshness window"
                        )
                elif precheckout_exit:
                    attempt = vacant_attempts.get(slot, 0) + 1
                    vacant_attempts[slot] = attempt
                    retry_not_before[slot] = (
                        time.monotonic() + _pool_backfill_delay(attempt)
                    )
                    if returncode == 0:
                        print(
                            f"worker {slot} exited without checkout; checking "
                            "the authoritative waiting queue before replacement"
                        )
                    else:
                        print(
                            f"worker {slot} exited {returncode} before checkout; "
                            f"retrying this vacant slot after bounded backoff "
                            f"({attempt}/{_POOL_BACKFILL_MAX_ATTEMPTS})"
                        )
                elif returncode != 0 and not backfill_disabled:
                    # Freeze everything that was already held when this local
                    # failure surfaced. Replacement workers may only consume
                    # assignments explicitly claimed later; the shared cutoff
                    # is also enforced by every surviving child before its
                    # next checkout. This preserves the old anti-cascade
                    # drain while preventing later web claims from silently
                    # expiring behind a still-live parent process.
                    failure_cutoff = datetime.now(timezone.utc)
                    _write_pool_failure_cutoff(
                        failure_cutoff_file, failure_cutoff,
                    )
                    print(
                        f"worker {slot} exited {returncode}; existing waiting "
                        "work is frozen, watching only for assignments claimed later"
                    )
                elif consumed_assignment:
                    # A healthy child may finish while other assignments are
                    # still waiting. Its slot remains eligible for inventory-
                    # driven replacement rather than retiring the whole pool.
                    vacant_attempts.pop(slot, None)
                    retry_not_before.pop(slot, None)

            if not active_processes and not _pool_backfill_v2_enabled():
                break
            new_target = _read_pool_target(
                target_file, default=target, maximum=maximum,
            )
            if new_target != target:
                direction = "up" if new_target > target else "down"
                print(f"scaling worker pool {direction}: {target} -> {new_target}")
                if getattr(args, "refill", False):
                    try:
                        resized = refill_plan.resize_target(HOME, new_target)
                        if resized is not None:
                            print(
                                "live refill queue target: "
                                f"{resized['refill_to']}"
                            )
                            if new_target > target:
                                refill_plan.refill_once(HOME, client)
                    except (refill_plan.RefillError, ApiError) as exc:
                        refill_plan.stop(
                            HOME, f"live resize failed: {type(exc).__name__}",
                        )
                        backfill_disabled = True
                        print(
                            "continuous refill stopped after a live resize "
                            f"failure ({exc}); active workers will finish"
                        )
                target = new_target
            if pool_abort_file.is_file():
                if abort_reason is None:
                    directive = _pool_stop_directive(pool_abort_file)
                    abort_interrupts_siblings, abort_reason = (
                        directive or (True, "account stop")
                    )
                    backfill_disabled = True
                    if abort_interrupts_siblings:
                        print(
                            f"worker pool circuit opened: {abort_reason}; "
                            "stopping sibling workers"
                        )
                        _signal_workers(list(active_processes.values()))
                    else:
                        print(
                            f"worker pool drain opened: {abort_reason}; no new "
                            "checkout or backfill will start, active workers will finish"
                        )
                if abort_interrupts_siblings:
                    if not active_processes:
                        break
                    time.sleep(_POOL_SUPERVISOR_POLL_SECONDS)
                    continue

            if backfill_disabled and not active_processes:
                break

            if target == 0 and not active_processes:
                break

            current_time = time.monotonic()
            if (not backfill_disabled
                    and len(active_processes) < target
                    and current_time >= next_backfill_check):
                ready = _pool_ready_work_count(
                    client, claimed_after=failure_cutoff,
                    desired_workers=(
                        None if _scoped_fleet_refill(args) else target
                    ),
                    returned_assignment_ids=returned_assignment_ids,
                )
                next_backfill_check = (
                    current_time + (
                        _POOL_BACKFILL_ERROR_RETRY_SECONDS
                        if ready is None else _POOL_BACKFILL_REFRESH_SECONDS
                    )
                )
                if ready:
                    vacant_slots = sorted(
                        set(range(1, target + 1)) - set(active_processes)
                        - quarantined_slots - suppressed_slots
                    )
                    exhausted_slots = {
                        slot for slot in vacant_slots
                        if vacant_attempts.get(slot, 0)
                        >= _POOL_BACKFILL_MAX_ATTEMPTS
                    }
                    if exhausted_slots:
                        suppressed_slots.update(exhausted_slots)
                        backfill_exhausted = True
                        detail = ", ".join(map(str, sorted(exhausted_slots)))
                        print(
                            "bounded replacement is exhausted for worker "
                            f"slot(s) {detail}; held work remains waiting"
                        )
                        vacant_slots = [
                            slot for slot in vacant_slots
                            if slot not in exhausted_slots
                        ]
                    eligible_slots = [
                        slot for slot in vacant_slots
                        if current_time >= retry_not_before.get(slot, 0.0)
                    ]
                    for slot in eligible_slots[:ready]:
                        print(
                            f"held work is waiting; restoring worker slot "
                            f"{slot}/{target}"
                        )
                        try:
                            spawn_worker(slot)
                        except OSError as exc:
                            # Existing children remain observed by this
                            # parent. A replacement failure must not interrupt
                            # paid work merely to restore target throughput.
                            backfill_error = str(exc)
                            backfill_disabled = True
                            print(
                                f"couldn't restore worker slot {slot}/{target} "
                                f"({exc}); current workers will finish safely"
                            )
                            break
                    if not active_processes and not eligible_slots:
                        retryable_slots = (
                            set(range(1, target + 1))
                            - quarantined_slots - suppressed_slots
                        )
                        if not retryable_slots:
                            break
                elif ready == 0 and not active_processes:
                    if _scoped_fleet_refill(args):
                        waiting_active = _wait_for_scoped_refill_work(
                            args, client, desired_workers=target,
                        )
                        if waiting_active:
                            next_backfill_check = 0.0
                            continue
                    break
                elif ready is None and not active_processes:
                    zero_live_inventory_failures += 1
                    if zero_live_inventory_failures >= _POOL_BACKFILL_MAX_ATTEMPTS:
                        backfill_error = "worker inventory remained unavailable"
                        print(
                            "worker inventory remained unavailable after bounded "
                            "retries; leaving held work untouched"
                        )
                        break
                if ready is not None:
                    zero_live_inventory_failures = 0
            time.sleep(_POOL_SUPERVISOR_POLL_SECONDS)
    except (KeyboardInterrupt, EOFError):
        print("\nstopping workers safely; each active task is recoverable only after "
              "the server confirms checkout cleanup or its completed upload is durable...")
        _signal_workers(processes)
        if not fleet_pool:
            _maintain_image_cache(client, cfg, phase="after interrupted worker pool")
        cleanup_abort_file()
        raise
    except OSError as exc:
        # A later spawn can fail after earlier children are already live
        # (process limit, executable disappeared, Windows group setup, ...).
        # Never orphan those children: an unobserved Pier run can keep using
        # model quota even though the command appears to have failed.
        print(f"couldn't start every worker ({exc}); stopping those already started")
        _signal_workers(processes)
        if not fleet_pool:
            _maintain_image_cache(client, cfg, phase="after failed worker pool")
        cleanup_abort_file()
        return 1
    cleanup_abort_file()
    failed = [
        (slot, rc) for slot, rc in returncodes
        if rc not in (0, _WORKER_SLOT_QUARANTINED_EXIT_CODE)
    ]
    if not fleet_pool:
        _maintain_image_cache(client, cfg, phase="after worker pool")
    boundary_safe = _finish_assignment_boundary(client, boundary_path)
    if not boundary_safe:
        return 1
    if abort_reason is not None:
        if _scoped_fleet_refill(args):
            # A drain must let active model/upload work finish. If the first
            # upload response was lost, make one immediate exact-scope replay
            # before settling. A remaining durable entry turns the pool into
            # an interrupted (recoverable) local state instead of a false clean
            # stop; the Agent can later invoke run --upload-only without
            # re-enrolling this device or starting another model.
            _retry_pending_uploads(client, batch_id=args.batch_id)
            if _pending_uploads_for_batch(args.batch_id):
                print(
                    "worker pool stopped with a completed result still waiting "
                    "to upload; no model work will be repeated"
                )
                return 1
        if abort_interrupts_siblings:
            print(f"worker pool stopped cleanly by circuit breaker: {abort_reason}")
        else:
            print(f"worker pool drained cleanly after account stop: {abort_reason}")
        return 0
    if backfill_error:
        print("worker pool finished after a backfill spawn error; completed uploads "
              "are preserved and the next resume can restore the full pool")
        return 1
    if backfill_exhausted:
        print(
            "worker pool finished after bounded pre-checkout replacement "
            "failures; held assignments remain waiting for a later resume"
        )
        return 1
    if failed:
        detail = ", ".join(f"worker {i}=exit {rc}" for i, rc in failed)
        print(f"worker pool finished with errors: {detail}")
        print("completed uploads are preserved; use `dradar leases`, "
              "`dradar retry-upload`, and `dradar resume` for remaining work")
        return 1
    if quarantined_slots:
        detail = ", ".join(str(slot) for slot in sorted(quarantined_slots))
        print(
            f"worker pool finished with quarantined slot(s): {detail}. "
            "Their leases were kept running because exact-job cleanup was not proven."
        )
        print(
            "Completed sibling work is preserved; inspect the affected runtime "
            "before starting replacement workers."
        )
        return 1
    print("all workers finished")
    return 0


def _align_refill_target_with_workers(args) -> None:
    """A refill queue smaller than its worker pool is accidental idling.

    Raise only the queue target, never the user's task/quota ceilings.  If an
    explicit max_tasks is lower than the requested worker count, that hard cap
    wins and the pool naturally starts fewer children after the bounded top-up.
    """
    if not getattr(args, "refill", False):
        return
    workers = getattr(args, "workers", 1)
    if not isinstance(workers, int) or workers <= 1:
        return
    target_file = _pool_target_file(args)
    if target_file is not None:
        floor = _read_pool_target(
            target_file, default=workers, maximum=workers,
        )
        if floor == 0:
            sys.exit(
                "a refill pool cannot start with worker target 0; start at 1 "
                "or higher, then write 0 to drain it live"
            )
    else:
        floor = workers
    if getattr(args, "max_tasks", None) is not None:
        floor = min(floor, int(args.max_tasks))
    current = getattr(args, "refill_to", None)
    if target_file is not None:
        if current != floor:
            args.refill_to = floor
            print(
                f"refill queue target synchronized to live worker target {floor}; "
                "quota/task caps remain unchanged"
            )
    elif current is None or current < floor:
        args.refill_to = floor
        print(f"refill queue target raised to {floor} so {workers} worker(s) can stay busy; "
              "quota/task caps remain unchanged")


def _acquire_batch(
    client: ApiClient,
    yes: bool,
    *,
    allow_new_claims: bool = True,
    allow_empty_exact_campaign: bool = False,
) -> tuple[list[dict], bool]:
    """The volunteer's held batch, plus whether this is a free-pick instance.
    Free-pick: the batch is whatever they claimed on the web. Menu mode
    (non-free-pick, e.g. claude) with nothing held: claim one from the menu
    right here. Normalizes the older single-`assignment` payload shape so an
    older server still works."""
    try:
        data = client.get_assignment()
    except ApiError as exc:
        if allow_empty_exact_campaign and exc.status_code == 404:
            # A run-plan continuation may legitimately have no live assignment
            # between its selected seed batch and the next server-budgeted
            # claim.  Only the exact scoped caller opts into this interpretation;
            # ordinary resume keeps treating the same 404 as a terminal lookup.
            data = {"active": [], "free_pick": True}
        else:
            _exit_for(exc)
    active = data.get("active")
    if active is None:
        one = data.get("assignment")
        active = [one] if one else []
    free_pick = data.get("free_pick", False)
    menu = data.get("menu")

    if not active and not free_pick and menu and allow_new_claims:
        try:
            one = _claim_from_menu(client, menu, yes)
        except ApiError as exc:
            _exit_for(exc)
        active = [one] if one else []
    pending_ids = pending.assignment_ids(HOME)
    blocked = [a for a in active if a.get("assignment_id") in pending_ids]
    if blocked:
        print(
            f"holding {len(blocked)} completed assignment(s) for upload recovery; "
            "the model will not be run again"
        )
    return [a for a in active if a.get("assignment_id") not in pending_ids], free_pick


def _run_batch(args, client: ApiClient, tasks_root: Path, active: list[dict],
               telemetry: RunnerTelemetry | None = None) -> int:
    """Run a non-empty held batch serially: one version-pin check covers the
    whole batch (a single local checkout serves every cell; it sys.exit's on
    a mismatch unless --allow-task-drift), then per-cell confirm/skip/run."""
    blocked_ids = pending.assignment_ids(HOME)
    active = [a for a in active if a.get("assignment_id") not in blocked_ids]
    if not active:
        print("all held assignments already have durable pending results; refusing to rerun")
        return 1
    tasks_root, local_commit = _version_pinned_tasks_root(
        active[0].get("deep_swe_commit"), tasks_root, args.allow_task_drift,
    )
    _ack_fleet_startup_ready(args)

    n = len(active)
    if n > 1:
        print(f"you're holding {n} cells — running them one at a time "
              "(Ctrl-C anytime; unrun cells auto-release):")
    results = []
    for i, assignment in enumerate(active, 1):
        if telemetry:
            telemetry.bind_batch(assignment.get("batch_id"))
        if n > 1:
            print(f"\n=== cell {i}/{n} ===")
        _print_assignment(assignment)
        if not args.dev_agent and assignment.get("est_quota_pct"):
            print("  it's your call whether you have room for this — dradar doesn't track "
                  "your subscription usage. If you don't finish before the lease expires, "
                  "the cell just reopens for someone else and nothing is counted.")
        if not args.yes:
            prompt = "run it now? [y/N]" + (" (or 's' to skip this one)" if n > 1 else "") + " "
            answer = input(prompt).strip().lower()
            if n > 1 and answer == "s":
                print("skipped (its lease stays active; `dradar resume` to come back "
                      "or `dradar release` to give it back)")
                continue
            if answer != "y":
                print("aborted (remaining leases stay active; use `dradar resume` "
                      "to continue or `dradar release` to give them back)")
                return 1
        if telemetry:
            telemetry.set_phase(
                "preparing", assignment["assignment_id"],
                assignment.get("owner_epoch"),
            )
            # Make the session/assignment relationship visible before the
            # subprocess can start or fail. assignment/started then stamps
            # started_at + this same session id in one server transaction.
            telemetry.flush()
        outcome = _run_and_submit(
            client, assignment, tasks_root, args, local_commit, telemetry=telemetry)
        boundary_recorded = _record_assignment_boundary(args, assignment, outcome)
        results.append(outcome)
        if telemetry:
            if outcome == "cleanup-unconfirmed":
                telemetry.set_phase(
                    "paused", assignment["assignment_id"],
                    assignment.get("owner_epoch"),
                )
            else:
                telemetry.set_phase("queued")
        if not boundary_recorded:
            break
        cleanup_blocked = getattr(args, "_docker_cleanup_blocked", None)
        if cleanup_blocked:
            if getattr(args, "refill", False):
                refill_plan.stop(HOME, "local Docker cleanup was not confirmed")
            print(
                "本题结果已保存，但本机运行环境没有清理完整；已停止继续领取或运行下一题。"
            )
            print(f"  -> {cleanup_blocked}")
            results.append(outcome)
            break
        if outcome == "cleanup-unconfirmed":
            if getattr(args, "worker_child", False):
                print(
                    "quarantining this worker slot; sibling slots may continue "
                    "without starting another held task here"
                )
                return _WORKER_SLOT_QUARANTINED_EXIT_CODE
            break
        if outcome in _ACCOUNT_TERMINAL_OUTCOMES:
            _announce_account_stop(outcome)
            break
        if outcome == "environment-build-failed":
            print(
                "stopping this batch after repeated environment setup failures; "
                "no later cell will be started. Fix Docker/network/Pier, then run "
                "`dradar resume`."
            )
            break
        if outcome == "task-content-mismatch":
            print(
                "stopping this batch before later cells use the same mismatched "
                "task checkout"
            )
            break
    ok = all(o in _NON_FAULT_RUNNER_OUTCOMES for o in results)
    return 0 if ok else 1


def _run_checkout_loop(args, client: ApiClient, tasks_root: Path,
                       active: list[dict],
                       telemetry: RunnerTelemetry | None = None) -> int | None:
    """The parallel-safe run loop: repeatedly ask the server to atomically
    check out the next not-yet-started cell, run it, repeat until drained.
    N sessions (or machines) doing this concurrently partition the held
    batch instead of racing over a shared snapshot. Returns None when the
    server predates the checkout endpoint — the caller falls back to the
    legacy whole-batch flow."""
    tasks_root, local_commit = _version_pinned_tasks_root(
        active[0].get("deep_swe_commit"), tasks_root, args.allow_task_drift,
    )
    _ack_fleet_startup_ready(args)
    results, failed_ids = [], set()
    batch_assignment_ids = {
        item["assignment_id"] for item in active if item.get("assignment_id")
    }
    while True:
        if not _worker_slot_is_enabled():
            print("worker slot retired by the live pool target; leaving after current work")
            break
        pool_abort_reason = _pool_abort_reason()
        if pool_abort_reason:
            print(f"worker pool stopped before another checkout: {pool_abort_reason}")
            break
        if getattr(args, "refill", False) and not refill_plan.is_running(HOME):
            print("continuous refill is stopped; leaving already held tasks for a later resume")
            break
        degraded_exclusions = _pool_degraded_exclusions(client)
        if degraded_exclusions is None:
            results.append("degraded-pool-inventory-failed")
            break
        checkout_exclusions = (
            failed_ids | degraded_exclusions
            | (pending.assignment_ids(HOME) & batch_assignment_ids)
        )
        try:
            # A failed local cell is marked stopped so it is retryable later,
            # but this session must not immediately take the same cell again.
            # The server applies this exclusion before stamping started_at,
            # allowing the loop to keep draining other waiting cells.
            if telemetry:
                if telemetry.flush_for_checkout():
                    print("this device was asked to stop before another checkout")
                    break
                data = client.checkout(
                    exclude_assignment_ids=checkout_exclusions,
                    session_id=telemetry.session_id,
                )
            else:
                data = client.checkout(
                    exclude_assignment_ids=checkout_exclusions,
                )
        except ApiError as exc:
            if (telemetry and exc.status_code == 409
                    and exc.code != "runner_session_capacity_reached"
                    and "runner session is not registered or already closed"
                    in str(exc)):
                # A first heartbeat and checkout can cross on a very fast
                # machine. Serialize one fresh heartbeat and retry checkout
                # exactly once; no assignment was stamped by the rejected
                # transaction, so this retry cannot duplicate work.
                telemetry.flush_for_checkout()
                try:
                    data = client.checkout(
                        exclude_assignment_ids=checkout_exclusions,
                        session_id=telemetry.session_id,
                    )
                except ApiError as retry_exc:
                    _exit_for(retry_exc)
            elif exc.status_code == 404:
                return None if not results else 0  # old server / endpoint gone
            else:
                _exit_for(exc)
        assignment = data.get("assignment")
        if not assignment:
            if getattr(args, "refill", False):
                refill_plan.complete_if_empty(HOME, int(data.get("held") or 0))
            if not results:
                print("nothing left to start — every held cell is already "
                      "checked out (another session?) or submitted. "
                      "`dradar leases` shows exactly what is still held.")
            break
        if telemetry:
            # Checkout has already bound this assignment to the exact runner
            # session.  Preserve that fence immediately, before any local
            # provider/runtime preparation can fail.  Waiting until the model
            # process starts leaves pre-model failures unable to call the
            # owner-fenced stopped endpoint, stranding a phantom running cell.
            assignment["_runner_session_id"] = telemetry.session_id
        if assignment["assignment_id"] in checkout_exclusions:
            # Compatibility with an older server that ignores the exclusion
            # field: checkout just stamped this cell started again. Undo that
            # stamp before stopping, otherwise `resume` reports nothing to do
            # while the UI shows a permanently running cell (incident
            # 019f656c-cf16-70e2-ae4c-d1d51146acb2, 2026-07-15).
            _mark_stopped_quietly(client, assignment)
            print(
                f"stopping after excluded assignment {assignment['task_id']} "
                "re-entered checkout — it already failed in this session or "
                "was held before the pool failure. Its checkout stamp was "
                "returned; run a fresh `dradar resume` after inspecting the "
                "earlier failure."
            )
            break
        try:
            assignment_boundary.add_expected(
                _assignment_boundary_path(args), [assignment],
            )
        except (assignment_boundary.BoundaryError, OSError) as exc:
            _mark_stopped_quietly(
                client, assignment, defer_seconds=0,
                failure_kind="runner_failed",
            )
            print(
                f"refusing to start {assignment['assignment_id']}: {exc}. "
                "The checkout stamp was returned and the worker is stopping."
            )
            results.append("assignment-boundary-failed")
            break
        if not _record_worker_checkout(assignment["assignment_id"]):
            _mark_stopped_quietly(
                client, assignment, defer_seconds=0,
                failure_kind="runner_failed",
            )
            print(
                "worker checkout could not be recorded for safe supervision; "
                "the assignment was returned before the model started"
            )
            results.append("worker-activity-unrecorded")
            break
        extra = data.get("unstarted")
        if telemetry:
            telemetry.bind_batch(assignment.get("batch_id"))
            telemetry.set_phase(
                "preparing", assignment["assignment_id"],
                assignment.get("owner_epoch"),
            )
        print(f"\n=== checked out {assignment['task_id']} "
              f"{assignment['model']}@{assignment['effort']}"
              + (f" · {extra} more waiting" if extra else "") + " ===")
        _print_assignment(assignment)
        if not args.dev_agent and assignment.get("est_quota_pct"):
            print("  it's your call whether you have room for this — dradar doesn't track "
                  "your subscription usage. If you don't finish before the lease expires, "
                  "the cell just reopens for someone else and nothing is counted.")
        outcome = _run_and_submit(
            client, assignment, tasks_root, args, local_commit, telemetry=telemetry)
        boundary_recorded = _record_assignment_boundary(args, assignment, outcome)
        if telemetry:
            if outcome == "cleanup-unconfirmed":
                telemetry.set_phase(
                    "paused", assignment["assignment_id"],
                    assignment.get("owner_epoch"),
                )
            else:
                telemetry.set_phase("queued")
        if not boundary_recorded:
            results.append(outcome)
            break
        cleanup_blocked = getattr(args, "_docker_cleanup_blocked", None)
        if cleanup_blocked:
            if getattr(args, "refill", False):
                refill_plan.stop(HOME, "local Docker cleanup was not confirmed")
            print(
                "本题结果已保存，但本机运行环境没有清理完整；已停止继续领取或运行下一题。"
            )
            print(f"  -> {cleanup_blocked}")
            results.append(outcome)
            break
        if outcome == "cleanup-unconfirmed":
            results.append(outcome)
            if getattr(args, "worker_child", False):
                print(
                    "quarantining this worker slot; sibling slots may continue "
                    "without checking out another task here"
                )
                return _WORKER_SLOT_QUARANTINED_EXIT_CODE
            break
        if outcome in _ACCOUNT_TERMINAL_OUTCOMES:
            if getattr(args, "refill", False):
                refill_plan.stop(HOME, f"account stop: {outcome}")
            _announce_account_stop(outcome)
            results.append(outcome)
            break
        if outcome == "environment-build-failed":
            if getattr(args, "refill", False):
                refill_plan.stop(HOME, "local environment build failed")
            _signal_pool_abort(
                "local environment build failed", interrupt_siblings=False,
            )
            print(
                "stopping this worker before the next checkout after repeated "
                "environment setup failures. Fix Docker/network/Pier, then run "
                "`dradar resume`."
            )
            results.append(outcome)
            break
        configured_fail_fast = os.environ.get(
            "DRADAR_BATCH_FAIL_FAST", "",
        ).lower() in {
            "1", "true", "yes", "on",
        }
        if getattr(args, "refill", False):
            if outcome not in _NON_FAULT_RUNNER_OUTCOMES:
                refill_plan.stop(HOME, f"task outcome={outcome}")
                print(f"continuous refill stopped after outcome={outcome}; no new tasks "
                      "will be claimed, and existing leases stay untouched")
                results.append(outcome)
                break
            if outcome in ("submitted", "interrupted"):
                refill_plan.mark_submitted(HOME, assignment["assignment_id"])
            try:
                _sync_worker_refill_target()
            except refill_plan.RefillError as exc:
                refill_plan.stop(HOME, "live worker target synchronization failed")
                print(
                    "continuous refill stopped because the live worker target "
                    f"could not be synchronized ({exc})"
                )
            replenished = None
            runtime_cfg = _run_config(args)
            maintenance_allows_refill = True
            if (getattr(args, "worker_child", False)
                    and image_cache.claim_periodic_maintenance(
                        HOME,
                        interval_seconds=_POOL_IMAGE_CACHE_MAINTENANCE_SECONDS,
                    )):
                maintenance_allows_refill = _maintain_image_cache(
                    client, runtime_cfg, phase="during worker pool",
                )
            if (not maintenance_allows_refill
                    or not _disk_allows_refill(runtime_cfg)):
                refill_plan.stop(HOME, "disk free below image-cache safety floor")
                print("continuous refill stopped before claiming another task: disk free "
                      "is below the 25 GiB safety floor. Existing work stays held; run "
                      "`dradar cleanup --docker --dry-run` first.")
            else:
                try:
                    replenished = refill_plan.refill_once(HOME, client)
                except ApiError as exc:
                    # One attempt per completed task is naturally bounded by task
                    # duration. Do not busy-loop; existing held work remains safe.
                    print(f"auto-refill unavailable for now ({exc}); continuing the held queue "
                          "without retrying in a tight loop")
                    replenished = None
            if replenished is not None:
                claimed = replenished.get("claimed", 0)
                held = replenished.get("held", data.get("held", "?"))
                target = (refill_plan.load(HOME) or {}).get("refill_to", "?")
                progress = (
                    "submitted 1 task"
                    if outcome in ("submitted", "interrupted")
                    else f"isolated task outcome={outcome}"
                )
                if claimed:
                    print(f"{progress}; held {held}/{target}; auto-claimed {claimed}")
                elif replenished.get("seed_pending"):
                    print(f"{progress}; waiting for "
                          f"{replenished['seed_pending']} selected task(s) before auto-refill")
                elif replenished.get("status") == "draining":
                    print("refill limit reached; no more tasks will be claimed, "
                          "draining the existing queue")
                elif replenished.get("status") == "stopped":
                    print(f"continuous refill stopped: "
                          f"{replenished.get('reason') or 'safety limit reached'}")
                elif replenished.get("waiting_for_inventory"):
                    print("no open cells match the refill scope right now; no "
                          "other harness was claimed. The plan remains saved; "
                          "run the same `dradar resume` command later to continue.")
        worker_failure = (
            getattr(args, "worker_child", False)
            and outcome not in _NON_FAULT_RUNNER_OUTCOMES
        )
        continuous_claim_failure = (
            (getattr(args, "auto", None) is not None or len(active) > 1)
            and outcome not in _NON_FAULT_RUNNER_OUTCOMES
        )
        configured_failure = configured_fail_fast and outcome != "submitted"
        if worker_failure or continuous_claim_failure or configured_failure:
            # Large operator-managed batches should fail closed: continuing to
            # drain the queue turned one shared proxy incident into 27 invalid
            # submissions on 2026-07-16. Supervised children also fail closed
            # so their parent can drain healthy siblings without respawning a
            # worker onto the same cooled-down assignment.
            print(f"stopping this automatic batch runner after outcome={outcome} — "
                  "fix the agent/network issue before resuming; no later task "
                  "will be checked out")
            results.append(outcome)
            break
        if outcome == "failed" or outcome in _TERMINAL_LOCAL_OUTCOMES:
            failed_ids.add(assignment["assignment_id"])
        results.append(outcome)
    ok = all(o in _NON_FAULT_RUNNER_OUTCOMES for o in results)
    return 0 if ok else 1


def _prompt_positive_int(prompt: str, default: int) -> int:
    raw = input(f"{prompt} [{default}]: ").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise refill_plan.RefillError(f"{prompt} must be a positive integer")
    return value


def _setup_refill(args, client: ApiClient, active: list[dict], free_pick: bool) -> list[dict]:
    """Configure a plan that drains its initial selected batch before refill."""
    explicit = getattr(args, "refill", False)
    if any(item.get("billing_mode") == "api" for item in active):
        if explicit:
            raise refill_plan.RefillError(
                "continuous refill is unavailable for paid-API assignments; "
                "run the DeepSeek task as an explicit one-off"
            )
        return active
    if not explicit and not args.yes and free_pick and active:
        answer = input(
            f"run your {len(active)} selected task(s) first, then keep auto-refilling? [y/N] "
        ).strip().lower()
        if answer not in ("y", "yes"):
            return active
        args.refill = True
        args.refill_to = _prompt_positive_int("held queue target", len(active))
        args.max_tasks = DEFAULT_REFILL_TASK_SAFETY_CAP
        tier = input("quota tier [plus/pro-5x/pro-20x] [plus]: ").strip().lower()
        args.quota_tier = tier or "plus"
        quota = input("estimated 7-day quota cap in percent (required): ").strip()
        try:
            args.max_estimated_quota_pct = float(quota)
        except ValueError as exc:
            raise refill_plan.RefillError(
                "estimated quota cap is required and must be a number"
            ) from exc
        explicit = True
    if not explicit:
        return active
    if not free_pick:
        raise refill_plan.RefillError("continuous refill is not available on this server")

    try:
        me = client.whoami()
    except ApiError as exc:
        raise refill_plan.RefillError(f"couldn't read account refill limits: {exc}") from exc
    if me.get("claim_limit") is None or me.get("concurrent_limit") is None:
        raise refill_plan.RefillError(
            "this server is too old for safe continuous refill; ordinary go/resume is unchanged"
        )
    requested = (
        getattr(args, "refill_to", None)
        or getattr(args, "auto", None)
        or len(active)
        or 1
    )
    target = min(int(requested), int(me["claim_limit"]), int(args.max_tasks))
    if target != requested:
        print(f"refill target {requested} exceeds the applicable claim/task limit; using {target}")
    if args.max_tasks is None or args.max_tasks < len(active):
        raise refill_plan.RefillError(
            f"--max-tasks must be at least the {len(active)} task(s) already held"
        )
    if args.quota_tier not in refill_plan.TIERS:
        raise refill_plan.RefillError(f"unknown quota tier: {args.quota_tier}")
    if args.max_estimated_quota_pct is not None and args.max_estimated_quota_pct <= 0:
        raise refill_plan.RefillError("estimated quota cap must be greater than zero")

    print("continuous refill plan:")
    print(f"  held queue target: {target} (server claim limit {me['claim_limit']})")
    print(f"  server concurrent limit: {me['concurrent_limit']}")
    print(f"  internal task safety cap: {args.max_tasks}")
    if getattr(args, "refill_harness", None):
        scope = args.refill_harness
        if getattr(args, "refill_model", None):
            scope += f"/{args.refill_model}"
        if getattr(args, "refill_effort", None):
            scope += f"@{args.refill_effort}"
        print(f"  exact refill scope: {scope} (no cross-harness fallback)")
        print("  candidate order: " + (
            "least historical graded runs first"
            if getattr(args, "refill_order", None) == "least-run"
            else "estimated cost first"
        ))
    if args.max_estimated_quota_pct is not None:
        print(f"  estimated quota cap: {args.max_estimated_quota_pct}% {args.quota_tier}")
    print("  order: all initially selected tasks must submit before auto-refill starts")
    print("  safety: any non-submitted task stops refill; existing work is never released")
    if not args.yes:
        answer = input("start this refill plan? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("continuous refill not started; running only the selected tasks")
            args.refill = False
            return active

    server_campaign_id = None
    points_tier = None
    if getattr(args, "fleet_pool", False):
        if not getattr(args, "batch_id", None):
            raise refill_plan.RefillError(
                "Fleet refill requires an exact seed batch ID"
            )
        if not all(
            getattr(args, name, None)
            for name in ("refill_harness", "refill_model", "refill_effort")
        ):
            raise refill_plan.RefillError(
                "Fleet refill requires exact Harness, model, and effort scope"
            )
        runtime = _run_config(args)
        if getattr(args, "credentials_file", None):
            points_tier = runtime.get("run_plan_points_tier")
            if points_tier not in refill_plan.TIERS:
                raise refill_plan.RefillError(
                    "private run-plan credentials lack an authorized points tier"
                )
        try:
            configured = client.configure_refill_campaign(
                batch_id=args.batch_id,
                harness=args.refill_harness,
                model=args.refill_model,
                effort=args.refill_effort,
                refill_to=target,
                max_tasks=args.max_tasks,
            )
        except ApiError as exc:
            raise refill_plan.RefillError(
                f"server refused the exact Fleet refill campaign: {exc}"
            ) from exc
        campaign = configured.get("campaign") or {}
        if campaign.get("batch_id") != args.batch_id:
            raise refill_plan.RefillError(
                "server returned a mismatched Fleet refill campaign"
            )
        server_campaign_id = args.batch_id

    plan = refill_plan.configure(
        HOME,
        volunteer_id=me.get("volunteer_id", "unknown"),
        refill_to=target,
        max_tasks=args.max_tasks,
        quota_tier=args.quota_tier,
        max_estimated_quota_pct=args.max_estimated_quota_pct,
        active=active,
        refill_harness=getattr(args, "refill_harness", None),
        refill_model=getattr(args, "refill_model", None),
        refill_effort=getattr(args, "refill_effort", None),
        refill_order=getattr(args, "refill_order", None) or "cost",
        server_campaign_id=server_campaign_id,
        points_tier=points_tier,
        # A normal parent owns the exclusive per-machine run lock here, so no
        # live local campaign can be displaced. Manual --parallel sessions do
        # not own that proof and must keep the fail-closed conflict behavior.
        replace_existing=explicit and not getattr(args, "parallel", False),
    )
    if plan.get("replaced_plan_id"):
        print("replaced a stale earlier refill configuration with the "
              "newly confirmed limits")
    args.yes = True  # the one campaign confirmation replaces per-task prompts
    try:
        result = refill_plan.refill_once(HOME, client)
    except ApiError as exc:
        refill_plan.stop(HOME, f"initial refill failed: {exc}")
        raise refill_plan.RefillError(
            f"initial refill request failed ({exc}); selected tasks remain held"
        ) from exc
    if result.get("claimed"):
        print(f"initial auto-refill claimed {result['claimed']} task(s); "
              f"held {result.get('held', '?')}/{target}")
    elif result.get("seed_pending"):
        print(f"selected batch locked first: auto-refill starts after all "
              f"{result['seed_pending']} selected task(s) submit")
    elif result.get("status") == "stopped":
        raise refill_plan.RefillError(result.get("reason") or "refill plan stopped")
    # Return the authoritative post-refill batch, including claims accepted by
    # another local worker while this process waited for the shared plan lock.
    refreshed, _ = _acquire_batch(
        client,
        True,
        allow_empty_exact_campaign=server_campaign_id is not None,
    )
    return refreshed


def _scoped_fleet_refill(args) -> bool:
    return bool(
        getattr(args, "fleet_pool", False)
        and getattr(args, "refill", False)
        and getattr(args, "batch_id", None)
        and getattr(args, "credentials_file", None)
    )


def _wait_for_scoped_refill_work(
    args,
    client: ApiClient,
    *,
    desired_workers: int,
) -> list[dict]:
    """Keep one exact run-plan device healthy across an empty refill gap.

    No model child exists during this phase. A normal runner heartbeat is not
    sufficient because the exact batch may contain zero live assignments and
    the server deliberately avoids creating an empty runner session. Instead,
    replay the same logical device's idempotent run-plan start at the bounded
    polling cadence. This refreshes device liveness without reserving another
    worker or widening the plan. The shared drain marker remains authoritative
    for a local stop request.
    """

    if not _scoped_fleet_refill(args):
        return []
    announced = False
    while True:
        reason = _pool_abort_reason()
        if reason:
            print(f"continuation wait stopped: {reason}")
            return []
        try:
            runtime = _run_config(args)
            plan_id = runtime.get("run_plan_id")
            logical_session_id = runtime.get("run_plan_logical_session_id")
            if (
                not isinstance(plan_id, str) or not plan_id
                or not isinstance(logical_session_id, str)
                or not logical_session_id.startswith("drl_")
            ):
                raise refill_plan.RefillError(
                    "private run-plan credentials lack a stable device session"
                )
            refresh = client.start_run_plan(
                plan_id=plan_id,
                logical_session_id=logical_session_id,
                concurrency_mode="fixed",
                concurrency=desired_workers,
            )
            envelope = refresh.get("envelope") if isinstance(refresh, dict) else None
            if not isinstance(envelope, dict):
                raise refill_plan.RefillError(
                    "server returned an invalid run-plan device status"
                )
            if envelope.get("decision_required"):
                raise refill_plan.RefillError(
                    "the server unexpectedly requires a new device decision"
                )
            if envelope.get("agent_action") in {"stop_runner", "done"}:
                return []

            scoped_pending = _pending_uploads_for_batch(args.batch_id)
            blocked_pending = [
                entry for entry in scoped_pending if entry.get("upload_blocked")
            ]
            if blocked_pending:
                raise SystemExit(
                    "a completed result on this device needs upload review; "
                    "no new model work will start until it is resolved"
                )
            if scoped_pending:
                _retry_pending_uploads(client, batch_id=args.batch_id)
                pending_after_retry = _pending_uploads_for_batch(args.batch_id)
                blocked_after_retry = [
                    entry for entry in pending_after_retry
                    if entry.get("upload_blocked")
                ]
                if blocked_after_retry:
                    raise SystemExit(
                        "a completed result on this device needs upload review; "
                        "no new model work will start until it is resolved"
                    )
                if pending_after_retry:
                    if not announced:
                        print(
                            "a completed result is waiting to upload; this "
                            "device will retry it before starting another task"
                        )
                        announced = True
                    time.sleep(_SCOPED_REFILL_WAIT_SECONDS)
                    continue
            result = refill_plan.refill_once(HOME, client)

            status = result.get("status")
            active, _ = _acquire_batch(
                client,
                True,
                allow_new_claims=False,
                allow_empty_exact_campaign=True,
            )
            # Exact plan inventory already distinguishes waiting assignments
            # from work owned by another device. Do not subtract the other
            # device's live workers from this device's local target: the server
            # has separately reserved each device's concurrency and atomic
            # checkout remains the final partitioning boundary.
            ready = _pool_ready_work_count(client)
            if active and ready:
                return active
            if status in {"stopped", "completed"} or (
                status == "draining" and not active
            ):
                return []
            if not announced:
                if result.get("seed_pending"):
                    print(
                        "selected work is still finishing across the active "
                        "devices; this device will wait for the shared queue"
                    )
                else:
                    print(
                        "no matching task is open right now; this device will "
                        "stay ready and continue automatically"
                    )
                announced = True
            time.sleep(_SCOPED_REFILL_WAIT_SECONDS)
        except ApiError as exc:
            # Retry only transport failures and explicitly retryable HTTP
            # responses. Authentication, scope, expiry and other 4xx failures
            # are terminal so an invalid device cannot look healthy forever.
            transient = (
                exc.status_code is None
                or exc.status_code in {408, 425, 429}
                or (exc.status_code is not None and exc.status_code >= 500)
            )
            if not transient:
                raise SystemExit(
                    "the exact continuation is no longer authorized on "
                    f"this device ({exc}); no model was started"
                ) from exc
            if not announced:
                print(
                    "the next matching task is temporarily unavailable "
                    f"({exc}); this device will keep waiting safely"
                )
                announced = True
            retry_after = getattr(exc, "retry_after", None)
            delay = _SCOPED_REFILL_WAIT_SECONDS
            if isinstance(retry_after, (int, float)) and retry_after > delay:
                delay = min(float(retry_after), 600.0)
            time.sleep(delay)
        except refill_plan.RefillError as exc:
            raise SystemExit(
                "the exact continuation status was invalid; no model was "
                f"started ({exc})"
            ) from exc


def _prepare_batch(args, client: ApiClient) -> tuple[list[dict], bool]:
    """Claim/configure once, shared by the serial and supervised run paths."""
    allow_new_claims = getattr(args, "allow_new_claims", True)
    wants_refill = getattr(args, "refill", False)
    active, free_pick = _acquire_batch(
        client, args.yes, allow_new_claims=allow_new_claims,
        allow_empty_exact_campaign=(
            bool(getattr(args, "fleet_pool", False))
            and bool(wants_refill)
            and bool(getattr(args, "batch_id", None))
        ),
    )
    wants_pick = getattr(args, "pick", None)
    auto_target = getattr(args, "auto", None)
    wants = wants_pick or auto_target is not None
    if not allow_new_claims and wants:
        print("disk safety floor reached — not claiming new tasks; already held work "
              "can still run. Use `dradar cleanup --docker --dry-run` to inspect cleanup.")
    elif not allow_new_claims and not active:
        print("disk safety floor reached — not claiming a new task. Existing leases are "
              "unchanged; use `dradar cleanup --docker --dry-run` to inspect cleanup.")
    elif free_pick and wants_pick:
        try:
            active = _top_up_picks(client, active, wants_pick)
        except ApiError as exc:
            _exit_for(exc)
    elif free_pick and auto_target is not None and not wants_refill:
        # --auto is a target batch size, not "claim N more": preserve existing
        # leases and ask only for the shortfall. The server keeps the ordinary
        # account cap while configured super accounts may request larger pools.
        missing = max(0, auto_target - len(active))
        if missing:
            try:
                active += _claim_auto(client, missing)
            except ApiError as exc:
                _exit_for(exc)
        else:
            print(f"already holding {len(active)} cell(s) — --auto target "
                  f"{auto_target} already met")
    if not allow_new_claims and wants_refill:
        refill_plan.stop(HOME, "disk free below image-cache safety floor")
        args.refill = False
        print("continuous refill not started because disk space is below the safety floor")
    elif getattr(args, "worker_child", False):
        # The parent configured the shared plan before launching us. Rewriting
        # it from every child would reset its counters and race its file lock.
        if wants_refill and not refill_plan.is_running(HOME):
            saved = refill_plan.load(HOME)
            if saved and saved.get("status") == refill_plan.FAULTED_STATE:
                print(
                    "continuous refill circuit is open; this worker will not "
                    "check out or run another held task"
                )
                args.refill = False
                return [], free_pick
            print("continuous refill plan is no longer active; draining held tasks only")
            args.refill = False
    else:
        try:
            active = _setup_refill(args, client, active, free_pick)
        except refill_plan.RefillCircuitOpen as exc:
            print(f"continuous refill not started: {exc}")
            args.refill = False
            return [], free_pick
        except refill_plan.RefillError as exc:
            # Setup validation belongs to this invocation. It must never mutate
            # an already-active shared plan owned by other parallel workers.
            if getattr(args, "fleet_pool", False):
                raise SystemExit(
                    f"exact Fleet refill campaign was not started: {exc}"
                ) from exc
            print(f"continuous refill not started: {exc}")
            args.refill = False
    if not active:
        saved_refill = refill_plan.load(HOME) if wants_refill else None
        if (saved_refill and saved_refill.get("status") == "active"
                and saved_refill.get("refill_harness")):
            if _scoped_fleet_refill(args):
                print(
                    "no matching task is open yet; the supervised device will "
                    "stay ready for this exact continuation"
                )
            else:
                print("no open cells match the refill scope right now; no other "
                      "harness was claimed. The plan is saved; run the same "
                      "`dradar resume` command later to continue.")
        elif free_pick and wants:
            print("nothing claimed — try again, or pick on the radar page instead.")
        elif free_pick:
            print("no cells claimed — pick some on the radar page, then paste the "
                  "command it gives you (or run `dradar go` again after claiming), "
                  "or use `dradar go --auto` / `--pick` to claim straight from the CLI.")
        elif getattr(args, "resume", False):
            print("nothing to resume — no active lease (it may have expired). Run `dradar go`.")
        else:
            print("no work available right now — thank you, check back later")
        return [], free_pick
    return active, free_pick


def _go_menu(args, cfg: dict, client: ApiClient, tasks_root: Path,
             telemetry: RunnerTelemetry | None = None) -> int:
    """Prepare a held batch and run it through atomic checkout when possible."""
    active, free_pick = _prepare_batch(args, client)
    if not active:
        return 0
    boundary_path = _assignment_boundary_path(args)
    if cfg.get("benchmark"):
        boundary_path = _prepare_assignment_boundary(
            args, client, cfg["benchmark"], active,
        )
        try:
            assignment_boundary.add_expected(boundary_path, active)
        except (assignment_boundary.BoundaryError, OSError) as exc:
            print(f"assignment boundary check failed: {exc}. No model was started.")
            return 1
    if telemetry:
        telemetry.bind_batch(active[0].get("batch_id"))
    # Non-interactive free-pick runs go through the parallel-safe checkout
    # loop (the standard paste-command path). Interactive runs keep the
    # legacy batch flow — its per-cell confirm/skip prompts don't translate
    # to a dispenser that stamps cells at checkout time.
    if free_pick and args.yes:
        rc = _run_checkout_loop(args, client, tasks_root, active, telemetry=telemetry)
        if rc is not None:
            return rc
        if getattr(args, "refill", False):
            refill_plan.stop(HOME, "server has no atomic checkout endpoint")
            print("continuous refill stopped: this server lacks atomic checkout support")
            return 1
    rc = _run_batch(args, client, tasks_root, active, telemetry=telemetry)
    # Free-pick: the batch was a snapshot taken at startup, but the classic
    # first-session flow is "paste the command, then go claim more on the
    # page while it runs" — those later claims used to be silently ignored
    # until the next manual `dradar resume` (volunteer report, 2026-07-13).
    # Re-fetch until nothing NEW appears; `seen` keeps a deliberately-skipped
    # cell from being re-prompted in a loop (it stays held for a later
    # resume). Menu-mode instances keep their one-cell-per-run contract.
    seen = {a["assignment_id"] for a in active}
    while rc == 0 and free_pick:
        active, _ = _acquire_batch(client, args.yes)
        fresh = [a for a in active if a["assignment_id"] not in seen]
        if not fresh:
            break
        seen.update(a["assignment_id"] for a in fresh)
        try:
            assignment_boundary.add_expected(boundary_path, fresh)
        except (assignment_boundary.BoundaryError, OSError) as exc:
            print(
                f"could not extend the assignment boundary ({exc}); "
                "the newly claimed task(s) were left untouched"
            )
            return 1
        print(f"\n{len(fresh)} more cell(s) were claimed while that batch ran — continuing:")
        rc = _run_batch(args, client, tasks_root, fresh, telemetry=telemetry)
    return rc


__all__ = ["cmd_go", "_go_menu",
           "_run_and_submit", "_check_version_pin", "_claim_from_menu",
           "_choose_menu_entry", "_print_menu", "_print_assignment",
           "cmd_retry_upload", "_retry_pending_uploads", "_upload_trial",
           "_artifacts_from_trial_dir"]
