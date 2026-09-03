"""Wire contract of ApiClient over httpx.MockTransport: the status_code
attached to ApiError (the 409/410 pending-ledger pruning branches on it),
the submit() multipart shape, and the Authorization header rules."""

import json
import urllib.parse
from email.utils import formatdate

import httpx
import pytest

from dradar import __version__
from dradar.api_client import ApiClient, ApiError, normalize_batch_id
from dradar.providers import DEEPSEEK_CAPABILITY, TASK_PACKAGE_SYNC_CAPABILITY
from dradar.submission_intent import (
    UPLOAD_INTENT_VERSION,
    submission_payload_sha256,
)


def _client(handler, token="drt_test"):
    return ApiClient("https://api.example.com", token,
                     transport=httpx.MockTransport(handler))


def test_version_is_always_sent_and_capabilities_remain_sparse():
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(handler), capabilities=(),
    ).whoami()
    ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(handler),
        capabilities=(DEEPSEEK_CAPABILITY,),
    ).whoami()

    assert seen[0]["x-dradar-client-version"] == __version__
    assert "x-dradar-capabilities" not in seen[0]
    assert seen[1]["x-dradar-capabilities"] == DEEPSEEK_CAPABILITY


def test_capability_snapshot_is_exposed_for_worker_inheritance():
    client = ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"ok": True})
        ),
        capabilities=("z-last", "a-first", "a-first"),
    )
    assert client.capabilities == ("a-first", "z-last")


def test_default_client_advertises_task_package_sync_to_old_server():
    seen = []

    def old_server(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, json={"assignment": None})

    client = ApiClient(
        "https://old.example.com", "drt_test",
        transport=httpx.MockTransport(old_server),
    )
    assert client.get_assignment() == {"assignment": None}
    advertised = set(seen[0]["x-dradar-capabilities"].split(","))
    assert TASK_PACKAGE_SYNC_CAPABILITY in advertised


def test_http_error_attaches_status_code_and_detail():
    def handler(request):
        return httpx.Response(409, json={
            "detail": "cell went stale", "code": "cell_unavailable",
        })

    with pytest.raises(ApiError) as ei:
        _client(handler).get_assignment()
    assert ei.value.status_code == 409
    assert ei.value.code == "cell_unavailable"
    assert "cell went stale" in str(ei.value)


def test_http_error_preserves_required_capability():
    def handler(request):
        return httpx.Response(426, json={
            "detail": "provider is not ready",
            "code": "provider_capability_required",
            "required_capability": "antigravity-ready-v1",
        })

    with pytest.raises(ApiError) as ei:
        _client(handler).get_assignment()
    assert ei.value.status_code == 426
    assert ei.value.code == "provider_capability_required"
    assert ei.value.required_capability == "antigravity-ready-v1"


def test_legacy_http_error_without_code_remains_compatible():
    def handler(request):
        return httpx.Response(409, json={"detail": "legacy conflict"})

    with pytest.raises(ApiError) as ei:
        _client(handler).get_assignment()
    assert ei.value.status_code == 409
    assert ei.value.code is None
    assert "legacy conflict" in str(ei.value)


def test_transport_failure_has_no_status_code():
    def handler(request):
        raise httpx.ConnectError("name resolution failed")

    with pytest.raises(ApiError) as ei:
        _client(handler).whoami()
    assert ei.value.status_code is None
    assert ei.value.code is None
    assert "cannot reach" in str(ei.value)


def test_429_honors_retry_after_with_jitter_before_retrying():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) < 3:
            return httpx.Response(
                429, headers={"Retry-After": "2"},
                json={"detail": "rate limited — slow down"},
            )
        return httpx.Response(200, json={"nickname": "vol"})

    client = _client(handler)
    waits = []
    client._sleep = waits.append
    client._jitter = lambda _low, _high: 0.25

    assert client.whoami() == {"nickname": "vol"}
    assert calls == ["/api/v1/whoami"] * 3
    assert waits == [2.25, 2.25]


def test_429_retry_is_bounded_and_exposes_retry_after():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, headers={"Retry-After": "9999"},
            json={"detail": "rate limited — slow down"},
        )

    client = _client(handler)
    waits = []
    client._sleep = waits.append
    client._jitter = lambda _low, _high: 0

    with pytest.raises(ApiError) as ei:
        client.whoami()
    assert calls == 6
    assert waits == [60.0] * 5
    assert ei.value.status_code == 429
    assert ei.value.retry_after == 60.0


def test_run_plan_interactions_surface_rate_limit_without_sleeping():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, headers={"Retry-After": "30"},
            json={"detail": "try again shortly"},
        )

    client = _client(handler)
    waits = []
    client._sleep = waits.append

    with pytest.raises(ApiError) as raised:
        client.start_run_plan(
            plan_id="plan-1", logical_session_id="session-1",
            concurrency_mode="fixed", concurrency=1,
        )

    assert calls == 1
    assert waits == []
    assert raised.value.status_code == 429
    assert raised.value.retry_after == 30.0


def test_multipart_submission_can_retry_after_429(tmp_path):
    bodies = []

    def handler(request):
        bodies.append(request.read())
        if len(bodies) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(
            200, json={"submission_id": "s1", "grade_status": "pending"},
        )

    patch = tmp_path / "model.patch"
    patch.write_text("diff")
    client = _client(handler)
    client._sleep = lambda _seconds: None
    client._jitter = lambda _low, _high: 0

    ack = client.submit("a1", "nonce", patch, None, None, {})
    assert ack["submission_id"] == "s1"
    assert len(bodies) == 2
    assert all(b'name="patch"' in body for body in bodies)


def test_submission_maintenance_honors_http_date_retry_after(tmp_path):
    calls = []
    wall_time = 1_700_000_000.0

    def handler(request):
        calls.append(request.read())
        if len(calls) == 1:
            return httpx.Response(
                503,
                headers={
                    "Retry-After": formatdate(wall_time + 7, usegmt=True),
                },
                json={
                    "detail": "deployment in progress",
                    "code": "deployment_maintenance",
                },
            )
        return httpx.Response(
            200, json={"submission_id": "s1", "grade_status": "pending"},
        )

    patch = tmp_path / "model.patch"
    patch.write_text("diff")
    client = _client(handler)
    now = [0.0]
    waits = []
    client._wall_time = lambda: wall_time
    client._monotonic = lambda: now[0]

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    client._sleep = sleep

    ack = client.submit("a1", "nonce", patch, None, None, {})
    assert ack["submission_id"] == "s1"
    assert waits == [7.0]
    assert len(calls) == 2
    assert all(b'name="assignment_id"' in body for body in calls)
    assert all(b'name="patch"' in body and b"diff" in body for body in calls)


def test_submission_write_does_not_retry_unrelated_503(tmp_path):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            headers={"Retry-After": "1"},
            json={
                "detail": "upstream unavailable",
                "code": "upstream_unavailable",
            },
        )

    patch = tmp_path / "model.patch"
    patch.write_text("diff")
    client = _client(handler)
    waits = []
    client._sleep = waits.append

    with pytest.raises(ApiError) as raised:
        client.submit("a1", "nonce", patch, None, None, {})

    assert raised.value.status_code == 503
    assert raised.value.code == "upstream_unavailable"
    assert calls == 1
    assert waits == []


def test_submission_maintenance_refuses_retry_after_above_single_wait_limit(
    tmp_path,
):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            headers={"Retry-After": "61"},
            json={
                "detail": "deployment in progress",
                "code": "deployment_maintenance",
            },
        )

    patch = tmp_path / "model.patch"
    patch.write_text("diff")
    client = _client(handler)
    waits = []
    client._sleep = waits.append

    with pytest.raises(ApiError, match="safe single-wait limit exceeded") as raised:
        client.submit("a1", "nonce", patch, None, None, {})

    assert raised.value.status_code == 503
    assert raised.value.code == "deployment_maintenance"
    assert raised.value.retry_after == 61.0
    assert calls == 1
    assert waits == []


def test_submission_upload_intent_and_submit_share_exact_content_hash(tmp_path):
    requests = []

    def handler(request):
        requests.append((request.url.path, request.read()))
        if request.url.path.endswith("submission-upload-intents"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            200, json={"submission_id": "s1", "grade_status": "pending"},
        )

    patch = tmp_path / "model.patch"
    patch.write_bytes(b"diff --git a/x b/x\n")
    meta = {"z": 1, "a": "值"}
    client = _client(handler)
    intent_id = submission_payload_sha256(
        assignment_id="a1", session_id="session-1234",
        resume_generation=2, outcome="completed", meta=meta,
        patch=patch, trajectory=None, result=None, trajectory_bundle=None,
    )
    registered = client.register_submission_upload_intent(
        "a1", "nonce", "session-1234", 2, intent_id,
    )
    assert registered == intent_id
    client.submit(
        "a1", "nonce", patch, None, None, meta,
        resume_generation=2, upload_intent_id=intent_id,
    )

    intent_form = urllib.parse.parse_qs(requests[0][1].decode())
    assert requests[0][0] == "/api/v1/submission-upload-intents"
    assert intent_form["upload_intent_id"] == [intent_id]
    assert intent_form["session_id"] == ["session-1234"]
    assert intent_form["intent_version"] == [UPLOAD_INTENT_VERSION]
    assert len(intent_id) == 64
    assert requests[1][0] == "/api/v1/submissions"
    assert intent_id.encode() in requests[1][1]
    assert json.dumps(meta).encode() in requests[1][1]


def test_submission_upload_salvage_rebind_wire_contract():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["form"] = urllib.parse.parse_qs(request.read().decode())
        return httpx.Response(200, json={
            "ok": True,
            "owner_epoch": 8,
            "salvage_session_id": "salvage-session-0001",
        })

    response = _client(handler).rebind_submission_upload_salvage(
        "a1", "nonce", "source-session-0001", 3, 7,
        "salvage-session-0001",
    )

    assert response["owner_epoch"] == 8
    assert seen["path"] == "/api/v1/submission-upload-salvage/rebind"
    assert seen["form"] == {
        "assignment_id": ["a1"],
        "nonce": ["nonce"],
        "source_session_id": ["source-session-0001"],
        "source_owner_epoch": ["3"],
        "expected_owner_epoch": ["7"],
        "salvage_session_id": ["salvage-session-0001"],
    }


def test_suggest_passes_n_and_returns_cells():
    seen = {}

    def handler(request):
        seen["path"] = str(request.url)
        return httpx.Response(200, json={"cells": [{"task_id": "t1"}]})

    got = _client(handler).suggest(3)
    assert seen["path"].endswith("/api/v1/suggest?n=3")
    assert got == {"cells": [{"task_id": "t1"}]}


def test_table_fetches_public_full_board():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json={"cells": {"t1|m|low": {"st": "open"}}})

    got = _client(handler).table()
    assert seen["path"] == "/api/v1/table"
    assert got["cells"]["t1|m|low"]["st"] == "open"


def test_benchmark_catalog_and_authenticated_stream_download(tmp_path):
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("authorization")))
        if request.url.path == "/api/v1/benchmarks":
            return httpx.Response(200, json={"benchmarks": []})
        return httpx.Response(
            200, content=b"bundle", headers={"X-Content-SHA256": "abc"})

    client = _client(handler)
    assert client.benchmarks() == {"benchmarks": []}
    destination = tmp_path / "tasks.tar.gz"
    assert client.download("/api/v1/benchmark-bundles/pompeii", destination) == "abc"
    assert destination.read_bytes() == b"bundle"
    assert seen == [
        ("/api/v1/benchmarks", "Bearer drt_test"),
        ("/api/v1/benchmark-bundles/pompeii", "Bearer drt_test"),
    ]


def test_download_rejects_cross_origin_urls_before_sending(tmp_path):
    client = _client(lambda _request: pytest.fail("request must not be sent"))
    with pytest.raises(ApiError, match="unsafe download URL"):
        client.download("https://evil.example/tasks.tar.gz", tmp_path / "x")


def test_selected_benchmark_is_sent_on_reads_claims_and_checkout():
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url), request.read()))
        if request.url.path.endswith("/assignment"):
            return httpx.Response(200, json={"active": []})
        if request.url.path.endswith("/suggest"):
            return httpx.Response(200, json={"cells": []})
        if request.url.path.endswith("/table"):
            return httpx.Response(200, json={"cells": {}})
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"assignment": {}})
        return httpx.Response(200, json={"assignment": None})

    client = ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(handler),
        benchmark_id="pompeii-adjacency",
    )
    client.get_assignment()
    client.suggest(2)
    client.table()
    client.claim_assignment("p1", "gpt-5.6-sol", "xhigh")
    client.checkout()

    assert all("benchmark=pompeii-adjacency" in url for _, url, _ in seen[:3])
    assert b"benchmark_id=pompeii-adjacency" in seen[3][2]
    assert b"benchmark_id=pompeii-adjacency" in seen[4][2]


def test_exact_batch_is_sent_and_broader_inventory_is_filtered_locally():
    seen = []
    selected = "550e8400e29b41d4a716446655440000"
    other = "123e4567e89b12d3a456426614174000"

    def handler(request):
        seen.append((request.method, str(request.url), request.read()))
        if request.url.path.endswith("/assignment"):
            # This intentionally models an old server that ignores the query.
            return httpx.Response(200, json={"active": [
                {"assignment_id": "other", "batch_id": other},
                {"assignment_id": "selected", "batch_id": selected},
            ]})
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"assignment": {}})
        return httpx.Response(200, json={"assignment": None})

    client = ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(handler),
        benchmark_id="pompeii-adjacency", batch_id=selected,
    )

    inventory = client.get_assignment()
    client.claim_assignment("p1", "gpt-5.6-sol", "xhigh")
    client.checkout(session_id="session-1")

    assert [item["assignment_id"] for item in inventory["active"]] == ["selected"]
    assert inventory["assignment"]["assignment_id"] == "selected"
    assert "benchmark=pompeii-adjacency" in seen[0][1]
    assert f"batch_id={selected}" in seen[0][1]
    assert f"batch_id={selected}".encode() in seen[1][2]
    assert f"batch_id={selected}".encode() in seen[2][2]


def test_account_inventory_read_is_explicit_and_not_batch_scoped():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"active": []})

    client = ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(handler),
        benchmark_id="deep-swe",
    )
    assert client.get_assignment_inventory() == {"active": []}
    assert "benchmark=deep-swe" in seen[0]
    assert "inventory=true" in seen[0]
    assert "batch_id=" not in seen[0]


@pytest.mark.parametrize("tier", ("plus", "pro-5x", "pro-20x"))
def test_scoped_refill_claim_sends_authorized_points_tier(tier):
    seen = {}

    def handler(request):
        seen["form"] = urllib.parse.parse_qs(request.read().decode())
        return httpx.Response(200, json={"assignment": {}})

    client = ApiClient(
        "https://api.example.com",
        "drp_plan_scoped",
        transport=httpx.MockTransport(handler),
        benchmark_id="deep-swe",
        batch_id="550e8400e29b41d4a716446655440000",
    )
    client.claim_assignment(
        "p1",
        "gpt-5.6-sol",
        "xhigh",
        refill_campaign_id="550e8400e29b41d4a716446655440000",
        tier=tier,
    )

    assert seen["form"]["tier"] == [tier]
    assert seen["form"]["refill_campaign_id"] == [
        "550e8400e29b41d4a716446655440000",
    ]


def test_exact_batch_missing_from_inventory_fails_closed_as_404():
    selected = "550e8400e29b41d4a716446655440000"

    def handler(_request):
        return httpx.Response(200, json={"active": [{
            "assignment_id": "other",
            "batch_id": "123e4567e89b12d3a456426614174000",
        }]})

    client = ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(handler), batch_id=selected,
    )
    with pytest.raises(ApiError) as exc:
        client.get_assignment()
    assert exc.value.status_code == 404
    assert exc.value.code == "claim_batch_not_found"


def test_legacy_unscoped_assignment_requests_remain_unchanged():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.read()))
        if request.url.path.endswith("/assignment"):
            return httpx.Response(200, json={"active": []})
        return httpx.Response(200, json={"assignment": None})

    client = ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(handler),
    )
    assert client.get_assignment() == {"active": []}
    client.checkout()
    assert all("batch_id" not in url for url, _body in seen)
    assert all(b"batch_id" not in body for _url, body in seen)


@pytest.mark.parametrize("value", (
    "not-a-uuid",
    "550e8400e29b41d4a71644665544000",
    "{550e8400-e29b-41d4-a716-446655440000}",
    "550e8400-e29b-41d4-a716-446655440000-extra",
))
def test_batch_id_validation_rejects_noncanonical_values(value):
    with pytest.raises(ValueError):
        normalize_batch_id(value)


def test_batch_id_validation_accepts_canonical_forms_and_normalizes_hex():
    expected = "550e8400e29b41d4a716446655440000"
    assert normalize_batch_id(expected) == expected
    assert normalize_batch_id("550E8400-E29B-41D4-A716-446655440000") == expected
    assert normalize_batch_id(None) is None


def test_checkout_sends_failed_cell_exclusions():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"assignment": None, "held": 1, "unstarted": 0})

    _client(handler).checkout(
        exclude_assignment_ids={"a2", "a1"}, session_id="session-123")
    assert b"exclude_assignment_ids=a1%2Ca2" in seen["body"]
    assert b"session_id=session-123" in seen["body"]
    assert b"prepare_only=true" in seen["body"]


def test_mark_started_sends_runner_session_id():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"ok": True})

    _client(handler).mark_started("a1", session_id="session-123")
    assert b"assignment_id=a1" in seen["body"]
    assert b"session_id=session-123" in seen["body"]


def test_release_sends_bulk_target_and_force_flags():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(200, json={
            "released": [], "skipped": [], "already_released": [], "held": 0})

    _client(handler).release_assignments(["a1", "a2"], force=True)
    assert seen["path"] == "/api/v1/assignments/release"
    assert b"assignment_ids=a1%2Ca2" in seen["body"]
    assert b"release_all=false" in seen["body"]
    assert b"force=true" in seen["body"]


def test_mark_stopped_requests_cross_session_cooldown():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"ok": True, "retry_after": "later"})

    _client(handler).mark_stopped(
        "a1", resume_generation=3, failure_kind="environment_build_failed",
    )
    assert b"assignment_id=a1" in seen["body"]
    assert b"defer_seconds=300" in seen["body"]
    assert b"resume_generation=3" in seen["body"]
    assert b"failure_kind=environment_build_failed" in seen["body"]


def test_mark_stopped_can_allow_immediate_user_resume():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"ok": True, "retry_after": None})

    _client(handler).mark_stopped("a1", defer_seconds=0)
    assert b"assignment_id=a1" in seen["body"]
    assert b"defer_seconds=0" in seen["body"]
    assert b"resume_generation" not in seen["body"]
    assert b"failure_kind" not in seen["body"]


def test_mark_stopped_encodes_bounded_failure_diagnostic():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"ok": True})

    diagnostic = {
        "schema": "dradar-runner-failure-v1",
        "failure_code": "trial_timeout",
        "trial_timeout_sec": 3600,
        "zcode_session_timeout_sec": 3660,
    }
    _client(handler).mark_stopped(
        "a1", failure_kind="runner_failed", failure_diagnostic=diagnostic,
    )
    body = urllib.parse.parse_qs(seen["body"].decode())
    assert json.loads(body["failure_diagnostic"][0]) == diagnostic


def _do_submit(handler, tmp_path, with_optional):
    patch = tmp_path / "model.patch"
    patch.write_bytes(b"diff --git a/f b/f\n")
    trajectory = result = None
    if with_optional:
        trajectory = tmp_path / "trajectory.json"
        trajectory.write_text("[]")
        result = tmp_path / "result.json"
        result.write_text("{}")
    return _client(handler).submit(
        "a1", "nonce1", patch, trajectory, result,
        {"dradar_version": "0.test"}, outcome="completed")



def test_submit_sends_only_patch_part_when_optionals_absent(tmp_path):
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"submission_id": "s1", "grade_status": "pending"})

    _do_submit(handler, tmp_path, with_optional=False)
    body = seen["body"]
    assert b'name="patch"' in body
    assert b'name="trajectory"' not in body
    assert b'name="result"' not in body


def test_submit_sends_three_parts_and_client_meta_as_json_string(tmp_path):
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"submission_id": "s1", "grade_status": "pending"})

    _do_submit(handler, tmp_path, with_optional=True)
    body = seen["body"]
    for part in (b'name="patch"', b'name="trajectory"', b'name="result"'):
        assert part in body
    # client_meta travels as a single JSON-encoded string form field
    assert json.dumps({"dradar_version": "0.test"}).encode() in body


def test_submit_sends_multi_agent_trajectory_bundle(tmp_path):
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"submission_id": "s1", "grade_status": "pending"})

    patch = tmp_path / "model.patch"
    patch.write_text("diff")
    bundle = tmp_path / "trajectory_bundle.json"
    bundle.write_text('{"schema_version":"dradar-codex-trajectory-bundle-v1"}')
    _client(handler).submit(
        "a1", "nonce", patch, None, None, {}, trajectory_bundle=bundle,
    )
    body = seen["body"]
    assert b'name="trajectory_bundle"' in body
    assert b'filename="trajectory_bundle.json"' in body


def test_submit_sends_resume_generation_when_fenced(tmp_path):
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"submission_id": "s1", "grade_status": "pending"})

    patch = tmp_path / "model.patch"
    patch.write_text("diff")
    _client(handler).submit(
        "a1", "nonce", patch, None, None, {}, resume_generation=7,
    )
    assert b"resume_generation" in seen["body"]
    assert b"7" in seen["body"]


def test_tokenless_client_sends_no_authorization_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"nickname": "n", "token": "t"})

    _client(handler, token="").register("n")
    assert seen["auth"] is None


def test_token_becomes_bearer_authorization_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"nickname": "n"})

    _client(handler, token="drt_test").whoami()
    assert seen["auth"] == "Bearer drt_test"


_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def _clear_proxy_env(monkeypatch):
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)


def test_env_proxy_is_still_honored(monkeypatch):
    """Passing ANY explicit transport makes httpx skip HTTP(S)_PROXY mounting
    entirely — the retrying default must stand aside on proxied machines or
    every proxied volunteer hard-breaks on upgrade."""
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    client = ApiClient("https://radar.example", "tok")
    assert client._client._mounts, "env proxy was not mounted"


def test_connect_retries_default_when_unproxied(monkeypatch):
    _clear_proxy_env(monkeypatch)
    client = ApiClient("https://radar.example", "tok")
    assert client._client._mounts == {}
    # httpcore internal, pinned deliberately: this is the only observable
    # evidence that the connect-phase retry default is actually in effect.
    assert client._client._transport._pool._retries == 2
