import json
import pytest

from dradar.worker_events import (
    WORKER_EVENT_FILE_ENV,
    WorkerRegistrationTracker,
    emit_worker_registered,
    parse_worker_event,
    read_worker_event,
)


def _event(seq=1):
    return {
        "protocol_version": 1,
        "event": "worker_registered",
        "session_id": "session-1234",
        "client_seq": seq,
        "runtime": "pier",
        "context": "agent",
        "profile": "custom",
        "occurred_at_ms": 1000,
    }


def test_parser_drops_untrusted_fields():
    value = {**_event(), "stderr": "token=secret"}
    parsed = parse_worker_event(value)
    assert parsed is not None
    assert not hasattr(parsed, "stderr")
    assert parse_worker_event({**value, "event": "log_line"}) is None


def test_tracker_is_idempotent_and_rejects_other_sessions():
    tracker = WorkerRegistrationTracker(session_id="session-1234", started_at=10.0)
    assert tracker.observe(_event())
    assert not tracker.observe(_event())
    assert not tracker.observe({**_event(2), "session_id": "other-session"})
    assert tracker.registered.client_seq == 1


def test_sidecar_round_trip_is_structured_and_bounded(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv(WORKER_EVENT_FILE_ENV, str(path))
    monkeypatch.setenv("DRADAR_RUNNER_SESSION_ID", "session-1234")
    assert emit_worker_registered(runtime="pier", context="agent", profile="custom")
    raw, offset = read_worker_event(path)
    assert parse_worker_event(raw).event_id == "session-1234:1"
    assert offset == path.stat().st_size
    assert json.loads(path.read_text()) ["event"] == "worker_registered"


def test_missing_signal_is_unknown_not_started():
    tracker = WorkerRegistrationTracker(session_id="session-1234", started_at=10.0)
    assert tracker.finish(now=20.0, timed_out=True) == {
        "result": "unknown", "reason_code": "unobserved_timeout", "duration_ms": None,
    }


def test_registration_wait_uses_exact_thirty_minute_grace(monkeypatch, tmp_path):
    import dradar.runner as runner

    class LiveProcess:
        def poll(self):
            return None

    ticks = iter((0.0, 1799.0, 1800.1))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    with pytest.raises(runner.RunnerError, match="grace window"):
        runner._wait_for_worker_registration(
            LiveProcess(), tmp_path / "events.jsonl",
            environment_build_timeout_multiplier=8.0,
            worker_event_source=lambda: None,
        )
    assert runner.WORKER_REGISTRATION_GRACE_SEC == 1800
