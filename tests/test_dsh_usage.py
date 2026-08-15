from __future__ import annotations

import json
from pathlib import Path

from dradar import runloop
from dradar.runloop import _apply_usage_to_result, _dsh_trial_usage


def _write_usage(trial_dir: Path, **overrides: object) -> None:
    payload: dict[str, object] = {
        "schema": "dsh-provider-usage-v1",
        "model": "deepseek-v4-flash",
        "uncachedInputTokens": 120,
        "cacheReadTokens": 30,
        "cacheWriteTokens": 5,
        "outputTokens": 17,
        "requestCount": 2,
    }
    payload.update(overrides)
    path = trial_dir / "agent" / "dsh-home" / "dsh-usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_dsh_usage_maps_disjoint_provider_buckets_to_upload_contract(
    tmp_path: Path,
) -> None:
    _write_usage(tmp_path)

    usage = _dsh_trial_usage(tmp_path)

    assert usage is not None
    assert usage["complete"] is True
    assert usage["n_input_tokens"] == 155
    assert usage["n_cache_tokens"] == 30
    assert usage["n_output_tokens"] == 17
    assert usage["cache_write_tokens"] == 5
    assert usage["request_count"] == 2


def test_dsh_usage_rejects_missing_malformed_or_empty_samples(tmp_path: Path) -> None:
    assert _dsh_trial_usage(tmp_path) is None

    _write_usage(tmp_path, cacheReadTokens=-1)
    assert _dsh_trial_usage(tmp_path) is None

    _write_usage(tmp_path, cacheReadTokens=True)
    assert _dsh_trial_usage(tmp_path) is None

    _write_usage(tmp_path, cacheReadTokens=0, requestCount=0)
    assert _dsh_trial_usage(tmp_path) is None


def test_dsh_usage_is_written_into_upload_result(tmp_path: Path) -> None:
    _write_usage(tmp_path)
    usage = _dsh_trial_usage(tmp_path)
    assert usage is not None
    result = tmp_path / "result.json"
    result.write_text(
        json.dumps({"agent_result": {"cost_usd": 999, "metadata": {}}}),
        encoding="utf-8",
    )

    _apply_usage_to_result(result, usage)

    agent = json.loads(result.read_text(encoding="utf-8"))["agent_result"]
    assert agent["cost_usd"] is None
    assert agent["n_input_tokens"] == 155
    assert agent["n_cache_tokens"] == 30
    assert agent["n_output_tokens"] == 17
    assert agent["metadata"]["provider_usage"]["schema"] == (
        "dsh-provider-usage-v1"
    )
    assert "codex_session_usage" not in agent["metadata"]


def test_dsh_upload_sends_token_counters_for_server_side_pricing(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial = tmp_path / "trial"
    (trial / "artifacts").mkdir(parents=True)
    (trial / "artifacts" / "model.patch").write_text(
        "diff --git a/file b/file\n", encoding="utf-8",
    )
    (trial / "result.json").write_text(
        json.dumps({"agent_result": {"cost_usd": None}}), encoding="utf-8",
    )
    _write_usage(trial)

    class CaptureClient:
        def submit(
            self, assignment_id, nonce, patch, trajectory, result, meta,
            outcome="completed", resume_generation=None,
        ):
            assert meta["dsh_version"] == "0.1.0-rc.6"
            assert meta["usage_aggregation"] == "dsh-provider-usage-v1"
            assert meta["usage_aggregation_complete"] is True
            assert meta["n_input_tokens"] == 155
            assert meta["n_cache_tokens"] == 30
            assert meta["n_output_tokens"] == 17
            assert meta["cache_write_tokens"] == 5
            uploaded = json.loads(result.read_text(encoding="utf-8"))
            assert uploaded["agent_result"]["n_input_tokens"] == 155
            return {"submission_id": "submission-1", "grade_status": "pending"}

        def mark_stopped(self, assignment_id, **kwargs):
            raise AssertionError((assignment_id, kwargs))

    outcome = runloop._upload_trial(CaptureClient(), {
        "assignment_id": "assignment-1",
        "nonce": "nonce-1",
        "task_id": "task-1",
        "trial_dir": str(trial),
        "meta": {"dsh_version": "0.1.0-rc.6"},
        "outcome": "completed",
        "job_dir": None,
        "keep": True,
    })

    assert outcome == "submitted"
