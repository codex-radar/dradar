"""Interactive, local-only model-provider credential setup."""

from __future__ import annotations

import getpass
import hashlib
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import httpx

from .codebuddy_provider import (
    CODEBUDDY_CLI_VERSION,
    CODEBUDDY_CONTAINER_IMAGE,
    CODEBUDDY_MODEL,
    codebuddy_executable,
    codebuddy_runtime_image_error,
    codebuddy_subscription_session,
    codebuddy_version,
    ensure_codebuddy_runtime_image,
    import_host_login,
    managed_codebuddy_home,
)
from .codebuddy_provider import (
    credential_status as codebuddy_credential_status,
)
from .providers import (
    ANTIGRAVITY_CLI_VERSION,
    ANTIGRAVITY_LINUX_ARTIFACTS,
    ANTIGRAVITY_MODEL,
    ANTIGRAVITY_RUNTIME_MODELS,
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_MODELS,
    GROK_API_KEY_ENV,
    GROK_CLI_VERSION,
    GROK_MODEL,
    KIMI_API_KEY_ENVS,
    KIMI_BINARY_BASE_URL,
    KIMI_BINARY_SHA256,
    KIMI_CLI_VERSION,
    ZCODE_CLI_VERSION,
    ZCODE_MODELS,
    ZCODE_OFFICIAL_DOWNLOAD_PAGE,
    antigravity_auth_path,
    antigravity_home,
    antigravity_ready_path,
    deepseek_api_key,
    deepseek_credential_source,
    deepseek_secret_error,
    deepseek_secret_path,
    grok_auth_error,
    grok_auth_path,
    grok_cli_path,
    grok_home,
    grok_live_error,
    kimi_auth_error,
    kimi_auth_path,
    kimi_cli_path,
    kimi_home,
    kimi_live_error,
    managed_grok_cli_path,
    managed_kimi_cli_path,
    mark_antigravity_ready,
    parse_grok_cli_version,
    parse_kimi_cli_version,
    parse_zcode_cli_version,
    prepare_antigravity_auth,
    privatize_antigravity_home,
    provider_subprocess_env,
    restore_antigravity_settings,
    store_deepseek_api_key,
    store_grok_auth,
    store_zcode_api_key,
    store_zcode_cli,
    zcode_api_key,
    zcode_cli_error,
    zcode_cli_path,
    zcode_credential_source,
    zcode_secret_error,
    zcode_secret_path,
)

_DEEPSEEK_MODELS_URL = "https://api.deepseek.com/models"
_ZCODE_MODELS_URL = "https://open.bigmodel.cn/api/coding/paas/v4/models"
_GROK_INSTALLER_URL = "https://x.ai/cli/install.sh"
_ANTIGRAVITY_SETUP_IMAGE = "debian:bookworm-slim"
_ANTIGRAVITY_CA_BUNDLE_TARGET = "/tmp/dradar-ca-certificates.crt"


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _provider_proxy_for_url(url: str, env: dict[str, str]) -> str | None:
    """Resolve shell/macOS proxy settings while respecting NO_PROXY."""

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy")
    if no_proxy and urllib.request.proxy_bypass_environment(
        host, {"no": no_proxy},
    ):
        return None
    prefix = parsed.scheme.upper()
    value = (
        env.get(f"{prefix}_PROXY")
        or env.get(f"{prefix.lower()}_proxy")
        or env.get("ALL_PROXY")
        or env.get("all_proxy")
    )
    if isinstance(value, str) and value.lower().startswith("socks://"):
        value = "socks5://" + value[len("socks://"):]
    return value or None


def _provider_httpx_get(url: str, **kwargs):
    """GET through the same explicit/OS proxy contract as provider CLIs."""

    env = provider_subprocess_env()
    proxy = _provider_proxy_for_url(url, env)
    timeout = kwargs.pop("timeout", 10.0)
    follow_redirects = kwargs.pop("follow_redirects", False)
    with httpx.Client(
        proxy=proxy,
        trust_env=False,
        timeout=timeout,
        follow_redirects=follow_redirects,
    ) as client:
        return client.get(url, **kwargs)


def _grok_cli_version(executable: str | Path) -> str | None:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return parse_grok_cli_version(result.stdout + "\n" + result.stderr)


def _kimi_cli_version(executable: str | Path) -> str | None:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return parse_kimi_cli_version(result.stdout + "\n" + result.stderr)


def _install_managed_grok_cli() -> str | None:
    """Install DRadar's required Grok release into its private runtime slot."""

    bash = shutil.which("bash")
    if not bash:
        print(
            "Could not auto-install Grok: bash is unavailable. Install Git Bash "
            "on Windows, or bash on Linux/macOS, then retry."
        )
        return None
    target = managed_grok_cli_path()
    runtime = target.parent.parent
    _private_directory(runtime)
    _private_directory(target.parent)
    try:
        response = _provider_httpx_get(
            _GROK_INSTALLER_URL, timeout=30.0, follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        print(f"Could not download the official Grok installer: {type(exc).__name__}.")
        return None
    if response.status_code != 200 or not response.content.startswith(b"#!"):
        print(
            "Could not download the official Grok installer "
            f"(HTTP {response.status_code})."
        )
        return None
    try:
        with tempfile.TemporaryDirectory(prefix=".install-", dir=runtime) as name:
            script = Path(name) / "install.sh"
            script.write_bytes(response.content)
            if os.name != "nt":
                script.chmod(0o700)
            installer_home = runtime / "installer-home"
            _private_directory(installer_home)
            env = provider_subprocess_env()
            env["HOME"] = str(installer_home)
            env["GROK_BIN_DIR"] = str(target.parent)
            env["GROK_CHANNEL"] = "stable"
            for key in ("GROK_DEPLOYMENT_KEY", GROK_API_KEY_ENV):
                env.pop(key, None)
            print(f"Installing official Grok CLI {GROK_CLI_VERSION} for DRadar...")
            result = subprocess.run(
                [bash, str(script), GROK_CLI_VERSION], env=env, check=False,
            )
    except OSError as exc:
        print(f"Could not install Grok CLI: {exc}")
        return None
    if result.returncode != 0 or _grok_cli_version(target) != GROK_CLI_VERSION:
        print("The official Grok installer completed without a usable DRadar runtime.")
        return None
    return str(target)


def _install_managed_kimi_cli() -> str | None:
    """Install the reviewed official Kimi Code native bundle privately."""

    system = platform.system().lower()
    system = {
        "windows": "win32", "darwin": "darwin", "linux": "linux",
    }.get(system, system)
    machine = platform.machine().lower()
    if machine in {"aarch64", "arm64"}:
        arch = "arm64"
    elif machine in {"x86_64", "amd64"}:
        arch = "x64"
    else:
        arch = ""
    artifact = f"{system}-{arch}" if arch else ""
    expected = KIMI_BINARY_SHA256.get(artifact)
    if expected is None:
        print(f"Could not auto-install Kimi Code on {system}/{machine}.")
        return None
    target = managed_kimi_cli_path()
    runtime = target.parent.parent
    _private_directory(runtime)
    _private_directory(target.parent)
    print(f"Installing official Kimi Code CLI {KIMI_CLI_VERSION} for DRadar...")
    try:
        suffix = ".exe" if system == "win32" else ""
        response = _provider_httpx_get(
            f"{KIMI_BINARY_BASE_URL}/kimi-code-{artifact}{suffix}",
            timeout=60.0,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        print(f"Could not download official Kimi Code CLI: {type(exc).__name__}.")
        return None
    if response.status_code != 200:
        print(f"Could not download official Kimi Code CLI (HTTP {response.status_code}).")
        return None
    if hashlib.sha256(response.content).hexdigest() != expected:
        print("Official Kimi Code binary checksum mismatch; refusing to install it.")
        return None
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".kimi-", dir=target.parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(response.content)
            handle.flush()
            os.fsync(handle.fileno())
        temp = Path(temp_name)
        if os.name != "nt":
            temp.chmod(0o700)
        os.replace(temp, target)
    except OSError as exc:
        print(f"Could not install Kimi Code CLI: {exc}")
        if temp_name is not None:
            try:
                Path(temp_name).unlink()
            except OSError:
                pass
        return None
    if _kimi_cli_version(target) != KIMI_CLI_VERSION:
        print("The official Kimi installer completed without a usable DRadar runtime.")
        return None
    return str(target)


def _ensure_grok_cli() -> str | None:
    executable = grok_cli_path()
    found = _grok_cli_version(executable) if executable else None
    if found == GROK_CLI_VERSION:
        return executable
    if executable:
        print(
            f"Found Grok CLI {found or 'unknown'}; preparing current DRadar "
            f"runtime {GROK_CLI_VERSION} without changing the global install."
        )
    return _install_managed_grok_cli()


def _ensure_kimi_cli() -> str | None:
    executable = kimi_cli_path()
    found = _kimi_cli_version(executable) if executable else None
    if found == KIMI_CLI_VERSION:
        return executable
    if executable:
        print(
            f"Found Kimi Code CLI {found or 'unknown'}; preparing current DRadar "
            f"runtime {KIMI_CLI_VERSION} without changing the global install."
        )
    return _install_managed_kimi_cli()


def _antigravity_docker_arch(docker: str) -> str | None:
    try:
        result = subprocess.run(
            [docker, "info", "--format", "{{.Architecture}}"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip().lower()
    if value in {"amd64", "x86_64"}:
        return "x86_64"
    if value in {"arm64", "aarch64"}:
        return "aarch64"
    return None


def _managed_antigravity_linux_cli(arch: str) -> Path:
    return (
        antigravity_home() / "runtime" / ANTIGRAVITY_CLI_VERSION
        / arch / "antigravity"
    )


def _ensure_antigravity_linux_cli(docker: str) -> Path | None:
    """Download and verify Google's exact Linux artifact for Docker's arch."""

    arch = _antigravity_docker_arch(docker)
    artifact = ANTIGRAVITY_LINUX_ARTIFACTS.get(arch or "")
    if artifact is None:
        print("Antigravity setup needs a Docker Linux amd64 or arm64 engine.")
        return None
    target = _managed_antigravity_linux_cli(arch or "")
    if target.is_file():
        try:
            payload = target.read_bytes()
        except OSError:
            payload = b""
        # The manifest hash covers the tarball, not the extracted executable;
        # a small adjacent proof binds this cached runtime to that reviewed
        # archive without re-downloading it for every status check.
        proof = target.with_suffix(".sha512")
        try:
            proof_lines = proof.read_text(encoding="ascii").splitlines()
            expected_proof = [
                "archive=" + artifact["sha512"],
                "binary=" + hashlib.sha512(payload).hexdigest(),
            ]
            if proof_lines == expected_proof:
                if os.name != "nt":
                    target.chmod(0o700)
                if payload:
                    return target
        except OSError:
            pass
    _private_directory(target.parent)
    print(
        f"Downloading official Antigravity CLI {ANTIGRAVITY_CLI_VERSION} "
        f"for Docker {arch}..."
    )
    try:
        response = _provider_httpx_get(
            artifact["url"], timeout=120.0, follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        print(f"Could not download official Antigravity CLI: {type(exc).__name__}.")
        return None
    if response.status_code != 200:
        print(
            "Could not download official Antigravity CLI "
            f"(HTTP {response.status_code})."
        )
        return None
    if hashlib.sha512(response.content).hexdigest() != artifact["sha512"]:
        print("Official Antigravity archive checksum mismatch; refusing to use it.")
        return None
    try:
        with tempfile.TemporaryDirectory(prefix=".agy-", dir=target.parent) as name:
            archive = Path(name) / "antigravity.tar.gz"
            archive.write_bytes(response.content)
            with tarfile.open(archive, mode="r:gz") as bundle:
                members = bundle.getmembers()
                if (
                    len(members) != 1
                    or members[0].name != "antigravity"
                    or not members[0].isfile()
                ):
                    print("Official Antigravity archive has an unexpected layout.")
                    return None
                source = bundle.extractfile(members[0])
                if source is None:
                    return None
                fd, temp_name = tempfile.mkstemp(prefix=".antigravity-", dir=target.parent)
                temp = Path(temp_name)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        shutil.copyfileobj(source, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if os.name != "nt":
                        temp.chmod(0o700)
                    os.replace(temp, target)
                except BaseException:
                    try:
                        temp.unlink()
                    except OSError:
                        pass
                    raise
            proof = target.with_suffix(".sha512")
            installed = target.read_bytes()
            proof.write_text(
                "archive=" + artifact["sha512"] + "\n"
                "binary=" + hashlib.sha512(installed).hexdigest() + "\n",
                encoding="ascii",
            )
            if os.name != "nt":
                proof.chmod(0o600)
    except (OSError, tarfile.TarError) as exc:
        print(f"Could not install Antigravity CLI: {exc}")
        return None
    return target


def _antigravity_container_command(
    docker: str, executable: Path, arguments: list[str], *, interactive: bool,
) -> tuple[list[str], dict[str, str]]:
    auth = antigravity_auth_path()
    auth.mkdir(parents=True, exist_ok=True, mode=0o700)
    ca_bundle = _antigravity_ca_bundle()
    command = [docker, "run", "--rm"]
    if interactive:
        command.append("-i")
        if sys.stdin.isatty() and sys.stdout.isatty():
            command.append("-t")
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        command += ["--user", f"{os.getuid()}:{os.getgid()}"]
    command += [
        "--workdir", "/tmp",
        "--env", "HOME=/tmp/dradar-antigravity-user",
        "--env", "AGY_CLI_HIDE_LOGO=1",
        "--env", f"SSL_CERT_FILE={_ANTIGRAVITY_CA_BUNDLE_TARGET}",
        "--mount",
        f"type=bind,source={executable.resolve()},target=/opt/antigravity,readonly",
        "--mount",
        f"type=bind,source={auth.resolve()},target=/tmp/dradar-antigravity-user/.gemini",
        "--mount",
        (
            f"type=bind,source={ca_bundle},"
            f"target={_ANTIGRAVITY_CA_BUNDLE_TARGET},readonly"
        ),
    ]
    env = provider_subprocess_env()
    # A loopback proxy that is valid for the host points back at the setup
    # container itself. Honor DRadar's explicit, user-supplied Docker-side
    # endpoint when present; otherwise retain the existing host/OS fallback.
    if proxy := env.get("DRADAR_CONTAINER_HTTP_PROXY", "").strip():
        for name in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        ):
            env[name] = proxy
    if no_proxy := env.get("DRADAR_CONTAINER_NO_PROXY", "").strip():
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    ):
        if env.get(name):
            command += ["--env", name]
    for name in (
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
        "AGY_ADC_AUTH",
    ):
        env.pop(name, None)
    command += [_ANTIGRAVITY_SETUP_IMAGE, "/opt/antigravity", *arguments]
    return command, env


def _antigravity_ca_bundle() -> Path:
    """Return a canonical, non-empty CA bundle for the slim setup container."""

    try:
        bundle = Path(certifi.where()).resolve(strict=True)
        size = bundle.stat().st_size
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("the trusted CA bundle is unavailable") from exc
    if not bundle.is_file() or size <= 0:
        raise ValueError("the trusted CA bundle is not a non-empty regular file")
    try:
        ssl.create_default_context(cafile=str(bundle))
    except (OSError, ssl.SSLError) as exc:
        raise ValueError("the trusted CA bundle is not valid PEM") from exc
    return bundle


def _antigravity_models_live(docker: str, executable: Path) -> str | None:
    try:
        command, env = _antigravity_container_command(
            docker, executable, ["models"], interactive=False,
        )
    except (OSError, ValueError) as exc:
        return f"could not prepare the official models check: {type(exc).__name__}"
    proc = None
    run_issue = None
    try:
        proc = subprocess.run(
            command, env=env, capture_output=True, text=True,
            timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        run_issue = f"could not run the official models check: {type(exc).__name__}"
    try:
        # Even read-only AGY commands currently normalize away explicit false
        # settings.  Restore the reviewed fail-closed policy after every live
        # check so status/setup never weakens or invalidates the paid runtime.
        restore_antigravity_settings()
    except (OSError, ValueError) as exc:
        return f"the official models check left unsafe local state: {type(exc).__name__}"
    if run_issue is not None:
        return run_issue
    assert proc is not None
    if proc.returncode != 0:
        output = (proc.stdout + "\n" + proc.stderr).lower()
        if "authentication" in output or "oauth" in output:
            return "the isolated Google OAuth session was rejected"
        return "the official models check failed; check Docker/network/proxy"
    available = {
        line.split()[0]
        for line in proc.stdout.splitlines()
        if line.strip()
    }
    missing = set(ANTIGRAVITY_RUNTIME_MODELS.values()) - available
    if missing:
        return "the account cannot access " + ", ".join(sorted(missing))
    return None


def _setup_antigravity_subscription() -> int:
    docker = shutil.which("docker")
    if not docker:
        print("Antigravity setup needs Docker, which DRadar also uses for tasks.")
        return 1
    executable = _ensure_antigravity_linux_cli(docker)
    if executable is None:
        return 1
    if prepare_antigravity_auth() is None:
        issue = _antigravity_models_live(docker, executable)
        if issue is None:
            print(
                f"Antigravity subscription provider is already ready (CLI "
                f"{ANTIGRAVITY_CLI_VERSION}, {ANTIGRAVITY_MODEL} low/medium/high verified)."
            )
            return 0
    if not sys.stdin.isatty():
        print(
            "Antigravity OAuth setup needs an interactive terminal. Run:\n"
            "  dradar provider setup antigravity\n"
            "This uses Google's official OAuth flow in a DRadar-owned container; "
            "no API key is accepted."
        )
        return 2
    try:
        restore_antigravity_settings()
        command, env = _antigravity_container_command(
            docker,
            executable,
            [
                "--new-project", "--print", "/usage",
                "--output-format", "json", "--print-timeout", "10m",
            ],
            interactive=True,
        )
        print(
            "Starting official Google OAuth for the dedicated DRadar "
            "Antigravity slot. Complete the browser/code prompt shown below."
        )
        proc = subprocess.run(command, env=env, check=False)
    except (OSError, ValueError) as exc:
        print(f"could not start Antigravity login: {exc}")
        return 1
    if proc.returncode != 0:
        try:
            # Even an interrupted official login creates logs and local state.
            # Restore the reviewed settings and owner-only permissions before
            # returning so a failed setup never leaves a broad credential tree.
            restore_antigravity_settings()
        except (OSError, ValueError) as exc:
            print(f"Antigravity OAuth cleanup found unsafe local state: {exc}")
            return 1
        print("Antigravity OAuth login did not complete successfully.")
        return proc.returncode or 1
    try:
        # Login may add presentation preferences.  Replace only settings.json;
        # credentials live in separate files below the same private tree.
        restore_antigravity_settings()
    except (OSError, ValueError) as exc:
        print(f"Antigravity login returned unsafe local state: {exc}")
        return 1
    live_issue = _antigravity_models_live(docker, executable)
    if live_issue is not None:
        print(f"Antigravity login completed, but live model verification failed: {live_issue}.")
        return 1
    try:
        mark_antigravity_ready()
        privatize_antigravity_home()
    except (OSError, ValueError) as exc:
        print(f"could not seal Antigravity readiness state: {exc}")
        return 1
    issue = prepare_antigravity_auth()
    if issue is not None:
        print(f"Antigravity provider is not ready: {issue}")
        return 1
    print(
        f"Antigravity subscription OAuth is ready at {antigravity_auth_path()} "
        f"(tokens hidden, CLI {ANTIGRAVITY_CLI_VERSION}, three Gemini 3.7 Flash "
        "effort levels verified)."
    )
    return 0


def _status_antigravity_subscription(*, live: bool) -> int:
    issue = prepare_antigravity_auth()
    if issue is not None:
        print(f"Antigravity subscription provider not ready: {issue}")
        return 1
    if live:
        docker = shutil.which("docker")
        if not docker:
            print("Antigravity live status needs Docker.")
            return 1
        executable = _ensure_antigravity_linux_cli(docker)
        if executable is None:
            return 1
        issue = _antigravity_models_live(docker, executable)
        if issue is not None:
            print(f"Antigravity subscription provider not ready: {issue}.")
            return 1
    print(
        f"Antigravity subscription provider ready via {antigravity_auth_path()} "
        f"(OAuth tokens hidden, CLI {ANTIGRAVITY_CLI_VERSION}, "
        f"{ANTIGRAVITY_MODEL} low/medium/high, API keys disabled)."
    )
    return 0


def cmd_provider_setup(args) -> int:
    """Read a DeepSeek key without echoing it or placing it in argv/history."""

    if args.provider == "grok":
        return _setup_grok_subscription()
    if args.provider == "kimi":
        return _setup_kimi_subscription()
    if args.provider == "zcode":
        return _setup_zcode()
    if args.provider in {"agy", "antigravity"}:
        return _setup_antigravity_subscription()
    if args.provider in {"codebuddy", "hy4"}:
        return _setup_codebuddy_subscription()
    if args.provider != "deepseek":
        raise ValueError(f"unsupported provider: {args.provider}")
    if not sys.stdin.isatty():
        print(
            "DeepSeek setup needs an interactive terminal so the key can be "
            "entered with echo disabled. Open your own Terminal and run:\n"
            "  dradar provider setup deepseek\n"
            "Never paste the API key into Codex/chat or pass it as a command argument."
        )
        return 2
    key = getpass.getpass("DeepSeek API key (input hidden): ")
    try:
        path = store_deepseek_api_key(key)
    except (OSError, ValueError) as exc:
        print(f"could not save DeepSeek API key: {exc}")
        return 1
    print(
        f"DeepSeek API key saved locally at {path} (value hidden).\n"
        "It is not stored in config.json and is never sent to the DRadar server."
    )
    if _live_deepseek_status(key) != 0:
        print(
            "The credential remains saved, but it is not ready for a task yet. "
            "Fix the reported account/network issue, then run: "
            "dradar provider status deepseek --live"
        )
        return 1
    return 0


def cmd_provider_status(args) -> int:
    """Report credential readiness without printing secret material."""

    live = bool(getattr(args, "live", False))
    if args.provider == "grok":
        return _status_grok_subscription()
    if args.provider == "kimi":
        return _status_kimi_subscription(live=live)
    if args.provider == "zcode":
        return _status_zcode(live=live)
    if args.provider in {"agy", "antigravity"}:
        return _status_antigravity_subscription(live=live)
    if args.provider in {"codebuddy", "hy4"}:
        return _status_codebuddy_subscription(live=live)
    if args.provider != "deepseek":
        raise ValueError(f"unsupported provider: {args.provider}")
    path = deepseek_secret_path()
    error = deepseek_secret_error(path)
    if error is not None:
        print(f"DeepSeek provider not ready: {error}")
        return 1
    source = deepseek_credential_source()
    key = deepseek_api_key()
    if source == "environment" and key:
        print(f"DeepSeek provider configured via {DEEPSEEK_API_KEY_ENV} (value hidden).")
        return _live_deepseek_status(key) if live else 0
    if source == "file" and key:
        print(f"DeepSeek provider configured via {path} (value hidden).")
        return _live_deepseek_status(key) if live else 0
    print(
        "DeepSeek provider not configured. In your own interactive Terminal run:\n"
        "  dradar provider setup deepseek"
    )
    return 1


def _setup_codebuddy_subscription() -> int:
    executable = codebuddy_executable()
    version = codebuddy_version(executable)
    if version != CODEBUDDY_CLI_VERSION:
        print(
            f"CodeBuddy CLI {CODEBUDDY_CLI_VERSION} is required; found "
            f"{version or 'none'}. Install that reviewed official release, "
            "complete CodeBuddy login in your own interactive Terminal, then retry."
        )
        return 1
    try:
        target = import_host_login()
    except (OSError, ValueError) as exc:
        print(
            "CodeBuddy login could not be imported. Complete the official "
            "CodeBuddy login in your own interactive Terminal, then run "
            f"`dradar provider setup codebuddy` again: {exc}"
        )
        return 2
    ok, detail = codebuddy_credential_status()
    if not ok:
        print(f"CodeBuddy provider not ready: {detail}")
        return 1
    docker = shutil.which("docker")
    if not docker:
        print("CodeBuddy setup needs Docker, which DRadar also uses for tasks.")
        return 1
    try:
        image = ensure_codebuddy_runtime_image(docker)
    except ValueError as exc:
        print(f"CodeBuddy login was imported, but runtime preparation failed: {exc}")
        return 1
    print(
        f"CodeBuddy subscription login imported to {target} (values hidden).\n"
        f"Pinned Linux runtime ready as {image}; model {CODEBUDDY_MODEL}, "
        "isolated concurrent task sessions enabled."
    )
    print(
        "No provider request was made. To spend one minimal probe request and "
        "verify HY4 access, run: dradar provider status codebuddy --live"
    )
    return 0


def _status_codebuddy_subscription(*, live: bool) -> int:
    executable = codebuddy_executable()
    version = codebuddy_version(executable)
    if version != CODEBUDDY_CLI_VERSION:
        print(
            f"CodeBuddy provider not ready: CLI {CODEBUDDY_CLI_VERSION} required, "
            f"found {version or 'none'}."
        )
        return 1
    ok, detail = codebuddy_credential_status()
    if not ok:
        print(
            f"CodeBuddy provider not ready: {detail}. Run "
            "`dradar provider setup codebuddy` after completing official login."
        )
        return 1
    issue = codebuddy_runtime_image_error()
    if issue is not None:
        print(
            f"CodeBuddy provider not ready: {issue}. Run "
            "`dradar provider setup codebuddy` to rebuild the reviewed runtime."
        )
        return 1
    print(
        f"CodeBuddy provider ready via {managed_codebuddy_home()} "
        f"({detail}, CLI {CODEBUDDY_CLI_VERSION}, model {CODEBUDDY_MODEL}, "
        "API keys disabled, concurrent task sessions enabled)."
    )
    return _live_codebuddy_status() if live else 0


def _live_codebuddy_status() -> int:
    """Spend one minimal, tool-free request to verify container HY4 access."""

    docker = shutil.which("docker")
    if not docker:
        print("CodeBuddy live status needs Docker.")
        return 1
    prompt = (
        "Reply with exactly DRADAR_CODEBUDDY_AUTH_OK and nothing else. "
        "Do not use tools."
    )
    script = r"""
set -euo pipefail
install -d -m 700 /tmp/codebuddy-config/local_storage /tmp/codebuddy-home
install -d -m 700 \
  /tmp/codebuddy-home/.local/share/CodeBuddyExtension/Data/Public/auth
cp /run/secrets/codebuddy-login/local_storage/entry_*.info \
  /tmp/codebuddy-config/local_storage/
cp /run/secrets/codebuddy-login/auth/*.info \
  /tmp/codebuddy-home/.local/share/CodeBuddyExtension/Data/Public/auth/
chmod 600 /tmp/codebuddy-config/local_storage/entry_*.info
chmod 600 /tmp/codebuddy-home/.local/share/CodeBuddyExtension/Data/Public/auth/*.info
printf '%s\n' '{"mcpServers":{}}' > /tmp/codebuddy-empty-mcp.json
export HOME=/tmp/codebuddy-home
export CODEBUDDY_CONFIG_DIR=/tmp/codebuddy-config
export CODEBUDDY_CODE_ENABLE_TELEMETRY=0
export CODEBUDDY_DISABLE_AUTO_MEMORY=1
export CODEBUDDY_CODE_DISABLE_AUTO_MEMORY=1
export CODEBUDDY_DISABLE_BACKGROUND_TASKS=1
export CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS=1
export CODEBUDDY_DISABLE_CRON=1
export CODEBUDDY_DISABLE_IDE=1
export CODEBUDDY_SKIP_BUILTIN_MARKETPLACE=1
export DISABLE_AUTOUPDATER=1
exec /opt/codebuddy/bin/codebuddy --print --output-format text \
  --model hy4-preview --effort minimal --permission-mode bypassPermissions \
  --tools "" --strict-mcp-config --mcp-config /tmp/codebuddy-empty-mcp.json \
  --setting-sources user --no-session-persistence --max-turns 1 "$1"
""".strip()
    print(
        "Starting one minimal CodeBuddy HY4 access probe "
        "(this consumes provider usage)..."
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix="dradar-codebuddy-probe-",
        ) as temporary:
            with codebuddy_subscription_session(Path(temporary)) as login_root:
                command = [
                    docker, "run", "--rm", "--pull", "never",
                    "--mount",
                    "type=bind,source="
                    f"{login_root.resolve()},target=/run/secrets/codebuddy-login,"
                    "readonly",
                    CODEBUDDY_CONTAINER_IMAGE,
                    "/bin/bash", "-lc", script,
                    "dradar-codebuddy-probe", prompt,
                ]
                probed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"CodeBuddy live probe failed: {type(exc).__name__}.")
        return 1
    except ValueError as exc:
        print(f"CodeBuddy live probe did not start: {exc}")
        return 1
    combined = probed.stdout + "\n" + probed.stderr
    if (
        probed.returncode != 0
        or probed.stdout.strip() != "DRADAR_CODEBUDDY_AUTH_OK"
    ):
        lowered = combined.lower()
        if any(
            marker in lowered
            for marker in ("unauthorized", "login", "token expired")
        ):
            category = "the imported login was rejected"
        elif any(
            marker in lowered
            for marker in ("quota", "usage limit", "rate limit")
        ):
            category = "the CodeBuddy usage window is unavailable"
        else:
            category = "the container could not complete the HY4 request"
        print(
            f"CodeBuddy live probe failed: {category}. Credentials and raw "
            "provider output were not displayed."
        )
        return 1
    print(
        f"CodeBuddy container authentication and {CODEBUDDY_MODEL} access verified "
        f"live with CLI {CODEBUDDY_CLI_VERSION}."
    )
    return 0


def _live_deepseek_status(key: str) -> int:
    """Verify auth and reachability without making a billable model request."""

    try:
        response = _provider_httpx_get(
            _DEEPSEEK_MODELS_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=10.0,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        print(
            "DeepSeek live check failed before authentication completed: "
            f"{type(exc).__name__}. Check this machine's network/proxy, then retry."
        )
        return 1
    if response.status_code == 401:
        print(
            "DeepSeek live check rejected this API key (HTTP 401). Run "
            "`dradar provider setup deepseek` to replace it."
        )
        return 1
    if response.status_code != 200:
        print(
            "DeepSeek live check failed "
            f"(HTTP {response.status_code}); the saved key was not displayed."
        )
        return 1
    try:
        payload = response.json()
    except ValueError:
        print("DeepSeek live check returned an invalid models response.")
        return 1
    available = {
        item.get("id")
        for item in payload.get("data", [])
        if isinstance(item, dict)
    } if isinstance(payload, dict) else set()
    missing = [model for model in DEEPSEEK_MODELS if model not in available]
    if missing:
        print(
            "DeepSeek authentication succeeded, but the required V4 models are "
            "not available to this account: " + ", ".join(missing)
        )
        return 1
    print("DeepSeek API authentication and V4 model availability verified live.")
    return 0


def _setup_zcode() -> int:
    if not sys.stdin.isatty():
        print(
            "ZCode setup needs an interactive terminal so the Coding Plan key "
            "can be entered with echo disabled. Run:\n"
            "  dradar provider setup zcode\n"
            "The key stays in DRadar's owner-only secret directory."
        )
        return 2
    cli = zcode_cli_path()
    issue = zcode_cli_error(cli)
    if issue is not None:
        print(
            "ZCode setup could not find a compatible official "
            f"desktop runtime: {issue}\n"
            f"Install it from {ZCODE_OFFICIAL_DOWNLOAD_PAGE}, then retry. "
            "Advanced users may point ZCODE_CLI_PATH at "
            "Resources/glm/zcode.cjs; DRadar verifies its CLI version before use."
        )
        return 1
    try:
        imported_cli = store_zcode_cli(cli)
    except (OSError, ValueError) as exc:
        print(f"could not import the compatible ZCode runtime: {exc}")
        return 1
    key = getpass.getpass("BigModel Coding Plan API key (input hidden): ")
    try:
        path = store_zcode_api_key(key)
    except (OSError, ValueError) as exc:
        print(f"could not save ZCode Coding Plan API key: {exc}")
        return 1
    print(
        f"ZCode Coding Plan API key saved locally at {path} (value hidden).\n"
        f"Compatible ZCode runtime imported to {imported_cli}.\n"
        "It is never sent to the DRadar server."
    )
    if _live_zcode_status(key) != 0:
        print(
            "The credential remains saved, but it is not ready for a task yet. "
            "Fix the reported account/network issue, then run: "
            "dradar provider status zcode --live"
        )
        return 1
    return 0


def _status_zcode(*, live: bool) -> int:
    path = zcode_secret_path()
    issue = zcode_secret_error(path)
    key = zcode_api_key()
    if issue is not None:
        print(f"ZCode provider not ready: {issue}")
        return 1
    if key is None:
        print(
            "ZCode provider not configured. In your own interactive Terminal run:\n"
            "  dradar provider setup zcode"
        )
        return 1
    cli = zcode_cli_path()
    issue = zcode_cli_error(cli)
    if issue is not None:
        print(f"ZCode provider not ready: {issue}")
        return 1
    node = shutil.which("node")
    if not node:
        print("ZCode provider not ready: Node.js is not available on PATH.")
        return 1
    try:
        proc = subprocess.run(
            [node, str(cli), "version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"ZCode provider not ready: could not verify the CLI: {exc}")
        return 1
    found = parse_zcode_cli_version(proc.stdout + "\n" + proc.stderr)
    if proc.returncode != 0 or found != ZCODE_CLI_VERSION:
        print(
            f"ZCode provider not ready: CLI {ZCODE_CLI_VERSION} required, "
            f"found {found or 'unknown'}."
        )
        return 1
    source = zcode_credential_source()
    print(
        f"ZCode provider ready via {source or 'local credential'} "
        f"(value hidden, CLI {ZCODE_CLI_VERSION}, models "
        f"{', '.join(sorted(ZCODE_MODELS))})."
    )
    return _live_zcode_status(key) if live else 0


def _live_zcode_status(key: str) -> int:
    """Check the domestic Coding Plan catalog without starting a paid turn."""

    try:
        response = _provider_httpx_get(
            _ZCODE_MODELS_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=10.0,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        print(
            "ZCode live check failed before authentication completed: "
            f"{type(exc).__name__}. Check this machine's network/proxy, then retry."
        )
        return 1
    if response.status_code in {401, 403}:
        print(
            f"ZCode live check rejected this Coding Plan key (HTTP "
            f"{response.status_code}). Run `dradar provider setup zcode` to replace it."
        )
        return 1
    if response.status_code != 200:
        print(
            f"ZCode live check failed (HTTP {response.status_code}); the saved "
            "key was not displayed."
        )
        return 1
    try:
        payload = response.json()
    except ValueError:
        print("ZCode live check returned an invalid models response.")
        return 1
    available = {
        item.get("id")
        for item in payload.get("data", [])
        if isinstance(item, dict)
    } if isinstance(payload, dict) else set()
    missing = sorted(ZCODE_MODELS - available)
    if missing:
        print(
            "ZCode authentication succeeded, but the following models are not "
            f"available: {', '.join(missing)} "
            "to this Coding Plan account."
        )
        return 1
    print(
        "ZCode Coding Plan authentication and model availability verified live: "
        + ", ".join(sorted(ZCODE_MODELS)) + "."
    )
    return 0


def _setup_grok_subscription() -> int:
    """Launch official device OAuth in a DRadar-owned GROK_HOME."""

    executable = _ensure_grok_cli()
    if not executable:
        return 1
    if grok_auth_error() is None:
        live_issue = grok_live_error(executable)
        if live_issue is None:
            print(
                f"Grok subscription provider is already ready (CLI "
                f"{GROK_CLI_VERSION}, {GROK_MODEL} verified)."
            )
            return 0
    if not sys.stdin.isatty():
        print(
            f"Grok CLI {GROK_CLI_VERSION} is ready. OAuth setup needs an "
            "interactive terminal. Run:\n"
            "  dradar provider setup grok\n"
            "This opens the official xAI device OAuth flow; no API key is accepted."
        )
        return 2
    home = grok_home()
    home.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with tempfile.TemporaryDirectory(
            prefix=".grok-login-", dir=home.parent,
        ) as name:
            native_home = Path(name)
            # Grok is a Rust binary and does not read macOS System
            # Configuration proxies itself.  Preserve explicit shell proxy
            # variables and fill otherwise-missing values from the OS proxy
            # settings, just as the installer and live probe do.
            env = provider_subprocess_env()
            env["HOME"] = str(native_home)
            env.pop("GROK_HOME", None)
            env.pop(GROK_API_KEY_ENV, None)
            print(
                "Starting official Grok device OAuth for the dedicated DRadar slot. "
                "Complete the browser/device prompt shown by Grok."
            )
            proc = subprocess.run(
                [executable, "login", "--device-auth"], env=env,
            )
            if proc.returncode != 0:
                print("Grok OAuth login did not complete successfully.")
                return proc.returncode or 1
            native_auth = native_home / ".grok" / "auth.json"
            try:
                store_grok_auth(native_auth)
            except (OSError, ValueError) as exc:
                print(f"Grok login returned but the credential is not ready: {exc}")
                return 1
    except OSError as exc:
        print(f"could not start Grok login: {exc}")
        return 1
    live_issue = grok_live_error(executable)
    if live_issue is not None:
        print(
            "Grok login completed, but the live subscription check failed: "
            f"{live_issue}. Run `dradar provider setup grok` again after fixing "
            "the account/network issue."
        )
        return 1
    print(
        f"Grok subscription OAuth is ready at {grok_auth_path()} (tokens hidden).\n"
        "The credential stays local and API-key authentication is disabled."
    )
    return 0


def _status_grok_subscription() -> int:
    executable = grok_cli_path()
    if not executable:
        print("Grok subscription provider not ready: official Grok CLI not found.")
        return 1
    found_version = _grok_cli_version(executable)
    if found_version != GROK_CLI_VERSION:
        print(
            f"Grok subscription provider not ready: CLI {GROK_CLI_VERSION} "
            f"required, found {found_version or 'unknown'}. Run "
            "`dradar provider setup grok` to prepare it automatically."
        )
        return 1
    issue = grok_auth_error()
    if issue is not None:
        print(f"Grok subscription provider not ready: {issue}")
        return 1
    live_issue = grok_live_error(executable)
    if live_issue is not None:
        print(f"Grok subscription provider not ready: {live_issue}.")
        return 1
    print(
        f"Grok subscription provider ready via {grok_auth_path()} "
        f"(OAuth tokens hidden, CLI {GROK_CLI_VERSION}, {GROK_MODEL} verified, "
        "API keys disabled)."
    )
    return 0


def _setup_kimi_subscription() -> int:
    """Launch Kimi's device OAuth in a dedicated DRadar data root."""

    executable = _ensure_kimi_cli()
    if not executable:
        return 1
    if kimi_auth_error() is None:
        live_issue = kimi_live_error(executable)
        if live_issue is None:
            print(
                f"Kimi subscription provider is already ready "
                f"(CLI {KIMI_CLI_VERSION}, K3 verified)."
            )
            return 0
    if not sys.stdin.isatty():
        print(
            f"Kimi Code CLI {KIMI_CLI_VERSION} is ready. OAuth setup needs an "
            "interactive terminal. Run:\n"
            "  dradar provider setup kimi\n"
            "This opens the official Kimi device OAuth flow; no API key is accepted."
        )
        return 2
    home = kimi_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(home, 0o700)
    try:
        env = provider_subprocess_env()
        env["KIMI_CODE_HOME"] = str(home)
        env["KIMI_DISABLE_TELEMETRY"] = "1"
        env["KIMI_CODE_NO_AUTO_UPDATE"] = "1"
        for name in KIMI_API_KEY_ENVS:
            env.pop(name, None)
        print(
            "Starting official Kimi device OAuth for the dedicated DRadar slot. "
            "Complete the browser/device prompt shown by Kimi."
        )
        proc = subprocess.run([executable, "login"], env=env)
    except OSError as exc:
        print(f"could not start Kimi login: {exc}")
        return 1
    if proc.returncode != 0:
        print("Kimi OAuth login did not complete successfully.")
        return proc.returncode or 1
    path = kimi_auth_path()
    if os.name != "nt" and path.is_file():
        os.chmod(path, 0o600)
    issue = kimi_auth_error(path)
    if issue is not None:
        print(f"Kimi login returned but the credential is not ready: {issue}")
        return 1
    live_issue = kimi_live_error(executable)
    if live_issue is not None:
        print(
            "Kimi login completed, but the live K3 subscription check failed: "
            f"{live_issue}. Run `dradar provider setup kimi` again after fixing "
            "the account/network issue."
        )
        return 1
    print(
        f"Kimi subscription OAuth is ready at {path} (tokens hidden).\n"
        "The credential stays local and API-key authentication is disabled."
    )
    return 0


def _status_kimi_subscription(*, live: bool = True) -> int:
    executable = kimi_cli_path()
    if not executable:
        print("Kimi subscription provider not ready: official Kimi CLI not found.")
        return 1
    found_version = _kimi_cli_version(executable)
    if found_version != KIMI_CLI_VERSION:
        print(
            f"Kimi subscription provider not ready: CLI {KIMI_CLI_VERSION} "
            f"required, found {found_version or 'unknown'}. Run "
            "`dradar provider setup kimi` to prepare it automatically."
        )
        return 1
    issue = kimi_auth_error()
    if issue is not None:
        print(f"Kimi subscription provider not ready: {issue}")
        return 1
    # Keep the default status strict, matching Grok: a structurally valid but
    # revoked refresh token must not be reported as ready. `live` is accepted
    # for CLI symmetry and future callers; both paths are intentionally live.
    del live
    live_issue = kimi_live_error(executable)
    if live_issue is not None:
        print(f"Kimi subscription provider not ready: {live_issue}.")
        return 1
    print(
        f"Kimi subscription provider ready via {kimi_auth_path()} "
        f"(OAuth tokens hidden, CLI {KIMI_CLI_VERSION}, K3 verified, "
        "API keys disabled)."
    )
    return 0


__all__ = ["cmd_provider_setup", "cmd_provider_status"]
