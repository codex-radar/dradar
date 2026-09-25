"""Privacy-bounded CLI failure reports with a durable retry queue.

Reports deliberately contain no exception text, command output, paths,
hostnames, account identifiers, credentials, prompts, patches, or trajectories.
Each queued report is a separate atomic file so parallel workers cannot clobber
one another's pending feedback.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import uuid
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from . import __version__
from .api_client import ApiError


SCHEMA = "dradar-failure-report-v1"
_SAFE_ATOM = re.compile(r"^[A-Za-z0-9._:@+-]{1,128}$")
_DETAIL_KEYS = {
    "task_id", "benchmark_id", "model", "effort", "agent", "outcome",
    "session_id", "worker_event_id", "registration_result",
    "registration_elapsed_sec", "process_exit_code", "process_signal",
    "ack_http_status",
}
REGISTRATION_RESULTS = frozenset({
    "process_exited", "registration_timeout", "heartbeat_disabled",
    "heartbeat_http_error", "heartbeat_transport_error", "heartbeat_rejected",
    "recorder_missing", "flight_no_client", "flight_remote_disabled",
    "flight_no_scope", "flight_no_pending", "flight_target_not_pending",
    "flight_http_error", "flight_transport_error", "flight_invalid_response",
    "flight_target_not_acknowledged", "flight_ack_persist_error",
    "flight_local_read_error", "flight_ack_cache_missing",
    "flight_target_acknowledged", "flight_unknown",
})
_REGISTRATION_DETAIL_KEYS = _DETAIL_KEYS - {
    "task_id", "benchmark_id", "model", "effort", "agent", "outcome",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _client_version() -> str:
    try:
        return version("dradar-cli")
    except PackageNotFoundError:
        # Zipapp/OTA builds do not necessarily carry installed distribution
        # metadata, but the package version is still compiled into the client.
        return __version__


def _safe(value: Any, *, maximum: int = 128) -> str | None:
    if value is None:
        return None
    text = str(value)[:maximum]
    return text if _SAFE_ATOM.fullmatch(text) else None


def _safe_detail(key: str, value: Any) -> str | None:
    if key not in _DETAIL_KEYS:
        return None
    if key in {"session_id", "worker_event_id"}:
        return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) else None
    if key == "registration_result":
        return value if isinstance(value, str) and value in REGISTRATION_RESULTS else None
    limits = {"registration_elapsed_sec": (0, 3600), "process_exit_code": (0, 255),
              "process_signal": (1, 64), "ack_http_status": (100, 599)}
    if key in limits:
        low, high = limits[key]
        if type(value) is int and low <= value <= high:
            return str(value)
        if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,3}", value):
            number = int(value)
            return value if low <= number <= high else None
        return None
    return _safe(value)


def build_report(
    *,
    source: str,
    phase: str,
    failure_kind: str,
    failure_code: str | None = None,
    assignment_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_detail: dict[str, str] = {}
    for key, value in (detail or {}).items():
        clean = _safe_detail(key, value)
        if clean is not None:
            clean_detail[key] = clean
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "report_key": uuid.uuid4().hex,
        "source": source,
        "phase": _safe(phase, maximum=64) or "unknown",
        "failure_kind": _safe(failure_kind, maximum=64) or "unknown",
        "client_version": _client_version(),
        "platform": f"{sys.platform}-{platform.machine() or 'unknown'}"[:64].lower(),
        "occurred_at": _now(),
        "detail": clean_detail,
    }
    clean_code = _safe(failure_code, maximum=64)
    clean_assignment = _safe(assignment_id)
    if clean_code is not None:
        payload["failure_code"] = clean_code
    if clean_assignment is not None:
        payload["assignment_id"] = clean_assignment
    return payload


def _queue_dir(home: Path) -> Path:
    return home / "failure-reports"


def _wire_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if not key.startswith("_")}


def _send_compatible(client, record: dict[str, Any]) -> None:
    payload = _wire_payload(record)
    try:
        client.report_runner_failure(payload)
    except ApiError as exc:
        # An older server rejects the new optional detail keys with 422. Keep
        # the same report key and submit its original safe base fields once;
        # the registration gate itself never depends on failure reporting.
        detail = payload.get("detail")
        if exc.status_code != 422 or not isinstance(detail, dict) or not (
            detail.keys() & _REGISTRATION_DETAIL_KEYS
        ):
            raise
        legacy = dict(payload)
        legacy["detail"] = {
            key: value for key, value in detail.items()
            if key not in _REGISTRATION_DETAIL_KEYS
        }
        client.report_runner_failure(legacy)


def _store(home: Path, record: dict[str, Any]) -> None:
    directory = _queue_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    key = record["report_key"]
    target = directory / f"{key}.json"
    temporary = directory / f".{key}.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(record, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def submit_or_queue(client, home: Path, payload: dict[str, Any]) -> str:
    try:
        _send_compatible(client, payload)
    except Exception:
        record = dict(payload)
        record["_attempts"] = int(record.get("_attempts", 0)) + 1
        record["_last_attempt_at"] = _now()
        try:
            _store(home, record)
        except OSError:
            pass
        return "send-failed"
    return "received"


def pending(home: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    directory = _queue_dir(home)
    if not directory.is_dir():
        return reports
    for path in sorted(directory.glob("*.json"))[:100]:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("schema") == SCHEMA:
            reports.append(item)
    return reports


def flush_pending(client, home: Path) -> dict[str, int]:
    result = {"received": 0, "send_failed": 0}
    directory = _queue_dir(home)
    for record in pending(home):
        key = record.get("report_key")
        if not isinstance(key, str):
            continue
        path = directory / f"{key}.json"
        try:
            _send_compatible(client, record)
        except Exception:
            record["_attempts"] = int(record.get("_attempts", 0)) + 1
            record["_last_attempt_at"] = _now()
            try:
                _store(home, record)
            except OSError:
                pass
            result["send_failed"] += 1
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        result["received"] += 1
    return result
