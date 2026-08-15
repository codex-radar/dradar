"""Opt-in, local-only retention for Codex session transcripts.

Completed Pier jobs are normally removed after the server acknowledges their
submission.  Volunteers who explicitly request an archive can keep the raw
``agent/sessions`` JSONL files under DRadar's own private history directory,
without mixing them into Codex's native session index.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path

from .local_config import HOME


ARCHIVE_RELATIVE = Path("history") / "codex-sessions"
_SAFE_COMPONENT = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)


def safe_assignment_component(assignment_id: str) -> str:
    """Return a bounded, collision-resistant path component.

    Normal UUID-like assignment IDs remain unchanged.  Unexpected characters
    are replaced and a digest is appended, so two malformed IDs cannot
    silently share an archive directory.
    """

    raw = str(assignment_id)
    cleaned = "".join(char if char in _SAFE_COMPONENT else "_" for char in raw)
    if not cleaned:
        cleaned = "assignment"
    if cleaned != raw or len(cleaned) > 96:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        cleaned = f"{cleaned[:80]}-{digest}"
    return cleaned


def _private_directory(path: Path, boundary: Path) -> Path:
    """Create ``path`` below ``boundary`` without traversing symlink dirs."""

    boundary.mkdir(parents=True, exist_ok=True)
    resolved_boundary = boundary.resolve()
    try:
        relative = path.relative_to(boundary)
    except ValueError as exc:
        raise OSError(f"archive path escapes {boundary}") from exc
    current = resolved_boundary
    for component in relative.parts:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError(f"archive path component is not a real directory: {current}")
        if os.name != "nt":
            current.chmod(0o700)
    return current


def _session_sources(root: Path) -> list[tuple[Path, Path]]:
    """Return regular JSONL files and safe relative paths below ``root``."""

    if root.is_symlink() or not root.is_dir():
        return []
    resolved_root = root.resolve()
    sources: list[tuple[Path, Path]] = []
    for directory, dirnames, filenames in os.walk(resolved_root, followlinks=False):
        current = Path(directory)
        dirnames[:] = [
            name for name in dirnames
            if not (current / name).is_symlink()
        ]
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            source = current / name
            try:
                info = source.lstat()
                relative = source.relative_to(resolved_root)
            except (OSError, ValueError):
                continue
            if stat.S_ISREG(info.st_mode):
                sources.append((source, relative))
    return sorted(sources, key=lambda item: item[1].as_posix())


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"not a regular file: {path}")
        with os.fdopen(fd, "rb") as source:
            fd = -1
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        if fd >= 0:
            os.close(fd)
    return digest.hexdigest()


def _atomic_private_copy(source: Path, destination: Path) -> tuple[Path, bool]:
    """Copy one regular file atomically; never overwrite different content."""

    source_digest = _digest_file(source)
    target = destination
    if target.exists() or target.is_symlink():
        if not target.is_symlink() and target.is_file() and _digest_file(target) == source_digest:
            return target, False
        target = destination.with_name(
            f"{destination.stem}-{source_digest[:12]}{destination.suffix}"
        )
        if target.exists() or target.is_symlink():
            if not target.is_symlink() and target.is_file() and _digest_file(target) == source_digest:
                return target, False
            raise OSError(f"archive destination already exists: {target}")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    source_fd = os.open(source, flags)
    temp_fd, temp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp",
    )
    temporary = Path(temp_name)
    try:
        with os.fdopen(source_fd, "rb") as source_file, os.fdopen(
            temp_fd, "wb"
        ) as destination_file:
            source_fd = temp_fd = -1
            if not stat.S_ISREG(os.fstat(source_file.fileno()).st_mode):
                raise OSError(f"not a regular file: {source}")
            shutil.copyfileobj(source_file, destination_file, length=1024 * 1024)
            destination_file.flush()
            os.fsync(destination_file.fileno())
        if _digest_file(temporary) != source_digest:
            raise OSError(f"session archive copy verification failed: {source}")
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, target)
        return target, True
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if temp_fd >= 0:
            os.close(temp_fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def archive_codex_sessions(
    home: Path, trial_dir: Path, assignment_id: str,
) -> tuple[int, Path] | None:
    """Best-effort archive of a completed trial's Codex JSONL sessions."""

    sessions = Path(trial_dir) / "agent" / "sessions"
    sources = _session_sources(sessions)
    if not sources:
        return None
    boundary = Path(home)
    boundary.mkdir(parents=True, exist_ok=True)
    boundary = boundary.resolve()
    archive_root = _private_directory(boundary / ARCHIVE_RELATIVE, boundary)
    destination_root = _private_directory(
        archive_root / safe_assignment_component(assignment_id), boundary,
    )
    copied = 0
    for source, relative in sources:
        destination_parent = _private_directory(
            destination_root / relative.parent, boundary,
        )
        _target, created = _atomic_private_copy(
            source, destination_parent / relative.name,
        )
        copied += int(created)
    return copied, destination_root


def archive_after_submit(home: Path, entry: dict) -> None:
    """Apply the ledger-recorded opt-in without affecting submit success."""

    if not entry.get("archive_session") or entry.get("keep"):
        return
    try:
        result = archive_codex_sessions(
            home, Path(entry["trial_dir"]), str(entry["assignment_id"]),
        )
    except Exception as exc:  # best effort: an accepted submission stays accepted
        print(f"  warning: could not archive Codex sessions locally: {exc}")
        return
    if result is not None:
        copied, destination = result
        print(f"  archived {copied} Codex session file(s) to {destination}")


def cmd_sessions_prune(args) -> int:
    """Report or explicitly delete DRadar-owned session archives."""

    root = HOME / ARCHIVE_RELATIVE
    current = HOME
    for component in ARCHIVE_RELATIVE.parts:
        current = current / component
        if current.is_symlink():
            print(f"refusing to prune a symlinked session archive path: {current}")
            return 1
    if not root.is_dir():
        print("no archived Codex sessions to prune")
        return 0
    assignments = sorted(
        path for path in root.iterdir()
        if path.is_dir() and not path.is_symlink()
    )
    total_bytes = sum(
        path.stat().st_size
        for assignment in assignments
        for path in assignment.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    size_mib = total_bytes / (1024 * 1024)
    if not getattr(args, "yes", False):
        print(
            f"{len(assignments)} archived assignment(s), {size_mib:.1f} MiB total; "
            "pass --yes to delete them"
        )
        return 0
    for assignment in assignments:
        shutil.rmtree(assignment)
    try:
        root.rmdir()
    except OSError:
        pass
    print(
        f"pruned {len(assignments)} archived assignment(s), freed {size_mib:.1f} MiB"
    )
    return 0


__all__ = [
    "archive_after_submit", "archive_codex_sessions", "cmd_sessions_prune",
    "safe_assignment_component",
]
