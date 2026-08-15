"""`dradar status`: prints the volunteer's own recent submissions/points/flags
(fetched from GET /api/v1/my-submissions on the server) plus a nudge toward
`retry-upload` when local pending uploads exist.
"""

from dradar import identity


class FakeStatusClient:
    def __init__(self, payload, active=None):
        self.payload = payload
        self.active = active or []
    def my_submissions(self):
        return self.payload
    def get_assignment(self):
        return {"active": self.active}


def test_cmd_status_prints_summary(monkeypatch, capsys, tmp_path):
    from dradar.api_client import ApiError
    payload = {
        "nickname": "vol-abc", "points": 42.5,
        "submissions": [
            {"submission_id": "s1", "task_id": "ytt-jsonpath-query-api", "model": "gpt-5.5",
             "effort": "medium", "submitted_at": "2026-07-07T10:00:00+00:00",
             "graded_at": "2026-07-07T10:05:00+00:00", "grade_status": "graded",
             "reward": 1.0, "flags": [], "public": True},
            {"submission_id": "s2", "task_id": "abs-module-cache-flags", "model": "gpt-5.5",
             "effort": "high", "submitted_at": "2026-07-07T09:00:00+00:00",
             "graded_at": None, "grade_status": "error", "reward": None,
             "flags": [], "public": False},
        ],
    }
    monkeypatch.setattr(identity, "_load_config", lambda: {"server": "https://x", "token": "t"})
    monkeypatch.setattr(identity, "_client", lambda cfg: FakeStatusClient(payload))
    monkeypatch.setattr(identity.pending, "load", lambda home: [])
    from types import SimpleNamespace
    rc = identity.cmd_status(SimpleNamespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "vol-abc" in out and "42.5" in out
    assert "ytt-jsonpath-query-api" in out and "graded" in out
    assert "abs-module-cache-flags" in out and "error" in out
    assert "infra hiccup" in out


def test_cmd_status_notes_local_pending(monkeypatch, capsys):
    monkeypatch.setattr(identity, "_load_config", lambda: {"server": "https://x", "token": "t"})
    monkeypatch.setattr(identity, "_client",
                        lambda cfg: FakeStatusClient({"nickname": "v", "points": 0, "submissions": []}))
    monkeypatch.setattr(identity.pending, "load", lambda home: [{"assignment_id": "a1"}])
    from types import SimpleNamespace
    identity.cmd_status(SimpleNamespace())
    assert "retry-upload" in capsys.readouterr().out


def test_cmd_status_makes_lease_recovery_commands_discoverable(monkeypatch, capsys):
    payload = {"nickname": "v", "points": 0, "submissions": []}
    active = [
        {"assignment_id": "a1", "started_at": None},
        {"assignment_id": "a2", "started_at": "2026-07-15T00:00:00+00:00"},
    ]
    monkeypatch.setattr(identity, "_load_config", lambda: {"server": "https://x", "token": "t"})
    monkeypatch.setattr(identity, "_client", lambda cfg: FakeStatusClient(payload, active))
    monkeypatch.setattr(identity.pending, "load", lambda home: [])
    from types import SimpleNamespace

    identity.cmd_status(SimpleNamespace())

    out = capsys.readouterr().out
    assert "1 running, 1 waiting" in out
    assert "dradar leases" in out and "dradar release" in out


def test_cmd_status_uses_live_runner_health_not_historical_started_at(
    monkeypatch, capsys,
):
    payload = {"nickname": "v", "points": 0, "submissions": []}
    active = [
        {"assignment_id": "a1", "started_at": "2026-07-15T00:00:00+00:00",
         "heartbeat_running": False, "execution_state": "running"},
        {"assignment_id": "a2", "started_at": "2026-07-15T00:00:00+00:00",
         "heartbeat_running": True, "execution_state": "running"},
    ]
    monkeypatch.setattr(identity, "_load_config", lambda: {"server": "https://x", "token": "t"})
    monkeypatch.setattr(identity, "_client", lambda cfg: FakeStatusClient(payload, active))
    monkeypatch.setattr(identity.pending, "load", lambda home: [])
    from types import SimpleNamespace

    identity.cmd_status(SimpleNamespace())

    assert "1 running, 1 stale" in capsys.readouterr().out
