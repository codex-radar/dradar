"""Offline evidence for the two strict registration failure boundaries."""

import json
import os
import time

import pytest

from dradar import failure_reports, runner
from dradar.api_client import ApiError
from dradar.telemetry import RunnerTelemetry


SESSION = "a" * 32
BATCH = "b" * 32
ASSIGNMENT = "c" * 32


class Client:
    def __init__(self, flight_reply=None, heartbeat_reply=None):
        self.flight_reply = flight_reply
        self.heartbeat_reply = heartbeat_reply or {"accepted": True, "batch_id": BATCH}
        self.events = []

    def runner_heartbeat(self, _payload):
        if isinstance(self.heartbeat_reply, Exception):
            raise self.heartbeat_reply
        return self.heartbeat_reply

    def flight_events(self, events):
        self.events.extend(events)
        if isinstance(self.flight_reply, Exception):
            raise self.flight_reply
        if self.flight_reply is None:
            return {"acknowledged_event_ids": [event["event_id"] for event in events]}
        return self.flight_reply


def telemetry_with_worker(tmp_path, client):
    telemetry = RunnerTelemetry(client, jitter=False, home=tmp_path)
    telemetry.bind_batch(BATCH)
    telemetry.set_phase("building", ASSIGNMENT)
    worker = telemetry.record_event(
        "worker_registered", component="provider", assignment_id=ASSIGNMENT,
        attributes={"provider": "codex"},
    )
    return telemetry, worker["event_id"]


@pytest.mark.parametrize("status,expected", [
    (0, {"process_exit_code": 0}),
    (17, {"process_exit_code": 17}),
    (-9, {"process_signal": 9}),
])
def test_actual_process_exit_reports_only_bounded_status(
    tmp_path, monkeypatch, status, expected,
):
    class Pier:
        def poll(self):
            return status

    monkeypatch.setattr(runner.time, "monotonic", iter((10.0, 17.9)).__next__)
    with pytest.raises(runner.RunnerError) as raised:
        runner._wait_for_worker_registration(
            Pier(), tmp_path / "event", environment_build_timeout_multiplier=1,
            worker_event_source=lambda: None, expected_session_id=SESSION,
        )
    assert raised.value.report_code == "worker-registration-process-exited"
    assert raised.value.report_detail == {
        "registration_result": "process_exited", "registration_elapsed_sec": 7,
        "session_id": SESSION, **expected,
    }


def test_exit_zero_shows_only_fresh_local_trial_exception_type(
    tmp_path, capsys,
):
    job = tmp_path / "jobs" / f"a{ASSIGNMENT}"
    trial = job / "task__1"
    trial.mkdir(parents=True)
    launched = time.time_ns()
    (trial / "result.json").write_text(json.dumps({
        "exception_info": {
            "exception_type": "RuntimeError",
            "exception_message": "SECRET credential at /private/user/task",
            "exception_traceback": "SECRET traceback",
        },
    }))

    class Exited:
        def poll(self):
            return 0

    with pytest.raises(runner.RunnerError) as raised:
        runner._wait_for_worker_registration(
            Exited(), tmp_path / "missing-sidecar",
            environment_build_timeout_multiplier=1,
            expected_session_id=SESSION, job_dir=job,
            launch_started_ns=launched,
        )
    output = capsys.readouterr().out
    assert "Pier trial recorded exception type: RuntimeError" in output
    assert str(trial / "result.json") in output
    assert "SECRET" not in output + str(raised.value)
    assert raised.value.report_detail == {
        "registration_result": "process_exited", "registration_elapsed_sec": 0,
        "session_id": SESSION, "process_exit_code": 0,
    }
    report = failure_reports.build_report(
        source="cli", phase="runner", failure_kind="runner_failed",
        failure_code=raised.value.report_code,
        detail=raised.value.report_detail | {"exception_message": "SECRET"},
    )
    assert "exception_message" not in report["detail"]
    assert "RuntimeError" not in json.dumps(report)


@pytest.mark.parametrize("case", [
    "missing", "malformed", "stale", "ambiguous", "untrusted-type",
    "no-exception", "invalid-exception-type",
])
def test_registration_result_hint_fails_closed_for_uncertain_local_evidence(
    tmp_path, case,
):
    job = tmp_path / "jobs" / f"a{ASSIGNMENT}"
    trial = job / "task__1"
    trial.mkdir(parents=True)
    launched = time.time_ns()
    result = trial / "result.json"
    if case == "malformed":
        result.write_text("{not json")
    elif case == "stale":
        result.write_text('{"exception_info":{"exception_type":"RuntimeError"}}')
        os.utime(result, ns=(launched - 1_000_000_000, launched - 1_000_000_000))
    elif case == "ambiguous":
        result.write_text('{"exception_info":{"exception_type":"RuntimeError"}}')
        other = job / "other__2"
        other.mkdir()
        (other / "result.json").write_text(result.read_text())
    elif case == "untrusted-type":
        result.write_text(json.dumps({"exception_info": {
            "exception_type": "SECRET_CUSTOM_TYPE", "exception_message": "SECRET"
        }}))
    elif case == "no-exception":
        result.write_text('{"exception_info":null}')
    elif case == "invalid-exception-type":
        result.write_text('{"exception_info":{"exception_type":["SECRET"]}}')
    hint = runner._fresh_registration_trial_hint(job, launched)
    assert hint == ""


def test_nonzero_process_exit_does_not_show_trial_hint(tmp_path, capsys):
    job = tmp_path / "jobs" / f"a{ASSIGNMENT}"
    trial = job / "task__1"
    trial.mkdir(parents=True)
    launched = time.time_ns()
    (trial / "result.json").write_text(
        '{"exception_info":{"exception_type":"RuntimeError"}}'
    )

    class Exited:
        def poll(self):
            return 17

    with pytest.raises(runner.RunnerError) as raised:
        runner._wait_for_worker_registration(
            Exited(), tmp_path / "missing-sidecar",
            environment_build_timeout_multiplier=1,
            expected_session_id=SESSION, job_dir=job,
            launch_started_ns=launched,
        )
    assert raised.value.report_detail["process_exit_code"] == 17
    assert capsys.readouterr().out == ""


def test_wrong_session_sidecar_cannot_register_even_with_current_trial_result(
    tmp_path,
):
    job = tmp_path / "jobs" / f"a{ASSIGNMENT}"
    trial = job / "task__1"
    trial.mkdir(parents=True)
    launched = time.time_ns()
    (trial / "result.json").write_text(
        '{"exception_info":{"exception_type":"RuntimeError"}}'
    )
    foreign = {
        "protocol_version": 1, "event": "worker_registered",
        "session_id": "d" * 32, "client_seq": 1,
        "occurred_at_ms": 1, "runtime": "pier",
        "context": "agent", "profile": "provider",
    }

    class Exited:
        def poll(self):
            return 0

    with pytest.raises(runner.RunnerError) as raised:
        runner._wait_for_worker_registration(
            Exited(), tmp_path / "sidecar",
            environment_build_timeout_multiplier=1,
            worker_event_source=lambda: foreign,
            expected_session_id=SESSION, job_dir=job,
            launch_started_ns=launched,
        )
    assert raised.value.report_code == "worker-registration-process-exited"
    assert raised.value.report_detail["session_id"] == SESSION


def test_valid_current_worker_event_wins_over_exit_zero_and_has_no_failure_hint(
    tmp_path, capsys,
):
    job = tmp_path / "jobs" / f"a{ASSIGNMENT}"
    trial = job / "task__1"
    trial.mkdir(parents=True)
    launched = time.time_ns()
    (trial / "result.json").write_text(
        '{"exception_info":{"exception_type":"RuntimeError"}}'
    )
    current = {
        "protocol_version": 1, "event": "worker_registered",
        "session_id": SESSION, "client_seq": 1,
        "occurred_at_ms": 1, "runtime": "pier",
        "context": "agent", "profile": "provider",
    }

    class Exited:
        def poll(self):
            return 0

    event = runner._wait_for_worker_registration(
        Exited(), tmp_path / "sidecar",
        environment_build_timeout_multiplier=1,
        worker_event_source=lambda: current,
        expected_session_id=SESSION, job_dir=job,
        launch_started_ns=launched,
    )
    assert event["event"] == "worker_registered"
    assert event["session_id"] == SESSION
    assert capsys.readouterr().out == ""


def test_corrupt_worker_event_does_not_register_or_leak_raw_content(
    tmp_path, capsys,
):
    class Exited:
        def poll(self):
            return 0

    with pytest.raises(runner.RunnerError) as raised:
        runner._wait_for_worker_registration(
            Exited(), tmp_path / "sidecar",
            environment_build_timeout_multiplier=1,
            worker_event_source=lambda: '{"event":"worker_registered","secret":"SECRET"',
            expected_session_id=SESSION,
        )
    assert raised.value.report_code == "worker-registration-process-exited"
    assert "SECRET" not in capsys.readouterr().out + str(raised.value)


def test_poll_observation_error_is_not_mislabeled_as_process_exit(tmp_path):
    class Pier:
        def poll(self):
            raise OSError("SECRET_POLL_MESSAGE")

    with pytest.raises(OSError, match="SECRET_POLL_MESSAGE"):
        runner._wait_for_worker_registration(
            Pier(), tmp_path / "event", environment_build_timeout_multiplier=1,
            worker_event_source=lambda: None,
        )


@pytest.mark.parametrize("reply,expected", [
    ({"acknowledged_event_ids": []}, "flight_target_not_acknowledged"),
    ({"acknowledged_event_ids": "SECRET"}, "flight_invalid_response"),
    (ApiError("SECRET", status_code=429), "flight_http_error"),
    (OSError("SECRET"), "flight_transport_error"),
])
def test_registration_ack_failure_keeps_exact_event_pending(
    tmp_path, reply, expected,
):
    telemetry, worker_id = telemetry_with_worker(tmp_path, Client(flight_reply=reply))
    assert telemetry.flush_for_worker_registration(worker_id) is False
    detail = telemetry.worker_registration_diagnostic
    assert detail["registration_result"] == expected
    assert detail["worker_event_id"] == worker_id
    assert detail["session_id"] == telemetry.session_id
    if expected == "flight_http_error":
        assert detail["ack_http_status"] == 429
    assert worker_id not in telemetry.flight_recorder.last_acknowledged_event_ids
    pending = telemetry.flight_recorder._load(telemetry.flight_recorder.pending_path)
    assert worker_id in {event["event_id"] for event in pending}
    assert "SECRET" not in json.dumps(detail)


def test_registration_ack_persist_failure_is_distinct(tmp_path, monkeypatch):
    telemetry, worker_id = telemetry_with_worker(tmp_path, Client())
    monkeypatch.setattr(
        telemetry.flight_recorder, "_write_acknowledged_ids_unlocked",
        lambda _ids: (_ for _ in ()).throw(OSError("SECRET_STORAGE")),
    )
    assert telemetry.flush_for_worker_registration(worker_id) is False
    assert telemetry.worker_registration_diagnostic["registration_result"] == "flight_ack_persist_error"
    assert worker_id not in telemetry.flight_recorder.last_acknowledged_event_ids
    assert "SECRET" not in json.dumps(telemetry.worker_registration_diagnostic)


def test_success_and_rejected_heartbeat_remain_distinct(tmp_path):
    success, event_id = telemetry_with_worker(tmp_path / "ok", Client())
    assert success.flush_for_worker_registration(event_id) is True
    assert success.worker_registration_diagnostic["registration_result"] == "flight_target_acknowledged"

    rejected, event_id = telemetry_with_worker(
        tmp_path / "rejected", Client(heartbeat_reply={"accepted": False}),
    )
    assert rejected.flush_for_worker_registration(event_id) is False
    assert rejected.worker_registration_diagnostic["registration_result"] == "heartbeat_rejected"


def test_heartbeat_http_error_keeps_existing_exception_and_records_only_status(tmp_path):
    telemetry, worker_id = telemetry_with_worker(
        tmp_path, Client(heartbeat_reply=ApiError("SECRET_SERVER_BODY", status_code=503)),
    )
    with pytest.raises(ApiError):
        telemetry.flush_for_worker_registration(worker_id)
    assert telemetry.worker_registration_diagnostic == {
        "session_id": telemetry.session_id, "worker_event_id": worker_id,
        "registration_result": "heartbeat_http_error", "ack_http_status": 503,
    }


def test_failure_report_drops_non_allowlisted_diagnostics_and_secrets():
    report = failure_reports.build_report(
        source="cli", phase="runner", failure_kind="runner_failed",
        failure_code="worker-registration-unacknowledged",
        detail={
            "session_id": SESSION, "worker_event_id": "d" * 32,
            "registration_result": "flight_invalid_response",
            "ack_http_status": 502, "process_exit_code": -999,
            "stderr": "SECRET_STDERR", "command": "SECRET_COMMAND",
            "response_body": "SECRET_RESPONSE", "environment": "SECRET_ENV",
        },
    )
    assert report["detail"] == {
        "session_id": SESSION, "worker_event_id": "d" * 32,
        "registration_result": "flight_invalid_response", "ack_http_status": "502",
    }
    assert "SECRET" not in json.dumps(report)


def test_older_server_receives_same_report_key_without_new_fields(tmp_path):
    class LegacyServer:
        def __init__(self):
            self.attempts = []

        def report_runner_failure(self, payload):
            self.attempts.append(payload)
            if "registration_result" in payload["detail"]:
                raise ApiError("old detail schema", status_code=422,
                               payload={"detail": "failure report detail has unsupported fields"})
            return {"status": "received"}

    report = failure_reports.build_report(
        source="cli", phase="runner", failure_kind="runner_failed",
        failure_code="worker-registration-process-exited",
        detail={"task_id": "task-1", "registration_result": "process_exited",
                "process_exit_code": 17, "session_id": SESSION},
    )
    client = LegacyServer()
    assert failure_reports.submit_or_queue(client, tmp_path, report) == "received"
    assert len(client.attempts) == 2
    assert client.attempts[0]["report_key"] == client.attempts[1]["report_key"]
    assert client.attempts[1]["detail"] == {"task_id": "task-1"}
    assert failure_reports.pending(tmp_path) == []
