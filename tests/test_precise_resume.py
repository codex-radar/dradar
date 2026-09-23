"""Protected exact-assignment resume never selects or runs a sibling."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from dradar import assignment_boundary, empty_submission_circuit, runloop
from dradar import cli
from dradar.cli import _assignment_id_value


BATCH = "b5fee058c5f54476a6023bc87e43fb30"
FIRST = "1c3c1f83869e4b28988311115f19e72c"
SECOND = "21343bdb20334837a6317764a880e02b"
OLD_BATCH = "22be49862ac94c95916fcc2237961382"
OLD_ASSIGNMENT = "e1bf9d9882fd498da16bd21d39efca88"


def _held(assignment_id, task_id):
    return {
        "assignment_id": assignment_id,
        "task_id": task_id,
        "batch_id": BATCH,
        "benchmark_id": "deep-swe",
        "agent": "codex",
        "provider": "openai",
        "agent_version": "fixture-agent",
        "model": "gpt-6-sol",
        "effort": "medium",
        "nonce": "fixture-nonce",
        "lease_generation": 1,
        "owner_epoch": 1,
        "expires_at": "2099-01-01T00:00:00Z",
        "execution_state": "waiting",
        "runner_state": "waiting",
        "heartbeat_running": False,
        "runner_phase": None,
        "started_at": None,
        "checkpoint_id": None,
        "deep_swe_commit": None,
        "est_quota_pct": 0,
    }


class ExactBatchClient:
    batch_id = BATCH
    account_scope = "fixture-account"

    def __init__(self, inventories):
        self.inventories = list(inventories)
        self.reads = 0

    def get_assignment(self):
        inventory = self.inventories[min(self.reads, len(self.inventories) - 1)]
        self.reads += 1
        return {"active": deepcopy(inventory), "free_pick": True}

    def checkout(self, *args, **kwargs):
        pytest.fail("exact resume must not use the next-task dispenser")

    def claim_assignment(self, *args, **kwargs):
        pytest.fail("exact resume must not claim new work")


def _args(**overrides):
    values = {
        "assignment": FIRST,
        "batch_id": BATCH,
        "resume": True,
        "yes": False,
        "parallel": False,
        "workers": 1,
        "refill": False,
        "dev_agent": None,
        "auto": None,
        "pick": None,
        "allow_task_drift": False,
        "keep": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _setup(monkeypatch, tmp_path, response="y", circuit_version=None):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: response)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    prior = _held("8829e57009b44414acbda7e590053664", "goreleaser")
    empty_submission_circuit.record_empty(
        tmp_path, prior, circuit_version or runloop.__version__,
        account_scope="fixture-account",
    )


def _run(monkeypatch, client, tmp_path, args=None):
    ran = []
    monkeypatch.setattr(
        runloop, "_run_and_submit",
        lambda _client, assignment, *_a, **_kw: ran.append(assignment["assignment_id"])
        or "submitted",
    )
    rc = runloop._go_menu(
        args or _args(), {"benchmark": "deep-swe"}, client, tmp_path,
    )
    return rc, ran


def _old_grok_assignment():
    return {
        "assignment_id": OLD_ASSIGNMENT,
        "batch_id": OLD_BATCH,
        "task_id": "testem-per-launcher-reports",
        "model": "grok-4.6",
        "effort": "high",
    }


def _luna_held(assignment_id, task_id):
    return {**_held(assignment_id, task_id), "model": "gpt-6-luna"}


def test_precise_boundary_keeps_unrelated_legacy_batch_intact(monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    old_path = assignment_boundary.prepare(
        tmp_path, "deep-swe", [_old_grok_assignment()],
    )
    old_bytes = old_path.read_bytes()
    active = [_luna_held(FIRST, "csstree"), _luna_held(SECOND, "yaegi")]
    args = _args()
    client = ExactBatchClient([active])

    path = runloop._prepare_assignment_boundary(args, client, "deep-swe")

    assert client.reads == 1
    assert path == assignment_boundary.state_path(tmp_path, "deep-swe", BATCH)
    assert set(assignment_boundary.reconcile(path, active).expected_ids) == {FIRST, SECOND}
    assert old_path.read_bytes() == old_bytes


def test_precise_boundary_rejects_missing_sibling_in_current_batch(monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    first, second = _luna_held(FIRST, "csstree"), _luna_held(SECOND, "yaegi")
    path = assignment_boundary.prepare(
        tmp_path, "deep-swe", [first, second], batch_id=BATCH,
    )
    before = path.read_bytes()

    with pytest.raises(SystemExit, match=f"disappeared.*{SECOND}"):
        runloop._prepare_assignment_boundary(
            _args(), ExactBatchClient([[first]]), "deep-swe",
        )
    assert path.read_bytes() == before


def test_precise_boundary_rechecks_after_initial_admission(monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    first, second = _held(FIRST, "csstree"), _held(SECOND, "yaegi")
    args = _args()
    client = ExactBatchClient([[first, second]])
    path = runloop._prepare_assignment_boundary(args, client, "deep-swe")
    before = path.read_bytes()

    with pytest.raises(SystemExit, match=f"disappeared.*{SECOND}"):
        runloop._prepare_assignment_boundary(args, client, "deep-swe", [first])
    assert path.read_bytes() == before


def test_precise_boundary_retains_legacy_guard_for_same_batch(monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    first, second = _held(FIRST, "csstree"), _held(SECOND, "yaegi")
    legacy = assignment_boundary.prepare(tmp_path, "deep-swe", [first, second])
    before = legacy.read_bytes()

    with pytest.raises(SystemExit, match=f"disappeared.*{SECOND}"):
        runloop._prepare_assignment_boundary(
            _args(), ExactBatchClient([[first]]), "deep-swe",
        )
    assert legacy.read_bytes() == before
    assert not assignment_boundary.state_path(tmp_path, "deep-swe", BATCH).exists()


def test_precise_boundary_rejects_legacy_without_attribution(monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    legacy = assignment_boundary.prepare(
        tmp_path, "deep-swe", [{"assignment_id": OLD_ASSIGNMENT}],
    )
    before = legacy.read_bytes()

    with pytest.raises(SystemExit, match="unknown batch attribution"):
        runloop._prepare_assignment_boundary(
            _args(), ExactBatchClient([[_held(FIRST, "csstree")]]), "deep-swe",
        )
    assert legacy.read_bytes() == before


def test_precise_boundary_rejects_corrupt_legacy(monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    legacy = assignment_boundary.state_path(tmp_path, "deep-swe")
    legacy.parent.mkdir(parents=True)
    legacy.write_text("not-json")

    with pytest.raises(SystemExit, match="missing or invalid"):
        runloop._prepare_assignment_boundary(
            _args(), ExactBatchClient([[_held(FIRST, "csstree")]]), "deep-swe",
        )
    assert legacy.read_text() == "not-json"


def test_precise_boundary_rejects_legacy_id_overlap_with_other_batch(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    legacy = assignment_boundary.prepare(
        tmp_path, "deep-swe",
        [{**_old_grok_assignment(), "assignment_id": FIRST}],
    )
    before = legacy.read_bytes()

    with pytest.raises(SystemExit, match="overlaps the requested batch"):
        runloop._prepare_assignment_boundary(
            _args(), ExactBatchClient([[_held(FIRST, "csstree")]]), "deep-swe",
        )
    assert legacy.read_bytes() == before


@pytest.mark.parametrize("bad_item", [
    {"batch_id": OLD_BATCH}, {"benchmark_id": "other"},
])
def test_precise_boundary_rejects_cross_scope_inventory(
    monkeypatch, tmp_path, bad_item,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    active = [_held(FIRST, "csstree"), {**_held(SECOND, "yaegi"), **bad_item}]
    with pytest.raises(SystemExit, match="inventory crosses"):
        runloop._prepare_assignment_boundary(
            _args(), ExactBatchClient([active]), "deep-swe",
        )
    assert not assignment_boundary.state_path(tmp_path, "deep-swe", BATCH).exists()


def test_precise_boundary_rejects_inherited_path(monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setenv(runloop._ASSIGNMENT_BOUNDARY_ENV, str(tmp_path / "other.json"))
    with pytest.raises(SystemExit, match="cannot inherit"):
        runloop._prepare_assignment_boundary(
            _args(), ExactBatchClient([[_held(FIRST, "csstree")]]), "deep-swe",
        )


def test_precise_resume_cli_entry_uses_full_batch_boundary_without_model(
    monkeypatch, tmp_path,
):
    _setup(monkeypatch, tmp_path)
    legacy = assignment_boundary.prepare(
        tmp_path, "deep-swe", [_old_grok_assignment()],
    )
    old_bytes = legacy.read_bytes()
    first, second = _luna_held(FIRST, "csstree"), _luna_held(SECOND, "yaegi")
    empty_submission_circuit.record_empty(
        tmp_path, first, runloop.__version__, account_scope="fixture-account",
    )
    client = ExactBatchClient([[first, second]])
    ran = []

    class Telemetry:
        def __init__(self, *args, **kwargs):
            pass

        def bind_batch(self, *args):
            pass

        def start(self):
            pass

        def set_phase(self, *args):
            pass

        def close(self, *args):
            pass

    monkeypatch.setattr(runloop, "preflight_artifact_platform", lambda: None)
    monkeypatch.setattr(runloop, "_run_config", lambda _args: {"benchmark": "deep-swe"})
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_kw: client)
    monkeypatch.setattr(runloop, "_selected_tasks_root", lambda _cfg: tmp_path)
    monkeypatch.setattr(runloop, "RunnerTelemetry", Telemetry)
    monkeypatch.setattr(runloop, "acquire_run_lock", lambda _home: None)
    monkeypatch.setattr(runloop, "sweep_orphan_compose", lambda *_a: None)
    monkeypatch.setattr(runloop, "_maintain_image_cache", lambda *_a, **_kw: False)
    monkeypatch.setattr(runloop, "ensure_benchmark_task_pack", lambda *_a: None)
    monkeypatch.setattr(runloop, "_ensure_selected_tasks_root", lambda *_a: None)
    monkeypatch.setattr(runloop, "ensure_pier", lambda: None)
    monkeypatch.setattr(runloop, "_ensure_egress_runtime", lambda **_kw: None)
    monkeypatch.setattr(runloop, "_mark_pending_scope_required", lambda _client: None)
    monkeypatch.setattr(
        runloop, "_run_batch",
        lambda _args, _client, _tasks_root, selected, **_kw:
        ran.extend(item["assignment_id"] for item in selected) or 1,
    )

    assert cli.main([
        "resume", "--benchmark", "deep-swe", "--batch-id", BATCH,
        "--assignment", FIRST,
    ]) == 1
    assert ran == [FIRST]
    assert client.reads >= 3
    path = assignment_boundary.state_path(tmp_path, "deep-swe", BATCH)
    assert assignment_boundary.reconcile(path, [first, second]).expected_ids == {FIRST, SECOND}
    assert legacy.read_bytes() == old_bytes


def test_precise_resume_runs_only_confirmed_held_assignment(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    first, second = _held(FIRST, "csstree"), _held(SECOND, "yaegi")
    client = ExactBatchClient([[first, second], [first, second]])

    rc, ran = _run(monkeypatch, client, tmp_path)

    assert rc == 0 and ran == [FIRST]
    assert client.reads == 2
    assert "other 1 held assignment(s) will remain untouched" in capsys.readouterr().out
    assert empty_submission_circuit.open_for(
        tmp_path, second, runloop.__version__, account_scope="fixture-account",
    )


@pytest.mark.parametrize("answer", ["n", "", "no"])
def test_precise_resume_decline_never_runs(monkeypatch, tmp_path, answer):
    _setup(monkeypatch, tmp_path, answer)
    client = ExactBatchClient([[_held(FIRST, "csstree"), _held(SECOND, "yaegi")]])
    rc, ran = _run(monkeypatch, client, tmp_path)
    assert rc == 1 and ran == [] and client.reads == 1


def test_precise_resume_eof_never_runs(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(EOFError()))
    client = ExactBatchClient([[_held(FIRST, "csstree"), _held(SECOND, "yaegi")]])
    ran = []
    monkeypatch.setattr(runloop, "_run_and_submit", lambda *_a, **_kw: ran.append(True))
    with pytest.raises(EOFError):
        runloop._go_menu(_args(), {"benchmark": "deep-swe"}, client, tmp_path)
    assert ran == [] and client.reads == 1


@pytest.mark.parametrize("change", [
    {"execution_state": "preparing"},
    {"runner_state": "running", "heartbeat_running": True},
    {"started_at": "2026-09-23T00:00:00Z"},
    {"expires_at": "2000-01-01T00:00:00Z"},
    {"checkpoint_id": "retired"},
    {"batch_id": "a" * 32},
    {"benchmark_id": "other"},
    {"model": "other"},
    {"effort": "high"},
    {"owner_epoch": 2},
])
def test_precise_resume_rechecks_server_identity_after_confirmation(
    monkeypatch, tmp_path, change,
):
    _setup(monkeypatch, tmp_path)
    first, second = _held(FIRST, "csstree"), _held(SECOND, "yaegi")
    client = ExactBatchClient([[first, second], [{**first, **change}, second]])
    rc, ran = _run(monkeypatch, client, tmp_path)
    assert rc == 1 and ran == [] and client.reads == 2


def test_precise_resume_missing_or_pending_assignment_never_claims(
    monkeypatch, tmp_path,
):
    _setup(monkeypatch, tmp_path)
    client = ExactBatchClient([[_held(SECOND, "yaegi")]])
    rc, ran = _run(monkeypatch, client, tmp_path)
    assert rc == 1 and ran == []

    monkeypatch.setattr(
        runloop, "_pending_assignment_ids_for_client", lambda *_a, **_kw: {FIRST},
    )
    client = ExactBatchClient([[_held(FIRST, "csstree"), _held(SECOND, "yaegi")]])
    rc, ran = _run(monkeypatch, client, tmp_path)
    assert rc == 1 and ran == []


@pytest.mark.parametrize("option", [
    {"yes": True},
    {"parallel": True},
    {"refill": True},
    {"workers": 2},
    {"worker_child": True},
    {"fleet_pool": True},
    {"auto": 1},
    {"forget_assignment_boundary": True},
    {"allow_task_drift": True},
    {"batch_id": None},
])
def test_precise_resume_rejects_unsafe_invocations(monkeypatch, option):
    monkeypatch.setattr(runloop.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    with pytest.raises(SystemExit):
        runloop._validate_precise_resume_options(_args(**option))


def test_precise_resume_rejects_noninteractive_and_invalid_ids(monkeypatch):
    monkeypatch.setattr(runloop.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(SystemExit):
        runloop._validate_precise_resume_options(_args())
    with pytest.raises(SystemExit):
        runloop._validate_precise_resume_options(_args(assignment="not-an-id"))
    with pytest.raises(Exception):
        _assignment_id_value("not-an-id")


def test_precise_success_retains_scope_protection_for_held_sibling(
    monkeypatch, tmp_path,
):
    _setup(monkeypatch, tmp_path)
    selected, sibling = _held(FIRST, "csstree"), _held(SECOND, "yaegi")
    client = ExactBatchClient([[selected, sibling]])
    runloop._record_empty_submission_outcome(
        _args(_precise_retry_assignment_id=FIRST), client, selected, "submitted",
    )
    assert empty_submission_circuit.open_for(
        tmp_path, sibling, runloop.__version__, account_scope="fixture-account",
    )

    # Ordinary single-task resume retains its historical rearm-on-success rule.
    runloop._record_empty_submission_outcome(
        _args(assignment=None), client, selected, "submitted",
    )
    assert not empty_submission_circuit.open_for(
        tmp_path, sibling, runloop.__version__, account_scope="fixture-account",
    )


def test_repeated_precise_command_cannot_rerun_submitted_assignment(
    monkeypatch, tmp_path,
):
    _setup(monkeypatch, tmp_path)
    first, second = _held(FIRST, "csstree"), _held(SECOND, "yaegi")
    ran = []
    monkeypatch.setattr(
        runloop, "_run_and_submit",
        lambda _client, assignment, *_a, **_kw: ran.append(assignment["assignment_id"])
        or "submitted",
    )
    cfg = {"benchmark": "deep-swe"}
    first_client = ExactBatchClient([[first, second], [first, second]])
    assert runloop._go_menu(_args(), cfg, first_client, tmp_path) == 0

    # The authenticated server no longer lists the submitted assignment.
    second_client = ExactBatchClient([[second]])
    assert runloop._go_menu(_args(), cfg, second_client, tmp_path) == 1
    assert ran == [FIRST]


def test_assignment_option_is_resume_only(monkeypatch):
    parsed = []
    monkeypatch.setattr(cli, "cmd_go", lambda args: parsed.append(args) or 0)
    assert cli.main(["resume", "--batch-id", BATCH, "--assignment", FIRST]) == 0
    assert parsed[0].resume is True and parsed[0].assignment == FIRST
    with pytest.raises(SystemExit):
        cli.main(["go", "--batch-id", BATCH, "--assignment", FIRST])


def test_upgrade_keeps_old_circuit_and_precise_retry_one_shot(
    monkeypatch, tmp_path,
):
    assert runloop.__version__ == "0.5.227"
    _setup(monkeypatch, tmp_path, circuit_version="0.5.226")
    first, second = _held(FIRST, "csstree"), _held(SECOND, "yaegi")
    automatic = _args(assignment=None, yes=True)
    client = ExactBatchClient([[first, second]])
    assert runloop._empty_submission_blocked_ids([first, second], client) == {
        FIRST, SECOND,
    }
    assert not runloop._allow_explicit_empty_submission_retry(
        automatic, [first, second], client,
    )
    assert runloop._go_menu(
        automatic, {"benchmark": "deep-swe"}, client, tmp_path,
    ) == 1

    selected_args = _args()
    ran = []

    def submit(client, assignment, *_a, **_kw):
        ran.append(assignment["assignment_id"])
        runloop._record_empty_submission_outcome(
            selected_args, client, assignment, "submitted",
        )
        return "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", submit)
    client = ExactBatchClient([[first, second], [first, second]])
    assert runloop._go_menu(
        selected_args, {"benchmark": "deep-swe"}, client, tmp_path,
    ) == 0
    assert ran == [FIRST]
    assert all(
        empty_submission_circuit.open_for(
            tmp_path, second, version, account_scope="fixture-account",
        ) for version in ("0.5.226", "0.5.227")
    )
    assert not runloop._allow_explicit_empty_submission_retry(
        automatic, [second], client,
    )
    assert runloop._go_menu(
        _args(), {"benchmark": "deep-swe"},
        ExactBatchClient([[second]]), tmp_path,
    ) == 1
    assert ran == [FIRST]


def test_upgrade_failed_retry_does_not_rearm_scope(monkeypatch, tmp_path):
    assert runloop.__version__ == "0.5.227"
    _setup(monkeypatch, tmp_path, circuit_version="0.5.226")
    first, second = _held(FIRST, "csstree"), _held(SECOND, "yaegi")
    args = _args()

    def fail(client, assignment, *_a, **_kw):
        runloop._record_empty_submission_outcome(args, client, assignment, "failed")
        return "failed"

    monkeypatch.setattr(runloop, "_run_and_submit", fail)
    assert runloop._go_menu(
        args, {"benchmark": "deep-swe"},
        ExactBatchClient([[first, second], [first, second]]), tmp_path,
    ) == 1
    assert empty_submission_circuit.open_for(
        tmp_path, second, "0.5.227", account_scope="fixture-account",
    )


def test_upgrade_scope_matching_preserves_account_and_model_isolation(tmp_path):
    first = _held(FIRST, "csstree")
    empty_submission_circuit.record_empty(
        tmp_path, first, "0.5.226", account_scope="account-a",
    )
    assert empty_submission_circuit.open_for(
        tmp_path, first, "0.5.227", account_scope="account-a",
    )
    assert empty_submission_circuit.open_for_claim(
        tmp_path, first, "0.5.227", account_scope="account-a",
    )
    for changed, account in (
        (first, "account-b"),
        ({**first, "model": "gpt-6-luna"}, "account-a"),
        ({**first, "effort": "high"}, "account-a"),
    ):
        assert not empty_submission_circuit.open_for(
            tmp_path, changed, "0.5.227", account_scope=account,
        )
    other_model = {**first, "model": "gpt-6-luna"}
    empty_submission_circuit.record_empty(
        tmp_path, other_model, "0.5.227", account_scope="account-a",
    )
    empty_submission_circuit.record_empty(
        tmp_path, first, "0.5.226", account_scope="account-b",
    )
    empty_submission_circuit.record_success(
        tmp_path, first, "0.5.227", account_scope="account-a",
    )
    assert not empty_submission_circuit.open_for(
        tmp_path, first, "0.5.226", account_scope="account-a",
    )
    assert empty_submission_circuit.open_for(
        tmp_path, other_model, "0.5.227", account_scope="account-a",
    )
    assert empty_submission_circuit.open_for(
        tmp_path, first, "0.5.227", account_scope="account-b",
    )


def test_precise_selector_without_old_or_new_protection_fails_closed(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "__version__", "0.5.227")
    first = _held(FIRST, "csstree")
    assert runloop._select_precise_resume_assignment(
        _args(), ExactBatchClient([[first]]), [first], "deep-swe",
    ) is None
