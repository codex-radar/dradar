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
def fixture(tmp_path, monkeypatch, fault="normal"):
    monkeypatch.setenv("NO_PROXY", "*")
    state = {"paths": [], "seqs": [], "events": [], "closed": False,
             "started": False, "hb": 0, "flight": 0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            data = (json.loads(raw) if self.headers.get("Content-Type", "").startswith("application/json")
                    else {k:v[0] for k,v in parse_qs(raw.decode()).items()})
            state["paths"].append(self.path)
            status, body = 200, {"ok": True}
            if self.path.endswith("/heartbeat"):
                state["hb"] += 1
                state["seqs"].append(data["seq"])
                if fault == "first_disconnect" and state["hb"] == 1:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if fault == "delay_first" and state["hb"] == 1:
                    time.sleep(3.2)
                if fault == "slow":
                    time.sleep(1.2)
                body = {"accepted": fault != "rejected", "stop_requested": fault == "stop"}
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
                state["started"] = True
                body = {"ok": True, "owner_epoch": 1}
                if fault in ("start_disconnect", "close_disconnect"):
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
            elif self.path.endswith("/close"):
                state["closed"] = True
                if fault == "close_disconnect":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Retry-After", "60")
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
    window = RegistrationWindow(time.monotonic()+120, lambda: True)
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
        assert state["seqs"] == sorted(set(state["seqs"]))
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
