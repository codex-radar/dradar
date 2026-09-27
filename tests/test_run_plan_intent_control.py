"""Official command ordering at the local/remote intent boundary."""
import json

import pytest

from dradar import fleet, run_intent, run_plans
from dradar.api_client import ApiError
import test_run_plans as old
from test_plan_intents import receipt, unknown


def prepare(tmp_path, monkeypatch, client):
    return old._prepare_run(monkeypatch, tmp_path, plan=old._plan(), client=client,
                            snapshot=old._snapshot(available=2, auto_workers=2))


def stopped():
    return old._server_response(old._plan(), old._envelope(status="stopped", agent_action="stop_runner"))


def test_stop_observation_cannot_adopt_a_later_local_resume(tmp_path, monkeypatch, capsys):
    client = old.FakeClient(stops=[stopped()])
    path, state = prepare(tmp_path, monkeypatch, client)
    observe = client.whoami
    newer = {}

    def whoami():
        newer["generation"] = run_intent.begin(tmp_path, old.BATCH_ID)
        saved = json.loads(path.read_text())
        saved.update(intent_generation=100, authorized_concurrency=3)
        run_plans._atomic_json(path, saved)
        return dict(observe(), device_intent_revision=2, current_start_intent_id="a" * 32)

    monkeypatch.setattr(client, "whoami", whoami)
    assert run_plans.cmd_stop_plan(old._args(scope="this-device")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["agent"]["local_stop_recorded"] is True
    assert result["agent"]["remote_error_code"] == "stop_superseded_locally"
    assert client.stop_calls == []
    assert json.loads(path.read_text())["intent_generation"] == 100
    run_intent.require(tmp_path, old.BATCH_ID, newer["generation"])


def test_late_stop_ack_does_not_overwrite_or_drain_new_local_run(tmp_path, monkeypatch, capsys):
    client = old.FakeClient()
    path, state = prepare(tmp_path, monkeypatch, client)
    newer = {}

    def stop(**request):
        # The original request has completed; another explicit local run
        # publishes its lifecycle before that response reaches this caller.
        body = receipt("stop", request, **stopped())
        newer["generation"] = run_intent.begin(tmp_path, old.BATCH_ID)
        saved = json.loads(path.read_text())
        saved.update(intent_generation=200, device_intent_revision=3,
                     current_start_intent_id="b" * 32)
        run_plans._atomic_json(path, saved)
        newer["bytes"] = path.read_bytes()
        return body

    monkeypatch.setattr(client, "stop_run_plan", stop)
    monkeypatch.setattr(fleet, "stop_batch", lambda *_a, **_k: pytest.fail("late generic stop"))
    assert run_plans.cmd_stop_plan(old._args(scope="this-device")) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "stopped"
    assert path.read_bytes() == newer["bytes"]
    run_intent.require(tmp_path, old.BATCH_ID, newer["generation"])


def test_repeated_live_run_only_touches_exact_admission(tmp_path, monkeypatch, capsys):
    response = old._server_response(old._plan(), old._envelope(status="already_running"))
    client = old.FakeClient(progress=[response])
    client.revision = 7
    client.current_start = "a" * 32
    path, state = prepare(tmp_path, monkeypatch, client)
    state["pending_decision"] = None
    monkeypatch.setattr(fleet, "batch_status", lambda _: {
        "status": "running", "plan_id": state["plan_id"], "workers": 2})
    monkeypatch.setattr(fleet, "add_batch", lambda **kw: {"batch": {"status": "running", "workers": 2}})
    monkeypatch.setattr(client, "start_run_plan", lambda **kw: pytest.fail("maintenance minted a start"))
    assert run_plans.cmd_run_plan(old._args()) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_running"
    assert client.heartbeat_calls == [{"plan_id": state["plan_id"], "current_start_intent_id": "a" * 32,
                                      "expected_intent_revision": 7, "expected_generation": 0}]
    assert json.loads(path.read_text())["device_intent_revision"] == 7


def test_old_fleet_fault_does_not_stop_new_local_or_remote_admission(tmp_path, monkeypatch):
    path, state = old._state(tmp_path, old._plan())
    monkeypatch.setattr(fleet, "HOME", tmp_path)
    original = run_intent.begin(tmp_path, old.BATCH_ID)
    run_intent.stop(tmp_path, old.BATCH_ID)
    newer = run_intent.begin(tmp_path, old.BATCH_ID)
    state.update(intent_protocol=1, device_intent_revision=9, current_start_intent_id="c" * 32)
    run_plans._atomic_json(path, state)
    item = {"credentials_file": str(path), "intent_generation": original,
            "run_plan_credential_generation": 0, "run_plan_intent_revision": 1}
    monkeypatch.setattr(fleet, "_client", lambda _: pytest.fail("superseded fault sent a stop"))
    assert "superseded" in fleet._stop_run_plan_device(item, "old process failure")
    run_intent.require(tmp_path, old.BATCH_ID, newer)


def test_generation_scoped_stop_cannot_publish_over_newer_lifecycle(tmp_path):
    old_generation = run_intent.begin(tmp_path, old.BATCH_ID)
    run_intent.stop(tmp_path, old.BATCH_ID)
    current = run_intent.begin(tmp_path, old.BATCH_ID)
    before = run_intent.lifecycle_snapshot(tmp_path, old.BATCH_ID)
    with pytest.raises(run_intent.IntentStopped):
        run_intent.stop(tmp_path, old.BATCH_ID, expected_generation=old_generation)
    assert run_intent.lifecycle_snapshot(tmp_path, old.BATCH_ID) == before
    run_intent.require(tmp_path, old.BATCH_ID, current)


def test_intent_capability_requires_narrow_heartbeat(tmp_path, monkeypatch, capsys):
    client = old.FakeClient()
    prepare(tmp_path, monkeypatch, client)
    capabilities = client.run_plan_capabilities()
    capabilities.pop("admission_heartbeat")
    monkeypatch.setattr(client, "run_plan_capabilities", lambda: capabilities)
    assert run_plans.cmd_run_plan(old._args()) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "run_plan_intents_upgrade_required"
    assert not client.start_calls


@pytest.mark.parametrize("committed", [False, True])
def test_explicit_stop_recovers_original_unknown_id_after_process_restart(
        tmp_path, monkeypatch, capsys, committed):
    client = old.FakeClient(stops=[stopped()])
    path, state = prepare(tmp_path, monkeypatch, client)
    sent = []
    saved = {}
    normal_stop = client.stop_run_plan

    def send(**request):
        sent.append(dict(request))
        if len(sent) == 1:
            if committed:
                saved.update(receipt("stop", request, **stopped()))
                client.revision = request["expected_intent_revision"] + 1
            raise ApiError("lost response")
        return normal_stop(**request)

    def get(intent_id, **kwargs):
        if saved and saved.get("readable"):
            return dict({key: value for key, value in saved.items() if key != "readable"},
                        idempotent_replay=True)
        raise unknown(intent_id)

    monkeypatch.setattr(client, "stop_run_plan", send)
    monkeypatch.setattr(client, "run_plan_intent_receipt", get)
    assert run_plans.cmd_stop_plan(old._args(scope="this-device")) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["error_code"] == "remote_stop_unconfirmed"
    assert first["agent"]["local_stop_recorded"] is True
    first_snapshot = run_intent.lifecycle_snapshot(tmp_path, old.BATCH_ID)
    # A fresh process loads only durable credentials, which need not contain
    # the revision observed just before the lost response.
    state.clear()
    state.update(json.loads(path.read_text()))
    if committed:
        saved["readable"] = True
        monkeypatch.setattr(client, "whoami", lambda: pytest.fail("receipt recovery refreshed authority"))
    assert run_plans.cmd_stop_plan(old._args(scope="this-device")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "stopped"
    assert run_intent.lifecycle_snapshot(tmp_path, old.BATCH_ID) != first_snapshot
    assert len(sent) == (1 if committed else 2)
    assert all(request == sent[0] for request in sent)
    records = list((tmp_path / "run-plans" / "remote-intents").glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["status"] == "received"


def test_known_capacity_rejection_allows_later_bounded_recheck_at_same_revision(
        tmp_path, monkeypatch, capsys):
    client = old.FakeClient(starts=[old._capacity_error(requested=2, available=0, original_mode="auto"), old._server_response(old._plan())])
    path, state = prepare(tmp_path, monkeypatch, client)
    monkeypatch.setattr(fleet, "add_batch", lambda **kw: {"batch": {"status": "running", "workers": 2}})
    assert run_plans.cmd_run_plan(old._args()) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["agent_action"] == "recheck_plan"
    generation = state["pending_recheck_generation"]
    assert run_plans.cmd_run_plan(old._args(recheck_generation=generation)) == 0
    assert json.loads(capsys.readouterr().out)["agent_action"] == "monitor"
    assert len(client.start_calls) == 2
    assert client.start_calls[0]["intent_id"] != client.start_calls[1]["intent_id"]
    assert client.start_calls[0]["expected_intent_revision"] == client.start_calls[1]["expected_intent_revision"]


def test_expired_stop_confirmation_replaces_existing_scrubbed_challenge(
        tmp_path, monkeypatch, capsys):
    def challenge(token):
        return old._server_response(old._plan(), old._envelope(
            status="decision_required", interaction="confirm", decision_required=True,
            agent_action="ask_user", decision="stop_all_devices", decision_token=token,
            choices=[{"id": "stop_all_devices", "label": "停止所有设备"},
                     {"id": "cancel", "label": "取消"}]))

    client = old.FakeClient(stops=[challenge("drd_original"), old._stale_decision_error(),
                                   challenge("drd_renewed"), stopped()])
    path, state = prepare(tmp_path, monkeypatch, client)
    assert run_plans.cmd_stop_plan(old._args(scope="all-devices")) == 0
    assert json.loads(capsys.readouterr().out)["decision_token"] == "drd_original"
    original_id = client.stop_calls[0]["intent_id"]
    assert "decision_token" not in client.run_plan_intent_receipt(original_id)["envelope"]
    # A later command recovers from the durable state and original journal.
    state.clear()
    state.update(json.loads(path.read_text()))
    assert run_plans.cmd_stop_plan(old._args(
        scope="all-devices", decision_token="drd_original")) == 0
    renewed = json.loads(capsys.readouterr().out)
    assert renewed["agent_action"] == "ask_user"
    assert renewed["decision_token"] == "drd_renewed"
    assert len(client.stop_calls) == 3
    assert len({call["intent_id"] for call in client.stop_calls}) == 3
    assert {call["expected_intent_revision"] for call in client.stop_calls} == {0}
    assert [call["decision_token"] for call in client.stop_calls] == [None, "drd_original", None]
    assert client.revision == 0
    assert run_plans.cmd_stop_plan(old._args(
        scope="all-devices", decision_token="drd_renewed")) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "stopped"
    assert client.revision == 1 and len(client.stop_calls) == 4
