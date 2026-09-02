"""Invocation-scoped repeated runner-failure circuit state."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1
THRESHOLD = 2
_PROCESS_LOCK = threading.Lock()
_LOCAL_STATE: dict = {}


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        path.with_name(f"{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600,
    )
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
    os.lseek(fd, 0, os.SEEK_SET)
    windows_lock = False
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows CI
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            windows_lock = True
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


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return (
        value
        if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION
        else {}
    )


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
    )
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def observe(
    *, scope: str, signature: str | None, state_path: Path | None,
    clear_open: bool = True,
) -> tuple[int, bool]:
    """Record one result; success clears the matching scope's failure streak."""
    def update(state: dict) -> tuple[dict, int, bool]:
        scopes = state.get("scopes")
        if not isinstance(scopes, dict):
            scopes = {}
        if signature is None:
            current = scopes.get(scope)
            if (
                not clear_open and isinstance(current, dict)
                and current.get("open") is True
            ):
                return {
                    "schema_version": SCHEMA_VERSION, "scopes": scopes,
                }, int(current.get("count") or THRESHOLD), True
            scopes.pop(scope, None)
            return {
                "schema_version": SCHEMA_VERSION, "scopes": scopes,
            }, 0, False
        current = scopes.get(scope)
        same = isinstance(current, dict) and current.get("signature") == signature
        count = int(current.get("count") or 0) + 1 if same else 1
        scopes[scope] = {
            "signature": signature, "count": count,
            "open": count >= THRESHOLD,
        }
        return {
            "schema_version": SCHEMA_VERSION, "scopes": scopes,
        }, count, scopes[scope]["open"]

    if state_path is None:
        global _LOCAL_STATE
        with _PROCESS_LOCK:
            _LOCAL_STATE, count, opened = update(_LOCAL_STATE)
        return count, opened
    with _PROCESS_LOCK:
        with _locked(state_path):
            state, count, opened = update(_load(state_path))
            if state.get("scopes"):
                _save(state_path, state)
            else:
                state_path.unlink(missing_ok=True)
            return count, opened


def status(*, scope: str, state_path: Path | None) -> tuple[int, bool]:
    """Read one circuit scope without mutating it."""
    if state_path is None:
        with _PROCESS_LOCK:
            current = _LOCAL_STATE.get("scopes", {}).get(scope, {})
            return int(current.get("count") or 0), current.get("open") is True
    with _PROCESS_LOCK:
        with _locked(state_path):
            current = _load(state_path).get("scopes", {}).get(scope, {})
            return int(current.get("count") or 0), current.get("open") is True


def clear(*, scope: str | None, state_path: Path) -> None:
    """Explicitly rearm one scope, or every scope in a dedicated file."""
    with _PROCESS_LOCK:
        with _locked(state_path):
            if scope is None:
                state_path.unlink(missing_ok=True)
                return
            state = _load(state_path)
            scopes = state.get("scopes", {})
            scopes.pop(scope, None)
            if scopes:
                _save(state_path, {"schema_version": SCHEMA_VERSION, "scopes": scopes})
            else:
                state_path.unlink(missing_ok=True)


def reset_local_for_tests() -> None:
    global _LOCAL_STATE
    with _PROCESS_LOCK:
        _LOCAL_STATE = {}
