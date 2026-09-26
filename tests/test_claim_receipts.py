import json

import httpx
import pytest

from dradar import claim_receipts, cli
from dradar.api_client import ApiClient

REQUEST, FINGERPRINT, ASSIGNMENT = "original-request-id", "a" * 64, "b" * 32


def receipt():
    return {"schema_version": 1, "status": "accepted", "request_id": REQUEST,
            "request_fingerprint": FINGERPRINT, "plan_id": "original-plan", "batch_id": "c" * 32,
            "benchmark_id": "deep-swe", "harness": "codex", "committed_at": "2026-09-26T00:00:00Z",
            "read_at": "2026-09-26T01:00:00Z", "assignment_ids": [ASSIGNMENT],
            "assignments": [{"assignment_id": ASSIGNMENT, "task_id": "task", "model": "model",
                             "effort": "high", "status": "leased"}], "invitation": "private-extension"}


def setup(monkeypatch, status, body, *, lost=False, token="fixture-account"):
    requests = []
    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/claim-requests/" + REQUEST
        if lost:
            raise httpx.ReadError("private-transport-error", request=request)
        return httpx.Response(status, json=body)
    client = ApiClient("https://fixture.invalid", token, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(claim_receipts, "_load_config", lambda: {"token": token})
    def configured(cfg, *, auto_register):
        assert not auto_register
        return client
    monkeypatch.setattr(claim_receipts, "_client", configured)
    return requests


@pytest.mark.parametrize("verify", [False, True])
def test_official_receipt_is_one_get_without_invitation_or_execution(monkeypatch, capsys, verify):
    requests = setup(monkeypatch, 200, receipt())
    args = ["claim-receipt", "--request-id", REQUEST, "--json"]
    if verify:
        args += ["--expected-fingerprint", FINGERPRINT]
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["request_binding"] == ("verified" if verify else "not_checked")
    assert "invitation" not in result
    assert len(requests) == 1
    assert requests[0].url.params.get("expected_fingerprint") == (FINGERPRINT if verify else None)


@pytest.mark.parametrize("kind", ["unknown", "old-server", "lost", "conflict", "wrong-id", "wrong-fingerprint", "bool-schema"])
def test_uncertain_or_mismatched_receipt_never_creates_replacement(monkeypatch, capsys, kind):
    body, status = receipt(), 200
    if kind == "unknown":
        status, body = 404, {"schema_version": 1, "code": "claim_request_unknown", "status": "unknown",
                             "request_id": REQUEST, "retry_same_request_only": True}
    elif kind == "old-server":
        status, body = 404, {"detail": "Not Found"}
    elif kind == "conflict":
        status, body = 409, {"code": "claim_request_conflict"}
    elif kind == "wrong-id":
        body["request_id"] = "different-request"
    elif kind == "wrong-fingerprint":
        body["request_fingerprint"] = "d" * 64
    elif kind == "bool-schema":
        body["schema_version"] = True
    requests = setup(monkeypatch, status, body, lost=kind == "lost")
    assert cli.main(["claim-receipt", "--request-id", REQUEST,
                     "--expected-fingerprint", FINGERPRINT, "--json"]) != 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] in {"unknown", "conflict"}
    assert result["retry_same_request_only"] is True
    assert len(requests) == 1
    assert "private" not in json.dumps(result)


def test_plan_token_cannot_be_reused_as_account_query(monkeypatch, capsys):
    requests = setup(monkeypatch, 200, receipt(), token="drp_fixture_plan")
    assert cli.main(["claim-receipt", "--request-id", REQUEST, "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "account_credential_required"
    assert requests == []
