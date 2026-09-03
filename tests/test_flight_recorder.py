import json
import multiprocessing as mp
import queue
import threading
import time
import zipfile
from pathlib import Path

import pytest

from dradar.flight_recorder import FlightRecorder, UPLOAD_BATCH_SIZE, validate_event


BATCH_ID = "1" * 32
SESSION_ID = "2" * 32
ASSIGNMENT_ID = "3" * 32
OLD_BATCH_ID = "a" * 32
NEW_BATCH_ID = "b" * 32
OLD_SESSION_ID = "c" * 32
NEW_SESSION_ID = "d" * 32
SENSITIVE_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "flight_event_sensitive_vectors.json")
    .read_text(encoding="utf-8")
)


def _record_events_in_child(home: str, worker: int, barrier) -> None:
    """Force overlapping read/modify/write cycles in separate processes."""
    # Deliberately widen the race window.  The production lock must serialize
    # this without relying on a scheduler-friendly filesystem timing.
    original_write = FlightRecorder._write

    def slow_write(path, events):
        time.sleep(0.002)
        original_write(path, events)

    FlightRecorder._write = staticmethod(slow_write)
    recorder = FlightRecorder(Path(home))
    barrier.wait(timeout=30)
    session_id = f"{worker + 4:032x}"
    for _ in range(25):
        recorder.record(
            "phase_changed",
            component="heartbeat",
            batch_id=BATCH_ID,
            session_id=session_id,
            attributes={"previous_phase": "preparing", "phase": "building"},
        )


def _record_worker_event_and_wait(
    home: str, ready, release, result,
) -> None:
    recorder = FlightRecorder(Path(home))
    event = recorder.record(
        "worker_registered",
        component="provider",
        batch_id=BATCH_ID,
        session_id=SESSION_ID,
        assignment_id=ASSIGNMENT_ID,
        attributes={"provider": "codex"},
    )
    result.put(event["event_id"])
    ready.set()
    if not release.wait(timeout=10):
        result.put(False)
        return
    # The parent/supervisor may have flushed this event.  The receipt must be
    # visible through the shared recorder state, not only the child's cache.
    result.put(event["event_id"] in recorder.last_acknowledged_event_ids)


class FlightClient:
    def __init__(self):
        self.batches = []

    def flight_events(self, events):
        self.batches.append(events)
        return {"acknowledged_event_ids": [event["event_id"] for event in events]}


def test_parent_and_children_serialize_recorder_read_modify_write(tmp_path):
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    workers = [
        ctx.Process(target=_record_events_in_child, args=(str(tmp_path), index, barrier))
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    try:
        assert all(worker.exitcode == 0 for worker in workers)
        recorder = FlightRecorder(tmp_path)
        history = recorder._load(recorder.events_path)
        pending = recorder._load(recorder.pending_path)
        assert len(history) == 50
        assert len(pending) == 50
        assert {event["client_id"] for event in history} == {recorder.client_id}
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()


def test_worker_registration_ack_is_visible_to_child_after_parent_flush(tmp_path):
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    result = ctx.Queue()
    worker = ctx.Process(
        target=_record_worker_event_and_wait,
        args=(str(tmp_path), ready, release, result),
    )
    worker.start()
    try:
        assert ready.wait(timeout=30)
        event_id = result.get(timeout=5)
        client = FlightClient()
        supervisor = FlightRecorder(tmp_path, client)
        assert supervisor.flush() == 1
        release.set()
        assert result.get(timeout=10) is True
        worker.join(timeout=30)
        assert worker.exitcode == 0
        assert event_id not in {
            event["event_id"] for event in supervisor._load(supervisor.pending_path)
        }
    finally:
        if worker.is_alive():
            release.set()
            worker.terminate()
            worker.join()
        try:
            while True:
                result.get_nowait()
        except queue.Empty:
            pass


def test_flush_commit_preserves_event_recorded_while_request_is_in_flight(tmp_path):
    class BlockingClient(FlightClient):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def flight_events(self, events):
            self.started.set()
            assert self.release.wait(timeout=10)
            return super().flight_events(events)

    client = BlockingClient()
    first = FlightRecorder(tmp_path, client)
    first_event = first.record(
        "worker_registered",
        component="provider",
        batch_id=BATCH_ID,
        session_id=SESSION_ID,
        assignment_id=ASSIGNMENT_ID,
        attributes={"provider": "codex"},
    )
    flush_thread = threading.Thread(target=first.flush)
    flush_thread.start()
    assert client.started.wait(timeout=10)

    sibling = FlightRecorder(tmp_path)
    sibling_event = sibling.record(
        "heartbeat_sent",
        component="heartbeat",
        batch_id=BATCH_ID,
        session_id=SESSION_ID,
    )
    client.release.set()
    flush_thread.join(timeout=10)
    assert not flush_thread.is_alive()

    pending_ids = {
        event["event_id"] for event in first._load(first.pending_path)
    }
    assert first_event["event_id"] not in pending_ids
    assert sibling_event["event_id"] in pending_ids
    assert first_event["event_id"] in first.last_acknowledged_event_ids


def test_acknowledged_event_ids_are_cumulative_across_interleaved_flushes(tmp_path):
    class OneAtATime(FlightClient):
        def flight_events(self, events):
            self.batches.append(events)
            return {"acknowledged_event_ids": [events[0]["event_id"]]}

    client = OneAtATime()
    recorder = FlightRecorder(tmp_path, client)
    first = recorder.record(
        "worker_registered", component="provider", session_id=SESSION_ID,
        assignment_id=ASSIGNMENT_ID, attributes={"provider": "codex"},
    )
    assert recorder.flush() == 1
    second = recorder.record(
        "heartbeat_sent", component="heartbeat", session_id=SESSION_ID,
        batch_id=BATCH_ID,
    )
    assert recorder.flush() == 1
    assert first["event_id"] in recorder.last_acknowledged_event_ids
    assert second["event_id"] in recorder.last_acknowledged_event_ids


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"acknowledged_event_ids": 1},
        {"acknowledged_event_ids": {"a" * 32: True}},
        {"acknowledged_event_ids": "a" * 32},
        {"acknowledged_event_ids": ["not-an-event-id"]},
    ],
)
def test_malformed_flight_response_keeps_pending_event(tmp_path, response):
    class MalformedClient(FlightClient):
        def flight_events(self, events):
            self.batches.append(events)
            return response

    recorder = FlightRecorder(tmp_path, MalformedClient())
    event = recorder.record(
        "worker_registered", component="provider",
        batch_id=BATCH_ID, session_id=SESSION_ID,
        assignment_id=ASSIGNMENT_ID, attributes={"provider": "codex"},
    )
    assert recorder.flush() == 0
    assert event["event_id"] in {
        item["event_id"] for item in recorder._load(recorder.pending_path)
    }
    assert not recorder.last_acknowledged_event_ids


def test_generator_ack_response_keeps_pending_event(tmp_path):
    class GeneratorClient(FlightClient):
        def flight_events(self, events):
            self.batches.append(events)
            return {
                "acknowledged_event_ids": (
                    event["event_id"] for event in events
                ),
            }

    recorder = FlightRecorder(tmp_path, GeneratorClient())
    event = recorder.record(
        "worker_registered", component="provider",
        batch_id=BATCH_ID, session_id=SESSION_ID,
        assignment_id=ASSIGNMENT_ID, attributes={"provider": "codex"},
    )
    assert recorder.flush() == 0
    assert event["event_id"] in {
        item["event_id"] for item in recorder._load(recorder.pending_path)
    }
    assert not recorder.last_acknowledged_event_ids


def test_offline_jsonl_replays_idempotent_envelopes_and_keeps_history(tmp_path):
    offline = FlightRecorder(tmp_path)
    event = offline.record(
        "assignment_checked_out", component="claim",
        batch_id=BATCH_ID, session_id=SESSION_ID,
        assignment_id=ASSIGNMENT_ID,
    )
    assert offline.flush() == 0
    assert offline.pending_path.read_text().count("\n") == 1

    client = FlightClient()
    replay = FlightRecorder(tmp_path, client)
    assert replay.flush() == 1
    assert client.batches[0][0]["event_id"] == event["event_id"]
    assert replay.pending_path.read_text() == ""
    assert replay.events_path.read_text().count("\n") == 1


def test_scoped_flush_keeps_other_plan_and_session_pending_across_restart(tmp_path):
    """A new plan may upload only its own session without deleting old evidence."""
    writer = FlightRecorder(tmp_path)
    old_null_batch = writer.record(
        "session_started",
        component="cli",
        session_id=OLD_SESSION_ID,
    )
    old_plan = writer.record(
        "phase_changed",
        component="heartbeat",
        batch_id=OLD_BATCH_ID,
        session_id=OLD_SESSION_ID,
        attributes={"previous_phase": "preparing", "phase": "building"},
    )
    current_plan = writer.record(
        "worker_registered",
        component="provider",
        batch_id=NEW_BATCH_ID,
        session_id=NEW_SESSION_ID,
        assignment_id=ASSIGNMENT_ID,
        attributes={"provider": "codex"},
    )

    client = FlightClient()
    replay = FlightRecorder(tmp_path, client)
    assert replay.flush(batch_id=NEW_BATCH_ID, session_id=NEW_SESSION_ID) == 1
    assert [event["event_id"] for event in client.batches[0]] == [
        current_plan["event_id"]
    ]

    pending = replay._load(replay.pending_path)
    assert {event["event_id"] for event in pending} == {
        old_null_batch["event_id"], old_plan["event_id"],
    }

    # A later process can deliberately replay the old scope; it was retained,
    # not silently discarded while the new plan was registering.
    old_client = FlightClient()
    old_replay = FlightRecorder(tmp_path, old_client)
    assert old_replay.flush(batch_id=OLD_BATCH_ID, session_id=OLD_SESSION_ID) == 1
    assert [event["event_id"] for event in old_client.batches[0]] == [
        old_plan["event_id"]
    ]
    assert old_null_batch["event_id"] in {
        event["event_id"] for event in old_replay._load(old_replay.pending_path)
    }


def test_session_scope_without_batch_stays_local(tmp_path):
    """An unknown plan/account scope must not replay a null-batch event."""
    recorder = FlightRecorder(tmp_path)
    event = recorder.record(
        "session_started", component="cli", session_id=OLD_SESSION_ID,
    )
    client = FlightClient()
    replay = FlightRecorder(tmp_path, client)

    assert replay.flush(session_id=OLD_SESSION_ID) == 0
    assert client.batches == []
    assert event["event_id"] in {
        item["event_id"] for item in replay._load(replay.pending_path)
    }


def test_scoped_flush_prioritizes_required_worker_event_behind_backlog(tmp_path):
    client = FlightClient()
    recorder = FlightRecorder(tmp_path, client)
    for _ in range(UPLOAD_BATCH_SIZE + 5):
        recorder.record(
            "phase_changed",
            component="heartbeat",
            batch_id=NEW_BATCH_ID,
            session_id=NEW_SESSION_ID,
            attributes={"previous_phase": "preparing", "phase": "building"},
        )
    required = recorder.record(
        "worker_registered",
        component="provider",
        batch_id=NEW_BATCH_ID,
        session_id=NEW_SESSION_ID,
        assignment_id=ASSIGNMENT_ID,
        attributes={"provider": "codex"},
    )

    assert recorder.flush(
        batch_id=NEW_BATCH_ID,
        session_id=NEW_SESSION_ID,
        required_event_id=required["event_id"],
    ) == UPLOAD_BATCH_SIZE
    assert client.batches[0][0]["event_id"] == required["event_id"]
    assert required["event_id"] not in {
        event["event_id"] for event in recorder._load(recorder.pending_path)
    }


@pytest.mark.parametrize("forbidden", [
    "token", "oauth", "prompt", "source_code", "request_body",
    "hostname", "proxy", "command",
])
def test_privacy_allowlist_rejects_sensitive_or_arbitrary_attributes(
    tmp_path, forbidden,
):
    recorder = FlightRecorder(tmp_path)
    with pytest.raises(ValueError, match="unsupported flight-event attributes"):
        recorder.record(
            "request_failed", component="api", attributes={forbidden: "secret"},
        )
    assert not recorder.events_path.exists()


def test_diagnostic_bundle_contains_only_manifest_and_allowlisted_events(tmp_path):
    recorder = FlightRecorder(tmp_path)
    recorder.record(
        "release_completed", component="release",
        request_id="a" * 32, assignment_id=ASSIGNMENT_ID,
        reason_code="user_force",
        attributes={"force": True, "was_running": True},
    )
    output = recorder.export_diagnostic_bundle(tmp_path / "diagnostics.zip")
    with zipfile.ZipFile(output) as bundle:
        assert set(bundle.namelist()) == {"manifest.json", "events.jsonl"}
        manifest = json.loads(bundle.read("manifest.json"))
        events = bundle.read("events.jsonl").decode()
    assert manifest["privacy_policy"] == "strict_allowlist_no_content_or_credentials"
    assert "user_force" in events
    for forbidden in ("token", "oauth", "prompt", "source_code", "hostname", "command"):
        assert forbidden not in events.lower()


def test_client_actor_is_fixed_and_tampered_actor_is_rejected(tmp_path):
    recorder = FlightRecorder(tmp_path)
    event = recorder.record("checkpoint_replayed", component="checkpoint")
    assert event["actor"] == "cli"
    tampered = dict(event, actor="legacy_unknown")
    with pytest.raises(ValueError, match="actor must be cli"):
        validate_event(tampered)


@pytest.mark.parametrize(
    "vector", SENSITIVE_VECTORS, ids=[item["name"] for item in SENSITIVE_VECTORS],
)
def test_real_sensitive_values_are_rejected_by_closed_field_enums(
    tmp_path, vector,
):
    recorder = FlightRecorder(tmp_path)
    with pytest.raises(ValueError, match="not an allowed value"):
        recorder.record(
            "request_failed", component="api",
            attributes={"outcome": vector["value"]},
        )


def test_tampered_jsonl_cannot_enter_upload_or_diagnostic_bundle(tmp_path):
    recorder = FlightRecorder(tmp_path)
    safe = recorder.record(
        "request_failed", component="api", attributes={"outcome": "upload-failed"},
    )
    tampered = dict(safe)
    tampered["event_id"] = "b" * 32
    tampered["attributes"] = {"outcome": SENSITIVE_VECTORS[0]["value"]}
    poisoned = json.dumps(tampered, separators=(",", ":")) + "\n"
    recorder.events_path.write_text(poisoned, encoding="utf-8")
    recorder.pending_path.write_text(poisoned, encoding="utf-8")

    client = FlightClient()
    replay = FlightRecorder(tmp_path, client)
    assert replay.flush() == 0
    assert client.batches == []
    output = replay.export_diagnostic_bundle(tmp_path / "tampered.zip")
    with zipfile.ZipFile(output) as bundle:
        assert bundle.read("events.jsonl") == b""
        manifest = json.loads(bundle.read("manifest.json"))
    assert manifest["event_count"] == 0
    assert manifest["pending_count"] == 0


def test_runtime_try_record_drops_invalid_diagnostic_without_writing(tmp_path):
    recorder = FlightRecorder(tmp_path)
    assert recorder.try_record(
        "assignment_checked_out", component="claim", assignment_id="legacy-test-id",
    ) is None
    assert not recorder.events_path.exists()
    assert not recorder.pending_path.exists()


def test_building_phase_is_allowlisted_for_environment_preparation(tmp_path):
    recorder = FlightRecorder(tmp_path)
    event = recorder.record(
        "phase_changed",
        component="heartbeat",
        batch_id=BATCH_ID,
        session_id=SESSION_ID,
        assignment_id=ASSIGNMENT_ID,
        attributes={"previous_phase": "preparing", "phase": "building"},
    )
    assert event["attributes"] == {
        "previous_phase": "preparing", "phase": "building",
    }
