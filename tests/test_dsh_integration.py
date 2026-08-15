"""DRadar main-flow routing for the pinned DSH Minimal Pier adapter."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dradar import runner
from dradar.providers import (
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_PROVIDER,
    DSH_AGENT,
    DSH_FLASH_MODEL,
    DSH_FLASH_CAPABILITY,
    DSH_PRO_CAPABILITY,
    DSH_PRO_MODEL,
    DSH_VERSION,
    advertised_capabilities,
)
from dradar.runner import RunnerError


def _assignment(**overrides: object) -> dict:
    value = {
        "assignment_id": "a-dsh-1",
        "task_id": "task-1",
        "agent": DSH_AGENT,
        "provider": DEEPSEEK_PROVIDER,
        "model": DSH_FLASH_MODEL,
        "effort": "high",
        "agent_version": DSH_VERSION,
        "resume_generation": 0,
        "est_minutes": 5,
    }
    value.update(overrides)
    return value


def _key(path: Path, value: str = "sentinel-never-in-command") -> Path:
    path.write_text(value + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assignment: dict,
) -> tuple[list[str], Path, Path]:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/uvx")
    tasks = tmp_path / "tasks"
    (tasks / assignment["task_id"]).mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    key = _key(tmp_path / "dsh-key")
    command = runner.build_pier_command(
        assignment,
        tasks,
        tmp_path / "jobs",
        "job",
        home,
        provider_auth_path=key,
    )
    return command, home, key


def test_dsh_capabilities_require_a_configured_deepseek_key() -> None:
    assert DSH_FLASH_CAPABILITY not in advertised_capabilities({})
    assert DSH_PRO_CAPABILITY not in advertised_capabilities({})

    capabilities = advertised_capabilities({DEEPSEEK_API_KEY_ENV: "ready"})
    assert DSH_FLASH_CAPABILITY in capabilities
    assert DSH_PRO_CAPABILITY in capabilities


def test_dsh_temporary_key_file_is_private_and_rejects_whitespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The product deliberately prefers an explicitly configured private file
    # over a stale environment variable. Keep this env-fallback test hermetic
    # so a developer's real ~/.dradar credential cannot shadow its sentinel.
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path / "dradar-home"))
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "one-line-key")
    key_file = runner.create_deepseek_api_key_file(tmp_path)
    assert key_file.read_text(encoding="utf-8") == "one-line-key\n"
    if os.name != "nt":
        assert key_file.stat().st_mode & 0o777 == 0o600

    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "invalid key")
    with pytest.raises(ValueError, match="one non-empty line"):
        runner.create_deepseek_api_key_file(tmp_path)


@pytest.mark.parametrize(
    ("model", "effort"),
    [
        (model, effort)
        for model in (DSH_FLASH_MODEL, DSH_PRO_MODEL)
        for effort in ("off", "high", "max")
    ],
)
def test_main_flow_builds_isolated_dsh_minimal_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    effort: str,
) -> None:
    secret = "sentinel-never-in-command"
    assignment = _assignment(model=model, effort=effort)
    command, home, key = _command(tmp_path, monkeypatch, assignment)
    joined = " ".join(command)

    assert command[:5] == [
        "/usr/bin/uvx",
        "--isolated",
        "--from",
        "datacurve-pier==0.3.0",
        "pier",
    ]
    assert command[command.index("--agent-import-path") + 1] == (
        runner.DSH_AGENT_IMPORT_PATH
    )
    assert "--agent" not in command
    assert command[command.index("--model") + 1] == model
    assert f"reasoning_effort={effort}" in command
    assert f"api_key_file={key}" in command
    assert f"version={DSH_VERSION}" in command
    assert any(item.startswith("prompt_template_path=") for item in command)
    assert secret not in joined
    assert DEEPSEEK_API_KEY_ENV not in joined
    assert (home / runner.DSH_AGENT_MODULE_FILENAME).read_bytes() == (
        Path(runner.__file__).with_name("pier_dsh.py").read_bytes()
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"provider": "future-provider"}, "explicitly use provider"),
        ({"model": "deepseek-other"}, "unsupported DSH model"),
        ({"effort": "low"}, "effort must be one of high, max, off"),
        ({"effort": "medium"}, "effort must be one of high, max, off"),
        ({"agent_version": "0.1.0-rc.5"}, "pinned to"),
    ],
)
def test_dsh_rejects_unverified_assignment_before_paid_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict,
    message: str,
) -> None:
    assignment = _assignment(**overrides)
    with pytest.raises(RunnerError, match=message):
        _command(tmp_path, monkeypatch, assignment)


def test_dsh_checkpoint_resume_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/uvx")
    assignment = _assignment()
    tasks = tmp_path / "tasks"
    (tasks / assignment["task_id"]).mkdir(parents=True)
    key = _key(tmp_path / "dsh-key")

    with pytest.raises(RunnerError, match="checkpoints are not supported"):
        runner.build_pier_command(
            assignment,
            tasks,
            tmp_path / "jobs",
            "job",
            tmp_path,
            resume_checkpoint=tmp_path / "checkpoint",
            provider_auth_path=key,
        )


def test_dsh_process_env_strips_secret_and_isolates_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "must-not-leak")
    monkeypatch.setenv("PYTHONPATH", "/private/pier")
    monkeypatch.setenv("PYTHONHOME", "/private/python")

    env = runner._pier_process_env(
        _assignment(),
        dsh_module_dir=tmp_path,
    )

    assert DEEPSEEK_API_KEY_ENV not in env
    assert env["PYTHONPATH"] == str(tmp_path)
    assert "PYTHONHOME" not in env


def test_dsh_task_overlay_adds_public_pier_artifact_hook_without_mutation(
    tmp_path: Path,
) -> None:
    tasks = tmp_path / "tasks"
    task = tasks / "task-1"
    task.mkdir(parents=True)
    (task / "instruction.md").write_text("fix it\n")
    (task / "task.toml").write_text(
        'schema_version = "1.3"\n[agent]\nnetwork_mode = "no-network"\n'
        '[environment]\ndocker_image = "example.invalid/task:v1"\n'
        '[metadata]\nbase_commit_hash = "' + "a" * 40 + '"\n'
    )
    work = tmp_path / "work"

    with runner._dsh_tasks_overlay(_assignment(), tasks, work, "job") as overlay:
        assert overlay != tasks
        hook = overlay / "task-1" / "pre_artifacts.sh"
        assert "base_ref='" + "a" * 40 + "'" in hook.read_text()
        assert "__DRADAR_BASE_COMMIT__" not in hook.read_text()
        assert hook.stat().st_mode & 0o111
        assert not (task / "pre_artifacts.sh").exists()
        overlaid_config = (overlay / "task-1" / "task.toml").read_text()
        assert "allow_internet = false" in overlaid_config
        assert "allow_internet" not in (task / "task.toml").read_text()

    assert not any(work.iterdir())


def test_dsh_task_overlay_preserves_a_task_owned_hook(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    task = tasks / "task-1"
    task.mkdir(parents=True)
    (task / "task.toml").write_text('schema_version = "1.3"\n')
    hook = task / "pre_artifacts.sh"
    hook.write_text("#!/bin/sh\nexit 0\n")

    with runner._dsh_tasks_overlay(
        _assignment(), tasks, tmp_path / "work", "job"
    ) as overlay:
        assert overlay == tasks
        assert hook.read_text() == "#!/bin/sh\nexit 0\n"


def test_dsh_run_removes_temporary_key_when_command_build_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sentinel-secret")
    task = tmp_path / "tasks" / "task-1"
    task.mkdir(parents=True)
    (task / "task.toml").write_text('schema_version = "1.3"\n')
    created: list[Path] = []
    original = runner.create_deepseek_api_key_file

    def create(directory: Path) -> Path:
        path = original(directory)
        created.append(path)
        return path

    monkeypatch.setattr(runner, "create_deepseek_api_key_file", create)
    monkeypatch.setattr(
        runner,
        "build_pier_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(RunnerError("intentional")),
    )

    with pytest.raises(RunnerError, match="intentional"):
        runner.run_trial(
            _assignment(),
            tmp_path / "tasks",
            tmp_path / "work",
        )

    assert len(created) == 1
    assert not created[0].exists()
