from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from dradar import runner
from dradar.manifest import task_content_hash
from dradar.v2.journal import Journal
from dradar.v2.runtime import CodexRuntime, RuntimeUnavailable, normalize_assignment
from dradar.v2.scheduler import ExecutionBlocked
from test_runner_exit_evidence import runtime as old_runtime
from private_artifact_fixture import private_trial

@pytest.fixture
def package(tmp_path):
    tasks = tmp_path / "tasks"
    task = tasks / "task"
    task.mkdir(parents=True)
    (task / "instruction.md").write_text("synthetic instruction")
    (task / "task.toml").write_text("[metadata]\n")
    a = {"assignment_id": "a" * 32, "owner_epoch": 1,
         "task": {"task_id": "task", "benchmark": "synthetic", "model": "codex-test", "effort": "high", "task_content_hash": task_content_hash(tasks, "task"), "task_commit": "0" * 40, "task_bundle": None},
         "runner": {"agent": "codex", "agent_version": "0.1.0", "agent_version_verified": False, "auth_runtime": None, "provider": None, "billing_mode": "subscription", "est_minutes": 1}}
    return tasks, a

def test_runtime_version_auth_billing_fields_are_not_replaced(package):
    _, a = package
    normalized = normalize_assignment(a)
    assert all(normalized[k] == v for k, v in a["runner"].items())
    assert normalized["deep_swe_commit"] == a["task"]["task_commit"]
    assert normalized["benchmark_id"] == a["task"]["benchmark"]

def test_actual_worker_gate_orders_ack_fence_before_fake_paid_call(tmp_path, monkeypatch, package):
    tasks, a = package
    journal = Journal(tmp_path / "state")
    eid = journal.identity("execution")
    calls = []
    def fake_trial(assignment, tasks_root, work, **kw):
        calls.append("environment_ready")
        assert journal.execution(a["assignment_id"]) is None
        assert assignment["_runner_session_id"] == eid
        scope = {"assignment_id": a["assignment_id"], "runner_session_id": eid}
        emit = lambda event: kw["execution_observer"]({"schema": "dradar.execution_audit.v1", "execution_id": "b" * 32, "scope": scope, "event": event})
        emit("spawned")
        kw["on_worker_registered"]({"provider": "codex"})
        with kw["provider_launch_guard"]():
            assert journal.execution(a["assignment_id"])["execution_id"] == eid
            calls.append("simulated_paid_call")
        emit("confirmed_absent")
        trial = work / "trial"
        private_trial(trial)
        patch = trial / "patch"
        patch.write_bytes(b"")
        return SimpleNamespace(trial_dir=trial, patch=patch, trajectory=None, result=None, returncode=0)
    monkeypatch.setattr(runner, "build_codex_trajectory_bundle", lambda _: None)
    runtime = CodexRuntime(journal, tasks, run_trial=fake_trial)
    prepared = runtime.prepare(a)
    def barrier():
        calls.append("start_ack")
        assert journal.begin_execution(a["assignment_id"], eid)
    @contextmanager
    def guard():
        yield
    completion = runtime.execute_with_barrier(prepared, eid, barrier, guard)
    assert calls == ["environment_ready", "start_ack", "simulated_paid_call"]
    assert completion.outcome == "completed" and completion.exit_confirmed
    assert completion.tokens["total"] is None
    events = journal.audits(a["assignment_id"])
    assert all(e["v2_execution_id"] == eid and e["audit_execution_id"] != eid for e in events)

def test_missing_exit_proof_is_not_completion(tmp_path, package):
    tasks, a = package
    j = Journal(tmp_path / "state")
    def fake_trial(assignment, tasks, work, **kw):
        kw["on_worker_registered"]({})
        return SimpleNamespace()
    runtime = CodexRuntime(j, tasks, run_trial=fake_trial)
    with pytest.raises(RuntimeUnavailable):
        runtime.execute_with_barrier(runtime.prepare(a), "e" * 32, lambda: j.begin_execution(a["assignment_id"], "e" * 32), lambda: __import__("contextlib").nullcontext())

def test_task_mismatch_fails_before_start(tmp_path, package):
    tasks, a = package
    runtime = CodexRuntime(Journal(tmp_path / "state"), tasks, run_trial=lambda *a, **k: pytest.fail("runtime must not run"))
    (tasks / "task/instruction.md").write_text("changed")
    with pytest.raises(RuntimeUnavailable): runtime.prepare(a)

def test_existing_real_runner_calls_v2_guard_at_spawn_and_nonce(old_runtime):
    state = old_runtime
    calls = []
    @contextmanager
    def guard():
        calls.append("guard")
        yield
    runner.run_trial(state["assignment"], state["tasks"], state["work"],
                     on_worker_registered=lambda _: calls.append("ready"),
                     provider_launch_guard=guard, execution_observer=state["observer"])
    assert calls == ["guard", "ready", "guard"]
    assert any(e["event"] == "confirmed_absent" for e in state["events"])

def test_stop_guard_rejects_before_spawn(old_runtime):
    state = old_runtime
    @contextmanager
    def guard():
        raise ExecutionBlocked("synthetic stop")
        yield
    with pytest.raises(ExecutionBlocked):
        runner.run_trial(state["assignment"], state["tasks"], state["work"],
                         on_worker_registered=lambda _: None,
                         provider_launch_guard=guard, execution_observer=state["observer"])
    assert "popen" not in state["calls"]


def test_v2_run_stop_terminates_exact_runner_and_retains_patch(old_runtime):
    state = old_runtime
    stopped = False
    def ready(_):
        nonlocal stopped
        stopped = True
    result = runner.run_trial(state["assignment"], state["tasks"], state["work"],
        on_worker_registered=ready, execution_stop_requested=lambda:stopped,
        execution_observer=state["observer"])
    assert result.patch.read_text()=="diff"
    assert "group_stop" in state["calls"]
    assert any(e["event"]=="confirmed_absent" for e in state["events"])

def test_registration_stop_obeys_exact_callback(tmp_path):
    process = SimpleNamespace(poll=lambda:None)
    with pytest.raises(runner.RunnerError, match="v2 run stop"):
        runner._wait_for_worker_registration(process,tmp_path/"events",
            environment_build_timeout_multiplier=1,
            execution_stop_requested=lambda:True)

def test_normal_structured_null_exception_is_success_not_blank_diagnostic(tmp_path, monkeypatch, package):
    tasks,a=package
    j=Journal(tmp_path/'state')
    eid=j.identity('execution')
    def fake_trial(assignment,tasks_root,work,**kw):
        scope={'assignment_id':a['assignment_id'],'runner_session_id':eid}
        kw['on_worker_registered']({})
        kw['execution_observer']({'schema':'dradar.execution_audit.v1','execution_id':'b'*32,'scope':scope,'event':'confirmed_absent'})
        trial=work/'trial';private_trial(trial)
        patch=trial/'patch';patch.write_bytes(b'')
        result=trial/'result.json';result.write_text('{"exception_info":null}')
        return SimpleNamespace(trial_dir=trial,patch=patch,trajectory=None,result=result,returncode=0)
    monkeypatch.setattr(runner,'build_codex_trajectory_bundle',lambda _:None)
    runtime=CodexRuntime(j,tasks,run_trial=fake_trial)
    outcome=runtime.execute_with_barrier(runtime.prepare(a),eid,lambda:j.begin_execution(a['assignment_id'],eid),lambda:__import__('contextlib').nullcontext())
    assert outcome.outcome=='completed' and outcome.failure is None
