"""Interactive, local-only model-provider credential setup."""

from __future__ import annotations

import getpass
import os
import subprocess
import sys

import httpx

from .providers import (
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_MODELS,
    GROK_API_KEY_ENV,
    GROK_CLI_VERSION,
    deepseek_api_key,
    deepseek_credential_source,
    deepseek_secret_error,
    deepseek_secret_path,
    grok_auth_error,
    grok_auth_path,
    grok_cli_path,
    grok_home,
    parse_grok_cli_version,
    store_deepseek_api_key,
    DEEPSEEK_OPENCODE_API_KEY_ENV,
    opencode_api_key,
    opencode_credential_source,
    opencode_secret_error,
    opencode_secret_path,
    store_opencode_api_key,
)

_DEEPSEEK_MODELS_URL = "https://api.deepseek.com/models"

def _provider_spec(provider: str):
    if provider == "deepseek":
        return (
            "DeepSeek", DEEPSEEK_API_KEY_ENV,
            deepseek_secret_path, deepseek_secret_error,
            deepseek_credential_source, deepseek_api_key,
            store_deepseek_api_key,
        )
    if provider == "opencode-go":
        return (
            "OpenCode Go", DEEPSEEK_OPENCODE_API_KEY_ENV,
            opencode_secret_path, opencode_secret_error,
            opencode_credential_source, opencode_api_key,
            store_opencode_api_key,
        )
    raise ValueError(f"unsupported provider: {provider}")



def cmd_provider_setup(args) -> int:
    """Read a DeepSeek key without echoing it or placing it in argv/history."""

    if args.provider == "grok":
        return _setup_grok_subscription()
    label, _env, _path, _error, _source, _key, store = _provider_spec(
        args.provider
    )
    if not sys.stdin.isatty():
        print(
            f"{label} setup needs an interactive terminal so the key can be "
            "entered with echo disabled. Open your own Terminal and run:\n"
            f"  dradar provider setup {args.provider}\n"
            "Never paste the API key into Codex/chat or pass it as a command argument."
        )
        return 2
    key = getpass.getpass(f"{label} API key (input hidden): ")
    try:
        path = store(key)
    except (OSError, ValueError) as exc:
        print(f"could not save {label} API key: {exc}")
        return 1
    print(
        f"{label} API key saved locally at {path} (value hidden).\n"
        "It is not stored in config.json and is never sent to the DRadar server."
    )
    if args.provider == "deepseek":
        print("Next, verify it with: dradar provider status deepseek --live")
    return 0


def cmd_provider_status(args) -> int:
    """Report credential readiness without printing secret material."""

    live = bool(getattr(args, "live", False))
    if args.provider == "grok":
        if live:
            print("--live is currently supported only for the DeepSeek provider.")
            return 2
        return _status_grok_subscription()
    label, env, path_fn, error_fn, source_fn, key_fn, _store = _provider_spec(args.provider)
    path = path_fn()
    error = error_fn(path)
    if error is not None:
        print(f"{label} provider not ready: {error}")
        return 1
    source = source_fn()
    key = key_fn()
    if source == "environment" and key:
        print(f"{label} provider configured via {env} (value hidden).")
        if args.provider == "deepseek" and live:
            return _live_deepseek_status(key)
        return 0
    if source == "file" and key:
        print(f"{label} provider configured via {path} (value hidden).")
        if args.provider == "deepseek" and live:
            return _live_deepseek_status(key)
        return 0
    print(
        f"{label} provider not configured. In your own interactive Terminal "
        f"run:\n  dradar provider setup {args.provider}"
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
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(home, 0o700)
    env = dict(os.environ)
    env["GROK_HOME"] = str(home)
    env.pop(GROK_API_KEY_ENV, None)
    print(
        "Starting official Grok device OAuth for the dedicated DRadar slot. "
        "Complete the browser/device prompt shown by Grok."
    )
    try:
        proc = subprocess.run([executable, "login", "--device-auth"], env=env)
    except OSError as exc:
        print(f"could not start Grok login: {exc}")
        return 1
    if proc.returncode != 0:
        print("Grok OAuth login did not complete successfully.")
        return proc.returncode or 1
    issue = grok_auth_error(grok_auth_path())
    if issue is not None:
        print(f"Grok login returned but the credential is not ready: {issue}")
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
    print(
        f"Grok subscription provider ready via {grok_auth_path()} "
        f"(OAuth tokens hidden, CLI {GROK_CLI_VERSION}, API keys disabled)."
    )
    return 0


__all__ = ["cmd_provider_setup", "cmd_provider_status"]
