import inspect
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from dradar.ota.integration import (
    _cleanup_windows_candidate,
    _locked_windows_candidate,
    _run_windows_candidate,
    cmd_update_doctor,
    cmd_update_status,
    diagnose_update,
    load_trusted_keys,
    runloop_safe_point,
    store_trusted_keys,
    update_status,
)


def test_legacy_status_is_read_only_and_preserves_installed_client(tmp_path, capsys):
    home = tmp_path / "new-home"
    status = update_status(home)
    assert status["state"] == "legacy"
    assert status["pending"] is False
    assert not home.exists()
    healthy, notes = diagnose_update(home)
    assert healthy is True
    assert notes == (
        "legacy client has no signed OTA baseline; current version is preserved",
    )
    assert not home.exists()


def test_safe_point_blocks_forty_workers_refill_and_durable_upload(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "pending_uploads.json").write_text(json.dumps([{"id": "durable"}]))
    snapshot = runloop_safe_point(
        home=home,
        active_assignments=40,
        checkouts_inflight=1,
        refill_accepting_new=True,
        worker_supervisor_idle=False,
    )
    assert snapshot.ready is False
    assert set(snapshot.blockers()) == {
        "active_assignments",
        "checkouts_inflight",
        "durable_uploads_pending",
        "refill_accepting_new",
        "worker_supervisor_not_idle",
    }


def test_corrupt_pending_ledger_fails_safe_point_closed(tmp_path):
    (tmp_path / "pending_uploads.json").write_text("{")
    snapshot = runloop_safe_point(home=tmp_path)
    assert snapshot.durable_uploads_pending == 1
    assert snapshot.ready is False


def test_trusted_key_round_trip_is_private(tmp_path):
    key = bytes(range(32))
    store_trusted_keys({"release-root": key}, tmp_path)
    path = tmp_path / "ota" / "trusted-keys.json"
    assert load_trusted_keys(tmp_path) == {"release-root": key}
    if path.stat().st_mode & 0o077:
        pytest.fail("trusted key file is not private")


def test_update_commands_render_status(monkeypatch, tmp_path, capsys):
    from dradar.ota import integration

    monkeypatch.setattr(integration, "HOME", tmp_path)
    assert cmd_update_status(SimpleNamespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "legacy"
    assert cmd_update_doctor(SimpleNamespace()) == 0
    assert "OTA diagnostics: PASS" in capsys.readouterr().out


def test_malformed_or_symlinked_state_is_not_reported_as_legacy(tmp_path):
    root = tmp_path / "ota"
    root.mkdir(mode=0o700)
    state = root / "update-state.json"
    state.write_text("{")
    assert update_status(tmp_path)["state"] == "invalid"
    healthy, notes = diagnose_update(tmp_path)
    assert healthy is False
    assert "persisted OTA state is invalid" in notes

    state.unlink()
    target = tmp_path / "outside.json"
    target.write_text("{}")
    state.symlink_to(target)
    assert update_status(tmp_path)["state"] == "invalid"
    assert diagnose_update(tmp_path)[0] is False


def test_windows_candidate_handle_lives_through_child_and_then_cleans(tmp_path):
    events = []
    candidate = tmp_path / "candidate.pyz"

    @contextmanager
    def locked(data):
        candidate.write_bytes(data)
        events.append("handle-open")
        try:
            yield candidate
        finally:
            events.append("handle-close")
            candidate.unlink()

    def runner(command, *, check):
        assert check is False
        assert events == ["handle-open"]
        assert command[1] == str(candidate)
        assert candidate.read_bytes() == b"verified candidate"
        events.append("child-finished")
        return SimpleNamespace(returncode=0)

    assert (
        _run_windows_candidate(
            b"verified candidate",
            ["--version"],
            locked_candidate=locked,
            runner=runner,
        )
        == 0
    )
    assert events == ["handle-open", "child-finished", "handle-close"]
    assert not candidate.exists()
    source = inspect.getsource(_locked_windows_candidate)
    assert "FILE_SHARE_READ: deny writers, delete and rename" in source
    assert "NamedTemporaryFile" not in source


def test_windows_cleanup_failure_cannot_change_candidate_result(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.pyz"
    candidate.write_bytes(b"candidate")
    scheduled = []

    def denied(*args, **kwargs):
        raise PermissionError("held by scanner")

    monkeypatch.setattr(candidate.__class__, "unlink", denied)
    assert (
        _cleanup_windows_candidate(
            candidate,
            lambda path, replacement, flags: scheduled.append(
                (path, replacement, flags)
            ),
        )
        is False
    )
    assert scheduled == [(str(candidate), None, 0x00000004)]


def test_releases_symlink_is_diagnosed_and_never_legacy_healthy(tmp_path):
    root = tmp_path / "ota"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "releases").symlink_to(outside, target_is_directory=True)
    healthy, notes = diagnose_update(tmp_path)
    assert healthy is False
    assert "OTA releases directory is a symlink" in notes
