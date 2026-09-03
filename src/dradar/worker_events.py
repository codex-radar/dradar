"""Small structured Pier->CLI lifecycle sidecar protocol.

The sidecar is deliberately separate from Pier's stdout/stderr (which contain
untrusted provider output).  Adapter code emits only a redacted enum and the
opaque runner session id; the CLI validates the record before charging the
runtime lease.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORKER_EVENT_PROTOCOL_VERSION = 1
WORKER_REGISTERED = "worker_registered"
WORKER_EVENT_FILE_ENV = "DRADAR_PIER_WORKER_EVENT_FILE"


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
