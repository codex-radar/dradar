"""Per-invocation liveness locks; callers serialize registration and activation."""
from __future__ import annotations
import os
import uuid
from pathlib import Path
from .state import UpdateLock


def active_invocations(root: Path) -> bool:
    directory = root / "invocations"
    if directory.is_symlink():
        return True
    if not directory.exists():
        return False
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            return True
        try:
            with UpdateLock(path, timeout_seconds=0):
                pass
        except OSError:
            return True
        except RuntimeError:
            return True
        # Caller holds launch.lock, so registration cannot race stale cleanup.
        path.unlink(missing_ok=True)
    return False


def register_invocation(root: Path):
    directory = root / "invocations"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise OSError("unsafe activity directory")
    return UpdateLock(directory / f"{os.getpid()}-{uuid.uuid4().hex}.lock",
                      timeout_seconds=0)
