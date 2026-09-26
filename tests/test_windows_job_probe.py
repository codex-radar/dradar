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
        "'env':os.environ.get('DRADAR_JOB_PROBE')}))"
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(45)"])
    job = kernel.CreateJobObjectW(None, None)
    assert job, ctypes.get_last_error()
    limits = ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE; no breakaway
    assert kernel.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits),
    ), ctypes.get_last_error()
    process = thread = None
    bound = False
    try:
        process, thread, _pid, _tid = _winapi.CreateProcess(
            sys.executable,
            subprocess.list2cmdline([sys.executable, "-c", code, str(marker)]),
            None, None, False, 0x00000004,
            dict(os.environ, DRADAR_JOB_PROBE="exact-job"), str(tmp_path),
            subprocess.STARTUPINFO(),
        )
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
        assert json.loads(marker.read_text())["cwd"] == str(tmp_path)
        assert json.loads(marker.read_text())["env"] == "exact-job"
        accounting = BasicAccounting()
        returned = wintypes.DWORD()
        assert kernel.QueryInformationJobObject(
            job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting),
            ctypes.byref(returned),
        ), ctypes.get_last_error()
        assert accounting.TotalProcesses >= 2
        assert accounting.ActiveProcesses >= 1, "descendant escaped after parent exit"
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
        unrelated.terminate()
        unrelated.wait(timeout=5)
