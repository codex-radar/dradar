"""Environment preflight checks (`dradar doctor`): docker/pier/agent auth,
per-platform fix hints, and a live server-login probe.
"""

import ctypes
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__, egress, runner
from .capacity import docker_resources, worker_resource_warnings
from .codebuddy_provider import (
    CODEBUDDY_AGENT,
    CODEBUDDY_CLI_VERSION,
    CODEBUDDY_MODEL,
    codebuddy_executable,
    codebuddy_runtime_image_error,
    codebuddy_version,
    credential_status as codebuddy_credential_status,
    managed_codebuddy_home,
)
from .identity import _client
from .local_config import _load_config, tasks_root_from_config
from .providers import (
    ANTIGRAVITY_AGENT,
    ANTIGRAVITY_CLI_VERSION,
    DEEPSEEK_API_KEY_ENV,
    GROK_CLI_VERSION,
    GROK_AGENT,
    KIMI_AGENT,
    KIMI_CLI_VERSION,
    ZCODE_AGENT,
    ZCODE_CLI_VERSION,
    prepare_antigravity_auth,
    antigravity_auth_path,
    deepseek_api_key,
    deepseek_catalog_error,
    deepseek_opted_in,
    grok_auth_error,
    grok_auth_path,
    grok_cli_path,
    grok_live_error,
    kimi_auth_error,
    kimi_auth_path,
    kimi_cli_path,
    kimi_live_error,
    parse_kimi_cli_version,
    parse_grok_cli_version,
    zcode_api_key,
    zcode_cli_error,
    zcode_cli_path,
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

_PIER_EGRESS_BASE_IMAGE = "docker.io/library/ubuntu:24.04"
_DOCKER_HUB_PROBE_TIMEOUT_SEC = 20


def _probe(cmd: list[str]) -> bool:
    """Run a doctor probe; a wedged daemon must not hang doctor forever."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _docker_hub_hint(output: str, platform: str) -> str:
    """Turn BuildKit/registry failures into an actionable volunteer hint."""

    lowered = output.lower()
    if platform == "macos":
        action = (
            "configure OrbStack/Docker Desktop to use the host proxy and a "
            "working DNS resolver, then restart it"
        )
    elif platform == "windows":
        action = (
            "configure Docker Desktop's proxy and DNS, then restart Docker "
            "Desktop in Linux-container mode"
        )
    elif platform == "wsl":
        action = (
            "configure Docker Desktop/WSL proxy and DNS, then restart the "
            "Linux Docker engine"
        )
    else:
        action = "configure the Docker daemon proxy and DNS, then restart Docker"
    if "x509:" in lowered and "certificate is valid for" in lowered:
        reason = "Docker Hub resolved to the wrong TLS endpoint (DNS/proxy interception)"
    elif "toomanyrequests" in lowered or "429 too many requests" in lowered:
        reason = "Docker Hub anonymous pull limit was reached; run `docker login`"
    elif "timed out" in lowered or "timeout" in lowered:
        reason = "Docker Hub authentication/registry access failed"
    elif any(marker in lowered for marker in (
        "failed to fetch anonymous token",
        "auth.docker.io",
        "registry-1.docker.io",
    )) and any(marker in lowered for marker in (
        "timeout", "timed out", "deadline exceeded", "connection refused",
        "network is unreachable", "no such host",
    )):
        reason = "Docker Hub authentication/registry access failed"
    else:
        reason = f"cannot inspect required image {_PIER_EGRESS_BASE_IMAGE}"
    return f"{reason}; {action}"


def _docker_hub_preflight(
    docker: str, platform: str,
) -> tuple[bool, str]:
    """Use Docker's own BuildKit path to validate Hub auth and base metadata.

    ``--check --pull`` resolves the real Pier sidecar base through the same
    daemon/proxy path used by ``docker compose build`` without downloading its
    layers or creating a container. Older engines that do not support build
    checks fall back to the CLI's non-downloading manifest inspection.
    """

    dockerfile = f"FROM {_PIER_EGRESS_BASE_IMAGE}\n"
    try:
        with tempfile.TemporaryDirectory(
            prefix="dradar-docker-hub-probe-",
        ) as context:
            proc = subprocess.run(
                [
                    docker, "build", "--check", "--pull", "--file", "-",
                    context,
                ],
                input=dockerfile,
                capture_output=True,
                text=True,
                timeout=_DOCKER_HUB_PROBE_TIMEOUT_SEC,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return False, _docker_hub_hint("Docker Hub request timed out", platform)
    except OSError as exc:
        return False, f"could not start Docker Hub preflight: {exc}"

    output = f"{proc.stdout}\n{proc.stderr}"
    unsupported = any(marker in output.lower() for marker in (
        "unknown flag: --check",
        "unknown flag: --pull",
        "unknown shorthand flag",
        "build checks require buildkit",
    ))
    if proc.returncode != 0 and unsupported:
        try:
            proc = subprocess.run(
                [docker, "manifest", "inspect", _PIER_EGRESS_BASE_IMAGE],
                capture_output=True,
                text=True,
                timeout=_DOCKER_HUB_PROBE_TIMEOUT_SEC,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, _docker_hub_hint(
                "Docker Hub request timed out", platform,
            )
        except OSError as exc:
            return False, f"could not start Docker Hub preflight: {exc}"
        output = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode == 0:
        return True, ""
    return False, _docker_hub_hint(output, platform)


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
    selected_agent = getattr(args, "agent", None)
    dsh_only = selected_agent == "dsh-minimal"
    grok_only = selected_agent == GROK_AGENT
    kimi_only = selected_agent == KIMI_AGENT
    zcode_only = selected_agent == ZCODE_AGENT
    antigravity_only = selected_agent == ANTIGRAVITY_AGENT
    codebuddy_only = selected_agent == CODEBUDDY_AGENT
    scopes = {
        "dsh-minimal": " — DSH Minimal",
        GROK_AGENT: " — Grok Build",
        KIMI_AGENT: " — Kimi Code",
        ZCODE_AGENT: " — ZCode",
        ANTIGRAVITY_AGENT: " — Google Antigravity",
        CODEBUDDY_AGENT: " — CodeBuddy HY4",
    }
    scope = scopes.get(selected_agent, "")
    print(f"dradar {__version__} doctor ({plat}{scope})")
    if plat == "windows":
        dependencies = (
            "Docker Desktop (Linux containers), uv/uvx, and the DeepSeek credential"
            if dsh_only else
            "Docker Desktop (Linux containers), Pier, and the Codex CLI"
        )
        print("  native Windows support is experimental — this check validates "
              f"{dependencies}; WSL2 remains the established fallback")
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
            try:
                legacy_egress = (
                    egress.egress_proxy_mode() == egress.EGRESS_PROXY_LEGACY_MODE
                )
            except egress.EgressProxyError:
                legacy_egress = False
            if legacy_egress:
                egress_ready, egress_hint = _docker_hub_preflight(docker, plat)
                egress_label = "legacy Pier egress build base (ubuntu:24.04)"
            else:
                egress_ready, egress_hint = egress.egress_proxy_preflight(
                    docker, plat,
                )
                egress_label = (
                    "container network for Pier — pinned egress image (amd64/arm64)"
                )
            all_ok &= _check(
                egress_label,
                egress_ready,
                egress_hint,
            )
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

    # DSH runs the published Pier release in an isolated uvx environment. It
    # deliberately does not require or alter the host Pier used by other agent
    # families.
    if dsh_only:
        uvx = runner._resolve_user_tool("uvx")
        all_ok &= _check(
            "uvx — isolated public DSH runner",
            bool(uvx),
            "install uv from https://docs.astral.sh/uv/getting-started/installation/",
        )
    # Pier is auto-installed on `dradar go`; do it here too so doctor reflects
    # the ready state instead of a scary FAIL a volunteer (or an agent following
    # a runbook) then chases with the wrong fix.
    elif windows_bootstrap_blocked:
        _skip("pier bootstrap", "fix the Docker daemon first; no installation attempted")
    else:
        try:
            runner.ensure_pier()
        except runner.RunnerError:
            pass
        pier = runner._resolve_user_tool("pier")
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
    grok_requested = grok_only or (
        selected_agent is None and grok_auth_path().exists()
    )
    grok = grok_cli_path() if grok_requested else None
    grok_cli_ready = False
    if grok:
        try:
            grok_version = subprocess.run(
                [grok, "--version"], capture_output=True, text=True, timeout=10,
            )
            grok_cli_ready = (
                grok_version.returncode == 0
                and parse_grok_cli_version(grok_version.stdout) == GROK_CLI_VERSION
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
    grok_oauth_issue = grok_auth_error() if grok_requested else None
    grok_live_issue = (
        grok_live_error(grok)
        if grok_cli_ready and grok_oauth_issue is None else None
    )
    grok_ready = (
        grok_cli_ready and grok_requested
        and grok_oauth_issue is None and grok_live_issue is None
    )
    kimi_requested = kimi_only or (
        selected_agent is None and kimi_auth_path().exists()
    )
    kimi = kimi_cli_path() if kimi_requested else None
    kimi_cli_ready = False
    if kimi:
        try:
            kimi_version = subprocess.run(
                [kimi, "--version"], capture_output=True, text=True, timeout=10,
            )
            kimi_cli_ready = (
                kimi_version.returncode == 0
                and parse_kimi_cli_version(kimi_version.stdout) == KIMI_CLI_VERSION
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
    kimi_oauth_issue = kimi_auth_error() if kimi_requested else None
    kimi_live_issue = (
        kimi_live_error(kimi)
        if kimi_cli_ready and kimi_oauth_issue is None else None
    )
    kimi_ready = (
        kimi_cli_ready and kimi_requested
        and kimi_oauth_issue is None and kimi_live_issue is None
    )
    zcode_key_ready = bool(zcode_api_key())
    zcode_requested = zcode_only or (selected_agent is None and zcode_key_ready)
    zcode_cli_issue = zcode_cli_error(zcode_cli_path()) if zcode_requested else None
    zcode_ready = zcode_requested and zcode_key_ready and zcode_cli_issue is None
    antigravity_requested = antigravity_only or (
        selected_agent is None and antigravity_auth_path().exists()
    )
    antigravity_issue = (
        prepare_antigravity_auth() if antigravity_requested else None
    )
    antigravity_ready = antigravity_requested and antigravity_issue is None
    codebuddy_requested = codebuddy_only or (
        selected_agent is None and managed_codebuddy_home().exists()
    )
    codebuddy_cli = codebuddy_executable() if codebuddy_requested else None
    codebuddy_cli_ready = bool(
        codebuddy_cli
        and codebuddy_version(codebuddy_cli) == CODEBUDDY_CLI_VERSION
    )
    codebuddy_credentials_ready, codebuddy_credential_detail = (
        codebuddy_credential_status()
        if codebuddy_requested else (False, "not configured")
    )
    codebuddy_image_issue = (
        codebuddy_runtime_image_error(docker)
        if codebuddy_requested and docker and daemon_ready else
        "Docker daemon is unavailable" if codebuddy_requested else None
    )
    codebuddy_ready = bool(
        codebuddy_requested
        and codebuddy_cli_ready
        and codebuddy_credentials_ready
        and codebuddy_image_issue is None
    )
    deepseek_requested = deepseek_opted_in()
    deepseek_key_ready = bool(deepseek_api_key())
    if dsh_only:
        all_ok &= _check(
            "DeepSeek API key — local provider credential",
            deepseek_key_ready,
            "run `dradar provider setup deepseek` in your own interactive "
            f"Terminal, or temporarily export {DEEPSEEK_API_KEY_ENV}",
        )
        if deepseek_key_ready:
            _check("DeepSeek V4 Flash / Pro / Vision — DSH Minimal agent ready", True)
    elif (
        grok_requested or kimi_requested or zcode_requested
        or antigravity_requested or codebuddy_requested
    ):
        if grok_requested:
            all_ok &= _check(
                f"Grok CLI {GROK_CLI_VERSION} — subscription runner",
                grok_cli_ready,
                "run `dradar provider setup grok` to prepare it automatically",
            )
            all_ok &= _check(
                "Grok subscription OAuth — dedicated DRadar slot",
                grok_oauth_issue is None,
                grok_oauth_issue or "run `dradar provider setup grok`",
            )
            if grok_cli_ready and grok_oauth_issue is None:
                all_ok &= _check(
                    "Grok 4.6 — live subscription access",
                    grok_live_issue is None,
                    grok_live_issue or "run `dradar provider setup grok`",
                )
            if grok_ready:
                _check("Grok 4.6 — subscription provider ready", True)
        if kimi_requested:
            all_ok &= _check(
                f"Kimi Code CLI {KIMI_CLI_VERSION} — subscription runner",
                kimi_cli_ready,
                "run `dradar provider setup kimi` to prepare it automatically",
            )
            all_ok &= _check(
                "Kimi subscription OAuth — dedicated DRadar slot",
                kimi_oauth_issue is None,
                kimi_oauth_issue or "run `dradar provider setup kimi`",
            )
            if kimi_cli_ready and kimi_oauth_issue is None:
                all_ok &= _check(
                    "Kimi K3 — live subscription access",
                    kimi_live_issue is None,
                    kimi_live_issue or "run `dradar provider setup kimi`",
                )
            if kimi_ready:
                _check("Kimi K3 — subscription provider ready", True)
        if zcode_requested:
            all_ok &= _check(
                f"ZCode CLI {ZCODE_CLI_VERSION} — pinned Coding Plan runner",
                zcode_cli_issue is None,
                zcode_cli_issue or "reinstall the verified official ZCode runtime",
            )
            all_ok &= _check(
                "ZCode Coding Plan API key — local provider credential",
                zcode_key_ready,
                "run `dradar provider setup zcode` in your own interactive Terminal",
            )
            if zcode_ready:
                _check("ZCode GLM-5.3 — Coding Plan provider ready", True)
        if antigravity_requested:
            all_ok &= _check(
                "Antigravity subscription OAuth — dedicated DRadar slot",
                antigravity_issue is None,
                antigravity_issue or "run `dradar provider setup antigravity`",
            )
            if antigravity_ready:
                _check(
                    f"Gemini 3.7 Flash low/medium/high — AGY CLI "
                    f"{ANTIGRAVITY_CLI_VERSION} provider ready",
                    True,
                )
        if codebuddy_requested:
            all_ok &= _check(
                f"CodeBuddy CLI {CODEBUDDY_CLI_VERSION} — subscription setup source",
                codebuddy_cli_ready,
                "install the reviewed official CLI and run "
                "`dradar provider setup codebuddy`",
            )
            all_ok &= _check(
                "CodeBuddy subscription login — isolated DRadar copy",
                codebuddy_credentials_ready,
                codebuddy_credential_detail,
            )
            all_ok &= _check(
                f"CodeBuddy Linux runtime {CODEBUDDY_CLI_VERSION} — pinned image",
                codebuddy_image_issue is None,
                codebuddy_image_issue or "run `dradar provider setup codebuddy`",
            )
            if codebuddy_ready:
                _check(
                    f"{CODEBUDDY_MODEL}@max/xhigh/medium — concurrent CodeBuddy "
                    "provider ready "
                    "(live access not consumed by doctor)",
                    True,
                )
    elif deepseek_requested:
        catalog_issue = deepseek_catalog_error()
        catalog_ready = catalog_issue is None
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
            _check("DeepSeek V4 Flash / Pro — Codex provider ready", True)
        if deepseek_key_ready:
            _check("DeepSeek V4 Flash / Pro / Vision — DSH Minimal agent ready", True)
    elif codex_ready:
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
    if (
        not dsh_only
        and not deepseek_requested
        and not grok_requested
        and not kimi_requested
        and not zcode_requested
        and not antigravity_requested
        and not codebuddy_requested
    ):
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

    retry = (
        f"dradar doctor --agent {selected_agent}"
        if selected_agent else "dradar doctor"
    )
    print("all checks passed" if all_ok else f"fix the FAIL items above, then re-run: {retry}")
    return 0 if all_ok else 1


__all__ = [
    "cmd_doctor", "_platform", "_check", "_warn", "_probe", "_DOCKER_HINTS",
    "_CODEX_HINTS", "_docker_hub_hint", "_docker_hub_preflight",
    "_windows_virtualization_state",
]
