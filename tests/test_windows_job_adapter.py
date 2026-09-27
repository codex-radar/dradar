"""Platform-neutral safety mapping from an exact Windows Job to A's quarantine."""

import pytest
from types import SimpleNamespace

from dradar import runner, windows_job


def _fake_process(monkeypatch, error=None):
    proc = object.__new__(windows_job.WindowsJobProcess)
    def close_checked():
        if error is not None:
            raise error
    monkeypatch.setattr(proc, "close_checked", close_checked)
    return proc


def test_unknown_job_audit_keeps_exact_container_cleanup_and_quarantines(
    tmp_path, monkeypatch,
):
    job_dir = tmp_path / "exact-job"
    proc = _fake_process(monkeypatch, windows_job.WindowsJobError(
        "injected unknown Job query", cleanup_unknown=True,
    ))
    inspected = []
    monkeypatch.setattr(runner, "_cleanup_terminated_pier_containers",
                        lambda path: inspected.append(path) or runner.PierContainerCleanup())
    monkeypatch.setattr(runner, "_confirm_terminated_pier_containers_absent", lambda path: None)
    with pytest.raises(runner.RunnerCleanupUnconfirmedError, match="result is unknown") as caught:
        runner._finalize_pier_process(proc, job_dir)
    assert caught.value.job_dir == job_dir
    assert inspected == [job_dir]


def test_exact_job_container_residue_quarantines_even_after_process_exit(
    tmp_path, monkeypatch,
):
    job_dir = tmp_path / "exact-job"
    proc = _fake_process(monkeypatch)
    monkeypatch.setattr(runner, "_cleanup_terminated_pier_containers",
                        lambda path: runner.PierContainerCleanup(matched=1, running=1))
    monkeypatch.setattr(runner, "_confirm_terminated_pier_containers_absent", lambda path: None)
    with pytest.raises(runner.RunnerCleanupUnconfirmedError, match="result is unknown"):
        runner._finalize_pier_process(proc, job_dir)


def test_unknown_spawn_audits_exact_containers_and_keeps_job_dir(tmp_path, monkeypatch):
    job_dir = tmp_path / "exact-job"
    inspected = []
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(windows_job.WindowsJobProcess, "spawn", lambda *args, **kwargs: (
        (_ for _ in ()).throw(windows_job.WindowsJobError(
            "injected post-resume failure", cleanup_unknown=True,
        ))
    ))
    monkeypatch.setattr(runner, "_cleanup_terminated_pier_containers",
                        lambda path: inspected.append(path) or runner.PierContainerCleanup())
    with pytest.raises(runner.RunnerCleanupUnconfirmedError) as caught:
        runner._spawn_pier_process([], None, tmp_path, {}, job_dir=job_dir)
    assert caught.value.job_dir == job_dir
    assert inspected == [job_dir]


@pytest.mark.parametrize("change", ["job", "pid", "epoch", "generation"])
def test_wrong_job_or_generation_cannot_seal_capacity(tmp_path, change):
    from dradar import capacity_journal
    from dradar.execution_audit import ExecutionAudit, ExecutionObserverError
    from test_capacity_journal import ReceiptServer, SID, BID, SCOPE
    journal = capacity_journal.CapacityJournal(tmp_path, session_id=SID, server=ReceiptServer.server)
    journal.bind(BID)
    assignment = dict(SCOPE, owner_epoch=4, resume_generation=2)
    observe = journal.begin_attempt(assignment)
    audit = ExecutionAudit(assignment, tmp_path, observe)
    audit.emit("entered", execution_started=False)
    audit.pending("job", tmp_path / "job")
    audit.record_spawn(123, windows_job_id="e" * 32)
    if change == "job":
        audit.windows_job_id = "f" * 32
    elif change == "pid":
        audit.pid = 124
    elif change == "epoch":
        audit.scope["owner_epoch"] = 5
    else:
        audit.scope["resume_generation"] = 3
    with pytest.raises(ExecutionObserverError):
        audit.absent()
    assert not journal.seal(close_seq=3, reason="unknown")


@pytest.mark.parametrize("pid", [None, 0, -1, True, "123"])
def test_missing_windows_pid_cannot_become_exit_evidence(tmp_path, pid):
    from dradar import capacity_journal
    from dradar.execution_audit import ExecutionAudit, ExecutionObserverError
    from test_capacity_journal import prepare, event, SCOPE
    local = prepare(tmp_path)
    observe = local.begin_attempt(SCOPE)
    audit = ExecutionAudit(SCOPE, tmp_path, observe)
    audit.emit("entered", execution_started=False)
    audit.pending("job", tmp_path / "job")
    with pytest.raises(ExecutionObserverError, match="process identity"):
        audit.record_spawn(pid, windows_job_id="e" * 32)

    # A bad adapter can bypass the producer. The journal consumer must also
    # reject matching-but-invalid identities before creating a release body.
    observe(event("spawned", execution_id=audit.execution_id, pid=pid,
                  windows_job_id="e" * 32, process_identity_kind="exact_windows_job"))
    with pytest.raises(capacity_journal.CapacityEvidenceError):
        observe(event("confirmed_absent", execution_id=audit.execution_id,
                      pid=pid, windows_job_id="e" * 32, process_group="absent",
                      exact_job_containers="absent",
                      evidence_kind="windows_job_and_exact_job_docker_recheck_v1"))
    assert not local.seal(close_seq=3, reason="unknown")


@pytest.mark.parametrize("pid", [None, 0, -1, True, "123"])
def test_saved_windows_exit_with_invalid_pid_cannot_release(tmp_path, pid):
    import json
    from dradar import capacity_journal
    from dradar.execution_audit import ExecutionAudit
    from test_capacity_journal import prepare, SCOPE, ReceiptServer
    local = prepare(tmp_path)
    audit = ExecutionAudit(SCOPE, tmp_path, local.begin_attempt(SCOPE))
    audit.emit("entered", execution_started=False)
    audit.pending("job", tmp_path / "job")
    audit.record_spawn(123, windows_job_id="e" * 32)
    audit.absent()
    saved = json.loads(local.path.read_text())
    for attempt in saved["attempts"].values():
        for item in attempt["events"]:
            if item["event"] in {"spawned", "confirmed_absent"}:
                item["pid"] = pid
    local.path.write_text(json.dumps(saved))
    server = ReceiptServer()
    with pytest.raises(capacity_journal.CapacityEvidenceError):
        capacity_journal.reconcile_file(local.path, server)
    assert server.calls == []
