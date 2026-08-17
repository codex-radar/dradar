"""ZCode GLM-5.3 domestic Coding Plan integration contract."""

from __future__ import annotations

import os
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
    assert 'return NetworkAllowlist(domains=["open.bigmodel.cn"])' in source
    assert '"baseURL": "https://open.bigmodel.cn/api/anthropic"' in source
    assert '"apiKey": {"source": "inline", "value": key}' in source
    assert 'key_file.unlink()' in source
    assert '"memoryEnabled": False' in source
    assert '"WebFetch", "WebSearch", "web_search"' in source
    assert 'required_tools = {"Read", "Write", "Edit", "Bash"}' in source
    assert '"Read(/tmp/dradar-zcode-*)"' not in source
    assert 'message.get("content") or message.get("parts")' in source
    assert 'info.get("role") if isinstance(info, dict) else None' in source
    assert "[REDACTED_ZCODE_CREDENTIAL]" in source
