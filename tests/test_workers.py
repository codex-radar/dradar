"""One-command worker pool: selection happens once; children only resume."""

import argparse
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dradar import cli, runloop


@pytest.fixture(autouse=True)
def _enable_checkpoint_runtime_for_worker_mechanics(monkeypatch):
    """Keep legacy recovery mechanics covered behind the disabled rollout."""

    monkeypatch.setattr(
        runloop, "durable_checkpoint_rollout_enabled", lambda: True,
    )


def _args(**overrides):
    values = dict(
        workers=3, yes=True, keep=False, allow_task_drift=False,
        dev_agent=None, refill=False, refill_to=None, max_tasks=None,
        max_estimated_quota_pct=None, quota_tier="plus", auto=5, pick=None,
        assignment=None, parallel=False, worker_child=False, resume=False,
        worker_target_file=None, archive_session=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_cli_parses_workers_for_go(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_go", lambda args: seen.append(args) or 0)
    assert cli.main(["go", "--auto", "5", "--workers", "3", "-y"]) == 0
    assert seen[0].workers == 3
    assert seen[0].auto == 5


def test_cli_accepts_auto_workers(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_go", lambda args: seen.append(args) or 0)
    assert cli.main(["resume", "--workers", "auto", "-y"]) == 0
    assert seen[0].workers == "auto"


def test_cli_parses_archive_session_as_explicit_opt_in(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_go", lambda args: seen.append(args) or 0)
    assert cli.main(["go", "--archive-session", "-y"]) == 0
    assert seen[0].archive_session is True


@pytest.mark.parametrize("workers", [0, 41])
def test_worker_count_is_bounded_before_any_setup(workers):
    with pytest.raises(SystemExit, match="1 <= N <= 40"):
        runloop.cmd_go(_args(workers=workers))


def test_worker_count_accepts_40(monkeypatch):
    seen = []
    monkeypatch.setattr(runloop, "_run_worker_pool", lambda args: seen.append(args.workers) or 0)
    assert runloop.cmd_go(_args(workers=40)) == 0
    assert seen == [40]


def test_dynamic_target_requires_a_fixed_multi_worker_pool():
    with pytest.raises(SystemExit, match="fixed --workers N greater than 1"):
        runloop.cmd_go(_args(workers=1, worker_target_file="target"))
    with pytest.raises(SystemExit, match="fixed --workers N greater than 1"):
        runloop.cmd_go(_args(workers="auto", worker_target_file="target"))


def test_internal_worker_child_accepts_parent_dynamic_target_env(
        tmp_path, monkeypatch):
    target = tmp_path / "workers"
    target.write_text("3")
    monkeypatch.setenv("DRADAR_POOL_TARGET_FILE", str(target))
    monkeypatch.setattr(runloop, "_load_config", lambda: pytest.fail(
        "validation passed; stop before runtime setup",
    ))

    with pytest.raises(pytest.fail.Exception, match="validation passed"):
        runloop.cmd_go(_args(
            workers=1, worker_child=True, parallel=True, resume=True,
        ))


def test_workers_cannot_mix_with_manual_parallel():
    with pytest.raises(SystemExit, match="already manages parallel"):
        runloop.cmd_go(_args(parallel=True))


def test_internal_worker_mode_cannot_be_used_as_a_normal_go():
    with pytest.raises(SystemExit, match="invalid internal worker"):
        runloop.cmd_go(_args(workers=1, worker_child=True))


def test_quota_only_refill_gets_an_internal_task_safety_cap(monkeypatch):
    seen = []
    monkeypatch.setattr(
        runloop, "_run_worker_pool",
        lambda args: seen.append(args.max_tasks) or 0,
    )
    args = _args(
        refill=True, max_tasks=None, max_estimated_quota_pct=12.5,
    )
    assert runloop.cmd_go(args) == 0
    assert seen == [runloop.DEFAULT_REFILL_TASK_SAFETY_CAP]


def test_manual_workers_raise_too_small_refill_queue(monkeypatch):
    seen = []
    monkeypatch.setattr(
        runloop, "_run_worker_pool",
        lambda args: seen.append((args.workers, args.refill_to)) or 0,
    )
    args = _args(
        workers=3, refill=True, refill_to=1, max_tasks=100,
        max_estimated_quota_pct=5,
    )

    assert runloop.cmd_go(args) == 0
    assert seen == [(3, 3)]


def test_dynamic_worker_target_controls_initial_refill_queue(
        tmp_path, monkeypatch):
    target = tmp_path / "workers"
    target.write_text("2")
    seen = []
    monkeypatch.setattr(
        runloop, "_run_worker_pool",
        lambda args: seen.append(args.refill_to) or 0,
    )
    args = _args(
        workers=4, worker_target_file=str(target), refill=True,
        refill_to=40, max_tasks=100, max_estimated_quota_pct=5,
    )

    assert runloop.cmd_go(args) == 0
    assert seen == [2]


def test_refill_pool_cannot_start_at_zero_workers(tmp_path):
    target = tmp_path / "workers"
    target.write_text("0")
    with pytest.raises(SystemExit, match="cannot start with worker target 0"):
        runloop.cmd_go(_args(
            workers=4, worker_target_file=str(target), refill=True,
            refill_to=4, max_tasks=100, max_estimated_quota_pct=5,
        ))


def test_refill_without_any_limit_is_rejected_before_setup():
    with pytest.raises(SystemExit, match="requires --max-estimated-quota-pct"):
        runloop.cmd_go(_args(refill=True))


def test_worker_command_never_forwards_auto_selection():
    command = runloop._worker_command(_args())
    assert command[3:6] == ["resume", "-y", "--parallel"]
    assert "--worker-child" in command
    assert "--auto" not in command
    assert "go" not in command


def test_worker_command_forwards_archive_opt_in_only():
    assert "--archive-session" not in runloop._worker_command(_args())
    assert "--archive-session" in runloop._worker_command(
        _args(archive_session=True))


class _Telemetry:
    session_id = "session-test"

    def __init__(self, _client, **_kwargs):
        self.closed = None

    def start(self):
        pass

    def set_phase(self, _phase):
        pass

    def close(self, reason):
        self.closed = reason


@pytest.mark.parametrize(
    ("worker_child", "expected_rc", "expected_checkout", "expected_retry"),
    ((True, 0, True, False), (False, 1, False, True)),
)
def test_only_supervised_worker_skips_busy_checkpoint_and_drains_waiting_work(
        monkeypatch, capsys, worker_child, expected_rc, expected_checkout,
        expected_retry):
    """One checkpoint owner must not leave another confirmed pool slot idle."""
    checked_out = []
    pending_retries = []
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: object())
    monkeypatch.setattr(runloop, "tasks_root_from_config", lambda _cfg: object())
    monkeypatch.setattr(runloop, "RunnerTelemetry", _Telemetry)
    monkeypatch.setattr(runloop, "ensure_tasks_root", lambda _root: None)
    monkeypatch.setattr(runloop, "ensure_pier", lambda: None)
    monkeypatch.setattr(
        runloop, "_retry_pending_uploads",
        lambda _client: pending_retries.append(True),
    )
    monkeypatch.setattr(
        runloop, "_resume_local_checkpoints",
        lambda *_a, **_k: ([], True),  # every checkpoint lock was busy
    )
    monkeypatch.setattr(
        runloop, "_go_menu",
        lambda *_a, **_k: checked_out.append(True) or 0,
    )
    args = _args(
        workers=1, auto=None, parallel=True, resume=True,
        worker_child=worker_child,
    )

    assert runloop.cmd_go(args) == expected_rc
    assert bool(checked_out) is expected_checkout
    assert bool(pending_retries) is expected_retry
    output = capsys.readouterr().out
    if worker_child:
        assert "checking for a different waiting task" in output


def test_recovery_repeat_failure_stops_before_waiting_checkout(monkeypatch):
    checked_out = []
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: object())
    monkeypatch.setattr(runloop, "tasks_root_from_config", lambda _cfg: object())
    monkeypatch.setattr(runloop, "RunnerTelemetry", _Telemetry)
    monkeypatch.setattr(runloop, "acquire_run_lock", lambda _home: None)
    monkeypatch.setattr(runloop, "sweep_orphan_compose", lambda *_a: None)
    monkeypatch.setattr(
        runloop, "_maintain_image_cache", lambda *_a, **_k: True,
    )
    monkeypatch.setattr(runloop, "ensure_tasks_root", lambda _root: None)
    monkeypatch.setattr(runloop, "ensure_pier", lambda: None)
    monkeypatch.setattr(runloop, "_ensure_egress_runtime", lambda **_k: None)
    monkeypatch.setattr(runloop, "_retry_pending_uploads", lambda _client: None)
    monkeypatch.setattr(
        runloop, "_resume_local_checkpoints",
        lambda *_a, **_k: (["repeat-agent-failure"], True),
    )
    monkeypatch.setattr(
        runloop, "_go_menu", lambda *_a, **_k: checked_out.append(True) or 0,
    )

    assert runloop.cmd_go(_args(workers=1, auto=None)) == 1
    assert checked_out == []


def test_checkpoint_recovery_stops_before_third_item_after_repeat_failure(
        monkeypatch, tmp_path):
    items = {
        name: SimpleNamespace(assignment_id=name)
        for name in ("first", "second", "must-not-resume")
    }
    monkeypatch.setattr(
        runloop.checkpoints, "latest_by_assignment", lambda _home: items,
    )
    monkeypatch.setattr(
        runloop, "_active_by_id", lambda _client: {name: {} for name in items},
    )
    attempted = []
    outcomes = iter(("interrupted", "repeat-agent-failure"))

    def resume(_client, item, *_args, **_kwargs):
        attempted.append(item.assignment_id)
        return next(outcomes)

    monkeypatch.setattr(runloop, "_resume_one_checkpoint", resume)
    args = _args(workers=1, auto=None)

    results, found = runloop._resume_local_checkpoints(
        object(), args, tmp_path, None,
    )

    assert found is True
    assert results == ["interrupted", "repeat-agent-failure"]
    assert attempted == ["first", "second"]


def test_egress_preflight_failure_happens_before_checkout(monkeypatch):
    checked_out = []
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: object())
    monkeypatch.setattr(runloop, "tasks_root_from_config", lambda _cfg: object())
    monkeypatch.setattr(runloop, "RunnerTelemetry", _Telemetry)
    monkeypatch.setattr(runloop, "acquire_run_lock", lambda _home: None)
    monkeypatch.setattr(runloop, "sweep_orphan_compose", lambda *_a: None)
    monkeypatch.setattr(
        runloop, "_maintain_image_cache", lambda *_a, **_k: True,
    )
    monkeypatch.setattr(runloop, "ensure_tasks_root", lambda _root: None)
    monkeypatch.setattr(runloop, "ensure_pier", lambda: None)
    monkeypatch.setattr(
        runloop.egress,
        "ensure_egress_runtime_ready",
        lambda **_kwargs: (_ for _ in ()).throw(
            runloop.egress.EgressProxyError("container route unavailable")
        ),
    )
    monkeypatch.setattr(
        runloop, "_go_menu", lambda *_a, **_k: checked_out.append(True) or 0,
    )

    with pytest.raises(SystemExit, match="no task was started"):
        runloop.cmd_go(_args(workers=1, auto=None))

    assert checked_out == []


class _Process:
    next_pid = 100

    def __init__(self, command, env, returncode=0, **kwargs):
        self.command = command
        self.env = env
        self.returncode = returncode
        self.pid = self.next_pid
        _Process.next_pid += 1

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode


class _LiveProcess(_Process):
    def __init__(self, command, env, **kwargs):
        super().__init__(command, env, **kwargs)
        self.returncode = None
        self.signals = []

    def send_signal(self, value):
        self.signals.append(value)
        self.returncode = 130

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class _ScriptedProcess(_Process):
    def __init__(self, command, env, polls, on_poll=None, **kwargs):
        super().__init__(command, env, **kwargs)
        self.returncode = None
        self.polls = list(polls)
        self.on_poll = on_poll

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self.on_poll:
            self.on_poll()
            self.on_poll = None
        value = self.polls.pop(0)
        if value is not None:
            self.returncode = value
        return value


def _patch_pool_setup(monkeypatch, active_count=5):
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: object())
    monkeypatch.setattr(runloop, "tasks_root_from_config", lambda _cfg: object())
    monkeypatch.setattr(runloop, "acquire_run_lock", lambda _home: None)
    monkeypatch.setattr(runloop, "sweep_orphan_compose", lambda _home, _yes: None)
    monkeypatch.setattr(runloop, "ensure_tasks_root", lambda _root: None)
    monkeypatch.setattr(runloop, "ensure_pier", lambda: None)
    monkeypatch.setattr(
        "dradar.capacity.docker_resources",
        lambda: (64, 128.0, ()),
    )
    monkeypatch.setattr(runloop, "_retry_pending_uploads", lambda _client: None)
    monkeypatch.setattr(
        runloop, "_maintain_image_cache",
        lambda _client, _cfg, *, phase: True,
    )
    monkeypatch.setattr(
        runloop, "_prepare_batch",
        lambda _args, _client: ([{"assignment_id": str(i)} for i in range(active_count)], True),
    )


def test_auto_workers_use_capacity_recommendation(monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=5)
    from dradar.capacity import CapacityReport

    report = CapacityReport(
        recommended_workers=2, docker_cpus=8, docker_memory_gib=16,
        disk_free_gib=100, account_limit=5, held_tasks=5, task_limit=5,
        cpu_limit=4, memory_limit=2, disk_limit=7,
    )
    monkeypatch.setattr("dradar.capacity.inspect_capacity", lambda *_a, **_k: report)
    monkeypatch.setattr("dradar.capacity.print_report", lambda r: print(f"auto={r.recommended_workers}"))
    calls = []
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: calls.append(_Process(command, env, **kwargs)) or calls[-1],
    )

    assert runloop._run_worker_pool(_args(workers="auto")) == 0
    assert len(calls) == 2
    assert "auto=2" in capsys.readouterr().out


def test_one_claim_auto_workers_refill_to_detected_concurrency(monkeypatch):
    seen_refill_targets = []
    _patch_pool_setup(monkeypatch, active_count=3)
    monkeypatch.setattr(
        runloop, "_prepare_batch",
        lambda args, _client: (
            seen_refill_targets.append(args.refill_to)
            or ([{"assignment_id": str(i)} for i in range(3)], True)
        ),
    )
    from dradar.capacity import CapacityReport

    report = CapacityReport(
        recommended_workers=3, docker_cpus=12, docker_memory_gib=24,
        disk_free_gib=100, account_limit=5, held_tasks=1, task_limit=4,
        cpu_limit=6, memory_limit=3, disk_limit=7,
    )
    monkeypatch.setattr("dradar.capacity.inspect_capacity", lambda *_a, **_k: report)
    monkeypatch.setattr("dradar.capacity.print_report", lambda _report: None)
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: _Process(command, env, **kwargs),
    )
    args = _args(
        workers="auto", refill=True, refill_to=1, max_tasks=100,
        max_estimated_quota_pct=5,
    )

    assert runloop._run_worker_pool(args) == 0
    assert seen_refill_targets == [3]
    assert args.refill_to == 3


def test_worker_floor_never_overrides_explicit_task_cap():
    args = _args(workers=4, refill=True, refill_to=1, max_tasks=2)

    runloop._align_refill_target_with_workers(args)

    assert args.refill_to == 2


def test_pool_prepares_once_then_starts_requested_resume_workers(monkeypatch):
    _patch_pool_setup(monkeypatch)
    calls = []

    def popen(command, env, **kwargs):
        process = _Process(command, env, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)
    assert runloop._run_worker_pool(_args()) == 0
    assert len(calls) == 3
    assert [p.env["DRADAR_WORKER_INDEX"] for p in calls] == ["1", "2", "3"]
    assert [p.env["DRADAR_POOL_SIZE"] for p in calls] == ["3", "3", "3"]
    abort_files = {p.env["DRADAR_POOL_ABORT_FILE"] for p in calls}
    assert len(abort_files) == 1
    assert not runloop.Path(abort_files.pop()).exists()
    assert all("resume" in p.command and "--auto" not in p.command for p in calls)


def test_pool_live_target_scales_up_without_restarting_existing_workers(
        tmp_path, monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=4)
    target = tmp_path / "workers"
    target.write_text("2")
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runloop, "_pool_ready_work_count", lambda _client: 2)
    calls = []

    def popen(command, env, **kwargs):
        if len(calls) == 1:
            target.write_text("4")
        polls = [None, 0] if len(calls) < 2 else [0]
        process = _ScriptedProcess(command, env, polls, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)
    assert runloop._run_worker_pool(
        _args(workers=4, worker_target_file=str(target)),
    ) == 0
    assert [p.env["DRADAR_WORKER_INDEX"] for p in calls] == ["1", "2", "3", "4"]
    assert calls[0].signals == [] if hasattr(calls[0], "signals") else True
    assert "scaling worker pool up: 2 -> 4" in capsys.readouterr().out


def test_pool_live_scale_up_refills_to_new_worker_target(
        tmp_path, monkeypatch):
    _patch_pool_setup(monkeypatch, active_count=4)
    target = tmp_path / "workers"
    target.write_text("2")
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runloop, "_pool_ready_work_count", lambda _client: 2)
    resized = []
    replenished = []
    monkeypatch.setattr(
        runloop.refill_plan, "resize_target",
        lambda _home, value: resized.append(value) or {"refill_to": value},
    )
    monkeypatch.setattr(
        runloop.refill_plan, "refill_once",
        lambda _home, _client: replenished.append(True) or {"claimed": 2},
    )
    calls = []

    def popen(command, env, **kwargs):
        if len(calls) == 1:
            target.write_text("4")
        polls = [None, 0] if len(calls) < 2 else [0]
        process = _ScriptedProcess(command, env, polls, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)
    assert runloop._run_worker_pool(_args(
        workers=4, worker_target_file=str(target), refill=True,
        max_tasks=100, max_estimated_quota_pct=5,
    )) == 0
    assert resized == [4]
    assert replenished == [True]


def test_worker_syncs_refill_target_before_replenishing(
        tmp_path, monkeypatch):
    target = tmp_path / "workers"
    target.write_text("1")
    monkeypatch.setenv("DRADAR_POOL_TARGET_FILE", str(target))
    monkeypatch.setenv("DRADAR_POOL_MAX_SIZE", "4")
    monkeypatch.setenv("DRADAR_POOL_SIZE", "4")
    resized = []
    monkeypatch.setattr(
        runloop.refill_plan, "resize_target",
        lambda home, value: resized.append((home, value)) or {"refill_to": value},
    )
    runloop._POOL_TARGET_CACHE.clear()

    assert runloop._sync_worker_refill_target() == 1
    assert resized == [(runloop.HOME, 1)]


def test_pool_live_target_scales_down_without_signalling_inflight_workers(
        tmp_path, monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=4)
    target = tmp_path / "workers"
    target.write_text("4")
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        runloop, "_signal_workers",
        lambda _processes: pytest.fail("live scale-down must not signal workers"),
    )
    monkeypatch.setattr(runloop, "_pool_ready_work_count", lambda _client: 4)
    calls = []

    def popen(command, env, **kwargs):
        on_poll = (lambda: target.write_text("2")) if len(calls) == 0 else None
        polls = [None, 0] if len(calls) < 2 else [0]
        process = _ScriptedProcess(
            command, env, polls, on_poll=on_poll, **kwargs,
        )
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)
    assert runloop._run_worker_pool(
        _args(workers=4, worker_target_file=str(target)),
    ) == 0
    assert len(calls) == 4
    assert "scaling worker pool down: 4 -> 2" in capsys.readouterr().out


def test_worker_above_live_target_retires_before_next_checkout(
        tmp_path, monkeypatch):
    target = tmp_path / "workers"
    target.write_text("2")
    monkeypatch.setenv("DRADAR_POOL_TARGET_FILE", str(target))
    monkeypatch.setenv("DRADAR_WORKER_INDEX", "3")
    monkeypatch.setenv("DRADAR_POOL_MAX_SIZE", "4")
    monkeypatch.setenv("DRADAR_POOL_SIZE", "4")
    runloop._POOL_TARGET_CACHE.clear()
    assert runloop._worker_slot_is_enabled() is False
    monkeypatch.setenv("DRADAR_WORKER_INDEX", "2")
    assert runloop._worker_slot_is_enabled() is True


def test_worker_target_zero_retires_every_slot_before_next_checkout(
        tmp_path, monkeypatch):
    target = tmp_path / "workers"
    target.write_text("0")
    monkeypatch.setenv("DRADAR_POOL_TARGET_FILE", str(target))
    monkeypatch.setenv("DRADAR_WORKER_INDEX", "1")
    monkeypatch.setenv("DRADAR_POOL_MAX_SIZE", "4")
    monkeypatch.setenv("DRADAR_POOL_SIZE", "4")
    runloop._POOL_TARGET_CACHE.clear()
    assert runloop._worker_slot_is_enabled() is False


def test_pool_can_start_at_zero_without_spawning_workers(
        tmp_path, monkeypatch):
    _patch_pool_setup(monkeypatch, active_count=4)
    target = tmp_path / "workers"
    target.write_text("0")
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda *_args, **_kwargs: pytest.fail("zero target must not spawn workers"),
    )
    assert runloop._run_worker_pool(
        _args(workers=4, worker_target_file=str(target)),
    ) == 0


def test_worker_keeps_last_valid_target_during_atomic_file_replacement(
        tmp_path, monkeypatch):
    target = tmp_path / "workers"
    target.write_text("2")
    monkeypatch.setenv("DRADAR_POOL_TARGET_FILE", str(target))
    monkeypatch.setenv("DRADAR_WORKER_INDEX", "3")
    monkeypatch.setenv("DRADAR_POOL_MAX_SIZE", "4")
    monkeypatch.setenv("DRADAR_POOL_SIZE", "4")
    runloop._POOL_TARGET_CACHE.clear()
    assert runloop._worker_slot_is_enabled() is False
    target.write_text("")
    assert runloop._worker_slot_is_enabled() is False


def test_manual_workers_warn_before_starting_on_small_docker_vm(
    monkeypatch, capsys,
):
    _patch_pool_setup(monkeypatch, active_count=3)
    monkeypatch.setattr(
        "dradar.capacity.docker_resources",
        lambda: (2, 4.0, ()),
    )
    calls = []
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: calls.append(
            _Process(command, env, **kwargs)
        ) or calls[-1],
    )

    assert runloop._run_worker_pool(_args(workers=3)) == 0
    out = capsys.readouterr().out
    assert "reserve 6 Docker CPU" in out
    assert "reserve 20 GiB Docker memory" in out
    assert "use `--workers auto`" in out
    assert len(calls) == 3


def test_pool_does_not_start_more_workers_than_held_tasks(monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=2)
    calls = []
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: calls.append(_Process(command, env, **kwargs)) or calls[-1],
    )
    assert runloop._run_worker_pool(_args(workers=5)) == 0
    assert len(calls) == 2
    assert "starting 2 worker" in capsys.readouterr().out


def test_pool_reports_child_failure_without_hiding_other_results(monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=2)
    returncodes = iter((0, 1))
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: _Process(command, env, next(returncodes), **kwargs),
    )
    assert runloop._run_worker_pool(_args(workers=2)) == 1
    out = capsys.readouterr().out
    assert "worker 2=exit 1" in out
    assert "completed uploads are preserved" in out


def test_pool_restores_vacant_slot_when_fresh_held_work_is_waiting(
        monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=2)

    class Client:
        def __init__(self):
            self.calls = 0

        def get_assignment(self):
            self.calls += 1
            return {"active": [{"assignment_id": "new", "started_at": None}]}

    client = Client()
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: client)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    calls = []

    def popen(command, env, **kwargs):
        polls = [None, 0] if len(calls) == 0 else [0]
        process = _ScriptedProcess(command, env, polls, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=2)) == 0
    assert [p.env["DRADAR_WORKER_INDEX"] for p in calls] == ["1", "2", "2"]
    assert client.calls == 1
    assert "restoring worker slot 2/2" in capsys.readouterr().out


def test_pool_child_failure_drains_sibling_without_backfill_or_signal(
        monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=3)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count",
        lambda _client: pytest.fail("failed pool must not inspect or backfill"),
    )
    monkeypatch.setattr(
        runloop, "_signal_workers",
        lambda _processes: pytest.fail("failed pool must not signal siblings"),
    )
    calls = []

    def popen(command, env, **kwargs):
        polls = [1] if not calls else [None, 0]
        process = _ScriptedProcess(command, env, polls, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=2)) == 1
    assert len(calls) == 2
    assert calls[1].polls == []
    output = capsys.readouterr().out
    assert "disabling automatic backfill" in output
    assert "draining workers already in flight" in output


def test_explicit_resume_can_start_fresh_pool_after_failure_drain(
        monkeypatch):
    _patch_pool_setup(monkeypatch, active_count=1)
    calls = []

    def popen(command, env, **kwargs):
        process = _Process(command, env, returncode=1, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=1)) == 1
    first_abort_file = runloop.Path(calls[-1].env["DRADAR_POOL_ABORT_FILE"])
    assert not first_abort_file.exists()
    assert runloop._run_worker_pool(_args(workers=1)) == 1
    second_abort_file = runloop.Path(calls[-1].env["DRADAR_POOL_ABORT_FILE"])
    assert len(calls) == 2
    assert second_abort_file != first_abort_file
    assert not second_abort_file.exists()


def test_pool_does_not_restore_slot_for_future_retry(monkeypatch):
    _patch_pool_setup(monkeypatch, active_count=2)
    retry_after = datetime.now(timezone.utc) + timedelta(hours=1)

    class Client:
        def get_assignment(self):
            return {"active": [{
                "assignment_id": "cooling-down",
                "started_at": None,
                "retry_after": retry_after.isoformat(),
            }]}

    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: Client())
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    calls = []

    def popen(command, env, **kwargs):
        polls = [None, 0] if not calls else [0]
        process = _ScriptedProcess(command, env, polls, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=2)) == 0
    assert len(calls) == 2


def test_pool_abort_never_restores_a_worker(monkeypatch):
    _patch_pool_setup(monkeypatch, active_count=2)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count",
        lambda _client: pytest.fail("aborted pool must not inspect or backfill"),
    )
    calls = []

    def popen(command, env, **kwargs):
        on_poll = None
        polls = [None, 0]
        if len(calls) == 1:
            polls = [0]
            on_poll = lambda: runloop.Path(
                env["DRADAR_POOL_ABORT_FILE"]
            ).write_text("account stop")
        process = _ScriptedProcess(
            command, env, polls, on_poll=on_poll, **kwargs,
        )
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=2)) == 0
    assert len(calls) == 2


def test_pool_drain_keeps_active_siblings_running_without_backfill(
        monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=2)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count",
        lambda _client: pytest.fail("draining pool must not inspect or backfill"),
    )
    monkeypatch.setattr(
        runloop, "_signal_workers",
        lambda _processes: pytest.fail("draining pool must not signal siblings"),
    )
    calls = []

    def popen(command, env, **kwargs):
        if not calls:
            process = _ScriptedProcess(
                command, env, [0],
                on_poll=lambda: runloop.Path(
                    env["DRADAR_POOL_ABORT_FILE"]
                ).write_text("drain:account quota exhausted"),
                **kwargs,
            )
        else:
            process = _ScriptedProcess(command, env, [None, 0], **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=2)) == 0
    assert len(calls) == 2
    out = capsys.readouterr().out
    assert "active workers will finish" in out
    assert "drained cleanly" in out


def test_eight_worker_quota_drain_never_starts_replacements_before_reset(
        monkeypatch):
    _patch_pool_setup(monkeypatch, active_count=8)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count",
        lambda _client: pytest.fail("open quota circuit must not inspect/backfill"),
    )
    monkeypatch.setattr(
        runloop, "_signal_workers",
        lambda _processes: pytest.fail("graceful quota drain must not interrupt siblings"),
    )
    calls = []

    def popen(command, env, **kwargs):
        if not calls:
            process = _ScriptedProcess(
                command,
                env,
                [0],
                on_poll=lambda: runloop.Path(
                    env["DRADAR_POOL_ABORT_FILE"],
                ).write_text("drain:account quota exhausted"),
                **kwargs,
            )
        else:
            process = _ScriptedProcess(command, env, [None, 0], **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=8)) == 0
    assert len(calls) == 8


def test_external_pool_circuit_is_persistent_and_prevents_worker_start(
        tmp_path, monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=2)
    abort_file = tmp_path / "ACCOUNT_STOP"
    abort_file.write_text("account quota exhausted")
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(abort_file))
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda *_a, **_k: pytest.fail("open circuit must not start workers"),
    )

    assert runloop._run_worker_pool(_args(workers=2)) == 0
    assert abort_file.read_text() == "account quota exhausted"
    assert "circuit-broken" in capsys.readouterr().out


def test_backfill_spawn_failure_keeps_existing_worker_running(monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=2)

    class Client:
        def get_assignment(self):
            return {"active": [{"assignment_id": "new", "started_at": None}]}

    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: Client())
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    calls = []
    attempts = 0

    def popen(command, env, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            raise OSError("process limit")
        polls = [None, 0] if not calls else [0]
        process = _ScriptedProcess(command, env, polls, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=2)) == 1
    assert attempts == 3
    assert len(calls) == 2
    assert calls[0].returncode == 0
    out = capsys.readouterr().out
    assert "current workers will finish safely" in out
    assert "backfill spawn error" in out


def test_ready_assignment_filter_excludes_running_paused_and_bad_retry_time():
    now = datetime.now(timezone.utc)
    assert runloop._assignment_is_ready_for_checkout(
        {"started_at": None, "retry_after": None}, now=now,
    )
    assert not runloop._assignment_is_ready_for_checkout(
        {"started_at": now.isoformat(), "execution_state": "running"}, now=now,
    )
    assert not runloop._assignment_is_ready_for_checkout(
        {"started_at": now.isoformat(), "execution_state": "paused"}, now=now,
    )
    assert not runloop._assignment_is_ready_for_checkout(
        {"started_at": None, "execution_state": "paused"}, now=now,
    )
    assert not runloop._assignment_is_ready_for_checkout(
        {"started_at": None, "checkpoint_id": "checkpoint"}, now=now,
    )
    assert not runloop._assignment_is_ready_for_checkout(
        {"started_at": None, "retry_after": "not-a-time"}, now=now,
    )


def test_backfill_counts_fresh_and_safely_recoverable_work(monkeypatch):
    checkpoint = SimpleNamespace(
        valid=True, checkpoint_id="cp-1", resume_generation=2,
    )
    monkeypatch.setattr(
        runloop.checkpoints, "latest_by_assignment", lambda _home: {"a-2": checkpoint},
    )
    monkeypatch.setattr(runloop.checkpoints, "is_expired", lambda _item: False)
    monkeypatch.setattr(runloop.checkpoints, "is_terminal", lambda _home, _item: False)
    monkeypatch.setattr(
        runloop, "_checkpoint_backoff_seconds",
        lambda _item, *, generation=None: 0,
    )

    class Client:
        def get_assignment(self):
            return {"active": [
                {"assignment_id": "a-1", "started_at": None},
                {
                    "assignment_id": "a-2", "started_at": "earlier",
                    "execution_state": "paused", "runner_state": "resumable",
                    "checkpoint_id": "cp-1", "resume_generation": 2,
                },
            ]}

    assert runloop._pool_ready_work_count(Client()) == 2


def test_disabled_rollout_never_backfills_paused_checkpoint(monkeypatch):
    checkpoint = SimpleNamespace(
        valid=True, checkpoint_id="cp-1", resume_generation=2,
    )
    assignment = {
        "assignment_id": "a-1", "started_at": "earlier",
        "execution_state": "paused", "runner_state": "resumable",
        "checkpoint_id": "cp-1", "resume_generation": 2,
    }
    monkeypatch.setattr(
        runloop, "durable_checkpoint_rollout_enabled", lambda: False,
    )
    monkeypatch.setattr(
        runloop.checkpoints, "latest_by_assignment",
        lambda _home: {"a-1": checkpoint},
    )

    class Client:
        def get_assignment(self):
            return {"active": [assignment]}

    assert not runloop._assignment_is_recoverable_checkpoint(
        assignment, {"a-1": checkpoint},
    )
    assert runloop._pool_ready_work_count(Client()) == 0


def test_backfill_retries_compensated_checkpoint_when_server_is_one_generation_ahead(
    monkeypatch,
):
    checkpoint = SimpleNamespace(
        valid=True, checkpoint_id="cp-1", resume_generation=2,
    )
    assignment = {
        "assignment_id": "a-1", "started_at": "earlier",
        "execution_state": "paused", "runner_state": "paused",
        "checkpoint_id": "cp-1", "resume_generation": 3,
    }
    monkeypatch.setattr(runloop.checkpoints, "is_expired", lambda _item: False)
    monkeypatch.setattr(runloop.checkpoints, "is_terminal", lambda _home, _item: False)
    seen = []
    monkeypatch.setattr(
        runloop,
        "_checkpoint_backoff_seconds",
        lambda _item, *, generation=None: seen.append(generation) or 0,
    )

    assert runloop._assignment_is_recoverable_checkpoint(
        assignment, {"a-1": checkpoint},
    )
    assert seen == [3]


@pytest.mark.parametrize(
    ("override", "local_override"),
    [
        ({"execution_state": "running"}, {}),
        ({"runner_state": "running"}, {}),
        ({"checkpoint_id": "different"}, {}),
        ({"resume_generation": 1}, {}),
        ({"resume_generation": runloop.MAX_CHECKPOINT_RESUMES}, {}),
        ({"resume_generation": "2"}, {}),
        ({}, {"valid": False}),
    ],
)
def test_backfill_rejects_unsafe_checkpoint_candidates(
    monkeypatch, override, local_override,
):
    assignment = {
        "assignment_id": "a-1", "started_at": "earlier",
        "execution_state": "paused", "runner_state": "paused",
        "checkpoint_id": "cp-1", "resume_generation": 2,
    }
    assignment.update(override)
    checkpoint_values = {
        "valid": True, "checkpoint_id": "cp-1", "resume_generation": 2,
    }
    checkpoint_values.update(local_override)
    checkpoint = SimpleNamespace(**checkpoint_values)
    monkeypatch.setattr(runloop.checkpoints, "is_expired", lambda _item: False)
    monkeypatch.setattr(runloop.checkpoints, "is_terminal", lambda _home, _item: False)
    monkeypatch.setattr(runloop, "_checkpoint_backoff_seconds", lambda _item: 0)

    assert not runloop._assignment_is_recoverable_checkpoint(
        assignment, {"a-1": checkpoint},
    )


def test_backfill_queue_read_fails_closed(monkeypatch, capsys):
    class Client:
        def get_assignment(self):
            raise runloop.ApiError("network unavailable")

    assert runloop._pool_ready_work_count(Client()) is None
    assert "keeping current workers only" in capsys.readouterr().out


def test_declining_pool_claims_nothing(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    monkeypatch.setattr(
        runloop, "_load_config",
        lambda: pytest.fail("configuration must not be touched after decline"),
    )
    assert runloop._run_worker_pool(_args(yes=False)) == 1


def test_later_spawn_failure_stops_already_started_worker(monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=2)
    first = None

    def popen(command, env, **kwargs):
        nonlocal first
        if first is not None:
            raise OSError("process limit")
        first = _LiveProcess(command, env, **kwargs)
        return first

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)
    assert runloop._run_worker_pool(_args(workers=2)) == 1
    assert first.poll() is not None
    assert first.signals
    assert "stopping those already started" in capsys.readouterr().out
