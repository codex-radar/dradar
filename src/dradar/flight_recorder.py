"""Privacy-minimized, offline-capable lifecycle flight recorder.

The recorder intentionally accepts a small allowlist rather than trying to
redact arbitrary dictionaries after the fact.  It never records request
bodies, prompts, source code, commands, host/user names, credentials, proxy
configuration, or model output.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "dradar.flight_event.v1"
MAX_EVENT_BYTES = 4096
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_LOG_EVENTS = 5000
UPLOAD_BATCH_SIZE = 100

EVENT_TYPES = frozenset({
    "session_started", "session_closed", "phase_changed",
    "claim_requested", "claim_accepted", "claim_failed",
    "assignment_checked_out", "assignment_stopped",
    "build_started", "build_completed", "build_failed",
    "provider_started", "provider_completed", "provider_failed",
    "heartbeat_sent", "heartbeat_acknowledged", "heartbeat_failed",
    "checkpoint_saved", "checkpoint_replayed", "checkpoint_failed",
    "release_requested", "release_completed", "release_failed",
    "upload_started", "upload_completed", "upload_failed",
    "request_started", "request_completed", "request_failed",
})
COMPONENTS = frozenset({
    "cli", "claim", "build", "provider", "heartbeat", "checkpoint",
    "release", "upload", "api",
})
EVENT_KEYS = frozenset({
    "schema_version", "event_id", "client_id", "occurred_at", "seq",
    "event_type", "component", "actor", "batch_id", "session_id",
    "assignment_id", "request_id", "reason_code", "attributes",
})
ATTRIBUTE_KEYS = frozenset({
    "attempt", "elapsed_ms", "force", "http_status", "offline_replay",
    "outcome", "phase", "previous_phase", "provider", "release_count",
    "target_workers", "was_running", "worker_slot",
})
REASON_CODES = frozenset({
    "api_error", "transport_error", "completed", "paused", "interrupted",
    "error", "explicit_force", "explicit_safe", "user_force", "user",
    "build_flake", "provider_failed", "submitted", "artifact-staging-failed",
    "upload-blocked", "upload-failed", "pending_upload", "not-uploaded",
    "assignment-reopened", "expired", "rejected",
})
OUTCOMES = frozenset({
    "completed", "interrupted", "submitted", "artifact-staging-failed",
    "upload-blocked", "upload-failed", "not-uploaded",
    "assignment-reopened", "expired", "rejected",
})
PHASES = frozenset({"preparing", "queued", "running", "uploading", "paused"})
PROVIDERS = frozenset({
    "codex", "claude-code", "dsh-minimal", "grok-build", "kimi-code",
    "zcode", "antigravity", "codebuddy",
})
ATTRIBUTE_RULES = {
    "attempt": (int, 1, 100),
    "elapsed_ms": (int, 0, 2_147_483_647),
    "force": (bool, None, None),
    "http_status": (int, 0, 599),
    "offline_replay": (bool, None, None),
    "outcome": (str, OUTCOMES, None),
    "phase": (str, PHASES, None),
    "previous_phase": (str, PHASES, None),
    "provider": (str, PROVIDERS, None),
    "release_count": (int, 0, 40),
    "target_workers": (int, 1, 40),
    "was_running": (bool, None, None),
    "worker_slot": (int, 1, 40),
}


def _optional_id(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str) or len(value) != 32
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field} must be 32 lowercase hex characters")
    return value


def _safe_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    if attributes is None:
        values = {}
    elif isinstance(attributes, dict):
        values = dict(attributes)
    else:
        raise ValueError("flight-event attributes must be an object")
    unknown = set(values) - ATTRIBUTE_KEYS
    if unknown:
        raise ValueError(f"unsupported flight-event attributes: {sorted(unknown)}")
    safe: dict[str, Any] = {}
    for key, value in values.items():
        expected, allowed_or_min, maximum = ATTRIBUTE_RULES[key]
        if expected is bool:
            if type(value) is not bool:
                raise ValueError(f"flight-event attribute {key} must be boolean")
        elif expected is int:
            if type(value) is not int or not allowed_or_min <= value <= maximum:
                raise ValueError(f"flight-event attribute {key} is out of range")
        elif type(value) is not str or value not in allowed_or_min:
            raise ValueError(f"flight-event attribute {key} is not an allowed value")
        safe[key] = value
    return safe


def validate_event(value: Any) -> dict[str, Any]:
    """Return one canonical event or reject it at every persistence boundary."""
    if not isinstance(value, dict) or set(value) != EVENT_KEYS:
        raise ValueError("flight event must contain exactly the versioned envelope")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported flight event schema")
    for field in ("event_id", "client_id"):
        candidate = value.get(field)
        if (
            not isinstance(candidate, str) or len(candidate) != 32
            or any(char not in "0123456789abcdef" for char in candidate)
        ):
            raise ValueError(f"{field} must be 32 lowercase hex characters")
    occurred_at = value.get("occurred_at")
    if not isinstance(occurred_at, str):
        raise ValueError("occurred_at must be a timezone-aware ISO timestamp")
    try:
        parsed_at = datetime.fromisoformat(occurred_at)
    except ValueError as exc:
        raise ValueError("occurred_at must be a timezone-aware ISO timestamp") from exc
    if parsed_at.tzinfo is None:
        raise ValueError("occurred_at must include a timezone")
    seq = value.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= 2_147_483_647:
        raise ValueError("seq is out of range")
    if value.get("event_type") not in EVENT_TYPES:
        raise ValueError("unsupported flight event type")
    if value.get("component") not in COMPONENTS:
        raise ValueError("unsupported flight event component")
    if value.get("actor") != "cli":
        raise ValueError("client flight event actor must be cli")
    for field in ("batch_id", "session_id", "assignment_id", "request_id"):
        _optional_id(value.get(field), field=field)
    reason_code = value.get("reason_code")
    if reason_code is not None and reason_code not in REASON_CODES:
        raise ValueError("flight event reason_code is not an allowed value")
    attributes = _safe_attributes(value.get("attributes"))
    canonical = dict(value)
    canonical["attributes"] = attributes
    if len(json.dumps(canonical, separators=(",", ":")).encode("utf-8")) > MAX_EVENT_BYTES:
        raise ValueError("flight event exceeds the local size limit")
    return canonical


class FlightRecorder:
    """Append a bounded local history and replay an independent pending queue."""

    def __init__(self, home: Path, client=None):
        self.home = Path(home)
        self.client = client
        self.root = self.home / "flight-recorder"
        self.events_path = self.root / "events.jsonl"
        self.pending_path = self.root / "pending.jsonl"
        self.client_id_path = self.root / "client_id"
        self._lock = threading.Lock()
        self._seq = 0
        self._remote_disabled = False
        self.client_id = self._load_client_id()

    def _load_client_id(self) -> str:
        try:
            value = self.client_id_path.read_text(encoding="ascii").strip()
            if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
                return value
        except OSError:
            pass
        value = uuid.uuid4().hex
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.client_id_path.with_name(
                f".{self.client_id_path.name}.{os.getpid()}.{time.time_ns()}"
            )
            temporary.write_text(value, encoding="ascii")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.client_id_path)
        except OSError:
            pass
        return value

    @staticmethod
    def _load(path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events = []
        for line in lines[-MAX_LOG_EVENTS:]:
            try:
                value = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            try:
                events.append(validate_event(value))
            except ValueError:
                continue
        return events

    @staticmethod
    def _write(path: Path, events: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded = [
            json.dumps(validate_event(event), sort_keys=True, separators=(",", ":"))
            for event in events
        ]
        while encoded and (
            len(encoded) > MAX_LOG_EVENTS
            or sum(len(line.encode("utf-8")) + 1 for line in encoded) > MAX_LOG_BYTES
        ):
            del encoded[: max(1, len(encoded) // 4)]
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}")
        temporary.write_text("".join(line + "\n" for line in encoded), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def record(
        self,
        event_type: str,
        *,
        component: str,
        batch_id: str | None = None,
        session_id: str | None = None,
        assignment_id: str | None = None,
        request_id: str | None = None,
        reason_code: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = {
                "schema_version": SCHEMA_VERSION,
                "event_id": uuid.uuid4().hex,
                "client_id": self.client_id,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "seq": self._seq,
                "event_type": event_type,
                "component": component,
                "actor": "cli",
                "batch_id": batch_id,
                "session_id": session_id,
                "assignment_id": assignment_id,
                "request_id": request_id,
                "reason_code": reason_code,
                "attributes": attributes or {},
            }
            event = validate_event(event)
            history = self._load(self.events_path)
            pending = self._load(self.pending_path)
            self._write(self.events_path, [*history, event])
            self._write(self.pending_path, [*pending, event])
            return event

    def try_record(self, event_type: str, **kwargs) -> dict[str, Any] | None:
        """Record best-effort evidence without affecting the primary workflow.

        ``record`` remains the strict persistence boundary used by tests and
        diagnostic tooling. Runtime integrations use this wrapper so a legacy
        or malformed identifier, or an unavailable local disk, drops only the
        diagnostic event instead of interrupting a claim, run, or release.
        """
        try:
            return self.record(event_type, **kwargs)
        except (OSError, ValueError):
            return None

    def flush(self) -> int:
        if self.client is None or self._remote_disabled:
            return 0
        with self._lock:
            pending = self._load(self.pending_path)
            if not pending:
                return 0
            batch = pending[:UPLOAD_BATCH_SIZE]
            batch = [validate_event(event) for event in batch]
            try:
                response = self.client.flight_events(batch)
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404:
                    self._remote_disabled = True
                return 0
            acknowledged = set(response.get("acknowledged_event_ids") or ())
            if not acknowledged:
                return 0
            self._write(
                self.pending_path,
                [event for event in pending if event.get("event_id") not in acknowledged],
            )
            return len(acknowledged)

    def export_diagnostic_bundle(self, destination: Path) -> Path:
        """Create a local-only ZIP containing allowlisted events and a manifest."""
        destination = Path(destination).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            events = self._load(self.events_path)
            pending = self._load(self.pending_path)
            events = [validate_event(event) for event in events]
            pending = [validate_event(event) for event in pending]
        manifest = {
            "schema_version": "dradar.flight_diagnostics.v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "event_count": len(events),
            "pending_count": len(pending),
            "privacy_policy": "strict_allowlist_no_content_or_credentials",
        }
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(
                "manifest.json", json.dumps(manifest, sort_keys=True, indent=2) + "\n"
            )
            bundle.writestr(
                "events.jsonl",
                "".join(
                    json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
                    for event in events
                ),
            )
        return destination


def cmd_diagnostics(args) -> int:
    from .local_config import HOME

    output = FlightRecorder(HOME).export_diagnostic_bundle(Path(args.output))
    print(f"diagnostic bundle written: {output}")
    return 0
