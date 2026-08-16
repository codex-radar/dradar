"""Private Pier adapter for the official Grok Build subscription CLI.

This module deliberately supports only grok.com's OAuth subscription session.
It never accepts an xAI API key.  DRadar exposes this single file to Pier's
isolated Python environment through ``--agent-import-path``.
"""

from __future__ import annotations

import json
import os
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


class GrokBuild(BaseInstalledAgent):
    """Run the official Grok CLI headlessly with an isolated OAuth home."""

    SUPPORTS_ATIF = True
    _REMOTE_HOME = PurePosixPath("/tmp/dradar-grok-home")
    _REMOTE_AUTH = _REMOTE_HOME / "auth.json"
    _REMOTE_BIN_DIR = PurePosixPath("/tmp/dradar-grok-bin")
    _REMOTE_CLI = _REMOTE_BIN_DIR / "grok"
    _STREAM_FILE = "grok-build.jsonl"
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
            raise ValueError("Pinned Grok CLI executable is missing")
        if reasoning_effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError(
                "Grok reasoning_effort must be low, medium, high, or xhigh"
            )
        self._auth_json_file = auth
        self._grok_cli_file = cli
        self._reasoning_effort = reasoning_effort
        super().__init__(*args, **kwargs)

    def get_version_command(self) -> str:
        # The pinned host binary is uploaded only after the task container is
        # running.  The exact version is verified immediately after upload.
        return "true"

    def install_spec(self) -> AgentInstallSpec:
        version = self._version or "1.0.0"
        return AgentInstallSpec(
            agent_name=self.name(),
            version=version,
            # Docker build networking happens before Pier's runtime egress
            # proxy exists.  Keep the image layer offline and inject the
            # already verified standalone CLI binary at runtime instead.
            steps=[InstallStep(user="root", run="true")],
            verification_command="true",
            cache_key=f"dradar-grok-subscription-{version}-host-binary-v2",
        )

    def network_allowlist(self) -> NetworkAllowlist:
        # Runtime model traffic and silent OAuth refresh only.  Web search and
        # fetch are also removed at the CLI layer below.
        return NetworkAllowlist(
            domains=["auth.x.ai", "cli-chat-proxy.grok.com", "grok.com"]
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        del context
        remote_home = self._REMOTE_HOME.as_posix()
        remote_auth = self._REMOTE_AUTH.as_posix()
        remote_bin = self._REMOTE_BIN_DIR.as_posix()
        remote_cli = self._REMOTE_CLI.as_posix()
        env = self.build_process_env({
            "GROK_HOME": remote_home,
            "GROK_TELEMETRY_ENABLED": "0",
            "GROK_TELEMETRY_MIXPANEL_ENABLED": "0",
            "GROK_TELEMETRY_TRACE_UPLOAD": "0",
        })
        # API keys are intentionally unsupported, including accidental ambient
        # keys baked into a task image or injected by a caller.
        env.pop("XAI_API_KEY", None)
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(remote_home)} {shlex.quote(remote_bin)} "
                f"&& chmod 700 {shlex.quote(remote_home)} {shlex.quote(remote_bin)}"
            ),
            env=env,
        )
        await environment.upload_file(self._grok_cli_file, remote_cli)
        await environment.upload_file(self._auth_json_file, remote_auth)
        if environment.default_user is not None:
            await self.exec_as_root(
                environment,
                command=(
                    f"chown {shlex.quote(str(environment.default_user))} "
                    f"{shlex.quote(remote_cli)} {shlex.quote(remote_auth)} "
                    f"&& chmod 700 {shlex.quote(remote_cli)} "
                    f"&& chmod 600 {shlex.quote(remote_auth)}"
                ),
                env=env,
            )
        else:
            await self.exec_as_agent(
                environment,
                command=(
                    f"chmod 700 {shlex.quote(remote_cli)} "
                    f"&& chmod 600 {shlex.quote(remote_auth)}"
                ),
                env=env,
            )
        version = self._version or "1.0.0"
        version_pattern = version.replace(".", r"\.")
        await self.exec_as_agent(
            environment,
            command=(
                f"{shlex.quote(remote_cli)} --version "
                f"| grep -Eq '(^| ){version_pattern}( |$)'"
            ),
            env=env,
        )
        # Grok 1.0.0 discovers subscription models dynamically.  A fresh,
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
            "--deny", "Bash(*GROK_HOME*)",
            "--deny", f"Bash(*{remote_home}*)",
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
        input_tokens = output_tokens = 0
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            session_id = event.get("session_id") or event.get("sessionId") or session_id
            usage = event.get("usage")
            if isinstance(usage, dict):
                input_tokens = max(input_tokens, int(usage.get("input_tokens") or 0))
                output_tokens = max(output_tokens, int(usage.get("output_tokens") or 0))
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
        metrics = FinalMetrics(
            total_prompt_tokens=input_tokens or None,
            total_completion_tokens=output_tokens or None,
            total_cost_usd=None,
            total_steps=len(steps),
            extra={"billing_basis": "subscription", "cost_not_reported": True},
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
