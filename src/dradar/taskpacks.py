"""Checksum-pinned installation of non-repository benchmark task packs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from .api_client import ApiClient, ApiError

MARKER = ".dradar-task-bundle.json"
MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$",
    *(f"COM{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
    *(f"LPT{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
})
_WINDOWS_FORBIDDEN_FILENAME_CHARS = frozenset('<>:"|?*')


class TaskPackError(RuntimeError):
    pass


def _benchmark(catalog: dict, benchmark_id: str) -> dict:
    for item in catalog.get("benchmarks", []):
        if item.get("id") == benchmark_id:
            return item
    raise TaskPackError(f"server does not advertise benchmark {benchmark_id!r}")


def _installed(tasks_root: Path, benchmark_id: str, digest: str) -> bool:
    marker = tasks_root / MARKER
    if not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (payload.get("benchmark_id") == benchmark_id
            and payload.get("sha256") == digest)


def _managed_pack(tasks_root: Path, benchmark_id: str) -> bool:
    """Whether an existing directory is a checksum-pinned DRadar task pack."""
    if tasks_root.is_symlink():
        return False
    marker = tasks_root / MARKER
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    digest = payload.get("sha256")
    return (
        payload.get("benchmark_id") == benchmark_id
        and isinstance(digest, str)
        and len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unsafe_windows_component(component: str) -> bool:
    """Reject names Windows rewrites or routes outside a normal file."""

    if component.endswith((" ", ".")):
        return True
    if any(
        ord(character) < 32
        or ord(character) == 127
        or character in _WINDOWS_FORBIDDEN_FILENAME_CHARS
        for character in component
    ):
        return True
    # Windows reserves device basenames case-insensitively, even when an
    # extension is present or spaces precede it (for example ``NUL .txt``).
    device_basename = component.split(".", 1)[0].rstrip(" ").upper()
    return device_basename in _WINDOWS_RESERVED_DEVICE_NAMES


def _safe_target(root: Path, member_name: str) -> Path:
    pure = PurePosixPath(member_name)
    windows = PureWindowsPath(member_name)
    if (
        # Tar member names are POSIX paths. A backslash is ambiguous on
        # Windows and can turn one apparently safe member into a traversal.
        "\\" in member_name
        or windows.anchor
        or pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(_unsafe_windows_component(part) for part in pure.parts)
    ):
        raise TaskPackError(f"unsafe path in task bundle: {member_name!r}")
    return root.joinpath(*pure.parts)


def _extract_bundle(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, mode="r:gz") as tar:
        members = tar.getmembers()
        if len(members) > MAX_ENTRIES:
            raise TaskPackError("task bundle contains too many entries")
        total = sum(member.size for member in members if member.isfile())
        if total > MAX_BUNDLE_BYTES:
            raise TaskPackError("expanded task bundle exceeds the 2 GiB safety limit")
        for member in members:
            target = _safe_target(destination, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise TaskPackError(
                    f"task bundle contains a non-file entry: {member.name!r}")
            source = tar.extractfile(member)
            if source is None:
                raise TaskPackError(f"cannot read task bundle entry: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            os.chmod(target, member.mode & 0o755 or 0o600)


def ensure_benchmark_task_pack(
    client: ApiClient,
    benchmark_id: str,
    tasks_root: Path,
    *,
    catalog: dict | None = None,
) -> bool:
    """Install the advertised bundle atomically; return True when downloaded."""
    metadata = _benchmark(catalog or client.benchmarks(), benchmark_id)
    bundle = metadata.get("task_bundle")
    if not isinstance(bundle, dict):
        if tasks_root.is_dir():
            return False
        raise TaskPackError(
            f"benchmark {benchmark_id!r} has no downloadable task bundle; "
            "pass --tasks-root with a verified local task directory")
    digest = str(bundle.get("sha256") or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise TaskPackError("server advertised an invalid task-bundle SHA-256")
    if _installed(tasks_root, benchmark_id, digest):
        return False
    replace_managed_pack = False
    if tasks_root.exists():
        if tasks_root.is_dir() and not any(tasks_root.iterdir()):
            tasks_root.rmdir()
        elif _managed_pack(tasks_root, benchmark_id):
            # A checksum change is a normal signed bundle upgrade. Only a
            # directory carrying our valid marker may be replaced; arbitrary
            # user directories remain strictly protected.
            replace_managed_pack = True
        else:
            raise TaskPackError(
                f"task directory already exists but does not match the advertised bundle: "
                f"{tasks_root}; choose a new --tasks-root or restore its marker")

    parent = tasks_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{benchmark_id}-install-", dir=parent))
    archive = work.with_suffix(".tar.gz")
    staged = work / "tasks"
    try:
        print(f"downloading {metadata.get('title') or benchmark_id} task pack "
              f"({int(bundle.get('bytes') or 0) / 1024 / 1024:.1f} MiB)...")
        observed_header = client.download(bundle["url"], archive)
        observed = _sha256(archive)
        if observed != digest or (observed_header and observed_header != digest):
            raise TaskPackError(
                f"task-bundle checksum mismatch: expected {digest}, got {observed}")
        staged.mkdir()
        _extract_bundle(archive, staged)
        (staged / MARKER).write_text(json.dumps({
            "benchmark_id": benchmark_id,
            "sha256": digest,
        }, indent=2) + "\n")
        if replace_managed_pack:
            backup = Path(tempfile.mkdtemp(
                prefix=f".{tasks_root.name}-previous-", dir=parent,
            ))
            backup.rmdir()
            os.replace(tasks_root, backup)
            try:
                os.replace(staged, tasks_root)
            except BaseException:
                os.replace(backup, tasks_root)
                raise
            shutil.rmtree(backup)
        else:
            os.replace(staged, tasks_root)
    except (ApiError, OSError, tarfile.TarError, KeyError) as exc:
        raise TaskPackError(f"could not install benchmark task pack: {exc}") from exc
    finally:
        if archive.exists():
            archive.unlink()
        shutil.rmtree(work, ignore_errors=True)
    print(f"  verified and installed at {tasks_root}")
    return True
