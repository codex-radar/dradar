import json
from types import SimpleNamespace

from dradar import failure_reports, identity


class FailingClient:
    def report_runner_failure(self, payload):
        raise OSError("private transport detail must not be persisted")


class RecordingClient:
    def __init__(self):
        self.payloads = []

    def report_runner_failure(self, payload):
        self.payloads.append(payload)
        return {"status": "received"}


def test_report_builder_drops_arbitrary_and_path_like_values():
    report = failure_reports.build_report(
        source="cli",
        phase="runner",
        failure_kind="runner_failed",
        failure_code="agent_no_artifact",
        assignment_id="assignment-1",
        detail={
            "task_id": "task-1",
            "model": "gpt-5.5",
            "outcome": "/Users/alice/private/result.json",
            "stderr": "Bearer secret-token",
        },
    )

    encoded = json.dumps(report)
    assert report["detail"] == {"task_id": "task-1", "model": "gpt-5.5"}
    assert "alice" not in encoded
    assert "secret-token" not in encoded
    assert report["platform"].count("/") == 0


def test_failed_send_is_atomic_and_later_flushes_without_internal_fields(tmp_path):
    report = failure_reports.build_report(
        source="cli", phase="upload", failure_kind="upload-failed",
        failure_code="upload-failed",
    )

    assert failure_reports.submit_or_queue(FailingClient(), tmp_path, report) == "send-failed"
    queued = failure_reports.pending(tmp_path)
    assert len(queued) == 1
    assert queued[0]["_attempts"] == 1

    client = RecordingClient()
    assert failure_reports.flush_pending(client, tmp_path) == {
        "received": 1, "send_failed": 0,
    }
    assert failure_reports.pending(tmp_path) == []
    assert client.payloads[0]["report_key"] == report["report_key"]
    assert not any(key.startswith("_") for key in client.payloads[0])


def test_status_distinguishes_received_and_failed_delivery(monkeypatch, capsys):
    class StatusClient:
        def my_submissions(self):
            return {"nickname": "vol", "points": 0, "submissions": []}

        def runner_failure_reports(self):
            return {"failure_reports": [{
                "failure_kind": "runner_failed",
                "failure_code": "agent_no_artifact",
                "occurred_at": "2026-09-04T02:00:00+00:00",
                "occurrences": 1,
            }]}

        def get_assignment(self):
            return {}

    monkeypatch.setattr(identity, "_load_config", lambda: {"server": "x", "token": "t"})
    monkeypatch.setattr(identity, "_client", lambda _cfg: StatusClient())
    monkeypatch.setattr(identity.pending, "load", lambda _home: [])
    monkeypatch.setattr(failure_reports, "pending", lambda _home: [{
        "failure_kind": "upload-failed",
        "occurred_at": "2026-09-04T02:00:00+00:00",
        "_attempts": 1,
    }])

    assert identity.cmd_status(SimpleNamespace()) == 0
    output = capsys.readouterr().out
    assert "已反馈" in output
    assert "发送失败，待重试" in output
