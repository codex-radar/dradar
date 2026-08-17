"""Grok Build integration is subscription OAuth-only and single-slot."""

from __future__ import annotations

import json
import os
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
    assert parse_grok_cli_version("grok 1.0.0 (3cd0d0cbcebe)\n") == "1.0.0"
    assert parse_grok_cli_version("unexpected") is None


def test_oauth_validator_rejects_api_key_shaped_auth(tmp_path: Path):
    path = _write_auth(
        tmp_path / "auth.json",
        {"xai": {"auth_mode": "api_key", "key": "secret"}},
    )
    assert "not a refreshable subscription OAuth" in (grok_auth_error(path) or "")


def test_subscription_session_is_private_and_writes_back_refresh(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("DRADAR_HOME", str(home))
    canonical = _write_auth(grok_auth_path(), _oauth("old", "old-refresh"))

    with grok_subscription_session(tmp_path / "work") as run_copy:
        assert run_copy != canonical
        if os.name != "nt":
            assert run_copy.stat().st_mode & 0o777 == 0o600
        run_copy.write_text(json.dumps(_oauth("new", "new-refresh")))

    assert not run_copy.exists()
    payload = json.loads(canonical.read_text())
    assert next(iter(payload.values()))["refresh_token"] == "new-refresh"


def test_pier_command_uses_private_adapter_without_key_in_argv(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    monkeypatch.setenv(GROK_API_KEY_ENV, "must-not-leak")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    auth = _write_auth(tmp_path / "run-auth.json")
    cli = tmp_path / "grok"
    cli.write_text("binary", encoding="utf-8")

    cmd = runner.build_pier_command(
        _assignment(), tasks, tmp_path / "jobs", "job", home,
        provider_auth_path=auth,
        provider_cli_path=cli,
    )

    assert runner.GROK_AGENT_IMPORT_PATH in cmd
    assert f"auth_json_file={auth}" in cmd
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
    auth = _write_auth(tmp_path / "auth.json")
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
    assert 'f"{shlex.quote(remote_cli)} models "' in source
    assert "grep -Fq" in source
    assert "grok-4.6" in source
    assert '"grok.com"' in source
    assert '"code.grok.com"' in source
    assert "GROK_TELEMETRY_ENABLED" in source


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

    assert "not authenticated" in (
        providers.grok_live_error("/usr/bin/grok", auth) or ""
    )
