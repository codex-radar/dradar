"""Carry the running CLI payload across local process boundaries.

OTA selection and signature verification belong to the launcher. Children reuse
that capability, never the installed module or a newly selected shared pointer.
"""
from __future__ import annotations

import atexit
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

_WINDOWS_HANDLE_ENV = "DRADAR_OTA_PAYLOAD_HANDLE"
_windows_pin = None


def _fd() -> int | None:
    match = re.fullmatch(r"/dev/fd/([1-9][0-9]*)", sys.argv[0])
    if os.name == "nt" or match is None:
        return None
    descriptor = int(match.group(1))
    # A lost OTA capability is an error, never permission to run old code.
    os.fstat(descriptor)
    try:
        with zipfile.ZipFile(f"/dev/fd/{descriptor}") as archive:
            if archive.testzip() is not None:
                raise OSError("running DRadar payload is damaged")
    except (zipfile.BadZipFile, EOFError) as exc:
        raise OSError("running DRadar payload is damaged") from exc
    return descriptor


def _archive() -> Path | None:
    path = Path(sys.argv[0])
    if path.suffix.lower() == ".pyz":
        if not path.is_file() or not zipfile.is_zipfile(path):
            raise OSError("running DRadar payload is missing or damaged")
        return path.resolve()
    if path.is_file() and zipfile.is_zipfile(path):
        return path.resolve()
    return None


def _windows_payload() -> tuple[Path, int]:
    """Pin a private copy before a legacy outer launcher's handle can close."""
    global _windows_pin
    if _windows_pin is None:
        import msvcrt
        from .ota.integration import _locked_windows_candidate, _windows_candidate_fds

        path = _archive()
        if path is None:
            raise OSError("missing running DRadar payload")
        # This also works under old launchers that do not export a handle. The
        # outer launcher still denies replacement while these bytes are read.
        context = _locked_windows_candidate(path.read_bytes())
        pinned = context.__enter__()
        handle = msvcrt.get_osfhandle(_windows_candidate_fds[str(pinned)])
        _windows_pin = (pinned, handle)
        atexit.register(context.__exit__, None, None, None)
    return _windows_pin


def command(executable: str | None = None) -> list[str]:
    python = executable or sys.executable
    descriptor = _fd()
    if descriptor is not None:
        return [python, f"/dev/fd/{descriptor}"]
    path = _archive()
    if path is not None:
        if os.name == "nt":
            path, _ = _windows_payload()
        return [python, str(path)]
    return [python, "-m", "dradar.cli"]


def pass_fds() -> tuple[int, ...]:
    descriptor = _fd()
    return () if descriptor is None else (descriptor,)


def popen_options(env: dict[str, str], *, extra_fds: tuple[int, ...] = ()) -> dict:
    """Merge payload and caller locks, and suppress recursive OTA selection."""
    prefix = command()
    bundled = len(prefix) == 2
    env.pop(_WINDOWS_HANDLE_ENV, None)
    env.pop("DRADAR_OTA_SELF_TEST", None)
    if bundled:
        env["DRADAR_OTA_DISPATCH"] = "1"
    else:
        env.pop("DRADAR_OTA_DISPATCH", None)
    if os.name != "nt":
        descriptors = tuple(dict.fromkeys((*pass_fds(), *extra_fds)))
        return {"pass_fds": descriptors} if descriptors else {}
    if not bundled:
        return {"close_fds": False} if extra_fds else {}
    import msvcrt
    _, handle = _windows_payload()
    handles = [handle, *(msvcrt.get_osfhandle(fd) for fd in extra_fds)]
    for item in handles:
        os.set_handle_inheritable(item, True)
    startup = subprocess.STARTUPINFO()
    startup.lpAttributeList = {"handle_list": list(dict.fromkeys(handles))}
    env[_WINDOWS_HANDLE_ENV] = str(handle)
    return {"startupinfo": startup, "close_fds": True}


def retain_inherited_windows_payload() -> None:
    """Own the inherited replacement-denying handle for this process lifetime."""
    global _windows_pin
    raw = os.environ.pop(_WINDOWS_HANDLE_ENV, None)
    if os.name != "nt" or raw is None:
        return
    import msvcrt
    descriptor = msvcrt.open_osfhandle(int(raw), os.O_RDONLY | os.O_BINARY)
    os.fstat(descriptor)
    path = _archive()
    if path is None:
        os.close(descriptor)
        raise OSError("missing inherited DRadar payload")
    _windows_pin = (path, msvcrt.get_osfhandle(descriptor))
    def release():
        os.close(descriptor)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # Other descendants still own the same replacement-denying handle.
            pass
    atexit.register(release)
