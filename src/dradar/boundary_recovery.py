"""Explicit, per-assignment recovery of a personal boundary."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from . import assignment_boundary, fleet, local_jobs, launcher_handoff
from .api_client import ApiError
from .identity import _client
from .local_config import DEFAULT_BENCHMARK, HOME, _load_config
from .machine import acquire_run_lock


class RecoveryBlocked(RuntimeError):
    pass


def cmd_boundary_inspect(args) -> int:
    """Show the exact locally saved set; never query or change server state."""
    cfg = _load_config()
    benchmark = args.benchmark or cfg.get("benchmark") or DEFAULT_BENCHMARK
    try:
        path = assignment_boundary.state_path(HOME, benchmark)
        if not path.exists() and not path.is_symlink():
            print(f"No saved personal assignment boundary for {benchmark}.")
            return 0
        state, digest = assignment_boundary.inspect_snapshot(path)
        if state.get("benchmark_id") != benchmark or state.get("batch_id") is not None:
            raise RecoveryBlocked("saved boundary scope differs from the selected benchmark")
        expected = state["expected"]
        if not expected:
            raise RecoveryBlocked("saved boundary has no assignment IDs")
    except (RecoveryBlocked, assignment_boundary.BoundaryError, OSError) as exc:
        print(f"boundary inspection blocked: {exc}")
        return 1
    print(f"Saved personal boundary for {benchmark} (local SHA-256 {digest}):")
    for aid in sorted(expected):
        outcome = state["outcomes"].get(aid, {}).get("outcome", "unresolved")
        print(f"  {aid}  local outcome: {outcome}")
    print(f"Exact saved set: {len(expected)} assignment ID(s). This is local state, not server recovery proof.")
    print("Recovery checks every saved ID and records only independently verified outcomes; "
          "it keeps unknown work and files. After inspecting local work and server evidence, "
          "repeat `--accept-assignment ID` for each saved ID with `dradar boundary recover`.")
    return 0


def _pending_ids(home: Path) -> set[str]:
    path = home / "pending_uploads.json"
    if path.is_symlink():
        raise RecoveryBlocked("pending-upload ledger is a symlink; inspect it manually")
    if not path.exists():
        return set()
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryBlocked("pending-upload ledger is unreadable; keep it for review") from exc
    if not isinstance(entries, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("assignment_id"), str)
        for row in entries
    ):
        raise RecoveryBlocked("pending-upload ledger has unknown rows; keep it for review")
    return {row["assignment_id"] for row in entries}


def _check_jobs(home: Path, expected: set[str]) -> list[Path]:
    kept = []
    jobs_root = home / "work" / "jobs"
    if jobs_root.is_symlink():
        raise RecoveryBlocked("local jobs directory is a symlink; inspect it manually")
    scanned = local_jobs.scan(home)
    known = {job.job_dir for job in scanned if job.assignment_id in expected}
    if jobs_root.exists():
        for lexical in jobs_root.iterdir():
            if any(lexical.name.startswith(f"a{aid}") for aid in expected) and (
                lexical.is_symlink() or not lexical.is_dir() or lexical.resolve() not in known
            ):
                raise RecoveryBlocked("an old local job has an unknown path type")
    for job in scanned:
        if job.assignment_id not in expected:
            continue
        kept.append(job.job_dir)
        trial = job.trial_dir
        if trial is None or trial.is_symlink():
            raise RecoveryBlocked("an old local job has an ambiguous trial directory")
        artifacts = trial / "artifacts"
        host_output = trial / ".dradar" / "host-output"
        if (artifacts.is_symlink() or (trial / ".dradar").is_symlink()
                or host_output.is_symlink()):
            raise RecoveryBlocked("an old artifact directory is a symlink")
        if (artifacts.exists() and not artifacts.is_dir()) or (
            host_output.exists() and not host_output.is_dir()
        ):
            raise RecoveryBlocked("an old artifact directory has an unknown path type")
        if artifacts.is_dir() and any(artifacts.iterdir()):
            raise RecoveryBlocked("a local job still has possible upload artifacts")
        patch = host_output / "model.patch"
        if patch.exists() or patch.is_symlink():
            raise RecoveryBlocked("a local job still has possible upload artifacts")
        result_path = trial / "result.json"
        if result_path.is_symlink():
            raise RecoveryBlocked("a local result is a symlink")
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RecoveryBlocked("a local result is unreadable; inspect it manually") from exc
            execution = result.get("agent_execution") if isinstance(result, dict) else None
            if (
                not isinstance(result, dict)
                or result.get("finished_at") is not None
                or (isinstance(execution, dict) and execution.get("finished_at") is not None)
            ):
                raise RecoveryBlocked("a local job may have a completed result")
            for key in ("completed", "n_completed", "num_completed"):
                value = result.get(key, 0)
                if not isinstance(value, (int, float)) or value > 0:
                    raise RecoveryBlocked("a local job may have a completed result")
        state_path = host_output / "state.json"
        if state_path.is_symlink():
            raise RecoveryBlocked("a local artifact state is a symlink")
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RecoveryBlocked("a local artifact state is unreadable") from exc
            if not isinstance(state, dict) or state.get("complete") is True:
                raise RecoveryBlocked("a local artifact state reports completed work")
    return kept


def _looks_like_runner_process(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return True  # An unparseable command cannot prove a runner is absent.
    return _looks_like_runner_argv(parts)


def _looks_like_runner_argv(parts: list[str]) -> bool:
    if "--worker-child" in parts:
        return True
    actions = {"go", "resume", "run", "fleet"}
    if not actions.intersection(parts):
        return False
    if any(
        Path(part).name in {"dradar", "dradar.exe", "dradar.cli"}
        or "dradar/" in part
        for part in parts
    ):
        return True
    # The signed OTA launcher can execute Python from an anonymous fd: its
    # process command line contains only /dev/fd/N and the CLI action.
    return any(part.startswith("/dev/fd/") for part in parts)


def _fleet_process_argv(pid: int, ppid: int) -> tuple[dict, list[str]] | None:
    """Read Linux argument boundaries, bound to the same PID and parent.

    ps command text is a display string, not shell-quoted argv. In particular,
    task prompts containing quotes must never become host runner evidence.
    No argument content is included in errors or persisted.
    """
    from . import runtime_identity

    try:
        before = runtime_identity.process_identity(pid)
        if before is None:
            return None  # An exited process cannot own live work.
        root = Path(f"/proc/{pid}")
        stat = (root / "stat").read_text().rsplit(") ", 1)[1].split()
        raw = (root / "cmdline").read_bytes()
        after = runtime_identity.process_identity(pid)
        final_stat = (root / "stat").read_text().rsplit(") ", 1)[1].split()
        if (before != after or int(stat[1]) != ppid or int(final_stat[1]) != ppid
                or int(stat[19]) != before["start_ticks"]
                or int(final_stat[19]) != before["start_ticks"]):
            raise RecoveryBlocked("runner process identity or ancestry changed")
        if not raw:
            # Linux kernel threads and zombies cannot execute a CLI action.
            # Empty argv from a live userspace process is unknown, not proof.
            if (stat[0] == final_stat[0] == "Z"
                    or int(stat[6]) & int(final_stat[6]) & 0x00200000):
                return before, []
            raise RecoveryBlocked("runner process arguments are unavailable")
        if not raw.endswith(b"\0") or raw.startswith(b"\0"):
            raise RecoveryBlocked("runner process arguments are invalid")
        return before, [os.fsdecode(part) for part in raw[:-1].split(b"\0")]
    except FileNotFoundError as exc:
        # A disappearing row is safe only when the PID really is absent;
        # exec/PID reuse or unreadable identity must not silently pass.
        try:
            current = runtime_identity.process_identity(pid)
        except (OSError, ValueError, IndexError) as identity_exc:
            raise RecoveryBlocked("runner process identity could not be verified") from identity_exc
        if current is None:
            return None
        raise RecoveryBlocked("runner process identity could not be verified") from exc
    except (OSError, ValueError, IndexError, KeyError) as exc:
        raise RecoveryBlocked("runner process identity could not be verified") from exc


def _check_fleet_claim_processes(home: Path, operation_id: str) -> None:
    """Exclude only this controller's proven pool tree, never all runners."""
    from . import capacity_journal, runtime_identity

    if os.name == "nt" or runtime_identity.process_identity(os.getpid()) is None:
        raise RecoveryBlocked("Fleet claim process ownership is unavailable on this platform")
    state = fleet._read_json(fleet._state_path(home))
    window = state.get("claim_window") if isinstance(state, dict) else None
    operation = window.get("operation") if isinstance(window, dict) else None
    if (not fleet.controller_is_active(home) or not isinstance(operation, dict)
            or operation.get("operation_id") != operation_id
            or operation.get("status") != "active"
            or operation.get("controller_id") != state.get("controller_id")
            or operation.get("owner_identity") != runtime_identity.process_identity(os.getpid())):
        raise RecoveryBlocked("Fleet claim controller or foreground ownership changed")
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid="], capture_output=True,
            text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryBlocked("runner process inspection failed") from exc
    processes = {}
    identities = {}
    seen_processes = set()
    for line in proc.stdout.splitlines():
        match = re.fullmatch(r"\s*(\d+)\s+(\d+)\s*", line)
        if not match or int(match.group(1)) in seen_processes:
            raise RecoveryBlocked("runner process inspection returned an unknown row")
        pid, ppid = int(match.group(1)), int(match.group(2))
        seen_processes.add(pid)
        snapshot = _fleet_process_argv(pid, ppid)
        if snapshot is not None:
            identities[pid], argv = snapshot
            processes[pid] = (ppid, argv)
    own = processes.get(os.getpid())
    if own is None or own[0] != os.getppid() or state.get("pid") not in processes:
        raise RecoveryBlocked("Fleet claim process ancestry could not be verified")
    supervisor = launcher_handoff.supervisor()
    if supervisor is not None and supervisor[0] not in processes:
        raise RecoveryBlocked("runner launcher process is missing")
    pool_roots: dict[int, str] = {}
    for batch_id, item in fleet._active_batches(state).items():
        pid = item.get("pid")
        if (type(pid) is not int or pid not in processes
                or runtime_identity.process_identity(pid) != item.get("process_identity")
                or processes[pid][0] != state["pid"]):
            raise RecoveryBlocked("Fleet pool process identity is unconfirmed")
        pool_roots[pid] = batch_id

    def pool_for(pid: int) -> str | None:
        seen = set()
        while pid in processes and pid not in seen:
            current = _fleet_process_argv(pid, processes[pid][0])
            if current != (identities[pid], processes[pid][1]):
                raise RecoveryBlocked("Fleet process identity or ancestry changed")
            if pid in pool_roots:
                return pool_roots[pid]
            if pid == state["pid"]:
                return None
            seen.add(pid)
            pid = processes[pid][0]
        return None

    for pid, (_ppid, argv) in processes.items():
        if pid == os.getpid() or pid == state["pid"] or pid in pool_roots:
            continue
        if supervisor is not None and pid == supervisor[0] == own[0]:
            if launcher_handoff.argv_digest(argv) == supervisor[1]:
                continue
            raise RecoveryBlocked("runner launcher identity could not be verified")
        if _looks_like_runner_argv(argv) and pool_for(pid) is None:
            raise RecoveryBlocked("another DRadar runner process may be active")

    jobs_root = (home / "work" / "jobs").resolve()
    if (home / "work" / "jobs").is_symlink():
        raise RecoveryBlocked("local jobs directory is a symlink")
    owned_jobs: set[Path] = set()
    journal_root = home / "runner-reservations"
    if journal_root.exists():
        for path in journal_root.glob("*.json"):
            if path.is_symlink():
                raise RecoveryBlocked("runner reservation ownership is unreadable")
            try:
                journal = capacity_journal._read(path)
            except capacity_journal.CapacityEvidenceError as exc:
                raise RecoveryBlocked("runner reservation ownership is unreadable") from exc
            batch_id = journal.get("batch_id")
            if batch_id not in pool_roots.values():
                continue
            owner_identity = journal.get("owner_identity")
            owner_pid = owner_identity.get("pid") if isinstance(owner_identity, dict) else None
            if (type(owner_pid) is not int or pool_for(owner_pid) != batch_id
                    or runtime_identity.process_identity(owner_pid) != owner_identity):
                continue
            for attempt in journal.get("attempts", {}).values():
                if attempt.get("scope", {}).get("batch_id") != batch_id:
                    continue
                spawn = next((event for event in attempt.get("events", [])
                              if event.get("event") == "spawned"), None)
                raw_job = spawn.get("job_dir") if isinstance(spawn, dict) else None
                if isinstance(raw_job, str):
                    job = Path(raw_job).resolve()
                    if job != jobs_root and job.is_relative_to(jobs_root):
                        owned_jobs.add(job)
    try:
        running = subprocess.run(
            ["docker", "ps", "-q"], capture_output=True, text=True,
            timeout=20, check=True,
        ).stdout.split()
        inspected = subprocess.run(
            ["docker", "inspect", *running], capture_output=True,
            text=True, timeout=20, check=True,
        ) if running else None
        containers = json.loads(inspected.stdout) if inspected is not None else []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise RecoveryBlocked("Docker process inspection failed") from exc
    if not isinstance(containers, list) or len(containers) != len(running):
        raise RecoveryBlocked("Docker returned an incomplete process inventory")
    full_ids = [item.get("Id") for item in containers if isinstance(item, dict)]
    if (len(full_ids) != len(containers)
            or any(not isinstance(identifier, str) or not identifier
                   for identifier in full_ids)
            or any(sum(identifier.startswith(short_id) for identifier in full_ids) != 1
                   for short_id in running)):
        raise RecoveryBlocked("Docker returned an inconsistent process inventory")
    project_jobs: dict[str, set[Path]] = {}
    if jobs_root.exists():
        for job_dir in jobs_root.iterdir():
            if job_dir.is_symlink() or not job_dir.is_dir():
                raise RecoveryBlocked("local job ownership is unreadable")
            for trial in job_dir.iterdir():
                if trial.is_symlink():
                    raise RecoveryBlocked("local Pier project ownership is unreadable")
                if trial.is_dir() and re.fullmatch(
                        r"[a-z0-9][a-z0-9-]*__[a-z0-9]{6,8}", trial.name, re.I):
                    project_jobs.setdefault(trial.name.lower(), set()).add(job_dir.resolve())
    owned_projects = {
        project for project, jobs in project_jobs.items()
        if jobs and jobs <= owned_jobs
    }
    for container in containers:
        if not isinstance(container, dict):
            raise RecoveryBlocked("Docker returned an invalid process inventory")
        config = container.get("Config")
        labels = config.get("Labels") or {} if isinstance(config, dict) else {}
        if not isinstance(labels, dict):
            raise RecoveryBlocked("Docker ownership labels are invalid")
        project = labels.get("com.docker.compose.project")
        config_files = labels.get("com.docker.compose.project.config_files", "")
        if not isinstance(config_files, str):
            raise RecoveryBlocked("Docker Compose ownership is invalid")
        for raw in config_files.split(","):
            value = raw.strip()
            if not value or not Path(value).is_absolute():
                continue
            config_path = Path(value).resolve()
            if config_path.is_relative_to(jobs_root):
                if not any(config_path.is_relative_to(job) for job in owned_jobs):
                    raise RecoveryBlocked("a non-Fleet DRadar Pier container is still running")
                if isinstance(project, str) and project:
                    if (project.lower() in project_jobs
                            and not project_jobs[project.lower()] <= owned_jobs):
                        raise RecoveryBlocked("Docker Pier project identity is ambiguous")
                    owned_projects.add(project.lower())
    for container in containers:
        if not isinstance(container, dict) or not isinstance(container.get("Mounts"), list):
            raise RecoveryBlocked("Docker returned an invalid process inventory")
        labels = (container.get("Config") or {}).get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        if isinstance(project, str) and project.lower() in project_jobs \
                and project.lower() not in owned_projects:
            raise RecoveryBlocked("a non-Fleet DRadar Pier sidecar is still running")
        for mount in container["Mounts"]:
            source = mount.get("Source") if isinstance(mount, dict) else None
            if isinstance(source, str) and Path(source).resolve().is_relative_to(jobs_root):
                if not any(Path(source).resolve().is_relative_to(job) for job in owned_jobs):
                    raise RecoveryBlocked("a non-Fleet DRadar job container is still running")

    # Docker inspection may take time. Recheck the foreground, controller and
    # every process before relying on the earlier absence/ownership proof.
    for pid, (ppid, argv) in processes.items():
        current = _fleet_process_argv(pid, ppid)
        if current is None and pid not in {os.getpid(), state["pid"], *pool_roots}:
            continue
        if current != (identities[pid], argv):
            raise RecoveryBlocked("runner process identity or ancestry changed")


def _check_processes(home: Path, *, fleet_claim_operation: str | None = None) -> None:
    if fleet_claim_operation is not None:
        _check_fleet_claim_processes(home, fleet_claim_operation)
        return
    if fleet.controller_is_active(home):
        raise RecoveryBlocked("a local Fleet controller is active")
    if os.name != "nt":
        try:
            proc = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,command="], capture_output=True,
                text=True, timeout=10, check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RecoveryBlocked("runner process inspection failed") from exc
        processes = {}
        for line in proc.stdout.splitlines():
            match = re.fullmatch(r"\s*(\d+)\s+(\d+)\s+(.+)", line)
            if not match or int(match.group(1)) in processes:
                raise RecoveryBlocked("runner process inspection returned an unknown row")
            processes[int(match.group(1))] = (int(match.group(2)), match.group(3))
        own = processes.get(os.getpid())
        if own is None or own[0] != os.getppid():
            raise RecoveryBlocked("runner process ancestry could not be verified")
        supervisor = launcher_handoff.supervisor()
        if supervisor is not None and supervisor[0] not in processes:
            raise RecoveryBlocked("runner launcher process is missing")
        for pid, (_ppid, command) in processes.items():
            if pid == os.getpid():
                continue
            if supervisor is not None and pid == supervisor[0] == own[0]:
                try:
                    if launcher_handoff.argv_digest(shlex.split(command)) == supervisor[1]:
                        continue
                except ValueError:
                    pass
                raise RecoveryBlocked("runner launcher identity could not be verified")
            if _looks_like_runner_process(command):
                raise RecoveryBlocked("another DRadar runner process may be active")
    else:  # Windows cannot inspect other processes' arguments with tasklist.
        raise RecoveryBlocked("process inspection is unavailable on this platform")
    try:
        running = subprocess.run(
            ["docker", "ps", "-q"], capture_output=True, text=True,
            timeout=20, check=True,
        ).stdout.split()
        if not running:
            return
        inspected = subprocess.run(
            ["docker", "inspect", *running], capture_output=True,
            text=True, timeout=20, check=True,
        )
        containers = json.loads(inspected.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise RecoveryBlocked("Docker process inspection failed") from exc
    if not isinstance(containers, list) or len(containers) != len(running):
        raise RecoveryBlocked("Docker returned an incomplete process inventory")
    jobs_root = (home / "work" / "jobs").resolve()
    for container in containers:
        if not isinstance(container, dict):
            raise RecoveryBlocked("Docker returned an invalid process inventory")
        for mount in container.get("Mounts") or []:
            source = mount.get("Source") if isinstance(mount, dict) else None
            if isinstance(source, str) and Path(source).resolve().is_relative_to(jobs_root):
                raise RecoveryBlocked("a DRadar job container is still running")


def _scoped_status(client, state: dict, aid: str) -> dict:
    """Require current authenticated evidence for the exact saved identity."""
    try:
        row = client.assignment_recovery_status(aid)
    except (ApiError, ValueError, AttributeError) as exc:
        raise RecoveryBlocked(f"{aid}: exact server status unavailable") from exc
    saved = state["expected"][aid]
    if not isinstance(row, dict) or row.get("recovery_evidence_version") != 1 or any(
        row.get(key) != value for key, value in (
            ("assignment_id", aid),
            ("benchmark_id", client.benchmark_id),
            ("batch_id", saved.get("batch_id")),
            ("task_id", saved.get("task_id")),
            ("model", saved.get("model")),
            ("effort", saved.get("effort")),
        )
    ):
        raise RecoveryBlocked(f"{aid}: server evidence version or assignment scope differs")
    return row


def _verified_outcome(client, state: dict, aid: str) -> str:
    """Classify only proof returned by the authenticated, exact server read."""
    row = _scoped_status(client, state, aid)
    if row.get("status") in {"expired", "released"} and row.get("has_submission") is False:
        if row.get("start_evidence") == "never_started":
            return "not_started_terminal"
        raise RecoveryBlocked(f"{aid}: no durable never-started proof")
    if row.get("status") in {"submitted", "invalid"} and row.get("has_submission") is True:
        if row.get("exit_evidence") == "cleanup_receipt_confirmed":
            return "submitted"
        raise RecoveryBlocked(f"{aid}: submission exists but original exit is unconfirmed")
    raise RecoveryBlocked(f"{aid}: terminal state or accepted submission is unconfirmed")


def historical_unknown_allows_claim(
    client, state: dict, digest: str, path: Path, home: Path,
    *, fleet_claim_operation: str | None = None,
) -> int:
    """Read a fresh, complete admission proof without settling old results.

    The historical boundary remains on disk. Every later claim attempt must
    repeat this read; the Server's actual claim/capacity gate still decides.
    """
    client.historical_admission_reference = None
    if (state.get("benchmark_id") != getattr(client, "benchmark_id", None)
            or state.get("batch_id") is not None
            or getattr(client, "batch_id", None) is not None
            or getattr(client, "plan_scoped", False)):
        raise RecoveryBlocked("personal boundary identity or scope differs")
    if any(
        not isinstance(saved.get(key), str) or not saved[key]
        for saved in state["expected"].values()
        for key in ("task_id", "model", "effort", "batch_id")
    ):
        raise RecoveryBlocked("historical assignment metadata is incomplete")
    unresolved = sorted(set(state["expected"]) - {
        aid for aid, record in state["outcomes"].items()
        if record.get("outcome") in assignment_boundary.SETTLED_OUTCOMES
    })
    if not unresolved:
        raise RecoveryBlocked("no unresolved historical outcome needs admission review")
    # A personal boundary retains settled history from earlier batches. Only
    # unresolved outcomes require this fresh admission, but every unresolved
    # ID must belong to the same reviewed batch and appear in its exact proof.
    saved_batches = {state["expected"][aid].get("batch_id") for aid in unresolved}
    if len(saved_batches) != 1 or not next(iter(saved_batches)):
        raise RecoveryBlocked("historical assignments have incomplete batch scope")
    if _pending_ids(home):
        raise RecoveryBlocked("a pending upload remains")
    reviewed_ref = None
    reviewed_shape = None
    legacy_seen = False
    retained_seen = False
    for aid in unresolved:
        row = _scoped_status(client, state, aid)
        proof = row.get("admission_evidence")
        if row.get("admission_evidence_version") == 3:
            from . import retained_admission
            if (len(unresolved) != 1 or row.get("status") not in ("expired", "released")
                    or row.get("has_submission") is not False
                    or row.get("start_evidence") != "unknown_or_started"
                    or row.get("exit_evidence") != "unknown" or not isinstance(proof, dict)):
                raise RecoveryBlocked("retained exception does not match the exact unknown result")
            try:
                reviewed_ref = retained_admission.validate(client, home, aid, proof,
                    batch_id=next(iter(saved_batches)))
            except retained_admission.AdmissionBlocked as exc:
                raise RecoveryBlocked(str(exc)) from exc
            retained_seen = True
            continue
        if (row.get("status") not in ("submitted", "invalid")
                or row.get("has_submission") is not True
                or row.get("start_evidence") != "unknown_or_started"
                or row.get("exit_evidence") != "unknown"
                or not isinstance(proof, dict)
                or proof.get("state") != "exit_unknown"
                or type(proof.get("related_session_count")) is not int
                or proof["related_session_count"] < 1
                or proof.get("result_status") != "preserve_unknown"):
            raise RecoveryBlocked(f"{aid}: historical nonblocking evidence is incomplete")
        if row.get("admission_evidence_version") == 2:
            ids = proof.get("result_assignment_ids")
            operation_id = proof.get("operation_id")
            sha = proof.get("manifest_sha256")
            shape = (proof.get("batch_id"), proof.get("batch_session_count"),
                     proof.get("unlinked_session_count"), proof.get("counted_session_count"),
                     tuple(ids) if isinstance(ids, list) else None)
            if (legacy_seen
                    or proof.get("classification") != "batch_admission_reviewed"
                    or proof.get("closed") is not False
                    or proof.get("all_batch_sessions_reviewed") is not True
                    or proof.get("physical_exit") != "unknown"
                    or shape[0] != next(iter(saved_batches))
                    or any(type(value) is not int or value < 0 for value in shape[1:4])
                    or shape[1] < 1 or max(shape[2], shape[3]) > shape[1]
                    or proof["related_session_count"] > shape[1] - shape[2]
                    or proof.get("counts_toward_capacity") is not (shape[3] > 0)
                    or proof.get("all_related_sessions_linked") is not (shape[2] == 0)
                    or ids != unresolved
                    or not isinstance(operation_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", operation_id)
                    or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)):
                raise RecoveryBlocked(f"{aid}: reviewed original-batch proof is incomplete")
            reference = operation_id + ":" + sha
            if reviewed_ref is not None and (reference != reviewed_ref or shape != reviewed_shape):
                raise RecoveryBlocked("original results disagree on the batch review")
            reviewed_ref, reviewed_shape = reference, shape
        elif (row.get("admission_evidence_version") == 1
              and proof.get("classification") == "historical_unverified"
              and proof.get("closed") is True
              and proof.get("counts_toward_capacity") is False
              and proof.get("all_related_sessions_linked") is True
              and reviewed_ref is None):
            legacy_seen = True
        else:
            raise RecoveryBlocked(f"{aid}: historical nonblocking evidence is incomplete")
    # Close local races before the new claim. No state is recorded as settled
    # or cached for a future invocation.
    if not retained_seen:
        if fleet_claim_operation is None:
            _check_processes(home)
        else:
            _check_processes(home, fleet_claim_operation=fleet_claim_operation)
    if _pending_ids(home):
        raise RecoveryBlocked("a pending upload appeared during review")
    _, current_digest = assignment_boundary.snapshot(path)
    if current_digest != digest:
        raise RecoveryBlocked("saved personal boundary changed during review")
    client.historical_admission_reference = reviewed_ref
    return len(unresolved)


def _classify(client, state: dict, expected: set[str], home: Path) -> tuple[dict[str, str], list[str]]:
    verified: dict[str, str] = {}
    unknown: list[str] = []
    pending = _pending_ids(home)
    for aid in sorted(expected):
        if state["outcomes"].get(aid, {}).get("outcome") in assignment_boundary.SETTLED_OUTCOMES:
            continue
        try:
            outcome = _verified_outcome(client, state, aid)
            if aid in pending:
                raise RecoveryBlocked(f"{aid}: original pending upload remains")
            if outcome == "not_started_terminal":
                # Completed result files and patches are normal for a server-
                # accepted submission. Recovery never removes or uploads them.
                _check_jobs(home, {aid})
            verified[aid] = outcome
        except RecoveryBlocked as exc:
            unknown.append(str(exc))
    return verified, unknown


def cmd_boundary_recover(args) -> int:
    """Settle only verified original IDs; preserve unknowns and all local work."""
    home = HOME
    cfg = _load_config()
    benchmark = args.benchmark or cfg.get("benchmark") or DEFAULT_BENCHMARK
    selected = set(args.accept_expired_assignment)
    if len(selected) != len(args.accept_expired_assignment):
        print("recovery blocked: repeat each exact assignment ID only once")
        return 1
    path = assignment_boundary.state_path(home, benchmark)
    try:
        acquire_run_lock(home)
        if path.is_symlink():
            raise RecoveryBlocked("saved assignment boundary is a symlink")
        state, digest = assignment_boundary.snapshot(path)
        if state.get("benchmark_id") != benchmark or state.get("batch_id") is not None:
            raise RecoveryBlocked("this command handles only a personal benchmark boundary")
        expected = set(state["expected"])
        if not expected or selected != expected:
            raise RecoveryBlocked(
                "accepted IDs must exactly match the saved boundary; "
                "run `dradar boundary inspect` to see the exact local set"
            )
        client = _client(cfg)
        client.benchmark_id = benchmark
        identity = client.whoami()
        _check_processes(home)
        verified, unknown = _classify(client, state, expected, home)
        print(f"Authenticated radar account: {identity.get('nickname', 'unknown')}")
        print(f"Benchmark: {benchmark}; verified recoverable IDs: {', '.join(sorted(verified)) or 'none'}")
        for reason in unknown:
            print(f"kept unknown: {reason}")
        if not verified:
            if assignment_boundary._report(state, set()).complete:
                archived = assignment_boundary.archive_if_unchanged(path, digest)
                print(f"Completed recovery boundary archived at {archived}; original jobs retained.")
                return 0
            raise RecoveryBlocked("no new assignment has complete recovery evidence")
        print("Original job directories and logs stay in place. Unknown IDs stay in the boundary.")
        phrase = "ACCEPT " + ",".join(sorted(verified))
        if input(f"Type {phrase} to record these exact outcomes: ").strip() != phrase:
            raise RecoveryBlocked("confirmation did not match exact assignment IDs")
        # Recheck volatile evidence after the prompt; an upload or process can
        # appear while the human reads it. The ledger digest closes file races.
        _check_processes(home)
        rechecked, _ = _classify(client, state, expected, home)
        if rechecked != verified:
            raise RecoveryBlocked("recovery evidence changed during confirmation")
        archived = assignment_boundary.record_verified_recovery(path, digest, verified)
    except (RecoveryBlocked, assignment_boundary.BoundaryError, ApiError, OSError) as exc:
        print(f"recovery blocked: {exc}. No boundary or job was removed.")
        return 1
    if archived is None:
        print("Verified outcomes saved; unresolved IDs remain in the original boundary.")
        print("Do not claim new work until those IDs have exact recovery evidence.")
        return 1
    else:
        print(f"Old boundary archived at {archived}; local job evidence was retained.")
        print("You can now run your original `dradar go --pick ...` command.")
    return 0
