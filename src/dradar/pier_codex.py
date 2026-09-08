"""Stock Codex adapter with the structured Pier worker lifecycle signal."""

from pier.agents.installed.codex import Codex

try:
    from _dradar_pier_credential_delivery import credential_upload_environment
except ModuleNotFoundError as exc:
    if exc.name != "_dradar_pier_credential_delivery":
        raise
    from dradar.pier_credential_delivery import credential_upload_environment

try:
    from _dradar_worker_events import emit_worker_registered, verify_task_baseline
except ModuleNotFoundError:
    from dradar.worker_events import emit_worker_registered, verify_task_baseline


class CodexRegistered(Codex):
    async def run(self, instruction, environment, context):
        await verify_task_baseline(environment)
        emit_worker_registered(runtime="pier", context="agent", profile="codex")
        source = self._resolve_auth_json_path()
        auth_environment = credential_upload_environment(environment, self, [source] if source else [])
        await super().run(instruction, auth_environment, context)
