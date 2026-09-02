import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import dradar.ota.state as state_module
from dradar.flight_recorder import FlightRecorder
from dradar.ota.manifest import verify_signed_manifest
from dradar.ota.runtime import FlightRecorderEventSink
from dradar.ota.state import (
    InvalidTransition,
    SafePointSnapshot,
    UpdateController,
    UpdateLockBusy,
    UpdateState,
    _atomic_json,
)


def _release(
    tmp_path,
    *,
    with_keys=False,
    release_id="dradar-0.6.0",
    version="0.6.0",
    sequence=600,
    filename="candidate.whl",
    key_id="root",
):
    body = f"candidate-{sequence}".encode()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    document = {
        "schema_version": 1,
        "release_id": release_id,
        "version": version,
        "sequence": sequence,
        "channel": "stable",
        "published_at": "2026-09-02T05:00:00Z",
        "expires_at": "2099-09-02T05:00:00Z",
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
                "filename": filename,
                "url": f"https://releases.example.invalid/{filename}",
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        ],
    }
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    document["signature"] = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "value": base64.b64encode(private.sign(payload)).decode(),
    }
    manifest = verify_signed_manifest(document, {key_id: public})
    downloaded = tmp_path / f"downloaded-{sequence}.whl"
    downloaded.write_bytes(body)
    result = (manifest, manifest.artifacts[0], downloaded)
    if with_keys:
        return (*result, {key_id: public})
    return result


def _prepare_waiting(controller, manifest, artifact, downloaded):
    controller.detect(manifest, artifact)
    controller.transition(UpdateState.DOWNLOADED)
    controller.transition(UpdateState.VERIFIED)
    controller.stage(manifest, artifact, downloaded)
    controller.wait_for_safe_point()


def _install_signed_record(controller, manifest, artifact, body):
    destination = controller.root / "releases" / manifest.release_id / artifact.filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)
    pointer = {
        "release_id": manifest.release_id,
        "version": manifest.version,
        "sequence": manifest.sequence,
        "artifact": str(destination.relative_to(controller.root)),
    }
    _atomic_json(
        destination.parent / "release-record.json",
        {
            "schema_version": 1,
            "committed": True,
            "pointer": pointer,
            "manifest": json.loads(manifest.signed_document),
        },
    )
    return pointer


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
    manifest, artifact, downloaded, keys = _release(tmp_path, with_keys=True)
    controller = UpdateController(tmp_path / "ota", trusted_keys=keys)
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
    manifest, artifact, downloaded, keys = _release(tmp_path, with_keys=True)
    controller = UpdateController(tmp_path / "ota", trusted_keys=keys)
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

    restarted_launcher = UpdateController(controller.root, trusted_keys=keys)
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
    manifest, artifact, downloaded, keys = _release(tmp_path, with_keys=True)
    controller = UpdateController(tmp_path / "ota", trusted_keys=keys)
    _atomic_json(
        controller.current_path,
        {
            "release_id": "malicious",
            "version": "9.9.9",
            "sequence": 999,
            "artifact": "../../outside.whl",
        },
    )
    lkg = _install_signed_record(
        controller, manifest, artifact, downloaded.read_bytes()
    )
    _atomic_json(controller.last_known_good_path, lkg)

    assert controller.launch_pointer() == lkg


def test_launcher_rejects_symlink_current_even_when_target_stays_inside_root(tmp_path):
    manifest, artifact, downloaded, keys = _release(tmp_path, with_keys=True)
    controller = UpdateController(tmp_path / "ota", trusted_keys=keys)
    actual = controller.root / "releases" / "candidate" / "actual.whl"
    actual.parent.mkdir(parents=True)
    actual.write_bytes(b"candidate")
    linked = actual.with_name("linked.whl")
    linked.symlink_to(actual.name)
    current = {
        "release_id": "candidate",
        "version": "0.6.0",
        "sequence": 600,
        "artifact": str(linked.relative_to(controller.root)),
    }
    _atomic_json(controller.current_path, current)

    lkg = _install_signed_record(
        controller, manifest, artifact, downloaded.read_bytes()
    )
    _atomic_json(controller.last_known_good_path, lkg)

    assert controller.launch_pointer() == lkg


def test_launcher_reverifies_committed_artifact_and_rejects_post_commit_tamper(
    tmp_path,
):
    manifest, artifact, downloaded, keys = _release(tmp_path, with_keys=True)
    controller = UpdateController(tmp_path / "ota", trusted_keys=keys)
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
        controller.begin_self_test()
        controller.commit()

    candidate = controller.root / controller.launch_pointer()["artifact"]
    candidate.write_bytes(b"tampered!")
    with pytest.raises(InvalidTransition, match="no current or last-known-good"):
        controller.launch_pointer()


def test_launcher_never_accepts_an_arbitrary_regular_file_without_signed_record(
    tmp_path,
):
    _manifest, _artifact, _downloaded, keys = _release(tmp_path, with_keys=True)
    controller = UpdateController(tmp_path / "ota", trusted_keys=keys)
    arbitrary = controller.root / "releases" / "forged" / "client.whl"
    arbitrary.parent.mkdir(parents=True)
    arbitrary.write_bytes(b"arbitrary executable")
    _atomic_json(
        controller.current_path,
        {
            "release_id": "forged",
            "version": "99.0.0",
            "sequence": 999,
            "artifact": str(arbitrary.relative_to(controller.root)),
        },
    )

    with pytest.raises(InvalidTransition, match="no current or last-known-good"):
        controller.launch_pointer()


def test_launcher_selects_highest_valid_sequence_across_current_and_lkg(tmp_path):
    older_manifest, older_artifact, older_downloaded, older_keys = _release(
        tmp_path,
        with_keys=True,
        release_id="dradar-0.6.0",
        version="0.6.0",
        sequence=600,
        filename="older.whl",
        key_id="older-root",
    )
    newer_manifest, newer_artifact, newer_downloaded, newer_keys = _release(
        tmp_path,
        with_keys=True,
        release_id="dradar-0.7.0",
        version="0.7.0",
        sequence=601,
        filename="newer.whl",
        key_id="newer-root",
    )
    controller = UpdateController(
        tmp_path / "ota",
        trusted_keys={**older_keys, **newer_keys},
    )
    older = _install_signed_record(
        controller,
        older_manifest,
        older_artifact,
        older_downloaded.read_bytes(),
    )
    newest = _install_signed_record(
        controller,
        newer_manifest,
        newer_artifact,
        newer_downloaded.read_bytes(),
    )
    _atomic_json(controller.current_path, older)
    _atomic_json(controller.last_known_good_path, newest)

    assert controller.launch_pointer() == newest


def test_committed_pointer_rejects_forged_unsigned_lkg_baseline(tmp_path):
    _manifest, _artifact, _downloaded, keys = _release(tmp_path, with_keys=True)
    controller = UpdateController(tmp_path / "ota", trusted_keys=keys)
    forged = controller.root / "releases" / "forged" / "client.whl"
    forged.parent.mkdir(parents=True)
    forged.write_bytes(b"forged")
    _atomic_json(
        controller.last_known_good_path,
        {
            "release_id": "forged",
            "version": "99.0.0",
            "sequence": 999,
            "artifact": str(forged.relative_to(controller.root)),
        },
    )

    with pytest.raises(InvalidTransition, match="trusted committed OTA baseline"):
        controller.committed_pointer()


def test_committed_pointer_rejects_conflicting_highest_sequence(tmp_path):
    left_manifest, left_artifact, left_downloaded, left_keys = _release(
        tmp_path,
        with_keys=True,
        release_id="left-release",
        version="0.6.0",
        sequence=600,
        filename="left.whl",
        key_id="left-root",
    )
    right_manifest, right_artifact, right_downloaded, right_keys = _release(
        tmp_path,
        with_keys=True,
        release_id="right-release",
        version="0.6.1",
        sequence=600,
        filename="right.whl",
        key_id="right-root",
    )
    controller = UpdateController(
        tmp_path / "ota",
        trusted_keys={**left_keys, **right_keys},
    )
    left = _install_signed_record(
        controller, left_manifest, left_artifact, left_downloaded.read_bytes()
    )
    right = _install_signed_record(
        controller, right_manifest, right_artifact, right_downloaded.read_bytes()
    )
    _atomic_json(controller.current_path, left)
    _atomic_json(controller.last_known_good_path, right)

    with pytest.raises(InvalidTransition, match="conflicting committed OTA pointers"):
        controller.committed_pointer()


def test_stage_rejects_release_parent_symlink_and_never_writes_outside(tmp_path):
    manifest, artifact, downloaded = _release(tmp_path)
    controller = UpdateController(tmp_path / "ota")
    previous = {
        "release_id": "dradar-0.5.175",
        "version": "0.5.175",
        "sequence": 599,
        "artifact": "releases/dradar-0.5.175/current.whl",
    }
    _atomic_json(controller.current_path, previous)
    outside = tmp_path / "outside"
    outside.mkdir()
    controller.releases.mkdir(parents=True)
    (controller.releases / manifest.release_id).symlink_to(
        outside,
        target_is_directory=True,
    )
    with controller.transaction():
        controller.detect(manifest, artifact)
        controller.transition(UpdateState.DOWNLOADED)
        controller.transition(UpdateState.VERIFIED)
        with pytest.raises(InvalidTransition, match="safe release directory"):
            controller.stage(manifest, artifact, downloaded)

    assert not (outside / artifact.filename).exists()


def test_stage_rejects_release_directory_swap_after_initial_check(
    tmp_path, monkeypatch
):
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
    outside = tmp_path / "outside"
    outside.mkdir()
    original = state_module._safe_directory_beneath
    raced = False

    def replace_after_check(root, release_dir):
        nonlocal raced
        result = original(root, release_dir)
        if result and not raced:
            raced = True
            release_dir.rename(tmp_path / "detached-release")
            release_dir.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(state_module, "_safe_directory_beneath", replace_after_check)
    with controller.transaction():
        controller.detect(manifest, artifact)
        controller.transition(UpdateState.DOWNLOADED)
        controller.transition(UpdateState.VERIFIED)
        with pytest.raises(InvalidTransition, match="safe release directory"):
            controller.stage(manifest, artifact, downloaded)

    assert not (outside / artifact.filename).exists()


def test_stage_rejects_preexisting_external_hardlink_candidate(tmp_path):
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
    outside = tmp_path / "outside.whl"
    outside.write_bytes(downloaded.read_bytes())
    release_dir = controller.releases / manifest.release_id
    release_dir.mkdir(parents=True)
    (release_dir / artifact.filename).hardlink_to(outside)

    with controller.transaction():
        controller.detect(manifest, artifact)
        controller.transition(UpdateState.DOWNLOADED)
        controller.transition(UpdateState.VERIFIED)
        with pytest.raises(InvalidTransition, match="safe release directory"):
            controller.stage(manifest, artifact, downloaded)

    assert outside.read_bytes() == downloaded.read_bytes()


def test_launcher_rejects_symlinked_release_record(tmp_path):
    manifest, artifact, downloaded, keys = _release(tmp_path, with_keys=True)
    controller = UpdateController(tmp_path / "ota", trusted_keys=keys)
    pointer = _install_signed_record(
        controller,
        manifest,
        artifact,
        downloaded.read_bytes(),
    )
    _atomic_json(controller.current_path, pointer)
    record = controller.root / "releases" / manifest.release_id / "release-record.json"
    outside = tmp_path / "outside-record.json"
    outside.write_bytes(record.read_bytes())
    record.unlink()
    record.symlink_to(outside)

    with pytest.raises(InvalidTransition, match="no current or last-known-good"):
        controller.launch_pointer()


@pytest.mark.parametrize(
    "corrupt_state, message",
    [
        (
            {
                "schema_version": 2,
                "state": "future_state",
                "release": {
                    "release_id": "future",
                    "version": "9.0.0",
                    "sequence": 900,
                    "artifact": "releases/future/client.whl",
                },
            },
            "unsupported schema",
        ),
        (
            {
                "schema_version": 1,
                "state": "unknown_state",
                "release": {
                    "release_id": "corrupt",
                    "version": "9.0.0",
                    "sequence": 900,
                    "artifact": "releases/corrupt/client.whl",
                },
            },
            "update state is unknown",
        ),
    ],
)
def test_unknown_persisted_state_fails_closed_and_preserves_rollback_baseline(
    tmp_path,
    corrupt_state,
    message,
):
    recorder = FlightRecorder(tmp_path / "audit")
    controller = UpdateController(
        tmp_path / "ota",
        event_sink=FlightRecorderEventSink(recorder),
    )
    artifact = controller.root / "releases" / "dradar-0.5.175" / "current.whl"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"known-good")
    known_good = {
        "release_id": "dradar-0.5.175",
        "version": "0.5.175",
        "sequence": 599,
        "artifact": str(artifact.relative_to(controller.root)),
    }
    _atomic_json(controller.current_path, known_good)
    _atomic_json(controller.last_known_good_path, known_good)
    _atomic_json(controller.state_path, corrupt_state)

    with pytest.raises(InvalidTransition, match=message):
        controller.state()
    with controller.transaction(), pytest.raises(InvalidTransition, match=message):
        controller.transition(UpdateState.FAILED, reason="candidate_failed")

    assert json.loads(controller.current_path.read_text()) == known_good
    assert json.loads(controller.last_known_good_path.read_text()) == known_good
    assert json.loads(controller.state_path.read_text()) == corrupt_state
    assert recorder._load(recorder.events_path)[-1]["reason_code"] == (
        "update_state_incompatible"
    )


def test_damaged_state_json_is_audited_and_does_not_destroy_rollback_baseline(
    tmp_path,
):
    recorder = FlightRecorder(tmp_path / "audit")
    controller = UpdateController(
        tmp_path / "ota",
        event_sink=FlightRecorderEventSink(recorder),
    )
    artifact = controller.root / "releases" / "dradar-0.5.175" / "current.whl"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"known-good")
    known_good = {
        "release_id": "dradar-0.5.175",
        "version": "0.5.175",
        "sequence": 599,
        "artifact": str(artifact.relative_to(controller.root)),
    }
    _atomic_json(controller.current_path, known_good)
    _atomic_json(controller.last_known_good_path, known_good)
    controller.state_path.write_text('{"schema_version":1,"state":', encoding="utf-8")

    with pytest.raises(InvalidTransition, match="persisted update state is unreadable"):
        controller.state()
    assert json.loads(controller.current_path.read_text()) == known_good
    assert json.loads(controller.last_known_good_path.read_text()) == known_good
    assert recorder._load(recorder.events_path)[-1]["reason_code"] == (
        "update_state_corrupt"
    )


def test_unknown_resume_state_is_audited_and_pause_remains_fail_closed(tmp_path):
    recorder = FlightRecorder(tmp_path / "audit")
    controller = UpdateController(
        tmp_path / "ota",
        event_sink=FlightRecorderEventSink(recorder),
    )
    paused = {
        "schema_version": 1,
        "state": "paused",
        "release": {
            "release_id": "dradar-0.6.0",
            "version": "0.6.0",
            "sequence": 600,
            "artifact": "releases/dradar-0.6.0/client.whl",
        },
        "resume_state": "future_downloading",
        "updated_at": "2026-09-02T00:00:00+00:00",
    }
    _atomic_json(controller.state_path, paused)

    with (
        controller.transaction(),
        pytest.raises(
            InvalidTransition,
            match="no compatible resume state",
        ),
    ):
        controller.resume()
    assert json.loads(controller.state_path.read_text()) == paused
    event = recorder._load(recorder.events_path)[-1]
    assert event["event_type"] == "update_failed"
    assert event["reason_code"] == "update_state_incompatible"
