"""Historical unknown exits can be nonblocking without becoming settled."""

import json
from types import SimpleNamespace
from pathlib import Path

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


def _reviewed_response(aid, count):
    row = _response(aid, OLD_BATCH, count)
    row["admission_evidence_version"] = 2
    row["admission_evidence"] = {
        "classification": "batch_admission_reviewed", "state": "exit_unknown",
        "closed": False, "counts_toward_capacity": True,
        "all_related_sessions_linked": False, "related_session_count": count,
        "all_batch_sessions_reviewed": True, "batch_session_count": 8,
        "unlinked_session_count": 5, "counted_session_count": 3,
        "physical_exit": "unknown", "result_status": "preserve_unknown",
        "operation_id": "0227-original-reviewed", "manifest_sha256": "f" * 64,
        "batch_id": OLD_BATCH, "result_assignment_ids": [A, B],
    }
    return row


class Client:
    benchmark_id = "deep-swe"
    batch_id = None
    plan_scoped = False

    def __init__(self):
        self.rows = {A: _response(A, OLD_BATCH, 1), B: _response(B, OLD_BATCH, 2)}
        self.reads = 0

    def run_plan_capabilities(self):
        return {"capabilities": ["explicit-pick-batch-v1"]}

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
    acquisition_options = []
    monkeypatch.setattr(runloop, "_acquire_batch", lambda *_a, **kw: (
        acquisition_options.append(kw) or [], True))
    monkeypatch.setattr(runloop, "_top_up_picks", lambda *_a, **_kw: claimed.append(C) or [fresh])
    args = _args()
    # cmd_go does this preflight before _go_menu/_prepare_batch. It must not
    # reject the old personal boundary before the fresh admission proof runs.
    assert runloop._prepare_assignment_boundary(args, client, "deep-swe") is None
    assert old_path.read_bytes() == before
    active, _ = runloop._prepare_batch(args, client)
    assert claimed == [C] and active == [fresh]
    assert acquisition_options == []  # Fresh pick never reads/reuses other held work.
    new_path = runloop._prepare_assignment_boundary(args, client, "deep-swe", active)
    assert new_path == assignment_boundary.state_path(tmp_path, "deep-swe", NEW_BATCH)
    assert old_path.read_bytes() == before
    assert set(json.loads(old_path.read_text())["expected"]) == {A, B}
    assert set(json.loads(new_path.read_text())["expected"]) == {C}

    # The same saved old outcomes are rechecked after a process restart. The
    # new lease resumes from its exact batch; no second claim or old upload is
    # inferred from the historical evidence.
    monkeypatch.setattr(runloop, "_acquire_batch", lambda *_a, **_kw: ([fresh], True))
    client.batch_id = None  # A restarted personal invocation has a fresh client.
    restart = _args(pick=False)
    active, _ = runloop._prepare_batch(restart, client)
    assert runloop._prepare_assignment_boundary(restart, client, "deep-swe", active) == new_path
    assert claimed == [C] and client.reads == 8
    assert old_path.read_bytes() == before


def test_reviewed_batch_proof_sets_exact_claim_reference_without_settling(tmp_path, monkeypatch):
    path, client = _fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    client.rows = {A: _reviewed_response(A, 1), B: _reviewed_response(B, 2)}
    state, digest = assignment_boundary.snapshot(path)
    assert boundary_recovery.historical_unknown_allows_claim(
        client, state, digest, path, tmp_path) == 2
    assert client.historical_admission_reference == "0227-original-reviewed:" + "f" * 64
    assert path.read_bytes() == before


@pytest.mark.parametrize('sessions,unlinked,counted', ((4, 1, 1), (3, 0, 0), (7, 4, 4)))
def test_reviewed_batch_accepts_other_consistent_reviewed_counts(
    tmp_path, monkeypatch, sessions, unlinked, counted,
):
    path, client = _fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    client.rows = {A: _reviewed_response(A, 1), B: _reviewed_response(B, 2)}
    for row in client.rows.values():
        row['admission_evidence'].update(
            batch_session_count=sessions, unlinked_session_count=unlinked,
            counted_session_count=counted, counts_toward_capacity=counted > 0,
            all_related_sessions_linked=unlinked == 0)
    state, digest = assignment_boundary.snapshot(path)
    assert boundary_recovery.historical_unknown_allows_claim(
        client, state, digest, path, tmp_path) == 2
    assert client.historical_admission_reference == '0227-original-reviewed:' + 'f' * 64
    assert path.read_bytes() == before


@pytest.mark.parametrize("drift", (
    "partial_boundary", "missing_result", "different_operation", "counted_false",
    "not_reviewed", "physical_exit_claimed", "different_batch", "different_counts",
    "linked_true", "linked_count_impossible", "counted_exceeds", "unlinked_exceeds",
))
def test_reviewed_batch_proof_fails_closed_on_incomplete_scope(tmp_path, monkeypatch, drift):
    path, client = _fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    client.rows = {A: _reviewed_response(A, 1), B: _reviewed_response(B, 2)}
    state, digest = assignment_boundary.snapshot(path)
    if drift == "partial_boundary":
        state["expected"].pop(B)
    elif drift == "missing_result":
        client.rows[A]["admission_evidence"]["result_assignment_ids"] = [A]
    elif drift == "different_operation":
        client.rows[B]["admission_evidence"]["operation_id"] = "another-reviewed-op"
    elif drift == "counted_false":
        client.rows[A]["admission_evidence"]["counts_toward_capacity"] = False
    elif drift == "not_reviewed":
        client.rows[A]["admission_evidence"]["all_batch_sessions_reviewed"] = False
    elif drift == "physical_exit_claimed":
        client.rows[A]["admission_evidence"]["physical_exit"] = "confirmed"
    elif drift == "different_batch":
        client.rows[A]["admission_evidence"]["batch_id"] = NEW_BATCH
    elif drift == "different_counts":
        client.rows[B]["admission_evidence"]["counted_session_count"] = 2
    elif drift == "linked_true":
        client.rows[A]["admission_evidence"]["all_related_sessions_linked"] = True
    elif drift == "linked_count_impossible":
        client.rows[A]["admission_evidence"]["related_session_count"] = 4
    elif drift == "counted_exceeds":
        client.rows[A]["admission_evidence"]["counted_session_count"] = 9
    elif drift == "unlinked_exceeds":
        client.rows[A]["admission_evidence"]["unlinked_session_count"] = 9
    with pytest.raises(boundary_recovery.RecoveryBlocked):
        boundary_recovery.historical_unknown_allows_claim(
            client, state, digest, path, tmp_path)
    assert client.historical_admission_reference is None
    assert path.read_bytes() == before


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


def _finite_cells(n=20):
    return [
        {**_assignment(f"{i + 100:032x}", NEW_BATCH), "task_id": f"task-{i}"}
        for i in range(n)
    ]


@pytest.mark.parametrize("retained", (False, True))
@pytest.mark.parametrize("selection", ("pick", "auto"))
def test_historical_proof_admits_finite_twenty_in_one_batch(
    tmp_path, monkeypatch, selection, retained,
):
    path, client = (_retained_eighteen if retained else _fixture)(tmp_path, monkeypatch)
    before = path.read_bytes()
    cells = _finite_cells()
    args = _args()
    args.workers = 20
    if selection == "pick":
        args.pick = [f"task-{i}:gpt-6-sol:high" for i in range(20)]
    else:
        args.pick = None
        args.auto = 20
        client.suggest = lambda n: {"cells": [
            {"task_id": f"task-{i}", "model": "gpt-6-sol", "effort": "high"}
            for i in range(n)
        ]}
    monkeypatch.setattr(runloop, "_acquire_batch", lambda *_a, **kw: (
        [], True) if kw["allow_new_claims"] is False else pytest.fail(
            "menu claimed outside the explicit finite selection"))
    claimed = []
    monkeypatch.setattr(runloop, "_claim_cell", lambda *_a, **_kw: (
        claimed.append(cells[len(claimed)]) or claimed[-1]))
    active, _ = runloop._prepare_batch(args, client)
    assert active == cells and len(claimed) == args.workers == 20
    assert runloop._prepared_batch_ids(active) == []
    assert client.reads == 42  # initial proof and fresh proof before all 20 claims
    new_path = runloop._prepare_assignment_boundary(args, client, "deep-swe", active)
    assert new_path == assignment_boundary.state_path(tmp_path, "deep-swe", NEW_BATCH)
    assert set(json.loads(new_path.read_text())["expected"]) == {
        cell["assignment_id"] for cell in cells
    }
    assert client.reads == 44  # fresh proof again after all claims
    assert path.read_bytes() == before
    monkeypatch.setattr(runloop, "_worker_entrypoint", lambda: ["dradar"])
    args.keep = False
    args.allow_task_drift = False
    args.dev_agent = None
    runloop._scope_historical_worker_pool(args, client, active)
    assert args.batch_id == client.batch_id == NEW_BATCH
    command = runloop._worker_command(args)
    assert command[command.index("--batch-id") + 1] == NEW_BATCH
    assert command[command.index("--workers") + 1] == "1"


def test_historical_fresh_pick_excludes_held_batch_with_fresh_reads(tmp_path, monkeypatch):
    path, client = _fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    cells = _finite_cells(3)
    args = _args()
    args.pick = [f"task-{i}:gpt-6-sol:high" for i in range(1, 3)]
    monkeypatch.setattr(runloop, "_acquire_batch", lambda *_a, **kw: (
        [cells[0]], True) if kw["allow_new_claims"] is False else pytest.fail(
            "menu claimed before explicit top-up"))
    claimed = []
    monkeypatch.setattr(runloop, "_claim_cell", lambda *_a, **_kw: (
        claimed.append(cells[len(claimed) + 1]) or claimed[-1]))
    active, _ = runloop._prepare_batch(args, client)
    assert active == cells[1:] and len(claimed) == 2
    assert client.reads == 6
    assert path.read_bytes() == before


@pytest.mark.parametrize("retained", (False, True))
@pytest.mark.parametrize("failure", ("evidence_flip", "response_unknown", "cross_batch"))
def test_finite_selection_stops_after_partial_claims_without_starting(
    tmp_path, monkeypatch, failure, retained,
):
    path, client = (_retained_eighteen if retained else _fixture)(tmp_path, monkeypatch)
    before = path.read_bytes()
    cells = _finite_cells(5)
    args = _args()
    args.pick = [f"task-{i}:gpt-6-sol:high" for i in range(5)]
    monkeypatch.setattr(runloop, "_acquire_batch", lambda *_a, **_kw: ([], True))
    claimed = []

    def claim(*_a, **_kw):
        if failure == "response_unknown" and len(claimed) == 2:
            raise boundary_recovery.ApiError("claim response unknown")
        item = dict(cells[len(claimed)])
        if failure == "cross_batch" and len(claimed) == 1:
            item["batch_id"] = "f" * 32
        claimed.append(item)
        if failure == "evidence_flip" and len(claimed) == 3:
            client.rows[B]["admission_evidence"]["state"] = "active"
        return item

    monkeypatch.setattr(runloop, "_claim_cell", claim)
    with pytest.raises(SystemExit):
        runloop._prepare_batch(args, client)
    assert len(claimed) == {"evidence_flip": 3, "response_unknown": 2,
                            "cross_batch": 2}[failure]
    assert path.read_bytes() == before
    assert not assignment_boundary.state_path(tmp_path, "deep-swe", NEW_BATCH).exists()


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


def _retained_eighteen(tmp_path, monkeypatch):
    """Actual 0227 topology, with synthetic IDs: 14+2 settled, 2 unknown."""
    monkeypatch.setattr(runloop, 'HOME', tmp_path)
    monkeypatch.setattr(boundary_recovery, '_check_processes', lambda _home: None)
    fixture = json.loads((Path(__file__).parent / 'fixtures/0227_mixed_boundary.json').read_text())
    assignments = [{'assignment_id': row['assignment_id'], **row['saved']}
                   for row in fixture['rows']]
    path = assignment_boundary.prepare(tmp_path, 'deep-swe', assignments)
    for row, assignment in zip(fixture['rows'], assignments):
        if row['outcome']:
            assignment_boundary.record_outcome(path, assignment, row['outcome'])
    client = Client()
    client.rows = {row['assignment_id']: row for row in fixture['proofs']}
    return path, client


def test_real_eighteen_boundary_only_unresolved_need_review(tmp_path, monkeypatch):
    path, client = _retained_eighteen(tmp_path, monkeypatch)
    before = path.read_bytes()
    state, digest = assignment_boundary.snapshot(path)
    assert len(state['expected']) == 18 and len(state['outcomes']) == 16
    assert boundary_recovery.historical_unknown_allows_claim(
        client, state, digest, path, tmp_path) == 2
    assert client.reads == 2
    assert client.historical_admission_reference == '0227-original-reviewed:' + 'f' * 64
    assert path.read_bytes() == before
    # Also enter through the real go preparation, stopping before a claim.
    args = _args()
    assert runloop._prepare_assignment_boundary(args, client, 'deep-swe') is None
    class ReachedReadOnlyAcquisition(Exception):
        pass
    def no_claim(*_a, **kw):
        assert kw['allow_new_claims'] is False
        return [], True
    monkeypatch.setattr(runloop, '_acquire_batch', no_claim)
    monkeypatch.setattr(runloop, '_claim_cell', lambda *_a, **_kw: (_ for _ in ()).throw(ReachedReadOnlyAcquisition()))
    with pytest.raises(ReachedReadOnlyAcquisition):
        runloop._prepare_batch(args, client)
    assert path.read_bytes() == before


@pytest.mark.parametrize('extra', ('same_batch_unknown', 'other_batch_unknown',
                                  'missing_batch_unknown', 'forged_proof', 'settled_in_proof',
                                  'settled_metadata_missing', 'proof_revoked', 'proof_expired', 'duplicate_proof_id'))
def test_retained_history_never_hides_unreviewed_unknown(tmp_path, monkeypatch, extra):
    path, client = _retained_eighteen(tmp_path, monkeypatch)
    before = path.read_bytes()
    state, digest = assignment_boundary.snapshot(path)
    if extra.endswith('_unknown'):
        aid = f'{0 if extra == "same_batch_unknown" else 15:032x}'
        state['outcomes'].pop(aid)
        if extra == 'missing_batch_unknown':
            state['expected'][aid]['batch_id'] = None
        client.rows[aid] = _reviewed_response(aid, 1)
    elif extra == 'settled_metadata_missing':
        state['expected']['0' * 32].pop('model')
    elif extra == 'proof_revoked':
        client.rows[A]['admission_evidence']['state'] = 'blocked'
    elif extra == 'proof_expired':
        client.rows[A] = boundary_recovery.ApiError('410 proof expired')
    elif extra == 'duplicate_proof_id':
        client.rows[A]['admission_evidence']['result_assignment_ids'] = [A, A, B]
    elif extra == 'forged_proof':
        client.rows[B]['admission_evidence']['manifest_sha256'] = '0' * 64
    else:
        client.rows[A]['admission_evidence']['result_assignment_ids'].append('0' * 32)
    with pytest.raises(boundary_recovery.RecoveryBlocked):
        boundary_recovery.historical_unknown_allows_claim(client, state, digest, path, tmp_path)
    assert client.historical_admission_reference is None
    assert path.read_bytes() == before


@pytest.mark.parametrize('failure', [None, 'revoked', 'changed_proof', 'ledger_changed', 'wrong_benchmark', 'plan_scope'])
def test_fresh_pick_rechecks_retained_history_before_binding(tmp_path, monkeypatch, failure):
    path, client = _retained_eighteen(tmp_path, monkeypatch)
    original = path.read_bytes()
    fresh = _assignment(C, NEW_BATCH)
    args = _args()
    claimed = []

    def claim(*_a, **_kw):
        assert client.batch_id is None
        claimed.append(C)
        return fresh

    monkeypatch.setattr(runloop, '_claim_cell', claim)
    assert runloop._prepare_assignment_boundary(args, client, 'deep-swe') is None
    active, _ = runloop._prepare_batch(args, client)
    assert claimed == [C] and client.batch_id is None
    assert client.reads == 4  # Initial proof, then fresh proof immediately before claim.
    if failure == 'revoked':
        client.rows[A]['admission_evidence']['state'] = 'blocked'
    elif failure == 'changed_proof':
        client.rows[B]['admission_evidence']['manifest_sha256'] = '0' * 64
    elif failure == 'ledger_changed':
        path.write_bytes(original + b'\n')
    elif failure == 'wrong_benchmark':
        client.benchmark_id = 'pompeii'
    elif failure == 'plan_scope':
        client.plan_scoped = True
    before = path.read_bytes()
    new_path = assignment_boundary.state_path(tmp_path, 'deep-swe', NEW_BATCH)
    if failure:
        with pytest.raises(SystemExit, match='assignment boundary check failed'):
            runloop._prepare_assignment_boundary(args, client, 'deep-swe', active)
        assert client.batch_id is None and not new_path.exists()
    else:
        assert runloop._prepare_assignment_boundary(args, client, 'deep-swe', active) == new_path
        assert client.batch_id == NEW_BATCH and client.reads == 6
        assert set(json.loads(new_path.read_text())['expected']) == {C}
    assert path.read_bytes() == before
