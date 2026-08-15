"""Security and lifecycle tests for opt-in local session archives."""

import argparse
import os
import stat
from pathlib import Path

import pytest

from dradar import session_archive


def _write_session(
    trial: Path, relative: str = "2026/08/16/rollout.jsonl", content: str = "{}\n",
) -> Path:
    path = trial / "agent" / "sessions" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_archive_preserves_relative_layout_and_private_permissions(tmp_path: Path):
    home = tmp_path / "home"
    trial = tmp_path / "trial"
    _write_session(trial)

    result = session_archive.archive_codex_sessions(home, trial, "assignment-1")

    assert result is not None
    copied, destination = result
    archived = destination / "2026" / "08" / "16" / "rollout.jsonl"
    assert copied == 1
    assert archived.read_text() == "{}\n"
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o700
        assert stat.S_IMODE(archived.stat().st_mode) == 0o600


def test_archive_is_idempotent_and_preserves_different_colliding_content(
    tmp_path: Path,
):
    home = tmp_path / "home"
    trial = tmp_path / "trial"
    source = _write_session(trial, content="first\n")
    first = session_archive.archive_codex_sessions(home, trial, "a1")
    repeated = session_archive.archive_codex_sessions(home, trial, "a1")
    source.write_text("second\n")
    changed = session_archive.archive_codex_sessions(home, trial, "a1")

    assert first is not None and first[0] == 1
    assert repeated is not None and repeated[0] == 0
    assert changed is not None and changed[0] == 1
    files = sorted(first[1].rglob("*.jsonl"))
    assert len(files) == 2
    assert {path.read_text() for path in files} == {"first\n", "second\n"}


def test_archive_rejects_symlink_sources_and_destination_components(tmp_path: Path):
    home = tmp_path / "home"
    trial = tmp_path / "trial"
    sessions = trial / "agent" / "sessions"
    sessions.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("secret\n")
    (sessions / "linked.jsonl").symlink_to(outside)

    assert session_archive.archive_codex_sessions(home, trial, "a1") is None

    _write_session(trial, "real.jsonl")
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (home / "history").mkdir(parents=True)
    (home / "history" / "codex-sessions").symlink_to(redirected)
    with pytest.raises(OSError, match="not a real directory"):
        session_archive.archive_codex_sessions(home, trial, "a1")
    assert list(redirected.iterdir()) == []


def test_assignment_component_is_ascii_bounded_and_collision_resistant():
    safe = session_archive.safe_assignment_component("ok-id_1")
    first = session_archive.safe_assignment_component("../same")
    second = session_archive.safe_assignment_component("..\\same")
    long = session_archive.safe_assignment_component("a" * 200)

    assert safe == "ok-id_1"
    assert first != second
    assert "/" not in first and "\\" not in second
    assert len(long) <= 93


def test_prune_is_dry_run_by_default_and_requires_real_archive_path(
    tmp_path: Path, monkeypatch, capsys,
):
    home = tmp_path / "home"
    trial = tmp_path / "trial"
    _write_session(trial)
    session_archive.archive_codex_sessions(home, trial, "a1")
    monkeypatch.setattr(session_archive, "HOME", home)

    assert session_archive.cmd_sessions_prune(argparse.Namespace(yes=False)) == 0
    assert (home / session_archive.ARCHIVE_RELATIVE / "a1").is_dir()
    assert "pass --yes" in capsys.readouterr().out

    assert session_archive.cmd_sessions_prune(argparse.Namespace(yes=True)) == 0
    assert not (home / session_archive.ARCHIVE_RELATIVE).exists()


def test_prune_refuses_symlinked_archive_root(tmp_path: Path, monkeypatch, capsys):
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    (home / "history").mkdir(parents=True)
    (home / "history" / "codex-sessions").symlink_to(target)
    monkeypatch.setattr(session_archive, "HOME", home)

    assert session_archive.cmd_sessions_prune(argparse.Namespace(yes=True)) == 1
    assert (target / "keep.txt").is_file()
    assert "refusing" in capsys.readouterr().out
