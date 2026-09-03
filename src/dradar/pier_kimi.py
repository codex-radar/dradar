"""Credential-isolated Pier adapter for the official Kimi Code subscription CLI.

Only Kimi's managed OAuth service is supported.  The adapter installs the
exact public Kimi CLI release in the task image and injects one locked run-copy
of the credential; API-key providers and ambient Kimi settings are deliberately
excluded.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from pier.agents.installed.base import BaseInstalledAgent, with_prompt_template
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.install import AgentInstallSpec, InstallStep
from pier.models.agent.network import NetworkAllowlist
from pier.models.trajectories import Agent, FinalMetrics, Step, Trajectory
from pier.utils.trajectory_metrics import populate_context_from_final_metrics
try:
    from _dradar_worker_events import emit_worker_registered
except ModuleNotFoundError:
    from dradar.worker_events import emit_worker_registered

from _dradar_pier_runtime_safety import (
    AgentLogStore,
    RuntimeSafety,
    UnsafeAgentLog,
)
from _dradar_kimi_recovery import (
    KIMI_PROVIDER_CONNECTION_EXIT_CODE,
    kimi_provider_connection_stderr_is_retryable,
    pier_exit_code,
    run_with_kimi_resume,
    unique_session_probe_command,
    validated_session_id,
)


_MAX_USAGE_WIRE_BYTES = 16 * 1024 * 1024
_MAX_USAGE_WIRE_RECORDS = 50_000
_FINAL_SESSION_PROBE_TIMEOUT_SEC = 15.0

KIMI_CONFIG = """\
default_model = "kimi-code/k3"
default_permission_mode = "auto"
default_plan_mode = false
merge_all_available_skills = false
telemetry = false

[providers."managed:kimi-code"]
type = "kimi"
api_key = ""
base_url = "https://api.kimi.com/coding/v1"

[providers."managed:kimi-code".oauth]
storage = "file"
key = "oauth/kimi-code"

[models."kimi-code/k3"]
provider = "managed:kimi-code"
model = "k3"
max_context_size = 1048576
capabilities = ["thinking", "always_thinking", "tool_use"]
display_name = "K3"
support_efforts = ["low", "high", "max"]
default_effort = "high"

[thinking]
enabled = true

[background]
max_running_tasks = 1
keep_alive_on_exit = false
bash_auto_background_on_timeout = false

[tools]
disabled = ["WebSearch", "FetchURL"]

[[hooks]]
event = "PreToolUse"
matcher = "Read|ReadMediaFile|Glob|Grep|Write|Edit|Bash|Agent|AgentSwarm|Skill|AskUserQuestion|TodoList|TaskList|TaskOutput|TaskStop|WebSearch|FetchURL"
command = "/usr/bin/python3 /tmp/dradar-kimi-policy.py"
timeout = 5
"""

KIMI_POLICY = r'''#!/usr/bin/env python3
import json
import sys

DENIED_TOOLS = {"WebSearch", "FetchURL"}

PROTECTED = (
    "/tmp/dradar-kimi-home",
    "/logs/agent/kimi-code.stderr.log",
    "KIMI_CODE_HOME",
    "credentials/kimi-code.json",
    "oauth/kimi-code",
)

try:
    request = json.load(sys.stdin)
except Exception:
    print("Kimi policy could not validate the tool request", file=sys.stderr)
    raise SystemExit(2)

tool_name = request.get("tool_name")
if tool_name in DENIED_TOOLS:
    print("External web tools are disabled for benchmark runs", file=sys.stderr)
    raise SystemExit(2)

tool_input = json.dumps(request.get("tool_input", {}), ensure_ascii=False)
if any(marker in tool_input for marker in PROTECTED):
    print("Access to the isolated Kimi credential area is denied", file=sys.stderr)
    raise SystemExit(2)
'''

KIMI_CLI_VERSION = "0.39.1"
KIMI_BINARY_SHA256 = {
    "x86_64": "585547e082f2f3a32dd80825626a1c8dd4e82f55b4d6a8aa14e6397c00758eca",
    "aarch64": "f2e16073823cdeda207e3d228ef899cb9e43c8623ab21ddd3edd75702ae19ca3",
}


def _usage_instant(value: Any) -> str | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = float(value) / (1000 if value > 10_000_000_000 else 1)
            instant = datetime.fromtimestamp(seconds, timezone.utc)
        elif isinstance(value, str):
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                return None
            instant = instant.astimezone(timezone.utc)
        else:
            return None
    except (OverflowError, OSError, ValueError):
        return None
    return instant.isoformat().replace("+00:00", "Z")


def _kimi_usage_facts(
    records: list[dict], retry_records: list[dict] | None = None,
) -> dict:
    """Reconcile Kimi's durable per-request usage to completed turns.

    ``inputOther`` and ``inputCacheCreation`` are ordinary-priced prompt
    tokens. ``inputCacheRead`` is both part of total prompt tokens and the
    cached subset. Keeping that invariant matches Pier/DRadar's normalized
    contract: n_input_tokens already includes n_cache_tokens.

    Kimi 0.39.1 emits one ``usage.record`` for every successful provider
    response but advances ``turnStep`` for retryable failed attempts such as
    HTTP 429.  Those failures have no usage.  The stream-json output carries a
    separate, credential-free ``turn.step.retrying`` event for every failed
    attempt.  Reconciliation therefore requires all of the following:

    * strictly sequential durable-wire turn steps;
    * one later usage record for the final attempt of every logical request;
    * an exact ordered retry-event group for every preceding failed attempt;
    * a completed turn after all logical requests have settled.

    The durable wire order is authoritative for flat-priced K3 accounting.
    Wall-clock timestamps are retained when valid, but missing or skewed host
    clocks only make ``timed_usage_complete`` false; they do not erase an
    otherwise exact token ledger.  Managed OAuth catalog refreshes may also
    change the raw provider model id, so identity is pinned by ``modelAlias``.

    Retry counts alone are never sufficient. Unknown errors, malformed retry
    groups, missing or extra events, duplicate steps, and unfinished turns all
    fail closed.
    """

    totals = {name: 0 for name in (
        "inputOther", "inputCacheRead", "inputCacheCreation", "output",
    )}
    events: list[dict[str, Any]] = []
    usage_valid = True
    timed_usage_valid = True
    session_identity_valid = True
    request_ledger_valid = True
    turn_ledger_valid = True
    metadata_count = 0
    model_request_count = 0
    turn_prompt_count = 0
    ended_turns = 0
    duplicate_count = 0
    unexpected_usage = False
    turn_open = False
    pending_attempts: list[tuple[str, datetime | None]] = []
    pending_compaction_attempts: list[datetime | None] = []
    settled_request_steps: set[str] = set()
    request_attempt_count = 0
    request_retry_count = 0
    compaction_request_count = 0
    seen_turn_ids: set[int] = set()
    last_usage_instant: datetime | None = None
    last_request_instant: datetime | None = None
    current_turn_steps: list[tuple[int, int]] = []
    retry_group_index = 0

    retry_groups: list[list[dict[str, Any]]] = []
    retry_ledger_valid = True
    current_retry_group: list[dict[str, Any]] = []
    retry_event_required_keys = {
        "role", "type", "failed_attempt", "next_attempt", "max_attempts",
        "delay_ms", "error_name", "error_message",
    }
    retryable_errors = {
        ("APIProviderRateLimitError", 429),
        ("APIConnectionError", None),
        ("APITimeoutError", None),
    }
    for retry_record in retry_records or []:
        if (not isinstance(retry_record, dict)
                or frozenset(retry_record) not in {
                    frozenset(retry_event_required_keys),
                    frozenset({*retry_event_required_keys, "status_code"}),
                }
                or retry_record.get("role") != "meta"
                or retry_record.get("type") != "turn.step.retrying"):
            retry_ledger_valid = False
            continue
        failed_attempt = retry_record.get("failed_attempt")
        next_attempt = retry_record.get("next_attempt")
        max_attempts = retry_record.get("max_attempts")
        delay_ms = retry_record.get("delay_ms")
        error_message = retry_record.get("error_message")
        if ((retry_record.get("error_name"), retry_record.get("status_code"))
                not in retryable_errors
                or not isinstance(failed_attempt, int)
                or isinstance(failed_attempt, bool)
                or not isinstance(next_attempt, int)
                or isinstance(next_attempt, bool)
                or next_attempt != failed_attempt + 1
                or not isinstance(max_attempts, int)
                or isinstance(max_attempts, bool)
                or max_attempts != 10
                or not 1 <= failed_attempt < max_attempts
                or not isinstance(delay_ms, (int, float))
                or isinstance(delay_ms, bool)
                or delay_ms != delay_ms
                or not 0 <= delay_ms <= 3_600_000
                or not isinstance(error_message, str)
                or not 0 < len(error_message) <= 4_096):
            retry_ledger_valid = False
            continue
        if failed_attempt == 1:
            if current_retry_group:
                retry_groups.append(current_retry_group)
            current_retry_group = [retry_record]
        elif (not current_retry_group
              or current_retry_group[-1].get("next_attempt") != failed_attempt
              or current_retry_group[-1].get("max_attempts") != max_attempts):
            retry_ledger_valid = False
        else:
            current_retry_group.append(retry_record)
    if current_retry_group:
        retry_groups.append(current_retry_group)

    def instant(value: Any) -> tuple[str, datetime] | None:
        text = _usage_instant(value)
        if text is None:
            return None
        return text, datetime.fromisoformat(text.replace("Z", "+00:00"))

    for record in records:
        if not isinstance(record, dict):
            session_identity_valid = False
            continue
        record_type = record.get("type")
        if record_type == "metadata":
            metadata_count += 1
            continue
        if record_type == "turn.prompt":
            turn_prompt_count += 1
            if turn_open:
                turn_ledger_valid = False
            turn_open = True
            current_turn_steps = []
            last_request_instant = None
            continue
        if record_type == "llm.request":
            request_attempt_count += 1
            # Managed OAuth can resolve one stable configured alias to an
            # account-specific provider model id.  The alias is the durable
            # identity DRadar pinned in KIMI_CONFIG; do not reject otherwise
            # valid K3 usage merely because the catalog returned a different
            # raw provider id.  Older wire versions did not persist modelAlias,
            # so retain their exact ``model == k3`` contract.
            model_alias = record.get("modelAlias")
            provider_model = record.get("model")
            if model_alias is None:
                model_valid = provider_model == "k3"
            else:
                model_valid = (
                    model_alias == "kimi-code/k3"
                    and isinstance(provider_model, str)
                    and bool(provider_model.strip())
                )
            if not turn_open or not model_valid:
                session_identity_valid = False
            request_kind = record.get("kind", "loop")
            if request_kind not in {"loop", "compaction"}:
                request_ledger_valid = False
            request_step = record.get("turnStep")
            if request_kind == "compaction":
                parsed_step = None
            elif not isinstance(request_step, str) or not request_step:
                request_ledger_valid = False
                parsed_step = None
            else:
                parts = request_step.split(".")
                if (len(parts) != 2 or not all(part.isdigit() for part in parts)
                        or int(parts[1]) < 1):
                    request_ledger_valid = False
                    parsed_step = None
                else:
                    parsed_step = (int(parts[0]), int(parts[1]))
            request_instant = instant(record.get("time"))
            if request_instant is None:
                timed_usage_valid = False
                parsed_request_instant = None
            else:
                parsed_request_instant = request_instant[1]
                if ((last_request_instant is not None
                     and parsed_request_instant < last_request_instant)
                        or (last_usage_instant is not None
                            and parsed_request_instant < last_usage_instant)):
                    timed_usage_valid = False
                last_request_instant = parsed_request_instant
            if request_kind == "compaction":
                pending_compaction_attempts.append(parsed_request_instant)
                continue
            if isinstance(request_step, str) and request_step:
                if (request_step in settled_request_steps
                        or any(step == request_step for step, _ in pending_attempts)):
                    duplicate_count += 1
                    request_ledger_valid = False
                pending_attempts.append((request_step, parsed_request_instant))
                if parsed_step is not None:
                    current_turn_steps.append(parsed_step)
            continue
        if record_type == "turn.ended":
            ended_turns += 1
            if (not turn_open or pending_attempts
                    or pending_compaction_attempts):
                turn_ledger_valid = False
            turn_id = record.get("turnId")
            if (not isinstance(turn_id, int) or isinstance(turn_id, bool)
                    or turn_id < 0 or turn_id in seen_turn_ids):
                if isinstance(turn_id, int) and turn_id in seen_turn_ids:
                    duplicate_count += 1
                turn_ledger_valid = False
            else:
                seen_turn_ids.add(turn_id)
                if (not current_turn_steps
                        or any(prefix != turn_id
                               for prefix, _ in current_turn_steps)
                        or [step for _, step in current_turn_steps]
                           != list(range(1, len(current_turn_steps) + 1))):
                    turn_ledger_valid = False
            terminal_instant = instant(record.get("time"))
            if (terminal_instant is None
                    or (last_usage_instant is not None
                        and terminal_instant[1] < last_usage_instant)):
                timed_usage_valid = False
            if record.get("reason") != "completed":
                turn_ledger_valid = False
            turn_open = False
            continue
        if record_type != "usage.record":
            continue
        usage_scope = record.get("usageScope")
        if usage_scope not in {"turn", "session"}:
            continue
        if not turn_open or record.get("model") != "kimi-code/k3":
            session_identity_valid = False
        usage = record.get("usage")
        if not isinstance(usage, dict):
            usage_valid = False
            continue

        def counter(name: str) -> int | None:
            value = usage.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) \
                and 0 <= value <= 2**63 - 1 else None

        current = {name: counter(name) for name in totals}
        if any(value is None for value in current.values()):
            usage_valid = False
            continue
        current = {name: int(value) for name, value in current.items()}
        occurred = instant(
            record.get("time") or record.get("timestamp")
            or record.get("occurred_at")
        )
        if occurred is None:
            timed_usage_valid = False
            occurred_at = None
            occurred_instant = None
        else:
            occurred_at, occurred_instant = occurred
            if (last_usage_instant is not None
                    and occurred_instant < last_usage_instant):
                timed_usage_valid = False
            last_usage_instant = occurred_instant
        request_instant: datetime | None = None
        if usage_scope == "session":
            if not pending_compaction_attempts:
                request_ledger_valid = False
                unexpected_usage = True
                duplicate_count += 1
            else:
                if len(pending_compaction_attempts) != 1:
                    # Kimi persists every outbound compaction attempt but only
                    # the successful response has usage.  Without a separate
                    # retry ledger, multiple attempts cannot be reconciled.
                    request_ledger_valid = False
                request_instant = pending_compaction_attempts[-1]
                pending_compaction_attempts = []
                model_request_count += 1
                compaction_request_count += 1
        elif not pending_attempts:
            request_ledger_valid = False
            unexpected_usage = True
            duplicate_count += 1
        else:
            failed_attempts = len(pending_attempts) - 1
            if failed_attempts:
                if (retry_group_index >= len(retry_groups)
                        or len(retry_groups[retry_group_index])
                           != failed_attempts):
                    retry_ledger_valid = False
                else:
                    retry_group_index += 1
                    request_retry_count += failed_attempts
            model_request_count += 1
            request_step, request_instant = pending_attempts[-1]
            settled_request_steps.update(step for step, _ in pending_attempts)
            pending_attempts = []
        if (request_instant is None or occurred_instant is None
                or occurred_instant < request_instant):
            timed_usage_valid = False
        events.append({
            "occurred_at": occurred_at,
            "n_input_tokens": (
                current["inputOther"] + current["inputCacheRead"]
                + current["inputCacheCreation"]
            ),
            "n_cache_tokens": current["inputCacheRead"],
            "n_output_tokens": current["output"],
        })
        for name in totals:
            totals[name] += current[name]
            if totals[name] > 2**63 - 1:
                usage_valid = False
    prompt_tokens = (
        totals["inputOther"] + totals["inputCacheRead"]
        + totals["inputCacheCreation"]
    )
    request_ledger_valid = (
        request_ledger_valid
        and retry_ledger_valid
        and retry_group_index == len(retry_groups)
        and not pending_attempts
        and not pending_compaction_attempts
        and model_request_count == len(events)
    )
    turn_ledger_valid = (
        turn_ledger_valid
        and not turn_open
        and turn_prompt_count >= 1
        and ended_turns == turn_prompt_count
    )
    session_identity_valid = session_identity_valid and metadata_count == 1
    observed_valid = usage_valid and session_identity_valid
    complete = (
        observed_valid
        and request_ledger_valid
        and turn_ledger_valid
        and bool(events)
        and prompt_tokens + totals["output"] > 0
    )
    observed = (
        not complete
        and observed_valid
        and not unexpected_usage
        and duplicate_count == 0
        and bool(events)
        and prompt_tokens + totals["output"] > 0
    )
    timed_complete = complete and timed_usage_valid
    return {
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "kimi-code",
        "model": "k3",
        "complete": complete,
        "request_count": len(events),
        "session_usage_model_request_count": model_request_count,
        "session_usage_request_attempt_count": request_attempt_count,
        "session_usage_request_retry_count": request_retry_count,
        "session_usage_compaction_request_count": compaction_request_count,
        "completed_turn_count": ended_turns,
        "turn_prompt_count": turn_prompt_count,
        "n_input_tokens": prompt_tokens,
        "n_cache_tokens": totals["inputCacheRead"],
        "n_output_tokens": totals["output"],
        "cache_creation_tokens": totals["inputCacheCreation"],
        "token_usage_events": events if (complete or observed) else [],
        "request_usage_complete": complete,
        "request_usage_observed": complete or observed,
        "timed_usage_complete": timed_complete,
        "request_ledger_duplicate_count": duplicate_count,
        "request_ledger_source": "kimi-code-0.39.1-main-wire-retry-v4",
        "usage_counters_valid": usage_valid,
        "session_identity_valid": session_identity_valid,
        "request_ledger_valid": request_ledger_valid,
        "turn_ledger_valid": turn_ledger_valid,
        "timed_usage_valid": timed_usage_valid,
        "wire_metadata_count": metadata_count,
        "provider_actual_cost_observed": False,
        "cost_semantics": (
            "api_equivalent_from_complete_tokens" if complete
            else "unavailable_incomplete_tokens"
        ),
        "usage_incomplete_reason": (
            None if complete else
            "turn_completion_ledger_mismatch" if observed else
            "request_ledger_unavailable_or_invalid"
        ),
        "usage_evidence_tier": (
            "complete_reconciled" if complete
            else "observed_unreconciled" if observed
            else "unavailable"
        ),
    }


def _install_command() -> str:
    return (
        "set -euo pipefail; "
        "if [ -f /etc/alpine-release ] || ldd --version 2>&1 | grep -qi musl; then "
        "  echo 'Kimi Code requires a glibc task image' >&2; exit 1; "
        "elif command -v apt-get >/dev/null 2>&1; then "
        "  apt-get update && DEBIAN_FRONTEND=noninteractive "
        "  apt-get install -y --no-install-recommends ca-certificates curl python3; "
        "elif command -v dnf >/dev/null 2>&1; then "
        "  dnf install -y ca-certificates curl python3; "
        "elif command -v yum >/dev/null 2>&1; then "
        "  yum install -y ca-certificates curl python3; "
        "else echo 'No supported package manager found' >&2; exit 1; fi; "
        'case "$(uname -m)" in '
        f"  x86_64) kimi_arch=x64; kimi_sha={KIMI_BINARY_SHA256['x86_64']} ;; "
        f"  aarch64|arm64) kimi_arch=arm64; kimi_sha={KIMI_BINARY_SHA256['aarch64']} ;; "
        "  *) echo 'Unsupported CPU architecture' >&2; exit 1 ;; "
        "esac; "
        f"kimi_file=/tmp/kimi-code-{KIMI_CLI_VERSION}; "
        f"kimi_url=https://code.kimi.com/kimi-code/binaries/{KIMI_CLI_VERSION}/"
        'kimi-code-linux-${kimi_arch}; '
        "curl --fail --silent --show-error --location "
        '  --output "${kimi_file}" "${kimi_url}"; '
        'printf \'%s  %s\\n\' "${kimi_sha}" "${kimi_file}" '
        "  | sha256sum --check --strict -; "
        "mkdir -p /opt/kimi-runtime/bin; "
        'install -m 0755 "${kimi_file}" /opt/kimi-runtime/bin/kimi; '
        f"test \"$(/opt/kimi-runtime/bin/kimi --version)\" = '{KIMI_CLI_VERSION}'"
    )


class KimiCode(BaseInstalledAgent):
    """Run pinned Kimi K3 headlessly with an isolated OAuth data root."""

    SUPPORTS_ATIF = True
    _REMOTE_HOME = PurePosixPath("/tmp/dradar-kimi-home")
    _REMOTE_USER_HOME = PurePosixPath("/tmp/dradar-kimi-user")
    _REMOTE_AUTH = _REMOTE_HOME / "credentials" / "kimi-code.json"
    _REMOTE_OAUTH_LOCK = _REMOTE_HOME / "oauth" / "kimi-code"
    _REMOTE_CONFIG = _REMOTE_HOME / "config.toml"
    _REMOTE_CLI = PurePosixPath("/opt/kimi-runtime/bin/kimi")
    _REMOTE_POLICY = PurePosixPath("/tmp/dradar-kimi-policy.py")
    _REMOTE_SKILLS = PurePosixPath("/tmp/dradar-kimi-empty-skills")
    _STREAM_FILE = "kimi-code.jsonl"
    _STDERR_FILE = "kimi-code.stderr.log"
    _SESSION_LOG_FILE = "kimi-code-session.log"
    _USAGE_FILE = "provider-usage.json"

    @staticmethod
    def name() -> str:
        return "kimi-code"

    def __init__(
        self,
        *args: Any,
        auth_json_file: str,
        kimi_cli_file: str,
        reasoning_effort: str,
        shared_oauth: bool = False,
        **kwargs: Any,
    ):
        auth = Path(auth_json_file)
        if not auth.is_file():
            raise ValueError("Kimi OAuth run credential is missing")
        cli = Path(kimi_cli_file)
        if not cli.is_file():
            raise ValueError("Verified host Kimi CLI executable is missing")
        if reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("Kimi reasoning_effort must be low, high, or max")
        if not isinstance(shared_oauth, bool):
            raise ValueError("Kimi shared_oauth must be a boolean")
        try:
            payload = json.loads(auth.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Kimi OAuth run credential is invalid") from exc
        secrets = {
            payload.get(name)
            for name in ("access_token", "refresh_token")
            if isinstance(payload.get(name), str) and payload[name]
        }
        if len(secrets) < 2:
            raise ValueError("Kimi OAuth run credential is not refreshable")
        self._auth_json_file = auth
        self._shared_oauth = shared_oauth
        self._reasoning_effort = reasoning_effort
        self._credential_values = secrets
        self._instruction = ""
        self._resume_attempts = 0
        self._session_id: str | None = None
        super().__init__(*args, **kwargs)
        self._runtime_safety = RuntimeSafety(self.logs_dir)

    def get_version_command(self) -> str:
        return f"{self._REMOTE_CLI.as_posix()} --version"

    def install_spec(self) -> AgentInstallSpec:
        version = self._version or KIMI_CLI_VERSION
        return AgentInstallSpec(
            agent_name=self.name(),
            version=version,
            steps=[InstallStep(user="root", run=_install_command())],
            verification_command=(
                f"test \"$({self._REMOTE_CLI.as_posix()} --version)\" = "
                f"'{KIMI_CLI_VERSION}'"
            ),
            cache_key=(
                f"dradar-kimi-code-{version}-node-native-runtime-v1"
            ),
        )

    def network_allowlist(self) -> NetworkAllowlist:
        return NetworkAllowlist(domains=["auth.kimi.com", "api.kimi.com"])

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        emit_worker_registered(runtime="pier", context="agent", profile="kimi")
        del context
        self._instruction = instruction
        remote_home = self._REMOTE_HOME.as_posix()
        remote_user_home = self._REMOTE_USER_HOME.as_posix()
        remote_auth = self._REMOTE_AUTH.as_posix()
        remote_lock = self._REMOTE_OAUTH_LOCK.as_posix()
        remote_config = self._REMOTE_CONFIG.as_posix()
        remote_cli = self._REMOTE_CLI.as_posix()
        remote_policy = self._REMOTE_POLICY.as_posix()
        remote_skills = self._REMOTE_SKILLS.as_posix()
        stream = f"/logs/agent/{self._STREAM_FILE}"
        stderr_log = f"/logs/agent/{self._STDERR_FILE}"
        session_log = f"/logs/agent/{self._SESSION_LOG_FILE}"
        env = self.build_process_env({
            "HOME": remote_user_home,
            "KIMI_CODE_HOME": remote_home,
            "KIMI_DISABLE_TELEMETRY": "1",
            "KIMI_CODE_NO_AUTO_UPDATE": "1",
            "KIMI_CLI_NO_AUTO_UPDATE": "1",
            "KIMI_DISABLE_CRON": "1",
            "KIMI_MODEL_THINKING_EFFORT": self._reasoning_effort,
        })
        for name in (
            "KIMI_API_KEY",
            "KIMI_MODEL_API_KEY",
            "MOONSHOT_API_KEY",
            "KIMI_CODE_PASSWORD",
        ):
            env.pop(name, None)
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(remote_home + '/credentials')} "
                f"{shlex.quote(remote_home + '/oauth')} "
                f"{shlex.quote(remote_user_home)} "
                f"{shlex.quote(remote_skills)} "
                f"&& chmod 700 {shlex.quote(remote_home)} "
                f"{shlex.quote(remote_home + '/credentials')} "
                f"{shlex.quote(remote_home + '/oauth')} "
                f"{shlex.quote(remote_user_home)} "
                f"{shlex.quote(remote_skills)} "
                + (
                    f"&& test -r {shlex.quote(remote_auth)} "
                    f"&& test -w {shlex.quote(remote_auth)} "
                    f"&& test -e {shlex.quote(remote_lock)}"
                    if self._shared_oauth
                    else f"&& : > {shlex.quote(remote_lock)}"
                )
            ),
            env=env,
        )
        local_config = self.logs_dir / "kimi-config.toml"
        local_policy = self.logs_dir / "kimi-policy.py"
        self._runtime_safety.prepare_host_layout()
        log_store = AgentLogStore(self.logs_dir)
        log_store.replace_text(local_config, KIMI_CONFIG)
        log_store.replace_text(local_policy, KIMI_POLICY)
        if not self._shared_oauth:
            await environment.upload_file(self._auth_json_file, remote_auth)
        await environment.upload_file(local_config, remote_config)
        await environment.upload_file(local_policy, remote_policy)
        targets = " ".join(
            shlex.quote(value)
            for value in (
                remote_auth,
                remote_lock,
                remote_config,
                remote_policy,
            )
        )
        if environment.default_user is not None and not self._shared_oauth:
            command = (
                f"chown {shlex.quote(str(environment.default_user))} {targets} "
                f"&& chmod 600 {shlex.quote(remote_auth)} "
                f"{shlex.quote(remote_lock)} {shlex.quote(remote_config)} "
                f"&& chmod 500 {shlex.quote(remote_policy)}"
            )
            await self.exec_as_root(environment, command=command, env=env)
        elif not self._shared_oauth:
            await self.exec_as_agent(
                environment,
                command=(
                    f"chmod 600 {shlex.quote(remote_auth)} "
                    f"{shlex.quote(remote_lock)} {shlex.quote(remote_config)} "
                    f"&& chmod 500 {shlex.quote(remote_policy)}"
                ),
                env=env,
            )
        version = self._version or KIMI_CLI_VERSION
        version_pattern = version.replace(".", r"\.")
        await self.exec_as_agent(
            environment,
            command=(
                f"{shlex.quote(remote_cli)} --version "
                f"| grep -Eq '(^| ){version_pattern}( |$)'"
            ),
            env=env,
        )
        common_flags = [
            "--model", "kimi-code/k3",
            "--output-format", "stream-json",
            "--skills-dir", remote_skills,
        ]

        def shared_oauth_guarded_command(command: str) -> str:
            """Keep container-created refresh files owned by the host user.

            Kimi rotates ``kimi-code.json`` with an atomic rename.  A Docker
            container running as root consequently replaces the bind-mounted
            host file with a root-owned inode.  Derive the numeric owner from
            the mounted credentials directory (never from a username), repair
            replacements while Kimi runs, and make one final repair before the
            container command returns to the host.
            """

            auth = shlex.quote(remote_auth)
            credentials = shlex.quote(remote_home + "/credentials")
            guarded = (
                f"oauth_auth={auth}; oauth_credentials={credentials}; "
                "oauth_owner=$(stat -c '%u:%g' \"$oauth_credentials\") || exit 1; "
                "oauth_guard_pid=''; "
                "oauth_repair() { "
                "[ -f \"$oauth_auth\" ] && [ ! -L \"$oauth_auth\" ] || return 0; "
                "oauth_current=$(stat -c '%u:%g' \"$oauth_auth\" 2>/dev/null) "
                "|| return 0; "
                "if [ \"$oauth_current\" != \"$oauth_owner\" ]; then "
                "chown \"$oauth_owner\" \"$oauth_auth\" || return 1; fi; "
                "chmod 600 \"$oauth_auth\"; "
                "}; "
                "oauth_cleanup() { "
                "oauth_status=$?; "
                "if [ -n \"$oauth_guard_pid\" ]; then "
                "kill \"$oauth_guard_pid\" 2>/dev/null || true; "
                "wait \"$oauth_guard_pid\" 2>/dev/null || true; fi; "
                "oauth_repair || true; "
                "exit \"$oauth_status\"; "
                "}; "
                "if [ \"$(id -u)\" = 0 ]; then "
                "(while :; do oauth_repair || true; sleep 0.02; done) & "
                "oauth_guard_pid=$!; fi; "
                "trap oauth_cleanup EXIT; "
                "trap 'exit 130' INT; trap 'exit 143' TERM; "
                + command
            )
            return "bash -o pipefail -c " + shlex.quote(guarded)

        def command_for(extra_flags: list[str], *, append: bool) -> str:
            flags = [*common_flags, *extra_flags]
            cli = " ".join(shlex.quote(part) for part in flags)
            tee = "tee -a" if append else "tee"
            stderr_tee = "tee -a" if append else "tee"
            command = (
                f"cd /app && {remote_cli} {cli} "
                f"2> >({stderr_tee} {stderr_log} >&2) "
                f"| {tee} {stream}"
            )
            if self._shared_oauth:
                return shared_oauth_guarded_command(command)
            return "bash -o pipefail -c " + shlex.quote(command)

        async def remote_session_id(*, copy_log: bool = False) -> str | None:
            result = await self.exec_as_agent(
                environment,
                command=unique_session_probe_command(
                    remote_home + "/sessions",
                    copy_to=session_log if copy_log else None,
                ),
                env=env,
            )
            return validated_session_id(result.stdout)

        async def run_initial() -> None:
            await self.exec_as_agent(
                environment,
                command=command_for(["--prompt", instruction], append=False),
                env=env,
            )

        async def run_resume(session_id: str, _prompt: str) -> None:
            await self.exec_as_agent(
                environment,
                command=command_for(
                    ["--session", session_id, "--prompt", instruction], append=True,
                ),
                env=env,
            )

        async def classify_retryable_error(error: BaseException) -> bool:
            if pier_exit_code(error) != KIMI_PROVIDER_CONNECTION_EXIT_CODE:
                return False
            try:
                result = await self.exec_as_agent(
                    environment,
                    command=f"tail -n 1 {shlex.quote(stderr_log)}",
                    env=env,
                )
            except Exception:
                return False
            return kimi_provider_connection_stderr_is_retryable(result.stdout)

        def announce_retry(attempt: int, delay: float, session_id: str) -> None:
            self.logger.warning(
                "Kimi temporary provider failure; resuming session %s in %ss "
                "(attempt %s/2)",
                session_id,
                int(delay),
                attempt,
            )

        try:
            resume_attempts, runtime_session = await run_with_kimi_resume(
                run_initial=run_initial,
                find_session_id=remote_session_id,
                run_resume=run_resume,
                on_retry=announce_retry,
                classify_retryable_error=classify_retryable_error,
            )
            self._resume_attempts = resume_attempts
            self._session_id = runtime_session
        finally:
            probe_cancellation: asyncio.CancelledError | None = None
            try:
                recovered_session_id = await asyncio.wait_for(
                    remote_session_id(copy_log=True),
                    timeout=_FINAL_SESSION_PROBE_TIMEOUT_SEC,
                )
                if recovered_session_id is not None:
                    self._session_id = recovered_session_id
            except asyncio.CancelledError as exc:
                probe_cancellation = exc
                self.logger.warning(
                    "Kimi session recovery was cancelled; preserving OAuth "
                    "state before propagating cancellation"
                )
            except Exception as exc:
                self.logger.warning("Could not recover Kimi session log: %s", exc)
            if not self._shared_oauth:
                try:
                    await environment.download_file(remote_auth, self._auth_json_file)
                    if os.name != "nt":
                        os.chmod(self._auth_json_file, 0o600)
                except Exception as exc:
                    self.logger.warning(
                        "Could not recover refreshed Kimi OAuth state: %s", exc
                    )
            if probe_cancellation is not None:
                raise probe_cancellation

    def _redact_or_reject_credential_output(
        self, paths: list[Path],
    ) -> dict[Path, str]:
        return AgentLogStore(self.logs_dir).redact_texts(
            paths,
            self._credential_values,
            "[REDACTED_KIMI_CREDENTIAL]",
            retain_paths={
                path for path in paths
                if path.name in {self._STREAM_FILE, self._SESSION_LOG_FILE}
            },
        )

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if block.get("type") == "text" and isinstance(text, str):
                parts.append(text)
        return "\n\n".join(part for part in parts if part)

    def populate_context_post_run(self, context: AgentContext) -> None:
        stream_path = self.logs_dir / self._STREAM_FILE
        stderr_path = self.logs_dir / self._STDERR_FILE
        session_log_path = self.logs_dir / self._SESSION_LOG_FILE
        safe_logs = self._redact_or_reject_credential_output(
            [
                stream_path,
                stderr_path,
                session_log_path,
                self.logs_dir / "kimi-config.toml",
                self.logs_dir / "kimi-policy.py",
            ]
        )
        stream_text = safe_logs.get(stream_path)
        if stream_text is None:
            return
        lines = stream_text.splitlines()
        steps: list[Step] = []
        retry_records: list[dict] = []
        if self._instruction:
            steps.append(
                Step(step_id=1, source="user", message=self._instruction)
            )
        session_id: str | None = None
        assistant_calls = 0
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.step.retrying":
                retry_records.append(event)
            if event.get("type") == "session.resume_hint":
                value = event.get("session_id")
                if isinstance(value, str):
                    session_id = value
                continue
            if event.get("role") != "assistant":
                continue
            content = self._content_text(event.get("content"))
            if not content:
                continue
            assistant_calls += 1
            steps.append(
                Step(
                    step_id=len(steps) + 1,
                    source="agent",
                    message=content,
                    model_name=self.model_name,
                    reasoning_effort=self._reasoning_effort,
                    llm_call_count=1,
                )
            )
        if assistant_calls == 0:
            return
        session_log_text = safe_logs.get(session_log_path)
        if (
            session_log_text is None
            or len(session_log_text.encode("utf-8")) > _MAX_USAGE_WIRE_BYTES
        ):
            wire_lines = []
        else:
            wire_lines = session_log_text.splitlines()
            if len(wire_lines) > _MAX_USAGE_WIRE_RECORDS:
                wire_lines = []
        wire_records = []
        for line in wire_lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                wire_records.append(record)
        usage_facts = _kimi_usage_facts(wire_records, retry_records)
        prompt_tokens = usage_facts["n_input_tokens"] if usage_facts["complete"] else 0
        cached_tokens = usage_facts["n_cache_tokens"] if usage_facts["complete"] else 0
        output_tokens = usage_facts["n_output_tokens"] if usage_facts["complete"] else 0
        cache_creation_tokens = (
            usage_facts["cache_creation_tokens"] if usage_facts["complete"] else 0
        )
        try:
            AgentLogStore(self.logs_dir).replace_text(
                self.logs_dir / self._USAGE_FILE,
                json.dumps(usage_facts, ensure_ascii=False, separators=(",", ":")),
            )
        except UnsafeAgentLog:
            pass
        metrics = FinalMetrics(
            total_prompt_tokens=prompt_tokens or None,
            total_completion_tokens=output_tokens or None,
            total_cached_tokens=cached_tokens or None,
            total_cost_usd=None,
            total_steps=len(steps),
            extra={
                "billing_basis": "subscription",
                "cost_not_reported": True,
                "cache_creation_tokens": cache_creation_tokens,
                "resume_attempts": self._resume_attempts,
            },
        )
        trajectory = Trajectory(
            schema_version="ATIF-v1.7",
            session_id=session_id or self._session_id or str(uuid.uuid4()),
            agent=Agent(
                name=self.name(),
                version=self._version or "unknown",
                model_name=self.model_name,
                extra={"provider": "kimi-subscription", "oauth": True},
            ),
            steps=steps,
            final_metrics=metrics,
        )
        try:
            AgentLogStore(self.logs_dir).replace_text(
                self.logs_dir / "trajectory.json",
                json.dumps(trajectory.to_json_dict(), indent=2, ensure_ascii=False),
            )
        except UnsafeAgentLog:
            return
        populate_context_from_final_metrics(context, metrics)


__all__ = ["KIMI_CONFIG", "KimiCode"]
