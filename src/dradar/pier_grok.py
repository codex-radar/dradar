"""Credential-isolated Pier adapter for the official Grok Build CLI.

This module deliberately supports only grok.com's OAuth subscription session.
It never accepts an xAI API key.  DRadar exposes this single file to Pier's
isolated Python environment through ``--agent-import-path``.
"""

from __future__ import annotations

import json
import math
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


GROK_CLI_VERSION = "1.0.3"
GROK_VERSION_PATTERN = GROK_CLI_VERSION.replace(".", r"\.")
GROK_LINUX_SHA256 = {
    "x86_64": "2a7d46dea3fbed067e4072258b835d401e017d6848dc996279f0fb3d668a0961",
    "aarch64": "ed44950eab90573b6f475191f5791713a56943939b3b9a62e3f4e95edd14acd9",
}


def _grok_usage_facts(events: list[dict]) -> dict:
    """Read complete cumulative Grok usage without cache overlap."""

    names = (
        "input_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "output_tokens",
    )
    previous = {name: 0 for name in names}
    timed_events = []
    valid = True
    for event in events:
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        current = {
            name: usage.get(name)
            if (isinstance(usage.get(name), int)
                and not isinstance(usage.get(name), bool)
                and usage[name] >= 0)
            else None
            for name in names
        }
        if any(value is None for value in current.values()):
            valid = False
            continue
        current = {name: int(value) for name, value in current.items()}
        if any(current[name] < previous[name] for name in names):
            valid = False
            continue
        delta = {name: current[name] - previous[name] for name in names}
        if any(delta.values()):
            raw_time = (
                event.get("timestamp") or event.get("created_at")
                or event.get("createdAt") or event.get("time")
            )
            occurred_at = None
            try:
                if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool):
                    seconds = float(raw_time) / (
                        1000 if raw_time > 10_000_000_000 else 1
                    )
                    instant = datetime.fromtimestamp(seconds, timezone.utc)
                elif isinstance(raw_time, str):
                    instant = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                    if instant.tzinfo is None:
                        raise ValueError
                    instant = instant.astimezone(timezone.utc)
                else:
                    raise ValueError
                occurred_at = instant.isoformat().replace("+00:00", "Z")
            except (OverflowError, OSError, TypeError, ValueError):
                pass
            timed_events.append({
                "occurred_at": occurred_at,
                "n_input_tokens": (
                    delta["input_tokens"] + delta["cache_read_input_tokens"]
                    + delta["cache_creation_input_tokens"]
                ),
                "n_cache_tokens": delta["cache_read_input_tokens"],
                "n_output_tokens": delta["output_tokens"],
            })
        previous = current
    prompt = (
        previous["input_tokens"] + previous["cache_read_input_tokens"]
        + previous["cache_creation_input_tokens"]
    )
    complete = valid and bool(timed_events) and prompt + previous["output_tokens"] > 0
    timed_complete = complete and all(event["occurred_at"] for event in timed_events)
    reported_cost = None
    for event in reversed(events):
        value = event.get("total_cost_usd")
        try:
            candidate = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(candidate) and candidate >= 0:
            reported_cost = candidate
            break
    return {
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "grok",
        "model": "grok-4.6",
        "complete": complete,
        "request_count": len(timed_events),
        "n_input_tokens": prompt,
        "n_cache_tokens": previous["cache_read_input_tokens"],
        "n_output_tokens": previous["output_tokens"],
        "cache_creation_tokens": previous["cache_creation_input_tokens"],
        "subscription_reported_cost_usd": reported_cost,
        "subscription_reported_cost_basis": "official-grok-cli",
        "token_usage_events": timed_events if timed_complete else [],
        "timed_usage_complete": timed_complete,
    }


def _install_command() -> str:
    return (
        "set -euo pipefail; "
        "if [ -f /etc/alpine-release ] || ldd --version 2>&1 | grep -qi musl; then "
        "  echo 'Grok Build requires a glibc task image' >&2; exit 1; "
        "elif command -v apt-get >/dev/null 2>&1; then "
        "  apt-get update && DEBIAN_FRONTEND=noninteractive "
        "  apt-get install -y --no-install-recommends ca-certificates curl; "
        "elif command -v dnf >/dev/null 2>&1; then "
        "  dnf install -y ca-certificates curl; "
        "elif command -v yum >/dev/null 2>&1; then "
        "  yum install -y ca-certificates curl; "
        "else echo 'No supported package manager found' >&2; exit 1; fi; "
        'case "$(uname -m)" in '
        f"  x86_64) grok_arch=x86_64; grok_sha={GROK_LINUX_SHA256['x86_64']} ;; "
        f"  aarch64|arm64) grok_arch=aarch64; grok_sha={GROK_LINUX_SHA256['aarch64']} ;; "
        "  *) echo 'Unsupported CPU architecture' >&2; exit 1 ;; "
        "esac; "
        "mkdir -p /opt/grok-runtime/bin; "
        f"grok_url=https://storage.googleapis.com/grok-build-public-artifacts/cli/"
        f"grok-{GROK_CLI_VERSION}-linux-${{grok_arch}}; "
        "curl --fail --silent --show-error --location "
        "  --output /opt/grok-runtime/bin/grok \"${grok_url}\"; "
        "printf '%s  %s\\n' \"${grok_sha}\" /opt/grok-runtime/bin/grok "
        "  | sha256sum --check --strict -; "
        "chmod 0755 /opt/grok-runtime/bin/grok; "
        "/opt/grok-runtime/bin/grok --version "
        f"  | grep -Eq '(^| ){GROK_VERSION_PATTERN}( |$)'"
    )


class GrokBuild(BaseInstalledAgent):
    """Run the official Grok CLI headlessly with an isolated OAuth home."""

    SUPPORTS_ATIF = True
    # Grok resolves credentials from ``$HOME/.grok/auth.json``. Its
    # GROK_HOME setting controls selected configuration paths but is not the
    # credential-home override, so using it alone silently falls back to an
    # unauthenticated 4.5 catalog.
    _REMOTE_USER_HOME = PurePosixPath("/tmp/dradar-grok-user")
    _REMOTE_HOME = _REMOTE_USER_HOME / ".grok"
    _REMOTE_AUTH = _REMOTE_HOME / "auth.json"
    _REMOTE_CLI = PurePosixPath("/opt/grok-runtime/bin/grok")
    _STREAM_FILE = "grok-build.jsonl"
    _USAGE_FILE = "provider-usage.json"
    _TOOLS = "read_file,grep,list_dir,search_replace,run_terminal_cmd,todo_write"

    @staticmethod
    def name() -> str:
        return "grok-build"

    def __init__(
        self,
        *args: Any,
        auth_json_file: str,
        grok_cli_file: str,
        reasoning_effort: str,
        **kwargs: Any,
    ):
        auth = Path(auth_json_file)
        if not auth.is_file():
            raise ValueError("Grok OAuth run credential is missing")
        cli = Path(grok_cli_file)
        if not cli.is_file():
            raise ValueError("Verified host Grok CLI executable is missing")
        if reasoning_effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError(
                "Grok reasoning_effort must be low, medium, high, or xhigh"
            )
        self._auth_json_file = auth
        self._reasoning_effort = reasoning_effort
        super().__init__(*args, **kwargs)

    def get_version_command(self) -> str:
        return f"{self._REMOTE_CLI.as_posix()} --version"

    def install_spec(self) -> AgentInstallSpec:
        version = self._version or GROK_CLI_VERSION
        return AgentInstallSpec(
            agent_name=self.name(),
            version=version,
            steps=[InstallStep(user="root", run=_install_command())],
            verification_command=(
                f"{self._REMOTE_CLI.as_posix()} --version "
                f"| grep -Eq '(^| ){GROK_VERSION_PATTERN}( |$)'"
            ),
            cache_key=f"dradar-grok-subscription-{version}-linux-runtime-v3",
        )

    def network_allowlist(self) -> NetworkAllowlist:
        # Runtime model traffic and silent OAuth refresh only.  Web search and
        # fetch are also removed at the CLI layer below.
        return NetworkAllowlist(
            domains=[
                "auth.x.ai",
                "cli-chat-proxy.grok.com",
                # Grok Build loads the subscription settings and
                # dynamic model catalog from the Code control plane before
                # opening the chat stream.  The proxy treats allowlist hosts
                # as exact names, so the apex entry does not cover this host.
                "code.grok.com",
                "grok.com",
            ]
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        del context
        remote_user_home = self._REMOTE_USER_HOME.as_posix()
        remote_home = self._REMOTE_HOME.as_posix()
        remote_auth = self._REMOTE_AUTH.as_posix()
        remote_cli = self._REMOTE_CLI.as_posix()
        env = self.build_process_env({
            "HOME": remote_user_home,
            "GROK_TELEMETRY_ENABLED": "0",
            "GROK_TELEMETRY_MIXPANEL_ENABLED": "0",
            "GROK_TELEMETRY_TRACE_UPLOAD": "0",
        })
        env.pop("GROK_HOME", None)
        # API keys are intentionally unsupported, including accidental ambient
        # keys baked into a task image or injected by a caller.
        env.pop("XAI_API_KEY", None)
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(remote_home)} "
                f"&& chmod 700 {shlex.quote(remote_home)}"
            ),
            env=env,
        )
        await environment.upload_file(self._auth_json_file, remote_auth)
        if environment.default_user is not None:
            await self.exec_as_root(
                environment,
                command=(
                    f"chown {shlex.quote(str(environment.default_user))} "
                    f"{shlex.quote(remote_auth)} "
                    f"&& chmod 600 {shlex.quote(remote_auth)}"
                ),
                env=env,
            )
        else:
            await self.exec_as_agent(
                environment,
                command=f"chmod 600 {shlex.quote(remote_auth)}",
                env=env,
            )
        version = self._version or GROK_CLI_VERSION
        version_pattern = version.replace(".", r"\.")
        await self.exec_as_agent(
            environment,
            command=(
                f"{shlex.quote(remote_cli)} --version "
                f"| grep -Eq '(^| ){version_pattern}( |$)'"
            ),
            env=env,
        )
        # Grok discovers subscription models dynamically.  A fresh,
        # auth-only GROK_HOME otherwise retains the bundled 4.5 fallback and
        # rejects 4.6 before making a model request.  Populate the isolated
        # model cache and fail closed if this OAuth slot cannot see 4.6.
        await self.exec_as_agent(
            environment,
            command=(
                f"{shlex.quote(remote_cli)} models "
                f"| grep -Fq {shlex.quote('grok-4.6')}"
            ),
            env=env,
        )

        stream = f"/logs/agent/{self._STREAM_FILE}"
        model = self.model_name or "grok-4.6"
        # The path rules are defense in depth around the credential file.  The
        # Docker/Pier egress allowlist remains the primary data-exfiltration
        # boundary for untrusted benchmark instructions.
        flags = [
            "--model", model,
            "--reasoning-effort", self._reasoning_effort,
            "--output-format", "streaming-messages-json",
            "--always-approve",
            "--disable-web-search",
            "--no-subagents",
            "--no-memory",
            "--no-plan",
            "--tools", self._TOOLS,
            "--deny", f"Read({remote_home}/**)",
            "--deny", f"Grep({remote_home}/**)",
            "--deny", f"Edit({remote_home}/**)",
            "--deny", f"Write({remote_home}/**)",
            "--deny", "Bash(*auth.json*)",
            "--deny", "Bash(*.grok*)",
            "--deny", "Bash(*GROK_HOME*)",
            "--deny", f"Bash(*{remote_user_home}*)",
        ]
        cli = " ".join(shlex.quote(part) for part in flags)
        command = (
            f"{shlex.quote(remote_cli)} -p {shlex.quote(instruction)} "
            f"{cli} 2>&1 </dev/null | "
            f"tee {shlex.quote(stream)}"
        )
        try:
            await self.exec_as_agent(environment, command=command, env=env)
        finally:
            # Silent refresh mutates auth.json.  Return that mutation to the
            # locked host run-copy even when the model command fails.
            try:
                await environment.download_file(remote_auth, self._auth_json_file)
                if os.name != "nt":
                    os.chmod(self._auth_json_file, 0o600)
            except Exception as exc:
                self.logger.warning("Could not recover refreshed Grok OAuth state: %s", exc)

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        parts: list[str] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
        return "\n\n".join(part for part in parts if part)

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Create a conservative ATIF transcript from the Messages NDJSON.

        Subscription usage is not API billing, so cost deliberately remains
        unknown instead of being reported as zero dollars.
        """

        path = self.logs_dir / self._STREAM_FILE
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        steps: list[Step] = []
        session_id: str | None = None
        parsed_events = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            parsed_events.append(event)
            session_id = event.get("session_id") or event.get("sessionId") or session_id
            message = event.get("message") if isinstance(event.get("message"), dict) else event
            role = message.get("role")
            text = self._content_text(message.get("content"))
            if not text or role not in {"user", "assistant"}:
                continue
            steps.append(
                Step(
                    step_id=len(steps) + 1,
                    source="agent" if role == "assistant" else "user",
                    message=text,
                    model_name=(self.model_name if role == "assistant" else None),
                    reasoning_effort=(self._reasoning_effort if role == "assistant" else None),
                    llm_call_count=(1 if role == "assistant" else None),
                )
            )
        if not steps:
            return
        usage_facts = _grok_usage_facts(parsed_events)
        input_tokens = usage_facts["n_input_tokens"] if usage_facts["complete"] else 0
        cached_tokens = usage_facts["n_cache_tokens"] if usage_facts["complete"] else 0
        output_tokens = usage_facts["n_output_tokens"] if usage_facts["complete"] else 0
        created_tokens = usage_facts["cache_creation_tokens"] if usage_facts["complete"] else 0
        try:
            (self.logs_dir / self._USAGE_FILE).write_text(
                json.dumps(usage_facts, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError:
            pass
        metrics = FinalMetrics(
            total_prompt_tokens=input_tokens or None,
            total_completion_tokens=output_tokens or None,
            total_cached_tokens=cached_tokens or None,
            total_cost_usd=None,
            total_steps=len(steps),
            extra={
                "billing_basis": "subscription",
                "cost_not_reported": True,
                "cache_creation_tokens": created_tokens,
            },
        )
        trajectory = Trajectory(
            schema_version="ATIF-v1.7",
            session_id=session_id or str(uuid.uuid4()),
            agent=Agent(
                name=self.name(),
                version=self._version or "unknown",
                model_name=self.model_name,
                extra={"provider": "xai-subscription", "oauth": True},
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


__all__ = ["GrokBuild"]
