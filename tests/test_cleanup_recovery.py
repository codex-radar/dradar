"""One original cleanup fence can end without inventing a submitted result."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from dradar import (assignment_boundary, capacity_journal as journal,
                    cleanup_recovery, pending, session_recovery)
from dradar.api_client import ApiError
from dradar.ota.integration import pending_upload_count
from test_capacity_journal import AID, BID, SID, ReceiptServer


OWNER = {"pid": 10001, "start_ticks": 123, "host_id": "host", "boot_id": "boot"}
CHILD = {**OWNER, "pid": 10002, "start_ticks": 124}
DAEMON = {"endpoint": "unix:///var/run/docker.sock", "daemon_id": "original"}
SCOPE = {"assignment_id": AID, "task_id": "task", "batch_id": BID,
         "runner_session_id": SID, "owner_epoch": 2, "resume_generation": 0}
EXEC = "d" * 32


class Server(ReceiptServer):
    account_scope = "account"
    benchmark_id = "deep-swe"

    def __init__(self):
        super().__init__()
        self.batch_id = BID
        self.assignment_status = "leased"
        self.disposition = None
        self.lose_disposition_ack = False
        self.other_reservations = []

    def set_batch_id(self, value):
        self.batch_id = value

    def assignment_recovery_status(self, aid):
        assert aid == AID
        return {"recovery_evidence_version": 1, "assignment_id": AID,
                "batch_id": BID, "benchmark_id": "deep-swe", "task_id": "task",
                "model": "gpt-6-sol", "effort": "ultra",
                "status": self.assignment_status, "has_submission": False,
                "start_evidence": "unknown_or_started", "exit_evidence": "unknown"}

    def runner_reservations(self, *, limit=100, after=""):
        assert limit == 100 and after == ""
        return {"schema_version": 1, "reservations": self.other_reservations,
                "next_after": None}

    def recover_unsubmitted_cleanup(self, payload):
        assert payload["assignment_id"] == AID and payload["batch_id"] == BID
        assert payload["session_id"] == SID and payload["owner_epoch"] == 2
        if self.disposition is None:
            self.disposition = {"schema_version": 1, "assignment_id": AID,
                                "batch_id": BID, "session_id": SID,
                                "status": "terminated_unsubmitted",
                                "has_submission": False,
                                "execution_started": "unknown",
                                "request_id": payload["request_id"],
                                "release_evidence_id": payload["release_evidence_id"],
                                "local_journal_sha256": payload["local_journal_sha256"]}
            self.assignment_status = "released"
        else:
            assert self.disposition["request_id"] == payload["request_id"]
        if self.lose_disposition_ack:
            self.lose_disposition_ack = False
            raise ApiError("simulated lost disposition acknowledgement")
        return self.disposition


@pytest.fixture
def case(tmp_path, monkeypatch):
    server = Server()
    job = tmp_path / "work" / "jobs" / f"a{AID}"
    job.mkdir(parents=True)
    (job / "result.json").write_text(json.dumps({
        "id": "diagnostic", "started_at": None, "updated_at": None,
        "finished_at": None, "n_total_trials": 0, "stats": {},
    }))
    trial = job / "task__abc12345"
    trial.mkdir()
    for name in ("config.json", "docker-compose-egress-proxy.json",
                 "docker-compose-mounts.json", "exception.txt", "trial.log"):
        (trial / name).write_text("diagnostic only")
    local = journal.CapacityJournal(tmp_path, session_id=SID, server=server.server)
    local.bind(BID)
    local.bind_generation(3)
    local._update(lambda state: state.update(owner_identity=OWNER))
    observe = local.begin_attempt(SCOPE)
    def event(kind, **fields):
        return {"schema": "dradar.execution_audit.v1", "event": kind,
                "execution_id": EXEC, "scope": SCOPE, **fields}
    observe(event("entered"))
    observe(event("launch_pending"))
    observe(event("spawned", crash_recovery_supported=True,
                  pid=CHILD["pid"], pgid=CHILD["pid"], linux_identity=CHILD,
                  job_dir=str(job), docker_identity=DAEMON))
    observe(event("unknown"))
    def observed(state, _home):
        key, attempt = next(iter(state["attempts"].items()))
        return {key: {
            "observed_at": "2026-09-29T10:00:00Z",
            "prior_events_sha256": hashlib.sha256(journal._canonical(attempt["events"])).hexdigest(),
            "owner_identity": OWNER, "linux_identity": CHILD,
            "process_group": "absent",
            "docker": {"daemon": DAEMON, "containers": [], "running": False},
        }}
    monkeypatch.setattr(session_recovery, "_observe", observed)
    digest = session_recovery.recover(tmp_path, SID, server)["journal_sha256"]
    assert session_recovery.recover(tmp_path, SID, server, execute=True,
                                    expected_digest=digest)["status"] == "released"
    boundary = assignment_boundary.prepare(tmp_path, "deep-swe", [{
        "assignment_id": AID, "task_id": "task", "model": "gpt-6-sol",
        "effort": "ultra", "batch_id": BID,
    }], batch_id=BID, expected_ids=[AID])
    marker = {"record_kind": "cleanup_quarantine", "assignment_id": AID,
              "task_id": "task", "batch_id": BID, "runner_session_id": SID,
              "job_dir": str(job), "owner_epoch": 2, "resume_generation": 0,
              "scope_fingerprint": pending.scope_fingerprint(
                  server=server.server, account_scope=server.account_scope,
                  benchmark_id="deep-swe", batch_id=BID),
              "upload_blocked": "cleanup_unconfirmed"}
    pending.record(tmp_path, marker)
    monkeypatch.setattr(cleanup_recovery, "_client", lambda _cfg: server)
    monkeypatch.setattr(cleanup_recovery, "_load_config", lambda: {"benchmark": "deep-swe"})
    monkeypatch.setattr(cleanup_recovery.sys, "platform", "linux")
    monkeypatch.setattr(cleanup_recovery.runtime_identity, "process_identity",
                        lambda _pid: {**OWNER, "pid": os.getpid()})
    common = {"assignment_id": AID, "benchmark": "deep-swe",
              "batch_id": BID, "session_id": SID, "home": tmp_path}
    return server, job, boundary, common


def test_formal_flow_keeps_job_and_records_unknown_execution(case):
    server, job, boundary, common = case
    home = common["home"]
    before = pending.load(home)
    pre = cleanup_recovery.inspect(**common)
    assert pre["status"] == "ready" and pre["execution_started"] == "unknown"
    assert pending.load(home) == before and pending_upload_count(home) == 1
    result = cleanup_recovery.execute(**common,
                                      inventory_sha256=pre["inventory_sha256"])
    assert result["status"] == "terminated_unsubmitted"
    assert pending.load(home) == [] and pending_upload_count(home) == 0
    assert job.is_dir() and (job / "result.json").is_file()
    state, _ = assignment_boundary.inspect_snapshot(boundary)
    assert state["outcomes"][AID]["outcome"] == "terminated_unsubmitted"
    assert state["outcomes"][AID]["execution_started"] == "unknown"
    assert server.disposition["request_id"] == state["outcomes"][AID]["request_id"]


def test_expired_server_assignment_is_not_restarted(case):
    server, job, boundary, common = case
    server.assignment_status = "expired"
    pre = cleanup_recovery.inspect(**common)
    assert pre["status"] == "ready"
    result = cleanup_recovery.execute(**common,
                                      inventory_sha256=pre["inventory_sha256"])
    assert result["status"] == "terminated_unsubmitted"
    assert job.is_dir() and boundary.is_file()


def test_lost_server_ack_replays_original_request_without_losing_fence(case):
    server, job, boundary, common = case
    pre = cleanup_recovery.inspect(**common)
    server.lose_disposition_ack = True
    with pytest.raises(ApiError):
        cleanup_recovery.execute(**common,
                                 inventory_sha256=pre["inventory_sha256"])
    saved = pending.load(common["home"])[0]
    assert saved["cleanup_recovery"]["request_id"] == server.disposition["request_id"]
    assert job.is_dir() and boundary.is_file()
    assert cleanup_recovery.execute(**common,
                                    inventory_sha256=pre["inventory_sha256"])["status"] == "terminated_unsubmitted"


def test_boundary_change_after_server_receipt_keeps_pending_fence(case, monkeypatch):
    server, job, boundary, common = case
    pre = cleanup_recovery.inspect(**common)
    original = server.recover_unsubmitted_cleanup
    def change_boundary(payload):
        result = original(payload)
        state = json.loads(boundary.read_text())
        state["expected"][AID]["task_id"] = "other-task"
        boundary.write_text(json.dumps(state))
        return result
    monkeypatch.setattr(server, "recover_unsubmitted_cleanup", change_boundary)
    with pytest.raises(assignment_boundary.BoundaryError,
                       match="boundary changed"):
        cleanup_recovery.execute(**common,
                                 inventory_sha256=pre["inventory_sha256"])
    assert server.disposition is not None
    assert pending_upload_count(common["home"]) == 1
    assert job.is_dir()


def test_possible_trial_result_blocks_before_server_mutation(case):
    server, job, boundary, common = case
    trial = job / "task__abc12345"
    (trial / "result.json").write_text("{}")
    with pytest.raises(cleanup_recovery.CleanupRecoveryBlocked,
                       match="possible local result"):
        cleanup_recovery.inspect(**common)
    assert server.disposition is None
    assert pending.load(common["home"])[0]["record_kind"] == "cleanup_quarantine"
    assert boundary.is_file()


def test_unreadable_trial_tree_preserves_quarantine(case):
    server, job, _boundary, common = case
    hidden = job / "trial" / "hidden"
    hidden.mkdir(parents=True)
    (hidden / "model.patch").write_text("preserved result")
    hidden.chmod(0)
    try:
        with pytest.raises(cleanup_recovery.CleanupRecoveryBlocked,
                           match="unreadable"):
            cleanup_recovery.inspect(**common)
    finally:
        hidden.chmod(0o700)
    assert server.disposition is None
    assert pending_upload_count(common["home"]) == 1


def test_changed_inventory_and_wrong_session_fail_closed(case):
    server, job, _boundary, common = case
    pre = cleanup_recovery.inspect(**common)
    (job / "build.log").write_text("changed")
    with pytest.raises(cleanup_recovery.CleanupRecoveryBlocked,
                       match="inventory changed"):
        cleanup_recovery.execute(**common,
                                 inventory_sha256=pre["inventory_sha256"])
    assert server.disposition is None
    with pytest.raises(cleanup_recovery.CleanupRecoveryBlocked,
                       match="quarantine has another result or owner shape"):
        cleanup_recovery.inspect(**{**common, "session_id": "e" * 32})


def test_other_unreleased_pool_session_keeps_ota_blocked(case):
    server, _job, _boundary, common = case
    server.other_reservations = [{"session_id": "e" * 32, "batch_id": BID,
                                  "closed": False, "capacity_released": False}]
    with pytest.raises(cleanup_recovery.CleanupRecoveryBlocked,
                       match="another original session"):
        cleanup_recovery.inspect(**common)
    assert pending_upload_count(common["home"]) == 1
    assert server.disposition is None
