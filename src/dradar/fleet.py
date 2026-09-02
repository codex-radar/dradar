"""One machine-local coordinator for several exact DRadar claim batches.

The web can hand the same account's independent Honeypot batches to one or
several coding-agent conversations.  Those conversations must not become
independent process supervisors: they all submit idempotent requests to this
single local coordinator.  The coordinator owns the historical machine lock,
accounts for aggregate Docker resources, and starts one existing worker-pool
parent per exact batch.  It deliberately leaves uncertain pre-Fleet containers
untouched because older ``--parallel`` sessions did not hold the machine lock.

The control plane is deliberately local-file based.  It works without an
open TCP port, stores no credentials, and uses atomic rename for every public
state transition.  Request/response files are user-private and contain only
batch IDs, worker targets, bounded control flags, and (for run plans) the path
to a mode-0600 credential file. Credential values never enter Fleet state,
requests or process arguments. A request may carry a small allowlist of
absolute executable paths (never credential values) so a pool for a second
Harness inherits the environment of the Agent conversation that requested it,
instead of the unrelated conversation that happened to start the coordinator.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import __version__
from .api_client import ApiError, normalize_batch_id
from .capacity import (
    AUTO_WORKER_CAP,
    docker_resources,
    inspect_capacity,
    worker_resource_warnings,
)
from .codebuddy_provider import (
    CODEBUDDY_MANAGED_HOME_ENV,
    managed_codebuddy_home,
)
from .identity import _client
from .local_config import HOME, _load_config, runtime_config
from .machine import acquire_run_lock
from .providers import KIMI_CREDENTIAL_PATH_ENV


SCHEMA_VERSION = 1
# Version the machine-local controller contract independently from the public
# state schema.  A controller with no value here predates per-request runtime
# selection and would keep spawning pools from its own stale installation.
CONTROLLER_PROTOCOL_VERSION = 6
FLEET_DIR = "fleet"
STATE_FILE = "state.json"
START_LOCK_FILE = "start.lock"
CONTROLLER_LOCK_FILE = "controller.lock"
PREPARATION_LOCK_FILE = "preparation.lock"
POOL_LOCK_DIR = "batch-locks"
REQUEST_DIR = "requests"
RESPONSE_DIR = "responses"
LOG_DIR = "logs"
ABORT_DIR = "aborts"
STARTUP_DIR = "startup"
STARTUP_LOCK_DIR = "startup-locks"
CONTROLLER_LOG = "controller.log"

_LAUNCH_ID_ENV = "DRADAR_FLEET_LAUNCH_ID"
CONTROLLER_ID_ENV = "DRADAR_FLEET_CONTROLLER_ID"
POOL_BATCH_ENV = "DRADAR_FLEET_BATCH_ID"
POOL_STARTUP_FILE_ENV = "DRADAR_FLEET_STARTUP_FILE"

# These values select reviewed local provider executables and owner-private
# credential directories. They are paths, never credential contents. Never
# expand this allowlist to API keys, tokens, proxy URLs, or arbitrary values.
POOL_EXECUTABLE_ENV_KEYS = (
    "CODEBUDDY_CLI_PATH",
    "GROK_CLI_PATH",
    "KIMI_CLI_PATH",
    "ZCODE_CLI_PATH",
)
POOL_RUNTIME_ENV_PATH_KINDS = {
    **{key: "file" for key in POOL_EXECUTABLE_ENV_KEYS},
    CODEBUDDY_MANAGED_HOME_ENV: "directory",
    KIMI_CREDENTIAL_PATH_ENV: "private_file",
}
POOL_RUNTIME_ENV_KEYS = tuple(POOL_RUNTIME_ENV_PATH_KINDS)

START_TIMEOUT_SECONDS = 20.0
REQUEST_TIMEOUT_SECONDS = 60.0
HEARTBEAT_SECONDS = 1.0
HEARTBEAT_STALE_SECONDS = 60.0
IDLE_EXIT_SECONDS = 30.0
ENVIRONMENT_BUILD_FAILED_EXIT_CODE = 78
SETTLED_BATCH_STATUSES = {"completed", "failed", "interrupted", "stopped"}
STARTUP_OBSERVE_SECONDS = 480.0

_pool_lock_handles: dict[str, object] = {}


class FleetError(RuntimeError):
    pass


class FleetBusy(FleetError):
    pass


class FleetStartupError(FleetError):
    def __init__(
        self, code: str, user_message: str, *, retryable: bool = True,
    ) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.retryable = retryable


class FleetControllerUpdatePending(FleetError):
    code = "local_runtime_update_pending"
    user_message = (
        "这台设备正在继续运行先前启动的题目。DRadar 刚完成升级；为避免中断"
        "现有题目，新领取的题会等它们结束后再启动。我会稍后自动重试。"
    )

    def __init__(self) -> None:
        super().__init__(self.user_message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(home: Path = HOME) -> Path:
    return home / FLEET_DIR


def _state_path(home: Path = HOME) -> Path:
    return _root(home) / STATE_FILE


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _prepare_dirs(home: Path = HOME) -> None:
    root = _root(home)
    _private_dir(root)
    for name in (
        POOL_LOCK_DIR, REQUEST_DIR, RESPONSE_DIR, LOG_DIR, ABORT_DIR,
        STARTUP_DIR,
    ):
        _private_dir(root / name)


def _atomic_json(path: Path, payload: dict) -> None:
    _private_dir(path.parent)
    fd, raw = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
    )
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


@contextmanager
def _locked(path: Path, *, blocking: bool = True) -> Iterator[object]:
    _private_dir(path.parent)
    handle = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(handle.fileno(), mode, 1)
        else:
            import fcntl

            operation = fcntl.LOCK_EX
            if not blocking:
                operation |= fcntl.LOCK_NB
            fcntl.flock(handle.fileno(), operation)
    except OSError as exc:
        handle.close()
        raise FleetBusy(f"lock is already held: {path}") from exc
    try:
        yield handle
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_is_held(path: Path) -> bool:
    """Return true only when another live file description owns the lock."""
    if not path.is_file():
        return False
    try:
        with _locked(path, blocking=False):
            return False
    except FleetBusy:
        return True


@contextmanager
def _controller_lease(home: Path, controller_id: str) -> Iterator[None]:
    """Tie controller identity to process lifetime, defeating PID reuse."""
    path = _root(home) / CONTROLLER_LOCK_FILE
    with _locked(path, blocking=False) as handle:
        handle.seek(0)
        handle.truncate()
        json.dump({
            "schema_version": SCHEMA_VERSION,
            "controller_id": controller_id,
            "pid": os.getpid(),
            "started_at": _now(),
        }, handle)
        handle.write("\n")
        handle.flush()
        if os.name != "nt":
            os.fsync(handle.fileno())
        yield


def _controller_lease_matches(home: Path, controller_id: object) -> bool:
    path = _root(home) / CONTROLLER_LOCK_FILE
    recorded = _read_json(path)
    return bool(
        isinstance(controller_id, str)
        and recorded
        and recorded.get("controller_id") == controller_id
        and _lock_is_held(path)
    )


def _parse_time(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def controller_is_active(home: Path = HOME) -> bool:
    state = _read_json(_state_path(home))
    if not state or state.get("schema_version") != SCHEMA_VERSION:
        return False
    if state.get("status") not in {"starting", "active", "stopping"}:
        return False
    heartbeat = _parse_time(state.get("heartbeat_at"))
    if heartbeat is None or time.time() - heartbeat > HEARTBEAT_STALE_SECONDS:
        return False
    return bool(
        _pid_alive(state.get("pid"))
        and _controller_lease_matches(home, state.get("controller_id"))
    )


def controller_matches(controller_id: str, home: Path = HOME) -> bool:
    """Prove an internal pool was launched by the currently live controller."""
    state = _read_json(_state_path(home))
    return bool(
        controller_id
        and state
        and state.get("controller_id") == controller_id
        and controller_is_active(home)
    )


@contextmanager
def preparation_lock(home: Path = HOME) -> Iterator[None]:
    """Serialize shared task-pack/Pier preparation across Fleet batch parents."""
    with _locked(_root(home) / PREPARATION_LOCK_FILE):
        yield


def start_pool_watchdog(
    home: Path, controller_id: str, *, interval: float = 2.0,
) -> threading.Thread:
    """Interrupt a pool if its coordinator disappears or stops heartbeating.

    Pool parents inherit the coordinator's historical machine-lock descriptor,
    so an old CLI cannot enter its orphan sweep during this bounded shutdown.
    """
    pid = os.getpid()

    def watch() -> None:
        while True:
            state = _read_json(_state_path(home))
            healthy = bool(
                state
                and state.get("controller_id") == controller_id
                and state.get("status") in {"active", "stopping"}
                and _pid_alive(state.get("pid"))
            )
            heartbeat = _parse_time(state.get("heartbeat_at")) if state else None
            if heartbeat is None or time.time() - heartbeat > HEARTBEAT_STALE_SECONDS:
                healthy = False
            if not healthy:
                try:
                    os.kill(pid, signal.SIGINT)
                except OSError:
                    pass
                return
            time.sleep(interval)

    thread = threading.Thread(
        target=watch, name="dradar-fleet-watchdog", daemon=True,
    )
    thread.start()
    return thread


def _tail(path: Path, *, max_bytes: int = 6000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _controller_protocol_matches(state: dict | None) -> bool:
    return bool(
        isinstance(state, dict)
        and state.get("controller_protocol_version")
        == CONTROLLER_PROTOCOL_VERSION
    )


def _retire_incompatible_idle_controller(
    home: Path, state: dict,
) -> None:
    """Gracefully replace a stale controller only when it owns no live pool."""

    if _active_batches(state):
        raise FleetControllerUpdatePending()
    pid = state.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise FleetControllerUpdatePending()
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise FleetControllerUpdatePending() from exc
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if not controller_is_active(home):
            return
        time.sleep(0.1)
    raise FleetControllerUpdatePending()


def prepare_new_batch_runtime(home: Path = HOME) -> None:
    """Fail closed before server admission if an old live pool must drain.

    An incompatible but idle controller is rotated without user interaction.
    Existing pools are never interrupted merely because the foreground CLI was
    upgraded.
    """

    _prepare_dirs(home)
    with _locked(_root(home) / START_LOCK_FILE):
        state = _read_json(_state_path(home))
        if not controller_is_active(home) or _controller_protocol_matches(state):
            return
        _retire_incompatible_idle_controller(home, state or {})


def _ensure_controller(home: Path = HOME) -> dict:
    _prepare_dirs(home)
    with _locked(_root(home) / START_LOCK_FILE):
        state = _read_json(_state_path(home))
        if controller_is_active(home):
            return state or {}

        launch_id = uuid.uuid4().hex
        log_path = _root(home) / LOG_DIR / CONTROLLER_LOG
        log_handle = open(log_path, "a", encoding="utf-8")
        env = os.environ.copy()
        env[_LAUNCH_ID_ENV] = launch_id
        command = [
            sys.executable, "-m", "dradar.cli", "fleet", "serve", "--internal",
        ]
        kwargs: dict = {
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if os.name == "nt":  # pragma: no cover - Windows-specific process flags
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)
        finally:
            log_handle.close()

        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            state = _read_json(_state_path(home))
            if (
                state
                and state.get("controller_id") == launch_id
                and state.get("status") == "active"
                and controller_is_active(home)
            ):
                return state
            if process.poll() is not None:
                break
            time.sleep(0.1)
        detail = _tail(log_path).strip()
        message = "could not start the local DRadar Fleet coordinator"
        if detail:
            message += f":\n{detail}"
        raise FleetError(message)


def _pool_executable_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Capture only reviewed provider runtime paths, never secret values."""
    source = os.environ if environ is None else environ
    selected: dict[str, str] = {}
    candidates = dict(source)
    if environ is None and CODEBUDDY_MANAGED_HOME_ENV not in candidates:
        candidates[CODEBUDDY_MANAGED_HOME_ENV] = str(managed_codebuddy_home())
    for key, kind in POOL_RUNTIME_ENV_PATH_KINDS.items():
        value = candidates.get(key)
        if not isinstance(value, str) or not value or len(value) > 4096:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute() or path.is_symlink():
            continue
        if kind == "file" and path.is_file():
            selected[key] = str(path)
        elif kind == "private_file" and path.is_file():
            try:
                info = path.stat()
            except OSError:
                continue
            if os.name != "nt" and (
                info.st_mode & 0o077
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            ):
                continue
            selected[key] = str(path)
        elif kind == "directory" and path.is_dir():
            try:
                info = path.stat()
            except OSError:
                continue
            if os.name != "nt" and (
                info.st_mode & 0o077
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            ):
                continue
            selected[key] = str(path)
    return selected


def _request(command: str, payload: dict, *, home: Path = HOME) -> dict:
    state = _ensure_controller(home)
    controller_id = state.get("controller_id")
    request_id = uuid.uuid4().hex
    request_path = _root(home) / REQUEST_DIR / f"{request_id}.json"
    response_path = _root(home) / RESPONSE_DIR / f"{request_id}.json"
    body = {
        "schema_version": SCHEMA_VERSION,
        "controller_protocol_version": CONTROLLER_PROTOCOL_VERSION,
        "client_version": __version__,
        "runtime_executable": os.path.abspath(sys.executable),
        "runtime_environment": _pool_executable_environment(),
        "request_id": request_id,
        "controller_id": controller_id,
        "command": command,
        "created_at": _now(),
        **payload,
    }
    _atomic_json(request_path, body)
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            response = _read_json(response_path)
            if response is not None:
                return response
            if not controller_is_active(home):
                raise FleetError("the Fleet coordinator stopped before replying")
            time.sleep(0.1)
    finally:
        response_path.unlink(missing_ok=True)
    raise FleetError("timed out waiting for the Fleet coordinator")


def _active_batches(state: dict) -> dict[str, dict]:
    batches = state.get("batches")
    if not isinstance(batches, dict):
        return {}
    return {
        batch_id: item for batch_id, item in batches.items()
        if isinstance(item, dict)
        and item.get("status") in {"starting", "running", "stopping", "orphaned"}
    }


def _resolve_workers(
    requested: int | str, batch_id: str, state: dict,
    credentials_file: str | None = None,
) -> tuple[int, list[str], dict]:
    try:
        cfg = (
            runtime_config(credentials_file)
            if credentials_file else _load_config()
        )
        client = _client(cfg)
    except (SystemExit, ValueError) as exc:
        raise FleetError(str(exc)) from exc
    client.set_batch_id(batch_id)
    try:
        report = inspect_capacity(client)
    except ApiError as exc:
        raise FleetError(f"cannot inspect exact batch {batch_id}: {exc}") from exc
    reserved = sum(
        int(item.get("workers") or 0) for item in _active_batches(state).values()
    )
    if requested == "auto":
        total_auto_limit = min(
            report.cpu_limit,
            report.memory_limit,
            report.disk_limit,
            report.account_limit,
            AUTO_WORKER_CAP,
        )
        available = max(0, total_auto_limit - reserved)
        workers = min(report.held_tasks, available)
        if workers < 1:
            raise FleetError(
                "no safe automatic worker slot remains after accounting for the "
                f"{reserved} worker(s) already reserved by this machine's Fleet; "
                "finish a batch or choose a reviewed manual worker count"
            )
    else:
        workers = int(requested)
        if not 1 <= workers <= 40:
            raise FleetError("--workers N requires 1 <= N <= 40")
    cpus, memory_gib, probe_warnings = docker_resources()
    warnings = list(probe_warnings)
    warnings.extend(worker_resource_warnings(reserved + workers, cpus, memory_gib))
    if reserved + workers > report.account_limit:
        warnings.append(
            f"the Fleet requests {reserved + workers} workers, but the account's "
            f"server concurrency limit is {report.account_limit}; excess workers "
            "will wait without checking out an assignment"
        )
    metadata = {
        "reserved_before": reserved,
        "account_limit": report.account_limit,
        "held_tasks": report.held_tasks,
        "docker_cpus": report.docker_cpus,
        "docker_memory_gib": report.docker_memory_gib,
        "disk_free_gib": report.disk_free_gib,
    }
    return workers, warnings, metadata


def _pool_lock_path(home: Path, batch_id: str) -> Path:
    return _root(home) / POOL_LOCK_DIR / f"{batch_id}.lock"


def _pool_startup_path(home: Path, batch_id: str) -> Path:
    return _root(home) / STARTUP_DIR / f"{batch_id}.json"


def _pool_startup_lock_path(home: Path, batch_id: str) -> Path:
    return _root(home) / STARTUP_LOCK_DIR / f"{batch_id}.lock"


def _validated_pool_startup_target(home: Path, batch_id: str) -> Path:
    normalized = normalize_batch_id(batch_id)
    if normalized is None:
        raise FleetError("Fleet pool startup requires an exact batch ID")
    expected = _pool_startup_path(home, normalized)
    configured = os.environ.get(POOL_STARTUP_FILE_ENV)
    if (
        os.environ.get(POOL_BATCH_ENV) != normalized
        or not configured
        or os.path.abspath(configured) != os.path.abspath(expected)
    ):
        raise FleetError("invalid internal Fleet startup acknowledgement")
    controller_id = os.environ.get(CONTROLLER_ID_ENV)
    if not controller_id or not controller_matches(controller_id, home):
        raise FleetError("invalid internal Fleet startup acknowledgement")
    return expected


def publish_pool_startup_ready(home: Path, batch_id: str) -> None:
    """Prove that a child registered and checked out its first assignment."""

    path = _validated_pool_startup_target(home, batch_id)
    with _locked(_pool_startup_lock_path(home, batch_id)):
        existing = _read_json(path)
        if existing and existing.get("status") == "failed":
            raise FleetError("Fleet startup was already marked failed")
        _atomic_json(path, {
            "schema_version": SCHEMA_VERSION,
            "controller_id": os.environ[CONTROLLER_ID_ENV],
            "batch_id": normalize_batch_id(batch_id),
            "pid": os.getpid(),
            "status": "ready",
            "recorded_at": _now(),
        })


def publish_pool_startup_failure(
    home: Path,
    batch_id: str,
    *,
    error_code: str,
    user_message: str,
    retryable: bool = True,
) -> bool:
    """Publish a bounded, credential-free failure before a pool becomes ready."""

    path = _validated_pool_startup_target(home, batch_id)
    with _locked(_pool_startup_lock_path(home, batch_id)):
        existing = _read_json(path)
        if existing and existing.get("status") == "ready":
            return False
        safe_code = str(error_code)[:80]
        safe_message = " ".join(str(user_message).split())[:500]
        _atomic_json(path, {
            "schema_version": SCHEMA_VERSION,
            "controller_id": os.environ[CONTROLLER_ID_ENV],
            "batch_id": normalize_batch_id(batch_id),
            "pid": os.getpid(),
            "status": "failed",
            "error_code": safe_code,
            "user_message": safe_message,
            "retryable": bool(retryable),
            "recorded_at": _now(),
        })
        return True


def acquire_pool_lock(home: Path, batch_id: str, controller_id: str) -> None:
    """Hold one exact batch for a Fleet pool parent until process exit."""
    normalized = normalize_batch_id(batch_id)
    if normalized is None:
        raise FleetError("Fleet pool requires an exact batch ID")
    path = _pool_lock_path(home, normalized)
    context = _locked(path, blocking=False)
    try:
        handle = context.__enter__()
    except FleetBusy as exc:
        raise FleetError(
            f"batch {normalized} already has a local Fleet pool owner"
        ) from exc
    handle.seek(0)
    handle.truncate()
    json.dump({
        "schema_version": SCHEMA_VERSION,
        "batch_id": normalized,
        "controller_id": controller_id,
        "pid": os.getpid(),
        "started_at": _now(),
    }, handle)
    handle.write("\n")
    handle.flush()
    _pool_lock_handles[normalized] = (context, handle)


def release_pool_locks_for_tests() -> None:
    """Release process-lifetime pool locks; production relies on process exit."""
    for context, _handle in list(_pool_lock_handles.values()):
        context.__exit__(None, None, None)
    _pool_lock_handles.clear()


def _initial_state(controller_id: str, previous: dict | None) -> dict:
    batches = {}
    if isinstance(previous, dict) and isinstance(previous.get("batches"), dict):
        for batch_id, item in previous["batches"].items():
            if not isinstance(item, dict):
                continue
            kept = dict(item)
            if kept.get("status") in {"starting", "running", "stopping"}:
                kept["status"] = "interrupted"
                kept["detail"] = "previous Fleet coordinator stopped"
                kept["updated_at"] = _now()
            batches[batch_id] = kept
    return {
        "schema_version": SCHEMA_VERSION,
        "controller_protocol_version": CONTROLLER_PROTOCOL_VERSION,
        "dradar_version": __version__,
        "controller_id": controller_id,
        "pid": os.getpid(),
        "status": "starting",
        "started_at": _now(),
        "heartbeat_at": _now(),
        "batches": batches,
    }


def _write_state(home: Path, state: dict) -> None:
    state["heartbeat_at"] = _now()
    state["total_workers"] = sum(
        int(item.get("workers") or 0) for item in _active_batches(state).values()
    )
    _atomic_json(_state_path(home), state)


def _spawn_pool(
    home: Path, state: dict, batch_id: str, workers: int,
    *,
    refill: bool = False,
    max_tasks: int | None = None,
    refill_harness: str | None = None,
    refill_model: str | None = None,
    refill_effort: str | None = None,
    credentials_file: str | None = None,
    runtime_executable: str | None = None,
    runtime_environment: Mapping[str, str] | None = None,
) -> tuple[subprocess.Popen, object]:
    controller_id = str(state["controller_id"])
    log_path = _root(home) / LOG_DIR / f"batch-{batch_id}.log"
    log_handle = open(log_path, "a", encoding="utf-8")
    executable = runtime_executable or sys.executable
    if not os.path.isabs(executable) or not Path(executable).is_file():
        raise FleetError("invalid DRadar runtime for the new local run")
    command = [
        executable, "-m", "dradar.cli", "resume", "-y",
        "--batch-id", batch_id, "--workers", str(workers), "--fleet-pool",
    ]
    if credentials_file:
        command.extend(("--credentials-file", credentials_file))
    if refill:
        command.extend((
            "--refill", "--refill-to", str(workers),
            "--max-tasks", str(max_tasks),
            "--refill-harness", str(refill_harness),
            "--refill-model", str(refill_model),
            "--refill-effort", str(refill_effort),
        ))
    env = os.environ.copy()
    for key in POOL_RUNTIME_ENV_KEYS:
        env.pop(key, None)
    env.update(dict(runtime_environment or {}))
    env[CONTROLLER_ID_ENV] = controller_id
    env[POOL_BATCH_ENV] = batch_id
    startup_path = _pool_startup_path(home, batch_id)
    startup_path.unlink(missing_ok=True)
    env[POOL_STARTUP_FILE_ENV] = str(startup_path)
    env["DRADAR_REFILL_PLAN_SCOPE"] = batch_id
    abort_path = _root(home) / ABORT_DIR / f"{batch_id}.stop"
    abort_path.unlink(missing_ok=True)
    env["DRADAR_POOL_ABORT_FILE"] = str(abort_path)
    kwargs: dict = {
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }
    # Keep the historical machine-lock file description alive in every pool
    # parent. If the coordinator crashes, old CLIs still cannot start an
    # unsafe orphan sweep before the pool watchdog exits.
    from . import machine

    lock_handle = machine._lock_handle
    if lock_handle is not None:
        os.set_inheritable(lock_handle.fileno(), True)
        if os.name == "nt":  # pragma: no cover - Windows handle inheritance
            kwargs["close_fds"] = False
        else:
            kwargs["pass_fds"] = (lock_handle.fileno(),)
    if os.name == "nt":  # pragma: no cover
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(command, **kwargs)
    except BaseException:
        log_handle.close()
        raise
    return process, log_handle


def _send_interrupt(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":  # pragma: no cover
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
    except (OSError, ProcessLookupError):
        pass


def _stop_remote_refill(
    batch_id: str, reason: str, credentials_file: str | None = None,
) -> str | None:
    """Stop one server-authoritative campaign; return a user-facing warning."""
    try:
        cfg = runtime_config(credentials_file)
        remote = _client(cfg)
        remote.set_batch_id(batch_id)
        remote.stop_refill_campaign(batch_id, reason)
    except (ApiError, OSError, ValueError) as exc:
        return f"could not confirm server refill stop for {batch_id}: {exc}"
    return None


def _stop_item_refill(item: dict, batch_id: str, reason: str) -> str | None:
    credentials_file = item.get("credentials_file")
    if credentials_file:
        return _stop_remote_refill(batch_id, reason, credentials_file)
    return _stop_remote_refill(batch_id, reason)


def _is_run_plan_item(item: dict) -> bool:
    return bool(
        isinstance(item.get("plan_id"), str) and item.get("plan_id")
        and isinstance(item.get("credentials_file"), str)
        and item.get("credentials_file")
    )


def _request_pool_drain(home: Path, batch_id: str, reason: str) -> str | None:
    """Stop new checkout/backfill without interrupting active model/upload work."""

    try:
        directory = _root(home) / ABORT_DIR
        _private_dir(directory)
        path = directory / f"{batch_id}.stop"
        temporary = directory / f".{batch_id}.{os.getpid()}.{time.time_ns()}.tmp"
        safe_reason = " ".join(str(reason).split())[:300] or "this device was stopped"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"drain:{safe_reason}")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        return f"could not publish the local safe-stop marker for {batch_id}: {exc}"
    finally:
        if "temporary" in locals():
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return None


def _stop_run_plan_device(item: dict, reason: str) -> str | None:
    """Idempotently retire only this credential's device after a local fault."""

    try:
        cfg = runtime_config(item["credentials_file"])
        plan_id = cfg.get("run_plan_id")
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("missing run plan ID")
        remote = _client(cfg)
        remote.stop_run_plan(plan_id=plan_id, scope="this_device")
    except (ApiError, KeyError, OSError, ValueError) as exc:
        return f"could not confirm this device stopped after {reason}: {exc}"
    return None


def _response(home: Path, request_id: str, payload: dict) -> None:
    _atomic_json(
        _root(home) / RESPONSE_DIR / f"{request_id}.json",
        {"schema_version": SCHEMA_VERSION, "request_id": request_id, **payload},
    )


def _handle_request(
    home: Path,
    state: dict,
    processes: dict[str, subprocess.Popen],
    logs: dict[str, object],
    request: dict,
) -> None:
    request_id = str(request.get("request_id") or "")
    if not request_id:
        return
    if request.get("controller_id") != state.get("controller_id"):
        _response(home, request_id, {
            "ok": False, "error": "request targeted a stale Fleet coordinator",
        })
        return
    command = request.get("command")
    if command == "add":
        if (
            request.get("controller_protocol_version")
            != CONTROLLER_PROTOCOL_VERSION
        ):
            _response(home, request_id, {
                "ok": False,
                "error": FleetControllerUpdatePending.user_message,
                "error_code": FleetControllerUpdatePending.code,
            })
            return
        try:
            batch_id = normalize_batch_id(request.get("batch_id"))
            if batch_id is None:
                raise FleetError("Fleet add requires an exact batch ID")
            current = state["batches"].get(batch_id)
            if current and current.get("status") in {"starting", "running", "stopping"}:
                requested_shape = {
                    "plan_id": request.get("plan_id"),
                    "credentials_file": request.get("credentials_file"),
                    "refill": bool(request.get("refill")),
                    "max_tasks": request.get("max_tasks"),
                    "refill_harness": request.get("refill_harness"),
                    "refill_model": request.get("refill_model"),
                    "refill_effort": request.get("refill_effort"),
                }
                if any(
                    current.get(key) != value
                    for key, value in requested_shape.items()
                ):
                    raise FleetError(
                        f"batch {batch_id} is already active with different "
                        "refill limits or scope"
                    )
                requested_workers = request.get("workers", "auto")
                if (
                    requested_workers != "auto"
                    and int(requested_workers) != int(current.get("workers") or 0)
                ):
                    raise FleetError(
                        f"batch {batch_id} is already active with a different "
                        "worker target"
                    )
                _response(home, request_id, {
                    "ok": True,
                    "already_active": True,
                    "batch": current,
                    "total_workers": state.get("total_workers", 0),
                })
                return
            if (
                current
                and current.get("status") in SETTLED_BATCH_STATUSES
                and not request.get("retry")
            ):
                _response(home, request_id, {
                    "ok": True,
                    "already_settled": True,
                    "batch": current,
                    "total_workers": state.get("total_workers", 0),
                })
                return
            refill = bool(request.get("refill"))
            max_tasks = request.get("max_tasks")
            refill_harness = request.get("refill_harness")
            refill_model = request.get("refill_model")
            refill_effort = request.get("refill_effort")
            credentials_file = request.get("credentials_file")
            plan_id = request.get("plan_id")
            if (
                current
                and current.get("status") in SETTLED_BATCH_STATUSES
                and request.get("retry")
                and credentials_file is None
                and plan_id is None
                and (
                    current.get("credentials_file") is not None
                    or current.get("plan_id") is not None
                )
            ):
                saved_credentials = current.get("credentials_file")
                saved_plan_id = current.get("plan_id")
                if (
                    not isinstance(saved_credentials, str)
                    or not saved_credentials
                    or not isinstance(saved_plan_id, str)
                    or not saved_plan_id
                ):
                    raise FleetError(
                        "saved run-plan identity is incomplete; use the "
                        "original website run instructions to recover safely"
                    )
                credentials_file = saved_credentials
                plan_id = saved_plan_id
            if credentials_file is not None and not isinstance(credentials_file, str):
                raise FleetError("invalid private run-plan credentials file")
            if plan_id is not None and (
                not isinstance(plan_id, str) or not plan_id
                or credentials_file is None
            ):
                raise FleetError("a run plan requires its private credentials file")
            if refill:
                if (
                    not isinstance(max_tasks, int)
                    or isinstance(max_tasks, bool)
                    or max_tasks < 1
                ):
                    raise FleetError(
                        "Fleet refill requires a positive --max-tasks total cap"
                    )
                if not all(
                    isinstance(value, str) and value
                    for value in (
                        refill_harness, refill_model, refill_effort,
                    )
                ):
                    raise FleetError(
                        "Fleet refill requires exact --refill-harness, "
                        "--refill-model, and --refill-effort scope"
                    )
            workers, warnings, capacity = _resolve_workers(
                request.get("workers", "auto"), batch_id, state,
                credentials_file,
            )
            runtime_executable = request.get("runtime_executable")
            if not isinstance(runtime_executable, str):
                raise FleetError("invalid DRadar runtime for the new local run")
            raw_runtime_environment = request.get("runtime_environment", {})
            if not isinstance(raw_runtime_environment, dict):
                raise FleetError("invalid provider runtime paths for the new local run")
            runtime_environment: dict[str, str] = {}
            for key, value in raw_runtime_environment.items():
                kind = POOL_RUNTIME_ENV_PATH_KINDS.get(key)
                if kind is None or not isinstance(value, str):
                    raise FleetError("invalid provider runtime paths for the new local run")
                path = Path(value)
                if (
                    len(value) > 4096 or not path.is_absolute()
                    or path.is_symlink()
                ):
                    raise FleetError("invalid provider runtime paths for the new local run")
                if kind in {"file", "private_file"} and not path.is_file():
                    raise FleetError("invalid provider runtime paths for the new local run")
                if kind == "private_file":
                    info = path.stat()
                    if os.name != "nt" and (
                        info.st_mode & 0o077
                        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
                    ):
                        raise FleetError(
                            "invalid provider runtime paths for the new local run"
                        )
                if kind == "directory":
                    if not path.is_dir():
                        raise FleetError("invalid provider runtime paths for the new local run")
                    info = path.stat()
                    if os.name != "nt" and (
                        info.st_mode & 0o077
                        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
                    ):
                        raise FleetError("invalid provider runtime paths for the new local run")
                runtime_environment[key] = value
            process, log_handle = _spawn_pool(
                home, state, batch_id, workers,
                refill=refill,
                max_tasks=max_tasks,
                refill_harness=refill_harness,
                refill_model=refill_model,
                refill_effort=refill_effort,
                credentials_file=credentials_file,
                runtime_executable=runtime_executable,
                runtime_environment=runtime_environment,
            )
            try:
                item = {
                    "batch_id": batch_id,
                    "workers": workers,
                    "status": "starting",
                    "startup_status": "pending",
                    "pid": process.pid,
                    "added_at": _now(),
                    "updated_at": _now(),
                    "log_path": str(
                        _root(home) / LOG_DIR / f"batch-{batch_id}.log"
                    ),
                    "warnings": warnings,
                    "capacity": capacity,
                    "plan_id": plan_id,
                    "credentials_file": credentials_file,
                    "refill": refill,
                    "max_tasks": max_tasks,
                    "refill_harness": refill_harness,
                    "refill_model": refill_model,
                    "refill_effort": refill_effort,
                }
                state["batches"][batch_id] = item
                _write_state(home, state)
                processes[batch_id] = process
                logs[batch_id] = log_handle
            except BaseException:
                _send_interrupt(process)
                log_handle.close()
                state["batches"].pop(batch_id, None)
                raise
            _response(home, request_id, {
                "ok": True,
                "already_active": False,
                "batch": item,
                "total_workers": state.get("total_workers", 0),
            })
        except (FleetError, ValueError, OSError) as exc:
            _response(home, request_id, {"ok": False, "error": str(exc)})
        return
    if command == "stop":
        selected = request.get("batch_id")
        targets = list(processes) if request.get("all") else [selected]
        only_if_startup_pending = bool(
            request.get("only_if_startup_pending", False)
        )
        stopped = []
        condition_changed = []
        warnings = []
        for batch_id in targets:
            process = processes.get(batch_id)
            if process is None or process.poll() is not None:
                continue
            item = state["batches"].get(batch_id) or {}
            if only_if_startup_pending:
                with _locked(_pool_startup_lock_path(home, batch_id)):
                    event = _read_json(_pool_startup_path(home, batch_id))
                    if event and event.get("status") == "ready":
                        item["startup_status"] = "ready"
                        item["status"] = "running"
                        item["ready_at"] = event.get("recorded_at") or _now()
                        item["updated_at"] = _now()
                        condition_changed.append(batch_id)
                        continue
                    if (
                        item.get("status") != "starting"
                        or item.get("startup_status") != "pending"
                        or (event and event.get("status") == "failed")
                    ):
                        condition_changed.append(batch_id)
                        continue
                    # Reserve the timeout outcome under the same lock used by
                    # the parent ready writer. Once this lands, a child can no
                    # longer win a late ready race after the destructive stop.
                    _atomic_json(_pool_startup_path(home, batch_id), {
                        "schema_version": SCHEMA_VERSION,
                        "controller_id": state.get("controller_id"),
                        "batch_id": batch_id,
                        "pid": process.pid,
                        "status": "failed",
                        "error_code": "local_start_timeout",
                        "user_message": (
                            "这台设备在限定时间内没有任何 worker 完成注册并开始"
                            "首个题目；本机协调器已确认停止请求，题目仍然保留。"
                        ),
                        "retryable": True,
                        "recorded_at": _now(),
                    })
            run_plan_item = _is_run_plan_item(item)
            if run_plan_item:
                warning = _stop_run_plan_device(
                    item, "a machine-local stop request",
                )
                if warning:
                    warnings.append(warning)
                    item.setdefault("warnings", []).append(warning)
                drain_warning = _request_pool_drain(
                    home, batch_id,
                    "this device was asked to stop; active work will finish",
                )
                if drain_warning:
                    warnings.append(drain_warning)
                    item.setdefault("warnings", []).append(drain_warning)
            elif item.get("refill"):
                warning = _stop_item_refill(
                    item, batch_id, "stopped by the machine-local Fleet",
                )
                if warning:
                    warnings.append(warning)
                    item.setdefault("warnings", []).append(warning)
            if only_if_startup_pending or not run_plan_item:
                _send_interrupt(process)
            item["status"] = "stopping"
            item["updated_at"] = _now()
            stopped.append(batch_id)
        _write_state(home, state)
        _response(home, request_id, {
            "ok": True,
            "stopping": stopped,
            "condition_changed": condition_changed,
            "warnings": warnings,
        })
        return
    _response(home, request_id, {"ok": False, "error": "unknown Fleet command"})


def _process_inbox(
    home: Path,
    state: dict,
    processes: dict[str, subprocess.Popen],
    logs: dict[str, object],
) -> None:
    inbox = _root(home) / REQUEST_DIR
    for path in sorted(inbox.glob("*.json")):
        request = _read_json(path)
        if request is None:
            path.unlink(missing_ok=True)
            continue
        try:
            _handle_request(home, state, processes, logs, request)
        finally:
            path.unlink(missing_ok=True)


def _refresh_pool_startups(
    home: Path,
    state: dict,
    processes: dict[str, subprocess.Popen],
) -> None:
    """Promote only acknowledgements written by the exact supervised parent."""

    changed = False
    for batch_id, process in processes.items():
        item = state["batches"].get(batch_id)
        if not isinstance(item, dict) or item.get("startup_status") != "pending":
            continue
        event = _read_json(_pool_startup_path(home, batch_id))
        if not event or not (
            event.get("schema_version") == SCHEMA_VERSION
            and event.get("controller_id") == state.get("controller_id")
            and event.get("batch_id") == batch_id
            and event.get("pid") == process.pid
            and event.get("status") in {"ready", "failed"}
        ):
            continue
        item["startup_status"] = event["status"]
        item["updated_at"] = _now()
        if event["status"] == "ready":
            item["status"] = "running"
            item["ready_at"] = event.get("recorded_at") or _now()
        else:
            item["startup_error_code"] = str(
                event.get("error_code") or "local_start_failed"
            )[:80]
            item["startup_user_message"] = " ".join(str(
                event.get("user_message")
                or "这台设备未能完成运行准备；题目仍然保留。"
            ).split())[:500]
            item["startup_retryable"] = bool(event.get("retryable", True))
        changed = True
    if changed:
        _write_state(home, state)


def _settle_pool(
    home: Path,
    state: dict,
    processes: dict[str, subprocess.Popen],
    logs: dict[str, object],
    batch_id: str,
    returncode: int,
) -> None:
    """Persist one child exit without widening a run-plan device failure."""
    item = state["batches"][batch_id]
    requested_stop = item.get("status") == "stopping"
    startup_failed = item.get("startup_status") == "failed"
    run_plan_item = _is_run_plan_item(item)
    environment_build_failed = bool(
        returncode == ENVIRONMENT_BUILD_FAILED_EXIT_CODE
        and not startup_failed
        and not requested_stop
    )
    device_stop_warning: str | None = None
    if run_plan_item and (returncode != 0 or startup_failed) and not requested_stop:
        device_stop_warning = _stop_run_plan_device(
            item,
            (
                f"local startup failed ({item.get('startup_error_code')})"
                if startup_failed else
                "local isolated environment build failed before model start"
                if environment_build_failed else
                f"local runner exit code {returncode}"
            ),
        )
        if device_stop_warning:
            item.setdefault("warnings", []).append(device_stop_warning)
    elif item.get("refill") and returncode != 0 and not run_plan_item:
        warning = _stop_item_refill(
            item, batch_id,
            (
                "stopped by the machine-local Fleet"
                if requested_stop else
                f"local Fleet pool exited with code {returncode}"
            ),
        )
        if warning:
            item.setdefault("warnings", []).append(warning)
    user_interrupt = (
        run_plan_item
        and not startup_failed
        and returncode in {130, -signal.SIGINT}
    )
    clean_user_interrupt = (
        user_interrupt and not requested_stop and device_stop_warning is None
    )
    item["returncode"] = returncode
    item["status"] = (
        "stopped" if clean_user_interrupt else
        "interrupted" if requested_stop and run_plan_item and returncode != 0 else
        "interrupted" if user_interrupt else
        "stopped" if requested_stop else
        "failed" if startup_failed else
        "completed" if returncode == 0 else "failed"
    )
    if clean_user_interrupt:
        item["detail"] = "stopped by user; server acknowledged the device stop"
    elif (requested_stop and run_plan_item and returncode != 0) or user_interrupt:
        item["detail"] = (
            "active work stopped with a local recovery item still requiring attention"
        )
    elif environment_build_failed:
        item.update({
            "failure_kind": "environment_build_failed",
            "failure_state": "needs_attention",
            "retryable": True,
            "detail": (
                "The isolated task environment could not resolve its base image; "
                "the model did not start. Check Docker registry access and retry."
            ),
            "occurred_at": _now(),
        })
    item["updated_at"] = _now()
    log_handle = logs.pop(batch_id, None)
    if log_handle is not None:
        log_handle.close()
    processes.pop(batch_id, None)
    _write_state(home, state)


def _controller_loop(home: Path, state: dict) -> int:
    processes: dict[str, subprocess.Popen] = {}
    logs: dict[str, object] = {}
    stopping = False

    def request_stop(_signum=None, _frame=None):
        nonlocal stopping
        stopping = True
        for process in processes.values():
            _send_interrupt(process)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    last_activity = time.monotonic()
    last_heartbeat = 0.0
    try:
        while True:
            _process_inbox(home, state, processes, logs)
            _refresh_pool_startups(home, state, processes)
            for batch_id, process in list(processes.items()):
                returncode = process.poll()
                if returncode is None:
                    continue
                _settle_pool(
                    home, state, processes, logs, batch_id, returncode,
                )
                last_activity = time.monotonic()
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                _write_state(home, state)
                last_heartbeat = now
            if stopping and not processes:
                break
            if not processes and now - last_activity >= IDLE_EXIT_SECONDS:
                break
            time.sleep(0.1)
    finally:
        for process in processes.values():
            _send_interrupt(process)
        deadline = time.monotonic() + 15
        while processes and time.monotonic() < deadline:
            for batch_id, process in list(processes.items()):
                if process.poll() is not None:
                    del processes[batch_id]
            time.sleep(0.1)
        for process in processes.values():
            try:
                process.terminate()
            except OSError:
                pass
        for handle in logs.values():
            handle.close()
        state["status"] = "stopped"
        _write_state(home, state)
    return 0


def cmd_fleet_serve(args) -> int:
    launch_id = os.environ.get(_LAUNCH_ID_ENV)
    if not getattr(args, "internal", False) or not launch_id:
        raise SystemExit("Fleet serve is an internal command")
    _prepare_dirs(HOME)
    acquire_run_lock(HOME)
    # Do not run the historical orphan sweep here. Older `--parallel`
    # sessions deliberately did not hold run.lock, so a newly started Fleet
    # cannot prove that a Pier-shaped container below this HOME is abandoned
    # rather than live work from one of those compatible sessions. The pool
    # watchdog and inherited run.lock protect all Fleet-owned containers from
    # this point forward; uncertain pre-Fleet containers are left untouched.
    with _controller_lease(HOME, launch_id):
        previous = _read_json(_state_path(HOME))
        state = _initial_state(launch_id, previous)
        state["status"] = "active"
        _write_state(HOME, state)
        # One coordinator, one startup replay. Batch pool parents skip this shared
        # ledger pass so two Honeypots cannot race the same durable upload.
        from .runloop import _retry_pending_uploads

        try:
            cfg = _load_config()
        except SystemExit:
            # Plan-only devices have no need for the ordinary account config. The
            # exact credentials file arrives with each add request; startup replay
            # is simply unavailable until the unrelated config is repaired.
            cfg = {}
        if cfg.get("server") and cfg.get("token"):
            _retry_pending_uploads(_client(cfg))
        return _controller_loop(HOME, state)


def _print_add_response(response: dict) -> int:
    if not response.get("ok"):
        raise SystemExit(f"Fleet add failed: {response.get('error', 'unknown error')}")
    batch = response.get("batch") or {}
    batch_id = batch.get("batch_id", "?")
    if response.get("already_active"):
        print(
            f"batch {batch_id} is already active in this machine's Fleet "
            f"with {batch.get('workers', '?')} worker(s); no duplicate was started"
        )
    elif response.get("already_settled"):
        print(
            f"batch {batch_id} already has local Fleet status "
            f"{batch.get('status')}; no process was started. Review it and pass "
            "--retry only when resume is intentional"
        )
    elif batch.get("status") == "starting":
        print(
            f"preparing batch {batch_id} on this machine with "
            f"{batch.get('workers')} worker(s); no task has been reported as "
            "started yet"
        )
    else:
        print(
            f"added batch {batch_id} to this machine's Fleet with "
            f"{batch.get('workers')} worker(s)"
        )
    for warning in batch.get("warnings") or []:
        print(f"warning: {warning}")
    print(f"Fleet total: {response.get('total_workers', 0)} worker(s)")
    print(f"watch: dradar fleet watch --batch-id {batch_id}")
    return 0


def cmd_fleet_add(args) -> int:
    if args.refill and (
        args.max_tasks is None
        or args.refill_harness is None
        or args.refill_model is None
        or args.refill_effort is None
    ):
        raise SystemExit(
            "Fleet refill requires --max-tasks plus exact --refill-harness, "
            "--refill-model, and --refill-effort"
        )
    if not args.refill and any(
        value is not None for value in (
            args.max_tasks, args.refill_harness, args.refill_model,
            args.refill_effort,
        )
    ):
        raise SystemExit("Fleet refill limits and scope require --refill")
    try:
        response = add_batch(
            batch_id=args.batch_id,
            workers=args.workers,
            retry=bool(getattr(args, "retry", False)),
            refill=bool(args.refill),
            max_tasks=args.max_tasks,
            refill_harness=args.refill_harness,
            refill_model=args.refill_model,
            refill_effort=args.refill_effort,
        )
    except FleetError as exc:
        raise SystemExit(str(exc)) from exc
    return _print_add_response(response)


def _observe_pool_startup(response: dict, batch_id: str) -> dict:
    """Wait for a bounded, truthful ready-or-failed startup result."""

    if response.get("already_settled"):
        return response
    batch = response.get("batch") or {}
    if batch.get("status") != "starting":
        return response
    deadline = time.monotonic() + STARTUP_OBSERVE_SECONDS
    latest = batch
    while time.monotonic() < deadline:
        observed = batch_status(batch_id)
        if isinstance(observed, dict):
            latest = observed
            if (
                observed.get("status") != "starting"
                or observed.get("startup_status") == "failed"
            ):
                break
        time.sleep(0.05)
    # The loop's last value may predate a ready event written at the deadline.
    # Re-read before any destructive action; the controller then performs the
    # same pending-only condition under the startup transition lock.
    refreshed = batch_status(batch_id)
    if isinstance(refreshed, dict):
        latest = refreshed
    status = latest.get("status")
    startup_status = latest.get("startup_status")
    if status == "starting" and startup_status == "pending":
        try:
            stopped = stop_batch(
                batch_id, only_if_startup_pending=True,
            )
        except FleetError as exc:
            raise FleetStartupError(
                "local_start_timeout_stop_unconfirmed",
                "本地启动观察已超时，但无法确认本地监督器已经停止。"
                "请先运行 `dradar fleet status` 检查这次运行；若仍在启动或运行，"
                "请用 `dradar fleet stop` 安全停止后再重试。题目仍然保留。",
                retryable=False,
            ) from exc
        if batch_id in (stopped.get("stopping") or []):
            raise FleetStartupError(
                "local_start_timeout",
                "这台设备在限定时间内没有任何 worker 完成注册并开始首个题目；"
                "本机协调器已确认停止请求，正在安全停止，题目仍然保留。",
                retryable=True,
            )
        if batch_id in (stopped.get("condition_changed") or []):
            final = batch_status(batch_id)
            if isinstance(final, dict):
                latest = final
                status = latest.get("status")
                startup_status = latest.get("startup_status")
            if startup_status == "ready" or status == "running":
                updated = dict(response)
                updated["batch"] = latest
                return updated
            if startup_status != "failed":
                raise FleetStartupError(
                    "local_start_state_changed",
                    "本地启动状态在超时停止前发生了变化，因此没有发送停止请求。"
                    "请运行 `dradar fleet status` 确认当前状态后再决定是否重试。",
                    retryable=False,
                )
        else:
            raise FleetStartupError(
                "local_start_timeout_stop_unconfirmed",
                "本地启动观察已超时，但协调器没有确认停止任何进程。"
                "请先运行 `dradar fleet status` 检查这次运行；若仍在启动或运行，"
                "请用 `dradar fleet stop` 安全停止后再重试。题目仍然保留。",
                retryable=False,
            )
    updated = dict(response)
    updated["batch"] = latest
    failed_after_ready = (
        status in {"failed", "interrupted"} and startup_status == "ready"
    )
    if startup_status == "failed" or status in {"failed", "interrupted"} or (
        status == "completed" and startup_status != "ready"
    ):
        raise FleetStartupError(
            str(
                latest.get("startup_error_code")
                or (
                    "local_runner_interrupted"
                    if failed_after_ready else "local_start_failed"
                )
            ),
            str(
                latest.get("startup_user_message")
                or (
                    "这台设备刚开始运行后即中断；题目仍然保留，请检查本机后重试。"
                    if failed_after_ready else
                    "这台设备未能完成运行准备；题目仍然保留，请检查本机后重试。"
                )
            ),
            retryable=bool(latest.get("startup_retryable", True)),
        )
    return updated


def add_batch(
    *,
    batch_id: str,
    workers: int | str = "auto",
    retry: bool = False,
    refill: bool = False,
    max_tasks: int | None = None,
    refill_harness: str | None = None,
    refill_model: str | None = None,
    refill_effort: str | None = None,
    credentials_file: Path | str | None = None,
    plan_id: str | None = None,
) -> dict:
    """Programmatic, idempotent Fleet add used by the intent-level CLI."""
    try:
        normalized = normalize_batch_id(batch_id)
    except ValueError as exc:
        raise FleetError(str(exc)) from exc
    if normalized is None:
        raise FleetError("an exact batch is required")
    prepare_new_batch_runtime()
    response = _request("add", {
        "batch_id": normalized,
        "workers": workers,
        "retry": retry,
        "refill": refill,
        "max_tasks": max_tasks,
        "refill_harness": refill_harness,
        "refill_model": refill_model,
        "refill_effort": refill_effort,
        "credentials_file": str(credentials_file) if credentials_file else None,
        "plan_id": plan_id,
    })
    if not response.get("ok"):
        raise FleetError(str(response.get("error") or "local coordinator rejected the run"))
    return _observe_pool_startup(response, normalized)


def _public_state(home: Path = HOME) -> dict:
    state = _read_json(_state_path(home))
    if not state:
        return {"active": False, "status": "absent", "batches": {}}
    public = dict(state)
    active = controller_is_active(home)
    public["active"] = active
    if not active:
        # Persisted state is historical unless the controller's process-lifetime
        # lease proves it is live. Never let a crashed controller reserve workers
        # or make run-plan recovery believe a phantom pool is still running.
        batches = {}
        orphaned_workers = 0
        for batch_id, raw in (state.get("batches") or {}).items():
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            if item.get("status") in {"starting", "running", "stopping"}:
                try:
                    normalized = normalize_batch_id(batch_id)
                except ValueError:
                    normalized = None
                if normalized and _lock_is_held(_pool_lock_path(home, normalized)):
                    item["status"] = "orphaned"
                    item["detail"] = (
                        "pool is still winding down without its Fleet coordinator"
                    )
                    orphaned_workers += int(item.get("workers") or 0)
                else:
                    item["status"] = "interrupted"
                    item["detail"] = (
                        "Fleet coordinator is not active; safe retry is required"
                    )
            batches[batch_id] = item
        public["batches"] = batches
        public["total_workers"] = orphaned_workers
    return public


def credentials_file_in_use(path: Path | str, home: Path = HOME) -> bool:
    """Prove a run-plan file is referenced by a live or safely winding pool."""
    target = os.path.abspath(os.fspath(path))
    state = _read_json(_state_path(home))
    if not isinstance(state, dict):
        return False
    controller_live = controller_is_active(home)
    for batch_id, item in (state.get("batches") or {}).items():
        if (
            not isinstance(item, dict)
            or item.get("status") not in {"starting", "running", "stopping"}
            or not isinstance(item.get("credentials_file"), str)
            or os.path.abspath(item["credentials_file"]) != target
        ):
            continue
        # A pool owns its exact lock for process lifetime. This preserves the
        # credential during the watchdog's bounded shutdown even if its parent
        # controller has already crashed.
        try:
            normalized = normalize_batch_id(batch_id)
        except ValueError:
            normalized = None
        if controller_live or (
            normalized and _lock_is_held(_pool_lock_path(home, normalized))
        ):
            return True
    return False


def batch_status(batch_id: str, home: Path = HOME) -> dict | None:
    try:
        normalized = normalize_batch_id(batch_id)
    except ValueError:
        return None
    return (_public_state(home).get("batches") or {}).get(normalized)


def reserved_workers(home: Path = HOME, *, exclude_batch_id: str | None = None) -> int:
    state = _public_state(home)
    return sum(
        int(item.get("workers") or 0)
        for batch_id, item in _active_batches(state).items()
        if batch_id != exclude_batch_id
    )


def cmd_fleet_status(args) -> int:
    state = _public_state()
    if getattr(args, "json", False):
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0
    if not state.get("active"):
        print("no active local DRadar Fleet")
    else:
        print(
            f"local DRadar Fleet: {state.get('total_workers', 0)} worker(s), "
            f"controller pid {state.get('pid')}"
        )
    batches = state.get("batches") or {}
    if not batches:
        print("  no batches recorded")
        return 0
    for batch_id, item in sorted(batches.items()):
        suffix = ""
        if item.get("returncode") is not None:
            suffix = f" (exit {item['returncode']})"
        print(
            f"  {batch_id}  {item.get('status', '?'):11s}  "
            f"workers={item.get('workers', '?')}"
            + (
                f"  refill={item.get('max_tasks', '?')} total"
                if item.get("refill") else ""
            )
            + suffix
        )
        if item.get("failure_state") == "needs_attention" and item.get("detail"):
            print(f"    needs attention: {item['detail']}")
    return 0


def cmd_fleet_watch(args) -> int:
    batch_id = args.batch_id
    shown = 0
    while True:
        state = _public_state()
        item = (state.get("batches") or {}).get(batch_id)
        if item is None:
            raise SystemExit(f"batch {batch_id} is not recorded in the local Fleet")
        log_path = Path(item.get("log_path") or "")
        try:
            with log_path.open("rb") as handle:
                handle.seek(shown)
                chunk = handle.read()
                shown = handle.tell()
        except OSError:
            chunk = b""
        if chunk:
            print(chunk.decode("utf-8", errors="replace"), end="", flush=True)
        if item.get("status") not in {"starting", "running", "stopping"}:
            print(f"\nFleet batch {batch_id}: {item.get('status')}")
            return 0 if item.get("status") == "completed" else 1
        if not state.get("active"):
            raise SystemExit("Fleet coordinator stopped while this batch was active")
        time.sleep(0.5)


def cmd_fleet_stop(args) -> int:
    if not args.all and not args.batch_id:
        raise SystemExit("fleet stop requires --batch-id ID or --all")
    if not controller_is_active():
        print("no active local DRadar Fleet")
        return 0
    try:
        response = stop_batch(args.batch_id, all_batches=bool(args.all))
    except FleetError as exc:
        raise SystemExit(str(exc)) from exc
    if not response.get("ok"):
        raise SystemExit(f"Fleet stop failed: {response.get('error', 'unknown error')}")
    stopping = response.get("stopping") or []
    if stopping:
        print("stopping Fleet batch(es) safely: " + ", ".join(stopping))
    else:
        print("no matching active Fleet batch")
    for warning in response.get("warnings") or []:
        print(f"warning: {warning}")
    return 0


def stop_batch(
    batch_id: str | None = None,
    *,
    all_batches: bool = False,
    only_if_startup_pending: bool = False,
) -> dict:
    if not all_batches and not batch_id:
        raise FleetError("an exact batch is required")
    if not controller_is_active():
        return {"ok": True, "stopping": [], "warnings": []}
    response = _request("stop", {
        "batch_id": batch_id,
        "all": all_batches,
        "only_if_startup_pending": bool(only_if_startup_pending),
    })
    if not response.get("ok"):
        raise FleetError(str(response.get("error") or "local coordinator rejected the stop"))
    return response


__all__ = [
    "FleetError",
    "add_batch",
    "acquire_pool_lock",
    "cmd_fleet_add",
    "cmd_fleet_serve",
    "cmd_fleet_status",
    "cmd_fleet_stop",
    "cmd_fleet_watch",
    "batch_status",
    "controller_matches",
    "controller_is_active",
    "credentials_file_in_use",
    "preparation_lock",
    "reserved_workers",
    "stop_batch",
    "start_pool_watchdog",
]
