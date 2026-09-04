"""One-command worker pool: selection happens once; children only resume."""

import argparse
from datetime import datetime, timedelta, timezone
import os
import sys
from types import SimpleNamespace
import zipfile

import httpx
import pytest

from dradar import cli, fleet, runloop
from dradar.api_client import ApiClient


@pytest.fixture(autouse=True)
def _isolate_worker_boundary_mechanics(monkeypatch):
    # Pool mechanics tests use clients intentionally stripped of API methods.
    # Boundary integration has its own state/reconciliation coverage.
    monkeypatch.setattr(
        runloop, "_prepare_assignment_boundary", lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        runloop, "_finish_assignment_boundary", lambda *_a, **_k: True,
    )


def _args(**overrides):
    values = dict(
        workers=3, yes=True, keep=False, allow_task_drift=False,
        dev_agent=None, refill=False, refill_to=None, max_tasks=None,
        max_estimated_quota_pct=None, quota_tier="plus", auto=5, pick=None,
        assignment=None, parallel=False, worker_child=False, resume=False,
        worker_target_file=None, archive_session=False, batch_id=None,
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


def test_worker_command_reuses_direct_zipapp(monkeypatch, tmp_path):
    artifact = tmp_path / "dradar-linux-x86_64.pyz"
    with zipfile.ZipFile(artifact, "w") as bundle:
        bundle.writestr("__main__.py", "raise SystemExit(0)\n")
    monkeypatch.setattr(sys, "argv", [str(artifact)])

    command = runloop._worker_command(_args())

    assert command[:2] == [sys.executable, str(artifact.resolve())]
    assert "-m" not in command[:3]


@pytest.mark.skipif(os.name == "nt", reason="POSIX verified-fd launch")
def test_worker_command_preserves_verified_zipapp_fd(monkeypatch, tmp_path):
    artifact = tmp_path / "candidate.pyz"
    artifact.write_bytes(b"signed-candidate")
    with artifact.open("rb") as handle:
        entrypoint = f"/dev/fd/{handle.fileno()}"
        monkeypatch.setattr(sys, "argv", [entrypoint])

        assert runloop._worker_command(_args())[:2] == [sys.executable, entrypoint]
        assert runloop._worker_entrypoint_pass_fds() == (handle.fileno(),)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ModuleNotFoundError("private module path"), "startup-dependency-missing"),
        (PermissionError("/private/user/path"), "startup-permission-denied"),
        (OSError(getattr(os, "ENOSPC", 28), "/private/user/path"),
         "startup-local-storage-error"),
    ],
)
def test_worker_startup_failure_marker_is_low_cardinality(
        monkeypatch, tmp_path, failure, expected):
    marker = tmp_path / "worker.started"
    marker.write_text("preparing", encoding="utf-8")
    monkeypatch.setenv(runloop._POOL_WORKER_ACTIVITY_ENV, str(marker))

    runloop._publish_fleet_startup_failure(
        _args(worker_child=True, fleet_pool=False), failure,
    )

    assert marker.read_text(encoding="utf-8") == f"preparing:{expected}"
    assert "private" not in marker.read_text(encoding="utf-8")


def test_cli_parses_archive_session_as_explicit_opt_in(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_go", lambda args: seen.append(args) or 0)
    assert cli.main(["go", "--archive-session", "-y"]) == 0
    assert seen[0].archive_session is True


def test_cli_parses_repeatable_expected_assignment_boundary(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_go", lambda args: seen.append(args) or 0)

    assert cli.main([
        "resume", "-y",
        "--expect-assignment", "a1",
        "--expect-assignment", "a2",
    ]) == 0
    assert seen[0].expect_assignment == ["a1", "a2"]


@pytest.mark.parametrize("command", ("go", "resume"))
def test_cli_parses_and_normalizes_exact_batch_id(monkeypatch, command):
    seen = []
    monkeypatch.setattr(cli, "cmd_go", lambda args: seen.append(args) or 0)
    batch_id = "550E8400-E29B-41D4-A716-446655440000"

    assert cli.main([command, "--batch-id", batch_id, "-y"]) == 0
    assert seen[0].batch_id == "550e8400e29b41d4a716446655440000"


@pytest.mark.parametrize("value", (
    "not-a-uuid",
    "550e8400e29b41d4a71644665544000",
    "{550e8400-e29b-41d4-a716-446655440000}",
    "550e8400e29b41d4a716446655440000-extra",
))
def test_cli_rejects_noncanonical_batch_id(value):
    with pytest.raises(SystemExit) as exc:
        cli.main(["resume", "--batch-id", value, "-y"])
    assert exc.value.code == 2


@pytest.mark.parametrize("workers", [0, 41])
def test_worker_count_is_bounded_before_any_setup(workers):
    with pytest.raises(SystemExit, match="1 <= N <= 40"):
        runloop.cmd_go(_args(workers=workers))


def test_worker_count_accepts_40(monkeypatch):
    seen = []
    monkeypatch.setattr(runloop, "_run_worker_pool", lambda args: seen.append(args.workers) or 0)
    assert runloop.cmd_go(_args(workers=40)) == 0
    assert seen == [40]


def test_antigravity_refill_preflight_fails_before_pool_or_server(
        tmp_path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(
        runloop, "prepare_antigravity_auth",
        lambda: f"cannot inspect {tmp_path}/providers/antigravity/.gemini/token",
    )
    started = []
    monkeypatch.setattr(
        runloop, "_run_worker_pool", lambda _args: started.append(True) or 0,
    )
    runloop.refill_plan.configure(
        tmp_path,
        volunteer_id="vol-1", refill_to=1, max_tasks=2,
        quota_tier="plus", max_estimated_quota_pct=None, active=[],
        refill_harness="antigravity", refill_model="gemini-3.7-flash",
        refill_effort="medium",
    )
    args = _args(
        workers=2, refill=True, refill_to=1, max_tasks=2, auto=None,
        refill_harness="antigravity", refill_model="gemini-3.7-flash",
        refill_effort="medium", refill_order="cost",
        batch_id="550e8400e29b41d4a716446655440000",
    )

    with pytest.raises(SystemExit) as exc:
        runloop.cmd_go(args)

    assert started == []
    message = str(exc.value)
    assert "$DRADAR_HOME/providers/antigravity" in message
    assert str(tmp_path) not in message
    assert "dradar provider setup antigravity" in message
    plan = runloop.refill_plan.load(tmp_path)
    assert plan["status"] == runloop.refill_plan.FAULTED_STATE
    assert plan["circuit"] == {
        "state": "open",
        "volunteer_id": "vol-1",
        "harness": "antigravity",
        "provider": "google-antigravity-subscription",
        "batch_id": "550e8400e29b41d4a716446655440000",
        "failure_family": "provider_not_ready",
        "opened_at": plan["circuit"]["opened_at"],
        "last_observed_at": plan["circuit"]["last_observed_at"],
        "observation_count": 1,
        "assignment_id": None,
    }


def test_antigravity_capability_426_aborts_pool_with_setup_guidance(
        tmp_path, monkeypatch,
):
    abort = tmp_path / "abort"
    activity = tmp_path / "activity"
    activity.write_text("preparing", encoding="utf-8")
    monkeypatch.setenv(runloop._POOL_ABORT_ENV, str(abort))
    monkeypatch.setenv(runloop._POOL_WORKER_ACTIVITY_ENV, str(activity))

    with pytest.raises(SystemExit, match="provider setup antigravity"):
        runloop._exit_for(runloop.ApiError(
            "server returned 426: provider is not ready",
            status_code=426,
            code="provider_capability_required",
            required_capability=runloop.ANTIGRAVITY_CAPABILITY,
        ))

    assert "Antigravity provider is not ready" in abort.read_text()
    assert activity.read_text() == "preparing:provider_capability_required"


def test_dynamic_target_requires_a_fixed_multi_worker_pool():
    with pytest.raises(SystemExit, match="fixed --workers N greater than 1"):
        runloop.cmd_go(_args(workers=1, worker_target_file="target"))
    with pytest.raises(SystemExit, match="fixed --workers N greater than 1"):
        runloop.cmd_go(_args(workers="auto", worker_target_file="target"))


def test_direct_single_worker_fleet_checkout_publishes_ready(
    tmp_path, monkeypatch,
):
    """A workers=1 Fleet has no pool parent to promote its checkout marker."""

    fleet._prepare_dirs(tmp_path)
    batch_id = "550e8400e29b41d4a716446655440000"
    activity = tmp_path / "worker-activity"
    activity.write_text("preparing", encoding="utf-8")
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setenv(fleet.CONTROLLER_ID_ENV, "controller-1")
    monkeypatch.setenv(fleet.POOL_BATCH_ENV, batch_id)
    monkeypatch.setenv(
        fleet.POOL_STARTUP_FILE_ENV,
        str(fleet._pool_startup_path(tmp_path, batch_id)),
    )
    monkeypatch.setenv(runloop._POOL_WORKER_ACTIVITY_ENV, str(activity))
    monkeypatch.setattr(fleet, "controller_matches", lambda *_args: True)

    assert runloop._record_supervised_worker_checkout(
        _args(
            workers=1, fleet_pool=True, worker_child=False,
            batch_id=batch_id,
        ),
        "a" * 32,
    ) is True

    assert activity.read_text(encoding="utf-8") == "a" * 32
    startup = fleet._read_json(fleet._pool_startup_path(tmp_path, batch_id))
    assert startup["status"] == "ready"
    assert startup["batch_id"] == batch_id
    assert startup["pid"] == runloop.os.getpid()


def test_direct_single_worker_fails_closed_when_ready_cannot_be_published(
    tmp_path, monkeypatch,
):
    activity = tmp_path / "worker-activity"
    activity.write_text("preparing", encoding="utf-8")
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setenv(runloop._POOL_WORKER_ACTIVITY_ENV, str(activity))
    monkeypatch.setattr(
        fleet, "publish_pool_startup_ready",
        lambda *_args: (_ for _ in ()).throw(
            fleet.FleetError("coordinator unavailable")
        ),
    )

    assert runloop._record_supervised_worker_checkout(
        _args(
            workers=1, fleet_pool=True, worker_child=False,
            batch_id="550e8400e29b41d4a716446655440000",
        ),
        "a" * 32,
    ) is False


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


def test_refill_rejects_fixed_assignment_boundary_options():
    with pytest.raises(SystemExit, match="cannot be combined with --refill"):
        runloop.cmd_go(_args(
            refill=True,
            max_tasks=2,
            expect_assignment=["a1"],
        ))


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


def test_worker_command_forwards_exact_batch_to_every_child():
    batch_id = "550e8400e29b41d4a716446655440000"
    command = runloop._worker_command(_args(batch_id=batch_id))

    assert command.count("--batch-id") == 1
    assert command[command.index("--batch-id") + 1] == batch_id


def test_legacy_worker_command_omits_batch_id():
    assert "--batch-id" not in runloop._worker_command(_args())


class _Telemetry:
    session_id = "session-test"

    def __init__(self, _client, **_kwargs):
        self.closed = None

    def start(self):
        pass

    def bind_batch(self, _batch_id):
        pass

    def set_phase(self, _phase):
        pass

    def close(self, reason):
        self.closed = reason


@pytest.mark.parametrize(
    ("worker_child", "expected_rc", "expected_checkout", "expected_retry"),
    ((True, 0, True, False), (False, 0, True, True)),
)
def test_only_parent_retries_pending_uploads_before_draining_waiting_work(
        monkeypatch, capsys, worker_child, expected_rc, expected_checkout,
        expected_retry):
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
    capsys.readouterr()


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
        runloop, "_go_menu", lambda *_a, **_k: checked_out.append(True) or 0,
    )

    assert runloop.cmd_go(_args(workers=1, auto=None)) == 0
    assert checked_out == [True]


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
        if returncode == 0 and env.get(runloop._POOL_WORKER_ACTIVITY_ENV):
            runloop.Path(env[runloop._POOL_WORKER_ACTIVITY_ENV]).write_text(
                "test-assignment"
            )

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
    def __init__(
        self, command, env, polls, on_poll=None, mark_activity=True, **kwargs,
    ):
        super().__init__(command, env, returncode=1, **kwargs)
        self.returncode = None
        self.polls = list(polls)
        self.on_poll = on_poll
        self.mark_activity = mark_activity

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self.on_poll:
            self.on_poll()
            self.on_poll = None
        value = self.polls.pop(0)
        if value is not None:
            self.returncode = value
            if (
                value == 0 and self.mark_activity
                and self.env.get(runloop._POOL_WORKER_ACTIVITY_ENV)
            ):
                runloop.Path(
                    self.env[runloop._POOL_WORKER_ACTIVITY_ENV]
                ).write_text("test-assignment")
        return value


def _patch_pool_setup(monkeypatch, active_count=5):
    class Client:
        def get_assignment(self):
            return {"active": []}

    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: Client())
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
    monkeypatch.setattr(
        runloop, "_prepare_assignment_boundary",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        runloop, "_finish_assignment_boundary",
        lambda *_args, **_kwargs: True,
    )


def test_isolated_builder_preflight_fails_before_batch_or_worker_session(
    monkeypatch, capsys,
):
    _patch_pool_setup(monkeypatch, active_count=40)
    monkeypatch.setattr(
        runloop.image_cache,
        "preflight_trial_builder",
        lambda *_a, **_k: runloop.image_cache.TrialBuilderPreflight(
            False,
            1,
            "base_image_metadata",
            "registry_timeout",
            "load metadata for docker.io/library/node:22-bookworm: i/o timeout",
            (),
        ),
    )
    monkeypatch.setattr(
        runloop,
        "_prepare_batch",
        lambda *_a, **_k: pytest.fail("preflight must happen before batch preparation"),
    )
    monkeypatch.setattr(
        runloop.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("preflight must happen before worker sessions"),
    )

    rc = runloop._run_worker_pool(_args(workers=40))

    assert rc == runloop._ENVIRONMENT_BUILD_FAILED_EXIT_CODE
    output = capsys.readouterr().out
    assert "before worker sessions or task checkout" in output
    assert "stage=base_image_metadata" in output
    assert "exit=1" in output


def test_pool_children_inherit_the_preflighted_https_mirror_snapshot(monkeypatch):
    _patch_pool_setup(monkeypatch, active_count=2)
    monkeypatch.setattr(
        runloop.image_cache,
        "preflight_trial_builder",
        lambda *_a, **_k: runloop.image_cache.TrialBuilderPreflight(
            True,
            0,
            "base_image_metadata",
            None,
            "",
            ("https://mirror.example",),
        ),
    )
    calls = []
    monkeypatch.setattr(
        runloop.subprocess,
        "Popen",
        lambda command, env, **kwargs: (
            calls.append(_Process(command, env, **kwargs)) or calls[-1]
        ),
    )

    assert runloop._run_worker_pool(_args(workers=2)) == 0
    assert len(calls) == 2
    assert all(
        process.env[runloop.image_cache.BUILDER_MIRRORS_ENV]
        == '["https://mirror.example"]'
        for process in calls
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


def test_worker_parent_passes_one_shared_assignment_boundary_to_children(
        monkeypatch, tmp_path):
    _patch_pool_setup(monkeypatch, active_count=2)
    boundary = tmp_path / "boundary.json"
    monkeypatch.setattr(
        runloop, "_prepare_assignment_boundary",
        lambda *_args, **_kwargs: boundary,
    )
    calls = []
    monkeypatch.setattr(
        runloop.subprocess,
        "Popen",
        lambda command, env, **kwargs: (
            calls.append(_Process(command, env, **kwargs)) or calls[-1]
        ),
    )

    assert runloop._run_worker_pool(_args(workers=2)) == 0
    assert len(calls) == 2
    assert {
        process.env[runloop._ASSIGNMENT_BOUNDARY_ENV]
        for process in calls
    } == {str(boundary)}


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


def test_pool_children_inherit_the_parents_exact_capability_snapshot(monkeypatch):
    _patch_pool_setup(monkeypatch)
    calls = []

    class Client:
        capabilities = ("public-task-package-pin-v1", "codebuddy-ready-v4")

        def get_assignment(self):
            return {"active": []}

    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: Client())
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: (
            calls.append(_Process(command, env, **kwargs)) or calls[-1]
        ),
    )

    assert runloop._run_worker_pool(_args()) == 0
    assert len(calls) == 3
    assert all(
        process.env[runloop._POOL_CAPABILITIES_ENV]
        == '["public-task-package-pin-v1","codebuddy-ready-v4"]'
        for process in calls
    )


def test_worker_child_uses_parent_capabilities_without_reprobing(monkeypatch):
    monkeypatch.setattr(runloop, "_load_config", lambda: {
        "server": "https://api.example.com", "token": "token",
        "benchmark": "deep-swe",
    })
    monkeypatch.setenv(
        runloop._POOL_CAPABILITIES_ENV,
        '["public-task-package-pin-v1","codebuddy-ready-v4"]',
    )
    cfg = runloop._run_config(_args(worker_child=True))
    assert cfg["client_capabilities"] == (
        "codebuddy-ready-v4", "public-task-package-pin-v1",
    )


def test_multi_worker_pool_defaults_to_shared_build_cache(monkeypatch):
    monkeypatch.setattr(runloop, "_load_config", lambda: {
        "server": "https://api.example.com", "token": "token",
    })
    args = _args(workers=8)
    runloop._run_config(args)
    assert args._build_cache_mode == "shared"


def test_single_worker_keeps_isolated_build_cache_default(monkeypatch):
    monkeypatch.setattr(runloop, "_load_config", lambda: {
        "server": "https://api.example.com", "token": "token",
    })
    args = _args(workers=1)
    runloop._run_config(args)
    assert args._build_cache_mode == "isolated"


def test_explicit_build_cache_setting_wins_over_multi_worker_default(monkeypatch):
    monkeypatch.setattr(runloop, "_load_config", lambda: {
        "server": "https://api.example.com", "token": "token",
        "build_cache_mode": "isolated",
    })
    args = _args(workers=8)
    runloop._run_config(args)
    assert args._build_cache_mode == "isolated"


def test_worker_child_rejects_malformed_parent_capability_snapshot(monkeypatch):
    monkeypatch.setattr(runloop, "_load_config", lambda: {
        "server": "https://api.example.com", "token": "token",
    })
    monkeypatch.setenv(
        runloop._POOL_CAPABILITIES_ENV, '["duplicate","duplicate"]',
    )
    with pytest.raises(
        SystemExit, match="invalid internal worker capability snapshot",
    ):
        runloop._run_config(_args(worker_child=True))


def test_pool_scopes_inventory_and_all_children_to_exact_batch(monkeypatch):
    _patch_pool_setup(monkeypatch, active_count=2)
    batch_id = "550e8400e29b41d4a716446655440000"
    scoped = []

    class Client:
        def set_batch_id(self, value):
            scoped.append(value)

        def get_assignment(self):
            return {"active": []}

    client = Client()
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: client)
    calls = []
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: (
            calls.append(_Process(command, env, **kwargs)) or calls[-1]
        ),
    )

    assert runloop._run_worker_pool(
        _args(workers=2, batch_id=batch_id),
    ) == 0
    assert scoped == [batch_id]
    assert len(calls) == 2
    assert all(
        process.command[process.command.index("--batch-id") + 1] == batch_id
        for process in calls
    )


def test_pool_ready_count_uses_only_inventory_returned_by_scoped_client(
        monkeypatch):
    selected = "550e8400e29b41d4a716446655440000"

    class Client:
        def get_assignment(self):
            # The ApiClient contract has already filtered to the selected
            # batch. A different campaign may have abundant waiting work, but
            # it must not enter this supervisor's ready/live arithmetic.
            return {"active": [
                {"assignment_id": "live", "batch_id": selected,
                 "started_at": "2026-08-29T00:00:00Z",
                 "heartbeat_running": True},
                {"assignment_id": "waiting", "batch_id": selected,
                 "started_at": None, "heartbeat_running": False},
            ]}

    assert runloop._pool_ready_work_count(
        Client(), desired_workers=2,
    ) == 1
    assert runloop._pool_ready_work_count(
        Client(), desired_workers=1,
    ) == 0


def _scoped_refill_args(**overrides):
    return _args(
        workers=2,
        auto=None,
        refill=True,
        fleet_pool=True,
        batch_id="550e8400e29b41d4a716446655440000",
        credentials_file="/private/plan.json",
        **overrides,
    )


def _ready_run_plan_envelope():
    return {
        "schema_version": 1,
        "envelope": {
            "status": "already_running",
            "interaction": "silent",
            "decision_required": False,
            "user_message": "",
            "agent_action": "monitor",
            "error_code": None,
            "retryable": False,
            "choices": [],
        },
    }


def test_scoped_refill_wait_refreshes_device_and_opens_after_global_seed_barrier(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_run_config", lambda _args: {
        "run_plan_id": "plan-a",
        "run_plan_logical_session_id": "drl_same_device",
    })
    monkeypatch.setattr(runloop, "_pending_uploads_for_batch", lambda _batch: [])
    results = iter((
        {"status": "active", "claimed": 0, "seed_pending": 1},
        {"status": "active", "claimed": 2},
    ))
    monkeypatch.setattr(
        runloop.refill_plan, "refill_once", lambda _home, _client: next(results),
    )
    assignment = {"assignment_id": "new-a", "started_at": None}
    inventories = iter(([], [assignment]))
    monkeypatch.setattr(
        runloop,
        "_acquire_batch",
        lambda *_args, **_kwargs: (next(inventories), True),
    )
    ready_calls = []
    ready = iter((0, 1))
    monkeypatch.setattr(
        runloop,
        "_pool_ready_work_count",
        lambda _client, **kwargs: ready_calls.append(kwargs) or next(ready),
    )
    sleeps = []
    monkeypatch.setattr(runloop.time, "sleep", sleeps.append)

    class Client:
        def __init__(self):
            self.starts = []

        def start_run_plan(self, **kwargs):
            self.starts.append(kwargs)
            return _ready_run_plan_envelope()

    client = Client()
    active = runloop._wait_for_scoped_refill_work(
        _scoped_refill_args(), client, desired_workers=2,
    )

    assert active == [assignment]
    assert client.starts == [{
        "plan_id": "plan-a",
        "logical_session_id": "drl_same_device",
        "concurrency_mode": "fixed",
        "concurrency": 2,
    }] * 2
    assert sleeps == [runloop._SCOPED_REFILL_WAIT_SECONDS]
    assert runloop._SCOPED_REFILL_WAIT_SECONDS >= 30
    # A second device's live workers belong to the server's aggregate target,
    # not this device's local two-worker cap. The wait path counts exact local
    # waiting inventory without subtracting global heartbeat owners.
    assert ready_calls == [{}, {}]


@pytest.mark.parametrize("status_code", (401, 403, 410))
def test_scoped_refill_wait_does_not_retry_terminal_authorization_errors(
    tmp_path, monkeypatch, status_code,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_run_config", lambda _args: {
        "run_plan_id": "plan-a",
        "run_plan_logical_session_id": "drl_same_device",
    })
    sleeps = []
    monkeypatch.setattr(runloop.time, "sleep", sleeps.append)

    class Client:
        calls = 0

        def start_run_plan(self, **_kwargs):
            self.calls += 1
            raise runloop.ApiError(
                "plan no longer authorized", status_code=status_code,
            )

    client = Client()
    with pytest.raises(SystemExit, match="no longer authorized"):
        runloop._wait_for_scoped_refill_work(
            _scoped_refill_args(), client, desired_workers=2,
        )

    assert client.calls == 1
    assert sleeps == []


def test_scoped_refill_wait_honors_retry_after_for_transient_error(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_run_config", lambda _args: {
        "run_plan_id": "plan-a",
        "run_plan_logical_session_id": "drl_same_device",
    })
    monkeypatch.setattr(runloop, "_pending_uploads_for_batch", lambda _batch: [])
    monkeypatch.setattr(
        runloop.refill_plan,
        "refill_once",
        lambda _home, _client: {"status": "active", "claimed": 1},
    )
    assignment = {"assignment_id": "new-a", "started_at": None}
    monkeypatch.setattr(
        runloop, "_acquire_batch", lambda *_args, **_kwargs: ([assignment], True),
    )
    monkeypatch.setattr(runloop, "_pool_ready_work_count", lambda _client: 1)
    sleeps = []
    monkeypatch.setattr(runloop.time, "sleep", sleeps.append)

    class Client:
        def __init__(self):
            self.calls = 0

        def start_run_plan(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise runloop.ApiError(
                    "busy", status_code=503, retry_after=45,
                )
            return _ready_run_plan_envelope()

    client = Client()
    assert runloop._wait_for_scoped_refill_work(
        _scoped_refill_args(), client, desired_workers=2,
    ) == [assignment]
    assert client.calls == 2
    assert sleeps == [45]


def test_scoped_refill_wait_retries_exact_pending_upload_until_recovered(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_run_config", lambda _args: {
        "run_plan_id": "plan-a",
        "run_plan_logical_session_id": "drl_same_device",
    })
    pending_entries = [{
        "assignment_id": "done-a",
        "batch_id": "550e8400e29b41d4a716446655440000",
    }]
    monkeypatch.setattr(
        runloop, "_pending_uploads_for_batch", lambda _batch: list(pending_entries),
    )
    retry_calls = []

    def retry(_client, *, batch_id=None):
        retry_calls.append(batch_id)
        if len(retry_calls) == 3:
            pending_entries.clear()
        return ["submitted" if not pending_entries else "upload-failed"]

    monkeypatch.setattr(runloop, "_retry_pending_uploads", retry)
    monkeypatch.setattr(
        runloop.refill_plan,
        "refill_once",
        lambda _home, _client: {"status": "active", "seed_pending": 1},
    )
    assignment = {"assignment_id": "new-a", "started_at": None}
    inventories = iter(([assignment], [assignment], [assignment]))
    monkeypatch.setattr(
        runloop,
        "_acquire_batch",
        lambda *_args, **_kwargs: (next(inventories), True),
    )
    ready = iter((1, 1, 1))
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count", lambda _client: next(ready),
    )
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        runloop,
        "_run_and_submit",
        lambda *_args, **_kwargs: pytest.fail("pending replay must not rerun a model"),
    )

    class Client:
        def start_run_plan(self, **_kwargs):
            return _ready_run_plan_envelope()

    active = runloop._wait_for_scoped_refill_work(
        _scoped_refill_args(), Client(), desired_workers=2,
    )

    assert active == [assignment]
    assert retry_calls == [
        "550e8400e29b41d4a716446655440000",
    ] * 3


def test_scoped_refill_wait_exposes_blocked_upload_instead_of_waiting_forever(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_run_config", lambda _args: {
        "run_plan_id": "plan-a",
        "run_plan_logical_session_id": "drl_same_device",
    })
    monkeypatch.setattr(runloop, "_pending_uploads_for_batch", lambda _batch: [{
        "assignment_id": "done-a",
        "upload_blocked": "owner_superseded",
    }])
    monkeypatch.setattr(
        runloop.refill_plan,
        "refill_once",
        lambda *_args: pytest.fail("blocked upload must stop before refill"),
    )

    class Client:
        def start_run_plan(self, **_kwargs):
            return _ready_run_plan_envelope()

    with pytest.raises(SystemExit, match="needs upload review"):
        runloop._wait_for_scoped_refill_work(
            _scoped_refill_args(), Client(), desired_workers=2,
        )


def test_scoped_device_ready_count_does_not_subtract_other_device_workers():
    class Client:
        def get_assignment(self):
            return {"active": [
                {"assignment_id": "a-live", "started_at": "earlier",
                 "heartbeat_running": True},
                {"assignment_id": "b-live", "started_at": "earlier",
                 "heartbeat_running": True},
                {"assignment_id": "c-wait", "started_at": None},
                {"assignment_id": "d-wait", "started_at": None},
            ]}

    assert runloop._pool_ready_work_count(Client(), desired_workers=2) == 0
    assert runloop._pool_ready_work_count(Client()) == 2


def test_pool_live_target_scales_up_without_restarting_existing_workers(
        tmp_path, monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=4)
    target = tmp_path / "workers"
    target.write_text("2")
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    ready = iter((2, 0))
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count",
        lambda _client, **_kwargs: next(ready, 0),
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
    ready = iter((2, 0))
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count",
        lambda _client, **_kwargs: next(ready, 0),
    )
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
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count", lambda _client, **_kwargs: 0,
    )
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
            return {
                "active": (
                    [{"assignment_id": "new", "started_at": None}]
                    if self.calls == 1 else []
                )
            }

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
    assert client.calls == 2
    assert "restoring worker slot 2/2" in capsys.readouterr().out


def test_pool_reconciles_waiting_work_after_last_healthy_worker_exits(
        monkeypatch, capsys):
    """A zero-live pool must not retire while its batch still has waiting work."""
    _patch_pool_setup(monkeypatch, active_count=1)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    ready = iter((1, 0))
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count",
        lambda _client, **_kwargs: next(ready, 0),
    )
    calls = []

    def popen(command, env, **kwargs):
        process = _ScriptedProcess(command, env, [0], **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=1)) == 0
    assert len(calls) == 2
    assert [p.env["DRADAR_WORKER_INDEX"] for p in calls] == ["1", "1"]
    assert "restoring worker slot 1/1" in capsys.readouterr().out


def test_worker_checkout_activity_marker_is_atomic_and_local(
        tmp_path, monkeypatch):
    marker = tmp_path / "slot.started"
    monkeypatch.setenv(runloop._POOL_WORKER_ACTIVITY_ENV, str(marker))

    runloop._record_worker_checkout("assignment-1")

    assert marker.read_text() == "assignment-1"
    assert list(tmp_path.glob("slot.started.*.tmp")) == []


def test_no_waiting_after_pause_or_release_never_spawns_replacement(
        monkeypatch):
    _patch_pool_setup(monkeypatch, active_count=1)
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count", lambda _client, **_kwargs: 0,
    )
    calls = []

    def popen(command, env, **kwargs):
        process = _ScriptedProcess(
            command, env, [0], mark_activity=False, **kwargs,
        )
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=1)) == 0
    assert len(calls) == 1


def test_precheckout_capability_failure_has_bounded_backfill(
        monkeypatch, capsys):
    """A 426-like preflight exit cannot create an unbounded spawn storm."""
    _patch_pool_setup(monkeypatch, active_count=1)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runloop, "_pool_backfill_delay", lambda _attempt: 0)
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count", lambda _client, **_kwargs: 1,
    )
    calls = []

    def popen(command, env, **kwargs):
        process = _ScriptedProcess(
            command, env, [1], mark_activity=False, **kwargs,
        )
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=1)) == 1
    assert len(calls) == runloop._POOL_BACKFILL_MAX_ATTEMPTS
    output = capsys.readouterr().out
    assert "exited 1 before checkout" in output
    assert "bounded replacement is exhausted" in output


def test_fleet_pool_fails_after_24_seconds_when_every_child_exits_precheckout(
    tmp_path, monkeypatch,
):
    """A spawned parent is not ready until a child proves real checkout."""
    _patch_pool_setup(monkeypatch, active_count=2)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(
        runloop,
        "_run_config",
        lambda _args: {
            "benchmark": runloop.DEFAULT_BENCHMARK,
            "run_plan_id": "plan-a",
        },
    )
    monkeypatch.setattr(
        runloop, "_selected_tasks_root", lambda _cfg: object(),
    )
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count", lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        runloop, "_retry_pending_uploads", lambda *_args, **_kwargs: None,
    )
    fleet._prepare_dirs(tmp_path)
    batch_id = "550e8400e29b41d4a716446655440000"
    monkeypatch.setenv(fleet.CONTROLLER_ID_ENV, "controller-1")
    monkeypatch.setenv(fleet.POOL_BATCH_ENV, batch_id)
    monkeypatch.setenv(
        fleet.POOL_STARTUP_FILE_ENV,
        str(fleet._pool_startup_path(tmp_path, batch_id)),
    )
    monkeypatch.setattr(fleet, "controller_matches", lambda *_args: True)

    clock = {"seconds": 0.0}
    monkeypatch.setattr(runloop.time, "monotonic", lambda: clock["seconds"])
    monkeypatch.setattr(
        runloop.time,
        "sleep",
        lambda seconds: clock.__setitem__("seconds", clock["seconds"] + seconds),
    )
    observed_events = []
    observed_flushes = []

    class Recorder:
        def __init__(self, _home, _client):
            pass

        def try_record(self, event_type, **kwargs):
            observed_events.append((event_type, kwargs))

        def flush(self, **kwargs):
            observed_flushes.append(kwargs)
            return 0

    monkeypatch.setattr(runloop, "FlightRecorder", Recorder)

    class DelayedPrecheckoutExit(_Process):
        def __init__(self, command, env, **kwargs):
            super().__init__(command, env, returncode=1, **kwargs)
            self.returncode = None

        def poll(self):
            if clock["seconds"] < 24.0:
                assert not fleet._pool_startup_path(tmp_path, batch_id).exists()
                return None
            self.returncode = 0
            return 0

    monkeypatch.setattr(
        runloop.subprocess,
        "Popen",
        lambda command, env, **kwargs: DelayedPrecheckoutExit(
            command, env, **kwargs,
        ),
    )

    result = runloop._run_worker_pool(_args(
        workers=2,
        fleet_pool=True,
        batch_id=batch_id,
        credentials_file="/private/plan.json",
    ))

    startup = fleet._read_json(fleet._pool_startup_path(tmp_path, batch_id))
    assert result == 1
    assert clock["seconds"] >= 24.0
    assert startup["status"] == "failed"
    assert startup["error_code"] == "local_runner_never_ready"
    assert [event for event, _kwargs in observed_events].count(
        "precheckout_exit"
    ) == 2
    assert [event for event, _kwargs in observed_events] == [
        "supervisor_spawned",
        "precheckout_exit",
        "precheckout_exit",
        "startup_failed",
    ]
    assert all(
        kwargs.get("reason_code") == "worker-entrypoint-failed"
        for event, kwargs in observed_events
        if event in {"precheckout_exit", "startup_failed"}
    )
    assert observed_flushes
    assert all(item == {"batch_id": batch_id} for item in observed_flushes)


@pytest.mark.parametrize(
    "marker,error_code,message_fragment",
    [
        (
            "drain:account quota exhausted",
            "startup_aborted_by_account_stop",
            "账号停止指令",
        ),
        (
            "account safety circuit opened",
            "startup_aborted_by_circuit",
            "本地安全熔断",
        ),
    ],
)
def test_fleet_precheckout_abort_is_never_completed(
    tmp_path, monkeypatch, marker, error_code, message_fragment,
):
    _patch_pool_setup(monkeypatch, active_count=2)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(
        runloop,
        "_run_config",
        lambda _args: {
            "benchmark": runloop.DEFAULT_BENCHMARK,
            "run_plan_id": "plan-a",
        },
    )
    monkeypatch.setattr(
        runloop, "_selected_tasks_root", lambda _cfg: object(),
    )
    monkeypatch.setattr(
        runloop, "_retry_pending_uploads", lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    fleet._prepare_dirs(tmp_path)
    batch_id = "550e8400e29b41d4a716446655440000"
    monkeypatch.setenv(fleet.CONTROLLER_ID_ENV, "controller-1")
    monkeypatch.setenv(fleet.POOL_BATCH_ENV, batch_id)
    monkeypatch.setenv(
        fleet.POOL_STARTUP_FILE_ENV,
        str(fleet._pool_startup_path(tmp_path, batch_id)),
    )
    monkeypatch.setattr(fleet, "controller_matches", lambda *_args: True)
    observed_flushes = []

    class Recorder:
        def __init__(self, _home, _client):
            pass

        def try_record(self, *_args, **_kwargs):
            return None

        def flush(self, **kwargs):
            observed_flushes.append(kwargs)
            return 0

    monkeypatch.setattr(runloop, "FlightRecorder", Recorder)
    calls = []

    def popen(command, env, **kwargs):
        is_first = not calls

        def publish_abort():
            if is_first:
                runloop.Path(env[runloop._POOL_ABORT_ENV]).write_text(marker)

        process = _ScriptedProcess(
            command,
            env,
            [0],
            on_poll=publish_abort,
            mark_activity=False,
            **kwargs,
        )
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    result = runloop._run_worker_pool(_args(
        workers=2,
        fleet_pool=True,
        batch_id=batch_id,
        credentials_file="/private/plan.json",
    ))

    startup = fleet._read_json(fleet._pool_startup_path(tmp_path, batch_id))
    assert result == 1
    assert observed_flushes
    assert all(item == {"batch_id": batch_id} for item in observed_flushes)
    assert startup["status"] == "failed"
    assert startup["error_code"] == error_code
    assert message_fragment in startup["user_message"]


def test_egress_probe_timeout_is_the_user_visible_startup_failure(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    batch_id = "550e8400e29b41d4a716446655440000"
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setenv(fleet.CONTROLLER_ID_ENV, "controller-1")
    monkeypatch.setenv(fleet.POOL_BATCH_ENV, batch_id)
    monkeypatch.setenv(
        fleet.POOL_STARTUP_FILE_ENV,
        str(fleet._pool_startup_path(tmp_path, batch_id)),
    )
    monkeypatch.setattr(fleet, "controller_matches", lambda *_args: True)

    runloop._publish_fleet_startup_failure(
        _args(workers=2, fleet_pool=True, batch_id=batch_id),
        runloop.RunnerError(
            "Pier egress environment is not ready: the Pier egress preflight "
            "made no ready transition within 120 seconds; no task was started"
        ),
    )

    startup = fleet._read_json(fleet._pool_startup_path(tmp_path, batch_id))
    assert startup["status"] == "failed"
    assert startup["error_code"] == "egress_probe_timeout"
    assert "120 秒内仍未完成启动" in startup["user_message"]
    assert "题目没有开始执行" in startup["user_message"]


def test_transient_session_capacity_failure_keeps_short_backoff(
        monkeypatch, capsys):
    """One admission race recovers quickly while a healthy sibling runs."""
    _patch_pool_setup(monkeypatch, active_count=2)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    delays = []
    monkeypatch.setattr(
        runloop, "_pool_backfill_delay",
        lambda attempt: delays.append(attempt) or 0,
    )
    ready = iter((1, 0))
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count",
        lambda _client, **_kwargs: next(ready, 0),
    )
    calls = []

    def popen(command, env, **kwargs):
        if not calls:
            process = _ScriptedProcess(command, env, [None, 0], **kwargs)
        elif len(calls) == 1:
            process = _ScriptedProcess(
                command, env, [1], mark_activity=False, **kwargs,
            )
            runloop.Path(
                env[runloop._POOL_WORKER_ACTIVITY_ENV]
            ).write_text("preparing:runner_session_capacity_reached")
        else:
            process = _ScriptedProcess(command, env, [0], **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=2)) == 1
    assert [p.env["DRADAR_WORKER_INDEX"] for p in calls] == ["1", "2", "2"]
    assert delays == [1]
    output = capsys.readouterr().out
    assert "runner session capacity" in output
    assert "bounded backoff (1/3)" in output
    assert "server freshness window" not in output
    assert "bounded replacement is exhausted" not in output


def test_repeated_session_capacity_failure_cools_down_then_recovers(
        monkeypatch, capsys):
    """Three capacity conflicts cool down without permanently suppressing."""
    _patch_pool_setup(monkeypatch, active_count=1)
    now = [0.0]
    monkeypatch.setattr(runloop.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        runloop.time, "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    monkeypatch.setattr(runloop, "_POOL_SESSION_CAPACITY_RETRY_SECONDS", 5)
    calls = []
    spawn_times = []
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count",
        lambda _client, **_kwargs: 1 if len(calls) < 4 else 0,
    )

    def popen(command, env, **kwargs):
        spawn_times.append(now[0])
        if len(calls) < 3:
            process = _ScriptedProcess(
                command, env, [1], mark_activity=False, **kwargs,
            )
            runloop.Path(
                env[runloop._POOL_WORKER_ACTIVITY_ENV]
            ).write_text("preparing:runner_session_capacity_reached")
        else:
            process = _ScriptedProcess(command, env, [0], **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=1)) == 1
    assert len(calls) == 4
    assert spawn_times[1] - spawn_times[0] >= 2
    assert spawn_times[2] - spawn_times[1] >= 4
    assert spawn_times[3] - spawn_times[2] >= 5
    output = capsys.readouterr().out
    assert "bounded backoff (1/3)" in output
    assert "bounded backoff (2/3)" in output
    assert "server freshness window" in output
    assert "bounded replacement is exhausted" not in output


def test_session_capacity_exit_marks_worker_as_safe_precheckout_failure(
        tmp_path, monkeypatch):
    marker = tmp_path / "worker.started"
    marker.write_text("preparing")
    monkeypatch.setenv(runloop._POOL_WORKER_ACTIVITY_ENV, str(marker))

    with pytest.raises(SystemExit, match="runner_session_capacity_reached"):
        runloop._exit_for(runloop.ApiError(
            "server returned 409: runner session capacity reached",
            status_code=409,
            code="runner_session_capacity_reached",
        ))

    assert marker.read_text() == "preparing:runner_session_capacity_reached"
    assert list(tmp_path.glob("worker.started.*.tmp")) == []


def test_successful_mark_stopped_records_exact_returned_assignment(
        tmp_path, monkeypatch):
    marker = tmp_path / "worker.started"
    marker.write_text("assignment-1")
    monkeypatch.setenv(runloop._POOL_WORKER_ACTIVITY_ENV, str(marker))

    class Client:
        def mark_stopped(self, assignment_id, **kwargs):
            assert assignment_id == "assignment-1"
            assert kwargs["defer_seconds"] == 300
            assert kwargs["owner_epoch"] == 4
            assert kwargs["session_id"] == "session-1"
            return {"ok": True}

    assert runloop._mark_stopped_quietly(
        Client(), {
            "assignment_id": "assignment-1",
            "owner_epoch": 4,
            "_runner_session_id": "session-1",
        },
    )
    assert marker.read_text() == "waiting:assignment-1"


def test_task_failure_after_checkout_keeps_anti_cascade_cutoff(
        monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=2)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    seen_cutoffs = []
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count",
        lambda _client, **kwargs: seen_cutoffs.append(
            kwargs.get("claimed_after")
        ) or 0,
    )
    calls = []

    def popen(command, env, **kwargs):
        process = _ScriptedProcess(
            command, env, [1] if not calls else [None, 0], **kwargs,
        )
        # The nonzero child represents a provider/task failure after checkout.
        if not calls:
            runloop.Path(env[runloop._POOL_WORKER_ACTIVITY_ENV]).write_text("a1")
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=2)) == 1
    assert len(calls) == 2
    assert seen_cutoffs and seen_cutoffs[0] is not None
    assert "existing waiting work is frozen" in capsys.readouterr().out


def test_server_confirmed_stopped_assignment_backfills_after_retry_time(
        monkeypatch, capsys):
    """A stopped child may refill only its exact authoritative waiting row."""
    old_lease = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    returned = {
        "assignment_id": "returned",
        "leased_at": old_lease,
        "started_at": None,
        "execution_state": "waiting",
        "checkpoint_id": None,
        "retry_after": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "heartbeat_running": False,
        "runner_state": "waiting",
        "runner_phase": None,
    }

    class Client:
        def __init__(self):
            self.calls = 0

        def get_assignment(self):
            self.calls += 1
            if self.calls == 1:
                return {"active": [returned], "free_pick": True}
            return {"active": [], "free_pick": True}

    client = Client()
    _patch_pool_setup(monkeypatch, active_count=2)
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: client)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    calls = []

    def popen(command, env, **kwargs):
        if not calls:
            process = _ScriptedProcess(command, env, [None, 0], **kwargs)
        elif len(calls) == 1:
            process = _ScriptedProcess(
                command, env, [1], mark_activity=False, **kwargs,
            )
            runloop.Path(
                env[runloop._POOL_WORKER_ACTIVITY_ENV]
            ).write_text("waiting:returned")
        else:
            process = _ScriptedProcess(command, env, [0], **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=2)) == 1
    assert [p.env["DRADAR_WORKER_INDEX"] for p in calls] == ["1", "2", "2"]
    assert calls[2].env[
        runloop._POOL_RETURNED_ASSIGNMENTS_SNAPSHOT_ENV
    ] == '["returned"]'
    output = capsys.readouterr().out
    assert "existing waiting work is frozen" in output
    assert "restoring worker slot 2/2" in output


def test_backfill_v2_kill_switch_restores_previous_last_exit_behavior(
        monkeypatch):
    _patch_pool_setup(monkeypatch, active_count=1)
    monkeypatch.setenv("DRADAR_POOL_BACKFILL_V2", "0")
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count",
        lambda *_a, **_k: pytest.fail("disabled reconciler must not inspect"),
    )
    calls = []
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: (
            calls.append(_Process(command, env, **kwargs)) or calls[-1]
        ),
    )

    assert runloop._run_worker_pool(_args(workers=1)) == 0
    assert len(calls) == 1


def test_pool_child_failure_keeps_sibling_and_backfills_new_held_work(
        monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=3)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runloop, "_pool_backfill_delay", lambda _attempt: 0)
    monkeypatch.setattr(
        runloop, "_signal_workers",
        lambda _processes: pytest.fail("failed pool must not signal siblings"),
    )
    ready = iter((1, 0))
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count",
        lambda _client, **_kwargs: next(ready, 0),
    )
    calls = []

    def popen(command, env, **kwargs):
        if not calls:
            polls = [1]
        elif len(calls) == 1:
            polls = [None, None, 0]
        else:
            polls = [0]
        process = _ScriptedProcess(command, env, polls, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=2)) == 1
    assert len(calls) == 3
    assert calls[1].polls == []
    assert calls[2].env["DRADAR_WORKER_INDEX"] == "1"
    output = capsys.readouterr().out
    assert "exited 1 before checkout" in output
    assert "restoring worker slot 1/2" in output


def test_pool_quarantines_uncertain_slot_without_freezing_siblings(
        monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=2)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    ready_after_quarantine = []
    ready_values = iter((1, 1, 0))

    def ready(_client, **kwargs):
        ready_after_quarantine.append(kwargs.get("claimed_after"))
        return next(ready_values, 0)

    monkeypatch.setattr(runloop, "_pool_ready_work_count", ready)
    calls = []

    def popen(command, env, **kwargs):
        polls = (
            [runloop._WORKER_SLOT_QUARANTINED_EXIT_CODE]
            if not calls else ([None, 0] if len(calls) == 1 else [0])
        )
        process = _ScriptedProcess(command, env, polls, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=2)) == 1
    assert len(calls) == 3
    assert ready_after_quarantine == [None, None, None]
    output = capsys.readouterr().out
    assert "worker 1 quarantined" in output
    assert "sibling workers will continue" in output
    assert "existing waiting work is frozen" not in output
    assert "restoring worker slot 1/2" not in output
    assert calls[2].env["DRADAR_WORKER_INDEX"] == "2"


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

            def on_poll():
                runloop.Path(env["DRADAR_POOL_ABORT_FILE"]).write_text(
                    "account stop",
                )
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
    assert "worker pool drained after account stop" in out


def test_environment_build_drain_keeps_siblings_and_returns_distinct_failure(
        monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=2)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count",
        lambda _client: pytest.fail("environment drain must not backfill"),
    )
    monkeypatch.setattr(
        runloop, "_signal_workers",
        lambda _processes: pytest.fail("environment drain must not signal siblings"),
    )
    calls = []

    def popen(command, env, **kwargs):
        if not calls:
            def mark_environment_drain():
                runloop.Path(env["DRADAR_POOL_ABORT_FILE"]).write_text(
                    "drain:environment build unavailable: registry timeout",
                )

            process = _ScriptedProcess(
                command,
                env,
                [runloop._ENVIRONMENT_BUILD_FAILED_EXIT_CODE],
                on_poll=mark_environment_drain,
                **kwargs,
            )
        else:
            process = _ScriptedProcess(command, env, [None, 0], **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    assert runloop._run_worker_pool(_args(workers=2)) == 78
    assert len(calls) == 2
    out = capsys.readouterr().out
    assert "active workers will finish" in out
    assert "shared environment build failure" in out


def test_scoped_plan_drain_with_lost_upload_settles_as_recoverable_failure(
    tmp_path, monkeypatch, capsys,
):
    """A stop never turns a lost first upload response into a clean stop."""

    _patch_pool_setup(monkeypatch, active_count=1)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        runloop, "_run_config", lambda _args: {
            "benchmark": runloop.DEFAULT_BENCHMARK,
            "run_plan_id": "plan-a",
            "run_plan_logical_session_id": "drl_same_device",
        },
    )
    monkeypatch.setattr(runloop, "_pool_ready_work_count", lambda *_a, **_k: 1)
    monkeypatch.setattr(
        runloop, "_signal_workers",
        lambda _processes: pytest.fail("a plan drain must not signal paid work"),
    )
    batch_id = "550e8400e29b41d4a716446655440000"
    pending_entries = []
    monkeypatch.setattr(
        runloop, "_pending_uploads_for_batch",
        lambda selected: list(pending_entries) if selected == batch_id else [],
    )
    retry_calls = []

    def retry(_client, *, batch_id=None):
        retry_calls.append(batch_id)
        return ["upload-failed"] if pending_entries else []

    monkeypatch.setattr(runloop, "_retry_pending_uploads", retry)
    calls = []

    def popen(command, env, **kwargs):
        def finish_after_stop():
            runloop.Path(env[runloop._POOL_WORKER_ACTIVITY_ENV]).write_text(
                "done-a",
            )
            pending_entries.append({
                "assignment_id": "done-a",
                "batch_id": batch_id,
            })
            runloop.Path(env[runloop._POOL_ABORT_ENV]).write_text(
                "drain:stopped on this device",
            )

        process = _ScriptedProcess(
            command, env, [1], on_poll=finish_after_stop, **kwargs,
        )
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)

    args = _scoped_refill_args()
    args.workers = 1
    assert runloop._run_worker_pool(args) == 1
    assert len(calls) == 1
    assert retry_calls == [batch_id, batch_id]
    assert "completed result still waiting to upload" in capsys.readouterr().out


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
        {"started_at": None, "execution_state": "preparing"}, now=now,
    )
    assert not runloop._assignment_is_ready_for_checkout(
        {"started_at": None, "checkpoint_id": "checkpoint"}, now=now,
    )
    assert not runloop._assignment_is_ready_for_checkout(
        {"started_at": None, "retry_after": "not-a-time"}, now=now,
    )


def test_returned_assignment_bypasses_old_lease_only_with_complete_safe_state():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=1)
    assignment = {
        "assignment_id": "returned",
        "leased_at": (cutoff - timedelta(minutes=1)).isoformat(),
        "started_at": None,
        "execution_state": "waiting",
        "checkpoint_id": None,
        "retry_after": (now - timedelta(seconds=1)).isoformat(),
        "heartbeat_running": False,
        "runner_state": "waiting",
        "runner_phase": None,
    }

    assert runloop._assignment_is_ready_for_checkout(
        assignment, now=now, claimed_after=cutoff,
        returned_assignment_ids={"returned"},
    )
    for field, unsafe in (
        ("started_at", now.isoformat()),
        ("execution_state", "running"),
        ("checkpoint_id", "checkpoint"),
        ("heartbeat_running", True),
        ("runner_state", "stale"),
        ("runner_phase", "running"),
    ):
        candidate = {**assignment, field: unsafe}
        assert not runloop._assignment_is_ready_for_checkout(
            candidate, now=now, claimed_after=cutoff,
            returned_assignment_ids={"returned"},
        ), field
    assert not runloop._assignment_is_ready_for_checkout(
        {**assignment, "retry_after": (now + timedelta(seconds=1)).isoformat()},
        now=now, claimed_after=cutoff,
        returned_assignment_ids={"returned"},
    )
    assert not runloop._assignment_is_ready_for_checkout(
        assignment, now=now, claimed_after=cutoff,
        returned_assignment_ids=set(),
    )


def test_degraded_pool_only_accepts_assignments_claimed_after_failure():
    cutoff = datetime.now(timezone.utc)
    before = (cutoff - timedelta(seconds=1)).isoformat()
    after = (cutoff + timedelta(seconds=1)).isoformat()

    assert not runloop._assignment_is_ready_for_checkout(
        {"started_at": None, "leased_at": before},
        claimed_after=cutoff,
    )
    assert runloop._assignment_is_ready_for_checkout(
        {"started_at": None, "leased_at": after},
        claimed_after=cutoff,
    )
    assert not runloop._assignment_is_ready_for_checkout(
        {"started_at": None}, claimed_after=cutoff,
    )


def test_surviving_worker_excludes_pre_failure_inventory(
    tmp_path, monkeypatch,
):
    cutoff = datetime.now(timezone.utc)
    cutoff_file = tmp_path / "failure-cutoff"
    cutoff_file.write_text(cutoff.isoformat())
    monkeypatch.setenv("DRADAR_POOL_FAILURE_CUTOFF_FILE", str(cutoff_file))

    class Client:
        def get_assignment(self):
            return {"active": [
                {
                    "assignment_id": "old",
                    "started_at": None,
                    "leased_at": (cutoff - timedelta(seconds=1)).isoformat(),
                },
                {
                    "assignment_id": "new",
                    "started_at": None,
                    "leased_at": (cutoff + timedelta(seconds=1)).isoformat(),
                },
            ]}

    assert runloop._pool_degraded_exclusions(Client()) == {"old"}


def test_replacement_worker_accepts_exact_returned_pre_failure_assignment(
        tmp_path, monkeypatch,
):
    """The child must receive the safe-return proof used to spawn it.

    Regression for 0.5.142: the parent counted this row as ready, but the
    replacement child's degraded checkout filter excluded the same row and
    exited without checkout until its slot was permanently suppressed.
    """
    cutoff = datetime.now(timezone.utc)
    cutoff_file = tmp_path / "failure-cutoff"
    cutoff_file.write_text(cutoff.isoformat())
    returned_file = tmp_path / "returned.json"
    runloop._write_pool_returned_assignments(returned_file, {"returned"})
    monkeypatch.setenv(runloop._POOL_FAILURE_CUTOFF_ENV, str(cutoff_file))
    monkeypatch.setenv(
        runloop._POOL_RETURNED_ASSIGNMENTS_ENV, str(returned_file),
    )
    before = (cutoff - timedelta(seconds=1)).isoformat()
    ready = (cutoff - timedelta(milliseconds=1)).isoformat()

    class Client:
        def get_assignment(self):
            return {"active": [
                {
                    "assignment_id": "returned",
                    "started_at": None,
                    "leased_at": before,
                    "execution_state": "waiting",
                    "checkpoint_id": None,
                    "retry_after": ready,
                    "heartbeat_running": False,
                    "runner_state": "waiting",
                    "runner_phase": None,
                },
                {
                    "assignment_id": "unrelated-old",
                    "started_at": None,
                    "leased_at": before,
                    "execution_state": "waiting",
                    "checkpoint_id": None,
                    "retry_after": ready,
                    "heartbeat_running": False,
                    "runner_state": "waiting",
                    "runner_phase": None,
                },
            ]}

    assert runloop._pool_degraded_exclusions(Client()) == {"unrelated-old"}


def test_malformed_returned_assignment_proof_fails_closed(tmp_path, monkeypatch):
    returned_file = tmp_path / "returned.json"
    returned_file.write_text('["safe", "../unsafe"]')
    monkeypatch.setenv(
        runloop._POOL_RETURNED_ASSIGNMENTS_ENV, str(returned_file),
    )

    assert runloop._read_pool_returned_assignments() == set()


def test_replacement_uses_parent_proof_snapshot_when_windows_file_probe_fails(
        tmp_path, monkeypatch,
):
    returned_file = tmp_path / "returned.json"
    monkeypatch.setenv(
        runloop._POOL_RETURNED_ASSIGNMENTS_ENV, str(returned_file),
    )
    monkeypatch.setenv(
        runloop._POOL_RETURNED_ASSIGNMENTS_SNAPSHOT_ENV, '["returned"]',
    )
    monkeypatch.setattr(
        runloop.Path, "is_file",
        lambda _self: (_ for _ in ()).throw(PermissionError("sharing violation")),
    )

    assert runloop._read_pool_returned_assignments() == {"returned"}


def test_malformed_parent_proof_snapshot_fails_closed(monkeypatch):
    monkeypatch.setenv(
        runloop._POOL_RETURNED_ASSIGNMENTS_SNAPSHOT_ENV,
        '["returned", "../unsafe"]',
    )

    assert runloop._read_pool_returned_assignments() == set()


def test_backfill_counts_only_fresh_waiting_work():
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

    assert runloop._pool_ready_work_count(Client()) == 1


def test_backfill_never_spawns_when_batch_live_count_meets_target(monkeypatch):
    class Client:
        def get_assignment(self):
            return {"active": [
                {
                    "assignment_id": "live-1", "started_at": "earlier",
                    "heartbeat_running": True,
                },
                {
                    "assignment_id": "live-2", "started_at": "earlier",
                    "heartbeat_running": True,
                },
                {
                    "assignment_id": "waiting", "started_at": None,
                    "heartbeat_running": False,
                },
            ]}

    assert runloop._pool_ready_work_count(
        Client(), desired_workers=2,
    ) == 0
    assert runloop._pool_ready_work_count(
        Client(), desired_workers=3,
    ) == 1


def test_backfill_queue_read_fails_closed(monkeypatch, capsys):
    class Client:
        def get_assignment(self):
            raise runloop.ApiError("network unavailable")

    assert runloop._pool_ready_work_count(Client()) is None
    assert "keeping current workers only" in capsys.readouterr().out


@pytest.mark.parametrize("code", ("claim_batch_not_found", None))
def test_scoped_backfill_treats_finished_batch_404_as_clean_drain(code):
    class Client:
        batch_id = "550e8400e29b41d4a716446655440000"

        def get_assignment(self):
            raise runloop.ApiError(
                "server returned 404: active claim batch not found",
                status_code=404, code=code,
            )

    assert runloop._pool_ready_work_count(Client()) == 0


@pytest.mark.parametrize("batch_id", (None, "550e8400e29b41d4a716446655440000"))
def test_backfill_does_not_swallow_unrelated_404(batch_id, capsys):
    class Client:
        def __init__(self):
            self.batch_id = batch_id

        def get_assignment(self):
            raise runloop.ApiError(
                "server returned 404: unrelated route missing",
                status_code=404,
            )

    assert runloop._pool_ready_work_count(Client()) is None
    assert "keeping current workers only" in capsys.readouterr().out


def test_rolling_server_local_batch_filter_empty_is_clean_supervisor_drain():
    selected = "550e8400e29b41d4a716446655440000"

    def handler(_request):
        # Models an old server ignoring batch_id and returning only another
        # campaign. ApiClient synthesizes the exact claim_batch_not_found 404.
        return httpx.Response(200, json={"active": [{
            "assignment_id": "other",
            "batch_id": "123e4567e89b12d3a456426614174000",
        }]})

    client = ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(handler), batch_id=selected,
    )
    assert runloop._pool_ready_work_count(client) == 0


def test_last_worker_completion_followed_by_batch_404_exits_cleanly(
        monkeypatch):
    _patch_pool_setup(monkeypatch, active_count=1)

    class Client:
        batch_id = "550e8400e29b41d4a716446655440000"

        def set_batch_id(self, value):
            self.batch_id = value

        def get_assignment(self):
            raise runloop.ApiError(
                "server returned 404: active claim batch not found",
                status_code=404,
            )

    client = Client()
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: client)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    calls = []
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: (
            calls.append(_ScriptedProcess(command, env, [0], **kwargs))
            or calls[-1]
        ),
    )

    assert runloop._run_worker_pool(_args(
        workers=1, batch_id=client.batch_id,
    )) == 0
    assert len(calls) == 1


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
