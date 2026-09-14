"""Transport failures must survive the CLI exit and startup-report boundary."""

from types import SimpleNamespace

import httpx
import pytest

from dradar import fleet, runloop
from dradar.api_client import ApiClient, ApiError


@pytest.fixture
def startup_report(tmp_path, monkeypatch):
    batch_id = "550e8400e29b41d4a716446655440000"
    fleet._prepare_dirs(tmp_path)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setenv(fleet.CONTROLLER_ID_ENV, "test-controller")
    monkeypatch.setenv(fleet.POOL_BATCH_ENV, batch_id)
    monkeypatch.setenv(
        fleet.POOL_STARTUP_FILE_ENV,
        str(fleet._pool_startup_path(tmp_path, batch_id)),
    )
    monkeypatch.setattr(fleet, "controller_matches", lambda *_args: True)

    def publish(error):
        runloop._publish_fleet_startup_failure(
            SimpleNamespace(worker_child=False, fleet_pool=True, batch_id=batch_id),
            error,
        )
        return fleet._read_json(fleet._pool_startup_path(tmp_path, batch_id))

    return publish


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("The read operation timed out"),
        httpx.ConnectError("private auth endpoint in docker environment"),
        httpx.WriteTimeout("private request detail"),
    ],
)
def test_checkout_transport_failure_keeps_its_cause_and_safe_startup_report(
    failure, startup_report,
):
    requests = []

    def server(request):
        requests.append(request)
        raise failure

    client = ApiClient(
        "https://private.example.test", "",
        transport=httpx.MockTransport(server), capabilities=(),
    )
    try:
        with pytest.raises(ApiError) as api_failure:
            client.checkout(session_id="test-session")
    finally:
        client._client.close()

    # Exercise the helper outside an except block as well: relying on the
    # ambient exception context would silently discard the typed cause here.
    with pytest.raises(SystemExit) as exit_failure:
        runloop._exit_for(api_failure.value)

    assert len(requests) == 1  # A lost response must not replay checkout.
    assert exit_failure.value.__cause__ is api_failure.value
    assert "server may have processed" in str(exit_failure.value)
    assert "held leases stay active" not in str(exit_failure.value)

    report = startup_report(exit_failure.value)
    assert report["status"] == "failed"
    assert report["error_code"] == "api_connection_failed"
    assert report["retryable"] is True
    assert "完整响应" in report["user_message"]
    assert "可能已经处理" in report["user_message"]
    assert "private" not in report["user_message"]
    assert "docker" not in report["user_message"].lower()


def test_direct_api_transport_failure_uses_the_same_startup_report(startup_report):
    report = startup_report(ApiError("private host connection failed"))

    assert report["error_code"] == "api_connection_failed"
    assert "private" not in report["user_message"]


@pytest.mark.parametrize("status_code", [401, 403, 429, 503])
def test_received_http_response_is_not_a_transport_failure(
    startup_report, status_code,
):
    error = ApiError("HTTP response said read timeout", status_code=status_code)
    try:
        raise SystemExit("stopped") from error
    except SystemExit as exited:
        report = startup_report(exited)

    assert report["error_code"] != "api_connection_failed"


def test_worker_transport_failure_marker_does_not_contain_private_details(
    monkeypatch, tmp_path,
):
    marker = tmp_path / "worker.started"
    marker.write_text("preparing", encoding="utf-8")
    monkeypatch.setenv(runloop._POOL_WORKER_ACTIVITY_ENV, str(marker))

    runloop._publish_fleet_startup_failure(
        SimpleNamespace(worker_child=True, fleet_pool=False),
        ApiError("private connection details"),
    )

    assert marker.read_text(encoding="utf-8") == "preparing:startup-network-unavailable"
