"""Stock Codex adapter with the structured Pier worker lifecycle signal."""

from pier.agents.installed.codex import Codex

try:
    from _dradar_worker_events import emit_worker_registered, verify_task_baseline
except ModuleNotFoundError:
    from dradar.worker_events import emit_worker_registered, verify_task_baseline


try:
    from _dradar_artifact_boundary import private_post_run
except ModuleNotFoundError:
    from dradar.artifact_boundary import private_post_run


class CodexRegistered(Codex):
    async def run(self, instruction, environment, context):
        await verify_task_baseline(environment)
        emit_worker_registered(runtime="pier", context="agent", profile="codex")
        await super().run(instruction, environment, context)

    @private_post_run
    def populate_context_post_run(self, context):
        return super().populate_context_post_run(context)
