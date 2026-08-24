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

import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import (
    __version__, artifact_staging, checkpoints, egress, failure_circuit,
    image_cache, pending, refill as refill_plan,
)
from .api_client import ApiClient, ApiError
from .identity import _client
from .local_config import (
    DEFAULT_BENCHMARK, HOME, _load_config, tasks_root_from_config,
)
from .machine import acquire_run_lock, sweep_orphan_compose
from .providers import (
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
    ZCODE_AGENT,
    ZCODE_RUN_CONFIG_VERSION,
    ZCODE_RUNTIME_PROFILE,
    PAID_API_REFILL_AGENTS,
    SUBSCRIPTION_REFILL_AGENTS,
    assignment_codex_provider,
    validate_refill_scope,
)
from .runner import (
    CODEX_TRAJECTORY_BUNDLE_SCHEMA, DIAG_ADVICE, BuildFlakeError, RunnerError,
    POMPEII_BENCHMARK_ID,
    POMPEII_FINALIZATION_RESERVE_SEC, POMPEII_SOFT_BUDGET_SEC,
    POMPEII_TERMINAL_HEAVY_TIMEOUT_SEC,
    build_codex_trajectory_bundle, build_kimi_trajectory_bundle,
    _recover_completed_checkpoint_patch,
    check_task_content_hash, classify_exception_message,
    codex_trajectory_bundle_usage,
    diagnose_exception, ensure_pier, ensure_tasks_root,
    durable_checkpoint_rollout_enabled, local_deep_swe_commit,
    pompeii_agent_timeout_sec, run_trial,
    summarize_result, sync_deep_swe_commit, trial_artifact_paths,
)
from .scrub import (
    patch_structure_is_valid, redact_patch_secrets, scan_secrets,
    scrub_json_bytes,
)
from .session_archive import archive_after_submit
from .submission_intent import submission_payload_manifest, upload_intent_id
from .telemetry import RunnerTelemetry
from .taskpacks import TaskPackError, ensure_benchmark_task_pack


# Quota is the user-facing campaign limit. Keep a deliberately high internal
# count ceiling as a last-resort guard against corrupt estimates or a logic
# regression; normal quota-bounded plans should never reach it.
DEFAULT_REFILL_TASK_SAFETY_CAP = 1000
_TERMINAL_LOCAL_OUTCOMES = {
    "not-uploaded", "rejected", "task-content-mismatch",
}
_ACCOUNT_TERMINAL_OUTCOMES = {
    "auth-failure", "insufficient-balance", "quota-exhausted",
    "recovery-exhausted", "runtime-incompatible", "provider-preflight-failed",
    "repeat-agent-failure",
}
_POOL_ABORT_ENV = "DRADAR_POOL_ABORT_FILE"
_POOL_TARGET_FILE_ENV = "DRADAR_POOL_TARGET_FILE"
_REPEAT_FAILURE_STATE_ENV = "DRADAR_REPEAT_FAILURE_STATE_FILE"
_POOL_DRAIN_PREFIX = "drain:"
_POOL_SUPERVISOR_POLL_SECONDS = 0.2
_POOL_BACKFILL_REFRESH_SECONDS = 2.0
_POOL_BACKFILL_ERROR_RETRY_SECONDS = 10.0
_POOL_IMAGE_CACHE_MAINTENANCE_SECONDS = 15 * 60
_POOL_TARGET_CACHE: dict[Path, int] = {}
MAX_CHECKPOINT_RESUMES = 5
_CHECKPOINT_BACKOFF_BASE_SECONDS = 30.0
_CHECKPOINT_BACKOFF_MAX_SECONDS = 600.0
_CHECKPOINT_RESUME_REPLAY_ATTEMPTS = 3
_CHECKPOINT_RESUME_REPLAY_DELAY_SECONDS = 0.25


@dataclass(frozen=True)
class _CheckpointPauseFailure:
    """A non-resumable checkpoint failure that stopped automatic refill."""

    family: str
    item: checkpoints.Checkpoint
    discard_confirmed: bool

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
    benchmark = cfg.get("benchmark") or DEFAULT_BENCHMARK
    if benchmark == DEFAULT_BENCHMARK:
        return tasks_root_from_config(cfg)
    return tasks_root_from_config(cfg, benchmark)


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
        "recovery-exhausted": "checkpoint recovery reached its safety limit",
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
        "Unstarted leases and checkpoints remain untouched; fix or wait for "
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


def _checkpoint_backoff_seconds(
    item: checkpoints.Checkpoint, *, generation: int | None = None,
    now: datetime | None = None,
) -> float:
    """Remaining bounded delay before the next checkpoint recovery attempt."""
    effective_generation = (
        item.resume_generation if generation is None else generation
    )
    if effective_generation <= 0:
        return 0.0
    delay = min(
        _CHECKPOINT_BACKOFF_MAX_SECONDS,
        _CHECKPOINT_BACKOFF_BASE_SECONDS * (2 ** (effective_generation - 1)),
    )
    current = now or datetime.now(timezone.utc)
    elapsed = max(0.0, (current - item.updated_at).total_seconds())
    return max(0.0, delay - elapsed)


def _checkpoint_identity_mismatches(
    item: checkpoints.Checkpoint, assignment: dict,
) -> list[str]:
    """Return fail-closed runtime identity differences before lease resume."""

    expected = {
        "task_id": assignment.get("task_id"),
        "model": assignment.get("model"),
        "effort": assignment.get("effort"),
    }
    mismatched = [
        name for name, value in expected.items()
        if getattr(item, name) != value
    ]
    agent = assignment.get("agent")
    provider = assignment_codex_provider(assignment)
    extended_identity_required = (
        provider == DEEPSEEK_PROVIDER
        or agent in {DSH_AGENT, KIMI_AGENT, ZCODE_AGENT}
    )
    if extended_identity_required:
        expected_extended = {
            "harness": "codex" if provider == DEEPSEEK_PROVIDER else agent,
            "provider": assignment.get("provider"),
        }
        mismatched.extend(
            name for name, value in expected_extended.items()
            if getattr(item, name) != value
        )
        requested_version = assignment.get("agent_version")
        if agent != "codex" and (
            not isinstance(requested_version, str)
            or item.agent_version != requested_version
        ):
            mismatched.append("agent_version")
        elif agent == "codex" and not item.agent_version:
            # The exact stable DeepSeek Codex version is resolved immediately
            # before the run. The adapter compares that version again inside
            # the container before restoring provider state.
            mismatched.append("agent_version")
    return list(dict.fromkeys(mismatched))


def _checkpoint_resume_response_mismatches(
    item: checkpoints.Checkpoint,
    assignment: dict,
    resumed: dict,
) -> list[str]:
    """Reject a resume grant that drifts from the authenticated lease."""

    mismatched: list[str] = []
    if resumed.get("checkpoint_id") != item.checkpoint_id:
        mismatched.append("checkpoint_id")
    nonce = resumed.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        mismatched.append("nonce")
    original_nonce = assignment.get("nonce")
    if isinstance(original_nonce, str) and original_nonce and nonce != original_nonce:
        mismatched.append("nonce")
    for name in ("agent", "provider", "model", "effort", "agent_version"):
        if resumed.get(name) != assignment.get(name):
            mismatched.append(name)
    mismatched.extend(_checkpoint_identity_mismatches(item, resumed))
    return list(dict.fromkeys(mismatched))


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
    if exc.status_code is None:
        sys.exit(f"{exc}\ncheck your connection — held leases stay active, and "
                 "`dradar resume` continues where you left off")
    sys.exit(str(exc))


def _check_version_pin(pinned: str | None, tasks_root: Path, allow_drift: bool) -> str | None:
    """Refuse to burn real quota on a checkout the server won't grade the same
    way. The lease stays active across the exit."""
    local_commit = local_deep_swe_commit(tasks_root)
    if pinned and local_commit and local_commit != pinned:
        # Self-heal: fetch + checkout the exact commit the server grades against,
        # rather than making the volunteer do it by hand.
        print(f"deep-swe drifted (local {local_commit[:12]} != server {pinned[:12]}); "
              "syncing to the server's pinned commit...")
        if sync_deep_swe_commit(tasks_root, pinned):
            print(f"  synced to {pinned[:12]}")
            return pinned
        fix = (
            f"  git -C {tasks_root} fetch --depth 1 origin {pinned}\n"
            f"  git -C {tasks_root} checkout {pinned}"
        )
        if not allow_drift:
            sys.exit(
                "couldn't auto-sync your deep-swe checkout to the version this "
                f"server grades against:\n  local:  {local_commit}\n  server: {pinned}\n"
                f"do it by hand, then re-run (the lease stays active):\n{fix}\n"
                "or re-run with --allow-task-drift to proceed anyway (the "
                "submission will be flagged for review)"
            )
        print(
            f"warning: proceeding with task drift (local {local_commit[:12]} != "
            f"server {pinned[:12]}); the submission will be flagged for review"
        )
    return local_commit


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
        "zcode" if meta.get("zcode_cli_version")
        else "kimi-code" if meta.get("kimi_cli_version")
        else "grok" if meta.get("grok_cli_version")
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
        "zcode": {"provider_aggregate_missing_or_invalid"},
        "grok": {
            "terminal_aggregate_missing_or_inconsistent",
            "request_ledger_unavailable_or_invalid",
        },
        "kimi-code": {
            "turn_completion_ledger_mismatch",
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
    if assignment.get("agent") in {DSH_AGENT, GROK_AGENT, KIMI_AGENT}:
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
        # just long enough to ask the volunteer. Superseded checkpoint copies
        # are still removed immediately.
        keep_dir = job_dir if (entry.get("keep", False) or ask_cleanup) else None
        checkpoints.cleanup_assignment(
            HOME, assignment_id, keep_job_dir=keep_dir,
        )

    def settle_terminal_local_failure() -> None:
        """Keep evidence but make a non-retryable local result runnable again."""
        _mark_stopped_quietly(client, entry)
        item = checkpoints.find_latest(HOME, assignment_id)
        if item is not None:
            checkpoints.mark_terminal(HOME, item)
        elif job_dir and job_dir.is_dir():
            try:
                checkpoints.mark_terminal_job(HOME, job_dir)
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

    upload_meta = dict(entry.get("meta") or {})
    trial_dir = Path(entry["trial_dir"])
    trajectory_bundle = build_codex_trajectory_bundle(trial_dir)
    if trajectory_bundle is None:
        trajectory_bundle = build_kimi_trajectory_bundle(trial_dir)
    usage = (
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
            "provider_actual_cost_observed", "cost_semantics",
            "completed_turn_count", "turn_prompt_count",
            "cache_creation_tokens", "subscription_reported_cost_usd",
            "subscription_reported_cost_basis", "resume_attempts",
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
        while True:
            submit_kwargs = {
                "outcome": outcome,
                "resume_generation": entry.get("resume_generation"),
            }
            if submit_bundle is not None:
                submit_kwargs["trajectory_bundle"] = submit_bundle
            runner_session_id = entry.get("runner_session_id")
            if runner_session_id:
                manifest = submission_payload_manifest(
                    assignment_id=assignment_id,
                    session_id=runner_session_id,
                    resume_generation=int(entry.get("resume_generation", 0)),
                    outcome=outcome,
                    meta=upload_meta,
                    patch=upload_patch,
                    trajectory=traj_scrubbed,
                    result=result_scrubbed,
                    trajectory_bundle=submit_bundle,
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
                    return "upload-failed"
                try:
                    registered_intent_id = client.register_submission_upload_intent(
                        assignment_id,
                        entry["nonce"],
                        runner_session_id,
                        int(entry.get("resume_generation", 0)),
                        calculated_intent_id,
                    )
                except ApiError as exc:
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
                    submit_kwargs["upload_intent_id"] = calculated_intent_id
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
        item = checkpoints.find_latest(HOME, assignment_id)
        if item is not None and item.job_dir.resolve() == job_dir.resolve():
            checkpoints.mark_kept(HOME, item)
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
                item = checkpoints.find_latest(HOME, assignment_id)
                if item is not None and item.job_dir.resolve() == job_dir.resolve():
                    checkpoints.mark_kept(HOME, item)
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
    lease as apparently resumable with no checkpoint behind it.
    """
    assignment_id = (
        assignment if isinstance(assignment, str) else assignment["assignment_id"]
    )
    resume_generation = (
        None if isinstance(assignment, str) else assignment.get("resume_generation")
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            stop_kwargs = {"defer_seconds": defer_seconds}
            if resume_generation is not None:
                stop_kwargs["resume_generation"] = resume_generation
            if failure_kind is not None:
                stop_kwargs["failure_kind"] = failure_kind
            if failure_diagnostic is not None:
                stop_kwargs["failure_diagnostic"] = failure_diagnostic
            client.mark_stopped(assignment_id, **stop_kwargs)
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


def _discard_checkpoint_quietly(
    client: ApiClient,
    item: checkpoints.Checkpoint,
    assignment: dict | None = None,
    *,
    reason: str,
    preserve_local: bool = False,
) -> bool:
    """Invalidate server state, optionally preserving its local evidence."""
    assignment_id = item.assignment_id
    if not assignment_id:
        return False
    checkpoint_id = (
        (assignment or {}).get("checkpoint_id") or item.checkpoint_id
        or f"invalid-{assignment_id[:16]}"
    )
    generation = int(
        (assignment or {}).get("resume_generation", item.resume_generation)
    )
    try:
        client.checkpoint_discard(
            assignment_id, checkpoint_id, generation, reason=reason,
        )
    except ApiError as exc:
        # A compatibility endpoint miss, a stale generation fence, a mismatched
        # checkpoint id, and an already-settled assignment can all surface as
        # 404/409/410 across server versions.  Treat the response as terminal
        # only after the authoritative active list proves the lease is gone;
        # otherwise keep the local item so the next startup retries cleanup.
        if exc.status_code in (404, 409, 410):
            try:
                if assignment_id in _active_by_id(client):
                    print(
                        "  server still reports the checkpoint lease active after "
                        "discard was rejected; kept locally for a safe retry"
                    )
                    return False
            except ApiError:
                return False
        else:
            print(f"  couldn't discard checkpoint {item.checkpoint_id or '?'}: {exc}; kept locally")
            return False
    if preserve_local:
        checkpoints.mark_kept(HOME, item)
    else:
        checkpoints.cleanup_assignment(HOME, assignment_id)
    return True


def _pause_checkpoint_quietly(
    client: ApiClient, assignment: dict,
) -> checkpoints.Checkpoint | _CheckpointPauseFailure | None:
    item = checkpoints.find_latest(HOME, assignment["assignment_id"])
    if item is None:
        return None
    if item.phase == "agent_completed":
        # Ctrl-C can arrive after Pier harvested a valid patch but before
        # _run_and_submit regains control. Preserve the authoritative copy as
        # part of pausing, so resume can rebuild a lost canonical artifact.
        try:
            artifact_staging.ensure_staged_patch(item.trial_dir)
        except artifact_staging.PatchStagingError as exc:
            print(
                f"  completed checkpoint artifact staging will retry on resume ({exc})"
            )
    if not item.valid or not item.checkpoint_id:
        family = "checkpoint_invalid"
        refill_plan.open_circuit(HOME, assignment, family)
        discarded = _discard_checkpoint_quietly(
            client, item, assignment, reason="invalid", preserve_local=True,
        )
        if discarded:
            checkpoints.mark_terminal(HOME, item)
        return _CheckpointPauseFailure(family, item, discarded)
    if item.phase == "incompatible":
        family = "checkpoint_incompatible"
        refill_plan.open_circuit(HOME, assignment, family)
        discarded = _discard_checkpoint_quietly(
            client, item, assignment, reason="incompatible", preserve_local=True,
        )
        if discarded:
            checkpoints.mark_terminal(HOME, item)
        return _CheckpointPauseFailure(family, item, discarded)
    try:
        client.checkpoint_pause(
            assignment["assignment_id"], item.checkpoint_id,
            item.resume_generation,
        )
    except ApiError as exc:
        # The local checkpoint is still the source of truth while the network
        # is down. A future `dradar resume` can renew it directly.
        print(f"  checkpoint saved locally; server pause will retry later ({exc})")
    checkpoints.prune_superseded(HOME, assignment["assignment_id"], item)
    return item


def _compensate_failed_checkpoint_resume(
    client: ApiClient,
    item: checkpoints.Checkpoint,
    assignment: dict,
    generation: int,
) -> None:
    """Return a server-granted recovery fence to a non-running state."""

    try:
        client.checkpoint_pause(
            assignment["assignment_id"], item.checkpoint_id, generation,
        )
        return
    except ApiError as exc:
        print(
            "  checkpoint resume compensation could not pause the new "
            f"generation ({exc}); attempting to release its fence"
        )
    fenced = dict(assignment)
    fenced["resume_generation"] = generation
    fenced["checkpoint_id"] = item.checkpoint_id
    discarded = _discard_checkpoint_quietly(
        client,
        item,
        fenced,
        # The public server deliberately accepts only its stable discard
        # reason vocabulary.  A locally unpersistable or identity-drifted
        # granted fence is invalid recovery state, so invalidate that fence
        # and reopen the cell while retaining the local evidence.
        reason="invalid",
        preserve_local=True,
    )
    if discarded:
        checkpoints.mark_terminal(HOME, item)


def _resume_checkpoint_with_ambiguous_replay(
    client: ApiClient,
    *,
    assignment_id: str,
    checkpoint_id: str,
    generation: int,
    session_id: str | None,
) -> dict:
    """Replay only an ambiguous resume POST under the server's idempotency key.

    The server commits a checkpoint fence before its HTTP response reaches the
    client.  If that response is lost, advancing from a freshly observed
    server generation would consume another fence.  A live runner session may
    instead replay the *same* checkpoint, old generation and session id; the
    server returns the already-issued generation without incrementing it.
    Explicit 4xx responses are never retried here; 5xx and unreadable 2xx
    responses remain ambiguous because a reverse proxy can fail after commit.
    A caller without a session id has no safe idempotency key, so it gets one
    attempt only.
    """

    attempts = _CHECKPOINT_RESUME_REPLAY_ATTEMPTS if session_id else 1
    def minimally_valid_response(data: object) -> bool:
        if not isinstance(data, dict):
            return False
        resumed = data.get("assignment")
        if not isinstance(resumed, dict):
            return False
        resumed_generation = resumed.get("resume_generation")
        return (
            resumed.get("assignment_id") == assignment_id
            and resumed.get("checkpoint_id") == checkpoint_id
            and isinstance(resumed_generation, int)
            and not isinstance(resumed_generation, bool)
            and resumed_generation == generation + 1
        )

    for attempt in range(attempts):
        try:
            data = client.checkpoint_resume(
                assignment_id,
                checkpoint_id,
                generation,
                session_id=session_id,
            )
        except ApiError as exc:
            ambiguous_http = (
                exc.status_code is None or exc.status_code >= 500
            )
            if not ambiguous_http or attempt + 1 >= attempts:
                raise
        except json.JSONDecodeError as exc:
            # A 2xx response can be truncated after the server committed.
            # Normalize it only inside this idempotent checkpoint endpoint;
            # generic API calls keep their existing JSON error behaviour.
            if attempt + 1 >= attempts:
                raise ApiError(
                    "checkpoint resume returned an unreadable success response",
                ) from exc
        else:
            if minimally_valid_response(data) or attempt + 1 >= attempts:
                return data
            print(
                f"  {assignment_id}: checkpoint resume success response was "
                "incomplete; replaying the same fenced request"
            )
            time.sleep(_CHECKPOINT_RESUME_REPLAY_DELAY_SECONDS * (2 ** attempt))
            continue
        print(
            f"  {assignment_id}: checkpoint resume response was ambiguous; "
            "replaying the same fenced request"
        )
        time.sleep(_CHECKPOINT_RESUME_REPLAY_DELAY_SECONDS * (2 ** attempt))
    raise AssertionError("unreachable")


def _run_and_submit(client: ApiClient, assignment: dict, tasks_root: Path,
                    args, local_commit: str | None,
                    telemetry: RunnerTelemetry | None = None,
                    resume_checkpoint: checkpoints.Checkpoint | None = None,
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
    # The assignment lock must cover the whole quota-consuming lifetime, not
    # just checkpoint recovery.  Otherwise a second `dradar resume` can see
    # the checkpoint written by a healthy first run, ask the server for a new
    # recovery generation, and start a duplicate Codex process before Pier's
    # own job/container checks get a chance to reject it.
    if not _assignment_lock_held:
        try:
            with checkpoints.assignment_lock(HOME, assignment["assignment_id"]):
                return _run_and_submit(
                    client, assignment, tasks_root, args, local_commit,
                    telemetry=telemetry, resume_checkpoint=resume_checkpoint,
                    _assignment_lock_held=True,
                )
        except checkpoints.CheckpointBusy:
            print(
                f"assignment {assignment['assignment_id']} is already running on this "
                "machine; refusing to start a duplicate model session"
            )
            return "busy"
    hash_match = check_task_content_hash(assignment, tasks_root)
    if hash_match is False and not getattr(args, "allow_task_drift", False):
        print(
            "refusing to start: the selected benchmark task differs from the "
            "server copy. Restore local task changes or refresh the task pack, "
            "then run `dradar resume`; no model quota was consumed. Use "
            "`--allow-task-drift` only for an intentional non-comparable run."
        )
        _mark_stopped_quietly(client, assignment)
        return "task-content-mismatch"
    work_dir = HOME / "work"
    print("running trial (this can take a while)...")
    for attempt in (1, 2):
        try:
            art = run_trial(
                assignment, tasks_root, work_dir, dev_agent=args.dev_agent,
                on_started=lambda: (
                    client.mark_started(
                        assignment["assignment_id"], session_id=telemetry.session_id)
                    if telemetry else client.mark_started(assignment["assignment_id"])
                ),
                resume_checkpoint=(
                    resume_checkpoint.checkpoint_dir if resume_checkpoint else None
                ))
            break
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
            paused = _pause_checkpoint_quietly(client, assignment)
            if isinstance(paused, _CheckpointPauseFailure):
                print(
                    f"checkpoint infrastructure failed ({paused.family}); "
                    "local evidence was kept and automatic refill was faulted"
                )
                return paused.family.replace("_", "-")
            if paused is None:
                _mark_stopped_quietly(
                    client, assignment, failure_kind="environment_build_failed",
                )
            return "environment-build-failed"
        except RunnerError as exc:
            failure_kind = classify_exception_message(str(exc))
            terminal_outcome = _terminal_failure_outcome(failure_kind)
            item = _pause_checkpoint_quietly(client, assignment)
            if isinstance(item, _CheckpointPauseFailure):
                print(
                    f"trial stopped by {item.family}; local evidence was kept "
                    "and automatic refill was faulted"
                )
                return item.family.replace("_", "-")
            if item is not None:
                print(f"trial interrupted: {exc}\n"
                      f"checkpoint {item.checkpoint_id} was kept; `dradar resume` "
                      "continues the same workspace/session")
                return terminal_outcome or "paused"
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
            # A user can interrupt before an agent has produced a resumable
            # checkpoint (notably DSH minimal mode). In that case there is no
            # local recovery path, so undo the checkout stamp before bubbling
            # the interrupt up. Otherwise the UI reports a resumable lease
            # while every later ``dradar resume`` sees it as already checked
            # out and has nothing it can start.
            paused = _pause_checkpoint_quietly(client, assignment)
            if paused is None:
                _mark_stopped_quietly(
                    client,
                    assignment,
                    defer_seconds=0,
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
    item = checkpoints.find_latest(HOME, assignment["assignment_id"])
    if interrupted and item is not None and item.phase != "agent_completed":
        saved = _pause_checkpoint_quietly(client, assignment)
        if isinstance(saved, _CheckpointPauseFailure):
            print(
                f"trial stopped by {saved.family}; local evidence was kept "
                "and automatic refill was faulted"
            )
            return saved.family.replace("_", "-")
        if saved is not None:
            print(f"trial interrupted; checkpoint {saved.checkpoint_id} was kept — "
                  "the next `dradar resume` continues instead of submitting a partial run")
            return terminal_outcome or "paused"
    outcome = "interrupted" if interrupted else "completed"
    if telemetry:
        telemetry.set_phase(
            "uploading", assignment["assignment_id"],
            assignment.get("resume_generation"),
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
        "zcode_cli_version": art.zcode_cli_version,
        "dsh_version": art.dsh_version,
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
    if assignment_codex_provider(assignment) == DEEPSEEK_PROVIDER:
        # Server-side audit/gating can distinguish corrected official-catalog
        # runs from the earlier fallback-metadata benchmark without deleting
        # either history.
        meta.update({
            "model_config_version": DEEPSEEK_RUN_CONFIG_VERSION,
            "model_catalog_sha256": DEEPSEEK_CATALOG_SHA256,
            "model_runtime_profile": DEEPSEEK_RUNTIME_PROFILE,
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
    elif bundled_completion is not None:
        meta.update({
            "bundled_completion_evidence": bundled_completion,
            "pier_postrun_warning": True,
            "pier_failure_phase": "post_agent",
        })

    if item is not None and item.job_dir == art.job_dir:
        checkpoints.prune_superseded(HOME, assignment["assignment_id"], item)

    upload_outcome = _upload_trial(client, {
        "assignment_id": assignment["assignment_id"], "nonce": assignment["nonce"],
        "task_id": assignment["task_id"], "trial_dir": str(art.trial_dir),
        "meta": meta, "outcome": outcome,
        "job_dir": str(art.job_dir) if art.job_dir else None, "keep": args.keep,
        "archive_session": getattr(args, "archive_session", False),
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


def _retry_pending_uploads(client: ApiClient) -> None:
    """Auto-heal at the top of every `dradar go`/`resume`: flush anything a
    previous run couldn't upload before doing anything else. Silent no-op
    when the ledger is empty — this must never surprise a volunteer who has
    nothing pending."""
    entries = pending.load(HOME)
    if not entries:
        return
    print(f"retrying {len(entries)} upload(s) left over from a previous run...")
    for e in entries:
        # _upload_trial handles the gone-artifacts case (drops the entry).
        _upload_trial(client, e)
    print()


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
    _retry_pending_uploads(client)
    remaining = pending.load(HOME)
    if remaining:
        print(f"{len(remaining)} still pending (will retry again on the next "
              "`dradar go`/`retry-upload`)")
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


def _checkpoint_upload_entry(
    item: checkpoints.Checkpoint, assignment: dict, args, local_commit: str | None,
) -> dict:
    recovered, checkpoint_error = _recover_completed_checkpoint_patch(
        item.trial_dir, assignment,
    )
    if recovered:
        print(
            "  recovered model.patch from the completed, identity-matched "
            "checkpoint; uploading without rerunning"
        )
    elif checkpoint_error is not None:
        print(f"  completed checkpoint patch recovery blocked: {checkpoint_error}")
    _patch, _trajectory, result = trial_artifact_paths(item.trial_dir)
    stats = summarize_result(result)
    return {
        "assignment_id": assignment["assignment_id"],
        "nonce": assignment["nonce"],
        "task_id": assignment["task_id"],
        "trial_dir": str(item.trial_dir),
        "meta": {
            "dradar_version": __version__,
            "duration_sec": None,
            "pier_returncode": 0,
            "dev_agent": args.dev_agent,
            "task_content_hash_match": None,
            "deep_swe_commit": local_commit,
            "recovered_completed_checkpoint": True,
            **stats,
        },
        "outcome": "completed",
        "job_dir": str(item.job_dir),
        "keep": args.keep,
        "archive_session": getattr(args, "archive_session", False),
        "resume_generation": assignment.get(
            "resume_generation", item.resume_generation),
    }


def _resume_one_checkpoint(
    client: ApiClient,
    item: checkpoints.Checkpoint,
    assignment: dict | None,
    args,
    tasks_root: Path,
    telemetry: RunnerTelemetry | None,
) -> str:
    assignment_id = item.assignment_id
    if assignment_id is None:
        checkpoints.remove(HOME, item)
        return "discarded"
    try:
        with checkpoints.assignment_lock(HOME, assignment_id):
            if assignment is None:
                # Pending uploads were flushed before discovery. No active
                # lease now therefore means submitted/expired/released.
                checkpoints.cleanup_assignment(HOME, assignment_id)
                print(f"  {assignment_id}: no active server lease; removed stale local checkpoint")
                return "discarded"
            if not item.valid or checkpoints.is_expired(item):
                # Invalidity is an infrastructure fault even when the stale
                # timestamp also crosses the ordinary TTL.  Do not let age
                # downgrade it to routine expiry and bypass the circuit.
                reason = "invalid" if not item.valid else "expired"
                print(f"  {assignment_id}: checkpoint is {reason}; reopening the cell")
                if reason == "invalid":
                    refill_plan.open_circuit(
                        HOME, assignment, "checkpoint_invalid",
                    )
                    discarded = _discard_checkpoint_quietly(
                        client, item, assignment, reason=reason,
                        preserve_local=True,
                    )
                    if discarded:
                        checkpoints.mark_terminal(HOME, item)
                    return (
                        "checkpoint-invalid" if discarded else "paused"
                    )
                if _discard_checkpoint_quietly(
                    client, item, assignment, reason=reason,
                ):
                    return "discarded"
                return "paused"
            if item.phase == "incompatible":
                print(f"  {assignment_id}: checkpoint is incompatible; reopening the cell")
                refill_plan.open_circuit(
                    HOME, assignment, "checkpoint_incompatible",
                )
                discarded = _discard_checkpoint_quietly(
                    client, item, assignment, reason="incompatible",
                    preserve_local=True,
                )
                if discarded:
                    checkpoints.mark_terminal(HOME, item)
                return (
                    "checkpoint-incompatible" if discarded else "paused"
                )
            if item.checkpoint_dir != item.trial_dir / "checkpoint":
                print(
                    f"  {assignment_id}: legacy agent-mounted checkpoint is "
                    "untrusted; preserving its diagnostics without resuming paid work"
                )
                discarded = _discard_checkpoint_quietly(
                    client,
                    item,
                    assignment,
                    # Legacy agent-mounted state cannot satisfy the new
                    # host-private publication contract.  The public server's
                    # stable vocabulary calls that state incompatible.
                    reason="incompatible",
                    preserve_local=True,
                )
                if discarded:
                    checkpoints.mark_terminal(HOME, item)
                    return "legacy-checkpoint-unsupported"
                return "paused"
            if assignment.get("agent") == GROK_AGENT:
                print(
                    f"  {assignment_id}: Grok Build checkpoints are not "
                    "supported; discarding the stale local checkpoint"
                )
                if _discard_checkpoint_quietly(
                    client, item, assignment, reason="incompatible",
                ):
                    return "discarded"
                return "paused"
            identity_mismatches = _checkpoint_identity_mismatches(item, assignment)
            if identity_mismatches:
                print(
                    f"  {assignment_id}: checkpoint runtime identity does not "
                    "match the lease ("
                    + ", ".join(identity_mismatches)
                    + "); discarding it"
                )
                if _discard_checkpoint_quietly(
                    client, item, assignment, reason="incompatible",
                ):
                    return "discarded"
                return "paused"

            local_commit = _check_version_pin(
                assignment.get("deep_swe_commit"), tasks_root,
                args.allow_task_drift,
            )
            if item.phase == "agent_completed":
                try:
                    item = checkpoints.revalidate_host_checkpoint(HOME, item)
                except (OSError, RuntimeError, ValueError) as exc:
                    print(
                        f"  {assignment_id}: completed checkpoint failed its "
                        f"host-private revalidation ({exc}); kept locally"
                    )
                    return "paused"
                print(f"found completed checkpoint {item.checkpoint_id}; uploading without rerunning")
                checkpoints.prune_superseded(HOME, assignment_id, item)
                return _upload_trial(
                    client,
                    _checkpoint_upload_entry(
                        item, assignment, args, local_commit,
                    ),
                    ask_cleanup=(
                        not args.keep
                        and not getattr(args, "yes", False)
                        and not getattr(args, "parallel", False)
                    ),
                )

            if not durable_checkpoint_rollout_enabled():
                paused = _pause_checkpoint_quietly(client, assignment)
                if isinstance(paused, _CheckpointPauseFailure):
                    print(
                        f"  {assignment_id}: saved checkpoint is "
                        f"{paused.family}; automatic refill remains faulted"
                    )
                    return paused.family.replace("_", "-")
                print(
                    f"  {assignment_id}: durable checkpoint resume is "
                    "temporarily disabled in this release; the checkpoint "
                    "was kept and no model session was started"
                )
                return "paused"

            generation = max(
                item.resume_generation,
                int(assignment.get("resume_generation") or 0),
            )
            if generation >= MAX_CHECKPOINT_RESUMES:
                checkpoints.mark_terminal(HOME, item)
                _signal_pool_abort(
                    "checkpoint recovery safety limit reached",
                    interrupt_siblings=False,
                )
                print(
                    f"  {assignment_id}: checkpoint reached the "
                    f"{MAX_CHECKPOINT_RESUMES}-resume safety limit; automatic "
                    "recovery is now disabled and the diagnostic workspace was kept"
                )
                return "recovery-exhausted"

            wait_seconds = _checkpoint_backoff_seconds(
                item, generation=generation,
            )
            if wait_seconds > 0:
                print(
                    f"  {assignment_id}: checkpoint recovery backoff "
                    f"{wait_seconds:.0f}s (attempt {generation + 1}/"
                    f"{MAX_CHECKPOINT_RESUMES})"
                )
                time.sleep(wait_seconds)

            if telemetry:
                telemetry.bind_batch(assignment.get("batch_id"))
                # Register the recovery process without claiming ownership.
                # checkpoint/resume is the atomic bind + fencing operation.
                telemetry.set_phase("queued")
                telemetry.flush()
            try:
                data = _resume_checkpoint_with_ambiguous_replay(
                    client,
                    assignment_id=assignment_id,
                    checkpoint_id=item.checkpoint_id,
                    generation=generation,
                    session_id=telemetry.session_id if telemetry else None,
                )
            except ApiError as exc:
                if (
                    exc.status_code == 409
                    and getattr(args, "worker_child", False)
                    and (
                        exc.code == "checkpoint_runner_healthy"
                        or "assignment is still running with a healthy runner"
                        in str(exc)
                    )
                ):
                    print(
                        f"  {assignment_id}: checkpoint is already owned by a "
                        "healthy runner; checking for a different waiting task"
                    )
                    return "busy"
                if exc.status_code == 404:
                    try:
                        still_active = assignment_id in _active_by_id(client)
                    except ApiError:
                        still_active = True
                    if still_active:
                        print("  server does not support checkpoint resume yet; kept locally")
                        return "paused"
                if exc.status_code in (404, 410):
                    checkpoints.cleanup_assignment(HOME, assignment_id)
                    print(f"  {assignment_id}: checkpoint lease is gone ({exc}); removed locally")
                    return "discarded"
                print(f"  {assignment_id}: couldn't resume checkpoint ({exc}); kept locally")
                return "paused"
            resumed = data.get("assignment") if isinstance(data, dict) else None
            resumed_generation = (
                resumed.get("resume_generation")
                if isinstance(resumed, dict) else None
            )
            expected_generation = generation + 1
            response_identity_mismatches = (
                _checkpoint_resume_response_mismatches(
                    item, assignment, resumed,
                )
                if isinstance(resumed, dict)
                else ["assignment"]
            )
            if (
                not isinstance(resumed, dict)
                or not isinstance(resumed_generation, int)
                or isinstance(resumed_generation, bool)
                or resumed_generation != expected_generation
                or resumed.get("assignment_id") != assignment_id
                or resumed.get("task_id") != assignment.get("task_id")
                or response_identity_mismatches
            ):
                print(
                    f"  {assignment_id}: server returned an invalid checkpoint "
                    "resume response; pausing the granted fence and keeping it locally"
                )
                _compensate_failed_checkpoint_resume(
                    client, item, assignment, expected_generation,
                )
                return "paused"
            try:
                # Publish a legacy agent-mounted snapshot only after the
                # server has atomically granted this runner ownership. A peer
                # can otherwise receive checkpoint_runner_healthy while its
                # stale sibling copy hides the still-live legacy checkpoint.
                item = checkpoints.materialize_host_checkpoint(HOME, item)
                item = checkpoints.persist_resume_generation(
                    HOME, item, resumed_generation,
                )
            except (OSError, ValueError) as exc:
                print(
                    f"  {assignment_id}: couldn't materialize and persist the "
                    f"fenced checkpoint generation ({exc}); kept locally"
                )
                _compensate_failed_checkpoint_resume(
                    client, item, assignment, resumed_generation,
                )
                return "paused"
            if telemetry:
                telemetry.set_phase(
                    "running", assignment_id,
                    resumed.get("resume_generation"),
                )
                telemetry.flush()
            print(f"resuming checkpoint {item.checkpoint_id} for {resumed['task_id']} "
                  f"(generation {resumed.get('resume_generation', '?')})")
            outcome = _run_and_submit(
                client, resumed, tasks_root, args, local_commit,
                telemetry=telemetry, resume_checkpoint=item,
                _assignment_lock_held=True,
            )
            if telemetry:
                telemetry.set_phase("queued")
            return outcome
    except checkpoints.CheckpointBusy:
        return "busy"


def _resume_local_checkpoints(
    client: ApiClient,
    args,
    tasks_root: Path,
    telemetry: RunnerTelemetry | None,
) -> tuple[list[str], bool]:
    """Recover local work before the server is allowed to dispense new work."""
    target = getattr(args, "assignment", None)
    candidates = list(checkpoints.latest_by_assignment(HOME).values())
    if target:
        candidates = [c for c in candidates if c.assignment_id == target]
    if not candidates:
        if target:
            print(f"no local checkpoint for assignment {target}")
        return [], False

    try:
        active = _active_by_id(client)
    except ApiError as exc:
        _exit_for(exc)
    if getattr(client, "benchmark_id", None):
        # A checkpoint from another benchmark may still have a perfectly
        # valid lease, but this channel's filtered assignment view cannot see
        # it. Preserve it for the matching channel instead of misclassifying
        # it as stale and deleting its only recovery state.
        candidates = [c for c in candidates if c.assignment_id in active]
        if not candidates:
            return [], False
    print(f"found {len(candidates)} unfinished checkpoint(s); recovering before new work...")
    results = []
    for item in candidates:
        outcome = _resume_one_checkpoint(
            client, item, active.get(item.assignment_id), args, tasks_root, telemetry,
        )
        if outcome == "busy":
            continue
        results.append(outcome)
        if outcome == "submitted" and getattr(args, "refill", False):
            refill_plan.mark_submitted(HOME, item.assignment_id)
        if outcome in _ACCOUNT_TERMINAL_OUTCOMES:
            if getattr(args, "refill", False):
                refill_plan.stop(HOME, f"account stop: {outcome}")
            _announce_account_stop(outcome)
            break
        # Super-account batch workers use --parallel. Each process owns one
        # checkpoint for its whole lifetime, so one corrupt worker cannot
        # serialize or block the other 23.
        if getattr(args, "parallel", False):
            break
    return results, True


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def cmd_checkpoints(args) -> int:
    items = checkpoints.scan(HOME)
    if not items:
        print("no local checkpoints")
        return 0
    total = 0
    for item in items:
        size = item.size_bytes
        total += size
        if checkpoints.is_terminal(HOME, item):
            state = "terminal evidence (not resumable)"
        else:
            state = item.phase if item.valid else f"invalid ({item.invalid_reason})"
        print(f"{item.checkpoint_id or '?'}  assignment={item.assignment_id or '?'}  "
              f"task={item.task_id or '?'}  state={state}  size={_format_size(size)}  "
              f"updated={item.updated_at.isoformat()}")
    print(f"total: {len(items)} checkpoint(s), {_format_size(total)}")
    return 0


def cmd_checkpoint_discard(args) -> int:
    items = checkpoints.scan(HOME)
    matches = [item for item in items if (
        item.checkpoint_id == args.checkpoint_id
        or item.assignment_id == args.checkpoint_id
    )]
    if not matches:
        print(f"checkpoint not found: {args.checkpoint_id}")
        return 1
    terminal = [item for item in matches if checkpoints.is_terminal(HOME, item)]
    resumable = [item for item in matches if not checkpoints.is_terminal(HOME, item)]
    for item in terminal:
        checkpoints.remove(HOME, item)
    matches = resumable
    if not matches:
        print("terminal local evidence removed; server lease left unchanged")
        return 0
    cfg = _load_config()
    client = _client(cfg)
    try:
        active = _active_by_id(client)
    except ApiError as exc:
        _exit_for(exc)
    ok = True
    seen = set()
    for item in matches:
        if item.assignment_id in seen:
            continue
        seen.add(item.assignment_id)
        assignment = active.get(item.assignment_id)
        if assignment is None:
            if item.assignment_id:
                checkpoints.cleanup_assignment(HOME, item.assignment_id)
            else:
                checkpoints.remove(HOME, item)
            continue
        ok &= _discard_checkpoint_quietly(
            client, item, assignment, reason="user_discard",
        )
    print("checkpoint discarded; its cell is open again" if ok else "checkpoint kept")
    return 0 if ok else 1


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
    lease list, an ``agent_completed`` checkpoint may be a finished trial that
    crashed immediately before its upload ledger was recorded.
    """
    docker_requested = bool(
        getattr(args, "docker", False) or getattr(args, "all_task_images", False)
    )
    if getattr(args, "all_task_images", False) and not getattr(args, "docker", False):
        print("--all-task-images requires --docker")
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
    candidates: list[checkpoints.Checkpoint] = []
    protected_active = protected_pending = protected_kept = 0
    seen_jobs: set[Path] = set()
    for item in checkpoints.scan(HOME):
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
        if checkpoints.is_kept(HOME, item) and not args.include_kept:
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
            kept = " [kept]" if checkpoints.is_kept(HOME, item) else ""
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
                  "container, active/pending task, checkpoint, or kept job")

    has_images = bool(
        image_plan and image_plan.docker_available and image_plan.candidates
    )
    if (not candidates and not has_images) or args.dry_run:
        return 1 if docker_requested and image_plan and not image_plan.docker_available else 0
    if not args.yes:
        subject = "settled local files and Docker task images" if has_images else "settled local task files"
        answer = input(f"remove these {subject}? [Y/n] ").strip().lower()
        if answer not in ("", "y", "yes"):
            print("nothing was deleted")
            return 0
    for item in candidates:
        checkpoints.remove(HOME, item)
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
    return 1 if image_failed else 0


def _maintain_image_cache(client: ApiClient, cfg: dict, *, phase: str) -> bool:
    """Run bounded, ledger-only GC with fail-closed ownership checks.

    A server read failure makes cleanup a no-op: without the active lease set
    we cannot prove an image is disposable.  The return value controls only
    NEW claims; existing leases/checkpoints are still allowed to run. During
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
    try:
        return shutil.disk_usage(HOME).free >= policy.min_free_bytes
    except OSError:
        return True


def cmd_go(args) -> int:
    if getattr(args, "pick", None) and getattr(args, "auto", None):
        sys.exit("--auto and --pick are two different ways to choose cells; pass only one")
    if getattr(args, "auto", None) is not None and args.auto < 1:
        sys.exit("--auto N requires N >= 1")
    workers = getattr(args, "workers", 1)
    auto_workers = workers == "auto"
    if not auto_workers and (workers < 1 or workers > 40):
        sys.exit("--workers N requires 1 <= N <= 40")
    if getattr(args, "worker_child", False) and (
        workers != 1
        or not getattr(args, "parallel", False)
        or not getattr(args, "resume", False)
    ):
        sys.exit("invalid internal worker invocation")
    if (auto_workers or workers > 1) and getattr(args, "parallel", False):
        sys.exit("--workers already manages parallel sessions; do not combine it with --parallel")
    if (auto_workers or workers > 1) and getattr(args, "assignment", None):
        sys.exit("--assignment targets one checkpoint and requires --workers 1")
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
    )
    if any(value is not None for value in refill_options) and not getattr(args, "refill", False):
        sys.exit("refill limits and scope filters require --refill")
    if getattr(args, "refill", False):
        if getattr(args, "assignment", None):
            sys.exit("continuous refill cannot be combined with --assignment")
        if (getattr(args, "refill_harness", None) is None
                and (getattr(args, "refill_model", None) is not None
                     or getattr(args, "refill_effort", None) is not None)):
            sys.exit("--refill-model/--refill-effort require --refill-harness")
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
        return _run_worker_pool(args)
    cfg = _load_config()
    cfg["benchmark"] = (
        getattr(args, "benchmark", None)
        or cfg.get("benchmark")
        or DEFAULT_BENCHMARK
    )
    client = _client(cfg, auto_register=True)
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
    telemetry.start()
    close_reason = "error"

    try:
        # One runner per machine by default, THEN sweep containers stranded by
        # dead runs — the lock is what makes "a pier-shaped compose project
        # exists right now" mean "nobody alive owns it" (see machine.py).
        if getattr(args, "parallel", False):
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
        try:
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
            sys.exit(str(exc))

        telemetry.set_phase("queued")
        # Self-heal before anything else: a trial from a previous run that ran
        # but failed to upload must not just sit on disk forever. The worker
        # pool parent already drains this shared ledger before spawning its
        # children; letting every child replay the same entries creates a
        # duplicate-upload herd precisely when the server asks us to slow down.
        if not getattr(args, "worker_child", False):
            _retry_pending_uploads(client)

        recovered, found_checkpoints = _resume_local_checkpoints(
            client, args, tasks_root, telemetry,
        )
        recovery_ok = all(
            outcome in ("submitted", "interrupted", "discarded", "expired")
            for outcome in recovered
        )
        if getattr(args, "assignment", None):
            close_reason = "completed" if recovery_ok and recovered else "paused"
            return 0 if recovery_ok and recovered else 1
        if recovered and not recovery_ok:
            close_reason = "paused"
            return 1
        if found_checkpoints and getattr(args, "resume", False) and not recovered:
            # Every matching checkpoint is already owned by another local
            # worker. A supervised worker child may safely continue to the
            # server's atomic checkout dispenser: paused/running checkpoint
            # assignments already have started_at and cannot be dispensed,
            # while a different waiting assignment can fill this worker slot.
            # Keep standalone/manual --parallel conservative because it was
            # not launched as part of one confirmed worker pool.
            if getattr(args, "worker_child", False):
                print("checkpoint is already owned by another local worker; "
                      "checking for a different waiting task")
            else:
                close_reason = "paused"
                return 1

        rc = _go_menu(args, cfg, client, tasks_root, telemetry=telemetry)
        if not getattr(args, "parallel", False):
            _maintain_image_cache(client, cfg, phase="after run")
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
) -> bool:
    """Whether a held cell is genuinely waiting for a worker right now.

    Paused/checkpointed work deliberately stays out of automatic pool
    backfill: repeatedly reviving a broken checkpoint can burn quota and hide
    an incident. Fresh controller claims have no ``started_at`` value and are
    safe for the server's atomic checkout endpoint to assign.
    """
    if (assignment.get("started_at")
            or assignment.get("execution_state") == "paused"
            or assignment.get("checkpoint_id")):
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


def _assignment_is_recoverable_checkpoint(
    assignment: dict, local: dict[str, checkpoints.Checkpoint],
) -> bool:
    """Whether a paused server lease has matching, safe local recovery state.

    The server may be one generation ahead after it grants a resume fence and
    the runner fails to persist that generation locally.  The compensating
    pause deliberately keeps the original local snapshot intact, so a later
    worker can resume from the authoritative server generation.  Reject only
    local state *ahead* of the server; that direction cannot be explained by
    the fail-closed compensation path.
    """
    if not durable_checkpoint_rollout_enabled():
        return False
    assignment_id = assignment.get("assignment_id")
    item = local.get(assignment_id) if assignment_id else None
    server_generation = assignment.get("resume_generation", 0)
    if (
        item is None or not item.valid or checkpoints.is_expired(item)
        or checkpoints.is_terminal(HOME, item)
        or assignment.get("execution_state") != "paused"
        or assignment.get("runner_state") not in {None, "paused", "resumable"}
        or not assignment.get("checkpoint_id")
        or not isinstance(server_generation, int)
        or isinstance(server_generation, bool)
        or server_generation < 0
        or server_generation >= MAX_CHECKPOINT_RESUMES
    ):
        return False
    return (
        item.checkpoint_id == assignment.get("checkpoint_id")
        and item.resume_generation <= server_generation
        and _checkpoint_backoff_seconds(
            item, generation=server_generation,
        ) <= 0
    )


def _pool_ready_work_count(client: ApiClient) -> int | None:
    """Read fresh checkout work plus safely recoverable checkpoints.

    ``None`` means the safety check itself failed. The supervisor then keeps
    current workers but fails closed instead of guessing and overspawning.
    """
    try:
        data = client.get_assignment()
    except ApiError as exc:
        print(f"worker backfill check failed ({exc}); keeping current workers only")
        return None
    active = data.get("active")
    if active is None:
        one = data.get("assignment")
        active = [one] if one else []
    waiting = sum(
        _assignment_is_ready_for_checkout(assignment)
        for assignment in active
        if assignment
    )
    local = checkpoints.latest_by_assignment(HOME)
    recoverable = sum(
        _assignment_is_recoverable_checkpoint(assignment, local)
        for assignment in active
        if assignment
    )
    return waiting + recoverable


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
    if args.workers == "auto":
        from .capacity import AUTO_WORKER_CAP, inspect_capacity, print_report

        cfg = _load_config()
        cfg["benchmark"] = (
            getattr(args, "benchmark", None)
            or cfg.get("benchmark")
            or DEFAULT_BENCHMARK
        )
        client = _client(cfg, auto_register=True)
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
    else:
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
        cfg = _load_config()
        cfg["benchmark"] = (
            getattr(args, "benchmark", None)
            or cfg.get("benchmark")
            or DEFAULT_BENCHMARK
        )
        client = _client(cfg, auto_register=True)
    tasks_root = _selected_tasks_root(cfg)
    acquire_run_lock(HOME)
    sweep_orphan_compose(HOME, True)
    args.allow_new_claims = _maintain_image_cache(
        client, cfg, phase="before worker pool",
    )
    try:
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
    _retry_pending_uploads(client)

    active, _free_pick = _prepare_batch(args, client)
    if not active:
        return 0
    maximum = args.workers
    target_file = _pool_target_file(args)
    target = _read_pool_target(target_file, default=maximum, maximum=maximum)
    count = min(target, len(active))
    if count < target:
        print(f"only {len(active)} task(s) are currently held; starting {count} worker(s)")
    print(f"starting {count} worker(s); server-side checkout assigns each task exactly once")
    if target_file is not None:
        print(f"live worker target: {target_file} (range 0..{maximum})")
    command = _worker_command(args)
    pool_abort_file = configured_abort_file or (
        Path(tempfile.gettempdir())
        / f"dradar-pool-abort-{os.getpid()}-{time.time_ns()}"
    )
    repeat_failure_state_file = (
        Path(tempfile.gettempdir())
        / f"dradar-repeat-failure-{os.getpid()}-{time.time_ns()}.json"
    )
    owns_abort_file = configured_abort_file is None
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    processes: list[subprocess.Popen] = []
    active_processes: dict[int, subprocess.Popen] = {}
    returncodes: list[tuple[int, int]] = []
    backfill_error: str | None = None
    backfill_disabled = False
    abort_reason: str | None = None
    abort_interrupts_siblings = False

    def cleanup_abort_file() -> None:
        if owns_abort_file:
            pool_abort_file.unlink(missing_ok=True)
        repeat_failure_state_file.unlink(missing_ok=True)
        repeat_failure_state_file.with_name(
            f"{repeat_failure_state_file.name}.lock"
        ).unlink(missing_ok=True)

    def spawn_worker(slot: int) -> None:
        env = os.environ.copy()
        env["DRADAR_WORKER_INDEX"] = str(slot)
        env["DRADAR_POOL_SIZE"] = str(target)
        env["DRADAR_POOL_MAX_SIZE"] = str(maximum)
        if target_file is not None:
            env[_POOL_TARGET_FILE_ENV] = str(target_file)
        env[_POOL_ABORT_ENV] = str(pool_abort_file)
        env[_REPEAT_FAILURE_STATE_ENV] = str(repeat_failure_state_file)
        process = subprocess.Popen(command, env=env, **popen_kwargs)
        processes.append(process)
        active_processes[slot] = process
        print(f"  worker {slot}/{count}: pid {process.pid}")

    try:
        for index in range(1, count + 1):
            spawn_worker(index)

        next_backfill_check = 0.0
        while active_processes:
            for slot, process in list(active_processes.items()):
                returncode = process.poll()
                if returncode is None:
                    continue
                returncodes.append((slot, returncode))
                del active_processes[slot]
                if returncode != 0 and not backfill_disabled:
                    # A child already returned its failed assignment to a
                    # retryable state. Replacing that child automatically can
                    # pick the same cell again as soon as its cooldown expires
                    # (or rotate through other cells with the same local
                    # fault). Drain only: paid sibling runs stay untouched,
                    # while an explicit later `dradar resume` starts a fresh
                    # pool after the operator has inspected the failure.
                    backfill_disabled = True
                    print(
                        f"worker {slot} exited {returncode}; disabling automatic "
                        "backfill and draining workers already in flight"
                    )

            if not active_processes:
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
                    time.sleep(_POOL_SUPERVISOR_POLL_SECONDS)
                    continue

            current_time = time.monotonic()
            if (not backfill_disabled
                    and len(active_processes) < target
                    and current_time >= next_backfill_check):
                ready = _pool_ready_work_count(client)
                next_backfill_check = (
                    current_time + (
                        _POOL_BACKFILL_ERROR_RETRY_SECONDS
                        if ready is None else _POOL_BACKFILL_REFRESH_SECONDS
                    )
                )
                if ready:
                    vacant_slots = sorted(
                        set(range(1, target + 1)) - set(active_processes)
                    )
                    for slot in vacant_slots[:ready]:
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
            time.sleep(_POOL_SUPERVISOR_POLL_SECONDS)
    except (KeyboardInterrupt, EOFError):
        print("\nstopping workers safely; each active task is recoverable only after "
              "a checkpoint is saved or the server confirms checkout cleanup...")
        _signal_workers(processes)
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
        _maintain_image_cache(client, cfg, phase="after failed worker pool")
        cleanup_abort_file()
        return 1
    cleanup_abort_file()
    failed = [(slot, rc) for slot, rc in returncodes if rc != 0]
    _maintain_image_cache(client, cfg, phase="after worker pool")
    if abort_reason is not None:
        if abort_interrupts_siblings:
            print(f"worker pool stopped cleanly by circuit breaker: {abort_reason}")
        else:
            print(f"worker pool drained cleanly after account stop: {abort_reason}")
        return 0
    if backfill_error:
        print("worker pool finished after a backfill spawn error; completed uploads "
              "are preserved and the next resume can restore the full pool")
        return 1
    if failed:
        detail = ", ".join(f"worker {i}=exit {rc}" for i, rc in failed)
        print(f"worker pool finished with errors: {detail}")
        print("completed uploads are preserved; use `dradar leases`, `dradar checkpoints`, "
              "and `dradar resume` for remaining work")
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
    client: ApiClient, yes: bool, *, allow_new_claims: bool = True,
) -> tuple[list[dict], bool]:
    """The volunteer's held batch, plus whether this is a free-pick instance.
    Free-pick: the batch is whatever they claimed on the web. Menu mode
    (non-free-pick, e.g. claude) with nothing held: claim one from the menu
    right here. Normalizes the older single-`assignment` payload shape so an
    older server still works."""
    try:
        data = client.get_assignment()
    except ApiError as exc:
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
    return active, free_pick


def _run_batch(args, client: ApiClient, tasks_root: Path, active: list[dict],
               telemetry: RunnerTelemetry | None = None) -> int:
    """Run a non-empty held batch serially: one version-pin check covers the
    whole batch (a single local checkout serves every cell; it sys.exit's on
    a mismatch unless --allow-task-drift), then per-cell confirm/skip/run."""
    local_commit = _check_version_pin(active[0].get("deep_swe_commit"), tasks_root,
                                      args.allow_task_drift)

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
                "running", assignment["assignment_id"],
                assignment.get("resume_generation"),
            )
            # Make the session/assignment relationship visible before the
            # subprocess can start or fail. assignment/started then stamps
            # started_at + this same session id in one server transaction.
            telemetry.flush()
        outcome = _run_and_submit(
            client, assignment, tasks_root, args, local_commit, telemetry=telemetry)
        results.append(outcome)
        if telemetry:
            telemetry.set_phase("queued")
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
        if outcome in {"checkpoint-invalid", "checkpoint-incompatible"}:
            print(
                "stopping this batch after a checkpoint infrastructure fault; "
                "later cells were left untouched and the local evidence was kept"
            )
            break
    ok = all(o in ("submitted", "interrupted") for o in results)
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
    local_commit = _check_version_pin(active[0].get("deep_swe_commit"), tasks_root,
                                      args.allow_task_drift)
    results, failed_ids = [], set()
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
        try:
            # A failed local cell is marked stopped so it is retryable later,
            # but this session must not immediately take the same cell again.
            # The server applies this exclusion before stamping started_at,
            # allowing the loop to keep draining other waiting cells.
            if telemetry:
                telemetry.flush()  # register queued state before atomic checkout
                data = client.checkout(
                    exclude_assignment_ids=failed_ids,
                    session_id=telemetry.session_id,
                )
            else:
                data = client.checkout(exclude_assignment_ids=failed_ids)
        except ApiError as exc:
            if (telemetry and exc.status_code == 409
                    and "runner session" in str(exc)):
                # A first heartbeat and checkout can cross on a very fast
                # machine. Serialize one fresh heartbeat and retry checkout
                # exactly once; no assignment was stamped by the rejected
                # transaction, so this retry cannot duplicate work.
                telemetry.flush()
                try:
                    data = client.checkout(
                        exclude_assignment_ids=failed_ids,
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
        if assignment["assignment_id"] in failed_ids:
            # Compatibility with an older server that ignores the exclusion
            # field: checkout just stamped this cell started again. Undo that
            # stamp before stopping, otherwise `resume` reports nothing to do
            # while the UI shows a permanently running cell (incident
            # 019f656c-cf16-70e2-ae4c-d1d51146acb2, 2026-07-15).
            _mark_stopped_quietly(client, assignment)
            print(f"stopping after {assignment['task_id']} re-entered checkout — "
                  "it already failed in this session. `dradar resume` retries it "
                  "later; `dradar release` gives it back.")
            break
        extra = data.get("unstarted")
        if telemetry:
            telemetry.bind_batch(assignment.get("batch_id"))
            telemetry.set_phase(
                "running", assignment["assignment_id"],
                assignment.get("resume_generation"),
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
        if telemetry:
            telemetry.set_phase("queued")
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
            if outcome not in ("submitted", "interrupted"):
                refill_plan.stop(HOME, f"task outcome={outcome}")
                print(f"continuous refill stopped after outcome={outcome}; no new tasks "
                      "will be claimed, and existing leases/checkpoints stay untouched")
                results.append(outcome)
                break
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
            runtime_cfg = _load_config()
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
                if claimed:
                    print(f"submitted 1 task; held {held}/{target}; auto-claimed {claimed}")
                elif replenished.get("seed_pending"):
                    print(f"submitted 1 selected task; waiting for "
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
            and outcome not in ("submitted", "interrupted")
        )
        continuous_claim_failure = (
            (getattr(args, "auto", None) is not None or len(active) > 1)
            and outcome not in ("submitted", "interrupted")
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
    ok = all(o in ("submitted", "interrupted") for o in results)
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
    refreshed, _ = _acquire_batch(client, True)
    return refreshed


def _prepare_batch(args, client: ApiClient) -> tuple[list[dict], bool]:
    """Claim/configure once, shared by the serial and supervised run paths."""
    allow_new_claims = getattr(args, "allow_new_claims", True)
    active, free_pick = _acquire_batch(
        client, args.yes, allow_new_claims=allow_new_claims,
    )
    wants_pick = getattr(args, "pick", None)
    auto_target = getattr(args, "auto", None)
    wants_refill = getattr(args, "refill", False)
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
            print(f"continuous refill not started: {exc}")
            args.refill = False
    if not active:
        saved_refill = refill_plan.load(HOME) if wants_refill else None
        if (saved_refill and saved_refill.get("status") == "active"
                and saved_refill.get("refill_harness")):
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
        print(f"\n{len(fresh)} more cell(s) were claimed while that batch ran — continuing:")
        rc = _run_batch(args, client, tasks_root, fresh, telemetry=telemetry)
    return rc


__all__ = ["cmd_go", "_go_menu",
           "_run_and_submit", "_check_version_pin", "_claim_from_menu",
           "_choose_menu_entry", "_print_menu", "_print_assignment",
           "cmd_retry_upload", "_retry_pending_uploads", "_upload_trial",
           "_artifacts_from_trial_dir"]
