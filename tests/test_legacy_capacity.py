"""Author tests: real ApiClient GET wire, isolated HOME, no provider runtime."""

import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from dradar import api_client, identity, legacy_capacity, local_config, run_plans


SERVER = "https://inventory.invalid"
TOKEN = "drt_private_inventory_test_token"
PLAN_TOKEN = "drp_private_inventory_test_token"
RUN_CODE = "run_private_inventory_test_code"
PLAN = "a" * 32
BATCH = "b" * 32
SESSION = "c" * 32
QID = "d" * 64


def args(**kw):
    return SimpleNamespace(**{"plan": None, "server": None, "after": "",
                              "quarantine_after": "", "json": True, **kw})


def page():
    return {
        "schema_version": 1,
        "reservations": [{"session_id": SESSION, "batch_id": BATCH,
            "plan_id": PLAN, "device_generation": 0, "device_id_hash": "e" * 64,
            "closed": True, "reservation_protocol": -1, "assignment_id": None,
            "state": "exit_unknown", "required_action": "review original scope"}],
        "migration_quarantines": [{"quarantine_id": QID, "plan_id": PLAN,
            "device_id_hash": "e" * 64, "session_ids": [SESSION],
            "snapshot_sha256": hashlib.sha256(json.dumps([SESSION], separators=(",", ":")).encode()).hexdigest(),
            "required_action": "review original device"}],
        "next_after": SESSION, "next_quarantine_after": QID,
    }


def save(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))
    path.chmod(0o600)


def saved_plan(**kw):
    state = {"schema_version": 1, "credential_kind": "run_plan_v1",
        "server": SERVER, "token": PLAN_TOKEN, "run_code_hash": run_plans._run_code_digest(RUN_CODE),
        "plan_id": PLAN, "batch_id": BATCH, "benchmark": "deep-swe",
        "plan": {"plan_id": PLAN, "batch_id": BATCH, "benchmark_id": "deep-swe"}, **kw}
    path = local_config.HOME / "run-plans" / f"plan-{PLAN}.json"
    save(path, state)
    return path, state


def home_snapshot():
    if not local_config.HOME.exists():
        return {}
    return {str(p.relative_to(local_config.HOME)):
            (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
            for p in local_config.HOME.rglob("*")}


@pytest.fixture
def wire(monkeypatch):
    seen = []
    responses = [httpx.Response(200, json=page())]

    def handle(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/runner/reservations"
        assert request.headers["X-DRadar-Capabilities"] == "runner-reservation-v1"
        return responses.pop(0)

    original = api_client.ApiClient

    def client(*a, **kw):
        return original(*a, **kw, transport=httpx.MockTransport(handle))

    def forbidden(*_a, **_kw):
        raise AssertionError("inventory invoked a mutation or provider capability probe")

    monkeypatch.setattr(legacy_capacity, "ApiClient", client)
    monkeypatch.setattr(identity, "ApiClient", client)
    monkeypatch.setattr(api_client, "advertised_capabilities", forbidden)
    monkeypatch.setattr(api_client.ApiClient, "_post", forbidden)
    for name in ("_state_and_client", "_saved_state", "_exchange", "stable_device", "_cleanup_states", "_atomic_json"):
        monkeypatch.setattr(run_plans, name, forbidden)
    monkeypatch.setattr(identity, "_auto_register", forbidden)
    monkeypatch.setattr(identity, "_save_config", forbidden)
    monkeypatch.setattr(local_config, "_save_config", forbidden)
    save(local_config.CONFIG_PATH, {"server": SERVER, "token": TOKEN})
    return seen, responses


def invoke(capsys, **kw):
    before = home_snapshot()
    result = legacy_capacity.cmd_legacy_inventory(args(**kw))
    assert home_snapshot() == before
    captured = capsys.readouterr()
    assert not captured.err
    for secret in (TOKEN, PLAN_TOKEN, RUN_CODE):
        assert secret not in captured.out
    return result, json.loads(captured.out)


def test_account_one_page_uses_real_get_and_preserves_both_cursors(wire, capsys):
    seen, _ = wire
    rc, output = invoke(capsys, after="previous-session", quarantine_after="f" * 64)
    assert rc == 0 and len(seen) == 1
    assert dict(seen[0].url.params) == {"limit": "100", "after": "previous-session", "quarantine_after": "f" * 64}
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert output["scope"] == {"kind": "account"}
    assert output["next_after"] == SESSION and output["next_quarantine_after"] == QID
    assert output["migration_quarantines"][0]["session_ids"] == [SESSION]
    assert output["reservations"][0]["closed"] is True
    assert output["exit_evidence"] == "not_assessed"
    assert "classification" not in output["reservations"][0]
    assert "counts_toward_capacity" not in output["reservations"][0]


def test_plain_output_is_counts_and_next_not_an_exit_attestation(wire, capsys):
    assert legacy_capacity.cmd_legacy_inventory(args(json=False)) == 0
    out = capsys.readouterr().out
    assert "1 条 reservation" in out and "1 组旧历史" in out
    assert "分类未提供：1" in out and "不证明物理退出或容量释放" in out
    assert f"next_after={SESSION}" in out and f"next_quarantine_after={QID}" in out
    assert TOKEN not in out


def test_retired_expired_plan_is_read_without_exchange_or_account_config(wire, capsys, monkeypatch):
    seen, _ = wire
    saved_plan(retired_for_new_execution=True, access_expires_at="2000-01-01T00:00:00Z")
    monkeypatch.setattr(local_config, "_load_config", lambda: pytest.fail("plan lookup read unrelated account"))
    rc, output = invoke(capsys, plan=RUN_CODE)
    assert rc == 0 and len(seen) == 1
    assert seen[0].headers["Authorization"] == f"Bearer {PLAN_TOKEN}"
    assert output["scope"] == {"kind": "plan", "plan_id": PLAN, "batch_id": BATCH}


@pytest.mark.parametrize("plan", [None, RUN_CODE])
def test_wrong_explicit_server_does_not_send_credentials(wire, capsys, plan):
    saved_plan()
    rc, output = invoke(capsys, plan=plan, server="https://other.invalid")
    assert rc == 1 and output["error_code"] == "server_scope_mismatch"
    assert not wire[0]


@pytest.mark.parametrize("kind", ["absent", "corrupt", "symlink", "duplicate", "tokenless", "wrong_scope"])
def test_missing_or_unverifiable_plan_preserves_originals_and_never_exchanges(wire, capsys, kind):
    if kind != "absent":
        path, state = saved_plan()
        if kind == "corrupt":
            path.write_text('{"token":')
        elif kind == "symlink":
            target = path.with_suffix(".original")
            path.rename(target)
            path.symlink_to(target)
        elif kind == "duplicate":
            save(path.with_name("plan-duplicate.json"), state)
        elif kind == "tokenless":
            save(path, {**state, "token": None})
        elif kind == "wrong_scope":
            save(path, {**state, "batch_id": "f" * 32})
    rc, output = invoke(capsys, plan=RUN_CODE)
    assert rc == 1 and output["error_code"] in {"inventory_plan_evidence_missing", "inventory_credentials_unavailable"}
    assert not wire[0]


@pytest.mark.parametrize("body", [{}, {"server": SERVER}, {"server": SERVER, "token": PLAN_TOKEN}, []])
def test_account_requires_existing_account_identity_without_registration(wire, capsys, body):
    save(local_config.CONFIG_PATH, body)
    rc, _ = invoke(capsys)
    assert rc == 1 and not wire[0]


def test_corrupt_config_is_preserved(wire, capsys):
    local_config.CONFIG_PATH.write_text("{")
    rc, output = invoke(capsys)
    assert rc == 1 and output["error_code"] == "inventory_credentials_unavailable"
    assert not wire[0]


@pytest.mark.parametrize("status,code", [(404, "inventory_unsupported"), (426, "inventory_unsupported"),
    (401, "inventory_access_denied"), (403, "inventory_access_denied"), (500, "inventory_query_failed")])
def test_http_failure_is_not_empty_inventory_and_never_echoes_server_secrets(wire, capsys, status, code):
    wire[1][:] = [httpx.Response(status, json={"detail": f"{TOKEN} {PLAN_TOKEN} {RUN_CODE}"})]
    rc, output = invoke(capsys)
    assert rc == 1 and output["error_code"] == code and len(wire[0]) == 1
    assert "reservations" not in output


@pytest.mark.parametrize("fault", ["schema_bool", "missing_quarantine", "closed_string", "generation_bool",
    "protocol_bool", "bad_snapshot", "foreign_plan", "duplicate", "missing_cursor", "bad_classification", "bad_count", "missing_count"])
def test_invalid_response_is_rejected_without_guessing_empty(wire, capsys, fault):
    data = page()
    if fault == "schema_bool": data["schema_version"] = True
    elif fault == "missing_quarantine": del data["migration_quarantines"]
    elif fault == "closed_string": data["reservations"][0]["closed"] = "true"
    elif fault == "generation_bool": data["reservations"][0]["device_generation"] = True
    elif fault == "protocol_bool": data["reservations"][0]["reservation_protocol"] = False
    elif fault == "bad_snapshot": data["migration_quarantines"][0]["snapshot_sha256"] = "a" * 64
    elif fault == "foreign_plan": data["reservations"][0]["plan_id"] = "f" * 32
    elif fault == "duplicate": data["reservations"].append(copy.deepcopy(data["reservations"][0]))
    elif fault == "missing_cursor": del data["next_after"]
    elif fault == "bad_classification": data["reservations"][0]["classification"] = "released"
    elif fault == "bad_count": data["reservations"][0]["counts_toward_capacity"] = 0
    elif fault == "missing_count": data["reservations"][0]["classification"] = "historical_unverified"
    saved_plan()
    wire[1][:] = [httpx.Response(200, json=data)]
    rc, output = invoke(capsys, plan=RUN_CODE)
    assert rc == 1 and output["error_code"] == "inventory_unverifiable"
    assert len(wire[0]) == 1


def test_optional_classification_is_reported_as_server_fact(wire, capsys):
    data = page()
    data["reservations"][0].update(classification="historical_unverified", counts_toward_capacity=False)
    current = {**data["reservations"][0], "session_id": "f" * 32, "classification": "current_reservation", "counts_toward_capacity": True}
    data["reservations"].append(current)
    data["migration_quarantines"][0]["classification"] = "historical_unverified"
    wire[1][:] = [httpx.Response(200, json=data)]
    rc, output = invoke(capsys)
    assert rc == 0
    assert [row["counts_toward_capacity"] for row in output["reservations"]] == [False, True]
    assert output["exit_evidence"] == "not_assessed"
    assert output["migration_quarantines"][0]["classification"] == "historical_unverified"
    # A historical snapshot never overrides later activity on a member.
    assert output["reservations"][1]["classification"] == "current_reservation"


def test_history_snapshot_does_not_overwrite_a_current_member(wire, capsys):
    data = page()
    data["reservations"][0].update(classification="current_reservation", counts_toward_capacity=True)
    data["migration_quarantines"][0]["classification"] = "historical_unverified"
    wire[1][:] = [httpx.Response(200, json=data)]
    rc, output = invoke(capsys)
    assert rc == 0
    assert output["reservations"][0]["counts_toward_capacity"] is True
    assert output["reservations"][0]["classification"] == "current_reservation"
    assert output["migration_quarantines"][0]["session_ids"] == [SESSION]
    assert "counts_toward_capacity" not in output["migration_quarantines"][0]


def test_unmapped_legacy_scope_is_still_visible_without_guessing_ownership(wire, capsys):
    data = page()
    data["reservations"][0].update(plan_id=None, device_id_hash=None, device_generation=None)
    data["migration_quarantines"][0].update(plan_id=None, device_id_hash=None, classification="cleanup_required")
    wire[1][:] = [httpx.Response(200, json=data)]
    rc, output = invoke(capsys)
    assert rc == 0 and output["reservations"][0]["plan_id"] is None
    assert output["migration_quarantines"][0]["classification"] == "cleanup_required"


def test_unknown_fields_and_prose_are_not_printed(wire, capsys):
    data = page()
    data["token"] = TOKEN
    data["reservations"][0]["required_action"] = RUN_CODE
    data["reservations"][0]["blocks_admission"] = True
    data["migration_quarantines"][0]["run_code"] = RUN_CODE
    wire[1][:] = [httpx.Response(200, json=data)]
    rc, output = invoke(capsys)
    assert rc == 0 and "blocks_admission" not in output["reservations"][0]


def test_secret_in_a_scope_field_is_rejected(wire, capsys):
    data = page()
    data["next_after"] = TOKEN
    wire[1][:] = [httpx.Response(200, json=data)]
    assert invoke(capsys)[0] == 1


@pytest.mark.parametrize("cursor", [False, 123, "a\ncommand", "a" * 65])
def test_invalid_input_cursor_sends_nothing(wire, capsys, cursor):
    assert invoke(capsys, after=cursor)[0] == 1
    assert not wire[0]


def test_empty_page_remains_an_observation_not_cleanup(wire, capsys):
    wire[1][:] = [httpx.Response(200, json={"schema_version": 1, "reservations": [],
        "migration_quarantines": [], "next_after": None, "next_quarantine_after": None})]
    rc, output = invoke(capsys)
    assert rc == 0 and output["exit_evidence"] == "not_assessed"
    assert output["next_after"] is None and output["next_quarantine_after"] is None


def _reconcile_evidence(device_id="drv_original_device_123456"):
    return {
        "schema_version": 1,
        "device_id": device_id,
        "quarantine_id": QID,
        "snapshot_sha256": hashlib.sha256(json.dumps([SESSION], separators=(",", ":")).encode()).hexdigest(),
        "evidence_id": "f" * 32,
        "managed_process_inventory": "confirmed_absent",
        "owned_container_inventory": "confirmed_absent",
        "historical_scope_verified": True,
        "execution_manifest_sha256": "a" * 64,
    }


@pytest.fixture
def reconcile_wire(monkeypatch):
    seen = []

    def handle(request):
        seen.append(request)
        if request.method == "GET":
            data = page()
            data["migration_quarantines"][0]["device_id_hash"] = hashlib.sha256(
                b"dradar:device-id-v1:drv_original_device_123456"
            ).hexdigest()
            return httpx.Response(200, json={
                **data,
                "migration_quarantines": [{**data["migration_quarantines"][0],
                    "classification": "historical_unverified"}],
            })
        assert request.method == "POST"
        assert request.url.path == "/api/v1/runner/reconcile-legacy"
        return httpx.Response(200, json={"ok": True, "idempotent_replay": False})

    original = api_client.ApiClient

    def client(*a, **kw):
        return original(*a, **kw, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(legacy_capacity, "ApiClient", client)
    monkeypatch.setattr(api_client, "advertised_capabilities", lambda: ())
    saved_plan()
    device = local_config.HOME / "run-plans" / run_plans.DEVICE_FILE
    save(device, {"schema_version": 1, "device_id": "drv_original_device_123456", "device_name": "old"})
    evidence = local_config.HOME / "evidence.json"
    save(evidence, _reconcile_evidence())
    return seen, evidence


def test_reconcile_posts_once_after_exact_fresh_scope(reconcile_wire, capsys):
    seen, evidence = reconcile_wire
    rc = legacy_capacity.cmd_legacy_reconcile(args(plan=RUN_CODE, reconcile=str(evidence)))
    output = json.loads(capsys.readouterr().out)
    assert rc == 0 and output["idempotent_replay"] is False
    assert [item.method for item in seen] == ["GET", "POST"]
    body = json.loads(seen[1].content)
    assert body["quarantine_id"] == QID and body["device_id"] == "drv_original_device_123456"


@pytest.mark.parametrize("mutation,expected", [
    (lambda value: value.update(device_id="drv_other_device_123456"), "reconcile_device_mismatch"),
    (lambda value: value.update(snapshot_sha256="b" * 64), "reconcile_snapshot_changed"),
])
def test_reconcile_negative_scope_stops_before_post(reconcile_wire, capsys, mutation, expected):
    seen, evidence = reconcile_wire
    value = _reconcile_evidence()
    mutation(value)
    save(evidence, value)
    rc = legacy_capacity.cmd_legacy_reconcile(args(plan=RUN_CODE, reconcile=str(evidence)))
    output = json.loads(capsys.readouterr().out)
    assert rc == 1 and output["error_code"] == expected
    assert [item.method for item in seen] == ([] if expected == "reconcile_device_mismatch" else ["GET"])


def test_reconcile_unknown_quarantine_never_posts(reconcile_wire, capsys):
    seen, evidence = reconcile_wire
    value = _reconcile_evidence()
    value["quarantine_id"] = "1" * 64
    save(evidence, value)
    rc = legacy_capacity.cmd_legacy_reconcile(args(plan=RUN_CODE, reconcile=str(evidence)))
    output = json.loads(capsys.readouterr().out)
    assert rc == 1 and output["error_code"] == "reconcile_quarantine_unknown"
    assert [item.method for item in seen] == ["GET", "GET"]


def test_reconcile_unmapped_quarantine_never_posts(reconcile_wire, capsys, monkeypatch):
    seen, evidence = reconcile_wire
    original = api_client.ApiClient

    def client(*a, **kw):
        def handle(request):
            seen.append(request)
            if request.method == "GET":
                data = page()
                data["migration_quarantines"][0].update(classification="historical_unverified", device_id_hash=None)
                return httpx.Response(200, json=data)
            return httpx.Response(200, json={})
        return original(*a, **kw, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(legacy_capacity, "ApiClient", client)
    module = legacy_capacity
    rc = module.cmd_legacy_reconcile(args(plan=RUN_CODE, reconcile=str(evidence)))
    output = json.loads(capsys.readouterr().out)
    assert rc == 1 and output["error_code"] == "reconcile_scope_unmapped"
    assert [item.method for item in seen] == ["GET"]


@pytest.mark.parametrize("status,body,expected", [
    (200, {"ok": True, "idempotent_replay": True}, "ok"),
    (409, {"code": "evidence_conflict"}, "reconcile_conflict"),
    (503, {"detail": "temporary"}, "reconcile_query_failed"),
])
def test_reconcile_post_outcomes_are_explicit_and_bounded(
    reconcile_wire, capsys, monkeypatch, status, body, expected,
):
    seen, evidence = reconcile_wire
    original = api_client.ApiClient

    def client(*a, **kw):
        def handle(request):
            seen.append(request)
            if request.method == "GET":
                data = page()
                data["migration_quarantines"][0]["classification"] = "historical_unverified"
                data["migration_quarantines"][0]["device_id_hash"] = hashlib.sha256(
                    b"dradar:device-id-v1:drv_original_device_123456"
                ).hexdigest()
                return httpx.Response(200, json=data)
            return httpx.Response(status, json=body)
        return original(*a, **kw, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(legacy_capacity, "ApiClient", client)
    rc = legacy_capacity.cmd_legacy_reconcile(args(plan=RUN_CODE, reconcile=str(evidence)))
    output = json.loads(capsys.readouterr().out)
    assert [item.method for item in seen] == ["GET", "POST"]
    if expected == "ok":
        assert rc == 0 and output["read_only"] is False and output["idempotent_replay"] is True
    else:
        assert rc == 1 and output["read_only"] is False and output["error_code"] == expected
