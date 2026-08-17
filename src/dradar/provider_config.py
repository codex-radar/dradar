"""Interactive, local-only model-provider credential setup."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

from .providers import (
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_MODELS,
    GROK_API_KEY_ENV,
    GROK_CLI_VERSION,
    GROK_MODEL,
    KIMI_API_KEY_ENVS,
    KIMI_CLI_VERSION,
    ZCODE_APP_VERSION,
    ZCODE_CLI_VERSION,
    ZCODE_MODEL,
    ZCODE_OFFICIAL_DOWNLOAD_PAGE,
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
    parse_kimi_cli_version,
    parse_grok_cli_version,
    parse_zcode_cli_version,
    store_grok_auth,
    store_deepseek_api_key,
    store_zcode_cli,
    store_zcode_api_key,
    zcode_api_key,
    zcode_cli_error,
    zcode_cli_path,
    zcode_credential_source,
    zcode_secret_error,
    zcode_secret_path,
)

_DEEPSEEK_MODELS_URL = "https://api.deepseek.com/models"
_ZCODE_MODELS_URL = "https://open.bigmodel.cn/api/coding/paas/v4/models"


def cmd_provider_setup(args) -> int:
    """Read a DeepSeek key without echoing it or placing it in argv/history."""

    if args.provider == "grok":
        return _setup_grok_subscription()
    if args.provider == "kimi":
        return _setup_kimi_subscription()
    if args.provider == "zcode":
        return _setup_zcode()
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
        "It is not stored in config.json and is never sent to the DRadar server.\n"
        "Next, verify it with: dradar provider status deepseek --live"
    )
    return 0


def cmd_provider_status(args) -> int:
    """Report credential readiness without printing secret material."""

    live = bool(getattr(args, "live", False))
    if args.provider == "grok":
        return _status_grok_subscription()
    if args.provider == "kimi":
        if live:
            print("--live is currently supported only for the DeepSeek provider.")
            return 2
        return _status_kimi_subscription()
    if args.provider == "zcode":
        return _status_zcode(live=live)
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


def _live_deepseek_status(key: str) -> int:
    """Verify auth and reachability without making a billable model request."""

    try:
        response = httpx.get(
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
            "ZCode setup could not find the verified official ZCode "
            f"{ZCODE_APP_VERSION} "
            f"desktop runtime: {issue}\n"
            f"Install it from {ZCODE_OFFICIAL_DOWNLOAD_PAGE}, then retry. "
            "Advanced users may point ZCODE_CLI_PATH at "
            "Resources/glm/zcode.cjs; DRadar verifies its SHA-256 before use."
        )
        return 1
    try:
        imported_cli = store_zcode_cli(cli)
    except (OSError, ValueError) as exc:
        print(f"could not import the verified ZCode runtime: {exc}")
        return 1
    key = getpass.getpass("BigModel Coding Plan API key (input hidden): ")
    try:
        path = store_zcode_api_key(key)
    except (OSError, ValueError) as exc:
        print(f"could not save ZCode Coding Plan API key: {exc}")
        return 1
    print(
        f"ZCode Coding Plan API key saved locally at {path} (value hidden).\n"
        f"Verified ZCode runtime imported to {imported_cli}.\n"
        "It is never sent to the DRadar server. Verify the pinned runtime and "
        "model access with: dradar provider status zcode --live"
    )
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
        f"(value hidden, CLI {ZCODE_CLI_VERSION}, model {ZCODE_MODEL})."
    )
    return _live_zcode_status(key) if live else 0


def _live_zcode_status(key: str) -> int:
    """Check the domestic Coding Plan catalog without starting a paid turn."""

    try:
        response = httpx.get(
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
    if ZCODE_MODEL not in available:
        print(
            f"ZCode authentication succeeded, but {ZCODE_MODEL} is not available "
            "to this Coding Plan account."
        )
        return 1
    print(f"ZCode Coding Plan authentication and {ZCODE_MODEL} availability verified live.")
    return 0


def _setup_grok_subscription() -> int:
    """Launch official device OAuth in a DRadar-owned GROK_HOME."""

    if not sys.stdin.isatty():
        print(
            "Grok subscription setup needs an interactive terminal. Run:\n"
            "  dradar provider setup grok\n"
            "This opens the official xAI device OAuth flow; no API key is accepted."
        )
        return 2
    executable = grok_cli_path()
    if not executable:
        print(
            "Official Grok CLI was not found. Install version "
            f"{GROK_CLI_VERSION}, then run `dradar provider setup grok` again."
        )
        return 1
    try:
        version = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"could not verify Grok CLI: {exc}")
        return 1
    found_version = parse_grok_cli_version(version.stdout)
    if version.returncode != 0 or found_version != GROK_CLI_VERSION:
        print(
            f"Grok CLI {GROK_CLI_VERSION} is required; found "
            f"{found_version or 'unknown'}."
        )
        return 1
    home = grok_home()
    home.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with tempfile.TemporaryDirectory(
            prefix=".grok-login-", dir=home.parent,
        ) as name:
            native_home = Path(name)
            env = dict(os.environ)
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
    try:
        proc = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Grok subscription provider not ready: {exc}")
        return 1
    found_version = parse_grok_cli_version(proc.stdout)
    if proc.returncode != 0 or found_version != GROK_CLI_VERSION:
        print(
            f"Grok subscription provider not ready: CLI {GROK_CLI_VERSION} "
            f"required, found {found_version or 'unknown'}."
        )
        return 1
    issue = grok_auth_error()
    if issue is not None:
        print(f"Grok subscription provider not ready: {issue}")
        return 1
    live_issue = grok_live_error(executable, grok_auth_path())
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

    if not sys.stdin.isatty():
        print(
            "Kimi Code subscription setup needs an interactive terminal. Run:\n"
            "  dradar provider setup kimi\n"
            "This opens the official Kimi device OAuth flow; no API key is accepted."
        )
        return 2
    executable = kimi_cli_path()
    if not executable:
        print(
            "Official Kimi Code CLI was not found. Install version "
            f"{KIMI_CLI_VERSION}, then run `dradar provider setup kimi` again."
        )
        return 1
    try:
        version = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"could not verify Kimi Code CLI: {exc}")
        return 1
    found_version = parse_kimi_cli_version(version.stdout)
    if version.returncode != 0 or found_version != KIMI_CLI_VERSION:
        print(
            f"Kimi Code CLI {KIMI_CLI_VERSION} is required; found "
            f"{found_version or 'unknown'}."
        )
        return 1
    home = kimi_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(home, 0o700)
    env = dict(os.environ)
    env["KIMI_CODE_HOME"] = str(home)
    env["KIMI_DISABLE_TELEMETRY"] = "1"
    env["KIMI_CODE_NO_AUTO_UPDATE"] = "1"
    env["KIMI_CLI_NO_AUTO_UPDATE"] = "1"
    for name in KIMI_API_KEY_ENVS:
        env.pop(name, None)
    print(
        "Starting official Kimi device OAuth for the dedicated DRadar slot. "
        "Complete the browser/device prompt shown by Kimi."
    )
    try:
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
    print(
        f"Kimi subscription OAuth is ready at {path} (tokens hidden).\n"
        "The credential stays local and API-key authentication is disabled."
    )
    return 0


def _status_kimi_subscription() -> int:
    executable = kimi_cli_path()
    if not executable:
        print("Kimi subscription provider not ready: official Kimi CLI not found.")
        return 1
    try:
        proc = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Kimi subscription provider not ready: {exc}")
        return 1
    found_version = parse_kimi_cli_version(proc.stdout)
    if proc.returncode != 0 or found_version != KIMI_CLI_VERSION:
        print(
            f"Kimi subscription provider not ready: CLI {KIMI_CLI_VERSION} "
            f"required, found {found_version or 'unknown'}."
        )
        return 1
    issue = kimi_auth_error()
    if issue is not None:
        print(f"Kimi subscription provider not ready: {issue}")
        return 1
    print(
        f"Kimi subscription provider ready via {kimi_auth_path()} "
        f"(OAuth tokens hidden, CLI {KIMI_CLI_VERSION}, API keys disabled)."
    )
    return 0


__all__ = ["cmd_provider_setup", "cmd_provider_status"]
