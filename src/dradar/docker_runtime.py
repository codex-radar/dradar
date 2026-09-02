"""Provider-aware, bounded Docker recovery for website run plans.

Only two state transitions are permitted here:

* an installed engine may be started with a fixed, non-shell argv; and
* after a one-use user approval, a recommended engine may be installed from a
  trusted OS package manager and then started.

The module never changes Docker context/configuration, installs without an
approval, creates containers, or retries forever.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .local_config import HOME


START_LOCK_FILE = "docker-engine-start.lock"
INFO_TIMEOUT_SECONDS = 5
START_TIMEOUT_SECONDS = 20
INSTALL_TIMEOUT_SECONDS = 900
LOCK_WAIT_SECONDS = 60.0
LOCK_POLL_SECONDS = 0.1
READINESS_DELAYS = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 12.0)
_THREAD_LOCK = threading.Lock()


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    start_argv: tuple[str, ...]


@dataclass(frozen=True)
class Installer:
    provider: Provider
    label: str
    argv: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class Recovery:
    ready: bool
    code: str
    message: str
    requires_user_action: bool
    stage: str
    docker_path: str | None = None
    provider: str | None = None
    context: str | None = None
    attempted_start: bool = False
    attempted_install: bool = False
    install_required: bool = False


def host_family(proc_version: Path = Path("/proc/version")) -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        try:
            if "microsoft" in proc_version.read_text(encoding="utf-8").lower():
                return "wsl"
        except OSError:
            pass
    return "linux"


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, check=False, timeout=timeout,
    )


def _capture(argv: list[str], *, timeout: int = 5) -> str | None:
    try:
        result = _run(argv, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (result.stdout or "").strip()
    return value or None if result.returncode == 0 else None


def _which(name: str) -> str | None:
    return shutil.which(name)


def _exists(path: str | Path) -> bool:
    return Path(path).exists()


def _first_existing(paths: tuple[str, ...]) -> str | None:
    return next((path for path in paths if _exists(path)), None)


def docker_ready(docker: str) -> bool:
    try:
        result = _run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            timeout=INFO_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool((result.stdout or "").strip())


def _docker_version(docker: str) -> bool:
    try:
        result = _run([docker, "--version"], timeout=INFO_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "docker" in (result.stdout or "").lower()


def _docker_context(docker: str) -> tuple[str, str]:
    configured = os.environ.get("DOCKER_CONTEXT", "").strip()
    context = configured or _capture([docker, "context", "show"]) or "default"
    docker_host = os.environ.get("DOCKER_HOST", "").strip()
    endpoint = docker_host or _capture([
        docker, "context", "inspect", context,
        "--format", "{{.Endpoints.docker.Host}}",
    ]) or ""
    return context, endpoint


def _kind_from_context(family: str, context: str, endpoint: str) -> str | None:
    signal = f"{context} {endpoint}".lower().replace("_", "-")
    if "orbstack" in signal:
        return "orbstack"
    if "colima" in signal:
        return "colima"
    if "rancher-desktop" in signal or "rancher desktop" in signal:
        return "rancher-desktop"
    if (
        "desktop-linux" in signal or "docker-desktop" in signal
        or "dockerdesktop" in signal or "/.docker/desktop/" in signal
    ):
        return "docker-desktop"
    if "rootless" in signal or "/run/user/" in signal:
        return "rootless-docker"
    if endpoint.lower().startswith(("tcp://", "ssh://", "http://", "https://")):
        return None
    if context.lower() not in {"default", "env"}:
        return None
    if family in {"linux", "wsl"}:
        return "system-docker"
    return "docker-desktop"


def _app_provider(key: str, label: str, app: str, path: str) -> Provider | None:
    opener = _which("open")
    return (
        Provider(key, label, (opener, "-gj", "-a", app))
        if opener and _exists(path) else None
    )


def _mac_provider(kind: str) -> Provider | None:
    if kind == "orbstack":
        orb = _which("orb") or _first_existing((
            "/Applications/OrbStack.app/Contents/MacOS/bin/orb",
            "/Applications/OrbStack.app/Contents/MacOS/orb",
        ))
        return Provider(kind, "OrbStack", (orb, "start")) if orb else None
    if kind == "colima":
        colima = _which("colima")
        return Provider(kind, "Colima", (colima, "start")) if colima else None
    if kind == "rancher-desktop":
        rdctl = _which("rdctl")
        if rdctl and _exists("/Applications/Rancher Desktop.app"):
            return Provider(kind, "Rancher Desktop", (rdctl, "start"))
        return _app_provider(
            kind, "Rancher Desktop", "Rancher Desktop",
            "/Applications/Rancher Desktop.app",
        )
    if kind == "docker-desktop":
        return _app_provider(
            kind, "Docker Desktop", "Docker", "/Applications/Docker.app",
        )
    return None


def _windows_program(kind: str) -> str | None:
    program_files = os.environ.get("ProgramFiles", "C:/Program Files")
    if kind == "docker-desktop":
        relative = "Docker/Docker/Docker Desktop.exe"
    elif kind == "rancher-desktop":
        relative = "Rancher Desktop/Rancher Desktop.exe"
    else:
        return None
    windows = str(Path(program_files) / relative)
    wsl = str(Path("/mnt/c/Program Files") / relative)
    return _first_existing((windows, wsl))


def _service_loaded(systemctl: str, unit: str, *, user: bool = False) -> bool:
    argv = [systemctl]
    if user:
        argv.append("--user")
    argv.extend(["show", unit, "--property", "LoadState", "--value"])
    return _capture(argv) == "loaded"


def _linux_provider(kind: str) -> Provider | None:
    if kind == "rancher-desktop":
        rdctl = _which("rdctl")
        return Provider(kind, "Rancher Desktop", (rdctl, "start")) if rdctl else None
    systemctl = _which("systemctl")
    if not systemctl:
        return None
    if kind == "docker-desktop" and _service_loaded(
        systemctl, "docker-desktop", user=True,
    ):
        return Provider(
            kind, "Docker Desktop",
            (systemctl, "--user", "start", "docker-desktop"),
        )
    if kind == "rootless-docker" and _service_loaded(
        systemctl, "docker", user=True,
    ):
        return Provider(
            kind, "rootless Docker", (systemctl, "--user", "start", "docker"),
        )
    if kind == "system-docker" and _service_loaded(systemctl, "docker"):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            argv = (systemctl, "start", "docker")
        else:
            sudo = _which("sudo")
            if not sudo:
                return None
            argv = (sudo, "-n", systemctl, "start", "docker")
        return Provider(kind, "Docker Engine", argv)
    return None


def discover_provider(
    docker: str, *, family: str | None = None,
) -> tuple[Provider | None, str, str | None]:
    family = family or host_family()
    context, endpoint = _docker_context(docker)
    kind = _kind_from_context(family, context, endpoint)
    if kind is None:
        return None, context, None
    return _provider_for_kind(family, kind), context, kind


def _provider_for_kind(family: str, kind: str) -> Provider | None:
    if family == "macos":
        return _mac_provider(kind)
    if family in {"windows", "wsl"} and kind in {
        "docker-desktop", "rancher-desktop",
    }:
        executable = _windows_program(kind)
        label = "Docker Desktop" if kind == "docker-desktop" else "Rancher Desktop"
        if executable:
            return Provider(kind, label, (executable,))
        if family == "windows":
            return None
    return _linux_provider(kind)


def _installed_providers_without_context(family: str) -> list[Provider]:
    providers: list[Provider] = []
    if family == "macos":
        for kind in ("orbstack", "docker-desktop", "colima", "rancher-desktop"):
            provider = _mac_provider(kind)
            if provider:
                providers.append(provider)
    if family in {"windows", "wsl"}:
        for kind in ("docker-desktop", "rancher-desktop"):
            executable = _windows_program(kind)
            if executable:
                label = "Docker Desktop" if kind == "docker-desktop" else "Rancher Desktop"
                providers.append(Provider(kind, label, (executable,)))
    if family in {"linux", "wsl"}:
        for kind in ("rootless-docker", "system-docker", "docker-desktop", "rancher-desktop"):
            provider = _linux_provider(kind)
            if provider and all(existing.key != provider.key for existing in providers):
                providers.append(provider)
    return providers


def _installed_without_context(family: str) -> Provider | None:
    providers = _installed_providers_without_context(family)
    return providers[0] if providers else None


def _wsl_default_integration(
    *, family: str, context: str, endpoint: str, kind: str | None,
    installed: list[Provider],
) -> Provider | None:
    """Resolve WSL's shared default socket only when attribution is unique."""

    normalized_endpoint = endpoint.lower().rstrip("/")
    if not (
        family == "wsl"
        and context.lower() in {"default", "env"}
        and normalized_endpoint in {"unix:///var/run/docker.sock", "/var/run/docker.sock"}
        and kind == "system-docker"
        and len(installed) == 1
        and installed[0].key in {"docker-desktop", "rancher-desktop"}
    ):
        return None
    return installed[0]


def recommended_installer(family: str) -> Installer | None:
    if family == "macos":
        brew = _which("brew")
        provider = _mac_provider("orbstack") or Provider(
            "orbstack", "OrbStack", ("orb", "start"),
        )
        return (
            Installer(
                provider, "OrbStack", (brew, "install", "--cask", "orbstack"),
                "Homebrew official cask",
            ) if brew else None
        )
    if family in {"windows", "wsl"}:
        winget = _which("winget.exe") or _which("winget")
        provider = Provider(
            "docker-desktop", "Docker Desktop",
            ((_windows_program("docker-desktop") or "Docker Desktop.exe"),),
        )
        return (
            Installer(provider, "Docker Desktop", (
                winget, "install", "--exact", "--id", "Docker.DockerDesktop",
                "--source", "winget",
            ), "Microsoft WinGet official source") if winget else None
        )
    if family == "linux":
        apt = _which("apt-get")
        if not apt:
            return None
        systemctl = _which("systemctl") or "/usr/bin/systemctl"
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            prefix: tuple[str, ...] = ()
        else:
            pkexec = _which("pkexec")
            if pkexec:
                prefix = (pkexec,)
            else:
                sudo = _which("sudo")
                if not sudo:
                    return None
                prefix = (sudo, "-n")
        provider = Provider(
            "system-docker", "Docker Engine",
            (systemctl, "start", "docker") if not prefix else (
                *prefix, systemctl, "start", "docker",
            ),
        )
        return Installer(
            provider, "Docker Engine",
            (*prefix, apt, "install", "-y", "docker.io", "docker-compose-v2"),
            "operating-system signed package repository",
        )
    return None


def _trusted_docker_paths(family: str, provider: str) -> tuple[str, ...]:
    program_files = os.environ.get("ProgramFiles", "C:/Program Files")
    candidates: dict[tuple[str, str], tuple[str, ...]] = {
        ("macos", "orbstack"): (
            "/opt/homebrew/bin/docker", "/usr/local/bin/docker",
            "/Applications/OrbStack.app/Contents/MacOS/xbin/docker",
        ),
        ("macos", "docker-desktop"): (
            "/usr/local/bin/docker",
            "/Applications/Docker.app/Contents/Resources/bin/docker",
        ),
        ("macos", "rancher-desktop"): (
            "/usr/local/bin/docker",
            "/Applications/Rancher Desktop.app/Contents/Resources/resources/darwin/bin/docker",
        ),
        ("macos", "colima"): (
            "/opt/homebrew/bin/docker", "/usr/local/bin/docker",
        ),
        ("windows", "docker-desktop"): (
            str(Path(program_files) / "Docker/Docker/resources/bin/docker.exe"),
        ),
        ("windows", "rancher-desktop"): (
            str(Path(program_files) / "Rancher Desktop/resources/resources/win32/bin/docker.exe"),
        ),
        ("wsl", "docker-desktop"): (
            "/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe",
        ),
        ("wsl", "rancher-desktop"): (
            "/mnt/c/Program Files/Rancher Desktop/resources/resources/linux/bin/docker",
            "/mnt/c/Program Files/Rancher Desktop/resources/resources/win32/bin/docker.exe",
        ),
        ("linux", "system-docker"): ("/usr/bin/docker",),
        ("linux", "rootless-docker"): ("/usr/bin/docker", "/usr/local/bin/docker"),
        ("linux", "docker-desktop"): ("/usr/bin/docker", "/usr/local/bin/docker"),
        ("linux", "rancher-desktop"): ("/usr/bin/docker", "/usr/local/bin/docker"),
    }
    return candidates.get((family, provider), ())


def _installed_docker_path(family: str, provider: str) -> str | None:
    allowed = _trusted_docker_paths(family, provider)
    found = _which("docker")
    if found:
        try:
            resolved = Path(found).resolve(strict=True)
            if any(
                Path(candidate).exists()
                and resolved == Path(candidate).resolve(strict=True)
                for candidate in allowed
            ):
                return found
        except OSError:
            pass
    return _first_existing(allowed)


@contextmanager
def _thread_start_lock():
    acquired = _THREAD_LOCK.acquire(timeout=LOCK_WAIT_SECONDS)
    if not acquired:
        raise OSError("timed out waiting for Docker startup thread lock")
    try:
        yield
    finally:
        _THREAD_LOCK.release()


@contextmanager
def _startup_lock(path: Path):
    if path.parent.is_symlink() or (path.parent.exists() and not path.parent.is_dir()):
        raise OSError("unsafe startup lock directory")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = open(path, "a+b")
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                if os.name == "nt":  # pragma: no cover - Windows CI exercises this
                    import msvcrt

                    if path.stat().st_size == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    os.chmod(path, 0o600)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise OSError(
                        "timed out waiting for Docker startup process lock"
                    ) from exc
                time.sleep(LOCK_POLL_SECONDS)
        yield
    finally:
        try:
            if os.name == "nt":  # pragma: no cover
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _needs_permission(detail: str) -> bool:
    lowered = detail.lower()
    return any(value in lowered for value in (
        "password", "permission denied", "access is denied", "not permitted",
        "authentication", "uac", "polkit", "a terminal is required",
    ))


def _start_and_wait(
    docker: str, provider: Provider, *, context: str | None,
    installed: bool,
) -> Recovery:
    try:
        started = _run(list(provider.start_argv), timeout=START_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return Recovery(
            False, "docker_start_command_timeout",
            f"已识别 {provider.label}，但启动程序没有及时返回；题目尚未开始。",
            False, "start_command", docker, provider.key, context, True, installed,
        )
    except OSError:
        return Recovery(
            False, "docker_start_command_failed",
            f"已识别 {provider.label}，但无法执行它的启动程序；题目尚未开始。",
            True, "start_command", docker, provider.key, context, True, installed,
        )
    if started.returncode != 0:
        detail = "\n".join(filter(None, (started.stderr, started.stdout)))[:500]
        permission = _needs_permission(detail)
        reason = "需要完成一次系统权限授权" if permission else "启动程序返回失败"
        return Recovery(
            False, "docker_start_rejected",
            f"已识别 {provider.label}，但启动阶段失败（{reason}）；题目尚未开始。",
            permission, "start_command", docker, provider.key, context, True, installed,
        )
    for delay in READINESS_DELAYS:
        if delay:
            time.sleep(delay)
        if docker_ready(docker):
            return Recovery(
                True, "docker_started",
                f"已自动启动 {provider.label} 并确认 Docker 可用。",
                False, "ready", docker, provider.key, context, True, installed,
            )
    return Recovery(
        False, "docker_start_readiness_timeout",
        f"{provider.label} 已启动，但 Docker 在等待窗口内仍未就绪；题目尚未开始。",
        False, "readiness", docker, provider.key, context, True, installed,
    )


def ensure_docker(
    *, allow_install: bool = False, home: Path = HOME,
    family: str | None = None,
) -> Recovery:
    """Recover Docker once. All subprocesses are fixed argv and bounded."""
    family = family or host_family()
    docker = _which("docker")
    if docker and docker_ready(docker):
        return Recovery(
            True, "docker_ready", "Docker 已经在运行。", False, "ready", docker,
        )
    try:
        with _thread_start_lock(), _startup_lock(home / START_LOCK_FILE):
            docker = _which("docker")
            if docker and docker_ready(docker):
                return Recovery(
                    True, "docker_ready_after_wait", "Docker 已由另一次运行启动。",
                    False, "ready", docker,
                )
            if docker:
                provider, context, kind = discover_provider(
                    docker, family=family,
                )
                if provider is None:
                    installed_providers = _installed_providers_without_context(family)
                    integrated = None
                    if family == "wsl" and kind == "system-docker":
                        actual_context, endpoint = _docker_context(docker)
                        integrated = _wsl_default_integration(
                            family=family, context=actual_context,
                            endpoint=endpoint, kind=kind,
                            installed=installed_providers,
                        )
                    if integrated is not None:
                        return _start_and_wait(
                            docker, integrated, context=context, installed=False,
                        )
                    installed = installed_providers[0] if installed_providers else None
                    if kind is None or (
                        installed is not None and installed.key != kind
                    ):
                        return Recovery(
                            False, "docker_context_selection_required",
                            "当前 Docker 环境无法安全对应到可启动引擎；选择其他环境会改变运行语义，请先确认要使用的 Docker 环境。",
                            True, "provider_selection", docker,
                            installed.key if installed else None, context,
                        )
                    if installed is not None:
                        return Recovery(
                            False, "docker_provider_not_startable",
                            "当前 Docker 环境没有可安全自动启动的已安装引擎；题目尚未开始。",
                            True, "provider_detection", docker, kind, context,
                        )
                    # A standalone Docker CLI is not an installed engine. Fall
                    # through to the same explicit install-confirmation state
                    # as a machine with no CLI at all.
                else:
                    return _start_and_wait(
                        docker, provider, context=context, installed=False,
                    )

            if not docker:
                installed_provider = _installed_without_context(family)
                if installed_provider is not None:
                    trusted_docker = _installed_docker_path(
                        family, installed_provider.key,
                    )
                    if trusted_docker:
                        return _start_and_wait(
                            trusted_docker, installed_provider,
                            context=None, installed=False,
                        )
                    return Recovery(
                        False, "docker_cli_missing",
                        f"检测到 {installed_provider.label}，但无法从受信任位置验证 Docker 命令；需要修复现有安装后才能运行题目。",
                        True, "cli_detection", provider=installed_provider.key,
                    )
            installer = recommended_installer(family)
            if not allow_install:
                label = installer.label if installer else "受支持的 Docker 环境"
                return Recovery(
                    False, "docker_install_confirmation_required",
                    f"这台设备尚未安装可用的 Docker。是否安装推荐的 {label}？",
                    True, "install_confirmation", provider=(
                        installer.provider.key if installer else None
                    ), install_required=True,
                )
            if installer is None:
                return Recovery(
                    False, "docker_installer_unavailable",
                    "已获得安装许可，但这台设备没有受支持的官方安装工具；题目尚未开始。",
                    True, "installer_detection", install_required=True,
                )
            try:
                installed = _run(list(installer.argv), timeout=INSTALL_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                return Recovery(
                    False, "docker_install_timeout",
                    f"{installer.label} 安装没有在限定时间内完成；题目尚未开始。",
                    True, "install", provider=installer.provider.key,
                    attempted_install=True, install_required=True,
                )
            except OSError:
                return Recovery(
                    False, "docker_install_failed",
                    f"无法启动 {installer.label} 的受信任安装程序；题目尚未开始。",
                    True, "install", provider=installer.provider.key,
                    attempted_install=True, install_required=True,
                )
            if installed.returncode != 0:
                detail = "\n".join(filter(None, (installed.stderr, installed.stdout)))[:500]
                permission = _needs_permission(detail)
                reason = "需要完成系统权限或许可确认" if permission else "安装程序返回失败"
                return Recovery(
                    False, "docker_install_rejected",
                    f"{installer.label} 安装未完成（{reason}）；题目尚未开始。",
                    True, "install", provider=installer.provider.key,
                    attempted_install=True, install_required=True,
                )
            docker = _installed_docker_path(family, installer.provider.key)
            if not docker or not _docker_version(docker):
                return Recovery(
                    False, "docker_install_verification_failed",
                    f"{installer.label} 安装程序已结束，但未能验证官方 Docker 版本；题目尚未开始。",
                    True, "install_verification", provider=installer.provider.key,
                    attempted_install=True, install_required=True,
                )
            discovered = discover_provider(docker, family=family)[0]
            provider = (
                installer.provider
                if family == "linux"
                else discovered or _installed_without_context(family) or installer.provider
            )
            return _start_and_wait(
                docker, provider, context=None, installed=True,
            )
    except OSError:
        return Recovery(
            False, "docker_start_lock_failed",
            "本机无法安全协调 Docker 恢复；为避免重复安装或启动，题目尚未开始。",
            True, "coordination",
        )


__all__ = [
    "Installer", "Provider", "Recovery", "discover_provider", "docker_ready",
    "ensure_docker", "host_family", "recommended_installer",
]
