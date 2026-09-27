"""Native no-model contract for the Windows Pier Job adapter."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from dradar import runner, windows_job


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Win32 Job contract")


def _wait_for(path: Path, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()


def test_prebound_pier_job_keeps_descendant_after_parent_exit_and_spares_unrelated(tmp_path):
    marker = tmp_path / "descendant.json"
    log_path = tmp_path / "pier.log"
    code = (
        "import json,os,subprocess,sys; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(45)']); "
        "Path(sys.argv[1]).write_text(json.dumps({"
        "'pid':p.pid,'cwd':os.getcwd(),"
        "'env':os.environ.get('DRADAR_JOB_PRODUCT')})); "
        "print('stdout-contract');print('stderr-contract',file=sys.stderr)"
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(45)"])
    proc = None
    try:
        with log_path.open("w+b") as log:
            proc = runner._spawn_pier_process(
                [sys.executable, "-c", code, str(marker)], log, tmp_path,
                dict(os.environ, DRADAR_JOB_PRODUCT="exact"),
                job_dir=tmp_path / "exact-job",
            )
            assert isinstance(proc, windows_job.WindowsJobProcess)
            assert proc.wait(timeout=10) == 0
            _wait_for(marker)
            data = json.loads(marker.read_text())
            assert data["cwd"] == str(tmp_path)
            assert data["env"] == "exact"
            assert proc.active_processes() >= 1
            log.flush()
            log.seek(0)
            output = log.read()
            assert b"stdout-contract" in output
            assert b"stderr-contract" in output
            assert unrelated.poll() is None
            assert runner._terminate_pier_process_tree(proc)
            runner._confirm_pier_process_tree_stopped(proc)
            assert proc.active_processes() == 0
            proc.close_checked()
            assert unrelated.poll() is None
    finally:
        if proc is not None and not proc._closed:
            try:
                proc.terminate_tree()
            finally:
                try:
                    proc.close_checked()
                except windows_job.WindowsJobError:
                    pass
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.parametrize("gate", ["create", "configure", "assign", "confirm_member", "resume"])
def test_spawn_gate_failure_never_resumes_model(tmp_path, monkeypatch, gate):
    marker = tmp_path / "must-not-run"
    original = getattr(windows_job._JobApi, gate)

    def fail(self, *args):
        raise windows_job.WindowsJobError(f"injected {gate}")

    monkeypatch.setattr(windows_job._JobApi, gate, fail)
    code = "from pathlib import Path;import sys;Path(sys.argv[1]).write_text('ran')"
    with (tmp_path / "pier.log").open("w+b") as log:
        with pytest.raises(windows_job.WindowsJobError) as caught:
            windows_job.WindowsJobProcess.spawn(
                [sys.executable, "-c", code, str(marker)],
                stdout=log, cwd=tmp_path, env=dict(os.environ),
            )
    assert caught.value.cleanup_unknown == (gate == "resume")
    assert not marker.exists()
    monkeypatch.setattr(windows_job._JobApi, gate, original)


def test_release_failure_after_resume_kills_exact_job_and_quarantines(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    code = "from pathlib import Path;import sys,time;Path(sys.argv[1]).write_text('started');time.sleep(45)"
    import _winapi
    original = _winapi.CloseHandle
    failed = False
    terminations = []
    original_terminate = windows_job._JobApi.terminate

    def record_terminate(self, job):
        terminations.append(job)
        return original_terminate(self, job)

    def fail_once(handle):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected startup handle close")
        return original(handle)

    monkeypatch.setattr(_winapi, "CloseHandle", fail_once)
    monkeypatch.setattr(windows_job._JobApi, "terminate", record_terminate)
    with (tmp_path / "pier.log").open("w+b") as log:
        with pytest.raises(windows_job.WindowsJobError) as caught:
            windows_job.WindowsJobProcess.spawn(
                [sys.executable, "-c", code, str(marker)],
                stdout=log, cwd=tmp_path, env=dict(os.environ),
            )
    assert failed
    assert terminations
    assert caught.value.cleanup_unknown


def test_process_handle_close_error_still_closes_job(tmp_path, monkeypatch):
    import _winapi
    with (tmp_path / "pier.log").open("w+b") as log:
        proc = windows_job.WindowsJobProcess.spawn(
            [sys.executable, "-c", "pass"], stdout=log, cwd=tmp_path,
            env=dict(os.environ),
        )
        assert proc.wait(timeout=5) == 0
        original = _winapi.CloseHandle
        failed = False

        def fail_process_once(handle):
            nonlocal failed
            if handle == proc._process and not failed:
                failed = True
                raise OSError("injected process handle close")
            return original(handle)

        monkeypatch.setattr(_winapi, "CloseHandle", fail_process_once)
        with pytest.raises(windows_job.WindowsJobError) as caught:
            proc.close_checked()
        assert failed
        assert caught.value.cleanup_unknown
        assert proc._closed
        with pytest.raises(windows_job.WindowsJobError):
            proc.close_checked()  # A repeated finalizer cannot erase failed evidence.
        # The injected error did not close this handle; the test owns it now.
        original(proc._process)


def test_unknown_query_and_termination_are_not_reported_as_clean(tmp_path, monkeypatch):
    code = "import time;time.sleep(45)"
    with (tmp_path / "pier.log").open("w+b") as log:
        proc = windows_job.WindowsJobProcess.spawn(
            [sys.executable, "-c", code], stdout=log, cwd=tmp_path,
            env=dict(os.environ),
        )
        monkeypatch.setattr(proc._api, "active", lambda _job: (
            (_ for _ in ()).throw(windows_job.WindowsJobError("injected query"))
        ))
        with pytest.raises(windows_job.WindowsJobError) as caught:
            proc.close_checked()
        assert caught.value.cleanup_unknown
        # The failed audit still made a best-effort exact Job terminate.
        assert proc._closed

        second = windows_job.WindowsJobProcess.spawn(
            [sys.executable, "-c", code], stdout=log, cwd=tmp_path,
            env=dict(os.environ),
        )
        monkeypatch.setattr(second._api, "terminate", lambda _job: (
            (_ for _ in ()).throw(windows_job.WindowsJobError("injected terminate"))
        ))
        with pytest.raises(windows_job.WindowsJobError) as caught:
            second.close_checked()
        assert caught.value.cleanup_unknown
        assert second._closed


def test_host_exit_closes_job_and_stops_child(tmp_path):
    """KILL_ON_JOB_CLOSE is the last resort if the DRadar host exits abruptly."""
    marker = tmp_path / "child-pid"
    ready = tmp_path / "child-ready"
    host_script = tmp_path / "host.py"
    host_script.write_text(
        "import os,sys,time\n"
        "from pathlib import Path\n"
        "from dradar.windows_job import WindowsJobProcess\n"
        "marker=Path(sys.argv[1])\n"
        "code='from pathlib import Path;import sys,time;Path(sys.argv[1]).write_text(\"ready\");time.sleep(45)'\n"
        "with (marker.parent/'host.log').open('w+b') as log:\n"
        " p=WindowsJobProcess.spawn([sys.executable,'-c',code,str(marker.parent/'child-ready')],stdout=log,cwd=marker.parent,env=dict(os.environ))\n"
        " deadline=time.monotonic()+5\n"
        " while not (marker.parent/'child-ready').exists() and time.monotonic()<deadline: time.sleep(.02)\n"
        " marker.write_text(str(p.pid))\n"
        " time.sleep(0.2)\n"
        " os._exit(0)\n"
    )
    host = subprocess.Popen([sys.executable, str(host_script), str(marker)])
    assert host.wait(timeout=10) == 0
    _wait_for(marker)
    assert ready.read_text() == "ready"
    pid = int(marker.read_text())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 5000) == 0
        finally:
            kernel.CloseHandle(handle)


@pytest.mark.parametrize("containers_known", [True, False])
def test_native_job_exit_binds_reservation_audit_and_docker_fact(
        tmp_path, monkeypatch, containers_known):
    from dradar import capacity_journal, run_intent
    from dradar.execution_audit import ExecutionAudit
    from test_capacity_journal import ReceiptServer, SID, BID, SCOPE

    assignment = dict(SCOPE, owner_epoch=7, resume_generation=3)
    journal = capacity_journal.CapacityJournal(tmp_path, session_id=SID, server=ReceiptServer.server)
    journal.bind(BID)
    journal.bind_generation(3)
    audit = ExecutionAudit(assignment, tmp_path / "work", journal.begin_attempt(assignment))
    generation = run_intent.begin(tmp_path, BID)
    monkeypatch.setenv(run_intent.BATCH_ENV, BID)
    monkeypatch.setenv(run_intent.GENERATION_ENV, generation)
    job = tmp_path / "exact-job"
    job.mkdir()
    audit.emit("entered", execution_started=False)
    inspected = []
    monkeypatch.setattr(runner, "_cleanup_terminated_pier_containers",
                        lambda path: runner.PierContainerCleanup())

    def containers_absent(path):
        inspected.append(path)
        if not containers_known:
            raise runner.RunnerError("synthetic unavailable Docker inventory")

    monkeypatch.setattr(runner, "_confirm_terminated_pier_containers_absent", containers_absent)
    with (tmp_path / "native.log").open("w+b") as log:
        with run_intent.worker_launch_guard(tmp_path):
            audit.pending(job.name, job)
            proc = runner._spawn_pier_process([sys.executable, "-c", "pass"], log,
                                              tmp_path, dict(os.environ), job_dir=job)
        audit.record_spawn(proc.pid, windows_job_id=proc.job_id)
        assert proc.wait(timeout=5) == 0
        try:
            runner._cleanup_exited_pier_runtime(proc, job)
            runner._finalize_pier_process(proc, job)
        except runner.RunnerCleanupUnconfirmedError:
            audit.unknown("exact_job_container_exit_unconfirmed")
            assert not containers_known
        else:
            audit.absent()
            assert containers_known
        finally:
            if not proc._closed:
                proc.close_checked()
    assert inspected and all(path == job for path in inspected)
    assert journal.seal(close_seq=10, reason="completed") is containers_known
    server = ReceiptServer()
    result = capacity_journal.reconcile_saved(tmp_path, server, batch_id=BID)
    assert result["released"] == int(containers_known)
    assert result["unknown"] == int(not containers_known)
    saved = json.loads(journal.path.read_text())
    attempt, = saved["attempts"].values()
    if containers_known:
        spawn, exit_event = [event for event in attempt["events"] if event["event"] in {"spawned", "confirmed_absent"}]
        assert spawn["windows_job_id"] == exit_event["windows_job_id"] == proc.job_id
        assert exit_event["evidence_kind"] == "windows_job_and_exact_job_docker_recheck_v1"
        assert exit_event["scope"]["owner_epoch"] == 7
    else:
        assert not server.calls


def test_stop_before_native_job_launch_runs_no_child(tmp_path, monkeypatch):
    from dradar import run_intent
    from test_capacity_journal import BID
    generation = run_intent.begin(tmp_path, BID)
    monkeypatch.setenv(run_intent.BATCH_ENV, BID)
    monkeypatch.setenv(run_intent.GENERATION_ENV, generation)
    run_intent.stop(tmp_path, BID)
    marker = tmp_path / "must-not-run"
    with (tmp_path / "native.log").open("w+b") as log:
        with pytest.raises(run_intent.IntentStopped):
            with run_intent.worker_launch_guard(tmp_path):
                runner._spawn_pier_process(
                    [sys.executable, "-c", "from pathlib import Path;import sys;Path(sys.argv[1]).touch()", str(marker)],
                    log, tmp_path, dict(os.environ), job_dir=tmp_path / "job")
    assert not marker.exists()
