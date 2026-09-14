"""Bounded signed discovery, separate from activation and model execution."""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Callable

import httpx

from .. import __version__
from ..flight_recorder import FlightRecorder
from .integration import COMPATIBILITY, ota_root, update_status
from .manifest import PlatformTarget, RolloutContext, verify_signed_manifest
from .runtime import UpdateRuntime
from .state import UpdateLock, UpdateState, _atomic_json

LAUNCH_METHOD = "unknown"

STABLE_URL = "https://updates.codexradar.com/channels/stable/current.json"
# Existing production public trust root. Never use a key supplied by a manifest.
TRUSTED_KEYS = {
    "dradar-ota-prod-2026-09-03-01": base64.b64decode(
        "cNKyezPQwWVFv7rQua/e4mmQKho0OmgQvrLyR/R2otI="
    ),
}
CHECK_INTERVAL = 900.0
FAILURE_INTERVAL = 300.0
TOTAL_BUDGET = 8.0
MANIFEST_LIMIT = 128 * 1024


class DeadlineClient:
    """Bound each stream and its whole lifetime, including trickle responses."""
    def __init__(self, client, deadline: float, clock: Callable[[], float]):
        self.client, self.deadline, self.clock = client, deadline, clock

    def stream(self, method, url, **kwargs):
        from contextlib import contextmanager

        @contextmanager
        def bounded():
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError("OTA discovery budget exhausted")
            import threading
            # Also close the dedicated discovery transport if headers or a
            # compressed decoder withhold body chunks. Socket inactivity remains
            # bounded, so cancellation cannot leave an unbounded blocking read.
            cancel = getattr(self.client, "close", lambda: None)
            watchdog = threading.Timer(remaining, cancel)
            watchdog.daemon = True
            watchdog.start()
            try:
                with self.client.stream(method, url, timeout=min(3.0, remaining),
                                        headers={"Accept-Encoding": "identity"}, **kwargs) as response:
                    outer = self
                    class Response:
                        def raise_for_status(self):
                            response.raise_for_status()
                            if getattr(response, "headers", {}).get("content-encoding", "identity") != "identity":
                                raise ValueError("unexpected OTA content encoding")
                        def iter_bytes(self, chunk_size=65536):
                            for chunk in response.iter_bytes(None):
                                if outer.clock() >= outer.deadline:
                                    raise TimeoutError("OTA discovery budget exhausted")
                                yield chunk
                    yield Response()
            finally:
                watchdog.cancel()
        return bounded()


def discover_update(home: Path, *, client=None, clock=time.monotonic,
                    wall_time=time.time, trusted_keys=None) -> str:
    """Prepare at most one verified update. Never activate or run a candidate.

    Injection arguments are local test seams; production URL is fixed.
    All failures leave the running bundle available and return a bounded code.
    """
    keys = TRUSTED_KEYS if trusted_keys is None else trusted_keys
    root = ota_root(home)
    if root.is_symlink():
        return "unsafe_state"
    try:
        with UpdateLock(root / "discovery.lock", timeout_seconds=0):
            stamp = root / "discovery.json"
            if stamp.is_symlink():
                return "unsafe_state"
            try:
                previous = json.loads(stamp.read_text()) if stamp.exists() else {}
                next_at = float(previous.get("next_check_at", 0))
                if wall_time() <= next_at <= wall_time() + CHECK_INTERVAL:
                    return "cached"
            except (OSError, ValueError, TypeError):
                return "unsafe_state"
            # Persist retry throttle before networking, including crash cases.
            _atomic_json(stamp, {"next_check_at": wall_time() + FAILURE_INTERVAL})
            runtime = UpdateRuntime(root, recorder=FlightRecorder(home),
                                    download_client=None)
            state = runtime.controller.state()
            if state and state.get("state") in {
                UpdateState.WAITING_SAFE_POINT.value, UpdateState.STAGED.value,
                UpdateState.ACTIVATED.value, UpdateState.SELF_TESTING.value,
            }:
                return "pending"
            owns_client = client is None
            if owns_client:
                client = httpx.Client(follow_redirects=False)
            try:
                transport = DeadlineClient(client, clock() + TOTAL_BUDGET, clock)
                with transport.stream("GET", STABLE_URL, follow_redirects=False) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > MANIFEST_LIMIT:
                            raise ValueError("manifest too large")
                verify_signed_manifest(bytes(data), keys)
                status = update_status(home)
                runtime.download_client = transport
                decision = runtime.prepare(
                    bytes(data), trusted_keys=keys,
                    current_version=status["current_version"] or __version__,
                    committed_sequence=status["current_sequence"] or 0,
                    compatibility=COMPATIBILITY,
                    rollout=RolloutContext(subject=runtime.audit.recorder.client_id),
                    target=PlatformTarget.current(),
                )
                result = "prepared" if decision.eligible else decision.reason
                _atomic_json(stamp, {"next_check_at": wall_time() + CHECK_INTERVAL,
                                     "result": result})
                return result
            finally:
                runtime.audit.recorder.flush()
                if owns_client:
                    client.close()
    except Exception:
        # Never leak URLs, proxy settings or remote errors into user output.
        return "unavailable"


def start_periodic_discovery(home: Path):
    """Stage only while work runs; daemon never controls a runner or activation."""
    import threading
    stop = threading.Event()
    def loop():
        while not stop.wait(60):
            discover_update(home)
    thread = threading.Thread(target=loop, name="dradar-ota-discovery", daemon=True)
    thread.start()
    return stop


def runtime_observation(home: Path) -> dict:
    """Report a current local snapshot, never replay pre-login event history."""
    status = update_status(home)
    result = {"update_enabled": LAUNCH_METHOD != "unknown", "launch_method": LAUNCH_METHOD}
    state = status.get("state")
    if state in {item.value for item in UpdateState}:
        result["update_state"] = state
    sequence = status.get("current_sequence")
    # A staged candidate is distinct from the running version in heartbeat.
    try:
        record = UpdateRuntime(ota_root(home), recorder=FlightRecorder(home), download_client=None).controller.state()
        if record and isinstance(record.get("release"), dict):
            sequence = record["release"].get("sequence", sequence)
    except Exception:
        pass
    if type(sequence) is int and sequence > 0:
        result["update_sequence"] = sequence
    return result
