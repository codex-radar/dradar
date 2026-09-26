"""Actual HTTP adapter contracts; no real Server or runtime is contacted."""
import json

import httpx
import pytest

from dradar import plan_intents
from dradar.api_client import ApiClient, ApiError

PLAN, INTENT = "p" * 32, "a" * 32


def client(handler):
    return ApiClient("https://unit.invalid", "drp_test", transport=httpx.MockTransport(handler))


def start(c, **changes):
    payload = dict(plan_id=PLAN, logical_session_id="session-1", concurrency_mode="fixed", concurrency=2,
                   expected_generation=3, expected_intent_revision=4, intent_id=INTENT)
    payload.update(changes)
    return c.start_run_plan(**payload)


def stop(c, **changes):
    payload = dict(plan_id=PLAN, scope="this_device", expected_generation=3,
                   expected_intent_revision=4, intent_id=INTENT)
    payload.update(changes)
    return c.stop_run_plan(**payload)


@pytest.mark.parametrize("invoke,operation", [(start, "start"), (stop, "stop")])
def test_modern_full_shape_and_fingerprint_match_server_defaults(invoke, operation):
    seen = []
    c = client(lambda req: seen.append(req) or httpx.Response(200, json={"ok": True}))
    assert invoke(c) == {"ok": True}
    request, = seen
    payload = json.loads(request.content)
    assert request.url.path == "/api/v1/run-plans/" + operation
    assert payload["intent_id"] == INTENT and payload["expected_intent_revision"] == 4
    assert payload["decision_token"] is None
    assert payload["schema_version"] == 1
    if operation == "start":
        assert payload["decision"] is None
    else:
        assert not {"concurrency_mode", "concurrency", "decision", "logical_session_id"} & payload.keys()
    assert len(plan_intents.fingerprint(operation, payload)) == 64


@pytest.mark.parametrize("invoke", [start, stop])
@pytest.mark.parametrize("changes", [dict(intent_id=None), dict(expected_intent_revision=None),
    dict(expected_intent_revision=True), dict(expected_intent_revision="4"), dict(expected_intent_revision=-1),
    dict(expected_generation=True), dict(expected_generation=None), dict(intent_id="A" * 32),
    dict(intent_id="a/../"), dict(plan_id="short"), dict(decision_token=1)])
def test_modern_authority_is_strict_and_rejected_before_network(invoke, changes):
    c = client(lambda _: pytest.fail("invalid authority reached HTTP"))
    with pytest.raises(ValueError):
        invoke(c, **changes)


@pytest.mark.parametrize("changes", [dict(concurrency=True), dict(concurrency="2"), dict(concurrency=0),
    dict(concurrency=41), dict(concurrency_mode=[]), dict(decision="resume"), dict(logical_session_id="x")])
def test_start_request_fields_are_not_coerced(changes):
    with pytest.raises(ValueError):
        start(client(lambda _: pytest.fail("invalid payload reached HTTP")), **changes)


@pytest.mark.parametrize("kind", ["429", "409", "lost_ack"])
@pytest.mark.parametrize("invoke", [start, stop])
def test_mutation_single_send_without_rate_or_transport_retry(invoke, kind):
    seen = []
    def handler(request):
        seen.append(request)
        if kind == "lost_ack":
            raise httpx.ReadTimeout("lost response", request=request)
        return httpx.Response(int(kind), json={"code": "intent_revision_conflict", "applied": False})
    c = client(handler)
    c._sleep = lambda _: pytest.fail("intent mutation retried")
    with pytest.raises(ApiError):
        invoke(c)
    assert len(seen) == 1


def test_receipt_get_is_exact_bounded_and_preserves_original_shape():
    seen = []
    body = {"intent_id": INTENT, "intent_status": "rejected", "original_http_status": 409,
            "current_effective": False, "error_code": "intent_revision_conflict"}
    c = client(lambda req: seen.append(req) or httpx.Response(200, json=body))
    assert c.run_plan_intent_receipt(INTENT, plan_id=PLAN, expected_fingerprint="f" * 64) == body
    request, = seen
    assert request.method == "GET" and request.url.path == "/api/v1/run-plans/intents/" + INTENT
    assert dict(request.url.params) == dict(plan_id=PLAN, expected_fingerprint="f" * 64)
    assert request.extensions["timeout"]["read"] == 3.0


@pytest.mark.parametrize("kind", ["429", "404", "timeout"])
def test_receipt_get_never_retries_or_maps_unknown_to_absence(kind):
    seen = []
    def handler(request):
        seen.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("lost response", request=request)
        return httpx.Response(int(kind), json={"code": "intent_unknown", "applied": None})
    c = client(handler)
    c._sleep = lambda _: pytest.fail("receipt retried")
    with pytest.raises(ApiError) as info:
        c.run_plan_intent_receipt(INTENT, plan_id=PLAN)
    assert len(seen) == 1
    if kind == "404":
        assert info.value.payload["applied"] is None and info.value.code == "intent_unknown"


@pytest.mark.parametrize("params", [dict(intent_id="x", plan_id=PLAN), dict(intent_id=INTENT, plan_id="short"),
    dict(intent_id=INTENT, plan_id=PLAN, expected_fingerprint="bad")])
def test_bad_receipt_scope_never_reaches_http(params):
    with pytest.raises(ValueError):
        client(lambda _: pytest.fail("invalid scope reached HTTP")).run_plan_intent_receipt(**params)


def test_heartbeat_has_exact_admission_without_start_or_revision_refresh():
    seen = []
    c = client(lambda req: seen.append(req) or httpx.Response(200, json={"touched": True, "starts_new_work": False}))
    result = c.heartbeat_run_plan(plan_id=PLAN, current_start_intent_id=INTENT,
                                 expected_intent_revision=4, expected_generation=3)
    request, = seen
    assert request.url.path == "/api/v1/run-plans/heartbeat" and request.method == "POST"
    assert json.loads(request.content) == dict(schema_version=1, plan_id=PLAN,
        current_start_intent_id=INTENT, expected_intent_revision=4, expected_generation=3)
    assert result["starts_new_work"] is False


@pytest.mark.parametrize("field,value", [("expected_intent_revision", True), ("expected_generation", 0.0),
    ("current_start_intent_id", "A" * 32), ("plan_id", "short")])
def test_bad_heartbeat_does_not_reach_http(field, value):
    payload = dict(plan_id=PLAN, current_start_intent_id=INTENT, expected_intent_revision=4, expected_generation=3)
    payload[field] = value
    with pytest.raises(ValueError):
        client(lambda _: pytest.fail("bad heartbeat reached HTTP")).heartbeat_run_plan(**payload)


@pytest.mark.parametrize("code", [409, 429])
def test_heartbeat_never_retries(code):
    seen = []
    c = client(lambda req: seen.append(req) or httpx.Response(code, json={"code": "intent_revision_conflict"}))
    c._sleep = lambda _: pytest.fail("heartbeat retried")
    with pytest.raises(ApiError):
        c.heartbeat_run_plan(plan_id=PLAN, current_start_intent_id=INTENT,
                             expected_intent_revision=4, expected_generation=3)
    assert len(seen) == 1


def test_legacy_stop_shape_is_preserved_without_intent_downgrade():
    seen = []
    c = client(lambda req: seen.append(req) or httpx.Response(200, json={"ok": True}))
    c.stop_run_plan(plan_id="plan-1", scope="this_device")
    assert json.loads(seen[0].content) == dict(schema_version=1, plan_id="plan-1", scope="this_device")


def test_helper_and_http_adapter_recover_committed_start_after_lost_ack(tmp_path):
    seen, saved = [], {}
    def handler(request):
        seen.append(request.method)
        if request.method == "POST":
            payload = json.loads(request.content)
            intent_id = payload["intent_id"]
            saved.update(schema_version=1, plan_id=PLAN, operation="start", intent_id=intent_id,
                request_fingerprint=plan_intents.fingerprint("start", payload), intent_status="applied",
                applied=True, applied_intent_revision=5, device_intent_revision=5,
                current_start_intent_id=intent_id, admission_id=intent_id, current_effective=True,
                idempotent_replay=True, envelope={"status": "started", "agent_action": "start_runner"})
            raise httpx.ReadTimeout("server committed but response was lost", request=request)
        assert request.url.path.endswith(saved["intent_id"])
        assert request.url.params["expected_fingerprint"] == saved["request_fingerprint"]
        return httpx.Response(200, json=saved)
    result = plan_intents.execute(tmp_path, client(handler), operation="start",
        request=dict(plan_id=PLAN, logical_session_id="session-1", concurrency_mode="fixed",
                     concurrency=2, expected_generation=3), expected_revision=4, local_intent="original-local")
    assert seen == ["POST", "GET"] and result["current_effective"] is True
    record, = (tmp_path / "run-plans" / "remote-intents").glob("*.json")
    assert json.loads(record.read_text())["status"] == "received"
