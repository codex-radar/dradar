"""Explicit recovery for expired, unsubmitted personal assignment boundaries."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from . import assignment_boundary, fleet, local_jobs
from .api_client import ApiError
from .identity import _client
from .local_config import DEFAULT_BENCHMARK, HOME, _load_config
from .machine import acquire_run_lock


class RecoveryBlocked(RuntimeError):
    pass


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
        # A result with a finished run, a patch, or a trajectory may still be
        # uploaded or salvaged even when the old lease expired. Keep the guard.
        for path in job.job_dir.rglob("*"):
            if path.is_symlink():
                raise RecoveryBlocked("a local job has a symlink; inspect its artifacts manually")
            if not path.is_file():
                continue
            if "artifacts" in path.parts:
                raise RecoveryBlocked("a local job still has possible upload artifacts")
            if path.name.endswith(".patch"):
                raise RecoveryBlocked("a local job still has possible upload artifacts")
            if path.name == "result.json":
                try:
                    result = json.loads(path.read_text(encoding="utf-8"))
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
            if path.name == "state.json":
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
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


def _check_processes(home: Path) -> None:
    if fleet.controller_is_active(home):
        raise RecoveryBlocked("a local Fleet controller is active")
    if os.name != "nt":
        try:
            proc = subprocess.run(
                ["ps", "-axo", "pid=,command="], capture_output=True,
                text=True, timeout=10, check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RecoveryBlocked("runner process inspection failed") from exc
        for line in proc.stdout.splitlines():
            match = re.match(r"\s*(\d+)\s+(.+)", line)
            if not match or int(match.group(1)) == os.getpid():
                continue
            command = match.group(2)
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


def _verify_server_statuses(client, state: dict, expected: set[str]) -> None:
    for aid in sorted(expected):
        try:
            row = client.assignment_recovery_status(aid)
        except ApiError as exc:
            raise RecoveryBlocked(
                f"server cannot confirm exact assignment {aid}; ask support for private review"
            ) from exc
        saved = state["expected"][aid]
        if not isinstance(row, dict) or (
            row.get("assignment_id") != aid
            or row.get("benchmark_id") != client.benchmark_id
            or row.get("status") != "expired"
            or row.get("has_submission") is not False
        ) or any(
            saved.get(key) != row.get(key) for key in ("task_id", "model", "effort")
        ):
            raise RecoveryBlocked(
                "server cannot confirm an exact expired, unsubmitted assignment "
                f"for this account: {aid}; ask support for private review"
            )


def cmd_boundary_recover(args) -> int:
    """Archive only an exact expired/no-submission boundary after local review."""
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
            raise RecoveryBlocked("accepted IDs must exactly match the saved boundary")
        if any(state["outcomes"].get(aid, {}).get("outcome") != "failed" for aid in expected):
            raise RecoveryBlocked("only locally failed assignments qualify for this recovery")
        client = _client(cfg)
        client.benchmark_id = benchmark
        identity = client.whoami()
        _verify_server_statuses(client, state, expected)
        if expected & _pending_ids(home):
            raise RecoveryBlocked("an old assignment remains in the pending-upload queue")
        kept = _check_jobs(home, expected)
        _check_processes(home)
        print(f"Authenticated radar account: {identity.get('nickname', 'unknown')}")
        print(f"Benchmark: {benchmark}; expired, unsubmitted assignments: {', '.join(sorted(expected))}")
        print(f"Local failed job directories retained: {len(kept)}")
        print("The saved boundary will be archived; failed job directories and logs stay in place.")
        phrase = "ACCEPT " + ",".join(sorted(expected))
        if input(f"Type {phrase} to accept these expired runs and continue later: ").strip() != phrase:
            raise RecoveryBlocked("confirmation did not match exact assignment IDs")
        # Recheck volatile evidence after the prompt; an upload or process can
        # appear while the human reads it. The ledger digest closes file races.
        _verify_server_statuses(client, state, expected)
        if expected & _pending_ids(home):
            raise RecoveryBlocked("pending-upload queue changed during confirmation")
        _check_jobs(home, expected)
        _check_processes(home)
        archived = assignment_boundary.archive_if_unchanged(path, digest)
    except (RecoveryBlocked, assignment_boundary.BoundaryError, ApiError, OSError) as exc:
        print(f"recovery blocked: {exc}. No boundary or job was removed.")
        return 1
    print(f"Old boundary archived at {archived}; local job evidence was retained.")
    print("You can now run your original `dradar go --pick ...` command.")
    return 0
