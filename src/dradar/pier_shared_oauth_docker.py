"""Pier Docker environment with narrowly scoped shared OAuth mounts.

The paid subscription CLIs rotate refresh tokens while they run.  Multiple
independent task containers therefore need to see the same credential store
and the provider's own cross-process lock, while Pier's ordinary log mounts
and every task workspace remain isolated.

This module is copied into the per-run Pier import directory.  It deliberately
accepts only the credential targets used by DRadar's Kimi, Grok, and AGY
adapters; arbitrary host mounts are rejected.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import shutil
from pathlib import Path
from typing import Any

from pier.environments.docker.docker import DockerEnvironment


_ALLOWED_TARGETS = frozenset({
    "/tmp/dradar-kimi-home/credentials",
    "/tmp/dradar-kimi-home/oauth",
    "/tmp/dradar-grok-user/.grok",
    "/tmp/dradar-antigravity-user/.gemini",
})


def _validated_shared_mounts(raw: object) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            mounts = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("shared OAuth mounts are not valid JSON") from exc
    else:
        # Pier's --ek parser decodes JSON values before invoking custom
        # environments.  Accept that parsed value while applying the exact
        # same structural/path validation below.
        mounts = raw
    if not isinstance(mounts, list) or not 1 <= len(mounts) <= 2:
        raise ValueError("shared OAuth mounts must contain one or two entries")

    result: list[dict[str, Any]] = []
    targets: set[str] = set()
    for mount in mounts:
        if not isinstance(mount, dict) or set(mount) != {"type", "source", "target"}:
            raise ValueError("shared OAuth mount has an unsupported shape")
        source_value = mount.get("source")
        target = mount.get("target")
        if mount.get("type") != "bind" or target not in _ALLOWED_TARGETS:
            raise ValueError("shared OAuth mount target is not allowed")
        if target in targets:
            raise ValueError("shared OAuth mount target is duplicated")
        if not isinstance(source_value, str):
            raise ValueError("shared OAuth mount source must be a path")
        source = Path(source_value)
        if not source.is_absolute() or source.is_symlink() or not source.is_dir():
            raise ValueError("shared OAuth mount source must be an existing directory")
        resolved = source.resolve(strict=True)
        if resolved != source:
            raise ValueError("shared OAuth mount source must be canonical")
        if os.name != "nt":
            mode = stat.S_IMODE(source.stat().st_mode)
            if mode & 0o077:
                raise ValueError("shared OAuth mount source is too broadly accessible")
        targets.add(target)
        result.append({
            "type": "bind",
            "source": str(resolved),
            "target": target,
        })
    return result



def _shared_daemon_endpoint() -> str:
    """Resolve the endpoint used by Docker CLI without starting a container."""
    context = os.environ.get("DOCKER_CONTEXT", "").strip()
    if not context and os.environ.get("DOCKER_HOST", "").strip():
        return os.environ["DOCKER_HOST"].strip()
    docker = shutil.which("docker")
    if not docker:
        raise ValueError("shared OAuth daemon locality is unknown")
    args = [docker, "context", "inspect"]
    if context:
        args.append(context)
    args += ["--format", "{{.Endpoints.docker.Host}}"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("shared OAuth daemon locality is unknown") from None
    endpoint = (result.stdout or "").strip()
    if result.returncode or not endpoint or len(endpoint) > 4096 or "\n" in endpoint:
        raise ValueError("shared OAuth daemon locality is unknown")
    return endpoint


def _require_local_shared_daemon() -> None:
    endpoint = _shared_daemon_endpoint()
    # A network endpoint may represent a remote host even when it uses a
    # loopback tunnel. Never interpret client paths as that daemon's paths.
    if not (endpoint.startswith("unix:///") or endpoint.startswith("npipe:////./pipe/")):
        raise ValueError("shared OAuth requires a local Docker socket; remote daemon needs access-token delivery")

class SharedOAuthDockerEnvironment(DockerEnvironment):
    """Preserve Pier's defaults and append only DRadar OAuth bind mounts."""

    def __init__(self, *args: Any, shared_oauth_mounts_json: object, **kwargs: Any):
        _require_local_shared_daemon()
        super().__init__(*args, **kwargs)
        mounts = _validated_shared_mounts(shared_oauth_mounts_json)
        existing = list(self._mounts_json or [])
        existing_targets = {
            mount.get("target")
            for mount in existing
            if isinstance(mount, dict)
        }
        if existing_targets & {mount["target"] for mount in mounts}:
            raise ValueError("shared OAuth mount conflicts with an existing mount")
        self._mounts_json = [*existing, *mounts]
        self._shared_oauth_targets = tuple(mount["target"] for mount in mounts)

    def _guard_grok_shared_oauth_command(self, command: str) -> str:
        """Keep Grok's atomically replaced auth file host-readable mid-run."""

        if self._shared_oauth_targets != ("/tmp/dradar-grok-user/.grok",):
            return command
        root = shlex.quote(self._shared_oauth_targets[0])
        auth = shlex.quote(self._shared_oauth_targets[0] + "/auth.json")
        guarded = (
            f"oauth_root={root}; oauth_auth={auth}; "
            "oauth_owner=$(stat -c '%u:%g' \"$oauth_root\") || exit 1; "
            "oauth_guard_pid=''; "
            "oauth_repair() { "
            "[ -f \"$oauth_auth\" ] && [ ! -L \"$oauth_auth\" ] || return 0; "
            "oauth_current=$(stat -c '%u:%g' \"$oauth_auth\" 2>/dev/null) "
            "|| return 0; "
            "if [ \"$oauth_current\" != \"$oauth_owner\" ]; then "
            "chown \"$oauth_owner\" \"$oauth_auth\" || return 1; fi; "
            "chmod 600 \"$oauth_auth\"; "
            "}; "
            "oauth_cleanup() { "
            "oauth_status=$?; "
            "if [ -n \"$oauth_guard_pid\" ]; then "
            "kill \"$oauth_guard_pid\" 2>/dev/null || true; "
            "wait \"$oauth_guard_pid\" 2>/dev/null || true; fi; "
            "oauth_repair || true; "
            "exit \"$oauth_status\"; "
            "}; "
            "if [ \"$(id -u)\" = 0 ]; then "
            "(while :; do oauth_repair || true; sleep 0.02; done) & "
            "oauth_guard_pid=$!; fi; "
            "trap oauth_cleanup EXIT; "
            "trap 'exit 130' INT; trap 'exit 143' TERM; "
            + command
        )
        return "bash -o pipefail -c " + shlex.quote(guarded)

    async def _reconcile_shared_oauth_host_ownership(self) -> None:
        """Return root-authored shared OAuth state to the invoking host user.

        Antigravity deliberately runs as root inside the disposable task
        container so its tools and child agents retain the full-permission
        Honey contract. Grok has the same root-authored atomic refresh
        behaviour for ``auth.json``. Their OAuth directories are writable
        host bind mounts, while ``/logs/agent`` is consumed by host-side Pier
        code after the turn. Reconcile only those exact roots after each
        command; never traverse symlinks or another filesystem.
        """

        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        if (
            self._shared_oauth_targets not in {
                ("/tmp/dradar-antigravity-user/.gemini",),
                ("/tmp/dradar-grok-user/.grok",),
            }
            or not callable(getuid)
            or not callable(getgid)
        ):
            return
        owner = f"{getuid()}:{getgid()}"
        operations: list[str] = ["set -eu"]
        for root in ("/logs/agent",):
            quoted = shlex.quote(root)
            operations.extend((
                f"test -d {quoted}",
                f"test ! -L {quoted}",
                f"find -P {quoted} -xdev -exec chown -h -- {owner} {{}} +",
                f"find -P {quoted} -xdev -type d -exec chmod 700 -- {{}} +",
                f"find -P {quoted} -xdev -type f -exec chmod 600 -- {{}} +",
            ))
        oauth_root = self._shared_oauth_targets[0]
        quoted_root = shlex.quote(oauth_root)
        if oauth_root == "/tmp/dradar-antigravity-user/.gemini":
            operations.extend((
                f"test -d {quoted_root}",
                f"test ! -L {quoted_root}",
                f"find -P {quoted_root} -xdev -exec chown -h -- {owner} {{}} +",
                f"find -P {quoted_root} -xdev -type d -exec chmod 700 -- {{}} +",
                f"find -P {quoted_root} -xdev -type f -exec chmod 600 -- {{}} +",
            ))
        else:
            # Grok's bind is its complete native home, not a credentials-only
            # directory. It also contains the managed CLI runtime and session
            # artifacts, so recursively chmod'ing the bind removes the
            # executable bit from runtime/<version>/bin/grok. Reconcile only
            # the two exact OAuth coordination files that root may replace.
            operations.extend((
                f"test -d {quoted_root}",
                f"test ! -L {quoted_root}",
                f"chown -- {owner} {quoted_root}",
                f"chmod 700 -- {quoted_root}",
            ))
            for name in ("auth.json", "auth.json.lock"):
                quoted_file = shlex.quote(f"{oauth_root}/{name}")
                operations.append(
                    f"if test -e {quoted_file}; then "
                    f"test -f {quoted_file}; test ! -L {quoted_file}; "
                    f"chown -- {owner} {quoted_file}; "
                    f"chmod 600 -- {quoted_file}; fi"
                )
        result = await super().exec(
            command="; ".join(operations), user="root", timeout_sec=120,
        )
        if getattr(result, "return_code", 1) != 0:
            raise RuntimeError("failed to reconcile shared OAuth host ownership")

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> Any:
        """Preserve root-in-container execution without poisoning host binds."""

        try:
            result = await super().exec(
                command=self._guard_grok_shared_oauth_command(command),
                cwd=cwd,
                env=env,
                timeout_sec=timeout_sec,
                user=user,
            )
        except BaseException:
            try:
                await self._reconcile_shared_oauth_host_ownership()
            except BaseException:
                # Never hide the original provider/container exception.
                pass
            raise
        try:
            await self._reconcile_shared_oauth_host_ownership()
        except BaseException:
            if getattr(result, "return_code", 1) != 0:
                # Let BaseInstalledAgent report the original provider command
                # and return code rather than replacing it with cleanup noise.
                return result
            raise
        return result
