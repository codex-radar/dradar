"""Private Pier adapter for the official Kimi Code subscription CLI.

Only Kimi's managed OAuth service is supported.  The adapter injects a pinned
standalone CLI and one locked run-copy of the credential into the task
container; API-key providers and ambient Kimi settings are deliberately
excluded.
"""

from __future__ import annotations

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
keep_alive_on_exit = false
bash_task_timeout_s = 600
bash_auto_background_on_timeout = false

[tools]
enabled = ["Read", "Write", "Edit", "Bash", "Grep", "Glob"]
disabled = ["AgentSwarm", "WebSearch", "FetchURL", "CronCreate"]

[[permission.rules]]
decision = "deny"
pattern = "Read(/tmp/dradar-kimi-home/**)"

[[permission.rules]]
decision = "deny"
pattern = "Write(/tmp/dradar-kimi-home/**)"

[[permission.rules]]
decision = "deny"
pattern = "Edit(/tmp/dradar-kimi-home/**)"

[[permission.rules]]
decision = "deny"
pattern = "Grep(/tmp/dradar-kimi-home/**)"

[[permission.rules]]
decision = "deny"
pattern = "Glob(/tmp/dradar-kimi-home/**)"

[[permission.rules]]
decision = "deny"
pattern = "Bash(*dradar-kimi-home*)"

[[permission.rules]]
decision = "deny"
pattern = "Bash(*KIMI_CODE_HOME*)"

[[permission.rules]]
decision = "deny"
pattern = "Bash(*credentials/kimi-code.json*)"

[[permission.rules]]
decision = "deny"
pattern = "Bash(*oauth/kimi-code*)"
"""


class KimiCode(BaseInstalledAgent):
    """Run pinned Kimi K3 headlessly with an isolated OAuth data root."""

    SUPPORTS_ATIF = True
    _REMOTE_HOME = PurePosixPath("/tmp/dradar-kimi-home")
    _REMOTE_USER_HOME = PurePosixPath("/tmp/dradar-kimi-user")
    _REMOTE_AUTH = _REMOTE_HOME / "credentials" / "kimi-code.json"
    _REMOTE_OAUTH_LOCK = _REMOTE_HOME / "oauth" / "kimi-code"
    _REMOTE_CONFIG = _REMOTE_HOME / "config.toml"
    _REMOTE_BIN_DIR = PurePosixPath("/tmp/dradar-kimi-bin")
    _REMOTE_CLI = _REMOTE_BIN_DIR / "kimi"
    _REMOTE_SKILLS = PurePosixPath("/tmp/dradar-kimi-empty-skills")
    _STREAM_FILE = "kimi-code.jsonl"
    _SESSION_LOG_FILE = "kimi-code-session.log"

    @staticmethod
    def name() -> str:
        return "kimi-code"

    def __init__(
        self,
        *args: Any,
        auth_json_file: str,
        kimi_cli_file: str,
        reasoning_effort: str,
        **kwargs: Any,
    ):
        auth = Path(auth_json_file)
        if not auth.is_file():
            raise ValueError("Kimi OAuth run credential is missing")
        cli = Path(kimi_cli_file)
        if not cli.is_file():
            raise ValueError("Pinned Kimi CLI executable is missing")
        if reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("Kimi reasoning_effort must be low, high, or max")
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
        self._kimi_cli_file = cli
        self._reasoning_effort = reasoning_effort
        self._credential_values = secrets
        self._instruction = ""
        super().__init__(*args, **kwargs)

    def get_version_command(self) -> str:
        return "true"

    def install_spec(self) -> AgentInstallSpec:
        version = self._version or "0.36.0"
        return AgentInstallSpec(
            agent_name=self.name(),
            version=version,
            steps=[InstallStep(user="root", run="true")],
            verification_command="true",
            cache_key=f"dradar-kimi-code-{version}-host-binary-v1",
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
        del context
        self._instruction = instruction
        remote_home = self._REMOTE_HOME.as_posix()
        remote_user_home = self._REMOTE_USER_HOME.as_posix()
        remote_auth = self._REMOTE_AUTH.as_posix()
        remote_lock = self._REMOTE_OAUTH_LOCK.as_posix()
        remote_config = self._REMOTE_CONFIG.as_posix()
        remote_bin = self._REMOTE_BIN_DIR.as_posix()
        remote_cli = self._REMOTE_CLI.as_posix()
        remote_skills = self._REMOTE_SKILLS.as_posix()
        stream = f"/logs/agent/{self._STREAM_FILE}"
        session_log = f"/logs/agent/{self._SESSION_LOG_FILE}"
        env = self.build_process_env({
            "HOME": remote_user_home,
            "KIMI_CODE_HOME": remote_home,
            "KIMI_DISABLE_TELEMETRY": "1",
            "KIMI_CODE_NO_AUTO_UPDATE": "1",
            "KIMI_CLI_NO_AUTO_UPDATE": "1",
            "KIMI_DISABLE_CRON": "1",
            "KIMI_CODE_AGENT_SWARM_MAX_CONCURRENCY": "1",
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
                f"{shlex.quote(remote_user_home)} {shlex.quote(remote_bin)} "
                f"{shlex.quote(remote_skills)} "
                f"&& chmod 700 {shlex.quote(remote_home)} "
                f"{shlex.quote(remote_home + '/credentials')} "
                f"{shlex.quote(remote_home + '/oauth')} "
                f"{shlex.quote(remote_user_home)} {shlex.quote(remote_bin)} "
                f"{shlex.quote(remote_skills)} "
                f"&& : > {shlex.quote(remote_lock)}"
            ),
            env=env,
        )
        local_config = self.logs_dir / "kimi-config.toml"
        local_config.write_text(KIMI_CONFIG, encoding="utf-8")
        await environment.upload_file(self._kimi_cli_file, remote_cli)
        await environment.upload_file(self._auth_json_file, remote_auth)
        await environment.upload_file(local_config, remote_config)
        targets = " ".join(
            shlex.quote(value)
            for value in (remote_cli, remote_auth, remote_lock, remote_config)
        )
        if environment.default_user is not None:
            await self.exec_as_root(
                environment,
                command=(
                    f"chown {shlex.quote(str(environment.default_user))} {targets} "
                    f"&& chmod 700 {shlex.quote(remote_cli)} "
                    f"&& chmod 600 {shlex.quote(remote_auth)} "
                    f"{shlex.quote(remote_lock)} {shlex.quote(remote_config)}"
                ),
                env=env,
            )
        else:
            await self.exec_as_agent(
                environment,
                command=(
                    f"chmod 700 {shlex.quote(remote_cli)} "
                    f"&& chmod 600 {shlex.quote(remote_auth)} "
                    f"{shlex.quote(remote_lock)} {shlex.quote(remote_config)}"
                ),
                env=env,
            )
        version = self._version or "0.36.0"
        version_pattern = version.replace(".", r"\.")
        await self.exec_as_agent(
            environment,
            command=(
                f"{shlex.quote(remote_cli)} --version "
                f"| grep -Eq '(^| ){version_pattern}( |$)'"
            ),
            env=env,
        )
        flags = [
            "--model", "kimi-code/k3",
            "--prompt", instruction,
            "--output-format", "stream-json",
            "--skills-dir", remote_skills,
        ]
        cli = " ".join(shlex.quote(part) for part in flags)
        command = (
            f"bash -o pipefail -c "
            f"{shlex.quote(f'{remote_cli} {cli} 2>&1 | tee {stream}')}"
        )
        try:
            await self.exec_as_agent(environment, command=command, env=env)
        finally:
            try:
                await self.exec_as_agent(
                    environment,
                    command=(
                        "candidate=$(find " + shlex.quote(remote_home + "/sessions")
                        + " -type f -path '*/logs/kimi-code.log' -print 2>/dev/null "
                        "| tail -n 1); "
                        f"if [ -n \"$candidate\" ]; then cp \"$candidate\" "
                        f"{shlex.quote(session_log)}; fi"
                    ),
                    env=env,
                )
            except Exception as exc:
                self.logger.warning("Could not recover Kimi session log: %s", exc)
            try:
                await environment.download_file(remote_auth, self._auth_json_file)
                if os.name != "nt":
                    os.chmod(self._auth_json_file, 0o600)
            except Exception as exc:
                self.logger.warning("Could not recover refreshed Kimi OAuth state: %s", exc)

    def _redact_or_reject_credential_output(self, paths: list[Path]) -> None:
        leaked = False
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            redacted = text
            for value in self._credential_values:
                if value in redacted:
                    leaked = True
                    redacted = redacted.replace(value, "[REDACTED_KIMI_CREDENTIAL]")
            if redacted != text:
                path.write_text(redacted, encoding="utf-8")
        if leaked:
            raise ValueError(
                "Kimi credential material reached agent output; logs were redacted "
                "and the run was rejected"
            )

    def populate_context_post_run(self, context: AgentContext) -> None:
        stream_path = self.logs_dir / self._STREAM_FILE
        session_log_path = self.logs_dir / self._SESSION_LOG_FILE
        self._redact_or_reject_credential_output([stream_path, session_log_path])
        try:
            lines = stream_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return
        steps: list[Step] = []
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
            if event.get("type") == "session.resume_hint":
                value = event.get("session_id")
                if isinstance(value, str):
                    session_id = value
                continue
            if event.get("role") != "assistant":
                continue
            content = event.get("content")
            if not isinstance(content, str) or not content:
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
        output_tokens = 0
        try:
            session_log = session_log_path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            session_log = ""
        for value in re.findall(r"\boutputTokens=(\d+)\b", session_log):
            output_tokens += int(value)
        metrics = FinalMetrics(
            total_prompt_tokens=None,
            total_completion_tokens=output_tokens or None,
            total_cost_usd=None,
            total_steps=len(steps),
            extra={
                "billing_basis": "subscription",
                "cost_not_reported": True,
                "prompt_tokens_not_reported": True,
            },
        )
        trajectory = Trajectory(
            schema_version="ATIF-v1.7",
            session_id=session_id or str(uuid.uuid4()),
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
            (self.logs_dir / "trajectory.json").write_text(
                json.dumps(trajectory.to_json_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            return
        populate_context_from_final_metrics(context, metrics)


__all__ = ["KIMI_CONFIG", "KimiCode"]
