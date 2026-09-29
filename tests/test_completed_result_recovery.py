"""A cleanup fence with a real result remains protected during inspection."""

import hashlib
import json
import sys

import pytest

from dradar import assignment_boundary, cleanup_recovery, completed_result_recovery, pending, runloop
from dradar.api_client import ApiError
from dradar.ota import recovery
from dradar.ota.activity import register_invocation
from dradar.ota.state import UpdateLock
from test_cleanup_recovery import AID, case  # noqa: F401 - shared physical-exit fixture
from test_recover_upload import _signed_package


@pytest.fixture(autouse=True)
def _verified_source_for_inventory_tests(monkeypatch, request):
    if request.node.name == "test_original_zcode_meta_requires_observed_agent_and_signed_version":
        return
    # The shared physical-exit fixture uses a non-ZCode assignment.  These
    # cases exercise its artifact and quarantine boundary independently.
    monkeypatch.setattr(completed_result_recovery, "_original_meta",
                        lambda *_args: {"dradar_version": "0.5.281",
                                       "zcode_cli_version": "0.16.5"})


def _complete_trial(job):
    trial = job / "task__abc12345"
    artifacts = trial / "artifacts"
    artifacts.mkdir()
    (artifacts / "model.patch").write_bytes(b"diff --git a/x b/x\n+done\n")
    agent = trial / "agent"
    agent.mkdir()
    (agent / "trajectory.json").write_text('{"events":[]}')
    (trial / "result.json").write_text(json.dumps({
        "task_id": "task", "finished_at": "2026-09-29T16:40:00Z",
        "agent_execution": {"finished_at": "2026-09-29T16:39:00Z"},
        "exception_info": None, "agent_result": {"n_agent_steps": 4},
    }))
    return trial


def test_exact_completed_result_preflight_is_read_only(case):
    server, job, boundary, common = case
    trial = _complete_trial(job)
    before = (common["home"] / "pending_uploads.json").read_bytes()
    with pytest.raises(cleanup_recovery.CleanupRecoveryBlocked,
                       match="possible local result"):
        cleanup_recovery.inspect(**common)
    proof = completed_result_recovery.inspect(**common)
    assert proof["status"] == "ready"
    assert proof["result_status"] == "completed_local_result"
    assert proof["artifact_bytes"]["patch"] > 0
    assert proof["artifact_sha256"]["patch"] == hashlib.sha256(
        (trial / "artifacts" / "model.patch").read_bytes()).hexdigest()
    assert (common["home"] / "pending_uploads.json").read_bytes() == before
    assert pending.load(common["home"])[0]["upload_blocked"] == "cleanup_unconfirmed"
    assert boundary.is_file() and server.disposition is None


@pytest.mark.parametrize("change", ["other_task", "unfinished", "exception", "empty_patch", "link"])
def test_incomplete_or_changed_trial_keeps_quarantine(case, change):
    server, job, _boundary, common = case
    trial = _complete_trial(job)
    result_path = trial / "result.json"
    result = json.loads(result_path.read_text())
    if change == "other_task":
        result["task_id"] = "another-task"
    elif change == "unfinished":
        result["agent_execution"]["finished_at"] = None
    elif change == "exception":
        result["exception_info"] = {"exception_type": "AgentError"}
    elif change == "empty_patch":
        (trial / "artifacts" / "model.patch").write_bytes(b"")
    elif change == "link":
        patch = trial / "artifacts" / "model.patch"
        patch.unlink()
        patch.symlink_to(result_path)
    if change in {"other_task", "unfinished", "exception"}:
        result_path.write_text(json.dumps(result))
    before = (common["home"] / "pending_uploads.json").read_bytes()
    with pytest.raises((completed_result_recovery.CompletedResultRecoveryBlocked,
                        cleanup_recovery.CleanupRecoveryBlocked)):
        completed_result_recovery.inspect(**common)
    assert (common["home"] / "pending_uploads.json").read_bytes() == before
    assert server.disposition is None


def test_wrong_session_never_inspects_result_as_uploadable(case):
    server, job, _boundary, common = case
    _complete_trial(job)
    with pytest.raises(cleanup_recovery.CleanupRecoveryBlocked):
        completed_result_recovery.inspect(**{**common, "session_id": "e" * 32})
    assert pending.load(common["home"])[0]["assignment_id"] == AID
    assert server.disposition is None


def test_original_zcode_meta_requires_observed_agent_and_signed_version(tmp_path, monkeypatch):
    trial = tmp_path / "trial"
    trial.mkdir()
    result_path = trial / "result.json"
    result = {
        "agent_info": {"name": "zcode", "version": "0.16.5"},
        "config": {"agent": {
            "import_path": "_dradar_pier_zcode:ZCodeBigModel",
            "model_name": "glm-5.3",
            "kwargs": {"reasoning_effort": "high", "version": "0.16.5",
                       "api_key_file": "present-in-container"},
        }},
        "agent_result": {"n_agent_steps": 5},
    }
    result_path.write_text(json.dumps(result))
    monkeypatch.setattr(assignment_boundary, "inspect_snapshot", lambda _path: (
        {"expected": {AID: {"model": "glm-5.3", "effort": "high"}}}, "digest"))
    monkeypatch.setattr(completed_result_recovery, "_source_version",
                        lambda _home: "0.5.281")
    meta = completed_result_recovery._original_meta(
        tmp_path, "deep-swe", "b" * 32, AID, trial)
    assert meta["dradar_version"] == "0.5.281"
    assert meta["zcode_cli_version"] == "0.16.5"
    assert meta["model_config_version"].endswith("full-container-v3")
    assert meta["cost_usd"] is None
    result["config"]["agent"]["kwargs"]["reasoning_effort"] = "low"
    result_path.write_text(json.dumps(result))
    with pytest.raises(completed_result_recovery.CompletedResultRecoveryBlocked):
        completed_result_recovery._original_meta(
            tmp_path, "deep-swe", "b" * 32, AID, trial)


def test_cleanup_result_intent_is_exact_and_replayable():
    class Client:
        def __init__(self):
            self.payloads = []

        def register_completed_cleanup_result_intent(self, payload):
            self.payloads.append(payload)
            return {"schema_version": 1, "ok": True,
                    "replayed": len(self.payloads) > 1,
                    "request_id": payload["request_id"],
                    "assignment_id": payload["assignment_id"],
                    "batch_id": payload["batch_id"],
                    "session_id": payload["source_session_id"],
                    "owner_epoch": payload["source_owner_epoch"],
                    "upload_intent_id": payload["upload_intent_id"],
                    "source_client_version": "0.5.281",
                    "source_agent_version": "0.16.5"}

    intent = "f" * 64
    entry = {"record_kind": "cleanup_quarantine", "upload_blocked": "cleanup_unconfirmed",
             "assignment_id": AID, "batch_id": "b" * 32,
             "nonce": "n" * 32, "runner_session_id": "s" * 32,
             "owner_epoch": 1, "upload_intent": {"id": intent},
             "completed_result_recovery": {
                 "request_id": "r" * 32, "release_evidence_id": "e" * 32,
                 "release_evidence_sha256": "a" * 64,
                 "source_client_version": "0.5.281",
                 "source_agent_version": "0.16.5",
                 "mode": "source"}}
    client = Client()
    assert runloop._register_cleanup_result_intent(client, entry, intent) == intent
    assert runloop._register_cleanup_result_intent(client, entry, intent) == intent
    assert client.payloads[0] == client.payloads[1]
    assert client.payloads[0]["source_owner_epoch"] == 1
    entry["record_kind"] = None
    entry["upload_blocked"] = None
    with pytest.raises(ValueError):
        runloop._register_cleanup_result_intent(client, entry, intent)
    assert len(client.payloads) == 2


def test_verified_salvage_binds_exact_synthetic_owner_and_intent():
    class Client:
        def __init__(self):
            self.payloads = []

        def register_completed_cleanup_result_salvage(self, payload):
            self.payloads.append(payload)
            return {"ok": True, "replayed": len(self.payloads) > 1,
                    "assignment_id": payload["assignment_id"],
                    "session_id": payload["salvage_session_id"],
                    "owner_epoch": 3,
                    "upload_intent_id": payload["upload_intent_id"],
                    "source_client_version": "0.5.281",
                    "source_agent_version": "0.16.5"}

    intent = "f" * 64
    entry = {"record_kind": "cleanup_quarantine", "upload_blocked": "cleanup_unconfirmed",
             "assignment_id": AID, "batch_id": "b" * 32, "nonce": "n" * 32,
             "runner_session_id": "s" * 32, "owner_epoch": 1,
             "upload_intent": {"id": intent},
             "completed_result_recovery": {
                 "mode": "salvage", "expected_owner_epoch": 2,
                 "upload_session_id": "salvage-" + "c" * 32,
                 "upload_owner_epoch": 3,
                 "release_evidence_id": "e" * 32,
                 "release_evidence_sha256": "a" * 64,
                 "source_client_version": "0.5.281",
                 "source_agent_version": "0.16.5"}}
    client = Client()
    assert runloop._register_cleanup_result_intent(client, entry, intent) == intent
    assert runloop._register_cleanup_result_intent(client, entry, intent) == intent
    assert client.payloads[0] == client.payloads[1]
    assert client.payloads[0]["expected_owner_epoch"] == 2
    assert client.payloads[0]["source_owner_epoch"] == 1
    assert client.payloads[0]["upload_intent_id"] == intent


def test_explicit_flag_without_saved_binding_cannot_submit(tmp_path, monkeypatch):
    class Client:
        def submit(self, *_args, **_kwargs):
            pytest.fail("unfenced submission must not be reached")

    monkeypatch.setattr(runloop, "HOME", tmp_path)
    row = {"record_kind": "cleanup_quarantine", "upload_blocked": "cleanup_unconfirmed",
           "assignment_id": AID, "task_id": "task"}
    assert runloop._upload_trial(Client(), row,
                                 cleanup_result_recovery=True) == "upload-blocked"
    assert pending.load(tmp_path)[0]["record_kind"] == "cleanup_quarantine"


@pytest.mark.parametrize("intent_missing", [False, True])
def test_explicit_recovery_upload_uses_original_bytes_and_keeps_no_rerun(
    case, monkeypatch, intent_missing,
):
    server, job, _boundary, common = case
    trial = _complete_trial(job)
    home = common["home"]
    original = pending.load(home)[0]
    pending.replace_exact(home, original, {**original, "nonce": "n" * 32})
    proof = completed_result_recovery.inspect(**common)
    server.get_assignment = lambda: {"active": [{
        "assignment_id": AID, "batch_id": common["batch_id"],
        "nonce": pending.load(home)[0]["nonce"], "task_id": "task",
        "model": "glm-5.3", "effort": "high", "owner_epoch": 3,
        "execution_state": "waiting", "runner_state": "waiting",
        "started_at": None,
    }]}
    sent = []

    def register(payload):
        sent.append(("intent", payload.copy()))
        if intent_missing:
            raise ApiError("recovery endpoint unavailable", status_code=404)
        return {"ok": True, "replayed": len([k for k, _ in sent if k == "intent"]) > 1,
                "assignment_id": AID,
                "session_id": payload["salvage_session_id"],
                "owner_epoch": 4,
                "upload_intent_id": payload["upload_intent_id"],
                "source_client_version": "0.5.281",
                "source_agent_version": "0.16.5"}

    def submit(aid, nonce, patch, trajectory, result, meta, **kw):
        sent.append(("submit", {"aid": aid, "patch": patch.read_bytes(),
                                "trajectory": trajectory.read_bytes(),
                                "result": result.read_bytes(),
                                "meta": meta, **kw}))
        if len([k for k, _ in sent if k == "submit"]) == 1:
            raise ApiError("temporary upload error")
        return {"submission_id": "sub-test", "grade_status": "pending"}

    server.register_completed_cleanup_result_salvage = register
    server.submit = submit
    monkeypatch.setattr(completed_result_recovery, "_client", lambda _cfg: server)
    monkeypatch.setattr(runloop, "HOME", home)
    first = completed_result_recovery.execute(
        **common, inventory_sha256=proof["inventory_sha256"])
    assert first["status"] == "upload-failed"
    assert pending.load(home)[0]["record_kind"] == "cleanup_quarantine"
    if intent_missing:
        assert [name for name, _ in sent] == ["intent"]
        assert trial.is_dir()
        return
    refreshed = completed_result_recovery.inspect(**common)
    done = completed_result_recovery.execute(
        **common, inventory_sha256=refreshed["inventory_sha256"])
    assert done["status"] == "submitted"
    assert [name for name, _ in sent] == ["intent", "submit", "intent", "submit"]
    assert sent[0][1] == sent[2][1]
    for submission in (sent[1][1], sent[3][1]):
        assert submission["aid"] == AID
        assert submission["patch"] == (trial / "artifacts" / "model.patch").read_bytes()
        assert submission["session_id"] == sent[0][1]["salvage_session_id"]
        assert submission["owner_epoch"] == 4
        assert submission["upload_intent_id"] == sent[0][1]["upload_intent_id"]
    assert pending.load(home) == []
    assert trial.is_dir()


@pytest.mark.parametrize("changed", ["patch", "trajectory", "result"])
def test_changed_bytes_after_preflight_never_reach_server(case, monkeypatch, changed):
    server, job, _boundary, common = case
    trial = _complete_trial(job)
    home = common["home"]
    original = pending.load(home)[0]
    pending.replace_exact(home, original, {**original, "nonce": "n" * 32})
    proof = completed_result_recovery.inspect(**common)
    server.get_assignment = lambda: {"active": [{
        "assignment_id": AID, "batch_id": common["batch_id"],
        "nonce": "n" * 32, "task_id": "task",
        "model": "glm-5.3", "effort": "high", "owner_epoch": 3,
        "execution_state": "waiting", "runner_state": "waiting",
        "started_at": None,
    }]}
    touched = []
    server.register_completed_cleanup_result_salvage = lambda _payload: touched.append("intent")
    server.submit = lambda *_a, **_k: touched.append("submit")
    monkeypatch.setattr(completed_result_recovery, "_client", lambda _cfg: server)
    monkeypatch.setattr(runloop, "HOME", home)
    target = {
        "patch": trial / "artifacts" / "model.patch",
        "trajectory": trial / "agent" / "trajectory.json",
        "result": trial / "result.json",
    }[changed]
    replace = pending.replace_exact

    def replace_then_change(path, before, after):
        replace(path, before, after)
        if "completed_result_recovery" in after:
            target.write_bytes(target.read_bytes() + b"\n")

    monkeypatch.setattr(pending, "replace_exact", replace_then_change)
    outcome = completed_result_recovery.execute(
        **common, inventory_sha256=proof["inventory_sha256"])
    assert outcome["status"] == "upload-blocked"
    assert touched == []
    assert pending.load(home)[0]["record_kind"] == "cleanup_quarantine"


def test_signed_result_entry_verifies_package_before_preflight(tmp_path, monkeypatch):
    calls = []
    home, manifest, package, _ = _signed_package(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "HOME", home)
    monkeypatch.setattr(sys, "argv", [str(package), "recover-result"])
    monkeypatch.setattr(completed_result_recovery, "cmd_recover",
                        lambda _args: calls.append("preflight") or 0)
    args = ["--manifest", str(manifest),
            "--assignment-id", AID, "--benchmark", "deep-swe",
            "--batch-id", "b" * 32, "--runner-session-id", "a" * 32]
    assert recovery.main_result(args) == 0
    assert calls == ["preflight"]
    package.write_bytes(package.read_bytes() + b"tampered")
    assert recovery.main_result(args) == 2
    assert calls == ["preflight"]
    with UpdateLock(home / "ota" / "launch.lock"):
        active = register_invocation(home / "ota")
        active.__enter__()
    try:
        assert recovery.main_result(args) == 2
    finally:
        active.__exit__(None, None, None)
    assert calls == ["preflight"]
