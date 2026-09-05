"""Subscription-only Claude Code adapter for the stock Pier runtime.

The adapter accepts either an owner-only ``claude setup-token`` file or a
native subscription OAuth configuration. Native credentials stay outside
collected logs even if cleanup cannot run. API credentials are rejected.
"""

from __future__ import annotations

import os
import stat
import json
import re
import shlex
import tempfile
import uuid
from pathlib import Path

from pier.agents.installed.claude_code import ClaudeCode
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.trial.paths import EnvironmentPaths
try:
    from _dradar_credential_files import claude_config_payload, read_private_credential, is_claude_metered_auth
except ModuleNotFoundError as exc:
    if exc.name != "_dradar_credential_files":
        raise
    from dradar.credential_files import claude_config_payload, read_private_credential, is_claude_metered_auth
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

    def __init__(self, *args, oauth_token_file: str | None = None,
                 oauth_config_file: str | None = None, **kwargs):
        if bool(oauth_token_file) == bool(oauth_config_file):
            raise ValueError("select exactly one Claude subscription authentication format")
        self._oauth_token = None
        self._oauth_config = None
        self._remote_config_root = None
        if oauth_config_file:
            payload = claude_config_payload(read_private_credential(Path(oauth_config_file)))
            self._oauth_config = json.dumps(payload, separators=(",", ":")).encode()
        else:
            self._oauth_token_path = Path(oauth_token_file)
            self._oauth_token = self._read_private_oauth(self._oauth_token_path)
        extra_env = dict(kwargs.pop("extra_env", {}) or {})
        for name in list(extra_env):
            if is_claude_metered_auth(name) or name in {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}:
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
        if is_claude_metered_auth(key):
            return None
        return super()._get_env(key)

    def build_process_env(self, base=None, *, include_resolved_env=True):
        env = super().build_process_env(base, include_resolved_env=include_resolved_env)
        env = {key: value for key, value in env.items() if not is_claude_metered_auth(key)}
        if self._oauth_config is not None:
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        if self._remote_config_root and "CLAUDE_CONFIG_DIR" in env:
            env["CLAUDE_CONFIG_DIR"] = self._remote_config_root
        return env

    async def exec_as_agent(self, environment, command, env=None, cwd=None, timeout_sec=None):
        if env:
            env = {key: value for key, value in env.items() if not is_claude_metered_auth(key)}
            if self._remote_config_root and "CLAUDE_CONFIG_DIR" in env:
                env["CLAUDE_CONFIG_DIR"] = self._remote_config_root
                env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        return await super().exec_as_agent(
            environment, command, env=env, cwd=cwd, timeout_sec=timeout_sec,
        )

    async def _prepare_native_config(self, environment):
        # Upstream places CLAUDE_CONFIG_DIR inside /logs/agent. Never put an
        # OAuth JSON there, even temporarily: failed cleanup must stay private.
        root = "/tmp/dradar-claude-auth-" + uuid.uuid4().hex
        self._remote_config_root = root
        uid_result = await super().exec_as_agent(environment, "id -u", timeout_sec=30)
        uid = (uid_result.stdout or "").strip()
        if uid_result.return_code != 0 or not re.fullmatch(r"[0-9]+", uid):
            raise ValueError("cannot identify the Claude container user")
        result = await self.exec_as_root(environment, command=f"umask 077; mkdir {shlex.quote(root)}", timeout_sec=30)
        if result.return_code != 0:
            raise ValueError("cannot create private Claude configuration directory")
        with tempfile.TemporaryDirectory(prefix="dradar-claude-upload-") as temporary:
            source = Path(temporary) / ".credentials.json"
            fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(self._oauth_config)
            await environment.upload_file(source, root + "/.credentials.json")
        result = await self.exec_as_root(
            environment, command=(
                f"chmod 700 {shlex.quote(root)} && "
                f"chmod 600 {shlex.quote(root + '/.credentials.json')} && "
                f"chown -R {uid} {shlex.quote(root)}"
            ), timeout_sec=30,
        )
        if result.return_code != 0:
            raise ValueError("cannot protect private Claude configuration")
        projects = str(EnvironmentPaths.agent_dir / "sessions" / "projects")
        result = await super().exec_as_agent(
            environment, command=(
                f"mkdir -p {shlex.quote(projects)} && "
                f"ln -s {shlex.quote(projects)} {shlex.quote(root + '/projects')}"
            ), timeout_sec=30,
        )
        if result.return_code != 0:
            raise ValueError("cannot retain Claude session logs separately from authentication")

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        try:
            if self._oauth_config is not None:
                await self._prepare_native_config(environment)
            emit_worker_registered(runtime="pier", context="agent", profile="claude")
            await super().run(instruction, environment, context)
        finally:
            if self._remote_config_root:
                root = self._remote_config_root
                self._remote_config_root = None
                result = await self.exec_as_root(
                    environment, command=f"rm -rf -- {shlex.quote(root)}", timeout_sec=30,
                )
                if result.return_code != 0:
                    raise ValueError("private Claude credential cleanup failed; credentials remain outside collected logs")

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
