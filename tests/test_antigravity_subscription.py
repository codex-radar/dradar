"""Google Antigravity integration is isolated, pinned, and token-audited."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import dradar.providers as providers
import dradar.runner as runner
from dradar.providers import (
    ANTIGRAVITY_AGENT,
    ANTIGRAVITY_CAPABILITY,
    ANTIGRAVITY_CLI_VERSION,
    ANTIGRAVITY_MODEL,
    ANTIGRAVITY_PROVIDER,
    ANTIGRAVITY_RUNTIME_MODELS,
    antigravity_auth_error,
    antigravity_auth_path,
    antigravity_settings_payload,
    antigravity_subscription_session,
    advertised_capabilities,
    mark_antigravity_ready,
    privatize_antigravity_home,
    prepare_antigravity_auth,
    write_antigravity_settings,
)
from dradar.runner import RunnerError


def _assignment(**overrides) -> dict:
    value = {
        "assignment_id": "agy-1",
        "task_id": "task-1",
        "agent": ANTIGRAVITY_AGENT,
        "provider": ANTIGRAVITY_PROVIDER,
        "model": ANTIGRAVITY_MODEL,
        "effort": "low",
        "agent_version": ANTIGRAVITY_CLI_VERSION,
        "est_minutes": 5,
    }
    value.update(overrides)
    return value


def _ready_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "dradar"
    monkeypatch.setenv("DRADAR_HOME", str(home))
    auth = antigravity_auth_path()
    auth.mkdir(parents=True)
    token = auth / "config" / "oauth-state.json"
    token.parent.mkdir(parents=True)
    token.write_text('{"refresh":"hidden"}', encoding="utf-8")
    write_antigravity_settings()
    mark_antigravity_ready()
    privatize_antigravity_home()
    return auth.resolve()


def test_antigravity_task_overlay_captures_complete_worktree(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "DRadar Test"],
        cwd=repository,
        check=True,
    )
    (repository / "committed.txt").write_text("base\n", encoding="utf-8")
    (repository / "unstaged.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True,
    ).strip()

    (repository / "committed.txt").write_text("committed\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "agent commit"], cwd=repository, check=True)
    (repository / "staged.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=repository, check=True)
    (repository / "unstaged.txt").write_text("unstaged\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    tasks = tmp_path / "tasks"
    task = tasks / "task-1"
    task.mkdir(parents=True)
    (task / "task.toml").write_text(
        'schema_version = "1.3"\n[metadata]\nbase_commit_hash = "'
        + base
        + '"\n',
        encoding="utf-8",
    )
    original = task / "pre_artifacts.sh"
    original.write_text("#!/bin/sh\ngit diff HEAD\n", encoding="utf-8")

    work = tmp_path / "work"
    with runner._antigravity_tasks_overlay(
        _assignment(), tasks, work, "job",
    ) as overlay:
        assert overlay != tasks
        hook = overlay / "task-1" / "pre_artifacts.sh"
        script = hook.read_text(encoding="utf-8")
        assert "git add -N -- ." in script
        assert f"base_ref='{base}'" in script
        assert 'git diff --binary "$base" --' in script
        assert 'git diff --binary "$base" HEAD' not in script
        assert original.read_text(encoding="utf-8") == "#!/bin/sh\ngit diff HEAD\n"

        logs = tmp_path / "logs"
        runnable = tmp_path / "collect.sh"
        runnable.write_text(
            script.replace("cd /app", f"cd {repository}").replace(
                "/logs/artifacts", str(logs / "artifacts")
            ),
            encoding="utf-8",
        )
        subprocess.run(["sh", str(runnable)], check=True)
        patch = (logs / "artifacts" / "model.patch").read_text(encoding="utf-8")
        for filename in (
            "committed.txt", "staged.txt", "unstaged.txt", "untracked.txt",
        ):
            assert filename in patch

    assert not any(work.iterdir())


def test_antigravity_task_overlay_rejects_unverifiable_base(
    tmp_path: Path,
) -> None:
    task = tmp_path / "tasks" / "task-1"
    task.mkdir(parents=True)
    (task / "task.toml").write_text(
        '[metadata]\nbase_commit_hash = "not-a-commit"\n', encoding="utf-8",
    )

    with pytest.raises(RunnerError, match="invalid metadata.base_commit_hash"):
        with runner._antigravity_tasks_overlay(
            _assignment(), tmp_path / "tasks", tmp_path / "work", "job",
        ):
            pass


def test_antigravity_task_overlay_accepts_reviewed_pompeii_base_tag(
    tmp_path: Path,
) -> None:
    task_id = "pompeii-adjacency-rp-002"
    task = tmp_path / "tasks" / task_id
    task.mkdir(parents=True)
    (task / "task.toml").write_text(
        '[metadata]\nbase_commit_hash = "pompeii-base"\n',
        encoding="utf-8",
    )

    assignment = _assignment()
    assignment["task_id"] = task_id
    with runner._antigravity_tasks_overlay(
        assignment, tmp_path / "tasks", tmp_path / "work", "job",
    ) as overlay:
        script = (overlay / task_id / "pre_artifacts.sh").read_text(
            encoding="utf-8",
        )
        assert "base_ref='pompeii-base'" in script


def _usage_helper():
    source = Path(providers.__file__).with_name("pier_antigravity.py").read_text()
    module = ast.parse(source)
    names = {
        "_nonnegative_int", "_usage_values",
        "_antigravity_terminal_error_category", "_antigravity_usage_facts",
    }
    helpers = [
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace = {
        "ANTIGRAVITY_MODEL": ANTIGRAVITY_MODEL,
        "ANTIGRAVITY_STREAM_INTERRUPTED_MESSAGE": (
            "The stream was interrupted. Please continue the task you were "
            "working on."
        ),
        "ANTIGRAVITY_TERMINAL_RECOVERY_SCHEMA": (
            "dradar-antigravity-terminal-recovery-v1"
        ),
        "hashlib": hashlib,
    }
    exec(
        compile(ast.Module(body=helpers, type_ignores=[]), "pier_antigravity.py", "exec"),
        namespace,
    )
    return namespace["_antigravity_usage_facts"]


def _model_line_pattern_helper():
    source = Path(providers.__file__).with_name("pier_antigravity.py").read_text()
    module = ast.parse(source)
    helper = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_model_line_pattern"
    )
    namespace = {"re": re}
    exec(
        compile(ast.Module(body=[helper], type_ignores=[]), "pier_antigravity.py", "exec"),
        namespace,
    )
    return namespace["_model_line_pattern"]


def _usage(input_tokens: int, output_tokens: int, cache: int, thinking: int) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking,
        "cache_read_tokens": cache,
        "total_tokens": input_tokens + output_tokens,
    }


def test_isolated_oauth_home_requires_exact_full_container_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _ready_home(tmp_path, monkeypatch)
    assert antigravity_auth_error() is None
    payload = antigravity_settings_payload()
    assert payload["enableTerminalSandbox"] is False
    assert payload["allowNonWorkspaceAccess"] is True
    assert set(payload["permissions"]["allow"]) >= {
        "command(*)", "read_file(*)", "write_file(*)", "unsandboxed(*)",
    }
    assert payload["permissions"]["deny"] == []

    settings = auth / "antigravity-cli" / "settings.json"
    payload = json.loads(settings.read_text())
    payload["enableTerminalSandbox"] = True
    settings.write_text(json.dumps(payload), encoding="utf-8")
    if os.name != "nt":
        settings.chmod(0o600)
    assert "full-container policy" in (antigravity_auth_error() or "")


def test_oauth_home_rejects_links_and_broad_secret_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _ready_home(tmp_path, monkeypatch)
    secret = auth / "config" / "oauth-state.json"
    if os.name != "nt":
        secret.chmod(0o644)
        assert "broadly accessible" in (antigravity_auth_error() or "")
        secret.chmod(0o600)
    link = auth / "config" / "linked-token"
    link.symlink_to(secret)
    assert "must not be a symlink" in (antigravity_auth_error() or "")
    assert "contains a symlink" in (prepare_antigravity_auth() or "")
    assert link.is_symlink()


@pytest.mark.parametrize("target_exists", [True, False])
def test_preflight_removes_only_official_cli_log_link_before_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_exists: bool,
) -> None:
    auth = _ready_home(tmp_path, monkeypatch)
    log_dir = auth / "antigravity-cli" / "log"
    log_dir.mkdir(parents=True)
    if os.name != "nt":
        log_dir.chmod(0o700)
    log_file = log_dir / "cli-20260829_022534.log"
    if target_exists:
        log_file.write_text("official log", encoding="utf-8")
        if os.name != "nt":
            log_file.chmod(0o600)
    cli_log = log_dir.parent / "cli.log"
    cli_log.symlink_to(Path("log") / log_file.name)

    assert "must not be a symlink" in (antigravity_auth_error() or "")
    assert prepare_antigravity_auth() is None
    assert not cli_log.exists()
    assert not cli_log.is_symlink()
    assert log_file.exists() is target_exists


def test_preflight_hardens_official_cli_log_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _ready_home(tmp_path, monkeypatch)
    log_file = auth / "antigravity-cli" / "log" / "cli-new.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text("official log", encoding="utf-8")
    if os.name != "nt":
        log_file.parent.chmod(0o755)
        log_file.chmod(0o644)
        assert "broadly accessible" in (antigravity_auth_error() or "")

    assert prepare_antigravity_auth() is None
    assert log_file.read_text(encoding="utf-8") == "official log"
    if os.name != "nt":
        assert stat.S_IMODE(log_file.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
def test_preflight_accepts_private_files_owned_by_live_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _ready_home(tmp_path, monkeypatch)
    media = auth / "antigravity-cli" / "brain" / "run" / "media.png"
    media.parent.mkdir(parents=True)
    for directory in (media.parent, *media.parent.parents):
        if directory == auth.parent:
            break
        directory.chmod(0o700)
    media.write_bytes(b"volatile")
    media.chmod(0o600)
    real_uid = os.geteuid()
    monkeypatch.setattr(providers.os, "geteuid", lambda: real_uid + 1)
    chmod_calls = []
    monkeypatch.setattr(
        providers.os, "chmod",
        lambda path, mode: chmod_calls.append((Path(path), mode)),
    )

    assert prepare_antigravity_auth() is None
    assert ANTIGRAVITY_CAPABILITY in advertised_capabilities({})
    assert chmod_calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
def test_preflight_rejects_broad_files_owned_by_live_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _ready_home(tmp_path, monkeypatch)
    media = auth / "antigravity-cli" / "brain" / "run" / "media.png"
    media.parent.mkdir(parents=True)
    for directory in (media.parent, *media.parent.parents):
        if directory == auth.parent:
            break
        directory.chmod(0o700)
    media.write_bytes(b"volatile")
    media.chmod(0o644)
    real_uid = os.geteuid()
    monkeypatch.setattr(providers.os, "geteuid", lambda: real_uid + 1)

    issue = prepare_antigravity_auth()

    assert issue is not None
    assert "foreign-owned entry" in issue
    assert "too broadly accessible" in issue


def test_preflight_preserves_regular_cli_log_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _ready_home(tmp_path, monkeypatch)
    cli_log = auth / "antigravity-cli" / "cli.log"
    cli_log.parent.mkdir(parents=True, exist_ok=True)
    cli_log.write_text("ordinary file", encoding="utf-8")
    if os.name != "nt":
        cli_log.chmod(0o600)

    assert prepare_antigravity_auth() is None
    assert cli_log.read_text(encoding="utf-8") == "ordinary file"


def test_preflight_unlinks_known_log_path_without_following_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _ready_home(tmp_path, monkeypatch)
    outside = tmp_path / "outside.log"
    outside.write_text("must remain untouched", encoding="utf-8")
    cli_log = auth / "antigravity-cli" / "cli.log"
    cli_log.parent.mkdir(parents=True, exist_ok=True)
    cli_log.symlink_to(outside)

    assert prepare_antigravity_auth() is None
    assert not cli_log.exists()
    assert not cli_log.is_symlink()
    assert outside.read_text(encoding="utf-8") == "must remain untouched"


def test_home_hardening_preserves_only_managed_runtime_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "dradar"
    monkeypatch.setenv("DRADAR_HOME", str(home))
    runtime = (
        home / "providers" / "antigravity" / "runtime"
        / ANTIGRAVITY_CLI_VERSION / "aarch64"
    )
    runtime.mkdir(parents=True)
    executable = runtime / "antigravity"
    executable.write_bytes(b"reviewed-binary")
    proof = runtime / ".archive.sha512"
    proof.write_text("reviewed-proof", encoding="utf-8")
    auth_named_file = antigravity_auth_path() / "antigravity"
    auth_named_file.parent.mkdir(parents=True)
    auth_named_file.write_text("secret", encoding="utf-8")
    log_dir = antigravity_auth_path() / "antigravity-cli" / "log"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "cli-20260828_000000.log"
    log_file.write_text("official log", encoding="utf-8")
    cli_log = log_dir.parent / "cli.log"
    cli_log.symlink_to(Path("log") / log_file.name)

    privatize_antigravity_home()

    if os.name != "nt":
        assert executable.stat().st_mode & 0o777 == 0o700
        assert proof.stat().st_mode & 0o777 == 0o600
        assert auth_named_file.stat().st_mode & 0o777 == 0o600
        assert log_file.stat().st_mode & 0o777 == 0o600
    assert not cli_log.exists()


def test_subscription_session_exposes_only_canonical_gemini_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _ready_home(tmp_path, monkeypatch)
    with antigravity_subscription_session(tmp_path / "work") as shared:
        assert shared.resolve() == auth
        assert shared.name == ".gemini"


def test_subscription_session_migrates_legacy_policy_without_reauthentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _ready_home(tmp_path, monkeypatch)
    settings = auth / "antigravity-cli" / "settings.json"
    legacy = json.loads(settings.read_text(encoding="utf-8"))
    legacy["enableTerminalSandbox"] = True
    legacy["allowNonWorkspaceAccess"] = False
    legacy["permissions"] = {"allow": ["command(*)"], "deny": ["unsandboxed(*)"]}
    settings.write_text(json.dumps(legacy), encoding="utf-8")
    if os.name != "nt":
        settings.chmod(0o600)

    with antigravity_subscription_session(tmp_path / "work") as shared:
        assert shared == auth
        assert json.loads(settings.read_text(encoding="utf-8")) == (
            antigravity_settings_payload()
        )


def test_preflight_migrates_legacy_policy_before_capability_advertisement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _ready_home(tmp_path, monkeypatch)
    settings = auth / "antigravity-cli" / "settings.json"
    legacy = json.loads(settings.read_text(encoding="utf-8"))
    legacy["enableTerminalSandbox"] = True
    legacy["allowNonWorkspaceAccess"] = False
    legacy["permissions"] = {"allow": ["command(*)"], "deny": ["unsandboxed(*)"]}
    settings.write_text(json.dumps(legacy), encoding="utf-8")
    if os.name != "nt":
        settings.chmod(0o600)

    assert prepare_antigravity_auth() is None
    assert ANTIGRAVITY_CAPABILITY in advertised_capabilities({})
    assert json.loads(settings.read_text(encoding="utf-8")) == (
        antigravity_settings_payload()
    )


def test_preflight_does_not_repair_invalid_or_missing_oauth_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path / "missing"))
    assert "OAuth is not configured" in (prepare_antigravity_auth() or "")
    assert not antigravity_auth_path().exists()


def test_subscription_session_does_not_create_a_missing_oauth_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="OAuth is not configured"):
        with antigravity_subscription_session(tmp_path / "work"):
            pass
    assert not antigravity_auth_path().exists()


@pytest.mark.parametrize("raises", [False, True])
def test_subscription_session_restores_policy_after_every_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raises: bool,
) -> None:
    auth = _ready_home(tmp_path, monkeypatch)
    settings = auth / "antigravity-cli" / "settings.json"

    def run() -> None:
        with antigravity_subscription_session(tmp_path / "work"):
            payload = json.loads(settings.read_text(encoding="utf-8"))
            payload.pop("allowNonWorkspaceAccess")
            settings.write_text(json.dumps(payload), encoding="utf-8")
            if raises:
                raise RuntimeError("trial interrupted")

    if raises:
        with pytest.raises(RuntimeError, match="trial interrupted"):
            run()
    else:
        run()

    assert json.loads(settings.read_text(encoding="utf-8")) == (
        antigravity_settings_payload()
    )
    assert antigravity_auth_error() is None


def test_subscription_session_never_follows_a_runtime_created_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _ready_home(tmp_path, monkeypatch)
    settings_parent = auth / "antigravity-cli"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "settings.json"
    sentinel.write_text("outside must stay untouched", encoding="utf-8")

    with pytest.raises(ValueError, match="contains a symlink"):
        with antigravity_subscription_session(tmp_path / "work"):
            shutil.rmtree(settings_parent)
            settings_parent.symlink_to(outside, target_is_directory=True)

    assert sentinel.read_text(encoding="utf-8") == "outside must stay untouched"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_all_three_efforts_build_the_same_public_card_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effort: str,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    auth = tmp_path / "providers" / "antigravity" / ".gemini"
    auth.mkdir(parents=True)
    if os.name != "nt":
        auth.chmod(0o700)
    cmd = runner.build_pier_command(
        _assignment(effort=effort), tasks, tmp_path / "jobs", "job", tmp_path,
        provider_auth_path=auth.resolve(),
    )
    assert runner.ANTIGRAVITY_AGENT_IMPORT_PATH in cmd
    assert runner.SHARED_OAUTH_ENV_IMPORT_PATH in cmd
    assert cmd[cmd.index("--model") + 1] == ANTIGRAVITY_MODEL
    assert f"reasoning_effort={effort}" in cmd
    assert f"auth_home_dir={auth.resolve()}" in cmd
    assert "shared_oauth=true" in cmd
    assert f"version={ANTIGRAVITY_CLI_VERSION}" in cmd
    mounts = cmd[cmd.index("--ek") + 1]
    assert "/tmp/dradar-antigravity-user/.gemini" in mounts
    assert ANTIGRAVITY_RUNTIME_MODELS[effort] in Path(
        providers.__file__
    ).with_name("pier_antigravity.py").read_text()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"provider": "google-api"}, "explicitly use provider"),
        ({"model": "gemini-other"}, "unsupported Antigravity model"),
        ({"effort": "max"}, "effort must be low, medium, or high"),
    ],
)
def test_unverified_assignments_fail_before_a_paid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    overrides: dict, message: str,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    assignment = _assignment(**overrides)
    tasks = tmp_path / "tasks"
    (tasks / assignment["task_id"]).mkdir(parents=True)
    auth = tmp_path / "providers" / "antigravity" / ".gemini"
    auth.mkdir(parents=True)
    with pytest.raises(RunnerError, match=message):
        runner.build_pier_command(
            assignment, tasks, tmp_path / "jobs", "job", tmp_path,
            provider_auth_path=auth.resolve(),
        )


def test_antigravity_assignment_version_is_only_a_hint() -> None:
    runner._validate_antigravity_assignment(
        _assignment(agent_version="1.1.21")
    )
    runner._validate_antigravity_assignment(
        _assignment(agent_version="9.9.9")
    )


def test_adapter_uses_full_permissions_inside_pier_container() -> None:
    source = Path(providers.__file__).with_name("pier_antigravity.py").read_text()
    assert '"--new-project"' in source
    assert '"--sandbox"' not in source
    assert '"--dangerously-skip-permissions"' in source
    assert 'init.get("cwd") == "/app"' in source
    assert 'init.get("permission_mode") == "always-proceed"' in source
    assert "sha512sum --check --strict" in source
    assert "storage.googleapis.com" in source
    assert '"www.googleapis.com"' in source
    assert '"lh3.googleusercontent.com"' in source
    assert "*.googleapis.com" not in source
    assert "*.googleusercontent.com" not in source


def test_runtime_model_preflight_accepts_the_official_tabular_output() -> None:
    helper = _model_line_pattern_helper()
    slug = ANTIGRAVITY_RUNTIME_MODELS["low"]
    assert helper(slug) == "^" + re.escape(slug) + r"([[:space:]]|$)"
    source = Path(providers.__file__).with_name("pier_antigravity.py").read_text()
    assert "grep -Eq" in source
    assert "grep -Fqx {shlex.quote(slug)}" not in source


def test_official_step_ledger_reconciles_without_double_counting_thinking() -> None:
    helper = _usage_helper()
    runtime = ANTIGRAVITY_RUNTIME_MODELS["low"]
    first = _usage(100, 20, 60, 15)
    checkpoint = _usage(10, 2, 0, 0)
    terminal = _usage(110, 22, 60, 15)
    events = [
        {"event": "init", "init": {
            "model": runtime, "cwd": "/app", "permission_mode": "always-proceed",
        }},
        {"event": "step_update", "step_update": {
            "step_index": 1, "step_type": "agent_response", "state": "DONE",
            "usage": first,
        }},
        {"event": "step_update", "step_update": {
            "step_index": 2, "step_type": "checkpoint", "state": "DONE",
            "usage": checkpoint,
        }},
        {"event": "result", "result": {
            "status": "SUCCESS", "num_turns": 1, "usage": terminal,
        }},
    ]
    facts = helper(events, expected_runtime_model=runtime)
    assert facts["complete"] is True
    assert facts["request_count"] == 2
    # DRadar input includes the cached subset, while AGY's raw input excludes
    # it.  Thinking remains a subset of output and must not be added again.
    assert facts["n_input_tokens"] == 170
    assert facts["n_cache_tokens"] == 60
    assert facts["n_output_tokens"] == 22
    assert facts["thinking_tokens"] == 15
    assert sum(item["n_input_tokens"] for item in facts["token_usage_events"]) == 170
    assert sum(item["n_output_tokens"] for item in facts["token_usage_events"]) == 22


def test_stream_interrupted_after_final_response_emits_bound_recovery_evidence() -> None:
    helper = _usage_helper()
    runtime = ANTIGRAVITY_RUNTIME_MODELS["high"]
    usage = _usage(100, 20, 60, 15)
    response = "Implemented, validated, and committed."
    events = [
        {"event": "init", "init": {
            "model": runtime, "cwd": "/app", "permission_mode": "always-proceed",
        }},
        {"event": "step_update", "step_update": {
            "step_index": 1, "step_type": "agent_response", "state": "DONE",
            "usage": usage,
        }},
        {"event": "result", "result": {
            "status": "ERROR", "num_turns": 1, "usage": usage,
            "response": response,
            "error": (
                "The stream was interrupted. Please continue the task you were "
                "working on."
            ),
        }},
    ]

    facts = helper(events, expected_runtime_model=runtime)

    assert facts["complete"] is True
    assert facts["terminal_status"] == "ERROR"
    assert facts["terminal_error_category"] == "stream-interrupted"
    assert facts["terminal_recovery"] == {
        "schema": "dradar-antigravity-terminal-recovery-v1",
        "reason": "stream_interrupted_after_final_response",
        "response_sha256": hashlib.sha256(response.encode()).hexdigest(),
    }


@pytest.mark.parametrize("mutation", ["different-error", "empty-response"])
def test_other_antigravity_errors_never_emit_recovery_evidence(mutation: str) -> None:
    helper = _usage_helper()
    runtime = ANTIGRAVITY_RUNTIME_MODELS["high"]
    usage = _usage(100, 20, 60, 15)
    result = {
        "status": "ERROR", "num_turns": 1, "usage": usage,
        "response": "done",
        "error": (
            "The stream was interrupted. Please continue the task you were "
            "working on."
        ),
    }
    if mutation == "different-error":
        result["error"] = "provider failed"
    else:
        result["response"] = "  "
    events = [
        {"event": "init", "init": {
            "model": runtime, "cwd": "/app", "permission_mode": "always-proceed",
        }},
        {"event": "step_update", "step_update": {
            "step_index": 1, "step_type": "agent_response", "state": "DONE",
            "usage": usage,
        }},
        {"event": "result", "result": result},
    ]

    facts = helper(events, expected_runtime_model=runtime)

    assert facts["complete"] is True
    assert facts["terminal_error_category"] == (
        "provider-error" if mutation == "different-error" else "stream-interrupted"
    )
    assert "terminal_recovery" not in facts


def test_terminal_error_category_is_allowlisted_and_drops_sensitive_text() -> None:
    helper = _usage_helper()
    runtime = ANTIGRAVITY_RUNTIME_MODELS["high"]
    usage = _usage(1, 1, 0, 0)
    private_error = (
        "Eligibility check failed: account secret@example.invalid is not "
        "eligible for Antigravity, because it is not currently available in "
        "your location. request=private-request-body"
    )
    facts = helper([
        {"event": "init", "init": {
            "model": runtime, "cwd": "/app", "permission_mode": "always-proceed",
        }},
        {"event": "result", "result": {
            "status": "ERROR", "num_turns": 0, "usage": usage,
            "response": "", "error": private_error,
        }},
    ], expected_runtime_model=runtime)

    assert facts["terminal_error_category"] == "eligibility-location"
    serialized = json.dumps(facts)
    assert "secret@example.invalid" not in serialized
    assert "private-request-body" not in serialized
    assert "not currently available" not in serialized


def test_official_cache_can_exceed_uncached_input_and_still_reconcile() -> None:
    helper = _usage_helper()
    runtime = ANTIGRAVITY_RUNTIME_MODELS["low"]
    # Aggregate captured from a real AGY 1.1.22 Pompeii run.  Its official
    # input is uncached-only and its total excludes the separate cache bucket.
    usage = _usage(81_240, 4_603, 182_532, 3_280)
    events = [
        {"event": "init", "init": {
            "model": runtime, "cwd": "/app", "permission_mode": "always-proceed",
        }},
        {"event": "step_update", "step_update": {
            "step_index": 1, "step_type": "agent_response", "state": "DONE",
            "usage": usage,
        }},
        {"event": "result", "result": {
            "status": "SUCCESS", "num_turns": 1, "usage": usage,
        }},
    ]

    facts = helper(events, expected_runtime_model=runtime)

    assert facts["complete"] is True
    assert facts["n_input_tokens"] == 263_772
    assert facts["n_cache_tokens"] == 182_532
    assert facts["n_output_tokens"] == 4_603
    assert facts["thinking_tokens"] == 3_280


@pytest.mark.parametrize("broken_field", ["total_tokens", "thinking_tokens"])
def test_invalid_official_usage_never_reconciles(broken_field: str) -> None:
    helper = _usage_helper()
    runtime = ANTIGRAVITY_RUNTIME_MODELS["high"]
    usage = _usage(10, 2, 30, 1)
    usage[broken_field] = 13 if broken_field == "total_tokens" else 3
    events = [
        {"event": "init", "init": {
            "model": runtime, "cwd": "/app", "permission_mode": "always-proceed",
        }},
        {"event": "step_update", "step_update": {
            "step_index": 1, "step_type": "agent_response", "state": "DONE",
            "usage": usage,
        }},
        {"event": "result", "result": {
            "status": "SUCCESS", "num_turns": 1, "usage": usage,
        }},
    ]

    facts = helper(events, expected_runtime_model=runtime)

    assert facts["complete"] is False
    assert facts["usage_evidence_tier"] == "unavailable"


def test_conflicting_duplicate_or_wrong_runtime_never_becomes_complete() -> None:
    helper = _usage_helper()
    runtime = ANTIGRAVITY_RUNTIME_MODELS["medium"]
    events = [
        {"event": "init", "init": {
            "model": runtime, "cwd": "/app", "permission_mode": "always-proceed",
        }},
        {"event": "step_update", "step_update": {
            "step_index": 1, "step_type": "agent_response", "state": "DONE",
            "usage": _usage(10, 2, 0, 1),
        }},
        {"event": "step_update", "step_update": {
            "step_index": 1, "step_type": "agent_response", "state": "DONE",
            "usage": _usage(11, 2, 0, 1),
        }},
        {"event": "result", "result": {
            "status": "SUCCESS", "num_turns": 1, "usage": _usage(10, 2, 0, 1),
        }},
    ]
    assert helper(events, expected_runtime_model=runtime)["complete"] is False
    events[0]["init"]["model"] = "gemini-3.7-flash-low"
    assert helper(events[:2] + events[3:], expected_runtime_model=runtime)["complete"] is False


def test_provider_failure_is_additive_and_does_not_strip_other_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "direct-google-key-must-not-enter-agy")
    monkeypatch.setenv("XAI_API_KEY", "unrelated-grok-key")
    agy_env = runner._pier_process_env(_assignment())
    assert "GEMINI_API_KEY" not in agy_env
    assert agy_env["XAI_API_KEY"] == "unrelated-grok-key"

    grok_env = runner._pier_process_env({"agent": providers.GROK_AGENT})
    assert "XAI_API_KEY" not in grok_env
    assert grok_env["GEMINI_API_KEY"] == "direct-google-key-must-not-enter-agy"


def test_capability_name_is_additive_and_refill_alias_is_canonical() -> None:
    assert ANTIGRAVITY_CAPABILITY.startswith("antigravity-gemini-3.7-flash-")
    assert providers.normalize_refill_harness("agy") == ANTIGRAVITY_AGENT
    assert providers.validate_refill_scope(
        "antigravity", ANTIGRAVITY_MODEL, "high",
    ) == (ANTIGRAVITY_AGENT, ANTIGRAVITY_MODEL, "high")
