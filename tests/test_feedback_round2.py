"""Second volunteer-feedback round (2026-07-13): git identity inside the task
container, heartbeat log parsing, and free-pick batches picking up cells
claimed on the web while an earlier batch was still running."""
from pathlib import Path

import dradar.runloop as runloop
import dradar.runner as runner_mod
from dradar.runner import _last_activity, build_pier_command

from test_go_menu import FakeClient, _args, _patch_run


def test_pier_command_injects_git_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda _: "/usr/bin/pier")
    (tmp_path / "t").mkdir()
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(tmp_path / "auth.json"))
    (tmp_path / "auth.json").write_text("{}")
    home = tmp_path / "home"
    home.mkdir()
    a = {"assignment_id": "a1", "task_id": "t", "agent": "codex",
         "model": "gpt-5.6-sol", "effort": "low",
         "agent_version": "0.145.0"}
    cmd = build_pier_command(a, tmp_path, tmp_path / "jobs", "j", home)
    for var in ("GIT_AUTHOR_NAME=dradar-trial", "GIT_COMMITTER_NAME=dradar-trial",
                "GIT_AUTHOR_EMAIL=trial@dradar.invalid",
                "GIT_COMMITTER_EMAIL=trial@dradar.invalid"):
        assert var in cmd
        assert cmd[cmd.index(var) - 1] == "--ae"


def test_last_activity_unwraps_progress_bar_redraws(tmp_path: Path):
    log = tmp_path / "pier.log"
    log.write_text("cmd=pier run\n1/1 Mean: 0.000 ━━ 0:00:30\r1/1 Mean: 0.000 ━━ 0:07:10\n")
    assert "0:07:10" in _last_activity(log)


def test_last_activity_survives_empty_log(tmp_path: Path):
    log = tmp_path / "pier.log"
    log.write_text("")
    assert "still running" in _last_activity(log)


def _cell(aid):
    return {"assignment_id": aid, "task_id": f"task-{aid}", "agent": "codex",
            "model": "gpt-5.6-sol", "effort": "low", "nonce": "n",
            "expires_at": "2099-01-01T00:00:00+00:00", "est_minutes": 2,
            "est_quota_pct": 0.5}


def test_free_pick_continues_with_cells_claimed_mid_run(monkeypatch, capsys, tmp_path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = FakeClient([
        {"active": [_cell("a1")], "free_pick": True},                 # startup snapshot
        {"active": [_cell("a2"), _cell("a3")], "free_pick": True},    # claimed mid-run
        {"active": [], "free_pick": True},                            # drained
    ])
    rc = runloop._go_menu(_args(), {}, client, tmp_path)
    assert rc == 0
    assert ran == ["a1", "a2", "a3"]
    assert "claimed while that batch ran" in capsys.readouterr().out


def test_free_pick_still_held_cells_do_not_loop(monkeypatch, tmp_path):
    # A cell the volunteer deliberately skipped stays leased and keeps coming
    # back from GET /assignment — it must not re-prompt in an endless loop.
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = FakeClient({"active": [_cell("a1")], "free_pick": True})  # repeats forever
    rc = runloop._go_menu(_args(), {}, client, tmp_path)
    assert rc == 0
    assert ran == ["a1"]  # ran exactly once


def test_menu_mode_keeps_single_run_contract(monkeypatch, tmp_path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = FakeClient([
        {"active": [_cell("a1")], "free_pick": False, "menu": None},
        {"active": [_cell("a2")], "free_pick": False, "menu": None},
    ])
    rc = runloop._go_menu(_args(), {}, client, tmp_path)
    assert rc == 0
    assert ran == ["a1"]  # no auto-continuation on menu-mode instances


# --- quota-share denomination (owner report 2026-07-14) ----------------------

WINDOWS = {"plus": 91.37, "pro-5x": 456.86, "pro-20x": 1827.42}


def test_quota_share_converts_per_tier():
    line = runloop._quota_share_line(
        {"est_quota_pct": 0.5, "tier_windows_usd": WINDOWS})
    # 0.5% of Plus = 0.10% of 5x = 0.025% of 20x (adaptive precision, same
    # as the radar page's tags; 0.025 renders 0.02 under binary float)
    assert "Plus ~0.50%" in line
    assert "5x Pro ~0.10%" in line
    assert "20x Pro ~0.02%" in line


def test_quota_share_without_windows_labels_denomination():
    line = runloop._quota_share_line({"est_quota_pct": 0.5})
    assert "Plus" in line and "0.5" in line  # old server: say what 0.5% means


def test_quota_share_tiny_pct_never_shows_zero():
    line = runloop._quota_share_line(
        {"est_quota_pct": 0.05, "tier_windows_usd": WINDOWS})
    assert "20x Pro ~<0.01%" in line
    assert "0.00%" not in line


# --- build-flake classification + free auto-retry (volunteer report #3) ------

import pytest

from dradar.runner import (
    BuildDiskFullError, BuildFlakeError, BuildSnapshotterPermissionError,
    RunnerError, run_trial,
)


@pytest.fixture(autouse=True)
def _stub_latest_codex_version(monkeypatch):
    """Unit tests must never depend on the live npm registry."""
    monkeypatch.setattr(
        runner_mod, "resolve_latest_codex_cli_version", lambda *a, **k: "0.145.0",
    )


def _flaky_pier(monkeypatch, work_dir, fail_times, log_line, make_patch=True):
    """Fake Popen that fails the build `fail_times` times (no trial dir, flake
    marker in the log), then succeeds."""
    captured = {"job_names": [], "calls": 0}

    def fake_build(assignment, tasks_root, jobs_dir, job_name, home, dev_agent=None):
        captured["job_names"].append(job_name)
        return ["pier", "run", job_name]

    class FakePopen:
        def __init__(self, cmd, **kw):
            captured["calls"] += 1
            kw["stdout"].write(log_line + "\n")
            kw["stdout"].flush()
            if captured["calls"] > fail_times:
                trial = work_dir / "jobs" / captured["job_names"][-1] / "task__t0"
                (trial / "artifacts").mkdir(parents=True)
                (trial / "agent").mkdir()
                if make_patch:
                    (trial / "artifacts" / "model.patch").write_text("diff")
            self.returncode = 0 if captured["calls"] > fail_times else 1

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(runner_mod, "build_pier_command", fake_build)
    monkeypatch.setattr(
        runner_mod.image_cache,
        "prepare_trial_builder",
        lambda *_a, **_k: runner_mod.image_cache.TrialBuilderLease(
            "dradar-task-test", True,
        ),
    )
    monkeypatch.setattr(runner_mod.subprocess, "Popen", FakePopen)
    return captured


def test_build_flake_raises_typed_error_naming_the_real_cause(tmp_path, monkeypatch):
    _flaky_pier(monkeypatch, tmp_path, fail_times=99,
                log_line="E: Failed to fetch http://ports.ubuntu.com/... 503")
    with pytest.raises(BuildFlakeError) as exc:
        run_trial({"assignment_id": "a1", "task_id": "t", "agent": "codex",
                   "model": "m", "effort": "low", "est_minutes": 2}, tmp_path, tmp_path)
    assert "no quota was used" in str(exc.value)
    assert "ports.ubuntu.com" in str(exc.value)


def test_non_flake_missing_patch_stays_plain_runner_error(tmp_path, monkeypatch):
    _flaky_pier(monkeypatch, tmp_path, fail_times=0, make_patch=False,
                log_line="agent crashed for mysterious reasons")
    with pytest.raises(RunnerError) as exc:
        run_trial({"assignment_id": "a1", "task_id": "t", "agent": "codex",
                   "model": "m", "effort": "low", "est_minutes": 2}, tmp_path, tmp_path)
    assert not isinstance(exc.value, BuildFlakeError)
    assert "model.patch missing" in str(exc.value)


def test_run_and_submit_retries_build_flake_once(monkeypatch, capsys, tmp_path):
    from test_go_menu import ASSIGNMENT, SubmitClient, _fake_art
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    calls = {"n": 0}

    def flaky_run(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BuildFlakeError("mirror flake")
        return _fake_art(tmp_path, rc=0)

    monkeypatch.setattr(runloop, "run_trial", flaky_run)
    client = SubmitClient({})
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc")
    assert tag == "submitted" and calls["n"] == 2
    assert "retrying once automatically" in capsys.readouterr().out


def test_run_and_submit_gives_up_after_second_flake(monkeypatch, capsys, tmp_path):
    from test_go_menu import ASSIGNMENT, SubmitClient
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")

    def always_flaky(*a, **kw):
        raise BuildFlakeError("mirror flake")

    monkeypatch.setattr(runloop, "run_trial", always_flaky)
    client = SubmitClient({})
    stopped = []
    client.mark_stopped = lambda aid, **kw: stopped.append((aid, kw))
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc")
    assert tag == "environment-build-failed"
    assert stopped == [(
        ASSIGNMENT["assignment_id"],
        {"defer_seconds": 300, "failure_kind": "environment_build_failed"},
    )]
    assert "failed twice" in capsys.readouterr().out


def test_disk_full_build_is_not_a_mirror_flake(tmp_path, monkeypatch):
    _flaky_pier(
        monkeypatch, tmp_path, fail_times=99,
        log_line="ERROR: failed to solve: no space left on device",
    )
    with pytest.raises(BuildDiskFullError) as exc:
        run_trial({"assignment_id": "a1", "task_id": "t", "agent": "codex",
                   "model": "m", "effort": "low", "est_minutes": 2}, tmp_path, tmp_path)
    assert "disk is full" in str(exc.value)


def test_copy_file_range_without_enospc_stays_a_build_flake(tmp_path, monkeypatch):
    _flaky_pier(
        monkeypatch, tmp_path, fail_times=99,
        log_line="ERROR: failed to solve: copy_file_range: input/output error",
    )
    with pytest.raises(BuildFlakeError):
        run_trial({"assignment_id": "a1", "task_id": "t", "agent": "codex",
                   "model": "m", "effort": "low", "est_minutes": 2}, tmp_path, tmp_path)


def test_overlay_whiteout_permission_is_not_a_mirror_flake(tmp_path, monkeypatch):
    _flaky_pier(
        monkeypatch, tmp_path, fail_times=99,
        log_line=(
            "ERROR: failed to solve: fuse-overlayfs: converting whiteout: "
            "operation not permitted"
        ),
    )
    with pytest.raises(BuildSnapshotterPermissionError) as exc:
        run_trial({"assignment_id": "a1", "task_id": "t", "agent": "codex",
                   "model": "m", "effort": "low", "est_minutes": 2}, tmp_path, tmp_path)
    assert "overlay whiteouts" in str(exc.value)


def test_run_and_submit_retries_overlay_permission_on_default_builder(
    monkeypatch, capsys, tmp_path,
):
    from test_go_menu import ASSIGNMENT, SubmitClient, _fake_art
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    calls = {"n": 0}

    def flaky_run(*_a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            assert kw.get("allow_isolated_builder") is True
            raise BuildSnapshotterPermissionError("whiteout eperm")
        assert kw.get("allow_isolated_builder") is False
        return _fake_art(tmp_path, rc=0)

    monkeypatch.setattr(runloop, "run_trial", flaky_run)
    client = SubmitClient({})
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc")
    assert tag == "submitted" and calls["n"] == 2
    assert "host default builder" in capsys.readouterr().out


def test_run_and_submit_does_not_retry_disk_full(monkeypatch, capsys, tmp_path):
    from test_go_menu import ASSIGNMENT, SubmitClient
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    calls = {"n": 0}

    def always_full(*_a, **_kw):
        calls["n"] += 1
        raise BuildDiskFullError("no space left")

    monkeypatch.setattr(runloop, "run_trial", always_full)
    client = SubmitClient({})
    stopped = []
    client.mark_stopped = lambda aid, **kw: stopped.append((aid, kw))
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc")
    assert tag == "environment-build-failed"
    assert calls["n"] == 1
    assert stopped == [(
        ASSIGNMENT["assignment_id"],
        {"defer_seconds": 300, "failure_kind": "environment_build_failed"},
    )]
    assert "disk space" in capsys.readouterr().out
