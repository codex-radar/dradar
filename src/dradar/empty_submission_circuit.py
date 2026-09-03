"""Persistent local latch for server-verified completed empty patches."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1
STATE_FILE = "empty-submission-circuits.json"
LOCK_FILE = "empty-submission-circuits.lock"
_PROCESS_LOCK = threading.Lock()


def _scope(
    assignment: dict, client_version: str, account_scope: str | None = None,
) -> dict:
    return {
        "account_scope": account_scope,
        "benchmark_id": assignment.get("benchmark_id") or "deep-swe",
        "harness": assignment.get("agent") or "codex",
        "provider": assignment.get("provider"),
        "model": assignment.get("model"),
        "effort": assignment.get("effort"),
        "client_version": client_version,
        "agent_version": assignment.get("agent_version"),
    }


def _key(scope: dict) -> str:
    return json.dumps(scope, sort_keys=True, separators=(",", ":"))


@contextmanager
def _locked(home: Path) -> Iterator[None]:
    home.mkdir(parents=True, exist_ok=True)
    with _PROCESS_LOCK:
        fd = os.open(home / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
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
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
            os.close(fd)


def _load(home: Path) -> dict:
    try:
        value = json.loads((home / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "circuits": {}}
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return {"schema_version": SCHEMA_VERSION, "circuits": {}}
    circuits = value.get("circuits")
    if not isinstance(circuits, dict):
        value["circuits"] = {}
    return value


def _save(home: Path, state: dict) -> None:
    fd, raw_tmp = tempfile.mkstemp(
        dir=home, prefix=f".{STATE_FILE}.", suffix=".tmp",
    )
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, home / STATE_FILE)
    finally:
        tmp.unlink(missing_ok=True)


def open_for(
    home: Path, assignment: dict, client_version: str, *,
    account_scope: str | None = None,
) -> bool:
    """Read without creating state, for pre-claim/pre-checkout checks."""
    state = _load(home)
    return _key(_scope(
        assignment, client_version, account_scope,
    )) in state["circuits"]


def open_for_claim(
    home: Path, cell: dict, client_version: str, *,
    account_scope: str | None = None,
    conservative_missing_runtime: bool = False,
) -> bool:
    """Match a not-yet-claimed menu cell without inventing missing metadata."""
    candidate = _scope(cell, client_version, account_scope)
    for saved in _load(home)["circuits"].values():
        scope = saved.get("scope") if isinstance(saved, dict) else None
        if not isinstance(scope, dict):
            continue
        if any(
            scope.get(key) != candidate.get(key)
            for key in (
                "account_scope", "benchmark_id", "model", "effort",
                "client_version",
            )
        ):
            continue
        optional = ("harness", "provider", "agent_version")
        if any(
            scope.get(key) != candidate.get(key)
            and not (
                conservative_missing_runtime and candidate.get(key) is None
            )
            for key in optional
        ):
            continue
        return True
    return False


def record_empty(
    home: Path, assignment: dict, client_version: str, *,
    account_scope: str | None = None,
) -> None:
    with _locked(home):
        state = _load(home)
        scope = _scope(assignment, client_version, account_scope)
        state["circuits"][_key(scope)] = {
            "scope": scope,
            "assignment_id": assignment.get("assignment_id"),
            "task_id": assignment.get("task_id"),
        }
        _save(home, state)


def record_success(
    home: Path, assignment: dict, client_version: str, *,
    account_scope: str | None = None,
) -> None:
    """A normal non-empty accepted submission explicitly rearms its scope."""
    with _locked(home):
        state = _load(home)
        state["circuits"].pop(_key(_scope(
            assignment, client_version, account_scope,
        )), None)
        if state["circuits"]:
            _save(home, state)
        else:
            (home / STATE_FILE).unlink(missing_ok=True)
