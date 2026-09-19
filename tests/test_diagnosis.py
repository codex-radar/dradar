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


def _fixture(name):
    return Path(__file__).with_name("fixtures") / name


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


def test_diagnose_classifies_antigravity_individual_quota_as_quota_limit():
    d = diagnose_exception(_fixture("antigravity_individual_quota_result.json"))
    assert d["kind"] == "quota-limit"


def test_antigravity_quota_limit_opens_pool_circuit(
        monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    abort_file = tmp_path / "ACCOUNT_STOP"
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(abort_file))
    result = tmp_path / "result.json"
    result.write_bytes(
        _fixture("antigravity_individual_quota_result.json").read_bytes()
    )
    art = _fake_art(tmp_path, rc=0)
    art.result = result
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = InvalidAckClient({})
    assignment = {
        **ASSIGNMENT,
        "agent": "antigravity",
        "provider": "antigravity",
        "model": "gemini-3.7-flash",
    }

    outcome = runloop._run_and_submit(
        client, assignment, tmp_path, _args(), "abc123",
    )

    assert outcome == "quota-exhausted"
    assert abort_file.read_text() == "drain:account quota exhausted"
    assert client.submissions[0]["meta"]["failure_kind"] == "quota-limit"
    assert "quota window is exhausted" in capsys.readouterr().out


def test_transient_429_does_not_open_pool_circuit(
        monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    abort_file = tmp_path / "ACCOUNT_STOP"
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(abort_file))
    art = _fake_art(tmp_path, rc=0, result_data={
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": "429 Too Many Requests: burst rate limit",
        },
        "agent_result": {},
    })
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = InvalidAckClient({})

    outcome = runloop._run_and_submit(
        client, ASSIGNMENT, tmp_path, _args(), "abc123",
    )

    assert outcome == "interrupted"
    assert not abort_file.exists()
    assert client.submissions[0]["meta"]["failure_kind"] == "rate-limit"


def test_diagnose_classifies_403_as_auth_terminal(tmp_path):
    d = diagnose_exception(_result(tmp_path, "403 Forbidden: account suspended"))
    assert d["kind"] == "auth"


@pytest.mark.parametrize("message", [
    "internal: The provided authorization grant is invalid",
    "invalid_grant",
    "Kimi OAuth refresh was rejected; authenticate again",
])
def test_diagnose_classifies_kimi_oauth_rejection_as_auth_terminal(
    tmp_path, message,
):
    d = diagnose_exception(_result(tmp_path, message))
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


# A volunteer on a Hong Kong egress (2026-09-08) saw only the provider's raw
# "FAILED_PRECONDITION (400): User location is not supported for the API use",
# with no hint that the fix was to exit through another region. Classify it so
# the console prints a sentence instead of the wire text.
REGION_MSG = (
    "Command failed (exit 1): gemini --print\n"
    "stdout: FAILED_PRECONDITION (400): User location is not supported for "
    "the API use"
)

REGION_JSON_MSG = (
    'Command failed (exit 1): gemini --print\nstdout: {"error": {"code": 400, '
    '"message": "User location is not supported for the API use.", '
    '"status": "FAILED_PRECONDITION"}}'
)


# OpenAI answers the same refusal with a 403 and its own wording. Before this
# category existed the 403 fell through to the account-auth rule, so Codex --
# DRadar's default agent -- told volunteers to run `codex login`, which cannot
# move their egress. Keep both phrasings in one kind.
REGION_OPENAI_MSG = (
    'Command failed (exit 1): codex exec\nstdout: server returned 403: '
    '{"error":{"message":"Country, region, or territory not supported",'
    '"type":"request_forbidden","param":null,'
    '"code":"unsupported_country_region_territory"}}'
)

# The shape above does NOT match has_http_status(403); this one does, which is
# the shape the repo already documents for a real Codex 403 (see
# test_diagnose_classifies_https_api_403_as_auth_terminal). Keep both: only
# this one used to be answered with "run `codex login`", so only this one
# proves the rule has to sit ahead of the auth rules.
REGION_OPENAI_HTTP_MSG = (
    "Command failed (exit 1): codex exec\nstdout: HTTP 403 Forbidden, "
    "url: https://api.openai.com/v1/responses — Country, region, or "
    "territory not supported"
)


@pytest.mark.parametrize("message", [
    REGION_MSG, REGION_JSON_MSG, REGION_OPENAI_MSG, REGION_OPENAI_HTTP_MSG,
])
def test_diagnose_classifies_provider_region_rejection(tmp_path, message):
    d = diagnose_exception(_result(tmp_path, message))
    assert d["kind"] == "region-blocked"


def test_region_rule_precedes_the_generic_http_403_auth_path(tmp_path):
    """The OpenAI refusal carries a 403, so rule order is the whole fix here:
    placed after the auth rules this message would still read as `auth`."""
    message = (
        "http 403 forbidden: Country, region, or territory not supported"
    )
    assert diagnose_exception(_result(tmp_path, message))["kind"] == (
        "region-blocked"
    )


def test_region_block_is_account_terminal_and_drains_without_interrupting(
    monkeypatch, tmp_path,
):
    """A region refusal is bound to the host's egress IP, not to this cell, so
    the pool must stop claiming -- but siblings already paying for a model run
    are left to finish (interrupt_siblings=False)."""
    abort = tmp_path / "pool-abort"
    monkeypatch.setenv(runloop._POOL_ABORT_ENV, str(abort))

    outcome = runloop._terminal_failure_outcome("region-blocked")

    assert outcome == "region-blocked"
    assert abort.is_file(), "pool was not told to stop"
    interrupt_siblings, reason = runloop._pool_stop_directive(abort)
    assert interrupt_siblings is False
    assert "region" in reason
    # `_pool_abort_reason` is what the worker child actually reads before its
    # next checkout, so assert the value that loop would see -- not just the
    # helper pair above.
    assert runloop._pool_abort_reason() == reason


def test_non_terminal_kind_does_not_stop_the_pool(monkeypatch, tmp_path):
    """Negative control for the test above: the terminal table must not have
    turned every classified failure into a pool stop."""
    abort = tmp_path / "pool-abort"
    monkeypatch.setenv(runloop._POOL_ABORT_ENV, str(abort))

    for recoverable in ("rate-limit", "provider-transport", "agent-deadline"):
        assert runloop._terminal_failure_outcome(recoverable) is None
    assert runloop._terminal_failure_outcome(None) is None
    assert not abort.exists(), "a recoverable failure stopped the pool"


def test_region_block_never_sends_a_failure_diagnostic_to_the_server(
    monkeypatch, tmp_path,
):
    """Cross-repo contract with dradar-server #0165.

    The server refuses `failure_diagnostic` for any kind outside
    {runner_failed, task_content_mismatch} and has a test pinning that refusal
    for `region-blocked` specifically. The only thing making that assumption
    true is the conditional at this call site, so pin it from this side too:
    if someone later passes the diagnostic unconditionally, the server answers
    422 and the CLI silently drops BOTH fields on retry.
    """
    from test_go_menu import ASSIGNMENT, SubmitClient

    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    monkeypatch.setenv(runloop._POOL_ABORT_ENV, str(tmp_path / "pool-abort"))

    def region_refusal(*_a, **_kw):
        raise RunnerError(
            "trial failed: FAILED_PRECONDITION (400): User location is not "
            "supported for the API use",
            failure_diagnostic={
                "schema": "dradar-runner-failure-v1",
                "failure_code": "trial_timeout",
            },
        )

    monkeypatch.setattr(runloop, "run_trial", region_refusal)
    client = SubmitClient({})
    stopped = []
    client.mark_stopped = lambda aid, **kw: (
        stopped.append((aid, kw)) or {"ok": True}
    )

    runloop._run_and_submit(
        client, {**ASSIGNMENT, "assignment_id": "a-region"},
        tmp_path, _args(), "abc123",
    )

    assert stopped, "the lease was never returned to the server"
    _, kwargs = stopped[-1]
    assert kwargs.get("failure_kind") == "region-blocked"
    assert "failure_diagnostic" not in kwargs or (
        kwargs["failure_diagnostic"] is None
    ), "a region refusal must not carry a diagnostic the server will reject"


def test_region_block_stop_announcement_names_the_real_cause(capsys):
    """`_ACCOUNT_TERMINAL_OUTCOMES` membership alone would print the generic
    'an account-wide stop condition was detected', which is the unreadable
    message this ticket exists to remove."""
    runloop._announce_account_stop("region-blocked")
    printed = capsys.readouterr().out
    assert "region" in printed
    assert "account-wide stop condition was detected" not in printed


@pytest.mark.parametrize("message", [
    # FAILED_PRECONDITION is not a geography signal on its own: the same status
    # carries a disabled API, a retired model and a bare precondition failure.
    'FAILED_PRECONDITION (400): Precondition check failed.',
    '{"error": {"code": 400, "message": "Gemini 1.0 Pro Vision has been '
    'deprecated", "status": "FAILED_PRECONDITION"}}',
    '{"error": {"code": 400, "message": "Generative Language API has not been '
    'used in project 12345 before or it is disabled", '
    '"status": "FAILED_PRECONDITION"}}',
    # Nor is an ordinary rejected request that merely mentions a location.
    'server returned 400: {"error":{"message":"unknown location parameter"}}',
])
def test_diagnose_does_not_read_every_failed_precondition_as_region_block(
    tmp_path, message,
):
    assert diagnose_exception(_result(tmp_path, message))["kind"] != (
        "region-blocked"
    )


@pytest.mark.parametrize(("message", "kind"), [
    ("http status 401 unauthorized", "auth"),
    ("invalid credentials", "auth"),
    ("status_code: 429 too many requests", "rate-limit"),
    ("you've hit your usage limit", "quota-limit"),
    ("402 Payment Required: Insufficient Balance", "insufficient-balance"),
])
def test_region_block_rule_does_not_shadow_existing_classifications(
    tmp_path, message, kind,
):
    assert diagnose_exception(_result(tmp_path, message))["kind"] == kind


def test_region_block_advice_points_at_egress_not_credentials_or_quota():
    """The volunteer's complaint was an unreadable error, so the fix is only
    real if the printed sentence names the actual remedy. Wrong advice is worse
    than none: an earlier report proved "wait for your quota to reset" sent
    someone chasing a version problem. Ban the remedies the other kinds
    prescribe, not the words -- saying "not your quota" is the point."""
    advice = runner_mod.DIAG_ADVICE["region-blocked"]
    assert "region" in advice
    for prescribed_elsewhere in (
        "codex login", "dradar doctor", "dradar resume", "provider status",
        "recharge", "--refresh", "resets", "try again later",
    ):
        assert prescribed_elsewhere not in advice


def test_failed_trial_prints_region_advice_instead_of_raw_provider_text(
    tmp_path,
):
    """End of the chain the volunteer actually sees: runloop must reach
    DIAG_ADVICE for the new kind, not just classify it."""
    diag = diagnose_exception(_result(tmp_path, REGION_MSG))
    advice = runloop.DIAG_ADVICE.get(diag["kind"])
    assert advice, "runloop cannot reach advice for the new kind"
    assert "region" in advice


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
    assert "bounded retry budget" in out


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


def test_structured_quota_terminal_stops_fresh_retry_and_drains_pool(
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

    monkeypatch.setattr(runloop, "run_trial", quota_terminal)
    client = SubmitClient({})
    stopped = []
    client.mark_stopped = lambda *a, **k: stopped.append((a, k)) or {}

    outcome = runloop._run_and_submit(
        client, ASSIGNMENT, tmp_path, _args(), "abc123",
    )

    assert outcome == "quota-exhausted"
    assert stopped
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


def test_zcode_structured_network_failure_retries_once_serially(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    monkeypatch.setattr(runloop, "_ZCODE_NETWORK_RETRY_DELAY_SECONDS", 0)
    assignment = {**ASSIGNMENT, "agent": "zcode"}
    diagnostic = {
        "schema": "dradar-runner-failure-v1",
        "failure_code": "agent_no_artifact",
        "zcode_provider_failure_reason": "network_error",
        "zcode_provider_retryable": False,
    }
    art = _fake_art(tmp_path, rc=0)
    calls = []

    def transient_then_success(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RunnerError(
                "redacted ZCode terminal failure",
                failure_diagnostic=diagnostic,
            )
        return art

    monkeypatch.setattr(runloop, "run_trial", transient_then_success)
    stopped = []
    client = SubmitClient({})
    client.mark_stopped = lambda aid, **kw: stopped.append((aid, kw))

    assert runloop._run_and_submit(
        client, assignment, tmp_path, _args(), "abc",
    ) == "submitted"
    assert len(calls) == 2
    assert stopped == [(assignment["assignment_id"], {
        "defer_seconds": 0,
        "failure_kind": "provider-transport",
        "failure_diagnostic": diagnostic,
    })]


def test_zcode_network_retry_is_bounded_to_two_total_attempts(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    monkeypatch.setattr(runloop, "_ZCODE_NETWORK_RETRY_DELAY_SECONDS", 0)
    assignment = {**ASSIGNMENT, "agent": "zcode"}
    diagnostic = {
        "schema": "dradar-runner-failure-v1",
        "failure_code": "agent_no_artifact",
        "zcode_provider_failure_reason": "network_error",
    }
    calls = []

    def always_fails(*args, **kwargs):
        calls.append(1)
        raise RunnerError("redacted failure", failure_diagnostic=diagnostic)

    monkeypatch.setattr(runloop, "run_trial", always_fails)
    stopped = []
    client = SubmitClient({})
    client.mark_stopped = lambda aid, **kw: stopped.append((aid, kw))

    assert runloop._run_and_submit(
        client, assignment, tmp_path, _args(), "abc",
    ) == "failed"
    assert len(calls) == 2
    assert [entry[1]["defer_seconds"] for entry in stopped] == [0, 300]


def test_user_interrupt_without_checkpoint_reports_stopped_to_server(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    monkeypatch.setattr(
        runloop, "run_trial",
        lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    stopped = []
    client = SubmitClient({})
    client.mark_stopped = lambda aid, **kw: stopped.append((aid, kw))

    with pytest.raises(KeyboardInterrupt):
        runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc")

    assert stopped == [(
        ASSIGNMENT["assignment_id"],
        {"defer_seconds": 0, "failure_kind": "user_interrupted"},
    )]


def test_user_interrupt_relinquishes_owner_without_checkpoint(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    monkeypatch.setattr(
        runloop, "run_trial",
        lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    stopped = []
    client = SubmitClient({})
    client.mark_stopped = lambda aid, **kw: stopped.append((aid, kw))

    with pytest.raises(KeyboardInterrupt):
        runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc")

    assert stopped == [("a1", {
        "defer_seconds": 0, "failure_kind": "user_interrupted",
    })]


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


def test_task_mismatch_cleanup_rolls_back_observability_for_old_server(capsys):
    attempts = []

    class Client:
        def mark_stopped(self, assignment_id, **kwargs):
            attempts.append((assignment_id, kwargs))
            if len(attempts) == 1:
                raise ApiError(
                    "server returned 422: unsupported failure_diagnostic schema",
                    status_code=422,
                )
            if len(attempts) == 2:
                raise ApiError(
                    "server returned 422: unsupported failure_kind",
                    status_code=422,
                )
            return {"ok": True}

    assert runloop._mark_stopped_quietly(
        Client(), "assignment-old-task-pack",
        failure_kind="task_content_mismatch",
        failure_diagnostic={
            "schema": "dradar-task-content-mismatch-v1",
            "failure_code": "task_content_mismatch",
        },
    ) is True
    assert "failure_diagnostic" in attempts[0][1]
    assert attempts[1][1]["failure_kind"] == "task_content_mismatch"
    assert "failure_diagnostic" not in attempts[1][1]
    assert "failure_kind" not in attempts[2][1]
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
