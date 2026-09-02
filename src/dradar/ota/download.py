"""Bounded, resumeless OTA downloads that never publish partial artifacts."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Protocol

from .manifest import Artifact, ManifestError, verify_artifact


class StreamingResponse(Protocol):
    def __enter__(self): ...
    def __exit__(self, exc_type, exc, traceback): ...
    def raise_for_status(self) -> None: ...
    def iter_bytes(self, chunk_size: int = 65536): ...


class StreamingClient(Protocol):
    def stream(self, method: str, url: str, **kwargs) -> StreamingResponse: ...


def download_verified_artifact(
    client: StreamingClient,
    artifact: Artifact,
    destination: Path,
) -> Path:
    """Download once, verify, fsync, then atomically publish the exact artifact."""

    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = destination / artifact.filename
    temporary = destination / f".{artifact.filename}.{uuid.uuid4().hex}.partial"
    fd: int | None = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        written = 0
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
            handle.flush()
            os.fsync(handle.fileno())
        verify_artifact(temporary, artifact)
        os.replace(temporary, final)
        return final
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if fd is not None:
            os.close(fd)
