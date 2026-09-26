"""Checkpoint-free discovery and cleanup of ordinary local Pier job trees."""

from __future__ import annotations

import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path


KEEP_MARKER = ".dradar-keep"
TERMINAL_MARKER = ".dradar-terminal-evidence"
_ASSIGNMENT_RE = re.compile(r"^a([0-9a-f]{32})(?:-|$)")


class LocalEvidenceError(RuntimeError):
    """Local evidence could not be inspected safely before model admission."""


def _entry_mode(path: Path) -> int | None:
    try:
        return path.lstat().st_mode
    except FileNotFoundError:
        return None


def protected_assignment_ids(home: Path) -> set[str]:
    """Find historical results even when their pending row has been lost.

    This is a conservative start fence, never upload or cleanup authority.
    Do not read artifact bytes, infer credentials, or follow symbolic links.
    A suspicious/unreadable job must not become permission to spend again.
    """
    root = home / "work" / "jobs"
    protected: set[str] = set()
    try:
        mode = _entry_mode(root)
        if root.parent.is_symlink() or (mode is not None and stat.S_ISLNK(mode)):
            raise OSError("jobs root is a symbolic link")
        if mode is None:
            return protected
        for job in root.iterdir():
            assignment_id = assignment_id_for_job(job)
            if assignment_id is None:
                continue
            mode = _entry_mode(job)
            if mode is None:
                continue
            if not stat.S_ISDIR(mode):
                protected.add(assignment_id)
                continue
            if any(_entry_mode(job / name) is not None
                   for name in (KEEP_MARKER, TERMINAL_MARKER)):
                protected.add(assignment_id)
                continue
            for trial in job.iterdir():
                if trial.is_symlink():
                    protected.add(assignment_id)
                    break
                if not trial.is_dir():
                    continue
                candidates = (
                    trial / "artifacts" / "model.patch",
                    trial / ".dradar" / "host-output" / "model.patch",
                    trial / ".dradar" / "artifact-staging" / "manifest.json",
                    trial / ".dradar" / "artifact-staging" / "model.patch.source",
                )
                ancestors = (trial / "artifacts", trial / ".dradar",
                             trial / ".dradar" / "host-output",
                             trial / ".dradar" / "artifact-staging")
                if any(path.is_symlink() for path in ancestors):
                    protected.add(assignment_id)
                    break
                if any(_entry_mode(path) is not None for path in candidates):
                    protected.add(assignment_id)
                    break
    except OSError as exc:
        raise LocalEvidenceError(
            f"Cannot inspect preserved results in {root}; no new model work "
            "is safe. Keep this directory and repair access before resuming."
        ) from exc
    return protected


@dataclass(frozen=True)
class LocalJob:
    job_dir: Path
    assignment_id: str
    task_id: str | None
    trial_dir: Path | None
    size_bytes: int


def _root(home: Path) -> Path:
    return (home / "work" / "jobs").resolve()


def safe_job_dir(home: Path, path: Path) -> Path:
    root = _root(home)
    candidate = Path(path).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"job path escaped jobs directory: {candidate}")
    return candidate


def assignment_id_for_job(path: Path) -> str | None:
    match = _ASSIGNMENT_RE.match(Path(path).name)
    return match.group(1) if match else None


def _size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def scan(home: Path) -> list[LocalJob]:
    root = _root(home)
    if not root.is_dir():
        return []
    found: list[LocalJob] = []
    for lexical in root.iterdir():
        assignment_id = assignment_id_for_job(lexical)
        if assignment_id is None or lexical.is_symlink() or not lexical.is_dir():
            continue
        try:
            job = safe_job_dir(home, lexical)
        except ValueError:
            continue
        trials = [entry for entry in job.iterdir() if entry.is_dir() and not entry.is_symlink()]
        trial = trials[0] if len(trials) == 1 else None
        found.append(LocalJob(
            job_dir=job,
            assignment_id=assignment_id,
            task_id=trial.name if trial is not None else None,
            trial_dir=trial,
            size_bytes=_size(job),
        ))
    return found


def mark_kept(home: Path, job_dir: Path, *, terminal: bool = False) -> None:
    job = safe_job_dir(home, job_dir)
    (job / KEEP_MARKER).touch(mode=0o600, exist_ok=True)
    if terminal:
        (job / TERMINAL_MARKER).touch(mode=0o600, exist_ok=True)


def is_kept(home: Path, job_dir: Path) -> bool:
    return (safe_job_dir(home, job_dir) / KEEP_MARKER).is_file()


def cleanup_assignment(
    home: Path, assignment_id: str, *, keep_job_dir: Path | None = None,
) -> None:
    keep = safe_job_dir(home, keep_job_dir) if keep_job_dir else None
    for item in scan(home):
        if item.assignment_id == assignment_id and item.job_dir != keep:
            shutil.rmtree(item.job_dir, ignore_errors=True)


def remove(home: Path, item: LocalJob) -> None:
    shutil.rmtree(safe_job_dir(home, item.job_dir), ignore_errors=True)


__all__ = [
    "LocalJob", "assignment_id_for_job", "cleanup_assignment", "is_kept",
    "mark_kept", "remove", "safe_job_dir", "scan",
]
