from __future__ import annotations

import os
import json
from pathlib import Path

import pytest

import dradar.providers as providers
import dradar.runner as runner
from dradar.claude_usage import claude_usage_facts, observed_claude_models
from dradar.runloop import _subscription_trial_usage


def _token() -> str:
    return "sk-ant-oat01-" + "x" * 64


def _assignment(**changes):
    value = {
        "assignment_id": "a-claude-1",
        "task_id": "task-1",
        "agent": providers.CLAUDE_AGENT,
        "provider": providers.CLAUDE_PROVIDER,
        "model": providers.CLAUDE_SONNET_MODEL,
        "effort": "high",
        "agent_version": providers.CLAUDE_CLI_VERSION,
        "est_minutes": 5,
    }
    value.update(changes)
    return value


def test_claude_contract_has_three_cards_and_five_native_efforts() -> None:
    assert providers.CLAUDE_MODELS == {
        "claude-sonnet-5", "claude-opus-5", "claude-opus-5-5",
    }
    assert providers.CLAUDE_SUPPORTED_EFFORTS == {
        "low", "medium", "high", "xhigh", "max",
    }


def test_claude_oauth_store_is_private_and_rejects_api_keys(
    tmp_path: Path,
) -> None:
    saved = providers.store_claude_oauth_token(_token(), home=tmp_path)
    assert providers.claude_oauth_error(saved) is None
    if os.name != "nt":
        assert saved.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="API keys are not supported"):
        providers.store_claude_oauth_token("sk-ant-api03-secret", home=tmp_path)


def test_claude_capability_requires_private_subscription_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    assert providers.CLAUDE_CAPABILITY not in providers.advertised_capabilities({})
    providers.store_claude_oauth_token(_token(), home=tmp_path)
    assert providers.CLAUDE_CAPABILITY in providers.advertised_capabilities({})


@pytest.mark.parametrize("model", sorted(providers.CLAUDE_MODELS))
@pytest.mark.parametrize("effort", sorted(providers.CLAUDE_SUPPORTED_EFFORTS))
def test_claude_pier_command_uses_file_contract_without_secret_in_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str, effort: str,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    auth = providers.store_claude_oauth_token(_token(), home=tmp_path)

    command = runner.build_pier_command(
        _assignment(model=model, effort=effort), tasks, tmp_path / "jobs",
        "job", tmp_path / "work", provider_auth_path=auth,
    )

    assert runner.CLAUDE_AGENT_IMPORT_PATH in command
    assert f"reasoning_effort={effort}" in command
    assert f"oauth_token_file={auth}" in command
    assert f"version={providers.CLAUDE_CLI_VERSION}" in command
    assert _token() not in " ".join(command)
    assert "CLAUDE_CODE_OAUTH_TOKEN=" not in " ".join(command)
    assert (tmp_path / "work" / runner.CLAUDE_AGENT_MODULE_FILENAME).is_file()
    assert (tmp_path / "work" / runner.CLAUDE_USAGE_MODULE_FILENAME).is_file()


def test_claude_runner_scrubs_ambient_api_and_oauth_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (*providers.CLAUDE_API_KEY_ENVS, "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(name, "must-not-leak")
    env = runner._pier_process_env(_assignment())
    for name in (*providers.CLAUDE_API_KEY_ENVS, "CLAUDE_CODE_OAUTH_TOKEN"):
        assert name not in env


def test_claude_install_prefers_pinned_npm_on_non_alpine_images(
    tmp_path: Path,
) -> None:
    from dradar.pier_claude import ClaudeCodeSubscription

    auth = providers.store_claude_oauth_token(_token(), home=tmp_path)
    agent = ClaudeCodeSubscription(
        logs_dir=tmp_path / "logs", model_name=providers.CLAUDE_OPUS_55_MODEL,
        version=providers.CLAUDE_CLI_VERSION, reasoning_effort="medium",
        oauth_token_file=str(auth),
    )
    spec = agent.install_spec()

    assert spec.version == providers.CLAUDE_CLI_VERSION
    assert "if command -v npm" in spec.steps[-1].run
    assert "npm install -g @anthropic-ai/claude-code@2.1.280" in spec.steps[-1].run
    assert "curl -fsSL https://claude.ai/install.sh" in spec.steps[-1].run
    assert "claude --version" in spec.steps[-1].run


def test_opus_55_requires_new_cli_while_historical_opus_keeps_old_pin() -> None:
    runner._validate_claude_assignment(_assignment(
        model=providers.CLAUDE_OPUS_MODEL,
        agent_version=providers.CLAUDE_LEGACY_CLI_VERSION,
    ))
    with pytest.raises(runner.RunnerError, match="2.1.280"):
        runner._validate_claude_assignment(_assignment(
            model=providers.CLAUDE_OPUS_55_MODEL,
            agent_version=providers.CLAUDE_LEGACY_CLI_VERSION,
        ))


def test_claude_atif_usage_is_reconciled_for_server_repricing(tmp_path: Path) -> None:
    trajectory = {
        "agent": {"model_name": providers.CLAUDE_SONNET_MODEL},
        "steps": [
            {
                "timestamp": "2026-08-31T10:00:00+00:00",
                "metrics": {
                    "prompt_tokens": 120,
                    "cached_tokens": 40,
                    "completion_tokens": 12,
                    "extra": {"cache_creation_input_tokens": 20},
                },
            },
            {
                "timestamp": "2026-08-31T10:00:01+00:00",
                "metrics": {
                    "prompt_tokens": 80,
                    "cached_tokens": 10,
                    "completion_tokens": 8,
                    "extra": {"cache_creation_input_tokens": 5},
                },
            },
        ],
        "final_metrics": {
            "total_prompt_tokens": 200,
            "total_cached_tokens": 50,
            "total_completion_tokens": 20,
            "total_cost_usd": 0.0123,
            "extra": {"total_cache_creation_input_tokens": 25},
        },
    }
    usage = claude_usage_facts(trajectory, providers.CLAUDE_SONNET_MODEL)
    assert usage is not None
    assert usage["complete"] is True
    assert usage["request_count"] == 2
    assert usage["n_input_tokens"] == 200
    assert usage["n_cache_tokens"] == 50
    assert usage["n_output_tokens"] == 20
    assert usage["cache_creation_tokens"] == 25
    assert usage["provider_actual_cost_observed"] is False
    assert usage["cost_semantics"] == "api_equivalent_only"

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "provider-usage.json").write_text(
        json.dumps(usage), encoding="utf-8",
    )
    parsed = _subscription_trial_usage(
        tmp_path, {"claude_cli_version": providers.CLAUDE_CLI_VERSION},
    )
    assert parsed is not None and parsed["complete"] is True
    assert parsed["provider"] == "claude-code"


def test_claude_usage_fails_closed_when_final_totals_disagree() -> None:
    trajectory = {
        "agent": {"model_name": providers.CLAUDE_OPUS_MODEL},
        "steps": [{
            "metrics": {"prompt_tokens": 10, "cached_tokens": 0,
                        "completion_tokens": 2, "extra": {}},
        }],
        "final_metrics": {
            "total_prompt_tokens": 11,
            "total_cached_tokens": 0,
            "total_completion_tokens": 2,
            "extra": {"total_cache_creation_input_tokens": 0},
        },
    }
    assert claude_usage_facts(trajectory, providers.CLAUDE_OPUS_MODEL) is None


def test_native_response_model_evidence_rejects_fallback(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    path = session / "session.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {
        "model": providers.CLAUDE_OPUS_55_MODEL, "usage": {"input_tokens": 1},
    }}) + "\n", encoding="utf-8")
    assert observed_claude_models(session) == {providers.CLAUDE_OPUS_55_MODEL}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "assistant", "message": {
            "model": providers.CLAUDE_OPUS_MODEL, "usage": {"input_tokens": 1},
        }}) + "\n")
    assert observed_claude_models(session) == {
        providers.CLAUDE_OPUS_55_MODEL, providers.CLAUDE_OPUS_MODEL,
    }


def test_opus_55_usage_preserves_both_cache_write_durations() -> None:
    trajectory = {
        "agent": {"model_name": providers.CLAUDE_OPUS_55_MODEL},
        "steps": [{"source": "agent", "model_name": providers.CLAUDE_OPUS_55_MODEL,
                   "metrics": {"prompt_tokens": 100, "cached_tokens": 10,
                               "completion_tokens": 4, "extra": {
                                   "cache_creation_input_tokens": 30,
                                   "cache_creation": {"ephemeral_5m_input_tokens": 20,
                                                      "ephemeral_1h_input_tokens": 10},
                               }}}],
        "final_metrics": {"total_prompt_tokens": 100, "total_cached_tokens": 10,
                          "total_completion_tokens": 4,
                          "extra": {"total_cache_creation_input_tokens": 30}},
    }
    usage = claude_usage_facts(trajectory, providers.CLAUDE_OPUS_55_MODEL)
    assert usage is not None
    assert usage["n_cache_write_5m_tokens"] == 20
    assert usage["n_cache_write_1h_tokens"] == 10
    trajectory["steps"][0]["model_name"] = providers.CLAUDE_OPUS_MODEL
    assert claude_usage_facts(trajectory, providers.CLAUDE_OPUS_55_MODEL) is None
