"""One original session: inspect first, explicitly seal newly observed exit.

This command never stops processes, deletes containers, changes assignments,
claims work or manufactures historical launch identities.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from . import capacity_journal as journal, runtime_identity
from .api_client import ApiError

Error = journal.CapacityEvidenceError


def _digest(value):
    return hashlib.sha256(journal._canonical(value)).hexdigest()


def _process_absent(identity):
    if (not isinstance(identity, dict) or type(identity.get("pid")) is not int
            or identity["pid"] <= 0 or type(identity.get("start_ticks")) is not int
            or identity["start_ticks"] <= 0):
        raise Error("The original process identity is missing.")
    here = runtime_identity.process_identity(os.getpid())
    if not here or any(identity.get(k) != here[k] for k in ("host_id", "boot_id")):
        raise Error("Original host/boot cannot be verified; preserve the reservation.")
    current = runtime_identity.process_identity(identity["pid"])
    if current is not None:
        # Even a reused PID is deliberately not signalled or treated as absent.
        raise Error("Original PID is present or reused; exit remains unknown.")


def _containers(job, saved_daemon):
    daemon = runtime_identity.docker_identity()
    if not saved_daemon or daemon != saved_daemon:
        raise Error("Original Docker daemon cannot be verified; exit remains unknown.")
    def query(args):
        return subprocess.run(["docker", *args], capture_output=True, text=True,
                              check=True, timeout=15).stdout
    ids = query(["ps", "-aq", "--no-trunc"]).split()
    if any(not re.fullmatch(r"[0-9a-f]{64}", i) for i in ids) or len(set(ids)) != len(ids):
        raise Error("Docker inventory is invalid.")
    rows = json.loads(query(["inspect", *ids])) if ids else []
    if not isinstance(rows, list) or {r.get("Id") for r in rows} != set(ids) or len(rows) != len(ids):
        raise Error("Docker inventory is incomplete.")
    def owned(row):
        config = row.get("Config")
        mounts = row.get("Mounts")
        if not isinstance(config, dict) or not isinstance(mounts, list):
            raise Error("Docker ownership metadata is incomplete.")
        labels = config.get("Labels") or {}
        if not isinstance(labels, dict):
            raise Error("Docker ownership labels are invalid.")
        config_files = labels.get("com.docker.compose.project.config_files", "")
        if not isinstance(config_files, str):
            raise Error("Docker Compose ownership is invalid.")
        if any(Path(value.strip()).is_absolute()
               and Path(value.strip()).resolve().is_relative_to(job)
               for value in config_files.split(",") if value.strip()):
            return True
        for mount in row.get("Mounts", []):
            if (mount.get("Type") == "bind" and isinstance(mount.get("Source"), str)
                    and Path(mount["Source"]).is_relative_to(job)):
                return True
        return False
    # Pier trial directory names are the persisted Compose project identity.
    # This still finds an egress container after the main container disappeared.
    projects = {child.name.lower() for child in job.iterdir()
                if child.is_dir() and not child.is_symlink()
                and re.fullmatch(r"[a-z0-9][a-z0-9-]*__[a-z0-9]{6,8}", child.name, re.I)}
    for row in rows:
        if owned(row):
            labels = row.get("Config", {}).get("Labels") or {}
            project = labels.get("com.docker.compose.project")
            if not isinstance(project, str) or not project:
                raise Error("Owned container has no exact Compose project.")
            projects.add(project)
    matched = []
    for row in rows:
        labels = row.get("Config", {}).get("Labels") or {}
        if owned(row) or labels.get("com.docker.compose.project") in projects:
            # The existing wire contract declares containers absent, not just
            # stopped. Keep the same criterion as normal runner finalization.
            raise Error("An exact-job container remains (even if stopped); exit remains unknown.")
    if runtime_identity.docker_identity() != daemon:
        raise Error("Docker identity changed during inspection.")
    return {"daemon": daemon, "containers": matched, "running": False}


def _observe(state, home):
    if sys.platform != "linux":
        raise Error("Crash exit inspection currently requires the original Linux host.")
    _process_absent(state.get("owner_identity"))
    facts = {}
    for key, attempt in state["attempts"].items():
        if attempt["status"] in {"confirmed_absent", "never_started", "recovered_absent"}:
            continue
        spawn = next((e for e in attempt["events"] if e["event"] == "spawned"), None)
        if spawn is None:
            raise Error("Incomplete launch evidence cannot establish physical exit.")
        if spawn.get("crash_recovery_supported") is not True:
            raise Error("Independent host helpers were not bound; exit remains unknown.")
        _process_absent(spawn.get("linux_identity"))
        pgid = spawn.get("pgid")
        if type(pgid) is not int or pgid <= 0 or pgid != spawn.get("pid"):
            raise Error("Private process group identity is missing.")
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            pass
        else:
            raise Error("Original process group is still present; exit remains unknown.")
        raw = spawn.get("job_dir")
        if not isinstance(raw, str):
            raise Error("Original job binding is missing.")
        job = Path(raw)
        expected = home.resolve() / "work" / "jobs"
        if (not job.is_absolute() or job.parent != expected or job.resolve() != job
                or not job.is_dir() or job.is_symlink()):
            raise Error("Original local job directory cannot be verified.")
        facts[key] = {"observed_at": datetime.now(timezone.utc).isoformat(),
                      "prior_events_sha256": _digest(attempt["events"]),
                      "owner_identity": state["owner_identity"],
                      "linux_identity": spawn["linux_identity"], "process_group": "absent",
                      "docker": _containers(job, spawn.get("docker_identity"))}
    if not facts and any(a["status"] not in {"confirmed_absent", "never_started", "recovered_absent"}
                         for a in state["attempts"].values()):
        raise Error("Execution evidence is incomplete.")
    # Recheck the session owner after all potentially slow daemon queries.
    _process_absent(state.get("owner_identity"))
    return facts


def recover(home, session_id, client, *, execute=False, expected_digest=None):
    if not re.fullmatch(r"[0-9a-f]{32}", session_id):
        raise Error("An exact session ID is required.")
    path = home / "runner-reservations" / (session_id + ".json")
    if path.parent.is_symlink():
        raise Error("Journal directory cannot be a symbolic link.")
    # Preflight performs no local write, including creation of a lock file.
    def inspect():
        state = journal._read(path)
        if state["session_id"] != session_id or state["server"] != str(client.server).rstrip("/"):
            raise Error("Original session/server scope does not match.")
        digest = _digest(state)
        if (expected_digest is not None and expected_digest != digest
                and not (state.get("release_request") and state.get("recovery_source_sha256") == expected_digest)):
            raise Error("Journal changed since preflight; inspect again.")
        journal._receipt(client, state)
        return state, digest
    if not execute:
        state, digest = inspect()
        facts = {} if state.get("release_request") else _observe(state, home)
        return {"session_id": session_id, "journal_sha256": digest,
                "status": "ready", "observations": facts, "mutated": False}
    if not expected_digest or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise Error("Execution requires the journal SHA256 returned by read-only preflight.")
    from .run_plans import _exclusive_lock
    with _exclusive_lock(path.with_suffix(".lock")):
        state, digest = inspect()
        if not state.get("release_request"):
            facts = _observe(state, home)
            original = deepcopy(state)
            for key, observation in facts.items():
                attempt = state["attempts"][key]
                spawn = next(e for e in attempt["events"] if e["event"] == "spawned")
                event = {k: deepcopy(spawn[k]) for k in ("schema", "execution_id", "scope")}
                event.update(event="recovered_absent", evidence_kind="linux_crash_recheck_v1",
                             recovery=observation)
                attempt["events"].append(event)
                attempt["status"] = "recovered_absent"
            state["recovery_source_sha256"] = digest
            state["state"] = "sealed"
            state["close_request"] = state.get("close_request") or {
                "session_id": session_id, "batch_id": state["batch_id"], "seq": 1, "reason": "interrupted"}
            manifest = {k: state[k] for k in ("schema_version", "session_id", "server", "batch_id",
                                             "state", "attempts", "close_request")}
            state["execution_manifest"] = manifest
            state["release_request"] = {"schema_version": 1, "session_id": session_id,
                "batch_id": state["batch_id"], "evidence_id": uuid.uuid4().hex, "exit_state": "confirmed",
                "process_tree": "confirmed_absent", "owned_containers": "confirmed_absent",
                "execution_manifest_sha256": _digest(manifest)}
            state["recovery_seal_sha256"] = journal._recovery_seal_digest(state)
            # Compare again before the only local transition. Save the original
            # journal in a separate immutable-by-convention evidence snapshot.
            if _digest(journal._read(path)) != digest:
                raise Error("Journal changed during inspection.")
            snapshot = path.with_suffix(".before-recovery")
            if snapshot.is_symlink():
                raise Error("Recovery snapshot cannot be a symbolic link.")
            if snapshot.exists():
                if json.loads(snapshot.read_text()) != original:
                    raise Error("Original recovery snapshot conflicts; preserve both records.")
            else:
                journal._write(snapshot, original)
            journal._write(path, state)
    released = journal.reconcile_file(path, client)
    return {"session_id": session_id, "status": "released" if released else "receipt_pending",
            "mutated": True}


def cmd_recover(args):
    from . import local_config
    from .legacy_capacity import _existing_client
    if not re.fullmatch(r"[0-9a-f]{32}", args.recover_session):
        raise Error("An exact session ID is required.")
    client, scope, _ = _existing_client(args)
    state_path = local_config.HOME / "runner-reservations" / (args.recover_session + ".json")
    if scope.get("batch_id") and journal._read(state_path)["batch_id"] != scope["batch_id"]:
        raise Error("Saved plan belongs to a different batch.")
    try:
        result = recover(local_config.HOME, args.recover_session, client,
                         execute=args.execute, expected_digest=args.journal_sha256)
    except Error as exc:
        result = {"session_id": args.recover_session, "status": "unknown", "reason": str(exc)}
    except ApiError:
        result = {"session_id": args.recover_session, "status": "unknown",
                  "reason": "Exact server receipt unavailable; retain and retry the same saved evidence."}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        result = {"session_id": args.recover_session, "status": "unknown",
                  "reason": "Runtime inspection failed; preserve the journal and reservation."}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"ready", "released"} else 1
