import json
from types import SimpleNamespace

import pytest

from dradar.ota.integration import (
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
