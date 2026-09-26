"""Bounded registration diagnostic wire contract; no runtime or I/O dependencies."""
import re

START_CODES = frozenset("assignment-start-" + suffix for suffix in (
    "transport", "http-other", "http-400", "http-401", "http-403", "http-404",
    "http-409", "http-410", "http-422", "http-426", "http-429", "http-500",
    "http-502", "http-503", "http-504"))
WORKER_CODES = frozenset(code.replace("assignment-start-", "worker-registration-")
                         for code in START_CODES)
ENUMS = {
    "registration_failure_stage": frozenset({"heartbeat", "flight", "ack_persist",
        "start_preflight", "start_request", "local_state", "gate"}),
    "registration_failure_reason": frozenset({"budget_expired", "handoff_budget_insufficient",
        "worker_exited", "stop_requested", "transport_error", "http_rejected",
        "invalid_response", "local_state_error", "unknown"}),
    "registration_ack_state": frozenset({"not_received", "received", "persisted", "unknown"}),
    "registration_close_state": frozenset({"not_attempted", "confirmed", "http_rejected",
        "transport_error", "budget_expired", "invalid_response", "unknown"}),
}
LIMITS = {"registration_elapsed_ms": 120000, "registration_remaining_ms": 15000}
DIAGNOSTIC_KEYS = frozenset(ENUMS) | frozenset(LIMITS)


def safe_value(key, value):
    if key in ENUMS:
        return value if isinstance(value, str) and value in ENUMS[key] else None
    if key in LIMITS:
        if type(value) is int and 0 <= value <= LIMITS[key]:
            return str(value)
        if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,5}", value):
            return value if int(value) <= LIMITS[key] else None
    return None


def valid_diagnostic(detail, *, source, phase, failure_code):
    keys = DIAGNOSTIC_KEYS.intersection(detail)
    if not keys:
        return True
    if source != "cli" or phase != "runner" or failure_code not in START_CODES | WORKER_CODES:
        return False
    if any(safe_value(k, detail[k]) != detail[k] for k in keys):
        return False
    stage, reason = detail.get("registration_failure_stage"), detail.get("registration_failure_reason")
    if stage is None or reason is None:
        return False
    if failure_code in START_CODES and stage not in {"start_preflight", "start_request", "local_state", "gate"}:
        return False
    if failure_code in WORKER_CODES and stage not in {"heartbeat", "flight", "ack_persist", "local_state"}:
        return False
    if reason == "handoff_budget_insufficient" and stage != "start_preflight":
        return False
    if reason == "budget_expired" and detail.get("registration_remaining_ms", "0") != "0":
        return False
    if reason == "transport_error" and stage not in {"heartbeat", "flight", "start_preflight", "start_request"}:
        return False
    if reason == "http_rejected" and stage not in {"heartbeat", "flight", "start_preflight", "start_request"}:
        return False
    ack = detail.get("registration_ack_state")
    if stage == "heartbeat" and ack in {"received", "persisted"}:
        return False
    if stage == "ack_persist" and ack == "persisted":
        return False
    if detail.get("registration_close_state", "not_attempted") != "not_attempted" and failure_code not in START_CODES:
        return False
    return True
