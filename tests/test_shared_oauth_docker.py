"""Security contract for the narrow shared OAuth Docker environment."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest


class _DockerEnvironmentStub:
    pass


# Pier is intentionally installed only inside the ephemeral task runtime, not
# as a DRadar package dependency.  Supply the single class this copied module
# subclasses so its validation contract remains unit-testable here.
_pier_modules = {
    name: types.ModuleType(name)
    for name in (
        "pier",
        "pier.environments",
        "pier.environments.docker",
        "pier.environments.docker.docker",
    )
}
_pier_modules["pier.environments.docker.docker"].DockerEnvironment = (
    _DockerEnvironmentStub
)
_previous_modules = {name: sys.modules.get(name) for name in _pier_modules}
sys.modules.update(_pier_modules)
try:
    from dradar.pier_shared_oauth_docker import (
        SharedOAuthDockerEnvironment,
        _validated_shared_mounts,
    )
finally:
    for _name, _previous in _previous_modules.items():
        if _previous is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _previous


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path.resolve()


def test_validated_shared_mounts_accept_only_private_managed_targets(
    tmp_path: Path,
) -> None:
    source = _private_dir(tmp_path / "oauth")
    assert _validated_shared_mounts([
        {
            "type": "bind",
            "source": str(source),
            "target": "/tmp/dradar-kimi-home/oauth",
        }
    ]) == [
        {
            "type": "bind",
            "source": str(source),
            "target": "/tmp/dradar-kimi-home/oauth",
        }
    ]

    with pytest.raises(ValueError, match="target is not allowed"):
        _validated_shared_mounts([
            {"type": "bind", "source": str(source), "target": "/app"}
        ])


def test_validated_shared_mounts_reject_symlink_and_broad_permissions(
    tmp_path: Path,
) -> None:
    source = _private_dir(tmp_path / "oauth")
    symlink = tmp_path / "oauth-link"
    symlink.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="existing directory"):
        _validated_shared_mounts([
            {
                "type": "bind",
                "source": str(symlink),
                "target": "/tmp/dradar-kimi-home/oauth",
            }
        ])

    if os.name != "nt":
        source.chmod(0o755)
        with pytest.raises(ValueError, match="too broadly accessible"):
            _validated_shared_mounts([
                {
                    "type": "bind",
                    "source": str(source),
                    "target": "/tmp/dradar-kimi-home/oauth",
                }
            ])


def test_environment_preserves_pier_default_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _private_dir(tmp_path / "grok")

    def fake_init(self, *args, **kwargs):  # noqa: ANN001, ANN202, ARG001
        self._mounts_json = [
            {"type": "bind", "source": "/host/logs", "target": "/logs/agent"}
        ]

    monkeypatch.setattr(
        "dradar.pier_shared_oauth_docker.DockerEnvironment.__init__", fake_init
    )
    environment = SharedOAuthDockerEnvironment(
        shared_oauth_mounts_json=[
            {
                "type": "bind",
                "source": str(source),
                "target": "/tmp/dradar-grok-user/.grok",
            }
        ]
    )
    assert [mount["target"] for mount in environment._mounts_json] == [
        "/logs/agent",
        "/tmp/dradar-grok-user/.grok",
    ]
