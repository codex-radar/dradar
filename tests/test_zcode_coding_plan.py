"""ZCode GLM-5.3 domestic Coding Plan integration contract."""

from __future__ import annotations

import ast
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import dradar.providers as providers
import dradar.provider_config as provider_config
import dradar.runner as runner
from dradar.providers import (
    ZCODE_AGENT,
    ZCODE_API_KEY_ENV,
    ZCODE_CAPABILITY,
    ZCODE_CLI_RELATIVE_PATH,
    ZCODE_CLI_VERSION,
    ZCODE_MODEL,
    ZCODE_PROVIDER,
    advertised_capabilities,
    create_zcode_api_key_file,
    parse_zcode_cli_version,
    store_zcode_cli,
    store_zcode_api_key,
    zcode_cli_candidates,
    zcode_cli_path,
    zcode_secret_error,
)
from dradar.runner import RunnerError


def _assignment(**overrides: object) -> dict:
    value = {
        "assignment_id": "a-zcode-1",
        "task_id": "task-1",
        "agent": ZCODE_AGENT,
        "provider": ZCODE_PROVIDER,
        "model": ZCODE_MODEL,
        "effort": "high",
        "agent_version": ZCODE_CLI_VERSION,
        "est_minutes": 5,
    }
    value.update(overrides)
    return value


def _private(path: Path, value: str = "sentinel-coding-plan-key") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def test_zcode_version_banner_is_parsed() -> None:
    assert parse_zcode_cli_version("0.16.3\n") == ZCODE_CLI_VERSION
    assert parse_zcode_cli_version("zcode 0.16.3\n") == ZCODE_CLI_VERSION
    assert parse_zcode_cli_version("unexpected") is None


def test_zcode_cli_candidates_include_official_macos_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers.sys, "platform", "darwin")
    candidates = zcode_cli_candidates({}, user_home=tmp_path)
    assert Path(
        "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"
    ) in candidates
    assert (
        tmp_path / "Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"
    ) in candidates


def test_zcode_cli_candidates_support_linux_appdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers.sys, "platform", "linux")
    appdir = tmp_path / "mounted-appimage"
    candidates = zcode_cli_candidates(
        {"APPDIR": str(appdir), "DRADAR_HOME": str(tmp_path / "dradar")},
        user_home=tmp_path,
    )
    assert appdir / "resources/glm/zcode.cjs" in candidates
    assert appdir / "usr/lib/zcode/resources/glm/zcode.cjs" in candidates


def test_zcode_cli_path_preserves_invalid_explicit_path_for_diagnostics(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-zcode.cjs"
    assert zcode_cli_path({"ZCODE_CLI_PATH": str(missing)}) == str(missing)


def test_verified_zcode_cli_is_imported_to_local_provider_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "official" / "zcode.cjs"
    source.parent.mkdir()
    source.write_text("verified-runtime", encoding="utf-8")
    monkeypatch.setattr(providers, "zcode_cli_error", lambda _path: None)
    target = store_zcode_cli(source, home=tmp_path / "dradar")
    assert target == tmp_path / "dradar" / ZCODE_CLI_RELATIVE_PATH
    assert target.read_text(encoding="utf-8") == "verified-runtime"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_zcode_setup_imports_official_runtime_before_reading_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    source = tmp_path / "ZCode.app/Contents/Resources/glm/zcode.cjs"
    imported = tmp_path / "dradar/providers/zcode/current/zcode.cjs"
    secret = tmp_path / "dradar/secrets/zcode_coding_plan_api_key"
    monkeypatch.setattr(
        provider_config.sys, "stdin", SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr(provider_config, "zcode_cli_path", lambda: str(source))
    monkeypatch.setattr(provider_config, "zcode_cli_error", lambda _path: None)
    monkeypatch.setattr(provider_config, "store_zcode_cli", lambda _path: imported)
    monkeypatch.setattr(
        provider_config.getpass, "getpass", lambda _prompt: "super-secret-value",
    )
    monkeypatch.setattr(provider_config, "store_zcode_api_key", lambda _key: secret)
    monkeypatch.setattr(provider_config, "_live_zcode_status", lambda _key: 0)

    rc = provider_config.cmd_provider_setup(SimpleNamespace(provider="zcode"))
    output = capsys.readouterr().out
    assert rc == 0
    assert str(imported) in output
    assert str(secret) in output
    assert "super-secret-value" not in output


def test_zcode_setup_stops_before_key_prompt_when_runtime_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(
        provider_config.sys, "stdin", SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr(provider_config, "zcode_cli_path", lambda: None)
    monkeypatch.setattr(
        provider_config, "zcode_cli_error", lambda _path: "official CLI is missing",
    )
    monkeypatch.setattr(
        provider_config.getpass,
        "getpass",
        lambda _prompt: pytest.fail("must not request a key without a runtime"),
    )

    rc = provider_config.cmd_provider_setup(SimpleNamespace(provider="zcode"))
    output = capsys.readouterr().out
    assert rc == 1
    assert "https://zcode.z.ai/cn" in output
    assert "ZCODE_CLI_PATH" in output


def test_zcode_key_storage_and_run_copy_are_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("DRADAR_HOME", str(home))
    stored = store_zcode_api_key("one-line-key")
    run_copy = create_zcode_api_key_file(tmp_path / "work")
    assert stored.read_text(encoding="utf-8") == "one-line-key\n"
    assert run_copy.read_text(encoding="utf-8") == "one-line-key\n"
    if os.name != "nt":
        assert stored.stat().st_mode & 0o777 == 0o600
        assert run_copy.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="one non-empty line"):
        store_zcode_api_key("invalid key")


def test_zcode_secret_rejects_symlink_and_broad_mode(tmp_path: Path) -> None:
    target = _private(tmp_path / "target")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert "not a symlink" in (zcode_secret_error(link) or "")
    if os.name != "nt":
        target.chmod(0o644)
        assert "too broadly readable" in (zcode_secret_error(target) or "")


def test_zcode_capability_requires_key_cli_integrity_and_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ZCODE_CAPABILITY not in advertised_capabilities({})
    monkeypatch.setattr(providers, "zcode_cli_error", lambda *args, **kwargs: None)
    capabilities = advertised_capabilities({
        ZCODE_API_KEY_ENV: "ready",
        "ZCODE_CLI_PATH": "/pinned/zcode.cjs",
    })
    assert ZCODE_CAPABILITY in capabilities


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_pier_command_uses_private_zcode_adapter_without_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effort: str,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    monkeypatch.setenv(ZCODE_API_KEY_ENV, "must-not-leak")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    key = _private(tmp_path / "run-key")
    cli = tmp_path / "zcode.cjs"
    cli.write_text("pinned", encoding="utf-8")
    cmd = runner.build_pier_command(
        _assignment(effort=effort), tasks, tmp_path / "jobs", "job", home,
        provider_auth_path=key, provider_cli_path=cli,
    )
    joined = " ".join(cmd)
    assert runner.ZCODE_AGENT_IMPORT_PATH in cmd
    assert f"reasoning_effort={effort}" in cmd
    assert f"api_key_file={key}" in cmd
    assert f"zcode_cli_file={cli}" in cmd
    assert "session_timeout_sec=3660" in cmd
    assert f"version={ZCODE_CLI_VERSION}" in cmd
    assert "must-not-leak" not in joined
    assert (home / runner.ZCODE_AGENT_MODULE_FILENAME).read_bytes() == (
        Path(runner.__file__).with_name("pier_zcode.py").read_bytes()
    )
    env = runner._pier_process_env(_assignment(), zcode_module_dir=home)
    assert ZCODE_API_KEY_ENV not in env
    assert env["PYTHONPATH"] == str(home)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"provider": "foreign"}, "explicitly use provider"),
        ({"model": "glm-5"}, "unsupported ZCode model"),
        ({"effort": "medium"}, "effort must be low, high, or max"),
        ({"agent_version": "9.9.9"}, "pinned to CLI"),
    ],
)
def test_unverified_zcode_assignment_fails_before_paid_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict,
    message: str,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    assignment = _assignment(**overrides)
    tasks = tmp_path / "tasks"
    (tasks / assignment["task_id"]).mkdir(parents=True)
    with pytest.raises(RunnerError, match=message):
        runner.build_pier_command(
            assignment, tasks, tmp_path / "jobs", "job", tmp_path,
            provider_auth_path=_private(tmp_path / "key"),
            provider_cli_path=tmp_path / "zcode.cjs",
        )


def test_zcode_checkpoint_resume_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    cli = tmp_path / "zcode.cjs"
    cli.write_text("pinned")
    with pytest.raises(RunnerError, match="checkpoints are not supported"):
        runner.build_pier_command(
            _assignment(), tasks, tmp_path / "jobs", "job", tmp_path,
            resume_checkpoint=tmp_path / "checkpoint",
            provider_auth_path=_private(tmp_path / "key"),
            provider_cli_path=cli,
        )


def test_zcode_adapter_source_has_fixed_security_contract() -> None:
    source = Path(providers.__file__).with_name("pier_zcode.py").read_text()
    assert (
        'return NetworkAllowlist(domains=["open.bigmodel.cn", "zcode.z.ai"])'
        in source
    )
    assert '"baseURL": "https://open.bigmodel.cn/api/anthropic"' in source
    assert '"apiKey": {"source": "inline", "value": key}' in source
    assert 'key_file.unlink()' in source
    assert '"memoryEnabled": False' in source
    assert '"titleGenerationEnabled": False' in source
    assert '"WebFetch", "WebSearch", "web_search"' in source
    assert 'required_tools = {"Read", "Write", "Edit", "Bash"}' in source
    assert '"Read(/tmp/dradar-zcode-*)"' not in source
    assert 'message.get("content") or message.get("parts")' in source
    assert 'info.get("role") if isinstance(info, dict) else None' in source
    assert "[REDACTED_ZCODE_CREDENTIAL]" in source
    assert "deadline = time.monotonic() + session_timeout_sec" in source
    assert "90 * 60" not in source
    assert "dradar-zcode-runtime-v1" in source


def test_zcode_session_deadline_tracks_long_assignment_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    cli = tmp_path / "zcode.cjs"
    cli.write_text("pinned", encoding="utf-8")
    command = runner.build_pier_command(
        _assignment(est_minutes=40), tasks, tmp_path / "jobs", "job", home,
        provider_auth_path=_private(tmp_path / "key"), provider_cli_path=cli,
    )
    # DRadar outer cap is 40m * 4 = 160m. The adapter gets one extra minute
    # so the outer watchdog remains the authoritative termination path.
    assert "session_timeout_sec=9660" in command


def test_zcode_runtime_diagnostic_reads_only_allowlisted_lifecycle_facts(
    tmp_path: Path,
) -> None:
    diagnostic = tmp_path / "job" / "trial" / "agent"
    diagnostic.mkdir(parents=True)
    (diagnostic / "zcode-runtime-diagnostic.json").write_text(json.dumps({
        "schema": "dradar-zcode-runtime-v1",
        "status": "running",
        "turn_count": 7,
        "seen_running": True,
        "terminal_observed": False,
        "prompt": "must never leave the machine",
    }), encoding="utf-8")
    assert runner._zcode_runtime_diagnostic(tmp_path, "job") == {
        "zcode_last_status": "running",
        "zcode_turn_count": 7,
        "zcode_seen_running": True,
        "zcode_terminal_observed": False,
    }


def _zcode_usage(payload: dict) -> dict:
    source = Path(providers.__file__).with_name("pier_zcode.py").read_text()
    module = ast.parse(source)
    helper = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_zcode_usage_facts"
    )
    namespace = {"datetime": datetime, "timezone": timezone}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "pier_zcode.py", "exec"),
         namespace)
    return namespace["_zcode_usage_facts"]({
        "sessionId": "sess_contract", **payload,
    })


def _collect_rollout(session_id: str, **limits: int) -> dict:
    source = Path(providers.__file__).with_name("pier_zcode.py").read_text()
    module = ast.parse(source)
    runner_assignment = next(
        node for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_PROTOCOL_RUNNER"
                for target in node.targets)
    )
    runner_module = ast.parse(ast.literal_eval(runner_assignment.value))
    collector = next(
        node for node in runner_module.body
        if isinstance(node, ast.FunctionDef) and node.name == "collect_rollout_usage"
    )
    collector_source = ast.unparse(collector)
    assert "read_text" not in collector_source
    assert ".open('rb')" in collector_source
    namespace = {"json": json, "Path": Path}
    exec(compile(ast.Module(body=[collector], type_ignores=[]), "runner.py", "exec"),
         namespace)
    return namespace["collect_rollout_usage"](session_id, **limits)


def _provider_turn(
    *, session_id: str = "sess_contract", event_id: object = "evt-terminal-1",
    **overrides: int,
) -> dict:
    usage = {
        "source": "provider",
        "modelRequestCount": 1,
        "inputTokens": 3_283,
        "cacheReadTokens": 1_216,
        "cacheWriteTokens": 0,
        "outputTokens": 3,
        "totalTokens": 3_286,
        **overrides,
    }
    return {
        "events": [{
            "eventId": event_id,
            "sessionId": session_id,
            "turnId": "turn-1",
            "seq": 10,
            "timestamp": 1_787_051_447_467,
            "type": "turn.completed",
            "payload": {"resultType": "success", "usage": usage},
        }],
    }


def test_zcode_usage_ledger_preserves_cache_subset_without_double_counting() -> None:
    facts = _zcode_usage({
        "usage": {
            # This is deliberately a different session-baseline projection.
            "inputTokens": 2_000, "cacheReadTokens": 0,
            "cacheCreationTokens": 0, "outputTokens": 4,
            "totalTokens": 2_004, "modelRequestCount": 2,
        },
        "events": _provider_turn(),
        "notifications": [{
            "method": "v4/telemetry/event",
            "params": {
                "eventId": "usage-1",
                "kind": "usage.delta",
                "occurredAt": 1_787_051_447_467,
                "inputTokens": 3_283,
                "cacheReadTokens": 1_216,
                "cacheWriteTokens": 0,
                "outputTokens": 3,
                "totalTokens": 3_286,
            },
        }],
    })
    assert facts["complete"] is True
    assert facts["n_input_tokens"] == 3_283
    assert facts["n_cache_tokens"] == 1_216
    assert facts["n_output_tokens"] == 3
    assert facts["request_count"] == 1
    assert facts["session_usage_model_request_count"] == 2
    assert facts["timed_usage_complete"] is True
    assert facts["n_input_tokens"] + facts["n_output_tokens"] == 3_286
    assert facts["n_input_tokens"] + facts["n_cache_tokens"] + facts["n_output_tokens"] != 3_286


def test_zcode_usage_prefers_durable_rollout_ledger() -> None:
    facts = _zcode_usage({
        "usage": {
            "inputTokens": 5_000,
            "cacheReadTokens": 2_000,
            "cacheCreationTokens": 100,
            "outputTokens": 500,
            "totalTokens": 5_500,
            "modelRequestCount": 2,
        },
        "events": _provider_turn(
            modelRequestCount=2, inputTokens=5_000, cacheReadTokens=2_000,
            cacheWriteTokens=100, outputTokens=500, totalTokens=5_500,
        ),
        "notifications": [],
        "rolloutUsage": {
            "invalidRecordCount": 0,
            "duplicateRecordCount": 0,
            "events": [{
                "occurredAt": "2026-08-18T05:59:59.000Z",
                "inputTokens": 2_000,
                "cacheReadTokens": 500,
                "cacheWriteTokens": 100,
                "outputTokens": 200,
                "totalTokens": 2_200,
            },
            {
                "occurredAt": "2026-08-18T06:00:00.000Z",
                "inputTokens": 3_000,
                "cacheReadTokens": 1_500,
                "cacheWriteTokens": 0,
                "outputTokens": 300,
                "totalTokens": 3_300,
            }],
        },
    })
    assert facts["complete"] is True
    assert facts["n_input_tokens"] == 5_000
    assert facts["n_cache_tokens"] == 2_000
    assert facts["n_output_tokens"] == 500
    assert [event["occurred_at"] for event in facts["token_usage_events"]] == [
        "2026-08-18T05:59:59Z", "2026-08-18T06:00:00Z",
    ]


def test_zcode_provider_aggregate_is_retained_without_timed_ledger() -> None:
    facts = _zcode_usage({
        "usage": {"modelRequestCount": 2},
        "events": _provider_turn(
            modelRequestCount=1, inputTokens=90_000, cacheReadTokens=80_000,
            outputTokens=5_000, totalTokens=95_000,
        ),
        "notifications": [],
        "rolloutUsage": {
            "events": [], "invalidRecordCount": 0, "duplicateRecordCount": 0,
        },
    })
    assert facts["complete"] is True
    assert facts["n_input_tokens"] == 90_000
    assert facts["n_cache_tokens"] == 80_000
    assert facts["n_output_tokens"] == 5_000
    assert facts["timed_usage_complete"] is False
    assert facts["token_usage_events"] == []
    assert facts["timed_usage_incomplete_reason"] == "request_ledger_unavailable"


def test_zcode_mismatched_or_duplicate_ledger_never_double_counts() -> None:
    event = {
        "occurredAt": "2026-08-18T06:00:00Z", "inputTokens": 10_000,
        "cacheReadTokens": 8_000, "cacheWriteTokens": 0,
        "outputTokens": 500, "totalTokens": 10_500,
    }
    facts = _zcode_usage({
        "usage": {"modelRequestCount": 3},
        "events": _provider_turn(
            modelRequestCount=1, inputTokens=10_001, cacheReadTokens=8_000,
            outputTokens=500, totalTokens=10_501,
        ),
        "notifications": [],
        "rolloutUsage": {
            "events": [event], "invalidRecordCount": 0, "duplicateRecordCount": 1,
        },
    })
    assert facts["complete"] is True
    assert facts["n_input_tokens"] == 10_001
    assert facts["timed_usage_complete"] is False
    assert facts["token_usage_events"] == []
    assert facts["request_ledger_duplicate_count"] == 1
    assert facts["timed_usage_incomplete_reason"] == (
        "request_ledger_does_not_match_provider_aggregate"
    )


def test_zcode_missing_provider_aggregate_is_explicitly_incomplete() -> None:
    facts = _zcode_usage({
        "usage": {
            "modelRequestCount": 4, "inputTokens": 12_000,
            "cacheReadTokens": 0, "cacheCreationTokens": 0,
            "outputTokens": 900, "totalTokens": 12_900,
        },
        "events": {"events": []},
        "notifications": [],
        "rolloutUsage": {
            "events": [], "invalidRecordCount": 0, "duplicateRecordCount": 0,
        },
    })
    assert facts["complete"] is False
    assert facts["request_count"] == 0
    assert facts["n_input_tokens"] == 0
    assert facts["token_usage_events"] == []
    assert facts["usage_incomplete_reason"] == (
        "provider_aggregate_missing_or_invalid"
    )


def test_zcode_provider_aggregate_rejects_cross_session_event() -> None:
    facts = _zcode_usage({
        "sessionId": "sess_A",
        "events": _provider_turn(session_id="sess_B"),
        "notifications": [],
    })
    assert facts["complete"] is False
    assert facts["usage_incomplete_reason"] == (
        "provider_aggregate_missing_or_invalid"
    )


@pytest.mark.parametrize("event_id", [None, "", [], ["event-1"]])
def test_zcode_provider_aggregate_rejects_unstable_event_identity(
    event_id: object,
) -> None:
    facts = _zcode_usage({
        "events": _provider_turn(event_id=event_id),
        "notifications": [],
    })
    assert facts["complete"] is False


def test_zcode_provider_aggregate_deduplicates_only_identical_event() -> None:
    first = _provider_turn()["events"][0]
    identical = json.loads(json.dumps(first))
    facts = _zcode_usage({
        "events": {"events": [first, identical]},
        "notifications": [],
    })
    assert facts["complete"] is True
    assert facts["request_count"] == 1
    assert facts["n_input_tokens"] == 3_283

    conflicting = json.loads(json.dumps(first))
    conflicting["payload"]["usage"].update({
        "inputTokens": 3_284, "totalTokens": 3_287,
    })
    conflict = _zcode_usage({
        "events": {"events": [first, conflicting]},
        "notifications": [],
    })
    assert conflict["complete"] is False
    assert conflict["n_input_tokens"] == 0


def test_zcode_rollout_collector_exports_only_billing_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path(providers.__file__).with_name("pier_zcode.py").read_text()
    module = ast.parse(source)
    runner_assignment = next(
        node for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_PROTOCOL_RUNNER"
                for target in node.targets)
    )
    runner_source = ast.literal_eval(runner_assignment.value)
    runner_module = ast.parse(runner_source)
    collector = next(
        node for node in runner_module.body
        if isinstance(node, ast.FunctionDef) and node.name == "collect_rollout_usage"
    )
    namespace = {"json": json, "Path": Path}
    exec(compile(ast.Module(body=[collector], type_ignores=[]), "runner.py", "exec"),
         namespace)

    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = "sess_01234567-89ab-cdef-0123-456789abcdef"
    rollout = tmp_path / ".zcode" / "cli" / "rollout"
    rollout.mkdir(parents=True)
    # Exercise the actual 0.16.3 early-recorder filename, not only the ideal
    # per-session filename.
    (rollout / "model-io-no-session.jsonl").write_text(json.dumps({
        "type": "model_io",
        "sessionId": session_id,
        "completedAt": "2026-08-18T06:00:00.123Z",
        "model": {"modelId": "glm-5.3", "providerId": "bigmodel-coding-plan"},
        "request": {"body": {"messages": [{"content": "SECRET PROMPT"}]}},
        "response": {
            "text": "SECRET RESPONSE",
            "usage": {
                "inputTokens": 4_000,
                "cacheReadTokens": 1_500,
                "cacheWriteTokens": 100,
                "outputTokens": 300,
                "totalTokens": 4_300,
            },
        },
    }) + "\n", encoding="utf-8")

    facts = namespace["collect_rollout_usage"](session_id)
    assert facts == {
        "events": [{
            "occurredAt": "2026-08-18T06:00:00.123Z",
            "inputTokens": 4_000,
            "cacheReadTokens": 1_500,
            "cacheWriteTokens": 100,
            "outputTokens": 300,
            "totalTokens": 4_300,
        }],
        "invalidRecordCount": 0,
        "duplicateRecordCount": 0,
        "limitExceeded": False,
        "limitReason": None,
    }
    assert "SECRET" not in json.dumps(facts)


def test_zcode_rollout_large_file_only_disables_timed_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    rollout = tmp_path / ".zcode" / "cli" / "rollout"
    rollout.mkdir(parents=True)
    (rollout / "model-io-no-session.jsonl").write_bytes(b"x" * 129)

    collected = _collect_rollout(
        "sess_contract", max_file_bytes=128, max_total_bytes=256,
    )
    assert collected["limitExceeded"] is True
    assert collected["limitReason"] == "single_file_bytes"
    assert collected["events"] == []

    # Even exact unsolicited telemetry cannot turn a resource-truncated
    # rollout into a supposedly complete time series.
    facts = _zcode_usage({
        "events": _provider_turn(),
        "rolloutUsage": collected,
        "notifications": [{
            "method": "v4/telemetry/event",
            "params": {
                "eventId": "usage-1", "kind": "usage.delta",
                "occurredAt": 1_787_051_447_467, "inputTokens": 3_283,
                "cacheReadTokens": 1_216, "cacheWriteTokens": 0,
                "outputTokens": 3, "totalTokens": 3_286,
            },
        }],
    })
    assert facts["complete"] is True
    assert facts["n_input_tokens"] == 3_283
    assert facts["timed_usage_complete"] is False
    assert facts["token_usage_events"] == []
    assert facts["timed_usage_incomplete_reason"] == (
        "request_ledger_resource_limit_exceeded"
    )


def test_zcode_rollout_too_many_files_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    rollout = tmp_path / ".zcode" / "cli" / "rollout"
    rollout.mkdir(parents=True)
    (rollout / "model-io-a.jsonl").write_text("\n")
    (rollout / "model-io-b.jsonl").write_text("\n")

    collected = _collect_rollout("sess_contract", max_files=1)

    assert collected == {
        "events": [],
        "invalidRecordCount": 0,
        "duplicateRecordCount": 0,
        "limitExceeded": True,
        "limitReason": "file_count",
    }


def test_zcode_rollout_total_line_and_record_limits_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    rollout = tmp_path / ".zcode" / "cli" / "rollout"
    rollout.mkdir(parents=True)
    path = rollout / "model-io-no-session.jsonl"
    path.write_text("{}\n{}\n")
    assert _collect_rollout("sess_contract", max_lines=1)["limitReason"] == (
        "line_count"
    )
    assert _collect_rollout(
        "sess_contract", max_file_bytes=1_024, max_total_bytes=3,
    )["limitReason"] == "total_bytes"

    record = {
        "type": "model_io", "sessionId": "sess_contract",
        "completedAt": "2026-08-18T06:00:00Z",
        "model": {"modelId": "glm-5.3"},
        "response": {"usage": {
            "inputTokens": 10, "cacheReadTokens": 0, "cacheWriteTokens": 0,
            "outputTokens": 2, "totalTokens": 12,
        }},
    }
    first = {**record, "requestId": "request-1", "attempt": 1}
    second = {**record, "requestId": "request-2", "attempt": 1}
    path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n")
    assert _collect_rollout("sess_contract", max_records=1)["limitReason"] == (
        "record_count"
    )
