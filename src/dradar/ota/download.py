"""Bounded, resumeless OTA downloads that never publish partial artifacts."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import uuid
from pathlib import Path
from typing import Protocol

from .manifest import Artifact, ManifestError


class StreamingResponse(Protocol):
    def __enter__(self): ...
    def __exit__(self, exc_type, exc, traceback): ...
    def raise_for_status(self) -> None: ...
    def iter_bytes(self, chunk_size: int = 65536): ...


class StreamingClient(Protocol):
    def stream(self, method: str, url: str, **kwargs) -> StreamingResponse: ...


def _open_safe_directory(path: Path) -> int:
    """Open/create a directory without following any POSIX symlink component."""

    absolute = path.absolute()
    if os.name == "nt":  # pragma: no cover - exercised by Windows CI
        absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
        cursor = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            cursor /= part
            if cursor.is_symlink():
                raise ManifestError("download destination is not a safe directory")
        return os.open(absolute, os.O_RDONLY)

    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    current = os.open(absolute.anchor, directory_flags | nofollow)
    try:
        for part in absolute.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=current)
            except FileExistsError:
                pass
            next_fd = os.open(
                part,
                directory_flags | nofollow,
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        return current
    except OSError as exc:
        os.close(current)
        raise ManifestError("download destination is not a safe directory") from exc


def _verify_open_artifact(
    fd: int,
    artifact: Artifact,
    *,
    expected_links: int = 1,
) -> None:
    details = os.fstat(fd)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_size != artifact.size
        or details.st_nlink != expected_links
    ):
        raise ManifestError("downloaded artifact size or file type is invalid")
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    for chunk in iter(lambda: os.read(fd, 1024 * 1024), b""):
        digest.update(chunk)
    if not hmac.compare_digest(digest.hexdigest(), artifact.sha256):
        raise ManifestError("downloaded artifact SHA-256 mismatch")


def _open_existing(dir_fd: int, filename: str) -> int | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(filename, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ManifestError(
            "immutable artifact already exists with different content"
        ) from exc


def _directory_matches_path(dir_fd: int, path: Path) -> bool:
    try:
        opened = os.fstat(dir_fd)
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(current.st_mode) and (opened.st_dev, opened.st_ino) == (
        current.st_dev,
        current.st_ino,
    )


def _same_open_file(left_fd: int, right_fd: int) -> bool:
    left = os.fstat(left_fd)
    right = os.fstat(right_fd)
    return stat.S_ISREG(left.st_mode) and (left.st_dev, left.st_ino) == (
        right.st_dev,
        right.st_ino,
    )


def _name_matches_open_file(dir_fd: int, filename: str, file_fd: int) -> bool:
    try:
        named = os.stat(filename, dir_fd=dir_fd, follow_symlinks=False)
        opened = os.fstat(file_fd)
    except OSError:
        return False
    return stat.S_ISREG(named.st_mode) and (named.st_dev, named.st_ino) == (
        opened.st_dev,
        opened.st_ino,
    )


def download_verified_artifact(
    client: StreamingClient,
    artifact: Artifact,
    destination: Path,
) -> Path:
    """Download once, verify, fsync, then atomically publish the exact artifact."""

    directory_fd = _open_safe_directory(destination)
    final = destination / artifact.filename
    existing_fd = _open_existing(directory_fd, artifact.filename)
    if existing_fd is not None:
        try:
            _verify_open_artifact(existing_fd, artifact)
            if not _directory_matches_path(directory_fd, destination):
                raise ManifestError("download destination changed during verification")
        except ManifestError as exc:
            raise ManifestError(
                "immutable artifact already exists with different content"
            ) from exc
        finally:
            os.close(existing_fd)
            os.close(directory_fd)
        return final
    temporary_name = f".{artifact.filename}.{uuid.uuid4().hex}.partial"
    fd: int | None = None
    try:
        fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        written = 0
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as handle:
            fd = None
            with client.stream("GET", artifact.url, follow_redirects=False) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    if not isinstance(chunk, bytes):
                        raise ManifestError(
                            "artifact download returned a non-byte chunk"
                        )
                    written += len(chunk)
                    if written > artifact.size:
                        raise ManifestError(
                            "artifact download exceeded its signed size"
                        )
                    handle.write(chunk)
                    digest.update(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if written != artifact.size:
            raise ManifestError("downloaded artifact size or file type is invalid")
        if not hmac.compare_digest(digest.hexdigest(), artifact.sha256):
            raise ManifestError("downloaded artifact SHA-256 mismatch")
        published = False
        temporary_fd = _open_existing(directory_fd, temporary_name)
        if temporary_fd is None:
            raise ManifestError("verified temporary artifact disappeared")
        final_fd: int | None = None
        try:
            os.link(
                temporary_name,
                artifact.filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            published = True
        except FileExistsError:
            existing_fd = _open_existing(directory_fd, artifact.filename)
            if existing_fd is None:
                raise ManifestError("artifact publication raced with removal")
            try:
                _verify_open_artifact(existing_fd, artifact)
            except ManifestError as exc:
                raise ManifestError(
                    "immutable artifact already exists with different content"
                ) from exc
            finally:
                os.close(existing_fd)
        try:
            final_fd = _open_existing(directory_fd, artifact.filename)
            if final_fd is None or not _same_open_file(temporary_fd, final_fd):
                raise ManifestError("artifact name changed during publication")
            _verify_open_artifact(final_fd, artifact, expected_links=2)
            if not _directory_matches_path(directory_fd, destination):
                raise ManifestError("download destination changed during publication")
            os.unlink(temporary_name, dir_fd=directory_fd)
            if os.fstat(final_fd).st_nlink != 1 or not _name_matches_open_file(
                directory_fd,
                artifact.filename,
                final_fd,
            ):
                raise ManifestError("artifact name changed during publication")
            return final
        except BaseException:
            if (
                published
                and final_fd is not None
                and _name_matches_open_file(
                    directory_fd,
                    artifact.filename,
                    final_fd,
                )
            ):
                os.unlink(artifact.filename, dir_fd=directory_fd)
            raise
        finally:
            os.close(temporary_fd)
            if final_fd is not None:
                os.close(final_fd)
    except BaseException:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory_fd)
