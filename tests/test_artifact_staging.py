"""Integrity and crash-safety coverage for model.patch staging."""

import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dradar import artifact_staging, pending


PATCH = b"diff --git a/app.py b/app.py\n-old\n+new\n"


def _trial(root: Path, name: str = "trial") -> Path:
    trial = root / name
    staged = trial / artifact_staging.STAGED_RELATIVE
    staged.parent.mkdir(parents=True)
    staged.write_bytes(PATCH)
    return trial


def _entry(stage: artifact_staging.StagedPatch) -> dict:
    return {
        "assignment_id": "a1",
        "trial_dir": str(stage.staged.parents[1]),
        **stage.ledger_fields,
    }


def _process_finish(home: str, trial: str, index: int) -> None:
    stage = artifact_staging.ensure_staged_patch(Path(trial))
    pending.record(Path(home), {
        "assignment_id": f"process-{index}",
        "trial_dir": trial,
        **stage.ledger_fields,
    })


def test_normal_staging_creates_durable_source_manifest_and_digest(tmp_path: Path):
    trial = _trial(tmp_path)

    stage = artifact_staging.ensure_staged_patch(trial)

    assert stage.action == "source-initialized"
    assert stage.source.read_bytes() == PATCH
    assert stage.staged.read_bytes() == PATCH
    manifest = json.loads(
        (trial / artifact_staging.MANIFEST_RELATIVE).read_text()
    )
    assert manifest["sha256"] == stage.sha256
    assert manifest["bytes"] == len(PATCH)
    assert artifact_staging.ensure_staged_patch(trial, _entry(stage)).action == "verified"


def test_interruption_before_atomic_rename_keeps_original_and_is_retryable(
    tmp_path: Path, monkeypatch,
):
    trial = _trial(tmp_path)
    source = trial / artifact_staging.SOURCE_RELATIVE
    staged = trial / artifact_staging.STAGED_RELATIVE
    real_replace = artifact_staging.os.replace

    def interrupted_replace(temp, destination, **kwargs):
        if Path(destination).name == source.name:
            raise OSError("simulated process interruption before rename")
        return real_replace(temp, destination, **kwargs)

    monkeypatch.setattr(artifact_staging.os, "replace", interrupted_replace)
    with pytest.raises(
        artifact_staging.PatchStagingError,
        match="source_atomic_write_failed",
    ):
        artifact_staging.ensure_staged_patch(trial)

    assert staged.read_bytes() == PATCH
    assert not source.exists()
    monkeypatch.undo()
    recovered = artifact_staging.ensure_staged_patch(trial)
    assert recovered.source.read_bytes() == PATCH
    assert recovered.staged.read_bytes() == PATCH


def test_resume_uses_private_source_without_writing_into_shared_directory(tmp_path: Path):
    trial = _trial(tmp_path)
    initial = artifact_staging.ensure_staged_patch(trial)
    initial.staged.unlink()

    recovered = artifact_staging.ensure_staged_patch(trial, _entry(initial))

    assert recovered.action == "source-only"
    assert not recovered.staged.exists()
    assert recovered.data == PATCH
    assert recovered.recovery_telemetry == {
        "schema_version": 1,
        "status": "recovered",
        "reason": "source-present/staged-missing",
        "source_present": True,
        "staged_present": False,
        "patch_bytes": len(PATCH),
    }


def test_process_restart_uses_serialized_ledger_metadata(tmp_path: Path):
    trial = _trial(tmp_path)
    initial = artifact_staging.ensure_staged_patch(trial)
    persisted = json.loads(json.dumps(_entry(initial)))
    initial.staged.unlink()

    recovered = artifact_staging.ensure_staged_patch(trial, persisted)

    assert recovered.action == "source-only"
    assert recovered.sha256 == persisted["patch_sha256"]


def test_digest_mismatch_refuses_upload_without_modifying_either_copy(tmp_path: Path):
    trial = _trial(tmp_path)
    initial = artifact_staging.ensure_staged_patch(trial)
    source_before = initial.source.read_bytes()
    mismatched = b"diff --git a/wrong b/wrong\n"
    initial.staged.write_bytes(mismatched)

    with pytest.raises(
        artifact_staging.PatchStagingError,
        match="staged_digest_mismatch",
    ):
        artifact_staging.ensure_staged_patch(trial, _entry(initial))

    assert initial.source.read_bytes() == source_before
    assert initial.staged.read_bytes() == mismatched


def test_parallel_workers_stage_and_record_without_losing_entries(tmp_path: Path):
    count = 24
    trials = [_trial(tmp_path, f"trial-{index}") for index in range(count)]

    def finish(index: int) -> None:
        stage = artifact_staging.ensure_staged_patch(trials[index])
        pending.record(tmp_path / "home", {
            "assignment_id": f"a{index}",
            "trial_dir": str(trials[index]),
            **stage.ledger_fields,
        })

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(finish, range(count)))

    entries = pending.load(tmp_path / "home")
    assert {entry["assignment_id"] for entry in entries} == {
        f"a{index}" for index in range(count)
    }
    assert all(Path(entry["patch_source_path"]).read_bytes() == PATCH for entry in entries)


def test_parallel_worker_processes_do_not_overwrite_pending_ledger(tmp_path: Path):
    count = 12
    home = tmp_path / "home"
    trials = [_trial(tmp_path, f"process-trial-{index}") for index in range(count)]
    context = multiprocessing.get_context("fork" if hasattr(os, "fork") else "spawn")
    processes = [
        context.Process(
            target=_process_finish,
            args=(str(home), str(trials[index]), index),
        )
        for index in range(count)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    assert {entry["assignment_id"] for entry in pending.load(home)} == {
        f"process-{index}" for index in range(count)
    }


def test_parallel_recovery_of_same_trial_is_idempotent(tmp_path: Path):
    trial = _trial(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        stages = list(pool.map(
            lambda _index: artifact_staging.ensure_staged_patch(trial),
            range(16),
        ))

    assert {stage.sha256 for stage in stages} == {stages[0].sha256}
    assert stages[0].source.read_bytes() == PATCH
    assert stages[0].staged.read_bytes() == PATCH
