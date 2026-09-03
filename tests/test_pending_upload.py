"""Upload resilience: a trial that ran but failed to upload must survive on
disk and be retryable without re-running, via a local pending-upload ledger.
"""

import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dradar import local_jobs, pending, runloop
from dradar.api_client import ApiClient, ApiError


# --- pending.py: the ledger itself ------------------------------------------

def test_ledger_round_trip(tmp_path: Path):
    assert pending.load(tmp_path) == []
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})
    pending.record(tmp_path, {"assignment_id": "a2", "task_id": "t2"})
    entries = pending.load(tmp_path)
    assert {e["assignment_id"] for e in entries} == {"a1", "a2"}


def test_empty_ledger_read_is_side_effect_free(tmp_path: Path):
    home = tmp_path / "not-created"
    assert pending.load(home) == []
    assert not home.exists()


def test_record_replaces_same_assignment(tmp_path: Path):
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1", "attempt": 1})
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1", "attempt": 2})
    entries = pending.load(tmp_path)
    assert len(entries) == 1 and entries[0]["attempt"] == 2


def test_record_preserves_same_assignment_from_another_scope(tmp_path: Path):
    scope_a = pending.scope_fingerprint(
        server="https://a.example", account_scope="a", batch_id="plan-a",
    )
    scope_b = pending.scope_fingerprint(
        server="https://b.example", account_scope="b", batch_id="plan-b",
    )
    pending.record(tmp_path, {
        "assignment_id": "reused", "scope_fingerprint": scope_a,
    })
    pending.record(tmp_path, {
        "assignment_id": "reused", "scope_fingerprint": scope_b,
    })
    assert {
        entry["scope_fingerprint"] for entry in pending.load(tmp_path)
    } == {scope_a, scope_b}


def test_remove_is_idempotent(tmp_path: Path):
    pending.record(tmp_path, {"assignment_id": "a1"})
    pending.remove(tmp_path, "a1")
    pending.remove(tmp_path, "a1")  # no error on double-remove
    assert pending.load(tmp_path) == []


def test_load_tolerates_corrupt_file(tmp_path: Path):
    (tmp_path / "pending_uploads.json").write_text("{ not json")
    assert pending.load(tmp_path) == []


def test_load_tolerates_non_list_json(tmp_path: Path):
    (tmp_path / "pending_uploads.json").write_text('{"oops": "not a list"}')
    assert pending.load(tmp_path) == []


def test_save_is_atomic_failed_commit_does_not_corrupt_existing_ledger(tmp_path: Path, monkeypatch):
    # This is a crash-safety net; a save that can itself leave a truncated
    # file on disk would defeat the whole point. Assert the INVARIANT rather
    # than the mechanism (temp file + os.replace today): fail the atomic
    # commit after the replacement file is written — the
    # ledger a subsequent load() sees must be the complete ORIGINAL, never a
    # truncated/corrupt version (which load() would drop wholesale).
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})
    before = pending.load(tmp_path)

    def failed_commit(_source, _destination):
        raise OSError("simulated crash mid-write")
    monkeypatch.setattr(pending.os, "replace", failed_commit)
    with pytest.raises(OSError):
        pending.record(tmp_path, {"assignment_id": "a2", "task_id": "t2"})
    monkeypatch.undo()

    assert pending.load(tmp_path) == before  # untouched, not truncated/corrupted


# --- runloop._upload_trial: the shared upload+scrub+ledger logic -----------

class FakeClient:
    def __init__(self, behavior):
        self.behavior = behavior  # callable(assignment_id) -> dict | raises ApiError
        self.calls = []
        self.stopped = []
        self.intent_calls = []

    def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
               outcome="completed", resume_generation=None,
               owner_epoch=None, session_id=None, upload_intent_id=None,
               intent_version=None):
        self.calls.append(assignment_id)
        self.last_upload_intent_id = upload_intent_id
        return self.behavior(assignment_id)

    def register_submission_upload_intent(
        self, assignment_id, nonce, session_id, owner_epoch,
        upload_intent_id, *, resume_generation=None, intent_version=None,
    ):
        fence = owner_epoch if owner_epoch is not None else resume_generation
        self.intent_calls.append((
            assignment_id, session_id, fence, upload_intent_id,
        ))
        return upload_intent_id

    def mark_stopped(self, assignment_id, **_kwargs):
        self.stopped.append(assignment_id)


def _make_trial_dir(tmp_path: Path, name: str = "t") -> Path:
    trial_dir = tmp_path / name
    (trial_dir / "artifacts").mkdir(parents=True)
    (trial_dir / "artifacts" / "model.patch").write_text("diff --git a b\n")
    return trial_dir


def _entry(trial_dir: Path, **overrides) -> dict:
    """A pending-ledger entry dict — the shape _upload_trial takes."""
    e = {"assignment_id": "a1", "nonce": "nonce1", "task_id": "t1",
         "trial_dir": str(trial_dir), "meta": {}, "outcome": "completed",
         "job_dir": None, "keep": True,
         # Production retry scans now require a non-secret account/server
         # scope. Keep this fixture representative of a newly recorded row;
         # tests that exercise legacy rows omit the field explicitly.
         "scope_fingerprint": pending.scope_fingerprint(
             server="https://api.example.com",
             account_scope=ApiClient(
                 "https://api.example.com", "drt_test",
                 capabilities=(),
             ).account_scope,
         )}
    e.update(overrides)
    if "scope_fingerprint" not in overrides:
        e["scope_fingerprint"] = pending.scope_fingerprint(
            server="https://api.example.com",
            account_scope=ApiClient(
                "https://api.example.com", "drt_test",
                capabilities=(),
            ).account_scope,
            benchmark_id=e.get("benchmark_id"),
            batch_id=e.get("batch_id"),
        )
    return e


def test_upload_success_clears_ledger(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    pending.record(tmp_path, _entry(trial_dir))
    client = FakeClient(lambda aid: {"submission_id": "s1", "grade_status": "pending"})
    outcome = runloop._upload_trial(client, _entry(trial_dir, meta={"k": "v"}))
    assert outcome == "submitted"
    assert pending.load(tmp_path) == []


def test_exact_empty_submission_ack_is_not_collapsed_to_submitted(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    pending.record(tmp_path, _entry(trial_dir))
    client = FakeClient(lambda _aid: {
        "submission_id": "s-empty",
        "grade_status": "invalid",
        "terminal_outcome": "empty-submission",
        "failure_kind": "empty-submission",
        "failure_layer": "artifact",
        "failure_code": "empty-model-patch",
    })

    assert runloop._upload_trial(client, _entry(trial_dir)) == "empty-submission"
    assert pending.load(tmp_path) == []


def test_generic_progress_bearing_invalid_remains_non_circuit_submission(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    client = FakeClient(lambda _aid: {
        "submission_id": "s-invalid", "grade_status": "invalid",
        "failure_kind": "rate-limit",
    })

    assert runloop._upload_trial(client, _entry(trial_dir)) == "submitted"


def test_session_bound_upload_registers_intent_before_submit(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    client = FakeClient(
        lambda _aid: {"submission_id": "s1", "grade_status": "pending"},
    )
    outcome = runloop._upload_trial(
        client,
        _entry(
            trial_dir,
            resume_generation=3,
            runner_session_id="session-1234",
        ),
    )
    assert outcome == "submitted"
    assert client.intent_calls[0][:3] == ("a1", "session-1234", 3)
    assert client.calls == ["a1"]
    assert client.last_upload_intent_id == client.intent_calls[0][3]
    assert len(client.last_upload_intent_id) == 64


def test_plan_pending_upload_replays_closed_exact_session_without_model_rerun(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    batch_id = "550e8400e29b41d4a716446655440000"
    session_id = "session-plan-device-1"

    class ClosedSessionPlanClient(FakeClient):
        def __init__(self):
            super().__init__(
                lambda _aid: {"submission_id": "s1", "grade_status": "pending"},
            )
            self.closed_sessions = set()
            self.network_available = False

        def register_submission_upload_intent(
            self, assignment_id, nonce, session_id_arg, owner_epoch,
            upload_intent_id, *, resume_generation=None, intent_version=None,
        ):
            self.intent_calls.append((
                assignment_id, session_id_arg, owner_epoch, upload_intent_id,
            ))
            if not self.network_available:
                raise ApiError("connection lost before intent", status_code=None)
            # The server contract allows this same exact mapped session/owner
            # to register its first content intent immediately after close.
            assert session_id_arg in self.closed_sessions
            assert session_id_arg == session_id
            assert owner_epoch == 0
            return upload_intent_id

    client = ClosedSessionPlanClient()
    entry = _entry(
        trial_dir,
        batch_id=batch_id,
        runner_session_id=session_id,
        owner_epoch=0,
        ledger_version=3,
    )

    assert runloop._upload_trial(client, entry) == "upload-failed"
    saved = pending.load(tmp_path)
    assert len(saved) == 1
    assert saved[0]["batch_id"] == batch_id
    assert saved[0]["runner_session_id"] == session_id

    # Model work has already finished. Closing the runner and immediately
    # repeating the same plan must replay only the exact durable upload.
    client.closed_sessions.add(session_id)
    client.network_available = True
    monkeypatch.setattr(
        runloop,
        "_run_and_submit",
        lambda *_args, **_kwargs: pytest.fail("replay must not run the model again"),
    )

    outcomes = runloop._retry_pending_uploads(client, batch_id=batch_id)

    assert outcomes == ["submitted"]
    assert pending.load(tmp_path) == []
    assert client.calls == ["a1"]
    assert len(client.intent_calls) == 2


def test_maintenance_intent_waits_then_submits_pending_without_model_rerun(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    entry = _entry(
        trial_dir,
        runner_session_id="session-1234",
        owner_epoch=7,
        resume_generation=2,
        ledger_version=3,
    )
    pending.record(tmp_path, entry)
    requests = []
    intent_attempts = 0

    def handler(request):
        nonlocal intent_attempts
        body = request.read()
        requests.append((request.url.path, body))
        saved = pending.load(tmp_path)
        assert len(saved) == 1
        assert saved[0]["runner_session_id"] == "session-1234"
        assert saved[0]["owner_epoch"] == 7
        if request.url.path.endswith("submission-upload-intents"):
            intent_attempts += 1
            form = urllib.parse.parse_qs(body.decode())
            assert form["session_id"] == ["session-1234"]
            assert form["owner_epoch"] == ["7"]
            if intent_attempts == 1:
                return httpx.Response(
                    503,
                    headers={"Retry-After": "2"},
                    json={
                        "detail": "deployment in progress",
                        "code": "deployment_maintenance",
                    },
                )
            return httpx.Response(200, json={"ok": True})
        assert request.url.path.endswith("submissions")
        assert b'name="owner_epoch"' in body
        assert b"\r\n7\r\n" in body
        return httpx.Response(
            200, json={"submission_id": "s1", "grade_status": "pending"},
        )

    client = ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(handler), capabilities=(),
    )
    now = [0.0]
    waits = []
    client._monotonic = lambda: now[0]

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    client._sleep = sleep
    monkeypatch.setattr(
        runloop,
        "_run_and_submit",
        lambda *_a, **_k: pytest.fail("pending upload must not rerun the model"),
    )

    assert runloop._retry_pending_uploads(client) == ["submitted"]
    assert [path for path, _body in requests] == [
        "/api/v1/submission-upload-intents",
        "/api/v1/submission-upload-intents",
        "/api/v1/submissions",
    ]
    assert requests[0][1] == requests[1][1]
    assert waits == [2.0]
    assert pending.load(tmp_path) == []


def test_maintenance_total_budget_exhaustion_keeps_ledger_and_exits_nonzero(
    tmp_path: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    entry = _entry(
        trial_dir,
        runner_session_id="session-1234",
        owner_epoch=7,
        ledger_version=3,
        batch_id="550e8400e29b41d4a716446655440000",
    )
    pending.record(tmp_path, entry)
    paths = []
    intent_attempts = 0

    def handler(request):
        nonlocal intent_attempts
        paths.append(request.url.path)
        if request.url.path.endswith("submission-upload-intents"):
            intent_attempts += 1
            if intent_attempts == 1:
                return httpx.Response(
                    503,
                    headers={"Retry-After": "2"},
                    json={
                        "detail": "deployment in progress",
                        "code": "deployment_maintenance",
                    },
                )
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            503,
            headers={"Retry-After": "2"},
            json={
                "detail": "deployment still in progress",
                "code": "deployment_maintenance",
            },
        )

    client = ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(handler), capabilities=(),
        batch_id="550e8400e29b41d4a716446655440000",
    )
    now = [0.0]
    waits = []
    client._submission_maintenance_retry_budget = 3.0
    client._monotonic = lambda: now[0]

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    client._sleep = sleep
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda _cfg: client)
    monkeypatch.setattr(
        runloop,
        "_run_and_submit",
        lambda *_a, **_k: pytest.fail("retry-upload must not run the model"),
    )

    exit_code = runloop.cmd_retry_upload(
        SimpleNamespace(request_salvage=None, yes=True),
    )

    assert exit_code == 1
    assert paths == [
        "/api/v1/submission-upload-intents",
        "/api/v1/submission-upload-intents",
        "/api/v1/submissions",
    ]
    assert waits == [2.0]
    saved = pending.load(tmp_path)
    assert len(saved) == 1
    assert saved[0]["runner_session_id"] == "session-1234"
    assert saved[0]["owner_epoch"] == 7
    assert "upload_blocked" not in saved[0]
    output = capsys.readouterr().out
    assert "deployment maintenance retry budget exhausted" in output
    assert "still pending and retryable" in output


def test_maintenance_retry_preserves_owner_superseded_fail_closed(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    entry = _entry(
        trial_dir,
        runner_session_id="session-1234",
        owner_epoch=7,
        ledger_version=3,
    )
    pending.record(tmp_path, entry)
    paths = []

    def handler(request):
        body = request.read()
        paths.append(request.url.path)
        assert request.url.path.endswith("submission-upload-intents")
        form = urllib.parse.parse_qs(body.decode())
        assert form["session_id"] == ["session-1234"]
        assert form["owner_epoch"] == ["7"]
        if len(paths) == 1:
            return httpx.Response(
                503,
                headers={"Retry-After": "1"},
                json={
                    "detail": "deployment in progress",
                    "code": "deployment_maintenance",
                },
            )
        return httpx.Response(
            409,
            json={
                "detail": "upload owner superseded",
                "code": "upload_owner_superseded",
            },
        )

    client = ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(handler), capabilities=(),
    )
    now = [0.0]
    client._monotonic = lambda: now[0]
    client._sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    monkeypatch.setattr(
        runloop,
        "_run_and_submit",
        lambda *_a, **_k: pytest.fail("owner conflict must not rerun the model"),
    )

    assert runloop._retry_pending_uploads(client) == ["upload-blocked"]
    assert paths == [
        "/api/v1/submission-upload-intents",
        "/api/v1/submission-upload-intents",
    ]
    saved = pending.load(tmp_path)
    assert len(saved) == 1
    assert saved[0]["upload_blocked"] == "owner_superseded"
    assert pending.assignment_ids(tmp_path) == {"a1"}

    def must_not_contact_server(_request):
        pytest.fail("blocked owner must remain local on automatic retry")

    blocked_client = ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(must_not_contact_server), capabilities=(),
    )
    assert runloop._retry_pending_uploads(blocked_client) == ["upload-blocked"]
    assert pending.assignment_ids(tmp_path) == {"a1"}


def test_plan_pending_replay_is_exact_batch_scoped(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    batch_a = "550e8400e29b41d4a716446655440000"
    batch_b = "6ba7b8109dad11d180b400c04fd430c8"
    pending.record(tmp_path, {"assignment_id": "a", "batch_id": batch_a})
    pending.record(tmp_path, {"assignment_id": "b", "batch_id": batch_b})
    pending.record(tmp_path, {"assignment_id": "legacy-without-scope"})
    touched = []

    def upload(_client, entry):
        touched.append(entry["assignment_id"])
        pending.remove(tmp_path, entry["assignment_id"])
        return "submitted"

    monkeypatch.setattr(runloop, "_upload_trial", upload)

    assert runloop._retry_pending_uploads(object(), batch_id=batch_a) == ["submitted"]
    assert touched == ["a"]
    assert {entry["assignment_id"] for entry in pending.load(tmp_path)} == {
        "b", "legacy-without-scope",
    }


def test_pending_retry_isolated_by_server_account_and_plan_scope(
    tmp_path: Path, monkeypatch,
):
    """A shared HOME must not replay plan A's paid result through plan B."""
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    batch_a = "550e8400e29b41d4a716446655440000"
    batch_b = "6ba7b8109dad11d180b400c04fd430c8"
    client_a = ApiClient(
        "https://server-a.example", "token-a", capabilities=(),
        benchmark_id="bench", batch_id=batch_a,
    )
    client_b = ApiClient(
        "https://server-b.example", "token-b", capabilities=(),
        benchmark_id="bench", batch_id=batch_b,
    )
    pending.record(tmp_path, {
        "assignment_id": "shared-assignment", "batch_id": batch_a,
        "scope_fingerprint": runloop._pending_scope_fingerprint(
            client_a, batch_id=batch_a,
        ),
    })
    pending.record(tmp_path, {
        "assignment_id": "from-b", "batch_id": batch_b,
        "scope_fingerprint": runloop._pending_scope_fingerprint(
            client_b, batch_id=batch_b,
        ),
    })
    touched = []
    monkeypatch.setattr(
        runloop, "_upload_trial",
        lambda _client, entry: touched.append(entry["assignment_id"]) or (
            pending.remove(
                tmp_path, entry["assignment_id"],
                scope_fingerprint=entry.get("scope_fingerprint"),
            )
            or "submitted"
        ),
    )

    runloop._mark_pending_scope_required(client_b)
    assert runloop._retry_pending_uploads(client_b, batch_id=batch_b) == [
        "submitted"
    ]
    assert touched == ["from-b"]
    remaining = pending.load(tmp_path)
    assert [entry["assignment_id"] for entry in remaining] == ["shared-assignment"]
    # Scope-aware fencing must not let an old account/plan row block this
    # account's worker registration or checkout path.
    assert "shared-assignment" not in runloop._pending_assignment_ids_for_client(
        client_b, batch_id=batch_b,
    )


def test_personal_client_derives_one_pending_batch_for_retry_and_fence(
    tmp_path: Path, monkeypatch,
):
    """A normal client can recover one owned batch without guessing across plans."""
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    batch_id = "550e8400e29b41d4a716446655440000"
    client = ApiClient(
        "https://server.example", "token", capabilities=(),
        benchmark_id="bench",
    )
    scope = runloop._pending_scope_fingerprint(client, batch_id=batch_id)
    pending.record(tmp_path, {
        "assignment_id": "owned", "batch_id": batch_id,
        "scope_fingerprint": scope,
    })
    touched = []
    monkeypatch.setattr(
        runloop, "_upload_trial",
        lambda _client, entry: touched.append(entry["assignment_id"]) or (
            pending.remove(
                tmp_path, entry["assignment_id"],
                scope_fingerprint=entry.get("scope_fingerprint"),
            )
            or "submitted"
        ),
    )
    runloop._mark_pending_scope_required(client)
    assert "owned" in runloop._pending_assignment_ids_for_client(client)
    assert runloop._retry_pending_uploads(client) == ["submitted"]
    assert touched == ["owned"]
    assert runloop._pending_assignment_ids_for_client(client) == set()


def test_personal_client_fences_batch_pending_before_model_and_then_retries(
    tmp_path: Path, monkeypatch,
):
    """A plain (batch-less) claim cannot rerun work saved by a prior batch."""
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    batch_id = "550e8400e29b41d4a716446655440000"
    client = ApiClient(
        "https://server.example", "token", capabilities=(),
        benchmark_id="bench",
    )
    pending.record(tmp_path, {
        "assignment_id": "owned", "batch_id": batch_id,
        "scope_fingerprint": runloop._pending_scope_fingerprint(
            client, batch_id=batch_id,
        ),
    })
    monkeypatch.setattr(runloop, "check_task_content_hash", lambda *_a: True)
    monkeypatch.setattr(
        runloop, "run_trial",
        lambda *_a, **_k: pytest.fail("durable pending work must fence the model"),
    )
    args = SimpleNamespace(allow_task_drift=False, dev_agent=False)
    assignment = {
        "assignment_id": "owned", "task_id": "task-owned", "nonce": "nonce",
        "batch_id": None,
    }
    assert runloop._run_and_submit(
        client, assignment, tmp_path, args, None,
    ) == "pending-upload"

    touched = []
    monkeypatch.setattr(
        runloop, "_upload_trial",
        lambda _client, entry: touched.append(entry["assignment_id"]) or (
            pending.remove(
                tmp_path, entry["assignment_id"],
                scope_fingerprint=entry.get("scope_fingerprint"),
            )
            or "submitted"
        ),
    )
    runloop._mark_pending_scope_required(client)
    assert runloop._retry_pending_uploads(client) == ["submitted"]
    assert touched == ["owned"]


def test_personal_client_replays_multiple_owned_batches(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    client = ApiClient(
        "https://server.example", "token", capabilities=(),
        benchmark_id="bench",
    )
    batches = [
        "550e8400e29b41d4a716446655440000",
        "6ba7b8109dad11d180b400c04fd430c8",
    ]
    for index, batch_id in enumerate(batches):
        pending.record(tmp_path, {
            "assignment_id": f"owned-{index}", "batch_id": batch_id,
            "scope_fingerprint": runloop._pending_scope_fingerprint(
                client, batch_id=batch_id,
            ),
        })
    touched = []
    monkeypatch.setattr(
        runloop, "_upload_trial",
        lambda _client, entry: touched.append(entry["assignment_id"]) or (
            pending.remove(
                tmp_path, entry["assignment_id"],
                scope_fingerprint=entry.get("scope_fingerprint"),
            )
            or "submitted"
        ),
    )
    runloop._mark_pending_scope_required(client)
    assert runloop._retry_pending_uploads(client) == ["submitted", "submitted"]
    assert touched == ["owned-0", "owned-1"]
    assert pending.load(tmp_path) == []


def test_personal_client_fails_closed_on_assignment_collision_across_batches(
    tmp_path: Path, monkeypatch,
):
    """A reused assignment ID in two own batches is withheld, not guessed."""
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    client = ApiClient(
        "https://server.example", "token", capabilities=(),
        benchmark_id="bench",
    )
    batches = [
        "550e8400e29b41d4a716446655440000",
        "6ba7b8109dad11d180b400c04fd430c8",
    ]
    for batch_id in batches:
        pending.record(tmp_path, {
            "assignment_id": "reused", "batch_id": batch_id,
            "scope_fingerprint": runloop._pending_scope_fingerprint(
                client, batch_id=batch_id,
            ),
        })
    monkeypatch.setattr(
        runloop, "_upload_trial",
        lambda *_args, **_kwargs: pytest.fail(
            "assignment collision must stay local",
        ),
    )
    runloop._mark_pending_scope_required(client)
    assert runloop._retry_pending_uploads(client) == []
    assert len(pending.load(tmp_path)) == 2
    # Fencing remains conservative even though replay is withheld.
    assert runloop._pending_assignment_ids_for_client(client) == {"reused"}


def test_personal_client_skips_invalid_batch_but_preserves_evidence(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    client = ApiClient(
        "https://server.example", "token", capabilities=(),
        benchmark_id="bench",
    )
    pending.record(tmp_path, {
        "assignment_id": "malformed", "batch_id": "not-a-uuid",
        "scope_fingerprint": "not-a-real-scope",
    })
    monkeypatch.setattr(
        runloop, "_upload_trial",
        lambda *_args, **_kwargs: pytest.fail("invalid batch must stay local"),
    )
    runloop._mark_pending_scope_required(client)
    assert runloop._retry_pending_uploads(client) == []
    assert pending.load(tmp_path)[0]["assignment_id"] == "malformed"


def test_retry_upload_keeps_malformed_ledger_rows_without_crashing(
    tmp_path: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    raw_rows = [1, None, "not-an-entry"]
    (tmp_path / "pending_uploads.json").write_text(json.dumps(raw_rows))
    client = ApiClient("https://server.example", "token", capabilities=())
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda _cfg: client)

    assert runloop.cmd_retry_upload(None) == 1
    assert json.loads((tmp_path / "pending_uploads.json").read_text()) == raw_rows
    output = capsys.readouterr().out
    assert "malformed pending ledger row" in output
    assert "unknown, malformed" in output


def test_batch_helper_rejects_same_uuid_from_foreign_account(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    batch_id = "550e8400e29b41d4a716446655440000"
    local = ApiClient("https://server.example", "token", capabilities=())
    foreign = ApiClient("https://other.example", "token", capabilities=())
    pending.record(tmp_path, {
        "assignment_id": "foreign", "batch_id": batch_id,
        "scope_fingerprint": runloop._pending_scope_fingerprint(
            foreign, batch_id=batch_id,
        ),
    })
    assert runloop._pending_uploads_for_client_batch(local, batch_id) == []


def test_standalone_retry_keeps_legacy_entry_without_scope_fingerprint(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    client = ApiClient("https://server.example", "token", capabilities=())
    pending.record(tmp_path, {
        "assignment_id": "session-bound", "runner_session_id": "old-session",
    })
    monkeypatch.setattr(
        runloop, "_upload_trial",
        lambda *_args, **_kwargs: pytest.fail("unknown scope must stay local"),
    )
    runloop._mark_pending_scope_required(client)
    assert runloop._retry_pending_uploads(client) == []
    assert pending.load(tmp_path)[0]["assignment_id"] == "session-bound"


def test_cmd_retry_upload_does_not_send_another_account_queue(
    tmp_path: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    client_a = ApiClient("https://server.example", "token-a", capabilities=())
    client_b = ApiClient("https://server.example", "token-b", capabilities=())
    pending.record(tmp_path, {
        "assignment_id": "from-a",
        "scope_fingerprint": runloop._pending_scope_fingerprint(client_a),
    })
    pending.record(tmp_path, {
        "assignment_id": "from-b",
        "scope_fingerprint": runloop._pending_scope_fingerprint(client_b),
    })
    touched = []
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda _cfg: client_b)
    monkeypatch.setattr(
        runloop, "_upload_trial",
        lambda _client, entry: touched.append(entry["assignment_id"]) or (
            pending.remove(
                tmp_path, entry["assignment_id"],
                scope_fingerprint=entry.get("scope_fingerprint"),
            )
            or "submitted"
        ),
    )

    assert runloop.cmd_retry_upload(None) == 1
    assert touched == ["from-b"]
    assert [entry["assignment_id"] for entry in pending.load(tmp_path)] == ["from-a"]
    assert "scope is unknown, malformed, or does not match" in capsys.readouterr().out


def test_intent_registration_conflict_keeps_artifact_without_submit(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)

    class StaleIntentClient(FakeClient):
        def register_submission_upload_intent(self, *_args, **_kwargs):
            raise ApiError(
                "server returned 409: stale recovery generation",
                status_code=409,
            )

    client = StaleIntentClient(lambda _aid: pytest.fail("must not submit"))
    outcome = runloop._upload_trial(
        client, _entry(trial_dir, runner_session_id="session-1234"),
    )
    assert outcome == "upload-failed"
    assert client.calls == []
    assert pending.load(tmp_path)[0]["runner_session_id"] == "session-1234"


def test_superseded_owner_blocks_migrated_ledger_and_future_retries_locally(
    tmp_path: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)

    class SupersededDuringMigration(FakeClient):
        def register_submission_upload_intent(
            self, assignment_id, nonce, session_id, owner_epoch,
            upload_intent_id, *, resume_generation=None, intent_version=None,
        ):
            fence = owner_epoch if owner_epoch is not None else resume_generation
            self.intent_calls.append((assignment_id, session_id, fence, upload_intent_id))
            if owner_epoch is None:
                raise ApiError(
                    "server returned 409: owner protocol upgrade required",
                    status_code=409,
                    code="owner_protocol_upgrade_required",
                )
            raise ApiError(
                "server returned 409: upload owner superseded",
                status_code=409,
                code="upload_owner_superseded",
            )

    first = SupersededDuringMigration(
        lambda _aid: pytest.fail("superseded result must not submit"),
    )
    outcome = runloop._upload_trial(
        first,
        _entry(
            trial_dir,
            runner_session_id="session-old",
            resume_generation=0,
        ),
    )
    assert outcome == "upload-blocked"
    assert [call[2] for call in first.intent_calls] == [0, 0]
    blocked = pending.load(tmp_path)[0]
    assert blocked["ledger_version"] == 3
    assert blocked["owner_epoch"] == 0
    assert blocked["upload_blocked"] == "owner_superseded"
    assert pending.assignment_ids(tmp_path) == {"a1"}

    class MustStayLocal(FakeClient):
        def register_submission_upload_intent(self, *_args, **_kwargs):
            pytest.fail("blocked retry must not register an intent")

        def submit(self, *_args, **_kwargs):
            pytest.fail("blocked retry must not submit")

    monkeypatch.setattr(
        runloop.artifact_staging,
        "ensure_staged_patch",
        lambda *_a, **_k: pytest.fail("blocked retry must not restage"),
    )
    runloop._retry_pending_uploads(MustStayLocal(lambda _aid: None))
    assert pending.assignment_ids(tmp_path) == {"a1"}
    assert "will neither be retried nor run again automatically" in capsys.readouterr().out


def test_explicit_salvage_rebinds_upload_only_and_never_runs_model(
    tmp_path: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    entry = _entry(
        trial_dir,
        runner_session_id="source-session-0001",
        owner_epoch=1,
        ledger_version=3,
        upload_blocked="owner_superseded",
    )
    pending.record(tmp_path, entry)

    class SalvageClient(FakeClient):
        def __init__(self):
            super().__init__(
                lambda _aid: {"submission_id": "s1", "grade_status": "pending"},
            )
            self.rebind_calls = []

        def get_assignment(self):
            return {"active": [{
                "assignment_id": "a1",
                "owner_epoch": 3,
                "started_at": None,
            }]}

        def rebind_submission_upload_salvage(
            self, assignment_id, nonce, source_session_id,
            source_owner_epoch, expected_owner_epoch, salvage_session_id,
        ):
            self.rebind_calls.append({
                "assignment_id": assignment_id,
                "nonce": nonce,
                "source_session_id": source_session_id,
                "source_owner_epoch": source_owner_epoch,
                "expected_owner_epoch": expected_owner_epoch,
                "salvage_session_id": salvage_session_id,
            })
            return {"owner_epoch": 4, "replayed": False}

    client = SalvageClient()
    outcome = runloop._upload_trial(client, entry, request_salvage=True)

    assert outcome == "submitted"
    assert pending.load(tmp_path) == []
    assert len(client.rebind_calls) == 1
    call = client.rebind_calls[0]
    assert call["source_session_id"] == "source-session-0001"
    assert call["source_owner_epoch"] == 1
    assert call["expected_owner_epoch"] == 3
    assert call["salvage_session_id"].startswith("salvage-")
    assert client.intent_calls[0][1] == call["salvage_session_id"]
    assert client.intent_calls[0][2] == 4
    assert client.calls == ["a1"]
    assert "without rerunning the model" in capsys.readouterr().out


def test_explicit_salvage_refusal_keeps_block_and_artifacts(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    entry = _entry(
        trial_dir,
        runner_session_id="source-session-0001",
        owner_epoch=1,
        ledger_version=3,
        upload_blocked="owner_superseded",
    )

    class LiveOwnerClient(FakeClient):
        def get_assignment(self):
            return {"active": [{
                "assignment_id": "a1",
                "owner_epoch": 3,
                "started_at": "2026-08-31T00:00:00+00:00",
            }]}

        def rebind_submission_upload_salvage(self, *_args, **_kwargs):
            pytest.fail("a live owner must be rejected before rebind")

    client = LiveOwnerClient(lambda _aid: pytest.fail("must not submit"))
    outcome = runloop._upload_trial(client, entry, request_salvage=True)

    assert outcome == "upload-blocked"
    saved = pending.load(tmp_path)[0]
    assert saved["upload_blocked"] == "owner_superseded"
    assert saved["runner_session_id"] == "source-session-0001"
    assert (trial_dir / "artifacts" / "model.patch").is_file()
    assert client.calls == []


@pytest.mark.parametrize(
    "code",
    ["upload_salvage_owner_changed", "upload_salvage_identity_unavailable"],
)
def test_explicit_salvage_unusable_identity_is_cleared_for_fresh_request(
    tmp_path: Path, monkeypatch, code,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    entry = _entry(
        trial_dir,
        runner_session_id="source-session-0001",
        owner_epoch=1,
        ledger_version=3,
        upload_blocked="owner_superseded",
        salvage_rebind={
            "source_session_id": "source-session-0001",
            "source_owner_epoch": 1,
            "expected_owner_epoch": 3,
            "salvage_session_id": "salvage-session-stale",
        },
    )
    pending.record(tmp_path, entry)

    class UnusableIdentityClient(FakeClient):
        def rebind_submission_upload_salvage(self, *_args, **_kwargs):
            raise ApiError(
                "server returned 409: salvage identity is unusable",
                status_code=409,
                code=code,
            )

    client = UnusableIdentityClient(lambda _aid: pytest.fail("must not submit"))
    outcome = runloop._upload_trial(client, entry, request_salvage=True)

    assert outcome == "upload-blocked"
    saved = pending.load(tmp_path)[0]
    assert saved["upload_blocked"] == "owner_superseded"
    assert "salvage_rebind" not in saved
    assert (trial_dir / "artifacts" / "model.patch").is_file()
    assert client.calls == []


def test_explicit_salvage_transport_failure_preserves_idempotency_identity(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    entry = _entry(
        trial_dir,
        runner_session_id="source-session-0001",
        owner_epoch=1,
        ledger_version=3,
        upload_blocked="owner_superseded",
    )

    class LostResponseClient(FakeClient):
        def get_assignment(self):
            return {"active": [{
                "assignment_id": "a1",
                "owner_epoch": 3,
                "started_at": None,
            }]}

        def rebind_submission_upload_salvage(self, *_args, **_kwargs):
            raise ApiError("response lost", status_code=None)

    client = LostResponseClient(lambda _aid: pytest.fail("must not submit"))
    outcome = runloop._upload_trial(client, entry, request_salvage=True)

    assert outcome == "upload-blocked"
    saved = pending.load(tmp_path)[0]
    assert saved["upload_blocked"] == "owner_superseded"
    assert saved["salvage_rebind"]["salvage_session_id"].startswith("salvage-")
    assert (trial_dir / "artifacts" / "model.patch").is_file()
    assert client.calls == []


def test_salvaged_owner_superseded_during_intent_clears_stale_rebind(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    entry = _entry(
        trial_dir,
        runner_session_id="source-session-0001",
        owner_epoch=1,
        ledger_version=3,
        upload_blocked="owner_superseded",
    )

    class SupersededSalvageClient(FakeClient):
        def get_assignment(self):
            return {"active": [{
                "assignment_id": "a1",
                "owner_epoch": 3,
                "started_at": None,
            }]}

        def rebind_submission_upload_salvage(
            self, _assignment_id, _nonce, _source_session_id,
            _source_owner_epoch, _expected_owner_epoch, _salvage_session_id,
        ):
            return {"owner_epoch": 4, "replayed": False}

        def register_submission_upload_intent(self, *_args, **_kwargs):
            raise ApiError(
                "server returned 409: upload owner superseded",
                status_code=409,
                code="upload_owner_superseded",
            )

    client = SupersededSalvageClient(
        lambda _aid: pytest.fail("superseded salvage must not submit"),
    )
    outcome = runloop._upload_trial(client, entry, request_salvage=True)

    assert outcome == "upload-blocked"
    saved = pending.load(tmp_path)[0]
    assert saved["upload_blocked"] == "owner_superseded"
    assert "salvage_rebind" not in saved
    assert saved["salvaged_from"] == {
        "runner_session_id": "source-session-0001",
        "owner_epoch": 1,
    }
    assert saved["runner_session_id"].startswith("salvage-")
    assert saved["owner_epoch"] == 4
    assert (trial_dir / "artifacts" / "model.patch").is_file()
    assert client.calls == []

    class FreshSalvageClient(FakeClient):
        def __init__(self):
            super().__init__(
                lambda _aid: {"submission_id": "s1", "grade_status": "pending"},
            )
            self.rebind_calls = []

        def get_assignment(self):
            return {"active": [{
                "assignment_id": "a1",
                "owner_epoch": 5,
                "started_at": None,
            }]}

        def rebind_submission_upload_salvage(
            self, assignment_id, nonce, source_session_id,
            source_owner_epoch, expected_owner_epoch, salvage_session_id,
        ):
            self.rebind_calls.append({
                "assignment_id": assignment_id,
                "nonce": nonce,
                "source_session_id": source_session_id,
                "source_owner_epoch": source_owner_epoch,
                "expected_owner_epoch": expected_owner_epoch,
                "salvage_session_id": salvage_session_id,
            })
            return {"owner_epoch": 6, "replayed": False}

    retry = FreshSalvageClient()
    second_outcome = runloop._upload_trial(
        retry, saved, request_salvage=True,
    )

    assert second_outcome == "submitted"
    assert pending.load(tmp_path) == []
    assert len(retry.rebind_calls) == 1
    rebound = retry.rebind_calls[0]
    assert rebound["source_session_id"] == "source-session-0001"
    assert rebound["source_owner_epoch"] == 1
    assert rebound["expected_owner_epoch"] == 5
    assert rebound["salvage_session_id"].startswith("salvage-")
    assert rebound["salvage_session_id"] != saved["runner_session_id"]
    assert retry.calls == ["a1"]


def test_expired_intent_registration_is_terminal_for_only_that_run(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)

    class ExpiredIntentClient(FakeClient):
        def register_submission_upload_intent(self, *_args, **_kwargs):
            raise ApiError(
                "server returned 410: batch deadline passed",
                status_code=410,
            )

    client = ExpiredIntentClient(lambda _aid: pytest.fail("must not submit"))
    outcome = runloop._upload_trial(
        client, _entry(trial_dir, runner_session_id="session-expired"),
    )

    assert outcome == "expired"
    assert client.calls == []
    assert pending.load(tmp_path) == []


def test_old_server_without_intent_endpoint_keeps_completed_work_for_upgrade(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)

    class OldServerClient(FakeClient):
        def register_submission_upload_intent(self, *_args, **_kwargs):
            raise ApiError("server returned 404: not found", status_code=404)

    client = OldServerClient(
        lambda _aid: {"submission_id": "s1", "grade_status": "pending"},
    )
    assert runloop._upload_trial(
        client, _entry(trial_dir, runner_session_id="session-1234"),
    ) == "upload-failed"
    assert pending.assignment_ids(tmp_path) == {"a1"}
    assert client.calls == []


def test_opted_in_session_archive_runs_after_ack_before_job_cleanup(
    tmp_path: Path, monkeypatch,
):
    home = tmp_path / ".dradar"
    job_dir = home / "work" / "jobs" / "job-a1"
    trial_dir = _make_trial_dir(job_dir, "trial")
    session = trial_dir / "agent" / "sessions" / "2026" / "08" / "16" / "root.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text("{}\n")
    monkeypatch.setattr(runloop, "HOME", home)
    monkeypatch.setattr(runloop, "build_codex_trajectory_bundle", lambda _path: None)
    client = FakeClient(lambda _aid: {
        "submission_id": "s1", "grade_status": "pending",
    })

    outcome = runloop._upload_trial(client, _entry(
        trial_dir, job_dir=str(job_dir), keep=False, archive_session=True,
    ))

    archived = (
        home / "history" / "codex-sessions" / "a1" /
        "2026" / "08" / "16" / "root.jsonl"
    )
    assert outcome == "submitted"
    assert archived.read_text() == "{}\n"
    assert not job_dir.exists()


def test_session_archive_opt_in_persists_across_retry_but_keep_takes_precedence(
    tmp_path: Path, monkeypatch,
):
    home = tmp_path / ".dradar"
    job_dir = home / "work" / "jobs" / "job-a1"
    trial_dir = _make_trial_dir(job_dir, "trial")
    session = trial_dir / "agent" / "sessions" / "root.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text("{}\n")
    monkeypatch.setattr(runloop, "HOME", home)
    monkeypatch.setattr(runloop, "build_codex_trajectory_bundle", lambda _path: None)
    pending.record(home, _entry(
        trial_dir, job_dir=str(job_dir), keep=True, archive_session=True,
    ))
    client = FakeClient(lambda _aid: {
        "submission_id": "s1", "grade_status": "pending",
    })

    runloop._retry_pending_uploads(client)

    assert job_dir.is_dir()
    assert not (home / "history").exists()


def _write_codex_session(path: Path, session_id: str, role: str,
                         input_tokens: int | None, cached: int = 0,
                         output: int = 0, parent: str | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    source = "exec" if role == "user" else {
        "subagent": {"thread_spawn": {"parent_thread_id": parent}}}
    events = [{"type": "session_meta", "payload": {
        "id": session_id, "thread_source": role, "source": source}}]
    events += [
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "turn_context", "payload": {"model": "gpt-5.6-terra"}},
    ]
    if input_tokens is not None:
        events.append({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached,
                "output_tokens": output,
                "reasoning_output_tokens": 0,
                "total_tokens": input_tokens + output,
            }}}})
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def test_upload_replaces_pier_cost_with_complete_multi_agent_sum(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    (trial_dir / "result.json").write_text(json.dumps({"agent_result": {
        "cost_usd": 1.23, "n_input_tokens": 50,
        "n_cache_tokens": 20, "n_output_tokens": 5}}))
    sessions = trial_dir / "agent" / "sessions"
    _write_codex_session(sessions / "root.jsonl", "root-1", "user",
                         100, 60, 10)
    _write_codex_session(sessions / "child.jsonl", "child-1", "subagent",
                         50, 20, 5, parent="root-1")

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            assert meta["cost_usd"] is None
            assert meta["n_input_tokens"] == 150
            assert meta["n_cache_tokens"] == 80
            assert meta["n_output_tokens"] == 15
            assert meta["usage_aggregation_complete"] is True
            assert meta["subagent_session_count"] == 1
            assert len(meta["agent_session_usage"]) == 2
            bundle = json.loads(trajectory_bundle.read_text())
            assert bundle["schema_version"] == meta["usage_aggregation"]
            assert len(bundle["sessions"]) == 2
            uploaded = json.loads(result.read_text())
            agent = uploaded["agent_result"]
            assert agent["cost_usd"] is None
            assert agent["n_input_tokens"] == 150
            assert agent["metadata"]["codex_session_usage"]["complete"] is True
            return {"submission_id": "s1", "grade_status": "pending"}

    outcome = runloop._upload_trial(
        CaptureClient(lambda _aid: None), _entry(trial_dir, meta={"cost_usd": 1.23}))
    assert outcome == "submitted"


def test_upload_omits_bundle_when_redaction_breaks_its_json(
    tmp_path: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    (trial_dir / "result.json").write_text(json.dumps({"agent_result": {
        "cost_usd": 1.23, "n_input_tokens": 50}}))
    sessions = trial_dir / "agent" / "sessions"
    _write_codex_session(sessions / "root.jsonl", "root-1", "user",
                         100, 60, 10)
    _write_codex_session(sessions / "child.jsonl", "child-1", "subagent",
                         50, 20, 5, parent="root-1")
    real_scrub_json_bytes = runloop.scrub_json_bytes
    monkeypatch.setattr(
        runloop, "scrub_json_bytes",
        lambda data: (b'{"broken":"bad\\q"}'
                      if b'"schema_version"' in data
                      else real_scrub_json_bytes(data)),
    )

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            assert trajectory_bundle is None
            assert meta["usage_aggregation_complete"] is True
            agent = json.loads(result.read_text())["agent_result"]
            assert agent["cost_usd"] is None
            assert agent["n_input_tokens"] == 150
            return {"submission_id": "s1", "grade_status": "pending"}

    outcome = runloop._upload_trial(
        CaptureClient(lambda _aid: None),
        _entry(trial_dir, meta={"cost_usd": 1.23}),
    )
    assert outcome == "submitted"
    assert "malformed optional trajectory bundle" in capsys.readouterr().out


def test_upload_replaces_single_session_cost_and_sends_verified_bundle(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    original_result = {"agent_result": {
        "cost_usd": 1.23, "n_input_tokens": 50,
        "n_cache_tokens": 20, "n_output_tokens": 5}}
    (trial_dir / "result.json").write_text(json.dumps(original_result))
    _write_codex_session(
        trial_dir / "agent" / "sessions" / "root.jsonl",
        "root-1", "user", 50, 20, 5,
    )

    original_meta = {
        "cost_usd": 1.23, "n_input_tokens": 50,
        "n_cache_tokens": 20, "n_output_tokens": 5,
    }

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            assert meta["cost_usd"] is None
            assert meta["n_input_tokens"] == 50
            assert meta["n_cache_tokens"] == 20
            assert meta["n_output_tokens"] == 5
            assert meta["usage_aggregation_complete"] is True
            assert meta["agent_session_count"] == 1
            assert meta["subagent_session_count"] == 0
            assert trajectory_bundle is not None
            assert json.loads(trajectory_bundle.read_text())["session_file_count"] == 1
            uploaded = json.loads(result.read_text())["agent_result"]
            assert uploaded["cost_usd"] is None
            assert uploaded["n_input_tokens"] == 50
            return {"submission_id": "s1", "grade_status": "pending"}

    outcome = runloop._upload_trial(
        CaptureClient(lambda _aid: None),
        _entry(trial_dir, meta=original_meta),
    )
    assert outcome == "submitted"


def test_upload_uses_reconciled_kimi_provider_usage_with_audit_bundle(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    bundle = {
        "schema_version": "dradar-kimi-trajectory-bundle-v1",
        "complete": False,
        "session_file_count": 1,
        "agent_session_count": 1,
        "root_session_count": 1,
        "subagent_session_count": 0,
        "sessions": [],
    }
    monkeypatch.setattr(
        runloop, "build_codex_trajectory_bundle", lambda _path: None,
    )
    monkeypatch.setattr(
        runloop, "build_kimi_trajectory_bundle", lambda _path: bundle,
    )
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "provider-usage.json").write_text(json.dumps({
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "kimi-code",
        "model": "k3",
        "complete": True,
        "request_count": 1,
        "session_usage_model_request_count": 1,
        "completed_turn_count": 1,
        "turn_prompt_count": 1,
        "n_input_tokens": 300,
        "n_cache_tokens": 200,
        "n_output_tokens": 21,
        "request_usage_complete": True,
        "request_usage_observed": True,
        "usage_evidence_tier": "complete_reconciled",
        "timed_usage_complete": True,
        "request_ledger_duplicate_count": 0,
        "request_ledger_source": "kimi-code-0.36.1-main-wire-retry-v2",
        "session_usage_compaction_request_count": 0,
        "usage_counters_valid": True,
        "session_identity_valid": True,
        "request_ledger_valid": True,
        "turn_ledger_valid": True,
        "timed_usage_valid": True,
        "wire_metadata_count": 1,
        "token_usage_events": [{
            "occurred_at": "2026-08-20T00:00:00Z",
            "n_input_tokens": 300,
            "n_cache_tokens": 200,
            "n_output_tokens": 21,
        }],
    }))

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            assert meta["n_output_tokens"] == 21
            assert meta["usage_aggregation_complete"] is True
            assert meta["session_usage_model_request_count"] == 1
            assert meta["completed_turn_count"] == 1
            assert meta["turn_prompt_count"] == 1
            assert meta["request_ledger_duplicate_count"] == 0
            assert meta["request_ledger_source"] == (
                "kimi-code-0.36.1-main-wire-retry-v2"
            )
            assert meta["session_usage_compaction_request_count"] == 0
            assert meta["usage_counters_valid"] is True
            assert meta["session_identity_valid"] is True
            assert meta["request_ledger_valid"] is True
            assert meta["turn_ledger_valid"] is True
            assert meta["timed_usage_valid"] is True
            assert meta["wire_metadata_count"] == 1
            assert trajectory_bundle is not None
            uploaded = json.loads(trajectory_bundle.read_text())
            assert uploaded["schema_version"] == bundle["schema_version"]
            assert uploaded["complete"] is False
            return {"submission_id": "s1", "grade_status": "pending"}

    outcome = runloop._upload_trial(
        CaptureClient(lambda _aid: None),
        _entry(trial_dir, meta={
            "n_output_tokens": 321,
            "kimi_cli_version": "0.36.1",
        }),
    )
    assert outcome == "submitted"


def test_upload_prefers_complete_claude_sidecar_over_incomplete_codex_bundle(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "provider-usage.json").write_text(json.dumps({
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "claude-code",
        "model": "claude-sonnet-5",
        "complete": True,
        "request_count": 2,
        "n_input_tokens": 300,
        "n_cache_tokens": 200,
        "n_output_tokens": 21,
        "cache_creation_tokens": 75,
        "request_usage_complete": True,
        "request_usage_observed": True,
        "timed_usage_complete": True,
        "usage_evidence_tier": "complete_reconciled",
        "provider_actual_cost_observed": False,
        "cost_semantics": "api_equivalent_only",
        "token_usage_events": [{
            "occurred_at": "2026-08-31T00:00:00Z",
            "n_input_tokens": 100,
            "n_cache_tokens": 80,
            "n_output_tokens": 10,
        }, {
            "occurred_at": "2026-08-31T00:01:00Z",
            "n_input_tokens": 200,
            "n_cache_tokens": 120,
            "n_output_tokens": 11,
        }],
    }), encoding="utf-8")
    incomplete_bundle = {
        "schema_version": runloop.CODEX_TRAJECTORY_BUNDLE_SCHEMA,
        "complete": False,
        "session_file_count": 2,
        "agent_session_count": 2,
        "root_session_count": 0,
        "subagent_session_count": 0,
        "aggregate_usage": {},
        "timed_usage_complete": False,
        "usage_sessions": [],
        "sessions": [],
    }
    monkeypatch.setattr(
        runloop, "build_codex_trajectory_bundle",
        lambda _path: incomplete_bundle,
    )

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            assert meta["usage_aggregation"] == (
                "dradar-subscription-provider-usage-v1"
            )
            assert meta["usage_aggregation_complete"] is True
            assert meta["request_count"] == 2
            assert meta["n_input_tokens"] == 300
            assert meta["n_cache_tokens"] == 200
            assert meta["n_output_tokens"] == 21
            assert trajectory_bundle is not None
            assert json.loads(trajectory_bundle.read_text())["complete"] is False
            return {"submission_id": "s1", "grade_status": "pending"}

    assert runloop._upload_trial(
        CaptureClient(lambda _aid: None),
        _entry(trial_dir, meta={"claude_cli_version": "2.1.251"}),
    ) == "submitted"


def test_upload_rebuilds_claude_usage_from_trajectory_when_sidecar_is_missing(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "trajectory.json").write_text(json.dumps({
        "agent": {"model_name": "claude-sonnet-5"},
        "steps": [{
            "timestamp": "2026-09-01T00:00:00Z",
            "metrics": {
                "prompt_tokens": 300,
                "cached_tokens": 200,
                "completion_tokens": 21,
                "extra": {"cache_creation_input_tokens": 75},
            },
        }],
        "final_metrics": {
            "total_prompt_tokens": 300,
            "total_cached_tokens": 200,
            "total_completion_tokens": 21,
            "total_cost_usd": 0.25,
            "extra": {"total_cache_creation_input_tokens": 75},
        },
    }), encoding="utf-8")
    incomplete_bundle = {
        "schema_version": runloop.CODEX_TRAJECTORY_BUNDLE_SCHEMA,
        "complete": False,
        "session_file_count": 1,
        "agent_session_count": 1,
        "root_session_count": 0,
        "subagent_session_count": 0,
        "aggregate_usage": {},
        "timed_usage_complete": False,
        "usage_sessions": [],
        "sessions": [],
    }
    monkeypatch.setattr(
        runloop, "build_codex_trajectory_bundle",
        lambda _path: incomplete_bundle,
    )

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            assert meta["usage_aggregation"] == (
                "dradar-subscription-provider-usage-v1"
            )
            assert meta["usage_aggregation_complete"] is True
            assert meta["request_count"] == 1
            assert meta["n_input_tokens"] == 300
            assert meta["n_cache_tokens"] == 200
            assert meta["n_output_tokens"] == 21
            assert meta["subscription_reported_cost_usd"] == 0.25
            assert meta["claude_usage_source"] == "uploaded-trajectory"
            return {"submission_id": "s1", "grade_status": "pending"}

    assert runloop._upload_trial(
        CaptureClient(lambda _aid: None),
        _entry(trial_dir, meta={
            "claude_cli_version": "2.1.251",
            "claude_model": "claude-sonnet-5",
        }),
    ) == "submitted"


def test_claude_trajectory_read_retries_a_transient_partial_json(
    tmp_path: Path,
    monkeypatch,
):
    trial_dir = _make_trial_dir(tmp_path)
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    trajectory_path = agent_dir / "trajectory.json"
    trajectory_path.write_text("{", encoding="utf-8")
    complete = json.dumps({
        "agent": {"model_name": "claude-sonnet-5"},
        "steps": [{
            "timestamp": "2026-09-01T00:00:00Z",
            "metrics": {
                "prompt_tokens": 10,
                "cached_tokens": 6,
                "completion_tokens": 2,
                "extra": {"cache_creation_input_tokens": 3},
            },
        }],
        "final_metrics": {
            "total_prompt_tokens": 10,
            "total_cached_tokens": 6,
            "total_completion_tokens": 2,
            "total_cost_usd": 0.01,
            "extra": {"total_cache_creation_input_tokens": 3},
        },
    })
    original_read_text = Path.read_text
    reads = 0

    def finish_after_first_read(path, *args, **kwargs):
        nonlocal reads
        value = original_read_text(path, *args, **kwargs)
        if path == trajectory_path:
            reads += 1
            if reads == 1:
                trajectory_path.write_text(complete, encoding="utf-8")
        return value

    delays = []
    monkeypatch.setattr(Path, "read_text", finish_after_first_read)
    monkeypatch.setattr(runloop.time, "sleep", delays.append)

    usage = runloop._claude_trial_usage_from_trajectory(
        trial_dir, "claude-sonnet-5", attempts=3, retry_delay=0.2,
    )

    assert usage is not None and usage["request_count"] == 1
    assert reads == 2
    assert delays == [0.2]


def test_upload_does_not_rebuild_claude_usage_for_wrong_assignment_model(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "trajectory.json").write_text(json.dumps({
        "agent": {"model_name": "claude-sonnet-5"},
        "steps": [{
            "timestamp": "2026-09-01T00:00:00Z",
            "metrics": {
                "prompt_tokens": 10,
                "cached_tokens": 0,
                "completion_tokens": 1,
                "extra": {},
            },
        }],
        "final_metrics": {
            "total_prompt_tokens": 10,
            "total_cached_tokens": 0,
            "total_completion_tokens": 1,
            "extra": {"total_cache_creation_input_tokens": 0},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(runloop, "build_codex_trajectory_bundle", lambda _path: None)
    monkeypatch.setattr(runloop, "build_kimi_trajectory_bundle", lambda _path: None)

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            assert "usage_aggregation" not in meta
            assert "n_input_tokens" not in meta
            assert meta["claude_usage_diagnostic"] == (
                "provider-sidecar-and-trajectory-unavailable-or-invalid"
            )
            return {"submission_id": "s1", "grade_status": "pending"}

    assert runloop._upload_trial(
        CaptureClient(lambda _aid: None),
        _entry(trial_dir, meta={
            "claude_cli_version": "2.1.251",
            "claude_model": "claude-opus-5",
        }),
    ) == "submitted"


def test_incomplete_kimi_usage_keeps_tokens_and_cost_unavailable(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "provider-usage.json").write_text(json.dumps({
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "kimi-code",
        "model": "k3",
        "complete": False,
        "request_count": 1,
        "n_input_tokens": 300,
        "n_cache_tokens": 200,
        "n_output_tokens": 21,
        "request_usage_complete": False,
        "request_usage_observed": True,
        "usage_evidence_tier": "observed_unreconciled",
        "usage_incomplete_reason": "turn_completion_ledger_mismatch",
        "timed_usage_complete": False,
        "provider_actual_cost_observed": False,
        "cost_semantics": "unavailable_incomplete_tokens",
        "token_usage_events": [{
            "occurred_at": "2026-08-20T00:00:00Z",
            "n_input_tokens": 300,
            "n_cache_tokens": 200,
            "n_output_tokens": 21,
        }],
    }))

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            assert meta["usage_aggregation_complete"] is False
            assert meta["n_input_tokens"] is None
            assert meta["n_cache_tokens"] is None
            assert meta["n_output_tokens"] is None
            assert meta["cost_usd"] is None
            assert meta["provider_actual_cost_observed"] is False
            assert meta["cost_semantics"] == "unavailable_incomplete_tokens"
            return {"submission_id": "s1", "grade_status": "pending"}

    assert runloop._upload_trial(
        CaptureClient(lambda _aid: None),
        _entry(trial_dir, meta={"kimi_cli_version": "0.36.1"}),
    ) == "submitted"


def test_upload_suppresses_cost_when_any_subagent_usage_is_missing(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    (trial_dir / "result.json").write_text(json.dumps({"agent_result": {
        "cost_usd": 1.23, "n_input_tokens": 50}}))
    sessions = trial_dir / "agent" / "sessions"
    _write_codex_session(sessions / "root.jsonl", "root-1", "user", 100, 60, 10)
    _write_codex_session(sessions / "child.jsonl", "child-1", "subagent",
                         None, parent="root-1")

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            assert meta["cost_usd"] is None
            assert meta["n_input_tokens"] is None
            assert meta["usage_aggregation_complete"] is False
            assert trajectory_bundle is not None
            agent = json.loads(result.read_text())["agent_result"]
            assert agent["cost_usd"] is None
            assert agent["n_input_tokens"] is None
            return {"submission_id": "s1", "grade_status": "pending"}

    outcome = runloop._upload_trial(
        CaptureClient(lambda _aid: None), _entry(trial_dir, meta={"cost_usd": 1.23}))
    assert outcome == "submitted"


def test_retry_rebuilds_and_resends_multi_agent_bundle(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    (trial_dir / "result.json").write_text(json.dumps({"agent_result": {
        "cost_usd": 1.23}}))
    sessions = trial_dir / "agent" / "sessions"
    _write_codex_session(sessions / "root.jsonl", "root-1", "user", 100, 60, 10)
    _write_codex_session(sessions / "child.jsonl", "child-1", "subagent",
                         50, 20, 5, parent="root-1")

    class FlakyClient(FakeClient):
        def __init__(self):
            super().__init__(lambda _aid: None)
            self.attempts = 0

        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            self.attempts += 1
            assert trajectory_bundle is not None
            assert json.loads(trajectory_bundle.read_text())["complete"] is True
            if self.attempts == 1:
                raise ApiError("server returned 503: retry", status_code=503)
            return {"submission_id": "s1", "grade_status": "pending"}

    client = FlakyClient()
    first = runloop._upload_trial(
        client, _entry(trial_dir, meta={"cost_usd": 1.23}))
    assert first == "upload-failed"
    retry_entry = pending.load(tmp_path)[0]
    second = runloop._upload_trial(client, retry_entry)
    assert second == "submitted"
    assert client.attempts == 2
    assert pending.load(tmp_path) == []


def test_upload_omits_malformed_optional_trajectory(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    (trial_dir / "agent").mkdir()
    (trial_dir / "agent" / "trajectory.json").write_bytes(
        b'{"agent":{},"steps":["bad\\q"]}'
    )
    (trial_dir / "result.json").write_text("{}")

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None):
            assert trajectory is None
            assert result is not None
            return {"submission_id": "s1", "grade_status": "pending"}

    outcome = runloop._upload_trial(CaptureClient(lambda _aid: None), _entry(trial_dir))
    assert outcome == "submitted"
    assert "malformed optional trajectory" in capsys.readouterr().out


def test_upload_preserves_valid_trajectory_when_redaction_has_escaped_quote(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    (trial_dir / "agent").mkdir()
    token = "abcdefghijklmnop"
    payload = {"steps": [{
        "message": "authorization:" + token + '"suffix',
    }]}
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps(payload))
    (trial_dir / "result.json").write_text("{}")

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None):
            uploaded = json.loads(trajectory.read_text())
            message = uploaded["steps"][0]["message"]
            assert token not in message
            assert message.endswith('"suffix')
            return {"submission_id": "s1", "grade_status": "pending"}

    outcome = runloop._upload_trial(
        CaptureClient(lambda _aid: None), _entry(trial_dir),
    )
    assert outcome == "submitted"


def test_upload_success_removes_current_and_superseded_checkpoint_jobs(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    aid = "1" * 32
    jobs = []
    for suffix in ("old", "new"):
        job = tmp_path / "work" / "jobs" / f"a{aid}-{suffix}"
        checkpoint = job / f"task__{suffix}" / "agent" / "checkpoint"
        checkpoint.mkdir(parents=True)
        (checkpoint / "checkpoint.json").write_text(json.dumps({
            "schema_version": 1, "checkpoint_id": f"checkpoint-{suffix}12345678",
            "assignment_id": aid, "phase": "agent_completed",
            "created_at": "2026-07-16T00:00:00Z",
            "updated_at": "2026-07-16T01:00:00Z",
            "resume_generation": 1,
        }))
        jobs.append(job)
    trial_dir = jobs[-1] / "task__new"
    (trial_dir / "artifacts").mkdir(parents=True)
    (trial_dir / "artifacts" / "model.patch").write_text("diff --git a b\n")
    client = FakeClient(lambda aid_: {"submission_id": "s1", "grade_status": "pending"})
    outcome = runloop._upload_trial(client, _entry(
        trial_dir, assignment_id=aid, job_dir=str(jobs[-1]), keep=False,
        resume_generation=1,
    ))
    assert outcome == "submitted"
    assert not any(job.exists() for job in jobs)


def test_interactive_upload_defaults_to_cleaning_local_job(
    tmp_path: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    job = tmp_path / "work" / "jobs" / f"a{'2' * 32}-one"
    trial = job / "task__one"
    checkpoint = trial / "agent" / "checkpoint"
    checkpoint.mkdir(parents=True)
    (checkpoint / "checkpoint.json").write_text(json.dumps({
        "schema_version": 1, "checkpoint_id": "checkpoint-clean123",
        "assignment_id": "2" * 32, "phase": "agent_completed",
        "updated_at": "2026-07-18T01:00:00Z", "resume_generation": 0,
    }))
    (trial / "artifacts").mkdir()
    (trial / "artifacts" / "model.patch").write_text("diff --git a b\n")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    outcome = runloop._upload_trial(
        FakeClient(lambda _aid: {"submission_id": "s1", "grade_status": "pending"}),
        _entry(trial, assignment_id="2" * 32, job_dir=str(job), keep=False),
        ask_cleanup=True,
    )
    assert outcome == "submitted"
    assert not job.exists()
    assert "local task files cleaned" in capsys.readouterr().out


def test_interactive_upload_can_keep_and_protect_local_job(
    tmp_path: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    job = tmp_path / "work" / "jobs" / f"a{'3' * 32}-one"
    trial = job / "task__one"
    checkpoint = trial / "agent" / "checkpoint"
    checkpoint.mkdir(parents=True)
    (checkpoint / "checkpoint.json").write_text(json.dumps({
        "schema_version": 1, "checkpoint_id": "checkpoint-keep1234",
        "assignment_id": "3" * 32, "phase": "agent_completed",
        "updated_at": "2026-07-18T01:00:00Z", "resume_generation": 0,
    }))
    (trial / "artifacts").mkdir()
    (trial / "artifacts" / "model.patch").write_text("diff --git a b\n")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    outcome = runloop._upload_trial(
        FakeClient(lambda _aid: {"submission_id": "s1", "grade_status": "pending"}),
        _entry(trial, assignment_id="3" * 32, job_dir=str(job), keep=False),
        ask_cleanup=True,
    )
    assert outcome == "submitted"
    assert job.is_dir()
    assert (job / ".dradar-keep").is_file()
    assert "local artifacts kept" in capsys.readouterr().out


def test_upload_failure_records_ledger_entry(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)

    def fail(aid):
        raise ApiError("server returned 500: internal error", status_code=500)
    client = FakeClient(fail)
    attempted = _entry(trial_dir, meta={"k": "v"})
    outcome = runloop._upload_trial(client, attempted)
    assert outcome == "upload-failed"
    entries = pending.load(tmp_path)
    assert len(entries) == 1
    for key, value in attempted.items():
        assert entries[0][key] == value
    assert entries[0]["artifact_staging_schema"] == 1
    assert entries[0]["patch_bytes"] > 0
    assert len(entries[0]["patch_sha256"]) == 64
    assert Path(entries[0]["patch_source_path"]).read_bytes() == (
        trial_dir / "artifacts" / "model.patch"
    ).read_bytes()
    assert entries[0]["trial_dir"] == str(trial_dir)


def test_upload_409_means_already_landed_clears_ledger(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    pending.record(tmp_path, _entry(trial_dir))

    def already(aid):
        raise ApiError("server returned 409: already submitted", status_code=409)
    client = FakeClient(already)
    outcome = runloop._upload_trial(client, _entry(trial_dir))
    assert outcome == "submitted"
    assert pending.load(tmp_path) == []


def test_stale_generation_409_is_not_misread_as_already_submitted(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)

    def stale(aid):
        raise ApiError(
            "server returned 409: stale recovery generation; current generation is 2",
            status_code=409,
        )

    outcome = runloop._upload_trial(
        FakeClient(stale), _entry(trial_dir, resume_generation=1),
    )
    assert outcome == "upload-failed"
    assert len(pending.load(tmp_path)) == 1


def test_upload_410_means_expired_clears_ledger(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    pending.record(tmp_path, _entry(trial_dir))

    def expired(aid):
        raise ApiError("server returned 410: lease expired", status_code=410)
    client = FakeClient(expired)
    outcome = runloop._upload_trial(client, _entry(trial_dir))
    assert outcome == "expired"
    assert pending.load(tmp_path) == []


def test_transient_failure_with_409_in_message_is_not_misread_as_conflict(tmp_path: Path, monkeypatch):
    # A transport-level failure (never got a real HTTP response) has no
    # status_code -- even if the formatted message happens to contain the
    # digits "409" (e.g. embedded in the server URL/port), it must NOT be
    # treated as "already submitted". Only a real 409 response may do that.
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    pending.record(tmp_path, _entry(trial_dir))

    def fail(aid):
        raise ApiError("cannot reach https://dradar.example.com:8409: connection refused")
    client = FakeClient(fail)
    outcome = runloop._upload_trial(client, _entry(trial_dir))
    assert outcome == "upload-failed"  # not "submitted"
    assert len(pending.load(tmp_path)) == 1  # entry survives for a real retry


def test_secret_in_added_patch_line_is_redacted_and_uploaded(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = tmp_path / "t"
    (trial_dir / "artifacts").mkdir(parents=True)
    (trial_dir / "artifacts" / "model.patch").write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n old = True\n"
        "+api_key = \"ghp_ABCDEFghijkl0123456789ABCDEFghijkl0123\"\n")
    pending.record(tmp_path, _entry(trial_dir))
    uploaded = {}

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None):
            uploaded["patch"] = patch.read_bytes()
            uploaded["meta"] = meta
            self.calls.append(assignment_id)
            return {"submission_id": "s1", "grade_status": "pending"}

    client = CaptureClient(lambda _aid: None)
    outcome = runloop._upload_trial(client, _entry(trial_dir))
    assert outcome == "submitted"
    assert pending.load(tmp_path) == []
    assert b"ghp_" not in uploaded["patch"]
    assert uploaded["meta"]["patch_redacted"] is True
    assert "GHP" in uploaded["meta"]["patch_redaction_labels"]
    assert client.stopped == []


def test_secret_in_patch_context_is_not_uploaded_and_assignment_is_stopped(
        tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = tmp_path / "t"
    (trial_dir / "artifacts").mkdir(parents=True)
    (trial_dir / "artifacts" / "model.patch").write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n"
        " ghp_ABCDEFghijkl0123456789ABCDEFghijkl0123\n+safe = True\n")
    client = FakeClient(lambda _aid: {"submission_id": "s1"})
    assert runloop._upload_trial(client, _entry(trial_dir)) == "not-uploaded"
    assert client.calls == []
    assert client.stopped == ["a1"]


def test_missing_patch_stays_retryable_without_stopping_assignment(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = tmp_path / "missing"
    trial_dir.mkdir()
    client = FakeClient(lambda _aid: {"submission_id": "s1"})
    assert runloop._upload_trial(client, _entry(trial_dir)) == "artifact-staging-failed"
    assert client.stopped == []
    entry = pending.load(tmp_path)[0]
    assert entry["artifact_staging_failure"]["reason"] == "source_and_staged_missing"


def test_redacted_patch_retry_never_falls_back_to_raw_secret(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = tmp_path / "t"
    (trial_dir / "artifacts").mkdir(parents=True)
    (trial_dir / "artifacts" / "model.patch").write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n old = True\n"
        "+token = \"ghp_ABCDEFghijkl0123456789ABCDEFghijkl0123\"\n")

    class FlakyRedactionClient(FakeClient):
        def __init__(self):
            super().__init__(lambda _aid: None)
            self.patches = []

        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None):
            self.patches.append(patch.read_bytes())
            if len(self.patches) == 1:
                raise ApiError("temporary", status_code=503)
            return {"submission_id": "s1", "grade_status": "pending"}

    client = FlakyRedactionClient()
    assert runloop._upload_trial(client, _entry(trial_dir)) == "upload-failed"
    retry_entry = pending.load(tmp_path)[0]
    assert runloop._upload_trial(client, retry_entry) == "submitted"
    assert len(client.patches) == 2
    assert all(b"ghp_" not in patch for patch in client.patches)
    assert pending.load(tmp_path) == []


def test_retry_recovers_missing_staged_patch_and_uploads_verified_source(
    tmp_path: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    raw = (trial_dir / "artifacts" / "model.patch").read_bytes()

    first = FakeClient(_raise(503))
    assert runloop._upload_trial(first, _entry(trial_dir)) == "upload-failed"
    retry_entry = pending.load(tmp_path)[0]
    source = Path(retry_entry["patch_source_path"])
    staged = Path(retry_entry["patch_staged_path"])
    assert source.read_bytes() == raw
    staged.unlink()  # pause/process teardown lost only the canonical copy

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None):
            assert patch.read_bytes() == raw
            assert meta["artifact_staging_recovery"]["reason"] == (
                "source-present/staged-missing"
            )
            self.calls.append(assignment_id)
            return {"submission_id": "s-recovered", "grade_status": "pending"}

    client = CaptureClient(lambda _aid: None)
    assert runloop._upload_trial(client, retry_entry) == "submitted"
    assert client.calls == ["a1"]
    assert staged.read_bytes() == raw
    assert source.read_bytes() == raw
    assert pending.load(tmp_path) == []
    assert "recovered model.patch staging" in capsys.readouterr().out


def test_retry_digest_mismatch_keeps_both_files_and_never_uploads(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    assert runloop._upload_trial(
        FakeClient(_raise(503)), _entry(trial_dir),
    ) == "upload-failed"
    retry_entry = pending.load(tmp_path)[0]
    source = Path(retry_entry["patch_source_path"])
    staged = Path(retry_entry["patch_staged_path"])
    source_before = source.read_bytes()
    staged_mismatch = b"diff --git a/wrong b/wrong\n"
    staged.write_bytes(staged_mismatch)
    client = FakeClient(lambda _aid: {"submission_id": "must-not-upload"})

    assert runloop._upload_trial(client, retry_entry) == "artifact-staging-failed"
    assert client.calls == []
    assert source.read_bytes() == source_before
    assert staged.read_bytes() == staged_mismatch
    retained = pending.load(tmp_path)[0]
    assert retained["artifact_staging_failure"]["reason"] == "staged_digest_mismatch"


def test_submit_uses_verified_snapshot_if_local_copies_disappear_mid_request(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    raw = (trial_dir / "artifacts" / "model.patch").read_bytes()

    class TeardownClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None):
            entry = pending.load(tmp_path)[0]
            Path(entry["patch_source_path"]).unlink()
            Path(entry["patch_staged_path"]).unlink()
            # The multipart request owns a verified temp snapshot; teardown of
            # the trial tree cannot truncate its in-flight payload.
            assert patch.read_bytes() == raw
            self.calls.append(assignment_id)
            return {"submission_id": "s-snapshot", "grade_status": "pending"}

    client = TeardownClient(lambda _aid: None)
    assert runloop._upload_trial(client, _entry(trial_dir)) == "submitted"
    assert client.calls == ["a1"]
    assert pending.load(tmp_path) == []


def test_crash_mid_submit_leaves_a_ledger_entry(tmp_path: Path, monkeypatch):
    # The entry is recorded BEFORE the submit attempt: a process death
    # mid-POST (Ctrl-C/kill/OOM while the multipart upload is in flight)
    # must not orphan a completed, quota-burning trial. Simulate the death
    # with an exception that is not an ApiError, so nothing catches it.
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)

    def die(aid):
        raise KeyboardInterrupt
    client = FakeClient(die)
    attempted = _entry(trial_dir)
    with pytest.raises(KeyboardInterrupt):
        runloop._upload_trial(client, attempted)
    entries = pending.load(tmp_path)
    assert len(entries) == 1
    for key, value in attempted.items():
        assert entries[0][key] == value
    assert entries[0]["artifact_staging_schema"] == 1
    assert Path(entries[0]["patch_source_path"]).is_file()


def test_response_loss_restart_reuses_intent_and_clears_already_submitted(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)

    def disconnect(_aid):
        raise ApiError("Server disconnected", status_code=None)

    first = FakeClient(disconnect)
    entry = _entry(
        trial_dir,
        resume_generation=0,
        runner_session_id="session-1234",
    )
    assert runloop._upload_trial(first, entry) == "upload-failed"
    persisted = pending.load(tmp_path)
    assert len(persisted) == 1
    saved = persisted[0]["upload_intent"]
    assert saved["id"] == first.intent_calls[0][3]
    assert saved["manifest"]["session_id"] == "session-1234"

    def already_submitted(_aid):
        raise ApiError(
            "server returned 409: already submitted", status_code=409,
        )

    restarted = FakeClient(already_submitted)
    assert runloop._upload_trial(restarted, persisted[0]) == "submitted"
    assert restarted.intent_calls[0][3] == saved["id"]
    assert restarted.last_upload_intent_id == saved["id"]
    assert pending.load(tmp_path) == []


def test_saved_intent_rejects_changed_prepared_meta(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)

    def disconnect(_aid):
        raise ApiError("Server disconnected", status_code=None)

    first = FakeClient(disconnect)
    assert runloop._upload_trial(first, _entry(
        trial_dir, runner_session_id="session-1234",
    )) == "upload-failed"
    changed = pending.load(tmp_path)[0]
    changed["meta"] = {"changed_after_intent": True}
    second = FakeClient(lambda _aid: pytest.fail("must not submit changed body"))
    assert runloop._upload_trial(second, changed) == "upload-blocked"
    assert second.intent_calls == []


def test_successful_submit_leaves_no_pre_recorded_entry_behind(tmp_path: Path, monkeypatch):
    # Negative control for record-before-submit: on success the pre-recorded
    # entry must be removed, not linger and get re-uploaded on the next go.
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    client = FakeClient(lambda aid: {"submission_id": "s1", "grade_status": "pending"})
    outcome = runloop._upload_trial(client, _entry(trial_dir))
    assert outcome == "submitted"
    assert client.calls == ["a1"]  # the upload really happened
    assert pending.load(tmp_path) == []


# --- retry scan: reconstructs artifacts from a trial_dir alone -------------

def test_artifacts_from_trial_dir_matches_runner_layout(tmp_path: Path):
    trial_dir = tmp_path / "trial"
    (trial_dir / "artifacts").mkdir(parents=True)
    (trial_dir / "artifacts" / "model.patch").write_text("diff\n")
    (trial_dir / "agent").mkdir()
    (trial_dir / "agent" / "trajectory.json").write_text("{}")
    (trial_dir / "result.json").write_text("{}")
    patch, traj, result = runloop._artifacts_from_trial_dir(trial_dir)
    assert patch == trial_dir / "artifacts" / "model.patch"
    assert traj == trial_dir / "agent" / "trajectory.json"
    assert result == trial_dir / "result.json"


def test_artifacts_from_trial_dir_tolerates_missing_optional_files(tmp_path: Path):
    trial_dir = tmp_path / "trial"
    (trial_dir / "artifacts").mkdir(parents=True)
    (trial_dir / "artifacts" / "model.patch").write_text("diff\n")
    patch, traj, result = runloop._artifacts_from_trial_dir(trial_dir)
    assert patch.is_file() and traj is None and result is None


def test_retry_scan_uploads_each_pending_entry(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    t1, t2 = _make_trial_dir(tmp_path, "t1"), _make_trial_dir(tmp_path, "t2")
    pending.record(tmp_path, {"assignment_id": "a1", "nonce": "n1", "task_id": "task1",
                              "trial_dir": str(t1), "meta": {}, "outcome": "completed"})
    pending.record(tmp_path, {"assignment_id": "a2", "nonce": "n2", "task_id": "task2",
                              "trial_dir": str(t2), "meta": {}, "outcome": "completed"})
    client = FakeClient(lambda aid: {"submission_id": f"s-{aid}", "grade_status": "pending"})
    runloop._retry_pending_uploads(client)
    assert set(client.calls) == {"a1", "a2"}
    assert pending.load(tmp_path) == []


def test_retry_scan_keeps_entries_with_missing_local_artifacts(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    pending.record(tmp_path, {"assignment_id": "gone", "nonce": "n", "task_id": "t",
                              "trial_dir": str(tmp_path / "never-existed"), "meta": {},
                              "outcome": "completed"})
    client = FakeClient(lambda aid: {"submission_id": "s"})
    runloop._retry_pending_uploads(client)
    assert not client.calls  # never even tried the network call
    entries = pending.load(tmp_path)
    assert len(entries) == 1
    assert entries[0]["artifact_staging_failure"]["reason"] == "source_and_staged_missing"


def test_retry_scan_honors_keep_flag_recorded_at_failure_time(tmp_path: Path, monkeypatch):
    # `dradar go --keep` must still mean "keep the job dir" even if the
    # upload fails and gets replayed later by retry-upload -- the ledger
    # entry is where that intent has to survive to, since the original
    # process (and its args.keep) is long gone by retry time.
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    t1 = _make_trial_dir(tmp_path, "t1")
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    pending.record(tmp_path, {"assignment_id": "a1", "nonce": "n1", "task_id": "task1",
                              "trial_dir": str(t1), "meta": {}, "outcome": "completed",
                              "job_dir": str(job_dir), "keep": True})
    client = FakeClient(lambda aid: {"submission_id": "s1", "grade_status": "pending"})
    runloop._retry_pending_uploads(client)
    assert job_dir.is_dir()  # --keep honored even on a replayed upload


def test_retry_scan_cleans_job_dir_when_keep_was_not_set(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    t1 = _make_trial_dir(tmp_path, "t1")
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    pending.record(tmp_path, {"assignment_id": "a1", "nonce": "n1", "task_id": "task1",
                              "trial_dir": str(t1), "meta": {}, "outcome": "completed",
                              "job_dir": str(job_dir)})  # no "keep" -- defaults False
    client = FakeClient(lambda aid: {"submission_id": "s1", "grade_status": "pending"})
    runloop._retry_pending_uploads(client)
    assert not job_dir.exists()


def test_retry_scan_is_silent_noop_when_nothing_pending(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    client = FakeClient(lambda aid: {"submission_id": "s"})
    runloop._retry_pending_uploads(client)
    assert not client.calls
    assert capsys.readouterr().out == ""


# --- cmd_retry_upload: the standalone command -------------------------------

def test_cmd_retry_upload_reports_all_clear(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_load_config", lambda: {"server": "https://x", "token": "t"})
    monkeypatch.setattr(runloop, "_client", lambda cfg: FakeClient(lambda aid: {"submission_id": "s"}))
    rc = runloop.cmd_retry_upload(None)
    assert rc == 0
    assert "nothing pending" in capsys.readouterr().out


def test_cmd_retry_upload_flushes_and_succeeds(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    t1 = _make_trial_dir(tmp_path, "t1")
    pending.record(tmp_path, {"assignment_id": "a1", "nonce": "n1", "task_id": "task1",
                              "trial_dir": str(t1), "meta": {}, "outcome": "completed"})
    monkeypatch.setattr(runloop, "_load_config", lambda: {"server": "https://x", "token": "t"})
    monkeypatch.setattr(runloop, "_client", lambda cfg: FakeClient(lambda aid: {"submission_id": "s"}))
    rc = runloop.cmd_retry_upload(None)
    assert rc == 0
    assert "all clear" in capsys.readouterr().out
    assert pending.load(tmp_path) == []


def test_cmd_retry_upload_partial_failure_reports_rc_1_and_keeps_entry(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    t1 = _make_trial_dir(tmp_path, "t1")
    pending.record(tmp_path, {"assignment_id": "a1", "nonce": "n1", "task_id": "task1",
                              "trial_dir": str(t1), "meta": {}, "outcome": "completed"})

    def fail(aid):
        raise ApiError("server returned 500: internal error", status_code=500)
    monkeypatch.setattr(runloop, "_load_config", lambda: {"server": "https://x", "token": "t"})
    monkeypatch.setattr(runloop, "_client", lambda cfg: FakeClient(fail))
    rc = runloop.cmd_retry_upload(None)
    assert rc == 1
    assert "still pending" in capsys.readouterr().out
    entries = pending.load(tmp_path)
    assert len(entries) == 1 and entries[0]["assignment_id"] == "a1"  # kept for the next retry


def test_cmd_retry_upload_reports_blocked_as_manual_review(
    tmp_path: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    pending.record(tmp_path, {
        "assignment_id": "a1", "task_id": "task1",
        "upload_blocked": "owner_superseded",
    })
    client = FakeClient(lambda _aid: pytest.fail("blocked retry stays local"))
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda _cfg: client)

    assert runloop.cmd_retry_upload(None) == 1
    output = capsys.readouterr().out
    assert "require explicit review" in output
    assert "will not be retried automatically" in output
    assert "will retry again" not in output
    assert pending.assignment_ids(tmp_path) == {"a1"}


def test_blocked_upload_fences_go_resume_pool_and_direct_model_start(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    pending.record(tmp_path, {
        "assignment_id": "a1", "task_id": "task1",
        "upload_blocked": "owner_superseded",
    })
    assignment = {
        "assignment_id": "a1", "task_id": "task1", "nonce": "n1",
        "started_at": None, "retry_after": None,
    }

    class InventoryClient:
        def get_assignment(self):
            return {"active": [assignment], "free_pick": True}

    client = InventoryClient()
    # Both `go` and `resume` enter through the same acquisition gate.
    assert runloop._acquire_batch(client, True) == ([], True)
    # The supervisor must not count a blocked assignment as refill capacity.
    assert runloop._pool_ready_work_count(client) == 0
    monkeypatch.setattr(runloop, "check_task_content_hash", lambda *_a: True)
    monkeypatch.setattr(
        runloop, "run_trial",
        lambda *_a, **_k: pytest.fail("blocked assignment must not run a model"),
    )
    args = type("Args", (), {
        "allow_task_drift": False,
        "dev_agent": False,
    })()
    assert runloop._run_and_submit(
        client, assignment, tmp_path, args, None,
    ) == "pending-upload"


def _raise(status):
    def behavior(_aid):
        raise ApiError(f"server returned {status}: nope", status_code=status)
    return behavior


def test_definitively_rejected_upload_drops_ledger_entry(tmp_path: Path, monkeypatch, capsys):
    """A 413 cannot succeed with the same bytes on a later retry."""
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    client = FakeClient(_raise(413))
    outcome = runloop._upload_trial(client, _entry(trial_dir))
    assert outcome == "rejected"
    assert pending.load(tmp_path) == []
    out = capsys.readouterr().out
    assert "retrying can't fix it" in out
    assert str(trial_dir) in out  # the local files are named, not vaporized
    assert client.stopped == ["a1"]


def test_zcode_pompeii_preflight_names_binary_file_before_submit(
    tmp_path: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    raw_patch = (
        b"diff --git a/model_answer.json b/model_answer.json\n"
        b"index 9e26dfe..e9cfe4b 100644\n"
        b"--- a/model_answer.json\n"
        b"+++ b/model_answer.json\n"
        b"@@ -1 +1 @@\n-{}\n+{\"edges\":[]}\n"
        b"diff --git a/cache/stitch.png b/cache/stitch.png\n"
        b"new file mode 100644\n"
        b"index 0000000..1111111\n"
        b"GIT binary patch\n"
        b"literal 4\nLc${NkU|;|M00aO5\n\n"
    )
    (trial_dir / "artifacts" / "model.patch").write_bytes(raw_patch)
    client = FakeClient(lambda _aid: pytest.fail("guarded patch must not submit"))
    entry = _entry(
        trial_dir,
        task_id="pompeii-adjacency-rp-085",
        meta={"model_runtime_profile": runloop.ZCODE_RUNTIME_PROFILE},
    )

    outcome = runloop._upload_trial(client, entry)

    assert outcome == "assignment-reopened"
    assert client.calls == []
    assert client.stopped == ["a1"]
    assert pending.load(tmp_path) == []
    assert (trial_dir / "artifacts" / "model.patch").read_bytes() == raw_patch
    out = capsys.readouterr().out
    assert "patch preflight blocked upload" in out
    assert "cache/stitch.png" in out
    assert "binary" in out
    assert "independent ZCode re-solve" in out


def test_zcode_pompeii_preflight_allows_small_model_answer_patch(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    (trial_dir / "artifacts" / "model.patch").write_bytes(
        b"diff --git a/model_answer.json b/model_answer.json\n"
        b"index 9e26dfe..e9cfe4b 100644\n"
        b"--- a/model_answer.json\n"
        b"+++ b/model_answer.json\n"
        b"@@ -1 +1 @@\n-{}\n+{\"edges\":[]}\n"
    )
    client = FakeClient(
        lambda _aid: {"submission_id": "s1", "grade_status": "pending"},
    )

    outcome = runloop._upload_trial(
        client,
        _entry(
            trial_dir,
            task_id="pompeii-adjacency-rp-086",
            meta={"zcode_protocol_version": 1},
        ),
    )

    assert outcome == "submitted"
    assert client.calls == ["a1"]


def test_definitive_rejection_preserves_checkpoint_job(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    aid = "4" * 32
    job = tmp_path / "work" / "jobs" / f"a{aid}"
    trial = job / "task__one"
    checkpoint = trial / "agent" / "checkpoint"
    checkpoint.mkdir(parents=True)
    (checkpoint / "checkpoint.json").write_text(json.dumps({
        "schema_version": 1, "checkpoint_id": "checkpoint-rejected1",
        "assignment_id": aid, "phase": "agent_completed",
        "updated_at": "2026-07-19T01:00:00Z", "resume_generation": 0,
    }))
    (trial / "artifacts").mkdir()
    (trial / "artifacts" / "model.patch").write_text("diff --git a b\n")
    client = FakeClient(_raise(413))
    outcome = runloop._upload_trial(
        client,
        _entry(trial, assignment_id=aid, job_dir=str(job), keep=False),
    )
    assert outcome == "rejected"
    assert job.is_dir()
    assert (job / local_jobs.KEEP_MARKER).is_file()
    assert (job / local_jobs.TERMINAL_MARKER).is_file()
    assert client.stopped == [aid]


def test_bundle_422_retries_completed_result_without_optional_bundle(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    sessions = trial_dir / "agent" / "sessions"
    _write_codex_session(sessions / "root.jsonl", "root-1", "user", 100)
    _write_codex_session(
        sessions / "child.jsonl", "child-1", "subagent", 50,
        parent="root-1",
    )

    class BundleFallbackClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            self.calls.append(trajectory_bundle is not None)
            if trajectory_bundle is not None:
                raise ApiError(
                    "server returned 422: trajectory_bundle.json is not valid JSON",
                    status_code=422,
                )
            return {"submission_id": "s1", "grade_status": "pending"}

    client = BundleFallbackClient(lambda _aid: None)
    outcome = runloop._upload_trial(client, _entry(trial_dir))
    assert outcome == "submitted"
    assert client.calls == [True, False]
    assert pending.load(tmp_path) == []
    assert client.stopped == []


def test_bundle_rejection_then_required_fallback_is_terminal(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    sessions = trial_dir / "agent" / "sessions"
    _write_codex_session(sessions / "root.jsonl", "root-1", "user", 100)

    class IncompatibleServerClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            self.calls.append(trajectory_bundle is not None)
            if trajectory_bundle is not None:
                raise ApiError(
                    "server returned 422: trajectory_bundle.json is incomplete",
                    status_code=422,
                )
            raise ApiError(
                "server returned 422: complete trajectory_bundle.json is required",
                status_code=422,
            )

    client = IncompatibleServerClient(lambda _aid: None)
    assert runloop._upload_trial(client, _entry(trial_dir)) == "rejected"
    assert client.calls == [True, False]
    assert pending.load(tmp_path) == []
    assert client.stopped == ["a1"]
    assert trial_dir.is_dir()


def test_persisted_bundle_omission_required_by_server_is_terminal(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    entry = _entry(trial_dir)
    entry["omit_trajectory_bundle"] = True

    class StrictServerClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            self.calls.append(trajectory_bundle is not None)
            assert trajectory_bundle is None
            raise ApiError(
                "server returned 422: complete trajectory_bundle.json is required",
                status_code=422,
            )

    client = StrictServerClient(lambda _aid: None)
    assert runloop._upload_trial(client, entry) == "rejected"
    assert client.calls == [False]
    assert pending.load(tmp_path) == []
    assert client.stopped == ["a1"]


def test_oversized_projected_request_omits_bundle_before_submit(
    tmp_path: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    # Keep the test fixture tiny while exercising the production size-budget
    # branch: the patch + framing fit, but adding the bundle does not.
    monkeypatch.setattr(
            runloop, "_UPLOAD_BODY_BUDGET_BYTES",
            runloop._MULTIPART_OVERHEAD_BUDGET_BYTES + 2_000,
    )
    trial_dir = _make_trial_dir(tmp_path)
    sessions = trial_dir / "agent" / "sessions"
    _write_codex_session(sessions / "root.jsonl", "root-1", "user", 100)

    class CaptureClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            self.calls.append(trajectory_bundle is not None)
            assert trajectory_bundle is None
            return {"submission_id": "s1", "grade_status": "pending"}

    client = CaptureClient(lambda _aid: None)
    assert runloop._upload_trial(client, _entry(trial_dir)) == "submitted"
    assert client.calls == [False]
    assert pending.load(tmp_path) == []
    assert "omitting the optional trajectory bundle" in capsys.readouterr().out


def test_required_upload_body_is_capped_even_without_bundle(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(
        runloop, "_UPLOAD_BODY_BUDGET_BYTES",
        runloop._MULTIPART_OVERHEAD_BUDGET_BYTES + 1,
    )
    trial_dir = _make_trial_dir(tmp_path)
    client = FakeClient(lambda _aid: pytest.fail("oversized body must not submit"))
    assert runloop._upload_trial(client, _entry(trial_dir)) == "upload-failed"
    assert client.calls == []
    assert len(pending.load(tmp_path)) == 1


def test_bundle_edge_413_retries_once_without_optional_bundle(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    sessions = trial_dir / "agent" / "sessions"
    _write_codex_session(sessions / "root.jsonl", "root-1", "user", 100)

    class EdgeFallbackClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            self.calls.append(trajectory_bundle is not None)
            if trajectory_bundle is not None:
                raise ApiError(
                    "server returned 413: Payload Too Large (cloudflare)",
                    status_code=413,
                )
            return {"submission_id": "s1", "grade_status": "pending"}

    client = EdgeFallbackClient(lambda _aid: None)
    assert runloop._upload_trial(client, _entry(trial_dir)) == "submitted"
    assert client.calls == [True, False]
    assert pending.load(tmp_path) == []
    assert client.stopped == []


def test_bundle_edge_413_is_terminal_if_reduced_request_is_still_too_large(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    sessions = trial_dir / "agent" / "sessions"
    _write_codex_session(sessions / "root.jsonl", "root-1", "user", 100)

    class AlwaysOversizedClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            self.calls.append(trajectory_bundle is not None)
            raise ApiError("server returned 413: too large", status_code=413)

    client = AlwaysOversizedClient(lambda _aid: None)
    assert runloop._upload_trial(client, _entry(trial_dir)) == "rejected"
    assert client.calls == [True, False]
    assert pending.load(tmp_path) == []
    assert client.stopped == ["a1"]


def test_bundle_422_persists_downgrade_when_fallback_transport_fails(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    sessions = trial_dir / "agent" / "sessions"
    _write_codex_session(sessions / "root.jsonl", "root-1", "user", 100)
    _write_codex_session(
        sessions / "child.jsonl", "child-1", "subagent", 50,
        parent="root-1",
    )

    class FailingFallbackClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            self.calls.append(trajectory_bundle is not None)
            if trajectory_bundle is not None:
                raise ApiError(
                    "server returned 422: trajectory_bundle.json is not valid JSON",
                    status_code=422,
                )
            raise ApiError("server returned 503: retry", status_code=503)

    first = FailingFallbackClient(lambda _aid: None)
    assert runloop._upload_trial(first, _entry(trial_dir)) == "upload-failed"
    assert first.calls == [True, False]
    retry_entry = pending.load(tmp_path)[0]
    assert retry_entry["omit_trajectory_bundle"] is True

    class RecoveryClient(FakeClient):
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome="completed", resume_generation=None,
                   trajectory_bundle=None):
            self.calls.append(trajectory_bundle is not None)
            assert trajectory_bundle is None
            return {"submission_id": "s1", "grade_status": "pending"}

    second = RecoveryClient(lambda _aid: None)
    assert runloop._upload_trial(second, retry_entry) == "submitted"
    assert second.calls == [False]
    assert pending.load(tmp_path) == []


def test_unknown_422_keeps_completed_work_for_retry(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    client = FakeClient(_raise(422))
    assert runloop._upload_trial(client, _entry(trial_dir)) == "upload-failed"
    assert [e["assignment_id"] for e in pending.load(tmp_path)] == ["a1"]
    assert client.stopped == []


def test_transient_5xx_keeps_ledger_entry(tmp_path: Path, monkeypatch):
    """Negative control: a 503 is retryable and must stay queued."""
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    client = FakeClient(_raise(503))
    outcome = runloop._upload_trial(client, _entry(trial_dir))
    assert outcome == "upload-failed"
    assert [e["assignment_id"] for e in pending.load(tmp_path)] == ["a1"]
    assert client.stopped == []


def test_403_stays_retryable_by_policy(tmp_path: Path, monkeypatch):
    """403 covers both a permanent nonce mismatch and a suspension that may
    be lifted — dropping a suspended volunteer's completed trial would
    destroy recoverable work, so it stays in the queue."""
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    outcome = runloop._upload_trial(FakeClient(_raise(403)), _entry(trial_dir))
    assert outcome == "upload-failed"
    assert [e["assignment_id"] for e in pending.load(tmp_path)] == ["a1"]
