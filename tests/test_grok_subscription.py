"""Grok Build integration is subscription OAuth with native concurrency."""

from __future__ import annotations

import ast
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dradar.providers as providers
import dradar.runner as runner
from dradar.providers import (
    GROK_AGENT,
    GROK_API_KEY_ENV,
    GROK_CLI_VERSION,
    GROK_MODEL,
    GROK_PROVIDER,
    grok_auth_error,
    grok_auth_path,
    grok_subscription_session,
    parse_grok_cli_version,
)
from dradar.runner import RunnerError


def _oauth(token: str = "access", refresh: str = "refresh") -> dict:
    return {
        "https://auth.x.ai::client": {
            "auth_mode": "oauth",
            "key": token,
            "refresh_token": refresh,
        }
    }


def _write_auth(path: Path, payload: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload or _oauth()), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _assignment(**overrides) -> dict:
    value = {
        "assignment_id": "a1",
        "task_id": "task-1",
        "agent": GROK_AGENT,
        "provider": GROK_PROVIDER,
        "model": GROK_MODEL,
        "effort": "high",
        "agent_version": GROK_CLI_VERSION,
        "est_minutes": 5,
    }
    value.update(overrides)
    return value


def test_official_grok_version_banner_is_parsed():
    assert parse_grok_cli_version("grok 1.0.3 (release)\n") == GROK_CLI_VERSION
    assert parse_grok_cli_version("unexpected") is None


def test_oauth_validator_rejects_api_key_shaped_auth(tmp_path: Path):
    path = _write_auth(
        tmp_path / "auth.json",
        {"xai": {"auth_mode": "api_key", "key": "secret"}},
    )
    assert "not a refreshable subscription OAuth" in (grok_auth_error(path) or "")


def test_subscription_session_uses_canonical_native_lock_store(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("DRADAR_HOME", str(home))
    canonical = _write_auth(grok_auth_path(), _oauth("old", "old-refresh"))

    with grok_subscription_session(tmp_path / "work") as shared:
        assert shared == canonical
        if os.name != "nt":
            assert shared.parent.stat().st_mode & 0o777 == 0o700

    assert canonical.is_file()


def test_pier_command_uses_private_adapter_without_key_in_argv(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    monkeypatch.setenv(GROK_API_KEY_ENV, "must-not-leak")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    auth = _write_auth(tmp_path / "providers" / "grok" / "auth.json")
    cli = tmp_path / "grok"
    cli.write_text("binary", encoding="utf-8")

    cmd = runner.build_pier_command(
        _assignment(), tasks, tmp_path / "jobs", "job", home,
        provider_auth_path=auth,
        provider_cli_path=cli,
    )

    assert runner.GROK_AGENT_IMPORT_PATH in cmd
    assert f"auth_json_file={auth}" in cmd
    assert "shared_oauth=true" in cmd
    assert runner.SHARED_OAUTH_ENV_IMPORT_PATH in cmd
    assert f"grok_cli_file={cli}" in cmd
    assert f"version={GROK_CLI_VERSION}" in cmd
    assert "must-not-leak" not in " ".join(cmd)
    assert (home / runner.GROK_AGENT_MODULE_FILENAME).is_file()

    env = runner._pier_process_env(_assignment(), grok_module_dir=home)
    assert GROK_API_KEY_ENV not in env
    assert env["PYTHONPATH"] == str(home)


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
def test_all_grok_46_efforts_build_the_pinned_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effort: str,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    auth = _write_auth(tmp_path / "providers" / "grok" / "auth.json")
    cli = tmp_path / "grok"
    cli.write_text("binary", encoding="utf-8")
    cmd = runner.build_pier_command(
        _assignment(effort=effort), tasks, tmp_path / "jobs", "job", tmp_path,
        provider_auth_path=auth, provider_cli_path=cli,
    )
    assert f"reasoning_effort={effort}" in cmd
    assert cmd[cmd.index("--model") + 1] == "grok-4.6"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"provider": "xai-api"}, "explicitly use provider"),
        ({"model": "grok-other"}, "unsupported Grok subscription model"),
        ({"effort": "max"}, "effort must be low, medium, high, or xhigh"),
        ({"agent_version": "9.9.9"}, "pinned to CLI"),
    ],
)
def test_unverified_grok_assignments_fail_before_paid_run(
    tmp_path: Path, monkeypatch, overrides: dict, message: str
):
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    assignment = _assignment(**overrides)
    tasks = tmp_path / "tasks"
    (tasks / assignment["task_id"]).mkdir(parents=True)
    auth = _write_auth(tmp_path / "auth.json")
    cli = tmp_path / "grok"
    cli.write_text("binary", encoding="utf-8")
    with pytest.raises(RunnerError, match=message):
        runner.build_pier_command(
            assignment, tasks, tmp_path / "jobs", "job", tmp_path,
            provider_auth_path=auth,
            provider_cli_path=cli,
        )


def test_grok_checkpoint_resume_is_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    auth = _write_auth(tmp_path / "auth.json")
    cli = tmp_path / "grok"
    cli.write_text("binary", encoding="utf-8")
    with pytest.raises(RunnerError, match="checkpoints are not supported"):
        runner.build_pier_command(
            _assignment(), tasks, tmp_path / "jobs", "job", tmp_path,
            resume_checkpoint=tmp_path / "checkpoint",
            provider_auth_path=auth,
            provider_cli_path=cli,
        )


def test_grok_adapter_primes_dynamic_46_model_catalog() -> None:
    source = Path(providers.__file__).with_name("pier_grok.py").read_text()
    assert '_REMOTE_HOME = _REMOTE_USER_HOME / ".grok"' in source
    assert '"HOME": remote_user_home' in source
    assert '"GROK_HOME": remote_home' not in source
    assert 'f"models_output=$({shlex.quote(remote_cli)} models) "' in source
    assert "closing stdout while Grok is still" in source
    assert "grep -Fq" in source
    assert "grok-4.6" in source
    assert '"grok.com"' in source
    assert '"code.grok.com"' in source
    assert "GROK_TELEMETRY_ENABLED" in source
    assert "grok-1.0.3-linux-${grok_arch}" not in source
    assert "grok-{GROK_CLI_VERSION}-linux-${{grok_arch}}" in source
    assert "GROK_LINUX_SHA256" in source
    assert "sha256sum --check --strict" in source
    assert "await environment.upload_file(self._grok_cli_file" not in source


def test_grok_usage_keeps_cached_input_as_prompt_subset() -> None:
    source = Path(providers.__file__).with_name("pier_grok.py").read_text()
    module = ast.parse(source)
    helper = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_grok_usage_facts"
    )
    namespace = {"datetime": datetime, "timezone": timezone, "math": math}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "pier_grok.py", "exec"),
         namespace)
    first_usage = {
        "input_tokens": 300,
        "cache_read_input_tokens": 250,
        "cache_creation_input_tokens": 60,
        "output_tokens": 30,
    }
    second_usage = {
        "input_tokens": 200,
        "cache_read_input_tokens": 150,
        "cache_creation_input_tokens": 40,
        "output_tokens": 20,
    }
    official_usage = {
        "input_tokens": 500,
        "cache_read_input_tokens": 400,
        "cache_creation_input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 1_050,
    }
    response_events = [
        {"type": "assistant", "message": {"usage": first_usage}},
        {"type": "assistant", "message": {"usage": second_usage}},
    ]
    terminal = {
        "type": "result",
        "subtype": "success",
        "num_turns": 2,
        "total_cost_usd": 0.00142052,
        "usage": official_usage,
    }
    facts = namespace["_grok_usage_facts"]([*response_events, terminal])
    assert facts["complete"] is True
    assert facts["n_input_tokens"] == 1_000
    assert facts["n_cache_tokens"] == 400
    assert facts["n_output_tokens"] == 50
    assert facts["cache_creation_tokens"] == 100
    assert facts["request_count"] == 2
    assert facts["request_usage_complete"] is True
    assert facts["timed_usage_complete"] is False
    assert facts["token_usage_events"] == [
        {
            "n_input_tokens": 610,
            "n_cache_tokens": 250,
            "n_output_tokens": 30,
        },
        {
            "n_input_tokens": 390,
            "n_cache_tokens": 150,
            "n_output_tokens": 20,
        },
    ]
    assert facts["subscription_reported_cost_usd"] == pytest.approx(0.00142052)

    incomplete = namespace["_grok_usage_facts"]([
        *response_events,
        {**terminal, "usage_is_incomplete": True},
    ])
    assert incomplete["complete"] is False

    mismatched = namespace["_grok_usage_facts"]([
        *response_events,
        {**terminal, "usage": {**official_usage, "total_tokens": 1_051}},
    ])
    assert mismatched["complete"] is False

    missing_response = namespace["_grok_usage_facts"]([
        response_events[0], terminal,
    ])
    assert missing_response["complete"] is False
    assert missing_response["request_usage_observed"] is True
    assert missing_response["request_count"] == 1
    assert missing_response["n_input_tokens"] == 610
    assert missing_response["n_cache_tokens"] == 250
    assert missing_response["n_output_tokens"] == 30

    missing_terminal = namespace["_grok_usage_facts"](response_events)
    assert missing_terminal["complete"] is False
    assert missing_terminal["request_usage_complete"] is False
    assert missing_terminal["request_usage_observed"] is True
    assert missing_terminal["usage_evidence_tier"] == "observed_unreconciled"
    assert missing_terminal["request_count"] == 2
    assert missing_terminal["n_input_tokens"] == 1_000
    assert missing_terminal["n_cache_tokens"] == 400
    assert missing_terminal["n_output_tokens"] == 50
    assert len(missing_terminal["token_usage_events"]) == 2


def test_grok_live_probe_uses_native_private_home(
    tmp_path: Path, monkeypatch,
) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs["env"]
        native = Path(kwargs["env"]["HOME"]) / ".grok" / "auth.json"
        assert native.is_file()
        assert native.stat().st_mode & 0o777 == 0o600
        return providers.subprocess.CompletedProcess(
            cmd, 0,
            "You are logged in with grok.com.\n  * grok-4.6 (default)\n", "",
        )

    monkeypatch.setattr(providers.subprocess, "run", fake_run)

    assert providers.grok_live_error("/usr/bin/grok", auth) is None
    assert seen["cmd"] == ["/usr/bin/grok", "models"]
    assert "GROK_HOME" not in seen["env"]
    assert GROK_API_KEY_ENV not in seen["env"]


def test_grok_live_probe_writes_back_rotated_refresh_token(
    tmp_path: Path, monkeypatch,
) -> None:
    auth = _write_auth(tmp_path / "auth.json", _oauth("old", "old-refresh"))

    def fake_run(cmd, **kwargs):
        native = Path(kwargs["env"]["HOME"]) / ".grok" / "auth.json"
        _write_auth(native, _oauth("new", "new-refresh"))
        return providers.subprocess.CompletedProcess(
            cmd, 0, "* grok-4.6 (default)\n", "",
        )

    monkeypatch.setattr(providers.subprocess, "run", fake_run)

    assert providers.grok_live_error("/usr/bin/grok", auth) is None
    assert next(iter(json.loads(auth.read_text()).values()))["refresh_token"] == (
        "new-refresh"
    )


def test_provider_subprocess_env_adds_os_proxy_without_overriding_shell(
    monkeypatch,
) -> None:
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        providers.urllib.request,
        "getproxies",
        lambda: {
            "http": "http://127.0.0.1:18080",
            "https": "http://127.0.0.1:18080",
        },
    )

    env = providers.provider_subprocess_env()

    assert env["HTTP_PROXY"] == "http://127.0.0.1:18080"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:18080"

    monkeypatch.setenv("HTTPS_PROXY", "http://explicit.example:8080")
    assert providers.provider_subprocess_env()["HTTPS_PROXY"] == (
        "http://explicit.example:8080"
    )


def test_dradar_http_proxy_is_the_authoritative_cross_platform_interface(
    monkeypatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://ambient.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.example:8080")
    monkeypatch.setenv("DRADAR_HTTP_PROXY", "http://configured.example:43128")
    monkeypatch.setenv("DRADAR_NO_PROXY", "localhost,127.0.0.1")

    env = providers.provider_subprocess_env()

    assert env["HTTP_PROXY"] == "http://configured.example:43128"
    assert env["HTTPS_PROXY"] == "http://configured.example:43128"
    assert env["NO_PROXY"] == "localhost,127.0.0.1"


def test_grok_live_probe_rejects_unauthenticated_fallback(
    tmp_path: Path, monkeypatch,
) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    monkeypatch.setattr(
        providers.subprocess, "run",
        lambda cmd, **kwargs: providers.subprocess.CompletedProcess(
            cmd, 0,
            "You are not authenticated.\n  * grok-4.5 (default)\n", "",
        ),
    )

    issue = providers.grok_live_error("/usr/bin/grok", auth) or ""
    assert "not authenticated" in issue
    assert "dradar provider setup grok" in issue


def test_grok_live_probe_rejects_offline_builtin_catalog_as_network_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    monkeypatch.setattr(
        providers.subprocess, "run",
        lambda cmd, **kwargs: providers.subprocess.CompletedProcess(
            cmd,
            0,
            "You are logged in with grok.com.\n"
            "Settings fetch failed max_attempts=3\n"
            "Default model: grok-4.5\nAvailable models:\n  * grok-4.5\n",
            "",
        ),
    )

    issue = providers.grok_live_error("/usr/bin/grok", auth) or ""
    assert "network/proxy" in issue
    assert "cannot access" not in issue
