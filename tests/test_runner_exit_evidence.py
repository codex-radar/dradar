"""Exercise the real run_trial lifecycle with fake OS/Docker adapters only."""
from contextlib import contextmanager, nullcontext
from pathlib import Path
import json
import subprocess
import time

import pytest

from private_artifact_fixture import private_trial
from dradar import runner


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    work = tmp_path / "work"
    tasks = tmp_path / "tasks"
    assignment = dict(assignment_id="a1", task_id="task", batch_id="batch",
                      agent="codex", model="gpt-5.5", effort="medium",
                      agent_version="0.145.0", _runner_session_id="session-123",
                      owner_epoch=3, resume_generation=2)
    state = dict(calls=[], container=False, sticky=False, daemon_error=False,
                 no_job=False, spawn_error=None, surviving_group=False,
                 daemon_after_rm=False)
    monkeypatch.setattr(runner, "resolve_latest_codex_cli_version", lambda *a, **k: "0.145.0")
    monkeypatch.setattr(runner, "_pier_process_env", lambda *a, **k: {})
    monkeypatch.setattr(runner.AUTH_REGISTRY, "session", lambda *a, **k: nullcontext(None))
    monkeypatch.setattr(runner.image_cache, "prepare_trial_builder", lambda *a, **k:
                        runner.image_cache.TrialBuilderLease("builder", True))
    monkeypatch.setattr(runner, "build_pier_command", lambda *a, **k: ["fake-pier"])
    monkeypatch.setattr(runner, "_wait_for_worker_registration", lambda *a, **k:
                        {"start_deadline": time.monotonic() + 30})

    class Process:
        pid = 424242
        returncode = None

        def __init__(self, *args, **kwargs):
            state["calls"].append("popen")
            if state["spawn_error"]:
                raise state["spawn_error"]
            state["process"] = self
            state["container"] = True
            if not state["no_job"]:
                trial = work / "jobs" / "aa1" / "task__t0"
                private_trial(trial)
                (trial / "artifacts").mkdir()
                (trial / "artifacts/model.patch").write_text("diff")

        def wait(self, timeout=None):
            self.returncode = self.returncode or 0
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def killpg(pid, sig):
        assert pid == 424242
        state["calls"].append("group_probe" if sig == 0 else "group_stop")
        if sig:
            state["process"].returncode = -sig
            state["surviving_group"] = False
        elif state["process"].returncode is not None and not state["surviving_group"]:
            raise ProcessLookupError

    def docker(command, **kwargs):
        assert command[0] == "docker", "unexpected subprocess: " + repr(command)
        assert 0 < kwargs["timeout"] <= 90
        state["calls"].append("docker_" + command[1])
        if state["daemon_error"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="unavailable")
        if state["daemon_after_rm"] and "docker_rm" in state["calls"] and command[1] == "ps":
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="unavailable after removal")
        if command[1] == "ps":
            output = "a" * 12 if state["container"] else ""
        elif command[1] == "inspect":
            output = json.dumps([dict(Id="a" * 64, State={"Running": True},
                Config={"Labels": {"com.docker.compose.project": "pier__abcdef"}},
                Mounts=[{"Type": "bind", "Source": str(work / "jobs/aa1/task__t0")}])])
        elif command[1:3] == ["rm", "-f"]:
            state["container"] = state["sticky"]
            output = ""
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(runner.subprocess, "Popen", Process)
    monkeypatch.setattr(runner.subprocess, "run", docker)
    monkeypatch.setattr(runner.os, "killpg", killpg)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    state.update(work=work, tasks=tasks, assignment=assignment, events=[])

    def observer(event):
        state["events"].append(event)
        state["calls"].append("event_" + event["event"])

    state["observer"] = observer
    return state


def run(state, **kwargs):
    return runner.run_trial(state["assignment"], state["tasks"], state["work"],
                            execution_observer=state["observer"], **kwargs)


def test_normal_codex_audits_process_and_rechecks_removed_containers(runtime):
    result = run(runtime)
    assert result.patch.read_text() == "diff"
    assert [e["event"] for e in runtime["events"]] == [
        "entered", "launch_pending", "spawned", "confirmed_absent"]
    assert runtime["calls"].index("event_launch_pending") < runtime["calls"].index("popen")
    assert runtime["calls"].index("docker_rm") < len(runtime["calls"]) - 1
    assert runtime["calls"][-2:] == ["docker_ps", "event_confirmed_absent"]
    proof = runtime["events"][-1]
    assert proof["process_group"] == proof["exact_job_containers"] == "absent"
    assert proof["scope"]["runner_session_id"] == "session-123"
    assert proof["pid"] == proof["pgid"] == 424242


def test_normal_codex_reaps_surviving_host_group_before_absence(runtime):
    runtime["surviving_group"] = True
    run(runtime)
    assert "group_stop" in runtime["calls"]
    assert runtime["calls"].index("group_stop") < runtime["calls"].index("event_confirmed_absent")


@pytest.mark.parametrize("fault", ["sticky", "daemon_error", "daemon_after_rm", "no_job"])
def test_uncertain_container_exit_never_emits_absence(runtime, fault):
    runtime[fault] = True
    with pytest.raises(runner.RunnerCleanupUnconfirmedError) as exc:
        run(runtime)
    assert exc.value.job_dir == runtime["work"] / "jobs/aa1"
    assert runtime["events"][-1]["event"] == "unknown"
    assert "confirmed_absent" not in [e["event"] for e in runtime["events"]]


@pytest.mark.parametrize("invalid", [
    {"Mounts": [None]},
    {"Mounts": [{"Type": "bind", "Source": None}]},
    {"Config": {"Labels": {"com.docker.compose.project.config_files": []}}},
])
def test_fresh_container_audit_rejects_incomplete_ownership(tmp_path, monkeypatch, invalid):
    # A successful daemon response still cannot prove empty when ownership
    # metadata is malformed; it might describe this exact job.
    metadata = dict(Id="b" * 64, Config={"Labels": {}}, Mounts=[])
    metadata.update(invalid)

    def docker(command, **kwargs):
        output = "b" * 12 if command[1] == "ps" else json.dumps([metadata])
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", docker)
    with pytest.raises(runner.RunnerError, match="ownership"):
        runner._confirm_terminated_pier_containers_absent(tmp_path)


@pytest.mark.parametrize("failure_event", ["entered", "launch_pending", "spawned", "confirmed_absent"])
def test_observer_write_failure_prevents_launch_or_preserves_quarantine(runtime, failure_event):
    observer = runtime["observer"]
    def fail(event):
        if event["event"] == failure_event:
            raise OSError("disk full")
        observer(event)
    runtime["observer"] = fail
    with pytest.raises(runner.RunnerCleanupUnconfirmedError):
        run(runtime)
    assert runtime["events"][-1]["event"] == "unknown"
    if failure_event in {"entered", "launch_pending"}:
        assert "popen" not in runtime["calls"]
    else:
        assert runtime["container"] is False


def test_popen_oserror_has_explicit_never_started_evidence(runtime):
    runtime["spawn_error"] = OSError("exec failed")
    with pytest.raises(OSError):
        run(runtime)
    assert runtime["events"][-1]["event"] == "never_started"
    assert runtime["events"][-1]["execution_started"] is False
    assert runtime["events"][-1]["reason"] == "popen_failed"
    assert runtime["container"] is False


def test_uncertain_popen_failure_is_not_never_started(runtime):
    runtime["spawn_error"] = RuntimeError("adapter launch outcome unknown")
    with pytest.raises(runner.RunnerCleanupUnconfirmedError):
        run(runtime)
    assert runtime["events"][-1]["event"] == "unknown"
    assert runtime["events"][-1]["execution_started"] is None


def test_prelaunch_validation_has_entered_and_explicit_never_started(runtime):
    runtime["assignment"]["auth_runtime"] = "unsupported"
    with pytest.raises(runner.RunnerError, match="unsupported"):
        run(runtime)
    assert [e["event"] for e in runtime["events"]] == ["entered", "never_started"]
    assert "popen" not in runtime["calls"]


def test_unbound_scope_does_not_claim_never_started(runtime):
    runtime["assignment"].pop("_runner_session_id")
    runtime["assignment"]["auth_runtime"] = "unsupported"
    with pytest.raises(runner.RunnerError):
        run(runtime)
    assert runtime["events"][-1]["event"] == "unknown"


def test_unbound_success_cannot_publish_session_absence(runtime):
    runtime["assignment"].pop("_runner_session_id")
    with pytest.raises(runner.RunnerCleanupUnconfirmedError):
        run(runtime)
    assert runtime["events"][-1]["event"] == "unknown"


def test_registration_abort_network_follows_local_cleanup(runtime, monkeypatch):
    from dradar import registration
    class Window:
        def __init__(self, *args, defer_abort=False): assert defer_abort
        def abort(self):
            assert runtime["container"] is False
            assert runtime["process"].returncode is not None
            assert not list(runtime["work"].glob("*.worker-start.json"))
            runtime["calls"].append("remote_abort")
    monkeypatch.setattr(registration, "RegistrationWindow", Window)
    def reject(event):
        raise runner.RunnerError("registration rejected")
    reject._uses_registration_window = True
    with pytest.raises(runner.RunnerError, match="registration rejected"):
        run(runtime, on_worker_registered=reject)
    assert runtime["calls"].index("docker_rm") < runtime["calls"].index("remote_abort")
    assert runtime["events"][-1]["event"] == "confirmed_absent"


def test_real_registration_bind_defers_its_close_until_runner_cleanup(runtime, monkeypatch):
    from types import SimpleNamespace
    from threading import Event
    from dradar import registration
    from dradar.api_client import ApiError
    windows = []
    telemetry = SimpleNamespace(_stop=Event(), _wake=Event())

    async def uncertain_bind(window):
        window.started_sent = True
        window.assignment["_registration_start_uncertain"] = True
        raise ApiError("lost start response")

    async def close(window):
        assert runtime["container"] is False
        assert runtime["process"].returncode is not None
        assert not list(runtime["work"].glob("*.worker-start.json"))
        assert runtime["calls"][-1] == "docker_ps"
        runtime["calls"].append("real_registration_close")
        window._fenced = True
        window.assignment.pop("_registration_start_uncertain", None)

    monkeypatch.setattr(registration.RegistrationWindow, "_bind", uncertain_bind)
    monkeypatch.setattr(registration.RegistrationWindow, "_close_fence", close)

    def register(event):
        window = event["_registration_window"]
        windows.append(window)
        window.bind(SimpleNamespace(), telemetry, runtime["assignment"])

    register._uses_registration_window = True
    with pytest.raises(runner.RunnerError, match="registration lifetime"):
        run(runtime, on_worker_registered=register)
    assert windows[0]._fenced
    assert runtime["calls"].count("real_registration_close") == 1
    assert runtime["events"][-1]["event"] == "confirmed_absent"


def test_managed_independent_process_groups_remain_unknown(runtime):
    runtime["assignment"]["auth_runtime"] = "codex-managed-at-v1"
    with pytest.raises(runner.RunnerCleanupUnconfirmedError):
        run(runtime, managed_auth_config=Path("/unused/config"), on_worker_registered=lambda event: None)
    assert runtime["events"][-1]["event"] == "unknown"
    assert not list(runtime["work"].glob("*.managed-start.json"))


def test_unavailable_process_tree_audit_remains_unknown(runtime, monkeypatch):
    def unsupported(proc):
        raise runner.RunnerError("provider process-tree exit cannot be confirmed on this runtime")
    monkeypatch.setattr(runner, "_confirm_pier_process_tree_stopped", unsupported)
    with pytest.raises(runner.RunnerCleanupUnconfirmedError):
        run(runtime)
    assert runtime["events"][-1]["event"] == "unknown"


def test_windows_process_tree_is_not_assumed_absent(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    with pytest.raises(runner.RunnerError, match="cannot be confirmed"):
        runner._confirm_pier_process_tree_stopped(SimpleNamespace(pid=424242))


@pytest.mark.parametrize("managed", [False, True])
def test_late_registration_cannot_publish_permission_after_stop(runtime, monkeypatch, managed):
    from dradar import run_intent
    batch = "a" * 32
    home = runtime["work"].parent
    generation = run_intent.begin(home, batch)
    monkeypatch.setenv(run_intent.BATCH_ENV, batch)
    monkeypatch.setenv(run_intent.GENERATION_ENV, generation)
    writes = []
    original = runner._materialize_shared_file
    def materialize(path, *a, **k):
        if path.name.endswith((".worker-start.json", ".managed-start.json")):
            writes.append(path)
        return original(path, *a, **k)
    monkeypatch.setattr(runner, "_materialize_shared_file", materialize)
    def late_ack(event):
        run_intent.stop(home, batch)
    kwargs = dict(on_worker_registered=late_ack)
    if managed:
        runtime["assignment"]["auth_runtime"] = "codex-managed-at-v1"
        kwargs["managed_auth_config"] = Path("/unused/config")
    with pytest.raises(runner.RunnerError):
        run(runtime, **kwargs)
    assert writes == []
    assert runtime["container"] is False


def test_permission_write_and_local_finish_share_guard_but_network_does_not(runtime, monkeypatch):
    from dradar import run_intent, registration
    generation = run_intent.begin(runtime["work"].parent, "a" * 32)
    monkeypatch.setenv(run_intent.BATCH_ENV, "a" * 32)
    monkeypatch.setenv(run_intent.GENERATION_ENV, generation)
    locked = []
    @contextmanager
    def guard(home, batch, generation):
        assert home == runtime["work"].parent
        locked.append(True)
        try:
            yield
        finally:
            locked.pop()
    monkeypatch.setattr(run_intent, "launch_guard", guard)
    original_popen = runner.subprocess.Popen
    def popen(*args, **kwargs):
        assert locked
        return original_popen(*args, **kwargs)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    class Window:
        deadline = time.monotonic() + 30
        def __init__(self, *a, defer_abort=False): assert defer_abort
        def check(self): assert locked
        def finish(self): assert locked
        def abort(self): pytest.fail("normal registration should not abort")
    monkeypatch.setattr(registration, "RegistrationWindow", Window)
    def registration_callback(event):
        assert not locked
    registration_callback._uses_registration_window = True
    original = runner._materialize_shared_file
    def materialize(path, *a, **k):
        if path.name.endswith(".worker-start.json"):
            assert locked
        return original(path, *a, **k)
    monkeypatch.setattr(runner, "_materialize_shared_file", materialize)
    run(runtime, on_worker_registered=registration_callback)
    assert runtime["events"][-1]["event"] == "confirmed_absent"


@pytest.mark.parametrize("stop_during_preparation", [False, True])
def test_stop_before_local_launch_has_no_child_and_keeps_no_launch_evidence(
    runtime, monkeypatch, stop_during_preparation,
):
    from dradar import run_intent
    home, batch = runtime["work"].parent, "a" * 32
    generation = run_intent.begin(home, batch)
    monkeypatch.setenv(run_intent.BATCH_ENV, batch)
    monkeypatch.setenv(run_intent.GENERATION_ENV, generation)
    if stop_during_preparation:
        def command(*args, **kwargs):
            run_intent.stop(home, batch)
            return ["fake-pier"]
        monkeypatch.setattr(runner, "build_pier_command", command)
    else:
        run_intent.stop(home, batch)
    with pytest.raises(run_intent.IntentStopped):
        run(runtime)
    assert "popen" not in runtime["calls"]
    assert [e["event"] for e in runtime["events"]] == ["entered", "never_started"]
