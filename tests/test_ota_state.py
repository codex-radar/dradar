import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dradar.ota.manifest import verify_signed_manifest
from dradar.ota.state import (
    InvalidTransition,
    SafePointSnapshot,
    UpdateController,
    UpdateLockBusy,
    UpdateState,
    _atomic_json,
)


def _release(tmp_path):
    body = b"candidate"
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    document = {
        "schema_version": 1,
        "release_id": "dradar-0.6.0",
        "version": "0.6.0",
        "sequence": 600,
        "channel": "stable",
        "published_at": "2026-09-02T05:00:00Z",
        "rollout": {
            "stage": "general",
            "basis_points": 10_000,
            "salt": "s",
            "paused": False,
        },
        "compatibility": {
            "launcher_min_version": "1.0.0",
            "runner_protocol": {"min": 3, "max": 3},
            "doctor_contract": 1,
            "provider_contract": 1,
            "ledger_schema": {"min": 1, "max": 1},
            "checkpoint_schema": {"min": 0, "max": 0},
        },
        "artifacts": [
            {
                "os": "linux",
                "arch": "x86_64",
                "filename": "candidate.whl",
                "url": "https://releases.example.invalid/candidate.whl",
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        ],
    }
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    document["signature"] = {
        "algorithm": "ed25519",
        "key_id": "root",
        "value": base64.b64encode(private.sign(payload)).decode(),
    }
    manifest = verify_signed_manifest(document, {"root": public})
    downloaded = tmp_path / "downloaded.whl"
    downloaded.write_bytes(body)
    return manifest, manifest.artifacts[0], downloaded


def _prepare_waiting(controller, manifest, artifact, downloaded):
    controller.detect(manifest, artifact)
    controller.transition(UpdateState.DOWNLOADED)
    controller.transition(UpdateState.VERIFIED)
    controller.stage(manifest, artifact, downloaded)
    controller.wait_for_safe_point()


def test_safe_point_requires_natural_worker_and_upload_quiescence():
    blocked = SafePointSnapshot(
        active_assignments=2,
        durable_uploads_pending=1,
        refill_accepting_new=True,
        worker_supervisor_idle=False,
    )
    assert blocked.ready is False
    assert blocked.blockers() == (
        "active_assignments",
        "durable_uploads_pending",
        "refill_accepting_new",
        "worker_supervisor_not_idle",
    )
    assert SafePointSnapshot().ready is True


def test_pause_and_resume_preserve_exact_pre_activation_state(tmp_path):
    manifest, artifact, _downloaded = _release(tmp_path)
    controller = UpdateController(tmp_path / "ota")
    with controller.transaction():
        controller.detect(manifest, artifact)
        controller.transition(UpdateState.DOWNLOADED)
        assert (
            controller.pause("operator paused rollout")["resume_state"] == "downloaded"
        )
        assert controller.resume()["state"] == "downloaded"


def test_activation_is_blocked_until_every_worker_reaches_safe_point(tmp_path):
    manifest, artifact, downloaded = _release(tmp_path)
    controller = UpdateController(tmp_path / "ota")
    _atomic_json(
        controller.current_path,
        {
            "release_id": "dradar-0.5.175",
            "version": "0.5.175",
            "sequence": 599,
            "artifact": "releases/dradar-0.5.175/current.whl",
        },
    )
    with controller.transaction():
        _prepare_waiting(controller, manifest, artifact, downloaded)

        with pytest.raises(InvalidTransition, match="active_assignments"):
            controller.activate(SafePointSnapshot(active_assignments=1))

        assert controller.activate(SafePointSnapshot())["state"] == "activated"
        assert controller.begin_self_test()["state"] == "self_testing"
        assert controller.commit()["state"] == "committed"
    assert (
        controller.current_path.read_text()
        == controller.last_known_good_path.read_text()
    )
    assert not controller.pending_path.exists()


def test_failed_candidate_rolls_back_to_previous_pointer(tmp_path):
    manifest, artifact, downloaded = _release(tmp_path)
    controller = UpdateController(tmp_path / "ota")
    previous = {
        "release_id": "dradar-0.5.175",
        "version": "0.5.175",
        "sequence": 599,
        "artifact": "releases/dradar-0.5.175/current.whl",
    }
    _atomic_json(controller.current_path, previous)
    _atomic_json(controller.last_known_good_path, previous)
    with controller.transaction():
        _prepare_waiting(controller, manifest, artifact, downloaded)
        controller.activate(SafePointSnapshot())
        controller.begin_self_test()
        controller.request_rollback("doctor compatibility failed")

        assert controller.rollback()["state"] == "rolled_back"
    assert json.loads(controller.current_path.read_text()) == previous
    assert json.loads(controller.last_known_good_path.read_text()) == previous


def test_launcher_recovers_crash_during_uncommitted_activation(tmp_path):
    manifest, artifact, downloaded = _release(tmp_path)
    controller = UpdateController(tmp_path / "ota")
    previous = {
        "release_id": "dradar-0.5.175",
        "version": "0.5.175",
        "sequence": 599,
        "artifact": "releases/dradar-0.5.175/current.whl",
    }
    _atomic_json(controller.current_path, previous)
    with controller.transaction():
        _prepare_waiting(controller, manifest, artifact, downloaded)
        controller.activate(SafePointSnapshot())

    restarted_launcher = UpdateController(controller.root)
    with restarted_launcher.transaction():
        assert restarted_launcher.recover_on_launcher_start() is True
    assert json.loads(controller.current_path.read_text()) == previous
    assert controller.state()["state"] == "rolled_back"


def test_launcher_recovers_crash_before_current_pointer_switch(tmp_path):
    manifest, artifact, downloaded = _release(tmp_path)
    controller = UpdateController(tmp_path / "ota")
    previous = {
        "release_id": "dradar-0.5.175",
        "version": "0.5.175",
        "sequence": 599,
        "artifact": "releases/dradar-0.5.175/current.whl",
    }
    _atomic_json(controller.current_path, previous)
    with controller.transaction():
        _prepare_waiting(controller, manifest, artifact, downloaded)
        pending = json.loads(controller.pending_path.read_text())
        pending["activation_attempted"] = True
        _atomic_json(controller.pending_path, pending)

    restarted_launcher = UpdateController(controller.root)
    with restarted_launcher.transaction():
        assert restarted_launcher.recover_on_launcher_start() is True
    assert json.loads(controller.current_path.read_text()) == previous
    assert controller.state()["state"] == "rolled_back"


def test_launcher_finishes_lkg_commit_after_crash(tmp_path):
    manifest, artifact, downloaded = _release(tmp_path)
    controller = UpdateController(tmp_path / "ota")
    previous = {
        "release_id": "dradar-0.5.175",
        "version": "0.5.175",
        "sequence": 599,
        "artifact": "releases/dradar-0.5.175/current.whl",
    }
    _atomic_json(controller.current_path, previous)
    _atomic_json(controller.last_known_good_path, previous)
    with controller.transaction():
        _prepare_waiting(controller, manifest, artifact, downloaded)
        controller.activate(SafePointSnapshot())
        controller.begin_self_test()
        # Simulate a crash after the durable committed state but before commit()
        # advances LKG and clears pending.
        controller.transition(UpdateState.COMMITTED)

    restarted_launcher = UpdateController(controller.root)
    with restarted_launcher.transaction():
        assert restarted_launcher.recover_on_launcher_start() is False
    assert json.loads(controller.current_path.read_text()) == json.loads(
        controller.last_known_good_path.read_text()
    )
    assert not controller.pending_path.exists()


def test_host_update_lock_is_exclusive(tmp_path):
    controller = UpdateController(tmp_path / "ota")
    with controller.lock(), pytest.raises(UpdateLockBusy), controller.lock():
        pass


def test_illegal_force_activation_is_rejected(tmp_path):
    manifest, artifact, _downloaded = _release(tmp_path)
    controller = UpdateController(tmp_path / "ota")
    with controller.transaction():
        controller.detect(manifest, artifact)
        with pytest.raises(InvalidTransition, match="no staged update"):
            controller.activate(SafePointSnapshot())


def test_state_mutation_without_host_transaction_is_rejected(tmp_path):
    manifest, artifact, _downloaded = _release(tmp_path)
    controller = UpdateController(tmp_path / "ota")

    with pytest.raises(UpdateLockBusy, match="requires"):
        controller.detect(manifest, artifact)


def test_safe_point_rejects_negative_or_boolean_counters():
    with pytest.raises(ValueError, match="active_assignments"):
        SafePointSnapshot(active_assignments=-1)
    with pytest.raises(ValueError, match="uploads_inflight"):
        SafePointSnapshot(uploads_inflight=True)


def test_launcher_rejects_bad_current_pointer_and_uses_valid_lkg(tmp_path):
    controller = UpdateController(tmp_path / "ota")
    lkg_artifact = controller.root / "releases" / "dradar-0.5.175" / "current.whl"
    lkg_artifact.parent.mkdir(parents=True)
    lkg_artifact.write_bytes(b"known-good")
    _atomic_json(
        controller.current_path,
        {
            "release_id": "malicious",
            "version": "9.9.9",
            "sequence": 999,
            "artifact": "../../outside.whl",
        },
    )
    lkg = {
        "release_id": "dradar-0.5.175",
        "version": "0.5.175",
        "sequence": 599,
        "artifact": "releases/dradar-0.5.175/current.whl",
    }
    _atomic_json(controller.last_known_good_path, lkg)

    assert controller.launch_pointer() == lkg
