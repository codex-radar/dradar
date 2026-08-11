"""Upload resilience: a trial that ran but failed to upload must survive on
disk and be retryable without re-running, via a local pending-upload ledger.
"""

import json
from pathlib import Path

import pytest

from dradar import checkpoints, pending, runloop
from dradar.api_client import ApiError


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

    def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
               outcome="completed", resume_generation=None):
        self.calls.append(assignment_id)
        return self.behavior(assignment_id)

    def checkpoint_discard(self, assignment_id, checkpoint_id,
                           resume_generation, reason):
        self.discarded = (assignment_id, checkpoint_id, resume_generation, reason)

    def mark_stopped(self, assignment_id):
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
         "job_dir": None, "keep": True}
    e.update(overrides)
    return e


def test_upload_success_clears_ledger(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})
    client = FakeClient(lambda aid: {"submission_id": "s1", "grade_status": "pending"})
    outcome = runloop._upload_trial(client, _entry(trial_dir, meta={"k": "v"}))
    assert outcome == "submitted"
    assert pending.load(tmp_path) == []


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
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})

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
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})

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
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})

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
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})
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
    assert (job / checkpoints.KEEP_MARKER).is_file()
    assert (job / checkpoints.TERMINAL_MARKER).is_file()
    assert checkpoints.find_latest(tmp_path, aid) is None
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
        runloop._MULTIPART_OVERHEAD_BUDGET_BYTES + 200,
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
