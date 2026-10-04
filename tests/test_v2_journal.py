from concurrent.futures import ThreadPoolExecutor
import json
import os
import pytest
from dradar.v2.journal import Journal, JournalConflict

def test_request_survives_restart_and_exact_replay(tmp_path):
    first = Journal(tmp_path).prepare("claim:1", "/api/v2/runs/run1/claim", {"device_id": "d", "slot_id": "1"})
    second = Journal(tmp_path).prepare("claim:1", "/api/v2/runs/run1/claim", {"slot_id": "1", "device_id": "d"})
    assert first.request_id == second.request_id
    assert first.body_json == second.body_json
    assert second.response is None

def test_changed_body_or_scope_cannot_get_new_request(tmp_path):
    journal = Journal(tmp_path)
    journal.prepare("start", "/api/v2/assignments/a/start", {"device_id": "d", "execution_id": "e"})
    with pytest.raises(JournalConflict):
        journal.prepare("start", "/api/v2/assignments/a/start", {"device_id": "other", "execution_id": "e"})
    with pytest.raises(JournalConflict):
        journal.prepare("start", "/api/v2/assignments/b/start", {"device_id": "d", "execution_id": "e"})

def test_receipt_saved_and_conflict_protected(tmp_path):
    journal = Journal(tmp_path)
    req = journal.prepare("start", "/api/v2/assignments/a/start", {})
    journal.acknowledge(req, {"accepted": True})
    saved = Journal(tmp_path).requests()[0]
    assert saved.response == {"accepted": True}
    journal.acknowledge(req, {"accepted": True})
    with pytest.raises(JournalConflict):
        journal.acknowledge(req, {"accepted": False})

def test_local_duplicate_callers_start_at_most_once(tmp_path):
    journal = Journal(tmp_path)
    with ThreadPoolExecutor(8) as pool:
        started = list(pool.map(lambda _: journal.begin_execution("a", "e"), range(8)))
    assert sum(started) == 1
    assert not Journal(tmp_path).begin_execution("a", "e")

def test_crash_after_fence_is_unknown_and_never_reruns(tmp_path):
    journal = Journal(tmp_path)
    assert journal.begin_execution("a", "e")
    recovered = Journal(tmp_path)
    assert recovered.execution("a")["state"] == "launch_fenced"
    assert not recovered.begin_execution("a", "e")
    with pytest.raises(JournalConflict):
        recovered.begin_execution("a", "new-execution")

def test_completed_result_survives_and_cannot_be_replaced(tmp_path):
    journal = Journal(tmp_path)
    journal.begin_execution("a", "e")
    result = {"artifacts": [{"path": "retained.patch", "sha256": "proof"}], "execution": "completed"}
    journal.save_result("a", "e", result)
    recovered = Journal(tmp_path)
    assert json.loads(recovered.execution("a")["result_json"]) == result
    assert not recovered.begin_execution("a", "e")
    recovered.save_result("a", "e", result)
    with pytest.raises(JournalConflict):
        recovered.save_result("a", "e", {"artifacts": []})

def test_unfenced_result_is_rejected(tmp_path):
    with pytest.raises(JournalConflict):
        Journal(tmp_path).save_result("a", "e", {})

def test_identities_are_stable_and_separate(tmp_path):
    j = Journal(tmp_path)
    assert j.identity("device") == Journal(tmp_path).identity("device")
    assert j.identity("run") != j.identity("device")

def test_private_file_and_symlink_rejection(tmp_path):
    Journal(tmp_path / "state")
    assert os.stat(tmp_path / "state/journal.sqlite3").st_mode & 0o777 == 0o600
    (tmp_path / "link").symlink_to(tmp_path / "state", target_is_directory=True)
    with pytest.raises(JournalConflict):
        Journal(tmp_path / "link")

@pytest.mark.parametrize("new", [True, 1.0])
def test_exact_json_type_change_is_rejected(tmp_path, new):
    journal = Journal(tmp_path)
    journal.prepare("claim", "/api/v2/runs/r/claim", {"slot_id": 1})
    with pytest.raises(JournalConflict):
        journal.prepare("claim", "/api/v2/runs/r/claim", {"slot_id": new})


def test_receipt_replay_observations_do_not_replace_original_authorization(tmp_path):
    j=Journal(tmp_path)
    req=j.prepare("start", "/api/v2/assignments/a/start", {})
    first={"status":"started","server_time":"original","replayed":False,"assignment":{"assignment_id":"a","execution_id":"e","state":"running"}}
    j.acknowledge(req,first)
    j.acknowledge(req,{**first,"server_time":"later","replayed":True,"assignment":{**first["assignment"],"state":"submitted"}})
    assert j.requests()[0].response==first
    with pytest.raises(JournalConflict):j.acknowledge(req,{**first,"assignment":{**first["assignment"],"execution_id":"foreign"}})
