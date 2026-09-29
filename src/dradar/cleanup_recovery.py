"""Explicit recovery of one quarantined, physically exited assignment.

The original result remains unknown until this command checks the exact job.
It never starts a model, uploads an artifact, or infers ``never_started``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path

from . import assignment_boundary, capacity_journal, local_jobs, pending, runtime_identity
from .api_client import ApiError, normalize_batch_id
from .identity import _client
from .local_config import DEFAULT_BENCHMARK, HOME, _load_config
from .runloop import _pending_entry_matches_scope


class CleanupRecoveryBlocked(RuntimeError):
    pass


_RESULT_NAMES = frozenset({
    "model.patch", "model.patch.source", "result.json", "trajectory.json",
    "trajectory_bundle.json", "patch.diff", "submission.json",
})
_OUTPUT_DIRS = frozenset({"artifacts", "host-output", "artifact-staging"})


def _job_inventory(home: Path, row: dict, assignment_id: str) -> str:
    raw = row.get("job_dir")
    root = home / "work" / "jobs"
    if (not isinstance(raw, str) or not Path(raw).is_absolute()
            or root.is_symlink() or root.parent.is_symlink()):
        raise CleanupRecoveryBlocked("original job binding is unavailable")
    job = Path(raw)
    if (job.parent != root or job.is_symlink() or not job.is_dir()
            or job.resolve() != job
            or local_jobs.assignment_id_for_job(job) != assignment_id):
        raise CleanupRecoveryBlocked("original job identity changed")
    entries: list[tuple[str, int, int, int, str | None]] = []
    def walk_error(exc: OSError) -> None:
        raise CleanupRecoveryBlocked("original job inventory is unreadable") from exc

    for current, dirs, files in os.walk(job, followlinks=False, onerror=walk_error):
        base = Path(current)
        for name in dirs + files:
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise CleanupRecoveryBlocked("original job contains an unsafe link")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise CleanupRecoveryBlocked("original job contains an unknown file type")
            rel = path.relative_to(job)
            # Pier writes a job-level summary even when no trial output was
            # produced. Only that exact, bounded shape is diagnostic; a
            # trial-level result.json remains a recoverable-result blocker.
            job_summary = rel == Path("result.json") and stat.S_ISREG(info.st_mode)
            summary_sha256 = None
            if job_summary:
                if info.st_size > 2 * 1024 * 1024:
                    raise CleanupRecoveryBlocked("job summary exceeds review limit")
                try:
                    summary_raw = path.read_bytes()
                    summary = json.loads(summary_raw.decode("utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise CleanupRecoveryBlocked("job summary is unreadable") from exc
                if (not isinstance(summary, dict)
                        or not {"id", "started_at", "updated_at", "finished_at",
                                "n_total_trials", "stats"} <= summary.keys()
                        or set(summary) - {"id", "started_at", "updated_at",
                                           "finished_at", "n_total_trials", "stats"}):
                    raise CleanupRecoveryBlocked("job summary has an unknown result shape")
                summary_sha256 = hashlib.sha256(summary_raw).hexdigest()
            if ((name in _RESULT_NAMES and not job_summary)
                    or (stat.S_ISREG(info.st_mode)
                        and any(part in _OUTPUT_DIRS for part in rel.parts[:-1]))):
                raise CleanupRecoveryBlocked(
                    "possible local result remains; keep the quarantine for result review"
                )
            entries.append((str(rel), info.st_mode, info.st_size, info.st_mtime_ns,
                            summary_sha256))
            if len(entries) > 20000:
                raise CleanupRecoveryBlocked("original job inventory exceeds review limit")
    return hashlib.sha256(capacity_journal._canonical(sorted(entries))).hexdigest()


def _exact_row(home: Path, assignment_id: str) -> dict:
    rows = [row for row in pending.load(home)
            if row.get("assignment_id") == assignment_id]
    if len(rows) != 1 or not pending.is_cleanup_quarantine(rows[0]):
        raise CleanupRecoveryBlocked("exactly one original cleanup quarantine is required")
    return rows[0]


def _require_batch_capacity_settled(client, batch_id: str) -> None:
    """Keep OTA blocked while another original session still reserves capacity."""
    after = ""
    seen = set()
    for _ in range(20):
        page = client.runner_reservations(limit=200, after=after, batch_id=batch_id)
        if (not isinstance(page, dict) or page.get("schema_version") != 1
                or page.get("batch_id") != batch_id
                or not isinstance(page.get("reservations"), list)):
            raise CleanupRecoveryBlocked("server reservation inventory is incomplete")
        for item in page["reservations"]:
            if not isinstance(item, dict):
                raise CleanupRecoveryBlocked("server reservation inventory has an unknown row")
            if item.get("batch_id") != batch_id:
                raise CleanupRecoveryBlocked("server reservation inventory escaped batch scope")
            raise CleanupRecoveryBlocked(
                "another original session still has unknown capacity; keep OTA blocked"
            )
        next_after = page.get("next_after")
        if next_after is None:
            return
        if (not isinstance(next_after, str) or not next_after
                or next_after == after or next_after in seen):
            raise CleanupRecoveryBlocked("server reservation cursor is invalid")
        seen.add(next_after)
        after = next_after
    raise CleanupRecoveryBlocked("server reservation inventory exceeds review limit")


def inspect(
    *, assignment_id: str, benchmark: str, batch_id: str,
    session_id: str, home: Path = HOME,
) -> dict:
    """Read-only preflight with a digest that execution must recheck."""
    if (not re.fullmatch(r"[0-9a-f]{32}", assignment_id)
            or not re.fullmatch(r"[0-9a-f]{32}", session_id)
            or normalize_batch_id(batch_id) != batch_id or not benchmark):
        raise CleanupRecoveryBlocked("exact assignment, batch, session and benchmark are required")
    row = _exact_row(home, assignment_id)
    if (row.get("batch_id") != batch_id
            or row.get("runner_session_id") != session_id
            or type(row.get("owner_epoch")) is not int
            or type(row.get("resume_generation")) is not int
            or row.get("outcome") is not None or row.get("trial_dir") is not None
            or row.get("upload_intent") is not None
            or row.get("upload_receipt_status") is not None):
        raise CleanupRecoveryBlocked("saved quarantine has another result or owner shape")
    cfg = {**_load_config(), "benchmark": benchmark}
    client = _client(cfg)
    client.set_batch_id(batch_id)
    if not _pending_entry_matches_scope(client, row, batch_id=batch_id):
        raise CleanupRecoveryBlocked("original account, server or batch scope differs")
    path = assignment_boundary.state_path(home, benchmark, batch_id)
    state, boundary_sha256 = assignment_boundary.inspect_snapshot(path)
    expected = state.get("expected", {}).get(assignment_id)
    if (state.get("benchmark_id") != benchmark or state.get("batch_id") != batch_id
            or not isinstance(expected, dict)
            or any(not expected.get(key) for key in ("task_id", "model", "effort"))
            or expected.get("batch_id") != batch_id
            or row.get("task_id") != expected["task_id"]):
        raise CleanupRecoveryBlocked("original assignment boundary differs")
    prior = state["outcomes"].get(assignment_id)
    saved_request = row.get("cleanup_recovery")
    quarantined_prior = (isinstance(prior, dict)
                         and set(prior) == {"outcome", "updated_at"}
                         and prior.get("outcome") == "cleanup-unconfirmed")
    confirmed_prior = (isinstance(prior, dict)
                       and prior.get("outcome") == "terminated_unsubmitted"
                       and isinstance(saved_request, dict)
                       and prior.get("request_id") == saved_request.get("request_id"))
    if prior is not None and not (quarantined_prior or confirmed_prior):
        raise CleanupRecoveryBlocked("assignment has another saved outcome")
    journal_path = home / "runner-reservations" / f"{session_id}.json"
    journal = capacity_journal._read(journal_path)
    if (journal.get("session_id") != session_id
            or journal.get("server") != str(client.server).rstrip("/")
            or journal.get("batch_id") != batch_id
            or journal.get("state") != "sealed"
            or journal.get("released") is not True
            or not isinstance(journal.get("release_request"), dict)):
        raise CleanupRecoveryBlocked("original physical exit journal is not released")
    here = runtime_identity.process_identity(os.getpid())
    owner = journal.get("owner_identity")
    if (sys.platform != "linux" or not isinstance(here, dict)
            or not isinstance(owner, dict)
            or any(here.get(key) != owner.get(key) for key in ("host_id", "boot_id"))
            or not journal.get("recovery_source_sha256")):
        raise CleanupRecoveryBlocked("original Linux host and boot are not verified")
    attempts = [attempt for attempt in journal["attempts"].values()
                if attempt["scope"].get("assignment_id") == assignment_id]
    if not attempts or any(
        attempt["scope"].get("batch_id") != batch_id
        or attempt["scope"].get("task_id") != expected["task_id"]
        or attempt["scope"].get("owner_epoch") != row["owner_epoch"]
        or attempt["scope"].get("resume_generation") != row["resume_generation"]
        or attempt["status"] not in {"confirmed_absent", "never_started", "recovered_absent"}
        for attempt in attempts
    ):
        raise CleanupRecoveryBlocked("original execution audit does not match the assignment")
    receipt = capacity_journal._receipt(client, journal)
    release = journal["release_request"]
    release_sha256 = hashlib.sha256(capacity_journal._canonical(release)).hexdigest()
    if (receipt.get("closed") is not True
            or receipt.get("capacity_released") is not True
            or receipt.get("release_evidence_id") != release.get("evidence_id")
            or receipt.get("release_evidence_sha256") != release_sha256):
        raise CleanupRecoveryBlocked("server has not confirmed the original physical exit")
    _require_batch_capacity_settled(client, batch_id)
    status = client.assignment_recovery_status(assignment_id)
    if (not isinstance(status, dict)
            or status.get("recovery_evidence_version") != 1
            or any(status.get(key) != value for key, value in (
                ("assignment_id", assignment_id), ("batch_id", batch_id),
                ("benchmark_id", benchmark), ("task_id", expected["task_id"]),
                ("model", expected["model"]), ("effort", expected["effort"]),
            ))
            or status.get("has_submission") is not False
            or status.get("status") not in ({"leased", "expired", "released"}
                                          if saved_request else {"leased", "expired"})):
        raise CleanupRecoveryBlocked("exact server assignment is not unsubmitted and recoverable")
    inventory_sha256 = _job_inventory(home, row, assignment_id)
    journal_sha256 = hashlib.sha256(capacity_journal._canonical(journal)).hexdigest()
    quarantine_sha256 = (saved_request.get("quarantine_sha256") if isinstance(saved_request, dict)
                         else hashlib.sha256(capacity_journal._canonical(row)).hexdigest())
    result = {
        "schema_version": 1, "status": "ready", "assignment_id": assignment_id,
        "batch_id": batch_id, "session_id": session_id,
        "start_evidence": status.get("start_evidence"),
        "execution_started": "unknown", "has_submission": False,
        "inventory_sha256": inventory_sha256,
        "journal_sha256": journal_sha256,
        "boundary_sha256": boundary_sha256,
        "quarantine_sha256": quarantine_sha256,
        "release_evidence_id": release["evidence_id"],
        "release_evidence_sha256": release_sha256,
        "mutated": False,
    }
    return result


def execute(
    *, assignment_id: str, benchmark: str, batch_id: str,
    session_id: str, inventory_sha256: str, home: Path = HOME,
) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", inventory_sha256):
        raise CleanupRecoveryBlocked("exact preflight inventory digest is required")
    before = inspect(assignment_id=assignment_id, benchmark=benchmark,
                     batch_id=batch_id, session_id=session_id, home=home)
    if before["inventory_sha256"] != inventory_sha256:
        raise CleanupRecoveryBlocked("original job inventory changed since preflight")
    original_row = _exact_row(home, assignment_id)
    row = original_row
    saved = row.get("cleanup_recovery")
    if saved is None:
        saved = {"request_id": uuid.uuid4().hex,
                 "quarantine_sha256": before["quarantine_sha256"],
                 "inventory_sha256": inventory_sha256}
        row = {**row, "cleanup_recovery": saved}
        pending.replace_exact(home, original_row, row)
    elif (not isinstance(saved, dict)
          or saved.get("inventory_sha256") != inventory_sha256
          or saved.get("quarantine_sha256") != before["quarantine_sha256"]
          or not re.fullmatch(r"[0-9a-f]{32}", saved.get("request_id", ""))):
        raise CleanupRecoveryBlocked("saved recovery request changed")
    if _job_inventory(home, row, assignment_id) != inventory_sha256:
        raise CleanupRecoveryBlocked("original job changed before server disposition")
    cfg = {**_load_config(), "benchmark": benchmark}
    client = _client(cfg)
    client.set_batch_id(batch_id)
    payload = {
        "schema_version": 1, "assignment_id": assignment_id,
        "batch_id": batch_id, "session_id": session_id,
        "owner_epoch": row["owner_epoch"],
        "resume_generation": row["resume_generation"],
        "release_evidence_id": before["release_evidence_id"],
        "release_evidence_sha256": before["release_evidence_sha256"],
        "local_journal_sha256": before["journal_sha256"],
        "no_recoverable_local_result": True,
        "request_id": saved["request_id"],
    }
    response = client.recover_unsubmitted_cleanup(payload)
    if (not isinstance(response, dict)
            or any(response.get(key) != value for key, value in (
                ("assignment_id", assignment_id), ("batch_id", batch_id),
                ("session_id", session_id), ("request_id", saved["request_id"]),
                ("release_evidence_id", before["release_evidence_id"]),
                ("local_journal_sha256", before["journal_sha256"]),
                ("status", "terminated_unsubmitted"), ("has_submission", False),
                ("execution_started", "unknown"),
            ))):
        raise CleanupRecoveryBlocked("server disposition receipt did not match")
    status = client.assignment_recovery_status(assignment_id)
    if (status.get("status") != "released"
            or status.get("has_submission") is not False):
        raise CleanupRecoveryBlocked("terminal server state is not confirmed")
    path = assignment_boundary.state_path(home, benchmark, batch_id)
    assignment_boundary.confirm_cleanup_recovery(
        path, assignment_id=assignment_id,
        expected_digest=before["boundary_sha256"],
        request_id=saved["request_id"], session_id=session_id,
        journal_sha256=before["journal_sha256"],
        quarantine_sha256=before["quarantine_sha256"],
    )
    pending.remove_exact(home, row)
    return {"schema_version": 1, "status": "terminated_unsubmitted",
            "assignment_id": assignment_id, "batch_id": batch_id,
            "session_id": session_id, "execution_started": "unknown",
            "has_submission": False, "job_retained": True,
            "pending_fence_retired": True, "mutated": True}


def cmd_recover(args) -> int:
    try:
        common = {"assignment_id": args.assignment_id,
                  "benchmark": args.benchmark or _load_config().get("benchmark") or DEFAULT_BENCHMARK,
                  "batch_id": args.batch_id,
                  "session_id": args.runner_session_id}
        result = (execute(**common, inventory_sha256=args.inventory_sha256, home=HOME)
                  if args.execute else inspect(**common, home=HOME))
    except (CleanupRecoveryBlocked, assignment_boundary.BoundaryError,
            capacity_journal.CapacityEvidenceError, pending.PendingLedgerError,
            ApiError, OSError, ValueError, KeyError, TypeError) as exc:
        request_saved = False
        if args.execute:
            try:
                request_saved = isinstance(
                    _exact_row(HOME, args.assignment_id).get("cleanup_recovery"), dict
                )
            except (CleanupRecoveryBlocked, pending.PendingLedgerError,
                    OSError, ValueError):
                pass
        result = {"schema_version": 1, "status": "blocked",
                  "reason": str(exc),
                  "mutated": None if args.execute else False,
                  "local_request_saved": request_saved,
                  "server_outcome": "unknown" if args.execute else "not_requested",
                  "retry": "reinspect_then_same_request_only" if request_saved else "reinspect"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"ready", "terminated_unsubmitted"} else 1
