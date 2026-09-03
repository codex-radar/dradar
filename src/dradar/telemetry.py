"""Low-bandwidth runner heartbeat lifecycle.

One session reports regardless of how many cells it holds.  Payloads contain
only lifecycle metadata; never task text, prompts, trajectories, patches,
commands, hostname, username, IP or hardware details.
"""

from __future__ import annotations

import random
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from . import __version__
from .api_client import ApiClient, ApiError
from .flight_recorder import FlightRecorder


def platform_family() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
            return "wsl"
        return "linux"
    return "other"


PREPARATION_HEARTBEAT_SEC = 30


class RunnerTelemetry:
    """A daemon heartbeat with a fast preparation cadence.

    Background telemetry is best effort and can never abort a running trial.
    The synchronous pre-checkout registration path is strict because checkout
    cannot safely bind a session the server rejected. Three consecutive
    background failures produce one warning; recovery produces one notice.
    """

    def __init__(
        self,
        client: ApiClient,
        *,
        jitter: bool = True,
        target_workers: int = 1,
        home: Path | None = None,
    ):
        if not 1 <= target_workers <= 40:
            raise ValueError("target_workers must be between 1 and 40")
        self.client = client
        self.target_workers = target_workers
        self.session_id = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._phase = "preparing"
        self._active_assignment_id: str | None = None
        self._owner_epoch: int | None = None
        self._batch_id: str | None = None
        self._seq = 0
        # A successful heartbeat is the only server-verifiable registration
        # acknowledgement available to the local coordinator.  Keep this
        # separate from the generic sequence counter: an offline/legacy
        # server must never be mistaken for a registered worker.
        self._last_heartbeat_accepted = False
        self._last_flight_events_acked = 0
        self._progress_counter = 0
        # The first heartbeat is sent immediately by ``start``.  Keep the
        # fallback short while cloning/building/queuing so a stalled setup is
        # noticed quickly even when the server response is unavailable.
        self._interval = PREPARATION_HEARTBEAT_SEC
        self._failures = 0
        self._warned = False
        self._disabled = False
        self._jitter = jitter
        self._shown_notice_ids: set[str] = set()
        self._stop_requested = False
        self.flight_recorder = FlightRecorder(home, client) if home is not None else None
        if self.flight_recorder is not None:
            self.flight_recorder.try_record(
                "session_started", component="cli", session_id=self.session_id,
                attributes={"target_workers": target_workers},
            )

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    @staticmethod
    def _publish_stop_marker() -> None:
        raw = os.environ.get("DRADAR_POOL_ABORT_FILE")
        if not raw:
            return
        path = Path(raw)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary.write_text(
                "drain:the server asked this device to stop", encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError:
            pass
        finally:
            temporary.unlink(missing_ok=True)

    def _show_notices(self, response: dict) -> None:
        """Print each bounded server notice at most once per runner process."""
        notices = response.get("notices")
        if not isinstance(notices, list):
            return
        for value in notices[:10]:
            if not isinstance(value, dict):
                continue
            notice_id = value.get("id")
            message = value.get("message")
            severity = value.get("severity", "info")
            if (
                not isinstance(notice_id, str)
                or not 1 <= len(notice_id) <= 100
                or not isinstance(message, str)
                or not 1 <= len(message) <= 1000
                or severity not in {"info", "warning", "critical"}
                or notice_id in self._shown_notice_ids
            ):
                continue
            self._shown_notice_ids.add(notice_id)
            clean = " ".join(message.splitlines())
            print(f"server notice [{severity}]: {clean}", file=sys.stderr)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="dradar-heartbeat", daemon=True)
        self._thread.start()

    def bind_batch(self, batch_id: str | None) -> None:
        if not batch_id:
            return
        with self._lock:
            changed = self._batch_id != batch_id
            self._batch_id = batch_id
            if changed:
                self._progress_counter += 1
        if changed:
            self._wake.set()

    def record_event(self, event_type: str, *, component: str, **kwargs) -> dict | None:
        if self.flight_recorder is None:
            return None
        kwargs.setdefault("batch_id", self._batch_id)
        kwargs.setdefault("session_id", self.session_id)
        return self.flight_recorder.try_record(event_type, component=component, **kwargs)

    def set_phase(
        self,
        phase: str,
        assignment_id: str | None = None,
        owner_epoch: int | None = None,
    ) -> None:
        if phase not in {"preparing", "building", "queued", "running", "uploading", "paused"}:
            raise ValueError(f"unknown runner phase {phase!r}")
        if owner_epoch is not None and owner_epoch < 0:
            raise ValueError("owner_epoch must be non-negative")
        if assignment_id is None:
            owner_epoch = None
        with self._lock:
            previous_phase = self._phase
            changed = (
                self._phase, self._active_assignment_id, self._owner_epoch,
            ) != (phase, assignment_id, owner_epoch)
            self._phase = phase
            self._active_assignment_id = assignment_id
            self._owner_epoch = owner_epoch
            if changed:
                self._progress_counter += 1
        if changed:
            self.record_event(
                "phase_changed", component="heartbeat",
                assignment_id=assignment_id,
                attributes={"previous_phase": previous_phase, "phase": phase},
            )
            self._wake.set()

    def _payload(self) -> dict:
        with self._lock:
            self._seq += 1
            return {
                "protocol_version": 3,
                "client_version": __version__,
                "session_id": self.session_id,
                "batch_id": self._batch_id,
                "seq": self._seq,
                "phase": self._phase,
                "active_assignment_id": self._active_assignment_id,
                "owner_epoch": self._owner_epoch,
                "client_monotonic_ms": int(time.monotonic() * 1000),
                "progress_counter": self._progress_counter,
                "platform": platform_family(),
                "target_workers": self.target_workers,
            }

    def _send_once(self, *, propagate_errors: bool = False) -> int:
        """Send once and return the server-selected next interval."""
        with self._send_lock:
            if self._disabled:
                return self._interval
            try:
                self.record_event("heartbeat_sent", component="heartbeat")
                response = self.client.runner_heartbeat(self._payload())
            except ApiError as exc:
                # Older servers have no endpoint. Silence and disable rather than
                # alarming users or producing a 404 every two minutes forever.
                if exc.status_code == 404:
                    self._disabled = True
                    with self._lock:
                        self._last_heartbeat_accepted = False
                    return self._interval
                self._failures += 1
                self.record_event(
                    "heartbeat_failed", component="heartbeat",
                    reason_code="api_error",
                    attributes={"http_status": exc.status_code or 0},
                )
                if self._failures >= 3 and not self._warned:
                    print("warning: the server cannot see this runner's heartbeat; "
                          "work continues and your leases are not auto-released",
                          file=sys.stderr)
                    self._warned = True
                if propagate_errors:
                    raise
                return self._interval
            except Exception:
                self._failures += 1
                self.record_event(
                    "heartbeat_failed", component="heartbeat",
                    reason_code="transport_error",
                )
                if self._failures >= 3 and not self._warned:
                    print("warning: runner heartbeat is unavailable; work continues and "
                          "your leases are not auto-released", file=sys.stderr)
                    self._warned = True
                if propagate_errors:
                    raise
                return self._interval

            if self._warned:
                print("runner heartbeat recovered", file=sys.stderr)
            self._failures = 0
            self._warned = False
            with self._lock:
                self._last_heartbeat_accepted = response.get("accepted") is True
            self.record_event("heartbeat_acknowledged", component="heartbeat")
            self._last_flight_events_acked = (
                self.flight_recorder.flush() if self.flight_recorder is not None else 0
            )
            self._show_notices(response)
            if response.get("stop_requested") is True:
                self._stop_requested = True
                self._publish_stop_marker()
            if response.get("batch_id"):
                with self._lock:
                    self._batch_id = response["batch_id"]
            requested = response.get("next_heartbeat_sec", self._interval)
            try:
                requested_interval = min(600, max(30, int(requested)))
                with self._lock:
                    phase = self._phase
                if phase in {"preparing", "building", "queued"}:
                    # Preparation has no model output to act as a liveness
                    # signal. Do not let an adaptive server interval stretch
                    # this phase beyond the operator-visible 30s cadence.
                    self._interval = PREPARATION_HEARTBEAT_SEC
                else:
                    self._interval = requested_interval
            except (TypeError, ValueError):
                pass
            return self._interval

    def flush(self) -> None:
        """Best-effort synchronous update while work is already recoverable."""
        self._send_once()

    def flush_for_checkout(self) -> bool:
        """Register synchronously or expose the error before session checkout.

        A 404 keeps the legacy compatibility path: old servers did not require
        session registration. Every other failure must stop checkout instead
        of converting the real heartbeat error into a misleading downstream
        ``runner session is not registered`` response.
        """
        self._send_once(propagate_errors=True)
        return self._stop_requested

    def flush_for_worker_registration(self, required_event_id: str | None = None) -> bool:
        """Perform a strict worker-registration handshake.

        ``subprocess.Popen`` only proves that the local Pier parent was
        created; it says nothing about the server seeing this exact worker.
        The caller sets ``active_assignment_id`` and ``phase=building`` first,
        then uses this synchronous heartbeat.  A 200 response with
        ``accepted=true`` plus an acknowledged ``worker_registered`` flight
        event is the protocol's registration acknowledgement.
        Legacy/disabled heartbeat endpoints fail closed and return ``False``;
        the caller must not charge the execution lease in that case.
        """
        if self._disabled:
            return False
        self._send_once(propagate_errors=True)
        with self._lock:
            if not self._last_heartbeat_accepted:
                return False
            if self.flight_recorder is None:
                return required_event_id is None
            if required_event_id is not None:
                return required_event_id in self.flight_recorder.last_acknowledged_event_ids
            # A production recorder must always bind the exact lifecycle event;
            # an unspecified ID would silently downgrade to heartbeat-only.
            return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = self._send_once()
            if self._jitter:
                interval *= random.uniform(0.9, 1.1)
            self._wake.wait(interval)
            self._wake.clear()

    def close(self, reason: str) -> None:
        if reason not in {"completed", "paused", "interrupted", "error"}:
            raise ValueError(f"unknown close reason {reason!r}")
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if not self._disabled:
            with self._lock:
                self._seq += 1
                payload = {
                    "session_id": self.session_id,
                    "batch_id": self._batch_id,
                    "seq": self._seq,
                    "reason": reason,
                }
            with self._send_lock:
                try:
                    self.client.runner_close(payload)
                except Exception:
                    pass
        self.record_event(
            "session_closed", component="cli", reason_code=reason,
            assignment_id=self._active_assignment_id,
        )
        if self.flight_recorder is not None:
            self.flight_recorder.flush()
