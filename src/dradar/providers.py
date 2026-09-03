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

from .codebuddy_provider import (
    CODEBUDDY_AGENT,
    CODEBUDDY_CAPABILITY,
    CODEBUDDY_MODEL,
    CODEBUDDY_PROVIDER,
    CODEBUDDY_SUPPORTED_EFFORTS,
    codebuddy_executable,
    codebuddy_host_cli_status,
    codebuddy_runtime_image_error,
    credential_status as codebuddy_credential_status,
)

DEFAULT_CODEX_PROVIDER = "openai"
CLAUDE_PROVIDER = "anthropic-subscription"
CLAUDE_AGENT = "claude-code"
CLAUDE_SONNET_MODEL = "claude-sonnet-5"
CLAUDE_OPUS_MODEL = "claude-opus-5"
CLAUDE_MODELS = frozenset({CLAUDE_SONNET_MODEL, CLAUDE_OPUS_MODEL})
CLAUDE_CLI_VERSION = "2.1.251"
CLAUDE_SUPPORTED_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CLAUDE_CAPABILITY = "claude-code-5-subscription-oauth-sandbox-v1"
CLAUDE_RUN_CONFIG_VERSION = "claude-code-5-subscription-oauth-safe-mode-v1"
CLAUDE_RUNTIME_PROFILE = "pier-claude-code-5-private-oauth-full-container-v1"
CLAUDE_HOME_RELATIVE_PATH = Path("providers") / "claude"
CLAUDE_OAUTH_TOKEN_FILENAME = "oauth-token"
CLAUDE_API_KEY_ENVS = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_PROFILE", "AWS_REGION", "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
})
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
# the same local DeepSeek credential while preserving DSH 0.1.1-rc.2's native
# effort surface: off/high/max (there is deliberately no synthetic low mode).
DSH_AGENT = "dsh-minimal"
DSH_VERSION = "0.1.1-rc.2"
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
DSH_FLASH_CAPABILITY = "dsh-minimal-deepseek-v4-flash-artifact-v6"
DSH_PRO_CAPABILITY = "dsh-minimal-deepseek-v4-pro-artifact-v6"
DSH_VISION_CAPABILITY = (
    "dsh-minimal-deepseek-v4-flash-vision-exp-pompeii-image-v2"
)
DSH_VISION_TEXT_CAPABILITY = (
    "dsh-minimal-deepseek-v4-flash-vision-exp-deepswe-text-v2"
)
DSH_FLASH_LEGACY_CAPABILITY = "dsh-minimal-deepseek-v4-flash-artifact-v4"
DSH_PRO_LEGACY_CAPABILITY = "dsh-minimal-deepseek-v4-pro-artifact-v4"
DSH_RUN_CONFIG_VERSION = "dsh-minimal-native-full-container-0.1.1-rc.2-v3"
DSH_RUNTIME_PROFILE = "public-pier-0.3.0-dsh-minimal-full-container-v3"

# Grok Build is intentionally subscription/OAuth-only.  In particular, the
# runner strips XAI_API_KEY from Pier's environment and never accepts a key in
# config, argv, or an assignment.  A dedicated DRadar-owned GROK_HOME keeps a
# benchmark credential separate from the user's everyday Grok CLI session.
GROK_PROVIDER = "xai-subscription"
GROK_AGENT = "grok-build"
GROK_MODEL = "grok-4.6"
GROK_CLI_VERSION = "1.0.13"
GROK_SUPPORTED_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
GROK_CAPABILITY = "grok-build-4.6-subscription-oauth-concurrent-v5"
GROK_LEGACY_CAPABILITY = "grok-build-4.6-subscription-oauth-concurrent-v4"
GROK_RUN_CONFIG_VERSION = "grok-4.6-subscription-oauth-concurrent-v5"
GROK_RUNTIME_PROFILE = "pier-grok-build-4.6-shared-oauth-lock-v5"
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
KIMI_CLI_VERSION = "0.39.1"
KIMI_SUPPORTED_EFFORTS = frozenset({"low", "high", "max"})
KIMI_CAPABILITY = "kimi-code-k3-subscription-oauth-node-concurrent-v3"
KIMI_LEGACY_CAPABILITY = "kimi-code-k3-subscription-oauth-node-concurrent-v2"
KIMI_RUN_CONFIG_VERSION = "kimi-code-k3-subscription-oauth-web-disabled-v5"
KIMI_RUNTIME_PROFILE = "pier-kimi-code-k3-shared-oauth-web-disabled-v5"
KIMI_BINARY_BASE_URL = "https://code.kimi.com/kimi-code/binaries/0.39.1"
KIMI_BINARY_SHA256 = {
    "linux-x64": "585547e082f2f3a32dd80825626a1c8dd4e82f55b4d6a8aa14e6397c00758eca",
    "linux-arm64": "f2e16073823cdeda207e3d228ef899cb9e43c8623ab21ddd3edd75702ae19ca3",
    "darwin-x64": "b0d1897ae3fdd7f651939296655c4784ec8f3558c30ebb8bb8b954ba1fff55db",
    "darwin-arm64": "762ee3be8b67796657409b8d5074ab0beed6f42162035bd4a274055ef0c44cdd",
    "win32-x64": "ce3a74ead55994eb1350cc45a1d0d9bf083158f2bba4da49a5ee6168a1830338",
    "win32-arm64": "b6b3c22576eb44ae9f1956bca4913ab3957cc110812d71212055ad8c89641a28",
}
KIMI_HOME_RELATIVE_PATH = Path("providers") / "kimi"
KIMI_ACCOUNT_HOME_ENV = "DRADAR_KIMI_HOME"
KIMI_CREDENTIAL_PATH_ENV = "KIMI_CREDENTIAL_PATH"
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
ANTIGRAVITY_FLASH_38_MODEL = "gemini-3.8-flash"
ANTIGRAVITY_MODELS = frozenset({ANTIGRAVITY_MODEL, ANTIGRAVITY_FLASH_38_MODEL})
ANTIGRAVITY_CLI_VERSION = "1.1.24"
ANTIGRAVITY_LINUX_RELEASE = "1.1.24-6130423206641664"
ANTIGRAVITY_LINUX_ARTIFACTS = {
    "x86_64": {
        "url": (
            "https://storage.googleapis.com/antigravity-public/"
            "antigravity-cli/1.1.24-6130423206641664/linux-x64/"
            "cli_linux_x64.tar.gz"
        ),
        "sha512": (
            "ed4df91ea7ced986aa14507a0ab8225d92985190f7d551010eba0c46c569587e6"
            "02cb36af81c9cde7af0d6b380e8dd3a82131361806cd96012d44a3e47fb369a"
        ),
    },
    "aarch64": {
        "url": (
            "https://storage.googleapis.com/antigravity-public/"
            "antigravity-cli/1.1.24-6130423206641664/linux-arm/"
            "cli_linux_arm64.tar.gz"
        ),
        "sha512": (
            "316ca00d50389a08b162c66066b4e2db201e4ffb85acea05029e3c4532c69d5b"
            "8f7c741cf027325889f898ea8f747af8cd15c802e15fcf5d73b7137b6e2420a1"
        ),
    },
}
ANTIGRAVITY_SUPPORTED_EFFORTS = frozenset({"low", "medium", "high"})
ANTIGRAVITY_RUNTIME_MODELS = {
    effort: f"{ANTIGRAVITY_MODEL}-{effort}"
    for effort in ANTIGRAVITY_SUPPORTED_EFFORTS
}
ANTIGRAVITY_RUNTIME_MODELS_BY_MODEL = {
    (model, effort): f"{model}-{effort}"
    for model in ANTIGRAVITY_MODELS
    for effort in ANTIGRAVITY_SUPPORTED_EFFORTS
}
ANTIGRAVITY_CAPABILITY = (
    "antigravity-gemini-3-flash-family-subscription-oauth-sandbox-v2"
)
ANTIGRAVITY_RUN_CONFIG_VERSION = (
    "antigravity-gemini-3-flash-family-subscription-oauth-full-container-v3"
)
ANTIGRAVITY_RUNTIME_PROFILE = (
    "pier-antigravity-gemini-3-flash-family-shared-oauth-full-container-v3"
)
ANTIGRAVITY_ARTIFACT_CAPTURE = "full-worktree-v1"
ANTIGRAVITY_HOME_RELATIVE_PATH = Path("providers") / "antigravity"
ANTIGRAVITY_GEMINI_RELATIVE_PATH = ANTIGRAVITY_HOME_RELATIVE_PATH / ".gemini"
ANTIGRAVITY_READY_FILENAME = "ready.json"
_ANTIGRAVITY_VERSION_RE = re.compile(r"(?:^|\s)(\d+\.\d+\.\d+)(?:\s|$)")

# ZCode is driven through the official desktop bundle's headless protocol.  A
# compatible protocol line and the domestic Coding Plan endpoint keep this
# preview lane reproducible without forcing users onto one exact desktop app
# release; both GLM-5.3 variants expose native low/high/max thought levels.
ZCODE_PROVIDER = "bigmodel-coding-plan"
ZCODE_AGENT = "zcode"
ZCODE_MODEL = "glm-5.3"
ZCODE_FLASH_MODEL = "glm-5.3-flash"
ZCODE_MODELS = frozenset({ZCODE_MODEL, ZCODE_FLASH_MODEL})
ZCODE_CLI_VERSION = "0.16.5"
ZCODE_MIN_COMPATIBLE_CLI_VERSION = "0.16.3"
ZCODE_FLASH_MIN_CLI_VERSION = ZCODE_CLI_VERSION
ZCODE_SUPPORTED_EFFORTS = frozenset({"low", "high", "max"})
ZCODE_LEGACY_CAPABILITY = "zcode-glm-5.3-bigmodel-coding-plan-v1"
ZCODE_CAPABILITY = "zcode-glm-5.3-family-bigmodel-coding-plan-v2"
TASK_PACKAGE_SYNC_CAPABILITY = "public-task-package-pin-v1"
ZCODE_RUN_CONFIG_VERSION = "zcode-protocol-glm-5.3-family-full-container-v3"
ZCODE_RUNTIME_PROFILE = "pier-zcode-glm-5.3-family-api-key-full-container-v3"
ZCODE_HOME_RELATIVE_PATH = Path("providers") / "zcode"
ZCODE_CLI_RELATIVE_PATH = ZCODE_HOME_RELATIVE_PATH / "current" / "zcode.cjs"
ZCODE_SECRET_RELATIVE_PATH = Path("secrets") / "zcode_coding_plan_api_key"
ZCODE_API_KEY_ENV = "ZCODE_API_KEY"
ZCODE_OFFICIAL_DOWNLOAD_PAGE = "https://zcode.z.ai/cn"
_ZCODE_VERSION_RE = re.compile(r"(?:^|\s)(\d+\.\d+\.\d+)(?:\s|$)")

# Every benchmark Honey uses the disposable Pier container as its trust
# boundary.  These values are uploaded with each current run so the server can
# fail closed if a future adapter accidentally restores an inner approval
# prompt, hides child-agent tools, or weakens the outer isolation contract.
HONEY_EXECUTION_SECURITY_PROFILE = "full-container-tools-outer-boundary-v1"
HONEY_INNER_PERMISSION_MODE = "full-auto-approve"
HONEY_CHILD_AGENT_ACCESS = "native-enabled"
HONEY_OUTER_ISOLATION = "pier-docker-exact-egress-minimal-credentials-v1"
HONEY_SECURITY_AGENTS = frozenset({
    "codex",
    CLAUDE_AGENT,
    DSH_AGENT,
    ZCODE_AGENT,
    KIMI_AGENT,
    ANTIGRAVITY_AGENT,
})

# User-facing continuous-refill names resolve to the same canonical agent
# wire values used by assignments and the public table.  Keeping this beside
# the provider constants prevents the CLI from growing a second, drifting
# model of which harness owns K3/GLM/Grok cells.
REFILL_HARNESS_ALIASES = {
    "codex": "codex",
    "openai": "codex",
    "claude": CLAUDE_AGENT,
    "claude-code": CLAUDE_AGENT,
    "dsh": DSH_AGENT,
    "dsh-minimal": DSH_AGENT,
    "deepseek-harness": DSH_AGENT,
    "kimi": KIMI_AGENT,
    "kimi-code": KIMI_AGENT,
    "grok": GROK_AGENT,
    "grok-build": GROK_AGENT,
    "agy": ANTIGRAVITY_AGENT,
    "antigravity": ANTIGRAVITY_AGENT,
    "codebuddy": CODEBUDDY_AGENT,
    "hy4": CODEBUDDY_AGENT,
    "zcode": ZCODE_AGENT,
}
REFILL_HARNESS_CONSTRAINTS = {
    CLAUDE_AGENT: (CLAUDE_MODELS, CLAUDE_SUPPORTED_EFFORTS),
    DSH_AGENT: (frozenset(DSH_MODELS), DSH_SUPPORTED_EFFORTS),
    KIMI_AGENT: (frozenset({KIMI_MODEL}), KIMI_SUPPORTED_EFFORTS),
    GROK_AGENT: (frozenset({GROK_MODEL}), GROK_SUPPORTED_EFFORTS),
    ANTIGRAVITY_AGENT: (
        ANTIGRAVITY_MODELS, ANTIGRAVITY_SUPPORTED_EFFORTS,
    ),
    CODEBUDDY_AGENT: (
        frozenset({CODEBUDDY_MODEL}), CODEBUDDY_SUPPORTED_EFFORTS,
    ),
    ZCODE_AGENT: (ZCODE_MODELS, ZCODE_SUPPORTED_EFFORTS),
}
REFILL_HARNESS_PROVIDERS = {
    CLAUDE_AGENT: CLAUDE_PROVIDER,
    DSH_AGENT: DEEPSEEK_PROVIDER,
    KIMI_AGENT: KIMI_PROVIDER,
    GROK_AGENT: GROK_PROVIDER,
    ANTIGRAVITY_AGENT: ANTIGRAVITY_PROVIDER,
    CODEBUDDY_AGENT: CODEBUDDY_PROVIDER,
    ZCODE_AGENT: ZCODE_PROVIDER,
}
SUBSCRIPTION_REFILL_AGENTS = frozenset({
    CLAUDE_AGENT, KIMI_AGENT, GROK_AGENT, ZCODE_AGENT, ANTIGRAVITY_AGENT,
    CODEBUDDY_AGENT,
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


def zcode_cli_version_is_compatible(
    version: str | None, *, model: str | None = None,
) -> bool:
    """Accept compatible protocol patches without pinning desktop packaging."""

    if version is None:
        return False
    try:
        major, minor, patch = (int(part) for part in version.split("."))
    except (TypeError, ValueError):
        return False
    minimum_patch = 5 if model == ZCODE_FLASH_MODEL else 3
    return (major, minor) == (0, 16) and patch >= minimum_patch


def zcode_cli_error(
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    model: str | None = None,
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
    if proc.returncode != 0 or not zcode_cli_version_is_compatible(
        found, model=model,
    ):
        minimum = (
            ZCODE_FLASH_MIN_CLI_VERSION
            if model == ZCODE_FLASH_MODEL
            else ZCODE_MIN_COMPATIBLE_CLI_VERSION
        )
        return (
            f"a compatible ZCode CLI 0.16.x >= {minimum} is required; "
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


_ANTIGRAVITY_FULL_CONTAINER_ALLOW_RULES = (
    "command(*)",
    "execute_url(*)",
    "mcp(*)",
    "read_file(*)",
    "read_url(*)",
    "unsandboxed(*)",
    "write_file(*)",
)
_ANTIGRAVITY_POLICY_MISMATCH = (
    "Antigravity settings do not match DRadar's full-container policy"
)


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
    """Return the full-container policy used by every paid AGY run.

    Benchmark agents execute inside Pier's disposable Docker boundary.  The
    inner CLI must therefore not add a second approval/sandbox layer that can
    deny ordinary coding tools or child-agent work and skew the score.  Host
    files, network egress, and credentials remain constrained by Pier and the
    provider adapter rather than by Antigravity's interactive permission UI.
    """

    return {
        "enableTelemetry": False,
        "enableTerminalSandbox": False,
        "allowNonWorkspaceAccess": True,
        "trustedWorkspaces": ["/app"],
        "permissions": {
            "allow": list(_ANTIGRAVITY_FULL_CONTAINER_ALLOW_RULES),
            "deny": [],
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
    """Validate isolated OAuth state plus the full-container policy."""

    auth = antigravity_auth_path(home)
    issue = _private_tree_error(auth)
    if issue is not None:
        return issue
    settings = antigravity_settings_path(home)
    try:
        payload = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"Antigravity full-container settings are unavailable or invalid: {exc}"
    expected = antigravity_settings_payload()
    if payload != expected:
        return _ANTIGRAVITY_POLICY_MISMATCH
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
        != sorted(ANTIGRAVITY_RUNTIME_MODELS_BY_MODEL.values())
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
                "models": sorted(ANTIGRAVITY_RUNTIME_MODELS_BY_MODEL.values()),
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
    # AGY maintains this exact convenience link to its rotating log.  It is
    # unnecessary in the credential mount and would make the otherwise
    # symlink-free tree fail closed after every real login.  Unlink the known
    # path without following it; every other symlink remains an error.
    cli_log = antigravity_auth_path(home) / "antigravity-cli" / "cli.log"
    try:
        if stat.S_ISLNK(cli_log.lstat().st_mode):
            cli_log.unlink()
    except FileNotFoundError:
        pass


def _harden_antigravity_entry(path: Path, info: os.stat_result, mode: int) -> None:
    """Make owned state private without fighting a live container's UID.

    Linux bind mounts preserve the UID written by the process inside the
    container.  A root-running official CLI can therefore create volatile
    conversation/media files owned by root even though its command starts
    under ``umask 077``.  The host user cannot chmod those files, but their
    existing owner-only mode is already private.  Accept that exact safe
    state; continue to fail closed for any foreign-owned entry with group or
    world bits, and keep hardening every entry owned by this process.
    """
    if os.name == "nt":
        return
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError(
                "Antigravity OAuth state contains a foreign-owned entry "
                f"that is too broadly accessible: {path}"
            )
        return
    os.chmod(path, mode)


def privatize_antigravity_home(home: Path | None = None) -> None:
    root = antigravity_home(home)
    if not root.exists():
        return
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
            _harden_antigravity_entry(path, info, 0o700)
        elif stat.S_ISREG(info.st_mode):
            # The reviewed AGY Linux binary is cached below ``runtime``.
            # Keep that one file owner-executable after credential hardening;
            # Docker Desktop preserves the host mode on a read-only bind
            # mount. Every OAuth/config/proof file remains owner-readable only.
            executable = (
                path.name == "antigravity" and runtime in path.parents
            )
            _harden_antigravity_entry(
                path, info, 0o700 if executable else 0o600,
            )
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


def prepare_antigravity_auth(home: Path | None = None) -> str | None:
    """Migrate versioned DRadar policy, then return the remaining auth issue.

    OAuth credentials and readiness evidence are never repaired here.  Only a
    private, structurally valid DRadar-owned tree whose settings differ from
    the current reviewed policy is rewritten.  This must run before doctor,
    capability advertisement, and provider status so an ordinary CLI upgrade
    cannot strand a valid account before the per-trial context is entered.
    """

    # The official CLI recreates its convenience log link and may write new
    # log files with process-default permissions. Normalize those modes and
    # remove only the reviewed link before strict validation; unsafe links or
    # special files still fail closed inside ``privatize_antigravity_home``.
    try:
        privatize_antigravity_home(home)
    except (OSError, ValueError) as exc:
        return f"could not harden Antigravity OAuth state: {exc}"

    issue = antigravity_auth_error(home)
    if issue != _ANTIGRAVITY_POLICY_MISMATCH:
        return issue
    try:
        restore_antigravity_settings(home)
    except (OSError, ValueError) as exc:
        return f"could not migrate Antigravity full-container policy: {exc}"
    return antigravity_auth_error(home)


def parse_antigravity_cli_version(output: str) -> str | None:
    match = _ANTIGRAVITY_VERSION_RE.search(output.strip())
    return match.group(1) if match else None


@contextmanager
def antigravity_subscription_session(
    directory: Path, *, home: Path | None = None,
):
    """Expose only DRadar's validated .gemini tree to one Pier trial."""

    issue = prepare_antigravity_auth(home)
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


def claude_home(home: Path | None = None) -> Path:
    """Return DRadar's dedicated Claude subscription credential root."""

    if home is None:
        home = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
    return Path(home) / CLAUDE_HOME_RELATIVE_PATH


def claude_oauth_path(home: Path | None = None) -> Path:
    return claude_home(home) / CLAUDE_OAUTH_TOKEN_FILENAME


def claude_oauth_error(path: Path | None = None) -> str | None:
    """Validate an owner-only Claude.ai subscription setup token.

    The token is generated by the official ``claude setup-token`` command. It
    is not an Anthropic Console API key and is never accepted from ambient
    environment variables, assignment payloads, or server responses.
    """

    path = claude_oauth_path() if path is None else path
    try:
        info = path.lstat()
    except FileNotFoundError:
        return f"Claude Code subscription OAuth is not configured at {path}"
    except OSError as exc:
        return f"cannot inspect {path}: {exc}"
    if stat.S_ISLNK(info.st_mode):
        return f"{path} must be a regular file, not a symlink"
    if not stat.S_ISREG(info.st_mode):
        return f"{path} must be a regular file"
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        return f"{path} is too broadly readable; run: chmod 600 {path}"
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        return f"Claude OAuth credential is unreadable: {exc}"
    if not token.startswith("sk-ant-oat") or len(token) < 40:
        return (
            "Claude credential is not an official Claude.ai subscription "
            "OAuth setup token"
        )
    return None


def store_claude_oauth_token(
    token: str, *, home: Path | None = None,
) -> Path:
    """Atomically store a Claude subscription OAuth token with mode 0600."""

    normalized = token.strip()
    if not normalized.startswith("sk-ant-oat") or len(normalized) < 40:
        raise ValueError(
            "expected the OAuth token produced by `claude setup-token`; "
            "Anthropic Console API keys are not supported"
        )
    target = claude_oauth_path(home)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_info = target.parent.lstat()
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise ValueError("Claude credential directory must be a real directory")
    if os.name != "nt":
        os.chmod(target.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix=".oauth-token-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(normalized + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return target


@contextmanager
def claude_subscription_session(directory: Path, *, home: Path | None = None):
    """Expose the canonical OAuth token to one host-side Pier adapter.

    Claude setup tokens do not rotate in place, so concurrent trials may read
    the same owner-only file. The adapter transfers only the value to Claude's
    process environment inside the disposable task container.
    """

    canonical = claude_oauth_path(home)
    issue = claude_oauth_error(canonical)
    if issue is not None:
        raise ValueError(issue + "; run `dradar provider setup claude` first")
    directory.mkdir(parents=True, exist_ok=True)
    yield canonical
    issue = claude_oauth_error(canonical)
    if issue is not None:
        raise ValueError("Claude OAuth credential became unsafe: " + issue)


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
    if managed.is_file() and not os.access(managed, os.X_OK) and os.name != "nt":
        # CLI 0.5.138 briefly reconciled the whole Grok home as private data,
        # which could turn this exact user-owned managed executable from 0700
        # into 0600. Repair only that known mode/owner combination; never make
        # an unknown, symlinked, foreign-owned, or broadly accessible file
        # executable.
        try:
            info = managed.lstat()
            getuid = getattr(os, "getuid", None)
            if (
                stat.S_ISREG(info.st_mode)
                and callable(getuid)
                and info.st_uid == getuid()
                and stat.S_IMODE(info.st_mode) == 0o600
            ):
                os.chmod(managed, 0o700)
        except OSError:
            pass
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


def _configured_kimi_auth_path() -> Path | None:
    value = os.environ.get(KIMI_CREDENTIAL_PATH_ENV, "").strip()
    return Path(value).expanduser() if value else None


def _kimi_home_from_auth_path(path: Path) -> Path | None:
    if (
        not path.is_absolute()
        or path.name != KIMI_AUTH_RELATIVE_PATH.name
        or path.parent.name != KIMI_AUTH_RELATIVE_PATH.parent.name
    ):
        return None
    return path.parent.parent


def kimi_home(home: Path | None = None) -> Path:
    """Return the account-scoped Kimi provider home.

    ``DRADAR_HOME`` remains the default for ordinary single-profile users.
    Operators that keep campaign state in separate DRADAR_HOME directories
    must point every campaign for the same Kimi account at one explicit
    ``DRADAR_KIMI_HOME`` or the same absolute ``KIMI_CREDENTIAL_PATH``.
    Kimi rotates refresh tokens, so copying its OAuth JSON into
    campaign-local homes creates mutually stale credential forks.
    """

    if home is None:
        explicit_auth = _configured_kimi_auth_path()
        if explicit_auth is not None:
            explicit_home = _kimi_home_from_auth_path(explicit_auth)
            if explicit_home is not None:
                return explicit_home
            # ``kimi_auth_error()`` reports the malformed binding before any
            # provider process can use this fallback.  Keep path resolution
            # deterministic so diagnostics never silently inspect another
            # account's default credential.
            return explicit_auth.parent.parent
        account_home = os.environ.get(KIMI_ACCOUNT_HOME_ENV)
        if account_home:
            return Path(account_home).expanduser()
        home = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
    return Path(home) / KIMI_HOME_RELATIVE_PATH


def kimi_auth_path(home: Path | None = None) -> Path:
    if home is None:
        explicit_auth = _configured_kimi_auth_path()
        if explicit_auth is not None:
            return explicit_auth
    return kimi_home(home) / KIMI_AUTH_RELATIVE_PATH


def _valid_kimi_auth_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return all(
        isinstance(payload.get(name), str) and bool(payload[name].strip())
        for name in ("access_token", "refresh_token", "token_type")
    )


def _revoked_kimi_auth_payload(payload: object) -> bool:
    """Recognize the official CLI's invalid-grant tombstone."""

    if not isinstance(payload, dict):
        return False
    return (
        payload.get("access_token") == ""
        and payload.get("refresh_token") == ""
        and payload.get("expires_at") == 0
        and payload.get("expires_in") == 0
        and isinstance(payload.get("token_type"), str)
        and bool(payload["token_type"].strip())
    )


def kimi_auth_error(path: Path | None = None) -> str | None:
    """Fail closed for unsafe or non-refreshable Kimi OAuth credentials."""

    if path is None:
        explicit_auth = _configured_kimi_auth_path()
        if (
            explicit_auth is not None
            and _kimi_home_from_auth_path(explicit_auth) is None
        ):
            return (
                f"{KIMI_CREDENTIAL_PATH_ENV} must be an absolute "
                "credentials/kimi-code.json path"
            )
        path = kimi_auth_path()
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
    getuid = getattr(os, "getuid", None)
    if os.name != "nt" and callable(getuid) and info.st_uid != getuid():
        return f"{path} must be owned by the current user"
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        return f"{path} is too broadly readable; run: chmod 600 {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"Kimi credential is unreadable or invalid JSON: {exc}"
    if _revoked_kimi_auth_payload(payload):
        return (
            "Kimi OAuth refresh was rejected; run "
            "`dradar provider setup kimi` to authenticate again"
        )
    if not _valid_kimi_auth_payload(payload):
        return "Kimi credential is not a refreshable subscription OAuth session"
    return None


def _run_kimi_live_probe(
    executable: str | Path,
    credential: Path,
    data_home: Path,
) -> str | None:
    native_auth = data_home / KIMI_AUTH_RELATIVE_PATH
    if native_auth != credential:
        return "Kimi OAuth credential is outside the account-scoped home"
    env = provider_subprocess_env()
    env["KIMI_CODE_HOME"] = str(data_home)
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
    if proc.returncode != 0:
        output = (proc.stdout + "\n" + proc.stderr).lower()
        if any(marker in output for marker in ("unauthorized", "invalid_grant", "rejected")):
            return "Kimi OAuth session was rejected; run `dradar provider setup kimi`"
        return "Kimi OAuth refresh failed; check this machine's network/proxy"
    try:
        config_text = (data_home / "config.toml").read_text(encoding="utf-8")
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
    data_home = kimi_home()
    if auth_path is not None:
        if (
            canonical.name != KIMI_AUTH_RELATIVE_PATH.name
            or canonical.parent.name != KIMI_AUTH_RELATIVE_PATH.parent.name
        ):
            return "Kimi OAuth credential is outside an account-scoped home"
        data_home = canonical.parent.parent
    if auth_path is None:
        try:
            with tempfile.TemporaryDirectory(
                prefix="dradar-kimi-probe-work-"
            ) as name:
                with kimi_subscription_session(Path(name) / "slot") as shared:
                    return _run_kimi_live_probe(
                        cli, shared, data_home,
                    )
        except ValueError as exc:
            return str(exc)
    return _run_kimi_live_probe(cli, canonical, data_home)


def kimi_cli_path(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    explicit = env.get("KIMI_CLI_PATH")
    if explicit:
        return explicit
    if env.get(KIMI_ACCOUNT_HOME_ENV):
        executable = "kimi.exe" if os.name == "nt" else "kimi"
        managed = (
            Path(env[KIMI_ACCOUNT_HOME_ENV]).expanduser()
            / "runtime" / KIMI_CLI_VERSION / "bin" / executable
        )
    else:
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

    executable = "kimi.exe" if os.name == "nt" else "kimi"
    if home is None:
        if os.environ.get(KIMI_ACCOUNT_HOME_ENV):
            root = kimi_home()
        else:
            dradar_home = Path(
                os.environ.get("DRADAR_HOME", Path.home() / ".dradar")
            )
            root = dradar_home / KIMI_HOME_RELATIVE_PATH
    else:
        root = Path(home) / KIMI_HOME_RELATIVE_PATH
    return root / "runtime" / KIMI_CLI_VERSION / "bin" / executable


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
    body_failed = False
    try:
        yield canonical
    except BaseException:
        # Preserve the provider/Pier exception and its stderr.  In particular,
        # Kimi writes a revoked tombstone after invalid_grant; replacing the
        # original exception with a structural JSON error hides the root cause.
        body_failed = True
        raise
    finally:
        if os.name != "nt":
            # A concurrent rootful task container may have just atomically
            # replaced the shared credential.  Its in-container ownership guard
            # repairs the new inode; allow that bounded handoff to settle before
            # treating a transient EACCES as an invalid OAuth refresh.
            deadline = time.monotonic() + 1.0
            while not os.access(canonical, os.R_OK) and time.monotonic() < deadline:
                time.sleep(0.02)
        issue = kimi_auth_error(canonical)
        if issue is not None and not body_failed:
            if issue.startswith("Kimi OAuth refresh was rejected"):
                raise ValueError(issue)
            raise ValueError(
                "Kimi returned an invalid refreshed OAuth credential: " + issue
            )


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

    The pinned Grok CLI coordinates refreshes with ``auth.json.lock`` next to the
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
    """Advertise protocol support plus integrity-checked paid runtimes."""

    # Every current CLI can fetch an immutable pin from DRadar's canonical
    # public task repository.  Servers activate this marker only when their
    # configured task package requires the new distribution path, allowing a
    # CLI-first rolling upgrade while old servers harmlessly ignore it.
    capabilities = [TASK_PACKAGE_SYNC_CAPABILITY]
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
        ))
    # Unlike API-key providers, a subscription slot is scarce and stateful.
    # Advertise it only when both the CLI and a safe refreshable OAuth session
    # are actually present, preventing the server from assigning unusable work.
    if grok_cli_path(environ) and grok_auth_error() is None:
        capabilities.append(GROK_CAPABILITY)
    if (
        claude_oauth_error() is None
        and Path(__file__).with_name("pier_claude.py").is_file()
    ):
        capabilities.append(CLAUDE_CAPABILITY)
    if kimi_cli_path(environ) and kimi_auth_error() is None:
        capabilities.append(KIMI_CAPABILITY)
    if (
        prepare_antigravity_auth() is None
        and Path(__file__).with_name("pier_antigravity.py").is_file()
    ):
        capabilities.append(ANTIGRAVITY_CAPABILITY)
    if (
        zcode_api_key(environ) is not None
        and zcode_cli_error(environ=environ) is None
        and Path(__file__).with_name("pier_zcode.py").is_file()
    ):
        capabilities.append(ZCODE_CAPABILITY)
    codebuddy_cli = codebuddy_executable(environ)
    codebuddy_cli_issue, _codebuddy_cli_version = codebuddy_host_cli_status(
        codebuddy_cli,
    )
    codebuddy_credentials_ready, _ = codebuddy_credential_status()
    if (
        codebuddy_cli
        and codebuddy_cli_issue is None
        and codebuddy_credentials_ready
        and codebuddy_runtime_image_error() is None
        and Path(__file__).with_name("pier_codebuddy.py").is_file()
    ):
        capabilities.append(CODEBUDDY_CAPABILITY)
    return tuple(capabilities)


def normalize_capabilities(values: Iterable[str]) -> tuple[str, ...]:
    """Return a deterministic, header-safe capability list."""

    return tuple(sorted({
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip() and "," not in value
    }))
