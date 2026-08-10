"""Run one benchmark trial locally via pier and collect submission artifacts.

The volunteer client runs agent-only (`--disable-verification`); grading is
server-side. model.patch is produced inside the container by the task's own
pre_artifacts.sh, then downloaded by pier into the trial dir.
"""

import glob
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from .manifest import task_content_hash
from .providers import (
    DEFAULT_CODEX_PROVIDER,
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_CATALOG_REMOTE_PATH,
    DEEPSEEK_CODEX_VERSION,
    DEEPSEEK_MIN_CODEX_VERSION,
    DEEPSEEK_MODEL,
    DEEPSEEK_OPENCODE_API_KEY_ENV,
    DEEPSEEK_OPENCODE_BASE_URL,
    DEEPSEEK_OPENCODE_PROVIDER,
    DEEPSEEK_PROVIDER,
    DEEPSEEK_SUPPORTED_EFFORTS,
    assignment_codex_provider,
    create_provider_auth_json,
    deepseek_catalog_error,
    deepseek_catalog_path,
    is_deepseek_family,
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

# Public-safe DeepSeek configuration for an isolated stock Pier Codex agent.
# The official catalog is uploaded to this container-local path before Codex
# starts; do not add manual context/compaction/reasoning-summary overrides here,
# because DeepSeek's official setup script explicitly removes them when the
# catalog is active. Codex reads the provider credential from an uploaded
# auth.json because ``requires_openai_auth`` is true; no API-key value is
# passed through argv or ``docker compose exec -e``. Command construction
# snapshots the official base URL and uses the same value for this TOML and
# the standalone Pier adapter.
def deepseek_toml(base_url: str) -> str:
    return (
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
        f'base_url = "{base_url}"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = true\n'
    )
DEEPSEEK_AGENT_IMPORT_PATH = "_dradar_pier_deepseek:DeepSeekCodex"
DEEPSEEK_AGENT_MODULE_FILENAME = "_dradar_pier_deepseek.py"

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
POMPEII_AGENT_TIMEOUT_SEC = 90 * 60
POMPEII_SUBMISSION_PROMPT = CODEX_SUBMISSION_PROMPT + """

Time budget: aim to complete and commit a valid answer within 60 minutes.
After 60 minutes, this run may stop at any time; agent execution will stop no later than 90 minutes.
Keep the best current answer persisted in the repository. As
the deadline approaches, do not start time-consuming new experiments; use your
best current judgment to finish and commit. A complete, gradeable answer takes
priority over further exploration.
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


class RunnerError(RuntimeError):
    pass


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


# Signatures (in the pier log tail) of an image build / infra failure that
# happened before any agent ran. Deliberately specific: a false positive here
# would auto-retry a run that DID burn quota.
_BUILD_FLAKE_MARKERS = (
    "ports.ubuntu.com", "archive.ubuntu.com", "failed to solve",
    "apt-get update", "Temporary failure resolving", "proxyconnect",
    "TLS handshake timeout", "error getting credentials",
)


def _looks_like_build_flake(log_tail: str) -> bool:
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


def claude_oauth_token() -> str | None:
    """The Claude Code readiness signal — same sharing rationale as
    codex_auth_path()."""
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")


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
        path = home / "codex-submission-prompt-pompeii-v1.j2"
        prompt = POMPEII_SUBMISSION_PROMPT
    else:
        path = home / "codex-submission-prompt.j2"
        prompt = CODEX_SUBMISSION_PROMPT
    return _materialize_shared_file(path, prompt.encode())


def _ensure_deepseek_config(home: Path, base_url: str) -> Path:
    path = home / "codex-deepseek-v4-flash.toml"
    return _materialize_shared_file(path, deepseek_toml(base_url).encode())


def _validated_deepseek_catalog() -> Path:
    path = deepseek_catalog_path()
    error = deepseek_catalog_error(path)
    if error is not None:
        raise RunnerError(error)
    return path


def _ensure_deepseek_agent_module(home: Path) -> Path:
    """Expose only the narrow public adapter to Pier's isolated Python env."""

    source = Path(__file__).with_name("pier_deepseek.py")
    if not source.is_file():
        raise RunnerError(
            "DeepSeek Pier adapter is missing; reinstall or upgrade dradar "
            "before running a paid task"
        )
    target = home / DEEPSEEK_AGENT_MODULE_FILENAME
    return _materialize_shared_file(target, source.read_bytes())


def _version_tuple(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in value.split("."))


def _deepseek_codex_version(assignment: dict) -> str:
    """Return the exact stable Codex release tested with DeepSeek."""

    requested = assignment.get("agent_version") or DEEPSEEK_CODEX_VERSION
    if not isinstance(requested, str) or not _STABLE_CODEX_VERSION_RE.fullmatch(
        requested
    ):
        raise RunnerError("DeepSeek requires an exact stable Codex CLI version")
    if _version_tuple(requested) < _version_tuple(DEEPSEEK_MIN_CODEX_VERSION):
        raise RunnerError(
            f"DeepSeek requires Codex >= {DEEPSEEK_MIN_CODEX_VERSION}, "
            f"but the assignment requested {requested}"
        )
    if requested != DEEPSEEK_CODEX_VERSION:
        raise RunnerError(
            f"DeepSeek runs are pinned to tested Codex {DEEPSEEK_CODEX_VERSION}; "
            f"the server requested unverified {requested}"
        )
    return requested


def _validate_deepseek_assignment(assignment: dict) -> None:
    if assignment.get("model") != DEEPSEEK_MODEL:
        raise RunnerError(
            f"unsupported DeepSeek model {assignment.get('model')!r}; "
            f"only {DEEPSEEK_MODEL!r} is enabled"
        )
    if assignment.get("effort") not in DEEPSEEK_SUPPORTED_EFFORTS:
        supported = ", ".join(sorted(DEEPSEEK_SUPPORTED_EFFORTS))
        raise RunnerError(
            f"DeepSeek effort must be one of {supported}; "
            f"got {assignment.get('effort')!r}"
        )
    _deepseek_codex_version(assignment)


def _pier_process_env(
    assignment: dict,
    *,
    deepseek_module_dir: Path | None = None,
) -> dict[str, str]:
    """Keep provider secrets out of Pier's inherited environment."""

    env = dict(os.environ)
    if is_deepseek_family(assignment_codex_provider(assignment)):
        # The key has already been materialized in a private auth.json file.
        # Removing the ambient variable prevents accidental fallback to the
        # old ``docker compose exec -e KEY=value`` path.
        env.pop(DEEPSEEK_API_KEY_ENV, None)
        env.pop(DEEPSEEK_OPENCODE_API_KEY_ENV, None)
        # Stock Pier's Codex agent propagates an ambient OPENAI_BASE_URL into
        # the container config.toml (datacurve-pier codex.py). The DeepSeek
        # provider table pins its own base_url, so this is defense in depth:
        # no ambient variable may hint at a third-party endpoint.
        env.pop("OPENAI_BASE_URL", None)
        env.pop("OPENAI_API_BASE", None)
        # uvx's public Pier environment must see only the copied, standalone
        # adapter module. An ambient PYTHONPATH/PYTHONHOME could accidentally
        # shadow datacurve-pier with another local Pier installation.
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        if deepseek_module_dir is not None:
            env["PYTHONPATH"] = str(deepseek_module_dir)
    return env


def _task_agent_timeout_sec(task_path: Path) -> float | None:
    """The task's own declared agent watchdog (task.toml's [agent].timeout_sec
    -- currently 5400.0/90min flat across the whole deep-swe set). None if the
    file is missing or malformed; caller must not guess a number in that case."""
    try:
        with (task_path / "task.toml").open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return data.get("agent", {}).get("timeout_sec")


def _agent_timeout_multiplier(assignment: dict, task_path: Path) -> float:
    """Resolve Pier's agent watchdog multiplier for one assignment.

    Ordinary benchmarks stretch the task timeout to stay behind DRadar's outer
    watchdog. Pompeii deliberately does the opposite: it normalizes old and new
    managed task packs to the benchmark's strict 90-minute agent budget.
    """
    base = _task_agent_timeout_sec(task_path)
    if not base:
        if assignment.get("benchmark_id") == POMPEII_BENCHMARK_ID:
            raise RunnerError(
                "Pompeii tasks require a readable [agent].timeout_sec so the "
                "90-minute execution limit can be enforced"
            )
        return 1.0
    if assignment.get("benchmark_id") == POMPEII_BENCHMARK_ID:
        # Old managed Pompeii packs declared 7200s while refreshed packs declare
        # 5400s. Normalize both at launch so an already-installed old pack cannot
        # silently retain the former two-hour limit. Floor the serialized ratio
        # so unusual task defaults can stop a fraction early, never after 90m.
        raw = POMPEII_AGENT_TIMEOUT_SEC / base
        return math.floor(raw * 1_000_000) / 1_000_000
    raw = (_trial_timeout_sec(assignment) + 60) / base
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
    resume_checkpoint: Path | None = None,
    provider_auth_path: Path | None = None,
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
    if is_deepseek_family(provider):
        # Keep the provider independent of DRadar's legacy checkpoint Pier
        # build. uvx resolves this exact public PyPI release in an isolated
        # tool environment; PYTHONPATH later exposes only the narrow catalog
        # uploader copied into this run directory.
        public_pier = "datacurve-pier==0.3.0"
        uvx = shutil.which("uvx")
        uv = shutil.which("uv")
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
                "uv/uvx is required for the isolated public DeepSeek runner"
            )
    else:
        pier = shutil.which("pier")
        if not pier:
            raise RunnerError(
                "pier not found on PATH (run: uv tool install datacurve-pier)"
            )
        pier_command = [pier]
    deepseek_catalog = None
    deepseek_provider_base_url = None
    if is_deepseek_family(provider):
        _validate_deepseek_assignment(assignment)
        deepseek_provider_base_url = (
            DEEPSEEK_OPENCODE_BASE_URL
            if provider == DEEPSEEK_OPENCODE_PROVIDER
            else DEEPSEEK_BASE_URL
        )
        deepseek_catalog = _validated_deepseek_catalog()
        _ensure_deepseek_agent_module(home)
        if resume_checkpoint is not None:
            raise RunnerError(
                "DeepSeek checkpoints are not supported by the public runner; "
                "start a fresh explicit run"
            )
        agent_args = ["--agent-import-path", DEEPSEEK_AGENT_IMPORT_PATH]
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
    multiplier = _agent_timeout_multiplier(assignment, task_path)
    if not math.isclose(multiplier, 1.0):
        cmd += ["--agent-timeout-multiplier", f"{multiplier:.6f}"]
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
            "--ak", "checkpoint_enabled=true",
            "--ak", f"checkpoint_assignment_id={assignment['assignment_id']}",
            "--ak", f"checkpoint_task_id={assignment['task_id']}",
            "--ak", f"checkpoint_effort={assignment['effort']}",
            "--ak", f"checkpoint_resume_generation={assignment.get('resume_generation', 0)}",
            "--ae", f"CODEX_AUTH_JSON_PATH={auth}",
        ]
        if resume_checkpoint is not None:
            cmd += ["--ak", f"checkpoint_path={resume_checkpoint}"]
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
    elif agent == "codex" and is_deepseek_family(provider):
        if provider_auth_path is None or not provider_auth_path.is_file():
            setup_provider = (
                "deepseek"
                if provider == DEEPSEEK_PROVIDER
                else DEEPSEEK_OPENCODE_PROVIDER
            )
            raise RunnerError(
                "DeepSeek runtime credential is unavailable; run "
                f"`dradar provider setup {setup_provider}` in your own "
                "interactive Terminal"
            )
        if deepseek_provider_base_url is None:
            raise RunnerError("DeepSeek provider URL was not prepared")
        config_path = _ensure_deepseek_config(home, deepseek_provider_base_url)
        submission_prompt = _ensure_codex_submission_prompt(
            home, assignment.get("benchmark_id")
        )
        if deepseek_catalog is None:  # defensive: validation must precede argv
            raise RunnerError("DeepSeek model catalog was not prepared")
        cmd += [
            "--model", assignment["model"],
            "--ak", f"reasoning_effort={assignment['effort']}",
            "--ak", f"config_toml_file={config_path}",
            "--ak", f"model_catalog_json_file={deepseek_catalog}",
            "--ak", f"provider_base_url={deepseek_provider_base_url}",
            "--ak", f"prompt_template_path={submission_prompt}",
            "--ae", f"CODEX_AUTH_JSON_PATH={provider_auth_path}",
            "--ak", f"version={_deepseek_codex_version(assignment)}",
        ]
    elif agent == "codex":
        raise RunnerError(
            f"unsupported Codex provider {provider!r}; upgrade dradar if the "
            "server intentionally enabled a new provider"
        )
    elif agent == "claude-code":
        oauth_token = claude_oauth_token()
        if not oauth_token:
            raise RunnerError(
                "CLAUDE_CODE_OAUTH_TOKEN not set (run: claude setup-token, "
                "then export CLAUDE_CODE_OAUTH_TOKEN before dradar go)"
            )
        cmd += [
            "--model", assignment["model"],
            "--ak", f"reasoning_effort={assignment['effort']}",
            "--ak", "version=2.1.197",
            "--ak", f"disallowed_tools={CLAUDE_DISALLOWED_TOOLS}",
            "--ae", f"CLAUDE_CODE_OAUTH_TOKEN={oauth_token}",
            "--ae", "API_TIMEOUT_MS=3000000",
            "--ae", "CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000",
            "--ae", "CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000",
        ]
    return cmd


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


def _plain_file(path: Path) -> bool:
    """True only for a regular file at the named path, never a symlink."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _recover_completed_checkpoint_patch(
    trial_dir: Path,
    assignment: dict,
) -> tuple[bool, str | None]:
    """Recover Pier's downloaded patch from a completed Codex checkpoint.

    Older visual-task bundles omitted ``pre_artifacts.sh``. The Codex agent
    still committed a valid answer and the checkpoint provider preserved its
    workspace patch, but Pier had no ``artifacts/model.patch`` to download.
    Recover only from an identity-matched, completed checkpoint and never
    follow symlinks or accept arbitrary bytes as a submission patch.
    """
    checkpoint_dir = trial_dir / "agent" / "checkpoint"
    metadata_path = checkpoint_dir / "checkpoint.json"
    if not _plain_file(metadata_path):
        return False, None
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False, "completed checkpoint metadata is unreadable"
    if not isinstance(metadata, dict) or metadata.get("phase") != "agent_completed":
        return False, None

    expected = {
        "assignment_id": assignment.get("assignment_id"),
        "task_id": assignment.get("task_id"),
        "model": assignment.get("model"),
        "effort": assignment.get("effort"),
    }
    mismatched = [
        key for key, value in expected.items()
        if not isinstance(value, str) or metadata.get(key) != value
    ]
    if mismatched:
        return False, (
            "completed checkpoint identity mismatch: " + ", ".join(mismatched)
        )

    workspace_name = metadata.get("workspace_patch")
    if workspace_name != "workspace.patch":
        return False, "completed checkpoint has an unsafe workspace_patch path"
    workspace_patch = checkpoint_dir / workspace_name
    if not _plain_file(workspace_patch):
        return False, "completed checkpoint is missing its workspace patch"
    try:
        data = workspace_patch.read_bytes()
    except OSError:
        return False, "completed checkpoint workspace patch is unreadable"
    if b"\x00" in data or (data and not data.startswith(b"diff --git ")):
        return False, "completed checkpoint workspace patch is not a Git diff"

    artifact_dir = trial_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    patch_path = artifact_dir / "model.patch"
    fd, temp_name = tempfile.mkstemp(prefix=".model.patch.", dir=artifact_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, patch_path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return True, None


CODEX_TRAJECTORY_BUNDLE_SCHEMA = "dradar-codex-trajectory-bundle-v1"


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


def _analyze_codex_session_events(events: list[dict], fallback_id: str) -> dict:
    """Describe one Codex JSONL file and isolate this agent's own usage.

    A spawned Codex agent currently receives a copy of its parent's event
    prefix.  Consequently its final cumulative counters include the inherited
    parent tokens.  Summing the final counters would overcharge just as badly
    as Pier's single-file conversion undercharges.  The last ``task_started``
    marks the spawned agent's own segment; subtract the final counter before
    that boundary.
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
    usage_events: list[tuple[int, dict]] = []
    for index, event in enumerate(events):
        payload = event.get("payload") or {}
        if event.get("type") == "event_msg" and isinstance(payload, dict):
            if payload.get("type") == "task_started":
                starts.append(index)
            elif payload.get("type") == "token_count":
                info = payload.get("info") or {}
                usage = _codex_usage(
                    info.get("total_token_usage") if isinstance(info, dict) else None)
                if usage is not None:
                    usage_events.append((index, usage))

    # Root files start at zero.  Child files containing an inherited prefix
    # have multiple session_meta/task_started events; their last task_started
    # is the beginning of the child's own work.
    inherited_prefix = role == "subagent" and (
        len(meta_events) > 1 or len(starts) > 1)
    boundary = starts[-1] if starts else 0
    baseline = {name: 0 for name in (
        "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_output_tokens",
    )}
    baseline_found = not inherited_prefix
    if inherited_prefix:
        for index, usage in usage_events:
            if index >= boundary:
                break
            baseline = usage
            baseline_found = True

    final_usage = usage_events[-1][1] if usage_events else None
    final_after_boundary = bool(usage_events and usage_events[-1][0] >= boundary)
    own_usage = None
    if final_usage is not None and final_after_boundary and baseline_found:
        candidate = {name: final_usage[name] - baseline[name] for name in baseline}
        if (all(value >= 0 for value in candidate.values())
                and candidate["cached_input_tokens"] <= candidate["input_tokens"]):
            own_usage = candidate

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
    return {
        "schema_version": CODEX_TRAJECTORY_BUNDLE_SCHEMA,
        "complete": complete,
        "session_file_count": len(files),
        "agent_session_count": len(representatives),
        "root_session_count": root_count,
        "subagent_session_count": sum(1 for item in representatives.values()
                                      if item["role"] == "subagent"),
        "aggregate_usage": aggregate,
        "usage_sessions": usage_sessions,
        "sessions": sessions,
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


# The (public) task repo. Self-bootstrap clones it so a volunteer never has to.
DEEP_SWE_REPO = "https://github.com/datacurve-ai/deep-swe"

# Temporary SecurityMind Pier build containing datacurve-ai/pier#23 plus
# persistent workspace/Codex-session checkpoints. Keep the immutable commit
# pin until both fixes are released upstream, then follow the official tag.
PIER_VERSION = "0.3.0.post3"
PIER_COMMIT = "acd1d94a53c9ada225187e4b73206970f14ba415"
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


def _pier_version(pier: str) -> str | None:
    try:
        proc = subprocess.run(
            [pier, "--version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _pier_version_compatible(installed_version: str | None) -> bool:
    """Accept the pinned checkpoint build and compatible later post releases."""
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
    pier = shutil.which("pier")
    installed_version = _pier_version(pier) if pier else None
    if _pier_version_compatible(installed_version):
        return
    uv = shutil.which("uv")
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
        pier = shutil.which("pier")
        installed_version = _pier_version(pier) if pier else None
        if _pier_version_compatible(installed_version):
            return
        if pier:
            print(f"Pier {installed_version or 'unknown'} lacks persistent resume — "
                  f"installing SecurityMind build {PIER_VERSION}...")
        else:
            print(f"pier not found — installing SecurityMind build {PIER_VERSION}...")
        proc = subprocess.run([uv, "tool", "install", "--force", PIER_SPEC])
        active_pier = shutil.which("pier")
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
    for cmd in (["git", "-C", str(tasks_root), "fetch", "--depth", "1", "origin", pinned],
                ["git", "-C", str(tasks_root), "checkout", pinned]):
        try:
            if subprocess.run(cmd, capture_output=True, text=True, timeout=120).returncode != 0:
                return False
        except (OSError, subprocess.TimeoutExpired):
            return False
    return local_deep_swe_commit(tasks_root) == pinned


def check_task_content_hash(assignment: dict, tasks_root: Path) -> bool | None:
    """Compare the server's task_content_hash against this volunteer's local
    checkout. Returns None when the assignment carries no hash to compare
    against (older server). A mismatch is a detection signal for the server,
    not a client-side hard stop — the caller should warn but keep running."""
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


HEARTBEAT_SEC = 60
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


def run_trial(
    assignment: dict,
    tasks_root: Path,
    work_dir: Path,
    dev_agent: str | None = None,
    on_started: Callable[[], None] | None = None,
    resume_checkpoint: Path | None = None,
) -> TrialArtifacts:
    effective_assignment = assignment
    codex_cli_version = None
    codex_provider = None
    if (dev_agent or assignment["agent"]) == "codex":
        codex_provider = (
            assignment_codex_provider(assignment) or DEFAULT_CODEX_PROVIDER
        )
        if is_deepseek_family(codex_provider):
            _validate_deepseek_assignment(assignment)
            codex_cli_version = _deepseek_codex_version(assignment)
            print(f"verified pinned DeepSeek Codex CLI: {codex_cli_version}")
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

    work_dir.mkdir(parents=True, exist_ok=True)
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
    timeout_sec = _trial_timeout_sec(effective_assignment)
    terminal_error: RunnerError | None = None
    live_error_offsets: dict[Path, int] = {}
    live_error_counts: dict[str, int] = {}
    watch_live_account_errors = (
        (dev_agent or effective_assignment["agent"]) == "codex"
    )

    provider_auth_path = None
    try:
        if is_deepseek_family(codex_provider):
            try:
                provider_auth_path = create_provider_auth_json(
                    work_dir, codex_provider
                )
            except (OSError, ValueError) as exc:
                raise RunnerError(str(exc)) from exc
        provider_kwargs = (
            {"provider_auth_path": provider_auth_path}
            if is_deepseek_family(codex_provider)
            else {}
        )
        if resume_checkpoint is None:
            cmd = build_pier_command(
                effective_assignment, tasks_root, jobs_dir, job_name, work_dir,
                dev_agent, **provider_kwargs,
            )
        else:
            cmd = build_pier_command(
                effective_assignment, tasks_root, jobs_dir, job_name, work_dir,
                dev_agent, resume_checkpoint=resume_checkpoint,
                **provider_kwargs,
            )
        env = _pier_process_env(
            effective_assignment,
            deepseek_module_dir=(
                work_dir if is_deepseek_family(codex_provider) else None
            ),
        )
        if on_started is not None:
            # Best-effort by design: this only confirms to the server that a
            # free-pick claim's short initial lease should be extended (see
            # app.py's assignment_started endpoint) -- a network hiccup here must
            # never abort a real trial that's about to burn real quota.
            try:
                on_started()
            except Exception:
                pass
        started = time.time()
        with log_path.open("w") as log:
            log.write("cmd=" + " ".join(cmd) + "\n")
            log.flush()
            # Heartbeat loop instead of a blocking run: image build + a long
            # agent turn can be silent for many minutes, and volunteers couldn't
            # tell "working" from "wedged" without docker-exec'ing into the
            # container (volunteer report, 2026-07-13). Once a minute, print
            # elapsed time plus the newest pier log line.
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                    cwd=work_dir, env=env)
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
                            f"last lines of the log:\n{_tail(log_path)}")
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
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
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
    finally:
        if provider_auth_path is not None:
            try:
                provider_auth_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RunnerError(
                    f"could not remove temporary DeepSeek auth file "
                    f"{provider_auth_path}: {exc}"
                ) from exc
    duration = time.time() - started
    if isinstance(terminal_error, LiveAccountTerminalError):
        # Let the existing runloop checkpoint/pause path classify this
        # normalized failure and open the supervised pool's graceful drain.
        # Do not upload a partial patch from a terminal account failure.
        raise terminal_error

    tail = _tail(log_path)
    try:
        job_dir, trial_dir = locate_artifacts(jobs_dir, job_name)
    except RunnerError:
        if terminal_error is not None:
            raise terminal_error
        if _looks_like_build_flake(tail):
            raise BuildFlakeError(
                f"the task environment failed to BUILD (mirror/network flake) — "
                f"the agent never started and no quota was used.\n"
                f"last lines of the log:\n{tail}")
        raise
    patch, trajectory, result = trial_artifact_paths(trial_dir)
    if not patch.is_file():
        if terminal_error is not None:
            raise terminal_error
        recovered, checkpoint_error = _recover_completed_checkpoint_patch(
            trial_dir, effective_assignment,
        )
        if recovered:
            print(
                "  recovered model.patch from the completed, identity-matched "
                "checkpoint (the task artifact hook did not run)"
            )
        elif checkpoint_error is not None:
            raise RunnerError(
                "agent completed, but model.patch collection failed and the "
                f"checkpoint could not be recovered: {checkpoint_error}; "
                f"see {log_path} and {trial_dir}"
            )
    if not patch.is_file():
        # No patch at all means the agent never produced anything — usually
        # the environment died under it. Say which, instead of blaming the
        # agent for a mirror hiccup.
        result_exception = _result_exception_text(result)
        diagnostic = "\n".join(x for x in (tail, result_exception) if x)
        if _looks_like_build_flake(diagnostic):
            raise BuildFlakeError(
                f"the task environment failed to BUILD (mirror/network flake) — "
                f"the agent never started and no quota was used.\n"
                f"build diagnostic:\n{_diagnostic_tail(diagnostic)}")
        raise RunnerError(
            f"model.patch missing (agent likely failed; see {log_path} and {trial_dir})\n"
            f"last lines of the log:\n{tail}"
        )
    returncode = proc.returncode
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
        "you've hit your usage limit", "you have hit your usage limit",
    )):
        return "quota-limit"
    if has_http_status(401) or has_http_status(403) or any(s in low for s in (
        "unauthorized", "forbidden", "authentication failed",
        "invalid api key", "token expired", "account suspended",
    )):
        return "auth"
    if has_http_status(429) or any(s in low for s in (
        "rate limit", "rate_limit", "too many requests",
    )):
        return "rate-limit"
    if "at capacity" in low:
        return "model-capacity"
    return None


def diagnose_exception(result_path: Path | None) -> dict:
    """Classify a trial's recorded exception for honest console reporting:
    {} when there is none, else {type, tail, kind} where kind is one of
    stale-agent | insufficient-balance | quota-limit | rate-limit | auth |
    model-capacity | None
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
    tail = [ln.strip() for ln in msg.splitlines() if ln.strip()][-6:]
    return {"type": info.get("exception_type"), "kind": kind, "tail": tail}


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
        "the provider is rate-limiting requests — checkpoint recovery uses "
        "bounded exponential backoff and will stop after its retry budget."),
    "quota-limit": (
        "the account quota window is exhausted. This worker stops and the pool "
        "will not start new work, while already-running siblings are allowed to "
        "finish. After the quota resets, start it again to resume the preserved "
        "checkpoints."),
    "insufficient-balance": (
        "the paid API account has insufficient balance. This worker stops and "
        "the pool will not start another task, while already-running siblings "
        "are allowed to finish; recharge it, then run `dradar resume`."),
    "auth": (
        "the agent could not authenticate inside the container — for DeepSeek "
        "run `dradar provider status deepseek` (or set it up again); for the "
        "original OpenAI path run `codex login`, then re-check `dradar doctor`."),
    "model-capacity": (
        "the model stayed at capacity after Pier retried the original Codex "
        "session with bounded backoff. This is not a problem with your setup "
        "or work; the automatic recovery was attempted but could not finish "
        "within its retry budget. Claim the cell again later."),
}
