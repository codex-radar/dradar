"""Protected exact-assignment resume never selects or runs a sibling."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from dradar import empty_submission_circuit, runloop
from dradar import cli
from dradar.cli import _assignment_id_value


BATCH = "b5fee058c5f54476a6023bc87e43fb30"
FIRST = "1c3c1f83869e4b28988311115f19e72c"
SECOND = "21343bdb20334837a6317764a880e02b"


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


def _setup(monkeypatch, tmp_path, response="y"):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: response)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    prior = _held("8829e57009b44414acbda7e590053664", "goreleaser")
    empty_submission_circuit.record_empty(
        tmp_path, prior, runloop.__version__, account_scope="fixture-account",
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
