import json

from dradar import cli
from dradar.agent_schema import command_schema


def test_run_schema_defines_every_state_changing_argument():
    payload = command_schema("run")

    assert payload["schema_version"] == 1
    assert payload["result_contract"]["unknown_schema_version"] == "fail_closed"
    arguments = {item["name"]: item for item in payload["arguments"]}
    assert arguments["--plan"]["idempotency"]
    assert arguments["--concurrency"]["state_change"]
    assert arguments["--upload-only"]["conflicts_with"] == [
        "--concurrency", "--decision-token", "--recheck-generation",
    ]
    assert "不登记设备" in arguments["--upload-only"]["state_change"]
    assert arguments["--decision-token"]["decision_required"] is True
    assert payload["interaction_rules"]["fixed_capacity_shortfall"] == (
        "honor_explicit_count_without_local_estimates"
    )
    assert payload["interaction_rules"]["capacity_temporarily_zero"] == (
        "poll_then_replay_base_run_without_old_choices"
    )
    assert payload["environment_contract"]["wire_harness_aliases"] == {
        "dsh": "dsh-minimal",
    }
    assert payload["environment_contract"]["capability_set"] == (
        "all_unique_assignment_capabilities"
    )
    assert payload["environment_contract"]["unknown_capability"] == (
        "fail_closed_before_server_start"
    )
    assert payload["result_contract"]["plan_recovery"].startswith(
        "agent.next_commands 中 mode=replay_plan_command"
    )
    assert "不得继承旧 decision-token" in payload["result_contract"]["recheck_plan"]
    assert arguments["--recheck-generation"]["agent_only"] is True
    assert arguments["--recheck-generation"]["idempotency"] == (
        "single_use_generation"
    )
    assert all(
        code == code.lower()
        for item in arguments.values()
        for code in item["failure_codes"]
    )
    for item in arguments.values():
        assert set((
            "user_intent", "allowed_when", "default", "state_change",
            "decision_required", "conflicts_with", "idempotency",
            "failure_codes",
        )).issubset(item)


def test_cli_emits_machine_readable_agent_schema(capsys):
    assert cli.main(["schema", "stop", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "stop"
    scope = next(item for item in payload["arguments"] if item["name"] == "--scope")
    assert scope["decision_required"] is True
    assert "all-devices" in scope["allowed_when"]
