"""Saved physical-exit evidence survives lost acknowledgements and restarts."""
import hashlib
import json
from pathlib import Path

import pytest

from dradar import capacity_journal as journal
from dradar.api_client import ApiError

SID, BID, AID, EXEC = "a" * 32, "b" * 32, "c" * 32, "d" * 32
SCOPE = {"assignment_id": AID, "task_id": "task", "batch_id": BID,
         "runner_session_id": SID}


def event(kind, **extra):
    return {"schema": "dradar.execution_audit.v1", "event": kind,
            "execution_id": EXEC, "scope": SCOPE, **extra}


class ReceiptServer:
    server = "https://qa.invalid"

    def __init__(self, *, lose_ack=False):
        self.closed = False
        self.request = None
        self.lose_ack = lose_ack
        self.calls = []

    def runner_close(self, body):
        self.calls.append("close")
        assert body["session_id"] == SID and body["batch_id"] == BID
        self.closed = True

    def release_runner_capacity(self, body):
        self.calls.append("release")
        assert self.closed
        if self.request is not None:
            assert self.request == body
        self.request = json.loads(json.dumps(body))
        if self.lose_ack:
            raise ApiError("synthetic accepted release without acknowledgement")

    def runner_session_receipt(self, session_id, *, batch_id):
        assert (session_id, batch_id) == (SID, BID)
        self.calls.append("receipt")
        return {"session_id": SID, "batch_id": BID, "closed": self.closed,
                "capacity_released": self.request is not None,
                "device_generation": 3, "reservation_protocol": 1,
                "release_evidence_id": self.request["evidence_id"] if self.request else None,
                "release_evidence_sha256": hashlib.sha256(journal._canonical(self.request)).hexdigest() if self.request else None}


def prepare(tmp_path):
    local = journal.CapacityJournal(tmp_path, session_id=SID, server=ReceiptServer.server)
    local.bind(BID)
    return local


@pytest.mark.parametrize("lose_ack", [False, True])
def test_audited_exit_reconciles_same_receipt_after_restart(tmp_path, lose_ack):
    local = prepare(tmp_path)
    observe = local.begin_attempt(SCOPE)
    for kind in ("entered", "launch_pending", "spawned"):
        observe(event(kind))
    observe(event("confirmed_absent", process_group="absent", exact_job_containers="absent"))
    assert local.seal(close_seq=10, reason="completed")
    server = ReceiptServer(lose_ack=lose_ack)
    assert journal.reconcile_saved(tmp_path, server, batch_id=BID)["released"] == 1
    saved = json.loads(local.path.read_text())
    assert saved["released"] is True
    assert server.request["evidence_id"] == saved["release_request"]["evidence_id"]
    assert journal.reconcile_file(local.path, server)
    assert server.calls.count("release") == 1
    with pytest.raises(journal.CapacityEvidenceError):
        local.begin_attempt(SCOPE)


@pytest.mark.parametrize("last_event", [None, "entered", "launch_pending", "spawned", "unknown"])
def test_every_incomplete_attempt_retains_capacity(tmp_path, last_event):
    local = prepare(tmp_path)
    observe = local.begin_attempt(SCOPE)
    if last_event:
        for kind in ("entered", "launch_pending", "spawned", "unknown"):
            observe(event(kind))
            if kind == last_event:
                break
    assert local.seal(close_seq=4, reason="error") is False
    server = ReceiptServer()
    assert journal.reconcile_saved(tmp_path, server, batch_id=BID) == {"released": 0, "pending": 0, "unknown": 1}
    assert server.calls == []


def test_empty_session_requires_created_and_sealed_journal(tmp_path):
    local = prepare(tmp_path)
    assert local.seal(close_seq=1, reason="paused")
    assert journal.reconcile_file(local.path, ReceiptServer())
    with pytest.raises(journal.CapacityEvidenceError):
        journal.reconcile_file(tmp_path / "missing.json", ReceiptServer())


def test_close_commit_with_lost_ack_is_read_back_before_release(tmp_path):
    local = prepare(tmp_path)
    assert local.seal(close_seq=2, reason="completed")
    class LostClose(ReceiptServer):
        def runner_close(self, body):
            super().runner_close(body)
            raise ApiError("accepted close without acknowledgement")
    server = LostClose()
    assert journal.reconcile_file(local.path, server)
    assert server.calls.count("close") == server.calls.count("release") == 1


@pytest.mark.parametrize("bad", [
    event("confirmed_absent", process_group="absent", exact_job_containers="absent"),
    event("entered", scope={**SCOPE, "runner_session_id": "wrong-session"}),
    event("entered", execution_id=None),
])
def test_positive_exit_wording_cannot_replace_missing_identity_or_order(tmp_path, bad):
    local = prepare(tmp_path)
    observer = local.begin_attempt(SCOPE)
    before = local.path.read_bytes()
    with pytest.raises(journal.CapacityEvidenceError):
        observer(bad)
    assert local.path.read_bytes() == before


def test_explicit_prelaunch_failure_can_close_but_spawned_cannot_be_empty(tmp_path):
    local = prepare(tmp_path)
    observer = local.begin_attempt(SCOPE)
    observer(event("entered"))
    observer(event("launch_pending"))
    observer(event("spawned"))
    with pytest.raises(journal.CapacityEvidenceError):
        observer(event("never_started", execution_started=False, reason="popen_failed"))
    assert not local.seal(close_seq=8, reason="error")


@pytest.mark.parametrize("bad_field,bad_value", [("closed", 1), ("capacity_released", 1), ("device_generation", True), ("session_id", "wrong")])
def test_unknown_receipt_shape_never_releases(tmp_path, bad_field, bad_value):
    local = prepare(tmp_path)
    assert local.seal(close_seq=1, reason="completed")
    class BadServer(ReceiptServer):
        def runner_session_receipt(self, *args, **kwargs):
            return {**super().runner_session_receipt(*args, **kwargs), bad_field: bad_value}
    server = BadServer()
    with pytest.raises(journal.CapacityEvidenceError):
        journal.reconcile_file(local.path, server)
    assert "release" not in server.calls
