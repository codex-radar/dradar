import json

import pytest

from dradar.api_client import ApiError
from dradar.telemetry import RunnerTelemetry


class FakeClient:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.heartbeats = []
        self.closes = []

    def runner_heartbeat(self, payload):
        self.heartbeats.append(payload)
        response = self.responses.pop(0) if self.responses else {
            "accepted": True, "action": "continue", "batch_id": "batch-1",
            "next_heartbeat_sec": 60,
        }
        if isinstance(response, Exception):
            raise response
        return response

    def runner_close(self, payload):
        self.closes.append(payload)
        return {"ok": True}


def test_payload_is_one_session_not_one_per_assignment_and_stays_small():
    client = FakeClient()
    telemetry = RunnerTelemetry(client, jitter=False, target_workers=20)
    telemetry.bind_batch("batch-1")
    telemetry.set_phase("running", "assignment-1", 3)
    assert telemetry._send_once() == 60
    telemetry.set_phase("running", "assignment-2")
    telemetry._send_once()

    assert {p["session_id"] for p in client.heartbeats} == {telemetry.session_id}
    assert [p["active_assignment_id"] for p in client.heartbeats] == [
        "assignment-1", "assignment-2"]
    assert [p["owner_epoch"] for p in client.heartbeats] == [3, None]
    assert client.heartbeats[1]["seq"] > client.heartbeats[0]["seq"]
    assert len(json.dumps(client.heartbeats[-1]).encode()) < 1024
    assert set(client.heartbeats[-1]) == {
        "protocol_version", "client_version", "session_id", "batch_id", "seq",
        "phase", "active_assignment_id", "owner_epoch",
        "client_monotonic_ms", "progress_counter",
        "platform", "target_workers",
    }
    assert client.heartbeats[-1]["target_workers"] == 20


def test_target_worker_count_is_bounded():
    client = FakeClient()
    for value in (0, 41):
        try:
            RunnerTelemetry(client, target_workers=value)
        except ValueError as exc:
            assert "between 1 and 40" in str(exc)
        else:
            raise AssertionError("out-of-range target worker count was accepted")


def test_server_can_slow_cadence_but_not_make_it_pathological():
    client = FakeClient([
        {"next_heartbeat_sec": 99999},
        {"next_heartbeat_sec": 99999},
    ])
    telemetry = RunnerTelemetry(client, jitter=False)
    # Preparation/queueing is operator-visible and must stay on the fast
    # cadence even if the server asks for a much slower interval.
    assert telemetry._send_once() == 30
    telemetry.set_phase("running")
    assert telemetry._send_once() == 600


def test_worker_registration_requires_accepted_building_heartbeat():
    client = FakeClient([
        {"accepted": False, "action": "refresh", "next_heartbeat_sec": 30},
        {"accepted": True, "action": "continue", "next_heartbeat_sec": 30},
    ])
    telemetry = RunnerTelemetry(client, jitter=False)
    telemetry.set_phase("building", "assignment-1")
    assert telemetry.flush_for_worker_registration() is False
    assert telemetry.flush_for_worker_registration() is True
    assert [item["phase"] for item in client.heartbeats] == ["building", "building"]
    assert [item["active_assignment_id"] for item in client.heartbeats] == [
        "assignment-1", "assignment-1",
    ]


def test_record_event_returns_worker_registration_id(tmp_path):
    telemetry = RunnerTelemetry(FakeClient(), jitter=False, home=tmp_path)
    event = telemetry.record_event(
        "worker_registered",
        component="provider",
        assignment_id="a" * 32,
        attributes={"provider": "codex"},
    )
    assert isinstance(event, dict)
    assert len(event["event_id"]) == 32
    assert event["event_type"] == "worker_registered"


def test_recorder_present_without_exact_event_id_fails_closed(tmp_path):
    telemetry = RunnerTelemetry(FakeClient([{"accepted": True}]), jitter=False, home=tmp_path)
    telemetry.set_phase("building", "assignment-1")
    assert telemetry.flush_for_worker_registration() is False


def test_server_notices_are_bounded_validated_and_printed_once(capsys):
    notice = {
        "id": "dsh-usage-upgrade-20260814",
        "severity": "warning",
        "message": "当前任务继续运行。\n完成后刷新 CLI。",
    }
    client = FakeClient([
        {"next_heartbeat_sec": 60, "notices": [notice]},
        {"next_heartbeat_sec": 60, "notices": [notice]},
        {"next_heartbeat_sec": 60, "notices": [
            {"id": "bad", "severity": "unknown", "message": "ignored"},
            "not-an-object",
        ]},
    ])
    telemetry = RunnerTelemetry(client, jitter=False)

    telemetry._send_once()
    telemetry._send_once()
    telemetry._send_once()

    err = capsys.readouterr().err
    assert err.count("dsh-usage-upgrade-20260814") == 0
    assert err.count("server notice [warning]") == 1
    assert "当前任务继续运行。 完成后刷新 CLI。" in err
    assert "ignored" not in err


def test_three_failures_warn_once_then_recovery_is_visible(capsys):
    client = FakeClient([
        ApiError("offline"), ApiError("offline"), ApiError("offline"),
        ApiError("offline"), {"next_heartbeat_sec": 120},
    ])
    telemetry = RunnerTelemetry(client, jitter=False)
    for _ in range(5):
        telemetry._send_once()
    err = capsys.readouterr().err
    assert err.count("warning:") == 1
    assert "recovered" in err


def test_old_server_404_disables_future_traffic_silently(capsys):
    client = FakeClient([ApiError("not found", status_code=404)])
    telemetry = RunnerTelemetry(client, jitter=False)
    telemetry._send_once()
    telemetry._send_once()
    assert len(client.heartbeats) == 1
    assert capsys.readouterr().err == ""


def test_precheckout_flush_propagates_session_registration_error():
    expected = ApiError(
        "server returned 409: runner session capacity reached",
        status_code=409,
        code="runner_session_capacity_reached",
    )
    client = FakeClient([expected])
    telemetry = RunnerTelemetry(client, jitter=False)

    with pytest.raises(ApiError) as raised:
        telemetry.flush_for_checkout()

    assert raised.value is expected
    assert len(client.heartbeats) == 1


def test_precheckout_flush_keeps_legacy_heartbeat_404_compatibility():
    client = FakeClient([ApiError("not found", status_code=404)])
    telemetry = RunnerTelemetry(client, jitter=False)

    telemetry.flush_for_checkout()
    telemetry.flush_for_checkout()

    assert len(client.heartbeats) == 1


def test_plan_stop_heartbeat_publishes_shared_pool_abort_before_checkout(
    tmp_path, monkeypatch,
):
    marker = tmp_path / "aborts" / "batch.stop"
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(marker))
    client = FakeClient([{
        "accepted": True,
        "action": "continue",
        "batch_id": "batch-1",
        "next_heartbeat_sec": 60,
        "stop_requested": True,
        "user_message": "这次运行已经停止。",
    }])
    telemetry = RunnerTelemetry(client, jitter=False)
    telemetry.bind_batch("batch-1")

    assert telemetry.flush_for_checkout() is True
    assert telemetry.stop_requested is True
    assert marker.read_text().startswith("drain:")
    assert len(client.heartbeats) == 1


def test_close_carries_only_session_batch_seq_and_reason():
    client = FakeClient()
    telemetry = RunnerTelemetry(client, jitter=False)
    telemetry.bind_batch("batch-1")
    telemetry._send_once()
    telemetry.close("paused")
    assert client.closes == [{
        "session_id": telemetry.session_id,
        "batch_id": "batch-1",
        "seq": 2,
        "reason": "paused",
    }]


def test_session_started_is_deferred_until_batch_bind(tmp_path):
    telemetry = RunnerTelemetry(FakeClient(), jitter=False, home=tmp_path)
    assert telemetry.flight_recorder is not None
    # Constructor must not enqueue an event with a null batch identity.
    assert telemetry.flight_recorder._load(
        telemetry.flight_recorder.pending_path,
    ) == []
    telemetry.bind_batch("b" * 32)
    telemetry.bind_batch("c" * 32)
    events = telemetry.flight_recorder._load(telemetry.flight_recorder.pending_path)
    starts = [event for event in events if event["event_type"] == "session_started"]
    assert len(starts) == 1
    assert starts[0]["batch_id"] == "b" * 32


def test_prebind_session_started_is_dropped_instead_of_persisted(tmp_path):
    telemetry = RunnerTelemetry(FakeClient(), jitter=False, home=tmp_path)
    assert telemetry.record_event("session_started", component="cli") is None
    assert telemetry.flight_recorder._load(
        telemetry.flight_recorder.pending_path,
    ) == []
    telemetry.bind_batch("b" * 32)
    events = telemetry.flight_recorder._load(telemetry.flight_recorder.pending_path)
    assert [event["event_type"] for event in events].count("session_started") == 1


def test_legacy_heartbeat_backfills_batch_before_flight_upload(tmp_path):
    client = FakeClient([{
        "accepted": True,
        "batch_id": "b" * 32,
        "next_heartbeat_sec": 60,
    }])
    uploaded = []

    def flight_events(events):
        uploaded.extend(events)
        return {"acknowledged_event_ids": [event["event_id"] for event in events]}

    client.flight_events = flight_events
    telemetry = RunnerTelemetry(client, jitter=False, home=tmp_path)
    telemetry._send_once()
    events = uploaded
    starts = [event for event in events if event["event_type"] == "session_started"]
    assert len(starts) == 1
    assert starts[0]["batch_id"] == "b" * 32
