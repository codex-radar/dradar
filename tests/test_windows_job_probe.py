"""#0216 B feasibility probe: native Windows sleepers only; no Pier or model."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Win32 job contract")


class BasicAccounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", BasicLimitInformation),
        ("IoInfo", IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def test_suspended_spawn_binds_before_descendant_and_audits_after_parent_exit(tmp_path):
    import _winapi

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    marker = tmp_path / "child-pid.txt"
    code = (
        "import json,os,subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(45)']); "
        "Path(sys.argv[1]).write_text(json.dumps({"
        "'child_pid':p.pid,'cwd':os.getcwd(),"
        "'env':os.environ.get('DRADAR_JOB_PROBE')})); "
        "print('stdout-contract'); print('stderr-contract',file=sys.stderr)"
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(45)"])
    job = kernel.CreateJobObjectW(None, None)
    assert job, ctypes.get_last_error()
    limits = ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE; no breakaway
    assert not kernel.SetInformationJobObject(
        wintypes.HANDLE(0xBAD), 9, ctypes.byref(limits), ctypes.sizeof(limits),
    )
    assert kernel.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits),
    ), ctypes.get_last_error()
    process = thread = None
    bound = False
    import _winapi
    import msvcrt
    log = (tmp_path / "pier.log").open("w+b")
    devnull = open(os.devnull, "rb")
    inherited = []
    try:
        with pytest.raises(OSError):
            _winapi.CreateProcess(
                str(tmp_path / "missing-executable.exe"), "missing-executable.exe",
                None, None, False, 0x00000004, None, None,
                subprocess.STARTUPINFO(),
            )
        assert not marker.exists()
        current = _winapi.GetCurrentProcess()
        for handle in (
            msvcrt.get_osfhandle(devnull.fileno()),
            msvcrt.get_osfhandle(log.fileno()),
        ):
            inherited.append(_winapi.DuplicateHandle(
                current, handle, current, 0, True, _winapi.DUPLICATE_SAME_ACCESS,
            ))
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= _winapi.STARTF_USESTDHANDLES
        startup.hStdInput = inherited[0]
        startup.hStdOutput = startup.hStdError = inherited[1]
        startup.lpAttributeList = {"handle_list": inherited}
        process, thread, _pid, _tid = _winapi.CreateProcess(
            sys.executable,
            subprocess.list2cmdline([sys.executable, "-c", code, str(marker)]),
            None, None, True, 0x00000004,
            dict(os.environ, DRADAR_JOB_PROBE="exact-job"), str(tmp_path),
            startup,
        )
        for handle in inherited:
            _winapi.CloseHandle(handle)
        inherited.clear()
        assert kernel.AssignProcessToJobObject(job, process), ctypes.get_last_error()
        bound = True
        in_job = wintypes.BOOL()
        assert kernel.IsProcessInJob(process, job, ctypes.byref(in_job)), ctypes.get_last_error()
        assert in_job.value
        assert kernel.ResumeThread(thread) != 0xFFFFFFFF, ctypes.get_last_error()

        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "suspended parent did not spawn descendant after resume"
        assert kernel.WaitForSingleObject(process, 10000) == 0, "parent did not exit"
        assert _winapi.GetExitCodeProcess(process) == 0
        assert json.loads(marker.read_text())["cwd"] == str(tmp_path)
        assert json.loads(marker.read_text())["env"] == "exact-job"
        log.flush()
        log.seek(0)
        assert b"stdout-contract" in log.read()
        log.seek(0)
        assert b"stderr-contract" in log.read()
        accounting = BasicAccounting()
        returned = wintypes.DWORD()
        assert kernel.QueryInformationJobObject(
            job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting),
            ctypes.byref(returned),
        ), ctypes.get_last_error()
        assert accounting.TotalProcesses >= 2
        assert accounting.ActiveProcesses >= 1, "descendant escaped after parent exit"
        assert unrelated.poll() is None

        # A failed query or terminate call cannot be interpreted as proof of
        # cleanup. Keep the real Job handle and retry through the exact handle.
        assert not kernel.QueryInformationJobObject(
            wintypes.HANDLE(0xBAD), 1, ctypes.byref(accounting),
            ctypes.sizeof(accounting), ctypes.byref(returned),
        )
        assert not kernel.TerminateJobObject(wintypes.HANDLE(0xBAD), 1)
        assert unrelated.poll() is None

        assert kernel.TerminateJobObject(job, 1), ctypes.get_last_error()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            accounting = BasicAccounting()
            assert kernel.QueryInformationJobObject(
                job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting),
                ctypes.byref(returned),
            ), ctypes.get_last_error()
            if accounting.ActiveProcesses == 0:
                break
            time.sleep(0.02)
        assert accounting.ActiveProcesses == 0
        assert unrelated.poll() is None
    finally:
        for handle in inherited:
            _winapi.CloseHandle(handle)
        if process is not None:
            if bound:
                kernel.TerminateJobObject(job, 1)
            else:
                kernel.TerminateProcess(process, 1)
        if thread is not None:
            _winapi.CloseHandle(thread)
        if process is not None:
            _winapi.CloseHandle(process)
        kernel.CloseHandle(job)
        log.close()
        devnull.close()
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.parametrize("failed_gate", ["assign", "membership", "resume"])
def test_pre_resume_failure_never_executes_child(tmp_path, failed_gate):
    """Failure at any post-create gate kills the suspended child before code runs."""
    import _winapi

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.IsProcessInJob.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL),
    ]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    marker = tmp_path / "must-not-run"
    job = kernel.CreateJobObjectW(None, None)
    assert job
    process = thread = None
    bound = False
    try:
        code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')"
        process, thread, _pid, _tid = _winapi.CreateProcess(
            sys.executable,
            subprocess.list2cmdline([sys.executable, "-c", code, str(marker)]),
            None, None, False, 0x00000004, None, None,
            subprocess.STARTUPINFO(),
        )
        if failed_gate == "assign":
            assert not kernel.AssignProcessToJobObject(wintypes.HANDLE(0xBAD), process)
        else:
            assert kernel.AssignProcessToJobObject(job, process), ctypes.get_last_error()
            bound = True
            if failed_gate == "membership":
                member = wintypes.BOOL()
                assert not kernel.IsProcessInJob(
                    process, wintypes.HANDLE(0xBAD), ctypes.byref(member),
                )
            else:
                assert kernel.ResumeThread(wintypes.HANDLE(0xBAD)) == 0xFFFFFFFF
    finally:
        if process is not None:
            if bound:
                assert kernel.TerminateJobObject(job, 1), ctypes.get_last_error()
            else:
                assert kernel.TerminateProcess(process, 1), ctypes.get_last_error()
            assert kernel.WaitForSingleObject(process, 5000) == 0
        if thread is not None:
            _winapi.CloseHandle(thread)
        if process is not None:
            _winapi.CloseHandle(process)
        kernel.CloseHandle(job)
    assert not marker.exists()


def test_nested_jobs_bind_before_resume_and_stop_only_their_child(tmp_path):
    """Exercise the Windows runner's possible enclosing Job plus an exact child Job."""
    import _winapi

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.IsProcessInJob.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL),
    ]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    marker = tmp_path / "nested-executed"
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(45)"])
    outer = kernel.CreateJobObjectW(None, None)
    inner = kernel.CreateJobObjectW(None, None)
    assert outer and inner
    process = thread = None
    bound_outer = bound_inner = False
    try:
        code = (
            "from pathlib import Path; import sys,time; "
            "Path(sys.argv[1]).write_text('ran'); time.sleep(45)"
        )
        process, thread, _pid, _tid = _winapi.CreateProcess(
            sys.executable,
            subprocess.list2cmdline([sys.executable, "-c", code, str(marker)]),
            None, None, False, 0x00000004, None, None,
            subprocess.STARTUPINFO(),
        )
        assert kernel.AssignProcessToJobObject(outer, process), ctypes.get_last_error()
        bound_outer = True
        assert kernel.AssignProcessToJobObject(inner, process), ctypes.get_last_error()
        bound_inner = True
        for job in (outer, inner):
            member = wintypes.BOOL()
            assert kernel.IsProcessInJob(process, job, ctypes.byref(member))
            assert member.value
        assert kernel.ResumeThread(thread) != 0xFFFFFFFF
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        assert kernel.TerminateJobObject(inner, 1), ctypes.get_last_error()
        assert kernel.WaitForSingleObject(process, 5000) == 0
        assert unrelated.poll() is None
    finally:
        if process is not None:
            if bound_inner:
                kernel.TerminateJobObject(inner, 1)
            elif bound_outer:
                kernel.TerminateJobObject(outer, 1)
            else:
                kernel.TerminateProcess(process, 1)
        if thread is not None:
            _winapi.CloseHandle(thread)
        if process is not None:
            _winapi.CloseHandle(process)
        kernel.CloseHandle(inner)
        kernel.CloseHandle(outer)
        unrelated.terminate()
        unrelated.wait(timeout=5)
