"""Crash-safe update state, host lock, safe-point gate and launcher pointers."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import AbstractContextManager, contextmanager
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol

from .manifest import Artifact, ReleaseManifest, verify_artifact


class UpdateState(StrEnum):
    DETECTED = "detected"
    DOWNLOADED = "downloaded"
    VERIFIED = "verified"
    STAGED = "staged"
    WAITING_SAFE_POINT = "waiting_safe_point"
    PAUSED = "paused"
    ACTIVATED = "activated"
    SELF_TESTING = "self_testing"
    COMMITTED = "committed"
    ROLLBACK_PENDING = "rollback_pending"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


_PRE_ACTIVATION = frozenset(
    {
        UpdateState.DETECTED,
        UpdateState.DOWNLOADED,
        UpdateState.VERIFIED,
        UpdateState.STAGED,
        UpdateState.WAITING_SAFE_POINT,
    }
)
_TRANSITIONS = {
    UpdateState.DETECTED: {
        UpdateState.DOWNLOADED,
        UpdateState.PAUSED,
        UpdateState.FAILED,
    },
    UpdateState.DOWNLOADED: {
        UpdateState.VERIFIED,
        UpdateState.PAUSED,
        UpdateState.FAILED,
    },
    UpdateState.VERIFIED: {UpdateState.STAGED, UpdateState.PAUSED, UpdateState.FAILED},
    UpdateState.STAGED: {
        UpdateState.WAITING_SAFE_POINT,
        UpdateState.PAUSED,
        UpdateState.FAILED,
    },
    UpdateState.WAITING_SAFE_POINT: {
        UpdateState.ACTIVATED,
        UpdateState.PAUSED,
        UpdateState.FAILED,
    },
    UpdateState.ACTIVATED: {UpdateState.SELF_TESTING, UpdateState.ROLLBACK_PENDING},
    UpdateState.SELF_TESTING: {UpdateState.COMMITTED, UpdateState.ROLLBACK_PENDING},
    UpdateState.ROLLBACK_PENDING: {UpdateState.ROLLED_BACK},
    UpdateState.PAUSED: set(),
    UpdateState.COMMITTED: set(),
    UpdateState.ROLLED_BACK: set(),
    UpdateState.FAILED: set(),
}


class InvalidTransition(RuntimeError):
    pass


class UpdateLockBusy(RuntimeError):
    pass


class EventSink(Protocol):
    """#0011 supplies the envelope/identity/persistence adapter."""

    def emit(self, event_type: str, attributes: Mapping[str, Any]) -> None: ...


class NullEventSink:
    def emit(self, event_type: str, attributes: Mapping[str, Any]) -> None:
        del event_type, attributes


@dataclass(frozen=True)
class SafePointSnapshot:
    active_assignments: int = 0
    checkouts_inflight: int = 0
    uploads_inflight: int = 0
    durable_uploads_pending: int = 0
    ledger_writes_inflight: int = 0
    checkpoint_writes_inflight: int = 0
    refill_accepting_new: bool = False
    worker_supervisor_idle: bool = True

    def __post_init__(self) -> None:
        for name in (
            "active_assignments",
            "checkouts_inflight",
            "uploads_inflight",
            "durable_uploads_pending",
            "ledger_writes_inflight",
            "checkpoint_writes_inflight",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.refill_accepting_new, bool):
            raise ValueError("refill_accepting_new must be boolean")
        if not isinstance(self.worker_supervisor_idle, bool):
            raise ValueError("worker_supervisor_idle must be boolean")

    def blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        for name in (
            "active_assignments",
            "checkouts_inflight",
            "uploads_inflight",
            "durable_uploads_pending",
            "ledger_writes_inflight",
            "checkpoint_writes_inflight",
        ):
            if getattr(self, name) != 0:
                blockers.append(name)
        if self.refill_accepting_new:
            blockers.append("refill_accepting_new")
        if not self.worker_supervisor_idle:
            blockers.append("worker_supervisor_not_idle")
        return tuple(blockers)

    @property
    def ready(self) -> bool:
        return not self.blockers()


@dataclass(frozen=True)
class ReleasePointer:
    release_id: str
    version: str
    sequence: int
    artifact: str


class UpdateLock(AbstractContextManager["UpdateLock"]):
    """One machine-wide writer lock; works with flock and Windows msvcrt."""

    def __init__(self, path: Path, *, timeout_seconds: float = 0.0):
        self.path = path
        self.timeout_seconds = max(0.0, timeout_seconds)
        self._fd: int | None = None
        self._windows = False

    def __enter__(self) -> "UpdateLock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except ImportError:  # pragma: no cover - Windows CI exercises this
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    self._windows = True
                break
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise UpdateLockBusy(
                        "another DRadar process owns the update lock"
                    ) from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        self._fd = fd
        try:
            metadata = json.dumps({"pid": os.getpid(), "acquired_at": _now()}).encode()
            os.ftruncate(fd, 0)
            remaining = memoryview(metadata)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("could not write update lock ownership")
                remaining = remaining[written:]
            os.fsync(fd)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._fd is None:
            return
        if self._windows:  # pragma: no cover
            try:
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            try:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        os.close(self._fd)
        self._fd = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd: int | None = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidTransition(f"cannot read trusted OTA state {path.name}") from exc
    if not isinstance(value, dict):
        raise InvalidTransition(f"trusted OTA state {path.name} is not an object")
    return value


class UpdateController:
    """Durable control plane used by a stable launcher, never by model code."""

    def __init__(self, root: Path, *, event_sink: EventSink | None = None):
        self.root = root
        self.releases = root / "releases"
        self.current_path = root / "current.json"
        self.last_known_good_path = root / "last-known-good.json"
        self.pending_path = root / "pending.json"
        self.state_path = root / "update-state.json"
        self.lock_path = root / "update.lock"
        self.event_sink = event_sink or NullEventSink()
        self._local = threading.local()

    def lock(self, *, timeout_seconds: float = 0.0) -> UpdateLock:
        return UpdateLock(self.lock_path, timeout_seconds=timeout_seconds)

    @contextmanager
    def transaction(
        self, *, timeout_seconds: float = 0.0
    ) -> Iterator["UpdateController"]:
        """Hold the host lock across one or more related state transitions."""

        depth = getattr(self._local, "transaction_depth", 0)
        if depth:
            self._local.transaction_depth = depth + 1
            try:
                yield self
            finally:
                self._local.transaction_depth -= 1
            return
        with self.lock(timeout_seconds=timeout_seconds):
            self._local.transaction_depth = 1
            try:
                yield self
            finally:
                self._local.transaction_depth = 0

    def _require_transaction(self) -> None:
        if not getattr(self._local, "transaction_depth", 0):
            raise UpdateLockBusy(
                "OTA mutation requires UpdateController.transaction()",
            )

    def state(self) -> dict[str, Any] | None:
        return _load_json(self.state_path)

    def _write_state(
        self,
        state: UpdateState,
        *,
        release: ReleasePointer,
        reason: str | None = None,
        resume_state: UpdateState | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema_version": 1,
            "state": state.value,
            "release": asdict(release),
            "updated_at": _now(),
        }
        if reason:
            record["reason"] = reason[:500]
        if resume_state:
            record["resume_state"] = resume_state.value
        _atomic_json(self.state_path, record)
        self.event_sink.emit(
            f"ota.{state.value}",
            {
                "release_id": release.release_id,
                "version": release.version,
                "sequence": release.sequence,
                **({"reason": reason[:500]} if reason else {}),
            },
        )
        return record

    def detect(self, manifest: ReleaseManifest, artifact: Artifact) -> ReleasePointer:
        self._require_transaction()
        pointer = ReleasePointer(
            release_id=manifest.release_id,
            version=manifest.version,
            sequence=manifest.sequence,
            artifact=artifact.filename,
        )
        current = self.state()
        if current and current.get("state") not in {
            UpdateState.COMMITTED.value,
            UpdateState.ROLLED_BACK.value,
            UpdateState.FAILED.value,
        }:
            raise InvalidTransition("another update is already in progress")
        self._write_state(UpdateState.DETECTED, release=pointer)
        return pointer

    def transition(
        self, target: UpdateState, *, reason: str | None = None
    ) -> dict[str, Any]:
        self._require_transaction()
        record = self.state()
        if not record:
            raise InvalidTransition("no update is in progress")
        current = UpdateState(record["state"])
        if target not in _TRANSITIONS[current]:
            raise InvalidTransition(
                f"cannot transition {current.value} -> {target.value}"
            )
        pointer = ReleasePointer(**record["release"])
        return self._write_state(target, release=pointer, reason=reason)

    def pause(self, reason: str) -> dict[str, Any]:
        self._require_transaction()
        record = self.state()
        if not record:
            raise InvalidTransition("no update is in progress")
        current = UpdateState(record["state"])
        if current not in _PRE_ACTIVATION:
            raise InvalidTransition("only a pre-activation update can be paused")
        pointer = ReleasePointer(**record["release"])
        return self._write_state(
            UpdateState.PAUSED,
            release=pointer,
            reason=reason,
            resume_state=current,
        )

    def resume(self) -> dict[str, Any]:
        self._require_transaction()
        record = self.state()
        if not record or record.get("state") != UpdateState.PAUSED.value:
            raise InvalidTransition("update is not paused")
        try:
            target = UpdateState(record["resume_state"])
        except (KeyError, ValueError) as exc:
            raise InvalidTransition("paused update has no safe resume state") from exc
        if target not in _PRE_ACTIVATION:
            raise InvalidTransition("paused update resume state is unsafe")
        return self._write_state(target, release=ReleasePointer(**record["release"]))

    def stage(
        self,
        manifest: ReleaseManifest,
        artifact: Artifact,
        downloaded_path: Path,
    ) -> ReleasePointer:
        self._require_transaction()
        record = self.state()
        if not record or record.get("state") != UpdateState.VERIFIED.value:
            raise InvalidTransition("artifact may only be staged after verification")
        pointer = ReleasePointer(**record["release"])
        if (pointer.release_id, pointer.sequence) != (
            manifest.release_id,
            manifest.sequence,
        ):
            raise InvalidTransition("staged manifest does not match the active update")
        if pointer.artifact != artifact.filename:
            raise InvalidTransition(
                "staged artifact does not match the detected target"
            )
        previous = _load_json(self.current_path) or _load_json(
            self.last_known_good_path,
        )
        if previous is None:
            raise InvalidTransition(
                "OTA requires an existing current or last-known-good release",
            )
        verify_artifact(downloaded_path, artifact)
        release_dir = self.releases / manifest.release_id
        release_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = release_dir / artifact.filename
        if downloaded_path.resolve() != destination.resolve():
            temporary = release_dir / f".{artifact.filename}.{time.time_ns()}.tmp"
            try:
                with (
                    downloaded_path.open("rb") as source,
                    temporary.open("xb") as target,
                ):
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        verify_artifact(destination, artifact)
        pointer = ReleasePointer(
            release_id=manifest.release_id,
            version=manifest.version,
            sequence=manifest.sequence,
            artifact=str(destination.relative_to(self.root)),
        )
        pending = {
            "schema_version": 1,
            "candidate": asdict(pointer),
            "previous": previous,
            "activation_attempted": False,
            "created_at": _now(),
        }
        _atomic_json(self.pending_path, pending)
        self._write_state(UpdateState.STAGED, release=pointer)
        return pointer

    def wait_for_safe_point(self) -> dict[str, Any]:
        return self.transition(UpdateState.WAITING_SAFE_POINT)

    def activate(self, snapshot: SafePointSnapshot) -> dict[str, Any]:
        self._require_transaction()
        blockers = snapshot.blockers()
        if blockers:
            raise InvalidTransition("safe point is blocked by: " + ", ".join(blockers))
        record = self.state()
        pending = _load_json(self.pending_path)
        if (
            not record
            or record.get("state") != UpdateState.WAITING_SAFE_POINT.value
            or not pending
        ):
            raise InvalidTransition("no staged update is waiting for activation")
        pointer = ReleasePointer(**record["release"])
        if pending.get("candidate") != asdict(pointer):
            raise InvalidTransition("pending pointer does not match update state")
        pending["activation_attempted"] = True
        pending["activated_at"] = _now()
        _atomic_json(self.pending_path, pending)
        _atomic_json(self.current_path, asdict(pointer))
        return self._write_state(UpdateState.ACTIVATED, release=pointer)

    def begin_self_test(self) -> dict[str, Any]:
        return self.transition(UpdateState.SELF_TESTING)

    def commit(self) -> dict[str, Any]:
        self._require_transaction()
        record = self.state()
        if not record or record.get("state") != UpdateState.SELF_TESTING.value:
            raise InvalidTransition(
                "candidate must pass launcher self-tests before commit"
            )
        pointer = ReleasePointer(**record["release"])
        current = _load_json(self.current_path)
        if current != asdict(pointer):
            raise InvalidTransition("current pointer no longer names the candidate")
        committed = self._write_state(UpdateState.COMMITTED, release=pointer)
        _atomic_json(self.last_known_good_path, asdict(pointer))
        self.pending_path.unlink(missing_ok=True)
        return committed

    def request_rollback(self, reason: str) -> dict[str, Any]:
        self._require_transaction()
        record = self.state()
        if not record:
            raise InvalidTransition("no update is in progress")
        current = UpdateState(record["state"])
        if UpdateState.ROLLBACK_PENDING not in _TRANSITIONS[current]:
            raise InvalidTransition("only an activated candidate can roll back")
        return self._write_state(
            UpdateState.ROLLBACK_PENDING,
            release=ReleasePointer(**record["release"]),
            reason=reason,
        )

    def rollback(self, reason: str = "candidate_failed") -> dict[str, Any]:
        self._require_transaction()
        record = self.state()
        pending = _load_json(self.pending_path)
        if (
            not record
            or record.get("state")
            not in {
                UpdateState.ACTIVATED.value,
                UpdateState.SELF_TESTING.value,
                UpdateState.ROLLBACK_PENDING.value,
            }
            or not pending
        ):
            raise InvalidTransition("no activated candidate can be rolled back")
        previous = pending.get("previous")
        if not isinstance(previous, dict):
            raise InvalidTransition("last current pointer is unavailable; fail closed")
        _atomic_json(self.current_path, previous)
        pointer = ReleasePointer(**record["release"])
        rolled_back = self._write_state(
            UpdateState.ROLLED_BACK,
            release=pointer,
            reason=reason,
        )
        self.pending_path.unlink(missing_ok=True)
        return rolled_back

    def recover_on_launcher_start(self) -> bool:
        """Rollback an uncommitted activated pointer after a launcher restart."""

        self._require_transaction()

        pending = _load_json(self.pending_path)
        record = self.state()
        if not pending or not record or not pending.get("activation_attempted"):
            return False
        if record.get("state") == UpdateState.COMMITTED.value:
            candidate = pending.get("candidate")
            if _load_json(self.current_path) == candidate and isinstance(
                candidate, dict
            ):
                _atomic_json(self.last_known_good_path, candidate)
            self.pending_path.unlink(missing_ok=True)
            return False
        candidate = pending.get("candidate")
        previous = pending.get("previous")
        current = _load_json(self.current_path)
        if isinstance(previous, dict) and current in (candidate, previous):
            _atomic_json(self.current_path, previous)
            self._write_state(
                UpdateState.ROLLED_BACK,
                release=ReleasePointer(**record["release"]),
                reason="launcher_recovered_uncommitted_candidate",
            )
            self.pending_path.unlink(missing_ok=True)
            return True
        return False

    def launch_pointer(self) -> dict[str, Any]:
        """Choose a valid current artifact, falling back to a valid LKG."""

        for path in (self.current_path, self.last_known_good_path):
            pointer = _load_json(path)
            if not pointer:
                continue
            artifact = pointer.get("artifact")
            if not isinstance(artifact, str) or not artifact:
                continue
            candidate = (self.root / artifact).resolve()
            try:
                inside_root = candidate.is_relative_to(self.root.resolve())
            except AttributeError:  # pragma: no cover - Python 3.11+ has it
                inside_root = str(candidate).startswith(
                    str(self.root.resolve()) + os.sep
                )
            if inside_root and candidate.is_file() and not candidate.is_symlink():
                return pointer
        raise InvalidTransition("no current or last-known-good release is available")
