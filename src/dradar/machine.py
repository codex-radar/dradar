"""Per-machine guardrails for the run loop (born from a volunteer running
three dradar sessions at once, 2026-07-14):

- single-instance lock: two dradar runners on one machine fetch the SAME held
  batch and race each other cell by cell — the loser of every race uploads a
  duplicate the server 409s away, pure quota waste. An OS-level file lock
  (auto-released on process death, so never stale) makes the second runner
  refuse to start instead.
- orphan compose sweep: pier launches each trial as a docker compose project
  named <task>__<trialid>; a killed dradar/pier never runs `compose down`, so
  the agent keeps running (and burning quota) inside a container nobody will
  ever harvest. A project is eligible only when Docker proves that one of its
  bind mounts or compose files lives below this DRADAR_HOME's work/jobs tree.
  The per-home lock alone cannot prove ownership on a shared Docker daemon.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# pier compose projects look like <task-slug>__<7-char trial id>. Anchored and
# specific on purpose: this pattern decides what the sweep offers to docker
# compose down, and a false positive would kill a stranger's containers.
_PIER_PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9-]*__[a-z0-9]{6,8}$", re.IGNORECASE)

_lock_handle = None  # keeps the OS lock alive for the process lifetime


def _path_belongs_to(path: str, root: Path) -> bool:
    if not path:
        return False
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _project_belongs_to_home(name: str, home: Path) -> bool:
    """Return True only with positive Docker evidence of project ownership.

    Compose project names are not namespaced by user or DRADAR_HOME.  On a
    shared daemon another live runner can therefore have a perfectly valid
    Pier-shaped name.  Pier mounts its trial directory (and generated compose
    files) below ``HOME/work/jobs``; inspect those paths before allowing an
    automatic teardown.  Inspection failures deliberately fail closed.
    """
    try:
        proc = subprocess.run(
            ["docker", "ps", "-aq", "--filter",
             f"label=com.docker.compose.project={name}"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    container_ids = proc.stdout.split()
    if not container_ids:
        return False
    try:
        proc = subprocess.run(
            ["docker", "inspect", *container_ids],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    try:
        containers = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return False

    jobs_root = home / "work" / "jobs"
    for container in containers:
        if not isinstance(container, dict):
            continue
        for mount in container.get("Mounts", []):
            if (isinstance(mount, dict)
                    and mount.get("Type") == "bind"
                    and _path_belongs_to(str(mount.get("Source", "")), jobs_root)):
                return True
        labels = (container.get("Config", {}).get("Labels", {}) or {})
        config_files = labels.get("com.docker.compose.project.config_files", "")
        if any(_path_belongs_to(path.strip(), jobs_root)
               for path in config_files.split(",")):
            return True
    return False


def acquire_run_lock(home: Path) -> None:
    """Take the per-machine runner lock or exit with a clear explanation.
    flock/msvcrt locks evaporate with the process — a crash can't strand a
    stale lock, so there is deliberately no timeout/cleanup machinery."""
    global _lock_handle
    home.mkdir(parents=True, exist_ok=True)
    path = home / "run.lock"
    fh = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            # msvcrt cannot reliably lock a byte beyond EOF. A brand-new
            # run.lock is empty, so materialize the byte before locking it.
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write("\0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            fh.seek(0)
            holder = fh.read().strip() or "unknown PID"
        except Exception:
            holder = "unknown PID"
        fh.close()
        sys.exit(
            f"another dradar run is already active on this machine ({holder}).\n"
            "Running two at once makes them race each other over the same "
            "claimed cells — the duplicate runs are rejected on upload and "
            "their quota is simply wasted. Wait for it to finish (or stop it), "
            "then re-run. To run one batch safely in parallel, use "
            "`dradar resume --batch-id ID --workers N`. To run several exact "
            "Honeypot batches on this machine, add each one through "
            "`dradar fleet add --batch-id ID --workers N|auto`.")
    fh.seek(0)
    fh.truncate()
    fh.write(f"PID {os.getpid()}")
    fh.flush()
    _lock_handle = fh


def sweep_orphan_compose(home: Path, assume_yes: bool) -> None:
    """Find pier-shaped compose projects that predate this run and offer to
    take them down. Only callable while holding this home's run lock. Project
    names are merely a first-pass filter; Docker mounts/config paths must also
    prove that the project belongs to this home. Every failure path is silent:
    this is a courtesy sweep, never a reason to block a real run."""
    try:
        proc = subprocess.run(
            ["docker", "compose", "ls", "--format", "json"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return
    if proc.returncode != 0:
        return
    try:
        listed = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return
    candidates = [p.get("Name", "") for p in listed
                  if _PIER_PROJECT_RE.fullmatch(p.get("Name", "") or "")]
    orphans = [name for name in candidates
               if _project_belongs_to_home(name, home)]
    if not orphans:
        return
    print(f"found {len(orphans)} leftover task container project(s) from a "
          "previous run — the agent inside may STILL be burning your quota, "
          "and nothing will ever collect its result:")
    for name in orphans:
        print(f"  - {name}")
    if not assume_yes:
        answer = input("stop and remove them now? [Y/n] ").strip().lower()
        if answer not in ("", "y", "yes"):
            print("left alone — you can clean them later with "
                  "`docker compose -p <name> down`")
            return
    for name in orphans:
        try:
            subprocess.run(["docker", "compose", "-p", name, "down",
                            "--remove-orphans"],
                           capture_output=True, timeout=180)
            print(f"  cleaned {name}")
        except (OSError, subprocess.TimeoutExpired):
            print(f"  couldn't clean {name} — try `docker compose -p {name} down`")
