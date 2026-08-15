"""Per-cell checkout loop: the parallel-safe run path (owner decision
2026-07-14 — sessions get work from a server-side dispenser instead of
racing over a shared batch snapshot)."""
import dradar.runloop as runloop
from dradar.api_client import ApiError

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

    def __init__(self):
        self.bound = []
        self.flushes = 0
        self.phases = []

    def bind_batch(self, batch_id):
        self.bound.append(batch_id)

    def flush(self):
        self.flushes += 1

    def set_phase(self, phase, assignment_id=None, resume_generation=None):
        self.phases.append((phase, assignment_id, resume_generation))


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
    assert ("running", "a1", None) in telemetry.phases


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


def test_supervised_worker_stops_checkout_after_any_failed_outcome(
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

    args = _args()
    args.worker_child = True
    rc = runloop._go_menu(args, {}, client, tmp_path)

    assert rc == 1
    assert attempts == ["bad"]
    assert client.checkout_exclusions == [set()]
    assert len(client._checkouts) == 1
    assert "before resuming" in capsys.readouterr().out


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

    assert rc == 1
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
