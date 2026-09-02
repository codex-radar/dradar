import json
import zipfile
from pathlib import Path

import pytest

from dradar.flight_recorder import FlightRecorder, validate_event


BATCH_ID = "1" * 32
SESSION_ID = "2" * 32
ASSIGNMENT_ID = "3" * 32
SENSITIVE_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "flight_event_sensitive_vectors.json")
    .read_text(encoding="utf-8")
)


class FlightClient:
    def __init__(self):
        self.batches = []

    def flight_events(self, events):
        self.batches.append(events)
        return {"acknowledged_event_ids": [event["event_id"] for event in events]}


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
