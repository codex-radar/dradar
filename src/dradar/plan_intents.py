"""Durable start/stop requests: exact receipts, immutable IDs, no CAS refresh.

Only filesystem operations hold ``index.lock``. The caller must publish local
stop first and must recheck its local launch guard after every network reply.
A live GET may recover the original start; a cached receipt never grants one.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import uuid

from .api_client import ApiError, _run_plan_intent_payload, _wire_hex


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode()


def fingerprint(operation: str, request: dict) -> str:
    payload = _run_plan_intent_payload(operation, request)
    token = payload.pop("decision_token")
    payload.update(operation=operation,
                   decision_token_sha256=hashlib.sha256(token.encode()).hexdigest() if token else None)
    return hashlib.sha256(canonical(payload)).hexdigest()


def _error(code, message):
    # A local verification failure says nothing about a prior remote commit.
    return ApiError(message, code=code, payload={"code": code, "applied": None})


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _validate_receipt(value, record, *, replay=False):
    request = record["request"]
    try:
        if (not isinstance(value, dict)
                or type(value.get("schema_version")) is not int or value["schema_version"] != 1
                or value.get("plan_id") != request["plan_id"]
                or value.get("operation") != record["operation"]
                or value.get("intent_id") != request["intent_id"]
                or value.get("request_fingerprint") != record["request_fingerprint"]
                or value.get("intent_status") not in ("applied", "rejected", "decision_required")
                or type(value.get("device_intent_revision")) is not int
                or value["device_intent_revision"] < 0
                or type(value.get("applied")) is not bool
                or type(value.get("current_effective")) is not bool
                or type(value.get("idempotent_replay")) is not bool
                or (replay and value["idempotent_replay"] is not True)
                or "applied_intent_revision" not in value
                or "current_start_intent_id" not in value):
            raise ValueError("invalid receipt fields")
        for name in ("admission_id", "current_start_intent_id"):
            if value.get(name) is not None:
                _wire_hex(value[name], name, 32)
        for name in ("device_generation", "credential_generation", "original_http_status"):
            if name in value and (type(value[name]) is not int or value[name] < 0):
                raise ValueError("invalid receipt integer")
        if "intent_protocol" in value and (type(value["intent_protocol"]) is not int
                                            or value["intent_protocol"] not in (0, 1)):
            raise ValueError("invalid intent protocol")
        applied = value["applied_intent_revision"]
        if value["intent_status"] == "applied":
            if (not value["applied"] or type(applied) is not int
                    or applied != request["expected_intent_revision"] + 1
                    or value["device_intent_revision"] < applied):
                raise ValueError("invalid applied revision")
        elif value["applied"] or applied is not None or value["current_effective"]:
            raise ValueError("unapplied receipt claims authority")
        # Rejections can be ordinary error objects without an Agent envelope.
        if value["intent_status"] != "rejected" and not isinstance(value.get("envelope"), dict):
            raise ValueError("missing envelope")
        if value["current_effective"]:
            if record["operation"] == "start":
                admission = value.get("admission_id", request["intent_id"])
                if (not admission or value["current_start_intent_id"] != admission
                        or (admission != request["intent_id"]
                            and value["envelope"].get("status") != "already_running")):
                    raise ValueError("current start does not match receipt")
            elif value["device_intent_revision"] != applied:
                raise ValueError("stop receipt no longer current")
        elif record["operation"] == "start" and value["intent_status"] == "applied":
            if value["envelope"].get("agent_action") != "stop_runner":
                raise ValueError("historical receipt still allows a launch")
        canonical(value)  # Reject NaN and non-JSON adapter responses as well.
    except (ValueError, KeyError, TypeError) as exc:
        raise _error("intent_receipt_invalid", "The exact run intent receipt was not confirmed.") from exc
    return value


def _read(path):
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("unsafe intent file")
        stat = path.stat()
        if os.name != "nt" and (stat.st_mode & 0o077 or stat.st_uid != os.getuid()):
            raise ValueError("intent file is not private")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        required = {"schema_version", "sequence", "operation", "server", "account_scope",
                    "local_intent", "request", "request_fingerprint", "status"}
        if (not isinstance(value, dict) or not required <= value.keys()
                or value.keys() - required - {"receipt"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or type(value["sequence"]) is not int or value["sequence"] < 1
                or value["operation"] not in ("start", "stop")
                or not isinstance(value["server"], str) or not value["server"]
                or not isinstance(value["local_intent"], str) or not value["local_intent"]
                or value["status"] not in ("pending", "received")):
            raise ValueError("invalid intent journal")
        _wire_hex(value["account_scope"], "account_scope", 64)
        normalized = _run_plan_intent_payload(value["operation"], value["request"])
        normalized.pop("schema_version")
        if (normalized != value["request"]
                or value["request_fingerprint"] != fingerprint(value["operation"], value["request"])
                or path.stem != value["request"]["intent_id"]
                or (value["status"] == "received") != ("receipt" in value)):
            raise ValueError("invalid immutable request")
        if "receipt" in value:
            _validate_receipt(value["receipt"], value)
        return value
    except (OSError, ValueError, KeyError, TypeError, ApiError) as exc:
        raise _error("local_intent_evidence_invalid",
                     "Saved run intent cannot be verified. Preserve the record; local stop remains available.") from exc


def _directory(home):
    root = Path(home) / "run-plans" / "remote-intents"
    if root.is_symlink() or root.parent.is_symlink():
        raise _error("local_intent_evidence_invalid", "The run intent directory is unsafe.")
    return root


def _lock(root):
    from .run_plans import _exclusive_lock
    if (root / "index.lock").is_symlink():
        raise _error("local_intent_evidence_invalid", "The run intent lock is unsafe.")
    return _exclusive_lock(root / "index.lock")


def _save(path, value):
    from .run_plans import _atomic_json
    try:
        _atomic_json(path, value)
    except OSError as exc:
        raise _error("local_intent_evidence_unavailable",
                     "The run intent could not be saved; its remote outcome must be reconciled.") from exc


def _without_challenge(value):
    if isinstance(value, dict):
        return {k: _without_challenge(v) for k, v in value.items() if k != "decision_token"}
    if isinstance(value, list):
        return [_without_challenge(v) for v in value]
    return value


def _remember(path, record, response, *, replay=False):
    response = _validate_receipt(response, record, replay=replay)
    with _lock(path.parent):
        current = _read(path)
        immutable = ("request_fingerprint", "local_intent", "sequence", "server", "account_scope", "operation")
        if any(current[key] != record[key] for key in immutable):
            raise _error("local_intent_evidence_invalid", "The saved operation changed while reading its receipt.")
        if current["status"] == "received":
            old = current["receipt"]
            if any(old.get(key) != response.get(key) for key in
                   ("intent_status", "applied_intent_revision", "admission_id")):
                raise _error("intent_receipt_invalid", "The immutable run intent outcome changed.")
        current.update(status="received", receipt=_without_challenge(response))
        _save(path, current)
    return response


def _deliver(response):
    if response["intent_status"] == "rejected":
        code = response.get("error_code") or response.get("code") or "intent_rejected"
        if not isinstance(code, str):
            code = "intent_rejected"
        raise ApiError("The saved run intent was rejected; its revision and ID were not refreshed.",
                       code=code, status_code=response.get("original_http_status", 409), payload=response)
    return response


def read_receipt(path, client):
    """Read fresh Server state for the exact saved request, never cached authority."""
    path = Path(path)
    record = _read(path)
    if record["account_scope"] != client.account_scope or record["server"] != client.server:
        raise _error("intent_scope_mismatch", "This intent requires its original credential and server.")
    try:
        response = client.run_plan_intent_receipt(
            record["request"]["intent_id"], plan_id=record["request"]["plan_id"],
            expected_fingerprint=record["request_fingerprint"])
    except (ValueError, TypeError) as exc:
        raise _error("intent_receipt_invalid", "The receipt response was not valid JSON.") from exc
    return _deliver(_remember(path, record, response, replay=True))


def _is_exact_unknown(exc, record):
    value = exc.payload
    return (exc.status_code == 404 and exc.code == "intent_unknown" and isinstance(value, dict)
            and type(value.get("schema_version")) is int and value["schema_version"] == 1
            and value.get("intent_id") == record["request"]["intent_id"]
            and value.get("intent_status") == "unknown" and "applied" in value and value["applied"] is None
            and value.get("retry_same_intent_only") is True)


def _request_matches(record, payload, expected_revision, local_intent):
    original = {k: v for k, v in record["request"].items()
                if k not in {"intent_id", "expected_intent_revision"}}
    return (original == payload and record["request"]["expected_intent_revision"] == expected_revision
            and record["local_intent"] == local_intent)


def execute(home: Path, client, *, operation: str, request: dict,
            expected_revision: int, local_intent: str, explicit_retry: bool = False,
            new_intent: bool = False):
    """Persist before POST; reconcile uncertainty before any explicit same-ID retry.

    Pending requests fence new requests of the same operation and plan, including
    another local launch identity. Stop is independent of a pending start. A
    known terminal outcome permits a later, distinct local action. ``new_intent``
    replaces only a confirmed decision_required request (e.g. a new challenge or
    its explicit answer); it never supersedes unknown, applied or rejected work.
    """
    if type(explicit_retry) is not bool or type(new_intent) is not bool:
        raise _error("intent_request_invalid", "Intent options must be booleans.")
    if not isinstance(local_intent, str) or not local_intent:
        raise _error("local_intent_missing", "A durable local run or stop identity is required.")
    try:
        if not isinstance(client.server, str) or not client.server:
            raise ValueError("missing server")
        _wire_hex(client.account_scope, "account_scope", 64)
        if not isinstance(request, dict) or {"intent_id", "expected_intent_revision"} & request.keys():
            raise ValueError("caller cannot replace intent identity")
        normalized = _run_plan_intent_payload(operation, dict(request,
            expected_intent_revision=expected_revision, intent_id="0" * 32))
    except (TypeError, ValueError) as exc:
        raise _error("intent_request_invalid", "The complete run intent request must have strict wire types.") from exc
    payload = {k: v for k, v in normalized.items()
               if k not in {"schema_version", "intent_id", "expected_intent_revision"}}
    root = _directory(home)
    replace_id = None
    while True:
        with _lock(root):
            items = [(_read(path), path) for path in sorted(root.glob("*.json"))]
            sequences = [item["sequence"] for item, _ in items]
            if len(set(sequences)) != len(sequences):
                raise _error("local_intent_evidence_invalid", "The run intent journal has duplicate ordering evidence.")
            if any(item["server"] == client.server and item["request"]["plan_id"] == payload["plan_id"]
                   and item["operation"] == operation and item["status"] == "pending"
                   and item["account_scope"] != client.account_scope for item, _ in items):
                raise _error("intent_scope_mismatch", "Reconcile this plan's unknown intent with its original credential first.")
            scoped = [(item, path) for item, path in items
                      if item["server"] == client.server and item["account_scope"] == client.account_scope
                      and item["operation"] == operation and item["request"]["plan_id"] == payload["plan_id"]]
            pending = [(item, path) for item, path in scoped if item["status"] == "pending"]
            if len(pending) > 1:
                raise _error("local_intent_evidence_invalid", "Multiple unresolved intents require exact reconciliation.")
            same = [(item, path) for item, path in scoped if item["local_intent"] == local_intent]
            existing = (pending or sorted(same, key=lambda pair: pair[0]["sequence"], reverse=True))[:1]
            if existing and existing[0][0]["request"]["intent_id"] == replace_id:
                # A fresh exact GET confirmed this decision, outside the lock.
                item = existing[0][0]
                if item["status"] != "received" or item["receipt"]["intent_status"] != "decision_required":
                    raise _error("intent_request_conflict", "The prior decision cannot be replaced.")
                existing = []
            if not existing:
                body = dict(payload, expected_intent_revision=expected_revision, intent_id=uuid.uuid4().hex)
                record = dict(schema_version=1, sequence=max(sequences, default=0) + 1,
                              operation=operation, server=client.server, account_scope=client.account_scope,
                              local_intent=local_intent, request=body,
                              request_fingerprint=fingerprint(operation, body), status="pending")
                path = root / (body["intent_id"] + ".json")
                _save(path, record)
                break
            record, path = existing[0]
        matches = _request_matches(record, payload, expected_revision, local_intent)
        try:
            response = read_receipt(path, client)
        except ApiError as exc:
            if (_is_exact_unknown(exc, record) and record["status"] == "pending"
                    and explicit_retry and matches and not new_intent):
                break
            raise
        if new_intent:
            if (response["intent_status"] != "decision_required"
                    or record["local_intent"] != local_intent
                    or record["request"]["expected_intent_revision"] != expected_revision
                    or response["device_intent_revision"] != expected_revision):
                raise _error("intent_request_conflict", "Only a confirmed unchanged decision may use a new intent ID.")
            replace_id = record["request"]["intent_id"]
            continue
        if not matches:
            raise _error("intent_request_conflict", "The saved intent has another request or local identity; it was not replaced.")
        return response
    method = client.start_run_plan if operation == "start" else client.stop_run_plan
    try:
        response = method(**deepcopy(record["request"]))
    except ApiError as original:
        if isinstance(original.payload, dict) and "intent_status" in original.payload:
            # A durable 409 receipt is useful even without an Agent envelope.
            return _deliver(_remember(path, record, original.payload))
        try:
            return read_receipt(path, client)
        except ApiError as receipt_error:
            raise receipt_error from original
    except (ValueError, TypeError) as original:
        try:
            return read_receipt(path, client)
        except ApiError as receipt_error:
            raise receipt_error from original
    return _deliver(_remember(path, record, response))


def reconcile_saved(home: Path, client, *, plan_id: str):
    results = []
    for path in sorted(_directory(home).glob("*.json")):
        record = _read(path)
        if (record["server"] != client.server or record["request"]["plan_id"] != plan_id
                or record["status"] != "pending"):
            continue
        try:
            response = read_receipt(path, client)
            results.append({"intent_id": record["request"]["intent_id"],
                            "intent_status": response["intent_status"],
                            "current_effective": response["current_effective"]})
        except ApiError as exc:
            response = exc.payload or {}
            results.append({"intent_id": record["request"]["intent_id"],
                            "intent_status": "rejected" if response.get("intent_status") == "rejected" else "unknown",
                            "code": exc.code or "intent_receipt_unavailable"})
    return results
