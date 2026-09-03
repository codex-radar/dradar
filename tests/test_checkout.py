"""Per-cell checkout loop: the parallel-safe run path (owner decision
2026-07-14 — sessions get work from a server-side dispenser instead of
racing over a shared batch snapshot)."""
import json
import threading
from types import SimpleNamespace

import dradar.runloop as runloop
from dradar import empty_submission_circuit
import pytest
from dradar.api_client import ApiError
from dradar.runner import diagnose_exception

from test_go_menu import FakeClient, _args, _patch_run


def _cell(aid):
    return {"assignment_id": aid, "task_id": f"task-{aid}", "agent": "codex",
            "model": "gpt-5.6-sol", "effort": "low", "nonce": "n",
            "expires_at": "2099-01-01T00:00:00+00:00", "est_minutes": 2,
            "est_quota_pct": 0.5, "deep_swe_commit": None, "batch_id": "batch-1"}


class CheckoutClient(FakeClient):
    def __init__(self, assignment_data, checkouts):
        super().__init__(assignment_data)
        self._checkouts = list(checkouts)   # dicts returned in order, or exceptions
        self.checkout_exclusions = []
        self.checkout_sessions = []
        self.stopped = []

    def checkout(self, exclude_assignment_ids=None, session_id=None):
        self.checkout_exclusions.append(set(exclude_assignment_ids or ()))
        self.checkout_sessions.append(session_id)
        result = self._checkouts.pop(0) if self._checkouts else {"assignment": None,
                                                                 "held": 0, "unstarted": 0}
        if isinstance(result, Exception):
            raise result
        return result

    def mark_stopped(self, assignment_id, **_kwargs):
        self.stopped.append(assignment_id)
        return {"ok": True}


class StubTelemetry:
    session_id = "session-test"

    def __init__(self, checkout_error=None):
        self.bound = []
        self.flushes = 0
        self.phases = []
        self.checkout_error = checkout_error

    def bind_batch(self, batch_id):
        self.bound.append(batch_id)

    def flush(self):
        self.flushes += 1

    def flush_for_checkout(self):
        self.flushes += 1
        if self.checkout_error is not None:
            raise self.checkout_error

    def set_phase(self, phase, assignment_id=None, resume_generation=None):
        self.phases.append((phase, assignment_id, resume_generation))


def test_persisted_empty_submission_blocks_new_batch_before_checkout(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    failed = _cell("prior-empty")
    empty_submission_circuit.record_empty(
        tmp_path, failed, runloop.__version__,
    )
    restarted = {**_cell("new-batch"), "batch_id": "batch-2"}
    client = CheckoutClient(
        {"active": [restarted], "free_pick": True},
        [{"assignment": restarted, "held": 1, "unstarted": 0}],
    )
    args = _args()
    args.yes = True

    assert runloop._run_checkout_loop(args, client, tmp_path, [restarted]) == 1
    assert client.checkout_exclusions == []
    assert "no new checkout or refill" in capsys.readouterr().out


def test_first_exact_empty_submission_stops_before_next_checkout(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempts = []

    def run(_client, assignment, *_args, **_kwargs):
        attempts.append(assignment["assignment_id"])
        return "empty-submission"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    cells = [_cell("empty"), _cell("must-not-start")]
    client = CheckoutClient(
        {"active": cells, "free_pick": True},
        [
            {"assignment": cells[0], "held": 2, "unstarted": 1},
            {"assignment": cells[1], "held": 2, "unstarted": 0},
        ],
    )

    assert runloop._run_checkout_loop(_args(), client, tmp_path, cells) == 1
    assert attempts == ["empty"]
    assert len(client.checkout_exclusions) == 1


def test_interactive_single_task_can_explicitly_retry_and_success_rearms(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    assignment = _cell("retry")
    empty_submission_circuit.record_empty(
        tmp_path, assignment, runloop.__version__,
    )
    args = _args()
    args.yes = False
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    assert runloop._allow_explicit_empty_submission_retry(args, [assignment])
    empty_submission_circuit.record_success(
        tmp_path, assignment, runloop.__version__,
    )
    assert not empty_submission_circuit.open_for(
        tmp_path, assignment, runloop.__version__,
    )


def test_persisted_empty_submission_blocks_auto_claim_before_request(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    assignment = _cell("prior")
    empty_submission_circuit.record_empty(
        tmp_path, assignment, runloop.__version__,
    )

    class Client:
        def claim_assignment(self, *_args):
            raise AssertionError("automatic claim must be blocked locally")

    assert runloop._claim_cell(
        Client(), assignment["task_id"], assignment["model"],
        assignment["effort"], cell_metadata=assignment,
    ) is None


def test_persisted_empty_submission_does_not_block_new_runtime_claim(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    failed = {**_cell("prior"), "agent_version": "0.144.1"}
    empty_submission_circuit.record_empty(
        tmp_path, failed, runloop.__version__,
    )
    new_runtime = {**failed, "agent_version": "0.145.0"}

    class Client:
        def claim_assignment(self, *_args):
            return {"assignment": new_runtime}

    assert runloop._claim_cell(
        Client(), new_runtime["task_id"], new_runtime["model"],
        new_runtime["effort"], cell_metadata=new_runtime,
    ) == new_runtime


def test_persisted_empty_submission_does_not_block_other_provider_claim(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    failed = {**_cell("prior"), "provider": "openai"}
    empty_submission_circuit.record_empty(
        tmp_path, failed, runloop.__version__,
    )
    other_provider = {**failed, "provider": "azure-openai"}

    class Client:
        def claim_assignment(self, *_args):
            return {"assignment": other_provider}

    assert runloop._claim_cell(
        Client(), other_provider["task_id"], other_provider["model"],
        other_provider["effort"], cell_metadata=other_provider,
    ) == other_provider


def test_automatic_exact_pick_without_runtime_metadata_fails_closed(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    failed = {
        **_cell("prior"), "provider": "openai", "agent_version": "0.144.1",
    }
    empty_submission_circuit.record_empty(
        tmp_path, failed, runloop.__version__,
    )

    class Client:
        def claim_assignment(self, *_args):
            raise AssertionError("automatic exact pick must be blocked locally")

    assert runloop._claim_cell(
        Client(), failed["task_id"], failed["model"], failed["effort"],
    ) is None


def test_interactive_exact_pick_without_runtime_metadata_requires_confirmation(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    failed = {
        **_cell("prior"), "provider": "openai", "agent_version": "0.144.1",
    }
    empty_submission_circuit.record_empty(
        tmp_path, failed, runloop.__version__,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    class Client:
        def claim_assignment(self, *_args):
            return {"assignment": failed}

    assert runloop._claim_cell(
        Client(), failed["task_id"], failed["model"], failed["effort"],
        automatic=False,
    ) == failed


def test_checkout_mixed_scopes_excludes_only_empty_submission_scope(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    blocked = {**_cell("blocked"), "agent_version": "0.144.1"}
    safe = {**_cell("safe"), "agent_version": "0.145.0"}
    empty_submission_circuit.record_empty(
        tmp_path, blocked, runloop.__version__,
    )
    ran = []

    def run(_client, assignment, *_args, **_kwargs):
        ran.append(assignment["assignment_id"])
        return "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    client = CheckoutClient(
        {"active": [blocked, safe], "free_pick": True},
        [{"assignment": safe, "held": 2, "unstarted": 0},
         {"assignment": None, "held": 2, "unstarted": 0}],
    )

    assert runloop._run_checkout_loop(_args(), client, tmp_path, [blocked, safe]) == 0
    assert ran == ["safe"]
    assert client.checkout_exclusions[0] == {"blocked"}


def test_legacy_batch_mixed_scopes_runs_only_safe_assignments(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(
        runloop, "_version_pinned_tasks_root",
        lambda *_args, **_kwargs: (tmp_path, None),
    )
    blocked = {**_cell("blocked"), "agent_version": "0.144.1"}
    safe = {**_cell("safe"), "agent_version": "0.145.0"}
    empty_submission_circuit.record_empty(
        tmp_path, blocked, runloop.__version__,
    )
    ran = []

    def run(_client, assignment, *_args, **_kwargs):
        ran.append(assignment["assignment_id"])
        return "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    args = _args()
    args.yes = True

    assert runloop._run_batch(args, object(), tmp_path, [blocked, safe]) == 0
    assert ran == ["safe"]


def test_local_empty_submission_scope_does_not_cross_accounts(tmp_path):
    assignment = _cell("prior")
    empty_submission_circuit.record_empty(
        tmp_path, assignment, runloop.__version__, account_scope="account-a",
    )

    assert empty_submission_circuit.open_for(
        tmp_path, assignment, runloop.__version__, account_scope="account-a",
    )
    assert not empty_submission_circuit.open_for(
        tmp_path, assignment, runloop.__version__, account_scope="account-b",
    )


def test_checkout_loop_runs_dispensed_cells_until_drained(monkeypatch, capsys, tmp_path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = CheckoutClient(
        {"active": [_cell("a1")], "free_pick": True},
        [{"assignment": _cell("a1"), "held": 2, "unstarted": 1},
         {"assignment": _cell("a2"), "held": 2, "unstarted": 0},
         {"assignment": None, "held": 2, "unstarted": 0}])
    rc = runloop._go_menu(_args(), {}, client, tmp_path)
    assert rc == 0
    assert ran == ["a1", "a2"]
    out = capsys.readouterr().out
    assert "checked out task-a1" in out and "1 more waiting" in out


def test_checkout_flushes_and_passes_session_id_before_server_stamps_cell(
        monkeypatch, tmp_path):
    _patch_run(monkeypatch)
    telemetry = StubTelemetry()
    client = CheckoutClient(
        {"active": [_cell("a1")], "free_pick": True},
        [{"assignment": _cell("a1"), "held": 1, "unstarted": 0},
         {"assignment": None, "held": 1, "unstarted": 0}],
    )
    assert runloop._go_menu(
        _args(), {}, client, tmp_path, telemetry=telemetry) == 0
    assert client.checkout_sessions == ["session-test", "session-test"]
    assert telemetry.flushes >= 2
    assert ("preparing", "a1", None) in telemetry.phases


def test_checkout_preserves_session_fence_before_provider_preparation(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    seen = []

    def fail_before_model(_client, assignment, *_args, **_kwargs):
        seen.append(assignment.get("_runner_session_id"))
        return "failed"

    monkeypatch.setattr(runloop, "_run_and_submit", fail_before_model)
    telemetry = StubTelemetry()
    assignment = {**_cell("a1"), "owner_epoch": 7}
    client = CheckoutClient(
        {"active": [assignment], "free_pick": True},
        [
            {"assignment": assignment, "held": 1, "unstarted": 0},
            {"assignment": None, "held": 1, "unstarted": 0},
        ],
    )

    assert runloop._go_menu(
        _args(), {}, client, tmp_path, telemetry=telemetry,
    ) == 1
    assert seen == ["session-test"]


def test_checkout_surfaces_heartbeat_capacity_error_without_calling_checkout(
        monkeypatch, tmp_path):
    _patch_run(monkeypatch)
    telemetry = StubTelemetry(ApiError(
        "server returned 409: session capacity reached",
        status_code=409,
        code="runner_session_capacity_reached",
    ))
    client = CheckoutClient(
        {"active": [_cell("a1")], "free_pick": True},
        [{"assignment": _cell("a1"), "held": 1, "unstarted": 0}],
    )

    with pytest.raises(SystemExit) as exc:
        runloop._go_menu(
            _args(), {}, client, tmp_path, telemetry=telemetry,
        )

    assert "runner_session_capacity_reached" in str(exc.value)
    assert client.checkout_sessions == []
    assert telemetry.flushes == 1


def test_checkout_404_falls_back_to_legacy_batch(monkeypatch, tmp_path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = CheckoutClient(
        {"active": [_cell("a1"), _cell("a2")], "free_pick": True},
        [ApiError("not found", status_code=404)])
    rc = runloop._go_menu(_args(), {}, client, tmp_path)
    assert rc == 0
    assert ran == ["a1", "a2"]   # legacy whole-batch flow took over


def test_checkout_loop_never_retries_a_cell_that_failed_this_session(
        monkeypatch, capsys, tmp_path):
    # The failure path reports 'stopped', which puts the cell back in the
    # dispenser. A current server honors the exclusion and hands out the next
    # waiting cell instead of chewing on the failed one forever.
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempts = []

    def run(client, assignment, *a, **kw):
        attempts.append(assignment["assignment_id"])
        return "failed" if assignment["assignment_id"] == "bad" else "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    client = CheckoutClient(
        {"active": [_cell("bad")], "free_pick": True},
        [{"assignment": _cell("bad"), "held": 2, "unstarted": 1},
         {"assignment": _cell("ok"), "held": 2, "unstarted": 0},
         {"assignment": None, "held": 2, "unstarted": 0}])
    rc = runloop._go_menu(_args(), {}, client, tmp_path)
    assert attempts == ["bad", "ok"]
    assert client.checkout_exclusions == [set(), {"bad"}, {"bad"}]
    assert rc == 1                            # the failure still fails the run


@pytest.mark.parametrize("outcome", ["failed", "rejected", "not-uploaded"])
def test_supervised_worker_stops_checkout_after_any_failed_outcome(
        monkeypatch, capsys, tmp_path, outcome):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempts = []

    def run(client, assignment, *a, **kw):
        attempts.append(assignment["assignment_id"])
        return outcome

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    client = CheckoutClient(
        {"active": [_cell("bad")], "free_pick": True},
        [{"assignment": _cell("bad"), "held": 2, "unstarted": 1},
         {"assignment": _cell("must-not-run"), "held": 2, "unstarted": 0}],
    )

    args = _args()
    args.worker_child = True
    rc = runloop._go_menu(args, {}, client, tmp_path)

    assert rc == 1
    assert attempts == ["bad"]
    assert client.checkout_exclusions == [set()]
    assert len(client._checkouts) == 1
    assert "before resuming" in capsys.readouterr().out


def test_supervised_worker_continues_after_expired_old_batch_assignment(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempts = []

    def run(client, assignment, *a, **kw):
        attempts.append(assignment["assignment_id"])
        return "expired" if assignment["assignment_id"] == "old" else "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    cells = [_cell("old"), _cell("new")]
    client = CheckoutClient(
        {"active": cells, "free_pick": True},
        [
            {"assignment": cells[0], "held": 2, "unstarted": 1},
            {"assignment": cells[1], "held": 1, "unstarted": 0},
            {"assignment": None, "held": 0, "unstarted": 0},
        ],
    )
    args = _args()
    args.worker_child = True

    assert runloop._go_menu(args, {}, client, tmp_path) == 0
    assert attempts == ["old", "new"]
    assert client.checkout_exclusions == [set(), set(), set()]
    assert "stopping this automatic batch runner" not in capsys.readouterr().out


def test_supervised_worker_continues_after_assignment_local_rejection(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempts = []

    def run(client, assignment, *a, **kw):
        attempts.append(assignment["assignment_id"])
        if assignment["assignment_id"] == "invalid-answer":
            return "assignment-reopened"
        return "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    cells = [_cell("invalid-answer"), _cell("next")]
    client = CheckoutClient(
        {"active": cells, "free_pick": True},
        [
            {"assignment": cells[0], "held": 2, "unstarted": 1},
            {"assignment": cells[1], "held": 1, "unstarted": 0},
            {"assignment": None, "held": 0, "unstarted": 0},
        ],
    )
    args = _args()
    args.worker_child = True

    assert runloop._go_menu(args, {}, client, tmp_path) == 0
    assert attempts == ["invalid-answer", "next"]
    assert client.checkout_exclusions == [
        set(), {"invalid-answer"}, {"invalid-answer"},
    ]
    assert "stopping this automatic batch runner" not in capsys.readouterr().out


def test_supervised_worker_continues_after_task_local_runtime_failure(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempts = []

    def run(client, assignment, *a, **kw):
        attempts.append(assignment["assignment_id"])
        if assignment["assignment_id"] == "zcode-terminal-missing":
            return "assignment-isolated"
        return "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    cells = [_cell("zcode-terminal-missing"), _cell("next")]
    client = CheckoutClient(
        {"active": cells, "free_pick": True},
        [
            {"assignment": cells[0], "held": 2, "unstarted": 1},
            {"assignment": cells[1], "held": 1, "unstarted": 0},
            {"assignment": None, "held": 0, "unstarted": 0},
        ],
    )
    args = _args()
    args.worker_child = True

    assert runloop._go_menu(args, {}, client, tmp_path) == 0
    assert attempts == ["zcode-terminal-missing", "next"]
    assert client.checkout_exclusions == [
        set(), {"zcode-terminal-missing"}, {"zcode-terminal-missing"},
    ]
    assert "stopping this automatic batch runner" not in capsys.readouterr().out


def test_cleanup_unconfirmed_quarantines_only_supervised_worker_slot(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempts = []

    def run(_client, assignment, *_args, **_kwargs):
        attempts.append(assignment["assignment_id"])
        return "cleanup-unconfirmed"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    cells = [_cell("cleanup-unknown"), _cell("must-stay-waiting")]
    client = CheckoutClient(
        {"active": cells, "free_pick": True},
        [
            {"assignment": cells[0], "held": 2, "unstarted": 1},
            {"assignment": cells[1], "held": 2, "unstarted": 0},
        ],
    )
    args = _args()
    args.worker_child = True
    telemetry = StubTelemetry()

    assert runloop._run_checkout_loop(
        args, client, tmp_path, cells, telemetry=telemetry,
    ) == runloop._WORKER_SLOT_QUARANTINED_EXIT_CODE
    assert attempts == ["cleanup-unknown"]
    assert len(client._checkouts) == 1
    assert telemetry.phases[-1] == ("paused", "cleanup-unknown", None)
    assert "sibling slots may continue" in capsys.readouterr().out


def test_refill_continues_after_task_local_runtime_isolation(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    monkeypatch.setattr(runloop.refill_plan, "is_running", lambda _home: True)
    monkeypatch.setattr(
        runloop.refill_plan, "complete_if_empty", lambda *_args: None,
    )
    stopped = []
    submitted = []
    replenished = []
    monkeypatch.setattr(
        runloop.refill_plan, "stop",
        lambda _home, reason: stopped.append(reason),
    )
    monkeypatch.setattr(
        runloop.refill_plan, "mark_submitted",
        lambda _home, assignment_id: submitted.append(assignment_id),
    )
    monkeypatch.setattr(
        runloop.refill_plan, "refill_once",
        lambda *_args: replenished.append(True) or {"claimed": 0, "held": 1},
    )
    monkeypatch.setattr(
        runloop.refill_plan, "load", lambda _home: {"refill_to": 2},
    )
    monkeypatch.setattr(runloop, "_sync_worker_refill_target", lambda: None)
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_disk_allows_refill", lambda _cfg: True)

    attempts = []

    def run(_client, assignment, *_args, **_kwargs):
        attempts.append(assignment["assignment_id"])
        return (
            "assignment-isolated"
            if assignment["assignment_id"] == "zcode-terminal-missing"
            else "submitted"
        )

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    cells = [_cell("zcode-terminal-missing"), _cell("next")]
    client = CheckoutClient(
        {"active": cells, "free_pick": True},
        [
            {"assignment": cells[0], "held": 2, "unstarted": 1},
            {"assignment": cells[1], "held": 1, "unstarted": 0},
            {"assignment": None, "held": 0, "unstarted": 0},
        ],
    )
    args = _args()
    args.refill = True

    assert runloop._run_checkout_loop(args, client, tmp_path, cells) == 0
    assert attempts == ["zcode-terminal-missing", "next"]
    assert stopped == []
    assert submitted == ["next"]
    assert replenished == [True, True]


def test_auto_batch_stops_after_failure_without_checking_out_next_task(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempts = []

    def run(client, assignment, *a, **kw):
        attempts.append(assignment["assignment_id"])
        return "failed"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    client = CheckoutClient(
        {"active": [_cell("bad")], "free_pick": True},
        [{"assignment": _cell("bad"), "held": 2, "unstarted": 1},
         {"assignment": _cell("must-not-run"), "held": 2, "unstarted": 0}],
    )
    args = _args(auto=2)

    rc = runloop._run_checkout_loop(args, client, tmp_path, [_cell("bad")])

    assert rc == 1
    assert attempts == ["bad"]
    assert client.checkout_exclusions == [set()]
    assert len(client._checkouts) == 1
    assert "automatic batch runner" in capsys.readouterr().out


def test_multi_cell_resume_stops_after_failure_without_auto_flag(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempts = []
    monkeypatch.setattr(
        runloop, "_run_and_submit",
        lambda _client, assignment, *_a, **_kw: (
            attempts.append(assignment["assignment_id"]) or "failed"
        ),
    )
    cells = [_cell("bad"), _cell("must-not-run")]
    client = CheckoutClient(
        {"active": cells, "free_pick": True},
        [{"assignment": cells[0], "held": 2, "unstarted": 1},
         {"assignment": cells[1], "held": 2, "unstarted": 0}],
    )

    rc = runloop._run_checkout_loop(_args(), client, tmp_path, cells)

    assert rc == 1
    assert attempts == ["bad"]
    assert len(client._checkouts) == 1


def test_preclaimed_waiting_queue_stops_after_second_same_zero_progress_failure(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    monkeypatch.setenv(
        runloop._REPEAT_FAILURE_STATE_ENV, str(tmp_path / "failure-state.json"),
    )
    attempts = []
    signature = ("same-batch-runtime", "agent-command-failed:exit=1")

    def run(_client, assignment, *_args, **_kwargs):
        attempts.append(assignment["assignment_id"])
        opened = runloop._observe_repeat_failure(
            assignment, signature, success=False,
        )
        return "repeat-agent-failure" if opened else "interrupted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    cells = [_cell("first"), _cell("second"), _cell("must-stay-waiting")]
    client = CheckoutClient(
        {"active": cells, "free_pick": True},
        [
            {"assignment": cells[0], "held": 3, "unstarted": 2},
            {"assignment": cells[1], "held": 3, "unstarted": 1},
            {"assignment": cells[2], "held": 3, "unstarted": 0},
        ],
    )

    assert runloop._run_checkout_loop(_args(), client, tmp_path, cells) == 1
    assert attempts == ["first", "second"]
    assert len(client._checkouts) == 1
    assert client.stopped == []
    assert "safety circuit opened" in capsys.readouterr().out


def test_two_workers_share_long_exception_circuit_and_never_start_third(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    monkeypatch.setattr(
        runloop, "build_codex_trajectory_bundle", lambda _trial_dir: {},
    )
    monkeypatch.setenv(
        runloop._REPEAT_FAILURE_STATE_ENV, str(tmp_path / "failure-state.json"),
    )
    monkeypatch.setenv(
        "DRADAR_POOL_ABORT_FILE", str(tmp_path / "pool-abort"),
    )
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": (
                "Command failed (exit 1): codex exec\n"
                + "\n".join(f"long output {index}" for index in range(12))
            ),
        },
    }))
    diagnostic = diagnose_exception(result_path)
    assert diagnostic["exit_code"] == 1
    assert all("Command failed" not in line for line in diagnostic["tail"])

    first_observed = threading.Event()
    circuit_opened = threading.Event()
    started = []
    started_lock = threading.Lock()

    def run(_client, assignment, *_args, **_kwargs):
        with started_lock:
            started.append(assignment["assignment_id"])
        signature = runloop._repeat_failure_signature(
            assignment,
            {"n_agent_steps": 0, "n_input_tokens": None},
            diagnostic,
            SimpleNamespace(codex_cli_version="0.149.0", trial_dir=tmp_path),
        )
        assert signature is not None
        if assignment["assignment_id"] == "worker-one":
            assert not runloop._observe_repeat_failure(
                assignment, signature, success=False,
            )
            first_observed.set()
            assert circuit_opened.wait(timeout=5)
            return "interrupted"
        assert first_observed.wait(timeout=5)
        assert runloop._observe_repeat_failure(
            assignment, signature, success=False,
        )
        circuit_opened.set()
        return "repeat-agent-failure"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    first_cells = [_cell("worker-one"), _cell("worker-two")]
    clients = [
        CheckoutClient(
            {"active": [first_cell], "free_pick": True},
            [
                {"assignment": first_cell, "held": 3, "unstarted": 2},
                {"assignment": _cell(third), "held": 3, "unstarted": 1},
            ],
        )
        for first_cell, third in (
            (first_cells[0], "must-not-start-three"),
            (first_cells[1], "must-not-start-four"),
        )
    ]
    outcomes = []

    def worker(client, active):
        outcomes.append(runloop._run_checkout_loop(
            _args(), client, tmp_path, [active],
        ))

    threads = [
        threading.Thread(target=worker, args=(client, active))
        for client, active in zip(clients, first_cells)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(started) == ["worker-one", "worker-two"]
    assert sorted(outcomes) == [0, 1]
    assert [len(client.checkout_exclusions) for client in clients] == [1, 1]


def test_repeat_failure_stops_refill_before_second_replenishment(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    monkeypatch.setenv(
        runloop._REPEAT_FAILURE_STATE_ENV, str(tmp_path / "failure-state.json"),
    )
    monkeypatch.setattr(runloop.refill_plan, "is_running", lambda _home: True)
    stopped = []
    replenished = []
    monkeypatch.setattr(
        runloop.refill_plan, "stop",
        lambda _home, reason: stopped.append(reason),
    )
    monkeypatch.setattr(runloop.refill_plan, "mark_submitted", lambda *_a: None)
    monkeypatch.setattr(
        runloop.refill_plan, "refill_once",
        lambda *_a: replenished.append(True) or {"claimed": 0, "held": 2},
    )
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_disk_allows_refill", lambda _cfg: True)
    signature = ("same-batch-runtime", "agent-command-failed:exit=1")

    def run(_client, assignment, *_args, **_kwargs):
        opened = runloop._observe_repeat_failure(
            assignment, signature, success=False,
        )
        return "repeat-agent-failure" if opened else "interrupted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    cells = [_cell("first"), _cell("second"), _cell("must-stay-waiting")]
    client = CheckoutClient(
        {"active": cells, "free_pick": True},
        [
            {"assignment": cells[0], "held": 3, "unstarted": 2},
            {"assignment": cells[1], "held": 3, "unstarted": 1},
            {"assignment": cells[2], "held": 3, "unstarted": 0},
        ],
    )
    args = _args()
    args.refill = True
    args.worker_child = False

    assert runloop._run_checkout_loop(args, client, tmp_path, cells) == 1
    assert replenished == [True]
    assert stopped == ["account stop: repeat-agent-failure"]
    assert len(client._checkouts) == 1


def test_checkout_loop_fuses_after_environment_build_failure(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    monkeypatch.setattr(
        runloop, "_run_and_submit",
        lambda *a, **kw: "environment-build-failed",
    )
    client = CheckoutClient(
        {"active": [_cell("broken")], "free_pick": True},
        [{"assignment": _cell("broken"), "held": 2, "unstarted": 1},
         {"assignment": _cell("must-not-run"), "held": 2, "unstarted": 0}],
    )

    rc = runloop._go_menu(_args(), {}, client, tmp_path)

    assert rc == 78
    assert client.checkout_exclusions == [set()]
    assert len(client._checkouts) == 1
    assert "before the next checkout" in capsys.readouterr().out


def test_checkout_loop_fuses_after_interrupted_trial(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DRADAR_BATCH_FAIL_FAST", "1")
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempts = []

    def run(client, assignment, *a, **kw):
        attempts.append(assignment["assignment_id"])
        return "interrupted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    client = CheckoutClient(
        {"active": [_cell("network-failure")], "free_pick": True},
        [{"assignment": _cell("network-failure"), "held": 2, "unstarted": 1},
         {"assignment": _cell("must-not-run"), "held": 2, "unstarted": 0}],
    )

    rc = runloop._go_menu(_args(), {}, client, tmp_path)

    assert rc == 0
    assert attempts == ["network-failure"]
    assert len(client._checkouts) == 1
    assert "stopping this automatic batch runner" in capsys.readouterr().out


def test_checkout_loop_always_fuses_after_insufficient_balance(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempts = []

    def run(client, assignment, *a, **kw):
        attempts.append(assignment["assignment_id"])
        runloop._signal_pool_abort(
            "paid API balance exhausted", interrupt_siblings=False,
        )
        return "insufficient-balance"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    abort_file = tmp_path / "pool-abort"
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(abort_file))
    client = CheckoutClient(
        {"active": [_cell("out-of-money")], "free_pick": True},
        [{"assignment": _cell("out-of-money"), "held": 2, "unstarted": 1},
         {"assignment": _cell("must-not-run"), "held": 2, "unstarted": 0}],
    )

    rc = runloop._go_menu(_args(), {}, client, tmp_path)

    assert rc == 1
    assert attempts == ["out-of-money"]
    assert len(client._checkouts) == 1
    assert abort_file.read_text() == "drain:paid API balance exhausted"
    out = capsys.readouterr().out
    assert "stopping this worker before the next task" in out
    assert "siblings with model runs already in flight are allowed to finish" in out


def test_eight_way_quota_terminal_never_checks_out_or_releases_waiting_siblings(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempted = []

    def run(_client, assignment, *_args, **_kwargs):
        attempted.append(assignment["assignment_id"])
        runloop._signal_pool_abort(
            "account quota exhausted", interrupt_siblings=False,
        )
        return "quota-exhausted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    abort_file = tmp_path / "pool-abort"
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(abort_file))
    first = _cell("quota-probe")
    waiting = [_cell(f"waiting-{index}") for index in range(7)]
    client = CheckoutClient(
        {"active": [first, *waiting], "free_pick": True},
        [{"assignment": first, "held": 8, "unstarted": 7}],
    )
    client.release_assignments = lambda *_a, **_k: pytest.fail(
        "quota drain must preserve waiting siblings",
    )

    assert runloop._go_menu(_args(), {}, client, tmp_path) == 1
    assert attempted == ["quota-probe"]
    assert len(client.checkout_exclusions) == 1
    assert abort_file.read_text() == "drain:account quota exhausted"


def test_checkout_loop_fuses_after_grok_preflight_without_switching_task(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempts = []

    def run(client, assignment, *args, **kwargs):
        attempts.append(assignment["assignment_id"])
        runloop._signal_pool_abort(
            "Grok provider preflight failed (network)", interrupt_siblings=False,
        )
        return "provider-preflight-failed"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    abort_file = tmp_path / "pool-abort"
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(abort_file))
    client = CheckoutClient(
        {"active": [_cell("grok-bad")], "free_pick": True},
        [{"assignment": _cell("grok-bad"), "held": 2, "unstarted": 1},
         {"assignment": _cell("must-not-run"), "held": 2, "unstarted": 0}],
    )

    rc = runloop._go_menu(_args(), {}, client, tmp_path)

    assert rc == 1
    assert attempts == ["grok-bad"]
    assert len(client._checkouts) == 1
    assert abort_file.read_text().startswith("drain:")
    out = capsys.readouterr().out
    assert "stopping this worker before the next task" in out
    assert "siblings with model runs already in flight are allowed to finish" in out


def test_serial_batch_fuses_after_grok_preflight(monkeypatch, tmp_path):
    attempts = []
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    monkeypatch.setattr(
        runloop, "_run_and_submit",
        lambda _client, assignment, *_args, **_kwargs: (
            attempts.append(assignment["assignment_id"])
            or "provider-preflight-failed"
        ),
    )
    cells = [_cell("grok-bad"), _cell("must-not-run")]

    rc = runloop._run_batch(_args(), CheckoutClient({}, []), tmp_path, cells)

    assert rc == 1
    assert attempts == ["grok-bad"]


def test_checkout_worker_obeys_existing_pool_balance_fuse(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    abort_file = tmp_path / "pool-abort"
    abort_file.write_text("drain:paid API balance exhausted")
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(abort_file))
    client = CheckoutClient(
        {"active": [_cell("must-not-run")], "free_pick": True},
        [{"assignment": _cell("must-not-run"), "held": 1, "unstarted": 0}],
    )

    rc = runloop._go_menu(_args(), {}, client, tmp_path)

    assert rc == 0
    assert client.checkout_exclusions == []
    assert "stopped before another checkout" in capsys.readouterr().out


def test_old_server_redispatching_failed_cell_is_unstamped_before_exit(
        monkeypatch, capsys, tmp_path):
    # Regression for case 019f656c-cf16-70e2-ae4c-d1d51146acb2: an old
    # server ignores the exclusion and checks the failed cell out again. The
    # CLI must call stopped once more before exiting, otherwise that second
    # checkout leaves started_at set forever and future resume sees nothing.
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    monkeypatch.setattr(runloop, "_run_and_submit", lambda *a, **kw: "failed")
    client = CheckoutClient(
        {"active": [_cell("bad")], "free_pick": True},
        [{"assignment": _cell("bad"), "held": 1, "unstarted": 0},
         {"assignment": _cell("bad"), "held": 1, "unstarted": 0}])

    rc = runloop._go_menu(_args(), {}, client, tmp_path)

    assert rc == 1
    assert client.checkout_exclusions == [set(), {"bad"}]
    assert client.stopped == ["bad"]
    assert "already failed in this session" in capsys.readouterr().out.replace("\n", " ")


def test_interactive_run_keeps_legacy_batch_flow(monkeypatch, tmp_path):
    # no -y: the dispenser can't host confirm/skip prompts, so the legacy
    # path (with its prompts) must be the one that runs
    ran = []
    _patch_run(monkeypatch, ran=ran)
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    client = CheckoutClient(
        {"active": [_cell("a1")], "free_pick": True},
        [{"assignment": _cell("a1"), "held": 1, "unstarted": 0}])
    rc = runloop._go_menu(_args(yes=False), {}, client, tmp_path)
    assert rc == 0
    assert ran == ["a1"]
    assert len(client._checkouts) == 1        # checkout endpoint never consulted
