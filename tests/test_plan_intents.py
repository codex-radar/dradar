"""Bounded local/adapter evidence for durable start/stop request identity."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading

import pytest

from dradar import plan_intents as intents
from dradar.api_client import ApiError

PLAN = "p" * 32
START = dict(plan_id=PLAN, logical_session_id="session-0001", concurrency_mode="fixed",
             concurrency=2, expected_generation=3)
STOP = dict(plan_id=PLAN, scope="this_device", expected_generation=3)


def files(home):
    return sorted((home / "run-plans" / "remote-intents").glob("*.json"))


def unknown(intent_id, **changes):
    body = dict(schema_version=1, intent_id=intent_id, intent_status="unknown", applied=None,
                retry_same_intent_only=True)
    body.update(changes)
    return ApiError("No durable receipt yet", status_code=404, code="intent_unknown", payload=body)


def receipt(operation, request, *, status="applied", effective=True, replay=False, **changes):
    applied = status == "applied"
    body = dict(schema_version=1, plan_id=request["plan_id"], operation=operation,
        intent_id=request["intent_id"], request_fingerprint=intents.fingerprint(operation, request),
        intent_status=status, applied=applied, current_effective=effective if applied else False,
        applied_intent_revision=request["expected_intent_revision"] + 1 if applied else None,
        device_intent_revision=request["expected_intent_revision"] + int(applied),
        current_start_intent_id=request["intent_id"] if operation == "start" and applied else None,
        admission_id=request["intent_id"] if operation == "start" and applied else None,
        device_generation=request["expected_generation"], credential_generation=request["expected_generation"],
        intent_protocol=1, idempotent_replay=replay, original_http_status=200, error_code=None,
        envelope=dict(status="started" if operation == "start" else "stopped",
                      agent_action="start_runner" if operation == "start" and effective else "stop_runner"))
    if status == "decision_required":
        body["envelope"] = dict(status="decision_required", agent_action="ask_user", decision_token="new-challenge")
    if status == "rejected":
        body.pop("envelope")
        body.update(error_code="intent_revision_conflict", original_http_status=409)
    body.update(changes)
    return body


class Client:
    server = "https://unit.invalid"
    account_scope = "a" * 64

    def __init__(self):
        self.posts = []
        self.gets = []
        self.remote = {}
        self.send_hook = None
        self.get_hook = None

    def send(self, operation, request):
        self.posts.append((operation, deepcopy(request)))
        if self.send_hook:
            return self.send_hook(operation, request)
        body = receipt(operation, request)
        self.remote[request["intent_id"]] = deepcopy(body)
        return body

    def start_run_plan(self, **kw):
        return self.send("start", kw)

    def stop_run_plan(self, **kw):
        return self.send("stop", kw)

    def run_plan_intent_receipt(self, intent_id, **kw):
        self.gets.append((intent_id, kw))
        if self.get_hook:
            return self.get_hook(intent_id, kw)
        if intent_id not in self.remote:
            raise unknown(intent_id)
        value = deepcopy(self.remote[intent_id])
        value["idempotent_replay"] = True
        return intents._without_challenge(value)


def execute(home, client, **kw):
    args = dict(operation="start", request=START, expected_revision=7, local_intent="local-launch-1")
    args.update(kw)
    return intents.execute(home, client, **args)


def uncertain(home, client):
    def lost(operation, request):
        raise ApiError("request outcome unavailable")
    client.send_hook = lost
    with pytest.raises(ApiError, match="No durable receipt") as info:
        execute(home, client)
    assert info.value.code == "intent_unknown"
    return files(home)[0]


def test_persisted_private_request_precedes_network(tmp_path):
    client = Client()
    def send(operation, request):
        path, = files(tmp_path)
        saved = json.loads(path.read_text())
        assert saved["status"] == "pending"
        assert saved["request"] == request
        assert saved["request_fingerprint"] == intents.fingerprint(operation, request)
        if os.name != "nt":
            assert path.stat().st_mode & 0o077 == 0
        return receipt(operation, request)
    client.send_hook = send
    result = execute(tmp_path, client)
    assert result["current_effective"] is True
    assert json.loads(files(tmp_path)[0].read_text())["status"] == "received"


def test_failed_durable_write_never_posts(tmp_path, monkeypatch):
    client = Client()
    from dradar import run_plans
    monkeypatch.setattr(run_plans, "_atomic_json", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(ApiError) as info:
        execute(tmp_path, client)
    assert info.value.code == "local_intent_evidence_unavailable"
    assert client.posts == [] and client.gets == []


def test_lost_ack_recovers_exact_live_current_receipt(tmp_path):
    client = Client()
    def send(operation, request):
        client.remote[request["intent_id"]] = receipt(operation, request)
        raise ApiError("response lost")
    client.send_hook = send
    result = execute(tmp_path, client)
    assert result["current_effective"] and result["idempotent_replay"]
    assert len(client.posts) == len(client.gets) == 1
    assert client.gets[0][1] == dict(plan_id=PLAN, expected_fingerprint=result["request_fingerprint"])


def test_unknown_is_not_absence_and_only_explicit_same_id_retry_sends(tmp_path):
    client = Client()
    path = uncertain(tmp_path, client)
    before = path.read_bytes()
    with pytest.raises(ApiError):
        execute(tmp_path, client)
    assert len(client.posts) == 1 and path.read_bytes() == before
    client.send_hook = None
    response = execute(tmp_path, client, explicit_retry=True)
    assert len(client.posts) == 2
    assert client.posts[0] == client.posts[1]
    assert response["intent_id"] == path.stem
    assert len(files(tmp_path)) == 1


@pytest.mark.parametrize("change", [
    dict(request=dict(START, concurrency=3)), dict(expected_revision=8),
    dict(local_intent="new-local-launch"), dict(new_intent=True),
])
def test_unknown_changed_payload_revision_or_local_identity_cannot_create_id(tmp_path, change):
    client = Client()
    path = uncertain(tmp_path, client)
    before = path.read_bytes()
    with pytest.raises(ApiError):
        execute(tmp_path, client, explicit_retry=True, **change)
    assert len(client.posts) == 1 and files(tmp_path) == [path]
    assert path.read_bytes() == before and len(client.gets) == 2


@pytest.mark.parametrize("change", [dict(intent_id="e" * 32), dict(applied=False),
    dict(schema_version=True), dict(retry_same_intent_only=1), dict(intent_status="rejected")])
def test_malformed_or_foreign_404_cannot_trigger_explicit_replay(tmp_path, change):
    client = Client()
    uncertain(tmp_path, client)
    def get(intent_id, _):
        raise unknown(intent_id, **change)
    client.get_hook = get
    with pytest.raises(ApiError):
        execute(tmp_path, client, explicit_retry=True)
    assert len(client.posts) == 1


def test_stop_is_independent_of_pending_start(tmp_path):
    client = Client()
    start_path = uncertain(tmp_path, client)
    start_before = start_path.read_bytes()
    client.send_hook = None
    result = execute(tmp_path, client, operation="stop", request=STOP, local_intent="local-stop-1")
    assert result["intent_status"] == "applied"
    assert len(files(tmp_path)) == 2 and start_path.read_bytes() == start_before


def test_new_explicit_stop_does_not_refresh_unknown_old_stop(tmp_path):
    client = Client()
    client.send_hook = lambda *_: (_ for _ in ()).throw(ApiError("lost stop"))
    with pytest.raises(ApiError):
        execute(tmp_path, client, operation="stop", request=STOP, local_intent="stop-1")
    with pytest.raises(ApiError):
        execute(tmp_path, client, operation="stop", request=STOP, expected_revision=8,
                local_intent="stop-2", explicit_retry=True)
    assert len(client.posts) == 1 and len(files(tmp_path)) == 1


def test_refreshed_credentials_cannot_bypass_unknown_same_plan(tmp_path):
    client = Client()
    uncertain(tmp_path, client)
    replacement = Client()
    replacement.account_scope = "b" * 64
    with pytest.raises(ApiError) as info:
        execute(tmp_path, replacement, local_intent="new-local")
    assert info.value.code == "intent_scope_mismatch" and replacement.posts == []
    result = intents.reconcile_saved(tmp_path, replacement, plan_id=PLAN)
    assert result[0]["code"] == "intent_scope_mismatch" and replacement.gets == []


def test_definitive_cas_rejection_is_saved_without_envelope_or_refresh(tmp_path):
    client = Client()
    def send(operation, request):
        body = receipt(operation, request, status="rejected", device_intent_revision=9)
        client.remote[request["intent_id"]] = deepcopy(body)
        raise ApiError("conflict", code="intent_revision_conflict", status_code=409, payload=body)
    client.send_hook = send
    with pytest.raises(ApiError) as info:
        execute(tmp_path, client)
    assert info.value.code == "intent_revision_conflict" and client.gets == []
    saved = json.loads(files(tmp_path)[0].read_text())
    assert saved["status"] == "received" and saved["receipt"]["intent_status"] == "rejected"
    with pytest.raises(ApiError) as again:
        execute(tmp_path, client, expected_revision=9, explicit_retry=True, new_intent=True)
    assert again.value.code == "intent_revision_conflict"
    assert len(client.posts) == 1 and len(files(tmp_path)) == 1


def test_applied_historical_get_does_not_use_cached_launch_authority(tmp_path):
    client = Client()
    original = execute(tmp_path, client)
    body = client.remote[original["intent_id"]]
    body.update(current_effective=False, device_intent_revision=9)
    body["envelope"] = dict(status="stopped", agent_action="stop_runner")
    recovered = execute(tmp_path, client, explicit_retry=True)
    assert not recovered["current_effective"]
    assert recovered["envelope"]["agent_action"] == "stop_runner"
    assert len(client.posts) == 1 and len(client.gets) == 1
    client.remote.clear()
    with pytest.raises(ApiError):
        execute(tmp_path, client, explicit_retry=True)
    assert len(client.posts) == 1  # Even a later 404 cannot erase a known decision.


def test_same_local_id_different_payload_is_not_a_new_intent(tmp_path):
    client = Client()
    execute(tmp_path, client)
    with pytest.raises(ApiError) as info:
        execute(tmp_path, client, request=dict(START, concurrency=3), explicit_retry=True)
    assert info.value.code == "intent_request_conflict"
    assert len(client.posts) == 1 and len(files(tmp_path)) == 1


def test_pending_old_local_is_reconciled_but_not_attached_to_new_local(tmp_path):
    client = Client()
    path = uncertain(tmp_path, client)
    request = json.loads(path.read_text())["request"]
    client.remote[path.stem] = receipt("start", request)
    with pytest.raises(ApiError) as info:
        execute(tmp_path, client, local_intent="another-local")
    assert info.value.code == "intent_request_conflict"
    assert len(client.posts) == 1
    assert json.loads(path.read_text())["status"] == "received"


def test_new_local_action_after_known_terminal_get_uses_new_id(tmp_path):
    client = Client()
    first = execute(tmp_path, client)
    second = execute(tmp_path, client, local_intent="new-local", expected_revision=8)
    assert second["intent_id"] != first["intent_id"]
    assert client.posts[1][1]["expected_intent_revision"] == 8


def test_decision_history_has_no_challenge_and_only_explicit_new_intent_mints_one(tmp_path):
    client = Client()
    def send(operation, request):
        body = receipt(operation, request, status="decision_required")
        client.remote[request["intent_id"]] = deepcopy(body)
        return body
    client.send_hook = send
    first = execute(tmp_path, client)
    assert first["envelope"]["decision_token"] == "new-challenge"
    assert "new-challenge" not in files(tmp_path)[0].read_text()
    history = execute(tmp_path, client, explicit_retry=True)
    assert "decision_token" not in history["envelope"]
    assert len(client.posts) == 1
    second = execute(tmp_path, client, new_intent=True)
    assert first["intent_id"] != second["intent_id"] and len(client.posts) == 2
    client.send_hook = None
    accepted = execute(tmp_path, client, new_intent=True,
        request=dict(START, decision="join_existing", decision_token=second["envelope"]["decision_token"]))
    assert accepted["intent_status"] == "applied" and len(files(tmp_path)) == 3
    assert len({payload["intent_id"] for _, payload in client.posts}) == 3


def test_changed_revision_cannot_refresh_decision_challenge(tmp_path):
    client = Client()
    client.send_hook = lambda op, req: client.remote.setdefault(req["intent_id"], receipt(op, req, status="decision_required"))
    first = execute(tmp_path, client)
    client.remote[first["intent_id"]]["device_intent_revision"] = 8
    with pytest.raises(ApiError) as info:
        execute(tmp_path, client, new_intent=True)
    assert info.value.code == "intent_request_conflict" and len(client.posts) == 1


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("device_intent_revision", True), ("applied_intent_revision", 8.0),
    ("current_effective", 1), ("idempotent_replay", 0), ("current_start_intent_id", "b" * 32),
    ("request_fingerprint", "b" * 64), ("plan_id", "q" * 32), ("operation", "stop"),
    ("intent_status", []), ("applied", 1), ("admission_id", "bad"),
])
def test_malformed_receipt_never_settles_pending(tmp_path, field, value):
    client = Client()
    client.send_hook = lambda op, req: dict(receipt(op, req), **{field: value})
    with pytest.raises(ApiError) as info:
        execute(tmp_path, client)
    assert info.value.code == "intent_receipt_invalid"
    assert json.loads(files(tmp_path)[0].read_text())["status"] == "pending"


@pytest.mark.parametrize("bad", [
    dict(expected_revision=True), dict(expected_revision="7"), dict(expected_revision=-1),
    dict(request=dict(START, expected_generation=True)), dict(request=dict(START, concurrency=2.0)),
    dict(request=dict(START, unknown=1)), dict(request=dict(START, intent_id="a" * 32)),
    dict(request=dict(START, plan_id="short")), dict(explicit_retry=1), dict(new_intent=1),
])
def test_invalid_request_never_persists_or_sends(tmp_path, bad):
    client = Client()
    with pytest.raises(ApiError):
        execute(tmp_path, client, **bad)
    assert client.posts == [] and files(tmp_path) == []


@pytest.mark.parametrize("damage", ["json", "duplicate", "bool", "fingerprint", "permission", "receipt"])
def test_corrupt_original_is_preserved_and_blocks_new_id(tmp_path, damage):
    client = Client()
    path = uncertain(tmp_path, client)
    saved = json.loads(path.read_text())
    if damage == "json":
        path.write_text("{truncated")
    elif damage == "duplicate":
        path.write_text(path.read_text().replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'))
    elif damage == "bool":
        saved["request"]["expected_intent_revision"] = True
        path.write_text(json.dumps(saved))
    elif damage == "fingerprint":
        saved["request"]["concurrency"] = 3
        path.write_text(json.dumps(saved))
    elif damage == "permission":
        if os.name == "nt":
            pytest.skip("POSIX private file mode")
        path.chmod(0o644)
    else:
        saved["status"] = "received"
        saved["receipt"] = {"current_effective": True}
        path.write_text(json.dumps(saved))
    before = path.read_bytes()
    with pytest.raises(ApiError) as info:
        execute(tmp_path, client, local_intent="fresh")
    assert info.value.code == "local_intent_evidence_invalid"
    assert path.read_bytes() == before and len(client.posts) == 1 and len(files(tmp_path)) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX lock contention adapter")
def test_network_is_outside_index_lock_and_unknown_start_does_not_block_stop(tmp_path):
    import fcntl
    client = Client()
    entered, finish = threading.Event(), threading.Event()
    def assert_unlocked():
        lock = tmp_path / "run-plans" / "remote-intents" / "index.lock"
        with lock.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
    def send(operation, request):
        assert_unlocked()
        if operation == "start":
            entered.set()
            assert finish.wait(3)
        return receipt(operation, request)
    def get(intent_id, _):
        assert_unlocked()
        raise unknown(intent_id)
    client.send_hook, client.get_hook = send, get
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute, tmp_path, client)
        assert entered.wait(3)
        try:
            with pytest.raises(ApiError):
                execute(tmp_path, client)  # Pending is visible before the first POST completes.
            stopped = execute(tmp_path, client, operation="stop", request=STOP, local_intent="stop")
            assert stopped["intent_status"] == "applied"
            assert [op for op, _ in client.posts] == ["start", "stop"]
        finally:
            finish.set()
        future.result(timeout=3)


def test_fingerprint_matches_full_server_shape_and_secret_digest():
    request = dict(START, expected_intent_revision=7, intent_id="a" * 32, decision_token="sëcret")
    full = dict(schema_version=1, expected_generation=3, expected_intent_revision=7,
        intent_id="a" * 32, plan_id=PLAN, logical_session_id="session-0001",
        concurrency_mode="fixed", concurrency=2, decision=None, operation="start",
        decision_token_sha256=hashlib.sha256("sëcret".encode()).hexdigest())
    expected = hashlib.sha256(json.dumps(full, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    assert intents.fingerprint("start", request) == expected
    stop = dict(STOP, expected_intent_revision=7, intent_id="a" * 32)
    stop_full = dict(stop, schema_version=1, operation="stop", decision_token_sha256=None)
    assert intents.fingerprint("stop", stop) == hashlib.sha256(intents.canonical(stop_full)).hexdigest()
    assert intents.fingerprint("start", dict(request, decision_token="other")) != expected
