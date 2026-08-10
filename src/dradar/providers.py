"""Optional Codex providers and local-only credential handling.

Provider support is additive: a missing ``provider`` field on every legacy
Codex assignment continues to mean the original OpenAI/ChatGPT path.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

DEFAULT_CODEX_PROVIDER = "openai"
DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_ENABLE_ENV = "DRADAR_ENABLE_DEEPSEEK"
DEEPSEEK_SECRET_RELATIVE_PATH = Path("secrets") / "deepseek_api_key"
DEEPSEEK_CAPABILITY = "codex-deepseek-v4-flash-v2"
DEEPSEEK_CODEX_VERSION = "0.146.0"
DEEPSEEK_MIN_CODEX_VERSION = "0.144.0"
DEEPSEEK_SUPPORTED_EFFORTS = frozenset({"high", "max"})
DEEPSEEK_CATALOG_FILENAME = "deepseek_codex_models.json"
DEEPSEEK_CATALOG_SHA256 = (
    "b459a6e438d6a9939d01fd0dbb4693f165ed732bc8e4fd58d7145d9d94bd49a4"
)
DEEPSEEK_CATALOG_REMOTE_PATH = "/tmp/codex-home/models.json"
DEEPSEEK_CATALOG_SOURCE = (
    "https://cdn.deepseek.com/api-docs/codex-deepseek-setup-en.sh"
)
DEEPSEEK_CATALOG_SOURCE_VERSION = "1.0.0"
DEEPSEEK_RUN_CONFIG_VERSION = "deepseek-codex-official-catalog-v1"
DEEPSEEK_RUNTIME_PROFILE = "public-pier-0.3.0-catalog-v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/"


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def deepseek_catalog_path() -> Path:
    """Return the immutable DeepSeek Codex catalog bundled in the wheel."""

    return Path(__file__).with_name(DEEPSEEK_CATALOG_FILENAME)


def deepseek_catalog_error(path: Path | None = None) -> str | None:
    """Return a fail-closed diagnostic for a missing or modified catalog."""

    catalog = deepseek_catalog_path() if path is None else path
    try:
        payload = catalog.read_bytes()
    except OSError as exc:
        return f"DeepSeek model catalog is unreadable: {catalog}: {exc}"
    digest = hashlib.sha256(payload).hexdigest()
    if digest != DEEPSEEK_CATALOG_SHA256:
        return (
            "DeepSeek model catalog integrity check failed; reinstall or "
            "upgrade dradar before running a paid task"
        )
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"DeepSeek model catalog is invalid JSON: {exc}"
    models = parsed.get("models") if isinstance(parsed, dict) else None
    if not isinstance(models, list):
        return "DeepSeek model catalog has no models list"
    flash = next(
        (
            item for item in models
            if isinstance(item, dict) and item.get("slug") == DEEPSEEK_MODEL
        ),
        None,
    )
    if flash is None:
        return f"DeepSeek model catalog is missing {DEEPSEEK_MODEL}"
    efforts = {
        item.get("effort")
        for item in flash.get("supported_reasoning_levels", [])
        if isinstance(item, dict)
    }
    if not DEEPSEEK_SUPPORTED_EFFORTS <= efforts:
        return "DeepSeek model catalog is missing the benchmark reasoning levels"
    return None


def assignment_codex_provider(assignment: dict) -> str | None:
    """Resolve an assignment's explicit Codex provider without guessing."""

    if assignment.get("agent") != "codex":
        return None
    value = assignment.get("provider")
    return value if isinstance(value, str) and value else DEFAULT_CODEX_PROVIDER


def deepseek_secret_path(home: Path | None = None) -> Path:
    """Return DRadar's provider-secret path without consulting config.json."""

    if home is None:
        home = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
    return Path(home) / DEEPSEEK_SECRET_RELATIVE_PATH


def deepseek_secret_error(path: Path | None = None) -> str | None:
    """Explain why an existing provider-secret file is unsafe to read."""

    path = deepseek_secret_path() if path is None else path
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot inspect {path}: {exc}"
    if stat.S_ISLNK(info.st_mode):
        return f"{path} must be a regular file, not a symlink"
    if not stat.S_ISREG(info.st_mode):
        return f"{path} must be a regular file"
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        return f"{path} is too broadly readable; run: chmod 600 {path}"
    return None


def _deepseek_key_from_file(path: Path) -> str | None:
    if deepseek_secret_error(path) is not None:
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, OSError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            value = handle.read().strip()
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    return value or None


def deepseek_api_key(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> str | None:
    """Resolve the key from the environment first, then the private file."""

    env = os.environ if environ is None else environ
    value = env.get(DEEPSEEK_API_KEY_ENV)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if environ is not None and home is None:
        return None
    return _deepseek_key_from_file(deepseek_secret_path(home))


def deepseek_credential_source(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> str | None:
    """Return the active credential source without exposing its value."""

    env = os.environ if environ is None else environ
    value = env.get(DEEPSEEK_API_KEY_ENV)
    if isinstance(value, str) and value.strip():
        return "environment"
    if environ is not None and home is None:
        return None
    return "file" if _deepseek_key_from_file(deepseek_secret_path(home)) else None


def deepseek_opted_in(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (
        env.get(DEEPSEEK_ENABLE_ENV, "").strip().lower() in _TRUE_VALUES
        or deepseek_api_key(environ) is not None
        or (environ is None and deepseek_secret_path().exists())
    )


def store_deepseek_api_key(value: str, *, home: Path | None = None) -> Path:
    """Atomically store a key in a DRadar-owned 0600 file."""

    key = value.strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError("DeepSeek API key must be one non-empty line")
    path = deepseek_secret_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def create_deepseek_auth_json(directory: Path) -> Path:
    """Create a short-lived Codex auth file without putting the key in argv.

    Public Pier's stock Codex agent uploads ``CODEX_AUTH_JSON_PATH`` into the
    task container. Only the non-secret path appears in Pier/Docker command
    lines; the caller must unlink the returned file after Pier exits.
    """

    key = deepseek_api_key()
    if key is None:
        raise ValueError(
            "DeepSeek API key is not configured; run "
            "`dradar provider setup deepseek` in your own interactive Terminal"
        )
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=".deepseek-auth.", suffix=".json", dir=directory,
    )
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"OPENAI_API_KEY": key}, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(path, 0o600)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path


def advertised_capabilities(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Advertise only a complete, integrity-checked paid-provider runtime."""

    del environ
    return () if deepseek_catalog_error() is not None else (DEEPSEEK_CAPABILITY,)


def normalize_capabilities(values: Iterable[str]) -> tuple[str, ...]:
    """Return a deterministic, header-safe capability list."""

    return tuple(sorted({
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip() and "," not in value
    }))
