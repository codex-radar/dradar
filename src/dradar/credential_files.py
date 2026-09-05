"""Small file-boundary helpers for explicit, local credential imports."""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
from pathlib import Path

MAX_CREDENTIAL_BYTES = 256 * 1024


def is_claude_metered_auth(name: str) -> bool:
    return name in {
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
        "GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
    } or name.startswith(("AWS_", "ANTHROPIC_FOUNDRY_", "AZURE_"))


def _reject_links(path: Path) -> None:
    for component in (path.absolute(), *path.absolute().parents):
        if component.is_symlink():
            raise ValueError("credential paths must not contain symbolic links")


def read_private_credential(path: Path) -> bytes:
    """Read one owned regular file without following a final symlink or FIFO."""
    _reject_links(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("credential source must be a regular file, not a link")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("credential source changed while opening")
        if os.name != "nt":
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("credential source must be owned by you and mode 0600")
        content = source.read(MAX_CREDENTIAL_BYTES + 1)
    if not content or len(content) > MAX_CREDENTIAL_BYTES:
        raise ValueError("credential source is empty or too large")
    return content


def private_directory(path: Path) -> None:
    _reject_links(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("credential directory must not be a symlink")
    if os.name != "nt":
        if info.st_uid != os.getuid():
            raise ValueError("credential directory must be owned by you")
        os.chmod(path, 0o700)


def atomic_private_credential(path: Path, content: bytes) -> None:
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        read_private_credential(path)
    fd, name = tempfile.mkstemp(prefix=".credential-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def credential_json(content: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate credential field")
            result[key] = value
        return result
    try:
        payload = json.loads(content, object_pairs_hook=unique)
    except (UnicodeError, ValueError):
        raise ValueError("credential JSON is invalid") from None
    if not isinstance(payload, dict):
        raise ValueError("credential JSON must be an object")
    return payload


def claude_config_payload(content: bytes) -> dict:
    """Keep native OAuth intact; never reinterpret it as a setup token."""
    payload = credential_json(content)
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise ValueError("official Claude OAuth configuration is missing")
    access = oauth.get("accessToken")
    refresh = oauth.get("refreshToken")
    scopes = oauth.get("scopes")
    subscription = oauth.get("subscriptionType")
    if (
        not isinstance(access, str) or not access.startswith("sk-ant-oat")
        or not isinstance(refresh, str) or not refresh.strip()
        or not isinstance(scopes, list) or "user:inference" not in scopes
        or not all(isinstance(scope, str) for scope in scopes)
        or not isinstance(subscription, str) or subscription.lower() not in {"pro", "max", "team", "enterprise"}
        or type(oauth.get("expiresAt")) not in (int, float)
        or not math.isfinite(oauth["expiresAt"]) or oauth["expiresAt"] <= 0
        or any(key in payload for key in ("apiKey", "anthropicApiKey", "ANTHROPIC_API_KEY"))
    ):
        raise ValueError("a complete Claude subscription OAuth configuration is required; API credentials are not accepted")
    return {"claudeAiOauth": oauth}
