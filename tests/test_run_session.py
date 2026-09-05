"""Bounded local-controller fixtures. No model, Docker, or live API calls."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from dradar import run_plans, run_session, session_entry


def args(**kw):
    return SimpleNamespace(plan="inert_plan_code_123456789", server="https://api.example.invalid",
                           concurrency=kw.pop("concurrency", 5), locale="en-US", **kw)


def result(action="monitor", status="running", **kw):
    return 0, dict(schema_version=1, status=status, interaction="notify",
                   decision_required=False, retryable=False, choices=[],
                   error_code=None, agent_action=action, user_message="Progress", **kw)


class Harness:
    def __init__(self, runs, progress=()):
        self.runs, self.progresses = iter(runs), iter(progress)
        self.calls, self.messages, self.waits = [], [], []
        self.now = 0
        self.locked = False

    def run(self, a):
        self.calls.append(("run", a))
        return next(self.runs)

    def progress(self, a):
        self.calls.append(("progress", a))
        return next(self.progresses)

    def sleep(self, seconds):
        assert not self.locked
        self.waits.append(seconds)
        self.now += seconds

    def follow(self, a=None, **kw):
        return run_session.follow_plan(a or args(), run=self.run, progress=self.progress,
                                       emit=self.messages.append, sleep=self.sleep,
                                       clock=lambda: self.now, interactive=kw.pop("interactive", False), **kw)


def test_local_run_progress_and_terminal_never_interpret_response_commands():
    inert = {"next_commands": [{"argv": ["unsupported-inert-tool"]}],
             "choice_actions": {"unused": {"args": ["unsupported-option"]}},
             "followup_launcher": {"argv_prefix": ["unused-inert-launcher"]}}
    h = Harness([result(agent=inert)], [result("done", "completed")])
    assert h.follow() == 0
    assert [name for name, _ in h.calls] == ["run", "progress"]
    assert h.calls[0][1].concurrency == 5
    assert h.calls[1][1].concurrency is None
    assert all(a.plan == args().plan and a.server == args().server for _, a in h.calls)
    assert "unsupported" not in " ".join(h.messages)


def test_upload_recovery_is_direct_scoped_call_without_stale_parameters():
    h = Harness([result(), result()],
                [result("recover_upload", "waiting"), result("done", "completed")])
    assert h.follow() == 0
    upload = h.calls[2][1]
    assert upload.upload_only and upload.concurrency is None
    assert upload.decision_token is upload.recheck_generation is None


def test_upload_retries_remain_bounded_across_monitor_responses():
    h = Harness([result()] * 4, [result("recover_upload", "waiting")] * 4)
    assert h.follow() == 1
    assert sum(a.upload_only for _, a in h.calls) == 3
    assert "retry limit" in " ".join(h.messages)


def test_recheck_uses_local_one_use_generation_not_argv(monkeypatch):
    monkeypatch.setattr(run_plans, "_session_local_state", lambda _: {
        "intent_generation": 7, "pending_recheck_generation": 7})
    h = Harness([result("recheck_plan", "waiting", poll_after_seconds=30,
                        agent={"intent_generation": 7, "next_commands": [{"args": ["unused"]}]}), result()],
                [result("done", "completed")])
    assert h.follow() == 0
    assert h.calls[1][1].recheck_generation == 7
    assert h.calls[1][1].concurrency is None


@pytest.mark.parametrize("generation", [None, True, "7", 8])
def test_stale_or_malformed_recheck_cannot_start_work(monkeypatch, generation):
    monkeypatch.setattr(run_plans, "_session_local_state", lambda _: {
        "intent_generation": 7, "pending_recheck_generation": 7})
    h = Harness([result("recheck_plan", "waiting", poll_after_seconds=30,
                        agent={"intent_generation": generation})])
    assert h.follow() == 1 and len(h.calls) == 1


def decision(choice="install", name="install_recommended_docker"):
    code, value = result("ask_user", "decision_required")
    value.update(decision_required=True, decision=name, decision_token="drdi_inert_confirmation",
                 choices=[{"id": choice, "label": "Confirm"}, {"id": "cancel", "label": "Cancel"}],
                 agent={"choice_actions": {"ignored": {"args": ["unsupported-inert-option"]}}})
    return code, value


def confirmation_state(monkeypatch, tmp_path):
    from test_run_plans import _plan, _state
    path, state = _state(tmp_path / "run-plans", _plan(mode="fixed", concurrency=5, task_count=5))
    state.update(server=args().server, run_code_hash=run_plans._run_code_digest(args().plan), intent_generation=1,
                 pending_docker_install={"token_hash": hashlib.sha256(decision()[1]["decision_token"].encode()).hexdigest()})
    run_plans._atomic_json(path, state)
    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(run_plans, "_state_in_active_fleet", lambda *_args, **_kw: False)
    return path


def test_noninteractive_confirmation_pauses_without_input_or_replay(monkeypatch, tmp_path):
    confirmation_state(monkeypatch, tmp_path)
    h = Harness([decision()])
    assert h.follow(read_choice=lambda _: pytest.fail("no interactive input")) == 2
    assert len(h.calls) == 1 and not h.waits
    assert "Choose explicitly in the conversation" in " ".join(h.messages)
    assert decision()[1]["decision_token"] not in " ".join(h.messages)


def issued(monkeypatch, tmp_path):
    path = confirmation_state(monkeypatch, tmp_path)
    h = Harness([decision()])
    assert h.follow() == 2
    event = json.loads(next(m[len("DRADAR_CONFIRMATION "):] for m in h.messages if m.startswith("DRADAR_CONFIRMATION ")))
    assert set(event) == {"version", "request", "choices"}
    assert path.stat().st_mode & 0o777 == 0o600
    return path, event


def test_non_tty_pause_explicit_choice_resumes_same_scope_only_once(monkeypatch, tmp_path):
    path, event = issued(monkeypatch, tmp_path)
    selected = args(confirmation=event["request"], choice="install")
    resumed = Harness([result("done", "completed")])
    assert resumed.follow(selected) == 0
    call = resumed.calls[0][1]
    assert call.plan == args().plan and call.server == args().server and call.concurrency == 5
    assert call.docker_install_token == decision()[1]["decision_token"]
    assert json.loads(path.read_text())["pending_session_confirmation"] is None
    repeated = Harness([])
    assert repeated.follow(selected) == 1 and not repeated.calls


@pytest.mark.parametrize("change", ["expired", "generation", "server", "plan", "concurrency"])
def test_non_tty_ack_rejects_changed_or_expired_authority(monkeypatch, tmp_path, change):
    path, event = issued(monkeypatch, tmp_path)
    selected = args(confirmation=event["request"], choice="install")
    state = json.loads(path.read_text())
    if change == "expired": state["pending_session_confirmation"]["expires_at"] = 0
    if change == "generation": state["intent_generation"] += 1
    if change == "server": selected.server = "https://other.example.invalid"
    if change == "plan": selected.plan = "different_inert_plan_123456789"
    if change == "concurrency": selected.concurrency = 6
    run_plans._atomic_json(path, state)
    h = Harness([])
    assert h.follow(selected) == 1 and not h.calls


def test_non_tty_cancel_does_not_start_or_stop_work(monkeypatch, tmp_path):
    path, event = issued(monkeypatch, tmp_path)
    h = Harness([])
    assert h.follow(args(confirmation=event["request"], choice="cancel")) == 0
    assert not h.calls
    assert json.loads(path.read_text())["pending_docker_install"] is None


def test_recheck_preserves_explicit_count_below_the_saved_plan_ceiling(monkeypatch, tmp_path):
    from test_run_plans import _plan, _prepare_run, _server_response, _capacity_error, _args, FakeClient
    from dradar import fleet
    plan = _plan(mode="fixed", concurrency=5, task_count=5)
    client = FakeClient(starts=[_capacity_error(requested=2, available=0, original_mode="fixed"),
                               _server_response(plan)])
    _prepare_run(monkeypatch, tmp_path, plan=plan, client=client)
    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(fleet, "prepare_new_batch_runtime", lambda **_: None)
    monkeypatch.setattr(fleet, "add_batch", lambda **kw: {"batch": {"workers": kw["workers"]}})
    code, waiting = run_plans._run_plan_operation(run_session._base(_args(), concurrency=2))
    assert code == 0 and waiting["agent_action"] == "recheck_plan"
    generation = waiting["agent"]["intent_generation"]
    code, running = run_plans._run_plan_operation(run_session._base(_args(), recheck_generation=generation))
    assert code == 0 and running["agent_action"] == "monitor"
    assert [call["concurrency"] for call in client.start_calls] == [2, 2]


def test_interactive_cancel_never_replays_or_stops_a_background_job():
    h = Harness([decision()])
    assert h.follow(interactive=True, read_choice=lambda _: "2") == 0
    assert len(h.calls) == 1
    assert "does not mean background work has stopped" in " ".join(h.messages)


def test_install_requires_explicit_input_and_local_confirmation_state(monkeypatch):
    token = decision()[1]["decision_token"]
    monkeypatch.setattr(run_plans, "_session_local_state", lambda _: {
        "pending_docker_install": {"token_hash": hashlib.sha256(token.encode()).hexdigest()}})
    h = Harness([decision(), result()], [result("done", "completed")])
    assert h.follow(interactive=True, read_choice=lambda _: "1") == 0
    assert h.calls[1][1].docker_install_token == token
    assert h.calls[1][1].concurrency == 5
    assert token not in " ".join(h.messages)


def test_cross_device_confirmation_uses_saved_decision_not_remote_commands(monkeypatch):
    monkeypatch.setattr(run_plans, "_session_local_state", lambda _: {
        "pending_decision": {"command": "run", "decision": "join_existing"}})
    h = Harness([decision("join_existing", "join_existing"), result()], [result("done", "completed")])
    assert h.follow(interactive=True, read_choice=lambda _: "1") == 0
    assert h.calls[1][1].decision_token == decision()[1]["decision_token"]


@pytest.mark.parametrize("action,status", [("unsupported-action", "running"), ("monitor", "unknown-state"),
                                           ("done", "running"), ("notify_only", "error")])
def test_unknown_transition_or_expired_scope_fails_closed(action, status):
    h = Harness([result(action, status)])
    assert h.follow() == 1 and len(h.calls) == 1


def test_authentication_is_not_automated_by_response_argv():
    h = Harness([result("authenticate_current_tool", "error", agent={
        "requires_user_action": True, "next_commands": [{"argv": ["codex", "login"]}]})])
    assert h.follow() == 2 and len(h.calls) == 1


def test_local_deadline_does_not_claim_background_stopped():
    h = Harness([result()], [result()])
    assert h.follow(max_seconds=45) == 2
    assert len(h.calls) == 1
    assert "does not mean background work has stopped" in " ".join(h.messages)


def test_raw_plan_and_terminal_escape_sequences_are_not_rendered():
    a = args()
    value = result("done", "completed")[1]
    value["user_message"] = "\x1b[31mresult " + a.plan
    h = Harness([(0, value)])
    assert h.follow(a) == 0
    assert a.plan not in " ".join(h.messages) and "\x1b" not in " ".join(h.messages)


def test_session_operation_keeps_dependency_stdout_and_stderr_private(capfd):
    import os
    import sys
    secret = args().plan
    def operation():
        print(secret)
        print(secret, file=sys.stderr)
        os.write(2, secret.encode())
        return result()[1]
    code, value = run_plans._plan_operation(run_session._base(args()), operation)
    assert code == 0 and value["agent_action"] == "monitor"
    captured = capfd.readouterr()
    assert secret not in captured.out + captured.err


def test_follow_and_json_cannot_silently_change_the_output_contract():
    h = Harness([])
    assert h.follow(args(json=True)) == 2
    assert not h.calls


def test_unexpected_local_error_never_prints_private_exception_text():
    h = Harness([])
    def failed(_):
        raise ValueError(args().plan)
    assert run_session.follow_plan(args(), run=failed, emit=h.messages.append, interactive=False) == 1
    assert args().plan not in " ".join(h.messages)
    assert "ValueError" in " ".join(h.messages)


def test_fixed_entry_reaches_real_local_steps_with_inert_api_and_fleet(monkeypatch, tmp_path, capsys):
    from test_run_plans import _plan, _prepare_run, _server_response, _envelope, FakeClient, RUN_CODE
    from dradar import fleet
    plan = _plan(mode="fixed", concurrency=5, task_count=5)
    client = FakeClient(starts=[_server_response(plan, _envelope(agent_action="start_runner"))],
                        progress=[_server_response(plan, _envelope(status="completed", agent_action="done"))])
    _prepare_run(monkeypatch, tmp_path, plan=plan, client=client)
    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(fleet, "prepare_new_batch_runtime", lambda **_: None)
    monkeypatch.setattr(run_plans, "_exact_pending_uploads", lambda *_: [])
    added = []
    monkeypatch.setattr(fleet, "add_batch", lambda **kw: added.append(kw) or {"batch": {"workers": kw["workers"]}})
    monkeypatch.setattr(session_entry, "verified_source", lambda revision: revision == "a" * 40)
    locking = {"held": False}
    original_admission = run_plans._run_with_admission
    def admission(operation):
        locking["held"] = True
        try:
            return original_admission(operation)
        finally:
            locking["held"] = False
    monkeypatch.setattr(run_plans, "_run_with_admission", admission)
    def sleep(_):
        assert locking["held"] is False, "waiting must release admission"
    monkeypatch.setattr(run_session.time, "sleep", sleep)
    assert session_entry.main(["--revision", "a" * 40, "--plan", RUN_CODE,
                               "--server", "https://api.codexradar.com"]) == 0
    assert client.start_calls[0]["concurrency"] == 5 and added[0]["workers"] == 5
    assert client.progress_calls == [plan["plan_id"]]
    output = capsys.readouterr().out
    assert RUN_CODE not in output and "next_commands" not in output
