"""Stock Codex adapter with the structured Pier worker lifecycle signal."""

from pier.agents.installed.codex import Codex

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
        await self.verify_gpt6_runtime(environment)
        await verify_task_baseline(environment)
        await register_worker(runtime="pier", context="agent", profile="codex")
        source = self._resolve_auth_json_path()
        auth_environment = credential_upload_environment(environment, self, [source] if source else [])
        await super().run(instruction, auth_environment, context)

    @private_post_run
    def populate_context_post_run(self, context):
        return super().populate_context_post_run(context)
