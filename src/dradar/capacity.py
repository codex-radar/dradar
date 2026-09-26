"""Conservative local worker recommendation.

The Docker engine's limits are more relevant than the host's headline specs:
Docker Desktop/OrbStack may expose only part of the host CPU and memory.  The
recommendation is deliberately a floor, not a benchmark claim.  A DeepSWE
build can spike far above its steady-state usage, so normal users never get an
automatic recommendation above four workers even on a very large machine.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


CPU_PER_WORKER = 2
MEM_GIB_PER_WORKER = 6
DOCKER_MEM_RESERVE_GIB = 2
FIRST_WORKER_DISK_GIB = 20
EXTRA_WORKER_DISK_GIB = 12
AUTO_WORKER_CAP = 4


@dataclass(frozen=True)
class CapacityReport:
    recommended_workers: int
    docker_cpus: int | None
    docker_memory_gib: float | None
    disk_free_gib: float
    account_limit: int
    held_tasks: int
    task_limit: int
    cpu_limit: int
    memory_limit: int
    disk_limit: int
    warnings: tuple[str, ...] = ()


def _docker_resources() -> tuple[int | None, float | None, tuple[str, ...]]:
    docker = shutil.which("docker")
    if not docker:
        return None, None, ("docker CLI not found; falling back to 1 worker",)
    try:
        proc = subprocess.run(
            [docker, "info", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None, ("cannot inspect Docker resources; falling back to 1 worker",)
    if proc.returncode != 0:
        return None, None, ("Docker daemon is unavailable; falling back to 1 worker",)
    try:
        info = json.loads(proc.stdout)
        cpus = int(info.get("NCPU") or 0)
        memory = int(info.get("MemTotal") or 0) / (1024 ** 3)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None, ("Docker returned unreadable capacity data; falling back to 1 worker",)
    if cpus < 1 or memory <= 0:
        return None, None, ("Docker omitted CPU/memory capacity; falling back to 1 worker",)
    return cpus, memory, ()


def docker_resources() -> tuple[int | None, float | None, tuple[str, ...]]:
    """Read the capacity exposed by Docker, not the host headline specs."""

    return _docker_resources()


def worker_resource_warnings(
    workers: int,
    docker_cpus: int | None,
    docker_memory_gib: float | None,
) -> tuple[str, ...]:
    """Explain when a requested worker count exceeds the published budget.

    These are warnings rather than hard failures: task footprints vary, and a
    user may deliberately accept slower execution.  Surfacing the shortfall is
    still important because memory/CPU pressure can change an agent's retry
    path even when the trial eventually completes.
    """

    if workers < 1:
        raise ValueError("workers must be positive")
    if docker_cpus is None or docker_memory_gib is None:
        return ()

    required_cpus = workers * CPU_PER_WORKER
    required_memory_gib = (
        DOCKER_MEM_RESERVE_GIB + workers * MEM_GIB_PER_WORKER
    )
    warnings = []
    if docker_cpus < required_cpus:
        warnings.append(
            f"{workers} worker(s) reserve {required_cpus} Docker CPU, but "
            f"the daemon exposes {docker_cpus}"
        )
    if docker_memory_gib < required_memory_gib:
        warnings.append(
            f"{workers} worker(s) reserve {required_memory_gib:.0f} GiB Docker "
            f"memory (including {DOCKER_MEM_RESERVE_GIB} GiB daemon reserve), "
            f"but the daemon exposes {docker_memory_gib:.1f} GiB"
        )
    return tuple(warnings)


def inspect_capacity(client, requested_tasks: int | None = None) -> CapacityReport:
    """Inspect Docker + disk + server limits without claiming any task."""
    me = client.whoami()
    assignment_data = client.get_assignment()
    active = assignment_data.get("active")
    if active is None:
        active = [assignment_data["assignment"]] if assignment_data.get("assignment") else []
    held = len(active)
    account_limit = max(1, int(
        me.get("concurrent_limit") or me.get("claim_limit") or 1))
    task_limit = max(1, int(requested_tasks or held or account_limit))

    cpus, memory_gib, warnings = docker_resources()
    disk_gib = shutil.disk_usage(Path.home()).free / (1024 ** 3)
    if cpus is None or memory_gib is None:
        cpu_limit = memory_limit = 1
    else:
        cpu_limit = max(1, cpus // CPU_PER_WORKER)
        memory_limit = max(
            1, int((memory_gib - DOCKER_MEM_RESERVE_GIB) // MEM_GIB_PER_WORKER))
    disk_limit = max(
        1,
        1 + int(max(0.0, disk_gib - FIRST_WORKER_DISK_GIB)
                // EXTRA_WORKER_DISK_GIB),
    )
    recommended = max(1, min(
        cpu_limit, memory_limit, disk_limit, account_limit, task_limit,
        AUTO_WORKER_CAP,
    ))
    return CapacityReport(
        recommended_workers=recommended,
        docker_cpus=cpus,
        docker_memory_gib=memory_gib,
        disk_free_gib=disk_gib,
        account_limit=account_limit,
        held_tasks=held,
        task_limit=task_limit,
        cpu_limit=cpu_limit,
        memory_limit=memory_limit,
        disk_limit=disk_limit,
        warnings=warnings,
    )


def print_report(report: CapacityReport) -> None:
    docker = "unavailable"
    if report.docker_cpus is not None and report.docker_memory_gib is not None:
        docker = (f"{report.docker_cpus} CPU / "
                  f"{report.docker_memory_gib:.1f} GiB memory")
    print("local worker capacity:")
    print(f"  Docker: {docker}")
    print(f"  disk free: {report.disk_free_gib:.0f} GiB")
    print(f"  account concurrency limit: {report.account_limit}")
    print(f"  currently held tasks: {report.held_tasks}")
    print("  safe ceilings by constraint:")
    print(f"    CPU: {report.cpu_limit} worker(s) "
          f"({CPU_PER_WORKER} CPU reserved per worker)")
    print(f"    memory: {report.memory_limit} worker(s) "
          f"({MEM_GIB_PER_WORKER} GiB per worker + "
          f"{DOCKER_MEM_RESERVE_GIB} GiB Docker reserve)")
    print(f"    disk: {report.disk_limit} worker(s) "
          f"({FIRST_WORKER_DISK_GIB} GiB first-worker reserve, "
          f"{EXTRA_WORKER_DISK_GIB} GiB each extra)")
    print(f"    account: {report.account_limit} worker(s)")
    print(f"    requested task capacity: {report.task_limit} worker(s)")
    print(f"    automatic safety cap: {AUTO_WORKER_CAP} worker(s)")
    for warning in report.warnings:
        print(f"  warning: {warning}")
    limits = {
        "CPU": report.cpu_limit,
        "memory": report.memory_limit,
        "disk": report.disk_limit,
        "account": report.account_limit,
        "requested tasks": report.task_limit,
        "automatic safety cap": AUTO_WORKER_CAP,
    }
    bottlenecks = ", ".join(
        name for name, limit in limits.items()
        if limit == report.recommended_workers
    )
    print(f"  limiting constraint(s): {bottlenecks}")
    print(f"recommended workers: {report.recommended_workers} "
          "(conservative; each task can spike during builds)")


def cmd_capacity(args) -> int:
    if getattr(args, "reservations", False):
        if getattr(args, "reconcile", None):
            raise SystemExit("capacity --reservations and --reconcile are mutually exclusive")
        from .legacy_capacity import cmd_legacy_inventory
        return cmd_legacy_inventory(args)
    if getattr(args, "reconcile", None):
        from .legacy_capacity import cmd_legacy_reconcile
        return cmd_legacy_reconcile(args)
    if any(getattr(args, name, None) for name in ("plan", "server", "after", "quarantine_after", "json", "reconcile")):
        raise SystemExit("inventory arguments require capacity --reservations")
    # Local imports avoid making a read-only machine probe participate in the
    # identity module's import graph.
    from .api_client import ApiError
    from .identity import _client
    from .local_config import _load_config

    try:
        # The standalone command answers "what can this machine safely run if
        # refill supplies work?", not merely "how many of today's held cells
        # exist?".  The actual resume path still clamps to its real task target.
        report = inspect_capacity(
            _client(_load_config()), requested_tasks=AUTO_WORKER_CAP)
    except ApiError as exc:
        raise SystemExit(f"capacity check failed: {exc}") from exc
    print_report(report)
    return 0


__all__ = [
    "CapacityReport", "cmd_capacity", "docker_resources", "inspect_capacity",
    "print_report", "worker_resource_warnings",
]
