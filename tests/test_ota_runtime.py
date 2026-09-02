import base64
import hashlib
import json
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import dradar.ota.state as state_module
from dradar.flight_recorder import FlightRecorder
from dradar.ota import (
    CompatibilitySnapshot,
    InvalidTransition,
    ManifestError,
    PlatformTarget,
    RolloutContext,
    SafePointSnapshot,
    UpdateRuntime,
    UpdateState,
)
from dradar.ota.state import UpdateController, _atomic_json

BODY = b"signed-cross-platform-candidate"
SIGNING_KEY = Ed25519PrivateKey.generate()
PUBLIC_KEY = SIGNING_KEY.public_key().public_bytes(
    serialization.Encoding.Raw,
    serialization.PublicFormat.Raw,
)
TRUSTED_KEYS = {"runtime-root": PUBLIC_KEY}


def sign_document(document):
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    document["signature"] = {
        "algorithm": "ed25519",
        "key_id": "runtime-root",
        "value": base64.b64encode(SIGNING_KEY.sign(payload)).decode(),
    }
    return document


class Response:
    def __init__(self, chunks, failure=None):
        self.chunks = chunks
        self.failure = failure

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        if self.failure:
            raise self.failure

    def iter_bytes(self, chunk_size=65536):
        del chunk_size
        yield from self.chunks


class Client:
    def __init__(self, response):
        self.response = response

    def stream(self, method, url, **kwargs):
        assert (method, kwargs) == ("GET", {"follow_redirects": False})
        assert url.startswith("https://")
        return self.response


def signed_release():
    artifacts = []
    for os_name in ("macos", "linux", "windows"):
        for arch in ("x86_64", "arm64"):
            suffix = {"macos": "pkg", "linux": "whl", "windows": "zip"}[os_name]
            artifacts.append(
                {
                    "os": os_name,
                    "arch": arch,
                    "filename": f"dradar-0.6.0-{os_name}-{arch}.{suffix}",
                    "url": f"https://releases.example.invalid/{os_name}/{arch}/candidate",
                    "size": len(BODY),
                    "sha256": hashlib.sha256(BODY).hexdigest(),
                }
            )
    document = {
        "schema_version": 1,
        "release_id": "dradar-cli-0.6.0-runtime",
        "version": "0.6.0",
        "sequence": 600,
        "channel": "stable",
        "published_at": "2026-09-02T06:00:00Z",
        "expires_at": "2099-09-02T06:00:00Z",
        "rollout": {
            "stage": "general",
            "basis_points": 10_000,
            "salt": "runtime-600",
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
        "artifacts": artifacts,
    }
    return sign_document(document), TRUSTED_KEYS


def compatibility():
    return CompatibilitySnapshot(
        launcher_version="1.0.0",
        runner_protocol=3,
        doctor_contract=1,
        provider_contract=1,
        ledger_schema=1,
        checkpoint_schema=0,
    )


def seed_lkg(root):
    body = b"known-good"
    release_id = "dradar-0.5.175"
    path = root / "releases" / release_id / "current.whl"
    path.parent.mkdir(parents=True)
    path.write_bytes(body)
    pointer = {
        "release_id": release_id,
        "version": "0.5.175",
        "sequence": 599,
        "artifact": str(path.relative_to(root)),
    }
    manifest = sign_document(
        {
            "schema_version": 1,
            "release_id": release_id,
            "version": "0.5.175",
            "sequence": 599,
            "channel": "stable",
            "published_at": "2026-09-01T06:00:00Z",
            "expires_at": "2099-09-01T06:00:00Z",
            "rollout": {
                "stage": "general",
                "basis_points": 10_000,
                "salt": "baseline-599",
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
                    "filename": path.name,
                    "url": "https://releases.example.invalid/baseline/current.whl",
                    "size": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            ],
        }
    )
    _atomic_json(
        path.parent / "release-record.json",
        {
            "schema_version": 1,
            "committed": True,
            "pointer": pointer,
            "manifest": manifest,
        },
    )
    _atomic_json(root / "current.json", pointer)
    _atomic_json(root / "last-known-good.json", pointer)
    return pointer


def prepare(runtime, target=None):
    document, keys = signed_release()
    target = target or PlatformTarget("linux", "x86_64")
    return runtime.prepare(
        document,
        trusted_keys=keys,
        current_version="0.5.175",
        committed_sequence=599,
        compatibility=compatibility(),
        rollout=RolloutContext(subject=runtime.audit.recorder.client_id),
        target=target,
    )


def event_names(recorder):
    return [event["event_type"] for event in recorder._load(recorder.events_path)]


def test_pre_adapter_gap_has_no_audit_but_runtime_records_complete_chain(tmp_path):
    old_root = tmp_path / "old"
    seed_lkg(old_root)
    old = UpdateController(old_root)
    assert not (tmp_path / "old-recorder" / "flight-recorder" / "events.jsonl").exists()
    assert old.event_sink.__class__.__name__ == "NullEventSink"

    root = tmp_path / "new"
    seed_lkg(root)
    recorder = FlightRecorder(tmp_path / "new-recorder")
    runtime = UpdateRuntime(
        root,
        recorder=recorder,
        download_client=Client(Response([BODY[:8], BODY[8:]])),
    )
    decision = prepare(runtime)
    assert decision.eligible is True
    opened = []

    def self_test(artifact):
        opened.append(artifact)
        return artifact.read_bytes() == BODY

    assert (
        runtime.activate_and_self_test(SafePointSnapshot(), self_test)
        is UpdateState.COMMITTED
    )
    with pytest.raises(ManifestError, match="closed"):
        opened[0].read_bytes()

    assert event_names(recorder) == [
        "update_detected",
        "update_downloaded",
        "update_verified",
        "update_staged",
        "update_waiting_safe_point",
        "update_activated",
        "update_self_testing",
        "update_committed",
    ]
    raw = recorder.events_path.read_text()
    assert "dradar-cli-0.6.0-runtime" not in raw
    assert '"version"' not in raw
    assert runtime.controller.launch_pointer()["version"] == "0.6.0"


@pytest.mark.parametrize(
    "target, suffix",
    [
        (PlatformTarget("macos", "x86_64"), ".pkg"),
        (PlatformTarget("macos", "arm64"), ".pkg"),
        (PlatformTarget("linux", "x86_64"), ".whl"),
        (PlatformTarget("linux", "arm64"), ".whl"),
        (PlatformTarget("windows", "x86_64"), ".zip"),
        (PlatformTarget("windows", "arm64"), ".zip"),
    ],
)
def test_signed_cross_platform_packages_select_and_verify(target, suffix, tmp_path):
    root = tmp_path / f"{target.os}-{target.arch}"
    seed_lkg(root)
    recorder = FlightRecorder(tmp_path / "audit" / f"{target.os}-{target.arch}")
    runtime = UpdateRuntime(
        root, recorder=recorder, download_client=Client(Response([BODY]))
    )
    decision = prepare(runtime, target)
    assert decision.artifact.filename.endswith(suffix)
    assert runtime.controller.state()["state"] == "waiting_safe_point"


def test_safe_point_block_is_audited_without_forcing_or_releasing_work(tmp_path):
    root = tmp_path / "ota"
    seed_lkg(root)
    recorder = FlightRecorder(tmp_path / "audit")
    runtime = UpdateRuntime(
        root, recorder=recorder, download_client=Client(Response([BODY]))
    )
    prepare(runtime)
    blocked = SafePointSnapshot(
        active_assignments=40,
        uploads_inflight=1,
        durable_uploads_pending=1,
        refill_accepting_new=False,
        worker_supervisor_idle=False,
    )
    with pytest.raises(InvalidTransition, match="active_assignments"):
        runtime.activate_and_self_test(blocked, lambda _artifact: True)
    assert runtime.controller.state()["state"] == "waiting_safe_point"
    event = recorder._load(recorder.events_path)[-1]
    assert event["reason_code"] == "update_safe_point_blocked"


def test_failed_self_test_rolls_back_and_preserves_auditable_terminal_state(tmp_path):
    root = tmp_path / "ota"
    previous = seed_lkg(root)
    recorder = FlightRecorder(tmp_path / "audit")
    runtime = UpdateRuntime(
        root, recorder=recorder, download_client=Client(Response([BODY]))
    )
    prepare(runtime)
    opened = []

    def self_test(artifact):
        opened.append(artifact)
        return False

    assert (
        runtime.activate_and_self_test(SafePointSnapshot(), self_test)
        is UpdateState.ROLLED_BACK
    )
    with pytest.raises(ManifestError, match="closed"):
        opened[0].read_bytes()
    assert json.loads((root / "current.json").read_text()) == previous
    assert event_names(recorder)[-2:] == [
        "update_rollback_pending",
        "update_rolled_back",
    ]
    assert (
        recorder._load(recorder.events_path)[-1]["reason_code"]
        == "update_self_test_failed"
    )


def test_interrupted_download_fails_closed_and_records_reason(tmp_path):
    class Offline(Response):
        def iter_bytes(self, chunk_size=65536):
            del chunk_size
            yield b"partial"
            raise ConnectionError("offline with private details")

    root = tmp_path / "ota"
    seed_lkg(root)
    recorder = FlightRecorder(tmp_path / "audit")
    runtime = UpdateRuntime(
        root, recorder=recorder, download_client=Client(Offline([]))
    )
    with pytest.raises(ConnectionError):
        prepare(runtime)
    assert runtime.controller.state()["state"] == "failed"
    event = recorder._load(recorder.events_path)[-1]
    assert event["reason_code"] == "update_download_failed"
    assert "private details" not in recorder.events_path.read_text()
    assert not list((root / "downloads").rglob("*.partial"))


def test_keyboard_interrupt_during_prepare_is_durable_and_cleans_partial(tmp_path):
    class Interrupted(Response):
        def iter_bytes(self, chunk_size=65536):
            del chunk_size
            yield b"partial"
            raise KeyboardInterrupt

    root = tmp_path / "ota"
    seed_lkg(root)
    recorder = FlightRecorder(tmp_path / "audit")
    runtime = UpdateRuntime(
        root, recorder=recorder, download_client=Client(Interrupted([]))
    )
    with pytest.raises(KeyboardInterrupt):
        prepare(runtime)
    assert runtime.controller.state()["state"] == "failed"
    assert not list((root / "downloads").rglob("*.partial"))


def test_live_safe_point_is_sampled_inside_update_transaction(tmp_path):
    root = tmp_path / "ota"
    seed_lkg(root)
    recorder = FlightRecorder(tmp_path / "audit")
    runtime = UpdateRuntime(
        root, recorder=recorder, download_client=Client(Response([BODY]))
    )
    prepare(runtime)
    sampled = []

    def live_snapshot():
        sampled.append(runtime.controller._local.transaction_depth)
        return SafePointSnapshot(active_assignments=1)

    with pytest.raises(InvalidTransition, match="active_assignments"):
        runtime.activate_and_self_test(live_snapshot, lambda _artifact: True)
    assert sampled == [1]
    assert runtime.controller.state()["state"] == "waiting_safe_point"


def test_keyboard_interrupt_during_self_test_rolls_back_before_propagating(tmp_path):
    root = tmp_path / "ota"
    previous = seed_lkg(root)
    recorder = FlightRecorder(tmp_path / "audit")
    runtime = UpdateRuntime(
        root, recorder=recorder, download_client=Client(Response([BODY]))
    )
    prepare(runtime)

    def interrupted(_artifact):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        runtime.activate_and_self_test(SafePointSnapshot(), interrupted)
    assert json.loads((root / "current.json").read_text()) == previous
    assert runtime.controller.state()["state"] == "rolled_back"


@pytest.mark.skipif(
    not os.path.isdir("/dev/fd"), reason="requires observable POSIX fds"
)
def test_lkg_enospc_after_committed_state_closes_staged_capability(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "ota"
    seed_lkg(root)
    recorder = FlightRecorder(tmp_path / "audit")
    runtime = UpdateRuntime(
        root, recorder=recorder, download_client=Client(Response([BODY]))
    )
    prepare(runtime)
    staged = runtime.controller.staged_artifact()
    before = len(os.listdir("/dev/fd"))
    original = state_module._atomic_json

    def fail_lkg(path, value):
        if path == runtime.controller.last_known_good_path:
            raise OSError("ENOSPC")
        return original(path, value)

    monkeypatch.setattr(state_module, "_atomic_json", fail_lkg)
    with pytest.raises(OSError, match="ENOSPC"):
        runtime.activate_and_self_test(SafePointSnapshot(), lambda _artifact: True)

    assert runtime.controller.state()["state"] == "committed"
    assert runtime.controller._staged_artifact is None
    with pytest.raises(ManifestError, match="closed"):
        staged.read_bytes()
    assert len(os.listdir("/dev/fd")) <= before - 2


def test_old_runner_protocol_fails_closed_before_download_and_is_audited(tmp_path):
    class NoDownload(Client):
        def stream(self, method, url, **kwargs):
            raise AssertionError("ineligible legacy client must not download")

    root = tmp_path / "ota"
    seed_lkg(root)
    recorder = FlightRecorder(tmp_path / "audit")
    runtime = UpdateRuntime(root, recorder=recorder, download_client=NoDownload(None))
    document, keys = signed_release()
    legacy = CompatibilitySnapshot(
        launcher_version="1.0.0",
        runner_protocol=2,
        doctor_contract=1,
        provider_contract=1,
        ledger_schema=1,
        checkpoint_schema=0,
    )
    decision = runtime.prepare(
        document,
        trusted_keys=keys,
        current_version="0.5.175",
        committed_sequence=599,
        compatibility=legacy,
        rollout=RolloutContext(subject=recorder.client_id),
        target=PlatformTarget("linux", "x86_64"),
    )
    assert decision.reason == "runner_protocol_incompatible"
    assert runtime.controller.state() is None
    assert event_names(recorder) == ["update_policy_rejected"]


def test_legacy_client_first_signed_update_commits_and_launches_open_inode(tmp_path):
    document, keys = signed_release()
    recorder = FlightRecorder(tmp_path)
    runtime = UpdateRuntime(
        tmp_path / "ota",
        recorder=recorder,
        download_client=Client(Response([BODY])),
    )
    decision = runtime.prepare(
        document,
        trusted_keys=keys,
        current_version="0.5.175",
        committed_sequence=0,
        compatibility=compatibility(),
        rollout=RolloutContext(subject="legacy-client"),
        target=PlatformTarget("linux", "x86_64"),
    )
    assert decision.eligible is True
    assert (
        runtime.activate_and_self_test(SafePointSnapshot(), lambda item: True)
        is UpdateState.COMMITTED
    )
    launched = runtime.controller.launch_artifact()
    try:
        assert launched.read_bytes() == BODY
    finally:
        launched.close()


def test_legacy_client_failed_candidate_restores_bundled_fallback(tmp_path):
    document, keys = signed_release()
    runtime = UpdateRuntime(
        tmp_path / "ota",
        recorder=FlightRecorder(tmp_path),
        download_client=Client(Response([BODY])),
    )
    runtime.prepare(
        document,
        trusted_keys=keys,
        current_version="0.5.175",
        committed_sequence=0,
        compatibility=compatibility(),
        rollout=RolloutContext(subject="legacy-client"),
        target=PlatformTarget("linux", "x86_64"),
    )
    assert (
        runtime.activate_and_self_test(SafePointSnapshot(), lambda item: False)
        is UpdateState.ROLLED_BACK
    )
    assert json.loads((tmp_path / "ota" / "current.json").read_text()) == {
        "schema_version": 1,
        "legacy_fallback": True,
    }


def test_caller_cannot_spoof_the_durable_anti_rollback_baseline(tmp_path):
    class NoDownload(Client):
        def stream(self, method, url, **kwargs):
            raise AssertionError("inconsistent baseline must fail before download")

    root = tmp_path / "ota"
    seed_lkg(root)
    recorder = FlightRecorder(tmp_path / "audit")
    runtime = UpdateRuntime(root, recorder=recorder, download_client=NoDownload(None))
    document, keys = signed_release()
    with pytest.raises(InvalidTransition, match="durable committed pointer"):
        runtime.prepare(
            document,
            trusted_keys=keys,
            current_version="0.1.0",
            committed_sequence=1,
            compatibility=compatibility(),
            rollout=RolloutContext(subject=recorder.client_id),
            target=PlatformTarget("linux", "x86_64"),
        )
    assert runtime.controller.state() is None


def test_launcher_crash_recovery_restores_lkg_and_records_recovery(tmp_path):
    root = tmp_path / "ota"
    previous = seed_lkg(root)
    recorder = FlightRecorder(tmp_path / "audit")
    runtime = UpdateRuntime(
        root, recorder=recorder, download_client=Client(Response([BODY]))
    )
    prepare(runtime)
    with runtime.controller.transaction():
        runtime.controller.activate(SafePointSnapshot())
    restarted = UpdateRuntime(
        root, recorder=recorder, download_client=Client(Response([]))
    )
    assert restarted.recover_on_launcher_start() is True
    assert json.loads((root / "current.json").read_text()) == previous
    event = recorder._load(recorder.events_path)[-1]
    assert event["event_type"] == "update_rolled_back"
    assert event["reason_code"] == "update_crash_recovery"
