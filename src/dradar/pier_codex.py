"""Stock Codex adapter with the structured Pier worker lifecycle signal."""

import os
from pathlib import Path
import tempfile

from pier.agents.installed.codex import Codex

try:
    from _dradar_credential_files import credential_json, read_private_credential
except ModuleNotFoundError as exc:
    if exc.name != "_dradar_credential_files":
        raise
    from dradar.credential_files import credential_json, read_private_credential

try:
    from _dradar_pier_credential_delivery import credential_upload_environment
except ModuleNotFoundError as exc:
    if exc.name != "_dradar_pier_credential_delivery":
        raise
    from dradar.pier_credential_delivery import credential_upload_environment

try:
    from _dradar_worker_events import register_worker, verify_task_baseline
except ModuleNotFoundError:
    from dradar.worker_events import register_worker, verify_task_baseline


try:
    from _dradar_artifact_boundary import private_post_run
except ModuleNotFoundError:
    from dradar.artifact_boundary import private_post_run


class CodexRegistered(Codex):
    def verify_gpt6_subscription_auth(self, source):
        if (self.model_name or "").split("/")[-1] not in ("gpt-6-sol", "gpt-6-luna"):
            return
        # These lanes are subscription-only. Reject an API credential before
        # Pier can deliver it to the task container; never echo auth contents.
        extra_env = getattr(self, "_extra_env", {}) or {}
        forbidden = (
            "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE",
        )
        if any((os.environ.get(key) or extra_env.get(key)) for key in forbidden):
            raise RuntimeError("GPT-6 trial requires ChatGPT subscription authentication")
        if not source:
            raise RuntimeError("GPT-6 trial requires ChatGPT subscription authentication")
        try:
            snapshot = read_private_credential(Path(source))
            data = credential_json(snapshot)
            tokens = data.get("tokens")
            if (
                data.get("auth_mode") != "chatgpt"
                or data.get("OPENAI_API_KEY")
                or not isinstance(tokens, dict)
                or not isinstance(tokens.get("access_token"), str)
                or not tokens["access_token"]
            ):
                raise ValueError("not subscription auth")
        except (OSError, ValueError, TypeError):
            raise RuntimeError("GPT-6 trial requires ChatGPT subscription authentication") from None
        return snapshot

    async def verify_gpt6_runtime(self, environment):
        if (self.model_name or "").split("/")[-1] not in ("gpt-6-sol", "gpt-6-luna"):
            return
        result = await self.exec_as_agent(
            environment, command=self.get_version_command(), timeout_sec=10,
        )
        versions = [line.strip() for line in (result.stdout or "").splitlines()
                    if line.strip().startswith("codex-cli ")]
        if result.return_code != 0 or versions != [f"codex-cli {self._version}"]:
            raise RuntimeError("GPT-6 Codex container version does not match the requested runtime")

    async def run(self, instruction, environment, context):
        gpt6 = (self.model_name or "").split("/")[-1] in ("gpt-6-sol", "gpt-6-luna")
        source = self._resolve_auth_json_path() if gpt6 else None
        snapshot = None
        if gpt6:
            snapshot = self.verify_gpt6_subscription_auth(source)
        await self.verify_gpt6_runtime(environment)
        await verify_task_baseline(environment)
        await register_worker(runtime="pier", context="agent", profile="codex")
        if not gpt6:
            source = self._resolve_auth_json_path()
            auth_environment = credential_upload_environment(environment, self, [source] if source else [])
            await super().run(instruction, auth_environment, context)
            return
        # Stock Pier resolves and uploads auth.json again inside run(). Pin both
        # reads to the same private snapshot that passed the subscription check.
        with tempfile.TemporaryDirectory(prefix="dradar-gpt6-auth-") as directory:
            frozen = Path(directory) / "auth.json"
            fd = os.open(frozen, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            with os.fdopen(fd, "wb") as handle:
                handle.write(snapshot)
            prior = self._extra_env.get("CODEX_AUTH_JSON_PATH")
            self._extra_env["CODEX_AUTH_JSON_PATH"] = str(frozen)
            try:
                auth_environment = credential_upload_environment(environment, self, [frozen])
                await super().run(instruction, auth_environment, context)
            finally:
                if prior is None:
                    self._extra_env.pop("CODEX_AUTH_JSON_PATH", None)
                else:
                    self._extra_env["CODEX_AUTH_JSON_PATH"] = prior

    @private_post_run
    def populate_context_post_run(self, context):
        return super().populate_context_post_run(context)
