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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = "dradar.flight_event.v1"
MAX_EVENT_BYTES = 4096
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_LOG_EVENTS = 5000
UPLOAD_BATCH_SIZE = 100
_LOCK_FILENAME = "recorder.lock"
_ACKNOWLEDGED_FILENAME = "acknowledged.jsonl"

# ``FlightRecorder`` is instantiated once by the fleet supervisor and once by
# every worker child.  A Python lock only coordinates threads in one process;
# keep a small in-process guard as well so Windows' byte-range locking does not
# trip over two handles opened by sibling objects in the same process.
_PROCESS_LOCK = threading.Lock()


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize recorder read/modify/write cycles across processes.

    The lock is deliberately a separate, never-replaced file.  The JSONL files
    themselves are atomically replaced by :meth:`FlightRecorder._write`, so a
    reader from an older CLI still sees a complete old or new snapshot.  Both
    POSIX ``flock`` and Windows ``msvcrt`` are used without adding a runtime
    dependency; a crashed process releases either kernel lock automatically.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    windows_lock = False
    with _PROCESS_LOCK:
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - Windows CI
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                windows_lock = True
            locked = True
            yield
        finally:
            if locked:
                try:
                    if windows_lock:  # pragma: no cover - Windows CI
                        import msvcrt

                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
            os.close(fd)


EVENT_TYPES = frozenset({
    "session_started", "session_closed", "phase_changed",
    "claim_requested", "claim_accepted", "claim_failed",
    "assignment_checked_out", "assignment_stopped",
    "build_started", "build_completed", "build_failed",
    "worker_registered",
    "provider_started", "provider_completed", "provider_failed",
    "heartbeat_sent", "heartbeat_acknowledged", "heartbeat_failed",
    "checkpoint_saved", "checkpoint_replayed", "checkpoint_failed",
    "release_requested", "release_completed", "release_failed",
    "upload_started", "upload_completed", "upload_failed",
    "request_started", "request_completed", "request_failed",
    "update_policy_rejected", "update_detected", "update_downloaded",
    "update_verified", "update_staged", "update_waiting_safe_point",
    "update_paused", "update_activated", "update_self_testing",
    "update_committed", "update_rollback_pending", "update_rolled_back",
    "update_failed",
    "supervisor_spawned", "child_ready", "precheckout_exit",
    "startup_failed", "probe_start", "probe_progress", "probe_timeout",
    "probe_ready",
})
COMPONENTS = frozenset({
    "cli", "claim", "build", "provider", "heartbeat", "checkpoint",
    "release", "upload", "api", "ota",
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
    "update_eligible", "update_sequence", "update_state",
})
REASON_CODES = frozenset({
    "api_error", "transport_error", "completed", "paused", "interrupted",
    "error", "explicit_force", "explicit_safe", "user_force", "user",
    "build_flake", "provider_failed", "submitted", "artifact-staging-failed",
    "upload-blocked", "upload-failed", "pending_upload", "not-uploaded",
    "assignment-reopened", "expired", "rejected",
    "update_manifest_invalid", "update_policy_rejected",
    "update_download_failed", "update_verification_failed",
    "update_stage_failed", "update_safe_point_blocked",
    "update_self_test_failed", "update_crash_recovery", "candidate_failed",
    "update_state_corrupt", "update_state_incompatible",
    "precheckout-exit", "startup-timeout", "startup-failed",
})
OUTCOMES = frozenset({
    "completed", "interrupted", "submitted", "artifact-staging-failed",
    "upload-blocked", "upload-failed", "not-uploaded",
    "assignment-reopened", "expired", "rejected",
})
PHASES = frozenset({
    "preparing", "building", "queued", "running", "uploading", "paused",
})
PROVIDERS = frozenset({
    "codex", "claude-code", "dsh-minimal", "grok-build", "kimi-code",
    "zcode", "antigravity", "codebuddy",
})
UPDATE_STATES = frozenset({
    "detected", "downloaded", "verified", "staged", "waiting_safe_point",
    "paused", "activated", "self_testing", "committed",
    "rollback_pending", "rolled_back", "failed",
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
    "update_eligible": (bool, None, None),
    "update_sequence": (int, 1, 2_147_483_647),
    "update_state": (str, UPDATE_STATES, None),
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
        self.lock_path = self.root / _LOCK_FILENAME
        self.acknowledged_path = self.root / _ACKNOWLEDGED_FILENAME
        self._lock = threading.Lock()
        self._seq = 0
        self._remote_disabled = False
        self._last_acknowledged_event_ids: set[str] = set()
        self.client_id = self._load_client_id()

    def _load_client_id(self) -> str:
        # Re-read after taking the inter-process lock.  Without this, two
        # freshly-started fleet children can each mint a client ID and the last
        # atomic replace wins on disk while the other process keeps a different
        # in-memory identity.
        try:
            with _exclusive_file_lock(self.lock_path):
                return self._load_or_create_client_id()
        except OSError:
            # Diagnostics and best-effort telemetry must still work on a
            # read-only/misconfigured home.  There is no durable cross-process
            # guarantee in this fallback, but callers already treat the local
            # recorder as optional in that environment.
            return self._load_or_create_client_id()

    def _load_or_create_client_id(self) -> str:
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

    @staticmethod
    def _valid_event_id(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 32
            and all(char in "0123456789abcdef" for char in value)
        )

    def _load_acknowledged_ids_unlocked(self) -> list[str]:
        try:
            lines = self.acknowledged_path.read_text(encoding="ascii").splitlines()
        except OSError:
            return []
        values: list[str] = []
        seen: set[str] = set()
        for value in lines[-MAX_LOG_EVENTS:]:
            if self._valid_event_id(value) and value not in seen:
                values.append(value)
                seen.add(value)
        return values

    def _write_acknowledged_ids_unlocked(self, event_ids: set[str]) -> None:
        """Persist a bounded shared receipt set under the recorder lock."""
        if not event_ids:
            return
        values = self._load_acknowledged_ids_unlocked()
        seen = set(values)
        for event_id in event_ids:
            if self._valid_event_id(event_id) and event_id not in seen:
                values.append(event_id)
                seen.add(event_id)
        values = values[-MAX_LOG_EVENTS:]
        self.acknowledged_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.acknowledged_path.with_name(
            f".{self.acknowledged_path.name}.{os.getpid()}.{time.time_ns()}"
        )
        temporary.write_text("".join(value + "\n" for value in values), encoding="ascii")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.acknowledged_path)

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
            with _exclusive_file_lock(self.lock_path):
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

    def flush(
        self,
        *,
        batch_id: str | None = None,
        session_id: str | None = None,
        required_event_id: str | None = None,
    ) -> int:
        """Upload pending events, optionally restricted to one lifecycle scope.

        ``pending.jsonl`` is intentionally shared by the CLI's lightweight
        recorder instances.  A run-plan token can therefore leave events from
        another plan, account, or device session in the same file.  The
        server's flight-event endpoint validates the whole request atomically;
        sending that mixed prefix would make an otherwise valid
        ``worker_registered`` event unacknowledgeable.  Callers that know the
        active scope should provide its batch and session IDs.  ``None`` keeps
        the historical unscoped replay behavior for diagnostics and legacy
        integrations.

        ``required_event_id`` is used by the synchronous worker-registration
        handshake.  When present, the matching event is moved to the front of
        the selected scope so an unrelated backlog cannot hide the receipt
        behind the 100-event upload limit.  It is never selected when it falls
        outside the requested scope.
        """
        if self.client is None or self._remote_disabled:
            return 0
        # A session-only (or required-event-only) scope cannot prove the plan
        # or account boundary.  In particular, never replay a legacy
        # ``batch_id=null`` event under a newly authenticated session; retain
        # it for explicit diagnostics or a future, manually selected replay.
        if batch_id is None and (
            session_id is not None or required_event_id is not None
        ):
            return 0
        with self._lock:
            try:
                with _exclusive_file_lock(self.lock_path):
                    pending = self._load(self.pending_path)
                    if not pending:
                        return 0
                    scoped = [
                        event for event in pending
                        if (batch_id is None or event.get("batch_id") == batch_id)
                        and (session_id is None or event.get("session_id") == session_id)
                    ]
                    if not scoped:
                        return 0
                    if required_event_id is not None:
                        required = next(
                            (
                                event for event in scoped
                                if event.get("event_id") == required_event_id
                            ),
                            None,
                        )
                        if required is not None:
                            scoped = [
                                required,
                                *(
                                    event for event in scoped
                                    if event.get("event_id") != required_event_id
                                ),
                            ]
                    batch = [
                        validate_event(event) for event in scoped[:UPLOAD_BATCH_SIZE]
                    ]
            except OSError:
                # Flight evidence is best effort.  A locked/read-only home
                # must never turn a heartbeat into a worker crash; strict
                # worker registration will fail closed when no receipt exists.
                return 0
            try:
                response = self.client.flight_events(batch)
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404:
                    self._remote_disabled = True
                return 0
            sent_ids = {event["event_id"] for event in batch}
            try:
                raw_acknowledged = response.get("acknowledged_event_ids") or ()
                acknowledged = {
                    event_id for event_id in raw_acknowledged
                    if self._valid_event_id(event_id) and event_id in sent_ids
                }
            except (AttributeError, TypeError):
                # A malformed response is not evidence of acceptance.  Keep
                # the durable pending event and let a later retry reconcile it.
                return 0
            if not acknowledged:
                return 0
            try:
                with _exclusive_file_lock(self.lock_path):
                    # Another process may have recorded or flushed events while
                    # the request was in flight.  Re-read before removing only
                    # the IDs this response actually acknowledged.
                    current_pending = self._load(self.pending_path)
                    self._write_acknowledged_ids_unlocked(acknowledged)
                    self._write(
                        self.pending_path,
                        [
                            event for event in current_pending
                            if event.get("event_id") not in acknowledged
                        ],
                    )
            except OSError:
                return 0
            # Keep the local cache for callers that inspect this recorder, and
            # also persist receipts so a sibling process can prove that its
            # exact worker_registered event was accepted.
            self._last_acknowledged_event_ids.update(acknowledged)
            return len(acknowledged)

    @property
    def last_acknowledged_event_ids(self) -> frozenset[str]:
        with self._lock:
            try:
                with _exclusive_file_lock(self.lock_path):
                    shared = self._load_acknowledged_ids_unlocked()
            except OSError:
                shared = []
            return frozenset((*self._last_acknowledged_event_ids, *shared))

    def export_diagnostic_bundle(self, destination: Path) -> Path:
        """Create a local-only ZIP containing allowlisted events and a manifest."""
        destination = Path(destination).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            try:
                with _exclusive_file_lock(self.lock_path):
                    events = self._load(self.events_path)
                    pending = self._load(self.pending_path)
                    events = [validate_event(event) for event in events]
                    pending = [validate_event(event) for event in pending]
            except OSError:
                events = []
                pending = []
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
