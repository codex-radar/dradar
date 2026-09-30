"""Host-user cache for the public, checksum-pinned Linux Grok executable.

Only the public CLI binary lives here. OAuth files and trial data stay in their
existing private locations. A lock serializes downloads across Fleet homes.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import platform
import re
import shutil
import socket
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from .providers import GROK_CLI_VERSION, provider_subprocess_env

GROK_ARTIFACT_ENV = "DRADAR_GROK_PUBLIC_ARTIFACT"
GROK_ARTIFACT_CACHE_MODE_ENV = "DRADAR_GROK_ARTIFACT_CACHE"
GROK_ARTIFACT_SHA_ENV = "DRADAR_GROK_PUBLIC_ARTIFACT_SHA256"
GROK_LINUX_SHA256 = {
    "x86_64": "92c997dfd109c0672d40d5ae6fbd15835d53ffaf12cf9ea124d22aaef3ff23fc",
    "aarch64": "a16d26cf06892ebb3eca9a702c65e031a053431ed4dde3b23bebc58a92b6117f",
}
GROK_ARTIFACT_URL = "https://storage.googleapis.com/grok-build-public-artifacts/cli"
_DOWNLOAD_BUDGET_SECONDS = 1200
_LOCK_BUDGET_SECONDS = 1500


class GrokArtifactError(RuntimeError):
    pass


def linux_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "aarch64"
    raise GrokArtifactError("unsupported Linux architecture for Grok artifact")


def _valid(path: Path, digest: str) -> bool:
    try:
        if not path.is_file() or path.is_symlink():
            return False
        h = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest() == digest
    except OSError:
        return False


@contextmanager
def _artifact_lock(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + _LOCK_BUDGET_SECONDS
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if not (
                    isinstance(exc, BlockingIOError)
                    or (os.name == "nt" and getattr(exc, "winerror", None) in {33, 36})
                ):
                    raise GrokArtifactError("Grok artifact lock unavailable") from None
                if time.monotonic() >= deadline:
                    raise GrokArtifactError("timed out waiting for Grok artifact download")
                time.sleep(0.25)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _download(url: str, target: Path, deadline: float) -> None:
    """Retry a partial public download only when the server confirms Range."""
    env = provider_subprocess_env()
    proxy = env.get("HTTPS_PROXY") or env.get("https_proxy")
    opener = (
        urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        ) if proxy else urllib.request.build_opener()
    )
    for attempt in range(3):
        if time.monotonic() >= deadline:
            break
        offset = target.stat().st_size if target.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            request = urllib.request.Request(url, headers=headers)
            with opener.open(request, timeout=120) as response:
                status = response.status
                if offset:
                    content_range = response.headers.get("Content-Range", "")
                    if status != 206 or not content_range.startswith(f"bytes {offset}-"):
                        target.unlink(missing_ok=True)
                        raise GrokArtifactError("Grok artifact server rejected safe resume")
                elif status != 200:
                    raise GrokArtifactError("Grok artifact server returned unexpected status")
                with target.open("ab" if offset else "wb") as output:
                    while True:
                        if time.monotonic() >= deadline:
                            raise GrokArtifactError("Grok artifact download budget exhausted")
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            return
                        output.write(chunk)
        except (
            urllib.error.URLError, TimeoutError, socket.timeout,
            http.client.IncompleteRead,
        ) as exc:
            if attempt == 2 or time.monotonic() >= deadline:
                raise GrokArtifactError("Grok artifact download failed after bounded retries") from None
            time.sleep(2)
    raise GrokArtifactError("Grok artifact download budget exhausted")


def ensure_grok_artifact(
    *, cache_root: Path | None = None, seed_path: Path | None = None,
    version: str = GROK_CLI_VERSION, arch: str | None = None,
    digests: dict[str, str] | None = None,
) -> tuple[Path, str]:
    """Return a verified immutable cache entry, filling it once per host user."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise GrokArtifactError("invalid Grok artifact version")
    arch = arch or linux_arch()
    sha = (digests or GROK_LINUX_SHA256).get(arch)
    if sha is None or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise GrokArtifactError("unsupported or unpinned Grok artifact")
    root = cache_root or Path.home() / ".dradar" / "public-artifacts" / "grok"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_id = hashlib.sha256(GROK_ARTIFACT_URL.encode("ascii")).hexdigest()[:12]
    name = f"grok-{version}-linux-{arch}-{sha}-{source_id}"
    target = root / name
    with _artifact_lock(root / (name + ".lock")):
        if _valid(target, sha):
            return target, sha
        target.unlink(missing_ok=True)
        # A killed process may leave a partial file; it is never a cache hit.
        # The next lock owner may resume it only after an exact Range response.
        temporary = root / (name + ".part")
        try:
            if seed_path is not None and _valid(seed_path, sha):
                shutil.copyfile(seed_path, temporary)
            else:
                url = f"{GROK_ARTIFACT_URL}/grok-{version}-linux-{arch}"
                deadline = time.monotonic() + _DOWNLOAD_BUDGET_SECONDS
                for checksum_attempt in range(2):
                    _download(url, temporary, deadline)
                    if _valid(temporary, sha):
                        break
                    temporary.unlink(missing_ok=True)
                    if checksum_attempt == 1:
                        raise GrokArtifactError("Grok artifact checksum mismatch")
            if not _valid(temporary, sha):
                raise GrokArtifactError("Grok artifact checksum mismatch")
            temporary.chmod(0o600)
            os.replace(temporary, target)
            return target, sha
        finally:
            temporary.unlink(missing_ok=True)
