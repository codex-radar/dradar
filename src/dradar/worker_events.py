"""Small structured Pier->CLI lifecycle sidecar protocol.

The sidecar is deliberately separate from Pier's stdout/stderr (which contain
untrusted provider output).  Adapter code emits only a redacted enum and the
opaque runner session id; the CLI validates the record before charging the
runtime lease.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORKER_EVENT_PROTOCOL_VERSION = 1
WORKER_REGISTERED = "worker_registered"
WORKER_EVENT_FILE_ENV = "DRADAR_PIER_WORKER_EVENT_FILE"


async def verify_task_baseline(environment):
    try:
        from _dradar_task_baseline import verify_task_baseline as verify
    except ModuleNotFoundError:
        from dradar.task_baseline import verify_task_baseline as verify
    await verify(environment)


@dataclass(frozen=True)
class WorkerRegistered:
    session_id: str
    client_seq: int
    runtime: str
    context: str
    profile: str
    occurred_at_ms: int

    @property
    def event_id(self) -> str:
        return f"{self.session_id}:{self.client_seq}"


def parse_worker_event(value: str | bytes | dict[str, Any]) -> WorkerRegistered | None:
    """Parse one JSON record, rejecting unrelated or malformed records."""
    try:
        data = json.loads(value) if isinstance(value, (str, bytes)) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("protocol_version") != WORKER_EVENT_PROTOCOL_VERSION:
        return None
    if data.get("event") != WORKER_REGISTERED:
        return None
    session_id, seq = data.get("session_id"), data.get("client_seq")
    occurred = data.get("occurred_at_ms")
    if not isinstance(session_id, str) or not (8 <= len(session_id) <= 64):
        return None
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return None
    if not isinstance(occurred, int) or isinstance(occurred, bool) or occurred < 0:
        return None
    categories: list[str] = []
    for key in ("runtime", "context", "profile"):
        item = data.get(key, "unknown")
        if not isinstance(item, str) or len(item) > 48 or not item.replace("_", "").isalnum():
            item = "unknown"
        categories.append(item)
    return WorkerRegistered(session_id, seq, *categories, occurred)


class WorkerRegistrationTracker:
    """Deduplicate events and classify a missing registration signal."""

    def __init__(self, *, session_id: str, started_at: float | None = None):
        self.session_id = session_id
        self.started_at = time.monotonic() if started_at is None else started_at
        self.registered: WorkerRegistered | None = None
        self._seen: set[str] = set()

    def observe(self, event: WorkerRegistered | dict[str, Any]) -> bool:
        parsed = event if isinstance(event, WorkerRegistered) else parse_worker_event(event)
        if parsed is None or parsed.session_id != self.session_id or parsed.event_id in self._seen:
            return False
        self._seen.add(parsed.event_id)
        self.registered = parsed
        return True

    def finish(self, *, now: float | None = None, timed_out: bool = False) -> dict[str, Any]:
        if self.registered is not None:
            duration_ms = max(0, round(((time.monotonic() if now is None else now) - self.started_at) * 1000))
            return {"result": "registered", "reason_code": None, "duration_ms": duration_ms}
        return {"result": "unknown", "reason_code": "unobserved_timeout" if timed_out else "build_timeout", "duration_ms": None}


def emit_worker_registered(*, runtime: str = "pier", context: str = "agent", profile: str = "provider") -> bool:
    """Atomically append a minimal registration event to the sidecar.

    The adapter runs in Pier's host process, so a private host file is shared
    across platforms without requiring Docker mounts or platform-specific
    inherited descriptor handling.  Missing/invalid paths fail closed.
    """
    raw_path = os.environ.get(WORKER_EVENT_FILE_ENV, "").strip()
    if not raw_path:
        return False
    path = Path(raw_path)
    values = {"runtime": runtime, "context": context, "profile": profile}
    for key, value in values.items():
        if not isinstance(value, str) or not value or len(value) > 48 or not value.replace("_", "").isalnum():
            values[key] = "unknown"
    payload = {
        "protocol_version": WORKER_EVENT_PROTOCOL_VERSION,
        "event": WORKER_REGISTERED,
        "session_id": os.environ.get("DRADAR_RUNNER_SESSION_ID", ""),
        "client_seq": 1,
        "occurred_at_ms": int(time.time() * 1000),
        **values,
    }
    session = payload["session_id"]
    if not isinstance(session, str) or not (8 <= len(session) <= 64):
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        return False
    return True


def read_worker_event(path: Path, *, offset: int = 0) -> tuple[dict[str, Any] | None, int]:
    """Read one complete sidecar record without parsing logs."""
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, offset))
            line = stream.readline()
            new_offset = stream.tell()
    except OSError:
        return None, offset
    if not line or not line.endswith(b"\n"):
        return None, offset
    try:
        value = json.loads(line)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, new_offset
    return (value if isinstance(value, dict) else None), new_offset


WORKER_START_ENV = "DRADAR_WORKER_START_GATE"
WORKER_START_SCHEMA = "dradar.worker_start.v1"
WORKER_START_WAIT_SEC = 120.0


def _windows_parent_alive(pid: int) -> bool:
    """Read process state without requesting termination rights."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        return False
    try:
        # WAIT_TIMEOUT means a live process; signalled/failed is not proof.
        return kernel.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel.CloseHandle(handle)


def _parent_alive(pid: int) -> bool:
    if os.name == "nt":
        return _windows_parent_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def register_worker(*, runtime="pier", context="agent", profile="provider"):
    """Publish readiness, then wait cancellably for this launch's owner ACK.

    This host-only permission is not a credential. A fresh nonce scopes it to
    one job/session; a dead parent, expired wait, or malformed permission fails
    closed. Ordinary adapters must await this before invoking provider work.
    """
    raw = os.environ.get(WORKER_START_ENV)
    if not raw:
        # Standalone adapter embedding without a DRadar runner stays supported.
        # A runner session without its gate must never silently downgrade.
        if os.environ.get("DRADAR_RUNNER_SESSION_ID") or os.environ.get(WORKER_EVENT_FILE_ENV):
            raise RuntimeError("worker start gate missing for runner session")
        return
    try:
        request = json.loads(raw)
        path = Path(request["path"])
        identity = {key: request[key] for key in ("schema", "nonce", "session_id", "job", "parent_pid")}
        if (identity["schema"] != WORKER_START_SCHEMA
                or identity["session_id"] != os.environ.get("DRADAR_RUNNER_SESSION_ID")
                or not isinstance(identity["nonce"], str) or len(identity["nonce"]) != 32
                or not isinstance(identity["job"], str) or not identity["job"]
                or type(identity["parent_pid"]) is not int or identity["parent_pid"] <= 0
                or not path.is_absolute()):
            raise ValueError("invalid identity")
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("invalid worker start gate") from None
    if not emit_worker_registered(runtime=runtime, context=context, profile=profile):
        raise RuntimeError("worker registration was not persisted")
    deadline = time.monotonic() + WORKER_START_WAIT_SEC
    while True:
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError("worker start permission expired")
        if not _parent_alive(identity["parent_pid"]):
            raise RuntimeError("worker start parent unavailable")
        try:
            permit = json.loads(path.read_text())
        except FileNotFoundError:
            await asyncio.sleep(0.05)
            continue
        except (OSError, ValueError):
            raise RuntimeError("worker start permission unreadable") from None
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError("worker start permission expired")
        if (not isinstance(permit, dict)
                or any(permit.get(key) != value for key, value in identity.items())
                or type(permit.get("expires_at")) not in (float, int)
                or not now < permit["expires_at"] <= now + WORKER_START_WAIT_SEC):
            raise RuntimeError("worker start permission identity or expiry mismatch")
        return
