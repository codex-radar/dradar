"""A one-use, inherited pipe identifies the waiting OTA launcher only."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager

_ENV = "DRADAR_LAUNCHER_HANDOFF_FD"
_supervisor: tuple[int, str] | None = None


def argv_digest(argv: list[str]) -> str:
    return hashlib.sha256(json.dumps(argv, ensure_ascii=True).encode()).hexdigest()


@contextmanager
def handoff():
    read_fd, write_fd = os.pipe()
    try:
        payload = json.dumps({"pid": os.getpid(), "argv_sha256": argv_digest(sys.orig_argv),
                              "args_sha256": argv_digest(sys.argv[1:]),
                              "worker": "--worker-child" in sys.orig_argv}).encode()
        if len(payload) > 4096:
            raise OSError("launcher command is too large to verify")
        os.write(write_fd, payload)
        os.close(write_fd)
        write_fd = -1
        yield read_fd, {**os.environ, _ENV: str(read_fd)}
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def consume(*, verified_child: bool) -> None:
    global _supervisor
    _supervisor = None
    raw = os.environ.pop(_ENV, None)
    if not verified_child or raw is None:
        return
    fd = -1
    try:
        fd = int(raw)
        if fd < 3 or not stat.S_ISFIFO(os.fstat(fd).st_mode):
            return
        os.set_blocking(fd, False)
        payload = os.read(fd, 4097)
        # A live writer, oversize record or malformed marker proves nothing.
        if len(payload) > 4096 or os.read(fd, 1):
            return
        row = json.loads(payload)
        pid, digest = row["pid"], row["argv_sha256"]
        if (type(pid) is int and pid == os.getppid() and pid > 1
                and row.get("args_sha256") == argv_digest(sys.argv[1:])
                and isinstance(digest, str) and len(digest) == 64
                and all(c in "0123456789abcdef" for c in digest)
                and row.get("worker") is False):
            _supervisor = (pid, digest)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    finally:
        if fd >= 3:
            try:
                os.close(fd)
            except OSError:
                pass


def supervisor() -> tuple[int, str] | None:
    if _supervisor is not None and _supervisor[0] == os.getppid():
        return _supervisor
    return None
