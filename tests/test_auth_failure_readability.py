"""#0226: an auth failure must say what the model platform answered.

`classify_exception_message` decided *that* a run was `auth`, and the console
then printed one generic line for every shape: a refused API key and a request
that carried no credential at all read the same. The fixtures are real text
(three deliberately broken credentials run through codex-cli 0.153.4, and
redacted production results); the sentences are pinned to the file
dradar-server pins its own copy to.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dradar import identity, runloop
from dradar.auth_failure import (
    AUTH_REPORTED,
    auth_failure_sentence,
    auth_failure_signal,
)
from dradar.runner import (
    LiveAccountTerminalError,
    _scan_live_account_errors,
    classify_exception_message,
    diagnose_exception,
)
from test_go_menu import ASSIGNMENT, SubmitClient, _args, _fake_art
from test_my_submissions import FakeStatusClient

SHARED = json.loads(
    (Path(__file__).with_name("fixtures") / "auth_failure_messages.json")
    .read_text(encoding="utf-8")
)
FIXTURES = SHARED["messages"]
BY_NAME = {item["name"]: item for item in FIXTURES}
SENTENCES_EN = {signal: pair["en"] for signal, pair in SHARED["sentences"].items()}
MISCONFIGURED = ("probe_missing", "probe_fake_access_fresh", "probe_bad_api_key")


def test_sentences_match_the_copy_the_server_shows():
    for signal, text in SENTENCES_EN.items():
        assert auth_failure_sentence(signal) == text, signal
    assert auth_failure_sentence("not-a-signal") == SENTENCES_EN[AUTH_REPORTED]


# Where this client's `auth` classification and the platform text disagree.
# Both predate #0226 and are left for their own ticket: changing what counts
# as `auth` changes which failures stop the pool.
#  - DeepSeek's "Authentication Fails, Your api key ... is invalid" is not
#    caught (the marker is "authentication failed"); the server still
#    explains it from result.json.
#  - The classifier reads Pier's whole message, task instruction included.
KNOWN_CLASSIFIER_GAPS = {
    "prod_dsh_api_key_rejected": None,
    "instruction_mentions_auth": "auth",
}


@pytest.mark.parametrize("item", FIXTURES, ids=lambda i: i["name"])
def test_auth_messages_read_like_the_server(item):
    kind = classify_exception_message(item["message"])
    if item["name"] in KNOWN_CLASSIFIER_GAPS:
        assert kind == KNOWN_CLASSIFIER_GAPS[item["name"]]
        return
    assert (kind == "auth") == (item["expected_signal"] is not None), kind
    if kind == "auth":
        assert auth_failure_signal(item["message"]) == item["expected_signal"]


def test_three_deliberate_misconfigurations_print_three_different_sentences(tmp_path):
    sentences = set()
    for name in MISCONFIGURED:
        result = tmp_path / f"{name}.json"
        result.write_text(json.dumps({"exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": BY_NAME[name]["message"]}}))
        diag = diagnose_exception(result)
        assert diag["kind"] == "auth", name
        sentences.add(auth_failure_sentence(diag["auth_signal"]))
    assert len(sentences) == 3


def test_only_auth_diagnoses_carry_a_signal(tmp_path):
    result = tmp_path / "rate.json"
    result.write_text(json.dumps({"exception_info": {
        "exception_type": "NonZeroAgentExitCodeError",
        "exception_message": BY_NAME["prod_dsh_rate_limit"]["message"]}}))
    assert "auth_signal" not in diagnose_exception(result)


FORBIDDEN = ("is invalid", "are invalid", "has expired", "have expired",
             "misconfigured", "your credential", "your api key", "wrong key")


@pytest.mark.parametrize("signal", sorted(SENTENCES_EN))
def test_sentences_state_observations_not_the_volunteers_state(signal):
    text = auth_failure_sentence(signal)
    for phrase in FORBIDDEN:
        assert phrase not in text.lower(), (signal, phrase)
    for needle in ("invalid", "no points", "if the model was called",
                   "your own account quota", "dradar doctor", "Radar Mailbox"):
        assert needle in text, (signal, needle)


def _interrupted_run(monkeypatch, tmp_path, message):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0, result_data={"exception_info": {
        "exception_type": "NonZeroAgentExitCodeError",
        "exception_message": message,
    }, "agent_result": {"n_input_tokens": 0, "n_output_tokens": 0,
                        "n_cache_tokens": 0, "n_agent_steps": 0}})
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    return tag, client


@pytest.mark.parametrize("name", MISCONFIGURED + ("prod_codex_api_key_rejected",))
def test_misconfigured_credential_prints_its_sentence(monkeypatch, tmp_path, capsys, name):
    tag, client = _interrupted_run(monkeypatch, tmp_path, BY_NAME[name]["message"])
    printed = capsys.readouterr().out
    assert tag == "auth-failure"
    assert client.submissions[0]["meta"]["failure_kind"] == "auth"
    assert f"  -> {SENTENCES_EN[BY_NAME[name]['expected_signal']]}" in printed


@pytest.mark.parametrize("name", ["prod_dsh_rate_limit", "prod_dsh_transport",
                                  "openai_region_refusal_http403"])
def test_non_auth_failures_print_no_auth_sentence(monkeypatch, tmp_path, capsys, name):
    _interrupted_run(monkeypatch, tmp_path, BY_NAME[name]["message"])
    printed = capsys.readouterr().out
    assert "Radar Mailbox" not in printed
    for text in SENTENCES_EN.values():
        assert text not in printed


def test_correct_credential_run_prints_no_auth_sentence(monkeypatch, tmp_path, capsys):
    """Positive control: a completed run (exception_info null, as in a real
    successful codex result) never reaches the sentence."""
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0, result_data={"exception_info": None,
                    "agent_result": {"n_input_tokens": 9, "n_agent_steps": 3}})
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    assert runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123") == "submitted"
    assert "Radar Mailbox" not in capsys.readouterr().out


def _codex_events(tmp_path, message, count=3):
    path = tmp_path / "jobs" / "a1" / "trial" / "agent" / "codex.txt"
    path.parent.mkdir(parents=True)
    path.write_text("".join(
        json.dumps({"type": "error", "message": message}) + "\n" for _ in range(count)))
    return tmp_path / "jobs"


def test_live_abort_keeps_the_platforms_answer(tmp_path):
    jobs = _codex_events(tmp_path,
        "unexpected status 401 Unauthorized: Incorrect API key provided: sk-fixtu***0000")
    counts, last = {}, {}
    assert _scan_live_account_errors(jobs, "a1", {}, counts, last) == "auth"
    assert auth_failure_signal(last["auth"]) == "api_key_rejected"


def test_live_abort_prints_its_sentence(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    error = LiveAccountTerminalError(
        "live agent repeatedly reported authentication failed; aborting this trial safely")
    error.auth_signal = "no_credential"

    def refuse(*_a, **_kw):
        raise error

    monkeypatch.setattr(runloop, "run_trial", refuse)
    client = SubmitClient({})
    client.mark_stopped = lambda aid, **kw: {"ok": True}
    runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    assert SENTENCES_EN["no_credential"] in capsys.readouterr().out


def test_status_prints_the_servers_sentence(monkeypatch, capsys):
    explanation = SHARED["sentences"]["api_key_rejected"]
    payload = {"nickname": "v", "points": 0, "submissions": [
        {"submission_id": "s1", "task_id": "t1", "model": "gpt-5.6-luna", "effort": "high",
         "submitted_at": "2026-09-20T18:55:49+00:00", "graded_at": None,
         "grade_status": "invalid", "reward": None, "flags": [], "public": False,
         "client_exception": "auth", "failure_explanation": explanation},
        {"submission_id": "s2", "task_id": "t2", "model": "gpt-5.6-luna", "effort": "high",
         "submitted_at": "2026-09-20T18:50:00+00:00", "graded_at": None,
         "grade_status": "invalid", "reward": None, "flags": [], "public": False,
         "client_exception": "rate-limit", "failure_explanation": None},
    ]}
    monkeypatch.setattr(identity, "_load_config", lambda: {"server": "https://x", "token": "t"})
    monkeypatch.setattr(identity, "_client", lambda cfg: FakeStatusClient(payload))
    monkeypatch.setattr(identity.pending, "load", lambda home: [])
    identity.cmd_status(SimpleNamespace())
    out = capsys.readouterr().out
    assert out.count(explanation["en"]) == 1
    assert "provider authentication failed" in out
