import argparse
import io
import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from dradar import cli, fleet, runloop
from dradar.capacity import CapacityReport


BATCH_A = "550e8400e29b41d4a716446655440000"
BATCH_B = "6ba7b8109dad11d180b400c04fd430c8"


@pytest.fixture(autouse=True)
def _release_process_locks():
    yield
    fleet.release_pool_locks_for_tests()


def test_cli_parses_agent_facing_fleet_add(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_fleet_add", lambda args: seen.append(args) or 0)

    assert cli.main([
        "fleet", "add",
        "--batch-id", "550E8400-E29B-41D4-A716-446655440000",
        "--workers", "2",
    ]) == 0

    assert seen[0].batch_id == BATCH_A
    assert seen[0].workers == 2


def test_fleet_help_teaches_public_commands_without_internal_serve(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["fleet", "--help"])

    assert stopped.value.code == 0
    output = capsys.readouterr().out
    assert "{add,status,watch,stop}" in output
    assert "idempotently add one exact claimed batch" in output
    assert "serve" not in output
    assert "SUPPRESS" not in output


def test_cli_parses_exact_post_seed_refill_campaign(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_fleet_add", lambda args: seen.append(args) or 0)

    assert cli.main([
        "fleet", "add", "--batch-id", BATCH_A, "--workers", "2",
        "--refill", "--max-tasks", "10",
        "--refill-harness", "kimi-code",
        "--refill-model", "kimi-k2.5",
        "--refill-effort", "high",
    ]) == 0

    assert seen[0].refill is True
    assert seen[0].max_tasks == 10
    assert seen[0].refill_harness == "kimi-code"
    assert seen[0].refill_model == "kimi-k2.5"
    assert seen[0].refill_effort == "high"


def test_fleet_add_is_idempotent_for_one_batch(tmp_path, monkeypatch):
    fleet._prepare_dirs(tmp_path)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    processes = {}
    logs = {}
    spawned = []

    class Process:
        pid = 1234

        def poll(self):
            return None

        def send_signal(self, _signal):
            pass

    monkeypatch.setattr(
        fleet, "_resolve_workers",
        lambda *_args: (2, [], {"account_limit": 5, "held_tasks": 2}),
    )

    def spawn(*_args, **_kwargs):
        spawned.append(True)
        return Process(), io.StringIO()

    monkeypatch.setattr(fleet, "_spawn_pool", spawn)
    first = {
        "request_id": "request-1", "controller_id": "controller-1",
        "controller_protocol_version": fleet.CONTROLLER_PROTOCOL_VERSION,
        "runtime_executable": sys.executable,
        "command": "add", "batch_id": BATCH_A, "workers": 2,
    }
    second = dict(first, request_id="request-2")

    fleet._handle_request(tmp_path, state, processes, logs, first)
    fleet._handle_request(tmp_path, state, processes, logs, second)

    assert spawned == [True]
    assert set(processes) == {BATCH_A}
    response = json.loads(
        (fleet._root(tmp_path) / fleet.RESPONSE_DIR / "request-2.json").read_text()
    )
    assert response["ok"] is True
    assert response["already_active"] is True
    assert response["batch"]["workers"] == 2


def test_later_harness_pool_uses_only_its_requesting_agent_executable_paths(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    executable = tmp_path / "codebuddy"
    executable.write_text("binary")
    managed_home = tmp_path / "codebuddy-managed"
    managed_home.mkdir(mode=0o700)
    kimi_credential = tmp_path / "kcode" / "credentials" / "kimi-code.json"
    kimi_credential.parent.mkdir(parents=True)
    kimi_credential.write_text("{}")
    kimi_credential.chmod(0o600)
    captured = {}

    class Process:
        pid = 1234

        def poll(self):
            return None

    monkeypatch.setattr(
        fleet, "_resolve_workers", lambda *_args: (1, [], {"account_limit": 5}),
    )

    def spawn(*_args, **kwargs):
        captured.update(kwargs.get("runtime_environment") or {})
        return Process(), io.StringIO()

    monkeypatch.setattr(fleet, "_spawn_pool", spawn)
    fleet._handle_request(tmp_path, state, {}, {}, {
        "request_id": "provider-path-request",
        "controller_id": "controller-1",
        "controller_protocol_version": fleet.CONTROLLER_PROTOCOL_VERSION,
        "runtime_executable": sys.executable,
        "runtime_environment": {
            "CODEBUDDY_CLI_PATH": str(executable),
            fleet.CODEBUDDY_MANAGED_HOME_ENV: str(managed_home),
            fleet.KIMI_CREDENTIAL_PATH_ENV: str(kimi_credential),
        },
        "command": "add",
        "batch_id": BATCH_A,
        "workers": 1,
    })

    assert captured == {
        "CODEBUDDY_CLI_PATH": str(executable),
        fleet.CODEBUDDY_MANAGED_HOME_ENV: str(managed_home),
        fleet.KIMI_CREDENTIAL_PATH_ENV: str(kimi_credential),
    }
    persisted = json.loads(fleet._state_path(tmp_path).read_text())
    assert "runtime_environment" not in json.dumps(persisted)


def test_provider_runtime_environment_rejects_secrets_and_unknown_keys(tmp_path):
    executable = tmp_path / "codebuddy"
    executable.write_text("binary")
    selected = fleet._pool_executable_environment({
        "CODEBUDDY_CLI_PATH": str(executable),
        "DEEPSEEK_API_KEY": "secret-value",
        "UNKNOWN_PROVIDER_VALUE": str(executable),
    })
    assert selected == {"CODEBUDDY_CLI_PATH": str(executable)}


def test_provider_runtime_environment_carries_only_private_codebuddy_home(tmp_path):
    private = tmp_path / "private-codebuddy"
    private.mkdir(mode=0o700)
    public = tmp_path / "public-codebuddy"
    public.mkdir(mode=0o755)

    assert fleet._pool_executable_environment({
        fleet.CODEBUDDY_MANAGED_HOME_ENV: str(private),
    }) == {fleet.CODEBUDDY_MANAGED_HOME_ENV: str(private)}
    assert fleet._pool_executable_environment({
        fleet.CODEBUDDY_MANAGED_HOME_ENV: str(public),
    }) == {}


def test_provider_runtime_environment_carries_only_private_kimi_credential(
    tmp_path,
):
    private = tmp_path / "kcode" / "credentials" / "kimi-code.json"
    private.parent.mkdir(parents=True)
    private.write_text("{}")
    private.chmod(0o600)

    assert fleet._pool_executable_environment({
        fleet.KIMI_CREDENTIAL_PATH_ENV: str(private),
    }) == {fleet.KIMI_CREDENTIAL_PATH_ENV: str(private)}

    private.chmod(0o644)
    assert fleet._pool_executable_environment({
        fleet.KIMI_CREDENTIAL_PATH_ENV: str(private),
    }) == {}

    private.chmod(0o600)
    link = tmp_path / "credential-link.json"
    link.symlink_to(private)
    assert fleet._pool_executable_environment({
        fleet.KIMI_CREDENTIAL_PATH_ENV: str(link),
    }) == {}


def test_fleet_tracks_separate_honeypot_batches_and_total_workers(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    processes = {}
    logs = {}

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

        def send_signal(self, _signal):
            pass

    worker_targets = iter((2, 3))
    monkeypatch.setattr(
        fleet, "_resolve_workers",
        lambda *_args: (next(worker_targets), [], {"account_limit": 5}),
    )
    pids = iter((111, 222))
    monkeypatch.setattr(
        fleet, "_spawn_pool",
        lambda *_args, **_kwargs: (Process(next(pids)), io.StringIO()),
    )

    for request_id, batch_id in (("a", BATCH_A), ("b", BATCH_B)):
        fleet._handle_request(tmp_path, state, processes, logs, {
            "request_id": request_id,
            "controller_id": "controller-1",
            "controller_protocol_version": fleet.CONTROLLER_PROTOCOL_VERSION,
            "runtime_executable": sys.executable,
            "command": "add",
            "batch_id": batch_id,
            "workers": "auto",
        })

    persisted = json.loads(fleet._state_path(tmp_path).read_text())
    assert persisted["total_workers"] == 5
    assert set(persisted["batches"]) == {BATCH_A, BATCH_B}
    assert persisted["batches"][BATCH_A]["workers"] == 2
    assert persisted["batches"][BATCH_B]["workers"] == 3


def test_controller_liveness_requires_process_lifetime_lease_not_reused_pid(
    tmp_path, monkeypatch,
):
    state = fleet._initial_state("controller-reused-pid", None)
    state["status"] = "active"
    fleet._write_state(tmp_path, state)
    monkeypatch.setattr(fleet, "_pid_alive", lambda _pid: True)

    # A live-looking/reused PID and fresh heartbeat are insufficient without
    # the exact controller lease held by the controller process.
    assert fleet.controller_is_active(tmp_path) is False

    with fleet._controller_lease(tmp_path, "controller-reused-pid"):
        assert fleet.controller_is_active(tmp_path) is True


def test_new_batch_waits_for_active_legacy_controller_without_interrupting(
    tmp_path, monkeypatch,
):
    state = fleet._initial_state("legacy-controller", None)
    state.pop("controller_protocol_version")
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "status": "running",
        "workers": 2,
    }
    fleet._write_state(tmp_path, state)
    monkeypatch.setattr(fleet, "controller_is_active", lambda _home: True)
    monkeypatch.setattr(
        fleet.os,
        "kill",
        lambda *_args: pytest.fail("an active legacy run must not be interrupted"),
    )

    with pytest.raises(
        fleet.FleetControllerUpdatePending,
        match="现有题目",
    ):
        fleet.prepare_new_batch_runtime(tmp_path)


@pytest.mark.parametrize("old_protocol", (None, 6))
def test_idle_legacy_controller_is_rotated_without_user_decision(
    tmp_path, monkeypatch, old_protocol,
):
    state = fleet._initial_state("legacy-controller", None)
    state["controller_protocol_version"] = old_protocol
    state["status"] = "active"
    fleet._write_state(tmp_path, state)
    live = {"value": True}
    signals = []
    monkeypatch.setattr(
        fleet, "controller_is_active", lambda _home: live["value"],
    )

    def stop(pid, signum):
        signals.append((pid, signum))
        live["value"] = False

    monkeypatch.setattr(fleet.os, "kill", stop)

    fleet.prepare_new_batch_runtime(tmp_path)

    assert signals == [(state["pid"], signal.SIGTERM)]


def test_legacy_add_request_cannot_make_current_controller_spawn_old_runtime(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    monkeypatch.setattr(
        fleet,
        "_spawn_pool",
        lambda *_args, **_kwargs: pytest.fail("legacy add must be rejected"),
    )

    fleet._handle_request(tmp_path, state, {}, {}, {
        "request_id": "legacy-add",
        "controller_id": "controller-1",
        "command": "add",
        "batch_id": BATCH_A,
        "workers": 1,
    })

    response = json.loads(
        (fleet._root(tmp_path) / fleet.RESPONSE_DIR / "legacy-add.json").read_text()
    )
    assert response["ok"] is False
    assert response["error_code"] == "local_runtime_update_pending"


def test_dead_controller_without_pool_lock_exposes_interrupted_zero_reservation(
    tmp_path,
):
    state = fleet._initial_state("dead-controller", None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "status": "running",
        "workers": 3,
        "plan_id": "plan-dead",
    }
    fleet._write_state(tmp_path, state)

    public = fleet._public_state(tmp_path)

    assert public["active"] is False
    assert public["batches"][BATCH_A]["status"] == "interrupted"
    assert public["total_workers"] == 0
    assert fleet.batch_status(BATCH_A, home=tmp_path)["status"] == "interrupted"
    assert fleet.reserved_workers(tmp_path) == 0


def test_dead_controller_with_live_pool_lock_keeps_orphan_reservation_and_credential(
    tmp_path,
):
    credentials = tmp_path / "run-plans" / "plan-orphan.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("{}")
    state = fleet._initial_state("dead-controller", None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "status": "running",
        "workers": 2,
        "plan_id": "plan-orphan",
        "credentials_file": str(credentials),
    }
    fleet._write_state(tmp_path, state)
    fleet.acquire_pool_lock(tmp_path, BATCH_A, "dead-controller")

    public = fleet._public_state(tmp_path)

    assert public["active"] is False
    assert public["batches"][BATCH_A]["status"] == "orphaned"
    assert public["total_workers"] == 2
    assert fleet.reserved_workers(tmp_path) == 2
    assert fleet.credentials_file_in_use(credentials, home=tmp_path) is True


def test_auto_workers_subtract_existing_machine_reservations(monkeypatch):
    class Client:
        def set_batch_id(self, value):
            self.batch_id = value

    report = CapacityReport(
        recommended_workers=3,
        docker_cpus=12,
        docker_memory_gib=32,
        disk_free_gib=100,
        account_limit=5,
        held_tasks=3,
        task_limit=3,
        cpu_limit=6,
        memory_limit=5,
        disk_limit=7,
    )
    monkeypatch.setattr(fleet, "_load_config", lambda: {})
    monkeypatch.setattr(fleet, "_client", lambda _cfg: Client())
    monkeypatch.setattr(fleet, "inspect_capacity", lambda _client: report)
    monkeypatch.setattr(fleet, "docker_resources", lambda: (12, 32, ()))
    state = {
        "batches": {
            BATCH_A: {"status": "running", "workers": 2},
        }
    }

    workers, warnings, metadata = fleet._resolve_workers("auto", BATCH_B, state)

    assert workers == 2  # global automatic cap 4 minus the existing two
    assert warnings == []
    assert metadata["reserved_before"] == 2


@pytest.mark.parametrize("probe_state", ("small", "timeout", "unavailable"))
def test_fixed_fleet_workers_ignore_resource_estimates_and_local_reservations(
    monkeypatch, probe_state,
):
    class Client:
        def set_batch_id(self, value):
            assert value == BATCH_B

        def whoami(self):
            return {"concurrent_limit": 5}

        def get_assignment(self):
            return {"active": [{"assignment_id": str(i)} for i in range(5)]}

    probes = []

    def probe(*_args, **_kwargs):
        probes.append(probe_state)
        if probe_state == "timeout":
            raise subprocess.TimeoutExpired("docker info", 10)
        if probe_state == "unavailable":
            raise OSError("Docker resource probe is unavailable")
        return 1, 1.0, ()

    monkeypatch.setattr(fleet, "_load_config", lambda: {})
    monkeypatch.setattr(fleet, "_client", lambda _cfg: Client())
    monkeypatch.setattr(fleet, "inspect_capacity", probe)
    monkeypatch.setattr(fleet, "docker_resources", probe)
    state = {"batches": {BATCH_A: {"status": "running", "workers": 40}}}

    workers, warnings, metadata = fleet._resolve_workers(5, BATCH_B, state)

    assert workers == 5
    assert warnings == []
    assert probes == []
    assert metadata["reserved_before"] == 40
    assert metadata["account_limit"] == 5
    assert metadata["held_tasks"] == 5
    assert metadata["docker_cpus"] is None


@pytest.mark.parametrize("workers", (0, 41, 6))
def test_fixed_fleet_workers_still_reject_invalid_or_unauthorized_count(
    monkeypatch, workers,
):
    client = argparse.Namespace(
        set_batch_id=lambda _batch: None,
        whoami=lambda: {"concurrent_limit": 5},
        get_assignment=lambda: {"active": []},
    )
    monkeypatch.setattr(fleet, "_load_config", lambda: {})
    monkeypatch.setattr(fleet, "_client", lambda _cfg: client)
    monkeypatch.setattr(fleet, "inspect_capacity", lambda *_args: (
        pytest.fail("fixed workers cannot use resource estimates")
    ))

    with pytest.raises(fleet.FleetError):
        fleet._resolve_workers(workers, BATCH_B, {"batches": {}})


def test_pool_command_is_exact_batch_resume_without_claim_or_refill(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    captured = {}

    class Process:
        pid = 123

    def popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(fleet.subprocess, "Popen", popen)
    monkeypatch.setattr("dradar.machine._lock_handle", None)
    monkeypatch.setenv("CODEBUDDY_CLI_PATH", "/stale/controller/codebuddy")
    requested_grok = tmp_path / "grok"
    requested_grok.write_text("binary")
    state = {"controller_id": "controller-1"}

    _process, log = fleet._spawn_pool(
        tmp_path, state, BATCH_A, 2,
        runtime_environment={"GROK_CLI_PATH": str(requested_grok)},
    )
    log.close()

    assert captured["command"][3:5] == ["resume", "-y"]
    assert captured["command"][captured["command"].index("--batch-id") + 1] == BATCH_A
    assert captured["command"][captured["command"].index("--workers") + 1] == "2"
    assert "--refill" not in captured["command"]
    assert "--auto" not in captured["command"]
    assert captured["env"][fleet.POOL_BATCH_ENV] == BATCH_A
    assert "CODEBUDDY_CLI_PATH" not in captured["env"]
    assert captured["env"]["GROK_CLI_PATH"] == str(requested_grok)
    assert captured["env"][fleet.POOL_STARTUP_FILE_ENV] == str(
        fleet._pool_startup_path(tmp_path, BATCH_A)
    )


def test_pool_is_not_running_until_exact_parent_acknowledges_readiness(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    controller_id = "controller-1"
    state = fleet._initial_state(controller_id, None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "workers": 2,
        "status": "starting",
        "startup_status": "pending",
    }

    class Process:
        pid = os.getpid()

    monkeypatch.setenv(fleet.CONTROLLER_ID_ENV, controller_id)
    monkeypatch.setenv(fleet.POOL_BATCH_ENV, BATCH_A)
    monkeypatch.setenv(
        fleet.POOL_STARTUP_FILE_ENV,
        str(fleet._pool_startup_path(tmp_path, BATCH_A)),
    )
    monkeypatch.setattr(fleet, "controller_matches", lambda *_args: True)

    fleet.publish_pool_startup_ready(tmp_path, BATCH_A)
    fleet._refresh_pool_startups(tmp_path, state, {BATCH_A: Process()})

    assert state["batches"][BATCH_A]["status"] == "running"
    assert state["batches"][BATCH_A]["startup_status"] == "ready"
    assert state["batches"][BATCH_A]["ready_at"]


def test_startup_observation_budget_is_exactly_thirty_minutes():
    assert fleet.STARTUP_OBSERVE_SECONDS == 1800.0
    assert fleet.START_TIMEOUT_SECONDS == 20.0
    assert fleet.REQUEST_TIMEOUT_SECONDS == 60.0
    assert fleet.HEARTBEAT_SECONDS == 1.0
    assert fleet.HEARTBEAT_STALE_SECONDS == 60.0


def test_real_budget_accepts_worker_ready_immediately_before_1800(monkeypatch):
    clock = {"seconds": 0.0}
    stopped = []
    monkeypatch.setattr(fleet.time, "monotonic", lambda: clock["seconds"])
    monkeypatch.setattr(
        fleet.time, "sleep",
        lambda _seconds: clock.__setitem__("seconds", 1799.0),
    )

    def status(_batch_id):
        return {
            "batch_id": BATCH_A,
            "status": "running" if clock["seconds"] >= 1799.0 else "starting",
            "startup_status": "ready" if clock["seconds"] >= 1799.0 else "pending",
        }

    monkeypatch.setattr(fleet, "batch_status", status)
    monkeypatch.setattr(
        fleet, "stop_batch", lambda *_args, **_kwargs: stopped.append(True),
    )

    response = fleet._observe_pool_startup({
        "batch": {
            "batch_id": BATCH_A, "status": "starting", "startup_status": "pending",
        },
    }, BATCH_A)

    assert response["batch"]["startup_status"] == "ready"
    assert clock["seconds"] == 1799.0
    assert stopped == []


def test_real_budget_stops_only_after_1800_seconds(monkeypatch):
    clock = {"seconds": 0.0}
    stopped_at = []
    monkeypatch.setattr(fleet.time, "monotonic", lambda: clock["seconds"])
    monkeypatch.setattr(
        fleet.time, "sleep",
        lambda _seconds: clock.__setitem__("seconds", 1800.0),
    )
    monkeypatch.setattr(
        fleet, "batch_status", lambda _batch_id: {
            "batch_id": BATCH_A, "status": "starting", "startup_status": "pending",
        },
    )
    monkeypatch.setattr(
        fleet, "stop_batch",
        lambda batch_id, **kwargs: stopped_at.append(
            (clock["seconds"], batch_id, kwargs),
        ) or {
            "ok": True, "stopping": [batch_id], "condition_changed": [], "warnings": [],
        },
    )

    with pytest.raises(fleet.FleetStartupError) as raised:
        fleet._observe_pool_startup({
            "batch": {
                "batch_id": BATCH_A, "status": "starting", "startup_status": "pending",
            },
        }, BATCH_A)

    assert raised.value.code == "local_start_timeout"
    assert stopped_at == [(1800.0, BATCH_A, {"only_if_startup_pending": True})]


def test_real_budget_preserves_unconfirmed_stop_failure_at_1800(monkeypatch):
    clock = {"seconds": 0.0}
    monkeypatch.setattr(fleet.time, "monotonic", lambda: clock["seconds"])
    monkeypatch.setattr(
        fleet.time, "sleep",
        lambda _seconds: clock.__setitem__("seconds", 1800.0),
    )
    monkeypatch.setattr(
        fleet, "batch_status", lambda _batch_id: {
            "batch_id": BATCH_A, "status": "starting", "startup_status": "pending",
        },
    )
    monkeypatch.setattr(
        fleet, "stop_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            fleet.FleetError("coordinator unavailable")
        ),
    )

    with pytest.raises(fleet.FleetStartupError) as raised:
        fleet._observe_pool_startup({
            "batch": {
                "batch_id": BATCH_A, "status": "starting", "startup_status": "pending",
            },
        }, BATCH_A)

    assert clock["seconds"] == 1800.0
    assert raised.value.code == "local_start_timeout_stop_unconfirmed"
    assert raised.value.retryable is False


def test_startup_observation_times_out_as_failure_instead_of_returning_starting(
    monkeypatch,
):
    clock = {"seconds": 0.0}
    stopped = []
    monkeypatch.setattr(fleet, "STARTUP_OBSERVE_SECONDS", 0.1)
    monkeypatch.setattr(fleet.time, "monotonic", lambda: clock["seconds"])
    monkeypatch.setattr(
        fleet.time,
        "sleep",
        lambda seconds: clock.__setitem__("seconds", clock["seconds"] + seconds),
    )
    monkeypatch.setattr(
        fleet,
        "batch_status",
        lambda _batch_id: {
            "batch_id": BATCH_A,
            "status": "starting",
            "startup_status": "pending",
        },
    )
    monkeypatch.setattr(
        fleet,
        "stop_batch",
        lambda batch_id, **kwargs: stopped.append((batch_id, kwargs)) or {
            "ok": True,
            "stopping": [batch_id],
            "condition_changed": [],
            "warnings": [],
        },
    )

    with pytest.raises(fleet.FleetStartupError) as raised:
        fleet._observe_pool_startup({
            "batch": {
                "batch_id": BATCH_A,
                "status": "starting",
                "startup_status": "pending",
            },
        }, BATCH_A)

    assert raised.value.code == "local_start_timeout"
    assert "已确认停止请求，正在安全停止" in raised.value.user_message
    assert stopped == [(
        BATCH_A, {"only_if_startup_pending": True},
    )]


def test_startup_timeout_does_not_claim_stop_when_coordinator_rejects_it(
    monkeypatch,
):
    clock = {"seconds": 0.0}
    monkeypatch.setattr(fleet, "STARTUP_OBSERVE_SECONDS", 0.1)
    monkeypatch.setattr(fleet.time, "monotonic", lambda: clock["seconds"])
    monkeypatch.setattr(
        fleet.time,
        "sleep",
        lambda seconds: clock.__setitem__("seconds", clock["seconds"] + seconds),
    )
    monkeypatch.setattr(
        fleet,
        "batch_status",
        lambda _batch_id: {
            "batch_id": BATCH_A,
            "status": "starting",
            "startup_status": "pending",
        },
    )
    monkeypatch.setattr(
        fleet,
        "stop_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            fleet.FleetError("coordinator unavailable")
        ),
    )

    with pytest.raises(fleet.FleetStartupError) as raised:
        fleet._observe_pool_startup({
            "batch": {
                "batch_id": BATCH_A,
                "status": "starting",
                "startup_status": "pending",
            },
        }, BATCH_A)

    assert raised.value.code == "local_start_timeout_stop_unconfirmed"
    assert raised.value.retryable is False
    assert "无法确认" in raised.value.user_message
    assert "本地启动已停止" not in raised.value.user_message
    assert "dradar fleet status" in raised.value.user_message


def test_ready_winning_at_timeout_is_returned_instead_of_stopped(monkeypatch):
    clock = {"seconds": 0.0}
    calls = {"status": 0, "stop": 0}
    monkeypatch.setattr(fleet, "STARTUP_OBSERVE_SECONDS", 0.1)
    monkeypatch.setattr(fleet.time, "monotonic", lambda: clock["seconds"])
    monkeypatch.setattr(
        fleet.time,
        "sleep",
        lambda seconds: clock.__setitem__("seconds", clock["seconds"] + seconds),
    )

    def status(_batch_id):
        calls["status"] += 1
        if calls["status"] < 3:
            return {
                "batch_id": BATCH_A,
                "status": "starting",
                "startup_status": "pending",
            }
        return {
            "batch_id": BATCH_A,
            "status": "running",
            "startup_status": "ready",
        }

    monkeypatch.setattr(fleet, "batch_status", status)
    monkeypatch.setattr(
        fleet,
        "stop_batch",
        lambda *_args, **_kwargs: calls.__setitem__("stop", calls["stop"] + 1),
    )

    response = fleet._observe_pool_startup({
        "batch": {
            "batch_id": BATCH_A,
            "status": "starting",
            "startup_status": "pending",
        },
    }, BATCH_A)

    assert response["batch"]["startup_status"] == "ready"
    assert calls["stop"] == 0


def test_conditional_timeout_stop_cannot_interrupt_a_ready_parent(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    controller_id = "controller-1"
    state = fleet._initial_state(controller_id, None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "workers": 2,
        "status": "starting",
        "startup_status": "pending",
        "plan_id": "plan-a",
        "credentials_file": str(tmp_path / "plan-a.json"),
    }

    class Process:
        pid = os.getpid()

        def __init__(self):
            self.signals = []

        def poll(self):
            return None

        def send_signal(self, value):
            self.signals.append(value)

    process = Process()
    monkeypatch.setenv(fleet.CONTROLLER_ID_ENV, controller_id)
    monkeypatch.setenv(fleet.POOL_BATCH_ENV, BATCH_A)
    monkeypatch.setenv(
        fleet.POOL_STARTUP_FILE_ENV,
        str(fleet._pool_startup_path(tmp_path, BATCH_A)),
    )
    monkeypatch.setattr(fleet, "controller_matches", lambda *_args: True)
    fleet.publish_pool_startup_ready(tmp_path, BATCH_A)

    fleet._handle_request(
        tmp_path,
        state,
        {BATCH_A: process},
        {},
        {
            "request_id": "request-ready-race",
            "controller_id": controller_id,
            "command": "stop",
            "batch_id": BATCH_A,
            "all": False,
            "only_if_startup_pending": True,
        },
    )

    response = fleet._read_json(
        fleet._root(tmp_path) / fleet.RESPONSE_DIR / "request-ready-race.json"
    )
    assert response["stopping"] == []
    assert response["condition_changed"] == [BATCH_A]
    assert process.signals == []
    assert state["batches"][BATCH_A]["status"] == "running"
    assert state["batches"][BATCH_A]["startup_status"] == "ready"


def test_ready_stop_race_preserves_drain_and_clean_exit_zero(
    tmp_path, monkeypatch,
):
    """running -> safe stop -> natural completion must never become failed."""

    fleet._prepare_dirs(tmp_path)
    controller_id = "controller-1"
    state = fleet._initial_state(controller_id, None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "workers": 1,
        "status": "starting",
        "startup_status": "pending",
        "plan_id": "plan-a",
        "credentials_file": str(tmp_path / "plan-a.json"),
    }

    class Process:
        pid = os.getpid()

        def poll(self):
            return None

    process = Process()
    processes = {BATCH_A: process}
    logs = {BATCH_A: io.StringIO()}
    monkeypatch.setenv(fleet.CONTROLLER_ID_ENV, controller_id)
    monkeypatch.setenv(fleet.POOL_BATCH_ENV, BATCH_A)
    monkeypatch.setenv(
        fleet.POOL_STARTUP_FILE_ENV,
        str(fleet._pool_startup_path(tmp_path, BATCH_A)),
    )
    monkeypatch.setattr(fleet, "controller_matches", lambda *_args: True)
    monkeypatch.setattr(fleet, "_stop_run_plan_device", lambda *_args: None)
    monkeypatch.setattr(fleet, "_request_pool_drain", lambda *_args: None)

    # The child has checked out real work, but the coordinator has not yet
    # refreshed the ready file when the user's supported stop arrives.
    fleet.publish_pool_startup_ready(tmp_path, BATCH_A)
    fleet._handle_request(
        tmp_path, state, processes, logs,
        {
            "request_id": "request-stop-after-ready",
            "controller_id": controller_id,
            "command": "stop",
            "batch_id": BATCH_A,
            "all": False,
            "only_if_startup_pending": False,
        },
    )
    assert state["batches"][BATCH_A]["status"] == "stopping"

    fleet._refresh_pool_startups(tmp_path, state, processes)
    assert state["batches"][BATCH_A]["startup_status"] == "ready"
    assert state["batches"][BATCH_A]["status"] == "stopping"

    fleet._settle_pool(
        tmp_path, state, processes, logs, BATCH_A, 0,
    )
    item = state["batches"][BATCH_A]
    assert item["status"] == "stopped"
    assert item["startup_status"] == "ready"
    assert item["returncode"] == 0
    assert "startup_error_code" not in item

    monkeypatch.setattr(fleet, "batch_status", lambda _batch_id: item)
    observed = fleet._observe_pool_startup({
        "batch": {
            "batch_id": BATCH_A,
            "status": "starting",
            "startup_status": "pending",
        },
    }, BATCH_A)
    assert observed["batch"]["status"] == "stopped"


def test_conditional_timeout_reserves_failure_before_interrupting_parent(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    controller_id = "controller-1"
    state = fleet._initial_state(controller_id, None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "workers": 2,
        "status": "starting",
        "startup_status": "pending",
        "plan_id": "plan-a",
        "credentials_file": str(tmp_path / "plan-a.json"),
    }

    class Process:
        pid = os.getpid()

        def __init__(self):
            self.signals = []

        def poll(self):
            return None

        def send_signal(self, value):
            self.signals.append(value)

    process = Process()
    monkeypatch.setattr(fleet, "_stop_run_plan_device", lambda *_args: None)
    monkeypatch.setattr(fleet, "_request_pool_drain", lambda *_args: None)

    fleet._handle_request(
        tmp_path,
        state,
        {BATCH_A: process},
        {},
        {
            "request_id": "request-pending-timeout",
            "controller_id": controller_id,
            "command": "stop",
            "batch_id": BATCH_A,
            "all": False,
            "only_if_startup_pending": True,
        },
    )

    event = fleet._read_json(fleet._pool_startup_path(tmp_path, BATCH_A))
    response = fleet._read_json(
        fleet._root(tmp_path) / fleet.RESPONSE_DIR
        / "request-pending-timeout.json"
    )
    assert event["status"] == "failed"
    assert event["error_code"] == "local_start_timeout"
    assert response["stopping"] == [BATCH_A]
    assert response["condition_changed"] == []
    assert process.signals == [signal.SIGINT]
    assert state["batches"][BATCH_A]["status"] == "stopping"

    monkeypatch.setenv(fleet.CONTROLLER_ID_ENV, controller_id)
    monkeypatch.setenv(fleet.POOL_BATCH_ENV, BATCH_A)
    monkeypatch.setenv(
        fleet.POOL_STARTUP_FILE_ENV,
        str(fleet._pool_startup_path(tmp_path, BATCH_A)),
    )
    monkeypatch.setattr(fleet, "controller_matches", lambda *_args: True)
    with pytest.raises(fleet.FleetError, match="already marked failed"):
        fleet.publish_pool_startup_ready(tmp_path, BATCH_A)


def test_structured_startup_failure_survives_parent_exit(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    controller_id = "controller-1"
    state = fleet._initial_state(controller_id, None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "workers": 2,
        "status": "starting",
        "startup_status": "pending",
        "plan_id": "plan-a",
        "credentials_file": str(tmp_path / "plan-a.json"),
    }

    class Process:
        pid = os.getpid()

    process = Process()
    log = io.StringIO()
    processes = {BATCH_A: process}
    logs = {BATCH_A: log}
    monkeypatch.setenv(fleet.CONTROLLER_ID_ENV, controller_id)
    monkeypatch.setenv(fleet.POOL_BATCH_ENV, BATCH_A)
    monkeypatch.setenv(
        fleet.POOL_STARTUP_FILE_ENV,
        str(fleet._pool_startup_path(tmp_path, BATCH_A)),
    )
    monkeypatch.setattr(fleet, "controller_matches", lambda *_args: True)
    stopped = []
    monkeypatch.setattr(
        fleet, "_stop_run_plan_device",
        lambda item, reason: stopped.append((item["plan_id"], reason)),
    )

    fleet.publish_pool_startup_failure(
        tmp_path,
        BATCH_A,
        error_code="task_environment_update_failed",
        user_message="这台设备未能准备题目环境；已有文件没有被修改。",
    )
    fleet._refresh_pool_startups(tmp_path, state, processes)
    fleet._settle_pool(tmp_path, state, processes, logs, BATCH_A, 1)

    item = state["batches"][BATCH_A]
    assert item["status"] == "failed"
    assert item["startup_error_code"] == "task_environment_update_failed"
    assert "已有文件没有被修改" in item["startup_user_message"]
    assert stopped == [
        ("plan-a", "local startup failed (task_environment_update_failed)"),
    ]


def test_environment_build_exit_is_persisted_as_retryable_needs_attention(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "workers": 40,
        "status": "running",
        "refill": True,
        "plan_id": "plan-a",
        "credentials_file": str(tmp_path / "plan-a.json"),
    }
    stopped = []
    monkeypatch.setattr(
        fleet,
        "_stop_run_plan_device",
        lambda item, reason: stopped.append((item["plan_id"], reason)),
    )

    fleet._settle_pool(
        tmp_path,
        state,
        {BATCH_A: object()},
        {BATCH_A: io.StringIO()},
        BATCH_A,
        fleet.ENVIRONMENT_BUILD_FAILED_EXIT_CODE,
    )

    item = state["batches"][BATCH_A]
    assert item["status"] == "failed"
    assert item["returncode"] == 78
    assert item["failure_kind"] == "environment_build_failed"
    assert item["failure_state"] == "needs_attention"
    assert item["retryable"] is True
    assert "model did not start" in item["detail"]
    assert stopped == [(
        "plan-a", "local isolated environment build failed before model start",
    )]


def test_refill_pool_command_keeps_exact_batch_and_total_cap(tmp_path, monkeypatch):
    fleet._prepare_dirs(tmp_path)
    captured = {}

    class Process:
        pid = 124

    def popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(fleet.subprocess, "Popen", popen)
    monkeypatch.setattr("dradar.machine._lock_handle", None)
    state = {"controller_id": "controller-1"}

    _process, log = fleet._spawn_pool(
        tmp_path, state, BATCH_A, 2,
        refill=True,
        max_tasks=10,
        refill_harness="kimi-code",
        refill_model="kimi-k2.5",
        refill_effort="high",
    )
    log.close()

    command = captured["command"]
    assert command[command.index("--batch-id") + 1] == BATCH_A
    assert command[command.index("--refill-to") + 1] == "2"
    assert command[command.index("--max-tasks") + 1] == "10"
    assert command[command.index("--refill-harness") + 1] == "kimi-code"
    assert command[command.index("--refill-model") + 1] == "kimi-k2.5"
    assert command[command.index("--refill-effort") + 1] == "high"
    assert captured["env"]["DRADAR_REFILL_PLAN_SCOPE"] == BATCH_A


def test_plan_token_stays_in_private_file_not_fleet_argv_env_or_state(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    credentials = tmp_path / "run-plans" / "plan-example.json"
    credentials.parent.mkdir(mode=0o700)
    token = "drp_extremely_private_plan_token"
    credentials.write_text(json.dumps({"token": token}))
    credentials.chmod(0o600)
    captured = {}

    class Process:
        pid = 321

        def poll(self):
            return None

    def popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(fleet.subprocess, "Popen", popen)
    monkeypatch.setattr("dradar.machine._lock_handle", None)
    process, log = fleet._spawn_pool(
        tmp_path,
        {"controller_id": "controller-1"},
        BATCH_A,
        2,
        credentials_file=str(credentials),
    )
    log.close()
    assert process.pid == 321
    assert captured["command"][captured["command"].index("--credentials-file") + 1] == str(credentials)
    assert token not in captured["command"]
    assert all(token not in str(value) for value in captured["env"].values())

    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    monkeypatch.setattr(
        fleet, "_resolve_workers",
        lambda *_args: (2, [], {"account_limit": 4}),
    )
    monkeypatch.setattr(
        fleet,
        "_spawn_pool",
        lambda *_args, **_kwargs: (Process(), io.StringIO()),
    )
    fleet._handle_request(
        tmp_path,
        state,
        {},
        {},
        {
            "request_id": "plan-request",
            "controller_id": "controller-1",
            "controller_protocol_version": fleet.CONTROLLER_PROTOCOL_VERSION,
            "runtime_executable": sys.executable,
            "command": "add",
            "batch_id": BATCH_A,
            "workers": 2,
            "credentials_file": str(credentials),
            "plan_id": "plan-example",
        },
    )
    persisted = fleet._state_path(tmp_path).read_text()
    assert token not in persisted
    assert str(credentials) in persisted


def test_retry_reuses_saved_run_plan_identity_when_raw_cli_omits_it(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    credentials = tmp_path / "run-plans" / "plan-retry.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("{}")
    credentials.chmod(0o600)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "status": "stopped",
        "workers": 2,
        "plan_id": "plan-retry",
        "credentials_file": str(credentials),
    }
    captured = {}

    class Process:
        pid = 654

        def poll(self):
            return None

    def resolve(_workers, _batch_id, _state, credentials_file):
        captured["resolved_credentials"] = credentials_file
        return 2, [], {"account_limit": 4}

    def spawn(*_args, **kwargs):
        captured["spawned_credentials"] = kwargs["credentials_file"]
        return Process(), io.StringIO()

    monkeypatch.setattr(fleet, "_resolve_workers", resolve)
    monkeypatch.setattr(fleet, "_spawn_pool", spawn)
    fleet._handle_request(
        tmp_path,
        state,
        {},
        {},
        {
            "request_id": "retry-plan-request",
            "controller_id": "controller-1",
            "controller_protocol_version": fleet.CONTROLLER_PROTOCOL_VERSION,
            "runtime_executable": sys.executable,
            "runtime_environment": {},
            "command": "add",
            "batch_id": BATCH_A,
            "workers": 2,
            "retry": True,
        },
    )

    response = json.loads(
        (fleet._root(tmp_path) / fleet.RESPONSE_DIR
         / "retry-plan-request.json").read_text()
    )
    assert response["ok"] is True
    assert captured == {
        "resolved_credentials": str(credentials),
        "spawned_credentials": str(credentials),
    }
    assert state["batches"][BATCH_A]["plan_id"] == "plan-retry"
    assert state["batches"][BATCH_A]["credentials_file"] == str(credentials)


def test_retry_rejects_incomplete_saved_run_plan_identity(tmp_path, monkeypatch):
    fleet._prepare_dirs(tmp_path)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "status": "failed",
        "workers": 2,
        "plan_id": "plan-retry",
        "credentials_file": None,
    }
    monkeypatch.setattr(
        fleet,
        "_spawn_pool",
        lambda *_args, **_kwargs: pytest.fail("incomplete identity must fail closed"),
    )

    fleet._handle_request(
        tmp_path,
        state,
        {},
        {},
        {
            "request_id": "retry-incomplete-plan",
            "controller_id": "controller-1",
            "controller_protocol_version": fleet.CONTROLLER_PROTOCOL_VERSION,
            "runtime_executable": sys.executable,
            "runtime_environment": {},
            "command": "add",
            "batch_id": BATCH_A,
            "workers": 2,
            "retry": True,
        },
    )

    response = json.loads(
        (fleet._root(tmp_path) / fleet.RESPONSE_DIR
         / "retry-incomplete-plan.json").read_text()
    )
    assert response["ok"] is False
    assert "saved run-plan identity is incomplete" in response["error"]


def test_missing_config_is_one_fleet_request_error_not_controller_exit(monkeypatch):
    monkeypatch.setattr(fleet, "_load_config", lambda: {})
    monkeypatch.setattr(
        fleet,
        "_client",
        lambda _cfg: (_ for _ in ()).throw(
            SystemExit("not configured — run: dradar login")
        ),
    )

    with pytest.raises(fleet.FleetError, match="not configured"):
        fleet._resolve_workers(1, BATCH_A, {"batches": {}})


def test_pool_lock_rejects_duplicate_parent_and_dies_with_process(tmp_path):
    source = Path(__file__).parent.parent / "src"
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(source)!r})
            from pathlib import Path
            from dradar.fleet import acquire_pool_lock
            acquire_pool_lock(Path({str(tmp_path)!r}), {BATCH_A!r}, "controller-a")
            print("locked", flush=True)
            time.sleep(30)
        """)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(fleet.FleetError, match="already has a local Fleet pool owner"):
            fleet.acquire_pool_lock(tmp_path, BATCH_A, "controller-b")
    finally:
        holder.kill()
        holder.wait()

    fleet.acquire_pool_lock(tmp_path, BATCH_A, "controller-b")


def test_internal_fleet_pool_requires_controller_contract(monkeypatch):
    args = argparse.Namespace(
        workers=2, yes=True, keep=False, allow_task_drift=False,
        dev_agent=None, refill=False, refill_to=None, max_tasks=None,
        max_estimated_quota_pct=None, quota_tier="plus", auto=None, pick=None,
        parallel=False, worker_child=False, resume=True,
        worker_target_file=None, archive_session=False, batch_id=BATCH_A,
        fleet_pool=True, expect_assignment=None,
        forget_assignment_boundary=False,
    )
    monkeypatch.delenv(fleet.CONTROLLER_ID_ENV, raising=False)
    monkeypatch.delenv(fleet.POOL_BATCH_ENV, raising=False)

    with pytest.raises(SystemExit, match="invalid internal Fleet pool"):
        runloop.cmd_go(args)


def test_internal_fleet_pool_rejects_stale_controller_identity(monkeypatch):
    args = argparse.Namespace(
        workers=2, yes=True, keep=False, allow_task_drift=False,
        dev_agent=None, refill=False, refill_to=None, max_tasks=None,
        max_estimated_quota_pct=None, quota_tier="plus", auto=None, pick=None,
        parallel=False, worker_child=False, resume=True,
        worker_target_file=None, archive_session=False, batch_id=BATCH_A,
        fleet_pool=True, expect_assignment=None,
        forget_assignment_boundary=False,
    )
    monkeypatch.setenv(fleet.CONTROLLER_ID_ENV, "stale-controller")
    monkeypatch.setenv(fleet.POOL_BATCH_ENV, BATCH_A)
    monkeypatch.setattr(fleet, "controller_matches", lambda *_args: False)

    with pytest.raises(SystemExit, match="invalid internal Fleet pool"):
        runloop.cmd_go(args)


def test_explicit_stop_is_reported_as_stopped_and_halts_refill(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "workers": 2,
        "status": "stopping",
        "refill": True,
    }

    class Process:
        def poll(self):
            return 130

    process = Process()
    log = io.StringIO()
    processes = {BATCH_A: process}
    logs = {BATCH_A: log}
    stopped = []
    monkeypatch.setattr(
        fleet, "_stop_remote_refill",
        lambda batch_id, reason: stopped.append((batch_id, reason)),
    )

    fleet._settle_pool(
        tmp_path, state, processes, logs, BATCH_A, 130,
    )

    item = state["batches"][BATCH_A]
    assert item["status"] == "stopped"
    assert item["returncode"] == 130
    assert processes == {}
    assert logs == {}
    assert log.closed
    assert stopped == [(BATCH_A, "stopped by the machine-local Fleet")]


def test_failed_refill_pool_stops_server_campaign(tmp_path, monkeypatch):
    fleet._prepare_dirs(tmp_path)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "workers": 1,
        "status": "running",
        "refill": True,
    }
    stopped = []
    monkeypatch.setattr(
        fleet, "_stop_remote_refill",
        lambda batch_id, reason: stopped.append((batch_id, reason)),
    )

    fleet._settle_pool(
        tmp_path, state, {BATCH_A: object()}, {}, BATCH_A, 7,
    )

    assert state["batches"][BATCH_A]["status"] == "failed"
    assert stopped == [(BATCH_A, "local Fleet pool exited with code 7")]


def test_run_plan_device_stop_drains_without_interrupting_or_stopping_campaign(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "workers": 2,
        "status": "running",
        "refill": True,
        "plan_id": "plan-a",
        "credentials_file": str(tmp_path / "plan-a.json"),
    }
    signals = []

    class Process:
        def poll(self):
            return None

        def send_signal(self, value):
            signals.append(value)

    stopped_devices = []
    monkeypatch.setattr(
        fleet,
        "_stop_run_plan_device",
        lambda item, reason: stopped_devices.append((item["plan_id"], reason)),
    )
    monkeypatch.setattr(
        fleet,
        "_stop_item_refill",
        lambda *_args: pytest.fail("a plan-local stop must not stop the shared campaign"),
    )

    fleet._handle_request(
        tmp_path,
        state,
        {BATCH_A: Process()},
        {BATCH_A: io.StringIO()},
        {
            "request_id": "stop-plan-a",
            "controller_id": "controller-1",
            "command": "stop",
            "batch_id": BATCH_A,
        },
    )

    assert signals == []
    assert stopped_devices == [("plan-a", "a machine-local stop request")]
    assert state["batches"][BATCH_A]["status"] == "stopping"
    marker = fleet._root(tmp_path) / fleet.ABORT_DIR / f"{BATCH_A}.stop"
    assert marker.read_text().startswith("drain:")
    response = json.loads(
        (fleet._root(tmp_path) / fleet.RESPONSE_DIR / "stop-plan-a.json").read_text()
    )
    assert response["ok"] is True
    assert response["stopping"] == [BATCH_A]
    assert response["warnings"] == []


def test_failed_run_plan_pool_stops_only_its_device_not_shared_campaign(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "workers": 2,
        "status": "running",
        "refill": True,
        "plan_id": "plan-a",
        "credentials_file": str(tmp_path / "plan-a.json"),
    }
    stopped_devices = []
    monkeypatch.setattr(
        fleet,
        "_stop_run_plan_device",
        lambda item, reason: stopped_devices.append((item["plan_id"], reason)),
    )
    monkeypatch.setattr(
        fleet,
        "_stop_item_refill",
        lambda *_args: pytest.fail("one failed device must not stop the shared campaign"),
    )

    fleet._settle_pool(
        tmp_path,
        state,
        {BATCH_A: object()},
        {BATCH_A: io.StringIO()},
        BATCH_A,
        7,
    )

    assert state["batches"][BATCH_A]["status"] == "failed"
    assert stopped_devices == [("plan-a", "local runner exit code 7")]


def test_direct_ctrl_c_is_stopped_when_server_acknowledges_device_stop(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "workers": 2,
        "status": "running",
        "refill": True,
        "plan_id": "plan-a",
        "credentials_file": str(tmp_path / "plan-a.json"),
    }
    stopped_devices = []
    monkeypatch.setattr(
        fleet,
        "_stop_run_plan_device",
        lambda item, reason: stopped_devices.append((item["plan_id"], reason)),
    )

    fleet._settle_pool(
        tmp_path,
        state,
        {BATCH_A: object()},
        {BATCH_A: io.StringIO()},
        BATCH_A,
        130,
    )

    item = state["batches"][BATCH_A]
    assert item["status"] == "stopped"
    assert item["returncode"] == 130
    assert item["detail"] == "stopped by user; server acknowledged the device stop"
    assert stopped_devices == [("plan-a", "local runner exit code 130")]


def test_direct_ctrl_c_needs_attention_when_device_stop_is_not_acknowledged(
    tmp_path, monkeypatch,
):
    fleet._prepare_dirs(tmp_path)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "workers": 1,
        "status": "running",
        "refill": True,
        "plan_id": "plan-a",
        "credentials_file": str(tmp_path / "plan-a.json"),
    }
    monkeypatch.setattr(
        fleet,
        "_stop_run_plan_device",
        lambda *_args: "server stop was not acknowledged",
    )

    fleet._settle_pool(
        tmp_path,
        state,
        {BATCH_A: object()},
        {BATCH_A: io.StringIO()},
        BATCH_A,
        -signal.SIGINT,
    )

    item = state["batches"][BATCH_A]
    assert item["status"] == "interrupted"
    assert item["warnings"] == ["server stop was not acknowledged"]
    assert "recovery" in item["detail"]


@pytest.mark.parametrize(
    "returncode,expected_status",
    [(0, "stopped"), (1, "interrupted")],
)
def test_run_plan_stop_is_clean_only_when_active_work_settles_cleanly(
    tmp_path, monkeypatch, returncode, expected_status,
):
    fleet._prepare_dirs(tmp_path)
    state = fleet._initial_state("controller-1", None)
    state["status"] = "active"
    state["batches"][BATCH_A] = {
        "batch_id": BATCH_A,
        "workers": 1,
        "status": "stopping",
        "refill": True,
        "plan_id": "plan-a",
        "credentials_file": str(tmp_path / "plan-a.json"),
    }
    monkeypatch.setattr(
        fleet,
        "_stop_item_refill",
        lambda *_args: pytest.fail("settling a plan stop must not stop the campaign"),
    )
    monkeypatch.setattr(
        fleet,
        "_stop_run_plan_device",
        lambda *_args: pytest.fail("the requested stop was already sent"),
    )

    fleet._settle_pool(
        tmp_path,
        state,
        {BATCH_A: object()},
        {BATCH_A: io.StringIO()},
        BATCH_A,
        returncode,
    )

    assert state["batches"][BATCH_A]["status"] == expected_status
    if returncode:
        assert "recovery" in state["batches"][BATCH_A]["detail"]
    else:
        assert "detail" not in state["batches"][BATCH_A]
