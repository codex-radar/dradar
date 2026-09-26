"""Persistent assignment-set integrity for one benchmark campaign.

The web-selected assignment IDs are the campaign boundary.  A runner may
submit an assignment or keep holding it for a later resume, but an unresolved
ID must never disappear silently between retries.  The state contains public
assignment metadata and bounded outcome tags only; it never stores nonces,
credentials, prompts, patches, or trajectories.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1
STATE_DIR = "assignment-boundaries"
SETTLED_OUTCOMES = frozenset({"submitted", "interrupted"})
_PROCESS_LOCK = threading.Lock()


class BoundaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoundaryReport:
    expected_ids: frozenset[str]
    settled_ids: frozenset[str]
    active_ids: frozenset[str]
    missing_ids: frozenset[str]
    unexpected_ids: frozenset[str]
    outcomes: dict[str, str]

    @property
    def complete(self) -> bool:
        return bool(self.expected_ids) and self.expected_ids <= self.settled_ids


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_assignment_id(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for ch in value):
        return None
    return value


def state_path(
    home: Path, benchmark_id: str, batch_id: str | None = None,
) -> Path:
    if not isinstance(benchmark_id, str) or not benchmark_id:
        raise BoundaryError("assignment boundary requires a benchmark ID")
    if batch_id is not None and (
        not isinstance(batch_id, str) or not batch_id
    ):
        raise BoundaryError("assignment boundary batch ID must be non-empty")
    scope = benchmark_id if batch_id is None else f"{benchmark_id}\0batch:{batch_id}"
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:20]
    return home / STATE_DIR / f"{digest}.json"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        path.with_name(f"{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600,
    )
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
    os.lseek(fd, 0, os.SEEK_SET)
    windows_lock = False
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows CI
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            windows_lock = True
        yield
    finally:
        if windows_lock:  # pragma: no cover - Windows CI
            try:
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        os.close(fd)


def _load(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return None
    expected = value.get("expected")
    outcomes = value.get("outcomes")
    if not isinstance(expected, dict) or not isinstance(outcomes, dict):
        return None
    if value.get("strict") not in (None, True, False):
        return None
    if any(
        _safe_assignment_id(key) is None or not isinstance(metadata, dict)
        for key, metadata in expected.items()
    ):
        return None
    if any(
        _safe_assignment_id(key) is None
        or not isinstance(outcome, dict)
        or not isinstance(outcome.get("outcome"), str)
        for key, outcome in outcomes.items()
    ):
        return None
    return value


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now()
    fd, raw_tmp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
    )
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _entry(assignment: dict) -> tuple[str, dict] | None:
    assignment_id = _safe_assignment_id(assignment.get("assignment_id"))
    if assignment_id is None:
        return None
    metadata = {}
    for name in ("task_id", "model", "effort", "batch_id"):
        value = assignment.get(name)
        if isinstance(value, str) and 0 < len(value) <= 256:
            metadata[name] = value
    return assignment_id, metadata


def _active_entries(assignments: list[dict]) -> dict[str, dict]:
    entries = {}
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        item = _entry(assignment)
        if item is not None:
            entries[item[0]] = item[1]
    return entries


def _report(state: dict, active_ids: set[str]) -> BoundaryReport:
    expected_ids = frozenset(
        assignment_id for assignment_id in state.get("expected", {})
        if _safe_assignment_id(assignment_id) is not None
    )
    outcomes = {
        assignment_id: value.get("outcome")
        for assignment_id, value in state.get("outcomes", {}).items()
        if (
            _safe_assignment_id(assignment_id) is not None
            and isinstance(value, dict)
            and isinstance(value.get("outcome"), str)
        )
    }
    settled_ids = frozenset(
        assignment_id for assignment_id, outcome in outcomes.items()
        if outcome in SETTLED_OUTCOMES
    )
    active = frozenset(active_ids)
    return BoundaryReport(
        expected_ids=expected_ids,
        settled_ids=settled_ids,
        active_ids=active,
        missing_ids=frozenset(expected_ids - settled_ids - active),
        unexpected_ids=frozenset(active - expected_ids),
        outcomes=outcomes,
    )


def admitted_batches(path: Path) -> list[str]:
    """Recover the exact batch identities of an unfinished shared boundary.

    Older ledgers without batch metadata keep their existing conservative
    reconciliation; this does not guess identities or forget missing work.
    """
    if not path.exists():
        return []
    with _PROCESS_LOCK:
        with _locked(path):
            state = _load(path)
            if state is None:
                raise BoundaryError("assignment boundary state is missing or invalid")
            values = [entry.get("batch_id") for entry in state["expected"].values()]
            if not values or not all(isinstance(value, str) and value for value in values):
                return []
            return list(dict.fromkeys(values))


def snapshot(path: Path) -> tuple[dict, str]:
    """Read an unfinished boundary and a digest for a later checked archive."""
    with _PROCESS_LOCK:
        with _locked(path):
            state = _load(path)
            if state is None:
                raise BoundaryError("assignment boundary state is missing or invalid")
            raw = path.read_bytes()
            return state, hashlib.sha256(raw).hexdigest()


def confirm_server_submissions(
    path: Path, digest: str, active_ids: set[str], submitted_ids: set[str],
) -> None:
    """Record an exact, fully verified missing set without losing a concurrent edit."""
    with _PROCESS_LOCK:
        with _locked(path):
            state = _load(path)
            if state is None or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise BoundaryError("saved assignment boundary changed during verification")
            report = _report(state, active_ids)
            if report.unexpected_ids or report.missing_ids != submitted_ids or not submitted_ids:
                raise BoundaryError("assignment inventory changed during verification")
            for assignment_id in submitted_ids:
                state["outcomes"][assignment_id] = {
                    "outcome": "submitted",
                    "source": "server-confirmed",
                    "updated_at": _now(),
                }
            _save(path, state)


def archive_if_unchanged(path: Path, digest: str) -> Path:
    """Retire a verified boundary without destroying its diagnostic record."""
    with _PROCESS_LOCK:
        with _locked(path):
            state = _load(path)
            if state is None or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise BoundaryError("saved assignment boundary changed during recovery")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            archived = path.with_name(f"{path.stem}.recovered-{stamp}.json")
            os.replace(path, archived)
            return archived


def prepare(
    home: Path,
    benchmark_id: str,
    active_assignments: list[dict],
    *,
    batch_id: str | None = None,
    expected_ids: list[str] | None = None,
    forget_existing: bool = False,
    require_matching_metadata: bool = False,
) -> Path | None:
    """Open or create a campaign boundary and reject any unresolved loss."""
    path = state_path(home, benchmark_id, batch_id)
    active = _active_entries(active_assignments)
    explicit = None
    if expected_ids is not None:
        explicit = []
        for value in expected_ids:
            assignment_id = _safe_assignment_id(value)
            if assignment_id is None:
                raise BoundaryError(f"invalid expected assignment ID: {value!r}")
            if assignment_id not in explicit:
                explicit.append(assignment_id)
    with _PROCESS_LOCK:
        with _locked(path):
            if forget_existing:
                path.unlink(missing_ok=True)
            state = _load(path)
            if state is None and path.exists():
                raise BoundaryError(
                    "saved assignment boundary is unreadable or has an unsupported schema"
                )
            if state is not None and state.get("benchmark_id") != benchmark_id:
                raise BoundaryError("saved assignment boundary has a benchmark mismatch")
            if state is not None and state.get("batch_id") != batch_id:
                raise BoundaryError("saved assignment boundary has a batch mismatch")
            if state is not None:
                if require_matching_metadata:
                    for assignment_id in state["expected"].keys() & active.keys():
                        saved = state["expected"][assignment_id]
                        current = active[assignment_id]
                        if any(
                            saved.get(field) != current.get(field)
                            or not saved.get(field)
                            for field in ("batch_id", "task_id", "model", "effort")
                        ):
                            raise BoundaryError(
                                "saved assignment identity differs from the current "
                                f"lease: {assignment_id}"
                            )
                report = _report(state, set(active))
                if report.complete:
                    path.unlink(missing_ok=True)
                    state = None
                else:
                    if explicit is not None and set(explicit) != set(report.expected_ids):
                        raise BoundaryError(
                            "expected assignment IDs differ from the unfinished saved boundary"
                        )
                    if report.missing_ids:
                        missing = ", ".join(sorted(report.missing_ids))
                        raise BoundaryError(
                            "unfinished assignment(s) disappeared from active leases: "
                            f"{missing}"
                        )
                    if report.unexpected_ids:
                        unexpected = ", ".join(sorted(report.unexpected_ids))
                        raise BoundaryError(
                            "active assignment(s) are outside the unfinished "
                            f"boundary: {unexpected}"
                        )
                    return path
            selected = explicit if explicit is not None else list(active)
            if not selected:
                return None
            missing = sorted(set(selected) - set(active))
            if missing:
                raise BoundaryError(
                    "expected assignment(s) are not active: " + ", ".join(missing)
                )
            unexpected = sorted(set(active) - set(selected))
            if explicit is not None and unexpected:
                raise BoundaryError(
                    "active assignment(s) are outside the explicit boundary: "
                    + ", ".join(unexpected)
                )
            now = _now()
            state = {
                "schema_version": SCHEMA_VERSION,
                "benchmark_id": benchmark_id,
                "batch_id": batch_id,
                "created_at": now,
                "updated_at": now,
                "strict": explicit is not None,
                "expected": {assignment_id: active[assignment_id] for assignment_id in selected},
                "outcomes": {},
            }
            _save(path, state)
            return path


def add_expected(
    path: Path | None, assignments: list[dict], *,
    require_matching_metadata: bool = False,
) -> None:
    if path is None:
        return
    entries = _active_entries(assignments)
    if not entries:
        return
    with _PROCESS_LOCK:
        with _locked(path):
            state = _load(path)
            if state is None:
                raise BoundaryError("assignment boundary state is missing or invalid")
            unexpected = sorted(set(entries) - set(state["expected"]))
            if state.get("strict") is True and unexpected:
                raise BoundaryError(
                    "assignment(s) are outside the explicit boundary: "
                    + ", ".join(unexpected)
                )
            if require_matching_metadata:
                for assignment_id in state["expected"].keys() & entries.keys():
                    saved = state["expected"][assignment_id]
                    current = entries[assignment_id]
                    if any(
                        saved.get(field) != current.get(field)
                        or not saved.get(field)
                        for field in ("batch_id", "task_id", "model", "effort")
                    ):
                        raise BoundaryError(
                            "saved assignment identity differs from the current "
                            f"lease: {assignment_id}"
                        )
            state["expected"].update(entries)
            _save(path, state)


def record_outcome(path: Path | None, assignment: dict, outcome: str) -> None:
    if path is None:
        return
    item = _entry(assignment)
    if item is None or not isinstance(outcome, str) or not 1 <= len(outcome) <= 64:
        raise BoundaryError("cannot record an invalid assignment outcome")
    assignment_id, metadata = item
    with _PROCESS_LOCK:
        with _locked(path):
            state = _load(path)
            if state is None:
                raise BoundaryError("assignment boundary state is missing or invalid")
            if assignment_id not in state["expected"]:
                raise BoundaryError(
                    f"assignment {assignment_id} is outside the saved boundary"
                )
            state["outcomes"][assignment_id] = {
                "outcome": outcome,
                "updated_at": _now(),
            }
            _save(path, state)


def reconcile(path: Path | None, active_assignments: list[dict]) -> BoundaryReport | None:
    if path is None:
        return None
    active = set(_active_entries(active_assignments))
    with _PROCESS_LOCK:
        with _locked(path):
            state = _load(path)
            if state is None:
                raise BoundaryError("assignment boundary state is missing or invalid")
            return _report(state, active)


def finish_if_complete(path: Path | None, report: BoundaryReport | None) -> None:
    if path is None or report is None or not report.complete:
        return
    with _PROCESS_LOCK:
        with _locked(path):
            current = _load(path)
            if current is not None and _report(current, set()).complete:
                path.unlink(missing_ok=True)
