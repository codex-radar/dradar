"""Interactive, local-only model-provider credential setup."""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable
from pathlib import Path

from .providers import (
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_OPENCODE_API_KEY_ENV,
    deepseek_api_key,
    deepseek_credential_source,
    deepseek_secret_error,
    deepseek_secret_path,
    opencode_api_key,
    opencode_credential_source,
    opencode_secret_error,
    opencode_secret_path,
    store_deepseek_api_key,
    store_opencode_api_key,
)


def _provider_spec(provider: str) -> tuple[
    str,
    str,
    Callable[[], Path],
    Callable[[Path], str | None],
    Callable[[], str | None],
    Callable[[], str | None],
    Callable[[str], Path],
]:
    """Return (label, env_var, secret_path, secret_error, source, key, store)."""

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
    """Read a provider key without echoing it or putting it in argv/history."""

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
    return 0


def cmd_provider_status(args) -> int:
    """Report credential readiness without printing secret material."""

    label, env, path_fn, error_fn, source_fn, key_fn, _store = _provider_spec(
        args.provider
    )
    path = path_fn()
    error = error_fn(path)
    if error is not None:
        print(f"{label} provider not ready: {error}")
        return 1
    source = source_fn()
    if source == "environment":
        print(f"{label} provider ready via {env} (value hidden).")
        return 0
    if source == "file" and key_fn():
        print(f"{label} provider ready via {path} (value hidden).")
        return 0
    print(
        f"{label} provider not configured. In your own interactive Terminal "
        f"run:\n  dradar provider setup {args.provider}"
    )
    return 1


__all__ = ["cmd_provider_setup", "cmd_provider_status"]
