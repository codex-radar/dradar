"""Normalized subscription usage sidecars are accepted only when auditable."""

from __future__ import annotations

import json

import pytest

from dradar.runloop import (
    _antigravity_terminal_observation,
    _subscription_trial_usage,
)


def _antigravity_payload(
    *, status: str, category: str | None = None, complete: bool = True,
    recovery: dict | None = None,
) -> dict:
    request_count = 1 if complete else 0
    payload = {
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "antigravity",
        "model": "gemini-3.7-flash",
        "provider_runtime_model": "gemini-3.7-flash-low",
        "complete": complete,
        "request_count": request_count,
        "n_input_tokens": 10 if complete else 0,
        "n_cache_tokens": 4 if complete else 0,
        "n_output_tokens": 2 if complete else 0,
        "thinking_tokens": 1 if complete else 0,
        "request_usage_complete": complete,
        "request_usage_observed": complete,
        "timed_usage_complete": False,
        "usage_incomplete_reason": (
            None if complete else "request_ledger_unavailable_or_invalid"
        ),
        "usage_evidence_tier": (
            "complete_reconciled" if complete else "unavailable"
        ),
        "token_usage_events": ([{
            "n_input_tokens": 10,
            "n_cache_tokens": 4,
            "n_output_tokens": 2,
            "thinking_tokens": 1,
        }] if complete else []),
        "terminal_status": status,
    }
    if category is not None:
        payload["terminal_error_category"] = category
    if recovery is not None:
        payload["terminal_recovery"] = recovery
    return payload


def _write_antigravity_payload(tmp_path, payload: dict) -> None:
    agent = tmp_path / "agent"
    agent.mkdir(exist_ok=True)
    (agent / "provider-usage.json").write_text(json.dumps(payload))


def test_antigravity_terminal_observation_is_fail_closed_and_stateless(
    tmp_path,
) -> None:
    _write_antigravity_payload(
        tmp_path,
        _antigravity_payload(
            status="ERROR", category="eligibility-location", complete=False,
        ),
    )
    assert _antigravity_terminal_observation(tmp_path, "1.1.22") == (
        True, "eligibility-location",
    )

    # A later SUCCESS, including a natural recovery on the same low tier, is
    # evaluated from its own terminal sidecar and is not poisoned by history.
    _write_antigravity_payload(
        tmp_path,
        _antigravity_payload(status="SUCCESS", complete=True),
    )
    assert _antigravity_terminal_observation(tmp_path, "1.1.22") == (
        False, None,
    )


def test_antigravity_complete_error_requires_narrow_recovery_candidate(
    tmp_path,
) -> None:
    ordinary = _antigravity_payload(
        status="ERROR", category="provider-error", complete=True,
    )
    _write_antigravity_payload(tmp_path, ordinary)
    assert _antigravity_terminal_observation(tmp_path, "1.1.22") == (
        True, "provider-error",
    )

    ordinary.update({
        "terminal_error_category": "stream-interrupted",
        "terminal_recovery": {
            "schema": "dradar-antigravity-terminal-recovery-v1",
            "reason": "stream_interrupted_after_final_response",
            "response_sha256": "a" * 64,
        },
    })
    _write_antigravity_payload(tmp_path, ordinary)
    assert _antigravity_terminal_observation(tmp_path, "1.1.22") == (
        False, "stream-interrupted",
    )


def test_antigravity_bad_sidecar_cannot_upload_text_or_become_success(
    tmp_path,
) -> None:
    payload = _antigravity_payload(
        status="ERROR", category="account@example.invalid private prompt",
    )
    payload["terminal_error"] = "raw account/location/prompt must never upload"
    _write_antigravity_payload(tmp_path, payload)

    assert _subscription_trial_usage(
        tmp_path, {"antigravity_cli_version": "1.1.22"},
    ) is None
    assert _antigravity_terminal_observation(tmp_path, "1.1.22") == (True, None)


@pytest.mark.parametrize(
    "dradar_version",
    ["0.5.170", "0.5.171", "0.5.172", "0.5.173", "0.5.174", "0.5.175"],
)
def test_legacy_antigravity_sidecar_stays_compatible_and_fail_closed(
    tmp_path, dradar_version: str,
) -> None:
    payload = _antigravity_payload(status="ERROR", complete=True)
    assert "terminal_error_category" not in payload
    _write_antigravity_payload(tmp_path, payload)

    parsed = _subscription_trial_usage(tmp_path, {
        "antigravity_cli_version": "1.1.22",
        "dradar_version": dradar_version,
    })
    assert parsed is not None
    assert _antigravity_terminal_observation(tmp_path, "1.1.22") == (True, None)


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
