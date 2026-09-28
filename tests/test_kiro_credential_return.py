"""Kiro's private native credential return fails closed without a live CLI."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from dradar.pier_kiro import KiroOpus55


def _token(refresh: str, expires: str = "2099-01-01T00:00:00Z") -> dict[str, str]:
    return {
        "access_token": "synthetic-access",
        "refresh_token": refresh,
        "expires_at": expires,
        "provider": "github",
        "profile_arn": "arn:aws:codewhisperer:us-east-1:123456789012:profile/test",
    }


def _agent(source: Path, return_code: int):
    calls = []

    async def exec_as_agent(*_args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(return_code=return_code)

    return SimpleNamespace(_auth_file=source,
                           _return_marker=KiroOpus55._return_marker,
                           exec_as_agent=exec_as_agent), calls


def _start(agent) -> Path:
    return KiroOpus55._begin_credential_return(agent)


def _finish(agent, environment, marker: Path) -> None:
    asyncio.run(KiroOpus55._return_credential(
        agent, environment, {}, "/private-home", "/private-auth/token.json", marker))


def test_export_nonzero_preserves_snapshot_and_blocks_download(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(_token("original")))
    agent, commands = _agent(source, 1)
    marker = _start(agent)

    class Environment:
        async def download_file(self, *_args):
            raise AssertionError("download must not follow failed export")

    with pytest.raises(RuntimeError, match="export failed"):
        _finish(agent, Environment(), marker)
    assert len(commands) == 1
    assert json.loads(source.read_text())["refresh_token"] == "original"
    assert marker.is_file() and os.stat(marker).st_mode & 0o077 == 0


def test_download_failure_retains_partial_copy_without_overwriting_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(_token("original")))
    agent, _ = _agent(source, 0)
    marker = _start(agent)

    class Environment:
        async def download_file(self, _remote, target):
            Path(target).write_text(json.dumps(_token("partial-new")))
            raise OSError("synthetic transfer failure")

    with pytest.raises(RuntimeError, match="credential return failed"):
        _finish(agent, Environment(), marker)
    assert json.loads(source.read_text())["refresh_token"] == "original"
    assert marker.exists()
    recovered = list(tmp_path.glob("snapshot.json.returned-*"))
    assert len(recovered) == 1
    assert os.stat(recovered[0]).st_mode & 0o077 == 0


def test_successful_download_atomically_replaces_snapshot_and_clears_marker(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(_token("original")))
    agent, _ = _agent(source, 0)
    marker = _start(agent)

    class Environment:
        async def download_file(self, _remote, target):
            Path(target).write_text(json.dumps(_token(
                "new-refresh", "2099-01-02T00:00:00Z")))

    _finish(agent, Environment(), marker)
    assert json.loads(source.read_text())["refresh_token"] == "new-refresh"
    assert os.stat(source).st_mode & 0o077 == 0
    assert not marker.exists()
    assert not list(tmp_path.glob("snapshot.json.returned-*"))


def test_returned_identity_mismatch_keeps_original_and_marks_recovery(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(_token("original")))
    agent, _ = _agent(source, 0)
    marker = _start(agent)

    class Environment:
        async def download_file(self, _remote, target):
            wrong = _token("new-refresh")
            wrong["profile_arn"] = "arn:aws:codewhisperer:other"
            Path(target).write_text(json.dumps(wrong))

    with pytest.raises(RuntimeError, match="credential return failed"):
        _finish(agent, Environment(), marker)
    assert json.loads(source.read_text())["refresh_token"] == "original"
    assert marker.exists()


def test_rotated_token_without_newer_expiry_is_not_published(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(_token("original")))
    agent, _ = _agent(source, 0)
    marker = _start(agent)

    class Environment:
        async def download_file(self, _remote, target):
            Path(target).write_text(json.dumps(_token("rotated-with-stale-expiry")))

    with pytest.raises(RuntimeError, match="credential return failed"):
        _finish(agent, Environment(), marker)
    assert json.loads(source.read_text())["refresh_token"] == "original"
    assert marker.exists()
