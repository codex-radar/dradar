"""Normalized subscription usage sidecars are accepted only when auditable."""

from __future__ import annotations

import json

from dradar.runloop import _subscription_trial_usage


def test_subscription_usage_requires_timed_events_to_match_aggregate(tmp_path) -> None:
    agent = tmp_path / "agent"
    agent.mkdir()
    payload = {
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "zcode",
        "model": "glm-5.3",
        "complete": True,
        "request_count": 2,
        "n_input_tokens": 300,
        "n_cache_tokens": 120,
        "n_output_tokens": 30,
        "cache_creation_tokens": 0,
        "request_usage_complete": True,
        "timed_usage_complete": True,
        "token_usage_events": [
            {
                "occurred_at": "2026-08-18T05:59:59Z",
                "n_input_tokens": 100,
                "n_cache_tokens": 40,
                "n_output_tokens": 10,
            },
            {
                "occurred_at": "2026-08-18T06:00:00Z",
                "n_input_tokens": 200,
                "n_cache_tokens": 80,
                "n_output_tokens": 20,
            },
        ],
    }
    (agent / "provider-usage.json").write_text(json.dumps(payload))

    facts = _subscription_trial_usage(
        tmp_path, {"zcode_cli_version": "0.16.3"},
    )

    assert facts is not None
    assert facts["n_input_tokens"] == 300
    assert facts["n_cache_tokens"] == 120

    payload["n_input_tokens"] += 1
    (agent / "provider-usage.json").write_text(json.dumps(payload))
    assert _subscription_trial_usage(
        tmp_path, {"zcode_cli_version": "0.16.3"},
    ) is None


def test_context_banded_usage_accepts_complete_untimed_request_ledger(tmp_path) -> None:
    agent = tmp_path / "agent"
    agent.mkdir()
    payload = {
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "grok",
        "model": "grok-4.6",
        "complete": True,
        "request_count": 2,
        "n_input_tokens": 300,
        "n_cache_tokens": 120,
        "n_output_tokens": 30,
        "request_usage_complete": True,
        "timed_usage_complete": False,
        "token_usage_events": [
            {"n_input_tokens": 100, "n_cache_tokens": 40,
             "n_output_tokens": 10},
            {"n_input_tokens": 200, "n_cache_tokens": 80,
             "n_output_tokens": 20},
        ],
    }
    (agent / "provider-usage.json").write_text(json.dumps(payload))

    facts = _subscription_trial_usage(
        tmp_path, {"grok_cli_version": "1.0.3"},
    )

    assert facts is not None
    assert facts["request_usage_complete"] is True
    assert facts["timed_usage_complete"] is False
    assert len(facts["token_usage_events"]) == 2


def test_zcode_incomplete_reason_survives_without_token_totals(tmp_path) -> None:
    agent = tmp_path / "agent"
    agent.mkdir()
    payload = {
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "zcode",
        "model": "glm-5.3",
        "complete": False,
        "request_count": 0,
        "n_input_tokens": 0,
        "n_cache_tokens": 0,
        "n_output_tokens": 0,
        "cache_creation_tokens": 0,
        "timed_usage_complete": False,
        "token_usage_events": [],
        "usage_incomplete_reason": "provider_aggregate_missing_or_invalid",
        "timed_usage_incomplete_reason": "provider_aggregate_missing_or_invalid",
    }
    (agent / "provider-usage.json").write_text(json.dumps(payload))

    facts = _subscription_trial_usage(
        tmp_path, {"zcode_cli_version": "0.16.3"},
    )

    assert facts is not None
    assert facts["complete"] is False
    assert facts["usage_incomplete_reason"] == (
        "provider_aggregate_missing_or_invalid"
    )

    payload["usage_incomplete_reason"] = "contains user supplied text"
    (agent / "provider-usage.json").write_text(json.dumps(payload))
    assert _subscription_trial_usage(
        tmp_path, {"zcode_cli_version": "0.16.3"},
    ) is None


def test_unreconciled_grok_ledger_retains_observed_tokens(tmp_path) -> None:
    agent = tmp_path / "agent"
    agent.mkdir()
    payload = {
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "grok",
        "model": "grok-4.6",
        "complete": False,
        "request_count": 2,
        "n_input_tokens": 300,
        "n_cache_tokens": 120,
        "n_output_tokens": 30,
        "request_usage_complete": False,
        "request_usage_observed": True,
        "timed_usage_complete": False,
        "usage_evidence_tier": "observed_unreconciled",
        "usage_incomplete_reason": (
            "terminal_aggregate_missing_or_inconsistent"
        ),
        "token_usage_events": [
            {"n_input_tokens": 100, "n_cache_tokens": 40,
             "n_output_tokens": 10},
            {"n_input_tokens": 200, "n_cache_tokens": 80,
             "n_output_tokens": 20},
        ],
    }
    (agent / "provider-usage.json").write_text(json.dumps(payload))

    facts = _subscription_trial_usage(
        tmp_path, {"grok_cli_version": "1.0.3"},
    )

    assert facts is not None
    assert facts["complete"] is False
    assert facts["request_usage_observed"] is True
    assert facts["n_input_tokens"] == 300
    assert len(facts["token_usage_events"]) == 2

    payload["n_input_tokens"] += 1
    (agent / "provider-usage.json").write_text(json.dumps(payload))
    assert _subscription_trial_usage(
        tmp_path, {"grok_cli_version": "1.0.3"},
    ) is None
