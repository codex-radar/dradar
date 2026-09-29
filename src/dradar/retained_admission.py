"""Read-only local checks for an explicitly reviewed legacy Mac exception.

This is not a crash recovery proof. The operator's fixed review accepts the
specified old evidence and binds today's machine, Docker context and files.
Nothing in this module writes outcomes, creates identities or removes work.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import capacity_journal, pending, runtime_identity


class AdmissionBlocked(RuntimeError):
    pass


def _digest(value):
    return hashlib.sha256(capacity_journal._canonical(value)).hexdigest()


def _host():
    if sys.platform != "darwin":
        raise AdmissionBlocked("reviewed exception requires the original Mac")
    raw = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                         capture_output=True, text=True, check=True, timeout=10).stdout
    ids = re.findall(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{36})"', raw)
    if len(ids) != 1:
        raise AdmissionBlocked("current Mac hardware identity is unavailable")
    return hashlib.sha256(ids[0].lower().encode()).hexdigest()


def _files(job):
    if job.is_symlink() or not job.is_dir() or job.resolve() != job:
        raise AdmissionBlocked("original job directory identity differs")
    inventory = {}
    total = 0
    for base, dirs, files in os.walk(job, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            if path.is_symlink():
                raise AdmissionBlocked("original job contains a symbolic link")
        for name in files:
            path = Path(base) / name
            if not path.is_file():
                raise AdmissionBlocked("original job contains an unknown file type")
            total += path.stat().st_size
            if total > 256 * 1024 * 1024 or len(inventory) >= 10000:
                raise AdmissionBlocked("original job exceeds bounded review inventory")
            data = path.read_bytes()
            if name == "result.json":
                value = json.loads(data)
                if isinstance(value, dict) and isinstance(value.get("agent_execution"), dict) and value["agent_execution"].get("finished_at") and not value.get("exception_info"):
                    raise AdmissionBlocked("a potentially completed result requires separate upload review")
            if name.endswith(".return-pending"):
                raise AdmissionBlocked("credential return is still pending")
            inventory[str(path.relative_to(job))] = hashlib.sha256(data).hexdigest()
    if not inventory:
        raise AdmissionBlocked("original job evidence is missing")
    return inventory


def _processes_absent(job, identities):
    raw = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,command="],
                         capture_output=True, text=True, check=True, timeout=10).stdout
    rows = {}
    for line in raw.splitlines():
        m = re.fullmatch(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(.+)", line)
        if not m or int(m[1]) in rows:
            raise AdmissionBlocked("process inventory is incomplete")
        rows[int(m[1])] = (int(m[2]), int(m[3]), m[4])
    if os.getpid() not in rows:
        raise AdmissionBlocked("current process identity is unavailable")
    for pid, (_ppid, pgid, command) in rows.items():
        # Reused numeric identities are rejected too, never treated as exit.
        if any(pid == p or pgid == g for p, g in identities):
            raise AdmissionBlocked("original PID or process group is present or reused")
        if pid != os.getpid() and str(job) in command:
            raise AdmissionBlocked("an original-job process or child remains")


def inspect(home, assignment_id, batch_id, session_ids, evidence_mapping_sha256, *, client=None):
    if not re.fullmatch(r"[0-9a-f]{32}", assignment_id) or not re.fullmatch(r"[0-9a-f]{64}", evidence_mapping_sha256):
        raise AdmissionBlocked("exact task and reviewed evidence mapping are required")
    job = home.resolve() / "work" / "jobs" / ("a" + assignment_id)
    host = _host()
    daemon = runtime_identity.docker_identity()
    if not daemon or not session_ids or len(session_ids) != len(set(session_ids)):
        raise AdmissionBlocked("complete original execution context is unavailable")
    rows = pending.load(home)
    if any(r.get("assignment_id") == assignment_id or r.get("batch_id") == batch_id for r in rows):
        raise AdmissionBlocked("original batch has a pending result")
    journals = {}
    identities = []
    attempts = 0
    attempt_scope = None
    for sid in sorted(session_ids):
        if not re.fullmatch(r"[0-9a-f]{32}", sid):
            raise AdmissionBlocked("session identity is invalid")
        path = home / "runner-reservations" / (sid + ".json")
        before = path.read_bytes()
        state = capacity_journal._read(path)
        if state.get("state") != "sealed" or state.get("released") is not True or state.get("batch_id") != batch_id:
            raise AdmissionBlocked("original journal is not sealed and released")
        q = state.get("release_request") or {}
        if any(q.get(k) != v for k, v in (("exit_state", "confirmed"), ("process_tree", "confirmed_absent"), ("owned_containers", "confirmed_absent"))):
            raise AdmissionBlocked("original exit declaration is incomplete")
        if client is not None:
            if state["server"] != str(client.server).rstrip("/"):
                raise AdmissionBlocked("original server identity differs")
            receipt = capacity_journal._receipt(client, state)
            if (receipt.get("closed") is not True or receipt.get("capacity_released") is not True
                    or receipt.get("release_evidence_id") != q.get("evidence_id")
                    or receipt.get("release_evidence_sha256") != _digest(q)):
                raise AdmissionBlocked("original journal does not match the formal release receipt")
        for attempt in state["attempts"].values():
            scope = attempt["scope"]
            if scope.get("assignment_id") != assignment_id or scope.get("batch_id") != batch_id or attempt.get("status") != "confirmed_absent":
                raise AdmissionBlocked("original attempt is incomplete or belongs to another task")
            attempt_scope = {**scope, "session_id": sid}
            spawned = [e for e in attempt["events"] if e["event"] == "spawned"]
            end = attempt["events"][-1]
            if (len(spawned) != 1 or spawned[0].get("process_identity_kind") != "live_child_handle_and_private_pgid"
                    or end.get("evidence_kind") != "private_pgid_and_exact_job_docker_recheck_v1"
                    or end.get("process_group") != "absent" or end.get("exact_job_containers") != "absent"):
                raise AdmissionBlocked("reviewed normal POSIX cleanup evidence is incomplete")
            spawn = spawned[0]
            pid, pgid = spawn.get("pid"), spawn.get("pgid")
            if type(pid) is not int or pid <= 0 or pgid != pid or end.get("pid") != pid or end.get("pgid") != pgid or Path(spawn.get("job_dir", "")) != job:
                raise AdmissionBlocked("original process and job identities disagree")
            identities.append((pid, pgid)); attempts += 1
        if path.read_bytes() != before:
            raise AdmissionBlocked("original journal changed during inspection")
        journals[sid] = hashlib.sha256(before).hexdigest()
    if attempts != 1:
        raise AdmissionBlocked("this exception requires one reviewed original execution")
    _processes_absent(job, identities)
    # Reuse exact-job all-container inspection, including stopped sidecars.
    from .session_recovery import _containers
    _containers(job, daemon)
    files = _files(job)
    _processes_absent(job, identities)
    if _host() != host or runtime_identity.docker_identity() != daemon:
        raise AdmissionBlocked("reviewed host or Docker context changed")
    return {"schema": "reviewed-mac-local-v1", "assignment_id": assignment_id,
            "batch_id": batch_id, "reviewed_host_sha256": host,
            "reviewed_daemon_sha256": _digest(daemon), "home_sha256": _digest(str(home.resolve())),
            "journals": journals, "files": files, "attempt_scope": attempt_scope,
            "evidence_mapping_sha256": evidence_mapping_sha256}


def validate(client, home, aid, proof, *, batch_id):
    try:
        m = proof["manifest"]
        scope = m["scope"]
        expires = datetime.fromisoformat(m["expires_at"])
        if (proof.get("classification") != "reviewed-retained-admission-v1"
                or proof.get("state") != "reviewed_exception"
                or proof.get("physical_exit") != "operator_reviewed_legacy_evidence"
                or proof.get("result_status") != "preserve_unknown"
                or m.get("schema") != "reviewed-retained-admission-v1"
                or type(m.get("max_claims")) is not int or m["max_claims"] != 1
                or scope.get("assignment_id") != aid or scope.get("batch_id") != batch_id
                or not re.fullmatch(r"[0-9a-f]{32}", proof["operation_id"])
                or _digest(m) != proof.get("manifest_sha256")
                or expires.tzinfo is None or expires <= datetime.now(timezone.utc)):
            raise AdmissionBlocked("exact reviewed exception is invalid or expired")
        review = m["local_review"]
        fresh = inspect(home, aid, batch_id, scope["session_ids"],
                        review["evidence_mapping_sha256"], client=client)
        if fresh != review:
            raise AdmissionBlocked("reviewed Mac evidence changed; keep original work")
        return "retained:" + proof["operation_id"] + ":" + proof["manifest_sha256"]
    except (KeyError, TypeError, ValueError, AttributeError, OSError, subprocess.SubprocessError, capacity_journal.CapacityEvidenceError) as exc:
        raise AdmissionBlocked("reviewed Mac evidence is incomplete or unavailable") from exc
