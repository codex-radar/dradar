"""Crash-safe staging for the integrity-critical ``model.patch`` artifact.

Pier owns ``trial/artifacts/model.patch`` while a trial is running.  Once the
trial finishes, DRadar keeps a second, private source copy plus a small digest
manifest.  Upload and resume paths verify both copies against that manifest;
if exactly one copy is missing, it can be rebuilt atomically from the other.
Conflicting copies are never overwritten automatically.
"""

from __future__ import annotations

from .artifact_boundary import TrialFiles, UnsafeArtifact, read_trial_file

import stat
import hashlib
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1
STAGED_RELATIVE = Path("artifacts") / "model.patch"
_STATE_RELATIVE = Path(".dradar") / "artifact-staging"
SOURCE_RELATIVE = _STATE_RELATIVE / "model.patch.source"
MANIFEST_RELATIVE = _STATE_RELATIVE / "manifest.json"
_LOCK_RELATIVE = _STATE_RELATIVE / "staging.lock"

LEDGER_SCHEMA_KEY = "artifact_staging_schema"
LEDGER_SOURCE_KEY = "patch_source_path"
LEDGER_STAGED_KEY = "patch_staged_path"
LEDGER_DIGEST_KEY = "patch_sha256"
LEDGER_BYTES_KEY = "patch_bytes"
_LEDGER_KEYS = (
    LEDGER_SCHEMA_KEY,
    LEDGER_SOURCE_KEY,
    LEDGER_STAGED_KEY,
    LEDGER_DIGEST_KEY,
    LEDGER_BYTES_KEY,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PROCESS_LOCK = threading.Lock()


class PatchStagingError(RuntimeError):
    """An integrity failure that must remain retryable and keep both files."""

    def __init__(
        self,
        reason: str,
        *,
        source_present: bool,
        staged_present: bool,
    ) -> None:
        self.reason = reason
        self.source_present = source_present
        self.staged_present = staged_present
        super().__init__(
            f"{reason} (source_present={source_present}, "
            f"staged_present={staged_present})"
        )

    def telemetry(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked",
            "reason": self.reason,
            "source_present": self.source_present,
            "staged_present": self.staged_present,
        }


@dataclass(frozen=True)
class StagedPatch:
    source: Path
    staged: Path
    sha256: str
    size: int
    action: str
    source_present_before: bool
    staged_present_before: bool
    data: bytes = field(repr=False, compare=False)

    @property
    def ledger_fields(self) -> dict:
        return {
            LEDGER_SCHEMA_KEY: SCHEMA_VERSION,
            LEDGER_SOURCE_KEY: str(self.source),
            LEDGER_STAGED_KEY: str(self.staged),
            LEDGER_DIGEST_KEY: self.sha256,
            LEDGER_BYTES_KEY: self.size,
        }

    @property
    def recovery_telemetry(self) -> dict | None:
        if self.action not in {"source-reconstructed", "source-only"}:
            return None
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "recovered",
            "reason": (
                "source-missing/staged-present"
                if self.action == "source-reconstructed"
                else "source-present/staged-missing"
            ),
            "source_present": self.source_present_before,
            "staged_present": self.staged_present_before,
            "patch_bytes": self.size,
        }


@dataclass(frozen=True)
class _ExpectedPatch:
    sha256: str
    size: int


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _raise(reason: str, source: Path, staged: Path) -> None:
    raise PatchStagingError(
        reason,
        source_present=source.is_file() and not source.is_symlink(),
        staged_present=staged.is_file() and not staged.is_symlink(),
    )


def _read_optional(path: Path, *, label: str, source: Path, staged: Path) -> bytes | None:
    root = source.parents[2]
    try:
        return read_trial_file(root, path.relative_to(root))
    except FileNotFoundError:
        return None


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    root = path.parents[2]
    with TrialFiles(root) as files:
        files.write_host(path.relative_to(root), data)


def _atomic_write_checked(
    path: Path,
    data: bytes,
    *,
    label: str,
    source: Path,
    staged: Path,
) -> None:
    try:
        _atomic_write(path, data)
    except OSError as exc:
        raise PatchStagingError(
            f"{label}_atomic_write_failed:{type(exc).__name__}",
            source_present=source.is_file() and not source.is_symlink(),
            staged_present=staged.is_file() and not staged.is_symlink(),
        ) from exc


@contextmanager
def _staging_lock(trial_dir: Path, source: Path, staged: Path) -> Iterator[None]:
    """Serialize same-trial recovery across threads and worker processes."""
    lock_path = trial_dir / _LOCK_RELATIVE
    with _PROCESS_LOCK:
        fd = None
        boundary = None
        windows_lock = False
        try:
            boundary = TrialFiles(trial_dir).__enter__()
            fd = boundary.open_lock(_LOCK_RELATIVE)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeArtifact("unsafe_lock_file")
            boundary.verify()
            if info.st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - Windows CI exercises callers
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                windows_lock = True
            yield
            boundary.verify()
        except PatchStagingError:
            raise
        except OSError as exc:
            raise PatchStagingError(
                f"staging_io_failed:{type(exc).__name__}",
                source_present=source.is_file() and not source.is_symlink(),
                staged_present=staged.is_file() and not staged.is_symlink(),
            ) from exc
        finally:
            if fd is not None and windows_lock:  # pragma: no cover
                try:
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            elif fd is not None:
                try:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
            if fd is not None:
                os.close(fd)
            if boundary is not None:
                boundary.__exit__(None, None, None)


def _parse_expected(
    value: dict,
    *,
    source_path: Path,
    staged_path: Path,
    source: Path,
    staged: Path,
    origin: str,
) -> _ExpectedPatch:
    schema = value.get(LEDGER_SCHEMA_KEY)
    source_value = value.get(LEDGER_SOURCE_KEY)
    staged_value = value.get(LEDGER_STAGED_KEY)
    digest = value.get(LEDGER_DIGEST_KEY)
    size = value.get(LEDGER_BYTES_KEY)
    if schema != SCHEMA_VERSION:
        _raise(f"{origin}_schema_invalid", source, staged)
    try:
        source_matches = (
            isinstance(source_value, str)
            and Path(source_value).absolute() == source_path
        )
    except (OSError, RuntimeError):
        source_matches = False
    if not source_matches:
        _raise(f"{origin}_source_path_invalid", source, staged)
    try:
        staged_matches = (
            isinstance(staged_value, str)
            and Path(staged_value).absolute() == staged_path
        )
    except (OSError, RuntimeError):
        staged_matches = False
    if not staged_matches:
        _raise(f"{origin}_staged_path_invalid", source, staged)
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        _raise(f"{origin}_digest_invalid", source, staged)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        _raise(f"{origin}_size_invalid", source, staged)
    return _ExpectedPatch(digest, size)


def _expected_from_entry(
    entry: dict | None,
    *,
    source_path: Path,
    staged_path: Path,
    source: Path,
    staged: Path,
) -> _ExpectedPatch | None:
    if entry is None:
        return None
    present = [key in entry for key in _LEDGER_KEYS]
    if not any(present):
        return None
    if not all(present):
        _raise("ledger_metadata_incomplete", source, staged)
    return _parse_expected(
        entry,
        source_path=source_path,
        staged_path=staged_path,
        source=source,
        staged=staged,
        origin="ledger",
    )


def _expected_from_manifest(
    manifest: Path,
    *,
    trial_dir: Path,
    source_path: Path,
    staged_path: Path,
    source: Path,
    staged: Path,
) -> _ExpectedPatch | None:
    if manifest.is_symlink():
        _raise("manifest_is_symlink", source, staged)
    if not manifest.exists():
        return None
    if not manifest.is_file():
        _raise("manifest_is_not_file", source, staged)
    try:
        value = json.loads(read_trial_file(trial_dir, manifest.relative_to(trial_dir)))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PatchStagingError(
            f"manifest_unreadable:{type(exc).__name__}",
            source_present=source.is_file() and not source.is_symlink(),
            staged_present=staged.is_file() and not staged.is_symlink(),
        ) from exc
    if not isinstance(value, dict):
        _raise("manifest_not_object", source, staged)
    translated = {
        LEDGER_SCHEMA_KEY: value.get("schema_version"),
        LEDGER_SOURCE_KEY: str(trial_dir / str(value.get("source_patch", ""))),
        LEDGER_STAGED_KEY: str(trial_dir / str(value.get("staged_patch", ""))),
        LEDGER_DIGEST_KEY: value.get("sha256"),
        LEDGER_BYTES_KEY: value.get("bytes"),
    }
    return _parse_expected(
        translated,
        source_path=source_path,
        staged_path=staged_path,
        source=source,
        staged=staged,
        origin="manifest",
    )


def _write_manifest(
    manifest: Path,
    expected: _ExpectedPatch,
    *,
    source: Path,
    staged: Path,
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_patch": SOURCE_RELATIVE.as_posix(),
        "staged_patch": staged.relative_to(source.parents[2]).as_posix(),
        "sha256": expected.sha256,
        "bytes": expected.size,
    }
    _atomic_write_checked(
        manifest,
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        label="manifest",
        source=source,
        staged=staged,
    )


def _matches(data: bytes, expected: _ExpectedPatch) -> bool:
    return len(data) == expected.size and _digest(data) == expected.sha256


def ensure_staged_patch(trial_dir: Path, entry: dict | None = None) -> StagedPatch:
    """Verify or reconstruct the durable source and canonical staged patch.

    The digest in an existing ledger/manifest is authoritative.  A missing
    copy is rebuilt via temp-file + fsync + atomic rename.  If both copies are
    present but either disagrees with the authority, neither is modified.
    """
    trial_dir = trial_dir.absolute()
    if not trial_dir.exists():
        raise PatchStagingError("source_and_staged_missing", source_present=False, staged_present=False)
    source = trial_dir / SOURCE_RELATIVE
    staged = trial_dir / ".dradar" / "host-output" / "model.patch"
    if not (staged.exists() or staged.is_symlink()):
        staged = trial_dir / STAGED_RELATIVE
    manifest = trial_dir / MANIFEST_RELATIVE

    # These directories are host-owned boundaries. Following a task-created
    # symlink here could copy an artifact outside its trial or overwrite an
    # unrelated file during recovery.
    for label, directory in (
        ("state_parent", trial_dir / _STATE_RELATIVE.parent),
        ("state_dir", source.parent),
        ("artifacts_dir", staged.parent),
    ):
        if directory.is_symlink():
            _raise(f"{label}_is_symlink", source, staged)

    with _staging_lock(trial_dir, source, staged):
        ledger_expected = _expected_from_entry(
            entry,
            source_path=source,
            staged_path=staged,
            source=source,
            staged=staged,
        )
        manifest_expected = _expected_from_manifest(
            manifest,
            trial_dir=trial_dir,
            source_path=source,
            staged_path=staged,
            source=source,
            staged=staged,
        )
        if (
            ledger_expected is not None
            and manifest_expected is not None
            and ledger_expected != manifest_expected
        ):
            _raise("ledger_manifest_mismatch", source, staged)
        expected = ledger_expected or manifest_expected

        source_data = _read_optional(
            source, label="source", source=source, staged=staged,
        )
        staged_data = _read_optional(
            staged, label="staged", source=source, staged=staged,
        )
        source_before = source_data is not None
        staged_before = staged_data is not None

        if expected is None:
            if source_data is None and staged_data is None:
                _raise("source_and_staged_missing", source, staged)
            if (
                source_data is not None
                and staged_data is not None
                and source_data != staged_data
            ):
                _raise("untracked_source_staged_mismatch", source, staged)
            authoritative = source_data if source_data is not None else staged_data
            assert authoritative is not None
            expected = _ExpectedPatch(_digest(authoritative), len(authoritative))

        if source_data is not None and not _matches(source_data, expected):
            _raise("source_digest_mismatch", source, staged)
        if staged_data is not None and not _matches(staged_data, expected):
            _raise("staged_digest_mismatch", source, staged)
        if source_data is None and staged_data is None:
            _raise("source_and_staged_missing", source, staged)

        action = "verified"
        had_authority = ledger_expected is not None or manifest_expected is not None
        if source_data is None:
            assert staged_data is not None
            _atomic_write_checked(
                source, staged_data, label="source", source=source, staged=staged,
            )
            source_data = staged_data
            action = "source-reconstructed" if had_authority else "source-initialized"
        if staged_data is None:
            assert source_data is not None
            # Upload the verified private source; never reconstruct host data
            # inside the container-writable artifacts directory.
            staged_data = source_data
            action = "source-only"

        # Verify the committed destinations, then persist the authority.  A
        # crash before this manifest write is harmless: two equal copies are
        # sufficient to recreate it on the next resume.
        committed_source = _read_optional(
            source, label="source", source=source, staged=staged,
        )
        committed_staged = _read_optional(
            staged, label="staged", source=source, staged=staged,
        )
        if committed_source is None or not _matches(committed_source, expected):
            _raise("source_commit_verification_failed", source, staged)
        if (committed_staged is None and staged_before) or (
            committed_staged is not None and not _matches(committed_staged, expected)
        ):
            _raise("staged_commit_verification_failed", source, staged)
        if manifest_expected is None:
            _write_manifest(
                manifest, expected, source=source, staged=staged,
            )

        return StagedPatch(
            source=source,
            staged=staged,
            sha256=expected.sha256,
            size=expected.size,
            action=action,
            source_present_before=source_before,
            staged_present_before=staged_before,
            data=committed_source,
        )


__all__ = [
    "PatchStagingError",
    "StagedPatch",
    "ensure_staged_patch",
    "LEDGER_SCHEMA_KEY",
    "LEDGER_SOURCE_KEY",
    "LEDGER_STAGED_KEY",
    "LEDGER_DIGEST_KEY",
    "LEDGER_BYTES_KEY",
]
