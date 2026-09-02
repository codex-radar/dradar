"""Safe lifecycle management for Docker images created by DRadar/Pier.

Pier's compose projects build task-specific images.  A normal ``compose
down`` can leave those tagged images behind, and high-throughput volunteers
quickly accumulate hundreds of gigabytes.  This module deliberately does not
use Docker's global prune commands: it records exact image references/IDs for
new DRadar trials and re-validates Compose ownership labels immediately before
removal.

Legacy Pier images can be discovered, but are never adopted by automatic GC;
they require the explicit ``cleanup --docker --all-task-images`` path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1
LEDGER_NAME = "image-cache.json"
LOCK_NAME = "image-cache.lock"
MAINTENANCE_STAMP_NAME = "image-cache-maintenance.stamp"
BUILDER_PREFIX = "dradar-task-"
SHARED_BUILDER_PREFIX = "dradar-cache-"
SHARED_BUILDER_LOCK_NAME = "shared-build-cache.lock"
SHARED_BUILDER_READY_STAMP_NAME = "shared-build-cache.ready.json"
SHARED_BUILDER_READY_STAMP_VERSION = 1
# A fresh stamp avoids repeating the expensive BuildKit daemon/bootstrap work
# for every harness, but it is deliberately finite so a daemon restart or a
# manually removed builder is revalidated and warmed again.
SHARED_BUILDER_READY_MAX_AGE_SEC = 6 * 60 * 60
SHARED_BUILDER_STATE_DIR_ENV = "DRADAR_SHARED_BUILDER_STATE_DIR"
BUILD_CACHE_MODES = frozenset({"isolated", "shared"})
DEFAULT_BUILD_CACHE_MODE = "isolated"
# docker-container BuildKit auto can select native, which copies a full
# rootfs for every Dockerfile RUN. Never accept that fallback.
ISOLATED_SNAPSHOTTERS = ("overlayfs", "fuse-overlayfs")
_BUILDER_BOOTSTRAP_TIMEOUT = 300
GIB = 1024 ** 3
DEFAULT_MIN_FREE_GIB = 25.0
_PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9-]*__[a-z0-9]{6,8}$")
_VALID_SERVICES = {"main", "pier-egress-proxy"}


class DockerUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class DockerImage:
    reference: str
    image_id: str
    project: str
    service: str
    unique_size: int
    containers: int
    created_at: str


@dataclass(frozen=True)
class CachePolicy:
    mode: str
    limit_bytes: int
    target_bytes: int
    min_free_bytes: int
    automatic: bool


@dataclass
class CleanupPlan:
    candidates: list[DockerImage]
    owned_references: set[str]
    protected: int
    estimated_reclaimable: int
    total_owned_bytes: int
    docker_available: bool = True
    note: str | None = None
    legacy_count: int = 0
    legacy_bytes: int = 0


@dataclass
class MaintenanceResult:
    removed: int = 0
    estimated_reclaimed: int = 0
    cache_bytes: int = 0
    limit_bytes: int = 0
    disk_free_bytes: int = 0
    host_disk_free_bytes: int | None = None
    allow_new_claims: bool = True
    note: str | None = None
    legacy_count: int = 0
    legacy_bytes: int = 0


@dataclass(frozen=True)
class TrialBuilderLease:
    name: str | None
    isolated: bool
    note: str | None = None
    # A shared builder is scoped to the current OS user's Docker context and
    # is selected only in the child Pier environment. It intentionally
    # survives one trial so BuildKit can reuse immutable layers for the next
    # task, even when Fleet gives each harness a separate DRADAR_HOME.
    reusable: bool = False
    # False when isolation was skipped on purpose (nested Docker, or neither
    # copy-on-write snapshotter could boot). Cleanup must not treat that as a
    # failed builder and block the next task.
    expected: bool = True


@dataclass
class TaskCleanupResult:
    removed_containers: int = 0
    removed_networks: int = 0
    removed_volumes: int = 0
    removed_images: int = 0
    estimated_reclaimed: int = 0
    builder_removed: bool = True
    success: bool = True
    note: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in release.lower()


def wsl_host_disk_free_bytes() -> int | None:
    """Read the Windows drive that physically stores this WSL distribution."""
    if not is_wsl():
        return None
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    distro = os.environ.get("WSL_DISTRO_NAME", "").strip()
    if not powershell or not distro:
        return None
    script = r"""
$entry = Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' |
  ForEach-Object { Get-ItemProperty $_.PSPath } |
  Where-Object { $_.DistributionName -eq $env:WSL_DISTRO_NAME } |
  Select-Object -First 1
if ($null -eq $entry) { exit 2 }
$base = [Environment]::ExpandEnvironmentVariables([string]$entry.BasePath)
if ($base -notmatch '^(?:\\\\\?\\)?([A-Za-z]):\\') { exit 3 }
$drive = Get-PSDrive -Name $Matches[1]
[Console]::Out.Write([string][int64]$drive.Free)
""".strip()
    try:
        proc = subprocess.run(
            [
                powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                "-Command", script,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "WSL_DISTRO_NAME": distro},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        value = int(proc.stdout.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def disk_free_bytes(home: Path) -> tuple[int | None, int | None]:
    """Return guest and, on WSL, physical Windows-host free bytes."""
    try:
        guest = int(shutil.disk_usage(home).free)
    except OSError:
        guest = None
    return guest, wsl_host_disk_free_bytes()


def disk_allows_new_tasks(home: Path, *, min_free_bytes: int) -> tuple[bool, str | None]:
    guest, host = disk_free_bytes(home)
    if guest is None:
        return False, "无法确认本机剩余磁盘空间"
    if guest < min_free_bytes:
        scope = "Ubuntu" if is_wsl() else "本机"
        return False, f"{scope}可用磁盘空间低于安全线"
    if is_wsl():
        if host is None:
            return False, "无法确认承载 Ubuntu 的 Windows 磁盘剩余空间"
        if host < min_free_bytes:
            return False, "承载 Ubuntu 的 Windows 磁盘空间低于安全线"
    return True, None


def running_in_container() -> bool:
    """Whether this process is already inside a container/pod.

    Nested Docker cannot create overlay whiteouts (character device 0/0), so
    fuse-overlayfs fails with ``operation not permitted`` even when BuildKit
    itself is privileged. Those hosts must use the daemon's default builder.
    """
    if os.environ.get("container"):
        return True
    if Path("/.dockerenv").is_file():
        return True
    try:
        text = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lowered = text.lower()
    return any(
        token in lowered
        for token in ("docker", "containerd", "kubepods", "lxc", "podman")
    )


def _sanitize_project(name: str) -> str:
    value = name.lower()
    if not re.match(r"^[a-z0-9]", value):
        value = "0" + value
    return re.sub(r"[^a-z0-9_-]", "-", value)


def trial_builder_name(home: Path, assignment_id: str) -> str:
    """Return a deterministic builder name scoped to one home and assignment."""
    identity = f"{home.resolve()}\0{assignment_id}".encode("utf-8", "surrogatepass")
    return BUILDER_PREFIX + hashlib.sha256(identity).hexdigest()[:20]


def shared_builder_name(home: Path) -> str:
    """Return the persistent BuildKit builder scoped to one OS user.

    The name is deterministic so concurrent workers converge on one builder,
    while the hash prevents a different OS user's builder from colliding on a
    shared Docker daemon.  It is never selected globally; callers pass it
    through ``BUILDX_BUILDER`` only to the Pier child. ``home`` remains in the
    signature for compatibility with the assignment-scoped helper, but is not
    the scope: Fleet deliberately uses separate DRADAR_HOME paths per harness.
    """

    # A Fleet uses one DRADAR_HOME per harness. Deliberately scope the shared
    # builder to the host OS user rather than that per-harness directory,
    # otherwise eight cars would still download the same immutable layers
    # eight times. Do not accept an arbitrary environment override here: the
    # OS user's home and Docker socket are the auditable isolation boundary;
    # task credentials never enter this name or the BuildKit cache.
    scope = Path.home()
    identity = f"{scope.resolve()}\0shared-build-cache".encode(
        "utf-8", "surrogatepass",
    )
    return SHARED_BUILDER_PREFIX + hashlib.sha256(identity).hexdigest()[:20]


def normalize_build_cache_mode(value: object) -> str:
    """Normalize the local build-cache policy without widening its scope."""

    mode = str(value or DEFAULT_BUILD_CACHE_MODE).strip().lower()
    return mode if mode in BUILD_CACHE_MODES else DEFAULT_BUILD_CACHE_MODE


def configured_build_cache_mode(cfg: dict | None) -> str:
    """Resolve the persisted policy; malformed config fails closed to isolated."""

    return normalize_build_cache_mode(
        (cfg or {}).get("build_cache_mode"),
    )


def _shared_builder_state_dir() -> Path:
    """Return the host-user state directory for the persistent builder.

    Fleet workers intentionally use different ``DRADAR_HOME`` directories,
    so this state must live outside those per-harness homes.  The optional
    environment override exists for tests and for an explicitly managed
    alternate local state root; it never carries credentials.
    """

    raw = os.environ.get(SHARED_BUILDER_STATE_DIR_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".dradar" / "build-cache"


def _shared_builder_ready_path() -> Path:
    return _shared_builder_state_dir() / SHARED_BUILDER_READY_STAMP_NAME


def _shared_builder_is_ready(name: str, *, now: float | None = None) -> bool:
    """Whether the bounded, non-sensitive bootstrap stamp is still fresh."""

    try:
        payload = json.loads(_shared_builder_ready_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SHARED_BUILDER_READY_STAMP_VERSION
        or payload.get("builder") != name
    ):
        return False
    try:
        bootstrapped_at = float(payload["bootstrapped_at"])
    except (KeyError, TypeError, ValueError):
        return False
    current = time.time() if now is None else float(now)
    age = current - bootstrapped_at
    return 0 <= age <= SHARED_BUILDER_READY_MAX_AGE_SEC


def _mark_shared_builder_ready(name: str) -> None:
    """Atomically write a tiny readiness stamp without any secret material."""

    state_dir = _shared_builder_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _shared_builder_ready_path()
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}"
    )
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "schema_version": SHARED_BUILDER_READY_STAMP_VERSION,
                    "builder": name,
                    "bootstrapped_at": time.time(),
                },
                stream,
            )
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _buildx_inspect_is_usable(result: subprocess.CompletedProcess) -> bool:
    """Treat an explicit non-running BuildKit status as needing bootstrap."""

    output = "\n".join((result.stdout or "", result.stderr or "")).lower()
    if "status:" not in output:
        # Older Docker/buildx versions and test doubles omit the status line;
        # a successful inspect is the best evidence available there.
        return True
    return bool(re.search(r"status:\s*running\b", output))


@contextmanager
def _shared_builder_lock() -> Iterator[None]:
    """Serialize shared-builder creation/bootstrap across all harness homes."""

    state_dir = _shared_builder_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state_dir / SHARED_BUILDER_LOCK_NAME
    fh = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _trial_builder_create_command(name: str, snapshotter: str) -> list[str]:
    return [
        "buildx", "create", "--name", name,
        "--driver", "docker-container",
        "--buildkitd-flags",
        f"--oci-worker-snapshotter={snapshotter}",
    ]


def _cow_snapshotter_failure(detail: str | None) -> str:
    suffix = f"：{detail[:160]}" if detail else ""
    return "隔离构建无法使用叠加文件系统" + suffix


def _ensure_named_builder(name: str, *, reusable: bool) -> str | None:
    """Create/refresh one overlay/fuse builder; never fall back to native."""

    existing = _run_docker(
        ["buildx", "inspect", name], timeout=30, allow_fail=True,
    )
    if existing.returncode == 0:
        # Assignment builders are disposable residue from a crashed attempt;
        # the shared builder is retained so its content-addressed cache
        # survives between tasks.
        if not reusable:
            _run_docker(["buildx", "rm", name], timeout=180)
            existing = subprocess.CompletedProcess(["buildx", "inspect", name], 1)
        elif _shared_builder_is_ready(name) and _buildx_inspect_is_usable(existing):
            return None

    if existing.returncode != 0:
        last_detail = None
        created_ok = False
        for snapshotter in ISOLATED_SNAPSHOTTERS:
            _run_docker(["buildx", "rm", name], timeout=180, allow_fail=True)
            created = _run_docker(
                _trial_builder_create_command(name, snapshotter),
                timeout=120, allow_fail=True,
            )
            if created.returncode != 0:
                last_detail = (created.stderr or created.stdout or "").strip()
                continue
            bootstrapped = _run_docker(
                ["buildx", "inspect", name, "--bootstrap"],
                timeout=_BUILDER_BOOTSTRAP_TIMEOUT, allow_fail=True,
            )
            if bootstrapped.returncode == 0:
                created_ok = True
                existing = bootstrapped
                break
            last_detail = (bootstrapped.stderr or bootstrapped.stdout or "").strip()
            _run_docker(["buildx", "rm", name], timeout=180, allow_fail=True)
        if not created_ok:
            # A shared create race is still possible; inspect before giving up.
            if reusable:
                raced = None
                for _ in range(8):
                    raced = _run_docker(
                        ["buildx", "inspect", name],
                        timeout=10,
                        allow_fail=True,
                    )
                    if raced.returncode == 0:
                        existing = raced
                        created_ok = True
                        break
                    time.sleep(0.5)
                if not created_ok:
                    return _cow_snapshotter_failure(last_detail)
            else:
                return _cow_snapshotter_failure(last_detail)

    if reusable:
        # Bootstrap only when the stamp is absent/stale or the builder was
        # recreated.  The host-user lock prevents eight workers from all
        # downloading the same BuildKit daemon/image metadata at once.
        if not _shared_builder_is_ready(name) or not _buildx_inspect_is_usable(
            existing,
        ):
            bootstrapped = _run_docker(
                ["buildx", "inspect", name, "--bootstrap"],
                timeout=180,
                allow_fail=True,
            )
            if bootstrapped.returncode != 0:
                detail = (bootstrapped.stderr or bootstrapped.stdout or "").strip()
                return (
                    "共享构建缓存空间未能启动"
                    + (f"：{detail[:160]}" if detail else "")
                )
            try:
                _mark_shared_builder_ready(name)
            except OSError as exc:
                # A missing readiness stamp is safe (the next worker will
                # bootstrap again), but never turn a successful Docker start
                # into a false runner failure because local bookkeeping could
                # not be written.
                print(
                    f"warning: shared BuildKit readiness stamp unavailable: {exc}",
                    file=sys.stderr,
                )
    return None


def _builder_proxy_is_safe(runtime: dict[str, str]) -> tuple[bool, str | None]:
    """Whether a dedicated bridge builder can use the configured build proxy.

    A loopback proxy is translated to ``host.docker.internal`` for normal
    Compose builds through an explicit host-gateway mapping. BuildKit's own
    daemon container does not inherit that mapping. Falling back for one task
    is safer than silently breaking a working volunteer setup; refill is then
    stopped after the task because its cache could not be isolated.
    """
    if runtime.get("DRADAR_EGRESS_UPSTREAM_HOST") == "host.docker.internal":
        return False, "本机代理暂时无法用于隔离的临时构建空间"
    raw = runtime.get("DRADAR_EGRESS_BUILD_PROXY")
    if not raw:
        return True, None
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return False, "本机代理地址无法安全用于临时构建空间"
    if parsed.username or parsed.password:
        # Never put proxy credentials into buildx driver options/process argv.
        return False, "带密码的本机代理无法安全传给临时构建空间"
    return True, None


def prepare_trial_builder(
    home: Path, *, assignment_id: str, runtime: dict[str, str] | None = None,
    mode: str = DEFAULT_BUILD_CACHE_MODE,
    force_default: bool = False,
) -> TrialBuilderLease:
    """Create a BuildKit builder for one assignment or a scoped shared cache.

    The builder is never selected globally. ``BUILDX_BUILDER`` is applied only
    to Pier's child environment.  ``isolated`` keeps the historical
    assignment-scoped lifecycle; ``shared`` keeps one host-user-scoped
    BuildKit state volume so immutable base/install layers can be reused by
    concurrent tasks.  No credential or task worktree is placed in that cache.
    The docker-container node must boot overlayfs or fuse-overlayfs; native
    full-rootfs copies are not used as a silent fallback. Nested Docker and
    hosts that cannot boot a copy-on-write snapshotter use the default builder
    instead of failing isolation cleanup.
    """
    mode = normalize_build_cache_mode(mode)
    runtime = runtime or {}
    if force_default:
        return TrialBuilderLease(
            None, False, "本题改用本机默认构建空间", expected=False,
        )
    safe, note = _builder_proxy_is_safe(runtime)
    if not safe:
        return TrialBuilderLease(None, False, note)
    if running_in_container():
        return TrialBuilderLease(
            None, False,
            "当前运行在容器内，隔离构建无法写入 overlay whiteout，本题改用本机默认构建空间",
            expected=False,
        )
    reusable = mode == "shared"
    name = shared_builder_name(home) if reusable else trial_builder_name(
        home, assignment_id,
    )
    try:
        version = _run_docker(["buildx", "version"], timeout=30, allow_fail=True)
        if version.returncode != 0:
            return TrialBuilderLease(
                None, False, "Docker 的临时构建空间功能不可用",
            )
        if reusable:
            # All harness homes converge on this host-user lock.  This keeps
            # builder creation/bootstrap outside the per-assignment lock but
            # still prevents a parallel cold-start herd.
            with _shared_builder_lock():
                failure = _ensure_named_builder(name, reusable=True)
        else:
            failure = _ensure_named_builder(name, reusable=False)
        if failure is not None:
            if failure.startswith("隔离构建无法使用叠加文件系统"):
                return TrialBuilderLease(None, False, failure, expected=False)
            return TrialBuilderLease(None, False, failure)
    except DockerUnavailable as exc:
        label = "共享构建缓存空间" if reusable else "临时构建空间"
        return TrialBuilderLease(None, False, f"{label}不可用：{exc}")
    except OSError as exc:
        label = "共享构建缓存空间" if reusable else "临时构建空间"
        return TrialBuilderLease(None, False, f"{label}本地锁不可用：{exc}")
    if reusable:
        return TrialBuilderLease(
            name, True, "共享 BuildKit 缓存已预热；仅限当前 OS 用户的 Docker 环境", True,
        )
    return TrialBuilderLease(name, True)


def remove_trial_builder(
    home: Path, assignment_id: str, *, mode: str = DEFAULT_BUILD_CACHE_MODE,
) -> tuple[bool, str | None]:
    """Remove only an assignment builder; retain a scoped shared cache."""

    if normalize_build_cache_mode(mode) == "shared":
        return True, None
    name = trial_builder_name(home, assignment_id)
    try:
        inspected = _run_docker(
            ["buildx", "inspect", name], timeout=30, allow_fail=True,
        )
        if inspected.returncode != 0:
            return True, None
        removed = _run_docker(
            ["buildx", "rm", name], timeout=180, allow_fail=True,
        )
    except DockerUnavailable as exc:
        return False, str(exc)
    if removed.returncode != 0:
        detail = (removed.stderr or removed.stdout or "Docker 拒绝清理").strip()
        return False, detail[:300]
    return True, None


def prune_shared_build_cache(
    home: Path, *, max_used_bytes: int,
) -> tuple[bool, str | None]:
    """Prune only DRadar's persistent shared builder to a bounded cap.

    The shared builder is intentionally retained between tasks, but it must
    have an explicit lifecycle. This helper never touches the default builder
    or another named builder: it first proves the deterministic, OS-user
    scoped name exists, then invokes BuildKit's own GC with a byte cap. The
    caller is responsible for refusing the operation while active assignments
    are running and for asking the user before the destructive invocation.
    """

    if (
        not isinstance(max_used_bytes, int)
        or isinstance(max_used_bytes, bool)
        or max_used_bytes <= 0
    ):
        return False, "共享构建缓存上限必须是正整数"
    name = shared_builder_name(home)
    try:
        inspected = _run_docker(
            ["buildx", "inspect", name], timeout=30, allow_fail=True,
        )
        if inspected.returncode != 0:
            return True, "共享 builder 尚未创建，无需清理"
        pruned = _run_docker(
            [
                "buildx", "prune", "--builder", name, "--force", "--all",
                "--max-used-space", str(max_used_bytes),
            ],
            timeout=600,
            allow_fail=True,
        )
    except DockerUnavailable as exc:
        return False, str(exc)
    if pruned.returncode != 0:
        detail = (pruned.stderr or pruned.stdout or "BuildKit 拒绝清理").strip()
        return False, detail[:300]
    detail = (pruned.stdout or "").strip().replace("\n", " ")
    return True, detail[:300] if detail else "共享构建缓存已按上限回收"


def _parse_size(value: object) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(value))
    text = str(value or "").strip()
    match = re.fullmatch(r"(-?[0-9.]+(?:e[+-]?[0-9]+)?)\s*([kmgt]?b)", text, re.I)
    if not match:
        return 0
    number = float(match.group(1))
    if number <= 0:
        return 0
    scale = {"b": 1, "kb": 1000, "mb": 1000 ** 2,
             "gb": 1000 ** 3, "tb": 1000 ** 4}[match.group(2).lower()]
    return int(number * scale)


def _run_docker(
    command: list[str], *, timeout: int = 60, allow_fail: bool = False,
) -> subprocess.CompletedProcess:
    docker = shutil.which("docker")
    if not docker:
        raise DockerUnavailable("Docker CLI not found")
    try:
        proc = subprocess.run(
            [docker, *command], capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerUnavailable(f"couldn't query Docker: {exc}") from exc
    if proc.returncode != 0 and not allow_fail:
        detail = (proc.stderr or proc.stdout or "Docker command failed").strip()
        raise DockerUnavailable(detail[:500])
    return proc


def _missing_ok_command(command: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a command tolerating only ``No such image`` failures.

    ``docker image inspect a b c`` exits non-zero as soon as any reference is
    gone, yet still prints valid JSON for the survivors. Real Docker faults
    (permission denied, daemon down, bad socket) look the same at the exit
    code level, so we must distinguish them by message: a missing reference
    is expected under concurrent cleanup and is swallowed, while every other
    failure still propagates as :class:`DockerUnavailable` so the caller can
    abort safely instead of mistaking a sick daemon for an empty cache.
    """
    proc = _run_docker(command, timeout=timeout, allow_fail=True)
    if proc.returncode == 0:
        return proc
    detail = (proc.stderr or proc.stdout or "Docker command failed").strip()
    lines = [line.strip().lower() for line in detail.splitlines() if line.strip()]
    # Fail closed when Docker reports a mixture of a stale tag and a real
    # daemon/socket/permission fault. A substring check would incorrectly
    # accept the whole command as soon as any one line said "No such image".
    if lines and all("no such image" in line for line in lines):
        return proc
    raise DockerUnavailable(detail[:500])


def _parse_inspect_payload(stdout: str, *, allow_empty: bool = False) -> list[dict]:
    """Parse ``docker image inspect`` JSON output into a list of dicts.

    Docker prints one JSON object per inspected image as a JSON array. A
    missing reference makes the whole command exit non-zero, but the images
    that *do* exist are still serialized to stdout, so we parse what we can
    instead of treating a single stale tag as a total failure.
    """
    if not stdout.strip():
        if allow_empty:
            return []
        raise DockerUnavailable("Docker returned empty image metadata")
    try:
        values = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DockerUnavailable("Docker returned malformed image metadata") from exc
    if (not isinstance(values, list)
            or any(not isinstance(value, dict) for value in values)):
        raise DockerUnavailable("Docker returned malformed image metadata")
    if not values and not allow_empty:
        raise DockerUnavailable("Docker returned empty image metadata")
    return values


def _df_images() -> list[dict]:
    proc = _run_docker(["system", "df", "-v", "--format", "{{json .}}"], timeout=120)
    for line in proc.stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("Images"), list):
            return [item for item in payload["Images"] if isinstance(item, dict)]
    raise DockerUnavailable("Docker returned no readable image inventory")


def _inspect(references: list[str]) -> dict[str, dict]:
    """Inspect references in bounded chunks and map every current RepoTag."""
    found: dict[str, dict] = {}
    for start in range(0, len(references), 80):
        chunk = references[start:start + 80]
        if not chunk:
            continue
        # ``docker image inspect a b c`` exits non-zero if even one reference
        # is missing, yet still emits valid JSON for the survivors. Query with
        # ``missing_ok`` so a stale tag is tolerated while a real Docker fault
        # (permission denied, daemon down) still propagates instead of being
        # mistaken for an empty cache. Only when the chunk yields nothing
        # usable do we fall back to inspecting references one by one, so a
        # single concurrently-deleted tag can never abort the whole batch.
        proc = _missing_ok_command(["image", "inspect", *chunk], timeout=120)
        values = _parse_inspect_payload(
            proc.stdout, allow_empty=proc.returncode != 0,
        )
        # When a missing-tag batch yielded no survivors, retry each reference
        # on its own so the references that still exist can be recovered.
        # ``_missing_ok_command`` only raises on a real Docker fault (permission
        # denied, daemon down); such a fault must propagate here rather than be
        # swallowed, otherwise a sick daemon would look like an empty cache and
        # the caller could prune the whole ledger by mistake.
        if not values and len(chunk) > 1:
            for reference in chunk:
                single = _missing_ok_command(
                    ["image", "inspect", reference], timeout=60,
                )
                values.extend(_parse_inspect_payload(
                    single.stdout, allow_empty=single.returncode != 0,
                ))
        for value in values:
            if not isinstance(value, dict):
                continue
            for tag in value.get("RepoTags") or []:
                if isinstance(tag, str):
                    found[tag] = value
    return found


def _inventory_rows() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for raw in _df_images():
        repository, tag = raw.get("Repository"), raw.get("Tag")
        if not repository or repository == "<none>" or not tag or tag == "<none>":
            continue
        rows[f"{repository}:{tag}"] = raw
    return rows


def _validated_image(reference: str, raw: dict, inspected: dict) -> DockerImage | None:
    config = inspected.get("Config") or {}
    labels = config.get("Labels") or {}
    project = labels.get("com.docker.compose.project")
    service = labels.get("com.docker.compose.service")
    if not isinstance(project, str) or not _PROJECT_RE.fullmatch(project):
        return None
    if service not in _VALID_SERVICES:
        return None
    if reference != f"{project}-{service}:latest":
        return None
    image_id = inspected.get("Id") or raw.get("ID")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        return None
    try:
        containers = int(raw.get("Containers") or 0)
    except (TypeError, ValueError):
        containers = 0
    return DockerImage(
        reference=reference,
        image_id=image_id,
        project=project,
        service=service,
        unique_size=_parse_size(raw.get("UniqueSize") or raw.get("Size")),
        containers=max(0, containers),
        created_at=str(raw.get("CreatedAt") or inspected.get("Created") or ""),
    )


def discover_pier_images() -> dict[str, DockerImage]:
    """Return only images whose current tag and Compose labels agree."""
    rows = _inventory_rows()
    possible = [ref for ref in rows if any(
        ref.endswith(f"-{service}:latest") for service in _VALID_SERVICES
    )]
    inspected = _inspect(possible)
    result: dict[str, DockerImage] = {}
    for reference in possible:
        value = inspected.get(reference)
        if value is None:
            continue
        image = _validated_image(reference, rows[reference], value)
        if image is not None:
            result[reference] = image
    return result


@contextmanager
def _ledger_lock(home: Path) -> Iterator[None]:
    home.mkdir(parents=True, exist_ok=True)
    path = home / LOCK_NAME
    fh = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _load_unlocked(home: Path) -> dict[str, dict]:
    path = home / LEDGER_NAME
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return {}
    records = payload.get("images")
    if not isinstance(records, dict):
        return {}
    return {str(key): value for key, value in records.items() if isinstance(value, dict)}


def load(home: Path) -> dict[str, dict]:
    with _ledger_lock(home):
        return _load_unlocked(home)


def claim_periodic_maintenance(
    home: Path, *, interval_seconds: float, now: float | None = None,
) -> bool:
    """Let at most one local worker start periodic cache maintenance.

    Long-running refill pools can stay alive for days, so their normal
    before/after-pool maintenance boundary may not arrive soon enough. The
    shared stamp is advanced before the Docker scan: concurrent workers then
    skip the same interval instead of creating an expensive inspection herd.
    A failed pass is retried at the next interval and never weakens the normal
    image ownership/protection checks.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be greater than zero")
    current = time.time() if now is None else float(now)
    stamp = home / MAINTENANCE_STAMP_NAME
    with _ledger_lock(home):
        try:
            previous = stamp.stat().st_mtime
        except FileNotFoundError:
            previous = None
        except OSError:
            return False
        if previous is not None and current - previous < interval_seconds:
            return False
        try:
            home.mkdir(parents=True, exist_ok=True)
            fd = os.open(stamp, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
            os.utime(stamp, (current, current))
            os.chmod(stamp, 0o600)
        except OSError:
            return False
    return True


def _save_unlocked(home: Path, records: dict[str, dict]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    path = home / LEDGER_NAME
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"schema_version": SCHEMA_VERSION, "images": records}, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def record_trial_images(
    home: Path, *, assignment_id: str, task_id: str, trial_name: str,
) -> int:
    """Record exact, label-validated images left by one completed Pier run."""
    project = _sanitize_project(trial_name)
    if not _PROJECT_RE.fullmatch(project):
        return 0
    expected = [f"{project}-main:latest", f"{project}-pier-egress-proxy:latest"]
    images = []
    for reference in expected:
        try:
            proc = _run_docker(["image", "inspect", reference], timeout=30)
            values = json.loads(proc.stdout)
        except (DockerUnavailable, json.JSONDecodeError):
            continue
        if not isinstance(values, list) or not values or not isinstance(values[0], dict):
            continue
        inspected = values[0]
        raw = {
            "ID": inspected.get("Id"),
            "UniqueSize": inspected.get("Size"),
            "Containers": 0,
            "CreatedAt": inspected.get("Created"),
        }
        image = _validated_image(reference, raw, inspected)
        if image is not None:
            images.append(image)
    if not images:
        return 0
    timestamp = _now()
    with _ledger_lock(home):
        records = _load_unlocked(home)
        for image in images:
            records[image.reference] = {
                "image_id": image.image_id,
                "project": image.project,
                "service": image.service,
                "assignment_id": assignment_id,
                "task_id": task_id,
                "last_used_at": timestamp,
            }
        _save_unlocked(home, records)
    return len(images)


def effective_policy(home: Path, cfg: dict) -> CachePolicy:
    mode = cfg.get("image_cache_mode", "balanced")
    if mode not in {"balanced", "metered", "disk"}:
        mode = "balanced"
    try:
        usage = shutil.disk_usage(home)
    except OSError:
        usage = shutil.disk_usage(Path.home())
    configured = cfg.get("image_cache_limit_gb")
    try:
        configured_gib = float(configured) if configured is not None else None
    except (TypeError, ValueError):
        configured_gib = None
    if configured_gib is not None and configured_gib > 0:
        limit = int(configured_gib * GIB)
    elif mode == "disk":
        limit = int(min(20, max(10, usage.total / GIB * 0.02)) * GIB)
    elif mode == "metered":
        limit = int(min(100, max(40, usage.total / GIB * 0.10)) * GIB)
    else:
        limit = int(min(50, max(20, usage.total / GIB * 0.05)) * GIB)
    target = int(limit * 0.75)
    return CachePolicy(
        mode=mode,
        limit_bytes=limit,
        target_bytes=target,
        min_free_bytes=_policy_min_free_bytes(),
        automatic=mode != "metered",
    )


def _policy_min_free_bytes() -> int:
    """vfs needs a much higher floor than overlay2; 25 GiB is not enough."""
    from .capacity import VFS_FIRST_WORKER_DISK_GIB, docker_storage_driver

    if docker_storage_driver() == "vfs":
        return int(VFS_FIRST_WORKER_DISK_GIB * GIB)
    return int(DEFAULT_MIN_FREE_GIB * GIB)


def _protected_projects(home: Path, protected_assignment_ids: set[str],
                        include_kept: bool) -> set[str]:
    from . import local_jobs, pending

    projects: set[str] = set()
    pending_entries = pending.load(home)
    pending_ids = {str(item.get("assignment_id")) for item in pending_entries
                   if item.get("assignment_id")}
    for entry in pending_entries:
        trial_dir = entry.get("trial_dir")
        if trial_dir:
            projects.add(_sanitize_project(Path(trial_dir).name))
    for item in local_jobs.scan(home):
        protected = (
            item.assignment_id in protected_assignment_ids
            or item.assignment_id in pending_ids
            or (local_jobs.is_kept(home, item.job_dir) and not include_kept)
        )
        if protected and item.trial_dir is not None:
            projects.add(_sanitize_project(item.trial_dir.name))
    # Legacy jobs may predate the image ledger. Their
    # directory still carries the assignment ID (a<32 hex>) and trial/project
    # name, so preserve that final recovery signal as well.
    jobs_root = home / "work" / "jobs"
    if jobs_root.is_dir():
        for job_dir in jobs_root.iterdir():
            if not job_dir.is_dir():
                continue
            job_name = job_dir.name
            assignment_id = None
            if job_name.startswith("a") and len(job_name) >= 33:
                candidate = job_name[1:33]
                if re.fullmatch(r"[0-9a-f]{32}", candidate):
                    assignment_id = candidate
            protect_job = (
                assignment_id in protected_assignment_ids
                or (job_dir / ".dradar-keep").is_file() and not include_kept
            )
            if protect_job:
                for trial_dir in job_dir.glob("*__*"):
                    if trial_dir.is_dir():
                        projects.add(_sanitize_project(trial_dir.name))
    return projects


def _estimate(images: list[DockerImage]) -> int:
    # Several compose tags can point at the same content-addressed image.
    # Count each image ID once; the number remains an estimate because Docker
    # may retain shared layers for unrelated tags/build cache.
    by_id: dict[str, int] = {}
    for image in images:
        by_id[image.image_id] = max(by_id.get(image.image_id, 0), image.unique_size)
    return sum(by_id.values())


def plan_cleanup(
    home: Path, *, protected_assignment_ids: set[str], include_kept: bool = False,
    include_legacy: bool = False,
) -> CleanupPlan:
    records = load(home)
    try:
        images = discover_pier_images()
    except DockerUnavailable as exc:
        return CleanupPlan([], set(records), 0, 0, 0, False, str(exc))
    protected_projects = _protected_projects(
        home, protected_assignment_ids, include_kept,
    )
    pending_ids = set()
    from . import pending
    for entry in pending.load(home):
        if entry.get("assignment_id"):
            pending_ids.add(str(entry["assignment_id"]))
    protected_ids = protected_assignment_ids | pending_ids
    candidates: list[DockerImage] = []
    protected = 0
    stale_records: list[str] = []
    owned_current: set[str] = set()
    for reference, record in records.items():
        image = images.get(reference)
        if image is None or image.image_id != record.get("image_id"):
            stale_records.append(reference)
            continue
        owned_current.add(reference)
        if (record.get("assignment_id") in protected_ids
                or image.project in protected_projects or image.containers > 0):
            protected += 1
            continue
        candidates.append(image)
    if stale_records:
        with _ledger_lock(home):
            latest = _load_unlocked(home)
            for reference in stale_records:
                latest.pop(reference, None)
            _save_unlocked(home, latest)
    legacy_images = [
        image for reference, image in images.items() if reference not in owned_current
    ]
    if include_legacy:
        for image in legacy_images:
            if image.project in protected_projects or image.containers > 0:
                protected += 1
                continue
            candidates.append(image)
    total_owned = _estimate([
        image for reference, image in images.items() if reference in owned_current
    ])
    candidates.sort(key=lambda image: (
        str(records.get(image.reference, {}).get("last_used_at") or image.created_at),
        image.reference,
    ))
    return CleanupPlan(
        candidates=candidates,
        owned_references=owned_current,
        protected=protected,
        estimated_reclaimable=_estimate(candidates),
        total_owned_bytes=total_owned,
        legacy_count=len(legacy_images),
        legacy_bytes=_estimate(legacy_images),
    )


def _remove_one(image: DockerImage) -> bool:
    """Revalidate tag, ID, labels and zero-container state, then untag."""
    try:
        rows = _inventory_rows()
        raw = rows.get(image.reference)
        if raw is None:
            return True
        inspected = _inspect([image.reference]).get(image.reference)
        if inspected is None:
            return False
        current = _validated_image(image.reference, raw, inspected)
        if current is None or current.image_id != image.image_id or current.containers > 0:
            return False
        _run_docker(["image", "rm", image.reference], timeout=180)
        return True
    except DockerUnavailable:
        return False


def remove_images(home: Path, images: list[DockerImage]) -> tuple[int, int]:
    removed = 0
    removed_images: list[DockerImage] = []
    for image in images:
        if _remove_one(image):
            removed += 1
            removed_images.append(image)
    if removed_images:
        with _ledger_lock(home):
            records = _load_unlocked(home)
            for image in removed_images:
                if records.get(image.reference, {}).get("image_id") == image.image_id:
                    records.pop(image.reference, None)
            _save_unlocked(home, records)
    return removed, _estimate(removed_images)


def remove_assignment_images(
    home: Path, *, assignment_id: str, project: str,
) -> tuple[int, int, str | None]:
    """Remove exact ledger-bound tags without scanning the whole Docker cache."""
    records = load(home)
    expected = {
        reference: record for reference, record in records.items()
        if record.get("assignment_id") == assignment_id
        and record.get("project") == project
    }
    removed: list[DockerImage] = []
    stale: set[str] = set()
    notes: list[str] = []
    for reference, record in expected.items():
        inspected_proc = _run_docker(
            ["image", "inspect", reference], timeout=60, allow_fail=True,
        )
        if inspected_proc.returncode != 0:
            detail = (inspected_proc.stderr or inspected_proc.stdout or "").lower()
            if "no such image" in detail:
                stale.add(reference)
                continue
            notes.append(f"{reference} 无法检查")
            continue
        try:
            values = json.loads(inspected_proc.stdout)
        except json.JSONDecodeError:
            notes.append(f"{reference} 返回了无效信息")
            continue
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            notes.append(f"{reference} 返回了无效信息")
            continue
        inspected = values[0]
        raw = {
            "ID": inspected.get("Id"),
            "UniqueSize": inspected.get("Size"),
            "Containers": 0,
            "CreatedAt": inspected.get("Created"),
        }
        image = _validated_image(reference, raw, inspected)
        if image is None or image.image_id != record.get("image_id"):
            notes.append(f"{reference} 归属发生变化")
            continue
        users = _run_docker([
            "ps", "-aq", "--filter", f"ancestor={image.image_id}",
        ], timeout=30)
        if users.stdout.split():
            notes.append(f"{reference} 仍被容器使用")
            continue
        deleted = _run_docker(
            ["image", "rm", reference], timeout=180, allow_fail=True,
        )
        if deleted.returncode != 0:
            notes.append(f"{reference} 删除失败")
            continue
        removed.append(image)
    cleared = stale | {item.reference for item in removed}
    if cleared:
        with _ledger_lock(home):
            latest = _load_unlocked(home)
            for reference in cleared:
                if latest.get(reference, {}).get("assignment_id") == assignment_id:
                    latest.pop(reference, None)
            _save_unlocked(home, latest)
    return len(removed), _estimate(removed), "；".join(notes) if notes else None


def _managed_trial_project(
    home: Path, job_dir: Path, trial_name: str,
) -> tuple[str, Path] | None:
    project = _sanitize_project(trial_name)
    if _PROJECT_RE.fullmatch(project) is None:
        return None
    try:
        jobs_root = (home / "work" / "jobs").resolve()
        resolved_job = job_dir.resolve()
        resolved_trial = (resolved_job / trial_name).resolve()
    except (OSError, RuntimeError):
        return None
    if resolved_job == jobs_root or jobs_root not in resolved_job.parents:
        return None
    if resolved_trial.parent != resolved_job or not resolved_trial.is_dir():
        return None
    return project, resolved_job


def _inspect_json(command: list[str], *, timeout: int = 60) -> list[dict]:
    proc = _run_docker(command, timeout=timeout)
    try:
        values = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise DockerUnavailable("Docker returned malformed resource metadata") from exc
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise DockerUnavailable("Docker returned malformed resource metadata")
    return values


def _container_owned_by_job(container: dict, project: str, job_dir: Path) -> bool:
    labels = (container.get("Config", {}).get("Labels", {}) or {})
    if labels.get("com.docker.compose.project") != project:
        return False
    mounts_owned = any(
        isinstance(mount, dict)
        and mount.get("Type") == "bind"
        and _path_belongs_to(str(mount.get("Source", "")), job_dir)
        for mount in container.get("Mounts", [])
    )
    configs_owned = any(
        _path_belongs_to(value.strip(), job_dir)
        for value in str(
            labels.get("com.docker.compose.project.config_files", ""),
        ).split(",")
        if value.strip()
    )
    return mounts_owned or configs_owned


def _path_belongs_to(path: str, root: Path) -> bool:
    if not path:
        return False
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _remove_project_runtime(project: str, job_dir: Path) -> tuple[int, int, int]:
    """Remove exact Compose runtime objects after proving trial ownership."""
    listed = _run_docker([
        "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}",
    ], timeout=30)
    container_ids = [
        value for value in listed.stdout.split()
        if re.fullmatch(r"[0-9a-f]{12,64}", value)
    ]
    owned_ids: list[str] = []
    if container_ids:
        containers = _inspect_json(["inspect", *container_ids], timeout=60)
        for container in containers:
            container_id = container.get("Id")
            if (
                isinstance(container_id, str)
                and any(container_id.startswith(value) for value in container_ids)
                and _container_owned_by_job(container, project, job_dir)
            ):
                owned_ids.append(container_id)
        if len(owned_ids) != len(container_ids):
            raise DockerUnavailable(
                "a Compose container matched the task name but not its exact job path"
            )
        if owned_ids:
            _run_docker(["rm", "-f", *owned_ids], timeout=180)

    removed_networks = 0
    networks = _run_docker([
        "network", "ls", "-q", "--filter",
        f"label=com.docker.compose.project={project}",
    ], timeout=30).stdout.split()
    if networks:
        inspected_networks = _inspect_json(
            ["network", "inspect", *networks], timeout=60,
        )
        exact_networks = [
            str(item.get("Id") or item.get("Name"))
            for item in inspected_networks
            if (item.get("Labels") or {}).get("com.docker.compose.project") == project
            and (item.get("Id") or item.get("Name"))
        ]
        if len(exact_networks) != len(networks):
            raise DockerUnavailable("Docker network ownership could not be confirmed")
        if exact_networks:
            _run_docker(["network", "rm", *exact_networks], timeout=120)
            removed_networks = len(exact_networks)

    removed_volumes = 0
    volumes = _run_docker([
        "volume", "ls", "-q", "--filter",
        f"label=com.docker.compose.project={project}",
    ], timeout=30).stdout.split()
    if volumes:
        inspected_volumes = _inspect_json(
            ["volume", "inspect", *volumes], timeout=60,
        )
        exact_volumes = [
            str(item.get("Name"))
            for item in inspected_volumes
            if (item.get("Labels") or {}).get("com.docker.compose.project") == project
            and item.get("Name")
        ]
        if len(exact_volumes) != len(volumes):
            raise DockerUnavailable("Docker volume ownership could not be confirmed")
        if exact_volumes:
            _run_docker(["volume", "rm", *exact_volumes], timeout=120)
            removed_volumes = len(exact_volumes)
    return len(owned_ids), removed_networks, removed_volumes


def cleanup_trial_resources(
    home: Path, *, assignment_id: str, job_dir: Path, trial_name: str,
    builder_isolated: bool, builder_reusable: bool = False,
    builder_name: str | None = None, keep_images: bool = False,
    builder_expected: bool = True,
) -> TaskCleanupResult:
    """Delete one settled task's exact Docker resources before any refill.

    No global prune is used. Runtime objects require the exact Compose project
    label plus a managed job path, images require the existing ID-validated
    ledger, and BuildKit cleanup removes only the per-assignment builder.
    """
    result = TaskCleanupResult()
    managed = _managed_trial_project(home, job_dir, trial_name)
    if managed is None:
        result.success = False
        result.note = "题目运行目录不属于 DRadar，未执行 Docker 清理"
        return result
    project, resolved_job = managed
    notes: list[str] = []
    try:
        (
            result.removed_containers,
            result.removed_networks,
            result.removed_volumes,
        ) = _remove_project_runtime(project, resolved_job)
    except DockerUnavailable as exc:
        result.success = False
        notes.append(f"运行环境清理失败：{exc}")

    if not keep_images:
        try:
            removed, reclaimed, image_note = remove_assignment_images(
                home, assignment_id=assignment_id, project=project,
            )
        except DockerUnavailable as exc:
            result.success = False
            notes.append(f"题目镜像检查失败：{exc}")
        else:
            result.removed_images = removed
            result.estimated_reclaimed = reclaimed
            if image_note:
                result.success = False
                notes.append(image_note)

    if builder_reusable:
        builder_removed, builder_note = remove_trial_builder(
            home, assignment_id, mode="shared",
        )
    else:
        # Preserve the narrow legacy call shape for embedders that provide a
        # small test double around the assignment-scoped cleanup helper.
        builder_removed, builder_note = remove_trial_builder(home, assignment_id)
    result.builder_removed = builder_removed
    if not builder_isolated:
        if builder_expected:
            result.success = False
            notes.append("本题未使用隔离的临时构建空间")
    elif not builder_removed:
        result.success = False
        notes.append(f"临时构建空间清理失败：{builder_note or 'unknown error'}")
    # A host-user-scoped shared builder is intentionally retained for the next
    # task; only this task's Compose objects/images are cleaned above.
    result.note = "；".join(notes) if notes else None
    return result


def automatic_maintenance(
    home: Path, cfg: dict, *, protected_assignment_ids: set[str],
) -> MaintenanceResult:
    policy = effective_policy(home, cfg)
    guest_free, host_free = disk_free_bytes(home)
    disk_known = guest_free is not None
    host_known = not is_wsl() or host_free is not None
    known_free = [value for value in (guest_free, host_free) if value is not None]
    disk_free = min(known_free) if known_free else policy.min_free_bytes
    plan = plan_cleanup(
        home, protected_assignment_ids=protected_assignment_ids,
        include_kept=False, include_legacy=False,
    )
    result = MaintenanceResult(
        cache_bytes=plan.total_owned_bytes,
        limit_bytes=policy.limit_bytes,
        disk_free_bytes=disk_free,
        host_disk_free_bytes=host_free,
        legacy_count=plan.legacy_count,
        legacy_bytes=plan.legacy_bytes,
    )
    if not plan.docker_available:
        result.note = plan.note
        result.allow_new_claims = (
            disk_known and host_known and disk_free >= policy.min_free_bytes
        )
        if not result.allow_new_claims:
            disk_reason = (
                "the Windows host disk could not be checked"
                if not host_known
                else "disk space is below the 25 GiB safety floor"
            )
            result.note = (
                f"{plan.note}; {disk_reason}, "
                "so no new task will be claimed"
            )
        return result
    pressure = disk_free < policy.min_free_bytes or not host_known or not disk_known
    over_limit = plan.total_owned_bytes > policy.limit_bytes
    if not pressure and not over_limit:
        return result
    if not policy.automatic:
        result.allow_new_claims = not pressure
        result.note = (
            "metered image-cache mode preserved Docker images; "
            "run `dradar cleanup --docker --dry-run` before claiming more"
        )
        return result
    goal = policy.target_bytes if over_limit else 0
    chosen: list[DockerImage] = []
    remaining = plan.total_owned_bytes
    reclaimed_ids: set[str] = set()
    needed_for_disk = max(0, policy.min_free_bytes - disk_free)
    estimated = 0
    for image in plan.candidates:
        if remaining <= goal and estimated >= needed_for_disk:
            break
        chosen.append(image)
        if image.image_id not in reclaimed_ids:
            reclaimed_ids.add(image.image_id)
            estimated += image.unique_size
            remaining = max(0, remaining - image.unique_size)
    removed, reclaimed = remove_images(home, chosen)
    result.removed = removed
    result.estimated_reclaimed = reclaimed
    result.cache_bytes = max(0, plan.total_owned_bytes - reclaimed)
    refreshed_guest, refreshed_host = disk_free_bytes(home)
    refreshed_known = [
        value for value in (refreshed_guest, refreshed_host) if value is not None
    ]
    if refreshed_known:
        result.disk_free_bytes = min(refreshed_known)
    result.host_disk_free_bytes = refreshed_host
    result.allow_new_claims = (
        refreshed_guest is not None
        and (not is_wsl() or refreshed_host is not None)
        and result.disk_free_bytes >= policy.min_free_bytes
    )
    if pressure and not result.allow_new_claims:
        if is_wsl() and refreshed_host is None:
            result.note = (
                "the Windows disk holding Ubuntu could not be checked; no new "
                "task will be claimed"
            )
        else:
            result.note = (
                "disk space is still below the 25 GiB safety floor; no new task "
                "will be claimed"
            )
    return result


def proxy_detected() -> bool:
    return any(os.environ.get(name) for name in (
        "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy",
    ))


def cmd_config_show(args) -> int:
    from . import local_config

    cfg = local_config._load_config()
    policy = effective_policy(local_config.HOME, cfg)
    configured = cfg.get("image_cache_limit_gb")
    source = "configured" if configured is not None else "automatic"
    print("local image-cache settings (credentials are never displayed):")
    print(f"  mode: {policy.mode}")
    print(f"  limit: {policy.limit_bytes / GIB:.1f} GiB ({source})")
    print(f"  cleanup target: {policy.target_bytes / GIB:.1f} GiB")
    print(f"  minimum free disk: {policy.min_free_bytes / GIB:.0f} GiB")
    configured_build_cache = cfg.get("build_cache_mode")
    if configured_build_cache is None:
        build_cache_display = "auto (isolated for 1 worker; shared for multi-worker)"
    else:
        build_cache_display = configured_build_cache_mode(cfg)
    print(f"  build cache mode: {build_cache_display}")
    build_timeout = cfg.get("environment_build_timeout_multiplier")
    print(
        "  environment build timeout multiplier: "
        f"{build_timeout if build_timeout is not None else '3 (default)'}"
    )
    print(f"  proxy environment detected: {'yes' if proxy_detected() else 'no'}")
    return 0


def cmd_config_set(args) -> int:
    from . import local_config

    cfg = local_config._load_config()
    if args.key == "environment-build-timeout-multiplier":
        try:
            value = float(args.value.strip())
        except (TypeError, ValueError) as exc:
            raise SystemExit(
                "environment-build-timeout-multiplier must be between 1 and 8"
            ) from exc
        if not (value == value and value not in (float("inf"), float("-inf"))
                and 1.0 <= value <= 8.0):
            raise SystemExit(
                "environment-build-timeout-multiplier must be between 1 and 8"
            )
        cfg["environment_build_timeout_multiplier"] = value
        shown = f"{value:g}"
    elif args.key == "build-cache-mode":
        value = args.value.strip().lower()
        if value not in BUILD_CACHE_MODES:
            raise SystemExit("build-cache-mode must be isolated or shared")
        cfg["build_cache_mode"] = value
        shown = value
    elif args.key == "image-cache-mode":
        value = args.value.strip().lower()
        if value not in {"balanced", "metered", "disk"}:
            raise SystemExit("image-cache-mode must be balanced, metered, or disk")
        cfg["image_cache_mode"] = value
        shown = value
    else:
        value = args.value.strip().lower()
        if value == "auto":
            cfg.pop("image_cache_limit_gb", None)
            shown = "automatic"
        else:
            try:
                limit = float(value)
            except ValueError as exc:
                raise SystemExit("image-cache-limit-gb must be a positive number or auto") from exc
            if limit <= 0:
                raise SystemExit("image-cache-limit-gb must be greater than zero")
            cfg["image_cache_limit_gb"] = limit
            shown = f"{limit:g} GiB"
    local_config._save_config(cfg)
    print(f"saved {args.key}={shown}")
    return 0


__all__ = [
    "CachePolicy", "CleanupPlan", "DockerImage", "DockerUnavailable",
    "MaintenanceResult", "TaskCleanupResult", "TrialBuilderLease",
    "automatic_maintenance", "cleanup_trial_resources", "discover_pier_images",
    "claim_periodic_maintenance", "disk_allows_new_tasks", "disk_free_bytes",
    "effective_policy", "is_wsl", "load", "plan_cleanup",
    "ISOLATED_SNAPSHOTTERS",
    "prepare_trial_builder", "proxy_detected", "record_trial_images",
    "running_in_container",
    "remove_assignment_images", "remove_images", "remove_trial_builder",
    "prune_shared_build_cache",
    "trial_builder_name", "shared_builder_name", "normalize_build_cache_mode",
    "configured_build_cache_mode", "BUILD_CACHE_MODES", "DEFAULT_BUILD_CACHE_MODE",
    "SHARED_BUILDER_READY_MAX_AGE_SEC", "SHARED_BUILDER_READY_STAMP_NAME",
    "wsl_host_disk_free_bytes", "cmd_config_set", "cmd_config_show",
]
