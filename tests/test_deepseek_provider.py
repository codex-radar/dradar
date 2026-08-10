"""DeepSeek V4 Flash is an additive, public-safe Codex provider."""

import hashlib
import json
import os
import stat
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import dradar.providers as providers
import dradar.runner as runner
from dradar.providers import (
    DEFAULT_CODEX_PROVIDER,
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_CAPABILITY,
    DEEPSEEK_CATALOG_REMOTE_PATH,
    DEEPSEEK_CATALOG_SHA256,
    DEEPSEEK_CODEX_VERSION,
    DEEPSEEK_MODEL,
    DEEPSEEK_PROVIDER,
    advertised_capabilities,
    assignment_codex_provider,
    deepseek_catalog_error,
    deepseek_catalog_path,
)
from dradar.runner import RunnerError


def _assignment(**overrides) -> dict:
    values = {
        "assignment_id": "a1",
        "task_id": "task-1",
        "agent": "codex",
        "provider": DEEPSEEK_PROVIDER,
        "model": DEEPSEEK_MODEL,
        "effort": "max",
        "agent_version": DEEPSEEK_CODEX_VERSION,
        "resume_generation": 0,
        "est_minutes": 5,
    }
    values.update(overrides)
    return values


def _command(tmp_path: Path, monkeypatch, assignment=None) -> tuple[list[str], Path]:
    assignment = assignment or _assignment()
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / assignment["task_id"]).mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    auth = tmp_path / "runtime-auth.json"
    auth.write_text("{}")
    if os.name != "nt":
        auth.chmod(0o600)
    return (
        runner.build_pier_command(
            assignment,
            tasks,
            tmp_path / "jobs",
            "job",
            home,
            provider_auth_path=auth,
        ),
        home,
    )


def test_capability_advertises_software_support_before_first_key_setup():
    assert advertised_capabilities({}) == (DEEPSEEK_CAPABILITY,)
    assert advertised_capabilities({DEEPSEEK_API_KEY_ENV: "key"}) == (
        DEEPSEEK_CAPABILITY,
    )


def test_bundled_catalog_has_expected_integrity_and_reasoning_levels():
    catalog = deepseek_catalog_path()
    payload = catalog.read_bytes()
    parsed = json.loads(payload)
    flash = next(item for item in parsed["models"] if item["slug"] == DEEPSEEK_MODEL)

    assert hashlib.sha256(payload).hexdigest() == DEEPSEEK_CATALOG_SHA256
    assert [item["slug"] for item in parsed["models"]] == [
        "deepseek-v4-flash", "deepseek-v4-pro",
    ]
    assert deepseek_catalog_error(catalog) is None
    assert {level["effort"] for level in flash["supported_reasoning_levels"]} >= {
        "high", "max",
    }
    assert flash["supports_parallel_tool_calls"] is True
    assert flash["apply_patch_tool_type"] == "freeform"
    assert flash["multi_agent_version"] == "v2"
    assert flash["shell_type"] == "shell_command"
    assert flash["context_window"] == 1_048_576
    assert flash["effective_context_window_percent"] == 95
    assert flash["default_reasoning_summary"] == "none"
    assert flash["minimal_client_version"] == "0.144.0"


def test_corrupt_catalog_withholds_paid_provider_capability(
    tmp_path: Path,
    monkeypatch,
):
    corrupt = tmp_path / "models.json"
    corrupt.write_text('{"models": []}')
    monkeypatch.setattr(providers, "deepseek_catalog_path", lambda: corrupt)

    assert "integrity check failed" in (deepseek_catalog_error(corrupt) or "")
    assert advertised_capabilities({}) == ()


def test_missing_provider_preserves_original_codex_path():
    assert assignment_codex_provider({"agent": "codex"}) == DEFAULT_CODEX_PROVIDER
    assert assignment_codex_provider({"agent": "claude-code"}) is None


def test_command_uses_official_catalog_adapter_and_auth_without_secret_env(
    tmp_path: Path,
    monkeypatch,
):
    command, home = _command(tmp_path, monkeypatch)
    joined = " ".join(command)

    assert command[:5] == [
        "/usr/bin/pier", "--isolated", "--from",
        "datacurve-pier==0.3.0", "pier",
    ]
    assert command[command.index("--agent-import-path") + 1] == (
        runner.DEEPSEEK_AGENT_IMPORT_PATH
    )
    assert "--agent" not in command
    assert runner.PIER_SPEC not in joined
    assert f"version={DEEPSEEK_CODEX_VERSION}" in command
    assert "reasoning_effort=max" in command
    assert "checkpoint_enabled=true" not in command
    assert "checkpoint_path=" not in joined
    assert DEEPSEEK_API_KEY_ENV not in joined
    assert "CODEX_AUTH_JSON_PATH=" in joined
    assert (home / runner.DEEPSEEK_AGENT_MODULE_FILENAME).is_file()

    catalog_arg = next(
        item for item in command if item.startswith("model_catalog_json_file=")
    )
    assert Path(catalog_arg.split("=", 1)[1]) == deepseek_catalog_path()

    config_arg = next(
        item for item in command if item.startswith("config_toml_file=")
    )
    config_path = Path(config_arg.split("=", 1)[1])
    assert config_path == home / "codex-deepseek-v4-flash.toml"
    parsed = tomllib.loads(config_path.read_text())
    assert parsed["model_provider"] == DEEPSEEK_PROVIDER
    assert parsed["model_catalog_json"] == DEEPSEEK_CATALOG_REMOTE_PATH
    assert "model_context_window" not in parsed
    assert "model_auto_compact_token_limit" not in parsed
    assert "model_reasoning_summary" not in parsed
    assert "experimental_use_unified_exec_tool" not in parsed
    assert parsed["features"] == {"apps": False, "remote_plugin": False}
    assert parsed["model_providers"][DEEPSEEK_PROVIDER] == {
        "name": "deepseek",
        "base_url": "https://api.deepseek.com/",
        "wire_api": "responses",
        "requires_openai_auth": True,
    }


def test_command_uses_one_official_endpoint_snapshot(
    tmp_path: Path,
    monkeypatch,
):
    command, _home = _command(tmp_path, monkeypatch)
    config_arg = next(
        item for item in command if item.startswith("config_toml_file=")
    )
    parsed = tomllib.loads(Path(config_arg.split("=", 1)[1]).read_text())
    config_url = parsed["model_providers"][DEEPSEEK_PROVIDER]["base_url"]

    assert config_url == "https://api.deepseek.com/"
    assert [
        item for item in command if item.startswith("provider_base_url=")
    ] == [f"provider_base_url={config_url}"]


def test_deepseek_shared_inputs_are_reused_and_owner_only(
    tmp_path: Path,
    monkeypatch,
):
    _, home = _command(tmp_path, monkeypatch)
    runner._ensure_allowlist(home)
    expected = {
        home / runner.DEEPSEEK_AGENT_MODULE_FILENAME: (
            Path(runner.__file__).with_name("pier_deepseek.py").read_bytes()
        ),
        home / "codex-deepseek-v4-flash.toml": runner.deepseek_toml(
            providers.DEEPSEEK_BASE_URL
        ).encode(),
        home / "codex-submission-prompt.j2": (
            runner.CODEX_SUBMISSION_PROMPT.encode()
        ),
        home / "codex-chatgpt-allowlist.toml": runner.ALLOWLIST_TOML.encode(),
    }
    inodes = {path: path.stat().st_ino for path in expected}
    if os.name != "nt":
        # Reusing matching content must also repair an overly broad legacy
        # mode without replacing the file.
        for path in expected:
            path.chmod(0o644)

    monkeypatch.setattr(
        runner.os,
        "replace",
        lambda *_args: pytest.fail("matching shared inputs must be reused"),
    )
    assert runner._ensure_deepseek_agent_module(home) in expected
    assert runner._ensure_deepseek_config(
        home, providers.DEEPSEEK_BASE_URL
    ) in expected
    assert runner._ensure_codex_submission_prompt(home) in expected
    assert runner._ensure_allowlist(home) in expected

    for path, payload in expected.items():
        assert path.read_bytes() == payload
        assert path.stat().st_ino == inodes[path]
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_shared_input_publication_is_atomic_under_concurrency(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "codex-deepseek-v4-flash.toml"
    old = b"complete-old-config\n"
    payload = (b"complete-new-config\n" * 65536)
    path.write_bytes(old)
    participants = 8
    ready_to_publish = threading.Barrier(participants)
    replace_lock = threading.Lock()
    real_replace = os.replace
    temp_paths: list[Path] = []

    def synchronized_replace(src, dst):
        source = Path(src)
        assert Path(dst) == path
        assert source.parent == path.parent
        assert source.read_bytes() == payload
        if os.name != "nt":
            assert stat.S_IMODE(source.stat().st_mode) == 0o600
        temp_paths.append(source)
        # No publisher may expose a truncated target while the other workers
        # are still writing their own unique temporary files.
        assert path.read_bytes() == old
        ready_to_publish.wait(timeout=10)
        with replace_lock:
            real_replace(source, dst)

    monkeypatch.setattr(runner.os, "replace", synchronized_replace)
    with ThreadPoolExecutor(max_workers=participants) as pool:
        results = list(pool.map(
            lambda _index: runner._materialize_shared_file(path, payload),
            range(participants),
        ))

    assert results == [path] * participants
    assert len(temp_paths) == participants
    assert len(set(temp_paths)) == participants
    assert path.read_bytes() == payload
    assert not list(tmp_path.glob(f".{path.name}.*"))


def test_failed_shared_input_publication_keeps_previous_complete_file(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "codex-submission-prompt.j2"
    path.write_bytes(b"previous complete prompt")
    monkeypatch.setattr(
        runner.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        runner._materialize_shared_file(path, b"replacement prompt")

    assert path.read_bytes() == b"previous complete prompt"
    assert not list(tmp_path.glob(f".{path.name}.*"))


def test_command_fails_before_paid_run_when_catalog_is_modified(
    tmp_path: Path,
    monkeypatch,
):
    corrupt = tmp_path / "bad-models.json"
    corrupt.write_text('{"models": []}')
    monkeypatch.setattr(runner, "deepseek_catalog_path", lambda: corrupt)

    with pytest.raises(RunnerError, match="integrity check failed"):
        _command(tmp_path, monkeypatch)

    assert not (tmp_path / "home" / runner.DEEPSEEK_AGENT_MODULE_FILENAME).exists()


@pytest.mark.parametrize("effort", ["low", "medium", "xhigh"])
def test_compatibility_aliases_are_not_duplicate_benchmark_cells(
    tmp_path: Path,
    monkeypatch,
    effort: str,
):
    with pytest.raises(RunnerError, match="effort must be one of high, max"):
        _command(tmp_path, monkeypatch, _assignment(effort=effort))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"model": "deepseek-other"}, "unsupported DeepSeek model"),
        ({"effort": "ultra"}, "effort must be"),
        ({"agent_version": "0.145.0"}, "pinned to tested Codex"),
        ({"agent_version": "latest"}, "exact stable"),
    ],
)
def test_rejects_unverified_assignment(
    tmp_path: Path,
    monkeypatch,
    overrides,
    message,
):
    with pytest.raises(RunnerError, match=message):
        _command(tmp_path, monkeypatch, _assignment(**overrides))


def test_unknown_provider_fails_without_touching_openai_auth(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(RunnerError, match="unsupported Codex provider"):
        runner.build_pier_command(
            _assignment(provider="future-provider"),
            tasks,
            tmp_path / "jobs",
            "job",
            home,
        )


def test_missing_runtime_auth_file_is_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(RunnerError, match="runtime credential"):
        runner.build_pier_command(
            _assignment(), tasks, tmp_path / "jobs", "job", home,
        )


def test_deepseek_checkpoint_resume_is_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    with pytest.raises(RunnerError, match="checkpoints are not supported"):
        runner.build_pier_command(
            _assignment(), tasks, tmp_path / "jobs", "job", tmp_path,
            resume_checkpoint=tmp_path / "checkpoint",
            provider_auth_path=auth,
        )


def test_pier_process_isolates_deepseek_secret_and_adapter_path(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sentinel-deepseek-secret")
    monkeypatch.setenv("PYTHONPATH", "/private/pier")
    monkeypatch.setenv("PYTHONHOME", "/private/python")

    deepseek_env = runner._pier_process_env(
        _assignment(), deepseek_module_dir=tmp_path,
    )
    openai_env = runner._pier_process_env(
        _assignment(provider=DEFAULT_CODEX_PROVIDER, model="gpt-5.5")
    )

    assert DEEPSEEK_API_KEY_ENV not in deepseek_env
    assert deepseek_env["PYTHONPATH"] == str(tmp_path)
    assert "PYTHONHOME" not in deepseek_env
    assert openai_env[DEEPSEEK_API_KEY_ENV] == "sentinel-deepseek-secret"
    assert openai_env["PYTHONPATH"] == "/private/pier"
    assert openai_env["PYTHONHOME"] == "/private/python"


def test_run_removes_temporary_auth_when_command_build_fails(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sentinel-deepseek-secret")
    created = []
    original = runner.create_deepseek_auth_json

    def create(directory):
        path = original(directory)
        created.append(path)
        return path

    monkeypatch.setattr(runner, "create_deepseek_auth_json", create)
    monkeypatch.setattr(
        runner,
        "build_pier_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(RunnerError("intentional")),
    )

    with pytest.raises(RunnerError, match="intentional"):
        runner.run_trial(_assignment(), tmp_path / "tasks", tmp_path / "work")

    assert len(created) == 1
    assert not created[0].exists()


def test_deepseek_run_never_queries_npm_latest(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sentinel-deepseek-secret")
    monkeypatch.setattr(
        runner,
        "resolve_latest_codex_cli_version",
        lambda *args, **kwargs: pytest.fail("DeepSeek must use the tested fixed pin"),
    )
    seen = {}

    def stop_after_version(assignment, *args, **kwargs):
        seen["version"] = assignment["agent_version"]
        raise RunnerError("intentional test stop")

    monkeypatch.setattr(runner, "build_pier_command", stop_after_version)
    with pytest.raises(RunnerError, match="intentional test stop"):
        runner.run_trial(_assignment(), tmp_path / "tasks", tmp_path / "work")
    assert seen["version"] == DEEPSEEK_CODEX_VERSION
