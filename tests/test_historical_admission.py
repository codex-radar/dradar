"""Historical unknown exits can be nonblocking without becoming settled."""

import json
from types import SimpleNamespace

import pytest

from dradar import assignment_boundary, boundary_recovery, runloop


A, B, C = (letter * 32 for letter in "abc")
OLD_BATCH, NEW_BATCH = "d" * 32, "e" * 32


def _assignment(aid, batch):
    return {"assignment_id": aid, "batch_id": batch,
            "benchmark_id": "deep-swe", "task_id": f"task-{aid[0]}",
            "model": "gpt-6-sol", "effort": "high"}


def _response(aid, batch, count):
    return {**_assignment(aid, batch), "recovery_evidence_version": 1,
            "admission_evidence_version": 1, "status": "invalid",
            "has_submission": True, "start_evidence": "unknown_or_started",
            "exit_evidence": "unknown", "admission_evidence": {
                "classification": "historical_unverified", "state": "exit_unknown",
                "closed": True, "counts_toward_capacity": False,
                "all_related_sessions_linked": True,
                "related_session_count": count,
                "result_status": "preserve_unknown",
            }}


class Client:
    benchmark_id = "deep-swe"
    batch_id = None
    plan_scoped = False

    def __init__(self):
        self.rows = {A: _response(A, OLD_BATCH, 1), B: _response(B, OLD_BATCH, 2)}
        self.reads = 0

    def assignment_recovery_status(self, aid):
        self.reads += 1
        value = self.rows[aid]
        if isinstance(value, Exception):
            raise value
        return value


def _fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(boundary_recovery, "_check_processes", lambda _home: None)
    old = [_assignment(A, OLD_BATCH), _assignment(B, OLD_BATCH)]
    path = assignment_boundary.prepare(tmp_path, "deep-swe", old)
    for item in old:
        assignment_boundary.record_outcome(path, item, "failed")
    return path, Client()


def _args(*, pick=True):
    return SimpleNamespace(
        yes=True, allow_new_claims=True, resume=False, batch_id=None,
        pick=["task-c:gpt-6-sol:high"] if pick else None,
        auto=None, refill=False, fleet_pool=False, assignment=None,
        expect_assignment=None, forget_assignment_boundary=False,
    )


def test_historical_unknown_claim_uses_existing_exact_batch_boundary_on_restart(
    tmp_path, monkeypatch,
):
    old_path, client = _fixture(tmp_path, monkeypatch)
    before = old_path.read_bytes()
    fresh = _assignment(C, NEW_BATCH)
    claimed = []
    monkeypatch.setattr(runloop, "_acquire_batch", lambda *_a, **_kw: ([], True))
    monkeypatch.setattr(runloop, "_top_up_picks", lambda *_a, **_kw: claimed.append(C) or [fresh])
    args = _args()
    # cmd_go does this preflight before _go_menu/_prepare_batch. It must not
    # reject the old personal boundary before the fresh admission proof runs.
    assert runloop._prepare_assignment_boundary(args, client, "deep-swe") is None
    assert old_path.read_bytes() == before
    active, _ = runloop._prepare_batch(args, client)
    assert claimed == [C] and active == [fresh]
    new_path = runloop._prepare_assignment_boundary(args, client, "deep-swe", active)
    assert new_path == assignment_boundary.state_path(tmp_path, "deep-swe", NEW_BATCH)
    assert old_path.read_bytes() == before
    assert set(json.loads(old_path.read_text())["expected"]) == {A, B}
    assert set(json.loads(new_path.read_text())["expected"]) == {C}

    # The same saved old outcomes are rechecked after a process restart. The
    # new lease resumes from its exact batch; no second claim or old upload is
    # inferred from the historical evidence.
    monkeypatch.setattr(runloop, "_acquire_batch", lambda *_a, **_kw: ([fresh], True))
    restart = _args(pick=False)
    active, _ = runloop._prepare_batch(restart, client)
    assert runloop._prepare_assignment_boundary(restart, client, "deep-swe", active) == new_path
    assert claimed == [C] and client.reads == 8
    assert old_path.read_bytes() == before


@pytest.mark.parametrize("problem", (
    "old_contract", "offline", "wrong_scope", "no_submission", "modern_unknown",
    "counts_false_only", "open_session", "missing_link", "zero_sessions",
    "new_activity", "one_of_two_unknown", "pending", "other_pending",
    "batch_scoped", "mixed_old_batches", "process",
))
def test_historical_admission_fails_closed_without_changing_old_boundary(
    tmp_path, monkeypatch, problem,
):
    path, client = _fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    if problem == "old_contract":
        client.rows[A].pop("admission_evidence_version")
    elif problem == "offline":
        client.rows[A] = boundary_recovery.ApiError("offline")
    elif problem == "wrong_scope":
        client.rows[A]["batch_id"] = NEW_BATCH
    elif problem == "no_submission":
        client.rows[A]["has_submission"] = False
    elif problem == "modern_unknown":
        client.rows[A]["admission_evidence"]["classification"] = "current_reservation"
    elif problem == "counts_false_only":
        client.rows[A]["admission_evidence"]["classification"] = "unknown"
    elif problem == "open_session":
        client.rows[A]["admission_evidence"]["closed"] = False
    elif problem == "missing_link":
        client.rows[A]["admission_evidence"]["all_related_sessions_linked"] = False
    elif problem == "zero_sessions":
        client.rows[A]["admission_evidence"]["related_session_count"] = 0
    elif problem == "new_activity":
        client.rows[A]["admission_evidence"]["state"] = "active"
    elif problem == "one_of_two_unknown":
        client.rows[B]["exit_evidence"] = "unknown"
        client.rows[B]["admission_evidence"]["classification"] = "unknown"
    elif problem == "pending":
        (tmp_path / "pending_uploads.json").write_text(json.dumps([{"assignment_id": A}]))
    elif problem == "other_pending":
        (tmp_path / "pending_uploads.json").write_text(json.dumps([{"assignment_id": C}]))
    elif problem == "batch_scoped":
        client.batch_id = NEW_BATCH
    elif problem == "mixed_old_batches":
        state, digest = assignment_boundary.snapshot(path)
        state["expected"][B]["batch_id"] = NEW_BATCH
        with pytest.raises(boundary_recovery.RecoveryBlocked):
            boundary_recovery.historical_unknown_allows_claim(
                client, state, digest, path, tmp_path,
            )
        assert path.read_bytes() == before
        return
    elif problem == "process":
        monkeypatch.setattr(boundary_recovery, "_check_processes", lambda _home: (
            _ for _ in ()).throw(boundary_recovery.RecoveryBlocked("runner active")))
    state, digest = assignment_boundary.snapshot(path)
    with pytest.raises(boundary_recovery.RecoveryBlocked):
        boundary_recovery.historical_unknown_allows_claim(client, state, digest, path, tmp_path)
    assert path.read_bytes() == before


def test_historical_proof_cannot_start_continuous_refill(tmp_path, monkeypatch):
    path, client = _fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    args = _args()
    args.refill = True
    monkeypatch.setattr(runloop, "_acquire_batch", lambda *_a, **_kw: pytest.fail(
        "continuous refill reached the claim path"))
    with pytest.raises(SystemExit, match="continuous refill"):
        runloop._prepare_batch(args, client)
    assert path.read_bytes() == before


def test_real_preflight_defers_but_old_contract_blocks_before_claim(tmp_path, monkeypatch):
    path, client = _fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    args = _args()
    assert runloop._prepare_assignment_boundary(args, client, "deep-swe") is None
    client.rows[A].pop("admission_evidence_version")
    monkeypatch.setattr(runloop, "_acquire_batch", lambda *_a, **_kw: pytest.fail(
        "old Server contract reached a claim"))
    with pytest.raises(SystemExit, match="No new assignment was claimed"):
        runloop._prepare_batch(args, client)
    assert path.read_bytes() == before


def test_historical_proof_does_not_chase_new_free_picks(monkeypatch, tmp_path):
    fresh = _assignment(C, NEW_BATCH)
    args = _args(pick=False)
    args.yes = False
    args._historical_admission_digest = "old-proof"
    monkeypatch.setattr(runloop, "_prepare_batch", lambda *_a: ([fresh], True))
    monkeypatch.setattr(runloop, "_prepared_batch_ids", lambda *_a: [])
    monkeypatch.setattr(runloop, "_prepare_assignment_boundary", lambda *_a: tmp_path / "new.json")
    monkeypatch.setattr(assignment_boundary, "add_expected", lambda *_a: None)
    monkeypatch.setattr(runloop, "_run_batch", lambda *_a, **_kw: 0)
    monkeypatch.setattr(runloop, "_acquire_batch", lambda *_a, **_kw: pytest.fail(
        "a second claim needs a new invocation and proof"))
    assert runloop._go_menu(args, {"benchmark": "deep-swe"}, Client(), tmp_path) == 0


def test_prior_nonblocking_read_is_never_cached(tmp_path, monkeypatch):
    path, client = _fixture(tmp_path, monkeypatch)
    state, digest = assignment_boundary.snapshot(path)
    assert boundary_recovery.historical_unknown_allows_claim(
        client, state, digest, path, tmp_path,
    ) == 2
    client.rows[B]["admission_evidence"]["counts_toward_capacity"] = True
    with pytest.raises(boundary_recovery.RecoveryBlocked):
        boundary_recovery.historical_unknown_allows_claim(client, state, digest, path, tmp_path)
    assert client.reads == 4
