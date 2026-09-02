"""Bounded, resumeless OTA downloads that never publish partial artifacts."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self

from .manifest import Artifact, ManifestError


class StreamingResponse(Protocol):
    def __enter__(self): ...
    def __exit__(self, exc_type, exc, traceback): ...
    def raise_for_status(self) -> None: ...
    def iter_bytes(self, chunk_size: int = 65536): ...


class StreamingClient(Protocol):
    def stream(self, method: str, url: str, **kwargs) -> StreamingResponse: ...


@dataclass(eq=False)
class VerifiedArtifact:
    """An artifact capability bound to open directory and file inodes."""

    path: Path
    artifact: Artifact
    _directory_fd: int
    _file_fd: int
    _closed: bool = False

    def __eq__(self, other: object) -> bool:
        if isinstance(other, VerifiedArtifact):
            return self.path == other.path
        if isinstance(other, Path):
            return self.path == other
        return NotImplemented

    def verify(self) -> None:
        self._ensure_open()
        _verify_open_artifact(self._file_fd, self.artifact)

    def binding_is_current(self) -> bool:
        self._ensure_open()
        return _directory_matches_path(
            self._directory_fd,
            self.path.parent,
        ) and _name_matches_open_file(
            self._directory_fd,
            self.path.name,
            self._file_fd,
        )

    def read_bytes(self) -> bytes:
        self._ensure_open()
        size = os.fstat(self._file_fd).st_size
        if hasattr(os, "pread"):
            chunks = []
            offset = 0
            while offset < size:
                chunk = os.pread(self._file_fd, min(1024 * 1024, size - offset), offset)
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
            return b"".join(chunks)
        current = os.lseek(self._file_fd, 0, os.SEEK_CUR)  # pragma: no cover
        try:  # pragma: no cover - Windows fallback
            os.lseek(self._file_fd, 0, os.SEEK_SET)
            return b"".join(iter(lambda: os.read(self._file_fd, 1024 * 1024), b""))
        finally:
            os.lseek(self._file_fd, current, os.SEEK_SET)

    def duplicate_fd(self) -> int:
        self._ensure_open()
        return os.dup(self._file_fd)

    def close(self) -> None:
        if self._closed:
            return
        os.close(self._file_fd)
        os.close(self._directory_fd)
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ManifestError("verified artifact capability is closed")

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - defensive leak guard
        try:
            self.close()
        except OSError:
            pass


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
) -> VerifiedArtifact:
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
            os.close(existing_fd)
            os.close(directory_fd)
            raise ManifestError(
                "immutable artifact already exists with different content"
            ) from exc
        return VerifiedArtifact(final, artifact, directory_fd, existing_fd)
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
            result = VerifiedArtifact(final, artifact, directory_fd, final_fd)
            directory_fd = -1
            final_fd = None
            return result
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
        if directory_fd >= 0:
            os.close(directory_fd)


class _OpenFileResponse:
    def __init__(self, fd: int):
        self.fd = fd

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, chunk_size: int = 65536):
        offset = 0
        size = os.fstat(self.fd).st_size
        while offset < size:
            if hasattr(os, "pread"):
                chunk = os.pread(self.fd, min(chunk_size, size - offset), offset)
            else:  # pragma: no cover - Windows fallback
                os.lseek(self.fd, offset, os.SEEK_SET)
                chunk = os.read(self.fd, min(chunk_size, size - offset))
            if not chunk:
                break
            offset += len(chunk)
            yield chunk


class _OpenFileClient:
    def __init__(self, fd: int):
        self.fd = fd

    def stream(self, method: str, url: str, **kwargs) -> _OpenFileResponse:
        del url, kwargs
        if method != "GET":
            raise ManifestError("local artifact copy requires GET semantics")
        return _OpenFileResponse(self.fd)


def stage_verified_artifact(
    source: Path | VerifiedArtifact,
    artifact: Artifact,
    destination: Path,
) -> VerifiedArtifact:
    """Consume an opened artifact inode and publish it into an anchored directory."""

    expected = destination / artifact.filename
    if isinstance(source, VerifiedArtifact):
        source.verify()
        if (
            source.path.absolute() == expected.absolute()
            and source.binding_is_current()
        ):
            return source
        source_fd = source.duplicate_fd()
    else:
        try:
            source_fd = os.open(
                source,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise ManifestError("staged source artifact is unsafe") from exc
    try:
        _verify_open_artifact(source_fd, artifact)
        return download_verified_artifact(
            _OpenFileClient(source_fd),
            artifact,
            destination,
        )
    finally:
        os.close(source_fd)
