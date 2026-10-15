"""Synthetic only: real runtime, Journal, persistence and upload replay logic."""
from contextlib import nullcontext
import copy
import hashlib
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from dradar.v2 import host_collection as collection_io
from dradar.v2 import host_runtime as host
from dradar.v2.journal import Journal
from dradar.v2.results import save_completion
from dradar.single.job import Job

THREAD = "11111111-1111-4111-8111-111111111111"
TURN = "22222222-2222-4222-8222-222222222222"
CONTAINER = "c" * 64
PATCH = (
    b"diff --git a/answer.txt b/answer.txt\n"
    b"--- a/answer.txt\n"
    b"+++ b/answer.txt\n"
    b"@@ -1 +1 @@\n"
    b"-old\n"
    b"+new\n"
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def make_case(tmp_path, monkeypatch, *, lost=True, fault=None,
              terminal="completed"):
    def forbidden(*args, **kwargs):
        raise AssertionError("real network/process invocation forbidden")
    monkeypatch.setattr(socket, "socket", forbidden)
    original_popen = subprocess.Popen
    def bounded_popen(args, *positional, **kwargs):
        if args != ["git", "apply", "--numstat", "-"]:
            return forbidden(args, *positional, **kwargs)
        return original_popen(args, *positional, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", bounded_popen)

    root = tmp_path.resolve()
    folder = root / "host"
    folder.mkdir(mode=0o700)
    task = root / "task"
    task.mkdir()
    (task / "instruction.md").write_text("synthetic only\n")
    journal = Journal(root / "journal")
    journal.bind("run", "run1")
    eid = journal.identity("execution:a1")
    cfg = {
        "assignment_id": "a1", "execution_id": eid,
        "benchmark": "synthetic", "task_id": "task",
        "task_content_hash": "a" * 64, "collector_sha256": "b" * 64,
        "deliverable_names": ["answer.txt"], "collection_nonce": "9" * 32,
        "model": host.MODEL, "effort": "low", "evidence_dir": str(folder),
    }
    result = {
        "public_inputs_unchanged": True, "missing_deliverables": [],
        "public_inputs_sha256": {"/app/instruction.md": sha(b"synthetic only\n")},
        "outputs": {"answer.txt": {"sha256": sha(b"new\n"), "bytes": 4}},
        "unexpected_workspace_changes": [],
        "patch_sha256": sha(PATCH), "patch_bytes": len(PATCH),
        "task": "task", "container_id": CONTAINER,
        "model_turn_status": terminal, "model_turn_error": None,
        "model": host.MODEL, "reasoning": "low", "agent_timeout_sec": 1,
        "official_task_timeout_sec": 1, "verifier_timeout_sec": 1,
        "grading_done": False,
    }
    case = SimpleNamespace(
        root=root, folder=folder, journal=journal, eid=eid, cfg=cfg,
        result=result, lost=lost, fault=fault, terminal=terminal,
        error=TimeoutError("synthetic original collect ACK lost"),
        proof_key=None, proof_value=None, close_error=None,
    )

    class Controller:
        def __init__(self):
            self.calls = []
            self.event_reads = 0
            self.closed = False
            self.report = {
                "task": "task", "task_container_id": CONTAINER,
                "model_thread_id": THREAD, "model_turn_id": TURN,
                "model_terminal_thread_id": THREAD,
                "model_terminal_turn_id": TURN,
                "model_turn_start_ack": True, "model_calls": 1,
                "last_turn_status": terminal, "last_turn_error": None,
                **{key: True for key in collection_io.PROOFS},
            }

        def request(self, value):
            self.calls.append(copy.deepcopy(value))
            if value["op"] == "authorize-one-turn":
                return {"single_turn_latched": True}
            if value["op"] == "rpc":
                assert value["method"] == "turn/start"
                return {"result": {"turn": {"id": TURN, "status": "inProgress"}}}
            if value["op"] == "status":
                return {"last_turn_status": terminal}
            if value["op"] == "events":
                self.event_reads += 1
                if self.event_reads > 1:
                    return {"events": []}
                events = [{
                    "method": "thread/tokenUsage/updated",
                    "params": {"threadId": THREAD, "turnId": TURN,
                               "tokenUsage": {"total": {
                                   "inputTokens": 2, "outputTokens": 3,
                                   "totalTokens": 5}}},
                }]
                if fault != "missing_terminal_event":
                    events.append({
                        "method": "turn/completed", "threadId": THREAD,
                        "turnId": TURN, "status": terminal, "error_code": None,
                    })
                return {"events": events}
            assert value == {"op": "collect"}
            assert sum(item["op"] == "collect" for item in self.calls) == 1
            digest = collection_io.persist_collection(
                folder, cfg, self.report, value, result, PATCH,
                {"answer.txt": b"new\n"})
            self.report.update(
                collected_manifest=str(folder / "collected" / "COLLECTION.json"),
                collection_receipt_sha256=digest)
            receipt_path = folder / collection_io.RECEIPT
            patch_path = folder / "collected" / "model.patch"
            if fault == "missing":
                receipt_path.unlink()
            elif fault == "corrupt":
                receipt_path.write_bytes(b"{")
            elif fault == "receipt_digest":
                self.report["collection_receipt_sha256"] = "0" * 64
            elif fault == "patch_hash":
                patch_path.write_bytes(PATCH + b"corruption\n")
            elif fault == "manifest_hash":
                path = folder / "collected" / "COLLECTION.json"
                path.write_bytes(path.read_bytes() + b"\n")
            elif fault == "missing_deliverable":
                (folder / "collected" / "answer.txt").unlink()
            elif fault in {"patch_symlink", "receipt_symlink"}:
                path = patch_path if fault == "patch_symlink" else receipt_path
                original = folder / ("retained-" + path.name)
                path.rename(original)
                path.symlink_to(original)
            elif fault == "directory_symlink":
                directory = folder / "collected"
                original = folder / "retained-collected"
                directory.rename(original)
                directory.symlink_to(original, target_is_directory=True)
            elif fault == "public_receipt":
                receipt_path.chmod(0o644)
            elif fault == "receipt_nonterminal" or (
                    isinstance(fault, str) and fault.startswith("scope:")):
                value = json.loads(receipt_path.read_bytes())
                if fault == "receipt_nonterminal":
                    value["terminal_status"] = "inProgress"
                else:
                    value["scope"][fault.split(":", 1)[1]] = "foreign"
                raw = collection_io.json_bytes(value)
                receipt_path.write_bytes(raw)
                # Update the outer digest to test ownership checks themselves.
                self.report["collection_receipt_sha256"] = sha(raw)
            case.observed_patch = patch_path.read_bytes()
            case.observed_manifest = (
                folder / "collected" / "COLLECTION.json").read_bytes()
            if case.lost:
                raise case.error
            return copy.deepcopy(result)

        def close(self):
            self.closed = True
            if case.close_error is not None:
                raise case.close_error
            report = copy.deepcopy(self.report)
            if case.proof_key:
                if case.proof_value == "missing":
                    report.pop(case.proof_key, None)
                else:
                    report[case.proof_key] = case.proof_value
            return report

    controller = Controller()
    runtime = object.__new__(host.HostCodexRuntime)
    runtime.journal = journal
    runtime.tasks_root = root
    runtime.controllers = {"a1": controller}
    prepared = {
        "assignment_id": "a1", "host_folder": str(folder),
        "benchmark_id": "synthetic", "task_id": "task",
        "task_content_hash": "a" * 64, "model": host.MODEL,
        "effort": "low", "agent_timeout_sec": 1,
        "host_preflight": {
            "no_model_thread_preflight": {"thread_id": THREAD},
            "task_container_id": CONTAINER,
        },
        "host_collection_binding": copy.deepcopy(cfg),
    }
    assignment = {
        "assignment_id": "a1", "run_id": "run1",
        "lease_id": "lease1", "owner_epoch": 1,
    }
    request = journal.prepare(
        "start:a1", "/api/v2/single/assignments/a1/start",
        {"lease_id": "lease1", "owner_epoch": 1, "execution_id": eid})
    journal.acknowledge(request, {
        "request_id": request.request_id, "status": "started",
        "assignment": {"assignment_id": "a1", "execution_id": eid},
    })

    def barrier():
        if not journal.begin_execution("a1", eid):
            raise host.RuntimeUnavailable("original execution already fenced")

    case.runtime = runtime
    case.controller = controller
    case.prepared = prepared
    case.assignment = assignment
    case.barrier = barrier
    case.original_start = (request.request_id, request.body_json)
    case.error_key = "host_collect_first_error:a1:" + eid
    case.recovery_key = "host_collect_recovery:a1:" + eid
    return case


def run_and_save(case):
    completion = case.runtime.execute_with_barrier(
        case.prepared, case.eid, case.barrier, nullcontext)
    payload = save_completion(case.journal.root / "artifacts",
                              case.assignment, case.eid, completion)
    case.journal.save_result("a1", case.eid, payload)
    return completion, payload


def assert_original_call_and_files(case):
    assert case.controller.closed
    assert [value["op"] for value in case.controller.calls] == [
        "authorize-one-turn", "rpc", "events", "status", "events", "collect"]
    assert sum(value.get("method") == "turn/start"
               for value in case.controller.calls) == 1
    request = next(value for value in case.journal.requests()
                   if value.operation == "start:a1")
    assert (request.request_id, request.body_json) == case.original_start
    saved_request = json.loads(
        (case.folder / "COLLECT-REQUEST.json").read_bytes())
    assert saved_request["request"] == {"op": "collect"}
    assert (case.folder / "collected" / "model.patch").read_bytes() == case.observed_patch
    assert (case.folder / "collected" / "COLLECTION.json").read_bytes() == case.observed_manifest
    assert not case.journal.begin_execution("a1", case.eid)


def assert_blocked(case):
    row = case.journal.execution("a1")
    assert row["state"] == "launch_fenced" and row["result_json"] is None
    assert not (case.journal.root / "artifacts" / "metadata" / "a1.json").exists()
    assert_original_call_and_files(case)


@pytest.mark.parametrize("lost", [False, True])
def test_same_nonempty_collection_normal_ack_and_lost_ack(tmp_path, monkeypatch, lost):
    case = make_case(tmp_path, monkeypatch, lost=lost)
    completion, payload = run_and_save(case)
    assert completion.outcome == "completed" and completion.exit_confirmed
    assert completion.failure is None
    assert set(completion.files) == {"patch", "runner_result", "trajectory"}
    assert {item["name"] for item in payload["artifacts"]} == {
        "patch", "runner_result", "trajectory"}
    assert payload["tokens"]["total"] == 5
    raw = case.journal.root / "artifacts" / "raw" / "a1"
    assert (raw / "patch").read_bytes() == PATCH
    assert (raw / "runner_result").read_bytes() == case.observed_manifest
    assert case.journal.execution("a1")["state"] == "result_saved"
    assert_original_call_and_files(case)
    if lost:
        error = json.loads(case.journal.value(case.error_key))
        assert error["type"] == "builtins.TimeoutError"
        assert error["message"] == str(case.error)
        assert "synthetic original collect ACK lost" in error["traceback"]
        assert error["successful_collect_ack_received"] is False
        recovery = json.loads(case.journal.value(case.recovery_key))
        assert recovery["source"] == "controller_durable_receipt"
        assert recovery["successful_collect_ack_received"] is False
        assert recovery["first_error_key"] == case.error_key
    else:
        assert case.journal.value(case.error_key) is None
        assert case.journal.value(case.recovery_key) is None


@pytest.mark.parametrize("fault", [
    "missing", "corrupt", "receipt_digest", "patch_hash", "manifest_hash",
    "missing_deliverable", "patch_symlink", "receipt_symlink",
    "directory_symlink", "public_receipt", "receipt_nonterminal",
    "missing_terminal_event",
] + ["scope:" + key for key in (
    "assignment_id", "execution_id", "benchmark", "task_id",
    "task_content_hash", "collector_sha256", "collection_nonce",
    "thread_id", "turn_id", "container_id",
)])
def test_invalid_receipts_never_save_empty_result(tmp_path, monkeypatch, fault):
    case = make_case(tmp_path, monkeypatch, fault=fault)
    with pytest.raises(host.RuntimeUnavailable) as error:
        run_and_save(case)
    assert error.value.__cause__ is case.error
    assert case.journal.value(case.recovery_key) is None
    assert_blocked(case)


@pytest.mark.parametrize("key", collection_io.PROOFS)
@pytest.mark.parametrize("value", [False, None, 1, "true", "missing"])
def test_every_exit_proof_requires_literal_true(tmp_path, monkeypatch, key, value):
    case = make_case(tmp_path, monkeypatch)
    case.proof_key, case.proof_value = key, value
    with pytest.raises(host.RuntimeUnavailable, match="retain fence"):
        run_and_save(case)
    assert json.loads(case.journal.value(case.error_key))["message"] == str(case.error)
    assert_blocked(case)


@pytest.mark.parametrize("terminal", ["failed", "interrupted"])
def test_noncompleted_terminal_is_not_recovered(tmp_path, monkeypatch, terminal):
    case = make_case(tmp_path, monkeypatch, terminal=terminal)
    with pytest.raises(host.RuntimeUnavailable):
        run_and_save(case)
    assert_blocked(case)


@pytest.mark.parametrize("error", [
    RuntimeError("unknown controller failure"), KeyboardInterrupt(),
])
def test_nontransport_errors_are_not_recovered(tmp_path, monkeypatch, error):
    case = make_case(tmp_path, monkeypatch)
    case.error = error
    with pytest.raises(host.RuntimeUnavailable):
        run_and_save(case)
    assert_blocked(case)


def test_close_failure_retains_first_collect_error(tmp_path, monkeypatch):
    case = make_case(tmp_path, monkeypatch)
    case.close_error = OSError("synthetic close failure")
    with pytest.raises(host.RuntimeUnavailable) as error:
        run_and_save(case)
    assert error.value.__cause__ is case.error
    assert json.loads(case.journal.value(case.error_key))["message"] == str(case.error)
    assert_blocked(case)


@pytest.mark.parametrize("lost", [False, True])
@pytest.mark.parametrize("failure", ["io", "different_existing"])
def test_event_persistence_cannot_discard_or_rewrite_originals(
        tmp_path, monkeypatch, lost, failure):
    case = make_case(tmp_path, monkeypatch, lost=lost)
    path = case.folder / "public-events.json"
    original = b'[{"retained":"different original"}]\n'
    if failure == "different_existing":
        collection_io.persist_once(path, original)
    else:
        actual = host.persist_once
        def fail_event(destination, raw):
            if Path(destination).name == "public-events.json":
                raise OSError("synthetic event fsync failure")
            return actual(destination, raw)
        monkeypatch.setattr(host, "persist_once", fail_event)
    with pytest.raises(host.RuntimeUnavailable, match="event persistence"):
        run_and_save(case)
    if failure == "different_existing":
        assert path.read_bytes() == original
    assert_blocked(case)


def test_receipt_publication_failure_cannot_ack_or_save(tmp_path, monkeypatch):
    case = make_case(tmp_path, monkeypatch)
    actual = collection_io.persist_once
    def fail_receipt(destination, raw):
        if Path(destination).name == collection_io.RECEIPT:
            raise OSError("synthetic receipt fsync failure")
        return actual(destination, raw)
    monkeypatch.setattr(collection_io, "persist_once", fail_receipt)
    with pytest.raises(host.RuntimeUnavailable):
        run_and_save(case)
    assert (case.folder / "collected" / "model.patch").read_bytes() == PATCH
    assert not (case.folder / collection_io.RECEIPT).exists()
    assert "collection_receipt_sha256" not in case.controller.report
    assert case.journal.execution("a1")["result_json"] is None
    assert sum(value["op"] == "collect" for value in case.controller.calls) == 1


def test_existing_result_prevents_any_second_authorization(tmp_path, monkeypatch):
    case = make_case(tmp_path, monkeypatch)
    assert case.journal.begin_execution("a1", case.eid)
    original = {"preserved": "existing result"}
    case.journal.save_result("a1", case.eid, original)
    before = case.journal.execution("a1")
    with pytest.raises(host.RuntimeUnavailable, match="already fenced"):
        run_and_save(case)
    assert case.controller.calls == []
    assert case.journal.execution("a1") == before


def test_result_upload_ack_loss_replays_original_request_and_bytes(
        tmp_path, monkeypatch):
    case = make_case(tmp_path, monkeypatch)
    _, payload = run_and_save(case)
    uploads = []
    def send_result(request, files):
        uploads.append((
            request.request_id, request.body_json,
            {name: sha(path.read_bytes()) for name, path in files.items()},
        ))
        if len(uploads) == 1:
            raise TimeoutError("synthetic upload ACK lost")
        receipt = {
            "schema_version": 2, "server_time": "2026-10-15T00:00:00Z",
            "request_id": request.request_id, "status": "submitted",
            "assignment_id": "a1", "execution_id": case.eid,
            "result_sha256": payload["result_sha256"],
            "submission_id": "synthetic-submission", "grading_state": "queued",
        }
        case.journal.acknowledge(request, receipt)
        return receipt
    client = SimpleNamespace(
        journal=case.journal,
        _bootstrap={"limits": {"max_result_bytes": 8 * 1024 * 1024}},
        send_result=send_result,
    )
    job = Job(client)
    with pytest.raises(TimeoutError, match="upload ACK"):
        job.upload(case.assignment)
    assert case.journal.pending_request("result:a1") is not None
    assert job.upload(case.assignment)["status"] == "submitted"
    assert uploads[0] == uploads[1]
    assert case.journal.pending_request("result:a1") is None
    assert_original_call_and_files(case)
