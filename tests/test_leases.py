from argparse import Namespace

import pytest

from dradar import leases


def _cell(aid, *, started=False):
    return {
        "assignment_id": aid,
        "task_id": f"task-{aid}",
        "model": "gpt-5.6-sol",
        "effort": "low",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "started_at": "2098-12-31T23:00:00+00:00" if started else None,
    }


class FakeClient:
    def __init__(self, active):
        self.active = active
        self.release_calls = []

    def get_assignment(self):
        return {"active": self.active, "free_pick": True}

    def release_assignments(self, assignment_ids=None, *, release_all=False, force=False):
        self.release_calls.append((assignment_ids, release_all, force))
        targets = self.active if release_all else [
            x for x in self.active if x["assignment_id"] in (assignment_ids or [])]
        released, skipped = [], []
        for item in targets:
            basic = {key: item[key] for key in
                     ("assignment_id", "task_id", "model", "effort")}
            if item.get("started_at") and not force:
                skipped.append({**basic, "reason": "running"})
            else:
                released.append({**basic, "was_running": bool(item.get("started_at"))})
        return {"released": released, "skipped": skipped,
                "already_released": [], "held": len(skipped)}


def _wire(monkeypatch, client):
    monkeypatch.setattr(leases, "_load_config", lambda: {})
    monkeypatch.setattr(leases, "_client", lambda cfg: client)


def test_leases_lists_waiting_and_running_with_recovery_hint(monkeypatch, capsys):
    client = FakeClient([_cell("a1"), _cell("a2", started=True)])
    _wire(monkeypatch, client)

    assert leases.cmd_leases(Namespace()) == 0

    out = capsys.readouterr().out
    assert "1 running, 1 waiting" in out
    assert "a1" in out and "a2" in out
    assert "dradar release --all" in out
    assert "--force" in out


def test_started_history_without_healthy_runner_is_resumable_not_running(
    monkeypatch, capsys,
):
    stale = _cell("a1", started=True)
    stale.update({"heartbeat_running": False, "execution_state": "running"})
    live = _cell("a2", started=True)
    live.update({"heartbeat_running": True, "execution_state": "running"})
    paused = _cell("a3", started=True)
    paused.update({"heartbeat_running": False, "execution_state": "paused"})
    client = FakeClient([stale, live, paused])
    _wire(monkeypatch, client)

    assert leases.cmd_leases(Namespace()) == 0

    out = capsys.readouterr().out
    assert "1 running, 1 paused, 1 stale" in out
    assert "a1" in out and "stale" in out
    assert "not actually resumable" in out


def test_release_all_protects_running_without_force(monkeypatch, capsys):
    client = FakeClient([_cell("a1"), _cell("a2", started=True)])
    _wire(monkeypatch, client)
    args = Namespace(assignment_ids=[], all=True, force=False, yes=True)

    assert leases.cmd_release(args) == 0

    assert client.release_calls == [(None, True, False)]
    out = capsys.readouterr().out
    assert "released 1" in out and "kept 1" in out
    assert "--force" in out


def test_release_interactive_selection(monkeypatch):
    client = FakeClient([_cell("a1"), _cell("a2")])
    _wire(monkeypatch, client)
    answers = iter(["2", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    args = Namespace(assignment_ids=[], all=False, force=False, yes=False)

    assert leases.cmd_release(args) == 0
    assert client.release_calls == [(["a2"], False, False)]


def test_release_rejects_ids_plus_all(monkeypatch):
    client = FakeClient([])
    _wire(monkeypatch, client)
    args = Namespace(assignment_ids=["a1"], all=True, force=False, yes=True)
    with pytest.raises(SystemExit, match="not both"):
        leases.cmd_release(args)
