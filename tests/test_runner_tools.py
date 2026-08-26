import os
import tomllib

import dradar.runner as runner_mod
from dradar.runner import CLAUDE_DISALLOWED_TOOLS, build_pier_command


def _assignment(agent, model="gpt-5.5", effort="medium"):
    return {"assignment_id": "a1", "task_id": "abs-module-cache-flags",
            "agent": agent, "model": model, "effort": effort,
            "agent_version": "0.145.0"}


def _stub_pier(monkeypatch):
    # build_pier_command resolves pier via shutil.which; stub it so the test
    # doesn't depend on pier being on the runner's PATH.
    monkeypatch.setattr(runner_mod.shutil, "which", lambda _: "/usr/bin/pier")


def test_codex_disables_server_side_network_tools(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    # make the local task path exist so build_pier_command doesn't bail
    task = tmp_path / "abs-module-cache-flags"
    task.mkdir()
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(tmp_path / "auth.json"))
    (tmp_path / "auth.json").write_text("{}")
    home = tmp_path / "home"
    home.mkdir()
    cmd = build_pier_command(
        _assignment("codex"), tmp_path, tmp_path / "jobs", "j", home,
    )
    assert cmd[cmd.index("--agent-import-path") + 1] == (
        runner_mod.CODEX_AGENT_IMPORT_PATH
    )
    assert (home / runner_mod.CODEX_AGENT_MODULE_FILENAME).is_file()
    assert (home / runner_mod.CHECKPOINT_MODULE_FILENAME).is_file()
    allowlist = (home / "codex-chatgpt-allowlist.toml").read_text()
    # web_search must be a top-level string key BEFORE any [table] header, or
    # TOML nests it and codex ignores it (verified: bool/nested = no effect).
    assert 'web_search = "disabled"' in allowlist
    assert allowlist.index("web_search") < allowlist.index("[__pier_allowlist]")
    config = tomllib.loads(allowlist)
    assert config["web_search"] == "disabled"
    assert config["features"]["apps"] is False
    assert config["features"]["remote_plugin"] is False
    assert config["__pier_allowlist"] == {"url": "https://chatgpt.com"}


def test_codex_prompt_leaves_post_run_artifact_to_pier(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = tmp_path / "abs-module-cache-flags"
    task.mkdir()
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(tmp_path / "auth.json"))
    (tmp_path / "auth.json").write_text("{}")
    home = tmp_path / "home"
    home.mkdir()

    cmd = build_pier_command(_assignment("codex"), tmp_path, tmp_path / "jobs", "j", home)

    prompt = home / "codex-submission-prompt.j2"
    assert f"prompt_template_path={prompt}" in cmd
    text = prompt.read_text()
    assert "{{ instruction }}" in text
    assert "complete and committed" in text
    assert "creates the submission artifact automatically" in text
    assert "bash /tests/pre_artifacts.sh" not in text
    assert "test -s /logs/artifacts/model.patch" not in text


def test_pompeii_prompt_sets_simple_time_budget(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = tmp_path / "abs-module-cache-flags"
    task.mkdir()
    (task / "task.toml").write_text("[agent]\ntimeout_sec = 7200.0\n")
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(tmp_path / "auth.json"))
    (tmp_path / "auth.json").write_text("{}")
    home = tmp_path / "home"
    home.mkdir()
    assignment = _assignment("codex") | {
        "benchmark_id": runner_mod.POMPEII_BENCHMARK_ID,
    }

    cmd = build_pier_command(
        assignment, tmp_path, tmp_path / "jobs", "j", home,
    )

    prompt = home / "codex-submission-prompt-pompeii-v2.j2"
    assert f"prompt_template_path={prompt}" in cmd
    text = prompt.read_text()
    assert "within 90 minutes" in text
    assert "no later than 120 minutes" in text
    assert "timeout to at most 600 seconds" in text
    assert "at least 10 minutes in reserve" in text
    assert "do not start time-consuming new experiments" in text
    assert "complete, gradeable answer" in text


def test_dev_agent_codex_uses_durable_openai_provider_default(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = tmp_path / "abs-module-cache-flags"
    task.mkdir()
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(tmp_path / "auth.json"))
    (tmp_path / "auth.json").write_text("{}")
    home = tmp_path / "home"
    home.mkdir()

    assignment = _assignment("claude-code", model="gpt-5.5")
    cmd = build_pier_command(
        assignment, tmp_path, tmp_path / "jobs", "j", home,
        dev_agent="codex",
    )

    assert cmd[cmd.index("--agent-import-path") + 1] == (
        runner_mod.CODEX_AGENT_IMPORT_PATH
    )
    assert "--agent" not in cmd


def test_codex_adapter_dir_is_the_only_added_python_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/ambient/unsafe")
    assignment = _assignment("codex")

    env = runner_mod._pier_process_env(
        assignment, codex_module_dir=tmp_path / "runtime",
    )

    assert env["PYTHONPATH"] == str(tmp_path / "runtime")


def test_claude_code_disallows_web_tools(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = tmp_path / "abs-module-cache-flags"
    task.mkdir()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    cmd = build_pier_command(_assignment("claude-code", model="claude-sonnet-5", effort="high"),
                             tmp_path, tmp_path / "jobs", "j", tmp_path / "home")
    assert f"disallowed_tools={CLAUDE_DISALLOWED_TOOLS}" in cmd
    assert "WebSearch" in CLAUDE_DISALLOWED_TOOLS and "WebFetch" in CLAUDE_DISALLOWED_TOOLS


# --- pier's inner agent timeout must never undercut DRadar's own outer one --
# (volunteer report #4, 2026-07-15: task.toml declares a flat 5400s/90min
# agent timeout across the whole deep-swe set; DRadar's own outer watchdog
# scales up to 4x the server's estimate, but build_pier_command never told
# pier to stretch its OWN timeout to match, so pier killed long/heavy cells
# far before DRadar's watchdog ever would have).

def _task_with_toml(tmp_path, task_id="t", timeout_sec=5400.0):
    task = tmp_path / task_id
    task.mkdir()
    (task / "task.toml").write_text(f"[agent]\ntimeout_sec = {timeout_sec}\n")
    return task


def test_task_agent_timeout_sec_reads_task_toml(tmp_path):
    task = _task_with_toml(tmp_path, timeout_sec=5400.0)
    assert runner_mod._task_agent_timeout_sec(task) == 5400.0


def test_task_agent_timeout_sec_none_when_missing(tmp_path):
    task = tmp_path / "no-toml"
    task.mkdir()
    assert runner_mod._task_agent_timeout_sec(task) is None


def test_task_agent_timeout_sec_none_when_malformed(tmp_path):
    task = tmp_path / "bad-toml"
    task.mkdir()
    (task / "task.toml").write_text("this is not [ valid toml")
    assert runner_mod._task_agent_timeout_sec(task) is None


def test_multiplier_stretches_pier_to_match_drader_outer_cap(tmp_path):
    # est_minutes=68 -> outer = max(1800, 68*60*4) = 16320s; base 5400s ->
    # pier must be stretched so its own timeout is >= outer, plus slack.
    task = _task_with_toml(tmp_path, timeout_sec=5400.0)
    assignment = {"est_minutes": 68}
    m = runner_mod._agent_timeout_multiplier(assignment, task)
    assert m > 1.0
    assert m * 5400.0 >= 16320 + 60


def test_multiplier_never_shrinks_below_one(tmp_path):
    # A short-estimate cell: outer cap (1800s floor) is well under the task's
    # own 5400s default -- must NOT ask pier to shrink its own timeout.
    task = _task_with_toml(tmp_path, timeout_sec=5400.0)
    assignment = {"est_minutes": 5}
    assert runner_mod._agent_timeout_multiplier(assignment, task) == 1.0


def test_pompeii_multiplier_keeps_two_hour_pack_at_120_minutes(tmp_path):
    task = _task_with_toml(tmp_path, timeout_sec=7200.0)
    assignment = {
        "benchmark_id": runner_mod.POMPEII_BENCHMARK_ID,
        "est_minutes": 120,
    }
    multiplier = runner_mod._agent_timeout_multiplier(assignment, task)
    assert multiplier == 1.0
    assert multiplier * 7200.0 == runner_mod.POMPEII_AGENT_TIMEOUT_SEC


def test_pompeii_multiplier_stretches_90_minute_pack_to_120_minutes(tmp_path):
    task = _task_with_toml(tmp_path, timeout_sec=5400.0)
    assignment = {
        "benchmark_id": runner_mod.POMPEII_BENCHMARK_ID,
        "est_minutes": 120,
    }
    multiplier = runner_mod._agent_timeout_multiplier(assignment, task)
    assert multiplier == 1.333333
    assert 7199.0 < multiplier * 5400.0 <= runner_mod.POMPEII_AGENT_TIMEOUT_SEC


def test_kimi_pompeii_multiplier_stretches_pack_to_240_minutes(tmp_path):
    for pack_timeout_sec, expected_multiplier in ((7200.0, 2.0), (5400.0, 2.666666)):
        task = _task_with_toml(
            tmp_path,
            task_id=f"kimi-{int(pack_timeout_sec)}",
            timeout_sec=pack_timeout_sec,
        )
        assignment = {
            "agent": runner_mod.KIMI_AGENT,
            "benchmark_id": runner_mod.POMPEII_BENCHMARK_ID,
            "est_minutes": 120,
        }
        multiplier = runner_mod._agent_timeout_multiplier(assignment, task)
        assert multiplier == expected_multiplier
        effective_timeout = multiplier * pack_timeout_sec
        assert (
            runner_mod.KIMI_POMPEII_AGENT_TIMEOUT_SEC - 1
            < effective_timeout
            <= runner_mod.KIMI_POMPEII_AGENT_TIMEOUT_SEC
        )


def test_pompeii_timeout_requires_readable_task_config(tmp_path):
    task = tmp_path / "no-toml"
    task.mkdir()
    assignment = {"benchmark_id": runner_mod.POMPEII_BENCHMARK_ID}
    with pytest.raises(RunnerError, match="120-minute execution limit"):
        runner_mod._agent_timeout_multiplier(assignment, task)


def test_kimi_pompeii_timeout_error_reports_240_minute_limit(tmp_path):
    task = tmp_path / "no-toml-kimi"
    task.mkdir()
    assignment = {
        "agent": runner_mod.KIMI_AGENT,
        "benchmark_id": runner_mod.POMPEII_BENCHMARK_ID,
    }
    with pytest.raises(RunnerError, match="240-minute execution limit"):
        runner_mod._agent_timeout_multiplier(assignment, task)


def test_multiplier_is_one_without_task_toml(tmp_path):
    task = tmp_path / "no-toml"
    task.mkdir()
    assert runner_mod._agent_timeout_multiplier({"est_minutes": 200}, task) == 1.0


def test_build_pier_command_passes_multiplier_for_long_estimate(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = _task_with_toml(tmp_path, task_id="abs-module-cache-flags", timeout_sec=5400.0)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    a = _assignment("claude-code", model="claude-sonnet-5", effort="high")
    a["est_minutes"] = 68
    cmd = build_pier_command(a, tmp_path, tmp_path / "jobs", "j", tmp_path / "home")
    assert "--agent-timeout-multiplier" in cmd
    got = float(cmd[cmd.index("--agent-timeout-multiplier") + 1])
    assert got * 5400.0 >= 16320 + 60


def test_build_pier_command_omits_multiplier_for_short_estimate(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = _task_with_toml(tmp_path, task_id="abs-module-cache-flags", timeout_sec=5400.0)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    a = _assignment("claude-code", model="claude-sonnet-5", effort="high")
    a["est_minutes"] = 5
    cmd = build_pier_command(a, tmp_path, tmp_path / "jobs", "j", tmp_path / "home")
    assert "--agent-timeout-multiplier" not in cmd


def test_build_pier_command_keeps_two_hour_pompeii_pack(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    _task_with_toml(
        tmp_path, task_id="abs-module-cache-flags", timeout_sec=7200.0,
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    assignment = _assignment(
        "claude-code", model="claude-sonnet-5", effort="high",
    ) | {
        "benchmark_id": runner_mod.POMPEII_BENCHMARK_ID,
        "est_minutes": 120,
    }
    cmd = build_pier_command(
        assignment, tmp_path, tmp_path / "jobs", "j", tmp_path / "home",
    )
    assert "--agent-timeout-multiplier" not in cmd


def test_build_pier_command_stretches_90_minute_pompeii_pack(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    _task_with_toml(
        tmp_path, task_id="abs-module-cache-flags", timeout_sec=5400.0,
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    assignment = _assignment(
        "claude-code", model="claude-sonnet-5", effort="high",
    ) | {
        "benchmark_id": runner_mod.POMPEII_BENCHMARK_ID,
        "est_minutes": 120,
    }
    cmd = build_pier_command(
        assignment, tmp_path, tmp_path / "jobs", "j", tmp_path / "home",
    )
    index = cmd.index("--agent-timeout-multiplier")
    assert cmd[index + 1] == "1.333333"


def test_codex_command_enables_credential_free_checkpoint_metadata(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = tmp_path / "task"
    task.mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(auth))
    resume = tmp_path / "previous" / "checkpoint"
    a = _assignment("codex") | {
        "assignment_id": "a123", "task_id": "task",
        "resume_generation": 3,
    }
    cmd = build_pier_command(
        a, tmp_path, tmp_path / "jobs", "j", tmp_path / "home",
        resume_checkpoint=resume,
    )
    agent_values = [cmd[i + 1] for i, value in enumerate(cmd[:-1]) if value == "--ak"]
    assert "checkpoint_enabled=true" in agent_values
    assert "checkpoint_assignment_id=a123" in agent_values
    assert "checkpoint_task_id=task" in agent_values
    assert "checkpoint_resume_generation=3" in agent_values
    assert f"checkpoint_path={resume}" in agent_values
    # Auth is injected separately into the ephemeral container, never encoded
    # in the persistent checkpoint metadata.
    assert not any("auth" in value.lower() or "token" in value.lower()
                   for value in agent_values if value.startswith("checkpoint_"))


# --- self-bootstrap (ensure_pier / ensure_tasks_root) ------------------------
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from dradar.runner import RunnerError, ensure_pier, ensure_tasks_root


def test_resolve_user_tool_falls_back_to_uv_user_bin(tmp_path, monkeypatch):
    user_bin = tmp_path / ".local" / "bin"
    user_bin.mkdir(parents=True)
    pier = user_bin / "pier"
    pier.write_text("#!/bin/sh\n")
    pier.chmod(0o700)
    monkeypatch.setattr(runner_mod.shutil, "which", lambda _name: None)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert runner_mod._resolve_user_tool("pier", home=tmp_path) == str(pier)


def test_resolve_user_tool_keeps_path_lookup_authoritative(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner_mod.shutil, "which", lambda name: f"/opt/tools/{name}",
    )

    assert runner_mod._resolve_user_tool("pier", home=tmp_path) == "/opt/tools/pier"


@pytest.fixture(autouse=True)
def _isolated_pier_install_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner_mod, "_pier_install_lock_path",
        lambda: tmp_path / "pier-install.lock",
    )


@pytest.fixture(autouse=True)
def _stub_latest_codex_version(monkeypatch, request):
    """Unit tests must never depend on the live npm registry."""
    if request.node.name.startswith("test_resolve_latest_codex_cli_version"):
        return
    monkeypatch.setattr(
        runner_mod, "resolve_latest_codex_cli_version", lambda *a, **k: "0.145.0",
    )


def test_ensure_pier_noop_when_required_version_present(monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: "/usr/bin/pier")
    monkeypatch.setattr(runner_mod, "_pier_version", lambda _: runner_mod.PIER_VERSION)
    called = []
    monkeypatch.setattr(runner_mod.subprocess, "run", lambda *a, **k: called.append(a))
    ensure_pier()
    assert called == []            # approved build -> never installs


def test_ensure_pier_accepts_newer_compatible_post_release(monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: "/usr/bin/pier")
    monkeypatch.setattr(runner_mod, "_pier_version", lambda _: "0.3.0.post3")
    called = []
    monkeypatch.setattr(runner_mod.subprocess, "run", lambda *a, **k: called.append(a))
    ensure_pier()
    assert called == []


def test_pier_version_prefers_local_distribution_metadata(monkeypatch):
    monkeypatch.setattr(
        runner_mod, "_pier_metadata_version", lambda _: runner_mod.PIER_VERSION,
    )
    monkeypatch.setattr(
        runner_mod, "_pier_cli_version",
        lambda _: pytest.fail("compatible local metadata must avoid importing Pier"),
    )

    assert runner_mod._pier_version("/usr/bin/pier") == runner_mod.PIER_VERSION


def test_pier_metadata_version_uses_tool_interpreter(monkeypatch, tmp_path):
    interpreter = tmp_path / "python3"
    interpreter.touch(mode=0o755)
    monkeypatch.setattr(
        runner_mod, "_pier_metadata_interpreters", lambda _: (interpreter,),
    )
    seen = []

    def fake_run(cmd, *args, **kwargs):
        seen.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="0.3.0.post3\n")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    assert runner_mod._pier_metadata_version("/usr/bin/pier") == "0.3.0.post3"
    assert seen[0][0][0] == str(interpreter)
    assert seen[0][0][1] == "-c"
    assert seen[0][1]["timeout"] == 5


def test_ensure_pier_installs_via_uv_when_missing(monkeypatch):
    seen = {"pier": None}  # pier missing first, present after "install"
    def which(name):
        if name == "uv":
            return "/usr/bin/uv"
        return seen["pier"]
    monkeypatch.setattr(runner_mod.shutil, "which", which)
    monkeypatch.setattr(
        runner_mod, "_pier_version",
        lambda path: runner_mod.PIER_VERSION if path else None,
    )
    def fake_run(cmd, *a, **k):
        assert cmd == [
            "/usr/bin/uv", "tool", "install", "--force", runner_mod.PIER_SPEC,
        ]
        seen["pier"] = "/root/.local/bin/pier"   # simulate the install landing
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    ensure_pier()                  # should not raise


def test_ensure_pier_replaces_old_version(monkeypatch):
    # Initial check, under-lock recheck, then post-install verification.
    versions = iter(["0.3.0", "0.3.0", runner_mod.PIER_VERSION])
    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(runner_mod, "_pier_version", lambda _: next(versions))
    called = []
    monkeypatch.setattr(
        runner_mod.subprocess, "run",
        lambda cmd, *a, **k: called.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    ensure_pier()

    assert called == [[
        "/usr/bin/uv", "tool", "install", "--force", runner_mod.PIER_SPEC,
    ]]


def test_ensure_pier_rechecks_version_after_waiting_for_install_lock(monkeypatch):
    seen = {"pier": None}

    def which(name):
        if name == "uv":
            return "/usr/bin/uv"
        return seen["pier"]

    @contextmanager
    def another_process_installs_first():
        seen["pier"] = "/root/.local/bin/pier"
        yield

    monkeypatch.setattr(runner_mod.shutil, "which", which)
    monkeypatch.setattr(
        runner_mod, "_pier_version",
        lambda path: runner_mod.PIER_VERSION if path else None,
    )
    monkeypatch.setattr(runner_mod, "_pier_install_lock", another_process_installs_first)
    monkeypatch.setattr(
        runner_mod.subprocess, "run",
        lambda *a, **k: pytest.fail("the second process must not reinstall Pier"),
    )

    ensure_pier()


def test_ensure_pier_never_reinstalls_unverifiable_existing_tool(monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(runner_mod, "_pier_version", lambda _: None)
    monkeypatch.setattr(
        runner_mod.subprocess, "run",
        lambda *a, **k: pytest.fail("must not mutate an in-use shared Pier tool"),
    )

    with pytest.raises(RunnerError, match="refusing to replace a shared tool"):
        ensure_pier()


def test_ensure_pier_errors_when_no_uv(monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: None)
    with pytest.raises(RunnerError, match="uv"):
        ensure_pier()


def test_ensure_pier_missing_uv_uses_windows_install_hint(monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: None)
    monkeypatch.setattr(runner_mod.sys, "platform", "win32")
    with pytest.raises(RunnerError, match=r"install\.ps1") as exc:
        ensure_pier()
    assert "install.sh" not in str(exc.value)


def test_ensure_tasks_root_noop_when_present(tmp_path, monkeypatch):
    tr = tmp_path / "deep-swe" / "tasks"
    tr.mkdir(parents=True)
    monkeypatch.setattr(runner_mod.subprocess, "run",
                        lambda *a, **k: pytest.fail("should not clone"))
    ensure_tasks_root(tr)          # exists -> no clone


def test_ensure_tasks_root_rejects_non_tasks_path(tmp_path):
    with pytest.raises(RunnerError, match="deep-swe/tasks"):
        ensure_tasks_root(tmp_path / "somewhere" / "else")


def test_ensure_tasks_root_wont_clobber_nonempty_parent(tmp_path):
    repo = tmp_path / "deep-swe"
    repo.mkdir()
    (repo / "junk").write_text("x")   # parent exists, non-empty, no tasks/
    with pytest.raises(RunnerError, match="not touching"):
        ensure_tasks_root(repo / "tasks")


def test_ensure_tasks_root_clones_when_missing(tmp_path, monkeypatch):
    tr = tmp_path / "deep-swe" / "tasks"
    def fake_run(cmd, *a, **k):
        assert cmd[0] == "git" and cmd[1] == "clone"
        (tr).mkdir(parents=True)   # simulate the clone creating tasks/
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    ensure_tasks_root(tr)
    assert tr.is_dir()


# --- run_trial / summarize_result (stubbed pier) ------------------------------
import json

from dradar.runner import (
    _effective_trial_timeout_sec,
    _trial_timeout_sec,
    run_trial,
    summarize_result,
)
from dradar.runner import (
    _dsh_tasks_overlay,
    _normalize_utf16_patch,
    _verify_dsh_artifact_binding,
)


def test_dsh_pompeii_pack_with_existing_hook_accepts_non_git_base_marker(tmp_path):
    task_id = "pompeii-adjacency-rp-055"
    task = tmp_path / "tasks" / task_id
    task.mkdir(parents=True)
    (task / "task.toml").write_text(
        '[agent]\nnetwork_mode = "no-network"\n'
        '[environment]\nallow_internet = false\n'
        '[metadata]\nbase_commit_hash = "pompeii-base"\n'
    )
    (task / "pre_artifacts.sh").write_text("#!/bin/sh\n")

    with _dsh_tasks_overlay(
        {"task_id": task_id}, tmp_path / "tasks", tmp_path / "work", "job"
    ) as selected:
        assert selected == tmp_path / "tasks"


def _fake_pier(monkeypatch, work_dir, *, patch=True, trajectory=True,
               trajectory_payload=None, runtime_diagnostic=None,
               zcode_outcome=None, provider_usage_sidecar=None,
               result=None, rc=0):
    """Stub build_pier_command + subprocess.run; the fake 'pier' lays down the
    trial-dir layout the real one would. Returns a dict capturing job_name."""
    captured = {}

    def fake_build(assignment, tasks_root, jobs_dir, job_name, home,
                   dev_agent=None, **provider_kwargs):
        captured["job_name"] = job_name
        return ["pier", "run", job_name]

    class FakePopen:
        # run_trial drives pier via Popen + a heartbeat wait loop; the fake
        # lays the artifacts down at construction ("process started and
        # finished") and reports done on the first wait().
        def __init__(self, cmd, **kw):
            trial = work_dir / "jobs" / captured["job_name"] / "task__t0"
            (trial / "artifacts").mkdir(parents=True)
            (trial / "agent").mkdir()
            if patch:
                (trial / "artifacts" / "model.patch").write_text("diff")
            if trajectory:
                payload = (
                    trajectory_payload
                    if trajectory_payload is not None
                    else []
                )
                (trial / "agent" / "trajectory.json").write_text(
                    json.dumps(payload)
                )
            if runtime_diagnostic is not None:
                (trial / "agent" / "zcode-runtime-diagnostic.json").write_text(
                    json.dumps(runtime_diagnostic)
                )
            if zcode_outcome is not None:
                (trial / "agent" / "zcode-outcome.json").write_text(
                    json.dumps(zcode_outcome)
                )
            if provider_usage_sidecar is not None:
                (trial / "agent" / "provider-usage.json").write_text(
                    json.dumps(provider_usage_sidecar)
                )
            if result is not None:
                (trial / "result.json").write_text(json.dumps(result))
            self.returncode = rc

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(runner_mod, "build_pier_command", fake_build)
    monkeypatch.setattr(runner_mod.subprocess, "Popen", FakePopen)
    return captured


def test_run_trial_on_started_exception_is_swallowed(tmp_path, monkeypatch):
    _fake_pier(monkeypatch, tmp_path)
    calls = []
    def boom():
        calls.append(True)
        raise RuntimeError("network hiccup")
    # a failed started-ping must never abort a quota-burning trial
    art = run_trial(_assignment("codex"), tmp_path, tmp_path, on_started=boom)
    assert calls == [True]
    assert art.returncode == 0 and art.patch.is_file()
    assert art.trajectory is not None and art.trajectory.is_file()


def test_run_trial_timeout_raises_naming_log(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "build_pier_command", lambda *a, **k: ["pier"])
    # deadline already passed -> the very first heartbeat check aborts
    monkeypatch.setattr(runner_mod, "_trial_timeout_sec", lambda a: -1)
    killed = []
    cleaned = []
    popen_kwargs = {}

    class HungPopen:
        def __init__(self, cmd, **kw):
            popen_kwargs.update(kw)
            # the wedged "pier" wrote its dying words to the log before hanging
            kw["stdout"].write("docker: no space left on device\n")
            kw["stdout"].flush()

        def wait(self, timeout=None):
            if killed:
                return -9
            raise subprocess.TimeoutExpired("pier", timeout)

        def terminate(self):
            pass  # the hung pier ignores TERM; the grace window must escalate

        def kill(self):
            killed.append(True)

    monkeypatch.setattr(runner_mod.subprocess, "Popen", HungPopen)
    monkeypatch.setattr(
        runner_mod,
        "_cleanup_terminated_pier_containers",
        lambda job_root: cleaned.append(job_root),
    )
    with pytest.raises(RunnerError) as exc:
        run_trial(_assignment("codex"), tmp_path, tmp_path)
    assert killed  # the wedged process was reaped, not left running
    assert cleaned == [tmp_path / "jobs" / "aa1"]
    assert popen_kwargs["start_new_session"] is (os.name != "nt")
    assert str(tmp_path / "aa1.log") in str(exc.value)
    # the actual cause is inlined, not just the file name
    assert "docker: no space left on device" in str(exc.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups only")
def test_terminate_pier_process_tree_escalates_the_entire_group(monkeypatch):
    signals = []
    direct = []

    class GroupProcess:
        pid = 43210

        def wait(self, timeout=None):
            if timeout == 15:
                raise subprocess.TimeoutExpired("pier", timeout)
            return -9

        def terminate(self):
            direct.append("terminate")

        def kill(self):
            direct.append("kill")

    monkeypatch.setattr(
        runner_mod.os, "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    assert runner_mod._terminate_pier_process_tree(GroupProcess()) is True
    assert signals == [
        (43210, runner_mod.signal.SIGTERM),
        (43210, runner_mod.signal.SIGKILL),
    ]
    assert direct == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups only")
def test_terminate_pier_kills_children_after_leader_exits(monkeypatch):
    signals = []

    class LeaderExited:
        pid = 54321

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            raise AssertionError("group TERM should be used")

        def kill(self):
            raise AssertionError("group KILL should be used")

    monkeypatch.setattr(
        runner_mod.os, "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    assert runner_mod._terminate_pier_process_tree(LeaderExited()) is True
    assert signals == [
        (54321, runner_mod.signal.SIGTERM),
        (54321, 0),
        (54321, runner_mod.signal.SIGKILL),
    ]


def test_forced_cleanup_removes_only_container_bound_to_exact_job(
    tmp_path, monkeypatch,
):
    owned_id = "a" * 64
    foreign_id = "b" * 64
    job_root = tmp_path / "jobs" / "aa1"
    foreign_root = tmp_path / "jobs" / "aa2"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(
                command, 0, stdout=owned_id[:12] + "\n" + foreign_id[:12] + "\n",
                stderr="",
            )
        if command[:2] == ["docker", "inspect"]:
            containers = [
                {
                    "Id": owned_id,
                    "Config": {"Labels": {
                        "com.docker.compose.project": "pier__abcdef",
                    }},
                    "Mounts": [{
                        "Type": "bind",
                        "Source": str(job_root / "task__t0"),
                    }],
                },
                {
                    "Id": foreign_id,
                    "Config": {"Labels": {
                        "com.docker.compose.project": "pier__123456",
                    }},
                    "Mounts": [{
                        "Type": "bind",
                        "Source": str(foreign_root / "task__t0"),
                    }],
                },
            ]
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(containers), stderr="",
            )
        if command[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    runner_mod._cleanup_terminated_pier_containers(job_root)

    assert calls[-1] == ["docker", "rm", "-f", owned_id]


def test_run_trial_stops_live_codex_quota_error_loop(tmp_path, monkeypatch):
    captured = {}
    terminated = []
    cleaned = []

    def fake_build(assignment, tasks_root, jobs_dir, job_name, home, dev_agent=None):
        captured["job_name"] = job_name
        return ["pier"]

    class QuotaLoopPopen:
        def __init__(self, cmd, **kwargs):
            agent_dir = (
                tmp_path / "jobs" / captured["job_name"]
                / "task__t0" / "agent"
            )
            agent_dir.mkdir(parents=True)
            records = [
                {"type": "thread.started"},
                *(
                    {"type": "error", "message": "usage limit reached"}
                    for _ in range(
                        runner_mod.LIVE_ACCOUNT_ERROR_CONFIRMATIONS
                    )
                ),
            ]
            (agent_dir / "codex.txt").write_text(
                "".join(json.dumps(record) + "\n" for record in records)
            )
            self.returncode = None

        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("pier", timeout)
            return self.returncode

        def terminate(self):
            terminated.append(True)
            self.returncode = 1

        def kill(self):
            raise AssertionError("clean TERM should not escalate to KILL")

    monkeypatch.setattr(runner_mod, "build_pier_command", fake_build)
    monkeypatch.setattr(runner_mod.subprocess, "Popen", QuotaLoopPopen)
    monkeypatch.setattr(
        runner_mod, "_cleanup_terminated_pier_containers",
        lambda job_root: cleaned.append(job_root),
    )

    with pytest.raises(RunnerError, match="quota exhausted") as exc:
        run_trial(_assignment("codex"), tmp_path, tmp_path)

    assert terminated == [True]
    assert cleaned == [tmp_path / "jobs" / "aa1"]
    assert runner_mod.classify_exception_message(str(exc.value)) == "quota-limit"


def test_live_error_watchdog_ignores_prompt_and_transient_errors(tmp_path):
    path = tmp_path / "jobs" / "a1" / "task__t0" / "agent" / "codex.txt"
    path.parent.mkdir(parents=True)
    path.write_text("".join((
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "quota exhausted in user prompt",
        }}) + "\n",
        json.dumps({"type": "error", "message": "temporary network error"}) + "\n",
        json.dumps({"type": "error", "message": "rate limit"}) + "\n",
    )))
    offsets = {}
    counts = {}

    assert runner_mod._scan_live_account_errors(
        tmp_path / "jobs", "a1", offsets, counts,
    ) is None
    assert counts == {}


def test_live_error_watchdog_allows_codex_http_fallback_after_websocket_403(
    tmp_path,
):
    path = tmp_path / "jobs" / "a1" / "task__t0" / "agent" / "codex.txt"
    path.parent.mkdir(parents=True)
    records = [
        {"type": "error", "message": (
            f"Reconnecting... {attempt}/5 (unexpected status 403 Forbidden, "
            "url: wss://chatgpt.com/backend-api/codex/responses)"
        )}
        for attempt in range(1, 6)
    ]
    records.extend((
        {"type": "error", "message": (
            "Falling back from WebSockets to HTTPS transport. unexpected "
            "status 403 Forbidden, "
            "url: wss://chatgpt.com/backend-api/codex/responses"
        )},
        {"type": "turn.completed"},
    ))
    path.write_text("".join(
        json.dumps(record) + "\n" for record in records
    ))
    offsets = {}
    counts = {}

    assert runner_mod._scan_live_account_errors(
        tmp_path / "jobs", "a1", offsets, counts,
    ) is None
    assert counts == {}


def test_live_error_watchdog_keeps_explicit_websocket_auth_terminal(tmp_path):
    path = tmp_path / "jobs" / "a1" / "task__t0" / "agent" / "codex.txt"
    path.parent.mkdir(parents=True)
    message = (
        "Reconnecting... 3/5 (unexpected status 403 Forbidden, "
        "url: wss://chatgpt.com/backend-api/codex/responses; account suspended)"
    )
    path.write_text("".join(
        json.dumps({"type": "error", "message": message}) + "\n"
        for _ in range(runner_mod.LIVE_ACCOUNT_ERROR_CONFIRMATIONS)
    ))
    offsets = {}
    counts = {}

    assert runner_mod._scan_live_account_errors(
        tmp_path / "jobs", "a1", offsets, counts,
    ) == "auth"
    assert counts == {"auth": runner_mod.LIVE_ACCOUNT_ERROR_CONFIRMATIONS}


def test_run_trial_timeout_salvages_patch_as_interrupted(tmp_path, monkeypatch):
    """A paid run that reached artifacts must report cost, never vanish."""
    captured = {}
    cleaned = []

    def fake_build(assignment, tasks_root, jobs_dir, job_name, home, dev_agent=None):
        captured["job_name"] = job_name
        return ["pier"]

    class TimedOutWithArtifacts:
        def __init__(self, cmd, **kw):
            trial = tmp_path / "jobs" / captured["job_name"] / "task__t0"
            (trial / "artifacts").mkdir(parents=True)
            (trial / "artifacts" / "model.patch").write_text("diff")
            (trial / "agent").mkdir()
            (trial / "agent" / "trajectory.json").write_text("[]")
            (trial / "result.json").write_text(json.dumps({
                "agent_result": {"cost_usd": 0.124942, "n_input_tokens": 10},
            }))
            self.returncode = None

        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("pier", timeout)
            return self.returncode

        def terminate(self):
            # Simulate Pier harvesting artifacts and exiting cleanly on TERM.
            self.returncode = 0

        def kill(self):
            raise AssertionError("clean TERM should not escalate to KILL")

    monkeypatch.setattr(runner_mod, "build_pier_command", fake_build)
    monkeypatch.setattr(runner_mod, "_trial_timeout_sec", lambda a: -1)
    monkeypatch.setattr(runner_mod.subprocess, "Popen", TimedOutWithArtifacts)
    monkeypatch.setattr(
        runner_mod, "_cleanup_terminated_pier_containers",
        lambda job_root: cleaned.append(job_root),
    )

    art = run_trial(_assignment("codex"), tmp_path, tmp_path)

    assert art.patch.is_file()
    assert art.trajectory is not None and art.trajectory.is_file()
    assert art.returncode == runner_mod.TRIAL_TIMEOUT_RETURNCODE
    assert summarize_result(art.result)["cost_usd"] == pytest.approx(0.124942)
    assert cleaned == [tmp_path / "jobs" / "aa1"]


def test_run_trial_missing_patch_raises(tmp_path, monkeypatch):
    _fake_pier(monkeypatch, tmp_path, patch=False)
    with pytest.raises(RunnerError, match="model.patch missing"):
        run_trial(_assignment("codex"), tmp_path, tmp_path)


def test_run_trial_dsh_normalizes_pier_zero_when_agent_failed(
    tmp_path, monkeypatch,
):
    task_id = "pompeii-adjacency-rp-055"
    task = tmp_path / task_id
    task.mkdir()
    (task / "task.toml").write_text(
        '[agent]\nnetwork_mode = "no-network"\n'
        '[environment]\nallow_internet = false\n'
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        runner_mod,
        "_verify_dsh_artifact_binding",
        lambda *_args, **_kwargs: {"visionInputAttached": True},
    )
    _fake_pier(
        monkeypatch,
        tmp_path,
        result={
            "exception_info": {
                "exception_type": "NonZeroAgentExitCodeError",
                "exception_message": "dsh: QUOTA: Insufficient Balance",
            }
        },
        rc=0,
    )
    assignment = {
        "assignment_id": "a1",
        "task_id": task_id,
        "benchmark_id": runner_mod.POMPEII_BENCHMARK_ID,
        "agent": runner_mod.DSH_AGENT,
        "provider": runner_mod.DEEPSEEK_PROVIDER,
        "model": runner_mod.DSH_VISION_MODEL,
        "effort": "high",
        "agent_version": runner_mod.DSH_VERSION,
    }

    art = run_trial(assignment, tmp_path, tmp_path)

    assert art.returncode == 1
    assert summarize_result(art.result)["exception_info"] is True


def _zcode_assignment_for_trial():
    return _assignment(
        runner_mod.ZCODE_AGENT,
        model=runner_mod.ZCODE_MODEL,
        effort="max",
    ) | {
        "provider": runner_mod.ZCODE_PROVIDER,
        "agent_version": runner_mod.ZCODE_CLI_VERSION,
        "est_minutes": 30,
    }


def _prepare_fake_zcode(monkeypatch, tmp_path):
    cli = tmp_path / "zcode.cjs"
    cli.write_text("pinned", encoding="utf-8")
    key = tmp_path / "zcode-key"
    key.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(runner_mod, "_validated_zcode_cli_path", lambda: cli)
    monkeypatch.setattr(
        runner_mod, "create_zcode_api_key_file", lambda _work_dir: key,
    )


def test_zcode_terminal_reader_rejects_oversized_json_without_loading_it(
        tmp_path):
    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as stream:
        stream.truncate(runner_mod._ZCODE_TERMINAL_ARTIFACT_MAX_BYTES + 1)

    assert runner_mod._read_capped_json_object(
        oversized, runner_mod._ZCODE_TERMINAL_ARTIFACT_MAX_BYTES,
    ) == {}


def test_zcode_real_redacted_1308_fixture_is_account_quota_terminal() -> None:
    fixture = Path(__file__).with_name("fixtures") / "zcode_quota_1308_outcome.json"

    assert runner_mod._zcode_quota_limit_facts(fixture) == {
        "provider_code": 1308,
        "status_code": 429,
        "reason": "rate_limited",
        "retryable": False,
        "window_reset_explicit": True,
    }


@pytest.mark.parametrize("overrides", [
    {"providerErrorCode": "1308", "statusCode": 429, "retryable": True,
     "reason": "rate_limited", "message": "usage limit; reset later"},
    {"providerErrorCode": "1308", "statusCode": 429, "retryable": False,
     "reason": "rate_limited", "message": "temporary burst rate limit"},
    {"providerErrorCode": "1308", "statusCode": 429, "retryable": False,
     "reason": "rate_limited", "message": "usage limit"},
    {"providerErrorCode": "429", "statusCode": 429, "retryable": False,
     "reason": "rate_limited", "message": "usage limit; reset later"},
])
def test_zcode_quota_terminal_rejects_ordinary_or_ambiguous_429(
        tmp_path, overrides):
    outcome = tmp_path / "zcode-outcome.json"
    outcome.write_text(json.dumps({
        "schema": "dradar-zcode-outcome-v1",
        "events": {"events": [{
            "type": "session.updated",
            "payload": {"type": "model_request_failed", **overrides},
        }]},
    }))

    assert runner_mod._zcode_quota_limit_facts(outcome) is None


def test_run_trial_maps_structured_zcode_quota_to_existing_quota_limit(
        tmp_path, monkeypatch):
    _prepare_fake_zcode(monkeypatch, tmp_path)
    fixture = Path(__file__).with_name("fixtures") / "zcode_quota_1308_outcome.json"
    _fake_pier(
        monkeypatch,
        tmp_path,
        patch=False,
        trajectory=False,
        zcode_outcome=json.loads(fixture.read_text()),
        runtime_diagnostic={
            "schema": "dradar-zcode-runtime-v1",
            "status": "idle",
            "turn_count": 1,
            "seen_running": True,
            "terminal_observed": True,
        },
        rc=0,
    )

    with pytest.raises(RunnerError, match="quota exhausted") as exc:
        run_trial(_zcode_assignment_for_trial(), tmp_path, tmp_path)

    assert runner_mod.classify_exception_message(str(exc.value)) == "quota-limit"
    assert exc.value.failure_diagnostic["failure_code"] == "provider_quota_exhausted"
    assert exc.value.failure_diagnostic["provider_code"] == 1308


def test_run_trial_rejects_real_zcode_model_error_rc0_signature(
        tmp_path, monkeypatch):
    _prepare_fake_zcode(monkeypatch, tmp_path)
    _fake_pier(
        monkeypatch,
        tmp_path,
        result={
            "agent_result": {
                "n_agent_steps": 72,
                "metadata": {"provider_usage": {
                    "schema": "dradar-subscription-provider-usage-v1",
                    "provider": "zcode",
                    "model": runner_mod.ZCODE_MODEL,
                    "complete": False,
                    "request_count": 0,
                    "request_usage_complete": False,
                    "session_usage_model_request_count": 134,
                    "usage_incomplete_reason":
                        "provider_aggregate_missing_or_invalid",
                }},
            },
        },
        trajectory_payload={
            "steps": [
                {"source": "user", "message": "task"},
                {"source": "agent", "message": "unfinished work"},
            ],
            "final_metrics": {
                "extra": {
                    "model_error_count": 1,
                    "session_usage_model_request_count": 134,
                    "model_request_count": 0,
                },
            },
        },
        zcode_outcome={
            "schema": "dradar-zcode-outcome-v1",
            "sessionId": "sess_real_signature",
            "state": {"projection": {"status": "idle", "turnCount": 73}},
            "usage": {"modelErrorCount": 1, "modelRequestCount": 134},
            "events": {"events": []},
        },
        provider_usage_sidecar={
            "schema": "dradar-subscription-provider-usage-v1",
            "provider": "zcode",
            "model": runner_mod.ZCODE_MODEL,
            "complete": False,
            "request_count": 0,
            "request_usage_complete": False,
            "model_error_count": 1,
        },
        runtime_diagnostic={
            "schema": "dradar-zcode-runtime-v1",
            "status": "idle",
            "turn_count": 73,
            "seen_running": True,
            "terminal_observed": True,
        },
        rc=0,
    )

    with pytest.raises(
        RunnerError, match="provider_model_error",
    ) as exc:
        run_trial(_zcode_assignment_for_trial(), tmp_path, tmp_path)

    assert exc.value.failure_diagnostic == {
        "schema": "dradar-runner-failure-v1",
        "failure_code": "agent_no_artifact",
        "trial_timeout_sec": runner_mod.BETA_SUBSCRIPTION_TRIAL_TIMEOUT_FLOOR_SEC,
        "zcode_session_timeout_sec":
            runner_mod.BETA_SUBSCRIPTION_TRIAL_TIMEOUT_FLOOR_SEC + 60,
        "est_minutes": 30.0,
        "zcode_last_status": "idle",
        "zcode_turn_count": 73,
        "zcode_seen_running": True,
        "zcode_terminal_observed": True,
    }


def test_run_trial_rejects_zcode_rc0_without_meaningful_agent_result(
        tmp_path, monkeypatch):
    _prepare_fake_zcode(monkeypatch, tmp_path)
    _fake_pier(
        monkeypatch,
        tmp_path,
        trajectory=False,
        result={
            "agent_result": {
                "n_agent_steps": None,
                "metadata": None,
                "n_input_tokens": None,
                "n_output_tokens": None,
            },
            "exception_info": None,
        },
        runtime_diagnostic={
            "schema": "dradar-zcode-runtime-v1",
            "status": "idle",
            "turn_count": 1,
            "seen_running": True,
            "terminal_observed": True,
        },
        rc=0,
    )

    with pytest.raises(RunnerError, match="empty_agent_result"):
        run_trial(_zcode_assignment_for_trial(), tmp_path, tmp_path)


def test_run_trial_accepts_recovered_zcode_model_error_with_valid_patch(
        tmp_path, monkeypatch):
    _prepare_fake_zcode(monkeypatch, tmp_path)
    _fake_pier(
        monkeypatch,
        tmp_path,
        result={
            "agent_result": {
                "n_agent_steps": 18,
                "metadata": {"provider_usage": {
                    "schema": "dradar-subscription-provider-usage-v1",
                    "provider": "zcode",
                    "model": runner_mod.ZCODE_MODEL,
                    "complete": True,
                    "request_count": 25,
                    "request_usage_complete": True,
                    "request_usage_observed": True,
                    "usage_evidence_tier": "complete_reconciled",
                    "n_input_tokens": 100_000,
                    "n_cache_tokens": 80_000,
                    "n_output_tokens": 5_000,
                }},
            },
        },
        trajectory_payload={
            "steps": [{"source": "agent", "message": "completed patch"}],
            "final_metrics": {"extra": {"model_error_count": 1}},
        },
        zcode_outcome={
            "schema": "dradar-zcode-outcome-v1",
            "sessionId": "sess_valid_patch",
            "state": {"projection": {"status": "idle", "turnCount": 18}},
            "usage": {"modelErrorCount": 1, "modelRequestCount": 25},
        },
        provider_usage_sidecar={
            "schema": "dradar-subscription-provider-usage-v1",
            "provider": "zcode",
            "model": runner_mod.ZCODE_MODEL,
            "complete": True,
            "request_count": 25,
            "request_usage_complete": True,
            "request_usage_observed": True,
            "usage_evidence_tier": "complete_reconciled",
            "model_error_count": 1,
        },
        runtime_diagnostic={
            "schema": "dradar-zcode-runtime-v1",
            "status": "idle",
            "turn_count": 18,
            "seen_running": True,
            "terminal_observed": True,
        },
        rc=0,
    )

    artifact = run_trial(_zcode_assignment_for_trial(), tmp_path, tmp_path)

    assert artifact.returncode == 0
    assert artifact.patch.read_text(encoding="utf-8") == "diff"
    assert summarize_result(artifact.result)["n_agent_steps"] == 18


def test_run_trial_keeps_zcode_patch_gradeable_when_only_usage_is_incomplete(
        tmp_path, monkeypatch):
    _prepare_fake_zcode(monkeypatch, tmp_path)
    _fake_pier(
        monkeypatch,
        tmp_path,
        result={
            "agent_result": {
                "n_agent_steps": 12,
                "metadata": {"provider_usage": {
                    "schema": "dradar-subscription-provider-usage-v1",
                    "provider": "zcode",
                    "model": runner_mod.ZCODE_MODEL,
                    "complete": False,
                    "request_count": 0,
                    "request_usage_complete": False,
                    "usage_incomplete_reason":
                        "provider_aggregate_missing_or_invalid",
                }},
            },
        },
        trajectory_payload={
            "steps": [{"source": "agent", "message": "completed patch"}],
            "final_metrics": {"extra": {"model_error_count": 0}},
        },
        zcode_outcome={
            "schema": "dradar-zcode-outcome-v1",
            "sessionId": "sess_usage_only",
            "state": {"projection": {"status": "idle", "turnCount": 12}},
            "usage": {"modelErrorCount": 0, "modelRequestCount": 12},
        },
        provider_usage_sidecar={
            "schema": "dradar-subscription-provider-usage-v1",
            "provider": "zcode",
            "model": runner_mod.ZCODE_MODEL,
            "complete": False,
            "request_count": 0,
            "request_usage_complete": False,
            "model_error_count": 0,
        },
        runtime_diagnostic={
            "schema": "dradar-zcode-runtime-v1",
            "status": "idle",
            "turn_count": 12,
            "seen_running": True,
            "terminal_observed": True,
        },
        rc=0,
    )

    artifact = run_trial(_zcode_assignment_for_trial(), tmp_path, tmp_path)

    assert artifact.returncode == 0
    assert artifact.patch.is_file()


def _fake_completed_checkpoint_pier(
        monkeypatch, work_dir, assignment, *, patch_bytes=None,
        metadata_overrides=None, layout="new", generation=None):
    captured = {}

    def fake_build(_assignment, tasks_root, jobs_dir, job_name, home,
                   dev_agent=None):
        captured["job_name"] = job_name
        return ["pier"]

    class FakePopen:
        def __init__(self, cmd, **kw):
            trial = work_dir / "jobs" / captured["job_name"] / "task__t0"
            checkpoint = (
                trial / "checkpoint"
                if layout == "new"
                else trial / "agent" / "checkpoint"
            )
            checkpoint.mkdir(parents=True)
            (trial / "artifacts").mkdir()
            metadata = {
                "phase": "agent_completed",
                "assignment_id": assignment["assignment_id"],
                "task_id": assignment["task_id"],
                "model": assignment["model"],
                "effort": assignment["effort"],
                "workspace_patch": "workspace.patch",
            }
            metadata.update(metadata_overrides or {})
            (checkpoint / "checkpoint.json").write_text(json.dumps(metadata))
            payload = checkpoint
            if generation is not None:
                payload = checkpoint / "snapshots" / generation
                payload.mkdir(parents=True)
                (checkpoint / "current-generation").write_text(
                    generation + "\n", encoding="ascii",
                )
            if patch_bytes is not None:
                (payload / "workspace.patch").write_bytes(patch_bytes)
            if os.name != "nt" and layout == "new":
                trial.chmod(0o700)
                for current, directories, files in os.walk(
                    checkpoint, followlinks=False,
                ):
                    Path(current).chmod(0o700)
                    for name in directories:
                        candidate = Path(current) / name
                        if not candidate.is_symlink():
                            candidate.chmod(0o700)
                    for name in files:
                        candidate = Path(current) / name
                        if candidate.is_file() and not candidate.is_symlink():
                            candidate.chmod(0o600)
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(runner_mod, "build_pier_command", fake_build)
    monkeypatch.setattr(runner_mod.subprocess, "Popen", FakePopen)


def test_run_trial_recovers_patch_from_matching_completed_checkpoint(
        tmp_path, monkeypatch, capsys):
    assignment = _assignment("codex")
    patch = b"diff --git a/model_answer.json b/model_answer.json\n"
    _fake_completed_checkpoint_pier(
        monkeypatch, tmp_path, assignment, patch_bytes=patch,
    )

    artifacts = run_trial(assignment, tmp_path, tmp_path)

    assert artifacts.patch.read_bytes() == patch
    assert "recovered model.patch" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership boundary")
def test_completed_checkpoint_recovery_rejects_world_writable_sibling(
        tmp_path, monkeypatch):
    assignment = _assignment("codex")
    patch = b"diff --git a/model_answer.json b/model_answer.json\n"
    _fake_completed_checkpoint_pier(
        monkeypatch, tmp_path, assignment, patch_bytes=patch,
    )
    checkpoint = tmp_path / "jobs" / "aa1" / "task__t0" / "checkpoint"
    original = runner_mod.subprocess.Popen

    class WritableCheckpointPopen:
        def __init__(self, cmd, **kwargs):
            self.inner = original(cmd, **kwargs)
            checkpoint.chmod(0o777)
            self.returncode = self.inner.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(
        runner_mod.subprocess, "Popen", WritableCheckpointPopen,
    )
    with pytest.raises(RunnerError, match="checkpoint tree is not host-private"):
        run_trial(assignment, tmp_path, tmp_path)


def test_run_trial_recovers_generation_patch_from_new_checkpoint_layout(
        tmp_path, monkeypatch, capsys):
    assignment = _assignment("codex")
    patch = b"diff --git a/model_answer.json b/model_answer.json\n+new\n"
    _fake_completed_checkpoint_pier(
        monkeypatch, tmp_path, assignment, patch_bytes=patch,
        layout="new", generation="1" * 32,
    )

    artifacts = run_trial(assignment, tmp_path, tmp_path)

    assert artifacts.patch.read_bytes() == patch
    assert "recovered model.patch" in capsys.readouterr().out


def test_run_trial_recovers_generation_patch_from_legacy_checkpoint_layout(
        tmp_path, monkeypatch):
    assignment = _assignment("codex")
    patch = b"diff --git a/model_answer.json b/model_answer.json\n+legacy\n"
    _fake_completed_checkpoint_pier(
        monkeypatch, tmp_path, assignment, patch_bytes=patch,
        layout="legacy", generation="2" * 32,
    )

    with pytest.raises(
        RunnerError, match="legacy completed checkpoint is not host-private",
    ):
        run_trial(assignment, tmp_path, tmp_path)


def test_completed_checkpoint_recovery_does_not_fall_back_from_new_layout(
        tmp_path):
    assignment = _assignment("codex")
    trial = tmp_path / "task__t0"
    legacy = trial / "agent" / "checkpoint"
    legacy.mkdir(parents=True)
    metadata = {
        "phase": "agent_completed",
        "assignment_id": assignment["assignment_id"],
        "task_id": assignment["task_id"],
        "model": assignment["model"],
        "effort": assignment["effort"],
        "workspace_patch": "workspace.patch",
    }
    (legacy / "checkpoint.json").write_text(json.dumps(metadata))
    (legacy / "workspace.patch").write_bytes(b"diff --git a/x b/x\n")
    current = trial / "checkpoint"
    current.mkdir()
    (current / "snapshot.lock").mkdir()
    if os.name != "nt":
        trial.chmod(0o700)
        current.chmod(0o700)
        (current / "snapshot.lock").chmod(0o700)

    recovered, error = runner_mod._recover_completed_checkpoint_patch(
        trial, assignment,
    )

    assert not recovered
    assert error == "completed checkpoint snapshot is incomplete"
    assert not (trial / "artifacts" / "model.patch").exists()


@pytest.mark.parametrize("location", ["root", "payload"])
def test_completed_checkpoint_recovery_rejects_invalid_secret_race(
    tmp_path, monkeypatch, location,
):
    assignment = _assignment("codex")
    trial = tmp_path / "task__t0"
    checkpoint = trial / "checkpoint"
    generation = "c" * 32
    payload = checkpoint / "snapshots" / generation
    payload.mkdir(parents=True)
    metadata = {
        "phase": "agent_completed",
        "assignment_id": assignment["assignment_id"],
        "task_id": assignment["task_id"],
        "model": assignment["model"],
        "effort": assignment["effort"],
        "workspace_patch": "workspace.patch",
    }
    (checkpoint / "checkpoint.json").write_text(json.dumps(metadata))
    (checkpoint / "current-generation").write_text(
        generation + "\n", encoding="ascii",
    )
    patch = payload / "workspace.patch"
    patch.write_bytes(b"diff --git a/x b/x\n")
    if os.name != "nt":
        trial.chmod(0o700)
        checkpoint.chmod(0o700)
        (checkpoint / "snapshots").chmod(0o700)
        payload.chmod(0o700)
        for candidate in (
            checkpoint / "checkpoint.json",
            checkpoint / "current-generation",
            patch,
        ):
            candidate.chmod(0o600)
    real_read = runner_mod.checkpoints._read_regular_file

    def add_marker_after_patch_read(path, *, max_bytes):
        data = real_read(path, max_bytes=max_bytes)
        if Path(path) == patch:
            marker_root = checkpoint if location == "root" else payload
            (marker_root / "invalid-secret").write_text(
                "rejected\n", encoding="utf-8",
            )
        return data

    monkeypatch.setattr(
        runner_mod.checkpoints, "_read_regular_file", add_marker_after_patch_read,
    )

    recovered, error = runner_mod._recover_completed_checkpoint_patch(
        trial, assignment,
    )

    assert not recovered
    assert error == "completed checkpoint changed while it was read"
    assert not (trial / "artifacts" / "model.patch").exists()


def test_run_trial_does_not_recover_mismatched_completed_checkpoint(
        tmp_path, monkeypatch):
    assignment = _assignment("codex")
    _fake_completed_checkpoint_pier(
        monkeypatch, tmp_path, assignment,
        patch_bytes=b"diff --git a/x b/x\n",
        metadata_overrides={"assignment_id": "another-lease"},
    )

    with pytest.raises(RunnerError, match="identity mismatch: assignment_id"):
        run_trial(assignment, tmp_path, tmp_path)


def test_run_trial_reports_completed_checkpoint_collection_failure(
        tmp_path, monkeypatch):
    assignment = _assignment("codex")
    _fake_completed_checkpoint_pier(
        monkeypatch, tmp_path, assignment, patch_bytes=None,
    )

    with pytest.raises(RunnerError) as exc:
        run_trial(assignment, tmp_path, tmp_path)
    message = str(exc.value)
    assert "agent completed" in message
    assert "missing its workspace patch" in message
    assert "agent likely failed" not in message


def test_completed_checkpoint_recovery_rejects_workspace_patch_symlink(
        tmp_path, monkeypatch):
    assignment = _assignment("codex")
    _fake_completed_checkpoint_pier(
        monkeypatch, tmp_path, assignment, patch_bytes=None,
    )
    checkpoint = tmp_path / "jobs" / "aa1" / "task__t0" / "checkpoint"
    # Create the symlink during Popen construction by wrapping the fake.
    original = runner_mod.subprocess.Popen

    class SymlinkPopen:
        def __init__(self, cmd, **kwargs):
            self.inner = original(cmd, **kwargs)
            target = tmp_path / "outside.patch"
            target.write_bytes(b"diff --git a/x b/x\n")
            (checkpoint / "workspace.patch").symlink_to(target)
            self.returncode = self.inner.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(runner_mod.subprocess, "Popen", SymlinkPopen)
    with pytest.raises(RunnerError, match="checkpoint contains a special file"):
        run_trial(assignment, tmp_path, tmp_path)


def test_run_trial_classifies_build_failure_from_nested_result(tmp_path, monkeypatch):
    # Pier's console tail can contain only a generic teardown; the actual
    # Docker failure from the production case is preserved in result.json.
    _fake_pier(
        monkeypatch, tmp_path, patch=False,
        result={"exception_info": {
            "exception_type": "RuntimeError",
            "exception_message": "RUN apt-get update: failed to solve: exit code 100",
        }},
    )
    with pytest.raises(runner_mod.BuildFlakeError) as exc:
        run_trial(_assignment("codex"), tmp_path, tmp_path)
    assert "agent never started" in str(exc.value)
    assert "failed to solve" in str(exc.value)


def test_run_trial_missing_patch_message_includes_log_tail(tmp_path, monkeypatch):
    captured = {}
    def fake_build(assignment, tasks_root, jobs_dir, job_name, home, dev_agent=None):
        captured["job_name"] = job_name
        return ["pier"]
    class FakePopen:
        def __init__(self, cmd, **kw):
            trial = tmp_path / "jobs" / captured["job_name"] / "task__t0"
            (trial / "artifacts").mkdir(parents=True)   # no model.patch inside
            kw["stdout"].write("agent auth rejected (401)\n")
            kw["stdout"].flush()
            self.returncode = 1

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass
    monkeypatch.setattr(runner_mod, "build_pier_command", fake_build)
    monkeypatch.setattr(runner_mod.subprocess, "Popen", FakePopen)
    with pytest.raises(RunnerError) as exc:
        run_trial(_assignment("codex"), tmp_path, tmp_path)
    assert "model.patch missing" in str(exc.value)
    assert "agent auth rejected (401)" in str(exc.value)


def test_tail_keeps_only_the_last_n_lines(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("".join(f"line{i}\n" for i in range(30)))
    tail = runner_mod._tail(p, n=15)
    got = tail.splitlines()
    assert got[0] == "line15" and got[-1] == "line29" and len(got) == 15


def test_tail_of_a_missing_log_is_empty(tmp_path):
    assert runner_mod._tail(tmp_path / "nope.log") == ""


def test_run_trial_missing_trajectory_and_result_are_none(tmp_path, monkeypatch):
    _fake_pier(monkeypatch, tmp_path, trajectory=False, result=None)
    art = run_trial(_assignment("codex"), tmp_path, tmp_path)
    assert art.trajectory is None and art.result is None


def test_run_trial_stale_job_dir_gets_suffixed_name(tmp_path, monkeypatch):
    # leftover dir from an earlier run of the same lease must not collide
    (tmp_path / "jobs" / "aa1").mkdir(parents=True)
    captured = _fake_pier(monkeypatch, tmp_path)
    art = run_trial(_assignment("codex"), tmp_path, tmp_path)
    assert captured["job_name"].startswith("aa1-")
    assert art.trial_dir.is_dir()


def test_trial_timeout_defaults_and_floor():
    # missing/None estimate falls back to 30 min -> 30*60*4
    assert _trial_timeout_sec({}) == 7200
    assert _trial_timeout_sec({"est_minutes": None}) == 7200
    assert _trial_timeout_sec({"est_minutes": 5}) == 3600   # floor wins
    assert _trial_timeout_sec({"est_minutes": 10}) == 3600  # floor wins


def test_trial_timeout_scales_with_estimate():
    assert _trial_timeout_sec({"est_minutes": 20}) == 4800
    assert _trial_timeout_sec({"est_minutes": 30}) == 7200
    assert _trial_timeout_sec({"est_minutes": 120}) == 28800


def test_beta_subscription_harnesses_have_two_hour_floor():
    for agent in (
        runner_mod.KIMI_AGENT,
        runner_mod.GROK_AGENT,
        runner_mod.ZCODE_AGENT,
    ):
        assert _effective_trial_timeout_sec({
            "agent": agent,
            "est_minutes": 5,
        }) == 7200
        assert _effective_trial_timeout_sec({
            "agent": agent,
            "est_minutes": 40,
        }) == 9600
    assert _effective_trial_timeout_sec({
        "agent": "codex",
        "est_minutes": 5,
    }) == 3600


def test_pompeii_outer_watchdog_leaves_setup_outside_agent_budget():
    expected = (
        runner_mod.POMPEII_AGENT_TIMEOUT_SEC
        + runner_mod.POMPEII_OUTER_WATCHDOG_SLACK_SEC
    )
    for agent in ("codex", runner_mod.GROK_AGENT, runner_mod.ZCODE_AGENT):
        assert _effective_trial_timeout_sec({
            "agent": agent,
            "benchmark_id": runner_mod.POMPEII_BENCHMARK_ID,
            "est_minutes": 5,
        }) == expected

    kimi_expected = (
        runner_mod.KIMI_POMPEII_AGENT_TIMEOUT_SEC
        + runner_mod.POMPEII_OUTER_WATCHDOG_SLACK_SEC
    )
    assert _effective_trial_timeout_sec({
        "agent": runner_mod.KIMI_AGENT,
        "benchmark_id": runner_mod.POMPEII_BENCHMARK_ID,
        "est_minutes": 5,
    }) == kimi_expected


def test_kimi_non_pompeii_keeps_subscription_timeout_policy():
    assert _effective_trial_timeout_sec({
        "agent": runner_mod.KIMI_AGENT,
        "est_minutes": 5,
    }) == 7200


def test_summarize_result_exception_info_present(tmp_path):
    p = tmp_path / "result.json"
    p.write_text(json.dumps({"agent_result": {"cost_usd": 1.23, "n_input_tokens": 5},
                             "exception_info": {"type": "RateLimit"}}))
    s = summarize_result(p)
    assert s["exception_info"] is True and s["n_input_tokens"] == 5
    assert s["cost_usd"] == 1.23


def test_summarize_result_exception_info_absent(tmp_path):
    p = tmp_path / "result.json"
    p.write_text(json.dumps({"agent_result": {"n_output_tokens": 7}}))
    s = summarize_result(p)
    assert s["exception_info"] is False and s["n_output_tokens"] == 7


def test_summarize_result_corrupt_json(tmp_path):
    p = tmp_path / "result.json"
    p.write_text("{not json")
    assert summarize_result(p) == {}


class _NpmResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_resolve_latest_codex_cli_version_uses_uncached_stable_tag(monkeypatch):
    seen = {}

    def get(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _NpmResponse({"version": "0.145.0"})

    monkeypatch.setattr(runner_mod.httpx, "get", get)

    assert runner_mod.resolve_latest_codex_cli_version() == "0.145.0"
    assert seen["url"] == runner_mod.CODEX_NPM_LATEST_URL
    assert seen["headers"]["Cache-Control"] == "no-cache"
    assert seen["follow_redirects"] is True


@pytest.mark.parametrize("version", ["latest", "0.146.0-alpha.1", "", None])
def test_resolve_latest_codex_cli_version_rejects_non_stable_values(
        monkeypatch, version):
    calls = []
    monkeypatch.setattr(
        runner_mod.httpx,
        "get",
        lambda *a, **k: calls.append(True) or _NpmResponse({"version": version}),
    )
    monkeypatch.setattr(runner_mod.time, "sleep", lambda _: None)

    with pytest.raises(RunnerError, match="refusing to start"):
        runner_mod.resolve_latest_codex_cli_version()
    assert len(calls) == runner_mod.CODEX_VERSION_LOOKUP_ATTEMPTS


def test_resolve_latest_codex_cli_version_fails_closed_after_network_errors(
        monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise runner_mod.httpx.ConnectError("registry unavailable")

    monkeypatch.setattr(runner_mod.httpx, "get", fail)
    monkeypatch.setattr(runner_mod.time, "sleep", lambda _: None)

    with pytest.raises(RunnerError, match="no model quota is consumed"):
        runner_mod.resolve_latest_codex_cli_version()
    assert len(calls) == runner_mod.CODEX_VERSION_LOOKUP_ATTEMPTS


def test_resolve_latest_codex_cli_version_accepts_fresh_server_fallback(
        monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        assert kwargs["timeout"] == (
            runner_mod.CODEX_VERSION_FALLBACK_LOOKUP_TIMEOUT_SEC
        )
        raise runner_mod.httpx.ConnectError("local proxy unavailable")

    monkeypatch.setattr(runner_mod.httpx, "get", fail)

    assert runner_mod.resolve_latest_codex_cli_version(
        "0.145.0", server_version_verified=True,
    ) == "0.145.0"
    assert calls == [True]


def test_run_trial_overrides_stale_server_pin_before_start(
        tmp_path, monkeypatch):
    captured = {}

    def resolve(server_version, server_version_verified):
        captured["server_version"] = server_version
        captured["server_version_verified"] = server_version_verified
        return "0.145.0"

    monkeypatch.setattr(runner_mod, "resolve_latest_codex_cli_version", resolve)

    def fake_build(assignment, tasks_root, jobs_dir, job_name, home, dev_agent=None):
        captured["version"] = assignment["agent_version"]
        captured["job_name"] = job_name
        return ["pier", "run", job_name]

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            trial = tmp_path / "jobs" / captured["job_name"] / "task__t0"
            (trial / "artifacts").mkdir(parents=True)
            (trial / "artifacts" / "model.patch").write_text("diff")
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(runner_mod, "build_pier_command", fake_build)
    monkeypatch.setattr(runner_mod.subprocess, "Popen", FakePopen)
    assignment = _assignment("codex") | {
        "agent_version": "0.144.1",
        "agent_version_verified": True,
    }

    art = run_trial(assignment, tmp_path, tmp_path)

    assert captured["version"] == "0.145.0"
    assert captured["server_version"] == "0.144.1"
    assert captured["server_version_verified"] is True
    assert art.codex_cli_version == "0.145.0"


def test_run_trial_registry_failure_starts_nothing(tmp_path, monkeypatch):
    started = []
    marked_started = []
    monkeypatch.setattr(
        runner_mod,
        "resolve_latest_codex_cli_version",
        lambda *a, **k: (_ for _ in ()).throw(
            RunnerError("registry unavailable")
        ),
    )
    monkeypatch.setattr(
        runner_mod.subprocess,
        "Popen",
        lambda *a, **k: started.append(True),
    )

    with pytest.raises(RunnerError, match="registry unavailable"):
        run_trial(
            _assignment("codex"),
            tmp_path,
            tmp_path,
            on_started=lambda: marked_started.append(True),
        )

    assert started == []
    assert marked_started == []
    assert not (tmp_path / "jobs").exists()


def test_run_trial_egress_failure_starts_nothing(tmp_path, monkeypatch):
    started = []
    marked_started = []
    monkeypatch.setattr(
        runner_mod.egress,
        "prepare_egress_proxy_runtime",
        lambda **_kwargs: (_ for _ in ()).throw(
            runner_mod.egress.EgressProxyError("container route unavailable")
        ),
    )
    monkeypatch.setattr(
        runner_mod.subprocess,
        "Popen",
        lambda *a, **k: started.append(True),
    )

    with pytest.raises(RunnerError, match="no model quota was used"):
        run_trial(
            _assignment("claude-code"),
            tmp_path,
            tmp_path,
            on_started=lambda: marked_started.append(True),
        )

    assert started == []
    assert marked_started == []
    assert not (tmp_path / "jobs").exists()


def test_no_network_task_requires_enforceable_environment_switch(
        tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = tmp_path / "abs-module-cache-flags"
    task.mkdir()
    (task / "task.toml").write_text(
        '[agent]\nnetwork_mode = "no-network"\n[environment]\n'
    )
    with pytest.raises(RunnerError, match="allow_internet=false"):
        build_pier_command(
            _assignment("codex"), tmp_path, tmp_path / "jobs", "j",
            tmp_path / "home")


def test_dsh_artifact_binding_accepts_exact_current_run(tmp_path):
    trial = tmp_path / "task__trial"
    sidecar = trial / "agent" / "dsh-home" / "dsh-outcome.json"
    sidecar.parent.mkdir(parents=True)
    assignment = {
        "assignment_id": "a" * 32,
        "_artifact_run_id": "b" * 32,
        "task_id": "httpx-streaming-json-iteration",
        "model": "dsh-deepseek-v4-pro",
        "effort": "off",
    }
    sidecar.write_text(json.dumps({
        "schema": "dradar-dsh-outcome-v1",
        "assignmentId": assignment["assignment_id"],
        "artifactRunId": assignment["_artifact_run_id"],
        "taskId": assignment["task_id"],
        "assignmentModel": assignment["model"],
        "reasoningEffort": assignment["effort"],
    }))

    _verify_dsh_artifact_binding(trial, assignment)


def test_dsh_artifact_binding_rejects_previous_assignment_bytes(tmp_path):
    trial = tmp_path / "task__trial"
    sidecar = trial / "agent" / "dsh-home" / "dsh-outcome.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps({
        "schema": "dradar-dsh-outcome-v1",
        "assignmentId": "a" * 32,
        "artifactRunId": "b" * 32,
        "taskId": "httpx-streaming-json-iteration",
        "assignmentModel": "dsh-deepseek-v4-pro",
        "reasoningEffort": "off",
    }))
    current = {
        "assignment_id": "c" * 32,
        "_artifact_run_id": "d" * 32,
        "task_id": "httpx-streaming-json-iteration",
        "model": "dsh-deepseek-v4-pro",
        "effort": "off",
    }

    with pytest.raises(RunnerError, match="does not match"):
        _verify_dsh_artifact_binding(trial, current)


def test_dsh_vision_artifact_binding_requires_attached_question_image(tmp_path):
    trial = tmp_path / "task__trial"
    sidecar = trial / "agent" / "dsh-home" / "dsh-outcome.json"
    sidecar.parent.mkdir(parents=True)
    assignment = {
        "assignment_id": "a" * 32,
        "_artifact_run_id": "b" * 32,
        "task_id": "pompeii-adjacency-rp-055",
        "model": "dsh-deepseek-v4-flash-vision-exp",
        "effort": "high",
    }
    payload = {
        "schema": "dradar-dsh-outcome-v1",
        "assignmentId": assignment["assignment_id"],
        "artifactRunId": assignment["_artifact_run_id"],
        "taskId": assignment["task_id"],
        "assignmentModel": assignment["model"],
        "reasoningEffort": assignment["effort"],
        "visionInputAttached": True,
        "visionInput": {
            "attachmentId": "sha256:" + "c" * 64,
            "mediaType": "image/png",
            "bytes": 207309,
            "width": 1074,
            "height": 904,
            "name": "question.png",
        },
    }
    sidecar.write_text(json.dumps(payload))

    binding = _verify_dsh_artifact_binding(trial, assignment)
    assert binding["visionInputAttached"] is True
    assert binding["visionInput"] == payload["visionInput"]

    payload["visionInputAttached"] = False
    sidecar.write_text(json.dumps(payload))
    with pytest.raises(RunnerError, match="did not attest"):
        _verify_dsh_artifact_binding(trial, assignment)


def test_dsh_utf16_patch_is_normalized_only_after_git_validation(tmp_path):
    patch = tmp_path / "model.patch"
    diff = (
        "diff --git a/answer.txt b/answer.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/answer.txt\n"
        "@@ -0,0 +1 @@\n"
        "+done\n"
    )
    patch.write_bytes(diff.encode("utf-16"))

    assert _normalize_utf16_patch(patch) is True
    assert patch.read_bytes() == diff.encode("utf-8")
    assert _normalize_utf16_patch(patch) is False
