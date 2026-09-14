"""Credential-isolated Pier adapter for Google Antigravity CLI.

The adapter intentionally supports only the Google-account subscription flow.
It pins Google's Linux binary, runs one fresh headless project per benchmark
task, and reconciles the official streaming token ledger before exposing any
usage to DRadar.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import uuid
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
    from _dradar_worker_events import emit_worker_registered, verify_task_baseline
except ModuleNotFoundError:
    from dradar.worker_events import emit_worker_registered, verify_task_baseline


ANTIGRAVITY_CLI_VERSION = "1.1.27"
ANTIGRAVITY_MODEL = "gemini-3.7-flash"
ANTIGRAVITY_FLASH_38_MODEL = "gemini-3.8-flash"
ANTIGRAVITY_MODELS = frozenset({ANTIGRAVITY_MODEL, ANTIGRAVITY_FLASH_38_MODEL})
ANTIGRAVITY_RUNTIME_MODELS = {
    "low": "gemini-3.7-flash-low",
    "medium": "gemini-3.7-flash-medium",
    "high": "gemini-3.7-flash-high",
}
ANTIGRAVITY_MODEL_RUNTIME_MODELS = {
    model: {effort: f"{model}-{effort}" for effort in ("low", "medium", "high")}
    for model in ANTIGRAVITY_MODELS
}
ANTIGRAVITY_LINUX_RELEASE = "1.1.27-5211191891591168"
ANTIGRAVITY_LINUX_SHA512 = {
    "x86_64": "793d4b9ea2c08d9a7e50bafa02cfc8c19424bd60d6e83f91408d45f9c6d4ce79a5d576fede5bef164d823abf84f81359a14b4ca665952c47b0a7cfd743bb69c0",
    "aarch64": "ed45f6930785aa4b42f14e07ace1c9d91a94fb76e760f54acbd7d3d3951e1f957fd456a0dae2a3124dd9a3b689bf7afb7c9303a3e4ba95037fc10063424d9bf9"
}
ANTIGRAVITY_STREAM_INTERRUPTED_MESSAGE = (
    "The stream was interrupted. Please continue the task you were working on."
)
ANTIGRAVITY_TERMINAL_RECOVERY_SCHEMA = (
    "dradar-antigravity-terminal-recovery-v1"
)
ANTIGRAVITY_LOOP_BREAKER_SCRIPT = r'''#!/usr/bin/env python3
"""Watchdog stream filter that breaks repetitive read-only tool deadlocks in Antigravity."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_MAX_REPEATS = 5
READ_ONLY_TOOLS = frozenset({
    "view_file",
    "list_dir",
    "grep_search",
    "find_by_name",
    "read_resource",
    "read_url_content",
    "list_resources",
})
TRIGGER_MARKER_PATH = Path("/tmp/dradar-loop-breaker-triggered")


def _find_antigravity_pids() -> list[int]:
    pids: list[int] = []
    my_pid = os.getpid()
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return pids
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            if pid == my_pid:
                continue
            comm_file = entry / "comm"
            comm = comm_file.read_bytes().strip() if comm_file.is_file() else b""
            if comm in (b"bash", b"sh", b"python", b"python3", b"tee"):
                continue
            cmdline = (entry / "cmdline").read_bytes()
            parts = [p for p in cmdline.split(b"\x00") if p]
            if not parts:
                continue
            argv0 = parts[0]
            if comm == b"antigravity" or argv0 == b"antigravity" or argv0.endswith(b"/antigravity"):
                pids.append(pid)
        except (OSError, IOError, ValueError):
            continue
    return pids


def _escalate_terminate(pids: list[int], delay: float = 10.0) -> None:
    time.sleep(delay)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


class LoopBreaker:
    def __init__(
        self,
        max_repeats: int | None = None,
        on_break: Any = None,
        stderr_stream: Any = None,
        marker_path: Path | None = None,
    ):
        if max_repeats is None:
            raw = os.environ.get("DRADAR_LOOP_BREAKER_MAX_REPEATS")
            try:
                max_repeats = int(raw) if raw else DEFAULT_MAX_REPEATS
            except ValueError:
                max_repeats = DEFAULT_MAX_REPEATS
        self.max_repeats = max(1, max_repeats)
        self.on_break = on_break
        self.stderr = stderr_stream if stderr_stream is not None else sys.stderr
        self.marker_path = marker_path or TRIGGER_MARKER_PATH
        self.last_tool_call: tuple[str, str] | None = None
        self.repeat_count = 0
        self.triggered = False
        self.seen_steps: set[int] = set()

    def handle_tool_call(self, tool_name: str, parameters: dict[str, Any]) -> bool:
        if tool_name not in READ_ONLY_TOOLS:
            self.last_tool_call = None
            self.repeat_count = 0
            return False

        try:
            serialized = json.dumps(parameters, sort_keys=True, ensure_ascii=False)
        except Exception:
            serialized = str(parameters)
        key = (tool_name, serialized)

        if key == self.last_tool_call:
            self.repeat_count += 1
        else:
            self.last_tool_call = key
            self.repeat_count = 1

        if self.repeat_count >= self.max_repeats and not self.triggered:
            self.triggered = True
            self.fire(tool_name, serialized)
            return True
        return False

    def process_line(self, line: str) -> bool:
        line_str = line.strip()
        if not line_str or not line_str.startswith("{"):
            return False
        try:
            event = json.loads(line_str)
        except Exception:
            return False
        if not isinstance(event, dict) or event.get("event") != "step_update":
            return False
        step = event.get("step_update")
        if not isinstance(step, dict) or step.get("state") != "ACTIVE":
            return False
        tool_name = step.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return False
        step_index = step.get("step_index")
        tool_info = step.get("tool_info")
        params = tool_info.get("parameters") if isinstance(tool_info, dict) else None
        # Incomplete updates are not evidence of a distinct tool invocation.
        if type(step_index) is not int or step_index < 0 or not isinstance(params, dict):
            self.last_tool_call = None
            self.repeat_count = 0
            return False
        if step_index in self.seen_steps:
            return False
        self.seen_steps.add(step_index)
        return self.handle_tool_call(tool_name, params)

    def fire(self, tool_name: str, serialized_params: str) -> None:
        try:
            self.marker_path.touch(exist_ok=True)
        except OSError:
            pass
        try:
            self.stderr.write(
                f"[dradar-loop-breaker] Deadlock detected: {self.repeat_count} consecutive "
                f"identical calls to read-only tool '{tool_name}' with parameters {serialized_params}. "
                "Triggering interrupt.\n"
            )
            self.stderr.flush()
        except Exception:
            pass

        if self.on_break is not None:
            self.on_break(tool_name, serialized_params)
        else:
            self._default_break()

    def _default_break(self) -> None:
        pids = _find_antigravity_pids()
        if not pids:
            try:
                self.stderr.write(
                    "[dradar-loop-breaker] Warning: No antigravity PID found to signal.\n"
                )
                self.stderr.flush()
            except Exception:
                pass
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGINT)
                self.stderr.write(
                    f"[dradar-loop-breaker] Sent SIGINT to antigravity (PID {pid}).\n"
                )
                self.stderr.flush()
            except OSError:
                pass
        t = threading.Thread(target=_escalate_terminate, args=(pids, 10.0), daemon=True)
        t.start()

    def run_stream(self, stdin=None, stdout=None) -> None:
        stdin = stdin if stdin is not None else sys.stdin
        stdout = stdout if stdout is not None else sys.stdout
        while True:
            line = stdin.readline()
            if not line:
                break
            stdout.write(line)
            stdout.flush()
            self.process_line(line)


if __name__ == "__main__":
    LoopBreaker().run_stream()
'''


def _model_line_pattern(model: str) -> str:
    """Match one exact model id in AGY's tabular ``models`` output."""

    return "^" + re.escape(model) + r"([[:space:]]|$)"


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _usage_values(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    names = (
        "input_tokens", "output_tokens", "thinking_tokens",
        "cache_read_tokens", "total_tokens",
    )
    parsed = {name: _nonnegative_int(value.get(name)) for name in names}
    if any(item is None for item in parsed.values()):
        return None
    raw = {name: int(item) for name, item in parsed.items() if item is not None}
    # AGY's official stream reports uncached prompt tokens in ``input_tokens``
    # and cached prompt tokens separately in ``cache_read_tokens``.  Its
    # ``total_tokens`` therefore excludes cache reads and includes thinking as
    # part of output.  DRadar's shared billing contract instead expects input
    # to include cache reads, with cache as a discounted subset.  Normalize at
    # this adapter boundary so the server can apply that contract without a
    # provider-specific exception or double-charging thinking.
    if (
        raw["total_tokens"] != raw["input_tokens"] + raw["output_tokens"]
        or raw["thinking_tokens"] > raw["output_tokens"]
    ):
        return None
    normalized_input = raw["input_tokens"] + raw["cache_read_tokens"]
    return {
        "input_tokens": normalized_input,
        "output_tokens": raw["output_tokens"],
        "thinking_tokens": raw["thinking_tokens"],
        "cache_read_tokens": raw["cache_read_tokens"],
        "total_tokens": normalized_input + raw["output_tokens"],
    }


def _antigravity_usage_facts(
    events: list[dict], *, expected_runtime_model: str,
) -> dict[str, object]:
    """Reconcile AGY's per-step ledger against its terminal aggregate.

    Checkpoint steps can consume tokens independently of agent-response steps,
    so every unique DONE step carrying an official usage object participates in
    the sum.  ACTIVE text deltas never carry billable weight.
    """

    init_events = [
        event.get("init") for event in events
        if isinstance(event, dict)
        and event.get("event") == "init"
        and isinstance(event.get("init"), dict)
    ]
    result_events = [
        event.get("result") for event in events
        if isinstance(event, dict)
        and event.get("event") == "result"
        and isinstance(event.get("result"), dict)
    ]
    init = init_events[0] if len(init_events) == 1 else None
    terminal = result_events[0] if len(result_events) == 1 else None
    terminal_usage = _usage_values(
        terminal.get("usage") if terminal is not None else None
    )

    totals = {
        name: 0 for name in (
            "input_tokens", "output_tokens", "thinking_tokens",
            "cache_read_tokens", "total_tokens",
        )
    }
    token_usage_events: list[dict[str, int]] = []
    seen_steps: dict[tuple[int, str], dict[str, int]] = {}
    ledger_valid = True
    for event in events:
        if not isinstance(event, dict) or event.get("event") != "step_update":
            continue
        step = event.get("step_update")
        if (
            not isinstance(step, dict)
            or step.get("state") != "DONE"
            or "usage" not in step
        ):
            continue
        index = _nonnegative_int(step.get("step_index"))
        step_type = step.get("step_type")
        usage = _usage_values(step.get("usage"))
        if index is None or not isinstance(step_type, str) or usage is None:
            ledger_valid = False
            continue
        identity = (index, step_type)
        previous = seen_steps.get(identity)
        if previous is not None:
            if previous != usage:
                ledger_valid = False
            continue
        seen_steps[identity] = usage
        for name in totals:
            totals[name] += usage[name]
        token_usage_events.append({
            "n_input_tokens": usage["input_tokens"],
            "n_cache_tokens": usage["cache_read_tokens"],
            "n_output_tokens": usage["output_tokens"],
            "thinking_tokens": usage["thinking_tokens"],
            "step_index": index,
            "step_type": step_type,
        })

    token_usage_events.sort(key=lambda item: (item["step_index"], item["step_type"]))
    init_valid = (
        init is not None
        and init.get("model") == expected_runtime_model
        and init.get("cwd") == "/app"
        and init.get("permission_mode") == "always-proceed"
    )
    num_turns = (
        _nonnegative_int(terminal.get("num_turns"))
        if terminal is not None else None
    )
    terminal_status = terminal.get("status") if terminal is not None else None
    terminal_valid = (
        terminal_usage is not None
        and num_turns == 1
        and terminal_status in {
            "SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID",
        }
    )
    reconciled = (
        ledger_valid
        and init_valid
        and terminal_valid
        and bool(token_usage_events)
        and terminal_usage == totals
        and totals["total_tokens"] > 0
    )
    observed = ledger_valid and bool(token_usage_events)
    selected = totals if observed else {name: 0 for name in totals}
    facts: dict[str, object] = {
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "antigravity",
        "model": expected_runtime_model.rsplit("-", 1)[0],
        "provider_runtime_model": expected_runtime_model,
        "complete": reconciled,
        "request_count": len(token_usage_events) if observed else 0,
        "n_input_tokens": selected["input_tokens"],
        "n_cache_tokens": selected["cache_read_tokens"],
        "n_output_tokens": selected["output_tokens"],
        "thinking_tokens": selected["thinking_tokens"],
        "token_usage_events": token_usage_events if observed else [],
        "request_usage_complete": reconciled,
        "request_usage_observed": observed,
        "timed_usage_complete": False,
        "usage_incomplete_reason": (
            None if reconciled else
            "terminal_aggregate_missing_or_inconsistent" if observed else
            "request_ledger_unavailable_or_invalid"
        ),
        "usage_evidence_tier": (
            "complete_reconciled" if reconciled
            else "observed_unreconciled" if observed
            else "unavailable"
        ),
        "terminal_status": terminal_status,
    }
    terminal_response = terminal.get("response") if terminal is not None else None
    terminal_error = terminal.get("error") if terminal is not None else None
    if (
        reconciled
        and terminal_status == "ERROR"
        and terminal_error == ANTIGRAVITY_STREAM_INTERRUPTED_MESSAGE
        and isinstance(terminal_response, str)
        and terminal_response.strip()
    ):
        # Do not reinterpret ERROR as success here.  Preserve the provider's
        # terminal status and expose only a narrow, content-bound recovery
        # candidate.  The server independently verifies the response hash
        # against trajectory.json, plus the non-empty patch and completed Pier
        # result, before it may accept the run for grading.
        facts["terminal_recovery"] = {
            "schema": ANTIGRAVITY_TERMINAL_RECOVERY_SCHEMA,
            "reason": "stream_interrupted_after_final_response",
            "response_sha256": hashlib.sha256(
                terminal_response.strip().encode("utf-8")
            ).hexdigest(),
        }
    return facts


def _install_command() -> str:
    b64_breaker = base64.b64encode(
        ANTIGRAVITY_LOOP_BREAKER_SCRIPT.encode("utf-8")
    ).decode("ascii")
    return (
        "set -euo pipefail; "
        "if [ -f /etc/alpine-release ] || ldd --version 2>&1 | grep -qi musl; then "
        "  echo 'Antigravity CLI requires a glibc task image' >&2; exit 1; "
        "elif command -v apt-get >/dev/null 2>&1; then "
        "  apt-get update && DEBIAN_FRONTEND=noninteractive "
        "  apt-get install -y --no-install-recommends ca-certificates curl python3; "
        "elif command -v dnf >/dev/null 2>&1; then "
        "  dnf install -y ca-certificates curl tar gzip python3; "
        "elif command -v yum >/dev/null 2>&1; then "
        "  yum install -y ca-certificates curl tar gzip python3; "
        "else echo 'No supported package manager found' >&2; exit 1; fi; "
        'case "$(uname -m)" in '
        f"  x86_64) agy_dir=x64; agy_arch=x64; agy_sha={ANTIGRAVITY_LINUX_SHA512['x86_64']} ;; "
        f"  aarch64|arm64) agy_dir=arm; agy_arch=arm64; agy_sha={ANTIGRAVITY_LINUX_SHA512['aarch64']} ;; "
        "  *) echo 'Unsupported CPU architecture' >&2; exit 1 ;; "
        "esac; "
        "mkdir -p /opt/antigravity-runtime/bin; "
        f"agy_url=https://storage.googleapis.com/antigravity-public/antigravity-cli/"
        f"{ANTIGRAVITY_LINUX_RELEASE}/linux-${{agy_dir}}/cli_linux_${{agy_arch}}.tar.gz; "
        "curl --fail --silent --show-error --location "
        "  --output /tmp/antigravity-cli.tar.gz \"${agy_url}\"; "
        "printf '%s  %s\\n' \"${agy_sha}\" /tmp/antigravity-cli.tar.gz "
        "  | sha512sum --check --strict -; "
        "tar -xzf /tmp/antigravity-cli.tar.gz -C /opt/antigravity-runtime/bin; "
        "rm -f /tmp/antigravity-cli.tar.gz; "
        "chmod 0755 /opt/antigravity-runtime/bin/antigravity; "
        f"echo {shlex.quote(b64_breaker)} | base64 -d > /opt/antigravity-runtime/bin/loop_breaker.py; "
        "chmod 0755 /opt/antigravity-runtime/bin/loop_breaker.py; "
        "/opt/antigravity-runtime/bin/antigravity --version "
        f"  | grep -Fqx '{ANTIGRAVITY_CLI_VERSION}'"
    )


class Antigravity(BaseInstalledAgent):
    """Run Gemini 3.7 Flash through a DRadar-owned AGY subscription."""

    SUPPORTS_ATIF = True
    _REMOTE_USER_HOME = PurePosixPath("/tmp/dradar-antigravity-user")
    _REMOTE_GEMINI_HOME = _REMOTE_USER_HOME / ".gemini"
    _REMOTE_CLI = PurePosixPath("/opt/antigravity-runtime/bin/antigravity")
    _STREAM_FILE = "antigravity.jsonl"
    _STDERR_FILE = "antigravity.stderr.log"
    _USAGE_FILE = "provider-usage.json"

    @staticmethod
    def name() -> str:
        return "antigravity"

    def __init__(
        self,
        *args: Any,
        auth_home_dir: str,
        reasoning_effort: str,
        shared_oauth: bool = False,
        **kwargs: Any,
    ):
        auth_home = Path(auth_home_dir)
        if not auth_home.is_dir():
            raise ValueError("Antigravity OAuth home is missing")
        if reasoning_effort not in ANTIGRAVITY_RUNTIME_MODELS:
            raise ValueError("Antigravity reasoning_effort must be low, medium, or high")
        if not isinstance(shared_oauth, bool):
            raise ValueError("Antigravity shared_oauth must be a boolean")
        self._auth_home_dir = auth_home
        self._reasoning_effort = reasoning_effort
        model = str(kwargs.get("model_name") or ANTIGRAVITY_MODEL).split("/")[-1]
        if model not in ANTIGRAVITY_MODELS:
            raise ValueError(f"unsupported Antigravity model: {model}")
        self._runtime_model = (
            ANTIGRAVITY_MODEL_RUNTIME_MODELS[model][reasoning_effort]
        )
        self._shared_oauth = shared_oauth
        self._instruction = ""
        super().__init__(*args, **kwargs)

    def get_version_command(self) -> str:
        return f"{self._REMOTE_CLI.as_posix()} --version"

    def install_spec(self) -> AgentInstallSpec:
        version = self._version or ANTIGRAVITY_CLI_VERSION
        return AgentInstallSpec(
            agent_name=self.name(),
            version=version,
            steps=[InstallStep(user="root", run=_install_command())],
            verification_command=(
                f"{self._REMOTE_CLI.as_posix()} --version "
                f"| grep -Fqx {shlex.quote(ANTIGRAVITY_CLI_VERSION)}"
            ),
            cache_key=f"dradar-antigravity-{version}-linux-runtime-v2",
        )

    def network_allowlist(self) -> NetworkAllowlist:
        return NetworkAllowlist(domains=[
            "accounts.google.com",
            "antigravity-unleash.goog",
            "daily-cloudcode-pa.googleapis.com",
            "lh3.googleusercontent.com",
            "oauth2.googleapis.com",
            "storage.googleapis.com",
            "www.googleapis.com",
        ])

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        await verify_task_baseline(environment)
        emit_worker_registered(runtime="pier", context="agent", profile="antigravity")
        del context
        self._instruction = instruction
        remote_home = self._REMOTE_USER_HOME.as_posix()
        remote_gemini = self._REMOTE_GEMINI_HOME.as_posix()
        remote_cli = self._REMOTE_CLI.as_posix()
        env = self.build_process_env({
            "HOME": remote_home,
            "AGY_CLI_HIDE_LOGO": "1",
        })
        for name in (
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
            "AGY_ADC_AUTH",
        ):
            env.pop(name, None)
        await self.exec_as_agent(
            environment,
            command=(
                f"test -d {shlex.quote(remote_gemini)} "
                f"&& test -r {shlex.quote(remote_gemini + '/antigravity-cli/settings.json')}"
            ),
            env=env,
        )
        models_file = "/tmp/dradar-antigravity-models.txt"
        model_checks = " && ".join(
            (
                f"grep -Eq {shlex.quote(_model_line_pattern(slug))} "
                f"{shlex.quote(models_file)}"
            )
            for slug in (self._runtime_model,)
        )
        await self.exec_as_agent(
            environment,
            command=(
                f"umask 077; {shlex.quote(remote_cli)} models > {shlex.quote(models_file)} "
                f"&& {model_checks}"
            ),
            env=env,
        )
        stream = f"/logs/agent/{self._STREAM_FILE}"
        stderr = f"/logs/agent/{self._STDERR_FILE}"
        invocation = [
            remote_cli,
            "--new-project",
            "--print", instruction,
            "--model", self._runtime_model,
            "--effort", self._reasoning_effort,
            "--mode", "accept-edits",
            # Pier's disposable Docker environment is the security boundary.
            # A second interactive approval/sandbox layer can soft-deny tools
            # in headless mode and produce an invalid empty patch, so every
            # model and child-agent tool is approved inside the container.
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
            "--output-format", "stream-json",
            "--print-timeout", "120m",
        ]
        command = " ".join(shlex.quote(part) for part in invocation)
        remote_loop_breaker = "/opt/antigravity-runtime/bin/loop_breaker.py"
        b64_breaker = base64.b64encode(
            ANTIGRAVITY_LOOP_BREAKER_SCRIPT.encode("utf-8")
        ).decode("ascii")
        breaker_setup = (
            f"if [ ! -f {shlex.quote(remote_loop_breaker)} ]; then "
            f"  echo {shlex.quote(b64_breaker)} | base64 -d > /tmp/loop_breaker.py "
            f"  && chmod 0755 /tmp/loop_breaker.py "
            f"  && dradar_breaker=/tmp/loop_breaker.py; "
            f"else dradar_breaker={shlex.quote(remote_loop_breaker)}; fi; "
        )
        # A watcher marker only records intent to interrupt. Preserve producer
        # errors until a native terminal-receipt contract supports recovery.
        pipeline_cmd = (
            f"{breaker_setup}"
            f"rm -f /tmp/dradar-loop-breaker-triggered; "
            f"umask 077; cd /app || exit $?; "
            f"{command} 2>{shlex.quote(stderr)} "
            f'| python3 -u "$dradar_breaker" '
            f"| tee {shlex.quote(stream)}; "
            f'pipe_status=("${{PIPESTATUS[@]}}"); '
            f'for i in 1 2 0; do s="${{pipe_status[$i]}}"; '
            f'[ "$s" -eq 0 ] || exit "$s"; done; exit 0'
        )
        await self.exec_as_agent(
            environment,
            command="bash -o pipefail -c " + shlex.quote(pipeline_cmd),
            env=env,
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        path = self.logs_dir / self._STREAM_FILE
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        events: list[dict] = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        usage = _antigravity_usage_facts(
            events, expected_runtime_model=self._runtime_model,
        )
        try:
            (self.logs_dir / self._USAGE_FILE).write_text(
                json.dumps(usage, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError:
            pass

        terminal = next((
            event.get("result") for event in events
            if event.get("event") == "result"
            and isinstance(event.get("result"), dict)
        ), {})
        response = terminal.get("response") if isinstance(terminal, dict) else None
        if not isinstance(response, str) or not response.strip():
            error_msg = terminal.get("error") if isinstance(terminal, dict) else None
            if isinstance(error_msg, str) and error_msg.strip():
                response = error_msg.strip()
            elif terminal.get("status") in {"INTERRUPTED", "CANCELED"}:
                response = f"Execution {terminal.get('status').lower()}."
            else:
                return
        steps = [
            Step(step_id=1, source="user", message=self._instruction),
            Step(
                step_id=2,
                source="agent",
                message=response.strip(),
                model_name=self.model_name,
                reasoning_effort=self._reasoning_effort,
                llm_call_count=usage["request_count"],
            ),
        ]
        complete = usage["complete"] is True
        metrics = FinalMetrics(
            total_prompt_tokens=usage["n_input_tokens"] if complete else None,
            total_completion_tokens=usage["n_output_tokens"] if complete else None,
            total_cached_tokens=usage["n_cache_tokens"] if complete else None,
            total_cost_usd=None,
            total_steps=len(steps),
            extra={
                "billing_basis": "subscription",
                "cost_not_reported": True,
                "thinking_tokens": usage["thinking_tokens"],
                "provider_runtime_model": self._runtime_model,
            },
        )
        conversation_id = terminal.get("conversation_id")
        trajectory = Trajectory(
            schema_version="ATIF-v1.7",
            session_id=(
                conversation_id
                if isinstance(conversation_id, str) and conversation_id
                else str(uuid.uuid4())
            ),
            agent=Agent(
                name=self.name(),
                version=self._version or ANTIGRAVITY_CLI_VERSION,
                model_name=self.model_name,
                extra={
                    "provider": "google-antigravity-subscription",
                    "oauth": True,
                    "runtime_model": self._runtime_model,
                },
            ),
            steps=steps,
            final_metrics=metrics,
        )
        try:
            (self.logs_dir / "trajectory.json").write_text(
                json.dumps(trajectory.to_json_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            return
        populate_context_from_final_metrics(context, metrics)


__all__ = ["Antigravity"]
