"""Run one benchmark trial locally via pier and collect submission artifacts.

The volunteer client runs agent-only (`--disable-verification`); grading is
server-side. model.patch is produced inside the container by the task's own
pre_artifacts.sh, then downloaded by pier into the trial dir.
"""

import glob
import hashlib
import json
import math
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from . import egress, image_cache
from .codebuddy_provider import (
    CODEBUDDY_AGENT,
    CODEBUDDY_API_KEY_ENVS,
    CODEBUDDY_CLI_VERSION,
    CODEBUDDY_CONTAINER_IMAGE,
    CODEBUDDY_MODEL,
    CODEBUDDY_PROVIDER,
    CODEBUDDY_SOURCE_IMAGE_ENV,
    CODEBUDDY_SUPPORTED_EFFORTS,
    codebuddy_runtime_image_error,
    codebuddy_subscription_session,
)
from .manifest import task_content_hash
from .providers import (
    ANTIGRAVITY_AGENT,
    ANTIGRAVITY_CLI_VERSION,
    ANTIGRAVITY_MODEL,
    ANTIGRAVITY_PROVIDER,
    ANTIGRAVITY_RUNTIME_MODELS,
    ANTIGRAVITY_SUPPORTED_EFFORTS,
    CLAUDE_AGENT,
    CLAUDE_API_KEY_ENVS,
    CLAUDE_CLI_VERSION,
    CLAUDE_MODELS,
    CLAUDE_PROVIDER,
    CLAUDE_SUPPORTED_EFFORTS,
    DEFAULT_CODEX_PROVIDER,
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_CATALOG_REMOTE_PATH,
    DEEPSEEK_MIN_CODEX_VERSION,
    DEEPSEEK_MODEL,
    DEEPSEEK_MODELS,
    DEEPSEEK_PROVIDER,
    DEEPSEEK_SUPPORTED_EFFORTS,
    deepseek_codex_reasoning_effort,
    DSH_AGENT,
    DSH_MODELS,
    DSH_VISION_MODEL,
    DSH_SUPPORTED_EFFORTS,
    DSH_VERSION,
    GROK_AGENT,
    GROK_API_KEY_ENV,
    GROK_CLI_VERSION,
    GROK_MODEL,
    GROK_PROVIDER,
    GROK_SUPPORTED_EFFORTS,
    KIMI_AGENT,
    KIMI_API_KEY_ENVS,
    KIMI_CLI_VERSION,
    KIMI_MODEL,
    KIMI_PROVIDER,
    KIMI_SUPPORTED_EFFORTS,
    ZCODE_AGENT,
    ZCODE_API_KEY_ENV,
    ZCODE_CLI_VERSION,
    ZCODE_MODEL,
    ZCODE_MODELS,
    ZCODE_PROVIDER,
    ZCODE_SUPPORTED_EFFORTS,
    assignment_codex_provider,
    antigravity_subscription_session,
    claude_subscription_session,
    create_deepseek_api_key_file,
    create_deepseek_auth_json,
    create_zcode_api_key_file,
    deepseek_catalog_error,
    deepseek_catalog_path,
    grok_cli_path,
    grok_live_error,
    grok_subscription_session,
    kimi_auth_path,
    kimi_cli_path,
    kimi_home,
    kimi_live_error,
    kimi_subscription_session,
    parse_kimi_cli_version,
    parse_grok_cli_version,
    parse_zcode_cli_version,
    prepare_antigravity_auth,
    zcode_cli_error,
    zcode_cli_path,
    zcode_cli_version_is_compatible,
)

# The egress allowlist alone does NOT stop the agent from searching the web:
# codex/Claude web tools execute server-side (at OpenAI/Anthropic), riding the
# same allowed API channel. So we also disable server-side tools at the
# agent-config layer. Codex's key is a TOP-LEVEL string
# `web_search = "disabled"` (verified
# behaviourally: with it, codex makes zero web_search calls and reports no web
# tool). It MUST come before any [table] header or TOML nests it into that
# table; pier appends this block first into an otherwise-empty config.toml.
# Apps/connectors have the same server-side property and can otherwise reach
# services such as GitHub despite the task container's network policy, so the
# stable `features.apps = false` switch must stay disabled as well.
# Server-side trajectory audit is the backstop if a client tampers with this.
ALLOWLIST_TOML = (
    'web_search = "disabled"\n'
    '[features]\n'
    'apps = false\n'
    'remote_plugin = false\n'
    '[__pier_allowlist]\n'
    'url = "https://chatgpt.com"\n'
)
_DSH_IMAGE_ATTACHMENT_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _resolve_user_tool(name: str, *, home: Path | None = None) -> str | None:
    """Resolve a CLI, including uv's per-user bin directory.

    Non-interactive SSH and systemd-launched shells commonly omit
    ``~/.local/bin`` even though ``uv tool install`` puts executables there.
    Keep the normal PATH result authoritative, then inspect only the current
    user's explicit/default uv tool directory when it was not already searched.
    """
    discovered = shutil.which(name)
    if discovered:
        return discovered

    user_home = home or Path.home()
    candidate_dirs: list[Path] = []
    configured = os.environ.get("UV_TOOL_BIN_DIR")
    if configured:
        candidate_dirs.append(Path(configured).expanduser())
    candidate_dirs.append(user_home / ".local" / "bin")

    searched_dirs = {
        str(Path(item).expanduser())
        for item in os.environ.get("PATH", "").split(os.pathsep)
        if item
    }
    suffixes = (".exe", "") if sys.platform == "win32" else ("",)
    for directory in candidate_dirs:
        if str(directory) in searched_dirs:
            continue
        for suffix in suffixes:
            candidate = directory / f"{name}{suffix}"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None

# Public-safe DeepSeek configuration for Pier's stock Codex agent.
# The official catalog is uploaded to this container-local path before Codex
# starts; do not add manual context/compaction/reasoning-summary overrides here,
# because DeepSeek's official setup script explicitly removes them when the
# catalog is active. Codex reads the provider credential from an uploaded
# auth.json because ``requires_openai_auth`` is true; no API-key value is
# passed through argv or ``docker compose exec -e``.
DEEPSEEK_TOML = (
    'web_search = "disabled"\n'
    'model_provider = "deepseek"\n'
    'preferred_auth_method = "apikey"\n'
    'forced_login_method = "api"\n'
    f'model_catalog_json = "{DEEPSEEK_CATALOG_REMOTE_PATH}"\n'
    '[features]\n'
    'apps = false\n'
    'remote_plugin = false\n'
    '[model_providers.deepseek]\n'
    'name = "deepseek"\n'
    'base_url = "https://api.deepseek.com/"\n'
    'wire_api = "responses"\n'
    'requires_openai_auth = true\n'
)
DEEPSEEK_AGENT_IMPORT_PATH = "_dradar_pier_deepseek:DeepSeekCodex"
DEEPSEEK_AGENT_MODULE_FILENAME = "_dradar_pier_deepseek.py"
CLAUDE_AGENT_IMPORT_PATH = "_dradar_pier_claude:ClaudeCodeSubscription"
CLAUDE_AGENT_MODULE_FILENAME = "_dradar_pier_claude.py"
CLAUDE_USAGE_MODULE_FILENAME = "_dradar_claude_usage.py"
GROK_AGENT_IMPORT_PATH = "_dradar_pier_grok:GrokBuild"
GROK_AGENT_MODULE_FILENAME = "_dradar_pier_grok.py"
GROK_RECOVERY_MODULE_FILENAME = "_dradar_grok_recovery.py"
KIMI_AGENT_IMPORT_PATH = "_dradar_pier_kimi:KimiCode"
KIMI_AGENT_MODULE_FILENAME = "_dradar_pier_kimi.py"
KIMI_RECOVERY_MODULE_FILENAME = "_dradar_kimi_recovery.py"
ANTIGRAVITY_AGENT_IMPORT_PATH = "_dradar_pier_antigravity:Antigravity"
ANTIGRAVITY_AGENT_MODULE_FILENAME = "_dradar_pier_antigravity.py"
SHARED_OAUTH_ENV_IMPORT_PATH = (
    "_dradar_pier_shared_oauth_docker:SharedOAuthDockerEnvironment"
)
SHARED_OAUTH_ENV_MODULE_FILENAME = "_dradar_pier_shared_oauth_docker.py"
ZCODE_AGENT_IMPORT_PATH = "_dradar_pier_zcode:ZCodeBigModel"
ZCODE_AGENT_MODULE_FILENAME = "_dradar_pier_zcode.py"
DSH_AGENT_IMPORT_PATH = "_dradar_pier_dsh:DshMinimal"
DSH_AGENT_MODULE_FILENAME = "_dradar_pier_dsh.py"
RUNTIME_SAFETY_MODULE_FILENAME = "_dradar_pier_runtime_safety.py"
CODEBUDDY_AGENT_IMPORT_PATH = (
    "_dradar_pier_codebuddy:CodeBuddySubscription"
)
CODEBUDDY_AGENT_MODULE_FILENAME = "_dradar_pier_codebuddy.py"
BETA_SUBSCRIPTION_TRIAL_TIMEOUT_FLOOR_SEC = 120 * 60
# A cold multi-worker BuildKit start can spend tens of minutes pulling base
# images and package layers.  Three task windows (90 minutes for the common
# 1800s task declaration) gives slow mirrors room to recover while the
# bounded 1..8 override and two-attempt Pier retry still prevent a wedged
# daemon from holding a lease forever.
DEFAULT_ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER = 3.0
DEFAULT_ENVIRONMENT_BUILD_TIMEOUT_SEC = 600.0
PIER_ENVIRONMENT_START_ATTEMPTS = 2
ENVIRONMENT_BUILD_WATCHDOG_SLACK_SEC = 120
BETA_SUBSCRIPTION_AGENTS = frozenset({
    CLAUDE_AGENT, GROK_AGENT, KIMI_AGENT, ZCODE_AGENT, ANTIGRAVITY_AGENT,
    CODEBUDDY_AGENT,
})

# Public Pier 0.3.0 only runs ``<task>/pre_artifacts.sh`` when verification is
# disabled.  Current DeepSWE packs express the same public collection command
# as ``[[verifier.collect]]`` instead, but DRadar must keep the verifier itself
# disabled on volunteer machines.  Per-run overlays therefore install this
# narrow, deterministic collector without mutating the shared task checkout.
DSH_PRE_ARTIFACTS_SCRIPT = """#!/bin/sh
set -eu
cd /app
mkdir -p /logs/artifacts
base_ref='__DRADAR_BASE_COMMIT__'
if [ -n "$base_ref" ]; then
  base=$(git rev-parse --verify "${base_ref}^{commit}")
else
  base=$(git rev-list --max-parents=0 HEAD | tail -1)
fi
git diff --binary "$base" HEAD > /logs/artifacts/model.patch
"""

# Antigravity frequently finishes with a valid implementation still staged or
# uncommitted even though the prompt asks it to commit.  The published task
# hook only compares the starting commit with HEAD, silently turning that work
# into an empty patch.  Use a provider-owned hook that snapshots the complete
# final worktree. ``git add -N`` makes new, non-ignored files visible to
# ``git diff`` without staging their contents or creating a commit.
ANTIGRAVITY_PRE_ARTIFACTS_SCRIPT = """#!/bin/sh
set -eu
cd /app
mkdir -p /logs/artifacts
base_ref='__DRADAR_BASE_COMMIT__'
base=$(git rev-parse --verify "${base_ref}^{commit}")
git add -N -- .
git diff --binary "$base" -- > /logs/artifacts/model.patch
"""

# Claude Code: deny the web tools (and keep pier's default EnterPlanMode deny).
CLAUDE_DISALLOWED_TOOLS = "WebSearch WebFetch EnterPlanMode"

CODEX_NPM_LATEST_URL = "https://registry.npmjs.org/@openai%2Fcodex/latest"
CODEX_VERSION_LOOKUP_ATTEMPTS = 3
CODEX_VERSION_LOOKUP_TIMEOUT_SEC = 10.0
CODEX_VERSION_FALLBACK_LOOKUP_TIMEOUT_SEC = 3.0
_STABLE_CODEX_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

CODEX_SUBMISSION_PROMPT = """{{ instruction }}

Before finishing, ensure the requested implementation is complete and committed.
DRadar/Pier creates the submission artifact automatically after your turn. Do
not wait for or invoke `/tests/pre_artifacts.sh`, and do not create or inspect
`/logs/artifacts/model.patch` yourself.
"""

POMPEII_BENCHMARK_ID = "pompeii-adjacency"
POMPEII_SOFT_BUDGET_SEC = 90 * 60
POMPEII_AGENT_TIMEOUT_SEC = 120 * 60
KIMI_POMPEII_AGENT_TIMEOUT_SEC = 240 * 60
POMPEII_TERMINAL_HEAVY_TIMEOUT_SEC = 10 * 60
POMPEII_FINALIZATION_RESERVE_SEC = 10 * 60
POMPEII_OUTER_WATCHDOG_SLACK_SEC = 15 * 60
POMPEII_SUBMISSION_PROMPT = CODEX_SUBMISSION_PROMPT + """

Time budget: aim to complete and commit a valid answer within 90 minutes.
After 90 minutes, this run may stop at any time; agent execution will stop no later than 120 minutes.
For bounded, compute-intensive offline work such as image stitching or grid
search, set the individual terminal-tool timeout to at most 600 seconds when
needed. Keep at least 10 minutes in reserve to persist the best answer, validate
its format, and commit it. Do not lengthen ordinary commands or start another
long computation once that reserve begins.
Keep the best current answer persisted in the repository. As
the deadline approaches, do not start time-consuming new experiments; use your
best current judgment to finish and commit. A complete, gradeable answer takes
priority over further exploration.
"""

ZCODE_POMPEII_SUBMISSION_PROMPT = POMPEII_SUBMISSION_PROMPT + """

This benchmark has exactly one declared deliverable: `model_answer.json` in
the repository root. Derive the answer independently from the task inputs and
write only that final JSON file. Do not modify or commit `question.png`,
`output_schema.json`, anything under `reference/`, generated images, caches,
logs, or intermediate analysis artifacts. Keep temporary image-processing and
analysis files outside `/app` (for example under `/tmp`) and remove any
accidental repository artifacts before committing.

Before finishing, verify that `git diff --name-only pompeii-base HEAD` prints
exactly `model_answer.json`, and that the file conforms to
`output_schema.json`. Do not reuse another model's answer.
"""


@dataclass
class TrialArtifacts:
    job_dir: Path
    trial_dir: Path
    patch: Path
    trajectory: Path | None
    result: Path | None
    returncode: int
    duration_sec: float
    log_path: Path
    codex_cli_version: str | None = None
    grok_cli_version: str | None = None
    kimi_cli_version: str | None = None
    antigravity_cli_version: str | None = None
    zcode_cli_version: str | None = None
    zcode_cli_sha256: str | None = None
    dsh_version: str | None = None
    codebuddy_cli_version: str | None = None
    dsh_artifact_binding: dict[str, object] | None = None
    # External/test adapters that construct TrialArtifacts predate the builder
    # field and represent an already-contained run. The real run_trial path
    # always sets this explicitly from prepare_trial_builder.
    builder_isolated: bool = True
    builder_note: str | None = None
    builder_reusable: bool = False
    builder_name: str | None = None
    builder_expected: bool = True


class RunnerError(RuntimeError):
    def __init__(
        self, *args: object, failure_diagnostic: dict[str, object] | None = None,
    ) -> None:
        super().__init__(*args)
        self.failure_diagnostic = failure_diagnostic


class RunnerCleanupUnconfirmedError(RunnerError):
    """The local runtime may still be alive, so its lease must stay running."""


class RunnerTaskRetryableError(RunnerError):
    """The exact local runtime is gone, so only this assignment must retry."""


class LiveAccountTerminalError(RunnerError):
    """A running agent reported an account-wide terminal provider failure."""


def resolve_latest_codex_cli_version(
    server_version: str | None = None,
    server_version_verified: bool = False,
) -> str:
    """Resolve npm's current stable Codex CLI tag to an exact version.

    Pier installs the agent in a Docker build layer. Passing the literal
    ``latest`` leaves that layer cacheable forever, while an exact version
    changes the install command whenever npm's stable tag moves. Refuse to
    start when neither the registry nor a freshly verified server pin is
    available: silently using an older image could consume a volunteer's
    quota before Codex rejects the model.
    """
    last_error: Exception | None = None
    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    trusted_server_version = (
        server_version
        if (
            server_version_verified
            and isinstance(server_version, str)
            and _STABLE_CODEX_VERSION_RE.fullmatch(server_version)
        )
        else None
    )
    for attempt in range(1, CODEX_VERSION_LOOKUP_ATTEMPTS + 1):
        try:
            response = httpx.get(
                CODEX_NPM_LATEST_URL,
                headers=headers,
                timeout=(
                    CODEX_VERSION_FALLBACK_LOOKUP_TIMEOUT_SEC
                    if trusted_server_version
                    else CODEX_VERSION_LOOKUP_TIMEOUT_SEC
                ),
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
            version = payload.get("version") if isinstance(payload, dict) else None
            if not isinstance(version, str) or not _STABLE_CODEX_VERSION_RE.fullmatch(
                version
            ):
                raise ValueError(
                    f"npm returned a non-stable or malformed version: {version!r}"
                )
            return version
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            # The server refreshes this exact pin once a minute and only marks
            # it verified while that check is still fresh. This avoids making
            # every volunteer's local proxy/TLS path a single point of failure
            # without trusting a static or stale server config value.
            if trusted_server_version:
                return trusted_server_version
            if attempt < CODEX_VERSION_LOOKUP_ATTEMPTS:
                time.sleep(0.5 * attempt)
    raise RunnerError(
        "could not verify npm's latest stable Codex CLI version after "
        f"{CODEX_VERSION_LOOKUP_ATTEMPTS} attempts; refusing to start an "
        "outdated agent container so no model quota is consumed. Check access "
        "to registry.npmjs.org, then run `dradar resume`."
    ) from last_error


class BuildFlakeError(RunnerError):
    """The trial died while BUILDING the task/proxy image — the agent never
    started, so zero quota was consumed. Distinct from RunnerError because
    the caller may retry it once for free; a Chinese-network ARM Mac hitting
    ports.ubuntu.com is the canonical case (volunteer report, 2026-07-14:
    this used to surface as 'model.patch missing (agent likely failed)',
    blaming the agent for a mirror hiccup)."""


class BuildDiskFullError(RunnerError):
    """The trial died while BUILDING because the host disk was full.

    Distinct from BuildFlakeError: retrying cannot create space, and BuildKit
    ENOSPC lines almost always also contain ``failed to solve``, which would
    otherwise be misread as a mirror/network flake.
    """


class BuildSnapshotterPermissionError(RunnerError):
    """Isolated overlay/fuse-overlayfs cannot write whiteouts here.

    Nested Docker often boots BuildKit, then fails on ``operation not
    permitted`` while converting whiteouts. Distinct from BuildFlakeError
    because retrying the same isolated builder cannot create that permission.
    """


# Signatures (in the pier log tail) of an image build / infra failure that
# happened before any agent ran. Deliberately specific: a false positive here
# would auto-retry a run that DID burn quota.
_BUILD_FLAKE_MARKERS = (
    "ports.ubuntu.com", "archive.ubuntu.com", "failed to solve",
    "apt-get update", "Temporary failure resolving", "proxyconnect",
    "TLS handshake timeout", "error getting credentials",
)
_DISK_FULL_MARKERS = (
    "no space left",
    "enospc",
    "disk quota exceeded",
)
_SNAPSHOTTER_PERM_MARKERS = (
    "whiteout",
    "fuse-overlayfs",
)


def _looks_like_disk_full(log_tail: str) -> bool:
    lowered = log_tail.lower()
    return any(marker in lowered for marker in _DISK_FULL_MARKERS)


def _looks_like_snapshotter_permission(log_tail: str) -> bool:
    lowered = log_tail.lower()
    if "operation not permitted" not in lowered and "eperm" not in lowered:
        return False
    return any(marker in lowered for marker in _SNAPSHOTTER_PERM_MARKERS)


def _looks_like_build_flake(log_tail: str) -> bool:
    if _looks_like_disk_full(log_tail) or _looks_like_snapshotter_permission(log_tail):
        return False
    return any(m in log_tail for m in _BUILD_FLAKE_MARKERS)


def _result_exception_text(result_path: Path | None) -> str:
    """The Pier console tail can end before Docker's actual build error.

    Pier preserves the full setup exception in result.json, so inspect that
    structured source as well. This is diagnostic-only: classification still
    requires one of the deliberately narrow build markers above.
    """
    if not result_path or not result_path.is_file():
        return ""
    try:
        data = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    info = data.get("exception_info") or {}
    if not isinstance(info, dict):
        return ""
    return "\n".join(str(info.get(key) or "") for key in (
        "exception_type", "exception_message", "exception_traceback"))


def _diagnostic_tail(text: str, max_chars: int = 4000) -> str:
    return text[-max_chars:]


def codex_auth_path() -> Path:
    """Where codex keeps its auth (CODEX_AUTH_JSON_PATH overrides the default).
    Shared with doctor so its "agent ready" verdict tests the exact condition
    `dradar go` enforces."""
    return Path(os.environ.get("CODEX_AUTH_JSON_PATH", Path.home() / ".codex" / "auth.json"))


def _materialize_shared_file(path: Path, data: bytes, mode: int = 0o600) -> Path:
    """Publish deterministic runner input without exposing partial contents.

    ``HOME / work`` is shared by supervised workers.  A direct ``write_text``
    or ``copyfile`` truncates the common destination before writing, so one
    Pier process can observe another process's half-written adapter/config.
    Reuse an identical regular file; otherwise write a uniquely named file in
    the same directory and make ``os.replace`` the single publication point.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and path.read_bytes() == data:
            if os.name != "nt":
                os.chmod(path, mode)
            return path
    except OSError:
        pass

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, mode)
        handle = os.fdopen(fd, "wb")
        fd = -1
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = -1
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    finally:
        if fd >= 0:
            os.close(fd)
    return path


def _ensure_allowlist(home: Path) -> Path:
    path = home / "codex-chatgpt-allowlist.toml"
    return _materialize_shared_file(path, ALLOWLIST_TOML.encode())


def _ensure_codex_submission_prompt(
    home: Path, benchmark_id: str | None = None,
) -> Path:
    if benchmark_id == POMPEII_BENCHMARK_ID:
        # Keep the benchmark-specific prompt at its own immutable path. Workers
        # from two benchmark channels can share DRADAR_HOME, so overwriting the
        # generic prompt in place would create a cross-run race.
        path = home / "codex-submission-prompt-pompeii-v2.j2"
        prompt = POMPEII_SUBMISSION_PROMPT
    else:
        path = home / "codex-submission-prompt.j2"
        prompt = CODEX_SUBMISSION_PROMPT
    return _materialize_shared_file(path, prompt.encode())


def _ensure_zcode_submission_prompt(
    home: Path, benchmark_id: str | None = None,
) -> Path:
    if benchmark_id != POMPEII_BENCHMARK_ID:
        return _ensure_codex_submission_prompt(home, benchmark_id)
    # ZCode needs an explicit repository boundary for this schema-only visual
    # benchmark. Keep it separate so other agents retain their existing prompt
    # and concurrent workers never overwrite each other's template.
    path = home / "zcode-submission-prompt-pompeii-v1.j2"
    return _materialize_shared_file(
        path, ZCODE_POMPEII_SUBMISSION_PROMPT.encode(),
    )


def _ensure_deepseek_config(home: Path) -> Path:
    path = home / "codex-deepseek-v4.toml"
    return _materialize_shared_file(path, DEEPSEEK_TOML.encode())


def _validated_deepseek_catalog() -> Path:
    path = deepseek_catalog_path()
    error = deepseek_catalog_error(path)
    if error is not None:
        raise RunnerError(error)
    return path


def _ensure_deepseek_agent_module(home: Path) -> Path:
    """Expose DeepSeek and its host-private Codex dependencies to Pier."""

    source = Path(__file__).with_name("pier_deepseek.py")
    if not source.is_file():
        raise RunnerError(
            "DeepSeek Pier adapter is missing; reinstall or upgrade dradar "
            "before running a paid task"
        )
    target = home / DEEPSEEK_AGENT_MODULE_FILENAME
    return _materialize_shared_file(target, source.read_bytes())


def _ensure_claude_agent_module(home: Path) -> Path:
    source = Path(__file__).with_name("pier_claude.py")
    usage_source = Path(__file__).with_name("claude_usage.py")
    if not source.is_file() or not usage_source.is_file():
        raise RunnerError(
            "Claude Code Pier adapter is missing; reinstall or upgrade dradar"
        )
    _materialize_shared_file(
        home / CLAUDE_USAGE_MODULE_FILENAME, usage_source.read_bytes()
    )
    return _materialize_shared_file(
        home / CLAUDE_AGENT_MODULE_FILENAME, source.read_bytes()
    )


def _ensure_runtime_safety_module(home: Path) -> Path:
    source = Path(__file__).with_name("pier_runtime_safety.py")
    if not source.is_file():
        raise RunnerError(
            "Pier runtime safety helper is missing; reinstall or upgrade dradar"
        )
    return _materialize_shared_file(
        home / RUNTIME_SAFETY_MODULE_FILENAME, source.read_bytes(),
    )


def _ensure_grok_agent_module(home: Path) -> Path:
    source = Path(__file__).with_name("pier_grok.py")
    recovery_source = Path(__file__).with_name("grok_recovery.py")
    if not source.is_file() or not recovery_source.is_file():
        raise RunnerError(
            "Grok Build Pier adapter is missing; reinstall or upgrade dradar"
        )
    _materialize_shared_file(
        home / GROK_RECOVERY_MODULE_FILENAME, recovery_source.read_bytes()
    )
    return _materialize_shared_file(
        home / GROK_AGENT_MODULE_FILENAME, source.read_bytes()
    )


def _ensure_kimi_agent_module(home: Path) -> Path:
    source = Path(__file__).with_name("pier_kimi.py")
    recovery_source = Path(__file__).with_name("kimi_recovery.py")
    if not source.is_file() or not recovery_source.is_file():
        raise RunnerError(
            "Kimi Code Pier adapter is missing; reinstall or upgrade dradar"
        )
    _ensure_runtime_safety_module(home)
    _materialize_shared_file(
        home / KIMI_RECOVERY_MODULE_FILENAME, recovery_source.read_bytes()
    )
    return _materialize_shared_file(
        home / KIMI_AGENT_MODULE_FILENAME, source.read_bytes()
    )


def _ensure_antigravity_agent_module(home: Path) -> Path:
    source = Path(__file__).with_name("pier_antigravity.py")
    if not source.is_file():
        raise RunnerError(
            "Antigravity Pier adapter is missing; reinstall or upgrade dradar"
        )
    return _materialize_shared_file(
        home / ANTIGRAVITY_AGENT_MODULE_FILENAME, source.read_bytes()
    )


def _ensure_shared_oauth_environment_module(home: Path) -> Path:
    source = Path(__file__).with_name("pier_shared_oauth_docker.py")
    if not source.is_file():
        raise RunnerError(
            "shared OAuth Docker environment is missing; reinstall or upgrade dradar"
        )
    return _materialize_shared_file(
        home / SHARED_OAUTH_ENV_MODULE_FILENAME, source.read_bytes()
    )


def _shared_oauth_mounts_json(agent: str, auth_path: Path) -> str:
    """Build the narrow provider mounts without putting secret values in argv."""

    try:
        canonical = auth_path.resolve(strict=True)
    except OSError as exc:
        raise RunnerError(f"subscription OAuth credential is unavailable: {exc}") from exc
    if canonical != auth_path or canonical.is_symlink():
        raise RunnerError("subscription OAuth credential must be a canonical path")
    if agent == KIMI_AGENT:
        expected_auth = kimi_auth_path()
        root = kimi_home()
        try:
            expected_canonical = expected_auth.resolve(strict=True)
            canonical_root = root.resolve(strict=True)
        except OSError as exc:
            raise RunnerError(
                f"Kimi OAuth managed store is unavailable: {exc}"
            ) from exc
        if (
            expected_auth != expected_canonical
            or root != canonical_root
            or canonical != expected_canonical
            or canonical.parent.parent != canonical_root
        ):
            raise RunnerError("Kimi OAuth credential is outside the managed store")
        oauth = canonical_root / "oauth"
        oauth.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(canonical.parent, 0o700)
            os.chmod(oauth, 0o700)
        mounts = [
            {
                "type": "bind",
                "source": str(canonical.parent.resolve(strict=True)),
                "target": "/tmp/dradar-kimi-home/credentials",
            },
            {
                "type": "bind",
                "source": str(oauth.resolve(strict=True)),
                "target": "/tmp/dradar-kimi-home/oauth",
            },
        ]
    elif agent == GROK_AGENT:
        root = canonical.parent
        if canonical.name != "auth.json" or root.name != "grok":
            raise RunnerError("Grok OAuth credential is outside the managed store")
        if os.name != "nt":
            os.chmod(root, 0o700)
        mounts = [{
            "type": "bind",
            "source": str(root.resolve(strict=True)),
            "target": "/tmp/dradar-grok-user/.grok",
        }]
    elif agent == ANTIGRAVITY_AGENT:
        root = canonical.parent
        if (
            not canonical.is_dir()
            or canonical.name != ".gemini"
            or root.name != "antigravity"
        ):
            raise RunnerError(
                "Antigravity OAuth home is outside the managed store"
            )
        if os.name != "nt":
            os.chmod(root, 0o700)
            os.chmod(canonical, 0o700)
        mounts = [{
            "type": "bind",
            "source": str(canonical),
            "target": "/tmp/dradar-antigravity-user/.gemini",
        }]
    else:  # pragma: no cover - private helper misuse
        raise RunnerError(
            "shared OAuth mounts are only supported for Kimi, Grok, and Antigravity"
        )
    return json.dumps(mounts, separators=(",", ":"), sort_keys=True)


def _ensure_zcode_agent_module(home: Path) -> Path:
    source = Path(__file__).with_name("pier_zcode.py")
    if not source.is_file():
        raise RunnerError(
            "ZCode Pier adapter is missing; reinstall or upgrade dradar"
        )
    _ensure_runtime_safety_module(home)
    return _materialize_shared_file(
        home / ZCODE_AGENT_MODULE_FILENAME, source.read_bytes()
    )


def _ensure_dsh_agent_module(home: Path) -> Path:
    """Expose only the pinned standalone DSH adapter to public Pier."""

    source = Path(__file__).with_name("pier_dsh.py")
    if not source.is_file():
        raise RunnerError(
            "DSH Minimal Pier adapter is missing; reinstall or upgrade dradar"
        )
    _ensure_runtime_safety_module(home)
    return _materialize_shared_file(
        home / DSH_AGENT_MODULE_FILENAME, source.read_bytes()
    )


def _ensure_codebuddy_agent_module(home: Path) -> Path:
    source = Path(__file__).with_name("pier_codebuddy.py")
    if not source.is_file():
        raise RunnerError(
            "CodeBuddy Pier adapter is missing; reinstall or upgrade dradar"
        )
    return _materialize_shared_file(
        home / CODEBUDDY_AGENT_MODULE_FILENAME, source.read_bytes()
    )


def _ensure_pier_sitecustomize(home: Path) -> Path:
    """Install the fail-closed prebuilt-egress shim into Pier's PYTHONPATH."""

    source = Path(__file__).with_name("pier_sitecustomize.py")
    if not source.is_file():
        raise RunnerError(
            "Pier egress bootstrap is missing; reinstall or upgrade dradar"
        )
    return _materialize_shared_file(
        home / "sitecustomize.py", source.read_bytes(), mode=0o600,
    )


def _version_tuple(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in value.split("."))


def _deepseek_codex_version(assignment: dict) -> str:
    """Validate the exact stable Codex release resolved for DeepSeek."""

    requested = assignment.get("agent_version")
    if not isinstance(requested, str) or not _STABLE_CODEX_VERSION_RE.fullmatch(
        requested
    ):
        raise RunnerError("DeepSeek requires an exact stable Codex CLI version")
    if _version_tuple(requested) < _version_tuple(DEEPSEEK_MIN_CODEX_VERSION):
        raise RunnerError(
            f"DeepSeek requires Codex >= {DEEPSEEK_MIN_CODEX_VERSION}, "
            f"but the assignment requested {requested}"
        )
    return requested


def _validate_deepseek_assignment(
    assignment: dict,
    *,
    validate_version: bool = True,
) -> None:
    if assignment.get("model") not in DEEPSEEK_MODELS:
        raise RunnerError(
            f"unsupported DeepSeek model {assignment.get('model')!r}; "
            f"enabled models are {', '.join(DEEPSEEK_MODELS)}"
        )
    if assignment.get("effort") not in DEEPSEEK_SUPPORTED_EFFORTS:
        supported = ", ".join(sorted(DEEPSEEK_SUPPORTED_EFFORTS))
        raise RunnerError(
            f"DeepSeek effort must be one of {supported}; "
            f"got {assignment.get('effort')!r}"
        )
    if validate_version:
        _deepseek_codex_version(assignment)


def _validate_grok_assignment(assignment: dict) -> None:
    if assignment.get("provider") != GROK_PROVIDER:
        raise RunnerError(
            "Grok Build assignments must explicitly use provider "
            f"{GROK_PROVIDER!r}"
        )
    if assignment.get("model") != GROK_MODEL:
        raise RunnerError(
            f"unsupported Grok subscription model {assignment.get('model')!r}; "
            f"only {GROK_MODEL!r} is enabled"
        )
    if assignment.get("effort") not in GROK_SUPPORTED_EFFORTS:
        raise RunnerError(
            "Grok subscription effort must be low, medium, high, or xhigh; "
            f"got {assignment.get('effort')!r}"
        )


def _validate_claude_assignment(assignment: dict) -> None:
    if assignment.get("provider") != CLAUDE_PROVIDER:
        raise RunnerError(
            "Claude Code assignments must explicitly use provider "
            f"{CLAUDE_PROVIDER!r}"
        )
    if assignment.get("model") not in CLAUDE_MODELS:
        raise RunnerError(
            f"unsupported Claude Code model {assignment.get('model')!r}; "
            f"enabled models are {', '.join(sorted(CLAUDE_MODELS))}"
        )
    if assignment.get("effort") not in CLAUDE_SUPPORTED_EFFORTS:
        raise RunnerError(
            "Claude Code effort must be low, medium, high, xhigh, or max; "
            f"got {assignment.get('effort')!r}"
        )
    if assignment.get("agent_version") != CLAUDE_CLI_VERSION:
        raise RunnerError(
            f"Claude Code requires CLI {CLAUDE_CLI_VERSION}; the assignment "
            f"requested {assignment.get('agent_version')!r}"
        )


def _validate_kimi_assignment(assignment: dict) -> None:
    if assignment.get("provider") != KIMI_PROVIDER:
        raise RunnerError(
            "Kimi Code assignments must explicitly use provider "
            f"{KIMI_PROVIDER!r}"
        )
    if assignment.get("model") != KIMI_MODEL:
        raise RunnerError(
            f"unsupported Kimi subscription model {assignment.get('model')!r}; "
            f"only {KIMI_MODEL!r} is enabled"
        )
    if assignment.get("effort") not in KIMI_SUPPORTED_EFFORTS:
        raise RunnerError(
            "Kimi subscription effort must be low, high, or max; "
            f"got {assignment.get('effort')!r}"
        )


def _validate_antigravity_assignment(assignment: dict) -> None:
    if assignment.get("provider") != ANTIGRAVITY_PROVIDER:
        raise RunnerError(
            "Antigravity assignments must explicitly use provider "
            f"{ANTIGRAVITY_PROVIDER!r}"
        )
    if assignment.get("model") != ANTIGRAVITY_MODEL:
        raise RunnerError(
            f"unsupported Antigravity model {assignment.get('model')!r}; "
            f"only {ANTIGRAVITY_MODEL!r} is enabled"
        )
    effort = assignment.get("effort")
    if effort not in ANTIGRAVITY_SUPPORTED_EFFORTS:
        raise RunnerError(
            "Antigravity effort must be low, medium, or high; "
            f"got {effort!r}"
        )
    if ANTIGRAVITY_RUNTIME_MODELS.get(effort) != f"{ANTIGRAVITY_MODEL}-{effort}":
        raise RunnerError("Antigravity runtime model mapping is inconsistent")


def _validate_zcode_assignment(assignment: dict) -> None:
    if assignment.get("provider") != ZCODE_PROVIDER:
        raise RunnerError(
            "ZCode assignments must explicitly use provider "
            f"{ZCODE_PROVIDER!r}"
        )
    if assignment.get("model") not in ZCODE_MODELS:
        raise RunnerError(
            f"unsupported ZCode model {assignment.get('model')!r}; "
            f"enabled models are {', '.join(sorted(ZCODE_MODELS))}"
        )
    if assignment.get("effort") not in ZCODE_SUPPORTED_EFFORTS:
        raise RunnerError(
            "ZCode effort must be low, high, or max; "
            f"got {assignment.get('effort')!r}"
        )


def _validate_dsh_assignment(assignment: dict) -> None:
    if assignment.get("provider") != DEEPSEEK_PROVIDER:
        raise RunnerError(
            "DSH Minimal assignments must explicitly use provider "
            f"{DEEPSEEK_PROVIDER!r}"
        )
    if assignment.get("model") not in DSH_MODELS:
        raise RunnerError(
            f"unsupported DSH model {assignment.get('model')!r}; enabled "
            f"models are {', '.join(DSH_MODELS)}"
        )
    if assignment.get("effort") not in DSH_SUPPORTED_EFFORTS:
        supported = ", ".join(sorted(DSH_SUPPORTED_EFFORTS))
        raise RunnerError(
            f"DSH effort must be one of {supported}; "
            f"got {assignment.get('effort')!r}"
        )


def _validate_codebuddy_assignment(assignment: dict) -> None:
    if assignment.get("provider") != CODEBUDDY_PROVIDER:
        raise RunnerError(
            "CodeBuddy assignments must explicitly use provider "
            f"{CODEBUDDY_PROVIDER!r}"
        )
    if assignment.get("model") != CODEBUDDY_MODEL:
        raise RunnerError(
            f"unsupported CodeBuddy model {assignment.get('model')!r}; "
            f"only {CODEBUDDY_MODEL!r} is enabled"
        )
    if assignment.get("effort") not in CODEBUDDY_SUPPORTED_EFFORTS:
        supported = ", ".join(sorted(CODEBUDDY_SUPPORTED_EFFORTS))
        raise RunnerError(
            f"CodeBuddy effort must be one of {supported}; "
            f"got {assignment.get('effort')!r}"
        )
    requested = assignment.get("agent_version")
    if requested != CODEBUDDY_CLI_VERSION:
        raise RunnerError(
            f"CodeBuddy requires CLI {CODEBUDDY_CLI_VERSION}; "
            f"the assignment requested {requested!r}"
        )


def _pier_process_env(
    assignment: dict,
    *,
    pier_bootstrap_dir: Path | None = None,
    egress_environment: dict[str, str] | None = None,
    codex_module_dir: Path | None = None,
    claude_module_dir: Path | None = None,
    deepseek_module_dir: Path | None = None,
    grok_module_dir: Path | None = None,
    kimi_module_dir: Path | None = None,
    antigravity_module_dir: Path | None = None,
    zcode_module_dir: Path | None = None,
    dsh_module_dir: Path | None = None,
    codebuddy_module_dir: Path | None = None,
) -> dict[str, str]:
    """Keep provider secrets out of Pier's inherited environment."""

    env = dict(os.environ)
    # Pier sees only the per-run adapters and bootstrap. Ambient Python paths
    # can shadow its isolated installation or inject unrelated sitecustomize
    # code before the network fence is installed.
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    python_dirs = [
        path for path in (
            pier_bootstrap_dir, codex_module_dir, claude_module_dir,
            deepseek_module_dir,
            grok_module_dir,
            kimi_module_dir, antigravity_module_dir,
            zcode_module_dir, dsh_module_dir, codebuddy_module_dir,
        ) if path is not None
    ]
    if python_dirs:
        env["PYTHONPATH"] = os.pathsep.join(
            dict.fromkeys(str(path) for path in python_dirs)
        )
    if assignment_codex_provider(assignment) == DEEPSEEK_PROVIDER:
        # The key has already been materialized in a private auth.json file.
        # Removing the ambient variable prevents accidental fallback to the
        # old ``docker compose exec -e KEY=value`` path.
        env.pop(DEEPSEEK_API_KEY_ENV, None)
        # The pinned Pier environment must see only the copied, standalone
        # adapter module. Ambient Python paths could accidentally shadow it.
    if assignment.get("agent") == GROK_AGENT:
        env.pop(GROK_API_KEY_ENV, None)
    if assignment.get("agent") == CLAUDE_AGENT:
        for name in (*CLAUDE_API_KEY_ENVS, "CLAUDE_CODE_OAUTH_TOKEN"):
            env.pop(name, None)
    if assignment.get("agent") == KIMI_AGENT:
        for name in KIMI_API_KEY_ENVS:
            env.pop(name, None)
    if assignment.get("agent") == ANTIGRAVITY_AGENT:
        for name in (
            "GEMINI_API_KEY", "GOOGLE_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS", "AGY_ADC_AUTH",
        ):
            env.pop(name, None)
    if assignment.get("agent") == ZCODE_AGENT:
        for name in (
            ZCODE_API_KEY_ENV, "BIGMODEL_API_KEY", "ZHIPUAI_API_KEY",
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        ):
            env.pop(name, None)
    if assignment.get("agent") == DSH_AGENT:
        env.pop(DEEPSEEK_API_KEY_ENV, None)
    if assignment.get("agent") == CODEBUDDY_AGENT:
        for name in tuple(env):
            if name.startswith("CODEBUDDY_") or name in CODEBUDDY_API_KEY_ENVS:
                env.pop(name, None)
        env[CODEBUDDY_SOURCE_IMAGE_ENV] = CODEBUDDY_CONTAINER_IMAGE
    if egress_environment:
        env.update(egress_environment)
    return env


def _task_agent_timeout_sec(task_path: Path) -> float | None:
    """The task's own declared agent watchdog (task.toml's [agent].timeout_sec
    -- commonly 5400.0/90min or 7200.0/120min across managed packs). None if the
    file is missing or malformed; caller must not guess a number in that case."""
    try:
        with (task_path / "task.toml").open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return data.get("agent", {}).get("timeout_sec")


def _task_environment_build_timeout_sec(task_path: Path) -> float | None:
    """Read the task's declared Pier environment build timeout.

    This value is used only to size DRadar's outer watchdog when the longer
    build profile is enabled.  Pier remains the source of truth for the actual
    environment timeout; malformed task metadata never weakens the historical
    watchdog.
    """

    try:
        with (task_path / "task.toml").open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = data.get("environment", {}).get("build_timeout_sec")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    ):
        return float(value)
    return None


def resolve_environment_build_timeout_multiplier(value: object = None) -> float:
    """Return a bounded environment-build timeout multiplier.

    A default of three gives slow mirrors and cold builders a long recovery
    window while keeping the upper bound finite. Passing ``1`` explicitly restores the
    previous behavior; values above eight are refused so a wedged daemon
    cannot keep a paid lease alive indefinitely.
    """

    if value is None:
        return DEFAULT_ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER
    try:
        multiplier = float(value)
    except (TypeError, ValueError) as exc:
        raise RunnerError(
            "environment build timeout multiplier must be a finite number "
            "between 1 and 8"
        ) from exc
    if not math.isfinite(multiplier) or not 1.0 <= multiplier <= 8.0:
        raise RunnerError(
            "environment build timeout multiplier must be a finite number "
            "between 1 and 8"
        )
    return multiplier


def pompeii_agent_timeout_sec(assignment: dict) -> int:
    """Return the hard agent budget for one Pompeii assignment."""
    if assignment.get("agent") == KIMI_AGENT:
        return KIMI_POMPEII_AGENT_TIMEOUT_SEC
    return POMPEII_AGENT_TIMEOUT_SEC


def _agent_timeout_multiplier(assignment: dict, task_path: Path) -> float:
    """Resolve Pier's agent watchdog multiplier for one assignment.

    Ordinary benchmarks stretch the task timeout to stay behind DRadar's outer
    watchdog. Pompeii normalizes the installed task timeout to the assignment's
    hard budget regardless of whether the pack declares the older 90-minute
    watchdog or the refreshed two-hour watchdog.
    """
    base = _task_agent_timeout_sec(task_path)
    if not base:
        if assignment.get("benchmark_id") == POMPEII_BENCHMARK_ID:
            hard_budget_sec = pompeii_agent_timeout_sec(assignment)
            raise RunnerError(
                "Pompeii tasks require a readable [agent].timeout_sec so the "
                f"{hard_budget_sec // 60}-minute execution limit can be enforced"
            )
        return 1.0
    if assignment.get("benchmark_id") == POMPEII_BENCHMARK_ID:
        # Managed Pompeii packs in the field declare either 5400s or 7200s.
        # Normalize both at launch to the assignment-specific hard limit. Floor
        # the serialized ratio so unusual task defaults can stop a fraction
        # early, never after that limit.
        raw = pompeii_agent_timeout_sec(assignment) / base
        return math.floor(raw * 1_000_000) / 1_000_000
    watchdog_sec = _effective_trial_timeout_sec(assignment) + 60
    raw = watchdog_sec / base
    if raw <= 1.0:
        return 1.0
    # Round UP to 3 decimals: --agent-timeout-multiplier is formatted to the
    # same precision, and rounding to nearest/down could shave the product
    # back under (outer + 60), silently reopening the exact race this exists
    # to close.
    return math.ceil(raw * 1000) / 1000


def build_pier_command(
    assignment: dict,
    tasks_root: Path,
    jobs_dir: Path,
    job_name: str,
    home: Path,
    dev_agent: str | None = None,
    provider_auth_path: Path | None = None,
    provider_cli_path: Path | None = None,
    environment_build_timeout_multiplier: float | None = (
        DEFAULT_ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER
    ),
) -> list[str]:
    task_path = tasks_root / assignment["task_id"]
    if not task_path.is_dir():
        raise RunnerError(f"task not found locally: {task_path}")

    # Pier's enforceable egress switch lives under [environment]. Merely
    # declaring [agent].network_mode="no-network" is descriptive and can
    # otherwise leave the Docker container on its ordinary network.
    task_toml_path = task_path / "task.toml"
    try:
        with task_toml_path.open("rb") as stream:
            task_config = tomllib.load(stream)
    except FileNotFoundError:
        task_config = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RunnerError(f"task.toml is unreadable: {exc}") from exc
    if task_config.get("agent", {}).get("network_mode") == "no-network":
        if task_config.get("environment", {}).get("allow_internet") is not False:
            raise RunnerError(
                "task requests no-network but does not set "
                "[environment].allow_internet=false; refusing an unsafe run"
            )

    agent = dev_agent or assignment["agent"]
    provider = assignment_codex_provider(assignment) if agent == "codex" else None
    if agent == "codex" and provider is None:
        # A developer override from another agent family predates provider
        # support and must retain the original OpenAI behavior.
        provider = DEFAULT_CODEX_PROVIDER
    if agent == DSH_AGENT:
        # DSH is a standalone adapter for stock Pier.
        public_pier = "datacurve-pier==0.3.0"
        uvx = _resolve_user_tool("uvx")
        uv = _resolve_user_tool("uv")
        if uvx:
            pier_command = [
                uvx, "--isolated", "--from", public_pier, "pier",
            ]
        elif uv:
            pier_command = [
                uv, "tool", "run", "--isolated", "--from", public_pier,
                "pier",
            ]
        else:
            raise RunnerError(
                "uv/uvx is required for the isolated public DSH runner"
            )
    else:
        pier = _resolve_user_tool("pier")
        if not pier:
            raise RunnerError(
                "pier not found on PATH (run: uv tool install datacurve-pier)"
            )
        pier_command = [pier]
    deepseek_catalog = None
    if provider == DEEPSEEK_PROVIDER:
        _validate_deepseek_assignment(assignment)
        deepseek_catalog = _validated_deepseek_catalog()
        _ensure_deepseek_agent_module(home)
        agent_args = ["--agent-import-path", DEEPSEEK_AGENT_IMPORT_PATH]
    elif agent == "codex" and provider == DEFAULT_CODEX_PROVIDER:
        agent_args = ["--agent", "codex"]
    elif agent == CLAUDE_AGENT:
        _validate_claude_assignment(assignment)
        _ensure_claude_agent_module(home)
        agent_args = ["--agent-import-path", CLAUDE_AGENT_IMPORT_PATH]
    elif agent == GROK_AGENT:
        _validate_grok_assignment(assignment)
        _ensure_grok_agent_module(home)
        _ensure_shared_oauth_environment_module(home)
        agent_args = ["--agent-import-path", GROK_AGENT_IMPORT_PATH]
    elif agent == KIMI_AGENT:
        _validate_kimi_assignment(assignment)
        _ensure_kimi_agent_module(home)
        _ensure_shared_oauth_environment_module(home)
        agent_args = ["--agent-import-path", KIMI_AGENT_IMPORT_PATH]
    elif agent == ANTIGRAVITY_AGENT:
        _validate_antigravity_assignment(assignment)
        _ensure_antigravity_agent_module(home)
        _ensure_shared_oauth_environment_module(home)
        agent_args = ["--agent-import-path", ANTIGRAVITY_AGENT_IMPORT_PATH]
    elif agent == ZCODE_AGENT:
        _validate_zcode_assignment(assignment)
        _ensure_zcode_agent_module(home)
        agent_args = ["--agent-import-path", ZCODE_AGENT_IMPORT_PATH]
    elif agent == DSH_AGENT:
        _validate_dsh_assignment(assignment)
        _ensure_dsh_agent_module(home)
        agent_args = ["--agent-import-path", DSH_AGENT_IMPORT_PATH]
    elif agent == CODEBUDDY_AGENT:
        _validate_codebuddy_assignment(assignment)
        _ensure_codebuddy_agent_module(home)
        agent_args = ["--agent-import-path", CODEBUDDY_AGENT_IMPORT_PATH]
    else:
        agent_args = ["--agent", agent]
    cmd = [
        *pier_command, "run",
        "-p", str(task_path),
        *agent_args,
        "--jobs-dir", str(jobs_dir),
        "--job-name", job_name,
        "--n-concurrent", "1",
        "--max-retries", "0",
        "--disable-verification",
        "--yes",
    ]
    if agent in (GROK_AGENT, KIMI_AGENT, ANTIGRAVITY_AGENT):
        if provider_auth_path is None:
            raise RunnerError("subscription OAuth credential is unavailable")
        cmd += [
            "--environment-import-path", SHARED_OAUTH_ENV_IMPORT_PATH,
            "--ek", "shared_oauth_mounts_json="
            + _shared_oauth_mounts_json(agent, provider_auth_path),
        ]
    multiplier = _agent_timeout_multiplier(assignment, task_path)
    if not math.isclose(multiplier, 1.0):
        cmd += ["--agent-timeout-multiplier", f"{multiplier:.6f}"]
    if environment_build_timeout_multiplier is not None:
        build_multiplier = resolve_environment_build_timeout_multiplier(
            environment_build_timeout_multiplier,
        )
        if not math.isclose(build_multiplier, 1.0):
            cmd += [
                "--environment-build-timeout-multiplier",
                f"{build_multiplier:.6f}",
            ]
    # Task containers ship no git identity, so an agent's final `git commit`
    # dies with "Author identity unknown" unless the model thinks to configure
    # one (volunteer report, 2026-07-13). These ride pier's --ae into the
    # agent's process env, which every git it spawns inherits. .invalid TLD:
    # never routable, per RFC 2606.
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        cmd += ["--ae", f"{var}=dradar-trial"]
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        cmd += ["--ae", f"{var}=trial@dradar.invalid"]
    if agent == "codex" and provider == DEFAULT_CODEX_PROVIDER:
        auth = codex_auth_path()
        if not auth.is_file():
            raise RunnerError(f"codex auth not found: {auth} (run `codex login` first)")
        allowlist = _ensure_allowlist(home)
        submission_prompt = _ensure_codex_submission_prompt(
            home, assignment.get("benchmark_id")
        )
        cmd += [
            "--model", assignment["model"],
            "--ak", f"reasoning_effort={assignment['effort']}",
            "--ak", f"config_toml_file={allowlist}",
            "--ak", f"prompt_template_path={submission_prompt}",
            "--ae", f"CODEX_AUTH_JSON_PATH={auth}",
        ]
        # The caller must resolve npm's stable tag to an exact version before
        # every task start. Pier bakes `npm install -g @openai/codex@...` into
        # a Docker layer, so the literal "latest" can stay cached forever.
        # Exact versions change the install command and invalidate that layer.
        version = assignment.get("agent_version")
        if not isinstance(version, str) or not _STABLE_CODEX_VERSION_RE.fullmatch(
            version
        ):
            raise RunnerError(
                "a verified exact stable Codex CLI version is required before "
                "starting the task container"
            )
        cmd += ["--ak", f"version={version}"]
    elif agent == "codex" and provider == DEEPSEEK_PROVIDER:
        if provider_auth_path is None or not provider_auth_path.is_file():
            raise RunnerError(
                "DeepSeek runtime credential is unavailable; run "
                "`dradar provider setup deepseek` in your own interactive Terminal"
            )
        config_path = _ensure_deepseek_config(home)
        submission_prompt = _ensure_codex_submission_prompt(
            home, assignment.get("benchmark_id")
        )
        if deepseek_catalog is None:  # defensive: validation must precede argv
            raise RunnerError("DeepSeek model catalog was not prepared")
        cmd += [
            "--model", assignment["model"],
            "--ak", "reasoning_effort=" + deepseek_codex_reasoning_effort(
                assignment["effort"]
            ),
            "--ak", f"config_toml_file={config_path}",
            "--ak", f"model_catalog_json_file={deepseek_catalog}",
            "--ak", f"prompt_template_path={submission_prompt}",
            "--ae", f"CODEX_AUTH_JSON_PATH={provider_auth_path}",
            "--ak", f"version={_deepseek_codex_version(assignment)}",
        ]
    elif agent == "codex":
        raise RunnerError(
            f"unsupported Codex provider {provider!r}; upgrade dradar if the "
            "server intentionally enabled a new provider"
        )
    elif agent == CLAUDE_AGENT:
        if provider_auth_path is None or not provider_auth_path.is_file():
            raise RunnerError(
                "Claude Code subscription OAuth is unavailable; run "
                "`dradar provider setup claude` in your own interactive Terminal"
            )
        cmd += [
            "--model", assignment["model"],
            "--ak", f"reasoning_effort={assignment['effort']}",
            "--ak", f"version={CLAUDE_CLI_VERSION}",
            "--ak", f"oauth_token_file={provider_auth_path}",
            "--ak", f"disallowed_tools={CLAUDE_DISALLOWED_TOOLS}",
            "--ae", "API_TIMEOUT_MS=3000000",
            "--ae", "CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000",
            "--ae", "CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000",
        ]
    elif agent == DSH_AGENT:
        if provider_auth_path is None or not provider_auth_path.is_file():
            raise RunnerError(
                "DSH runtime credential is unavailable; run "
                "`dradar provider setup deepseek` in your own interactive Terminal"
            )
        submission_prompt = _ensure_codex_submission_prompt(
            home, assignment.get("benchmark_id")
        )
        cmd += [
            "--model", assignment["model"],
            "--ak", f"reasoning_effort={assignment['effort']}",
            "--ak", f"api_key_file={provider_auth_path}",
            "--ak", f"prompt_template_path={submission_prompt}",
            "--ak", f"version={DSH_VERSION}",
            "--ak", f"artifact_assignment_id={assignment['assignment_id']}",
            "--ak", f"artifact_run_id={assignment.get('_artifact_run_id') or uuid.uuid4().hex}",
            "--ak", f"artifact_task_id={assignment['task_id']}",
        ]
    elif agent == GROK_AGENT:
        if provider_auth_path is None or not provider_auth_path.is_file():
            raise RunnerError(
                "Grok subscription OAuth is unavailable; run "
                "`dradar provider setup grok` in your own interactive Terminal"
            )
        if provider_cli_path is None or not provider_cli_path.is_file():
            raise RunnerError(
                "Pinned Grok CLI executable is unavailable; run "
                "`dradar provider status grok` first"
            )
        submission_prompt = _ensure_codex_submission_prompt(
            home, assignment.get("benchmark_id")
        )
        cmd += [
            "--model", assignment["model"],
            "--ak", f"reasoning_effort={assignment['effort']}",
            "--ak", f"auth_json_file={provider_auth_path}",
            "--ak", "shared_oauth=true",
            "--ak", f"grok_cli_file={provider_cli_path}",
            "--ak", f"prompt_template_path={submission_prompt}",
            "--ak", f"version={GROK_CLI_VERSION}",
        ]
    elif agent == KIMI_AGENT:
        if provider_auth_path is None or not provider_auth_path.is_file():
            raise RunnerError(
                "Kimi subscription OAuth is unavailable; run "
                "`dradar provider setup kimi` in your own interactive Terminal"
            )
        if provider_cli_path is None or not provider_cli_path.is_file():
            raise RunnerError(
                "Pinned Kimi CLI executable is unavailable; run "
                "`dradar provider status kimi` first"
            )
        submission_prompt = _ensure_codex_submission_prompt(
            home, assignment.get("benchmark_id")
        )
        cmd += [
            "--model", assignment["model"],
            "--ak", f"reasoning_effort={assignment['effort']}",
            "--ak", f"auth_json_file={provider_auth_path}",
            "--ak", "shared_oauth=true",
            "--ak", f"kimi_cli_file={provider_cli_path}",
            "--ak", f"prompt_template_path={submission_prompt}",
            "--ak", f"version={KIMI_CLI_VERSION}",
        ]
    elif agent == ANTIGRAVITY_AGENT:
        if provider_auth_path is None or not provider_auth_path.is_dir():
            raise RunnerError(
                "Antigravity subscription OAuth is unavailable; run "
                "`dradar provider setup antigravity` in your own interactive Terminal"
            )
        submission_prompt = _ensure_codex_submission_prompt(
            home, assignment.get("benchmark_id")
        )
        cmd += [
            "--model", assignment["model"],
            "--ak", f"reasoning_effort={assignment['effort']}",
            "--ak", f"auth_home_dir={provider_auth_path}",
            "--ak", "shared_oauth=true",
            "--ak", f"prompt_template_path={submission_prompt}",
            "--ak", f"version={ANTIGRAVITY_CLI_VERSION}",
        ]
    elif agent == ZCODE_AGENT:
        if provider_auth_path is None or not provider_auth_path.is_file():
            raise RunnerError(
                "ZCode Coding Plan credential is unavailable; run "
                "`dradar provider setup zcode` in your own interactive Terminal"
            )
        if provider_cli_path is None or not provider_cli_path.is_file():
            raise RunnerError(
                "Pinned ZCode CLI is unavailable; run "
                "`dradar provider status zcode` first"
            )
        submission_prompt = _ensure_zcode_submission_prompt(
            home, assignment.get("benchmark_id")
        )
        # Server assignment versions are compatibility hints, not runtime
        # pins. Normal runs replace this with the locally observed version;
        # direct command builders default to the newest verified runtime.
        zcode_version = (
            assignment.get("agent_version")
            if assignment.get("_zcode_cli_version_observed") is True
            else ZCODE_CLI_VERSION
        )
        cmd += [
            "--model", assignment["model"],
            "--ak", f"reasoning_effort={assignment['effort']}",
            "--ak", f"api_key_file={provider_auth_path}",
            "--ak", f"zcode_cli_file={provider_cli_path}",
            "--ak", f"session_timeout_sec={_zcode_session_timeout_sec(assignment)}",
            "--ak", f"prompt_template_path={submission_prompt}",
            "--ak", f"version={zcode_version}",
        ]
    elif agent == CODEBUDDY_AGENT:
        if provider_auth_path is None or not provider_auth_path.is_dir():
            raise RunnerError(
                "CodeBuddy subscription login is unavailable; run "
                "`dradar provider setup codebuddy` after completing official login"
            )
        submission_prompt = _ensure_codex_submission_prompt(
            home, assignment.get("benchmark_id")
        )
        cmd += [
            "--model", assignment["model"],
            "--ak", f"reasoning_effort={assignment['effort']}",
            "--ak", f"auth_dir={provider_auth_path}",
            "--ak", f"prompt_template_path={submission_prompt}",
            "--ak", f"version={CODEBUDDY_CLI_VERSION}",
        ]
    return cmd


def _validated_grok_cli_path() -> Path:
    """Resolve and verify the standalone subscription CLI before claiming quota."""

    discovered = grok_cli_path()
    if not discovered:
        raise RunnerError(
            f"Official Grok CLI {GROK_CLI_VERSION} is unavailable; run "
            "`dradar provider setup grok` first"
        )
    try:
        executable = Path(discovered).expanduser().resolve(strict=True)
        info = executable.stat()
    except OSError as exc:
        raise RunnerError(f"cannot inspect pinned Grok CLI: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(executable, os.X_OK):
        raise RunnerError("Pinned Grok CLI must resolve to an executable regular file")
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f"could not verify pinned Grok CLI: {exc}") from exc
    found = parse_grok_cli_version(result.stdout)
    if result.returncode != 0 or found != GROK_CLI_VERSION:
        raise RunnerError(
            f"Grok subscription runs require CLI {GROK_CLI_VERSION}; "
            f"found {found or 'an unrecognized version'}"
        )
    return executable


def _validated_kimi_cli_path() -> Path:
    """Resolve and verify the pinned Kimi CLI before claiming quota."""

    discovered = kimi_cli_path()
    if not discovered:
        raise RunnerError(
            f"Official Kimi Code CLI {KIMI_CLI_VERSION} is unavailable; run "
            "`dradar provider setup kimi` first"
        )
    try:
        executable = Path(discovered).expanduser().resolve(strict=True)
        info = executable.stat()
    except OSError as exc:
        raise RunnerError(f"cannot inspect pinned Kimi CLI: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(executable, os.X_OK):
        raise RunnerError("Pinned Kimi CLI must resolve to an executable regular file")
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f"could not verify pinned Kimi CLI: {exc}") from exc
    found = parse_kimi_cli_version(result.stdout)
    if result.returncode != 0 or found != KIMI_CLI_VERSION:
        raise RunnerError(
            f"Kimi subscription runs require CLI {KIMI_CLI_VERSION}; "
            f"found {found or 'an unrecognized version'}"
        )
    return executable


def _validated_zcode_cli_path(*, model: str | None = None) -> tuple[Path, str]:
    """Resolve the selected official protocol runtime and its actual version."""

    discovered = zcode_cli_path()
    issue = zcode_cli_error(discovered, model=model)
    if issue is not None:
        raise RunnerError(issue)
    try:
        cli = Path(discovered).expanduser().resolve(strict=True)
    except (AttributeError, OSError) as exc:
        raise RunnerError(f"cannot inspect ZCode CLI: {exc}") from exc
    try:
        result = subprocess.run(
            ["node", str(cli), "version"], capture_output=True, text=True,
            timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f"could not verify ZCode CLI: {exc}") from exc
    version = parse_zcode_cli_version(result.stdout + "\n" + result.stderr)
    if result.returncode != 0 or not zcode_cli_version_is_compatible(
        version, model=model,
    ):
        raise RunnerError("could not determine a compatible ZCode CLI version")
    return cli, version


def _preflight_subscription_before_build(
    agent: str,
    *,
    grok_cli: Path | None = None,
    kimi_cli: Path | None = None,
) -> None:
    """Run a bounded, no-prompt provider check before any Docker build.

    Subscription OAuth failures are account/setup failures, not environment
    failures.  Running the provider's existing live check first prevents a
    worker from spending tens of minutes pulling/building an image only to
    discover an invalid refresh grant.  The helpers use their normal
    account-scoped locks and redacted diagnostics; no prompt/model turn is
    started here.
    """

    issue: str | None = None
    if agent == GROK_AGENT:
        if grok_cli is None:
            raise RunnerError("Grok CLI was not prepared for provider preflight")
        issue = grok_live_error(grok_cli)
    elif agent == KIMI_AGENT:
        if kimi_cli is None:
            raise RunnerError("Kimi CLI was not prepared for provider preflight")
        issue = kimi_live_error(kimi_cli)
    if issue is not None:
        raise RunnerError(
            f"{agent} provider preflight failed before Docker build: {issue}"
        )


def locate_artifacts(jobs_dir: Path, job_name: str) -> tuple[Path, Path]:
    job_dir = jobs_dir / job_name
    trials = [Path(p) for p in glob.glob(str(job_dir / "*__*")) if Path(p).is_dir()]
    if not trials:
        raise RunnerError(f"no trial dir under {job_dir}")
    return job_dir, trials[0]


def trial_artifact_paths(trial_dir: Path) -> tuple[Path, Path | None, Path | None]:
    """The (patch, trajectory, result) paths inside a trial_dir — the single
    source of truth for pier's artifact layout. Used by run_trial right after
    a run, and by the retry-upload path, which reconstructs the paths from a
    bare trial_dir long after the process that ran the trial exited. The
    optional files are None when absent; the patch path is returned either
    way (callers decide whether a missing patch is fatal)."""
    patch = trial_dir / "artifacts" / "model.patch"
    trajectory = trial_dir / "agent" / "trajectory.json"
    result = trial_dir / "result.json"
    return patch, (trajectory if trajectory.is_file() else None), (result if result.is_file() else None)


def _verify_dsh_artifact_binding(
    trial_dir: Path, assignment: dict,
) -> dict[str, object]:
    """Reject DSH artifacts that cannot be tied to this exact checkout/run."""
    path = trial_dir / "agent" / "dsh-home" / "dsh-outcome.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError(
            "DSH artifact identity sidecar is missing or unreadable; refusing "
            "to upload files that cannot be bound to this assignment"
        ) from exc
    expected = {
        "schema": "dradar-dsh-outcome-v1",
        "assignmentId": assignment.get("assignment_id"),
        "artifactRunId": assignment.get("_artifact_run_id"),
        "taskId": assignment.get("task_id"),
        "assignmentModel": assignment.get("model"),
        "reasoningEffort": assignment.get("effort"),
    }
    if not isinstance(value, dict) or any(
        value.get(key) != expected_value
        for key, expected_value in expected.items()
    ):
        raise RunnerError(
            "DSH artifact identity does not match the current assignment/run; "
            "the stale files were kept locally and will not be uploaded"
        )
    if (
        assignment.get("model") == DSH_VISION_MODEL
        and str(assignment.get("task_id") or "").startswith(
            "pompeii-adjacency-rp-"
        )
    ):
        image = value.get("visionInput")
        if (
            value.get("visionInputAttached") is not True
            or not isinstance(image, dict)
            or not isinstance(image.get("attachmentId"), str)
            or _DSH_IMAGE_ATTACHMENT_ID_RE.fullmatch(
                image["attachmentId"]
            ) is None
            or image.get("mediaType") != "image/png"
            or image.get("name") != "question.png"
            or any(
                isinstance(image.get(key), bool)
                or not isinstance(image.get(key), int)
                or image[key] <= 0
                for key in ("bytes", "width", "height")
            )
        ):
            raise RunnerError(
                "DSH Vision Exp did not attest a valid question.png input; "
                "the artifact will not be uploaded"
            )
        return {
            **expected,
            "visionInputAttached": True,
            "visionInput": image,
        }
    if assignment.get("model") == DSH_VISION_MODEL:
        if (
            value.get("visionInputAttached") is not False
            or value.get("visionInput") is not None
        ):
            raise RunnerError(
                "DSH Vision Exp text run unexpectedly attached an image; "
                "the artifact will not be uploaded"
            )
    return {**expected, "visionInputAttached": False}


def _normalize_utf16_patch(patch: Path) -> bool:
    """Convert a BOM-marked, valid unified diff to UTF-8 before upload."""
    try:
        raw = patch.read_bytes()
    except OSError as exc:
        raise RunnerError(f"model.patch is unreadable: {exc}") from exc
    if not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    try:
        normalized = raw.decode("utf-16").encode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise RunnerError(
            "model.patch has a malformed UTF-16 encoding; keeping it locally "
            "instead of uploading an ungradeable patch"
        ) from exc
    try:
        parsed = subprocess.run(
            ["git", "apply", "--numstat", "-"],
            input=normalized,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise RunnerError(f"could not validate normalized model.patch: {exc}") from exc
    if parsed.returncode != 0:
        raise RunnerError(
            "UTF-16 model.patch could not be converted into a valid unified "
            "diff; keeping it locally instead of uploading"
        )
    _materialize_shared_file(patch, normalized, mode=0o600)
    return True


def _plain_file(path: Path) -> bool:
    """True only for a regular file at the named path, never a symlink."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


CODEX_TRAJECTORY_BUNDLE_SCHEMA = "dradar-codex-trajectory-bundle-v1"
KIMI_TRAJECTORY_BUNDLE_SCHEMA = "dradar-kimi-trajectory-bundle-v1"
KIMI_SESSION_LOG_RELATIVE = Path("agent") / "kimi-code.jsonl"


def _codex_usage(value) -> dict | None:
    """Normalize one cumulative Codex token counter object."""
    if not isinstance(value, dict):
        return None
    names = ("input_tokens", "cached_input_tokens", "output_tokens")
    counters = {}
    for name in names:
        item = value.get(name)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            return None
        counters[name] = item
    if counters["cached_input_tokens"] > counters["input_tokens"]:
        return None
    reasoning = value.get("reasoning_output_tokens", 0)
    if isinstance(reasoning, bool) or not isinstance(reasoning, int) or reasoning < 0:
        reasoning = 0
    counters["reasoning_output_tokens"] = reasoning
    return counters


def _codex_event_timestamp(event: dict) -> str | None:
    value = event.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if instant.tzinfo is not None else None


def _analyze_codex_session_events(events: list[dict], fallback_id: str) -> dict:
    """Describe one Codex JSONL file and isolate this agent's own usage.

    Legacy spawned sessions receive a copy of their parent's event prefix, so
    their final cumulative counters include inherited parent tokens.  Codex's
    paginated history protocol keeps the metadata prefix but starts token
    counters at zero for the child.  The explicit ``history_mode`` marker is
    therefore part of the accounting boundary: subtract legacy inherited
    counters, but sum paginated child counters as their own usage.
    """
    meta_events = [event for event in events if event.get("type") == "session_meta"]
    first_meta = meta_events[0].get("payload", {}) if meta_events else {}
    if not isinstance(first_meta, dict):
        first_meta = {}
    raw_id = first_meta.get("id") or first_meta.get("session_id")
    session_id = raw_id if isinstance(raw_id, str) and raw_id else fallback_id
    raw_role = first_meta.get("thread_source")
    role = "root" if raw_role == "user" else (
        "subagent" if raw_role == "subagent" else "unknown")
    parent_session_id = None
    source = first_meta.get("source") or {}
    if isinstance(source, dict) and isinstance(source.get("subagent"), dict):
        spawn = source["subagent"].get("thread_spawn")
        if isinstance(spawn, dict):
            raw_parent = spawn.get("parent_thread_id")
            if isinstance(raw_parent, str) and raw_parent:
                parent_session_id = raw_parent

    starts = []
    completes = []
    usage_events: list[tuple[int, dict]] = []
    for index, event in enumerate(events):
        payload = event.get("payload") or {}
        if event.get("type") == "event_msg" and isinstance(payload, dict):
            if payload.get("type") == "task_started":
                starts.append(index)
            elif payload.get("type") == "task_complete":
                completes.append(index)
            elif payload.get("type") == "token_count":
                info = payload.get("info") or {}
                usage = _codex_usage(
                    info.get("total_token_usage") if isinstance(info, dict) else None)
                if usage is not None:
                    usage_events.append(
                        (index, usage, _codex_event_timestamp(event)))

    # Root and paginated child counters start at zero.  Legacy child files
    # contain a complete inherited prefix; their last task_started is the
    # beginning of the child's own work.  A paginated child can receive more
    # than one task in Codex 0.149+, so its earlier task counters are still its
    # own usage and must not be mistaken for an inherited prefix.
    paginated_child = (
        role == "subagent" and first_meta.get("history_mode") == "paginated"
    )
    inherited_prefix = role == "subagent" and not paginated_child and (
        len(meta_events) > 1 or len(starts) > 1)
    boundary = 0 if paginated_child else (starts[-1] if starts else 0)
    baseline = {name: 0 for name in (
        "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_output_tokens",
    )}
    baseline_found = not inherited_prefix
    if inherited_prefix:
        for index, usage, _timestamp in usage_events:
            if index >= boundary:
                break
            baseline = usage
            baseline_found = True

    final_usage = usage_events[-1][1] if usage_events else None
    timed_usage = []
    timed_usage_complete = baseline_found
    aggregate_valid = baseline_found
    previous = baseline
    own_usage = {name: 0 for name in baseline}
    saw_usage = False
    for index, cumulative, occurred_at in usage_events:
        if index < boundary:
            continue
        delta = {name: cumulative[name] - previous[name] for name in previous}
        if any(value < 0 for value in delta.values()):
            # TokenUsage is cumulative for the lifetime of a paginated child,
            # including follow-up tasks.  Any rollback is therefore ambiguous
            # and must remain unpriced rather than starting a guessed epoch.
            aggregate_valid = False
            timed_usage_complete = False
            break
        saw_usage = True
        for name, value in delta.items():
            own_usage[name] += value
        timed_delta_valid = (
            occurred_at is not None
            and delta["cached_input_tokens"] <= delta["input_tokens"]
        )
        if not timed_delta_valid:
            timed_usage_complete = False
        elif timed_usage_complete and any(delta.values()):
            timed_usage.append({
                "occurred_at": occurred_at,
                "n_input_tokens": delta["input_tokens"],
                "n_cache_tokens": delta["cached_input_tokens"],
                "n_output_tokens": delta["output_tokens"],
            })
        previous = cumulative
    if not saw_usage or not aggregate_valid:
        own_usage = None
    if own_usage is None or not timed_usage:
        timed_usage_complete = False
    if timed_usage_complete:
        timed_totals = {
            "input_tokens": sum(item["n_input_tokens"] for item in timed_usage),
            "cached_input_tokens": sum(
                item["n_cache_tokens"] for item in timed_usage),
            "output_tokens": sum(item["n_output_tokens"] for item in timed_usage),
        }
        if any(timed_totals[name] != own_usage[name] for name in timed_totals):
            timed_usage_complete = False

    model_name = None
    for event in events[boundary:]:
        if event.get("type") != "turn_context":
            continue
        payload = event.get("payload") or {}
        model = payload.get("model") if isinstance(payload, dict) else None
        if isinstance(model, str) and model:
            model_name = model

    return {
        "session_id": session_id,
        "role": role,
        "parent_session_id": parent_session_id,
        "model_name": model_name,
        "inherited_usage": baseline if baseline_found else None,
        "total_usage": final_usage,
        "usage": own_usage,
        "token_usage_events": timed_usage if timed_usage_complete else [],
        "timed_usage_complete": timed_usage_complete,
        "task_started_count": len(starts),
        "task_complete_count": len(completes),
        "terminal_task_complete": (
            len(starts) == 1
            and len(completes) == 1
            and completes[0] == len(events) - 1
            and bool(usage_events)
            and usage_events[-1][0] < completes[0]
        ),
        "complete": (
            role != "unknown" and model_name is not None and own_usage is not None
        ),
        "events": events,
    }


def build_codex_trajectory_bundle(trial_dir: Path) -> dict | None:
    """Convert all Codex JSONL files into one versioned multi-agent bundle.

    The bundle retains every parsed event and the root/subagent relationship.
    It is scrubbed immediately before upload and scrubbed again by the server.
    Malformed lines or missing identity/usage evidence make the bundle
    incomplete; callers must suppress cost rather than report a partial sum.
    """
    sessions_dir = trial_dir / "agent" / "sessions"
    if not sessions_dir.is_dir():
        return None

    files = sorted(sessions_dir.rglob("*.jsonl"))
    if not files:
        return None

    sessions = []
    parse_complete = True
    for artifact_index, session_file in enumerate(files):
        events = []
        parse_error_count = 0
        try:
            handle = session_file.open(errors="replace")
        except OSError:
            parse_complete = False
            record = _analyze_codex_session_events(
                [], fallback_id=f"artifact:{artifact_index}")
            record["artifact_index"] = artifact_index
            record["parse_error_count"] = 1
            record["complete"] = False
            sessions.append(record)
            continue
        with handle:
            for line in handle:
                # Empty records carry no trajectory event and JSONL readers
                # conventionally ignore them.  Counting one as corrupt can
                # needlessly degrade an otherwise lossless Windows session.
                # Non-whitespace malformed records remain integrity failures
                # and are handled only by the strict terminal-evidence gate.
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    parse_error_count += 1
                    continue
                if not isinstance(event, dict):
                    parse_error_count += 1
                    continue
                events.append(event)

        record = _analyze_codex_session_events(
            events, fallback_id=f"artifact:{artifact_index}")
        record["artifact_index"] = artifact_index
        record["parse_error_count"] = parse_error_count
        if parse_error_count:
            record["complete"] = False
            parse_complete = False
        sessions.append(record)

    if not sessions:
        return None

    # A resumed run can leave multiple files for the same Codex session id.
    # Retain every event stream in the bundle, but count the richest complete
    # representative only once for billing.
    representatives = {}
    for item in sessions:
        key = item["session_id"]
        score = len(item["events"])
        previous = representatives.get(key)
        if previous is None or (item["complete"], score) > (
                previous["complete"], len(previous["events"])):
            representatives[key] = item

    usage_sessions = []
    for item in representatives.values():
        usage = item.get("usage")
        if usage is None:
            continue
        usage_sessions.append({
            "session_id": item["session_id"],
            "role": item["role"],
            "parent_session_id": item["parent_session_id"],
            "model_name": item["model_name"],
            "n_input_tokens": usage["input_tokens"],
            "n_cache_tokens": usage["cached_input_tokens"],
            "n_output_tokens": usage["output_tokens"],
            "n_reasoning_output_tokens": usage["reasoning_output_tokens"],
        })
    usage_sessions.sort(key=lambda item: (
        item["role"] != "root", item["session_id"]))
    root_count = sum(1 for item in representatives.values()
                     if item["role"] == "root")
    ids = set(representatives)
    parent_graph_valid = True
    for session_id, item in representatives.items():
        if item["role"] != "subagent":
            continue
        parent = item["parent_session_id"]
        seen = {session_id}
        while parent is not None:
            if parent in seen or parent not in ids:
                parent_graph_valid = False
                break
            seen.add(parent)
            parent = representatives[parent]["parent_session_id"]
    complete = (
        parse_complete
        and all(item["complete"] for item in representatives.values())
        and root_count == 1
        and parent_graph_valid
    )
    aggregate = {
        "n_input_tokens": sum(item["n_input_tokens"] for item in usage_sessions),
        "n_cache_tokens": sum(item["n_cache_tokens"] for item in usage_sessions),
        "n_output_tokens": sum(item["n_output_tokens"] for item in usage_sessions),
        "n_reasoning_output_tokens": sum(
            item["n_reasoning_output_tokens"] for item in usage_sessions),
    }
    token_usage_events = [
        event
        for item in representatives.values()
        for event in item["token_usage_events"]
    ]
    token_usage_events.sort(key=lambda item: item["occurred_at"])
    timed_usage_complete = (
        complete
        and all(item["timed_usage_complete"] for item in representatives.values())
    )
    parse_error_count = sum(item["parse_error_count"] for item in sessions)
    parse_degraded_completion_eligible = (
        not complete
        and len(files) == 1
        and len(representatives) == 1
        and root_count == 1
        and parse_error_count > 0
        and parent_graph_valid
        and all(
            item["role"] == "root"
            and item["model_name"] is not None
            and item["usage"] is not None
            and item["timed_usage_complete"]
            and item["terminal_task_complete"]
            for item in representatives.values()
        )
    )
    return {
        "schema_version": CODEX_TRAJECTORY_BUNDLE_SCHEMA,
        "complete": complete,
        "session_file_count": len(files),
        "agent_session_count": len(representatives),
        "root_session_count": root_count,
        "subagent_session_count": sum(1 for item in representatives.values()
                                      if item["role"] == "subagent"),
        "aggregate_usage": aggregate,
        "token_usage_events": token_usage_events if timed_usage_complete else [],
        "timed_usage_complete": timed_usage_complete,
        "parse_error_count": parse_error_count,
        "parse_degraded_completion_eligible": (
            parse_degraded_completion_eligible
        ),
        "usage_sessions": usage_sessions,
        "sessions": sessions,
    }


def build_kimi_trajectory_bundle(trial_dir: Path) -> dict | None:
    """Normalize Kimi's lossless session log into an auditable tool bundle.

    ``trajectory.json`` intentionally stays compact and display-oriented.  The
    official Kimi CLI also emits a private session protocol log containing
    every ToolCall/ToolResult pair; retain that evidence separately so the
    server can inspect shell arguments and their matching observations.

    A malformed or partial log still produces an incomplete bundle when there
    is usable evidence.  The bundle remains optional during the provider's
    rollout, so callers may upload the patch/result even when this returns
    ``None`` or ``complete`` is false.
    """
    log_path = trial_dir / KIMI_SESSION_LOG_RELATIVE
    if not log_path.is_file():
        return None

    trajectory_path = trial_dir / "agent" / "trajectory.json"
    session_id = None
    if trajectory_path.is_file():
        try:
            trajectory = json.loads(trajectory_path.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError):
            trajectory = None
        if isinstance(trajectory, dict):
            value = trajectory.get("session_id")
            if isinstance(value, str) and value:
                session_id = value

    events = []
    parse_error_count = 0
    call_ids: list[str] = []
    result_ids: list[str] = []
    runtime_version = None
    try:
        handle = log_path.open(errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                parse_error_count += 1
                continue
            if not isinstance(record, dict):
                parse_error_count += 1
                continue
            role = record.get("role")
            if role == "meta" and record.get("type") == "system.version":
                value = record.get("version")
                if isinstance(value, str):
                    runtime_version = value
                continue
            if role == "meta" and record.get("type") == "session.resume_hint":
                value = record.get("session_id")
                if isinstance(value, str) and value:
                    session_id = value
                continue
            if role == "meta":
                continue
            if role == "assistant":
                content = record.get("content")
                if isinstance(content, str) and content:
                    events.append({
                        "type": "content_part",
                        "occurred_at": None,
                        "payload": {"text": content},
                    })
                tool_calls = record.get("tool_calls", [])
                if tool_calls is None:
                    tool_calls = []
                if not isinstance(tool_calls, list):
                    parse_error_count += 1
                    continue
                for tool_call in tool_calls:
                    function = (
                        tool_call.get("function")
                        if isinstance(tool_call, dict) else None
                    )
                    call_id = (
                        tool_call.get("id")
                        if isinstance(tool_call, dict) else None
                    )
                    if (not isinstance(function, dict)
                            or not isinstance(call_id, str) or not call_id):
                        parse_error_count += 1
                        continue
                    name = function.get("name")
                    arguments = function.get("arguments")
                    if (not isinstance(name, str) or not name
                            or not isinstance(arguments, (str, dict))):
                        parse_error_count += 1
                        continue
                    call_ids.append(call_id)
                    events.append({
                        "type": "tool_call",
                        "occurred_at": None,
                        "payload": {
                            "call_id": call_id,
                            "tool_name": name,
                            "arguments": arguments,
                        },
                    })
                continue
            if role == "tool":
                call_id = record.get("tool_call_id")
                if not isinstance(call_id, str) or not call_id:
                    parse_error_count += 1
                    continue
                result_ids.append(call_id)
                events.append({
                    "type": "tool_result",
                    "occurred_at": None,
                    "payload": {
                        "call_id": call_id,
                        "output": record.get("content"),
                    },
                })
                continue
            parse_error_count += 1

    if not events:
        return None
    calls_unique = len(call_ids) == len(set(call_ids))
    results_unique = len(result_ids) == len(set(result_ids))
    paired = calls_unique and results_unique and set(call_ids) == set(result_ids)
    complete = (
        parse_error_count == 0
        and isinstance(session_id, str)
        and runtime_version == KIMI_CLI_VERSION
        and paired
    )
    session = {
        "session_id": session_id,
        "role": "root",
        "parent_session_id": None,
        "model_name": KIMI_MODEL,
        "runtime_generation": "node-stream-json-v1",
        "runtime_version": runtime_version,
        "artifact_index": 0,
        "parse_error_count": parse_error_count,
        "tool_call_count": len(call_ids),
        "tool_result_count": len(result_ids),
        "complete": complete,
        "events": events,
    }
    return {
        "schema_version": KIMI_TRAJECTORY_BUNDLE_SCHEMA,
        "complete": complete,
        "session_file_count": 1,
        "agent_session_count": 1,
        "root_session_count": 1,
        "subagent_session_count": 0,
        "sessions": [session],
    }


def codex_trajectory_bundle_usage(bundle: dict) -> dict:
    """Return the compact accounting/audit view of a full bundle."""
    return {
        "schema": bundle["schema_version"],
        "complete": bundle["complete"],
        "session_file_count": bundle["session_file_count"],
        "agent_session_count": bundle["agent_session_count"],
        "root_session_count": bundle["root_session_count"],
        "subagent_session_count": bundle["subagent_session_count"],
        **bundle["aggregate_usage"],
        "token_usage_events": bundle.get("token_usage_events", []),
        "timed_usage_complete": bool(bundle.get("timed_usage_complete")),
        "sessions": bundle["usage_sessions"],
    }


def aggregate_codex_session_usage(trial_dir: Path) -> dict | None:
    """Build a bundle and return its compact accounting/audit view."""
    bundle = build_codex_trajectory_bundle(trial_dir)
    return codex_trajectory_bundle_usage(bundle) if bundle is not None else None


def local_deep_swe_commit(tasks_root: Path) -> str | None:
    """HEAD commit of the volunteer's deep-swe checkout, or None when git is
    unavailable or tasks_root isn't inside a work tree (e.g. a plain tarball
    download — the per-task content hash still covers that case)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(tasks_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


# The public task repo DRadar pins in assignments.  It tracks the upstream
# corpus and also carries reviewed metadata corrections that must be identical
# on the server and every volunteer checkout.  Fetching pins from this URL
# (rather than an arbitrary existing ``origin``) keeps old upstream clones
# able to self-heal to a DRadar-published task commit.
DEEP_SWE_REPO = "https://github.com/SecurityMind/deep-swe"

# Temporary SecurityMind Pier build containing datacurve-ai/pier#23 and other
# reviewed compatibility fixes. Keep the immutable commit pin until those
# fixes are released upstream, then follow the official tag.
PIER_VERSION = "0.3.0.post4"
PIER_COMMIT = "fd5d8f18149844cbe255d7b98d655c7f7bbff030"
PIER_SPEC = (
    "datacurve-pier @ git+https://github.com/SecurityMind/pier.git@"
    f"{PIER_COMMIT}"
)
PIER_INSTALL_COMMAND = f"uv tool install --force '{PIER_SPEC}'"


def _pier_install_lock_path() -> Path:
    """One per-user lock because uv's tool store is shared across DRADAR_HOME."""
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "dradar" / "pier-install.lock"


@contextmanager
def _pier_install_lock():
    """Serialize the check/install/recheck transaction across CLI processes."""
    path = _pier_install_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as fh:
        lock_acquired = False
        windows_lock = False
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows runners
                import msvcrt
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    fh.write(b"\0")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                windows_lock = True
                lock_acquired = True
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                lock_acquired = True
            yield
        finally:
            if windows_lock and lock_acquired:  # pragma: no cover - Windows runners
                try:
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            elif os.name != "nt" and lock_acquired:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _pier_metadata_interpreters(pier: str) -> tuple[Path, ...]:
    """Return likely Python interpreters for the uv tool owning ``pier``.

    Importing Pier just to print its version also imports LiteLLM, which may
    fetch its remote model-price map.  Under a concurrent worker start that
    network-bound probe can time out and must never be mistaken for a missing
    installation.  Query the owning virtualenv's distribution metadata
    instead; this is local-only and does not import Pier.
    """
    try:
        executable = Path(pier).resolve(strict=True)
    except OSError:
        return ()
    candidates = (
        executable.parent / "python3",
        executable.parent / "python",
        executable.parent / "python.exe",
        executable.parent.parent / "python.exe",
    )
    return tuple(dict.fromkeys(
        candidate for candidate in candidates
        if candidate.is_file() and os.access(candidate, os.X_OK)
    ))


def _pier_metadata_version(pier: str) -> str | None:
    script = (
        "from importlib.metadata import version; "
        "print(version('datacurve-pier'))"
    )
    for interpreter in _pier_metadata_interpreters(pier):
        try:
            proc = subprocess.run(
                [str(interpreter), "-c", script],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return None


def _pier_cli_version(pier: str) -> str | None:
    try:
        proc = subprocess.run(
            [pier, "--version"], capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _pier_version(pier: str) -> str | None:
    """Read Pier's version locally, with its CLI only as a compatibility fallback."""
    return _pier_metadata_version(pier) or _pier_cli_version(pier)


def _pier_version_compatible(installed_version: str | None) -> bool:
    """Accept the pinned compatibility build and later post releases."""
    if installed_version == PIER_VERSION:
        return True
    required = re.fullmatch(r"(.+)\.post(\d+)", PIER_VERSION)
    installed = re.fullmatch(r"(.+)\.post(\d+)", installed_version or "")
    return bool(
        required and installed
        and installed.group(1) == required.group(1)
        and int(installed.group(2)) >= int(required.group(2))
    )


def ensure_pier() -> None:
    """Ensure the pinned Pier build with persistent-resume support is active."""
    pier = _resolve_user_tool("pier")
    installed_version = _pier_version(pier) if pier else None
    if _pier_version_compatible(installed_version):
        return
    uv = _resolve_user_tool("uv")
    if not uv:
        uv_hint = (
            "PowerShell: irm https://astral.sh/uv/install.ps1 | iex"
            if sys.platform == "win32"
            else "curl -LsSf https://astral.sh/uv/install.sh | sh"
        )
        raise RunnerError(
            f"Pier {PIER_VERSION} is required but uv is missing -- install uv first: "
            f"{uv_hint}")
    with _pier_install_lock():
        # Another process may have completed the same shared uv-tool install
        # while this one waited. Recheck under the lock before mutating it.
        pier = _resolve_user_tool("pier")
        installed_version = _pier_version(pier) if pier else None
        if _pier_version_compatible(installed_version):
            return
        if pier and installed_version is None:
            raise RunnerError(
                "couldn't verify the existing Pier installation; refusing to replace "
                "a shared tool that may be serving another worker. Run "
                f"`{PIER_INSTALL_COMMAND}` after active runs finish"
            )
        if pier:
            print(f"Pier {installed_version or 'unknown'} lacks persistent resume — "
                  f"installing SecurityMind build {PIER_VERSION}...")
        else:
            print(f"pier not found — installing SecurityMind build {PIER_VERSION}...")
        proc = subprocess.run([uv, "tool", "install", "--force", PIER_SPEC])
        active_pier = _resolve_user_tool("pier")
        active_version = _pier_version(active_pier) if active_pier else None
        if proc.returncode != 0 or not _pier_version_compatible(active_version):
            raise RunnerError(
                f"couldn't activate Pier {PIER_VERSION}; run `{PIER_INSTALL_COMMAND}` "
                "yourself and make sure ~/.local/bin precedes other Pier installs on PATH")


def ensure_tasks_root(tasks_root: Path, benchmark_id: str = "deep-swe") -> None:
    """Auto-clone the public deep-swe task repo if the configured tasks_root
    doesn't exist yet (magic-command convention: tasks_root is <repo>/tasks), so
    a fresh volunteer doesn't have to clone it by hand. No-op if it's already
    there; bails quietly if the path doesn't fit the convention (leave it to
    the user rather than clobber something)."""
    if tasks_root.is_dir():
        return
    if benchmark_id != "deep-swe":
        raise RunnerError(
            f"task pack for benchmark {benchmark_id!r} is not installed at "
            f"{tasks_root}; run `dradar login --benchmark {benchmark_id} "
            "--tasks-root <downloaded-task-directory>` first"
        )
    if tasks_root.name != "tasks":
        raise RunnerError(
            f"tasks_root {tasks_root} doesn't exist and doesn't look like a "
            f"deep-swe/tasks path; clone {DEEP_SWE_REPO} and point --tasks-root at its tasks/")
    repo_dir = tasks_root.parent
    if repo_dir.exists() and any(repo_dir.iterdir()):
        raise RunnerError(
            f"{repo_dir} exists but has no tasks/ dir; not touching it — "
            f"make sure it's a clean {DEEP_SWE_REPO} checkout")
    print(f"deep-swe task repo not found; cloning {DEEP_SWE_REPO} → {repo_dir} (one-time)...")
    proc = subprocess.run(["git", "clone", DEEP_SWE_REPO, str(repo_dir)])
    if proc.returncode != 0 or not tasks_root.is_dir():
        raise RunnerError(f"failed to clone deep-swe into {repo_dir}")
    print(f"  cloned; tasks at {tasks_root}")


def sync_deep_swe_commit(tasks_root: Path, pinned: str) -> bool:
    """Fetch + checkout the exact commit the server grades against, so a drifted
    checkout self-heals instead of hard-failing. Returns True on success."""
    for cmd in (["git", "-C", str(tasks_root), "fetch", "--depth", "1", DEEP_SWE_REPO, pinned],
                ["git", "-C", str(tasks_root), "checkout", pinned]):
        try:
            if subprocess.run(cmd, capture_output=True, text=True, timeout=120).returncode != 0:
                return False
        except (OSError, subprocess.TimeoutExpired):
            return False
    return local_deep_swe_commit(tasks_root) == pinned


def _task_snapshot_lock_path(home: Path) -> Path:
    return home / "task-snapshots" / "prepare.lock"


@contextmanager
def _task_snapshot_lock(home: Path):
    """Serialize immutable task-snapshot creation across local run parents."""

    path = _task_snapshot_lock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as handle:
        locked = False
        windows_lock = False
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows runners
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                windows_lock = True
                locked = True
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
            yield
        finally:
            if windows_lock and locked:  # pragma: no cover - Windows runners
                try:
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            elif os.name != "nt" and locked:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def prepare_pinned_deep_swe_tasks(home: Path, pinned: str) -> Path:
    """Return an immutable, DRadar-owned checkout for one grading commit.

    A volunteer's configured checkout may contain their own edits, and two live
    batches may temporarily target different grading commits.  Updating that
    shared checkout in place is therefore both destructive and racy.  Build one
    managed snapshot per immutable commit instead; the configured checkout is
    never fetched, reset, or checked out by this path.
    """

    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", str(pinned)):
        raise RunnerError("the server returned an invalid deep-swe version pin")
    commit = str(pinned).lower()
    snapshots = home / "task-snapshots" / "deep-swe"
    destination = snapshots / commit
    tasks_root = destination / "tasks"

    with _task_snapshot_lock(home):
        if (
            tasks_root.is_dir()
            and local_deep_swe_commit(tasks_root) == commit
        ):
            return tasks_root
        if destination.exists():
            raise RunnerError(
                "the managed deep-swe task snapshot is incomplete; remove only "
                f"{destination} after active DRadar runs finish, then retry"
            )

        snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = Path(tempfile.mkdtemp(
            prefix=f".{commit[:12]}-", dir=snapshots,
        ))
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            commands = (
                (["git", "init", "-q", str(temporary)], 30),
                ([
                    "git", "-C", str(temporary), "fetch", "--quiet",
                    "--depth", "1", "--no-tags", DEEP_SWE_REPO, commit,
                ], 180),
                ([
                    "git", "-C", str(temporary), "-c",
                    "advice.detachedHead=false", "checkout", "--quiet",
                    "--detach", "FETCH_HEAD",
                ], 60),
            )
            for command, timeout in commands:
                try:
                    result = subprocess.run(
                        command, capture_output=True, text=True,
                        timeout=timeout, env=env,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise RunnerError(
                        "couldn't prepare the isolated deep-swe task snapshot; "
                        "check Git, network access, and free disk space"
                    ) from exc
                if result.returncode != 0:
                    raise RunnerError(
                        "couldn't prepare the isolated deep-swe task snapshot; "
                        "check Git, network access, and free disk space"
                    )
            prepared_tasks = temporary / "tasks"
            if (
                not prepared_tasks.is_dir()
                or local_deep_swe_commit(prepared_tasks) != commit
            ):
                raise RunnerError(
                    "the isolated deep-swe task snapshot did not match the "
                    "server's grading version"
                )
            os.replace(temporary, destination)
            return tasks_root
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)


def check_task_content_hash(assignment: dict, tasks_root: Path) -> bool | None:
    """Compare the server's task_content_hash against this volunteer's local
    checkout. Returns None when the assignment carries no hash to compare
    against (older server). Callers fail closed on a mismatch unless the user
    explicitly selected the non-comparable ``--allow-task-drift`` path."""
    expected = assignment.get("task_content_hash")
    if not expected:
        return None
    actual = task_content_hash(tasks_root, assignment["task_id"])
    match = actual == expected
    if not match:
        print(
            "warning: your local benchmark task does not match the server "
            "copy; refresh the selected benchmark task pack"
        )
    return match


def task_content_mismatch_diagnostic(
    assignment: dict, tasks_root: Path, local_commit: str | None,
) -> dict[str, object]:
    """Return bounded, content-free evidence for a pre-model hash refusal.

    Only short hexadecimal identifiers and two false lifecycle facts leave the
    machine.  Paths, task text, Git output, environment values and provider
    credentials are deliberately excluded.
    """
    expected = str(assignment.get("task_content_hash") or "")
    actual = task_content_hash(tasks_root, assignment["task_id"])
    server_commit = str(assignment.get("deep_swe_commit") or "")

    diagnostic: dict[str, object] = {
        "schema": "dradar-task-content-mismatch-v1",
        "failure_code": "task_content_mismatch",
        "expected_hash_prefix": expected[:12].lower(),
        "actual_hash_prefix": actual[:12].lower(),
        "model_started": False,
        "quota_consumed": False,
    }
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", server_commit):
        diagnostic["server_task_commit_prefix"] = server_commit[:12].lower()
    if local_commit and re.fullmatch(r"[0-9a-fA-F]{7,64}", local_commit):
        diagnostic["local_task_commit_prefix"] = local_commit[:12].lower()
    return diagnostic


def _tail(log_path: Path, n: int = 15) -> str:
    """Last n lines of the pier log, for inlining into trial-failure messages:
    after a 30-120 min run the actual cause (docker pull failure, auth
    rejection, rate-limit death) sits at the end of that log, and just naming
    the file makes the volunteer go hunt for it. Local-terminal only — never
    uploaded — so no scrub concern."""
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


# Keep the local progress signal aligned with the preparation heartbeat.  A
# build can be silent while Docker pulls layers, so a one-minute cadence made
# a healthy cold start look abandoned in the radar UI.
HEARTBEAT_SEC = 30
TRIAL_TIMEOUT_RETURNCODE = 124
LIVE_ACCOUNT_ERROR_CONFIRMATIONS = 3
_LIVE_ACCOUNT_TERMINAL_KINDS = {
    "auth", "insufficient-balance", "quota-limit",
}


def _last_activity(log_path: Path) -> str:
    """The newest meaningful chunk of the pier log, for heartbeat lines.
    pier redraws its progress bar with carriage returns inside one physical
    line, so split on \\r as well and skip pure control/blank chunks."""
    raw = _tail(log_path, 1)
    chunks = [c.strip() for c in raw.replace("\r", "\n").splitlines() if c.strip()]
    return (chunks[-1][:120] if chunks else "still running (no new log output)")


def _scan_live_account_errors(
    jobs_dir: Path,
    job_name: str,
    offsets: dict[Path, int],
    counts: dict[str, int],
) -> str | None:
    """Inspect only structured Codex error events from the current job.

    Pier normally reports the agent's exception after ``pier run`` exits.  A
    Codex process can instead keep retrying a terminal account error forever,
    which prevents that post-process classification from ever running.  Read
    new JSONL records incrementally and return a confirmed account-wide kind.
    Prompt, reasoning, tool calls and trajectory content are ignored.
    """
    root = jobs_dir / job_name
    try:
        paths = sorted(root.glob("*/agent/codex.txt"))
    except OSError:
        return None
    for path in paths:
        try:
            size = path.stat().st_size
            offset = offsets.get(path, 0)
            if offset > size:
                offset = 0
            with path.open("rb") as stream:
                stream.seek(offset)
                while True:
                    line_start = stream.tell()
                    raw = stream.readline()
                    if not raw:
                        offsets[path] = stream.tell()
                        break
                    if not raw.endswith(b"\n"):
                        # Do not consume a record while Codex is still writing it.
                        offsets[path] = line_start
                        break
                    offsets[path] = stream.tell()
                    try:
                        event = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") == "turn.completed":
                        counts.clear()
                        continue
                    if event.get("type") != "error":
                        continue
                    message = event.get("message")
                    if not isinstance(message, str):
                        continue
                    kind = classify_exception_message(message)
                    if kind not in _LIVE_ACCOUNT_TERMINAL_KINDS:
                        continue
                    counts[kind] = counts.get(kind, 0) + 1
                    if counts[kind] >= LIVE_ACCOUNT_ERROR_CONFIRMATIONS:
                        return kind
        except OSError:
            continue
    return None


def _live_account_error_message(kind: str) -> str:
    messages = {
        "auth": "live agent repeatedly reported authentication failed",
        "insufficient-balance": (
            "live agent repeatedly reported insufficient balance"
        ),
        "quota-limit": "live agent repeatedly reported quota exhausted",
    }
    return messages[kind] + "; aborting this trial safely"


def _trial_timeout_sec(assignment: dict) -> int:
    """Cap for one trial: a generous multiple of the server's estimate, with
    a one-hour floor for image pull/build and long model turns."""
    est_min = assignment.get("est_minutes") or 30
    return max(3600, int(est_min) * 60 * 4)


def _subscription_trial_timeout_sec(assignment: dict) -> int:
    """Give public-beta subscription harnesses room for underestimated tasks."""
    return max(
        BETA_SUBSCRIPTION_TRIAL_TIMEOUT_FLOOR_SEC,
        _trial_timeout_sec(assignment),
    )


def _zcode_session_timeout_sec(assignment: dict) -> int:
    """Keep ZCode behind Pier's watchdog; DRadar's outer cap stays authoritative."""
    return _zcode_trial_timeout_sec(assignment) + 60


def _zcode_trial_timeout_sec(assignment: dict) -> int:
    """Leave one minute before ZCode's protocol-safe 24-hour ceiling."""
    return min(_subscription_trial_timeout_sec(assignment), 24 * 60 * 60 - 60)


def _effective_trial_timeout_sec(assignment: dict) -> int:
    if assignment.get("benchmark_id") == POMPEII_BENCHMARK_ID:
        # The outer watchdog starts before image/environment setup while Pier's
        # hard agent deadline starts at agent execution. Keep setup outside the
        # promised execution budget instead of undercutting it.
        ordinary = (
            _subscription_trial_timeout_sec(assignment)
            if assignment.get("agent") in BETA_SUBSCRIPTION_AGENTS
            else _trial_timeout_sec(assignment)
        )
        return max(
            ordinary,
            pompeii_agent_timeout_sec(assignment)
            + POMPEII_OUTER_WATCHDOG_SLACK_SEC,
        )
    agent = assignment.get("agent")
    if agent == ZCODE_AGENT:
        return _zcode_trial_timeout_sec(assignment)
    if agent in BETA_SUBSCRIPTION_AGENTS:
        return _subscription_trial_timeout_sec(assignment)
    return _trial_timeout_sec(assignment)


def _effective_run_timeout_sec(
    assignment: dict,
    task_path: Path,
    environment_build_timeout_multiplier: float,
) -> int:
    """Include the bounded two-attempt build allowance in DRadar's watchdog.

    Pier retries ``EnvironmentStartTimeoutError`` once.  The old outer
    watchdog started before image setup and could therefore kill a run while
    Pier was still within its enlarged build window.  Account for both build
    attempts, then retain the task/agent budget as the minimum.  For Pompeii
    the model's hard deadline and the setup allowance are additive: a full
    build must not steal the agent's promised execution time.
    """

    base = _task_environment_build_timeout_sec(task_path)
    if base is None:
        base = DEFAULT_ENVIRONMENT_BUILD_TIMEOUT_SEC
    build_allowance = int(math.ceil(
        base * environment_build_timeout_multiplier
        * PIER_ENVIRONMENT_START_ATTEMPTS
    )) + ENVIRONMENT_BUILD_WATCHDOG_SLACK_SEC
    current = _effective_trial_timeout_sec(assignment)
    # Test/integration adapters may deliberately return a non-positive
    # watchdog to force the first heartbeat path.  Preserve that explicit
    # override rather than replacing it with the normal build allowance.
    if current <= 0:
        return current
    if assignment.get("benchmark_id") == POMPEII_BENCHMARK_ID:
        # ``current`` already includes Pompeii's hard agent deadline plus its
        # finalization slack.  Keep that complete budget, then add the build
        # allowance before the outer watchdog is armed.
        agent_budget = current
    else:
        # For ordinary tasks Pier's inner watchdog is the task timeout scaled
        # by _agent_timeout_multiplier.  The old outer watchdog may be either
        # below that declared timeout (short server estimate) or above it
        # (long estimate), so account for the actual inner budget rather than
        # merely taking max(current, build_allowance).
        declared = _task_agent_timeout_sec(task_path)
        if (
            isinstance(declared, (int, float))
            and not isinstance(declared, bool)
            and math.isfinite(float(declared))
            and float(declared) > 0
        ):
            multiplier = _agent_timeout_multiplier(assignment, task_path)
            agent_budget = max(
                float(current),
                math.ceil(float(declared) * multiplier),
            )
        else:
            # If task metadata cannot prove an inner budget, retain the old
            # outer watchdog as the conservative model-time allowance.
            agent_budget = float(current)
    return max(
        current,
        int(math.ceil(agent_budget + build_allowance)),
    )


def _zcode_runtime_diagnostic(jobs_dir: Path, job_name: str) -> dict[str, object]:
    """Read the adapter's allowlisted lifecycle snapshot, never its raw logs."""
    try:
        paths = list(
            (jobs_dir / job_name).glob("*/agent/zcode-runtime-diagnostic.json")
        )
    except OSError:
        return {}
    if len(paths) != 1:
        return {}
    try:
        payload = json.loads(paths[0].read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema") != "dradar-zcode-runtime-v1":
        return {}
    status = payload.get("status")
    turns = payload.get("turn_count")
    if status not in {"idle", "running", "error", "failed", "stopped", "unknown"}:
        status = "unknown"
    if not isinstance(turns, int) or isinstance(turns, bool) or not 0 <= turns <= 100000:
        turns = 0
    return {
        "zcode_last_status": status,
        "zcode_turn_count": turns,
        "zcode_seen_running": payload.get("seen_running") is True,
        "zcode_terminal_observed": payload.get("terminal_observed") is True,
    }


def _zcode_provider_failure_facts(outcome_path: Path | None) -> dict[str, object]:
    """Return bounded transport facts from the last structured ZCode failure.

    Provider messages and request identifiers may contain private data, so the
    runner exposes only the small enum/number/bool set needed for retry policy.
    """
    payload = _read_capped_json_object(
        outcome_path, _ZCODE_TERMINAL_ARTIFACT_MAX_BYTES,
    )
    if payload.get("schema") != "dradar-zcode-outcome-v1":
        return {}
    events_object = payload.get("events")
    events = events_object.get("events") if isinstance(events_object, dict) else None
    if not isinstance(events, list) or len(events) > 5000:
        return {}
    result: dict[str, object] = {}
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "session.updated":
            continue
        value = event.get("payload")
        if not isinstance(value, dict) or value.get("type") != "model_request_failed":
            continue
        # Only the latest structured model failure controls retry policy. Do
        # not accidentally combine fields from two different requests.
        result = {}
        reason = value.get("reason")
        if reason in {
            "network_error", "rate_limited", "authentication_error",
            "permission_denied", "provider_error", "unknown",
        }:
            result["zcode_provider_failure_reason"] = reason
        status = value.get("statusCode")
        if isinstance(status, int) and not isinstance(status, bool) and 0 <= status <= 999:
            result["zcode_provider_status_code"] = status
        retryable = value.get("retryable")
        if isinstance(retryable, bool):
            result["zcode_provider_retryable"] = retryable
    return result


def _zcode_failure_diagnostic(
    assignment: dict,
    task_path: Path,
    jobs_dir: Path,
    job_name: str,
    failure_code: str,
) -> dict[str, object] | None:
    """Build a bounded numeric/enum-only diagnostic for assignment/stopped."""
    if assignment.get("agent") != ZCODE_AGENT:
        return None
    base_timeout = _task_agent_timeout_sec(task_path)
    multiplier = _agent_timeout_multiplier(assignment, task_path)
    diagnostic: dict[str, object] = {
        "schema": "dradar-runner-failure-v1",
        "failure_code": failure_code,
        "trial_timeout_sec": _zcode_trial_timeout_sec(assignment),
        "zcode_session_timeout_sec": _zcode_session_timeout_sec(assignment),
    }
    est_minutes = assignment.get("est_minutes")
    if (
        isinstance(est_minutes, (int, float))
        and not isinstance(est_minutes, bool)
        and 0 < est_minutes <= 1440
    ):
        diagnostic["est_minutes"] = float(est_minutes)
    if isinstance(base_timeout, (int, float)) and base_timeout > 0:
        diagnostic["task_agent_timeout_sec"] = int(base_timeout)
        diagnostic["pier_agent_timeout_sec"] = int(math.ceil(base_timeout * multiplier))
    diagnostic.update(_zcode_runtime_diagnostic(jobs_dir, job_name))
    try:
        outcomes = list((jobs_dir / job_name).glob("*/agent/zcode-outcome.json"))
    except OSError:
        outcomes = []
    if len(outcomes) == 1:
        diagnostic.update(_zcode_provider_failure_facts(outcomes[0]))
    return diagnostic


_ZCODE_TERMINAL_ARTIFACT_MAX_BYTES = 8 * 1024 * 1024


def _read_capped_json_object(path: Path | None, max_bytes: int) -> dict:
    if path is None:
        return {}
    try:
        if (
            not _plain_file(path)
            or path.stat().st_size > max_bytes
        ):
            return {}
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return {}
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _zcode_quota_limit_facts(outcome_path: Path | None) -> dict[str, object] | None:
    """Return allowlisted facts for ZCode's account-window exhaustion event.

    ZCode reports Coding Plan exhaustion as a structured provider event
    even when Pier later reduces the run to ``model.patch missing``.  A bare
    429 is only burst throttling and must remain retryable, so the account-wide
    terminal requires all of the provider code, HTTP status, reason,
    non-retryable bit, and explicit usage-limit/reset semantics to agree.

    The outcome is already adapter-redacted.  Still, expose only enums and
    numbers to callers; never propagate the provider message or request IDs.
    """
    payload = _read_capped_json_object(
        outcome_path, _ZCODE_TERMINAL_ARTIFACT_MAX_BYTES,
    )
    if payload.get("schema") != "dradar-zcode-outcome-v1":
        return None

    events_object = payload.get("events")
    events = events_object.get("events") if isinstance(events_object, dict) else None
    if not isinstance(events, list) or len(events) > 5000:
        return None
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "session.updated":
            continue
        value = event.get("payload")
        if not isinstance(value, dict) or value.get("type") != "model_request_failed":
            continue
        provider_code = value.get("providerErrorCode")
        status = value.get("statusCode")
        reason = value.get("reason")
        retryable = value.get("retryable")
        if not (
            str(provider_code) == "1308"
            and status == 429
            and not isinstance(status, bool)
            and reason == "rate_limited"
            and retryable is False
        ):
            continue
        messages = (
            value.get("providerErrorMessage"), value.get("message"),
        )
        for message in messages:
            if not isinstance(message, str) or len(message) > 4096:
                continue
            low = " ".join(message.lower().split())
            chinese_semantics = (
                ("使用上限" in message or "用量上限" in message)
                and "重置" in message
            )
            english_semantics = (
                ("usage limit" in low or "quota limit" in low)
                and "reset" in low
            )
            if chinese_semantics or english_semantics:
                return {
                    "provider_code": 1308,
                    "status_code": 429,
                    "reason": "rate_limited",
                    "retryable": False,
                    "window_reset_explicit": True,
                }
    return None


def _zcode_false_success_reason(
    result_path: Path | None,
    usage_path: Path | None,
    runtime_diagnostic: dict[str, object],
    expected_model: str = ZCODE_MODEL,
) -> str | None:
    """Recognize only hard ZCode rc=0 false-success evidence.

    ZCode can return to ``idle`` with process status zero after the provider
    reports a model error.  Pier then writes a syntactically valid result even
    though there is no completed provider turn to account for and, in the
    follow-on failure mode, no agent output at all.  Such a result must keep
    the assignment retryable instead of being uploaded as an ordinary model
    answer.

    Incomplete token telemetry alone is deliberately *not* a failure: the
    accounting path already records its own evidence tier, and a real patch
    must still be gradeable when the per-request ledger is unavailable or
    unreconciled.  ``complete`` here means ZCode emitted a provider-backed
    ``turn.completed`` aggregate; request-ledger completeness only controls
    accounting confidence.  A positive provider model-error counter is a
    false-success signature only when the session never recovered to such a
    completed provider turn.
    """
    result = _read_capped_json_object(
        result_path, _ZCODE_TERMINAL_ARTIFACT_MAX_BYTES,
    )
    agent_result = result.get("agent_result")
    if not isinstance(agent_result, dict):
        agent_result = {}
    metadata = agent_result.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    embedded_usage = metadata.get("provider_usage")
    if not isinstance(embedded_usage, dict):
        embedded_usage = {}
    sidecar_usage = _read_capped_json_object(
        usage_path, _ZCODE_TERMINAL_ARTIFACT_MAX_BYTES,
    )
    if not (
        sidecar_usage.get("schema")
        == "dradar-subscription-provider-usage-v1"
        and sidecar_usage.get("provider") == "zcode"
        and sidecar_usage.get("model") == expected_model
    ):
        sidecar_usage = {}
    provider_usage = sidecar_usage or embedded_usage

    request_count = provider_usage.get("request_count")
    provider_turn_completed = (
        provider_usage.get("schema") == "dradar-subscription-provider-usage-v1"
        and provider_usage.get("provider") == "zcode"
        and provider_usage.get("model") == expected_model
        and provider_usage.get("complete") is True
        and isinstance(request_count, int)
        and not isinstance(request_count, bool)
        and request_count > 0
    )

    model_error_count = provider_usage.get("model_error_count")
    explicit_model_error = (
        isinstance(model_error_count, int)
        and not isinstance(model_error_count, bool)
        and model_error_count > 0
    )

    steps = agent_result.get("n_agent_steps")
    meaningful_agent_result = (
        isinstance(steps, int)
        and not isinstance(steps, bool)
        and steps > 0
    )
    status = runtime_diagnostic.get("zcode_last_status")
    if runtime_diagnostic.get("zcode_terminal_observed") is not True:
        return "terminal_not_observed"
    if status in {"error", "failed", "stopped"}:
        return "terminal_status"
    if explicit_model_error and not provider_turn_completed:
        return "provider_model_error"
    if not meaningful_agent_result and not provider_turn_completed:
        return "empty_agent_result"
    return None


@contextmanager
def _dsh_tasks_overlay(
    assignment: dict,
    tasks_root: Path,
    work_dir: Path,
    job_name: str,
):
    """Supply public Pier's artifact hook without mutating the task pack."""

    task_id = assignment.get("task_id")
    if (
        not isinstance(task_id, str)
        or not task_id
        or Path(task_id).name != task_id
    ):
        raise RunnerError(f"unsafe DSH task id {task_id!r}")
    source = tasks_root / task_id
    if not source.is_dir():
        raise RunnerError(f"DSH task directory is missing: {source}")
    task_toml = source / "task.toml"
    try:
        task_text = task_toml.read_text(encoding="utf-8")
        task_config = tomllib.loads(task_text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RunnerError(f"DSH task.toml is unreadable: {exc}") from exc
    no_network = task_config.get("agent", {}).get("network_mode") == "no-network"
    allow_internet = task_config.get("environment", {}).get("allow_internet")
    if no_network and allow_internet is True:
        raise RunnerError(
            "DSH task has contradictory no-network/allow_internet=true policy"
        )
    needs_network_fence = no_network and allow_internet is not False
    needs_artifact_hook = not (source / "pre_artifacts.sh").is_file()
    if not needs_network_fence and not needs_artifact_hook:
        yield tasks_root
        return

    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{job_name}-dsh-task-", dir=work_dir,
    ) as temporary:
        overlay_root = Path(temporary)
        overlay_task = overlay_root / task_id
        shutil.copytree(source, overlay_task, symlinks=True)
        if needs_network_fence:
            environment_header = re.search(
                r"(?m)^\[environment\][ \t]*(?:#.*)?$", task_text
            )
            if environment_header is None:
                task_text = task_text.rstrip() + "\n\n[environment]\nallow_internet = false\n"
            else:
                insert_at = environment_header.end()
                task_text = (
                    task_text[:insert_at]
                    + "\nallow_internet = false"
                    + task_text[insert_at:]
                )
            (overlay_task / "task.toml").write_text(task_text, encoding="utf-8")
        if needs_artifact_hook:
            base_commit = task_config.get("metadata", {}).get(
                "base_commit_hash", ""
            )
            if not isinstance(base_commit, str) or (
                base_commit
                and re.fullmatch(r"[0-9a-f]{40}", base_commit) is None
            ):
                raise RunnerError(
                    "DSH task has an invalid metadata.base_commit_hash"
                )
            hook = overlay_task / "pre_artifacts.sh"
            hook.write_text(
                DSH_PRE_ARTIFACTS_SCRIPT.replace(
                    "__DRADAR_BASE_COMMIT__", base_commit
                ),
                encoding="utf-8",
            )
            hook.chmod(0o755)
        yield overlay_root


@contextmanager
def _artifact_tasks_overlay(
    assignment: dict,
    tasks_root: Path,
    work_dir: Path,
    job_name: str,
):
    """Backport ``verifier.collect`` to Pier's public pre-artifact hook.

    DRadar intentionally launches Pier with ``--disable-verification`` because
    volunteer clients must never run benchmark verification.  Pier 0.3.0 also
    skips ``[[verifier.collect]]`` in that mode, so task packs that migrated
    away from ``pre_artifacts.sh`` would otherwise complete a paid model turn
    and then lose ``model.patch``.  Copy only the selected task into a private
    overlay and add the equivalent, base-commit-pinned collection hook.
    """

    task_id = assignment.get("task_id")
    if (
        not isinstance(task_id, str)
        or not task_id
        or Path(task_id).name != task_id
    ):
        raise RunnerError(f"unsafe task id {task_id!r}")
    source = tasks_root / task_id
    if not source.is_dir():
        raise RunnerError(f"task directory is missing: {source}")
    if (source / "pre_artifacts.sh").is_file():
        yield tasks_root
        return
    try:
        task_config = tomllib.loads(
            (source / "task.toml").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RunnerError(f"task.toml is unreadable: {exc}") from exc
    base_commit = task_config.get("metadata", {}).get("base_commit_hash", "")
    if not isinstance(base_commit, str) or (
        base_commit
        and re.fullmatch(r"[0-9a-f]{40}", base_commit) is None
    ):
        raise RunnerError("task has an invalid metadata.base_commit_hash")

    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{job_name}-artifact-task-", dir=work_dir,
    ) as temporary:
        overlay_root = Path(temporary)
        overlay_task = overlay_root / task_id
        shutil.copytree(source, overlay_task, symlinks=True)
        hook = overlay_task / "pre_artifacts.sh"
        hook.write_text(
            DSH_PRE_ARTIFACTS_SCRIPT.replace(
                "__DRADAR_BASE_COMMIT__", base_commit
            ),
            encoding="utf-8",
        )
        hook.chmod(0o755)
        yield overlay_root


@contextmanager
def _antigravity_tasks_overlay(
    assignment: dict,
    tasks_root: Path,
    work_dir: Path,
    job_name: str,
):
    """Collect Antigravity's complete final worktree via public Pier.

    The overlay is per-run and leaves the downloaded benchmark task untouched.
    It deliberately replaces even an existing task hook: older hooks compare
    only ``base..HEAD`` and lose Antigravity edits that remain staged,
    unstaged, or newly created at terminal SUCCESS.
    """

    task_id = assignment.get("task_id")
    if (
        not isinstance(task_id, str)
        or not task_id
        or Path(task_id).name != task_id
    ):
        raise RunnerError(f"unsafe Antigravity task id {task_id!r}")
    source = tasks_root / task_id
    if not source.is_dir():
        raise RunnerError(f"Antigravity task directory is missing: {source}")
    task_toml = source / "task.toml"
    try:
        task_config = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RunnerError(f"Antigravity task.toml is unreadable: {exc}") from exc
    base_commit = task_config.get("metadata", {}).get("base_commit_hash")
    valid_commit = (
        isinstance(base_commit, str)
        and re.fullmatch(r"[0-9a-f]{40}", base_commit) is not None
    )
    # Pompeii's reviewed task pack creates a fixed local tag rather than a
    # portable 40-byte commit id.  Keep the shell substitution fail-closed:
    # only that exact tag is accepted, and only for Pompeii task ids.
    valid_pompeii_tag = (
        base_commit == "pompeii-base"
        and task_id.startswith(POMPEII_BENCHMARK_ID + "-")
    )
    if not (valid_commit or valid_pompeii_tag):
        raise RunnerError(
            "Antigravity task has an invalid metadata.base_commit_hash"
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{job_name}-antigravity-task-", dir=work_dir,
    ) as temporary:
        overlay_root = Path(temporary)
        overlay_task = overlay_root / task_id
        shutil.copytree(source, overlay_task, symlinks=True)
        hook = overlay_task / "pre_artifacts.sh"
        if hook.exists() or hook.is_symlink():
            hook.unlink()
        hook.write_text(
            ANTIGRAVITY_PRE_ARTIFACTS_SCRIPT.replace(
                "__DRADAR_BASE_COMMIT__", base_commit
            ),
            encoding="utf-8",
        )
        hook.chmod(0o755)
        yield overlay_root


_PIER_RUNTIME_PROJECT_RE = re.compile(
    r"[a-z0-9][a-z0-9-]*__[a-z0-9]{6,8}$", re.IGNORECASE,
)


def _terminate_pier_process_tree(proc: subprocess.Popen) -> bool:
    """TERM then KILL the isolated Pier process group; return if KILL was used."""

    pid = getattr(proc, "pid", None)
    group_signalled = False
    if os.name != "nt" and isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGTERM)
            group_signalled = True
        except ProcessLookupError:
            pass
        except OSError:
            group_signalled = False
    if not group_signalled:
        proc.terminate()
    try:
        proc.wait(timeout=15)
        leader_exited = True
    except subprocess.TimeoutExpired:
        leader_exited = False

    if leader_exited:
        if os.name == "nt" or not isinstance(pid, int) or pid <= 0:
            return False
        try:
            # The leader may exit before its compose/helper children. Since
            # Pier is started in a new session, PGID == leader PID; signal 0
            # tells us whether anything in that group still survives.
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise RunnerError(
                "Pier leader exited but its process group could not be audited",
            ) from exc
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise RunnerError(
                "Pier leader exited but its surviving process group could not be killed",
            ) from exc
        return True

    if os.name != "nt" and isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise RunnerError("Pier process group could not be killed") from exc
    else:
        proc.kill()
    proc.wait()
    return True


def _cleanup_exited_pier_process_group(proc: subprocess.Popen) -> bool:
    """Reap helpers left in Pier's isolated POSIX group after its leader exits."""

    pid = getattr(proc, "pid", None)
    if os.name == "nt" or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise RunnerError(
            "Pier exited but its process group could not be audited",
        ) from exc
    _terminate_pier_process_tree(proc)
    return True


def _path_is_below(path: str, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


@dataclass(frozen=True)
class PierContainerCleanup:
    matched: int = 0
    running: int = 0


def _cleanup_terminated_pier_containers(job_root: Path) -> PierContainerCleanup:
    """Remove only surviving containers bound to the exact terminated job."""

    # Docker Desktop/daemon queries can fail between ``ps`` and ``inspect``
    # while a just-exited compose project is disappearing. Re-run the whole
    # ownership audit a few times: every attempt still requires positive
    # exact-job ownership before removal, so retries do not broaden cleanup.
    audit_error: RunnerError | None = None
    for attempt in range(3):
        try:
            try:
                listed = subprocess.run(
                    [
                        "docker", "ps", "-aq", "--filter",
                        "label=com.docker.compose.project",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RunnerError(
                    "Docker could not be queried for terminated Pier containers",
                ) from exc
            if listed.returncode != 0:
                raise RunnerError(
                    "Docker rejected the terminated Pier container check",
                )
            container_ids = [
                value for value in listed.stdout.split()
                if re.fullmatch(r"[0-9a-f]{12,64}", value)
            ]
            if not container_ids:
                return PierContainerCleanup()
            try:
                inspected = subprocess.run(
                    ["docker", "inspect", *container_ids],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RunnerError(
                    "terminated Pier container ownership is unknown",
                ) from exc
            if inspected.returncode != 0:
                raise RunnerError(
                    "terminated Pier container inspection failed",
                )
            try:
                containers = json.loads(inspected.stdout or "[]")
            except json.JSONDecodeError as exc:
                raise RunnerError(
                    "Docker returned invalid orphan inspection data",
                ) from exc
            break
        except RunnerError as exc:
            audit_error = exc
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    else:  # pragma: no cover - the bounded loop either breaks or raises
        assert audit_error is not None
        raise audit_error

    owned: list[str] = []
    running: list[str] = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        container_id = container.get("Id")
        labels = (container.get("Config", {}).get("Labels", {}) or {})
        project = labels.get("com.docker.compose.project")
        if (
            not isinstance(container_id, str)
            or not any(container_id.startswith(value) for value in container_ids)
            or not isinstance(project, str)
            or _PIER_RUNTIME_PROJECT_RE.fullmatch(project) is None
        ):
            continue
        mounts_owned = any(
            isinstance(mount, dict)
            and mount.get("Type") == "bind"
            and _path_is_below(str(mount.get("Source", "")), job_root)
            for mount in container.get("Mounts", [])
        )
        config_owned = any(
            _path_is_below(value.strip(), job_root)
            for value in str(
                labels.get("com.docker.compose.project.config_files", ""),
            ).split(",")
            if value.strip()
        )
        if mounts_owned or config_owned:
            owned.append(container_id)
            state = container.get("State")
            if isinstance(state, dict) and state.get("Running") is True:
                running.append(container_id)
    if not owned:
        return PierContainerCleanup()
    try:
        removed = subprocess.run(
            ["docker", "rm", "-f", *owned],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(
            "terminated Pier task container could not be queried safely",
        ) from exc
    if removed.returncode != 0:
        raise RunnerError(
            "terminated Pier task container could not be removed",
        )
    return PierContainerCleanup(matched=len(owned), running=len(running))


def _cleanup_exited_pier_runtime(
    proc: subprocess.Popen,
    job_root: Path,
) -> tuple[bool, PierContainerCleanup]:
    """Reap and prove ownership of runtime residue after Pier exits.

    The process-group audit catches host-side helpers such as ``docker exec``;
    the container audit independently catches an agent or egress proxy that
    outlived that helper.  If either audit is unavailable, fail closed before
    the server can reopen the assignment and start a duplicate paid run.
    """

    cleanup_errors: list[str] = []
    process_residue = False
    try:
        process_residue = _cleanup_exited_pier_process_group(proc)
    except RunnerError as exc:
        cleanup_errors.append(str(exc))
    try:
        cleanup = _cleanup_terminated_pier_containers(job_root)
    except RunnerError as exc:
        cleanup_errors.append(str(exc))
        cleanup = PierContainerCleanup()
    if cleanup_errors:
        raise RunnerCleanupUnconfirmedError(
            "Pier exited, but exact-job runtime cleanup could not be "
            "confirmed; the server lease was intentionally left running "
            "to prevent a duplicate retry ("
            + "; ".join(cleanup_errors)
            + ")",
        )
    return process_residue, cleanup


def run_trial(
    assignment: dict,
    tasks_root: Path,
    work_dir: Path,
    dev_agent: str | None = None,
    on_started: Callable[[], None] | None = None,
    environment_build_timeout_multiplier: float | None = None,
    build_cache_mode: str = image_cache.DEFAULT_BUILD_CACHE_MODE,
    allow_isolated_builder: bool = True,
) -> TrialArtifacts:
    effective_assignment = assignment
    codex_cli_version = None
    kimi_cli_version = None
    antigravity_cli_version = None
    zcode_cli_version = None
    zcode_cli_sha256 = None
    dsh_version = None
    codebuddy_cli_version = None
    codex_provider = None
    effective_agent = dev_agent or assignment["agent"]
    environment_build_timeout_multiplier = resolve_environment_build_timeout_multiplier(
        environment_build_timeout_multiplier,
    )
    build_cache_mode = image_cache.normalize_build_cache_mode(build_cache_mode)
    if effective_agent == "codex":
        codex_provider = (
            assignment_codex_provider(assignment) or DEFAULT_CODEX_PROVIDER
        )
        if codex_provider == DEEPSEEK_PROVIDER:
            # Validate the requested cell before network access. The server's
            # version is only a fallback hint, so validate the version after
            # resolving npm stable rather than rejecting an otherwise usable
            # stale server hint here.
            _validate_deepseek_assignment(assignment, validate_version=False)
            codex_cli_version = resolve_latest_codex_cli_version(
                assignment.get("agent_version"),
                bool(assignment.get("agent_version_verified")),
            )
            codex_cli_version = _deepseek_codex_version({
                **assignment,
                "agent_version": codex_cli_version,
            })
            print(
                "verified latest stable DeepSeek Codex CLI: "
                f"{codex_cli_version}"
            )
        else:
            # Resolve before creating the job, extending the lease, or starting
            # Pier. A registry outage therefore consumes no model quota and leaves
            # the assignment safely retryable.
            codex_cli_version = resolve_latest_codex_cli_version(
                assignment.get("agent_version"),
                bool(assignment.get("agent_version_verified")),
            )
            print(f"verified latest stable Codex CLI: {codex_cli_version}")
        effective_assignment = {
            **assignment,
            "agent_version": codex_cli_version,
        }
    elif effective_agent == CLAUDE_AGENT:
        _validate_claude_assignment(assignment)
        effective_assignment = {
            **assignment,
            "agent_version": CLAUDE_CLI_VERSION,
        }
        print(f"verified pinned Claude Code subscription CLI: {CLAUDE_CLI_VERSION}")
    elif effective_agent == GROK_AGENT:
        _validate_grok_assignment(assignment)
        effective_assignment = {
            **assignment,
            "agent_version": GROK_CLI_VERSION,
        }
        print(f"verified pinned Grok subscription CLI: {GROK_CLI_VERSION}")
    elif effective_agent == KIMI_AGENT:
        _validate_kimi_assignment(assignment)
        kimi_cli_version = KIMI_CLI_VERSION
        effective_assignment = {
            **assignment,
            "agent_version": kimi_cli_version,
        }
        print(f"verified pinned Kimi subscription CLI: {kimi_cli_version}")
    elif effective_agent == ANTIGRAVITY_AGENT:
        _validate_antigravity_assignment(assignment)
        antigravity_cli_version = ANTIGRAVITY_CLI_VERSION
        effective_assignment = {
            **assignment,
            "agent_version": antigravity_cli_version,
        }
        print(
            "verified pinned Antigravity subscription CLI: "
            f"{antigravity_cli_version}"
        )
    elif effective_agent == ZCODE_AGENT:
        _validate_zcode_assignment(assignment)
        # ZCode's desktop release and embedded protocol CLI use independent
        # version numbers. Resolve the actual compatible protocol runtime only
        # after the provider slot is opened below; never trust the assignment
        # hint as the observed version.
        effective_assignment = dict(assignment)
    elif effective_agent == DSH_AGENT:
        _validate_dsh_assignment(assignment)
        dsh_version = DSH_VERSION
        effective_assignment = {
            **assignment,
            "agent_version": dsh_version,
            "_artifact_run_id": uuid.uuid4().hex,
        }
        print(f"verified pinned DSH Minimal: {dsh_version}")
    elif effective_agent == CODEBUDDY_AGENT:
        _validate_codebuddy_assignment(assignment)
        image_issue = codebuddy_runtime_image_error()
        if image_issue is not None:
            raise RunnerError(
                image_issue + "; run `dradar provider setup codebuddy` first"
            )
        codebuddy_cli_version = CODEBUDDY_CLI_VERSION
        effective_assignment = {
            **assignment,
            "agent_version": codebuddy_cli_version,
        }
        print(f"verified pinned CodeBuddy CLI: {codebuddy_cli_version}")

    work_dir.mkdir(parents=True, exist_ok=True)
    provider_auth_path = None
    provider_cli_path = None
    provider_stack = ExitStack()
    # Resolve subscription credentials before touching Docker.  A rejected
    # OAuth grant is an account problem and must not consume a full image
    # build window before it becomes visible to the worker.
    if effective_agent == GROK_AGENT:
        provider_cli_path = _validated_grok_cli_path()
        _preflight_subscription_before_build(
            effective_agent, grok_cli=provider_cli_path,
        )
        print("Grok provider preflight passed before Docker build")
    elif effective_agent == KIMI_AGENT:
        provider_cli_path = _validated_kimi_cli_path()
        _preflight_subscription_before_build(
            effective_agent, kimi_cli=provider_cli_path,
        )
        print("Kimi provider preflight passed before Docker build")
    elif effective_agent == ANTIGRAVITY_AGENT:
        issue = prepare_antigravity_auth()
        if issue is not None:
            raise RunnerError(
                "antigravity provider preflight failed before Docker build: "
                + issue
            )
        print("Antigravity provider preflight passed before Docker build")
    try:
        egress_environment = egress.prepare_egress_proxy_runtime(announce=True)
    except egress.EgressProxyError as exc:
        raise RunnerError(
            f"Pier egress environment is not ready: {exc}; no model quota was used"
        ) from exc
    if egress_environment or effective_agent == CODEBUDDY_AGENT:
        _ensure_pier_sitecustomize(work_dir)
    builder_lease = image_cache.prepare_trial_builder(
        work_dir.parent,
        assignment_id=str(assignment["assignment_id"]),
        runtime=egress_environment,
        mode=build_cache_mode,
        force_default=not allow_isolated_builder,
    )
    if builder_lease.isolated:
        if builder_lease.reusable:
            print("已为本题准备共享的 DRadar BuildKit 缓存")
        else:
            print("已为本题准备独立的临时运行环境")
    elif not builder_lease.expected:
        print(f"本题改用本机默认构建空间（{builder_lease.note}）")
    else:
        print(
            "提示：本题可以继续运行，但临时环境暂时无法安全隔离；"
            f"完成后不会继续领取下一题（{builder_lease.note}）"
        )
    jobs_dir = work_dir / "jobs"
    job_name = f"a{assignment['assignment_id']}"
    # A fresh lease re-run must not collide with a stale job dir.
    if (jobs_dir / job_name).exists():
        job_name = f"{job_name}-{int(time.time())}"

    log_path = work_dir / f"{job_name}.log"
    # Cap the run so a wedged docker/agent can't hang the CLI forever.
    # A mid-task rate-limit death just ends the run (no sleep-and-resume) --
    # it surfaces as a nonzero pier rc, which _run_and_submit reports as
    # `interrupted` -> the server marks it invalid and the cell reopens.
    timeout_sec = _effective_run_timeout_sec(
        effective_assignment,
        tasks_root / str(effective_assignment["task_id"]),
        environment_build_timeout_multiplier,
    )
    terminal_error: RunnerError | None = None
    live_error_offsets: dict[Path, int] = {}
    live_error_counts: dict[str, int] = {}
    watch_live_account_errors = (
        (dev_agent or effective_assignment["agent"]) in (
            "codex", CLAUDE_AGENT, DSH_AGENT, GROK_AGENT, KIMI_AGENT,
            ANTIGRAVITY_AGENT, ZCODE_AGENT, CODEBUDDY_AGENT,
        )
    )

    try:
        if codex_provider == DEEPSEEK_PROVIDER:
            try:
                provider_auth_path = create_deepseek_auth_json(work_dir)
            except (OSError, ValueError) as exc:
                raise RunnerError(str(exc)) from exc
        elif effective_agent == CLAUDE_AGENT:
            try:
                provider_auth_path = provider_stack.enter_context(
                    claude_subscription_session(work_dir)
                )
            except (OSError, ValueError) as exc:
                raise RunnerError(str(exc)) from exc
        elif effective_agent == DSH_AGENT:
            try:
                provider_auth_path = create_deepseek_api_key_file(work_dir)
            except (OSError, ValueError) as exc:
                raise RunnerError(str(exc)) from exc
        elif effective_agent == GROK_AGENT:
            try:
                provider_cli_path = provider_cli_path or _validated_grok_cli_path()
                provider_auth_path = provider_stack.enter_context(
                    grok_subscription_session(work_dir)
                )
            except (OSError, ValueError) as exc:
                raise RunnerError(str(exc)) from exc
        elif effective_agent == KIMI_AGENT:
            try:
                provider_cli_path = provider_cli_path or _validated_kimi_cli_path()
                provider_auth_path = provider_stack.enter_context(
                    kimi_subscription_session(work_dir)
                )
            except (OSError, ValueError) as exc:
                raise RunnerError(str(exc)) from exc
        elif effective_agent == ANTIGRAVITY_AGENT:
            try:
                provider_auth_path = provider_stack.enter_context(
                    antigravity_subscription_session(work_dir)
                )
            except (OSError, ValueError) as exc:
                raise RunnerError(str(exc)) from exc
        elif effective_agent == ZCODE_AGENT:
            try:
                provider_cli_path, zcode_cli_version = _validated_zcode_cli_path(
                    model=effective_assignment["model"],
                )
                effective_assignment = {
                    **effective_assignment,
                    "agent_version": zcode_cli_version,
                    "_zcode_cli_version_observed": True,
                }
                print(f"verified ZCode CLI version: {zcode_cli_version}")
                zcode_cli_sha256 = hashlib.sha256(
                    provider_cli_path.read_bytes()
                ).hexdigest()
                provider_auth_path = create_zcode_api_key_file(work_dir)
            except (OSError, ValueError) as exc:
                raise RunnerError(str(exc)) from exc
        elif effective_agent == CODEBUDDY_AGENT:
            try:
                provider_auth_path = provider_stack.enter_context(
                    codebuddy_subscription_session(work_dir)
                )
            except (OSError, ValueError) as exc:
                raise RunnerError(str(exc)) from exc
        provider_kwargs = (
            {
                "provider_auth_path": provider_auth_path,
                **(
                    {"provider_cli_path": provider_cli_path}
                    if effective_agent in (GROK_AGENT, KIMI_AGENT, ZCODE_AGENT) else {}
                ),
            }
            if (
                codex_provider == DEEPSEEK_PROVIDER
                or effective_agent in (
                    CLAUDE_AGENT, GROK_AGENT, KIMI_AGENT, ANTIGRAVITY_AGENT,
                    ZCODE_AGENT, DSH_AGENT, CODEBUDDY_AGENT,
                )
            )
            else {}
        )
        pier_tasks_root = tasks_root
        if effective_agent == DSH_AGENT:
            pier_tasks_root = provider_stack.enter_context(
                _dsh_tasks_overlay(
                    effective_assignment,
                    tasks_root,
                    work_dir,
                    job_name,
                )
            )
        elif effective_agent == ANTIGRAVITY_AGENT:
            pier_tasks_root = provider_stack.enter_context(
                _antigravity_tasks_overlay(
                    effective_assignment,
                    tasks_root,
                    work_dir,
                    job_name,
                )
            )
        elif (
            tasks_root
            / str(effective_assignment.get("task_id", ""))
            / "task.toml"
        ).is_file():
            pier_tasks_root = provider_stack.enter_context(
                _artifact_tasks_overlay(
                    effective_assignment,
                    tasks_root,
                    work_dir,
                    job_name,
                )
            )
        build_options = dict(provider_kwargs)
        # Keep the default path compatible with small embedders/test doubles
        # that still implement the historical positional builder signature;
        # build_pier_command itself carries the production default. Explicit
        # non-default policies are forwarded so operators can tune them.
        if not math.isclose(
            environment_build_timeout_multiplier,
            DEFAULT_ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER,
        ):
            build_options[
                "environment_build_timeout_multiplier"
            ] = environment_build_timeout_multiplier
        cmd = build_pier_command(
            effective_assignment, pier_tasks_root, jobs_dir, job_name, work_dir,
            dev_agent, **build_options,
        )
        env = _pier_process_env(
            effective_assignment,
            pier_bootstrap_dir=(work_dir if egress_environment else None),
            egress_environment=egress_environment,
            codex_module_dir=None,
            claude_module_dir=(
                work_dir if effective_agent == CLAUDE_AGENT else None
            ),
            deepseek_module_dir=(
                work_dir if codex_provider == DEEPSEEK_PROVIDER else None
            ),
            grok_module_dir=(work_dir if effective_agent == GROK_AGENT else None),
            kimi_module_dir=(work_dir if effective_agent == KIMI_AGENT else None),
            antigravity_module_dir=(
                work_dir if effective_agent == ANTIGRAVITY_AGENT else None
            ),
            zcode_module_dir=(work_dir if effective_agent == ZCODE_AGENT else None),
            dsh_module_dir=(work_dir if effective_agent == DSH_AGENT else None),
            codebuddy_module_dir=(
                work_dir if effective_agent == CODEBUDDY_AGENT else None
            ),
        )
        if builder_lease.name is not None:
            # Select the assignment builder only for Pier. Never mutate the
            # user's global/default buildx selection.
            env["BUILDX_BUILDER"] = builder_lease.name
        if on_started is not None:
            # This is now the authoritative ownership bind. Never start a paid
            # model when the server has not fenced this exact session/epoch.
            on_started()
        started = time.time()
        with log_path.open("w") as log:
            log.write("cmd=" + " ".join(cmd) + "\n")
            log.write(
                "build_cache_mode="
                + ("shared" if builder_lease.reusable else "isolated")
                + "\n"
            )
            log.write(
                "build_cache_builder="
                + (builder_lease.name or "default-fallback")
                + "\n"
            )
            log.write(
                "environment_build_timeout_multiplier="
                + f"{environment_build_timeout_multiplier:g}\n"
            )
            log.flush()
            # Heartbeat loop instead of a blocking run: image build + a long
            # agent turn can be silent for many minutes, and volunteers couldn't
            # tell "working" from "wedged" without docker-exec'ing into the
            # container (volunteer report, 2026-07-13). Once a minute, print
            # elapsed time plus the newest pier log line.
            proc = subprocess.Popen(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=work_dir,
                env=env,
                start_new_session=(os.name != "nt"),
            )
            try:
                next_beat = started + HEARTBEAT_SEC
                while True:
                    try:
                        proc.wait(timeout=min(30, HEARTBEAT_SEC))
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    now = time.time()
                    if watch_live_account_errors:
                        live_failure = _scan_live_account_errors(
                            jobs_dir, job_name,
                            live_error_offsets, live_error_counts,
                        )
                        if live_failure is not None:
                            raise LiveAccountTerminalError(
                                _live_account_error_message(live_failure)
                            )
                    if now - started > timeout_sec:
                        log.flush()
                        raise RunnerError(
                            f"trial exceeded {timeout_sec // 60} min and was aborted "
                            f"(see {log_path}); docker/agent likely wedged\n"
                            f"last lines of the log:\n{_tail(log_path)}",
                            failure_diagnostic=_zcode_failure_diagnostic(
                                effective_assignment,
                                tasks_root / assignment["task_id"],
                                jobs_dir,
                                job_name,
                                "trial_timeout",
                            ),
                        )
                    if now >= next_beat:
                        next_beat = now + HEARTBEAT_SEC
                        print(f"  … {int((now - started) / 60)} min elapsed — "
                              f"{_last_activity(log_path)}")
            except BaseException as exc:
                # Same contract subprocess.run had: no exception (timeout, Ctrl-C,
                # anything) leaves a pier process running detached. TERM first
                # with a grace window: a SIGKILLed pier can never `docker compose
                # down`, and its orphaned task container keeps the agent alive —
                # burning quota with nobody left to harvest the result.
                cleanup_errors: list[str] = []
                try:
                    _terminate_pier_process_tree(proc)
                except RunnerError as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
                try:
                    # A Pier parent can exit cleanly on TERM before compose
                    # teardown completes. Always inspect and remove only a
                    # container positively bound to this exact failed job.
                    _cleanup_terminated_pier_containers(jobs_dir / job_name)
                except RunnerError as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
                if cleanup_errors:
                    raise RunnerCleanupUnconfirmedError(
                        f"{exc}\nPier cleanup safety check failed: "
                        + "; ".join(cleanup_errors),
                    ) from exc
                if isinstance(exc, RunnerError):
                    # A watchdog timeout is terminal for this process, but Pier
                    # may already have harvested a patch, trajectory and token
                    # totals while handling TERM. Keep walking the artifact path
                    # so that work can be uploaded as ``interrupted`` and real
                    # API spend is recorded. Missing artifacts still re-raise
                    # this original, actionable timeout below.
                    terminal_error = exc
                else:
                    raise
        if effective_agent in (ANTIGRAVITY_AGENT, ZCODE_AGENT):
            process_residue, cleanup = _cleanup_exited_pier_runtime(
                proc, jobs_dir / job_name,
            )
            if process_residue or cleanup.running:
                if effective_agent == ZCODE_AGENT:
                    raise RunnerTaskRetryableError(
                        "Pier exited while the ZCode runtime was still active; exact-job "
                        "processes and containers were stopped before the "
                        "assignment was made retryable",
                        failure_diagnostic=_zcode_failure_diagnostic(
                            effective_assignment,
                            tasks_root / assignment["task_id"],
                            jobs_dir,
                            job_name,
                            "agent_no_artifact",
                        ),
                    )
                print(
                    "  warning: Pier exited while the Antigravity runtime was still "
                    "active; stopped exact-job residue and preserved harvested "
                    "artifacts"
                )
    finally:
        if provider_auth_path is not None:
            if (
                codex_provider == DEEPSEEK_PROVIDER
                or effective_agent in (DSH_AGENT, ZCODE_AGENT)
            ):
                try:
                    provider_auth_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise RunnerError(
                        f"could not remove temporary provider credential file "
                        f"{provider_auth_path}: {exc}"
                    ) from exc
        try:
            provider_stack.close()
        except (OSError, ValueError) as exc:
            raise RunnerError(str(exc)) from exc
    duration = time.time() - started
    if isinstance(terminal_error, LiveAccountTerminalError):
        # Let the runloop classify this normalized failure and open the
        # supervised pool's graceful drain.
        # Do not upload a partial patch from a terminal account failure.
        raise terminal_error

    tail = _tail(log_path)
    try:
        job_dir, trial_dir = locate_artifacts(jobs_dir, job_name)
    except RunnerError:
        if terminal_error is not None:
            if terminal_error.failure_diagnostic is not None:
                terminal_error.failure_diagnostic.update(
                    _zcode_runtime_diagnostic(jobs_dir, job_name)
                )
            raise terminal_error
        if _looks_like_disk_full(tail):
            raise BuildDiskFullError(
                "the task environment failed to BUILD because the disk is full "
                "— the agent never started and no quota was used.\n"
                f"last lines of the log:\n{tail}")
        if _looks_like_snapshotter_permission(tail):
            raise BuildSnapshotterPermissionError(
                "the task environment failed to BUILD because overlay whiteouts "
                "are not permitted here — the agent never started and no quota "
                "was used.\n"
                f"last lines of the log:\n{tail}")
        if _looks_like_build_flake(tail):
            raise BuildFlakeError(
                f"the task environment failed to BUILD (mirror/network flake) — "
                f"the agent never started and no quota was used.\n"
                f"last lines of the log:\n{tail}")
        raise
    patch, trajectory, result = trial_artifact_paths(trial_dir)
    if effective_agent == ZCODE_AGENT:
        quota_facts = _zcode_quota_limit_facts(
            trial_dir / "agent" / "zcode-outcome.json",
        )
        if quota_facts is not None:
            diagnostic = _zcode_failure_diagnostic(
                effective_assignment,
                tasks_root / assignment["task_id"],
                jobs_dir,
                job_name,
                "provider_quota_exhausted",
            ) or {}
            diagnostic.update(quota_facts)
            raise RunnerError(
                "ZCode structured provider outcome confirmed account quota "
                "exhausted (Coding Plan usage window; reset required)",
                failure_diagnostic=diagnostic,
            )
    dsh_artifact_binding = None
    if effective_agent == DSH_AGENT:
        dsh_artifact_binding = _verify_dsh_artifact_binding(
            trial_dir, effective_assignment,
        )
    if not patch.is_file():
        if terminal_error is not None:
            raise terminal_error
    if (
        effective_agent == DSH_AGENT
        and patch.is_file()
        and _normalize_utf16_patch(patch)
    ):
        print("  normalized BOM-marked UTF-16 model.patch to validated UTF-8")
    if not patch.is_file():
        # No patch at all means the agent never produced anything — usually
        # the environment died under it. Say which, instead of blaming the
        # agent for a mirror hiccup.
        result_exception = _result_exception_text(result)
        diagnostic = "\n".join(x for x in (tail, result_exception) if x)
        if _looks_like_disk_full(diagnostic):
            raise BuildDiskFullError(
                "the task environment failed to BUILD because the disk is full "
                "— the agent never started and no quota was used.\n"
                f"build diagnostic:\n{_diagnostic_tail(diagnostic)}")
        if _looks_like_snapshotter_permission(diagnostic):
            raise BuildSnapshotterPermissionError(
                "the task environment failed to BUILD because overlay whiteouts "
                "are not permitted here — the agent never started and no quota "
                "was used.\n"
                f"build diagnostic:\n{_diagnostic_tail(diagnostic)}")
        if _looks_like_build_flake(diagnostic):
            raise BuildFlakeError(
                f"the task environment failed to BUILD (mirror/network flake) — "
                f"the agent never started and no quota was used.\n"
                f"build diagnostic:\n{_diagnostic_tail(diagnostic)}")
        raise RunnerError(
            f"model.patch missing (agent likely failed; see {log_path} and {trial_dir})\n"
            f"last lines of the log:\n{tail}",
            failure_diagnostic=_zcode_failure_diagnostic(
                effective_assignment,
                tasks_root / assignment["task_id"],
                jobs_dir,
                job_name,
                "agent_no_artifact",
            ),
        )
    if effective_agent == ZCODE_AGENT and proc.returncode == 0:
        runtime_diagnostic = _zcode_runtime_diagnostic(jobs_dir, job_name)
        false_success_reason = _zcode_false_success_reason(
            result,
            trial_dir / "agent" / "provider-usage.json",
            runtime_diagnostic,
            expected_model=effective_assignment["model"],
        )
        if false_success_reason is not None:
            raise RunnerTaskRetryableError(
                "Pier exited with process status 0 without a gradeable ZCode terminal "
                f"result ({false_success_reason}); local artifacts were kept "
                "and the assignment remains retryable",
                failure_diagnostic=_zcode_failure_diagnostic(
                    effective_assignment,
                    tasks_root / assignment["task_id"],
                    jobs_dir,
                    job_name,
                    "agent_no_artifact",
                ),
            )
    returncode = proc.returncode
    if (
        effective_agent == DSH_AGENT
        and returncode == 0
        and _result_exception_text(result)
    ):
        # Public Pier can exit successfully after harvesting a trial whose
        # installed agent failed.  Keep the structured result for diagnosis,
        # but do not expose that wrapper status as a successful DSH run.
        returncode = 1
    if terminal_error is not None and returncode in (None, 0):
        # A TERM-aware Pier may exit cleanly after a DRadar watchdog fired.
        # Preserve the semantic failure so runloop uploads it as interrupted
        # instead of accidentally sending a partial patch for grading.
        returncode = TRIAL_TIMEOUT_RETURNCODE
    return TrialArtifacts(
        job_dir=job_dir,
        trial_dir=trial_dir,
        patch=patch,
        trajectory=trajectory,
        result=result,
        returncode=returncode,
        duration_sec=duration,
        log_path=log_path,
        codex_cli_version=codex_cli_version,
        grok_cli_version=(
            GROK_CLI_VERSION if effective_agent == GROK_AGENT else None
        ),
        kimi_cli_version=kimi_cli_version,
        antigravity_cli_version=antigravity_cli_version,
        zcode_cli_version=zcode_cli_version,
        zcode_cli_sha256=zcode_cli_sha256,
        dsh_version=dsh_version,
        codebuddy_cli_version=codebuddy_cli_version,
        dsh_artifact_binding=dsh_artifact_binding,
        builder_isolated=builder_lease.isolated,
        builder_note=builder_lease.note,
        builder_reusable=builder_lease.reusable,
        builder_name=builder_lease.name,
        builder_expected=builder_lease.expected,
    )


def summarize_result(result_path: Path | None) -> dict:
    """Extract token/cost stats from a trial result.json for client_meta."""
    if not result_path or not result_path.is_file():
        return {}
    try:
        data = json.loads(result_path.read_text())
    except json.JSONDecodeError:
        return {}
    agent = data.get("agent_result") or {}
    exc = data.get("exception_info") or {}
    return {
        "cost_usd": agent.get("cost_usd"),
        "n_input_tokens": agent.get("n_input_tokens"),
        "n_cache_tokens": agent.get("n_cache_tokens"),
        "n_output_tokens": agent.get("n_output_tokens"),
        "n_agent_steps": agent.get("n_agent_steps"),
        "exception_info": bool(exc),
        # the WHY, not just the whether: rides client_meta to the server so
        # `dradar status` / operators can tell rate-limit from stale-agent
        # from auth failure without opening the uploaded result.json
        "exception_type": exc.get("exception_type"),
    }


def classify_exception_message(message: str) -> str | None:
    """Classify an agent failure by the recovery action it permits.

    Account/runtime terminal failures must stop a supervised pool, while a
    plain burst-rate 429 may be retried later with bounded backoff. Keep the
    explicit quota signals ahead of the generic 429 fallback: Codex commonly
    includes both in the same error payload.
    """
    low = message.lower()

    def has_http_status(status: int) -> bool:
        code = str(status)
        return bool(re.search(
            rf"(?:\bhttp(?:\s+status)?\s*|[\"']?(?:status|status_code|code)"
            rf"[\"']?\s*[:=]\s*){code}\b|"
            rf"\b{code}\s+(?:unauthorized|forbidden|payment required|"
            rf"too many requests|rate limit)",
            low,
        ))

    if "requires a newer version of codex" in low:
        return "stale-agent"
    if is_insufficient_balance_message(message):
        return "insufficient-balance"
    if any(s in low for s in (
        "usage_limit", "usage limit", "quota exhausted", "quota_exhausted",
        "quota exceeded", "weekly limit", "weekly quota",
        "usage limit reached", "quota limit reached",
        "individual quota reached. please upgrade your subscription to "
        "increase your limits. resets in",
        "you've hit your usage limit", "you have hit your usage limit",
    )):
        return "quota-limit"
    # Explicit account-auth signals remain terminal even when Codex reports
    # them while attempting its optional WebSocket transport.  They must take
    # precedence over the recoverable WebSocket-403 exception below.
    if any(s in low for s in (
        "unauthorized", "authentication failed", "invalid authentication",
        "invalid api key", "invalid credentials", "token expired",
        "account suspended", "account disabled", "account deactivated",
        "invalid_grant", "provided authorization grant is invalid",
        "oauth refresh was rejected",
    )):
        return "auth"
    # Codex can receive a 403 while negotiating its optional WebSocket
    # transport, then recover by falling back to HTTPS.  Treating that
    # transport response as an account-wide auth failure makes the live
    # watchdog kill Pier before Codex gets a chance to perform the fallback.
    # Keep this exception deliberately narrow; every non-WebSocket HTTP 403
    # retains the fail-safe account-terminal behavior below.
    if (
        (
            any(marker in low for marker in (
                "responses_websocket",
                "wss://chatgpt.com/backend-api/codex/responses",
            ))
            or (
                "reconnecting" in low
                and "unexpected status 403 forbidden" in low
            )
        )
        and has_http_status(403)
    ):
        return "provider-transport"
    if has_http_status(401) or has_http_status(403):
        return "auth"
    if has_http_status(429) or any(s in low for s in (
        "rate limit", "rate_limit", "too many requests",
    )):
        return "rate-limit"
    if "at capacity" in low:
        return "model-capacity"
    if (
        "cli-chat-proxy.grok.com/v1/responses" in low
        and any(marker in low for marker in (
            "reqwest error stream", "error sending request for url",
            "stream disconnected before completion", "connection reset by peer",
        ))
    ) or any(marker in low for marker in (
        "provider.connection_error: connection error",
        "dsh: transport:",
    )):
        return "provider-transport"
    if "agenttimeouterror" in low:
        return "agent-deadline"
    if re.search(r"^command failed \(exit 75\):", low):
        return "provider-temporary"
    return None


_AGENT_COMMAND_EXIT_RE = re.compile(
    r"\bcommand failed \(exit\s+(-?\d{1,6})\):",
    flags=re.IGNORECASE,
)


def _agent_command_exit_code(
    exception_type: object,
    message: object,
) -> int | None:
    """Derive one bounded integer without retaining the full exception text."""
    if exception_type != "NonZeroAgentExitCodeError" or not isinstance(message, str):
        return None
    match = _AGENT_COMMAND_EXIT_RE.search(message)
    if match is None:
        return None
    exit_code = int(match.group(1))
    return exit_code if exit_code != 0 else None


def diagnose_exception(result_path: Path | None) -> dict:
    """Classify a trial's recorded exception for honest console reporting:
    {} when there is none, else {type, tail, kind, optional exit_code} where
    kind is one of
    stale-agent | insufficient-balance | quota-limit | rate-limit | auth |
    model-capacity | provider-transport | provider-temporary |
    agent-deadline | None
    (unrecognized). The message tail
    matters most: pier's exception_message embeds the agent's actual output,
    which for codex includes the API error JSON naming the real cause."""
    if not result_path or not result_path.is_file():
        return {}
    try:
        data = json.loads(result_path.read_text())
    except json.JSONDecodeError:
        return {}
    info = data.get("exception_info") or {}
    if not info:
        return {}
    msg = info.get("exception_message") or ""
    kind = classify_exception_message(msg)
    if kind is None and info.get("exception_type") == "AgentTimeoutError":
        kind = "agent-deadline"
    exception_type = info.get("exception_type")
    tail = [ln.strip() for ln in msg.splitlines() if ln.strip()][-6:]
    diagnostic = {"type": exception_type, "kind": kind, "tail": tail}
    # The command header can precede many lines of tool output and therefore
    # disappear from the bounded console tail.  Preserve only its numeric exit
    # code so the repeated-zero-progress circuit sees the same authoritative
    # signal as the server without retaining or propagating sensitive output.
    exit_code = _agent_command_exit_code(exception_type, msg)
    if exit_code is not None:
        diagnostic["exit_code"] = exit_code
    return diagnostic


def is_insufficient_balance_message(message: str) -> bool:
    """Recognize only explicit paid-API account exhaustion signals."""
    low = message.lower()
    return any(s in low for s in (
        "insufficient balance",
        "insufficient_balance",
        "balance is insufficient",
        "余额不足",
    )) or ("402" in low and "payment required" in low)


# Targeted advice per diagnose_exception kind. Only the rate-limit case may
# mention quota — an unrecognized failure gets the artifact paths, not a
# guess (a volunteer bug report proved "wait for your quota to reset" was
# actively misleading for a version error).
DIAG_ADVICE = {
    "stale-agent": (
        "the codex CLI baked into your pier container image is too old for "
        "this model. Update dradar (add --refresh to your uvx command) and "
        "re-run: the server now pins the agent version, which rebuilds the "
        "image automatically. If this repeats on the latest dradar, tell the "
        "radar operators — the server-side pin may need a bump."),
    "rate-limit": (
        "the provider is rate-limiting requests. The current task stops after "
        "its bounded retry budget; run it again after the provider recovers."),
    "quota-limit": (
        "the account quota window is exhausted. This worker stops and the pool "
        "will not start new work, while already-running siblings are allowed to "
        "finish. After the quota resets, start it again; any completed result "
        "waiting to upload remains protected by the local pending ledger."),
    "insufficient-balance": (
        "the paid API account has insufficient balance. This worker stops and "
        "the pool will not start another task, while already-running siblings "
        "are allowed to finish; recharge it, then run `dradar resume`."),
    "auth": (
        "the agent could not authenticate inside the container. Run the matching "
        "live check before claiming again: `dradar provider status deepseek "
        "--live`, `dradar provider status grok`, `dradar provider status kimi "
        "--live`, `dradar provider status zcode --live`, or `dradar provider "
        "status codebuddy --live`; for the original "
        "OpenAI path run `codex login`, then re-check `dradar doctor`."),
    "model-capacity": (
        "the model stayed at capacity after Pier retried the original Codex "
        "session with bounded backoff. This is not a problem with your setup "
        "or work; the automatic recovery was attempted but could not finish "
        "within its retry budget. Claim the cell again later."),
    "provider-transport": (
        "the provider response stream failed after bounded same-session "
        "recovery. Existing work and diagnostics were preserved; this run "
        "is not graded and the cell reopens for a fresh attempt."),
    "agent-deadline": (
        "the agent reached its benchmark hard deadline. The best persisted "
        "work and diagnostics were preserved, but this run is not graded."),
    "provider-temporary": (
        "the provider declared a temporary network or service failure. DRadar "
        "already exhausted its bounded same-session recovery budget, so the "
        "workspace and failure artifacts were preserved for diagnosis; retry "
        "the cell later rather than changing credentials or reinstalling."),
}
