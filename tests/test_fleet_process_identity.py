"""Linux argv evidence must not interpret task prompt bytes as shell syntax."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from dradar import boundary_recovery as recovery, capacity_journal, fleet, launcher_handoff, runtime_identity


@pytest.fixture
def proc(monkeypatch):
    fields = ["S", "1", "0", "0", "0", "0", "0"] + ["0"] * 12 + ["17"]
    values = {"stat": "77 (name with ) quotes) " + " ".join(fields),
              "cmdline": b"node\0/app/codex.js\0-p\0user's special \"quotes\n\0"}
    identity = {"pid": 77, "start_ticks": 17, "boot_id": "boot", "host_id": "host"}
    monkeypatch.setattr(runtime_identity, "process_identity", lambda pid: dict(identity))
    original_text, original_bytes = Path.read_text, Path.read_bytes
    def read(path, original, *args, **kwargs):
        if str(path).startswith("/proc/77/"):
            value = values[path.name]
            if isinstance(value, Exception):
                raise value
            return value
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **kw: read(path, original_text, *a, **kw))
    monkeypatch.setattr(Path, "read_bytes", lambda path, *a, **kw: read(path, original_bytes, *a, **kw))
    return values, fields, identity


def test_actual_argv_preserves_special_quotes_and_newline(proc):
    identity, argv = recovery._fleet_process_argv(77, 1)
    assert argv == ["node", "/app/codex.js", "-p", "user's special \"quotes\n"]
    assert identity == proc[2]
    assert recovery._looks_like_runner_process(" ".join(argv))  # regression control
    assert not recovery._looks_like_runner_argv(argv)


@pytest.mark.parametrize("argv", [
    ["dradar", "go"], ["python", "-m", "dradar.cli", "resume"],
    ["python", "/dev/fd/7", "fleet", "serve"],
    ["python", "-m", "dradar.cli", "go", "--worker-child"],
])
def test_real_external_runner_arguments_still_identified(argv):
    assert recovery._looks_like_runner_argv(argv)


@pytest.mark.parametrize("raw", [b"", b"node", b"\0node\0"])
def test_unknown_userspace_argv_is_rejected(proc, raw):
    proc[0]["cmdline"] = raw
    with pytest.raises(recovery.RecoveryBlocked, match="arguments"):
        recovery._fleet_process_argv(77, 1)


@pytest.mark.parametrize("value", [PermissionError(), FileNotFoundError()])
def test_inaccessible_live_process_is_not_ignored(proc, value):
    proc[0]["cmdline"] = value
    with pytest.raises(recovery.RecoveryBlocked, match="identity"):
        recovery._fleet_process_argv(77, 1)


def test_pid_reuse_is_rejected(proc, monkeypatch):
    count = 0
    def identity(pid):
        nonlocal count
        count += 1
        return {**proc[2], "start_ticks": 17 if count == 1 else 18}
    monkeypatch.setattr(runtime_identity, "process_identity", identity)
    with pytest.raises(recovery.RecoveryBlocked, match="identity or ancestry changed"):
        recovery._fleet_process_argv(77, 1)


def test_reparenting_is_rejected(proc):
    with pytest.raises(recovery.RecoveryBlocked, match="ancestry"):
        recovery._fleet_process_argv(77, 2)


@pytest.mark.parametrize("state,flags", [("Z", "0"), ("S", str(0x00200000))])
def test_proven_zombie_or_kernel_thread_has_no_cli_action(proc, state, flags):
    proc[0]["cmdline"] = b""
    fields = list(proc[1])
    fields[0], fields[6] = state, flags
    proc[0]["stat"] = "77 (fixture) " + " ".join(fields)
    assert recovery._fleet_process_argv(77, 1) == (proc[2], [])


def test_disappeared_process_is_only_ignored_if_identity_is_absent(proc, monkeypatch):
    monkeypatch.setattr(runtime_identity, "process_identity", lambda pid: None)
    assert recovery._fleet_process_argv(77, 1) is None


@pytest.fixture
def fleet_proof(tmp_path, monkeypatch):
    own = os.getpid()
    identities = {pid: {"pid": pid, "start_ticks": pid, "boot_id": "b", "host_id": "h"}
                  for pid in (own, 100, 101, 102, 999)}
    processes = {own: (os.getppid(), ["python", "-m", "dradar.cli", "fleet", "claim"]),
                 100: (1, ["python", "-m", "dradar.cli", "fleet", "serve"]),
                 101: (100, ["python", "-m", "dradar.cli", "resume"]),
                 102: (101, ["python", "-m", "dradar.cli", "go", "--worker-child"]),
                 999: (1, ["node", "codex.js", "-p", "user's \"special quotes\n"])}
    state = {"pid": 100, "controller_id": "controller", "claim_window": {"operation": {
        "operation_id": "op", "status": "active", "controller_id": "controller",
        "owner_identity": identities[own]}}, "batches": {"b" * 32: {
        "status": "running", "pid": 101, "process_identity": identities[101]}}}
    containers = []
    monkeypatch.setattr(fleet, "_read_json", lambda *_: state)
    monkeypatch.setattr(fleet, "controller_is_active", lambda *_: True)
    monkeypatch.setattr(runtime_identity, "process_identity", identities.get)
    monkeypatch.setattr(launcher_handoff, "supervisor", lambda: None)
    monkeypatch.setattr(recovery, "_fleet_process_argv", lambda pid, ppid:
                        (identities[pid], processes[pid][1]) if processes[pid][0] == ppid
                        else (_ for _ in ()).throw(recovery.RecoveryBlocked("ancestry changed")))
    def run(command, **kwargs):
        if command[0] == "ps":
            return SimpleNamespace(stdout="\n".join(f"{pid} {row[0]}" for pid, row in processes.items()))
        if command == ["docker", "ps", "-q"]:
            return SimpleNamespace(stdout="\n".join(c["Id"] for c in containers))
        assert command[:2] == ["docker", "inspect"]
        return SimpleNamespace(stdout=json.dumps(containers))
    monkeypatch.setattr(recovery.subprocess, "run", run)
    return tmp_path, identities, processes, containers


def test_fleet_claim_accepts_node_without_weakening_docker_ownership(fleet_proof):
    home, _, _, containers = fleet_proof
    kept = home / "pending_uploads.json"
    kept.write_text('[{"assignment_id":"old","unknown":true}]')
    before = kept.read_bytes()
    recovery._check_fleet_claim_processes(home, "op")
    assert kept.read_bytes() == before
    # An unowned job container remains a blocker, even for non-CLI Node argv.
    containers.append({"Id": "a" * 64, "Config": {"Labels": {}},
                       "Mounts": [{"Source": str(home / "work/jobs/foreign")}]})
    with pytest.raises(recovery.RecoveryBlocked, match="non-Fleet DRadar job container"):
        recovery._check_fleet_claim_processes(home, "op")
    assert kept.read_bytes() == before


def test_ancestor_drift_does_not_authorize_pool_descendant(fleet_proof):
    home, _, processes, _ = fleet_proof
    processes[102] = (1, processes[102][1])
    with pytest.raises(recovery.RecoveryBlocked, match="another DRadar runner"):
        recovery._check_fleet_claim_processes(home, "op")


def test_pool_pid_reuse_does_not_authorize_runner(fleet_proof):
    home, identities, _, _ = fleet_proof
    identities[101] = {**identities[101], "start_ticks": 200}
    with pytest.raises(recovery.RecoveryBlocked, match="pool process identity"):
        recovery._check_fleet_claim_processes(home, "op")


def test_identity_drift_during_docker_check_is_rejected(fleet_proof, monkeypatch):
    home, identities, _, _ = fleet_proof
    original = recovery.subprocess.run
    def run(command, **kwargs):
        result = original(command, **kwargs)
        if command[0] == "docker":
            identities[999] = {**identities[999], "start_ticks": 1000}
        return result
    monkeypatch.setattr(recovery.subprocess, "run", run)
    with pytest.raises(recovery.RecoveryBlocked, match="identity or ancestry changed"):
        recovery._check_fleet_claim_processes(home, "op")


def test_owned_docker_job_uses_real_reservation_contract(fleet_proof):
    home, identities, _, containers = fleet_proof
    job = home / "work/jobs/fixture-job"
    trial = job / "fixture__abc123"
    trial.mkdir(parents=True)
    session, batch = "a" * 32, "b" * 32
    journal = capacity_journal.CapacityJournal(home, session_id=session, server="http://fixture")
    journal.bind(batch)
    journal._update(lambda state: state.update(owner_identity=identities[102]))
    assignment = {"assignment_id": "c" * 32, "task_id": "fixture", "batch_id": batch,
                  "owner_epoch": 1, "resume_generation": 0}
    observe = journal.begin_attempt(assignment)
    scope = {**assignment, "runner_session_id": session}
    for event in ("entered", "launch_pending", "spawned"):
        observe({"schema": "dradar.execution_audit.v1", "event": event,
                 "scope": scope, "execution_id": "d" * 32, "job_dir": str(job)})
    containers.append({"Id": "e" * 64, "Config": {"Labels": {
        "com.docker.compose.project": trial.name,
        "com.docker.compose.project.config_files": str(trial / "docker-compose.yaml")}},
        "Mounts": [{"Source": str(trial)}]})
    before = journal.path.read_bytes()
    recovery._check_fleet_claim_processes(home, "op")
    assert journal.path.read_bytes() == before
    # Reusing the owner PID no longer makes this job/container Fleet-owned.
    identities[102] = {**identities[102], "start_ticks": 999}
    with pytest.raises(recovery.RecoveryBlocked, match="non-Fleet DRadar Pier container"):
        recovery._check_fleet_claim_processes(home, "op")
    assert journal.path.read_bytes() == before
