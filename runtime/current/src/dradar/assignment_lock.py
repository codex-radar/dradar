"""Cross-process ownership lock for one assignment's paid model attempt."""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class AssignmentBusy(RuntimeError):
    pass


@contextmanager
def lock(home: Path, assignment_id: str) -> Iterator[None]:
    lock_dir = home / "assignment-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{assignment_id}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
    os.lseek(fd, 0, os.SEEK_SET)
    windows_lock = False
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # pragma: no cover - Windows CI
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                windows_lock = True
            except OSError as exc:
                raise AssignmentBusy(
                    f"assignment {assignment_id} already has a local owner"
                ) from exc
        except BlockingIOError as exc:
            raise AssignmentBusy(
                f"assignment {assignment_id} already has a local owner"
            ) from exc
        yield
    finally:
        if windows_lock:  # pragma: no cover - Windows CI
            try:
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        os.close(fd)
