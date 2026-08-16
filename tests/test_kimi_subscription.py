"""Kimi Code integration is K3 subscription OAuth-only and single-slot."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import dradar.providers as providers
import dradar.runner as runner
from dradar.providers import (
    KIMI_AGENT,
    KIMI_API_KEY_ENVS,
    KIMI_CAPABILITY,
    KIMI_CLI_VERSION,
    KIMI_MODEL,
    KIMI_PROVIDER,
    advertised_capabilities,
    kimi_auth_error,
    kimi_auth_path,
    kimi_subscription_session,
    parse_kimi_cli_version,
)
from dradar.runner import RunnerError


def _oauth(access: str = "access", refresh: str = "refresh") -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_at": 4_102_444_800,
    }


def _write_auth(path: Path, payload: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload or _oauth()), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _assignment(**overrides: object) -> dict:
    value = {
        "assignment_id": "a-kimi-1",
        "task_id": "task-1",
        "agent": KIMI_AGENT,
        "provider": KIMI_PROVIDER,
        "model": KIMI_MODEL,
        "effort": "high",
        "agent_version": KIMI_CLI_VERSION,
        "est_minutes": 5,
    }
    value.update(overrides)
    return value


def test_official_kimi_version_banner_is_parsed() -> None:
    assert parse_kimi_cli_version("0.36.0\n") == KIMI_CLI_VERSION
    assert parse_kimi_cli_version("kimi version 0.36.0\n") == KIMI_CLI_VERSION
    assert parse_kimi_cli_version("unexpected") is None


def test_kimi_oauth_validator_rejects_api_key_shaped_auth(tmp_path: Path) -> None:
    path = _write_auth(tmp_path / "auth.json", {"api_key": "secret"})
    assert "not a refreshable subscription OAuth" in (kimi_auth_error(path) or "")


def test_kimi_oauth_validator_rejects_symlink_and_broad_mode(
    tmp_path: Path,
) -> None:
    target = _write_auth(tmp_path / "target.json")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    assert "not a symlink" in (kimi_auth_error(link) or "")
    if os.name != "nt":
        target.chmod(0o644)
        assert "too broadly readable" in (kimi_auth_error(target) or "")


def test_kimi_subscription_session_is_private_and_writes_back_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("DRADAR_HOME", str(home))
    canonical = _write_auth(kimi_auth_path(), _oauth("old", "old-refresh"))

    with kimi_subscription_session(tmp_path / "work") as run_copy:
        assert run_copy != canonical
        if os.name != "nt":
            assert run_copy.stat().st_mode & 0o777 == 0o600
        run_copy.write_text(json.dumps(_oauth("new", "new-refresh")))
        if os.name != "nt":
            run_copy.chmod(0o600)

    assert not run_copy.exists()
    payload = json.loads(canonical.read_text())
    assert payload["refresh_token"] == "new-refresh"


def test_kimi_capability_requires_cli_and_safe_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path / "home"))
    assert KIMI_CAPABILITY not in advertised_capabilities({})
    _write_auth(kimi_auth_path())
    assert KIMI_CAPABILITY in advertised_capabilities({"KIMI_CLI_PATH": "/kimi"})


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_pier_command_uses_private_kimi_adapter_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effort: str,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    for name in KIMI_API_KEY_ENVS:
        monkeypatch.setenv(name, "must-not-leak")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    auth = _write_auth(tmp_path / "run-auth.json")
    cli = tmp_path / "kimi"
    cli.write_text("binary", encoding="utf-8")

    assignment = _assignment(effort=effort)
    cmd = runner.build_pier_command(
        assignment,
        tasks,
        tmp_path / "jobs",
        "job",
        home,
        provider_auth_path=auth,
        provider_cli_path=cli,
    )

    joined = " ".join(cmd)
    assert runner.KIMI_AGENT_IMPORT_PATH in cmd
    assert f"reasoning_effort={effort}" in cmd
    assert f"auth_json_file={auth}" in cmd
    assert f"kimi_cli_file={cli}" in cmd
    assert f"version={KIMI_CLI_VERSION}" in cmd
    assert "must-not-leak" not in joined
    adapter = home / runner.KIMI_AGENT_MODULE_FILENAME
    assert adapter.read_bytes() == Path(runner.__file__).with_name("pier_kimi.py").read_bytes()

    env = runner._pier_process_env(assignment, kimi_module_dir=home)
    assert all(name not in env for name in KIMI_API_KEY_ENVS)
    assert env["PYTHONPATH"] == str(home)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"provider": "kimi-api"}, "explicitly use provider"),
        ({"model": "k3-256k"}, "unsupported Kimi subscription model"),
        ({"effort": "medium"}, "effort must be low, high, or max"),
        ({"agent_version": "9.9.9"}, "pinned to CLI"),
    ],
)
def test_unverified_kimi_assignments_fail_before_paid_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict,
    message: str,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    assignment = _assignment(**overrides)
    tasks = tmp_path / "tasks"
    (tasks / assignment["task_id"]).mkdir(parents=True)
    auth = _write_auth(tmp_path / "auth.json")
    cli = tmp_path / "kimi"
    cli.write_text("binary", encoding="utf-8")
    with pytest.raises(RunnerError, match=message):
        runner.build_pier_command(
            assignment,
            tasks,
            tmp_path / "jobs",
            "job",
            tmp_path,
            provider_auth_path=auth,
            provider_cli_path=cli,
        )


def test_kimi_checkpoint_resume_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    auth = _write_auth(tmp_path / "auth.json")
    cli = tmp_path / "kimi"
    cli.write_text("binary", encoding="utf-8")
    with pytest.raises(RunnerError, match="checkpoints are not supported"):
        runner.build_pier_command(
            _assignment(),
            tasks,
            tmp_path / "jobs",
            "job",
            tmp_path,
            resume_checkpoint=tmp_path / "checkpoint",
            provider_auth_path=auth,
            provider_cli_path=cli,
        )


def test_kimi_adapter_source_has_fixed_security_contract() -> None:
    source = Path(providers.__file__).with_name("pier_kimi.py").read_text()
    assert 'return NetworkAllowlist(domains=["auth.kimi.com", "api.kimi.com"])' in source
    assert 'enabled = ["Read", "Write", "Edit", "Bash", "Grep", "Glob"]' in source
    assert '"--auto"' not in source
    assert "KIMI_DISABLE_TELEMETRY" in source
    assert "KIMI_DISABLE_CRON" in source
    assert "[REDACTED_KIMI_CREDENTIAL]" in source
