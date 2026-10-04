import json
import httpx
import pytest
from dradar.v2.client import Client, TransportUnknown, ProtocolError, RemoteError, server_url
from dradar.v2.journal import Journal, JournalConflict

BOOTSTRAP = {"schema_version": 2, "server_time": "2026-10-02T17:00:00Z", "capabilities": ["on-demand-v2"], "account": {"account_id": "synthetic"}}

def mock(handler):
    return httpx.MockTransport(lambda req: httpx.Response(200, json=BOOTSTRAP) if req.url.path == "/api/v2/bootstrap" else handler(req))

def test_lost_ack_replays_exact_request_and_does_not_rotate_id(tmp_path):
    bodies = []
    def server(req):
        bodies.append(req.content)
        if len(bodies) == 1:
            raise httpx.ReadTimeout("simulated lost ACK", request=req)
        return httpx.Response(200, json={"schema_version": 2, "server_time": "now", "request_id": json.loads(req.content)["request_id"], "accepted": True})
    journal = Journal(tmp_path)
    client = Client("http://127.0.0.1:1234", "synthetic-token", journal, transport=mock(server))
    with pytest.raises(TransportUnknown):
        client.mutate("start:a", "/api/v2/assignments/a/start", {"execution_id": "e"})
    assert journal.requests()[0].response is None
    assert journal.execution("a") is None
    client.close()
    recovered = Client("http://127.0.0.1:1234", "synthetic-token", Journal(tmp_path), transport=mock(server))
    assert recovered.mutate("start:a", "/api/v2/assignments/a/start", {"execution_id": "e"})["accepted"] is True
    assert bodies[0] == bodies[1]
    assert recovered.mutate("start:a", "/api/v2/assignments/a/start", {"execution_id": "e"})["accepted"] is True
    assert len(bodies) == 2
    recovered.close()

def test_no_request_before_journal_commit(tmp_path):
    journal = Journal(tmp_path)
    def server(req):
        saved = Journal(tmp_path).requests()
        assert len(saved) == 1 and saved[0].body_json.encode() == req.content
        return httpx.Response(200, json={"schema_version": 2, "server_time": "now", "request_id": json.loads(req.content)["request_id"], "ok": True})
    client = Client("http://localhost:1", "synthetic", journal, transport=mock(server))
    client.mutate("create", "/api/v2/runs", {"run_id": "r"})
    client.close()

def test_state_cannot_switch_servers(tmp_path):
    journal = Journal(tmp_path)
    Client("http://localhost:1", "synthetic", journal).close()
    with pytest.raises(JournalConflict):
        Client("http://localhost:2", "synthetic", journal)

@pytest.mark.parametrize("url", ["http://example.com", "https://user:password@example.com", "https://example.com?token=secret", "https://example.com/api", "file:///tmp/x"])
def test_invalid_server(url):
    with pytest.raises(ValueError):
        server_url(url)

@pytest.mark.parametrize("status,body,error", [(500, {}, TransportUnknown), (403, {}, RemoteError), (302, {}, RemoteError), (200, [], ProtocolError)])
def test_failures_never_become_success_receipts(tmp_path, status, body, error):
    client = Client("http://localhost:1", "synthetic", Journal(tmp_path), transport=mock(lambda _: httpx.Response(status, json=body)))
    with pytest.raises(error):
        client.mutate("create", "/api/v2/runs", {})
    assert client.journal.requests()[0].response is None
    client.close()

def test_path_does_not_allow_scope_escape(tmp_path):
    client = Client("http://localhost:1", "synthetic", Journal(tmp_path))
    with pytest.raises(ProtocolError):
        client.get("/api/v2/runs/../account")
    client.close()

def test_missing_capability_refuses_before_mutation(tmp_path):
    calls = []
    def server(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={**BOOTSTRAP, "capabilities": []})
    client = Client("http://localhost:1", "synthetic", Journal(tmp_path), transport=httpx.MockTransport(server))
    with pytest.raises(ProtocolError):
        client.mutate("create", "/api/v2/runs", {})
    assert calls == ["/api/v2/bootstrap"]
    client.close()

def test_protocol_header_and_uuid_hex(tmp_path):
    def server(req):
        assert req.headers["X-DRadar-Protocol"] == "on-demand-v2"
        return httpx.Response(200, json={"schema_version": 2, "server_time": "now", "request_id": json.loads(req.content)["request_id"], "ok": True})
    client = Client("http://localhost:1", "synthetic", Journal(tmp_path), transport=mock(server))
    client.mutate("create", "/api/v2/runs", {})
    request_id = client.journal.requests()[0].request_id
    assert len(request_id) == 32 and all(c in "0123456789abcdef" for c in request_id)
    client.close()


def test_busy_response_preserves_request_and_honors_retry_after(tmp_path):
    client = Client("http://localhost:1", "synthetic", Journal(tmp_path), transport=mock(lambda _: httpx.Response(429, headers={"Retry-After":"40"})))
    with pytest.raises(TransportUnknown) as error:
        client.mutate("claim", "/api/v2/runs/r/claim", {})
    assert error.value.retry_after_seconds >= 40
    assert client.journal.requests()[0].response is None
    client.close()
