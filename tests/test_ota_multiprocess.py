"""Real process-level contention proof for the machine-wide OTA lock."""

import multiprocessing
from pathlib import Path

from dradar.ota.state import UpdateLock, UpdateLockBusy


def _contend_for_update_lock(lock_path, ready, release, results):
    ready.wait()
    try:
        with UpdateLock(Path(lock_path), timeout_seconds=0.0):
            results.put("acquired")
            release.wait(timeout=15)
    except UpdateLockBusy:
        results.put("busy")


def test_forty_processes_have_exactly_one_update_lock_owner(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Barrier(41)
    release = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_contend_for_update_lock,
            args=(tmp_path / "update.lock", ready, release, results),
        )
        for _ in range(40)
    ]
    for process in processes:
        process.start()
    ready.wait(timeout=30)
    outcomes = [results.get(timeout=30) for _ in range(40)]
    release.set()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert outcomes.count("acquired") == 1
    assert outcomes.count("busy") == 39
