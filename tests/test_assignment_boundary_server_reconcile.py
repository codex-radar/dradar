"""No-model admission checks for an existing exact Fleet batch."""

import json
from types import SimpleNamespace

import pytest

from dradar import assignment_boundary, runloop
from dradar.api_client import ApiError


BATCH = "b" * 32
HELD = "1" * 32
SUBMITTED = "2" * 32
OTHER = "3" * 32


def _assignment(aid, *, batch=BATCH):
    return {
        "assignment_id": aid, "batch_id": batch,
        "task_id": f"task-{aid[0]}", "model": "gpt-6-sol", "effort": "high",
    }


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    path = assignment_boundary.prepare(
        tmp_path, "deep-swe", [_assignment(HELD), _assignment(SUBMITTED)],
        batch_id=BATCH,
    )
    args = SimpleNamespace(
        fleet_pool=True, batch_id=BATCH, resume=True, refill=False,
        expect_assignment=None, forget_assignment_boundary=False,
    )
    return path, args


class Client:
    batch_id = BATCH

    def __init__(self, *, status="submitted", has_submission=True):
        self.status = status
        self.has_submission = has_submission
        self.queried = []

    def assignment_recovery_status(self, aid):
        self.queried.append(aid)
        return {
            **_assignment(aid), "benchmark_id": "deep-swe",
            "status": self.status, "has_submission": self.has_submission,
        }


def test_exact_batch_resume_confirms_only_missing_submitted_id(tmp_path, monkeypatch):
    path, args = _setup(tmp_path, monkeypatch)
    client = Client()

    assert runloop._prepare_assignment_boundary(
        args, client, "deep-swe", [_assignment(HELD)],
    ) == path
    assert client.queried == [SUBMITTED]
    state = json.loads(path.read_text())
    assert set(state["expected"]) == {HELD, SUBMITTED}
    assert state["outcomes"][SUBMITTED]["source"] == "server-confirmed"
    assert state["outcomes"][SUBMITTED]["outcome"] == "submitted"
    assert assignment_boundary.reconcile(path, [_assignment(HELD)]).missing_ids == frozenset()


def test_partial_ten_assignment_batch_keeps_eight_held(tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    ids = [f"{index:032x}" for index in range(1, 11)]
    active = [_assignment(aid) for aid in ids]
    path = assignment_boundary.prepare(tmp_path, "deep-swe", active, batch_id=BATCH)
    args = SimpleNamespace(
        fleet_pool=True, batch_id=BATCH, resume=True, refill=False,
        expect_assignment=None, forget_assignment_boundary=False,
    )
    client = Client()

    assert runloop._prepare_assignment_boundary(args, client, "deep-swe", active[:8]) == path
    report = assignment_boundary.reconcile(path, active[:8])
    assert report.expected_ids == frozenset(ids)
    assert report.settled_ids == frozenset(ids[8:])
    assert report.active_ids == frozenset(ids[:8])
    assert report.missing_ids == frozenset()
    assert client.queried == ids[8:]


def test_mixed_submitted_and_unknown_missing_ids_do_not_partially_settle(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    path = assignment_boundary.prepare(
        tmp_path, "deep-swe", [_assignment(HELD), _assignment(SUBMITTED),
                                  _assignment(OTHER)], batch_id=BATCH,
    )
    before = path.read_bytes()
    args = SimpleNamespace(
        fleet_pool=True, batch_id=BATCH, resume=True, refill=False,
        expect_assignment=None, forget_assignment_boundary=False,
    )

    class Mixed(Client):
        def assignment_recovery_status(self, aid):
            row = super().assignment_recovery_status(aid)
            if aid == OTHER:
                row["status"] = "unknown"
            return row

    with pytest.raises(SystemExit, match="No model was started"):
        runloop._prepare_assignment_boundary(args, Mixed(), "deep-swe", [_assignment(HELD)])
    assert path.read_bytes() == before


@pytest.mark.parametrize("status,has_submission", [
    ("invalid", True), ("released", False), ("expired", False),
    ("submitted", False), ("unknown", True),
])
def test_unconfirmed_missing_id_blocks_without_changing_boundary(
    tmp_path, monkeypatch, status, has_submission,
):
    path, args = _setup(tmp_path, monkeypatch)
    before = path.read_bytes()
    client = Client(status=status, has_submission=has_submission)

    with pytest.raises(SystemExit, match="No model was started"):
        runloop._prepare_assignment_boundary(
            args, client, "deep-swe", [_assignment(HELD)],
        )
    assert path.read_bytes() == before
    assert client.queried == [SUBMITTED]


def test_lost_status_reply_blocks_without_changing_boundary(tmp_path, monkeypatch):
    path, args = _setup(tmp_path, monkeypatch)
    before = path.read_bytes()

    class LostReply(Client):
        def assignment_recovery_status(self, aid):
            raise ApiError("recovery status reply lost")

    with pytest.raises(SystemExit, match="No model was started"):
        runloop._prepare_assignment_boundary(
            args, LostReply(), "deep-swe", [_assignment(HELD)],
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize("remote_batch", [None, OTHER])
def test_old_or_other_batch_server_response_blocks(tmp_path, monkeypatch, remote_batch):
    path, args = _setup(tmp_path, monkeypatch)
    before = path.read_bytes()

    class WrongBatch(Client):
        def assignment_recovery_status(self, aid):
            row = super().assignment_recovery_status(aid)
            if remote_batch is None:
                del row["batch_id"]
            else:
                row["batch_id"] = remote_batch
            return row

    with pytest.raises(SystemExit, match="No model was started"):
        runloop._prepare_assignment_boundary(
            args, WrongBatch(), "deep-swe", [_assignment(HELD)],
        )
    assert path.read_bytes() == before


def test_other_batch_metadata_cannot_be_reconciled(tmp_path, monkeypatch):
    path, args = _setup(tmp_path, monkeypatch)
    state = json.loads(path.read_text())
    state["expected"][SUBMITTED]["batch_id"] = OTHER
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    client = Client()

    with pytest.raises(SystemExit, match="No model was started"):
        runloop._prepare_assignment_boundary(
            args, client, "deep-swe", [_assignment(HELD)],
        )
    assert client.queried == []
    assert path.read_bytes() == before


def test_changed_boundary_is_not_overwritten_after_status_read(tmp_path, monkeypatch):
    path, args = _setup(tmp_path, monkeypatch)

    class RacingClient(Client):
        def assignment_recovery_status(self, aid):
            assignment_boundary.record_outcome(path, _assignment(HELD), "failed")
            return super().assignment_recovery_status(aid)

    with pytest.raises(SystemExit, match="changed during verification"):
        runloop._prepare_assignment_boundary(
            args, RacingClient(), "deep-swe", [_assignment(HELD)],
        )
    assert SUBMITTED not in json.loads(path.read_text())["outcomes"]
