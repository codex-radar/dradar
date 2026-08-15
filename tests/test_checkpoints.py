import argparse
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dradar import artifact_staging, checkpoints, pending, runloop


def _make_checkpoint(
    home: Path,
    assignment_id: str,
    *,
    checkpoint_id: str = "checkpoint-12345678",
    phase: str = "paused",
    generation: int = 0,
    updated_at: str | None = None,
    suffix: str = "one",
) -> checkpoints.Checkpoint:
    job = home / "work" / "jobs" / f"a{assignment_id}-{suffix}"
    checkpoint = job / "task__trial" / "agent" / "checkpoint"
    checkpoint.mkdir(parents=True)
    heartbeat = updated_at or datetime.now(timezone.utc).isoformat()
    manifest = checkpoint / "checkpoint.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "checkpoint_id": checkpoint_id,
        "assignment_id": assignment_id,
        "phase": phase,
        "created_at": "2026-07-16T00:00:00Z",
        "updated_at": heartbeat,
        "last_heartbeat": heartbeat,
        "model": "gpt-test",
        "task_id": "task-1",
        "effort": "high",
        "base_commit": "abc",
        "resume_generation": generation,
        "root_thread_id": "thread-1",
    }))
    return next(item for item in checkpoints.scan(home)
                if item.manifest_path == manifest)


def _assignment(assignment_id: str, generation: int = 0) -> dict:
    return {
        "assignment_id": assignment_id,
        "nonce": "server-only-nonce",
        "task_id": "task-1",
        "model": "gpt-test",
        "effort": "high",
        "resume_generation": generation,
        "checkpoint_id": "checkpoint-12345678",
        "deep_swe_commit": None,
    }


def _args(**overrides):
    values = dict(
        dev_agent=None, keep=False, allow_task_drift=False,
        parallel=False, assignment=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_scan_reads_metadata_but_never_requires_nonce_or_account_token(tmp_path: Path):
    item = _make_checkpoint(tmp_path, "1" * 32, generation=4)
    assert item.valid
    assert item.assignment_id == "1" * 32
    assert item.resume_generation == 4
    raw = item.manifest_path.read_text().lower()
    assert "nonce" not in raw
    assert "account_token" not in raw


def test_corrupt_manifest_infers_assignment_from_job_name(tmp_path: Path):
    assignment_id = "a" * 32
    item = _make_checkpoint(tmp_path, assignment_id)
    item.manifest_path.write_text("{broken")
    loaded = checkpoints.scan(tmp_path)[0]
    assert not loaded.valid
    assert loaded.phase == "invalid"
    assert loaded.assignment_id == assignment_id


def test_cleanup_removes_superseded_copies_but_can_keep_explicit_final_dir(tmp_path: Path):
    aid = "2" * 32
    old = _make_checkpoint(tmp_path, aid, suffix="old")
    new = _make_checkpoint(
        tmp_path, aid, checkpoint_id="checkpoint-abcdefgh", suffix="new",
        updated_at="2026-07-16T02:00:00Z",
    )
    checkpoints.cleanup_assignment(tmp_path, aid, keep_job_dir=new.job_dir)
    assert not old.job_dir.exists()
    assert new.job_dir.is_dir()
    checkpoints.cleanup_assignment(tmp_path, aid)
    assert not new.job_dir.exists()


def test_expiry_uses_checkpoint_heartbeat(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    item = _make_checkpoint(tmp_path, "3" * 32, updated_at=old)
    assert checkpoints.is_expired(item)


def test_assignment_lock_fences_a_second_worker_only_for_same_assignment(tmp_path: Path):
    with checkpoints.assignment_lock(tmp_path, "a1"):
        with pytest.raises(checkpoints.CheckpointBusy):
            with checkpoints.assignment_lock(tmp_path, "a1"):
                pass
        with checkpoints.assignment_lock(tmp_path, "a2"):
            pass


def test_terminal_evidence_is_listed_but_never_selected_for_resume(tmp_path: Path):
    aid = "c" * 32
    item = _make_checkpoint(tmp_path, aid)
    checkpoints.mark_terminal(tmp_path, item)
    assert len(checkpoints.scan(tmp_path)) == 1
    assert checkpoints.is_terminal(tmp_path, checkpoints.scan(tmp_path)[0])
    assert checkpoints.latest_by_assignment(tmp_path) == {}


def test_discarding_terminal_evidence_never_releases_server_lease(
        tmp_path: Path, monkeypatch, capsys):
    aid = "d" * 32
    item = _make_checkpoint(tmp_path, aid)
    checkpoints.mark_terminal(tmp_path, item)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(
        runloop, "_load_config",
        lambda: pytest.fail("terminal evidence removal must stay local"),
    )
    assert runloop.cmd_checkpoint_discard(
        argparse.Namespace(checkpoint_id=aid),
    ) == 0
    assert checkpoints.scan(tmp_path) == []
    assert "server lease left unchanged" in capsys.readouterr().out


class _RecoveryClient:
    def __init__(self, assignment):
        self.assignment = assignment
        self.resumes = []
        self.pauses = []
        self.discards = []

    def checkpoint_pause(self, assignment_id, checkpoint_id, generation):
        self.pauses.append((assignment_id, checkpoint_id, generation))
        return {"ok": True}

    def checkpoint_resume(self, assignment_id, checkpoint_id, generation, session_id=None):
        self.resumes.append((assignment_id, checkpoint_id, generation, session_id))
        resumed = dict(self.assignment, resume_generation=generation + 1)
        return {"assignment": resumed}

    def checkpoint_discard(self, assignment_id, checkpoint_id, generation, reason):
        self.discards.append((assignment_id, checkpoint_id, generation, reason))
        return {"ok": True}

    def get_assignment(self):
        return {"active": [self.assignment] if self.assignment else []}


def test_resume_one_passes_checkpoint_and_new_generation_to_runner(
    tmp_path: Path, monkeypatch,
):
    aid = "4" * 32
    item = _make_checkpoint(tmp_path, aid, generation=2)
    assignment = _assignment(aid, generation=2)
    client = _RecoveryClient(assignment)
    seen = {}
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **k: "base")
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)

    def fake_run(client_, resumed, tasks_root, args, local_commit, **kwargs):
        seen["assignment"] = resumed
        seen["checkpoint"] = kwargs["resume_checkpoint"]
        return "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", fake_run)
    outcome = runloop._resume_one_checkpoint(
        client, item, assignment, _args(), tmp_path / "tasks", None,
    )
    assert outcome == "submitted"
    assert client.resumes[0][2] == 2
    assert seen["assignment"]["resume_generation"] == 3
    assert seen["checkpoint"].checkpoint_id == item.checkpoint_id


def test_resume_registers_queued_then_announces_fenced_owner_after_success(
    tmp_path: Path, monkeypatch,
):
    aid = "d" * 32
    item = _make_checkpoint(tmp_path, aid, generation=2)
    assignment = dict(_assignment(aid, generation=2), batch_id="batch-1")
    events = []

    class Client(_RecoveryClient):
        def checkpoint_resume(self, *args, **kwargs):
            events.append("resume")
            return super().checkpoint_resume(*args, **kwargs)

    class Telemetry:
        session_id = "session-recovery"

        def bind_batch(self, batch_id):
            events.append(("batch", batch_id))

        def set_phase(self, phase, assignment_id=None, resume_generation=None):
            events.append(("phase", phase, assignment_id, resume_generation))

        def flush(self):
            events.append("flush")

    client = Client(assignment)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **k: "base")
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runloop, "_run_and_submit", lambda *a, **k: "submitted")

    assert runloop._resume_one_checkpoint(
        client, item, assignment, _args(), tmp_path / "tasks", Telemetry(),
    ) == "submitted"
    assert events[:6] == [
        ("batch", "batch-1"),
        ("phase", "queued", None, None),
        "flush",
        "resume",
        ("phase", "running", aid, 3),
        "flush",
    ]


def test_checkpoint_recovery_uses_exponential_backoff(tmp_path: Path):
    aid = "7" * 32
    now = datetime.now(timezone.utc)
    item = _make_checkpoint(
        tmp_path, aid, generation=3, updated_at=now.isoformat(),
    )

    assert runloop._checkpoint_backoff_seconds(item, now=now) == 120


def test_checkpoint_recovery_limit_opens_circuit_without_resuming(
        tmp_path: Path, monkeypatch, capsys):
    aid = "8" * 32
    item = _make_checkpoint(
        tmp_path, aid, generation=runloop.MAX_CHECKPOINT_RESUMES,
    )
    assignment = _assignment(aid, generation=runloop.MAX_CHECKPOINT_RESUMES)
    client = _RecoveryClient(assignment)
    abort_file = tmp_path / "ACCOUNT_STOP"
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(abort_file))
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **k: "base")

    outcome = runloop._resume_one_checkpoint(
        client, item, assignment, _args(), tmp_path / "tasks", None,
    )

    assert outcome == "recovery-exhausted"
    assert client.resumes == []
    assert checkpoints.is_terminal(tmp_path, item)
    assert abort_file.read_text() == "drain:checkpoint recovery safety limit reached"
    assert "5-resume safety limit" in capsys.readouterr().out


def test_completed_checkpoint_resume_recovers_missing_staged_patch_without_rerun(
    tmp_path: Path, monkeypatch,
):
    aid = "e" * 32
    item = _make_checkpoint(tmp_path, aid, phase="agent_completed")
    assignment = _assignment(aid)
    patch = item.trial_dir / "artifacts" / "model.patch"
    patch.parent.mkdir(parents=True)
    patch_bytes = b"diff --git a/app.py b/app.py\n-old\n+new\n"
    patch.write_bytes(patch_bytes)
    client = _RecoveryClient(assignment)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    # This is the narrow incident window: the model/Pier finished and wrote
    # the patch, then the runner paused before _upload_trial started.
    assert runloop._pause_checkpoint_quietly(client, assignment) is not None
    prepared = artifact_staging.ensure_staged_patch(item.trial_dir)
    assert client.pauses == [(aid, item.checkpoint_id, 0)]
    prepared.staged.unlink()  # paused/closed after completion, before upload

    class CompletedClient(_RecoveryClient):
        def __init__(self, assignment_):
            super().__init__(assignment_)
            self.submissions = []

        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None):
            self.submissions.append({
                "assignment_id": assignment_id,
                "patch": patch.read_bytes(),
                "meta": meta,
            })
            return {"submission_id": "s-recovered", "grade_status": "pending"}

    client = CompletedClient(assignment)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **k: "base")

    outcome = runloop._resume_one_checkpoint(
        client, item, assignment, _args(yes=True), tmp_path / "tasks", None,
    )

    assert outcome == "submitted"
    assert client.resumes == []  # completed work uploads; the model never reruns
    assert client.submissions[0]["patch"] == patch_bytes
    assert client.submissions[0]["meta"]["artifact_staging_recovery"]["reason"] == (
        "source-present/staged-missing"
    )
    assert pending.load(tmp_path) == []


def test_completed_checkpoint_resume_recovers_workspace_patch_without_rerun(
    tmp_path: Path, monkeypatch, capsys,
):
    aid = "f" * 32
    item = _make_checkpoint(tmp_path, aid, phase="agent_completed")
    assignment = _assignment(aid)
    metadata = json.loads(item.manifest_path.read_text())
    metadata["workspace_patch"] = "workspace.patch"
    item.manifest_path.write_text(json.dumps(metadata))
    patch_bytes = b"diff --git a/model_answer.json b/model_answer.json\n-old\n+new\n"
    (item.checkpoint_dir / "workspace.patch").write_bytes(patch_bytes)
    client = _RecoveryClient(assignment)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **k: "base")

    class CompletedClient(_RecoveryClient):
        def __init__(self, assignment_):
            super().__init__(assignment_)
            self.submissions = []

        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None):
            self.submissions.append(patch.read_bytes())
            return {"submission_id": "s-workspace", "grade_status": "pending"}

    client = CompletedClient(assignment)
    outcome = runloop._resume_one_checkpoint(
        client, item, assignment, _args(yes=True), tmp_path / "tasks", None,
    )

    assert outcome == "submitted"
    assert client.resumes == []
    assert client.submissions == [patch_bytes]
    assert "uploading without rerunning" in capsys.readouterr().out
    assert pending.load(tmp_path) == []


def test_healthy_local_run_holds_assignment_lock_before_checkpoint_resume(
    tmp_path: Path, monkeypatch,
):
    """A checkpoint written by an active first run is not resumable locally."""
    aid = "b" * 32
    item = _make_checkpoint(tmp_path, aid)
    assignment = _assignment(aid)
    client = _RecoveryClient(assignment)
    entered = threading.Event()
    release = threading.Event()
    result = []
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "check_task_content_hash", lambda *a, **k: True)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **k: "base")
    monkeypatch.setattr(runloop, "_pause_checkpoint_quietly", lambda *a, **k: None)
    monkeypatch.setattr(runloop, "_mark_stopped_quietly", lambda *a, **k: None)

    def blocking_trial(*_a, **_kw):
        entered.set()
        assert release.wait(5)
        raise runloop.RunnerError("test stop")

    monkeypatch.setattr(runloop, "run_trial", blocking_trial)
    worker = threading.Thread(target=lambda: result.append(
        runloop._run_and_submit(
            client, assignment, tmp_path / "tasks", _args(), "base",
        )
    ))
    worker.start()
    assert entered.wait(5)
    try:
        assert runloop._resume_one_checkpoint(
            client, item, assignment, _args(), tmp_path / "tasks", None,
        ) == "busy"
        assert client.resumes == []
    finally:
        release.set()
        worker.join(5)
    assert result == ["failed"]


def test_invalid_checkpoint_discards_server_lease_and_all_local_copies(
    tmp_path: Path, monkeypatch,
):
    aid = "5" * 32
    item = _make_checkpoint(tmp_path, aid)
    item.manifest_path.write_text("{broken")
    invalid = checkpoints.scan(tmp_path)[0]
    assignment = _assignment(aid)
    client = _RecoveryClient(assignment)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    outcome = runloop._resume_one_checkpoint(
        client, invalid, assignment, _args(), tmp_path / "tasks", None,
    )
    assert outcome == "discarded"
    assert client.discards[0][3] == "invalid"
    assert checkpoints.scan(tmp_path) == []


def test_cleanup_only_removes_server_settled_unprotected_jobs(
    tmp_path: Path, monkeypatch, capsys,
):
    _make_checkpoint(tmp_path, "6" * 32, suffix="active")
    _make_checkpoint(tmp_path, "7" * 32, suffix="pending")
    _make_checkpoint(tmp_path, "8" * 32, phase="agent_completed", suffix="kept")
    _make_checkpoint(tmp_path, "9" * 32, phase="agent_completed", suffix="settled")
    active = checkpoints.find_latest(tmp_path, "6" * 32)
    pending_item = checkpoints.find_latest(tmp_path, "7" * 32)
    kept = checkpoints.find_latest(tmp_path, "8" * 32)
    settled = checkpoints.find_latest(tmp_path, "9" * 32)
    assert active and pending_item and kept and settled
    checkpoints.mark_kept(tmp_path, kept)
    from dradar import pending
    pending.record(tmp_path, {"assignment_id": pending_item.assignment_id})

    client = _RecoveryClient(_assignment(active.assignment_id))
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda _cfg: client)

    dry = argparse.Namespace(dry_run=True, include_kept=False, yes=True)
    assert runloop.cmd_cleanup(dry) == 0
    assert all(item.job_dir.is_dir() for item in (active, pending_item, kept, settled))

    args = argparse.Namespace(dry_run=False, include_kept=False, yes=True)
    assert runloop.cmd_cleanup(args) == 0
    assert active.job_dir.is_dir()
    assert pending_item.job_dir.is_dir()
    assert kept.job_dir.is_dir()
    assert not settled.job_dir.exists()
    out = capsys.readouterr().out
    assert "protected: 1 active/resumable, 1 pending upload, 1 explicitly kept" in out

    include_kept = argparse.Namespace(dry_run=False, include_kept=True, yes=True)
    assert runloop.cmd_cleanup(include_kept) == 0
    assert not kept.job_dir.exists()
    assert active.job_dir.is_dir() and pending_item.job_dir.is_dir()


def test_cleanup_network_failure_deletes_nothing(tmp_path: Path, monkeypatch, capsys):
    item = _make_checkpoint(tmp_path, "a" * 32, phase="agent_completed")
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_load_config", lambda: {})

    class Offline:
        def get_assignment(self):
            from dradar.api_client import ApiError
            raise ApiError("offline")

    monkeypatch.setattr(runloop, "_client", lambda _cfg: Offline())
    args = argparse.Namespace(dry_run=False, include_kept=True, yes=True)
    assert runloop.cmd_cleanup(args) == 1
    assert item.job_dir.is_dir()
    assert "nothing was deleted" in capsys.readouterr().out
