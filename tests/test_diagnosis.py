"""Interrupted-run honesty (from the first real volunteer bug report,
2026-07-13): the CLI must pass an exact agent version to Pier (busts stale
image caches that shipped Codex too old for gpt-5.6), print the actual
in-container exception instead of a blanket "wait for your quota", and keep
the failure artifacts instead of deleting the only evidence."""
import json
from pathlib import Path

import pytest

import dradar.runloop as runloop
import dradar.runner as runner_mod
from dradar.api_client import ApiError
from dradar.runner import RunnerError, build_pier_command, diagnose_exception

from test_go_menu import ASSIGNMENT, SubmitClient, _args, _fake_art

STALE_MSG = (
    'Command failed (exit 1): codex exec ...\nstdout: {"type":"error","message":'
    '"{\\"status\\":400,\\"error\\":{\\"message\\":\\"The \'gpt-5.6-sol\' model '
    'requires a newer version of Codex. Please upgrade to the latest app or CLI '
    'and try again.\\"}}"}')


def _codex_cmd(tmp_path, monkeypatch, assignment):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda _: "/usr/bin/pier")
    (tmp_path / assignment["task_id"]).mkdir(exist_ok=True)
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(tmp_path / "auth.json"))
    (tmp_path / "auth.json").write_text("{}")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return build_pier_command(assignment, tmp_path, tmp_path / "jobs", "j", home)


def test_pier_command_passes_exact_agent_version(tmp_path, monkeypatch):
    a = {"assignment_id": "a1", "task_id": "t", "agent": "codex",
         "model": "gpt-5.6-sol", "effort": "low", "agent_version": "0.144.1"}
    cmd = _codex_cmd(tmp_path, monkeypatch, a)
    assert "version=0.144.1" in cmd
    assert cmd[cmd.index("version=0.144.1") - 1] == "--ak"


def test_pier_command_refuses_non_exact_version(tmp_path, monkeypatch):
    a = {"assignment_id": "a1", "task_id": "t", "agent": "codex",
         "model": "gpt-5.6-sol", "effort": "low"}
    with pytest.raises(RunnerError, match="exact stable Codex CLI version"):
        _codex_cmd(tmp_path, monkeypatch, a)


def _result(tmp_path, message, exc_type="NonZeroAgentExitCodeError"):
    p = tmp_path / "result.json"
    p.write_text(json.dumps({"exception_info": {
        "exception_type": exc_type, "exception_message": message}}))
    return p


def test_diagnose_classifies_stale_agent(tmp_path):
    d = diagnose_exception(_result(tmp_path, STALE_MSG))
    assert d["kind"] == "stale-agent"
    assert d["type"] == "NonZeroAgentExitCodeError"
    assert any("requires a newer version" in ln for ln in d["tail"])


def test_diagnose_classifies_rate_limit(tmp_path):
    d = diagnose_exception(_result(tmp_path, "429 Too Many Requests: burst rate limit"))
    assert d["kind"] == "rate-limit"


def test_diagnose_classifies_quota_limit_before_generic_429(tmp_path):
    d = diagnose_exception(_result(
        tmp_path, "429 Too Many Requests: usage_limit_reached, retry after reset"))
    assert d["kind"] == "quota-limit"


def test_diagnose_classifies_403_as_auth_terminal(tmp_path):
    d = diagnose_exception(_result(tmp_path, "403 Forbidden: account suspended"))
    assert d["kind"] == "auth"


def test_diagnose_does_not_treat_bare_task_numbers_as_http_errors(tmp_path):
    d = diagnose_exception(_result(
        tmp_path, "tests processed: 401; next case: 429; assertion failed"))
    assert d["kind"] is None


def test_diagnose_classifies_insufficient_balance_before_generic_http_errors(tmp_path):
    d = diagnose_exception(_result(
        tmp_path, "402 Payment Required: Insufficient Balance"))
    assert d["kind"] == "insufficient-balance"


def test_diagnose_classifies_model_capacity(tmp_path):
    d = diagnose_exception(_result(tmp_path,
        "turn.failed: Selected model is at capacity. Please try a different model."))
    assert d["kind"] == "model-capacity"


def test_diagnose_unrecognized_has_no_kind(tmp_path):
    d = diagnose_exception(_result(tmp_path, "segfault in libfoo"))
    assert d["kind"] is None and d["tail"]


def test_diagnose_empty_without_exception(tmp_path):
    p = tmp_path / "result.json"
    p.write_text(json.dumps({"agent_result": {}}))
    assert diagnose_exception(p) == {}


class InvalidAckClient(SubmitClient):
    def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
               outcome="completed", resume_generation=None):
        super().submit(assignment_id, nonce, patch, trajectory, result, meta,
                       outcome=outcome)
        return {"submission_id": f"s-{assignment_id}", "grade_status": "invalid"}


def test_interrupted_prints_cause_keeps_artifacts_no_quota_claim(
        monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0, result_data={
        "exception_info": {"exception_type": "NonZeroAgentExitCodeError",
                           "exception_message": STALE_MSG},
        "agent_result": {}})
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = InvalidAckClient({})
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    assert tag == "runtime-incompatible"
    assert client.submissions[0]["meta"]["exception_type"] == "NonZeroAgentExitCodeError"
    out = capsys.readouterr().out
    assert "NonZeroAgentExitCodeError" in out
    assert "requires a newer version" in out       # the agent's real error, surfaced
    assert "quota" not in out.lower()              # no unfounded quota guess
    assert art.job_dir.is_dir()                    # failure artifacts survive
    assert str(art.job_dir) in out                 # ...and the path is announced


def test_interrupted_rate_limit_advice_mentions_quota(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0, result_data={
        "exception_info": {"exception_type": "AgentError",
                           "exception_message": "429 Too Many Requests: rate limit"},
        "agent_result": {}})
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = InvalidAckClient({})
    runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    out = capsys.readouterr().out
    assert "bounded exponential backoff" in out


def test_interrupted_quota_limit_opens_pool_circuit(
        monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    abort_file = tmp_path / "ACCOUNT_STOP"
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(abort_file))
    art = _fake_art(tmp_path, rc=0, result_data={
        "exception_info": {
            "exception_type": "AgentError",
            "exception_message": "429: usage_limit_reached for weekly quota",
        },
        "agent_result": {},
    })
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = InvalidAckClient({})

    outcome = runloop._run_and_submit(
        client, ASSIGNMENT, tmp_path, _args(), "abc123")

    assert outcome == "quota-exhausted"
    assert abort_file.read_text() == "drain:account quota exhausted"
    assert "quota window is exhausted" in capsys.readouterr().out


def test_interrupted_insufficient_balance_returns_batch_terminal_outcome(
        monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0, result_data={
        "exception_info": {
            "exception_type": "AgentError",
            "exception_message": "402 Payment Required: Insufficient Balance",
        },
        "agent_result": {},
    })
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = InvalidAckClient({})

    outcome = runloop._run_and_submit(
        client, ASSIGNMENT, tmp_path, _args(), "abc123")

    assert outcome == "insufficient-balance"
    out = capsys.readouterr().out
    assert "insufficient balance" in out.lower()
    assert client.submissions[0]["outcome"] == "interrupted"
    assert client.submissions[0]["meta"]["failure_kind"] == "insufficient-balance"
    assert client.submissions[0]["meta"]["exception_type"] == "AgentError"


def test_interrupted_model_capacity_advice_is_not_a_quota_guess(
        monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0, result_data={
        "exception_info": {"exception_type": "NonZeroAgentExitCodeError",
                           "exception_message":
                               "Selected model is at capacity. Please try a different model."},
        "agent_result": {}})
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = InvalidAckClient({})
    runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    out = capsys.readouterr().out
    assert "retried the original Codex session" in out
    assert "wait for your quota" not in out.lower()   # not the rate-limit advice


def test_completed_run_still_cleans_job_dir(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0)
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    assert tag == "submitted"
    assert not art.job_dir.exists()  # tidy-by-default unchanged for successes


def test_failed_trial_reports_stopped_to_server(monkeypatch, tmp_path):
    from dradar.runner import RunnerError
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")

    def always_fails(*a, **kw):
        raise RunnerError("model.patch missing")

    monkeypatch.setattr(runloop, "run_trial", always_fails)
    stopped = []
    client = SubmitClient({})
    client.mark_stopped = lambda aid, **kw: stopped.append((aid, kw))
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc")
    assert tag == "failed"
    assert stopped == [(
        ASSIGNMENT["assignment_id"],
        {"defer_seconds": 300, "failure_kind": "runner_failed"},
    )]


def test_user_interrupt_without_checkpoint_reports_stopped_to_server(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    monkeypatch.setattr(
        runloop, "run_trial",
        lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(runloop, "_pause_checkpoint_quietly", lambda *a, **kw: None)
    stopped = []
    client = SubmitClient({})
    client.mark_stopped = lambda aid, **kw: stopped.append((aid, kw))

    with pytest.raises(KeyboardInterrupt):
        runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc")

    assert stopped == [(
        ASSIGNMENT["assignment_id"],
        {"defer_seconds": 0, "failure_kind": "user_interrupted"},
    )]


def test_user_interrupt_with_checkpoint_keeps_server_paused(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    monkeypatch.setattr(
        runloop, "run_trial",
        lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    checkpoint = object()
    monkeypatch.setattr(
        runloop, "_pause_checkpoint_quietly", lambda *a, **kw: checkpoint)
    stopped = []
    client = SubmitClient({})
    client.mark_stopped = lambda aid, **kw: stopped.append((aid, kw))

    with pytest.raises(KeyboardInterrupt):
        runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc")

    assert stopped == []


def test_mark_stopped_retries_transient_cleanup_failure(monkeypatch, capsys):
    attempts = []

    class Client:
        def mark_stopped(self, assignment_id, **kwargs):
            attempts.append((assignment_id, kwargs))
            if len(attempts) < 3:
                raise ApiError("temporary outage", status_code=503)
            return {"ok": True}

    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    assert runloop._mark_stopped_quietly(
        Client(),
        {"assignment_id": "assignment-1", "resume_generation": 4},
        defer_seconds=0,
    ) is True
    assert len(attempts) == 3
    assert all(kwargs["resume_generation"] == 4 for _, kwargs in attempts)
    assert capsys.readouterr().out == ""


def test_mark_stopped_surfaces_nonretryable_cleanup_failure(capsys):
    attempts = []

    class Client:
        def mark_stopped(self, assignment_id, **kwargs):
            attempts.append((assignment_id, kwargs))
            raise ApiError("upgrade required", status_code=426)

    assert runloop._mark_stopped_quietly(Client(), "assignment-2") is False
    assert len(attempts) == 1
    out = capsys.readouterr().out
    assert "could not confirm checkout cleanup" in out
    assert "assignment-2" in out
    assert "retry `dradar resume`" in out
