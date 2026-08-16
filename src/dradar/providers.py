"""Optional Codex providers and local-only credential handling.

Provider support is additive: a missing ``provider`` field on every legacy
Codex assignment continues to mean the original OpenAI/ChatGPT path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from collections.abc import Iterable, Mapping
from pathlib import Path

DEFAULT_CODEX_PROVIDER = "openai"
DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_FLASH_MODEL = "deepseek-v4-flash"
DEEPSEEK_PRO_MODEL = "deepseek-v4-pro"
# Backwards-compatible import used by older extensions and Flash-only tests.
DEEPSEEK_MODEL = DEEPSEEK_FLASH_MODEL
DEEPSEEK_MODELS = (DEEPSEEK_FLASH_MODEL, DEEPSEEK_PRO_MODEL)
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_ENABLE_ENV = "DRADAR_ENABLE_DEEPSEEK"
DEEPSEEK_SECRET_RELATIVE_PATH = Path("secrets") / "deepseek_api_key"
DEEPSEEK_CAPABILITY = "codex-deepseek-v4-flash-v2"
DEEPSEEK_PRO_CAPABILITY = "codex-deepseek-v4-pro-v1"
DEEPSEEK_FLASH_OFF_CAPABILITY = "codex-deepseek-v4-flash-off-v1"
DEEPSEEK_PRO_OFF_CAPABILITY = "codex-deepseek-v4-pro-off-v1"
DEEPSEEK_CODEX_VERSION = "0.147.0"
DEEPSEEK_MIN_CODEX_VERSION = "0.144.0"
# The bundled upstream catalog still describes ``low`` for integrity and
# compatibility checks, but DRadar's public DeepSeek Codex lane retires it.
DEEPSEEK_SUPPORTED_EFFORTS = frozenset({"off", "high", "max"})
DEEPSEEK_CATALOG_EFFORTS = frozenset({"none", "low", "high", "max"})
DEEPSEEK_CATALOG_FILENAME = "deepseek_codex_models.json"
DEEPSEEK_CATALOG_SHA256 = (
    "8cfa8ab037573ae9914478e6dcd544c43d93c1b126cab5ad58252230dcbe071d"
)
DEEPSEEK_CATALOG_REMOTE_PATH = "/tmp/codex-home/models.json"
DEEPSEEK_CATALOG_SOURCE = (
    "https://cdn.deepseek.com/api-docs/codex-deepseek-setup-en.sh"
)
DEEPSEEK_CATALOG_SOURCE_VERSION = "1.1.0+dradar-off"
DEEPSEEK_RUN_CONFIG_VERSION = "deepseek-codex-official-catalog-v2"
DEEPSEEK_RUNTIME_PROFILE = "public-pier-0.3.0-catalog-v1"

# DSH Minimal is a separate Pier agent, not a Codex provider alias. It reuses
# the same local DeepSeek credential while preserving DSH rc.6's native effort
# surface exactly: off/high/max (there is deliberately no synthetic low mode).
DSH_AGENT = "dsh-minimal"
DSH_VERSION = "0.1.0-rc.6"
DSH_FLASH_MODEL = "dsh-deepseek-v4-flash"
DSH_PRO_MODEL = "dsh-deepseek-v4-pro"
DSH_MODELS = (DSH_FLASH_MODEL, DSH_PRO_MODEL)
DSH_RUNTIME_MODELS = {
    DSH_FLASH_MODEL: DEEPSEEK_FLASH_MODEL,
    DSH_PRO_MODEL: DEEPSEEK_PRO_MODEL,
}
DSH_SUPPORTED_EFFORTS = frozenset({"off", "high", "max"})
DSH_FLASH_CAPABILITY = "dsh-minimal-deepseek-v4-flash-artifact-v5"
DSH_PRO_CAPABILITY = "dsh-minimal-deepseek-v4-pro-artifact-v5"
DSH_FLASH_LEGACY_CAPABILITY = "dsh-minimal-deepseek-v4-flash-artifact-v4"
DSH_PRO_LEGACY_CAPABILITY = "dsh-minimal-deepseek-v4-pro-artifact-v4"
DSH_RUN_CONFIG_VERSION = "dsh-minimal-native-rc6-v1"
DSH_RUNTIME_PROFILE = "public-pier-0.3.0-dsh-minimal-v1"

# Grok Build is intentionally subscription/OAuth-only.  In particular, the
# runner strips XAI_API_KEY from Pier's environment and never accepts a key in
# config, argv, or an assignment.  A dedicated DRadar-owned GROK_HOME keeps a
# benchmark credential separate from the user's everyday Grok CLI session.
GROK_PROVIDER = "xai-subscription"
GROK_AGENT = "grok-build"
GROK_MODEL = "grok-4.6"
GROK_CLI_VERSION = "1.0.0"
GROK_SUPPORTED_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
GROK_CAPABILITY = "grok-build-4.6-subscription-oauth-v2"
GROK_RUN_CONFIG_VERSION = "grok-4.6-subscription-oauth-isolated-v2"
GROK_RUNTIME_PROFILE = "pier-grok-build-4.6-single-slot-v2"
GROK_HOME_RELATIVE_PATH = Path("providers") / "grok"
GROK_AUTH_FILENAME = "auth.json"
GROK_API_KEY_ENV = "XAI_API_KEY"
_GROK_VERSION_RE = re.compile(r"(?:^|\s)(\d+\.\d+\.\d+)(?:\s|$)")

# Kimi Code is also subscription/OAuth-only.  Keep a dedicated DRadar data
# root instead of borrowing the user's everyday ~/.kimi-code session: OAuth
# refresh mutates the credential file and therefore needs a serialized slot.
KIMI_PROVIDER = "kimi-subscription"
KIMI_AGENT = "kimi-code"
KIMI_MODEL = "k3"
KIMI_CLI_VERSION = "0.36.0"
KIMI_SUPPORTED_EFFORTS = frozenset({"low", "high", "max"})
KIMI_CAPABILITY = "kimi-code-k3-subscription-oauth-v1"
KIMI_RUN_CONFIG_VERSION = "kimi-code-k3-subscription-oauth-isolated-v1"
KIMI_RUNTIME_PROFILE = "pier-kimi-code-k3-single-slot-v1"
KIMI_HOME_RELATIVE_PATH = Path("providers") / "kimi"
KIMI_AUTH_RELATIVE_PATH = Path("credentials") / "kimi-code.json"
KIMI_API_KEY_ENVS = frozenset({
    "KIMI_API_KEY",
    "KIMI_MODEL_API_KEY",
    "MOONSHOT_API_KEY",
})
_KIMI_VERSION_RE = re.compile(r"(?:^|\s)(\d+\.\d+\.\d+)(?:\s|$)")

# ZCode is driven through the official desktop bundle's headless protocol.  A
# fixed CLI digest and the domestic Coding Plan endpoint keep this private lane
# reproducible; only GLM-5.3's native low/high/max thought levels are exposed.
ZCODE_PROVIDER = "bigmodel-coding-plan"
ZCODE_AGENT = "zcode"
ZCODE_MODEL = "glm-5.3"
ZCODE_APP_VERSION = "3.7.7"
ZCODE_CLI_VERSION = "0.16.3"
ZCODE_CLI_SHA256 = (
    "4130592942dcaa070f898c2c0152a8345dbfacbf6efb6422b2753c626e756bf5"
)
ZCODE_SUPPORTED_EFFORTS = frozenset({"low", "high", "max"})
ZCODE_CAPABILITY = "zcode-glm-5.3-bigmodel-coding-plan-v1"
ZCODE_RUN_CONFIG_VERSION = "zcode-protocol-glm-5.3-v1"
ZCODE_RUNTIME_PROFILE = "pier-zcode-glm-5.3-api-key-v1"
ZCODE_HOME_RELATIVE_PATH = Path("providers") / "zcode"
ZCODE_CLI_RELATIVE_PATH = ZCODE_HOME_RELATIVE_PATH / "current" / "zcode.cjs"
ZCODE_SECRET_RELATIVE_PATH = Path("secrets") / "zcode_coding_plan_api_key"
ZCODE_API_KEY_ENV = "ZCODE_API_KEY"
_ZCODE_VERSION_RE = re.compile(r"(?:^|\s)(\d+\.\d+\.\d+)(?:\s|$)")

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
    by_slug = {
        item.get("slug"): item
        for item in models
        if isinstance(item, dict) and isinstance(item.get("slug"), str)
    }
    for model in DEEPSEEK_MODELS:
        entry = by_slug.get(model)
        if entry is None:
            return f"DeepSeek model catalog is missing {model}"
        efforts = {
            item.get("effort")
            for item in entry.get("supported_reasoning_levels", [])
            if isinstance(item, dict)
        }
        if not DEEPSEEK_CATALOG_EFFORTS <= efforts:
            return (
                f"DeepSeek model catalog entry {model} is missing the "
                "benchmark reasoning levels"
            )
    return None


def assignment_codex_provider(assignment: dict) -> str | None:
    """Resolve an assignment's explicit Codex provider without guessing."""

    if assignment.get("agent") != "codex":
        return None
    value = assignment.get("provider")
    return value if isinstance(value, str) and value else DEFAULT_CODEX_PROVIDER


def deepseek_codex_reasoning_effort(effort: str) -> str:
    """Translate the product's Off label to Responses API's ``none``."""

    return "none" if effort == "off" else effort


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
    """Resolve the private file first, then fall back to the environment.

    ``provider setup deepseek`` is an explicit local credential selection. A
    stale environment variable inherited from the desktop app or shell must
    not silently shadow the key the user just configured. Callers that pass
    an explicit ``environ`` without ``home`` still get a hermetic,
    environment-only lookup for tests and automation probes.
    """

    env = os.environ if environ is None else environ
    if environ is None or home is not None:
        value = _deepseek_key_from_file(deepseek_secret_path(home))
        if value:
            return value
    value = env.get(DEEPSEEK_API_KEY_ENV)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def deepseek_credential_source(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> str | None:
    """Return the active credential source without exposing its value."""

    env = os.environ if environ is None else environ
    if environ is None or home is not None:
        if _deepseek_key_from_file(deepseek_secret_path(home)):
            return "file"
    value = env.get(DEEPSEEK_API_KEY_ENV)
    if isinstance(value, str) and value.strip():
        return "environment"
    return None


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
    if any(character.isspace() for character in key):
        raise ValueError("DeepSeek API key must be one non-empty line")
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


def create_deepseek_api_key_file(directory: Path) -> Path:
    """Create DSH's short-lived owner-only raw-key input file.

    The DSH Pier adapter uploads this file and converts it to DSH's credential
    document inside the task container. The secret value therefore never
    appears in Pier's argv, inherited environment, or DRadar log.
    """

    key = deepseek_api_key()
    if key is None:
        raise ValueError(
            "DeepSeek API key is not configured; run "
            "`dradar provider setup deepseek` in your own interactive Terminal"
        )
    if any(character.isspace() for character in key):
        raise ValueError("DeepSeek API key must be one non-empty line")
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=".deepseek-dsh-key.", suffix=".txt", dir=directory,
    )
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key + "\n")
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


def zcode_secret_path(home: Path | None = None) -> Path:
    if home is None:
        home = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
    return Path(home) / ZCODE_SECRET_RELATIVE_PATH


def zcode_secret_error(path: Path | None = None) -> str | None:
    """Fail closed for symlinked or broadly readable Coding Plan keys."""

    path = zcode_secret_path() if path is None else path
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


def _zcode_key_from_file(path: Path) -> str | None:
    if zcode_secret_error(path) is not None:
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


def zcode_api_key(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> str | None:
    env = os.environ if environ is None else environ
    if environ is None or home is not None:
        value = _zcode_key_from_file(zcode_secret_path(home))
        if value:
            return value
    value = env.get(ZCODE_API_KEY_ENV)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def zcode_credential_source(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> str | None:
    env = os.environ if environ is None else environ
    if environ is None or home is not None:
        if _zcode_key_from_file(zcode_secret_path(home)):
            return "file"
    value = env.get(ZCODE_API_KEY_ENV)
    if isinstance(value, str) and value.strip():
        return "environment"
    return None


def store_zcode_api_key(value: str, *, home: Path | None = None) -> Path:
    key = value.strip()
    if not key or any(character.isspace() for character in key):
        raise ValueError("ZCode Coding Plan API key must be one non-empty line")
    path = zcode_secret_path(home)
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


def create_zcode_api_key_file(directory: Path) -> Path:
    """Materialize one owner-only run copy; callers must unlink it."""

    key = zcode_api_key()
    if key is None:
        raise ValueError(
            "ZCode Coding Plan API key is not configured; run "
            "`dradar provider setup zcode` in your own interactive Terminal"
        )
    if any(character.isspace() for character in key):
        raise ValueError("ZCode Coding Plan API key must be one non-empty line")
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=".zcode-coding-plan-key.", suffix=".txt", dir=directory,
    )
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key + "\n")
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


def zcode_cli_path(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    explicit = env.get("ZCODE_CLI_PATH")
    if explicit:
        return explicit
    if environ is None:
        home = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
        bundled = home / ZCODE_CLI_RELATIVE_PATH
        return str(bundled) if bundled.is_file() else None
    configured_home = env.get("DRADAR_HOME")
    if configured_home:
        bundled = Path(configured_home) / ZCODE_CLI_RELATIVE_PATH
        return str(bundled) if bundled.is_file() else None
    return None


def parse_zcode_cli_version(output: str) -> str | None:
    match = _ZCODE_VERSION_RE.search(output.strip())
    return match.group(1) if match else None


def zcode_cli_error(
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    candidate = path if path is not None else zcode_cli_path(environ)
    if not candidate:
        return f"official ZCode CLI {ZCODE_CLI_VERSION} is not installed"
    try:
        resolved = Path(candidate).expanduser().resolve(strict=True)
        info = resolved.stat()
        payload = resolved.read_bytes()
    except OSError as exc:
        return f"cannot inspect pinned ZCode CLI: {exc}"
    if not stat.S_ISREG(info.st_mode):
        return "pinned ZCode CLI must resolve to a regular file"
    digest = hashlib.sha256(payload).hexdigest()
    if digest != ZCODE_CLI_SHA256:
        return (
            "ZCode CLI integrity check failed; reinstall the tested "
            f"ZCode {ZCODE_APP_VERSION} runtime"
        )
    return None


def grok_home(home: Path | None = None) -> Path:
    if home is None:
        home = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
    return Path(home) / GROK_HOME_RELATIVE_PATH


def grok_auth_path(home: Path | None = None) -> Path:
    return grok_home(home) / GROK_AUTH_FILENAME


def _valid_grok_auth_payload(payload: object) -> bool:
    """Recognize an OAuth credential without depending on account-specific IDs."""

    if not isinstance(payload, dict) or not payload:
        return False
    for record in payload.values():
        if not isinstance(record, dict):
            continue
        if (
            isinstance(record.get("key"), str)
            and record["key"].strip()
            and isinstance(record.get("refresh_token"), str)
            and record["refresh_token"].strip()
            and record.get("auth_mode") != "api_key"
        ):
            return True
    return False


def grok_auth_error(path: Path | None = None) -> str | None:
    """Fail closed for missing, broad, symlinked, or non-OAuth credentials."""

    path = grok_auth_path() if path is None else path
    try:
        info = path.lstat()
    except FileNotFoundError:
        return f"Grok subscription OAuth is not configured at {path}"
    except OSError as exc:
        return f"cannot inspect {path}: {exc}"
    if stat.S_ISLNK(info.st_mode):
        return f"{path} must be a regular file, not a symlink"
    if not stat.S_ISREG(info.st_mode):
        return f"{path} must be a regular file"
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        return f"{path} is too broadly readable; run: chmod 600 {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"Grok OAuth credential is unreadable or invalid JSON: {exc}"
    if not _valid_grok_auth_payload(payload):
        return "Grok credential is not a refreshable subscription OAuth session"
    return None


def grok_cli_path(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    explicit = env.get("GROK_CLI_PATH")
    if explicit:
        return explicit
    # Keep the ordinary call signature compatible with doctor/test shims that
    # replace shutil.which with a one-argument platform probe.
    if environ is None:
        discovered = shutil.which("grok")
        if discovered:
            return discovered
        official = Path.home() / ".grok" / "bin" / "grok"
        return str(official) if official.is_file() and os.access(official, os.X_OK) else None
    return shutil.which("grok", path=env.get("PATH"))


def parse_grok_cli_version(output: str) -> str | None:
    match = _GROK_VERSION_RE.search(output.strip())
    return match.group(1) if match else None


def kimi_home(home: Path | None = None) -> Path:
    if home is None:
        home = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
    return Path(home) / KIMI_HOME_RELATIVE_PATH


def kimi_auth_path(home: Path | None = None) -> Path:
    return kimi_home(home) / KIMI_AUTH_RELATIVE_PATH


def _valid_kimi_auth_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return all(
        isinstance(payload.get(name), str) and bool(payload[name].strip())
        for name in ("access_token", "refresh_token", "token_type")
    )


def kimi_auth_error(path: Path | None = None) -> str | None:
    """Fail closed for unsafe or non-refreshable Kimi OAuth credentials."""

    path = kimi_auth_path() if path is None else path
    try:
        info = path.lstat()
    except FileNotFoundError:
        return f"Kimi Code subscription OAuth is not configured at {path}"
    except OSError as exc:
        return f"cannot inspect {path}: {exc}"
    if stat.S_ISLNK(info.st_mode):
        return f"{path} must be a regular file, not a symlink"
    if not stat.S_ISREG(info.st_mode):
        return f"{path} must be a regular file"
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        return f"{path} is too broadly readable; run: chmod 600 {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"Kimi credential is unreadable or invalid JSON: {exc}"
    if not _valid_kimi_auth_payload(payload):
        return "Kimi credential is not a refreshable subscription OAuth session"
    return None


def kimi_cli_path(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    explicit = env.get("KIMI_CLI_PATH")
    if explicit:
        return explicit
    if environ is None:
        discovered = shutil.which("kimi")
        if discovered:
            return discovered
        official = Path.home() / ".kimi-code" / "bin" / "kimi"
        return (
            str(official)
            if official.is_file() and os.access(official, os.X_OK)
            else None
        )
    return shutil.which("kimi", path=env.get("PATH"))


def parse_kimi_cli_version(output: str) -> str | None:
    match = _KIMI_VERSION_RE.search(output.strip())
    return match.group(1) if match else None


@contextmanager
def kimi_subscription_session(directory: Path, *, home: Path | None = None):
    """Yield a private Kimi credential copy and atomically retain refreshes."""

    canonical = kimi_auth_path(home)
    issue = kimi_auth_error(canonical)
    if issue is not None:
        raise ValueError(issue + "; run `dradar provider setup kimi` first")
    canonical.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = kimi_home(home) / "auth.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        if os.name == "nt":  # pragma: no cover - Windows runner
            import msvcrt
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        fd, name = tempfile.mkstemp(
            prefix=".kimi-oauth-run.", suffix=".json", dir=directory,
        )
        os.close(fd)
        run_copy = Path(name)
        try:
            _replace_private_file(canonical, run_copy)
            yield run_copy
            issue = kimi_auth_error(run_copy)
            if issue is not None:
                raise ValueError(
                    "Kimi returned an invalid refreshed OAuth credential: " + issue
                )
            _replace_private_file(run_copy, canonical)
        finally:
            try:
                run_copy.unlink()
            except FileNotFoundError:
                pass
            if os.name == "nt":  # pragma: no cover - Windows runner
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _replace_private_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(target.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, handle)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


@contextmanager
def grok_subscription_session(directory: Path, *, home: Path | None = None):
    """Yield a private run copy while serializing OAuth refresh/writeback.

    The lock covers the entire paid CLI process.  The Pier adapter downloads
    the possibly refreshed container credential back onto the yielded file;
    this context validates it before atomically advancing the canonical slot.
    """

    canonical = grok_auth_path(home)
    issue = grok_auth_error(canonical)
    if issue is not None:
        raise ValueError(issue + "; run `dradar provider setup grok` first")
    canonical.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = canonical.parent / "auth.lock"
    directory.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        if os.name == "nt":  # pragma: no cover - Windows runner
            import msvcrt
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        fd, name = tempfile.mkstemp(
            prefix=".grok-oauth-run.", suffix=".json", dir=directory,
        )
        os.close(fd)
        run_copy = Path(name)
        try:
            _replace_private_file(canonical, run_copy)
            yield run_copy
            issue = grok_auth_error(run_copy)
            if issue is not None:
                raise ValueError(
                    "Grok returned an invalid refreshed OAuth credential: " + issue
                )
            _replace_private_file(run_copy, canonical)
        finally:
            try:
                run_copy.unlink()
            except FileNotFoundError:
                pass
            if os.name == "nt":  # pragma: no cover - Windows runner
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def advertised_capabilities(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Advertise only a complete, integrity-checked paid-provider runtime."""

    capabilities = []
    if deepseek_catalog_error() is None:
        capabilities.extend((
            DEEPSEEK_CAPABILITY,
            DEEPSEEK_PRO_CAPABILITY,
            DEEPSEEK_FLASH_OFF_CAPABILITY,
            DEEPSEEK_PRO_OFF_CAPABILITY,
        ))
    # The adapter installs its pinned DSH runtime inside each task image, so
    # its only local runtime prerequisite is a usable DeepSeek credential.
    # Unlike the legacy Codex capabilities, keep these new paid-agent cells
    # hidden until the key is actually ready.
    if (
        deepseek_api_key(environ) is not None
        and Path(__file__).with_name("pier_dsh.py").is_file()
    ):
        capabilities.extend((
            DSH_FLASH_CAPABILITY,
            DSH_PRO_CAPABILITY,
            # Transitional compatibility: an old server knows only v4,
            # while the v5 marker lets the new server require timed usage.
            DSH_FLASH_LEGACY_CAPABILITY,
            DSH_PRO_LEGACY_CAPABILITY,
        ))
    # Unlike API-key providers, a subscription slot is scarce and stateful.
    # Advertise it only when both the CLI and a safe refreshable OAuth session
    # are actually present, preventing the server from assigning unusable work.
    if grok_cli_path(environ) and grok_auth_error() is None:
        capabilities.append(GROK_CAPABILITY)
    if kimi_cli_path(environ) and kimi_auth_error() is None:
        capabilities.append(KIMI_CAPABILITY)
    if (
        zcode_api_key(environ) is not None
        and zcode_cli_error(environ=environ) is None
        and Path(__file__).with_name("pier_zcode.py").is_file()
    ):
        capabilities.append(ZCODE_CAPABILITY)
    return tuple(capabilities)


def normalize_capabilities(values: Iterable[str]) -> tuple[str, ...]:
    """Return a deterministic, header-safe capability list."""

    return tuple(sorted({
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip() and "," not in value
    }))
