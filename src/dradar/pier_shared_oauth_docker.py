"""Pier Docker environment with narrowly scoped shared OAuth mounts.

The paid subscription CLIs rotate refresh tokens while they run.  Multiple
independent task containers therefore need to see the same credential store
and the provider's own cross-process lock, while Pier's ordinary log mounts
and every task workspace remain isolated.

This module is copied into the per-run Pier import directory.  It deliberately
accepts only the two credential targets used by DRadar's Kimi and Grok
adapters; arbitrary host mounts are rejected.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from pier.environments.docker.docker import DockerEnvironment


_ALLOWED_TARGETS = frozenset({
    "/tmp/dradar-kimi-home/credentials",
    "/tmp/dradar-kimi-home/oauth",
    "/tmp/dradar-grok-user/.grok",
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


class SharedOAuthDockerEnvironment(DockerEnvironment):
    """Preserve Pier's defaults and append only DRadar OAuth bind mounts."""

    def __init__(self, *args: Any, shared_oauth_mounts_json: object, **kwargs: Any):
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
