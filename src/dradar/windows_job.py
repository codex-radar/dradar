"""Exact Windows Job lifecycle for the one Pier child process.

The primary thread stays suspended until the process is in this Job.  This
module deliberately does not launch Docker containers: the Docker daemon and
BuildKit require the runner's separate exact-job container audit.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import time
import uuid
from typing import Sequence


_CREATE_SUSPENDED = 0x00000004
_KILL_ON_JOB_CLOSE = 0x00002000
_BASIC_ACCOUNTING = 1
_EXTENDED_LIMIT = 9
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_RESUME_FAILED = 0xFFFFFFFF


class WindowsJobError(RuntimeError):
    def __init__(self, message: str, *, cleanup_unknown: bool = False):
        super().__init__(message)
        self.cleanup_unknown = cleanup_unknown


class _BasicAccounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _BasicLimits(ctypes.Structure):
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


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobApi:
    """Narrow native API surface; all failures remain explicit to the caller."""

    def __init__(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
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
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel

    @staticmethod
    def _error(stage: str) -> WindowsJobError:
        return WindowsJobError(f"Windows Pier {stage} failed (WinError {ctypes.get_last_error()})")

    def create(self):
        job = self.kernel.CreateJobObjectW(None, None)
        if not job:
            raise self._error("Job creation")
        return job

    def configure(self, job) -> None:
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(
            job, _EXTENDED_LIMIT, ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            raise self._error("Job limits")

    def assign(self, job, process) -> None:
        if not self.kernel.AssignProcessToJobObject(job, process):
            raise self._error("Job assignment")

    def confirm_member(self, job, process) -> None:
        member = wintypes.BOOL()
        if not self.kernel.IsProcessInJob(process, job, ctypes.byref(member)):
            raise self._error("Job membership query")
        if not member.value:
            raise WindowsJobError("Windows Pier process is not in its exact Job")

    def resume(self, thread) -> None:
        if self.kernel.ResumeThread(thread) == _RESUME_FAILED:
            raise self._error("primary thread resume")

    def active(self, job) -> int:
        accounting = _BasicAccounting()
        returned = wintypes.DWORD()
        if not self.kernel.QueryInformationJobObject(
            job, _BASIC_ACCOUNTING, ctypes.byref(accounting),
            ctypes.sizeof(accounting), ctypes.byref(returned),
        ):
            raise self._error("Job process-count query")
        if returned.value < ctypes.sizeof(accounting):
            raise WindowsJobError("Windows Pier Job process-count reply was short")
        return int(accounting.ActiveProcesses)

    def terminate(self, job) -> None:
        if not self.kernel.TerminateJobObject(job, 1):
            raise self._error("Job termination")

    def close(self, job) -> None:
        if not self.kernel.CloseHandle(job):
            raise self._error("Job handle close")


def _duplicate_std_handles(stdout):
    """Match Popen's explicit std-handle inheritance without leaking extras."""
    import _winapi
    import msvcrt

    current = _winapi.GetCurrentProcess()
    stdin = _winapi.GetStdHandle(_winapi.STD_INPUT_HANDLE)
    devnull = None
    if stdin is None or stdin in (0, -1):
        devnull = open(os.devnull, "rb")
        stdin = msvcrt.get_osfhandle(devnull.fileno())
    handles = []
    try:
        for source in (stdin, msvcrt.get_osfhandle(stdout.fileno())):
            handles.append(_winapi.DuplicateHandle(
                current, source, current, 0, True, _winapi.DUPLICATE_SAME_ACCESS,
            ))
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= _winapi.STARTF_USESTDHANDLES
        startup.hStdInput = handles[0]
        startup.hStdOutput = startup.hStdError = handles[1]
        # Match subprocess.Popen's console pseudo-handle exception.  Those
        # handles are valid as std handles but invalid in the attribute list.
        startup.lpAttributeList = {"handle_list": [
            handle for handle in handles
            if handle & 0x3 != 0x3
            or _winapi.GetFileType(handle) != _winapi.FILE_TYPE_CHAR
        ]}
        return startup, handles, devnull
    except BaseException:
        for handle in handles:
            _winapi.CloseHandle(handle)
        if devnull is not None:
            devnull.close()
        raise


class WindowsJobProcess:
    """Popen-sized interface backed by exact Job and process handles."""

    def __init__(self, args: Sequence[str], pid: int, process, job, api: _JobApi):
        self.args = list(args)
        self.pid = pid
        self._process = process
        self._job = job
        self._api = api
        self.returncode: int | None = None
        self._closed = False
        self._close_error = None
        self.job_id = uuid.uuid4().hex

    @classmethod
    def spawn(cls, args: Sequence[str], *, stdout, cwd: Path, env: dict[str, str]):
        if os.name != "nt":
            raise WindowsJobError("Windows Job spawn requires native Windows")
        import _winapi

        api = _JobApi()
        job = process = thread = None
        startup = None
        handles = []
        devnull = None
        bound = False
        resumed = False
        resume_attempted = False
        failure = None
        created = None
        try:
            job = api.create()
            api.configure(job)
            startup, handles, devnull = _duplicate_std_handles(stdout)
            command = subprocess.list2cmdline([os.fspath(arg) for arg in args])
            process, thread, pid, _tid = _winapi.CreateProcess(
                None, command, None, None, True, _CREATE_SUSPENDED,
                env, os.fspath(cwd), startup,
            )
            api.assign(job, process)
            bound = True
            api.confirm_member(job, process)
            resume_attempted = True
            api.resume(thread)
            resumed = True
            created = cls(args, pid, process, job, api)
        except BaseException as exc:
            failure = exc
        release_errors = []
        for handle in handles:
            try:
                _winapi.CloseHandle(handle)
            except BaseException as exc:
                release_errors.append(exc)
        if devnull is not None:
            try:
                devnull.close()
            except BaseException as exc:
                release_errors.append(exc)
        if thread is not None:
            try:
                _winapi.CloseHandle(thread)
            except BaseException as exc:
                release_errors.append(exc)

        if failure is not None or release_errors:
            cleanup_unknown = False
            if process is not None:
                try:
                    if bound:
                        api.terminate(job)
                    else:
                        _winapi.TerminateProcess(process, 1)
                    if _winapi.WaitForSingleObject(process, 5000) != _WAIT_OBJECT_0:
                        cleanup_unknown = True
                    if bound and api.active(job) != 0:
                        cleanup_unknown = True
                except BaseException:
                    cleanup_unknown = True
                try:
                    _winapi.CloseHandle(process)
                except BaseException:
                    cleanup_unknown = True
            if job is not None:
                try:
                    api.close(job)  # KILL_ON_JOB_CLOSE is the final fallback
                except BaseException:
                    cleanup_unknown = True
            if isinstance(failure, WindowsJobError):
                message = str(failure)
            elif failure is not None:
                message = f"Windows Pier process creation failed ({type(failure).__name__})"
            else:
                message = "Windows Pier startup handle release failed"
            raise WindowsJobError(
                message, cleanup_unknown=cleanup_unknown or resume_attempted,
            ) from (failure or release_errors[0])
        assert created is not None
        return created

    def poll(self) -> int | None:
        import _winapi
        if self.returncode is None:
            result = _winapi.WaitForSingleObject(self._process, 0)
            if result == _WAIT_OBJECT_0:
                self.returncode = _winapi.GetExitCodeProcess(self._process)
            elif result != _WAIT_TIMEOUT:
                raise WindowsJobError("Windows Pier process status query failed")
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        import _winapi
        if self.returncode is not None:
            return self.returncode
        milliseconds = _winapi.INFINITE if timeout is None else max(0, int(timeout * 1000))
        result = _winapi.WaitForSingleObject(self._process, milliseconds)
        if result == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if result != _WAIT_OBJECT_0:
            raise WindowsJobError("Windows Pier process wait failed")
        self.returncode = _winapi.GetExitCodeProcess(self._process)
        return self.returncode

    def active_processes(self) -> int:
        if self._closed:
            raise WindowsJobError("Windows Pier Job handle is closed", cleanup_unknown=True)
        return self._api.active(self._job)

    def terminate_tree(self) -> None:
        self._api.terminate(self._job)

    def confirm_tree_stopped(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if self.active_processes() == 0:
                self.poll()
                return
            if time.monotonic() >= deadline:
                raise WindowsJobError("Windows Pier Job still has active descendants")
            time.sleep(0.05)

    def terminate(self) -> None:
        self.terminate_tree()

    def kill(self) -> None:
        self.terminate_tree()

    def close_checked(self) -> None:
        """Audit on every exit path before releasing exact process/Job handles."""
        if self._closed:
            if self._close_error:
                raise WindowsJobError(self._close_error, cleanup_unknown=True)
            return
        import _winapi
        problem = None
        try:
            count = self.active_processes()
            if count:
                try:
                    self.terminate_tree()
                    self.confirm_tree_stopped()
                except BaseException as exc:
                    problem = f"Windows Pier descendants could not be confirmed stopped ({type(exc).__name__})"
                else:
                    problem = "Windows Pier descendants remained after leader exit; result is unknown"
        except BaseException as exc:
            problem = f"Windows Pier Job could not be audited ({type(exc).__name__})"
            try:
                self.terminate_tree()
            except BaseException:
                pass
        finally:
            try:
                _winapi.CloseHandle(self._process)
            except BaseException as exc:
                problem = problem or f"Windows Pier process handle close failed ({type(exc).__name__})"
            try:
                self._api.close(self._job)
            except BaseException as exc:
                problem = problem or f"Windows Pier Job handle close failed ({type(exc).__name__})"
            self._closed = True
            self._close_error = problem
        if problem:
            raise WindowsJobError(problem, cleanup_unknown=True)
