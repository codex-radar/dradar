"""Environment preflight checks (`dradar doctor`): docker/pier/agent auth,
per-platform fix hints, and a live server-login probe.
"""

import ctypes
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, runner
from .capacity import docker_resources, worker_resource_warnings
from .identity import _client
from .local_config import _load_config, tasks_root_from_config
from .providers import (
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_OPENCODE_API_KEY_ENV,
    deepseek_api_key,
    deepseek_catalog_error,
    deepseek_opted_in,
    opencode_api_key,
    opencode_opted_in,
)
from .taskpacks import TaskPackError, ensure_benchmark_task_pack


def _check(label: str, ok: bool, hint: str = "") -> bool:
    mark = "ok " if ok else "FAIL"
    print(f"  [{mark}] {label}" + ("" if ok else f"  -> {hint}"))
    return ok


def _skip(label: str, reason: str) -> None:
    """Report a dependency that was deliberately not installed or downloaded."""
    print(f"  [skip] {label}  -> {reason}")


def _warn(label: str, reason: str) -> None:
    """Report a measurement risk without claiming the machine cannot run."""

    print(f"  [warn] {label}  -> {reason}")


def _platform(proc_version: Path = Path("/proc/version")) -> str:
    """Return the host family used to select platform-specific diagnostics."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        try:
            if "microsoft" in proc_version.read_text().lower():
                return "wsl"
        except OSError:
            pass
    return "linux"


# Per-platform fix hints for the environment checks. Volunteer machines are
# macOS / Linux / WSL2; these strings are the first thing a stuck volunteer
# sees, so they name the exact command for THEIR platform.
_DOCKER_HINTS = {
    "macos": {
        "cli": "install OrbStack: brew install --cask orbstack",
        "daemon": "start OrbStack: open -a OrbStack (or daemon is wedged)",
        "compose": "ln -s /Applications/OrbStack.app/Contents/MacOS/xbin/docker-compose "
                   "~/.docker/cli-plugins/docker-compose",
    },
    "linux": {
        "cli": "install Docker Engine: https://docs.docker.com/engine/install/ "
               "then add yourself to the docker group: sudo usermod -aG docker $USER (re-login)",
        "daemon": "sudo systemctl enable --now docker "
                  "(permission denied = you're not in the docker group yet)",
        "compose": "install the compose plugin: sudo apt install docker-compose-plugin "
                   "(or your distro's equivalent)",
    },
    "wsl": {
        "cli": "enable Docker Desktop's WSL integration (Settings > Resources > WSL "
               "integration), or install Docker Engine inside WSL: "
               "https://docs.docker.com/engine/install/ubuntu/",
        "daemon": "start Docker Desktop on Windows, or inside WSL: sudo service docker start",
        "compose": "update Docker Desktop (bundles compose v2), or: "
                   "sudo apt install docker-compose-plugin",
    },
    "windows": {
        "cli": "install Docker Desktop: winget install -e --id Docker.DockerDesktop",
        "daemon": "start Docker Desktop and switch it to Linux containers",
        "compose": "update Docker Desktop (it includes Docker Compose v2)",
    },
}

_CODEX_HINTS = {
    "macos": "brew install codex",
    "linux": "npm install -g @openai/codex",
    "wsl": "npm install -g @openai/codex",
    "windows": "PowerShell: irm https://chatgpt.com/codex/install.ps1 | iex",
}


def _probe(cmd: list[str]) -> bool:
    """Run a doctor probe; a wedged daemon must not hang doctor forever."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


_PF_VIRT_FIRMWARE_ENABLED = 21


def _windows_virtualization_state() -> str:
    """Best-effort native-Windows virtualization probe.

    Windows' language-neutral ``IsProcessorFeaturePresent`` API reports whether
    firmware virtualization is enabled *and available to the OS*. A false
    result is deliberately called ``unavailable`` rather than ``disabled``:
    BIOS/UEFI, Windows features, nested virtualization, or the host platform
    may each be responsible. If the API itself is unavailable, fail closed to
    ``unknown`` and retain the generic Docker guidance.
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        probe = kernel32.IsProcessorFeaturePresent
        probe.argtypes = [ctypes.c_uint32]
        probe.restype = ctypes.c_int
        available = bool(probe(_PF_VIRT_FIRMWARE_ENABLED))
    except (AttributeError, OSError, TypeError, ValueError):
        return "unknown"
    return "available" if available else "unavailable"


def _windows_daemon_hint(virtualization_state: str) -> str:
    if virtualization_state == "unavailable":
        return (
            "Windows reports that firmware virtualization is not available to "
            "the OS; check that Intel VT-x/Intel Virtualization Technology or "
            "AMD-V/SVM Mode is enabled in BIOS/UEFI, enable the required Windows "
            "backend (Virtual Machine Platform/WSL2, or Hyper-V when supported), "
            "enable nested virtualization when inside a VM, reboot, then start "
            "Docker Desktop in Linux-container mode"
        )
    return (
        "start Docker Desktop and switch it to Linux containers; if Docker "
        "Desktop says 'Virtualization support not detected', enable Intel "
        "VT-x or AMD-V/SVM in BIOS/UEFI, enable Virtual Machine Platform/WSL2 "
        "(or Hyper-V when supported), and reboot"
    )


def cmd_doctor(args) -> int:
    cfg = _load_config()
    plat = _platform()
    print(f"dradar {__version__} doctor ({plat})")
    if plat == "windows":
        print("  native Windows support is experimental — this check validates "
              "Docker Desktop (Linux containers), Pier, and the Codex CLI; "
              "WSL2 remains the established fallback")
    hints = _DOCKER_HINTS[plat]
    all_ok = True

    docker = shutil.which("docker")
    all_ok &= _check("docker CLI", bool(docker), hints["cli"])
    daemon_ready = False
    if docker:
        daemon_ready = _probe([docker, "info"])
        daemon_hint = hints["daemon"]
        if plat == "windows" and not daemon_ready:
            daemon_hint = _windows_daemon_hint(_windows_virtualization_state())
        all_ok &= _check("docker daemon", daemon_ready, daemon_hint)
        compose = _probe([docker, "compose", "version"])
        all_ok &= _check("docker compose plugin", compose, hints["compose"])
        if daemon_ready:
            cpus, memory_gib, probe_warnings = docker_resources()
            if cpus is not None and memory_gib is not None:
                resource_label = (
                    f"docker resources ({cpus} CPU / {memory_gib:.1f} GiB memory)"
                )
                resource_warnings = worker_resource_warnings(
                    1, cpus, memory_gib,
                )
                if resource_warnings:
                    detail = "; ".join(resource_warnings)
                    detail += (
                        "; low Docker VM resources can trigger agent retries "
                        "and distort benchmark results"
                    )
                    if plat == "macos":
                        detail += (
                            "; Colima users should prefer a dedicated DRadar "
                            "profile instead of resizing another project's VM"
                        )
                    _warn(resource_label, detail)
                else:
                    _check(resource_label, True)
            else:
                for warning in probe_warnings:
                    _warn("docker resources", warning)

    # Native Windows cannot run a task until Docker Desktop's Linux engine is
    # healthy. Avoid installing Pier or cloning the benchmark repository while
    # that prerequisite is blocked; this keeps doctor diagnostic/read-mostly
    # and prevents a large, unusable download. Other platforms retain their
    # established bootstrap behavior.
    windows_bootstrap_blocked = plat == "windows" and not daemon_ready

    # pier is auto-installed on `dradar go`; do it here too so doctor reflects
    # the ready state instead of a scary FAIL a volunteer (or an agent following
    # a runbook) then chases with the wrong fix.
    if windows_bootstrap_blocked:
        _skip("pier bootstrap", "fix the Docker daemon first; no installation attempted")
    else:
        try:
            runner.ensure_pier()
        except runner.RunnerError:
            pass
        pier = shutil.which("pier")
        pier_ready = bool(
            pier and runner._pier_version_compatible(runner._pier_version(pier)))
        all_ok &= _check("pier", pier_ready, runner.PIER_INSTALL_COMMAND)

    # Agent: you only need ONE family working. If the one you're set up for is
    # ready, say so and stay quiet about the other -- don't print a FAIL for
    # claude when you use codex (that false alarm is what sends an agent down a
    # rabbit hole). Only nag about specifics when NEITHER is ready.
    codex = shutil.which("codex")
    auth = runner.codex_auth_path()
    codex_ready = bool(codex) and auth.is_file()
    claude_ready = bool(shutil.which("claude")) and bool(runner.claude_oauth_token())
    deepseek_requested = deepseek_opted_in()
    deepseek_key_ready = bool(deepseek_api_key())
    opencode_requested = opencode_opted_in()
    opencode_key_ready = bool(opencode_api_key())
    catalog_issue = deepseek_catalog_error()
    catalog_ready = catalog_issue is None
    if deepseek_requested:
        all_ok &= _check(
            "DeepSeek API key — local provider credential",
            deepseek_key_ready,
            "run `dradar provider setup deepseek` in your own interactive "
            f"Terminal, or temporarily export {DEEPSEEK_API_KEY_ENV}",
        )
        all_ok &= _check(
            "DeepSeek Codex models.json — official catalog",
            catalog_ready,
            catalog_issue or "reinstall or upgrade dradar",
        )
        if deepseek_key_ready and catalog_ready:
            _check("DeepSeek V4 Flash — Codex provider ready", True)
    if opencode_requested:
        all_ok &= _check(
            "OpenCode Go API key — local provider credential",
            opencode_key_ready,
            "run `dradar provider setup opencode-go` in your own interactive "
            f"Terminal, or temporarily export {DEEPSEEK_OPENCODE_API_KEY_ENV}",
        )
        if opencode_key_ready and catalog_ready:
            _check("DeepSeek V4 Flash via OpenCode Go — Codex provider ready", True)
    if not (deepseek_requested or opencode_requested):
        if codex_ready:
            _check("codex — agent ready", True)
        elif claude_ready:
            _check("claude — agent ready", True)
        else:
            _check("codex CLI", bool(codex), _CODEX_HINTS[plat])
            _check("codex auth.json", auth.is_file(), "run: codex login")
            _check("claude CLI (alternative to codex)", bool(shutil.which("claude")),
                   "npm install -g @anthropic-ai/claude-code")
            _check("CLAUDE_CODE_OAUTH_TOKEN (alternative to codex)",
                   bool(runner.claude_oauth_token()),
                   "or: claude setup-token, then export CLAUDE_CODE_OAUTH_TOKEN each shell")
        all_ok &= (codex_ready or claude_ready)

    # The task repo is auto-cloned on `dradar go`; do it here too so a missing
    # checkout reports OK instead of a FAIL whose hint doesn't actually fix it.
    benchmark = cfg.get("benchmark") or "deep-swe"
    tasks_root = (tasks_root_from_config(cfg)
                  if benchmark == "deep-swe"
                  else tasks_root_from_config(cfg, benchmark))
    if not tasks_root.is_dir():
        if windows_bootstrap_blocked:
            _skip(
                "tasks_root download",
                "fix the Docker daemon first; no benchmark repository downloaded",
            )
        else:
            try:
                if benchmark == "deep-swe":
                    runner.ensure_tasks_root(tasks_root)
                elif cfg.get("server") and cfg.get("token"):
                    ensure_benchmark_task_pack(
                        _client(cfg), benchmark, tasks_root)
                else:
                    runner.ensure_tasks_root(tasks_root, benchmark)
            except (runner.RunnerError, TaskPackError):
                pass
    if tasks_root.is_dir() or not windows_bootstrap_blocked:
        all_ok &= _check(
            "tasks_root",
            tasks_root.is_dir(),
            "run `dradar go` once — it installs the selected task pack at "
            f"{tasks_root}",
        )

    free_gb = shutil.disk_usage(Path.home()).free / 1e9
    all_ok &= _check(f"disk free ({free_gb:.0f} GB)", free_gb > 20, "need >20GB for task images")

    if cfg.get("server") and cfg.get("token"):
        try:
            me = _client(cfg).whoami()
            all_ok &= _check(f"server login ({me['nickname']})", True)
        except Exception as exc:  # noqa: BLE001
            all_ok &= _check("server login", False, str(exc))
    else:
        all_ok &= _check("server login", False, "dradar login --server <url> --token <token>")

    print("all checks passed" if all_ok else "fix the FAIL items above, then re-run: dradar doctor")
    return 0 if all_ok else 1


__all__ = [
    "cmd_doctor", "_platform", "_check", "_warn", "_probe", "_DOCKER_HINTS",
    "_CODEX_HINTS", "_windows_virtualization_state",
]
