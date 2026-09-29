"""The exact normal 276 seal shape must pass the signed 279 recovery entry."""

import hashlib
import json
import sys
import time

import httpx
import pytest

from dradar import capacity_journal, local_config, session_exit_recovery, session_recovery
from dradar.api_client import ApiClient
from dradar.ota import recovery
from test_capacity_journal import event
from test_recover_upload import _signed_package


SID = "a" * 32
BID = "b" * 32
SERVER = "https://qa.invalid"
OWNER = {"host_id": "host", "boot_id": "boot", "pid": 10001, "start_ticks": 123}
CHILD = {**OWNER, "pid": 10002, "start_ticks": 124}
DAEMON = {"endpoint": "unix:///var/run/docker.sock", "daemon_id": "original"}


def _setup(tmp_path, monkeypatch):
    from dradar import legacy_capacity

    home, manifest, package, _ = _signed_package(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "HOME", home)
    monkeypatch.setattr(local_config, "HOME", home)
    monkeypatch.setattr(sys, "argv", [str(package), "recover-session-exit"])
    monkeypatch.setattr(session_recovery.sys, "platform", "linux")
    monkeypatch.setattr(session_recovery.runtime_identity, "process_identity",
                        lambda pid: None if pid in (OWNER["pid"], CHILD["pid"])
                        else {**OWNER, "pid": pid})
    monkeypatch.setattr(session_recovery.runtime_identity, "docker_identity", lambda: DAEMON)
    monkeypatch.setattr(session_recovery.os, "killpg",
                        lambda *_: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(session_recovery.subprocess, "run",
                        lambda *_args, **_kwargs: type("Result", (), {"stdout": ""})())
    local = capacity_journal.CapacityJournal(home, session_id=SID, server=SERVER)
    local.bind(BID)
    local._update(lambda state: state.update(owner_identity=OWNER))
    scope = {"assignment_id": "c" * 32, "task_id": "task", "batch_id": BID,
             "runner_session_id": SID}
    observer = local.begin_attempt(scope)
    job = home / "work" / "jobs" / "exact"
    job.mkdir(parents=True)
    for kind in ("entered", "launch_pending"):
        observer(event(kind, scope=scope, job_dir=str(job), platform="posix"))
    observer(event("spawned", scope=scope, job_dir=str(job), platform="posix",
                   crash_recovery_supported=True, pid=CHILD["pid"], pgid=CHILD["pid"],
                   linux_identity=CHILD, docker_identity=DAEMON))
    observer(event("confirmed_absent", scope=scope, job_dir=str(job), platform="posix",
                   pid=CHILD["pid"], pgid=CHILD["pid"], execution_started=True,
                   process_group="absent", exact_job_containers="absent",
                   evidence_kind="private_pgid_and_exact_job_docker_recheck_v1"))
    assert local.seal(close_seq=18, reason="paused")
    state = capacity_journal._read(local.path)
    assert "recovery_source_sha256" not in state
    body = state["close_request"]
    operation = "/api/v1/runner/close"
    scope_hash = hashlib.sha256(json.dumps([SERVER, operation, SID, BID, None],
        separators=(",", ":")).encode()).hexdigest()
    pending_root = home / "pending_session_exits"
    pending_root.mkdir()
    pending_path = pending_root / f"{scope_hash}.json"
    pending = {"schema_version": 1, "scope": scope_hash, "body": body,
               "server": SERVER, "path": operation, "deadline": time.monotonic() - 1,
               "wall_deadline": time.time() - 1, "attempts": 2,
               "receipt_reads": 3, "uncertain": True, "result": None,
               "last_busy": False}
    pending_path.write_text(json.dumps(pending))
    live = {"closed": False, "released": False, "request": None, "posts": []}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"schema_version": 1,
                "session_id": SID, "batch_id": BID, "plan_scoped": False,
                "registration_state": "created", "closed": live["closed"],
                "capacity_released": live["released"], "device_generation": 3,
                "reservation_protocol": 1,
                "release_evidence_id": live["request"]["evidence_id"] if live["request"] else None,
                "release_evidence_sha256": hashlib.sha256(capacity_journal._canonical(live["request"])).hexdigest()
                    if live["request"] else None})
        payload = json.loads(request.read())
        live["posts"].append((request.url.path, payload))
        if request.url.path.endswith("/close"):
            live["closed"] = True
            return httpx.Response(200, json={"ok": True, "closed": True,
                                              "capacity_released": False})
        assert request.url.path.endswith("/release-capacity")
        live["released"] = True
        live["request"] = payload
        return httpx.Response(200, json={"ok": True, "capacity_released": True})

    client = ApiClient(SERVER, "synthetic", capabilities=(),
                       transport=httpx.MockTransport(handler))
    monkeypatch.setattr(legacy_capacity, "_existing_client",
                        lambda _args: (client, {"kind": "account"}, ()))
    args = ["--manifest", str(manifest), "--session-id", SID, "--batch-id", BID]
    return home, local, pending_path, live, args


def test_signed_entry_normal_seal_preflight_then_http_close_release(tmp_path, monkeypatch, capsys):
    home, local, pending_path, live, args = _setup(tmp_path, monkeypatch)
    journal_before, pending_before = local.path.read_bytes(), pending_path.read_bytes()
    assert recovery.main_session_exit(args) == 0
    preflight = json.loads(capsys.readouterr().out)
    assert preflight["status"] == "ready" and len(preflight["observations"]) == 1
    assert local.path.read_bytes() == journal_before
    assert pending_path.read_bytes() == pending_before and live["posts"] == []
    assert recovery.main_session_exit(args + ["--execute", "--journal-sha256",
                                                preflight["journal_sha256"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "released"
    assert [path for path, _ in live["posts"]] == [
        "/api/v1/runner/close", "/api/v1/runner/release-capacity"]
    assert live["posts"][0][1] == json.loads(journal_before)["close_request"]
    assert capacity_journal._read(local.path)["released"] is True
    assert json.loads(pending_path.read_text())["explicit_replay_rounds"] == 1


def test_signed_entry_crash_recheck_form_keeps_its_seal(tmp_path, monkeypatch, capsys):
    _, local, _, live, args = _setup(tmp_path, monkeypatch)
    state = json.loads(local.path.read_text())
    attempt = next(iter(state["attempts"].values()))
    spawn = attempt["events"][-2]
    prior = attempt["events"][:-1]
    attempt["events"][-1] = {
        **attempt["events"][-1], "event": "recovered_absent",
        "evidence_kind": "linux_crash_recheck_v1",
        "recovery": {"prior_events_sha256": hashlib.sha256(
            capacity_journal._canonical(prior)).hexdigest(),
            "process_group": "absent", "linux_identity": spawn["linux_identity"],
            "owner_identity": state["owner_identity"],
            "docker": {"daemon": DAEMON, "running": False}},
    }
    attempt["status"] = "recovered_absent"
    state["execution_manifest"]["attempts"] = state["attempts"]
    state["release_request"]["execution_manifest_sha256"] = hashlib.sha256(
        capacity_journal._canonical(state["execution_manifest"])).hexdigest()
    state["recovery_source_sha256"] = "f" * 64
    state["recovery_seal_sha256"] = capacity_journal._recovery_seal_digest(state)
    local.path.write_text(json.dumps(state))
    capacity_journal._read(local.path)
    assert recovery.main_session_exit(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert live["posts"] == []
    state["recovery_seal_sha256"] = "0" * 64
    local.path.write_text(json.dumps(state))
    assert recovery.main_session_exit(args) == 2
    assert live["posts"] == []


@pytest.mark.parametrize("change", ["open", "no_request", "manifest", "live_pid",
                                    "container", "pending_body", "pending_budget",
                                    "already_replayed", "disguised_crash"])
def test_signed_entry_rejects_changed_normal_seal_without_post(
    tmp_path, monkeypatch, capsys, change,
):
    _, local, pending_path, live, args = _setup(tmp_path, monkeypatch)
    state = json.loads(local.path.read_text())
    pending = json.loads(pending_path.read_text())
    if change == "open":
        state["state"] = "open"
    elif change == "no_request":
        state["release_request"] = None
    elif change == "manifest":
        state["release_request"]["execution_manifest_sha256"] = "0" * 64
    elif change == "live_pid":
        monkeypatch.setattr(session_recovery.runtime_identity, "process_identity",
                            lambda pid: {**OWNER, "pid": pid})
    elif change == "container":
        row = {"Id": "f" * 64, "Config": {"Labels": {
            "com.docker.compose.project.config_files": str(local.path.parent.parent / "work/jobs/exact/compose.yaml"),
            "com.docker.compose.project": "exact"}}, "Mounts": []}
        monkeypatch.setattr(session_recovery.subprocess, "run",
                            lambda args, **_: type("Result", (), {"stdout":
                                json.dumps([row]) if args[1] == "inspect" else row["Id"]})())
    elif change == "pending_body":
        pending["body"]["seq"] = 19
    elif change == "pending_budget":
        pending.update(attempts=0, receipt_reads=0, deadline=time.monotonic() + 300,
                       wall_deadline=time.time() + 300)
    elif change == "already_replayed":
        pending["explicit_replay_rounds"] = 1
    elif change == "disguised_crash":
        state["recovery_seal_sha256"] = "0" * 64
    local.path.write_text(json.dumps(state))
    pending_path.write_text(json.dumps(pending))
    assert recovery.main_session_exit(args) == 2
    assert live["posts"] == []
    assert "unresolved" in capsys.readouterr().err
