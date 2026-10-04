import hashlib
import json
import pytest
from dradar.v2.results import Completion, save_completion, upload_files
from dradar.v2.artifacts import ArtifactError
from dradar.v2.protocol import result_hash, result_receipt
from dradar.v2.client import ProtocolError

A = {"assignment_id": "a", "device_id": "d", "lease_id": "l", "owner_epoch": 1}

def test_separate_scrubbed_copy_and_canonical_result_hash(tmp_path):
    patch = tmp_path / "patch"
    patch.write_bytes(b"")
    trajectory = tmp_path / "trajectory"
    trajectory.write_text(json.dumps({"api_key": "sk-" + "x" * 40}))
    value = Completion("completed", True, {"patch": patch, "trajectory": trajectory}, elapsed_ms=0)
    payload = save_completion(tmp_path / "artifacts", A, "e", value)
    assert "sk-" in (tmp_path / "artifacts/raw/a/trajectory").read_text()
    assert "sk-" not in (tmp_path / "artifacts/upload/a/trajectory").read_text()
    assert payload["elapsed_ms"] == 0 and payload["tokens"]["total"] is None
    keys = ("execution_id", "outcome", "exit_confirmed", "completed_at", "elapsed_ms", "tokens", "failure", "artifacts")
    canonical = json.dumps({k: payload[k] for k in keys}, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    assert payload["result_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert set(upload_files(tmp_path / "artifacts", "a", payload)) == {"patch", "trajectory"}

def test_unknown_exit_preserves_evidence_and_does_not_create_upload(tmp_path):
    patch = tmp_path / "patch"
    patch.write_bytes(b"")
    with pytest.raises(ArtifactError):
        save_completion(tmp_path / "artifacts", A, "e", Completion("completed", False, {"patch": patch}))
    assert (tmp_path / "artifacts/raw/a/patch").exists()
    assert not (tmp_path / "artifacts/upload/a").exists()

def test_failed_result_can_have_no_files(tmp_path):
    payload = save_completion(tmp_path / "artifacts", A, "e", Completion("failed", True, failure={"code": "test_failure", "message": "synthetic"}))
    assert payload["artifacts"] == []
    assert upload_files(tmp_path / "artifacts", "a", payload) == {}

def test_native_trajectory_array_is_preserved_without_legacy_wrapper(tmp_path):
    path = tmp_path / 'events.json'
    events = [{'method': 'turn/completed', 'threadId': 'real-thread', 'turnId': 'real-turn'}]
    path.write_text(json.dumps(events))
    payload = save_completion(tmp_path / 'artifacts', A, 'e', Completion('failed', True, {'trajectory': path}))
    assert json.loads(upload_files(tmp_path / 'artifacts', 'a', payload)['trajectory'].read_bytes()) == events

@pytest.mark.parametrize('name,raw',[('runner_result','[]'),('trajectory','[1]'),('trajectory','[{"usage":NaN}]')])
def test_bad_array_or_nonfinite_artifact_is_quarantined(tmp_path,name,raw):
    path=tmp_path/'display.json';path.write_text(raw)
    with pytest.raises(ArtifactError):save_completion(tmp_path/'artifacts',A,'e',Completion('failed',True,{name:path}))
    assert (tmp_path/'artifacts/raw/a'/name).read_text()==raw
    assert not (tmp_path/'artifacts/upload/a').exists()

def test_receipt_requires_exact_hash_identity_and_submission():
    payload = {"execution_id": "e", "result_sha256": "proof"}
    reply = {"schema_version": 2, "server_time": "now", "request_id": "r", "status": "submitted", "assignment_id": "a", "execution_id": "e", "submission_id": "s", "result_sha256": "proof", "grading_state": "queued"}
    assert result_receipt(reply, "r", "a", payload) == reply
    for key in ("request_id", "assignment_id", "execution_id", "result_sha256", "submission_id"):
        changed = {**reply, key: ""}
        with pytest.raises(ProtocolError):
            result_receipt(changed, "r", "a", payload)


def test_metadata_rename_before_journal_commit_is_recoverable(tmp_path):
    from dradar.v2.results import recover_completion
    patch = tmp_path / "patch"
    patch.write_bytes(b"")
    payload = save_completion(tmp_path / "artifacts", A, "e", Completion("completed", True, {"patch": patch}))
    assert recover_completion(tmp_path / "artifacts", A, "e") == payload
    with pytest.raises(ArtifactError):
        recover_completion(tmp_path / "artifacts", A, "different")


def test_result_hash_matches_frozen_v02_fixture():
    from pathlib import Path
    examples = json.loads((Path(__file__).parent / "fixtures/v2_contract_examples.json").read_text())
    meta = examples["requests"]["result_metadata"]
    assert result_hash(meta) == meta["result_sha256"]
