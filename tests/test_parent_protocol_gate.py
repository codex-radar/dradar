"""A pool must reject an old Server before it can prepare or claim work."""

import httpx
import pytest

from dradar import runloop
from dradar.api_client import ApiClient, ApiError
from test_workers import _args, _patch_pool_setup, _Process


CAPABILITIES = {
    "schema_version": 1,
    "capabilities": ["runner-reservation-v1"],
    "stop_generation_cas": True,
    "close_releases_capacity": False,
}


@pytest.mark.parametrize("workers", [1, 2, "auto", "prepared"])
@pytest.mark.parametrize("status,body", [
    (404, {"detail": "Not Found"}),
    (200, {**CAPABILITIES, "close_releases_capacity": True}),
    (503, {"detail": "maintenance"}),
])
def test_incompatible_server_stops_before_parent_side_effects(
        monkeypatch, tmp_path, workers, status, body):
    _patch_pool_setup(monkeypatch)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "preflight_artifact_platform", lambda: None)
    requests = []

    def request(req):
        requests.append((req.method, req.url.path))
        assert requests == [("GET", "/api/v1/run-plans/capabilities")]
        return httpx.Response(status, json=body)

    client = ApiClient("https://fixture.invalid", "fixture",
                       capabilities=(), transport=httpx.MockTransport(request))
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: client)

    def forbidden(*_a, **_k):
        pytest.fail("parent acted before the Server admitted execution")

    for name in ("_preflight_scoped_provider", "_selected_tasks_root",
                 "acquire_run_lock", "_prepare_batch", "RunnerTelemetry"):
        monkeypatch.setattr(runloop, name, forbidden)
    monkeypatch.setattr("dradar.capacity.inspect_capacity", forbidden)
    monkeypatch.setattr(runloop.subprocess, "Popen", forbidden)
    with pytest.raises(ApiError) as caught:
        if workers == "prepared":
            runloop._run_worker_pool(
                _args(workers=2), prepared=({}, client, [{"assignment_id": "held"}]),
            )
        else:
            runloop.cmd_go(_args(workers=workers, auto=2))
    assert caught.value.status_code == (426 if status == 200 else status)
    assert requests == [("GET", "/api/v1/run-plans/capabilities")]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("workers", [2, "auto", "prepared"])
def test_modern_server_allows_parent_after_protocol_check(
        monkeypatch, tmp_path, workers):
    _patch_pool_setup(monkeypatch, active_count=2)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    order = []

    def request(req):
        if req.url.path == "/api/v1/run-plans/capabilities":
            order.append("protocol")
            return httpx.Response(200, json=CAPABILITIES)
        assert req.method == "GET" and req.url.path == "/api/v1/assignment"
        return httpx.Response(200, json={"active": []})

    client = ApiClient("https://fixture.invalid", "fixture",
                       capabilities=(), transport=httpx.MockTransport(request))
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: client)
    monkeypatch.setattr(runloop, "_preflight_scoped_provider",
                        lambda *_a: order.append("provider_preflight"))

    def capacity(*_a, **_k):
        from dradar.capacity import CapacityReport
        order.append("capacity")
        return CapacityReport(
            recommended_workers=2, docker_cpus=8, docker_memory_gib=16,
            disk_free_gib=100, account_limit=5, held_tasks=2, task_limit=2,
            cpu_limit=4, memory_limit=2, disk_limit=7,
        )

    monkeypatch.setattr("dradar.capacity.inspect_capacity", capacity)

    def spawn(command, env, **kwargs):
        order.append("spawn")
        return _Process(command, env, **kwargs)

    monkeypatch.setattr(runloop.subprocess, "Popen", spawn)
    prepared = ({}, client, [{"assignment_id": "one"}, {"assignment_id": "two"}])
    assert runloop._run_worker_pool(
        _args(workers=2 if workers == "prepared" else workers),
        prepared=prepared if workers == "prepared" else None,
    ) == 0
    assert order[0] == "protocol"
    assert order.count("protocol") == 1
    assert order.count("spawn") == 2
    if workers != "prepared":
        assert order[1] == "provider_preflight"
    if workers == "auto":
        assert order[2] == "capacity"
