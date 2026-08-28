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
import subprocess
import sys
import tempfile
import time
import urllib.request
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
# DeepSeek follows npm's latest stable Codex release at run time.  Keep the
# last audited fixed release as a compatibility floor, not as a permanent pin.
DEEPSEEK_MIN_CODEX_VERSION = "0.147.0"
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
# the same local DeepSeek credential while preserving DSH 0.1.1-rc.1's native
# effort surface: off/high/max (there is deliberately no synthetic low mode).
DSH_AGENT = "dsh-minimal"
DSH_VERSION = "0.1.1-rc.1"
DSH_FLASH_MODEL = "dsh-deepseek-v4-flash"
DSH_PRO_MODEL = "dsh-deepseek-v4-pro"
DSH_VISION_MODEL = "dsh-deepseek-v4-flash-vision-exp"
DSH_MODELS = (DSH_FLASH_MODEL, DSH_PRO_MODEL, DSH_VISION_MODEL)
DSH_RUNTIME_MODELS = {
    DSH_FLASH_MODEL: DEEPSEEK_FLASH_MODEL,
    DSH_PRO_MODEL: DEEPSEEK_PRO_MODEL,
    DSH_VISION_MODEL: "deepseek-v4-flash-vision-exp",
}
DSH_SUPPORTED_EFFORTS = frozenset({"off", "high", "max"})
DSH_FLASH_CAPABILITY = "dsh-minimal-deepseek-v4-flash-artifact-v5"
DSH_PRO_CAPABILITY = "dsh-minimal-deepseek-v4-pro-artifact-v5"
DSH_VISION_CAPABILITY = (
    "dsh-minimal-deepseek-v4-flash-vision-exp-pompeii-image-v1"
)
DSH_VISION_TEXT_CAPABILITY = (
    "dsh-minimal-deepseek-v4-flash-vision-exp-deepswe-text-v1"
)
DSH_FLASH_LEGACY_CAPABILITY = "dsh-minimal-deepseek-v4-flash-artifact-v4"
DSH_PRO_LEGACY_CAPABILITY = "dsh-minimal-deepseek-v4-pro-artifact-v4"
DSH_RUN_CONFIG_VERSION = "dsh-minimal-native-0.1.1-rc.1-v1"
DSH_RUNTIME_PROFILE = "public-pier-0.3.0-dsh-minimal-0.1.1-rc.1-v1"

# Grok Build is intentionally subscription/OAuth-only.  In particular, the
# runner strips XAI_API_KEY from Pier's environment and never accepts a key in
# config, argv, or an assignment.  A dedicated DRadar-owned GROK_HOME keeps a
# benchmark credential separate from the user's everyday Grok CLI session.
GROK_PROVIDER = "xai-subscription"
GROK_AGENT = "grok-build"
GROK_MODEL = "grok-4.6"
GROK_CLI_VERSION = "1.0.3"
GROK_SUPPORTED_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
GROK_CAPABILITY = "grok-build-4.6-subscription-oauth-concurrent-v4"
GROK_LEGACY_CAPABILITY = "grok-build-4.6-subscription-oauth-v3"
GROK_RUN_CONFIG_VERSION = "grok-4.6-subscription-oauth-concurrent-v4"
GROK_RUNTIME_PROFILE = "pier-grok-build-4.6-shared-oauth-lock-v4"
GROK_HOME_RELATIVE_PATH = Path("providers") / "grok"
GROK_RUNTIME_RELATIVE_PATH = (
    GROK_HOME_RELATIVE_PATH / "runtime" / GROK_CLI_VERSION
)
GROK_AUTH_FILENAME = "auth.json"


def provider_subprocess_env() -> dict[str, str]:
    """Return an environment that also honors OS-level proxy settings.

    Terminal proxy variables remain authoritative.  On macOS, GUI proxy
    settings are otherwise invisible to Rust/Go CLIs launched by DRadar;
    ``urllib`` reads those settings through SystemConfiguration without
    changing the user's shell or global network configuration.
    """

    env = dict(os.environ)
    # One explicit DRadar interface can drive host-side OAuth/live checks,
    # Docker builds, and the Pier runtime sidecar. Standard HTTP(S)_PROXY
    # variables remain supported; this dedicated override is authoritative
    # when users need a deterministic runbook across shells and platforms.
    if proxy := env.get("DRADAR_HTTP_PROXY", "").strip():
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
    if no_proxy := env.get("DRADAR_NO_PROXY", "").strip():
        env["NO_PROXY"] = no_proxy
    proxy_names = {
        "http": "HTTP_PROXY",
        "https": "HTTPS_PROXY",
        "no": "NO_PROXY",
    }
    try:
        proxies = urllib.request.getproxies()
    except (OSError, ValueError):
        proxies = {}
    for proxy_type, env_name in proxy_names.items():
        if env.get(env_name) or env.get(env_name.lower()):
            continue
        value = proxies.get(proxy_type)
        if isinstance(value, str) and value.strip():
            env[env_name] = value.strip()
    return env
GROK_API_KEY_ENV = "XAI_API_KEY"
_GROK_VERSION_RE = re.compile(r"(?:^|\s)(\d+\.\d+\.\d+)(?:\s|$)")

# Kimi Code is also subscription/OAuth-only.  Keep a dedicated DRadar data
# root instead of borrowing the user's everyday ~/.kimi-code session.  Paid
# task containers share only the official credential and OAuth-lock folders;
# task sessions, configs, workspaces, logs, and artifacts stay isolated.
KIMI_PROVIDER = "kimi-subscription"
KIMI_AGENT = "kimi-code"
KIMI_MODEL = "k3"
KIMI_CLI_VERSION = "0.36.1"
KIMI_SUPPORTED_EFFORTS = frozenset({"low", "high", "max"})
KIMI_CAPABILITY = "kimi-code-k3-subscription-oauth-node-concurrent-v2"
KIMI_LEGACY_CAPABILITY = "kimi-code-k3-subscription-oauth-node-v1"
KIMI_RUN_CONFIG_VERSION = "kimi-code-k3-subscription-oauth-node-concurrent-v2"
KIMI_RUNTIME_PROFILE = "pier-kimi-code-k3-node-shared-oauth-lock-v2"
KIMI_BINARY_BASE_URL = "https://code.kimi.com/kimi-code/binaries/0.36.1"
KIMI_BINARY_SHA256 = {
    "linux-x64": "78c07b255e0bdc8dfe90d0cbd3204a3d862957394a08ca99c6e31144732451c7",
    "linux-arm64": "a48e90f49cacee600310b4aebb87df417bf7af9fc3ddc282e721d9fb811391a0",
    "darwin-x64": "037b201bf8dccca987fcc98645ea746d6d683bd2d8cc201891c062bf0b14798e",
    "darwin-arm64": "53b8a5d9380131a23c58937f28d64e93830c56aa92c41432f24ab9d8eccf0e50",
    "win32-x64": "9da56c617b2c51a55a313a33d52aebfe5729734e36b2fe6d5c989b4a51b7d327",
    "win32-arm64": "70e14eb27776e65b0ddc0660d06d020b7de88930fe412a7b504f26371c0ae533",
}
KIMI_HOME_RELATIVE_PATH = Path("providers") / "kimi"
KIMI_RUNTIME_RELATIVE_PATH = (
    KIMI_HOME_RELATIVE_PATH / "runtime" / KIMI_CLI_VERSION
)
KIMI_AUTH_RELATIVE_PATH = Path("credentials") / "kimi-code.json"
KIMI_API_KEY_ENVS = frozenset({
    "KIMI_API_KEY",
    "KIMI_MODEL_API_KEY",
    "MOONSHOT_API_KEY",
})
_KIMI_VERSION_RE = re.compile(r"(?:^|\s)(\d+\.\d+\.\d+)(?:\s|$)")

# Google Antigravity is subscription/OAuth-only.  Authentication is performed
# once inside Google's pinned Linux CLI so the resulting file-based session is
# portable into Pier task containers.  The user's everyday ~/.gemini tree and
# macOS keychain are never borrowed by DRadar.
ANTIGRAVITY_PROVIDER = "google-antigravity-subscription"
ANTIGRAVITY_AGENT = "antigravity"
ANTIGRAVITY_MODEL = "gemini-3.7-flash"
ANTIGRAVITY_CLI_VERSION = "1.1.22"
ANTIGRAVITY_LINUX_RELEASE = "1.1.22-5711547746615296"
ANTIGRAVITY_LINUX_ARTIFACTS = {
    "x86_64": {
        "url": (
            "https://storage.googleapis.com/antigravity-public/"
            "antigravity-cli/1.1.22-5711547746615296/linux-x64/"
            "cli_linux_x64.tar.gz"
        ),
        "sha512": (
            "40225d4b1f009412e905f0a234ba3d51487038d1ad1b8fa19331c84be55610a0"
            "1f5b0ad9916fb871151cc45456c6bc30cc0b1ea5dab6c0616bc8fb262bcdd7a9"
        ),
    },
    "aarch64": {
        "url": (
            "https://storage.googleapis.com/antigravity-public/"
            "antigravity-cli/1.1.22-5711547746615296/linux-arm/"
            "cli_linux_arm64.tar.gz"
        ),
        "sha512": (
            "b37a718330eb5e270e1ca70135bf964a407ba626fbff7537ac58e094ea31bc623"
            "e6d216ef197188fe8b5c46e6f57aee64a3b7c9e23fc855cefee43fe434179d3"
        ),
    },
}
ANTIGRAVITY_SUPPORTED_EFFORTS = frozenset({"low", "medium", "high"})
ANTIGRAVITY_RUNTIME_MODELS = {
    "low": "gemini-3.7-flash-low",
    "medium": "gemini-3.7-flash-medium",
    "high": "gemini-3.7-flash-high",
}
ANTIGRAVITY_CAPABILITY = (
    "antigravity-gemini-3.7-flash-subscription-oauth-sandbox-v1"
)
ANTIGRAVITY_RUN_CONFIG_VERSION = (
    "antigravity-gemini-3.7-flash-subscription-oauth-sandbox-v1"
)
ANTIGRAVITY_RUNTIME_PROFILE = (
    "pier-antigravity-gemini-3.7-flash-shared-oauth-sandbox-v1"
)
ANTIGRAVITY_ARTIFACT_CAPTURE = "full-worktree-v1"
ANTIGRAVITY_HOME_RELATIVE_PATH = Path("providers") / "antigravity"
ANTIGRAVITY_GEMINI_RELATIVE_PATH = ANTIGRAVITY_HOME_RELATIVE_PATH / ".gemini"
ANTIGRAVITY_READY_FILENAME = "ready.json"
_ANTIGRAVITY_VERSION_RE = re.compile(r"(?:^|\s)(\d+\.\d+\.\d+)(?:\s|$)")

# ZCode is driven through the official desktop bundle's headless protocol.  A
# tested CLI digest allowlist and the domestic Coding Plan endpoint keep this
# preview lane reproducible without forcing users onto one exact desktop app
# release; both GLM-5.3 variants expose native low/high/max thought levels.
ZCODE_PROVIDER = "bigmodel-coding-plan"
ZCODE_AGENT = "zcode"
ZCODE_MODEL = "glm-5.3"
ZCODE_FLASH_MODEL = "glm-5.3-flash"
ZCODE_MODELS = frozenset({ZCODE_MODEL, ZCODE_FLASH_MODEL})
ZCODE_CLI_VERSION = "0.16.5"
ZCODE_SUPPORTED_EFFORTS = frozenset({"low", "high", "max"})
ZCODE_LEGACY_CAPABILITY = "zcode-glm-5.3-bigmodel-coding-plan-v1"
ZCODE_CAPABILITY = "zcode-glm-5.3-family-bigmodel-coding-plan-v2"
ZCODE_RUN_CONFIG_VERSION = "zcode-protocol-glm-5.3-family-v2"
ZCODE_RUNTIME_PROFILE = "pier-zcode-glm-5.3-family-api-key-v2"
ZCODE_HOME_RELATIVE_PATH = Path("providers") / "zcode"
ZCODE_CLI_RELATIVE_PATH = ZCODE_HOME_RELATIVE_PATH / "current" / "zcode.cjs"
ZCODE_SECRET_RELATIVE_PATH = Path("secrets") / "zcode_coding_plan_api_key"
ZCODE_API_KEY_ENV = "ZCODE_API_KEY"
ZCODE_OFFICIAL_DOWNLOAD_PAGE = "https://zcode.z.ai/cn"
_ZCODE_VERSION_RE = re.compile(r"(?:^|\s)(\d+\.\d+\.\d+)(?:\s|$)")

# User-facing continuous-refill names resolve to the same canonical agent
# wire values used by assignments and the public table.  Keeping this beside
# the provider constants prevents the CLI from growing a second, drifting
# model of which harness owns K3/GLM/Grok cells.
REFILL_HARNESS_ALIASES = {
    "codex": "codex",
    "openai": "codex",
    "dsh": DSH_AGENT,
    "dsh-minimal": DSH_AGENT,
    "deepseek-harness": DSH_AGENT,
    "kimi": KIMI_AGENT,
    "kimi-code": KIMI_AGENT,
    "grok": GROK_AGENT,
    "grok-build": GROK_AGENT,
    "agy": ANTIGRAVITY_AGENT,
    "antigravity": ANTIGRAVITY_AGENT,
    "zcode": ZCODE_AGENT,
}
REFILL_HARNESS_CONSTRAINTS = {
    DSH_AGENT: (frozenset(DSH_MODELS), DSH_SUPPORTED_EFFORTS),
    KIMI_AGENT: (frozenset({KIMI_MODEL}), KIMI_SUPPORTED_EFFORTS),
    GROK_AGENT: (frozenset({GROK_MODEL}), GROK_SUPPORTED_EFFORTS),
    ANTIGRAVITY_AGENT: (
        frozenset({ANTIGRAVITY_MODEL}), ANTIGRAVITY_SUPPORTED_EFFORTS,
    ),
    ZCODE_AGENT: (ZCODE_MODELS, ZCODE_SUPPORTED_EFFORTS),
}
REFILL_HARNESS_PROVIDERS = {
    DSH_AGENT: DEEPSEEK_PROVIDER,
    KIMI_AGENT: KIMI_PROVIDER,
    GROK_AGENT: GROK_PROVIDER,
    ANTIGRAVITY_AGENT: ANTIGRAVITY_PROVIDER,
    ZCODE_AGENT: ZCODE_PROVIDER,
}
SUBSCRIPTION_REFILL_AGENTS = frozenset({
    KIMI_AGENT, GROK_AGENT, ZCODE_AGENT, ANTIGRAVITY_AGENT,
})
PAID_API_REFILL_AGENTS = frozenset({DSH_AGENT})

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def normalize_refill_harness(value: str) -> str:
    """Return the canonical assignment ``agent`` for a refill harness name."""

    normalized = value.strip().lower().replace("_", "-")
    try:
        return REFILL_HARNESS_ALIASES[normalized]
    except KeyError as exc:
        names = ", ".join(sorted(REFILL_HARNESS_ALIASES))
        raise ValueError(
            f"unknown refill harness {value!r}; choose one of: {names}"
        ) from exc


def validate_refill_scope(
    harness: str, model: str | None, effort: str | None,
) -> tuple[str, str | None, str | None]:
    """Normalize and fail fast on impossible built-in harness combinations.

    Codex remains server-catalog driven because its model surface evolves
    independently.  Subscription/DSH harnesses are pinned runtime contracts,
    so their provider constants are authoritative before any network call.
    """

    agent = normalize_refill_harness(harness)
    normalized_model = model.strip().lower() if model else None
    normalized_effort = effort.strip().lower() if effort else None
    constraint = REFILL_HARNESS_CONSTRAINTS.get(agent)
    if constraint is not None:
        models, efforts = constraint
        if normalized_model is not None and normalized_model not in models:
            raise ValueError(
                f"{agent} refill supports model(s) {', '.join(sorted(models))}; "
                f"got {model!r}"
            )
        if normalized_effort is not None and normalized_effort not in efforts:
            raise ValueError(
                f"{agent} refill supports effort(s) {', '.join(sorted(efforts))}; "
                f"got {effort!r}"
            )
    return agent, normalized_model, normalized_effort


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


def zcode_cli_candidates(
    environ: Mapping[str, str] | None = None,
    *,
    user_home: Path | None = None,
) -> tuple[Path, ...]:
    """Return explicit, imported, and official desktop ZCode CLI locations.

    DRadar never downloads or redistributes ZCode. Setup imports a compatible
    CLI from the user's official desktop installation (or from an explicit
    ``ZCODE_CLI_PATH``) into DRadar's owner-only provider slot.
    """

    env = os.environ if environ is None else environ
    home = Path(user_home) if user_home is not None else Path.home()
    dradar_home = Path(env.get("DRADAR_HOME", home / ".dradar"))
    candidates: list[Path] = []
    explicit = env.get("ZCODE_CLI_PATH")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(dradar_home / ZCODE_CLI_RELATIVE_PATH)

    if sys.platform == "darwin":
        candidates.extend((
            Path("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"),
            home / "Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
        ))
    elif os.name == "nt":  # pragma: no cover - exercised via candidate tests
        for variable in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            root = env.get(variable)
            if not root:
                continue
            base = Path(root)
            candidates.extend((
                base / "Programs/ZCode/resources/glm/zcode.cjs",
                base / "ZCode/resources/glm/zcode.cjs",
            ))
    else:
        appdir = env.get("APPDIR")
        if appdir:
            app_root = Path(appdir)
            candidates.extend((
                app_root / "resources/glm/zcode.cjs",
                app_root / "usr/lib/zcode/resources/glm/zcode.cjs",
            ))
        candidates.extend((
            Path("/opt/ZCode/resources/glm/zcode.cjs"),
            Path("/opt/zcode/resources/glm/zcode.cjs"),
            Path("/usr/lib/ZCode/resources/glm/zcode.cjs"),
            Path("/usr/lib/zcode/resources/glm/zcode.cjs"),
        ))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return tuple(unique)


def zcode_cli_path(environ: Mapping[str, str] | None = None) -> str | None:
    candidates = zcode_cli_candidates(environ)
    if not candidates:
        return None
    env = os.environ if environ is None else environ
    # Preserve an explicit path even when it is invalid so status/doctor can
    # report the compatibility error instead of a misleading "not installed".
    if env.get("ZCODE_CLI_PATH"):
        return str(candidates[0])
    first_installed: Path | None = None
    for candidate in candidates:
        if not candidate.is_file():
            continue
        if first_installed is None:
            first_installed = candidate
        # A previously imported runtime is intentionally the first candidate,
        # but it must not shadow a newly installed compatible desktop upgrade.
        # This lets setup/status discover a replacement after the CLI version
        # changes without tying compatibility to desktop packaging bytes.
        if zcode_cli_error(candidate) is None:
            return str(candidate)
    return str(first_installed) if first_installed is not None else None


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
    except OSError as exc:
        return f"cannot inspect ZCode CLI: {exc}"
    if not stat.S_ISREG(info.st_mode):
        return "ZCode CLI must resolve to a regular file"
    env = os.environ if environ is None else environ
    node = (
        shutil.which("node")
        if environ is None
        else shutil.which("node", path=env.get("PATH"))
    )
    if not node:
        return "Node.js is required to verify the ZCode CLI version"
    # Version probes do not need provider credentials or the user's home. Keep
    # the child environment deliberately small because an explicit
    # ZCODE_CLI_PATH is user-selected executable JavaScript.
    probe_env = {
        name: env[name]
        for name in (
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR",
            "TMPDIR", "TEMP", "TMP",
        )
        if env.get(name)
    }
    try:
        proc = subprocess.run(
            [node, str(resolved), "version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=probe_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not verify the ZCode CLI version: {type(exc).__name__}"
    found = parse_zcode_cli_version(proc.stdout + "\n" + proc.stderr)
    if proc.returncode != 0 or found != ZCODE_CLI_VERSION:
        return (
            f"ZCode CLI {ZCODE_CLI_VERSION} is required; "
            f"found {found or 'an unrecognized version'}"
        )
    return None


def store_zcode_cli(
    source: str | Path,
    *,
    home: Path | None = None,
) -> Path:
    """Import a compatible ZCode CLI into DRadar's local-only slot."""

    issue = zcode_cli_error(source)
    if issue is not None:
        raise ValueError(issue)
    if home is None:
        home = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
    target = Path(home) / ZCODE_CLI_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(target.parent, 0o700)
    source_path = Path(source).expanduser().resolve(strict=True)
    if target.exists() and source_path == target.resolve(strict=True):
        if os.name != "nt":
            os.chmod(target, 0o600)
        return target
    _replace_private_file(source_path, target)
    return target


_ANTIGRAVITY_REQUIRED_DENY_RULES = frozenset({
    "read_file(/tmp/dradar-antigravity-user/.gemini)",
    "write_file(/tmp/dradar-antigravity-user/.gemini)",
    "read_file(/logs)",
    "write_file(/logs)",
    "read_url(*)",
    "execute_url(*)",
    "mcp(*)",
    "unsandboxed(*)",
})


def antigravity_home(home: Path | None = None) -> Path:
    if home is None:
        home = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
    return Path(home) / ANTIGRAVITY_HOME_RELATIVE_PATH


def antigravity_auth_path(home: Path | None = None) -> Path:
    """Return the narrow .gemini directory mounted into task containers."""

    return antigravity_home(home) / ".gemini"


def antigravity_settings_path(home: Path | None = None) -> Path:
    return antigravity_auth_path(home) / "antigravity-cli" / "settings.json"


def antigravity_ready_path(home: Path | None = None) -> Path:
    return antigravity_home(home) / ANTIGRAVITY_READY_FILENAME


def antigravity_settings_payload() -> dict[str, object]:
    """Return the fail-closed headless policy used by every paid AGY run."""

    return {
        "enableTelemetry": False,
        "enableTerminalSandbox": True,
        "allowNonWorkspaceAccess": False,
        "trustedWorkspaces": ["/app"],
        "permissions": {
            # Commands are autonomous only inside AGY's native nsjail ring.
            # File tools are already auto-allowed in /app by the official CLI.
            "allow": ["command(*)"],
            "deny": sorted(_ANTIGRAVITY_REQUIRED_DENY_RULES),
        },
    }


def _private_tree_error(path: Path) -> str | None:
    """Reject links, special files, and group/world-readable OAuth state."""

    try:
        root_info = path.lstat()
    except FileNotFoundError:
        return f"Antigravity subscription OAuth is not configured at {path}"
    except OSError as exc:
        return f"cannot inspect {path}: {exc}"
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return f"{path} must be a private directory, not a symlink"
    try:
        entries = [path, *path.rglob("*")]
    except OSError as exc:
        return f"cannot inspect Antigravity OAuth home: {exc}"
    for entry in entries:
        try:
            info = entry.lstat()
        except OSError as exc:
            return f"cannot inspect {entry}: {exc}"
        if stat.S_ISLNK(info.st_mode):
            return f"{entry} must not be a symlink"
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            return f"{entry} must be a regular file or directory"
        if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            return f"{entry} is too broadly accessible; re-run provider setup"
    return None


def antigravity_auth_error(home: Path | None = None) -> str | None:
    """Validate isolated OAuth state plus the mandatory sandbox policy."""

    auth = antigravity_auth_path(home)
    issue = _private_tree_error(auth)
    if issue is not None:
        return issue
    settings = antigravity_settings_path(home)
    try:
        payload = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"Antigravity sandbox settings are unavailable or invalid: {exc}"
    expected = antigravity_settings_payload()
    if payload != expected:
        return "Antigravity sandbox settings do not match DRadar's safe policy"
    ready = antigravity_ready_path(home)
    try:
        ready_payload = json.loads(ready.read_text(encoding="utf-8"))
        ready_info = ready.lstat()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"Antigravity readiness proof is unavailable or invalid: {exc}"
    if (
        stat.S_ISLNK(ready_info.st_mode)
        or not stat.S_ISREG(ready_info.st_mode)
        or (os.name != "nt" and stat.S_IMODE(ready_info.st_mode) & 0o077)
    ):
        return "Antigravity readiness proof must be an owner-only regular file"
    if not isinstance(ready_payload, dict) or (
        ready_payload.get("schema") != "dradar-antigravity-ready-v1"
        or ready_payload.get("cli_version") != ANTIGRAVITY_CLI_VERSION
        or ready_payload.get("models")
        != sorted(ANTIGRAVITY_RUNTIME_MODELS.values())
    ):
        return "Antigravity readiness proof does not match this DRadar release"
    return None


def write_antigravity_settings(home: Path | None = None) -> Path:
    path = antigravity_settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix=".settings.", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                antigravity_settings_payload(), handle,
                ensure_ascii=False, separators=(",", ":"),
            )
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


def mark_antigravity_ready(home: Path | None = None) -> Path:
    path = antigravity_ready_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix=".ready.", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({
                "schema": "dradar-antigravity-ready-v1",
                "cli_version": ANTIGRAVITY_CLI_VERSION,
                "models": sorted(ANTIGRAVITY_RUNTIME_MODELS.values()),
            }, handle, separators=(",", ":"))
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


def _remove_antigravity_cli_log_link(home: Path | None = None) -> None:
    """Remove only AGY's known disposable convenience log symlink."""

    cli_log = antigravity_auth_path(home) / "antigravity-cli" / "cli.log"
    try:
        if stat.S_ISLNK(cli_log.lstat().st_mode):
            cli_log.unlink()
    except FileNotFoundError:
        pass


def privatize_antigravity_home(home: Path | None = None) -> None:
    root = antigravity_home(home)
    if not root.exists():
        return
    # AGY maintains this exact convenience link to its rotating log.  It is
    # unnecessary in the credential mount and would make the otherwise
    # symlink-free tree fail closed after every real login.  Unlink the known
    # path without following it; every other symlink remains an error.
    _remove_antigravity_cli_log_link(home)
    runtime = root / "runtime"
    for path in [root, *root.rglob("*")]:
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Antigravity OAuth state contains a symlink: {path}")
        if stat.S_ISDIR(info.st_mode):
            if os.name != "nt":
                os.chmod(path, 0o700)
        elif stat.S_ISREG(info.st_mode):
            if os.name != "nt":
                # The reviewed AGY Linux binary is cached below ``runtime``.
                # Keep that one file owner-executable after credential
                # hardening; Docker Desktop preserves the host mode on a
                # read-only bind mount.  Every OAuth/config/proof file remains
                # owner-readable only.
                executable = (
                    path.name == "antigravity" and runtime in path.parents
                )
                os.chmod(path, 0o700 if executable else 0o600)
        else:
            raise ValueError(f"Antigravity OAuth state contains a special file: {path}")


def restore_antigravity_settings(home: Path | None = None) -> Path:
    """Safely restore the reviewed policy after the official CLI mutates it."""

    # Inspect and harden first so an untrusted symlink created by the runtime
    # can never redirect the subsequent atomic settings replacement.
    privatize_antigravity_home(home)
    path = write_antigravity_settings(home)
    privatize_antigravity_home(home)
    return path


def parse_antigravity_cli_version(output: str) -> str | None:
    match = _ANTIGRAVITY_VERSION_RE.search(output.strip())
    return match.group(1) if match else None


@contextmanager
def antigravity_subscription_session(
    directory: Path, *, home: Path | None = None,
):
    """Expose only DRadar's validated .gemini tree to one Pier trial."""

    # Another active AGY process may have just recreated its convenience log
    # link.  It is the one reviewed exception that ``privatize`` also removes;
    # discard it before the otherwise fail-closed tree validation so parallel
    # workers do not race on a harmless runtime artifact.
    _remove_antigravity_cli_log_link(home)
    issue = antigravity_auth_error(home)
    if issue is not None:
        raise ValueError(issue + "; run `dradar provider setup antigravity` first")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield antigravity_auth_path(home)
    finally:
        # The official CLI currently normalizes explicit false settings after
        # both successful and failed runs.  Reassert the reviewed policy before
        # validating the credential tree so a harmless normalization cannot
        # strand a lease, while every genuinely unsafe mutation still fails.
        restore_antigravity_settings(home)
        issue = antigravity_auth_error(home)
        if issue is not None:
            raise ValueError("Antigravity returned unsafe OAuth state: " + issue)


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


def _run_grok_live_probe(cli: str, credential: Path, root: Path) -> str | None:
    native_user_home = root / "native-home"
    native_home = native_user_home / ".grok"
    native_home.mkdir(parents=True, mode=0o700)
    native_auth = native_home / GROK_AUTH_FILENAME
    _replace_private_file(credential, native_auth)
    env = provider_subprocess_env()
    env["HOME"] = str(native_user_home)
    env.pop("GROK_HOME", None)
    env.pop(GROK_API_KEY_ENV, None)
    env["GROK_TELEMETRY_ENABLED"] = "0"
    env["GROK_TELEMETRY_MIXPANEL_ENABLED"] = "0"
    env["GROK_TELEMETRY_TRACE_UPLOAD"] = "0"
    try:
        proc = subprocess.run(
            [cli, "models"], capture_output=True, text=True,
            timeout=30, check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Grok live model check failed: {type(exc).__name__}"
    finally:
        # `grok models` may silently rotate the refresh token. Preserve any
        # structurally valid update even when the catalog request itself
        # fails, otherwise a harmless readiness check can invalidate the
        # canonical OAuth slot.
        if grok_auth_error(native_auth) is None:
            _replace_private_file(native_auth, credential)
    output = f"{proc.stdout}\n{proc.stderr}"
    if "settings fetch failed" in output.lower():
        return "Grok live model check failed; check this machine's network/proxy"
    if "not authenticated" in output.lower():
        return (
            "Grok OAuth session is not authenticated; run "
            "`dradar provider setup grok` in your own interactive Terminal"
        )
    if proc.returncode != 0:
        return "Grok live model check failed; check this machine's network/proxy"
    if GROK_MODEL not in output:
        return f"Grok OAuth account cannot access {GROK_MODEL}"
    return None


def grok_live_error(
    executable: str | Path | None = None,
    auth_path: Path | None = None,
) -> str | None:
    """Verify the saved OAuth session and Grok 4.6 catalog without a prompt.

    Grok reads OAuth from ``$HOME/.grok/auth.json``. DRadar keeps its
    canonical slot at a provider-specific path, so the probe uses a private
    temporary HOME matching Grok's native layout and never exposes tokens in
    argv or output.
    """
    cli = str(executable or grok_cli_path() or "")
    if not cli:
        return f"official Grok CLI {GROK_CLI_VERSION} is not installed"
    canonical = grok_auth_path() if auth_path is None else auth_path
    issue = grok_auth_error(canonical)
    if issue is not None:
        return issue
    with tempfile.TemporaryDirectory(prefix="dradar-grok-probe-") as name:
        root = Path(name)
        if auth_path is None:
            # The readiness probe shares the same lock and refresh writeback
            # contract as a paid run.
            with grok_subscription_session(root / "slot") as run_copy:
                return _run_grok_live_probe(cli, run_copy, root)
        return _run_grok_live_probe(cli, canonical, root)


def store_grok_auth(source: Path, *, home: Path | None = None) -> Path:
    """Atomically install a native Grok OAuth file into DRadar's slot."""
    issue = grok_auth_error(source)
    if issue is not None:
        raise ValueError(issue)
    canonical = grok_auth_path(home)
    canonical.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(canonical.parent, 0o700)
    _replace_private_file(source, canonical)
    return canonical


def managed_grok_cli_path(home: Path | None = None) -> Path:
    """Return DRadar's versioned Grok runtime without consulting ``PATH``."""

    if home is None:
        home = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
    executable = "grok.exe" if os.name == "nt" else "grok"
    return Path(home) / GROK_RUNTIME_RELATIVE_PATH / "bin" / executable


def grok_cli_path(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    explicit = env.get("GROK_CLI_PATH")
    if explicit:
        return explicit
    managed = managed_grok_cli_path(
        Path(env["DRADAR_HOME"]) if env.get("DRADAR_HOME") else None
    )
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed)
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


def _run_kimi_live_probe(
    executable: str | Path,
    credential: Path,
    root: Path,
) -> str | None:
    share = root / "home"
    native_auth = share / KIMI_AUTH_RELATIVE_PATH
    _replace_private_file(credential, native_auth)
    env = provider_subprocess_env()
    env["KIMI_CODE_HOME"] = str(share)
    env["KIMI_DISABLE_TELEMETRY"] = "1"
    env["KIMI_CODE_NO_AUTO_UPDATE"] = "1"
    for name in KIMI_API_KEY_ENVS:
        env.pop(name, None)
    try:
        proc = subprocess.run(
            [str(executable), "login"],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Kimi live OAuth check failed: {type(exc).__name__}"
    finally:
        # The official refresh endpoint rotates tokens. Retain a valid new
        # credential even if the following model-catalog check failed.
        if kimi_auth_error(native_auth) is None:
            _replace_private_file(native_auth, credential)
    if proc.returncode != 0:
        output = (proc.stdout + "\n" + proc.stderr).lower()
        if any(marker in output for marker in ("unauthorized", "invalid_grant", "rejected")):
            return "Kimi OAuth session was rejected; run `dradar provider setup kimi`"
        return "Kimi OAuth refresh failed; check this machine's network/proxy"
    try:
        config_text = (share / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return "Kimi login did not provision the official model catalog"
    if '"kimi-code/k3"' not in config_text:
        return f"Kimi subscription account cannot access {KIMI_MODEL}"
    return None


def kimi_live_error(
    executable: str | Path | None = None,
    auth_path: Path | None = None,
) -> str | None:
    """Refresh the isolated OAuth token and verify K3 without a paid turn."""

    cli = str(executable or kimi_cli_path() or "")
    if not cli:
        return f"official Kimi Code CLI {KIMI_CLI_VERSION} is not installed"
    canonical = kimi_auth_path() if auth_path is None else auth_path
    issue = kimi_auth_error(canonical)
    if issue is not None:
        return issue
    with tempfile.TemporaryDirectory(prefix="dradar-kimi-probe-") as name:
        root = Path(name)
        if auth_path is None:
            with kimi_subscription_session(root / "slot") as run_copy:
                return _run_kimi_live_probe(cli, run_copy, root)
        return _run_kimi_live_probe(cli, canonical, root)


def kimi_cli_path(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    explicit = env.get("KIMI_CLI_PATH")
    if explicit:
        return explicit
    managed = managed_kimi_cli_path(
        Path(env["DRADAR_HOME"]) if env.get("DRADAR_HOME") else None
    )
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed)
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


def managed_kimi_cli_path(home: Path | None = None) -> Path:
    """Return DRadar's versioned Kimi runtime without consulting ``PATH``."""

    if home is None:
        home = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
    executable = "kimi.exe" if os.name == "nt" else "kimi"
    return Path(home) / KIMI_RUNTIME_RELATIVE_PATH / "bin" / executable


def parse_kimi_cli_version(output: str) -> str | None:
    match = _KIMI_VERSION_RE.search(output.strip())
    return match.group(1) if match else None


@contextmanager
def kimi_subscription_session(directory: Path, *, home: Path | None = None):
    """Validate and expose Kimi's shared native OAuth credential store.

    The official Kimi runtime coordinates concurrent refresh-token rotation in
    its ``credentials``/``oauth`` directories.  DRadar must not wrap the whole
    paid run in another host lock: doing so would turn ``--workers N`` into N
    checkouts followed by one-at-a-time provider execution.
    """

    canonical = kimi_auth_path(home)
    issue = kimi_auth_error(canonical)
    if issue is not None:
        raise ValueError(issue + "; run `dradar provider setup kimi` first")
    root = kimi_home(home)
    oauth = root / "oauth"
    for path in (root, canonical.parent, oauth):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(path, 0o700)
    native_lock = oauth / "kimi-code"
    native_lock.touch(mode=0o600, exist_ok=True)
    if os.name != "nt":
        os.chmod(native_lock, 0o600)
    directory.mkdir(parents=True, exist_ok=True)
    yield canonical
    if os.name != "nt":
        # A concurrent rootful task container may have just atomically
        # replaced the shared credential.  Its in-container ownership guard
        # repairs the new inode; allow that bounded handoff to settle before
        # treating a transient EACCES as an invalid OAuth refresh.
        deadline = time.monotonic() + 1.0
        while not os.access(canonical, os.R_OK) and time.monotonic() < deadline:
            time.sleep(0.02)
    issue = kimi_auth_error(canonical)
    if issue is not None:
        raise ValueError("Kimi returned an invalid refreshed OAuth credential: " + issue)


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
    """Validate and expose Grok's shared native OAuth home.

    Grok 1.0.3 coordinates refreshes with ``auth.json.lock`` next to the
    credential.  Sharing that narrow provider home lets independent task
    containers use the official lock instead of serializing whole trials.
    """

    canonical = grok_auth_path(home)
    issue = grok_auth_error(canonical)
    if issue is not None:
        raise ValueError(issue + "; run `dradar provider setup grok` first")
    canonical.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(canonical.parent, 0o700)
    directory.mkdir(parents=True, exist_ok=True)
    yield canonical
    issue = grok_auth_error(canonical)
    if issue is not None:
        raise ValueError("Grok returned an invalid refreshed OAuth credential: " + issue)


def advertised_capabilities(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Advertise only a complete, compatibility-checked paid-provider runtime."""

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
            DSH_VISION_CAPABILITY,
            DSH_VISION_TEXT_CAPABILITY,
            # Transitional compatibility: an old server knows only v4,
            # while the v5 marker lets the new server require timed usage.
            DSH_FLASH_LEGACY_CAPABILITY,
            DSH_PRO_LEGACY_CAPABILITY,
        ))
    # Unlike API-key providers, a subscription slot is scarce and stateful.
    # Advertise it only when both the CLI and a safe refreshable OAuth session
    # are actually present, preventing the server from assigning unusable work.
    if grok_cli_path(environ) and grok_auth_error() is None:
        capabilities.extend((GROK_CAPABILITY, GROK_LEGACY_CAPABILITY))
    if kimi_cli_path(environ) and kimi_auth_error() is None:
        capabilities.extend((KIMI_CAPABILITY, KIMI_LEGACY_CAPABILITY))
    if (
        antigravity_auth_error() is None
        and Path(__file__).with_name("pier_antigravity.py").is_file()
    ):
        capabilities.append(ANTIGRAVITY_CAPABILITY)
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
