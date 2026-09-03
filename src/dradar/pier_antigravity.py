"""Credential-isolated Pier adapter for Google Antigravity CLI.

The adapter intentionally supports only the Google-account subscription flow.
It pins Google's Linux binary, runs one fresh headless project per benchmark
task, and reconciles the official streaming token ledger before exposing any
usage to DRadar.
"""

from __future__ import annotations

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


ANTIGRAVITY_CLI_VERSION = "1.1.22"
ANTIGRAVITY_MODEL = "gemini-3.7-flash"
ANTIGRAVITY_RUNTIME_MODELS = {
    "low": "gemini-3.7-flash-low",
    "medium": "gemini-3.7-flash-medium",
    "high": "gemini-3.7-flash-high",
}
ANTIGRAVITY_LINUX_RELEASE = "1.1.22-5711547746615296"
ANTIGRAVITY_LINUX_SHA512 = {
    "x86_64": (
        "40225d4b1f009412e905f0a234ba3d51487038d1ad1b8fa19331c84be55610a0"
        "1f5b0ad9916fb871151cc45456c6bc30cc0b1ea5dab6c0616bc8fb262bcdd7a9"
    ),
    "aarch64": (
        "b37a718330eb5e270e1ca70135bf964a407ba626fbff7537ac58e094ea31bc623"
        "e6d216ef197188fe8b5c46e6f57aee64a3b7c9e23fc855cefee43fe434179d3"
    ),
}
ANTIGRAVITY_STREAM_INTERRUPTED_MESSAGE = (
    "The stream was interrupted. Please continue the task you were working on."
)
ANTIGRAVITY_TERMINAL_RECOVERY_SCHEMA = (
    "dradar-antigravity-terminal-recovery-v1"
)


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


def _antigravity_terminal_error_category(
    terminal_status: object, terminal_error: object,
) -> str | None:
    """Reduce provider text to a credential-free, fixed diagnostic enum."""

    if terminal_status == "SUCCESS":
        return None
    if terminal_status == "CANCELED":
        return "canceled"
    if terminal_status == "INTERRUPTED":
        return "interrupted"
    if terminal_status == "INVALID":
        return "invalid"
    if terminal_status != "ERROR":
        return None
    if terminal_error == ANTIGRAVITY_STREAM_INTERRUPTED_MESSAGE:
        return "stream-interrupted"
    low = terminal_error.casefold() if isinstance(terminal_error, str) else ""
    if (
        "eligibility check failed" in low
        and "not eligible for antigravity" in low
        and "not currently available in your location" in low
    ):
        return "eligibility-location"
    if any(marker in low for marker in (
        "individual quota reached",
        "quota exhausted",
        "quota exceeded",
        "usage limit reached",
        "you've hit your usage limit",
        "you have hit your usage limit",
    )):
        return "quota-limit"
    return "provider-error"


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
        "model": ANTIGRAVITY_MODEL,
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
    terminal_error_category = _antigravity_terminal_error_category(
        terminal_status, terminal_error,
    )
    if terminal_error_category is not None:
        # Never preserve the provider's raw error here. It may contain account,
        # location, prompt, or request details; the fixed enum is sufficient for
        # local outcome handling and authenticated operator diagnostics.
        facts["terminal_error_category"] = terminal_error_category
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
    return (
        "set -euo pipefail; "
        "if [ -f /etc/alpine-release ] || ldd --version 2>&1 | grep -qi musl; then "
        "  echo 'Antigravity CLI requires a glibc task image' >&2; exit 1; "
        "elif command -v apt-get >/dev/null 2>&1; then "
        "  apt-get update && DEBIAN_FRONTEND=noninteractive "
        "  apt-get install -y --no-install-recommends ca-certificates curl; "
        "elif command -v dnf >/dev/null 2>&1; then "
        "  dnf install -y ca-certificates curl tar gzip; "
        "elif command -v yum >/dev/null 2>&1; then "
        "  yum install -y ca-certificates curl tar gzip; "
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
        self._runtime_model = ANTIGRAVITY_RUNTIME_MODELS[reasoning_effort]
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
            cache_key=f"dradar-antigravity-{version}-linux-runtime-v1",
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
            for slug in ANTIGRAVITY_RUNTIME_MODELS.values()
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
        await self.exec_as_agent(
            environment,
            command="bash -o pipefail -c " + shlex.quote(
                f"umask 077; cd /app && {command} 2>{shlex.quote(stderr)} "
                f"| tee {shlex.quote(stream)}"
            ),
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
