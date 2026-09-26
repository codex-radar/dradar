"""Read one original claim receipt without replaying or creating a claim."""

import json
import re

from .api_client import ApiError
from .identity import _client
from .local_config import _load_config


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _accepted(value, request_id, expected_fingerprint):
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("status") != "accepted"
            or value.get("request_id") != request_id
            or not isinstance(value.get("request_fingerprint"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["request_fingerprint"])
            or any(not _text(value.get(key)) for key in (
                "plan_id", "batch_id", "benchmark_id", "harness", "committed_at", "read_at"))
            or not isinstance(value.get("assignment_ids"), list)
            or not value["assignment_ids"]
            or any(not isinstance(aid, str) or not re.fullmatch(r"[0-9a-f]{32}", aid)
                   for aid in value["assignment_ids"])
            or len(set(value["assignment_ids"])) != len(value["assignment_ids"])
            or not isinstance(value.get("assignments"), list)):
        raise ValueError("invalid claim receipt")
    if expected_fingerprint is not None and value["request_fingerprint"] != expected_fingerprint:
        raise ValueError("claim fingerprint mismatch")
    fields = ("assignment_id", "task_id", "model", "effort", "status")
    rows = value["assignments"]
    if (len(rows) != len(value["assignment_ids"])
            or any(not isinstance(row, dict) or any(not _text(row.get(key)) for key in fields)
                   for row in rows)
            or [row["assignment_id"] for row in rows] != value["assignment_ids"]):
        raise ValueError("invalid assignment receipt")
    # Only the public contract can reach stdout; unknown response extensions
    # must never accidentally expose an invitation or credential.
    result = {key: value[key] for key in (
        "schema_version", "status", "request_id", "request_fingerprint", "plan_id",
        "batch_id", "benchmark_id", "harness", "committed_at", "read_at", "assignment_ids",
    )}
    result["assignments"] = [{key: row[key] for key in fields} for row in rows]
    result["request_binding"] = "verified" if expected_fingerprint is not None else "not_checked"
    return result


def cmd_claim_receipt(args) -> int:
    request_id = args.request_id
    expected = args.expected_fingerprint
    result = {"schema_version": 1, "request_id": request_id, "status": "unknown",
              "retry_same_request_only": True}
    status = 2
    try:
        if (not isinstance(request_id, str) or not 16 <= len(request_id) <= 64
                or any(ord(char) < 32 for char in request_id)
                or (expected is not None and not re.fullmatch(r"[0-9a-f]{64}", expected))):
            raise ValueError("invalid original request identity")
        client = _client(_load_config(), auto_register=False)
        if client.plan_scoped:
            result["code"] = "account_credential_required"
        else:
            receipt = client.claim_request_receipt(request_id, expected_fingerprint=expected)
            result = _accepted(receipt, request_id, expected)
            status = 0
    except ApiError as exc:
        body = exc.payload
        if (exc.status_code == 404 and isinstance(body, dict)
                and type(body.get("schema_version")) is int and body["schema_version"] == 1
                and body.get("code") == "claim_request_unknown"
                and body.get("status") == "unknown" and body.get("request_id") == request_id
                and body.get("retry_same_request_only") is True):
            result["code"] = "claim_request_unknown"
        elif exc.status_code == 409 and exc.code == "claim_request_conflict":
            result.update(status="conflict", code="claim_request_conflict")
            status = 1
        else:
            # Includes old-server 404 and transport failures. Neither means
            # the original request was uncommitted, and neither is retried.
            result["code"] = "claim_receipt_unavailable"
        result["http_status"] = exc.status_code
    except ValueError:
        result["code"] = "claim_receipt_invalid"
        status = 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif status == 0:
        print(f"Original claim accepted: {request_id}")
        print(f"Batch: {result['batch_id']}; assignments: {len(result['assignment_ids'])}")
        print(f"Request fingerprint binding: {result['request_binding']}")
    else:
        print(f"Claim outcome: {result['status']} ({result['code']}).")
        print("Keep the original request ID and body. This query grants no new claim or execution.")
    return status
