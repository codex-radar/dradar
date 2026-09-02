"""Crash-safe update state, host lock, safe-point gate and launcher pointers."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, Self

from .download import VerifiedArtifact, open_verified_artifact, stage_verified_artifact
from .manifest import (
    Artifact,
    ManifestError,
    ReleaseManifest,
    verify_artifact,
    verify_signed_manifest,
)

_MAX_STATE_BYTES = 64 * 1024


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

    def fail_closed(self, reason_code: str) -> None: ...


class NullEventSink:
    def emit(self, event_type: str, attributes: Mapping[str, Any]) -> None:
        del event_type, attributes

    def fail_closed(self, reason_code: str) -> None:
        del reason_code


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
            raise TypeError("refill_accepting_new must be boolean")
        if not isinstance(self.worker_supervisor_idle, bool):
            raise TypeError("worker_supervisor_idle must be boolean")

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

    def __enter__(self) -> Self:
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
    return datetime.now(UTC).isoformat()


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
        if path.stat().st_size > _MAX_STATE_BYTES:
            raise InvalidTransition(f"trusted OTA state {path.name} is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidTransition(f"cannot read trusted OTA state {path.name}") from exc
    if not isinstance(value, dict):
        raise InvalidTransition(f"trusted OTA state {path.name} is not an object")
    return value


def _release_pointer(value: Any) -> ReleasePointer | None:
    if not isinstance(value, dict) or set(value) != {
        "release_id",
        "version",
        "sequence",
        "artifact",
    }:
        return None
    try:
        pointer = ReleasePointer(**value)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(pointer.release_id, str)
        or not pointer.release_id
        or not isinstance(pointer.version, str)
        or not pointer.version
        or type(pointer.sequence) is not int
        or pointer.sequence < 1
        or not isinstance(pointer.artifact, str)
        or not pointer.artifact
    ):
        return None
    return pointer


def _resolve_regular_artifact(root: Path, artifact: str) -> Path | None:
    relative = Path(artifact)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return None
    cursor = root
    try:
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                return None
        candidate = cursor.resolve()
        resolved_root = root.resolve()
        if not candidate.is_relative_to(resolved_root) or not candidate.is_file():
            return None
    except (OSError, RuntimeError):
        return None
    return candidate


def _safe_directory_beneath(root: Path, directory: Path) -> bool:
    try:
        relative = directory.absolute().relative_to(root.absolute())
    except ValueError:
        return False
    cursor = root.absolute()
    try:
        if cursor.is_symlink():
            return False
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                return False
        return cursor.is_dir() and cursor.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


class UpdateController:
    """Durable control plane used by a stable launcher, never by model code."""

    def __init__(
        self,
        root: Path,
        *,
        event_sink: EventSink | None = None,
        trusted_keys: Mapping[str, bytes | str] | None = None,
    ):
        self.root = root
        self.releases = root / "releases"
        self.current_path = root / "current.json"
        self.last_known_good_path = root / "last-known-good.json"
        self.pending_path = root / "pending.json"
        self.state_path = root / "update-state.json"
        self.lock_path = root / "update.lock"
        self.event_sink = event_sink or NullEventSink()
        self.trusted_keys = dict(trusted_keys or {})
        self._local = threading.local()
        self._staged_artifact: VerifiedArtifact | None = None

    def set_trusted_keys(self, trusted_keys: Mapping[str, bytes | str]) -> None:
        keys = dict(trusted_keys)
        if self.trusted_keys and keys != self.trusted_keys:
            raise InvalidTransition("trusted OTA key set cannot change during runtime")
        self.trusted_keys = keys

    def pristine_for_legacy_bootstrap(self) -> bool:
        """Whether this host has never created any OTA release metadata."""

        metadata = (
            self.current_path,
            self.last_known_good_path,
            self.pending_path,
            self.state_path,
        )
        if any(os.path.lexists(path) for path in metadata):
            return False
        if os.path.lexists(self.releases):
            try:
                return (
                    not self.releases.is_symlink()
                    and self.releases.is_dir()
                    and not any(self.releases.iterdir())
                )
            except OSError:
                return False
        return True

    def lock(self, *, timeout_seconds: float = 0.0) -> UpdateLock:
        return UpdateLock(self.lock_path, timeout_seconds=timeout_seconds)

    @contextmanager
    def transaction(
        self, *, timeout_seconds: float = 0.0
    ) -> Iterator[UpdateController]:
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
        try:
            record = _load_json(self.state_path)
        except InvalidTransition as exc:
            self.event_sink.fail_closed("update_state_corrupt")
            raise InvalidTransition(
                "OTA blocked: persisted update state is unreadable; current and "
                "last-known-good remain unchanged",
            ) from exc
        if record is None:
            return None
        if record.get("schema_version") != 1:
            self.event_sink.fail_closed("update_state_incompatible")
            raise InvalidTransition(
                "OTA blocked: persisted update state uses an unsupported schema; "
                "current and last-known-good remain unchanged",
            )
        raw_state = record.get("state")
        try:
            UpdateState(raw_state)
        except (TypeError, ValueError) as exc:
            self.event_sink.fail_closed("update_state_incompatible")
            raise InvalidTransition(
                "OTA blocked: persisted update state is unknown; current and "
                "last-known-good remain unchanged",
            ) from exc
        release = record.get("release")
        if not isinstance(release, dict):
            self.event_sink.fail_closed("update_state_corrupt")
            raise InvalidTransition(
                "OTA blocked: persisted release pointer is invalid; current and "
                "last-known-good remain unchanged",
            )
        pointer = _release_pointer(release)
        if pointer is None:
            self.event_sink.fail_closed("update_state_corrupt")
            raise InvalidTransition(
                "OTA blocked: persisted release pointer is invalid; current and "
                "last-known-good remain unchanged",
            )
        return record

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
        if target is UpdateState.FAILED:
            try:
                return self._write_state(target, release=pointer, reason=reason)
            finally:
                self._close_staged_artifact()
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
        except (KeyError, TypeError, ValueError) as exc:
            self.event_sink.fail_closed("update_state_incompatible")
            raise InvalidTransition(
                "OTA blocked: paused update has no compatible resume state; "
                "current and last-known-good remain unchanged",
            ) from exc
        if target not in _PRE_ACTIVATION:
            self.event_sink.fail_closed("update_state_incompatible")
            raise InvalidTransition(
                "OTA blocked: paused update resume state is unsafe; current and "
                "last-known-good remain unchanged",
            )
        return self._write_state(target, release=ReleasePointer(**record["release"]))

    def stage(
        self,
        manifest: ReleaseManifest,
        artifact: Artifact,
        downloaded_path: Path | VerifiedArtifact,
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
            # Pre-OTA clients have no signed pointer. The stable launcher
            # interprets this marker only as "run the bundled version"; it is
            # never accepted as a release pointer or anti-rollback baseline.
            previous = {"schema_version": 1, "legacy_fallback": True}
        release_dir = self.releases / manifest.release_id
        if release_dir.is_symlink():
            raise InvalidTransition("staging requires a safe release directory")
        release_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not _safe_directory_beneath(self.root, release_dir):
            raise InvalidTransition("staging requires a safe release directory")
        try:
            verified = stage_verified_artifact(
                downloaded_path,
                artifact,
                release_dir,
            )
        except ManifestError as exc:
            raise InvalidTransition(
                "staging requires a safe release directory"
            ) from exc
        if not verified.binding_is_current():
            verified.close()
            raise InvalidTransition("staged artifact name is no longer safely bound")
        self._close_staged_artifact()
        self._staged_artifact = verified
        destination = verified.path
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
            "manifest": json.loads(manifest.signed_document),
            "activation_attempted": False,
            "created_at": _now(),
        }
        try:
            _atomic_json(self.pending_path, pending)
            self._write_state(UpdateState.STAGED, release=pointer)
        except BaseException:
            self._close_staged_artifact()
            raise
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
        pending = _load_json(self.pending_path)
        if not pending or not isinstance(pending.get("manifest"), dict):
            raise InvalidTransition("signed pending release record is unavailable")
        try:
            staged = self.staged_artifact()
            staged.verify()
            if not staged.binding_is_current():
                raise InvalidTransition(
                    "staged artifact name is no longer safely bound"
                )
            self._write_committed_record(pointer, pending["manifest"], staged=staged)
            committed = self._write_state(UpdateState.COMMITTED, release=pointer)
            _atomic_json(self.last_known_good_path, asdict(pointer))
            self.pending_path.unlink(missing_ok=True)
            return committed
        finally:
            self._close_staged_artifact()

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
        try:
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
                raise InvalidTransition(
                    "last current pointer is unavailable; fail closed"
                )
            _atomic_json(self.current_path, previous)
            pointer = ReleasePointer(**record["release"])
            rolled_back = self._write_state(
                UpdateState.ROLLED_BACK,
                release=pointer,
                reason=reason,
            )
            self.pending_path.unlink(missing_ok=True)
            return rolled_back
        finally:
            self._close_staged_artifact()

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
                manifest = pending.get("manifest")
                pointer = _release_pointer(candidate)
                if pointer is None or not isinstance(manifest, dict):
                    raise InvalidTransition(
                        "committed release recovery lacks signed proof"
                    )
                self._write_committed_record(pointer, manifest)
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
                reason="update_crash_recovery",
            )
            self.pending_path.unlink(missing_ok=True)
            return True
        return False

    def launch_pointer(self) -> dict[str, Any]:
        """Choose a valid current artifact, falling back to a valid LKG."""

        if not self.trusted_keys:
            raise InvalidTransition("trusted OTA signing keys are unavailable")
        valid: list[tuple[ReleasePointer, dict[str, Any]]] = []
        for path in (self.current_path, self.last_known_good_path):
            pointer = _load_json(path)
            if not pointer:
                continue
            if self._verify_committed_pointer(pointer):
                parsed = _release_pointer(pointer)
                if parsed is not None:
                    valid.append((parsed, pointer))
        if valid:
            highest = max(item[0].sequence for item in valid)
            winners = [item for item in valid if item[0].sequence == highest]
            if len({tuple(asdict(item[0]).items()) for item in winners}) != 1:
                raise InvalidTransition("conflicting committed OTA pointers")
            return winners[0][1]
        raise InvalidTransition("no current or last-known-good release is available")

    def launch_artifact(self) -> VerifiedArtifact:
        """Return the selected executable bound to the inode just verified."""

        value = self.launch_pointer()
        pointer = ReleasePointer(**value)
        record = _load_json(self._record_path(pointer))
        if not record or not isinstance(record.get("manifest"), dict):
            raise InvalidTransition("signed launch record is unavailable")
        manifest = verify_signed_manifest(record["manifest"], self.trusted_keys)
        artifact = next(
            (
                item
                for item in manifest.artifacts
                if item.filename == Path(pointer.artifact).name
            ),
            None,
        )
        if artifact is None:
            raise InvalidTransition("signed launch artifact is unavailable")
        try:
            return open_verified_artifact(self.root / pointer.artifact, artifact)
        except ManifestError as exc:
            raise InvalidTransition("signed launch artifact changed") from exc

    def _record_path(self, pointer: ReleasePointer) -> Path:
        if Path(
            pointer.release_id
        ).name != pointer.release_id or pointer.release_id in {".", ".."}:
            raise InvalidTransition("release id is unsafe")
        return self.releases / pointer.release_id / "release-record.json"

    def _write_committed_record(
        self,
        pointer: ReleasePointer,
        signed_manifest: Mapping[str, Any],
        *,
        staged: VerifiedArtifact | None = None,
    ) -> None:
        self._require_transaction()
        manifest = verify_signed_manifest(signed_manifest, self.trusted_keys)
        artifact = next(
            (
                item
                for item in manifest.artifacts
                if item.filename == Path(pointer.artifact).name
            ),
            None,
        )
        expected_artifact = (
            f"releases/{pointer.release_id}/{Path(pointer.artifact).name}"
        )
        if (
            manifest.release_id != pointer.release_id
            or manifest.version != pointer.version
            or manifest.sequence != pointer.sequence
            or artifact is None
            or pointer.artifact != expected_artifact
        ):
            raise InvalidTransition("signed release record does not match pointer")
        if staged is not None:
            if (
                staged.artifact != artifact
                or staged.path != self.root / pointer.artifact
            ):
                raise InvalidTransition("opened staged artifact does not match pointer")
            staged.verify()
            if not staged.binding_is_current():
                raise InvalidTransition(
                    "staged artifact name is no longer safely bound"
                )
        else:
            candidate = _resolve_regular_artifact(self.root, pointer.artifact)
            if candidate is None:
                raise InvalidTransition("committed artifact is unavailable")
            verify_artifact(candidate, artifact)
        record = {
            "schema_version": 1,
            "committed": True,
            "pointer": asdict(pointer),
            "manifest": dict(signed_manifest),
        }
        record_path = self._record_path(pointer)
        if record_path.is_symlink() or (
            record_path.exists()
            and _resolve_regular_artifact(
                self.root,
                str(record_path.relative_to(self.root)),
            )
            is None
        ):
            raise InvalidTransition("immutable release record path is unsafe")
        existing = _load_json(record_path)
        if existing is not None and existing != record:
            raise InvalidTransition("immutable release record already differs")
        if existing is None:
            _atomic_json(record_path, record)

    def _verify_committed_pointer(self, value: Mapping[str, Any]) -> bool:
        pointer = _release_pointer(value)
        if pointer is None:
            return False
        try:
            record_path = self._record_path(pointer)
            resolved_record = _resolve_regular_artifact(
                self.root,
                str(record_path.relative_to(self.root)),
            )
            if resolved_record is None or resolved_record != record_path.resolve():
                return False
            record = _load_json(resolved_record)
            if (
                not record
                or set(record) != {"schema_version", "committed", "pointer", "manifest"}
                or record.get("schema_version") != 1
                or record.get("committed") is not True
                or record.get("pointer") != asdict(pointer)
                or not isinstance(record.get("manifest"), dict)
            ):
                return False
            manifest = verify_signed_manifest(record["manifest"], self.trusted_keys)
            artifact = next(
                (
                    item
                    for item in manifest.artifacts
                    if item.filename == Path(pointer.artifact).name
                ),
                None,
            )
            expected = f"releases/{pointer.release_id}/{Path(pointer.artifact).name}"
            candidate = _resolve_regular_artifact(self.root, pointer.artifact)
            if (
                manifest.release_id != pointer.release_id
                or manifest.version != pointer.version
                or manifest.sequence != pointer.sequence
                or pointer.artifact != expected
                or artifact is None
                or candidate is None
            ):
                return False
            verify_artifact(candidate, artifact)
        except (InvalidTransition, ManifestError, OSError, ValueError):
            return False
        return True

    def committed_pointer(self) -> ReleasePointer:
        """Read the authoritative anti-rollback baseline from durable pointers."""

        if not self.trusted_keys:
            raise InvalidTransition("trusted OTA signing keys are unavailable")
        valid: list[ReleasePointer] = []
        for path in (self.last_known_good_path, self.current_path):
            value = _load_json(path)
            parsed = _release_pointer(value)
            if parsed and self._verify_committed_pointer(value):
                valid.append(parsed)
        if valid:
            highest = max(item.sequence for item in valid)
            winners = [item for item in valid if item.sequence == highest]
            if len({tuple(asdict(item).items()) for item in winners}) != 1:
                raise InvalidTransition("conflicting committed OTA pointers")
            return winners[0]
        raise InvalidTransition("no trusted committed OTA baseline is available")

    def staged_artifact(self) -> VerifiedArtifact:
        record = self.state()
        pending = _load_json(self.pending_path)
        if not record or not pending or not isinstance(pending.get("manifest"), dict):
            raise InvalidTransition("signed staged artifact is unavailable")
        pointer = _release_pointer(record.get("release"))
        if pointer is None or pending.get("candidate") != asdict(pointer):
            raise InvalidTransition("staged artifact pointer is inconsistent")
        if self._staged_artifact is not None:
            return self._staged_artifact
        manifest = verify_signed_manifest(pending["manifest"], self.trusted_keys)
        artifact = next(
            (
                item
                for item in manifest.artifacts
                if item.filename == Path(pointer.artifact).name
            ),
            None,
        )
        if artifact is None:
            raise InvalidTransition("signed staged artifact metadata is unavailable")
        try:
            verified = stage_verified_artifact(
                self.root / pointer.artifact,
                artifact,
                (self.root / pointer.artifact).parent,
            )
        except ManifestError as exc:
            raise InvalidTransition(
                "staged artifact cannot be reopened safely"
            ) from exc
        self._staged_artifact = verified
        return verified

    def _close_staged_artifact(self) -> None:
        if self._staged_artifact is None:
            return
        staged = self._staged_artifact
        self._staged_artifact = None
        staged.close()
