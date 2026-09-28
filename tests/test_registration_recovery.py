"""Registration-only socket tests. No real tasks, services or model calls."""
import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from dradar import cancellation, registration
from dradar.api_client import ApiClient, ApiError
from dradar.registration import RegistrationWindow
from dradar.telemetry import RunnerTelemetry


@contextmanager
def fixture(tmp_path, monkeypatch, fault="normal", *, defer_abort=False):
    monkeypatch.setenv("NO_PROXY", "*")
    state = {"paths": [], "seqs": [], "events": [], "closed": False,
             "started": False, "hb": 0, "flight": 0,
             "start_payloads": [], "received_at": [],
             "start_received": threading.Event(), "close_received": threading.Event(),
             "late_done": threading.Event(), "late_status": None}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            data = (json.loads(raw) if self.headers.get("Content-Type", "").startswith("application/json")
                    else {k:v[0] for k,v in parse_qs(raw.decode()).items()})
            state["paths"].append(self.path)
            state["received_at"].append(time.monotonic())
            status, body = 200, {"ok": True}
            if self.path.endswith("/heartbeat"):
                state["hb"] += 1
                state["seqs"].append(data["seq"])
                if fault in ("first_disconnect", "heartbeat_duplicate", "disconnect_then_busy") and state["hb"] == 1:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if fault == "delay_first" and state["hb"] == 1:
                    time.sleep(registration.REGISTRATION_REQUEST_SECONDS + .2)
                if fault == "slow":
                    time.sleep(1.2)
                body = {"accepted": fault != "rejected", "stop_requested": fault == "stop"}
                if fault == "heartbeat_duplicate" and state["hb"] > 1:
                    body = {"accepted": False, "action": "continue", "batch_id": "b"*32}
                if fault in ("busy_once", "busy_long") and state["hb"] == 1:
                    status, body = 503, {"code": "mutation_busy", "retry_after_seconds": 0.1 if fault == "busy_once" else 60}
                if fault in ("known_busy_long", "disconnect_then_busy"):
                    status, body = 503, {"code": "mutation_busy", "retry_after_seconds": 60,
                                         "write_outcome": "not_executed"}
                if fault == "429":
                    status = 429
            elif self.path.endswith("/flight-events"):
                state["flight"] += 1
                ids = [e["event_id"] for e in data["events"]]
                state["events"].append(ids)
                if fault == "flight_disconnect" and state["flight"] == 1:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                body = {"acknowledged_event_ids": [] if fault == "wrong_ack" else ids}
            elif self.path.endswith("/started"):
                state["start_received"].set()
                state["start_payloads"].append(data)
                if fault == "cancel_late":
                    assert state["close_received"].wait(5)
                    state["late_status"] = 409 if state["closed"] else 200
                    state["started"] = not state["closed"]
                    state["late_done"].set()
                    status = state["late_status"]
                    body = {"ok": status == 200}
                else:
                    state["started"] = True
                    body = {"ok": True, "owner_epoch": 1}
                if fault in ("start_disconnect", "close_disconnect") or (
                        fault == "start_disconnect_once" and len(state["start_payloads"]) == 1) or (
                        fault == "start_disconnect_twice" and len(state["start_payloads"]) <= 2):
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if fault == "start_busy":
                    state["started"] = False
                    status, body = 503, {"code": "mutation_busy", "retry_after_seconds": 60,
                                         "write_outcome": "not_executed"}
            elif self.path.endswith("/close"):
                state["closed"] = True
                state["close_received"].set()
                if fault == "close_disconnect":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
            elif self.path.endswith("/stopped"):
                state["started"] = False
            raw = json.dumps(body).encode()
            if ((fault == "heartbeat_bad_json_once" and self.path.endswith("/heartbeat") and state["hb"] == 1)
                    or (fault == "flight_bad_json_once" and self.path.endswith("/flight-events") and state["flight"] == 1)
                    or (fault == "start_bad_json_once" and self.path.endswith("/started") and len(state["start_payloads"]) == 1)):
                raw = b'{"truncated":'
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Retry-After", str(body.get("retry_after_seconds", 60)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except OSError:
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api = ApiClient(f"http://127.0.0.1:{server.server_port}", "fixture", capabilities=())
    telemetry = RunnerTelemetry(api, home=tmp_path)
    telemetry.bind_batch("b"*32)
    telemetry.set_phase("building", "a"*32, 1)
    assignment = {"assignment_id": "a"*32, "owner_epoch":1, "agent":"codex"}
    window = RegistrationWindow(time.monotonic()+120, lambda: True, defer_abort=defer_abort)
    try:
        yield window, api, telemetry, assignment, state
    finally:
        api._client.close()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("fault", ["normal", "first_disconnect", "delay_first", "flight_disconnect"])
def test_exact_recovery_and_attempt_caps(tmp_path, monkeypatch, fault):
    with fixture(tmp_path, monkeypatch, fault) as (w, api, t, a, state):
        start = time.monotonic()
        assert w.bind(api,t,a)["ok"]
        w.finish()
        assert time.monotonic()-start < 15
        assert state["hb"] == (2 if fault in ("first_disconnect","delay_first") else 1)
        assert state["flight"] == (2 if fault=="flight_disconnect" else 1)
        assert len(set(state["seqs"])) == 1
        assert len({e[0] for e in state["events"]}) == 1
        assert state["paths"].count("/api/v1/assignment/started") == 1
        assert "_registration_start_uncertain" not in a
        assert not t._send_lock.locked()
        events = t.flight_recorder._load(t.flight_recorder.pending_path)
        assert any(e["event_type"] == "provider_started" for e in events)


@pytest.mark.parametrize("fault", ["429", "rejected", "stop", "wrong_ack"])
def test_refusals_are_terminal(tmp_path, monkeypatch, fault):
    with fixture(tmp_path, monkeypatch, fault) as (w, api, t, a, state):
        with pytest.raises(ApiError):
            w.bind(api,t,a)
        assert state["hb"] == 1 and state["flight"] <= 1
        assert not state["started"]
        assert not t._send_lock.locked()


@pytest.mark.parametrize("fault,fenced", [("start_disconnect",True),("close_disconnect",False)])
def test_ambiguous_start_requires_confirmed_close(tmp_path, monkeypatch, fault, fenced):
    from dradar.runloop import _mark_stopped_quietly
    with fixture(tmp_path, monkeypatch, fault) as (w, api, t, a, state):
        with pytest.raises(ApiError):
            w.bind(api,t,a)
        assert state["started"] and state["closed"]
        assert w._fenced == fenced
        assert bool(a.get("_registration_start_uncertain")) == (not fenced)
        assert state["paths"][-1] == "/api/v1/runner/close"
        w.abort()
        assert state["paths"].count("/api/v1/runner/close") == 1
        if not fenced:
            assert _mark_stopped_quietly(api,a) is False
            assert state["paths"][-1] == "/api/v1/runner/close"


def test_deferred_ambiguous_start_keeps_fence_obligation_for_owner(tmp_path, monkeypatch):
    with fixture(tmp_path, monkeypatch, "start_disconnect", defer_abort=True) as (w, api, t, a, state):
        with pytest.raises(ApiError):
            w.bind(api, t, a)
        assert state["started"] and not state["closed"]
        assert a["_registration_start_uncertain"] is True
        assert not w._abort_attempted and not w._fenced
        # The owning runner performs local cleanup here and then calls abort.
        w.abort()
        assert state["closed"] and w._fenced
        assert "_registration_start_uncertain" not in a
        w.abort()
        assert state["paths"].count("/api/v1/runner/close") == 1


def test_deferred_close_has_its_own_bounded_budget_after_local_teardown(tmp_path, monkeypatch):
    with fixture(tmp_path, monkeypatch, "start_disconnect", defer_abort=True) as (w, api, t, a, state):
        with pytest.raises(ApiError):
            w.bind(api, t, a)
        expired = time.monotonic() - 1
        w.deadline = expired
        start = time.monotonic()
        w.abort()
        assert time.monotonic() - start < registration.CLOSE_FENCE_SECONDS
        assert state["closed"] and w._fenced
        assert state["paths"].count("/api/v1/runner/close") == 1
        assert w.deadline == expired
        with pytest.raises(ApiError):
            w.finish()


def test_deferred_abort_still_fences_a_cancelled_late_start(tmp_path, monkeypatch):
    with fixture(tmp_path, monkeypatch, "cancel_late", defer_abort=True) as (w, api, t, a, state), cancellation.scope() as stop:
        def cancel_received_start():
            if state["start_received"].wait(5):
                stop.requested = True
        worker = threading.Thread(target=cancel_received_start)
        worker.start()
        try:
            with pytest.raises(KeyboardInterrupt):
                w.bind(api, t, a)
            assert not state["closed"]
            assert a["_registration_start_uncertain"] is True
            cancellation.protect_finalization(cancelled=True)
            w.abort()
            assert state["late_done"].wait(2)
            assert w._fenced and state["late_status"] == 409
            assert not state["started"]
        finally:
            worker.join(timeout=5)


def test_cooperative_cancel_cancels_and_awaits_inflight_request(tmp_path, monkeypatch):
    with fixture(tmp_path, monkeypatch, "slow") as (w,api,t,a,state), cancellation.scope() as stop:
        timer = threading.Timer(.1, lambda: setattr(stop,"requested",True))
        timer.start()
        start=time.monotonic()
        try:
            with pytest.raises(KeyboardInterrupt):
                w.bind(api,t,a)
            assert time.monotonic()-start < .5
            assert not state["started"] and state["flight"] == 0
            assert not t._send_lock.locked()
        finally:
            timer.join()


@pytest.mark.parametrize("lockname", ["_send_lock", "recorder", "process"])
def test_lock_wait_uses_operation_deadline(tmp_path, monkeypatch, lockname):
    from dradar.flight_recorder import _PROCESS_LOCK
    with fixture(tmp_path, monkeypatch) as (w,api,t,a,state):
        w.deadline=time.monotonic()+.15
        lock = t._send_lock if lockname=="_send_lock" else t.flight_recorder._lock if lockname=="recorder" else _PROCESS_LOCK
        lock.acquire()
        start=time.monotonic()
        try:
            with pytest.raises(ApiError,match="budget"):
                w.bind(api,t,a)
            assert time.monotonic()-start < .4
            assert not state["started"]
        finally:
            lock.release()


def test_local_ack_persistence_failure_never_starts(tmp_path, monkeypatch):
    with fixture(tmp_path, monkeypatch) as (w,api,t,a,state):
        def fail(*args): raise OSError("private local path")
        monkeypatch.setattr(t.flight_recorder,"_write_acknowledged_ids_unlocked", fail)
        with pytest.raises(ApiError,match="local data"):
            w.bind(api,t,a)
        assert state["flight"]==1 and not state["started"]
        assert t.worker_registration_diagnostic["registration_result"]=="flight_ack_persist_error"


def test_recorder_event_failure_is_named_without_leaking_local_error(tmp_path, monkeypatch):
    with fixture(tmp_path, monkeypatch) as (w, api, t, a, state):
        original = t.flight_recorder.record

        def fail_worker_registration(kind, *args, **kwargs):
            if kind == "worker_registered":
                raise OSError("private recorder path")
            return original(kind, *args, **kwargs)

        monkeypatch.setattr(t.flight_recorder, "record", fail_worker_registration)
        with pytest.raises(ApiError):
            w.bind(api, t, a)
        detail = t.worker_registration_diagnostic
        assert detail["registration_local_substage"] == "recorder_event_record"
        assert "private recorder path" not in json.dumps(detail)
        assert not state["started"]


def test_state_lock_failure_after_start_is_named(tmp_path, monkeypatch):
    with fixture(tmp_path, monkeypatch) as (w, api, t, a, state):
        after_start = {"value": False}
        original_request = w._start_request

        def mark_start(method, path, **kwargs):
            response = original_request(method, path, **kwargs)
            if path == "/api/v1/assignment/started":
                after_start["value"] = True
            return response

        original_checked_lock = registration._checked_lock

        def fail_after_start(lock, check=None):
            if after_start["value"] and lock is t._lock:
                raise OSError("state lock unavailable")
            return original_checked_lock(lock, check)

        monkeypatch.setattr(w, "_start_request", mark_start)
        monkeypatch.setattr(registration, "_checked_lock", fail_after_start)
        with pytest.raises(ApiError):
            w.bind(api, t, a)
        detail = t.worker_registration_diagnostic
        assert detail["registration_failure_stage"] == "local_state"
        assert detail["registration_local_substage"] == "state_lock"
        assert "state lock unavailable" not in json.dumps(detail)
        assert state["started"]


def test_worker_deadline_and_expiry_are_fail_closed():
    for value in (None, float("nan"), time.monotonic()-1, time.monotonic()+130):
        with pytest.raises(ApiError): RegistrationWindow(value, lambda: True)
    now=time.monotonic()
    w=RegistrationWindow(now+1.1,lambda:True)
    assert w.deadline <= now+.11


def test_late_gate_publish_is_rejected_before_atomic_rename(tmp_path):
    from dradar.runner import _materialize_shared_file
    w=RegistrationWindow(time.monotonic()+120,lambda:True)
    w.deadline=time.monotonic()-1
    path=tmp_path/"gate"
    with pytest.raises(ApiError):
        _materialize_shared_file(path,b"permit",check=w.check)
    assert not path.exists()


@pytest.mark.parametrize("fault", ["busy_once", "heartbeat_duplicate", "start_disconnect_once",
                                  "heartbeat_bad_json_once", "flight_bad_json_once", "start_bad_json_once"])
def test_congestion_recovery_preserves_operation_identity(tmp_path, monkeypatch, fault):
    with fixture(tmp_path, monkeypatch, fault) as (w, api, t, a, state):
        assert w.bind(api, t, a)["ok"] is True
        w.finish()
        assert len(set(state["seqs"])) == 1
        assert len({e[0] for e in state["events"]}) == 1
        if fault in ("start_disconnect_once", "start_bad_json_once"):
            assert len(state["start_payloads"]) == 2
            assert state["start_payloads"][0] == state["start_payloads"][1]
        if fault == "busy_once":
            assert state["received_at"][1] - state["received_at"][0] >= 0.1
        assert not state["closed"]
        assert "_registration_start_uncertain" not in a


def test_busy_retry_after_is_not_shortened_to_fit_budget(tmp_path, monkeypatch):
    with fixture(tmp_path, monkeypatch, "busy_long") as (w, api, t, a, state):
        with pytest.raises(ApiError) as failure:
            w.bind(api, t, a)
        assert failure.value.registration_reason == "budget_expired"
        assert state["hb"] == 1
        assert not state["started"]


def test_no_request_without_a_complete_attempt_budget(tmp_path, monkeypatch):
    with fixture(tmp_path, monkeypatch) as (w, api, t, a, state):
        w.deadline = time.monotonic() + 3.5
        with pytest.raises(ApiError) as failure:
            w.bind(api, t, a)
        assert failure.value.registration_reason == "budget_expired"
        assert state["hb"] == 0
        assert not state["started"]
        assert not hasattr(failure.value, "write_outcome")


@pytest.mark.parametrize("fault,expected", [
    ("busy_long", "unknown_unreconciled"),
    ("known_busy_long", "busy_not_executed"),
    ("disconnect_then_busy", "unknown_unreconciled"),
    ("start_busy", "busy_not_executed"),
])
def test_registration_reports_exact_failed_operation(tmp_path, monkeypatch, fault, expected, capsys):
    with fixture(tmp_path, monkeypatch, fault, defer_abort=True) as (w, api, t, a, state):
        with pytest.raises(ApiError) as failure:
            w.bind(api, t, a)
        outcome = failure.value.write_outcome
        assert outcome["status"] == expected
        assert outcome["execution_allowed"] is False
        assert outcome["request_identity"]["session_id"] == t.session_id
        if fault == "start_busy":
            assert outcome["phase"] == "started"
            assert outcome["request_identity"]["assignment_id"] == a["assignment_id"]
            assert outcome["request_identity"]["worker_event_id"] == state["start_payloads"][0]["worker_event_id"]
        else:
            assert outcome["phase"] == "heartbeat"
            assert outcome["request_identity"]["seq"] == state["seqs"][0]
        assert not w.gate_published
        assert not state["started"]
        emitted = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{')]
        assert any(row.get("write_recovery") == outcome for row in emitted)


def test_two_lost_started_receipts_use_remaining_window_before_fence(tmp_path, monkeypatch):
    # Scale only request duration, preserving the actual socket path and
    # original registration deadline checks; no provider process is started.
    monkeypatch.setattr(registration, "REGISTRATION_REQUEST_SECONDS", .1)
    monkeypatch.setattr(registration, "HANDOFF_MARGIN_SECONDS", .1)
    with fixture(tmp_path, monkeypatch, "start_disconnect_twice") as (w, api, t, a, state):
        w.deadline = time.monotonic() + 2
        assert w.bind(api, t, a)["ok"] is True
        w.finish()
        assert len(state["start_payloads"]) == 3
        assert all(payload == state["start_payloads"][0] for payload in state["start_payloads"])
        assert not state["closed"]


def test_last_raw_started_response_keeps_structured_busy(tmp_path, monkeypatch):
    with fixture(tmp_path, monkeypatch, "start_busy") as (w, api, t, a, state):
        w.api = api
        body = {"assignment_id": a["assignment_id"], "session_id": t.session_id,
                "worker_event_id": "e" * 32}
        async def send():
            async with w._client() as client:
                return await w._request(client, "/api/v1/assignment/started",
                                        attempts=1, raw_response=True, data=body)
        with pytest.raises(ApiError) as failure:
            asyncio.run(send())
        assert failure.value.status_code == 503
        assert failure.value.write_outcome["status"] == "busy_not_executed"
        assert failure.value.write_outcome["request_identity"] == body
        assert len(state["start_payloads"]) == 1


def test_registration_total_request_deadline_cancels_a_dribbling_response(monkeypatch):
    # A transport can keep every individual read below HTTPX's timeout while
    # the whole request exceeds it. The registration window caps that too.
    monkeypatch.setattr(registration, "REGISTRATION_REQUEST_SECONDS", .05)
    cancelled = []
    class Client:
        async def request(self, *args, **kwargs):
            try:
                for _ in range(20):
                    await asyncio.sleep(.02)
            finally:
                cancelled.append(True)
    w = RegistrationWindow(time.monotonic() + 30, lambda: True)
    started = time.monotonic()
    with pytest.raises(ApiError) as failure:
        asyncio.run(w._request(Client(), "/api/v1/runner/heartbeat", attempts=1,
                               json={"session_id": "a" * 32, "batch_id": "b" * 32, "seq": 1}))
    assert .04 <= time.monotonic() - started < .3
    assert cancelled == [True]
    assert failure.value.write_outcome["status"] == "unknown_unreconciled"


def test_continuous_lost_started_receipts_have_bounded_spaced_reconciliation(tmp_path, monkeypatch):
    from functools import partial
    # This fixture uses plain loopback HTTP. Avoid rebuilding platform TLS
    # certificate stores inside its deliberately subsecond retry budget.
    monkeypatch.setattr(registration.httpx, "AsyncClient",
                        partial(registration.httpx.AsyncClient, verify=False))
    monkeypatch.setattr(registration, "REGISTRATION_REQUEST_SECONDS", .1)
    monkeypatch.setattr(registration, "HANDOFF_MARGIN_SECONDS", .1)
    with fixture(tmp_path, monkeypatch, "start_disconnect", defer_abort=True) as (w, api, t, a, state):
        w.deadline = time.monotonic() + .8
        with pytest.raises(ApiError) as failure:
            w.bind(api, t, a)
        assert failure.value.registration_reason == "transport_error"
        assert failure.value.retry_exhausted is True
        assert 2 <= len(state["start_payloads"]) <= 4
        assert not state["closed"]
        # A further spaced attempt plus its full request cannot fit.
        assert w.deadline - time.monotonic() < .4
        assert all(payload == state["start_payloads"][0] for payload in state["start_payloads"])
        w.abort()
        assert state["closed"]
