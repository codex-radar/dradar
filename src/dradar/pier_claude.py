"""Subscription-only Claude Code adapter for the stock Pier runtime.

The adapter deliberately accepts one owner-only file produced from
``claude setup-token``. It rejects Anthropic API credentials and keeps the
secret out of Pier argv, task files, trajectories, and DRadar HTTP payloads.
Claude Code itself receives the OAuth value only in its process environment
inside the disposable Pier container.
"""

from __future__ import annotations

import os
import stat
import json
from pathlib import Path

from pier.agents.installed.claude_code import ClaudeCode
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
try:
    from _dradar_worker_events import emit_worker_registered
except ModuleNotFoundError:
    from dradar.worker_events import emit_worker_registered
try:
    from _dradar_claude_usage import claude_usage_facts
except ModuleNotFoundError as exc:
    if exc.name != "_dradar_claude_usage":
        raise
    from dradar.claude_usage import claude_usage_facts


class ClaudeCodeSubscription(ClaudeCode):
    """Claude.ai subscription variant with a fail-closed auth boundary."""

    def __init__(self, *args, oauth_token_file: str, **kwargs):
        self._oauth_token_path = Path(oauth_token_file)
        self._oauth_token = self._read_private_oauth(self._oauth_token_path)
        extra_env = dict(kwargs.pop("extra_env", {}) or {})
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            extra_env.pop(name, None)
        # Safe mode disables host/project customizations but leaves built-in
        # tools and the outer bypassPermissions policy intact. This makes the
        # benchmark reproducible without reducing in-container coding access.
        extra_env["CLAUDE_CODE_SAFE_MODE"] = "1"
        extra_env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        kwargs["extra_env"] = extra_env
        super().__init__(*args, **kwargs)

    @staticmethod
    def _read_private_oauth(path: Path) -> str:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("Claude OAuth credential must be a regular file")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Claude OAuth credential must have mode 0600")
        token = path.read_text(encoding="utf-8").strip()
        if not token.startswith("sk-ant-oat") or len(token) < 40:
            raise ValueError(
                "Claude credential is not an official subscription OAuth token"
            )
        return token

    def _get_env(self, key: str) -> str | None:
        if key == "CLAUDE_CODE_OAUTH_TOKEN":
            return self._oauth_token
        if key in {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK",
            "AWS_BEARER_TOKEN_BEDROCK",
        } or key.startswith("AWS_"):
            return None
        return super()._get_env(key)

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        emit_worker_registered(runtime="pier", context="agent", profile="claude")
        await super().run(instruction, environment, context)

    def populate_context_post_run(self, context) -> None:
        """Keep upstream ATIF output and add a reconciled subscription ledger."""

        super().populate_context_post_run(context)
        trajectory_path = self.logs_dir / "trajectory.json"
        try:
            trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        usage = claude_usage_facts(trajectory, self.model_name or "")
        if usage is None:
            return
        try:
            (self.logs_dir / "provider-usage.json").write_text(
                json.dumps(usage, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError:
            return
