"""Admission must protect paid results independently of the retry queue."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dradar import cli, image_cache, local_jobs, pending, runloop
from dradar.api_client import ApiClient

AID = "a" * 32
BID = "b" * 32


def client_and_assignment():
    assignment = {"assignment_id": AID, "batch_id": BID, "task_id": "t1",
                  "execution_state": "waiting", "started_at": None}
    def transport(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"active": [assignment], "free_pick": True})
    return ApiClient("https://qa.invalid", "drp_synthetic",
                     transport=httpx.MockTransport(transport),
                     benchmark_id="bench", batch_id=BID), assignment


@pytest.mark.parametrize("relative", [
    "t1/artifacts/model.patch", "t1/.dradar/artifact-staging/model.patch.source",
    "t1/.dradar/artifact-staging/manifest.json", "t1/.dradar/host-output/model.patch",
    local_jobs.TERMINAL_MARKER, local_jobs.KEEP_MARKER,
])
@pytest.mark.parametrize("suffix", ["", "-12345"])
def test_historical_result_without_pending_fences_real_admission(
    tmp_path, monkeypatch, relative, suffix,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    client, assignment = client_and_assignment()
    artifact = tmp_path / "work" / "jobs" / ("a" + AID + suffix) / relative
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"synthetic result")
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **k: pytest.fail("paid start"))
    monkeypatch.setattr(runloop, "check_task_content_hash", lambda *a: True)
    assert pending.load(tmp_path) == []
    assert runloop._pool_ready_work_count(client, desired_workers=1) == 0
    assert runloop._acquire_batch(client, True, allow_new_claims=False)[0] == []
    assert runloop._run_and_submit(
        client, assignment, tmp_path / "tasks",
        SimpleNamespace(dev_agent=False, allow_task_drift=False), None,
    ) == "pending-upload"
    assert artifact.read_bytes() == b"synthetic result"


@pytest.mark.parametrize("scope", [None, "old-token-scope", "foreign-scope"])
def test_scope_mismatch_never_authorizes_paid_rerun(tmp_path, monkeypatch, scope):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    client, assignment = client_and_assignment()
    pending.record(tmp_path, {"assignment_id": AID, "batch_id": BID,
                              "scope_fingerprint": scope})
    runloop._mark_pending_scope_required(client)
    assert runloop._pending_uploads_for_scope(client, batch_id=BID) == []
    assert runloop._pool_ready_work_count(client, desired_workers=1) == 0
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **k: pytest.fail("paid start"))
    monkeypatch.setattr(runloop, "check_task_content_hash", lambda *a: True)
    assert runloop._run_and_submit(client, assignment, tmp_path,
        SimpleNamespace(dev_agent=False, allow_task_drift=False), None) == "pending-upload"


@pytest.mark.parametrize("raw", [b"{bad", b"{}", b"[null]", b"[{}]", b"\xff",
    b'[{"assignment_id":"a","ledger_version":99}]',
    b'[{"assignment_id":"a","record_kind":"unknown"}]'])
def test_unknown_ledger_refuses_admission_and_mutation(tmp_path, monkeypatch, raw):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    ledger = tmp_path / "pending_uploads.json"
    ledger.write_bytes(raw)
    client, assignment = client_and_assignment()
    monkeypatch.setattr(runloop, "check_task_content_hash", lambda *a: True)
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **k: pytest.fail("paid start"))
    for operation in (
        lambda: runloop._pool_ready_work_count(client, desired_workers=1),
        lambda: runloop._run_and_submit(client, assignment, tmp_path,
            SimpleNamespace(dev_agent=False, allow_task_drift=False), None),
        lambda: pending.record(tmp_path, {"assignment_id": "new"}),
        lambda: pending.remove(tmp_path, AID),
    ):
        with pytest.raises(pending.PendingLedgerError):
            operation()
        assert ledger.read_bytes() == raw


def test_empty_failed_preparation_does_not_block_normal_start(tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    client, assignment = client_and_assignment()
    job = tmp_path / "work" / "jobs" / ("a" + AID)
    (job / "t1").mkdir(parents=True)
    (job / "t1" / "exception.txt").write_text("synthetic preparation failure")
    assert runloop._pool_ready_work_count(client, desired_workers=1) == 1
    class ModelReached(BaseException):
        pass
    def sentinel(*a, **k):
        raise ModelReached()
    monkeypatch.setattr(runloop, "check_task_content_hash", lambda *a: True)
    monkeypatch.setattr(runloop, "run_trial", sentinel)
    monkeypatch.setattr(image_cache, "remove_trial_builder", lambda *a, **k: (True, None))
    with pytest.raises(ModelReached):
        runloop._run_and_submit(client, assignment, tmp_path,
            SimpleNamespace(dev_agent=False, allow_task_drift=False), None)


def test_retry_upload_reports_missing_ledger_without_claiming_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    client, _ = client_and_assignment()
    job = tmp_path / "work" / "jobs" / ("a" + AID)
    job.mkdir(parents=True)
    local_jobs.mark_kept(tmp_path, job, terminal=True)
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda _: client)
    assert cli.main(["retry-upload"]) == 1
    assert "no pending upload record" in capsys.readouterr().out


def test_symlink_job_is_fenced_without_following_it(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    root = tmp_path / "work" / "jobs"
    root.mkdir(parents=True)
    (root / ("a" + AID)).symlink_to(external, target_is_directory=True)
    assert local_jobs.protected_assignment_ids(tmp_path) == {AID}


def test_incomplete_upload_metadata_is_preserved_without_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    client, _ = client_and_assignment()
    entry = {"assignment_id": AID, "batch_id": BID,
             "scope_fingerprint": runloop._pending_scope_fingerprint(client, batch_id=BID)}
    pending.record(tmp_path, entry)
    path = tmp_path / "pending_uploads.json"
    before = path.read_bytes()
    with pytest.raises(pending.PendingLedgerError):
        runloop._upload_trial(client, entry)
    assert path.read_bytes() == before
    assert runloop._pool_ready_work_count(client, desired_workers=1) == 0


@pytest.mark.parametrize("key,value", [
    ("owner_epoch", "bad"), ("owner_epoch", {}), ("owner_epoch", True),
    ("owner_epoch", -1), ("resume_generation", None),
    ("resume_generation", "0"), ("job_dir", []), ("runner_session_id", 42),
    ("keep", "false"), ("upload_intent", []),
])
def test_malformed_replay_field_does_not_rewrite_ledger(tmp_path, monkeypatch, key, value):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    client, _ = client_and_assignment()
    entry = {"assignment_id": AID, "nonce": "nonce", "task_id": "t1",
             "trial_dir": str(tmp_path / "work" / "jobs" / ("a" + AID) / "t1"),
             key: value}
    pending.record(tmp_path, entry)
    path = tmp_path / "pending_uploads.json"
    original = path.read_bytes()
    with pytest.raises(pending.PendingLedgerError):
        runloop._upload_trial(client, entry)
    assert path.read_bytes() == original
