"""Local pending-upload ledger: a trial that finished but failed to upload
must not just print a path and be forgotten. The volunteer's most expensive
artifact — a real trial that already burned real quota — gets a safety net.

On an upload failure (network drop, timeout, server 5xx), _run_and_submit
records everything needed to retry WITHOUT re-running the trial: the raw
trial_dir (patch/trajectory/result live under it, untouched by scrubbing —
scrubbing writes to a fresh tempdir and never mutates the originals) plus the
already-built client_meta and outcome. `dradar retry-upload` (and an
automatic scan at the top of `dradar go`) replays the upload later.

Confirmed submission removes the entry. Expiry and terminal upload rejection
retain a blocked result for review, so preserving the files also preserves the
model-start fence. A recovery-generation conflict is not success. Unreadable
or malformed ledgers stop admission and are never overwritten as empty.

The same ledger also holds ``cleanup_unconfirmed`` safety fences. Those rows
may have no result or trial directory. They block duplicate model starts but
must never be counted or replayed as completed uploads.
"""

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_FILENAME = "pending_uploads.json"
_LOCK_FILENAME = "pending_uploads.lock"
_PROCESS_LOCK = threading.Lock()


class PendingLedgerError(RuntimeError):
    """The safety ledger cannot be read; absence of evidence is not empty."""


def _ledger_error(path: Path) -> PendingLedgerError:
    return PendingLedgerError(
        f"Cannot verify saved results in {path}. The file was kept unchanged; "
        "no new model work is safe. Inspect or restore the ledger before "
        "resuming. Official stop remains available."
    )


def scope_fingerprint(
    *,
    server: str | None,
    account_scope: str | None,
    benchmark_id: str | None = None,
    batch_id: str | None = None,
) -> str | None:
    """Return a non-secret identity for a pending-upload queue.

    The bearer token is already reduced to ``account_scope`` by ``ApiClient``;
    neither it nor any other credential is persisted here.  Including the
    server, account and plan/batch makes a ledger entry from one login or
    private run plan ineligible for an unrelated context sharing the same
    ``DRADAR_HOME``.  ``None`` means the caller cannot prove a safe scope and
    must keep the entry for explicit review rather than guessing.
    """
    if not server or not account_scope:
        return None
    payload = {
        "schema": "dradar-pending-scope-v1",
        "server": str(server).rstrip("/"),
        "account_scope": str(account_scope),
        "benchmark_id": str(benchmark_id or ""),
        "batch_id": str(batch_id or ""),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _path(home: Path) -> Path:
    return home / _FILENAME


@contextmanager
def _locked(home: Path) -> Iterator[None]:
    """Serialize the ledger's read-modify-write cycle across all workers."""
    home.mkdir(parents=True, exist_ok=True)
    with _PROCESS_LOCK:
        fd = os.open(home / _LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        windows_lock = False
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - Windows CI exercises callers
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                windows_lock = True
            yield
        finally:
            if windows_lock:  # pragma: no cover
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


def _load_unlocked(home: Path) -> list[dict]:
    path = _path(home)
    try:
        if path.is_symlink():
            raise _ledger_error(path)
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, UnicodeError, OSError) as exc:
        raise _ledger_error(path) from exc
    if not isinstance(data, list) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("assignment_id"), str)
        or not row["assignment_id"]
        or (row.get("ledger_version") is not None and (
            type(row["ledger_version"]) is not int
            or row["ledger_version"] not in (1, 2, 3)
        ))
        or row.get("record_kind") not in (None, "cleanup_quarantine")
        for row in data
    ):
        raise _ledger_error(path)
    return data


def load(home: Path) -> list[dict]:
    # _save_unlocked commits with os.replace, so an unlocked reader still
    # sees either the complete old file or the complete new one. Keeping this
    # read-only also lets status/inspection work when DRADAR_HOME is mounted
    # read-only and avoids creating a lock file merely to report no entries.
    return _load_unlocked(home)


def is_cleanup_quarantine(entry: object) -> bool:
    """Identify a process-cleanup fence, including rows saved by older CLIs.

    This is a lifecycle state, not an upload result.  Artifact paths or an
    ``outcome`` field cannot turn an unconfirmed writer into completed work.
    """
    return isinstance(entry, dict) and (
        entry.get("record_kind") == "cleanup_quarantine"
        or entry.get("upload_blocked") == "cleanup_unconfirmed"
    )


def require_uploadable(entry: dict) -> None:
    """Check replay shape without rewriting an incomplete safety record."""
    if is_cleanup_quarantine(entry) or entry.get("upload_blocked"):
        return
    if any(not isinstance(entry.get(key), str) or not entry[key]
           for key in ("assignment_id", "nonce", "task_id", "trial_dir")) or (
        entry.get("meta") is not None and not isinstance(entry["meta"], dict)
    ):
        raise PendingLedgerError(
            "Saved result metadata is incomplete; the pending record was kept "
            "unchanged. Verify the original assignment and artifact metadata "
            "before upload recovery. No model work is safe for this record."
        )


def assignment_ids(home: Path) -> set[str]:
    """Assignments with saved results or cleanup fences that must not rerun.

    A quarantine row does not prove there is a result, but it does mean a
    writer may still exist.  Keep it in the duplicate-start fence.
    """
    return {
        str(entry["assignment_id"])
        for entry in load(home)
        if isinstance(entry, dict) and entry.get("assignment_id")
    }


def _save_unlocked(home: Path, entries: list[dict]) -> None:
    # A safety-net ledger that isn't itself crash-safe defeats the point: a
    # plain write_text() truncates the file before writing, so a kill/OOM/
    # power-loss mid-write leaves truncated JSON and load() would then drop
    # EVERY pending entry, not just the one being saved. Write-to-temp +
    # atomic rename means the file on disk is always either the old or the
    # new complete version, never a partial one.
    home.mkdir(parents=True, exist_ok=True)
    path = _path(home)
    fd, raw_tmp = tempfile.mkstemp(
        dir=home, prefix=f".{_FILENAME}.", suffix=".tmp",
    )
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(entries, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(home, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def record(home: Path, entry: dict) -> None:
    """Add or replace a pending-upload entry.

    Assignment IDs are normally globally unique, but old ledgers have no
    scope metadata.  A scoped entry must not replace an old/foreign entry with
    the same ID: preserving that evidence is safer than silently discarding
    it during a login or plan switch.
    """
    with _locked(home):
        entries = [
            e for e in _load_unlocked(home)
            if (
                not isinstance(e, dict)
                or e.get("assignment_id") != entry.get("assignment_id")
                or (
                    e.get("scope_fingerprint") != entry.get("scope_fingerprint")
                    and (
                        e.get("scope_fingerprint") is not None
                        or entry.get("scope_fingerprint") is not None
                    )
                )
            )
        ]
        entries.append(entry)
        _save_unlocked(home, entries)


def remove(
    home: Path,
    assignment_id: str,
    *,
    scope_fingerprint: str | None = None,
) -> None:
    """Remove one assignment, optionally limited to its queue scope.

    An omitted scope removes only legacy (unscoped) rows.  Scoped callers must
    identify their exact row so settling one context cannot delete preserved
    evidence from another context sharing the same assignment ID.
    """
    with _locked(home):
        entries = [
            e for e in _load_unlocked(home)
            if not (
                isinstance(e, dict)
                and e.get("assignment_id") == assignment_id
                and e.get("scope_fingerprint") == scope_fingerprint
            )
        ]
        _save_unlocked(home, entries)
