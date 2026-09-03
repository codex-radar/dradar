"""Stock Codex adapter with the structured Pier worker lifecycle signal."""

from pier.agents.installed.codex import Codex

try:
    from _dradar_worker_events import emit_worker_registered
except ModuleNotFoundError:
    from dradar.worker_events import emit_worker_registered


class CodexRegistered(Codex):
    async def run(self, instruction, environment, context):
        emit_worker_registered(runtime="pier", context="agent", profile="codex")
        await super().run(instruction, environment, context)
