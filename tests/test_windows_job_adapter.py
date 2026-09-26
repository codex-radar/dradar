"""Platform-neutral safety mapping from an exact Windows Job to A's quarantine."""

import pytest

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
    with pytest.raises(runner.RunnerCleanupUnconfirmedError, match="result is unknown"):
        runner._finalize_pier_process(proc, job_dir)
