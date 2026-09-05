"""Inert protocol fixtures: no shell, Docker, model or production calls."""

import copy
import json
from types import SimpleNamespace

import pytest

from dradar.agent_actions import ActionValidationError, validate_actions
from dradar import run_plans


def response(action="monitor", **overrides):
    return {
        "schema_version": 1, "status": "running", "interaction": "notify",
        "decision_required": False, "retryable": False,
        "user_message": "正在检查当前运行。", "agent_action": action,
        "error_code": None, "choices": [], **overrides,
    }


def replay(action="recover_upload"):
    args = (["--upload-only", "--json"] if action == "recover_upload"
            else ["--recheck-generation", "7", "--json"])
    return {"mode": "replay_plan_command", "command": "run", "args": args,
            "inherit": ["--plan", "--server"], "interactive": False}


@pytest.mark.parametrize("action", ["recover_upload", "recheck_plan"])
def test_recovery_reconstructs_exact_scope_without_mutating_input(action):
    original = response(action, poll_after_seconds=30,
                        agent={"next_commands": [replay(action)]})
    saved = copy.deepcopy(original)
    result = validate_actions(original)
    assert original == saved
    assert result["action_contract_version"] == 1
    assert result["next_commands"] == [replay(action)]
    assert result["next_commands"][0] is not original["agent"]["next_commands"][0]


@pytest.mark.parametrize("key,value", [
    ("schema_version", True), ("decision_required", "false"),
    ("retryable", 0), ("agent_action", "unsupported-action"),
    ("agent_action", []), ("choices", {}), ("user_message", None),
    ("poll_after_seconds", True), ("poll_after_seconds", float("nan")),
    ("poll_after_seconds", 10**1000),
])
def test_envelope_rejects_wrong_types_and_unknown_actions(key, value):
    with pytest.raises(ActionValidationError):
        validate_actions(response(**{key: value}))


@pytest.mark.parametrize("key,value", [
    ("command", "stop"), ("inherit", ["--plan", "--server", "--concurrency"]),
    ("inherit", ["--server", "--plan"]), ("interactive", 0),
    ("args", ["--upload-only", "--json", "--unsupported-option"]),
    ("args", ["--upload-only", True]), ("mode", "unknown-mode"),
    ("server", "https://other.example.invalid"),
    ("plan", "different-inert-plan"),
])
def test_replay_rejects_scope_and_parameter_changes(key, value):
    entry = {**replay(), key: value}
    with pytest.raises(ActionValidationError):
        validate_actions(response("recover_upload", agent={"next_commands": [entry]}))


@pytest.mark.parametrize("args", [
    ["--json"], ["--recheck-generation", "0", "--json"],
    ["--recheck-generation", "07", "--json"],
    ["--recheck-generation", str(2**63), "--json"],
])
def test_recheck_requires_exact_positive_generation(args):
    entry = {**replay("recheck_plan"), "args": args}
    with pytest.raises(ActionValidationError):
        validate_actions(response("recheck_plan", poll_after_seconds=30,
                                  agent={"next_commands": [entry]}))


@pytest.mark.parametrize("harness", [
    "codex", "claude-code", "dsh-minimal", "grok-build", "kimi-code",
    "zcode", "antigravity", "codebuddy",
])
def test_known_environment_checks_are_rebuilt(harness):
    entry = {"argv": ["dradar", "doctor", "--agent", harness], "interactive": False}
    agent = validate_actions(response(agent={"next_commands": [entry],
                                            "environment_scope": {"harness": harness}}))
    assert agent["next_commands"] == [entry]
    assert agent["next_commands"][0]["argv"] is not entry["argv"]


@pytest.mark.parametrize("argv", [
    ["unsupported-tool", "status"], ["dradar", "doctor", "--agent", "unknown"],
    ["dradar", "doctor", "--agent", "codex", "--extra"],
    ["dradar", "fleet", "stop"], ["dradar", "provider", "setup", "unknown"],
])
def test_unknown_environment_commands_are_not_an_execution_api(argv):
    with pytest.raises(ActionValidationError):
        validate_actions(response(agent={"next_commands": [{"argv": argv, "interactive": False}]}))


def test_interactive_login_cannot_be_marked_automatic_and_duplicates_rejected():
    login = {"argv": ["codex", "login"], "interactive": True}
    with pytest.raises(ActionValidationError):
        validate_actions(response(agent={"next_commands": [login]}))
    valid = response(agent={"requires_user_action": True, "next_commands": [login],
                            "environment_scope": {"harness": "codex"}})
    assert validate_actions(valid)["requires_user_action"] is True
    valid["agent"]["next_commands"].append(login)
    with pytest.raises(ActionValidationError):
        validate_actions(valid)


def decision():
    return response("ask_user", decision_required=True, decision_token="inert-decision",
                    choices=[{"id": "join_existing", "label": "加入"},
                             {"id": "cancel", "label": "取消"}],
                    agent={"choice_actions": {
                        "join_existing": {"mode": "replay_current_command_with_args",
                                          "args": ["--decision-token", "inert-decision"]},
                        "cancel": {"mode": "no_command", "args": []},
                    }})


def test_confirmation_has_no_automatic_action_and_cancel_has_no_command():
    value = decision()
    assert validate_actions(value)["choice_actions"]["cancel"]["args"] == []
    value["agent"]["next_commands"] = [replay()]
    with pytest.raises(ActionValidationError):
        validate_actions(value)


@pytest.mark.parametrize("args", [
    ["--decision-token", "different-inert-decision"],
    ["--decision-token", "inert-decision", "--concurrency", "2"],
    ["--server", "https://other.example.invalid"],
])
def test_choice_only_replays_the_local_template_and_current_decision(args):
    value = decision()
    value["agent"]["choice_actions"]["join_existing"]["args"] = args
    with pytest.raises(ActionValidationError):
        validate_actions(value)


def test_no_commands_in_nondecision_choice_mapping():
    value = decision()
    value.update(decision_required=False, agent_action="monitor", choices=[])
    with pytest.raises(ActionValidationError):
        validate_actions(value)


def test_server_cannot_supply_executable_fields_at_either_level():
    remote = {key: {"untrusted": "inert"} for key in
              ("agent", "next_commands", "choice_actions", "followup_launcher")}
    value = {"schema_version": 1, **remote,
             "envelope": {**response(), **remote}}
    result = run_plans._agent_response_from_server(value)
    assert all(key not in result for key in remote)
    assert all(key not in result.get("agent", {}) for key in remote)


def test_invalid_output_is_one_sanitized_diagnostic_and_no_followup(capsys):
    args = SimpleNamespace(json=True, plan="inert-private-run-code")
    value = response(agent={"next_commands": [{
        "argv": ["unsupported-tool", args.plan], "interactive": False,
    }]})
    assert run_plans._output(args, value) == 1
    raw = capsys.readouterr().out
    result = json.loads(raw)
    assert args.plan not in raw and "unsupported-tool" not in raw
    assert "允许范围" in result["user_message"]
    assert result["agent_action"] == "stop"
    assert "next_commands" not in result["agent"]


def test_specific_human_error_masks_known_run_capability(capsys, monkeypatch):
    monkeypatch.setattr(run_plans, "_followup_launcher", lambda: None)
    args = SimpleNamespace(json=True, plan="inert-private-run-code")
    value = response(user_message="本机环境未就绪：" + args.plan)
    assert run_plans._output(args, value) == 0
    result = json.loads(capsys.readouterr().out)
    assert "本机环境未就绪" in result["user_message"]
    assert args.plan not in result["user_message"]
    assert "已脱敏" in result["user_message"]


def test_environment_check_cannot_switch_to_another_plan_tool():
    with pytest.raises(ActionValidationError, match="运行范围"):
        validate_actions(response(agent={
            "environment_scope": {"harness": "codex"},
            "next_commands": [{"argv": ["dradar", "doctor", "--agent", "claude-code"],
                               "interactive": False}],
        }))


@pytest.mark.parametrize("field", ["plan_id", "batch_id", "benchmark_id", "harness"])
def test_changed_plan_scope_is_rejected_before_persistence(field, monkeypatch):
    # A minimal local fixture avoids network, credentials and real assignments.
    original = {"plan_id": "plan-a", "batch_id": "batch-a",
                "benchmark_id": "benchmark-a", "harness": "codex"}
    changed = {**original, field: "different-inert-scope"}
    monkeypatch.setattr(run_plans, "_validate_plan", lambda value: value)
    monkeypatch.setattr(run_plans, "_atomic_json",
                        lambda *_: pytest.fail("scope changes must not be saved"))
    state = {"plan": original}
    with pytest.raises(run_plans.RunPlanClientError, match="范围已改变"):
        run_plans._remember_response(None, state, {"plan": changed}, command="run")
    assert state["plan"] is original


def test_other_server_cannot_replace_saved_scope():
    saved = (None, {"server": "https://api.example.invalid"})
    with pytest.raises(run_plans.RunPlanClientError) as exc:
        run_plans._resolve_server("https://other.example.invalid", saved)
    assert exc.value.code == "server_scope_mismatch"


def test_transmitted_launcher_cannot_change_the_local_revision(monkeypatch, capsys):
    trusted = {"schema_version": 1, "mode": "uvx_offline_git_revision",
               "argv_prefix": ["uvx", "--offline", "--from",
                               "git+https://github.com/codex-radar/dradar@" + "a" * 40,
                               "dradar"], "interactive": False}
    other = copy.deepcopy(trusted)
    other["argv_prefix"][3] = "git+https://github.com/codex-radar/dradar@" + "b" * 40
    monkeypatch.setattr(run_plans, "_followup_launcher", lambda: trusted)
    assert run_plans._output(SimpleNamespace(json=True),
                             response(agent={"followup_launcher": other})) == 0
    actual = json.loads(capsys.readouterr().out)["agent"]["followup_launcher"]
    assert actual == trusted
