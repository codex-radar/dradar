"""Strict Claude Code ATIF to subscription-usage reconciliation."""

from __future__ import annotations

import math
import json
from pathlib import Path


def _nonnegative_int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def observed_claude_models(session_dir: Path | None) -> set[str] | None:
    """Read response model IDs from Claude's native assistant messages."""
    if session_dir is None:
        return None
    models: set[str] = set()
    for path in session_dir.rglob("*.jsonl"):
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    event = json.loads(line)
                    if not isinstance(event, dict) or event.get("type") != "assistant":
                        continue
                    message = event.get("message")
                    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                        continue
                    model = message.get("model")
                    if not isinstance(model, str) or not model:
                        return None
                    models.add(model)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
    return models or None


def claude_usage_facts(trajectory: object, expected_model: str) -> dict | None:
    """Reconcile stock Pier's Claude ATIF steps into a strict usage ledger."""

    if not isinstance(trajectory, dict):
        return None
    agent = trajectory.get("agent")
    metrics = trajectory.get("final_metrics")
    steps = trajectory.get("steps")
    if not isinstance(agent, dict) or not isinstance(metrics, dict) or not isinstance(steps, list):
        return None
    model = agent.get("model_name")
    if model != expected_model:
        return None
    events = []
    totals = {"n_input_tokens": 0, "n_cache_tokens": 0, "n_output_tokens": 0}
    cache_creation = 0
    cache_creation_5m = 0
    cache_creation_1h = 0
    timestamps_complete = True
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("metrics"), dict):
            continue
        if step.get("source") == "agent" and step.get("model_name") != expected_model:
            return None
        current = step["metrics"]
        prompt = _nonnegative_int(current.get("prompt_tokens"))
        cached = _nonnegative_int(current.get("cached_tokens"))
        completion = _nonnegative_int(current.get("completion_tokens"))
        if prompt is None or cached is None or completion is None or cached > prompt:
            return None
        extra = current.get("extra") if isinstance(current.get("extra"), dict) else {}
        created = _nonnegative_int(extra.get("cache_creation_input_tokens", 0))
        if created is None or cached + created > prompt:
            return None
        creation_detail = extra.get("cache_creation")
        if expected_model == "claude-opus-5-5":
            if not isinstance(creation_detail, dict) and created:
                return None
            creation_detail = creation_detail or {}
            write_5m = _nonnegative_int(creation_detail.get("ephemeral_5m_input_tokens", 0))
            write_1h = _nonnegative_int(creation_detail.get("ephemeral_1h_input_tokens", 0))
            if write_5m is None or write_1h is None or write_5m + write_1h != created:
                return None
        else:
            write_5m, write_1h = created, 0
        event = {
            "n_input_tokens": prompt,
            "n_cache_tokens": cached,
            "n_cache_write_tokens": created,
            "n_cache_write_5m_tokens": write_5m,
            "n_cache_write_1h_tokens": write_1h,
            "n_output_tokens": completion,
        }
        occurred_at = step.get("timestamp")
        if isinstance(occurred_at, str) and occurred_at:
            event["occurred_at"] = occurred_at
        else:
            timestamps_complete = False
        events.append(event)
        totals["n_input_tokens"] += prompt
        totals["n_cache_tokens"] += cached
        totals["n_output_tokens"] += completion
        cache_creation += created
        cache_creation_5m += write_5m
        cache_creation_1h += write_1h
    if not events:
        return None
    expected = {
        "n_input_tokens": _nonnegative_int(metrics.get("total_prompt_tokens")),
        "n_cache_tokens": _nonnegative_int(metrics.get("total_cached_tokens")),
        "n_output_tokens": _nonnegative_int(metrics.get("total_completion_tokens")),
    }
    if any(value is None for value in expected.values()) or totals != expected:
        return None
    final_extra = metrics.get("extra") if isinstance(metrics.get("extra"), dict) else {}
    final_created = _nonnegative_int(final_extra.get("total_cache_creation_input_tokens", 0))
    if final_created is None or final_created != cache_creation:
        return None
    reported_cost = metrics.get("total_cost_usd")
    if not isinstance(reported_cost, (int, float)) or isinstance(reported_cost, bool):
        reported_cost = None
    elif not math.isfinite(float(reported_cost)) or reported_cost < 0:
        return None
    return {
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "claude-code",
        "model": model,
        "complete": True,
        "request_count": len(events),
        **totals,
        "cache_creation_tokens": cache_creation,
        "n_cache_write_tokens": cache_creation,
        "n_cache_write_5m_tokens": cache_creation_5m,
        "n_cache_write_1h_tokens": cache_creation_1h,
        "subscription_reported_cost_usd": reported_cost,
        "subscription_reported_cost_basis": "official-claude-cli-api-equivalent",
        "provider_actual_cost_observed": False,
        "cost_semantics": "api_equivalent_only",
        "token_usage_events": events,
        "request_usage_complete": True,
        "request_usage_observed": True,
        "timed_usage_complete": timestamps_complete,
        "usage_incomplete_reason": None,
        "usage_evidence_tier": "complete_reconciled",
    }


__all__ = ["claude_usage_facts"]
