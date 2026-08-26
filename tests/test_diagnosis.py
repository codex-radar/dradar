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


def test_diagnose_classifies_grok_stream_failure_as_provider_transport(tmp_path):
    d = diagnose_exception(_result(
        tmp_path,
        "Command failed (exit 1): grok\n"
        "reqwest error stream: error sending request for url "
        "(https://cli-chat-proxy.grok.com/v1/responses)",
    ))
    assert d["kind"] == "provider-transport"


def test_diagnose_classifies_agent_hard_deadline(tmp_path):
    d = diagnose_exception(_result(
        tmp_path, "agent execution timed out", exc_type="AgentTimeoutError",
    ))
    assert d["type"] == "AgentTimeoutError"
    assert d["kind"] == "agent-deadline"


def test_diagnose_classifies_quota_limit_before_generic_429(tmp_path):
    d = diagnose_exception(_result(
        tmp_path, "429 Too Many Requests: usage_limit_reached, retry after reset"))
    assert d["kind"] == "quota-limit"


def test_diagnose_classifies_wrapped_codex_usage_limit_as_quota_limit(tmp_path):
    d = diagnose_exception(_result(
        tmp_path,
        "Command failed (exit 1): codex exec\n"
        "You've hit your usage limit. Try again after the reset.",
    ))
    assert d["kind"] == "quota-limit"


def test_diagnose_classifies_403_as_auth_terminal(tmp_path):
    d = diagnose_exception(_result(tmp_path, "403 Forbidden: account suspended"))
    assert d["kind"] == "auth"


def test_diagnose_classifies_https_api_403_as_auth_terminal(tmp_path):
    d = diagnose_exception(_result(
        tmp_path,
        "HTTP 403 Forbidden, "
        "url: https://chatgpt.com/backend-api/codex/responses",
    ))
    assert d["kind"] == "auth"


def test_diagnose_classifies_codex_websocket_403_as_provider_transport(tmp_path):
    d = diagnose_exception(_result(
        tmp_path,
        "failed to connect to websocket: HTTP error: 403 Forbidden, "
        "url: wss://chatgpt.com/backend-api/codex/responses",
    ))
    assert d["kind"] == "provider-transport"


def test_diagnose_keeps_unrelated_websocket_403_account_terminal(tmp_path):
    d = diagnose_exception(_result(
        tmp_path,
        "failed to connect to websocket: HTTP error: 403 Forbidden, "
        "url: wss://provider.example/v1/responses",
    ))
    assert d["kind"] == "auth"


def test_diagnose_classifies_codex_websocket_retries_exhausted_as_transport(
    tmp_path,
):
    d = diagnose_exception(_result(
        tmp_path,
        "Reconnecting... 5/5 (unexpected status 403 Forbidden)",
    ))
    assert d["kind"] == "provider-transport"


@pytest.mark.parametrize("terminal_marker", [
    "unauthorized",
    "invalid credentials",
    "token expired",
    "account suspended",
])
def test_diagnose_explicit_auth_wins_over_codex_websocket_403(
    tmp_path, terminal_marker,
):
    d = diagnose_exception(_result(
        tmp_path,
        "Reconnecting... 3/5 (unexpected status 403 Forbidden, "
        "url: wss://chatgpt.com/backend-api/codex/responses; "
        f"{terminal_marker})",
    ))
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


def test_diagnose_classifies_provider_declared_temporary_exit(tmp_path):
    d = diagnose_exception(_result(
        tmp_path,
        "Command failed (exit 75): kimi --print\nstdout: Connection error.",
    ))
    assert d["kind"] == "provider-temporary"


def test_diagnose_does_not_infer_temporary_failure_from_model_text(tmp_path):
    d = diagnose_exception(_result(
        tmp_path,
        "agent response mentioned Command failed (exit 75): as an example",
    ))
    assert d["kind"] is None


def test_diagnose_unrecognized_has_no_kind(tmp_path):
    d = diagnose_exception(_result(tmp_path, "segfault in libfoo"))
    assert d["kind"] is None and d["tail"]


def test_diagnose_preserves_only_exit_code_when_command_header_leaves_tail(
        tmp_path):
    message = "Command failed (exit 17): codex exec\n" + "\n".join(
        f"potentially sensitive output line {index}" for index in range(12)
    )

    diagnostic = diagnose_exception(_result(tmp_path, message))

    assert diagnostic["exit_code"] == 17
    assert len(diagnostic["tail"]) == 6
    assert all("Command failed" not in line for line in diagnostic["tail"])


@pytest.mark.parametrize(
    ("message", "exc_type"),
    [
        ("Command failed (exit 17): codex exec", "AgentError"),
        ("Command failed (exit 0): codex exec", "NonZeroAgentExitCodeError"),
        ("Command failed (exit 1234567): codex exec", "NonZeroAgentExitCodeError"),
    ],
)
def test_diagnose_rejects_untrusted_or_unbounded_exit_codes(
        tmp_path, message, exc_type):
    diagnostic = diagnose_exception(_result(tmp_path, message, exc_type=exc_type))
    assert "exit_code" not in diagnostic


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


def test_real_run_path_opens_repeat_zero_progress_circuit_on_second_exit(
        monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    result_data = {
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": "Command failed (exit 1): codex exec",
        },
        "agent_result": {
            "n_agent_steps": 0, "n_input_tokens": None,
            "n_cache_tokens": None, "n_output_tokens": None,
            "cost_usd": None,
        },
    }
    artifacts = [
        _fake_art(tmp_path / "first", result_data=result_data),
        _fake_art(tmp_path / "second", result_data=result_data),
    ]
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: artifacts.pop(0))
    client = InvalidAckClient({})
    args = _args()
    base = {
        **ASSIGNMENT, "agent": "codex", "model": "gpt-5.6-sol",
        "agent_version": "0.149.0", "batch_id": "batch-1",
    }

    first = runloop._run_and_submit(
        client, {**base, "assignment_id": "a-first"}, tmp_path, args, "abc123",
    )
    second = runloop._run_and_submit(
        client, {**base, "assignment_id": "a-second"}, tmp_path, args, "abc123",
    )

    assert first == "interrupted"
    assert second == "repeat-agent-failure"
    assert [item["meta"]["failure_kind"] for item in client.submissions] == [
        "agent-command-failed", "agent-command-failed",
    ]


def test_real_success_resets_zero_progress_failure_streak(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    failed = {
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": "Command failed (exit 1): codex exec",
        },
        "agent_result": {"n_agent_steps": 0},
    }
    artifacts = [
        _fake_art(tmp_path / "first", result_data=failed),
        _fake_art(tmp_path / "success", result_data={"agent_result": {}}),
        _fake_art(tmp_path / "third", result_data=failed),
    ]
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: artifacts.pop(0))
    args = _args()
    client = InvalidAckClient({})
    base = {
        **ASSIGNMENT, "agent": "codex", "model": "gpt-5.6-sol",
        "agent_version": "0.149.0", "batch_id": "batch-1",
    }

    outcomes = [
        runloop._run_and_submit(
            client, {**base, "assignment_id": f"a-{index}"},
            tmp_path, args, "abc123",
        )
        for index in range(3)
    ]

    assert outcomes == ["interrupted", "submitted", "interrupted"]


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


def test_structured_quota_terminal_keeps_existing_checkpoint_and_drains_pool(
        monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    abort_file = tmp_path / "ACCOUNT_STOP"
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(abort_file))
    diagnostic = {
        "schema": "dradar-runner-failure-v1",
        "failure_code": "provider_quota_exhausted",
        "provider_code": 1308,
        "status_code": 429,
    }

    def quota_terminal(*_args, **_kwargs):
        raise RunnerError(
            "ZCode structured provider outcome confirmed account quota exhausted",
            failure_diagnostic=diagnostic,
        )

    checkpoint = type("Checkpoint", (), {"checkpoint_id": "cp-kept"})()
    monkeypatch.setattr(runloop, "run_trial", quota_terminal)
    monkeypatch.setattr(
        runloop, "_pause_checkpoint_quietly", lambda *_a, **_k: checkpoint,
    )
    client = SubmitClient({})
    client.mark_stopped = lambda *_a, **_k: pytest.fail(
        "a resumable checkpoint must not be released or reset",
    )

    outcome = runloop._run_and_submit(
        client, ASSIGNMENT, tmp_path, _args(), "abc123",
    )

    assert outcome == "quota-exhausted"
    assert abort_file.read_text() == "drain:account quota exhausted"


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


def test_failed_trial_reports_only_structured_failure_diagnostic(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    diagnostic = {
        "schema": "dradar-runner-failure-v1",
        "failure_code": "trial_timeout",
        "trial_timeout_sec": 3600,
        "zcode_session_timeout_sec": 3660,
    }

    def always_fails(*args, **kwargs):
        raise RunnerError("secret must remain local", failure_diagnostic=diagnostic)

    monkeypatch.setattr(runloop, "run_trial", always_fails)
    stopped = []
    client = SubmitClient({})
    client.mark_stopped = lambda aid, **kw: stopped.append((aid, kw))
    assert runloop._run_and_submit(
        client, ASSIGNMENT, tmp_path, _args(), "abc",
    ) == "failed"
    assert stopped[0][1]["failure_diagnostic"] == diagnostic
    assert "secret" not in json.dumps(stopped[0][1])


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


def test_mark_stopped_downgrades_only_diagnostic_422(capsys):
    attempts = []

    class Client:
        def mark_stopped(self, assignment_id, **kwargs):
            attempts.append((assignment_id, kwargs))
            if len(attempts) == 1:
                raise ApiError(
                    "server returned 422: unsupported failure_diagnostic schema",
                    status_code=422,
                )
            return {"ok": True}

    assert runloop._mark_stopped_quietly(
        Client(), "assignment-3", failure_kind="runner_failed",
        failure_diagnostic={"schema": "dradar-runner-failure-v1"},
    ) is True
    assert len(attempts) == 2
    assert "failure_diagnostic" in attempts[0][1]
    assert "failure_diagnostic" not in attempts[1][1]
    assert capsys.readouterr().out == ""


def test_mark_stopped_downgrades_failure_kind_for_old_server(capsys):
    attempts = []

    class Client:
        def mark_stopped(self, assignment_id, **kwargs):
            attempts.append((assignment_id, kwargs))
            if len(attempts) == 1:
                raise ApiError(
                    "server returned 422: unsupported failure_kind",
                    status_code=422,
                )
            return {"ok": True}

    assert runloop._mark_stopped_quietly(
        Client(), "assignment-old", failure_kind="auth",
    ) is True
    assert len(attempts) == 2
    assert attempts[0][1]["failure_kind"] == "auth"
    assert "failure_kind" not in attempts[1][1]
    assert capsys.readouterr().out == ""


def test_mark_stopped_does_not_downgrade_unrelated_422(capsys):
    attempts = []

    class Client:
        def mark_stopped(self, assignment_id, **kwargs):
            attempts.append((assignment_id, kwargs))
            raise ApiError("server returned 422: stale fence", status_code=422)

    assert runloop._mark_stopped_quietly(
        Client(), "assignment-4", failure_kind="runner_failed",
        failure_diagnostic={"schema": "dradar-runner-failure-v1"},
    ) is False
    assert len(attempts) == 1
    assert "could not confirm checkout cleanup" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["auth", "network", "catalog", "unknown"])
def test_grok_preflight_parser_accepts_only_sanitized_stdout_kind(
    tmp_path: Path, kind: str,
):
    result = _result(
        tmp_path,
        "Command failed (exit 78): shell contains "
        "DRADAR_GROK_PREFLIGHT_FAILURE=%s\n"
        f"stdout: DRADAR_GROK_PREFLIGHT_FAILURE={kind}\n"
        "stderr: None",
    )
    assert runloop._grok_preflight_failure(result) == kind


def test_grok_preflight_parser_ignores_template_and_unbounded_output(tmp_path: Path):
    result = _result(
        tmp_path,
        "Command failed: printf DRADAR_GROK_PREFLIGHT_FAILURE=%s\n"
        "stdout: attacker-token=must-not-classify\n"
        "stderr: DRADAR_GROK_PREFLIGHT_FAILURE=auth",
    )
    assert runloop._grok_preflight_failure(result) is None


def test_grok_preflight_is_deferred_without_invalid_upload_or_task_switch(
    monkeypatch, capsys, tmp_path: Path,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    abort_file = tmp_path / "pool-abort"
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(abort_file))
    art = _fake_art(tmp_path, rc=0, result_data={
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": (
                "Command failed (exit 78): sanitized preflight\n"
                "stdout: DRADAR_GROK_PREFLIGHT_FAILURE=auth\n"
                "stderr: None"
            ),
        },
        "agent_result": {},
    }, codex_cli_version=None)
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    stopped = []
    client = SubmitClient({})
    client.mark_stopped = lambda aid, **kw: stopped.append((aid, kw)) or {"ok": True}
    assignment = {
        **ASSIGNMENT,
        "agent": "grok-build",
        "provider": "xai-subscription",
        "model": "grok-4.6",
        "effort": "medium",
    }

    outcome = runloop._run_and_submit(
        client, assignment, tmp_path, _args(), "abc123",
    )

    assert outcome == "provider-preflight-failed"
    assert client.submissions == []
    assert stopped == [("a1", {"defer_seconds": 900, "failure_kind": "auth"})]
    assert abort_file.read_text() == "drain:Grok provider preflight failed (auth)"
    assert art.result.is_file()
    out = capsys.readouterr().out
    assert "provider setup grok" in out
    assert "no invalid submission was created" in out
