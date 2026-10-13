"""Task content hash: reproducibility/tamper-evidence for what the agent sees.

Hashes only instruction.md, task.toml, and environment/ — never solution/ or
tests/, since a volunteer's local deep-swe checkout is a full public clone
that contains those too. Shared verbatim by server and client so both sides
compute the exact same digest.
"""

import hashlib
from pathlib import Path


def _portable_content(path: Path) -> bytes:
    """Return bytes stable across Git's LF/CRLF text checkout modes.

    Git's ``core.autocrlf=true`` can write CRLF into an otherwise clean work
    tree.  Hashing those raw checkout bytes made an identical commit look
    tampered with on some Windows/WSL machines.  Git does not line-normalize
    binary files; mirror that safety boundary with its primary NUL-byte
    signal so binary integrity remains byte-for-byte strict.
    """
    data = path.read_bytes()
    if b"\0" not in data:
        return data.replace(b"\r\n", b"\n")
    return data


def task_content_hash(deep_swe_tasks_dir: Path, task_id: str) -> str:
    task_dir = Path(deep_swe_tasks_dir) / task_id
    digest = hashlib.sha256()
    for name in ("instruction.md", "task.toml"):
        path = task_dir / name
        if path.is_file():
            digest.update(name.encode())
            digest.update(_portable_content(path))
    env_dir = task_dir / "environment"
    if env_dir.is_dir():
        rel_paths = sorted(p.relative_to(env_dir).as_posix() for p in env_dir.rglob("*") if p.is_file())
        for rel in rel_paths:
            digest.update(rel.encode())
            digest.update(_portable_content(env_dir / rel))
    return digest.hexdigest()
