"""Validation of the frozen on-demand-v2 v0 wire contract."""
from __future__ import annotations
import hashlib
import json
import re
from .client import ProtocolError

def opaque(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ProtocolError("invalid opaque identity")
    return value

def envelope(value: dict, request_id: str | None = None) -> dict:
    if type(value.get("schema_version")) is not int or value["schema_version"] != 2 or not isinstance(value.get("server_time"), str):
        raise ProtocolError("on-demand-v2 envelope required")
    if request_id is not None and value.get("request_id") != request_id:
        raise ProtocolError("receipt request identity mismatch")
    return value

def assignment(value: object, *, run_id: str, device_id: str, slot_id: int | None = None) -> dict:
    if not isinstance(value, dict):
        raise ProtocolError("assignment object required")
    for key in ("assignment_id", "run_id", "device_id", "lease_id", "work_key"):
        opaque(value.get(key))
    if value["run_id"] != run_id or value["device_id"] != device_id:
        raise ProtocolError("assignment owner scope mismatch")
    if type(value.get("owner_epoch")) is not int or value["owner_epoch"] <= 0:
        raise ProtocolError("positive owner epoch required")
    if type(value.get("slot_id")) is not int or value["slot_id"] < 0 or (slot_id is not None and value["slot_id"] != slot_id):
        raise ProtocolError("assignment slot mismatch")
    if value.get("state") not in {"leased", "running", "uncertain", "submitted", "released", "expired"}:
        raise ProtocolError("unknown assignment state")
    if value.get("execution_id") is not None:
        opaque(value["execution_id"])
    task = value.get("task")
    if not isinstance(task, dict) or not all(isinstance(task.get(k), str) and task[k] for k in ("task_id", "benchmark", "model", "effort")):
        raise ProtocolError("task selection is missing")
    if not isinstance(task.get("task_content_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", task["task_content_hash"]):
        raise ProtocolError("immutable task content hash required")
    if task.get("task_bundle") is None and (not isinstance(task.get("task_commit"), str) or not re.fullmatch(r"[a-f0-9]{40}", task["task_commit"])):
        raise ProtocolError("immutable task pin required")
    return value

def owner(value: dict) -> dict:
    return {k: value[k] for k in ("device_id", "lease_id", "owner_epoch")}

def result_hash(payload: dict) -> str:
    keys = ("execution_id", "outcome", "exit_confirmed", "completed_at", "elapsed_ms", "tokens", "failure", "artifacts")
    value = {k: payload[k] for k in keys}
    value["artifacts"] = sorted(value["artifacts"], key=lambda item: item["name"])
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()

def result_receipt(value: dict, request_id: str, assignment_id: str, payload: dict) -> dict:
    envelope(value, request_id)
    if (value.get("status") != "submitted" or value.get("assignment_id") != assignment_id
            or value.get("execution_id") != payload["execution_id"]
            or value.get("result_sha256") != payload["result_sha256"]
            or value.get("grading_state") not in {"queued", "not_applicable"}):
        raise ProtocolError("result ACK does not match saved evidence")
    opaque(value.get("submission_id"))
    return value

def correction_receipt(value: dict, request_id: str, assignment_id: str, body: dict) -> dict:
    result_receipt(value, request_id, assignment_id, body['corrected_result'])
    original, corrected = body['original_result'], body['corrected_result']
    proof = value.get('completion_correction')
    expected = {'schema': body['correction_schema'],
                'original_request_id': original['request_id'],
                'original_result_sha256': original['result_sha256'],
                'corrected_request_id': request_id,
                'corrected_result_sha256': corrected['result_sha256'],
                'exit_evidence_sha256': body['exit_evidence_sha256']}
    if (value.get('grading_state') != 'queued' or not isinstance(proof, dict)
            or any(proof.get(k) != v for k, v in expected.items())
            or not isinstance(proof.get('authorization_ref'), str) or not proof['authorization_ref']):
        raise ProtocolError('correction ACK does not bind original evidence and authorization')
    return value
