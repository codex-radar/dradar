"""User-intent run-plan commands.

The web gives an Agent a short-lived, high-entropy run code.  This module
exchanges it in an HTTPS request body for a plan-scoped ``drp_`` credential,
persists that credential in a private file, and connects the versioned server
decision protocol to the existing exact-batch Fleet coordinator.

The short-lived run code is necessarily present in the initial Agent command's
``--plan`` argument.  It is never placed in a URL, persisted in plaintext, or
forwarded to Fleet/worker arguments.  The exchanged ``drp_`` access token has
the stricter boundary: it is never printed or placed in any process argument.
Repeated commands locate private state by a one-way digest of the run code.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import secrets
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .api_client import ApiClient, ApiError, normalize_batch_id
from .local_config import HOME, _load_config


SCHEMA_VERSION = 1
DEFAULT_SERVER = "https://api.codexradar.com"
PLAN_DIR = "run-plans"
DEVICE_FILE = "device.json"
DEVICE_LOCK_FILE = "device.lock"
STATE_LOCK_FILE = "state.lock"
ADMISSION_LOCK_FILE = "admission.lock"
STATE_SUFFIX = ".json"
MAX_CREDENTIAL_STATES = 64
MAX_AUDIT_SUMMARIES = 64
AUDIT_RETENTION_SECONDS = 30 * 24 * 60 * 60
_SAFE_PLAN_ID = re.compile(r"[A-Za-z0-9_-]{8,160}\Z")
_JSON_STDOUT_LOCK = threading.RLock()
_STALE_SERVER_DECISION_CODES = {
    "decision_already_used",
    "decision_invalid_or_state_changed",
}
_TRUSTED_GIT_INSTALL_PATHS = {
    "/SecurityMind/dradar",
    "/codex-radar/dradar",
}


class RunPlanClientError(RuntimeError):
    def __init__(
        self,
        code: str,
        user_message: str,
        *,
        retryable: bool = False,
        agent_action: str | None = None,
        agent_details: dict[str, Any] | None = None,
    ):
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.retryable = retryable
        self.agent_action = agent_action or ("retry" if retryable else "stop")
        self.agent_details = agent_details


def _private_dir(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise RunPlanClientError(
            "local_state_unsafe",
            "本机运行信息目录不安全；请修复目录权限后重试。",
        )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _private_dir(path.parent)
    fd, raw = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
    )
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_private_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        stat = path.stat()
        if os.name != "nt" and stat.st_mode & 0o077:
            return None
        if (
            os.name != "nt" and hasattr(os, "getuid")
            and stat.st_uid != os.getuid()
        ):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


@contextmanager
def _exclusive_lock(path: Path):
    """Serialize first-use identity/state creation across Agent processes."""
    _private_dir(path.parent)
    handle = open(path, "a+", encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":  # pragma: no cover
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _root(home: Path = HOME) -> Path:
    return home / PLAN_DIR


def _device_path(home: Path = HOME) -> Path:
    return _root(home) / DEVICE_FILE


def stable_device(home: Path = HOME) -> tuple[str, str]:
    """Return a random stable ID and a privacy-preserving display label."""
    path = _device_path(home)
    with _exclusive_lock(_root(home) / DEVICE_LOCK_FILE):
        # Re-read only after taking the lock. Two conversations can enter on a
        # brand-new machine at the same instant, but only one identity may win.
        saved = _read_private_json(path)
        if saved and saved.get("schema_version") == SCHEMA_VERSION:
            device_id = saved.get("device_id")
            device_name = saved.get("device_name")
            if (
                isinstance(device_id, str) and device_id.startswith("drv_")
                and 20 <= len(device_id) <= 100
                and isinstance(device_name, str) and device_name
            ):
                return device_id, device_name
        family = {
            "Darwin": "这台 Mac",
            "Windows": "这台 Windows 设备",
            "Linux": "这台 Linux 设备",
        }.get(platform.system(), "这台设备")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "device_id": "drv_" + secrets.token_urlsafe(24),
            "device_name": family,
        }
        _atomic_json(path, payload)
        return payload["device_id"], payload["device_name"]


def _run_code_digest(run_code: str) -> str:
    return hashlib.sha256(
        b"dradar:local-run-code-v1:" + run_code.encode("utf-8"),
    ).hexdigest()


def _validate_run_code(value: object) -> str:
    if not isinstance(value, str):
        raise RunPlanClientError("run_code_invalid", "网页复制的运行信息无效，请回网页重新复制。")
    code = value.strip()
    if code != value or not 8 <= len(code) <= 256 or any(ch.isspace() for ch in code):
        raise RunPlanClientError("run_code_invalid", "网页复制的运行信息无效，请回网页重新复制。")
    return code


def validate_server_url(value: str) -> str:
    """Allow production HTTPS and loopback HTTP used by local development."""
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower()
        loopback = host in {"localhost", "127.0.0.1", "::1"}
        valid_scheme = parsed.scheme == "https" or (parsed.scheme == "http" and loopback)
        if (
            not valid_scheme or not host or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError
        # Touching port makes malformed ports fail here rather than inside httpx.
        parsed.port
    except (AttributeError, TypeError, ValueError):
        raise RunPlanClientError(
            "server_url_invalid",
            "运行地址无效；请使用网页提供的 HTTPS 地址。",
        ) from None
    netloc = parsed.netloc
    return urlunsplit((parsed.scheme, netloc, "", "", "")).rstrip("/")


def _state_path(plan_id: str, home: Path = HOME) -> Path:
    if not isinstance(plan_id, str) or not _SAFE_PLAN_ID.fullmatch(plan_id):
        raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。")
    return _root(home) / f"plan-{plan_id}{STATE_SUFFIX}"


def _iter_states(home: Path = HOME):
    root = _root(home)
    if root.is_symlink() or not root.is_dir():
        return
    for path in sorted(root.glob(f"plan-*{STATE_SUFFIX}")):
        state = _read_private_json(path)
        if (
            state and state.get("schema_version") == SCHEMA_VERSION
            and state.get("credential_kind") == "run_plan_v1"
        ):
            yield path, state


def _expiry_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _state_expired(state: dict[str, Any], *, now: float) -> bool:
    plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
    expiries = [
        value for value in (
            _expiry_timestamp(state.get("access_expires_at")),
            _expiry_timestamp(plan.get("expires_at")),
        )
        if value is not None
    ]
    return bool(expiries and min(expiries) <= now)


def _state_in_active_fleet(path: Path, *, home: Path = HOME) -> bool:
    """Do not scrub a credential while any live/orphan-safe pool holds it."""
    try:
        from . import fleet

        return fleet.credentials_file_in_use(path, home=home)
    except (OSError, ValueError):
        # A failed liveness probe is not proof that deletion is safe.
        return True


def _scrub_state(path: Path, state: dict[str, Any], *, reason: str, now: float) -> None:
    """Replace credentials with a bounded, non-secret local audit summary."""
    summary = {
        "schema_version": SCHEMA_VERSION,
        "credential_kind": "run_plan_expired_summary_v1",
        "plan_id": state.get("plan_id"),
        "server": state.get("server"),
        "expired_at": datetime.fromtimestamp(now).astimezone().isoformat(),
        "cleanup_reason": reason,
    }
    _atomic_json(path, summary)


def _cleanup_states(home: Path = HOME) -> None:
    """Bound inactive local state without touching credentials used by Fleet."""
    root = _root(home)
    if root.is_symlink() or not root.is_dir():
        return
    now = time.time()
    credentials: list[tuple[float, Path, dict[str, Any]]] = []
    summaries: list[tuple[float, Path]] = []
    for path in sorted(root.glob(f"plan-*{STATE_SUFFIX}")):
        state = _read_private_json(path)
        if not state or state.get("schema_version") != SCHEMA_VERSION:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if state.get("credential_kind") == "run_plan_v1":
            if _state_in_active_fleet(path, home=home):
                continue
            if _state_expired(state, now=now):
                _scrub_state(path, state, reason="expired", now=now)
                summaries.append((now, path))
            else:
                credentials.append((modified, path, state))
        elif state.get("credential_kind") == "run_plan_expired_summary_v1":
            summaries.append((modified, path))

    # Short-lived plan credentials are useful, but an inactive machine should
    # not become an unbounded token archive. Keep the newest bounded set.
    credentials.sort(reverse=True, key=lambda item: item[0])
    for _modified, path, state in credentials[MAX_CREDENTIAL_STATES:]:
        if _state_in_active_fleet(path, home=home):
            continue
        _scrub_state(path, state, reason="inactive_state_limit", now=now)
        summaries.append((now, path))

    summaries.sort(reverse=True, key=lambda item: item[0])
    for index, (modified, path) in enumerate(summaries):
        too_old = now - modified > AUDIT_RETENTION_SECONDS
        over_limit = index >= MAX_AUDIT_SUMMARIES
        if too_old or over_limit:
            path.unlink(missing_ok=True)


def _saved_state(run_code: str, home: Path = HOME) -> tuple[Path, dict[str, Any]] | None:
    _cleanup_states(home)
    digest = _run_code_digest(run_code)
    for path, state in _iter_states(home) or ():
        if _state_expired(state, now=time.time()):
            # Active Fleet credentials may still need to remain on disk for a
            # graceful close, but an interactive command must never reuse them.
            continue
        if secrets.compare_digest(str(state.get("run_code_hash") or ""), digest):
            return path, state
    return None


def _resolve_server(
    explicit: str | None,
    saved: tuple[Path, dict[str, Any]] | None,
) -> str:
    saved_server = None
    if saved is not None:
        raw = saved[1].get("server")
        if isinstance(raw, str):
            saved_server = validate_server_url(raw)
    if explicit:
        selected = validate_server_url(explicit)
        if saved_server and selected != saved_server:
            raise RunPlanClientError(
                "server_scope_mismatch",
                "这次运行属于另一个站点，请使用网页复制的原命令。",
            )
        return selected
    if saved_server:
        return saved_server
    cfg = _load_config()
    if isinstance(cfg.get("server"), str) and cfg["server"]:
        return validate_server_url(cfg["server"])
    return DEFAULT_SERVER


def _validate_envelope(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RunPlanClientError("protocol_invalid", "服务返回的信息不完整，请升级后重试。")
    required = {
        "status", "interaction", "decision_required", "user_message",
        "agent_action", "error_code", "retryable", "choices",
    }
    if not required.issubset(value):
        raise RunPlanClientError("protocol_invalid", "服务返回的信息不完整，请升级后重试。")
    if value.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise RunPlanClientError("schema_version_unsupported", "运行协议版本不兼容，请升级后重试。")
    return value


def _validate_response(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise RunPlanClientError("schema_version_unsupported", "运行协议版本不兼容，请升级后重试。")
    _validate_envelope(value.get("envelope"))
    return value


def _validate_plan(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("plan_version") != 1
    ):
        raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。")
    required_strings = ("plan_id", "batch_id", "benchmark_id", "harness")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required_strings):
        raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。")
    if value.get("points_tier") not in {"plus", "pro-5x", "pro-20x"}:
        raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。")
    try:
        normalize_batch_id(value["batch_id"])
    except ValueError:
        raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。") from None
    assignments = value.get("assignments")
    if not isinstance(assignments, list) or not assignments:
        raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。")
    for assignment in assignments:
        if not isinstance(assignment, dict) or any(
            not isinstance(assignment.get(key), str) or not assignment[key]
            for key in ("assignment_id", "task_id", "model", "effort")
        ):
            raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。")
        provider = assignment.get("provider")
        if provider is not None and (not isinstance(provider, str) or not provider):
            raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。")
    concurrency = value.get("concurrency")
    refill = value.get("refill")
    if not isinstance(concurrency, dict) or not isinstance(refill, dict):
        raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。")
    mode = concurrency.get("mode")
    configured = concurrency.get("value")
    if (
        mode not in {"auto", "fixed"}
        or (mode == "auto" and configured is not None)
        or (
            mode == "fixed" and (
                not isinstance(configured, int) or isinstance(configured, bool)
                or not 1 <= configured <= 40
            )
        )
    ):
        raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。")
    enabled = refill.get("enabled")
    refill_to = refill.get("refill_to")
    max_tasks = refill.get("max_tasks")
    def valid_positive(item: object) -> bool:
        return isinstance(item, int) and not isinstance(item, bool) and item >= 1
    if not isinstance(enabled, bool):
        raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。")
    if enabled:
        if (
            not valid_positive(max_tasks)
            or max_tasks < len(assignments)
            or (refill_to is not None and not valid_positive(refill_to))
            or (refill_to is not None and refill_to > max_tasks)
        ):
            raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。")
    elif refill_to is not None or max_tasks is not None:
        raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。")
    _state_path(value["plan_id"])
    return value


def _exchange(
    run_code: str,
    server: str,
    *,
    home: Path = HOME,
) -> tuple[Path, dict[str, Any]]:
    device_id, device_name = stable_device(home)
    response = ApiClient(server, "").exchange_run_plan(
        run_code=run_code,
        device_id=device_id,
        device_name=device_name,
    )
    if not isinstance(response, dict) or response.get("schema_version") != SCHEMA_VERSION:
        raise RunPlanClientError("schema_version_unsupported", "运行协议版本不兼容，请升级后重试。")
    token = response.get("plan_access_token")
    if not isinstance(token, str) or not token.startswith("drp_") or len(token) > 512:
        raise RunPlanClientError("plan_response_invalid", "运行信息无效，请回网页重新复制。")
    plan = _validate_plan(response.get("plan"))
    if response.get("envelope") is not None:
        _validate_envelope(response["envelope"])
    state = {
        "schema_version": SCHEMA_VERSION,
        "credential_kind": "run_plan_v1",
        "server": server,
        "token": token,
        "access_expires_at": response.get("access_expires_at"),
        "run_code_hash": _run_code_digest(run_code),
        "plan": plan,
        "plan_id": plan["plan_id"],
        "benchmark": plan["benchmark_id"],
        "batch_id": plan["batch_id"],
        "logical_session_id": "drl_" + secrets.token_urlsafe(24),
        "identity": response.get("identity") if isinstance(response.get("identity"), dict) else {},
        "limits": response.get("limits") if isinstance(response.get("limits"), dict) else {},
        "pending_decision": None,
        "pending_local_capacity": None,
        "pending_docker_install": None,
        "intent_generation": 0,
        "pending_recheck_generation": None,
        "authorized_concurrency": None,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    path = _state_path(plan["plan_id"], home)
    _atomic_json(path, state)
    _cleanup_states(home)
    return path, state


def _state_and_client(args) -> tuple[str, Path, dict[str, Any], ApiClient]:
    run_code = _validate_run_code(args.plan)
    # Progress/run in two conversations can race on first use. Serialize the
    # exchange and re-read state under the lock so only one logical session is
    # minted for this device.
    with _exclusive_lock(_root(HOME) / STATE_LOCK_FILE):
        saved = _saved_state(run_code, home=HOME)
        server = _resolve_server(getattr(args, "server", None), saved)
        if saved is None:
            path, state = _exchange(run_code, server, home=HOME)
        else:
            path, state = saved
    token = state.get("token")
    plan = state.get("plan")
    if not isinstance(token, str) or not token.startswith("drp_"):
        raise RunPlanClientError("credential_invalid", "本机运行权限无效，请回网页重新复制。")
    _validate_plan(plan)
    client = ApiClient(server, token, benchmark_id=plan["benchmark_id"], batch_id=plan["batch_id"])
    return run_code, path, state, client


def _concurrency(
    plan: dict[str, Any], requested: int | str | None,
) -> tuple[str, int | None, int | str]:
    configured = plan["concurrency"]
    plan_mode = configured.get("mode")
    plan_value = configured.get("value")
    selected = requested
    if selected is None:
        selected = "auto" if plan_mode == "auto" else plan_value
    if selected == "auto":
        if plan_mode == "fixed":
            # Auto may safely use fewer local slots than the fixed ceiling.
            return "auto", None, "auto"
        return "auto", None, "auto"
    try:
        workers = int(selected)
    except (TypeError, ValueError):
        raise RunPlanClientError("concurrency_invalid", "同时运行数量无效。") from None
    if not 1 <= workers <= 40:
        raise RunPlanClientError("concurrency_invalid", "同时运行数量必须在 1 到 40 之间。")
    if plan_mode == "fixed" and isinstance(plan_value, int) and workers > plan_value:
        raise RunPlanClientError(
            "concurrency_not_allowed",
            "这个数量超过网页为本次运行设置的范围，请保持原设置。",
        )
    return "fixed", workers, workers


def _capacity_snapshot(
    client: ApiClient,
    plan: dict[str, Any],
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from . import fleet
    from .capacity import AUTO_WORKER_CAP, inspect_capacity

    refill = plan["refill"]
    requested_tasks = max(
        len(plan["assignments"]),
        int(refill.get("refill_to") or 0),
        int(refill.get("max_tasks") or 0) if refill.get("enabled") else 0,
    )
    class PlanCapacityView:
        def whoami(self):
            return client.whoami()

        def get_assignment(self):
            try:
                return client.get_assignment()
            except ApiError as exc:
                if exc.status_code == 404:
                    # The server start state machine owns the authoritative
                    # no-remaining/future-budget decision. This view is used
                    # only after a private credential and its exact plan batch
                    # have been validated, so both the newer structured error
                    # and the older bare "active batch not found" 404 safely
                    # mean zero current inventory here. Ordinary resume keeps
                    # its stricter 404 behavior.
                    return {"active": []}
                raise

    report = inspect_capacity(
        PlanCapacityView(), requested_tasks=requested_tasks,
    )
    reserved = fleet.reserved_workers(exclude_batch_id=plan["batch_id"])
    limits = limits if isinstance(limits, dict) else {}
    server_limits = [
        int(value) for value in (
            limits.get("account_concurrency"),
            limits.get("account_claim_limit") if refill.get("enabled") else None,
            limits.get("plan_task_limit"),
        )
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1
    ]
    account_limit = min([report.account_limit, *server_limits])
    safe_total = min(
        report.cpu_limit, report.memory_limit, report.disk_limit,
        account_limit,
    )
    available = max(0, safe_total - reserved)
    auto_available = min(available, AUTO_WORKER_CAP)
    # A continuation plan may initially contain fewer seed assignments than
    # the safe queue target.  Its max_tasks authorization supplies future work,
    # so use that bounded target rather than permanently pinning the pool to the
    # seed count.  Without continuation, exact inventory remains the hard cap.
    supply = requested_tasks
    auto_workers = min(supply, requested_tasks, auto_available)
    facts = {
        "safe_total": safe_total,
        "reserved_by_other_runs": reserved,
        "available": available,
        "auto_workers": auto_workers,
        "docker_cpus": report.docker_cpus,
        "docker_memory_gib": report.docker_memory_gib,
        "disk_limit": report.disk_limit,
        "account_limit": account_limit,
        "held_tasks": report.held_tasks,
        "automatic_cap": AUTO_WORKER_CAP,
    }
    facts["digest"] = hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    return facts


def _local_capacity_response(
    path: Path,
    state: dict[str, Any],
    *,
    requested: int,
    recommended: int,
    snapshot: dict[str, Any],
    decision: str = "local_capacity",
    allow_keep: bool = True,
    user_message: str | None = None,
    server_status: dict[str, Any] | None = None,
    server_capacity: dict[str, Any] | None = None,
    bound_server_decision: str | None = None,
    bound_server_decision_token: str | None = None,
) -> dict[str, Any]:
    token = "drlc_" + secrets.token_urlsafe(24)
    if bound_server_decision_token:
        encoded = base64.urlsafe_b64encode(
            bound_server_decision_token.encode("utf-8"),
        ).decode("ascii").rstrip("=")
        token += "." + encoded
    state["pending_local_capacity"] = {
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "requested": requested,
        "recommended": recommended,
        "capacity_digest": snapshot["digest"],
        "decision": decision,
        "allow_keep": allow_keep,
        "bound_server_decision": bound_server_decision,
        "bound_server_token_hash": (
            hashlib.sha256(bound_server_decision_token.encode()).hexdigest()
            if bound_server_decision_token else None
        ),
        "expires_at": time.time() + 5 * 60,
    }
    _atomic_json(path, state)
    choices = []
    if recommended >= 1:
        choices.append({
            "id": "use_recommended",
            "label": f"按建议同时运行 {recommended} 道",
        })
    if allow_keep:
        choices.append({
            "id": "keep_requested",
            "label": f"仍然同时启动 {requested} 道",
        })
    choices.append({"id": "cancel", "label": "暂不启动"})
    agent = {
        "plan": state["plan"],
        "requested_concurrency": requested,
        "recommended_concurrency": recommended,
    }
    if server_status is not None:
        agent["server_status"] = server_status
    if server_capacity is not None:
        agent["server_capacity"] = server_capacity
    choice_actions = {}
    for choice in choices:
        choice_id = choice["id"]
        if choice_id == "cancel":
            choice_actions[choice_id] = {"mode": "no_command", "args": []}
            continue
        workers = recommended if choice_id == "use_recommended" else requested
        choice_actions[choice_id] = {
            "mode": "replay_current_command_with_args",
            "args": ["--concurrency", str(workers), "--decision-token", token],
        }
    agent["choice_actions"] = choice_actions
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "decision_required",
        "interaction": "confirm",
        "decision_required": True,
        "decision": decision,
        "decision_token": token,
        "user_message": user_message or (
            f"根据这台设备当前的负载，建议这次同时运行 {recommended} 道；"
            f"你原来选择了 {requested} 道。请选择如何运行。"
            if recommended >= 1 else
            f"这台设备正在运行其他题目。继续同时启动这 {requested} 道可能会让"
            "机器变慢，是否仍然启动？"
        ),
        "agent_action": "ask_user",
        "error_code": None,
        "retryable": False,
        "choices": choices,
        "agent": agent,
    }


def _consume_local_capacity(
    path: Path,
    state: dict[str, Any],
    *,
    token: str,
    selected: object,
    snapshot: dict[str, Any],
) -> tuple[int, str | None, str | None]:
    pending = state.get("pending_local_capacity")
    token_valid = (
        isinstance(pending, dict)
        and isinstance(token, str) and token.startswith("drlc_")
        and secrets.compare_digest(
            str(pending.get("token_hash") or ""),
            hashlib.sha256(token.encode()).hexdigest(),
        )
        and float(pending.get("expires_at") or 0) > time.time()
    )
    try:
        workers = int(selected)
    except (TypeError, ValueError):
        workers = 0
    allowed = (
        {int(pending.get("recommended") or 0)}
        | (
            {int(pending.get("requested") or 0)}
            if pending.get("allow_keep") else set()
        )
        if isinstance(pending, dict) else set()
    )
    # Docker/task telemetry can change while a person is answering even when
    # the recommendation has not become less safe (for example, the first
    # sibling Harness finishes preparing its container).  Do not trap the
    # user in a confirmation loop for non-worsening churn.  A changed snapshot
    # is accepted only when the selected recommendation is still safe, or when
    # an explicit over-capacity choice is no worse than the situation the user
    # already approved.  A genuinely lower recommendation still requires a
    # fresh confirmation.
    current_available = snapshot.get("available")
    pending_requested = pending.get("requested") if isinstance(pending, dict) else None
    pending_recommended = (
        pending.get("recommended") if isinstance(pending, dict) else None
    )
    comparable_capacity = all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (
            current_available, pending_requested, pending_recommended,
        )
    )
    capacity_not_worse = bool(
        comparable_capacity
        and (
            (workers == pending_recommended and current_available >= workers)
            or (
                bool(pending.get("allow_keep"))
                and workers == pending_requested
                and min(pending_requested, current_available)
                >= pending_recommended
            )
        )
    )
    valid = bool(
        token_valid
        and (
            pending.get("capacity_digest") == snapshot["digest"]
            or capacity_not_worse
        )
    )
    if not valid or workers not in allowed or workers < 1:
        state["pending_local_capacity"] = None
        _atomic_json(path, state)
        raise RunPlanClientError(
            "decision_invalid_or_capacity_changed",
            "本机资源状态已经变化，我会重新检查后再请你确认。",
            retryable=True,
        )
    state["pending_local_capacity"] = None
    state["authorized_concurrency"] = {
        "workers": workers,
        "capacity_digest": snapshot["digest"],
    }
    bound_decision = pending.get("bound_server_decision")
    bound_token = None
    encoded = token.partition(".")[2]
    if encoded:
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            bound_token = base64.urlsafe_b64decode(padded).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            bound_token = None
    expected_bound_hash = pending.get("bound_server_token_hash")
    if expected_bound_hash is not None and not (
        isinstance(bound_decision, str)
        and isinstance(bound_token, str)
        and secrets.compare_digest(
            str(expected_bound_hash), hashlib.sha256(bound_token.encode()).hexdigest(),
        )
    ):
        state["pending_local_capacity"] = None
        _atomic_json(path, state)
        raise RunPlanClientError(
            "decision_invalid_or_capacity_changed",
            "确认信息已经变化，我会重新检查后再请你确认。",
            retryable=True,
        )
    _atomic_json(path, state)
    return workers, bound_decision, bound_token


def _docker_install_binding(args) -> dict[str, Any]:
    """Bind an install approval to the exact replayable run arguments."""

    decision_token = getattr(args, "decision_token", None)
    return {
        "concurrency": getattr(args, "concurrency", None),
        "decision_token_hash": (
            hashlib.sha256(decision_token.encode()).hexdigest()
            if isinstance(decision_token, str) else None
        ),
    }


def _docker_install_response(
    path: Path,
    state: dict[str, Any],
    args,
    *,
    user_message: str,
) -> dict[str, Any]:
    """Ask once, without exposing an installer command or provider ID."""

    token = "drdi_" + secrets.token_urlsafe(24)
    state["pending_docker_install"] = {
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "intent_generation": _intent_generation(state),
        "binding": _docker_install_binding(args),
        "expires_at": time.time() + 10 * 60,
    }
    _atomic_json(path, state)
    install_action = (
        {
            "mode": "replay_plan_command",
            "command": "run",
            "args": ["--docker-install-token", token, "--json"],
            "inherit": ["--plan", "--server"],
            "interactive": False,
        }
        if getattr(args, "recheck_generation", None) is not None
        else {
            "mode": "replay_current_command_with_args",
            "args": ["--docker-install-token", token],
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "decision_required",
        "interaction": "confirm",
        "decision_required": True,
        "decision": "install_recommended_docker",
        "decision_token": token,
        "user_message": user_message,
        "agent_action": "ask_user",
        "error_code": "docker_install_confirmation_required",
        "retryable": False,
        "choices": [
            {"id": "install", "label": "安装推荐的 Docker 环境"},
            {"id": "cancel", "label": "暂不安装"},
        ],
        "agent": {
            "schema_version": SCHEMA_VERSION,
            "requires_user_action": True,
            "choice_actions": {
                "install": install_action,
                "cancel": {"mode": "no_command", "args": []},
            },
        },
    }


def _consume_docker_install(
    path: Path,
    state: dict[str, Any],
    args,
    token: object,
) -> None:
    """Consume exactly one user-approved install replay, failing closed."""

    pending = state.get("pending_docker_install")
    valid = bool(
        isinstance(pending, dict)
        and isinstance(token, str)
        and token.startswith("drdi_")
        and secrets.compare_digest(
            str(pending.get("token_hash") or ""),
            hashlib.sha256(token.encode()).hexdigest(),
        )
        and pending.get("intent_generation") == _intent_generation(state)
        and pending.get("binding") == _docker_install_binding(args)
        and float(pending.get("expires_at") or 0) > time.time()
    )
    state["pending_docker_install"] = None
    _atomic_json(path, state)
    if not valid:
        raise RunPlanClientError(
            "docker_install_decision_invalid",
            "Docker 安装确认已经失效；为避免未经许可安装，我会重新检查后再询问。",
            agent_action="notify_only",
        )


def _authorized_concurrency(
    state: dict[str, Any], selected: int, snapshot: dict[str, Any],
) -> bool:
    value = state.get("authorized_concurrency")
    return bool(
        isinstance(value, dict)
        and value.get("workers") == selected
        and value.get("capacity_digest") == snapshot["digest"]
    )


def _local_warn_response(
    server_response: dict[str, Any], *, selected: int,
) -> dict[str, Any]:
    result = _local_monitor_response(server_response, selected=selected)
    agent = dict(result.get("agent") or {})
    result.update({
        "status": "started",
        "interaction": "warn",
        "decision_required": False,
        "user_message": (
            f"这台设备当前适合同时运行 {selected} 道，系统已按这个数量开始，"
            "避免任务中断。无需操作。"
        ),
        "agent_action": "monitor",
        "error_code": None,
        "retryable": False,
        "choices": [],
        "agent": agent,
    })
    result.pop("decision", None)
    result.pop("decision_token", None)
    return result


def _local_monitor_response(
    server_response: dict[str, Any], *, selected: int,
) -> dict[str, Any]:
    """A successful local ensure changes start_runner into monitor."""
    result = _agent_response_from_server(server_response)
    server_status = {
        key: value for key, value in result.items()
        if key not in {"schema_version", "agent"}
    }
    agent = dict(result.get("agent") or {})
    agent["server_status"] = server_status
    agent["selected_concurrency"] = selected
    result["agent_action"] = "monitor"
    result["agent"] = agent
    return result


def _local_preparing_response(
    server_response: dict[str, Any], *, selected: int, adjusted: bool = False,
) -> dict[str, Any]:
    """Keep server admission distinct from a locally ready worker parent."""

    result = _local_monitor_response(server_response, selected=selected)
    agent = dict(result.get("agent") or {})
    agent["local_runner"] = {"status": "preparing"}
    if selected == 1:
        message = (
            "这台设备正在准备运行环境，稍后会逐个运行这次选择的题目。"
            "题目尚未开始执行。无需操作。"
        )
    else:
        message = (
            f"这台设备正在准备运行环境，稍后最多会同时运行 {selected} 道题。"
            "题目尚未开始执行。无需操作。"
        )
    if adjusted:
        if selected == 1:
            message = (
                "系统已根据这台设备的可用资源调整为逐个运行。"
                "正在准备运行环境，题目尚未开始执行。无需操作。"
            )
        else:
            message = (
                "系统已根据这台设备的可用资源调整为"
                f"最多同时运行 {selected} 道题。"
                "正在准备运行环境，题目尚未开始执行。无需操作。"
            )
    result.update({
        "status": "preparing",
        "interaction": "warn" if adjusted else "notify",
        "decision_required": False,
        "user_message": message,
        "agent_action": "monitor",
        "error_code": None,
        "retryable": False,
        "choices": [],
        "poll_after_seconds": 10,
        "user_message_policy": "on_change_or_heartbeat",
        "agent": agent,
    })
    result.pop("decision", None)
    result.pop("decision_token", None)
    return result


def _exact_pending_uploads(
    batch_id: str, client: ApiClient | None = None,
) -> list[dict[str, Any]]:
    """Read only durable completed results belonging to one exact plan batch."""

    from . import pending

    try:
        expected = normalize_batch_id(batch_id)
    except ValueError:
        return []
    expected_scope = None
    if (
        client is not None
        and getattr(client, "server", None)
        and getattr(client, "account_scope", None)
    ):
        expected_scope = pending.scope_fingerprint(
            server=client.server,
            account_scope=client.account_scope,
            benchmark_id=getattr(client, "benchmark_id", None),
            batch_id=expected,
        )
    matches = []
    for entry in pending.load(HOME):
        if not isinstance(entry, dict):
            continue
        try:
            actual = normalize_batch_id(entry.get("batch_id"))
        except ValueError:
            continue
        if actual == expected and (
            expected_scope is None
            or entry.get("scope_fingerprint") == expected_scope
        ):
            matches.append(entry)
    return matches


def _upload_recovery_action() -> dict[str, Any]:
    return {
        "id": "recover_completed_result",
        "mode": "replay_plan_command",
        "command": "run",
        "args": ["--upload-only", "--json"],
        "inherit": ["--plan", "--server"],
        "interactive": False,
    }


def _intent_generation(state: dict[str, Any]) -> int:
    value = state.get("intent_generation", 0)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value >= 2 ** 63 - 1
    ):
        raise RunPlanClientError(
            "local_state_invalid",
            "本机运行信息无效；为避免误启动，请重新从网页获取运行说明。",
        )
    return value


def _advance_intent_generation(
    path: Path,
    state: dict[str, Any],
    *,
    pending_recheck: bool = False,
) -> int:
    """Linearize a new explicit intent or one one-use automatic recheck."""

    generation = _intent_generation(state) + 1
    state["intent_generation"] = generation
    state["pending_recheck_generation"] = generation if pending_recheck else None
    state["pending_docker_install"] = None
    _atomic_json(path, state)
    return generation


def _consume_recheck_generation(
    path: Path,
    state: dict[str, Any],
    expected: object,
) -> None:
    """Consume one exact automatic generation, failing closed on stale state."""

    current = _intent_generation(state)
    valid = (
        isinstance(expected, int)
        and not isinstance(expected, bool)
        and expected >= 1
        and expected == current
        and state.get("pending_recheck_generation") == expected
    )
    if not valid:
        # Never let an older action erase a newer pending generation.
        if state.get("pending_recheck_generation") == expected:
            state["pending_recheck_generation"] = None
            _atomic_json(path, state)
        raise RunPlanClientError(
            "recheck_invalid_or_state_changed",
            "运行状态已经变化；旧的自动检查不会继续，也不会重新启动题目。",
            agent_action="notify_only",
        )
    state["pending_recheck_generation"] = None
    _atomic_json(path, state)


def _plan_recheck_action(command: str, generation: int) -> dict[str, Any]:
    """Describe a safe base-command replay without stale local choices."""

    return {
        "id": "recheck_current_plan",
        "mode": "replay_plan_command",
        "command": command,
        "args": ["--recheck-generation", str(generation), "--json"],
        "inherit": ["--plan", "--server"],
        "interactive": False,
    }


def _plan_recheck_response(
    *,
    path: Path,
    state: dict[str, Any],
    error_code: str,
    user_message: str,
    command: str = "run",
    poll_after_seconds: int = 30,
    server_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an executable bounded wait instead of promising hidden work."""

    generation = _advance_intent_generation(
        path, state, pending_recheck=True,
    )
    agent: dict[str, Any] = {
        "next_commands": [_plan_recheck_action(command, generation)],
    }
    if server_status is not None:
        agent["server_status"] = server_status
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "waiting",
        "interaction": "notify",
        "decision_required": False,
        "user_message": user_message,
        "agent_action": "recheck_plan",
        "error_code": error_code,
        "retryable": True,
        "choices": [],
        "poll_after_seconds": poll_after_seconds,
        "user_message_policy": "on_change_or_heartbeat",
        "agent": agent,
    }


def _progress_state_changed_response() -> dict[str, Any]:
    """Avoid publishing or persisting a progress snapshot older than intent."""

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "updating",
        "interaction": "silent",
        "decision_required": False,
        "user_message": "运行状态刚刚发生变化；下一次检查会显示最新进度。无需操作。",
        "agent_action": "monitor",
        "error_code": "progress_state_changed",
        "retryable": True,
        "choices": [],
        "poll_after_seconds": 30,
        "user_message_policy": "on_change_or_heartbeat",
    }


def _recover_plan_uploads(
    state: dict[str, Any], client: ApiClient,
) -> dict[str, Any]:
    """Replay this plan's completed local uploads without starting a runner."""

    batch_id = state["batch_id"]
    before = _exact_pending_uploads(batch_id, client)
    if not before:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "completed",
            "interaction": "silent",
            "decision_required": False,
            "user_message": "这台设备没有需要补交的完成结果。无需操作。",
            "agent_action": "done",
            "error_code": None,
            "retryable": False,
            "choices": [],
        }
    from . import runloop

    runloop._mark_pending_scope_required(client)
    runloop._retry_pending_uploads(client, batch_id=batch_id)
    remaining = _exact_pending_uploads(batch_id, client)
    if not remaining:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "completed",
            "interaction": "notify",
            "decision_required": False,
            "user_message": "这台设备已完成结果补交，没有重新运行题目。无需操作。",
            "agent_action": "done",
            "error_code": None,
            "retryable": False,
            "choices": [],
        }
    blocked = [entry for entry in remaining if entry.get("upload_blocked")]
    if blocked and len(blocked) == len(remaining):
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "review_required",
            "interaction": "warn",
            "decision_required": False,
            "user_message": (
                "这台设备有已完成结果需要人工检查后再补交；不会重新运行题目。"
            ),
            "agent_action": "notify_only",
            "error_code": "completed_result_review_required",
            "retryable": False,
            "choices": [],
            "agent": {
                "requires_user_action": True,
                "completed_result_count": len(blocked),
            },
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "waiting",
        "interaction": "notify",
        "decision_required": False,
        "user_message": (
            "这台设备有已完成结果尚未补交成功；网络恢复后只需继续补交，"
            "不会重新运行题目。"
        ),
        "agent_action": "recover_upload",
        "error_code": "completed_result_upload_pending",
        "retryable": True,
        "choices": [],
        "poll_after_seconds": 30,
        "user_message_policy": "on_change_or_heartbeat",
        "agent": {"next_commands": [_upload_recovery_action()]},
    }


def _local_progress_fault_response(
    server_response: dict[str, Any], local_item: dict[str, Any] | None,
    *, pending_upload_count: int = 0, blocked_upload_count: int = 0,
) -> dict[str, Any]:
    """Make a dead local runner visible without misreporting remote devices."""

    result = _agent_response_from_server(server_response)
    server_status = {
        key: value for key, value in result.items()
        if key not in {"schema_version", "agent"}
    }
    state = server_response.get("state")
    healthy_others = 0
    if isinstance(state, dict):
        public_count = state.get("healthy_other_devices")
        raw_devices = state.get("other_healthy")
        if isinstance(public_count, int) and not isinstance(public_count, bool):
            healthy_others = max(0, public_count)
        elif isinstance(raw_devices, list):
            healthy_others = len(raw_devices)
    agent = dict(result.get("agent") or {})
    agent["server_status"] = server_status
    if isinstance(local_item, dict):
        agent["local_runner"] = {
            "status": str(local_item.get("status") or "interrupted"),
            "returncode": local_item.get("returncode"),
        }
    agent["next_commands"] = [{
        "id": "inspect_local_runner",
        "argv": ["dradar", "fleet", "status"],
        "interactive": False,
    }]
    if pending_upload_count:
        if blocked_upload_count == pending_upload_count:
            agent.update({
                "requires_user_action": True,
                "completed_result_count": pending_upload_count,
                "next_commands": [],
            })
            result.update({
                "status": "review_required",
                "interaction": "warn",
                "decision_required": False,
                "user_message": (
                    "这台设备有已完成结果需要人工检查后再补交；"
                    "不会重新运行题目。"
                ),
                "agent_action": "notify_only",
                "error_code": "completed_result_review_required",
                "retryable": False,
                "choices": [],
                "poll_after_seconds": None,
                "agent": agent,
            })
            return result
        agent.update({
            "completed_result_count": pending_upload_count,
            "next_commands": [_upload_recovery_action()],
        })
        result.update({
            "status": "waiting",
            "interaction": "warn",
            "decision_required": False,
            "user_message": (
                "这台设备有已完成结果尚未补交成功；接下来只会补交结果，"
                "不会重新运行题目。"
            ),
            "agent_action": "recover_upload",
            "error_code": "completed_result_upload_pending",
            "retryable": True,
            "choices": [],
            "poll_after_seconds": 30,
            "user_message_policy": "on_change_or_heartbeat",
            "agent": agent,
        })
        return result
    if (
        isinstance(local_item, dict)
        and local_item.get("startup_status") == "failed"
    ):
        agent["requires_user_action"] = True
        result.update({
            "status": "paused",
            "interaction": "warn",
            "decision_required": False,
            "user_message": str(
                local_item.get("startup_user_message")
                or "这台设备未能完成运行准备，题目没有开始执行。请检查本机后重试。"
            ),
            "agent_action": "notify_only",
            "error_code": str(
                local_item.get("startup_error_code") or "local_start_failed"
            ),
            "retryable": bool(local_item.get("startup_retryable", True)),
            "choices": [],
            "poll_after_seconds": None,
            "agent": agent,
        })
        return result
    if healthy_others:
        result.update({
            "interaction": "warn",
            "decision_required": False,
            "user_message": (
                "这台设备的运行已中断，其他设备仍在继续处理。"
                "本机不会再开始新题；我会继续跟进整体进度。"
            ),
            "agent_action": "monitor",
            "error_code": "local_runner_interrupted",
            "retryable": True,
            "choices": [],
        })
    else:
        result.update({
            "status": "paused",
            "interaction": "warn",
            "decision_required": False,
            "user_message": (
                "这台设备的运行已中断，目前没有其他设备继续处理。"
                "请检查本机后，再次使用原运行说明即可继续。"
            ),
            "agent_action": "notify_only",
            "error_code": "local_runner_interrupted",
            "retryable": True,
            "choices": [],
            "poll_after_seconds": None,
        })
    result["agent"] = agent
    return result


def _remember_response(
    path: Path,
    state: dict[str, Any],
    response: dict[str, Any],
    *,
    command: str,
) -> None:
    if isinstance(response.get("plan"), dict):
        state["plan"] = _validate_plan(response["plan"])
    envelope = _validate_envelope(response["envelope"])
    if envelope.get("decision_required"):
        decision = envelope.get("decision")
        if not isinstance(decision, str) or not decision:
            raise RunPlanClientError("protocol_invalid", "服务返回的信息不完整，请升级后重试。")
        state["pending_decision"] = {"command": command, "decision": decision}
    elif (
        isinstance(state.get("pending_decision"), dict)
        and state["pending_decision"].get("command") == command
    ):
        state["pending_decision"] = None
    _atomic_json(path, state)


def _decision_for(state: dict[str, Any], command: str, token: str | None) -> str | None:
    if not token:
        return None
    pending = state.get("pending_decision")
    if not isinstance(pending, dict) or pending.get("command") != command:
        raise RunPlanClientError(
            "decision_context_missing",
            "当前确认已失效，我会重新检查状态后再询问。",
            retryable=True,
        )
    decision = pending.get("decision")
    if not isinstance(decision, str) or not decision:
        raise RunPlanClientError("decision_context_missing", "当前确认已失效，请重新检查状态。")
    return decision


def _output(args, response: dict[str, Any]) -> int:
    if not isinstance(response, dict) or response.get("schema_version") != SCHEMA_VERSION:
        raise RunPlanClientError("protocol_invalid", "服务返回的信息不完整，请升级后重试。")
    _validate_envelope(response)
    agent = response.get("agent")
    if isinstance(agent, dict):
        nested_version = agent.get("schema_version")
        if nested_version not in {None, SCHEMA_VERSION}:
            raise RunPlanClientError(
                "schema_version_unsupported", "运行协议版本不兼容，请升级后重试。",
            )
        agent.setdefault("schema_version", SCHEMA_VERSION)
        agent.pop("followup_launcher", None)
        if response.get("agent_action") in {
            "monitor", "recover_upload", "recheck_plan",
        }:
            launcher = _followup_launcher()
            if launcher is not None:
                agent["followup_launcher"] = launcher
    if getattr(args, "json", False):
        print(json.dumps(response, ensure_ascii=False, sort_keys=True))
    else:
        print(response["user_message"])
    return 0


def _followup_launcher() -> dict[str, Any] | None:
    """Describe the already-cached immutable launcher for follow-up commands.

    A bare ``uvx --from git+...`` resolves the remote HEAD again before every
    progress check.  That makes an otherwise healthy run depend on GitHub for
    its terminal handoff.  The first successful invocation has already cached
    both the exact source revision and its dependencies, so follow-up commands
    can safely reuse that revision in offline mode.

    Fail closed when package provenance is absent or not one of the public
    DRadar repositories.  Never manufacture a revision from the package
    version or the current working tree.
    """

    try:
        raw = importlib.metadata.distribution("dradar").read_text(
            "direct_url.json",
        )
        direct_url = json.loads(raw or "")
    except (
        importlib.metadata.PackageNotFoundError,
        json.JSONDecodeError,
        OSError,
        TypeError,
        UnicodeError,
    ):
        return None
    if not isinstance(direct_url, dict):
        return None
    source = direct_url.get("url")
    vcs_info = direct_url.get("vcs_info")
    if not isinstance(source, str) or not isinstance(vcs_info, dict):
        return None
    try:
        parsed = urlsplit(source)
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.hostname != "github.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in _TRUSTED_GIT_INSTALL_PATHS
        or vcs_info.get("vcs") != "git"
    ):
        return None
    commit = vcs_info.get("commit_id")
    if (
        not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
    ):
        return None
    normalized_source = urlunsplit((
        "https", "github.com", parsed.path.rstrip("/"), "", "",
    ))
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "uvx_offline_git_revision",
        "argv_prefix": [
            "uvx", "--offline", "--from",
            f"git+{normalized_source}@{commit}", "dradar",
        ],
        "interactive": False,
    }


def _agent_response_from_server(response: dict[str, Any]) -> dict[str, Any]:
    """Expose exactly one decision envelope while retaining machine state."""
    response = _validate_response(response)
    envelope = dict(response["envelope"])
    result = {"schema_version": SCHEMA_VERSION, **envelope}
    agent = {
        key: value for key, value in response.items()
        if key not in {"schema_version", "envelope", "plan_access_token"}
    }
    if envelope.get("decision_required"):
        token = envelope.get("decision_token")
        actions = {}
        for choice in envelope.get("choices") or []:
            if not isinstance(choice, dict) or not isinstance(choice.get("id"), str):
                continue
            choice_id = choice["id"]
            if choice_id == "cancel":
                actions[choice_id] = {"mode": "no_command", "args": []}
            elif isinstance(token, str) and token:
                actions[choice_id] = {
                    "mode": "replay_current_command_with_args",
                    "args": ["--decision-token", token],
                }
        if actions:
            agent["choice_actions"] = actions
    if agent:
        result["agent"] = agent
    return result


def _local_error_response(exc: RunPlanClientError) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "interaction": "notify",
        "decision_required": False,
        "user_message": exc.user_message,
        "agent_action": exc.agent_action,
        "error_code": exc.code,
        "retryable": exc.retryable,
        "choices": [],
    }
    if exc.agent_details is not None:
        result["agent"] = exc.agent_details
    return result


def _capacity_reservation(exc: ApiError) -> dict[str, Any] | None:
    """Validate one atomic cross-device capacity conflict from the server."""
    if exc.code != "concurrency_capacity_reserved":
        return None
    payload = exc.payload
    if not isinstance(payload, dict):
        raise RunPlanClientError(
            "protocol_invalid", "服务返回的信息不完整，请升级后重试。",
        )
    integer_fields = (
        "requested_concurrency", "available_concurrency",
        "account_concurrency", "account_concurrency_in_use",
        "plan_concurrency", "plan_concurrency_in_use",
    )
    if any(
        not isinstance(payload.get(key), int)
        or isinstance(payload.get(key), bool)
        or payload[key] < 0
        for key in integer_fields
    ) or payload.get("original_concurrency_mode") not in {"auto", "fixed"} or payload.get(
        "limiting_scope",
    ) not in {"account", "plan"}:
        raise RunPlanClientError(
            "protocol_invalid", "服务返回的信息不完整，请升级后重试。",
        )
    server = _api_error_response(exc)
    capacity = {key: payload[key] for key in integer_fields}
    capacity.update({
        "original_concurrency_mode": payload["original_concurrency_mode"],
        "limiting_scope": payload["limiting_scope"],
    })
    return {"available": payload["available_concurrency"], "server": server, "capacity": capacity}


def _api_error_response(exc: ApiError) -> dict[str, Any]:
    payload = exc.payload
    if isinstance(payload, dict):
        # FastAPI may place a structured application response below `detail`.
        candidate = payload.get("detail") if isinstance(payload.get("detail"), dict) else payload
        if isinstance(candidate.get("envelope"), dict):
            candidate = {**candidate, "schema_version": SCHEMA_VERSION}
        try:
            return _agent_response_from_server(candidate)
        except RunPlanClientError:
            pass
    return _local_error_response(RunPlanClientError(
        exc.code or "service_unavailable",
        "暂时无法连接运行服务，请稍后重试。",
        retryable=exc.status_code is None or exc.status_code >= 500 or exc.status_code == 429,
    ))


def _run_command(args, operation: Callable[[], dict[str, Any]]) -> int:
    @contextmanager
    def isolated_operation_stdout():
        """Keep Agent JSON stdout valid even when a dependency prints.

        ``redirect_stdout`` catches Python output while the fd-level redirect
        also catches installers and other child processes that inherit fd 1.
        JSON mode intentionally suppresses these diagnostics: forwarding raw
        dependency output could expose a capability or turn one JSON document
        into an unparsable mixed stream. Non-JSON invocations are unchanged.
        """

        if not getattr(args, "json", False):
            yield
            return
        with _JSON_STDOUT_LOCK:
            try:
                with tempfile.TemporaryFile(mode="w+b") as sink:
                    saved_stdout = os.dup(1)
                    try:
                        try:
                            sys.stdout.flush()
                        except (AttributeError, OSError):
                            pass
                        os.dup2(sink.fileno(), 1)
                        with os.fdopen(
                            os.dup(sink.fileno()),
                            "w",
                            encoding="utf-8",
                            errors="replace",
                        ) as text_sink, redirect_stdout(text_sink):
                            yield
                            text_sink.flush()
                    finally:
                        os.dup2(saved_stdout, 1)
                        os.close(saved_stdout)
            except OSError as exc:
                raise RunPlanClientError(
                    "json_output_isolation_failed",
                    "本机暂时无法安全生成运行状态，请稍后重试。",
                    retryable=True,
                ) from exc

    try:
        with isolated_operation_stdout():
            response = operation()
    except RunPlanClientError as exc:
        _output(args, _local_error_response(exc))
        return 1
    except ApiError as exc:
        response = _api_error_response(exc)
        _output(args, response)
        return 0 if response.get("decision_required") else 1
    if "envelope" in response:
        response = _agent_response_from_server(response)
    return _output(args, response)


def _run_with_admission(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Serialize local capacity admission across independent Agent dialogs."""
    with _exclusive_lock(_root(HOME) / ADMISSION_LOCK_FILE):
        return operation()


def cmd_run_plan(args) -> int:
    def operate(*, authoritative_recheck: bool = False) -> dict[str, Any]:
        recheck_generation = getattr(args, "recheck_generation", None)
        docker_install_token = getattr(args, "docker_install_token", None)
        if getattr(args, "upload_only", False):
            if (
                getattr(args, "concurrency", None) is not None
                or getattr(args, "decision_token", None) is not None
                or recheck_generation is not None
                or docker_install_token is not None
            ):
                raise RunPlanClientError(
                    "upload_only_argument_conflict",
                    "补交完成结果时不能同时更改运行数量或执行其他确认。",
                )
        if recheck_generation is not None and (
            getattr(args, "concurrency", None) is not None
            or getattr(args, "decision_token", None) is not None
            or docker_install_token is not None
        ):
            raise RunPlanClientError(
                "recheck_argument_conflict",
                "自动检查不能沿用之前的运行数量或确认信息。",
                agent_action="notify_only",
            )
        _run_code, path, state, client = _state_and_client(args)
        if getattr(args, "upload_only", False):
            return _recover_plan_uploads(state, client)
        if not authoritative_recheck:
            if recheck_generation is None:
                # Any explicit run is a newer user/Agent intent and invalidates
                # an older capacity recheck before server or Fleet state can
                # change.  The admission lock linearizes this with stop.
                if docker_install_token is None:
                    _advance_intent_generation(path, state)
                else:
                    _consume_docker_install(
                        path, state, args, docker_install_token,
                    )
            else:
                _consume_recheck_generation(
                    path, state, recheck_generation,
                )
        plan = state["plan"]
        # A stale one-use local decision is re-evaluated from the saved plan,
        # never by replaying its old token or carrying forward the old local
        # concurrency choice.  The second pass can only return a fresh
        # confirmation/current state; it cannot silently preserve stale
        # authority.
        requested_arg = (
            None if authoritative_recheck
            else getattr(args, "concurrency", None)
        )
        raw_decision_token = (
            None if authoritative_recheck
            else getattr(args, "decision_token", None)
        )
        local_decision_token = (
            raw_decision_token
            if isinstance(raw_decision_token, str)
            and raw_decision_token.startswith("drlc_") else None
        )
        server_decision_token = None if local_decision_token else raw_decision_token
        from . import fleet

        current_local = fleet.batch_status(plan["batch_id"])
        current_status = (
            current_local.get("status") if isinstance(current_local, dict) else None
        )
        same_local_plan = bool(
            isinstance(current_local, dict)
            and current_local.get("plan_id") == plan["plan_id"]
        )
        if (
            recheck_generation is not None
            and same_local_plan
            and current_status in {
                "stopping", "stopped", "failed", "interrupted", "completed",
            }
        ):
            raise RunPlanClientError(
                "recheck_cancelled_by_newer_state",
                "这台设备的运行状态已经变化；旧的自动检查不会重新启动题目。",
                agent_action="notify_only",
            )
        if same_local_plan and current_status == "stopping":
            raise RunPlanClientError(
                "local_run_stopping",
                "这台设备正在安全停止这次运行；不会自动重新开始。",
                agent_action="notify_only",
            )
        if current_status in {"starting", "running", "stopping", "orphaned"} and not same_local_plan:
            raise RunPlanClientError(
                "local_run_scope_conflict",
                "这台设备已有另一次运行占用了相同题目范围；请先检查当前运行状态。",
                agent_action="inspect_current_run",
            )
        already_local = bool(
            same_local_plan and current_status in {"starting", "running", "orphaned"}
        )

        assignments = plan["assignments"]
        first = assignments[0]
        refill = plan["refill"]

        def ensure_local_pool(workers: int) -> dict[str, Any]:
            try:
                return fleet.add_batch(
                    batch_id=plan["batch_id"],
                    workers=workers,
                    credentials_file=path,
                    plan_id=plan["plan_id"],
                    retry=True,
                    refill=bool(refill.get("enabled")),
                    max_tasks=refill.get("max_tasks"),
                    refill_harness=plan["harness"],
                    refill_model=first.get("model"),
                    refill_effort=first.get("effort"),
                )
            except fleet.FleetStartupError as exc:
                # Server admission is reversible until local readiness is
                # acknowledged. Do not leave another device accounting for a
                # machine that never actually became runnable.
                try:
                    client.stop_run_plan(
                        plan_id=plan["plan_id"], scope="this_device",
                    )
                except ApiError:
                    pass
                raise RunPlanClientError(
                    exc.code,
                    exc.user_message,
                    agent_action="notify_only",
                    retryable=exc.retryable,
                    agent_details={
                        "schema_version": SCHEMA_VERSION,
                        "requires_user_action": True,
                    },
                ) from exc

        def start_with_authoritative_recheck(
            *,
            concurrency_mode: str,
            concurrency: int,
            decision: str | None,
            decision_token: str | None,
        ) -> dict[str, Any]:
            """Consume a server decision once, then re-read without it once.

            Assignment/device state may change while a person is deciding.
            Replaying the old token would loop forever, while treating it as
            success could start work without current authority.  On the two
            explicit stale-token codes, discard both decision fields and make
            exactly one authoritative request.  Any second error propagates.
            """

            request = {
                "plan_id": plan["plan_id"],
                "logical_session_id": state["logical_session_id"],
                "concurrency_mode": concurrency_mode,
                "concurrency": concurrency,
                "decision": decision,
                "decision_token": decision_token,
            }
            try:
                return _validate_response(client.start_run_plan(**request))
            except ApiError as exc:
                if (
                    not decision_token
                    or exc.code not in _STALE_SERVER_DECISION_CODES
                ):
                    raise
                request.update({"decision": None, "decision_token": None})
                return _validate_response(client.start_run_plan(**request))

        if already_local:
            current_workers = int(current_local.get("workers") or 1)
            if requested_arg not in {None, "auto"}:
                _mode, requested_workers, _fleet_workers = _concurrency(
                    plan, requested_arg,
                )
                if int(requested_workers or 0) != current_workers:
                    raise RunPlanClientError(
                        "local_concurrency_change_requires_restart",
                        (
                            f"这台设备已在同时运行 {current_workers} 道。要改为 "
                            f"{requested_workers} 道，请先停止这台设备，再按新数量运行。"
                        ),
                        agent_action="ask_user",
                        agent_details={
                            "schema_version": SCHEMA_VERSION,
                            "requires_user_action": True,
                            "current_concurrency": current_workers,
                            "requested_concurrency": requested_workers,
                        },
                    )
            if local_decision_token:
                state["pending_local_capacity"] = None
                _atomic_json(path, state)
            decision = _decision_for(state, "run", server_decision_token)
            response = start_with_authoritative_recheck(
                concurrency_mode="fixed",
                concurrency=current_workers,
                decision=decision,
                decision_token=server_decision_token,
            )
            _remember_response(path, state, response, command="run")
            envelope = response["envelope"]
            if envelope.get("decision_required") or envelope.get("status") == "no_remaining":
                return response
            if envelope.get("agent_action") == "stop_runner":
                try:
                    fleet.stop_batch(plan["batch_id"])
                except fleet.FleetError:
                    pass
                return response
            if envelope.get("agent_action") not in {"start_runner", "monitor"}:
                return response
            if current_status == "orphaned":
                # The per-batch process lock proves the worker parent is still
                # alive during its bounded watchdog shutdown. Count it, report
                # it idempotently, and never try to spawn a duplicate.
                return _local_monitor_response(
                    response, selected=current_workers,
                )
            try:
                # This is intentionally called even for a known live pool. The
                # coordinator's exact-shape add is the idempotent local ensure
                # needed after a lost server response or stale public snapshot.
                fleet_response = ensure_local_pool(current_workers)
            except fleet.FleetError as exc:
                raise RunPlanClientError(
                    "local_start_failed",
                    "这台设备暂时无法继续运行；请检查本机状态后重试。",
                    retryable=True,
                ) from exc
            ensured_batch = fleet_response.get("batch") or {}
            if ensured_batch.get("status") == "starting":
                return _local_preparing_response(
                    response, selected=current_workers,
                )
            return _local_monitor_response(
                response, selected=current_workers,
            )

        # Check only the Docker/runtime/tool required by this exact plan before
        # the server marks this device active.  A missing unrelated provider is
        # deliberately invisible here.
        from .doctor import plan_environment_issue

        environment_issue = (
            plan_environment_issue(plan, allow_docker_install=True)
            if docker_install_token is not None
            else plan_environment_issue(plan)
        )
        if environment_issue is not None:
            if (
                environment_issue.get("install_required")
                and docker_install_token is None
                and environment_issue.get("error_code")
                == "docker_install_confirmation_required"
            ):
                return _docker_install_response(
                    path, state, args,
                    user_message=environment_issue["user_message"],
                )
            raise RunPlanClientError(
                environment_issue["error_code"],
                environment_issue["user_message"],
                agent_action=environment_issue["agent_action"],
                agent_details=environment_issue.get("agent"),
            )

        if not already_local:
            try:
                fleet.prepare_new_batch_runtime(home=path.parent.parent)
            except fleet.FleetControllerUpdatePending as exc:
                return _plan_recheck_response(
                    path=path,
                    state=state,
                    error_code=exc.code,
                    user_message=exc.user_message,
                    poll_after_seconds=30,
                )

        snapshot = _capacity_snapshot(client, plan, state.get("limits"))
        auto_downgraded = False
        refill_policy = plan["refill"]
        if refill_policy.get("enabled"):
            authorized_queue = (
                refill_policy.get("refill_to")
                or refill_policy.get("max_tasks")
                or snapshot["account_limit"]
            )
            supply_limit = min(
                int(authorized_queue), int(snapshot["account_limit"]),
            )
        else:
            supply_limit = min(
                len(plan["assignments"]), int(snapshot["account_limit"]),
            )

        if local_decision_token:
            try:
                (
                    selected_workers,
                    bound_server_decision,
                    bound_server_token,
                ) = _consume_local_capacity(
                    path, state, token=local_decision_token,
                    selected=requested_arg, snapshot=snapshot,
                )
            except RunPlanClientError as exc:
                if (
                    exc.code == "decision_invalid_or_capacity_changed"
                    and not authoritative_recheck
                ):
                    return operate(authoritative_recheck=True)
                raise
            if bound_server_token is not None:
                server_decision_token = bound_server_token
            mode, concurrency, fleet_workers = "fixed", selected_workers, selected_workers
            automatic_intent = False
        else:
            bound_server_decision = None
            mode, concurrency, fleet_workers = _concurrency(plan, requested_arg)
            automatic_intent = mode == "auto"
            if mode == "auto":
                selected_workers = int(snapshot["auto_workers"])
                if selected_workers < 1:
                    return _plan_recheck_response(
                        path=path,
                        state=state,
                        error_code="local_capacity_unavailable",
                        user_message=(
                            "这台设备当前没有空余运行位置；会按建议间隔重新检查。"
                            "无需手动更改设置。"
                        ),
                    )
                desired = min(supply_limit, int(snapshot["automatic_cap"]))
                auto_downgraded = selected_workers < desired
                mode, concurrency, fleet_workers = (
                    "fixed", selected_workers, selected_workers,
                )
            else:
                selected_workers = int(concurrency)

            if selected_workers > supply_limit:
                raise RunPlanClientError(
                    "concurrency_not_allowed",
                    f"这次运行最多可同时处理 {supply_limit} 道，请降低数量。",
                )

            exceeds_safe_capacity = selected_workers > int(snapshot["available"])
            if (
                exceeds_safe_capacity
                and not _authorized_concurrency(state, selected_workers, snapshot)
            ):
                recommended = min(selected_workers, int(snapshot["available"]))
                pending_server_decision = _decision_for(
                    state, "run", server_decision_token,
                )
                return _local_capacity_response(
                    path, state,
                    requested=selected_workers,
                    recommended=recommended,
                    snapshot=snapshot,
                    bound_server_decision=pending_server_decision,
                    bound_server_decision_token=server_decision_token,
                )

        if selected_workers > supply_limit:
            raise RunPlanClientError(
                "concurrency_not_allowed",
                f"这次运行最多可同时处理 {supply_limit} 道，请降低数量。",
            )

        decision = _decision_for(state, "run", server_decision_token)
        if bound_server_decision is not None and decision != bound_server_decision:
            raise RunPlanClientError(
                "decision_context_missing",
                "运行状态已经变化，我会重新检查后再询问。",
                retryable=True,
            )
        response = None
        last_capacity_response = None
        for _attempt in range(3):
            try:
                response = start_with_authoritative_recheck(
                    concurrency_mode=mode,
                    concurrency=concurrency,
                    decision=decision,
                    decision_token=server_decision_token,
                )
                break
            except ApiError as exc:
                reservation = _capacity_reservation(exc)
                if reservation is None:
                    raise
                last_capacity_response = reservation["server"]
                available = min(
                    int(reservation["available"]), selected_workers,
                    int(snapshot["available"]), supply_limit,
                )
                if available < 1:
                    return _plan_recheck_response(
                        path=path,
                        state=state,
                        error_code="concurrency_capacity_reserved",
                        user_message=(
                            "当前暂时没有空余运行位置；会按建议间隔重新检查。"
                            "无需手动更改设置。"
                        ),
                        server_status=reservation["server"],
                    )
                if not automatic_intent:
                    server_status = {
                        key: reservation["server"][key]
                        for key in (
                            "status", "interaction", "decision_required",
                            "user_message", "agent_action", "error_code",
                            "retryable", "choices",
                        )
                        if key in reservation["server"]
                    }
                    return _local_capacity_response(
                        path,
                        state,
                        requested=selected_workers,
                        recommended=available,
                        snapshot=snapshot,
                        decision="server_capacity",
                        allow_keep=False,
                        user_message=(
                            "其他设备刚刚占用了部分可用位置；现在最多还能同时运行 "
                            f"{available} 道。是否改按这个数量运行？"
                        ),
                        server_status=server_status,
                        server_capacity=reservation["capacity"],
                        bound_server_decision=decision,
                        bound_server_decision_token=server_decision_token,
                    )
                if available >= selected_workers:
                    return _plan_recheck_response(
                        path=path,
                        state=state,
                        error_code="concurrency_capacity_reserved",
                        user_message=(
                            "可用运行位置刚刚发生变化；会按建议间隔重新检查。"
                            "无需手动更改设置。"
                        ),
                        server_status=reservation["server"],
                    )
                selected_workers = available
                mode, concurrency, fleet_workers = (
                    "fixed", selected_workers, selected_workers,
                )
                auto_downgraded = True
                # A capacity reservation error occurs before the server
                # consumes a cross-device decision. Retain that exact decision
                # while retrying the same logical device with a lower value.
        if response is None:
            if last_capacity_response is not None:
                return _plan_recheck_response(
                    path=path,
                    state=state,
                    error_code="concurrency_capacity_reserved",
                    user_message=(
                        "可用运行位置仍在变化；会按建议间隔重新检查。"
                        "无需手动更改设置。"
                    ),
                    server_status=last_capacity_response,
                )
            raise RunPlanClientError(
                "protocol_invalid", "服务返回的信息不完整，请升级后重试。",
            )
        _remember_response(path, state, response, command="run")
        envelope = response["envelope"]
        if envelope.get("decision_required") or envelope.get("status") == "no_remaining":
            return response
        if envelope.get("agent_action") == "stop_runner":
            try:
                fleet.stop_batch(plan["batch_id"])
            except fleet.FleetError:
                pass
            return response
        if envelope.get("agent_action") not in {"start_runner", "monitor"}:
            return response

        current = fleet.batch_status(plan["batch_id"])
        if (
            isinstance(current, dict)
            and current.get("status") == "stopping"
            and current.get("plan_id") == plan["plan_id"]
        ):
            try:
                client.stop_run_plan(plan_id=plan["plan_id"], scope="this_device")
            except ApiError:
                pass
            raise RunPlanClientError(
                "local_run_stopping",
                "这台设备正在安全停止这次运行；不会自动重新开始。",
                agent_action="notify_only",
            )
        already_local = bool(
            isinstance(current, dict)
            and current.get("status") in {"starting", "running"}
            and current.get("plan_id") == plan["plan_id"]
        )
        try:
            fleet_response = (
                {"batch": current, "already_active": True}
                if already_local else
                ensure_local_pool(int(fleet_workers))
            )
        except fleet.FleetError as exc:
            # Admission is reversible until a local runner starts. Mark this
            # device stopped so another machine is not asked about a phantom.
            try:
                client.stop_run_plan(
                    plan_id=plan["plan_id"], scope="this_device",
                )
            except ApiError:
                pass
            raise RunPlanClientError(
                "local_start_failed",
                "这台设备暂时无法开始运行；已保留题目，请修复本机环境后重试。",
                retryable=True,
            ) from exc
        actual_workers = int(
            ((fleet_response.get("batch") or {}).get("workers"))
            or selected_workers
        )
        fleet_batch = fleet_response.get("batch") or {}
        if fleet_batch.get("status") == "starting":
            return _local_preparing_response(
                response,
                selected=actual_workers,
                adjusted=auto_downgraded,
            )
        if auto_downgraded:
            return _local_warn_response(response, selected=actual_workers)
        return _local_monitor_response(response, selected=actual_workers)

    return _run_command(args, lambda: _run_with_admission(operate))


def cmd_progress_plan(args) -> int:
    def operate() -> dict[str, Any]:
        _run_code, path, state, client = _state_and_client(args)
        pending_capacity = state.get("pending_local_capacity")
        if (
            isinstance(pending_capacity, dict)
            and float(pending_capacity.get("expires_at") or 0) > time.time()
        ):
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "blocked",
                "interaction": "notify",
                "decision_required": False,
                "user_message": (
                    "这次运行仍在等待你确认本机同时运行数量；在你回答刚才的"
                    "安全确认前，不会启动题目，当前状态仍是等待你的选择。"
                ),
                "agent_action": "notify_only",
                "error_code": "local_capacity_decision_pending",
                "retryable": False,
                "choices": [],
                "agent": {
                    "pending_user_decision": True,
                    "requested_concurrency": pending_capacity.get("requested"),
                    "recommended_concurrency": pending_capacity.get("recommended"),
                },
            }
        snapshot_generation = _intent_generation(state)
        response = _validate_response(client.run_plan_progress(state["plan_id"]))

        def merge_current_state() -> dict[str, Any] | None:
            current = _read_private_json(path)
            if (
                not isinstance(current, dict)
                or current.get("schema_version") != SCHEMA_VERSION
                or current.get("credential_kind") != "run_plan_v1"
                or current.get("plan_id") != state.get("plan_id")
                or current.get("batch_id") != state.get("batch_id")
                or current.get("server") != state.get("server")
            ):
                raise RunPlanClientError(
                    "local_state_invalid",
                    "本机运行信息已经变化；为避免覆盖新状态，本次进度未保存。",
                    agent_action="notify_only",
                )
            if _intent_generation(current) != snapshot_generation:
                return None
            # Merge only into the freshly re-read state.  Never write the old
            # pre-network snapshot back over a newer run/stop intent.
            _remember_response(path, current, response, command="progress")
            return current

        current = _run_with_admission(merge_current_state)
        if current is None:
            return _progress_state_changed_response()
        state = current
        from . import fleet

        local_item = fleet.batch_status(state["batch_id"])
        pending_uploads = _exact_pending_uploads(state["batch_id"], client)
        same_local_plan = bool(
            isinstance(local_item, dict)
            and local_item.get("plan_id") == state["plan_id"]
        )
        local_status = local_item.get("status") if same_local_plan else None
        local_fault = bool(
            local_status in {"failed", "interrupted"}
            or (
                same_local_plan
                and local_item.get("startup_status") == "failed"
            )
        )
        if pending_uploads:
            return _local_progress_fault_response(
                response,
                local_item if same_local_plan else None,
                pending_upload_count=len(pending_uploads),
                blocked_upload_count=sum(
                    bool(entry.get("upload_blocked"))
                    for entry in pending_uploads
                ),
            )
        if (
            same_local_plan
            and local_fault
            and response["envelope"].get("agent_action") == "monitor"
        ):
            return _local_progress_fault_response(
                response, local_item,
            )
        if (
            same_local_plan
            and local_status == "starting"
            and response["envelope"].get("agent_action") == "monitor"
        ):
            return _local_preparing_response(
                response, selected=int(local_item.get("workers") or 1),
            )
        return response

    # The potentially slow server read stays outside the global admission
    # lock.  Only the fresh-state comparison and merge take the lock.
    return _run_command(args, operate)


def cmd_stop_plan(args) -> int:
    def operate() -> dict[str, Any]:
        _run_code, path, state, client = _state_and_client(args)
        scope = args.scope.replace("-", "_")
        decision_token = getattr(args, "decision_token", None)
        if decision_token:
            _decision_for(state, "stop", decision_token)
        if scope == "this_device" or decision_token:
            # A concrete stop is a newer local intent even when this device
            # has no Fleet item yet.  Invalidate an outstanding automatic
            # capacity recheck before contacting the server, so a lost stop
            # response still cannot let the older action restart work.
            _advance_intent_generation(path, state)
        request = {
            "plan_id": state["plan_id"],
            "scope": scope,
            "decision_token": decision_token,
        }
        try:
            response = _validate_response(client.stop_run_plan(**request))
        except ApiError as exc:
            if (
                not decision_token
                or exc.code not in _STALE_SERVER_DECISION_CODES
            ):
                raise
            # The all-device stop confirmation changed while the user was
            # deciding.  Re-read exactly once without the stale capability;
            # this can return a fresh confirmation/current state, but cannot
            # authorize the destructive stop by itself.
            request["decision_token"] = None
            response = _validate_response(client.stop_run_plan(**request))
        _remember_response(path, state, response, command="stop")
        if response["envelope"].get("agent_action") == "stop_runner":
            from . import fleet

            try:
                fleet.stop_batch(state["batch_id"])
            except fleet.FleetError:
                # Server stop is authoritative; heartbeat propagation stops a
                # still-live local worker even if the coordinator disappeared.
                pass
        return response

    return _run_command(args, lambda: _run_with_admission(operate))


__all__ = [
    "DEFAULT_SERVER", "RunPlanClientError", "cmd_progress_plan",
    "cmd_run_plan", "cmd_stop_plan", "stable_device", "validate_server_url",
]
