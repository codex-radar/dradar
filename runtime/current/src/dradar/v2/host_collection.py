"""Owner-only collection commits; no RPC, relaunch, or historical recovery."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .artifacts import _sync_dir
from .native_evidence import collection_safety

RECEIPT = "COLLECT-RECEIPT.json"
PROOFS = (
    "native_app_server_reaped",
    "created_containers_absent",
    "owned_container_names_absent",
)
KEYS = (
    "assignment_id", "execution_id", "benchmark", "task_id",
    "task_content_hash", "collector_sha256", "deliverable_names",
    "collection_nonce", "model", "effort",
)
LIMIT = 8 * 1024 * 1024


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False) + "\n").encode()


def _directory(path, *, private=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("noncanonical evidence directory")
    info = path.stat()
    forbidden = 0o077 if private else 0o022
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & forbidden):
        raise ValueError("owned protected evidence directory required")
    return path


def _read(path):
    path = Path(path)
    _directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise ValueError("private unlinked regular evidence file required")
        raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("collection evidence exceeds bounded size")
    return raw


def persist_once(path, raw):
    """Durable no-clobber publication; never truncate an existing original."""
    path = Path(path)
    _directory(path.parent)
    if not isinstance(raw, bytes) or len(raw) > LIMIT:
        raise ValueError("bounded evidence bytes required")
    stage = path.parent / ("." + path.name + "-" + uuid.uuid4().hex)
    fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(stage, path, follow_symlinks=False)
    except FileExistsError:
        if _read(path) != raw:
            raise ValueError("different original evidence already exists")
    _sync_dir(path.parent)
    stage.unlink()
    _sync_dir(path.parent)
    # On an earlier failure our staging file remains for inspection.


def _scope(config, thread, turn, container):
    scope = {key: config[key] for key in KEYS}
    for key, value in scope.items():
        if key != "deliverable_names" and (
                not isinstance(value, str) or not value):
            raise ValueError("incomplete collection ownership")
    for key in ("task_content_hash", "collector_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", scope[key]):
            raise ValueError("immutable collection binding required")
    if not re.fullmatch(r"[a-f0-9]{32}", scope["collection_nonce"]):
        raise ValueError("current parent collection nonce required")
    names = scope["deliverable_names"]
    if (not isinstance(names, list) or not names
            or any(not isinstance(name, str)
                   or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
                   or name in {".", "..", "COLLECTION.json"} for name in names)
            or len(names) != len(set(names))):
        raise ValueError("exact deliverable allowlist required")
    if any(not isinstance(value, str) or not value
           for value in (thread, turn, container)):
        raise ValueError("owned thread, turn and container required")
    if not re.fullmatch(r"[a-f0-9]{64}", container):
        raise ValueError("exact container identity required")
    return {**scope, "deliverable_names": list(names),
            "thread_id": thread, "turn_id": turn, "container_id": container}


def _manifest_matches(value, scope, patch, status):
    expected = {
        "task": scope["task_id"], "container_id": scope["container_id"],
        "model_turn_status": status, "model": scope["model"],
        "reasoning": scope["effort"],
    }
    if not isinstance(value, dict) or any(
            value.get(key) != item for key, item in expected.items()):
        raise ValueError("collection manifest ownership mismatch")
    if (value.get("patch_sha256") != hashlib.sha256(patch).hexdigest()
            or type(value.get("patch_bytes")) is not int
            or value["patch_bytes"] != len(patch)):
        raise ValueError("original patch differs from collection manifest")


def persist_collection(folder, config, report, request, collected,
                       patch, deliverables):
    """Called by the controller before setting collected_manifest or ACK."""
    folder = _directory(folder, private=True)
    if config.get("evidence_dir") != str(folder):
        raise ValueError("collection directory binding mismatch")
    if request != {"op": "collect"}:
        raise ValueError("exact original collect request required")
    scope = _scope(config, report.get("model_thread_id"),
                   report.get("model_turn_id"), report.get("task_container_id"))
    status = report.get("last_turn_status")
    if (report.get("model_turn_start_ack") is not True
            or type(report.get("model_calls")) is not int
            or report["model_calls"] != 1
            or status not in {"completed", "failed", "interrupted"}
            or report.get("model_terminal_thread_id") != scope["thread_id"]
            or report.get("model_terminal_turn_id") != scope["turn_id"]):
        raise ValueError("matching owned terminal required before collection")
    _manifest_matches(collected, scope, patch, status)
    data = {"model.patch": patch}
    for name, raw in deliverables.items():
        if name not in scope["deliverable_names"]:
            raise ValueError("unexpected deliverable")
        if name in data and data[name] != raw:
            raise ValueError("deliverable would replace original patch")
        data[name] = raw
    # Preserve the controller's existing manifest byte representation.
    data["COLLECTION.json"] = (json.dumps(collected, indent=2) + "\n").encode()
    destination = folder / "collected"
    destination.mkdir(mode=0o700)
    records = {}
    for name, raw in sorted(data.items()):
        persist_once(destination / name, raw)
        records[name] = {"sha256": hashlib.sha256(raw).hexdigest(),
                         "size": len(raw)}
    receipt = {
        "schema": "dradar.host_collection_receipt.v1",
        "scope": scope, "request": request,
        "terminal_status": status, "files": records,
    }
    raw = json_bytes(receipt)
    persist_once(folder / RECEIPT, raw)
    return hashlib.sha256(raw).hexdigest()


def recover_collection(folder, config, thread, turn, container, events, closed):
    """Verify only a current parent's completed, durable original collection."""
    folder = _directory(folder, private=True)
    if config.get("evidence_dir") != str(folder):
        raise ValueError("collection directory binding mismatch")
    scope = _scope(config, thread, turn, container)
    if not all(closed.get(key) is True for key in PROOFS):
        raise ValueError("physical exit remains unconfirmed")
    expected_report = {
        "task": scope["task_id"], "task_container_id": container,
        "model_thread_id": thread, "model_turn_id": turn,
        "model_terminal_thread_id": thread, "model_terminal_turn_id": turn,
        "last_turn_status": "completed",
        "collected_manifest": str(folder / "collected" / "COLLECTION.json"),
    }
    if (any(closed.get(key) != value for key, value in expected_report.items())
            or closed.get("model_turn_start_ack") is not True
            or closed.get("last_turn_error") is not None):
        raise ValueError("closed controller ownership or terminal mismatch")
    terminal = [event for event in events
                if isinstance(event, dict)
                and event.get("method") == "turn/completed"]
    if (len(terminal) != 1
            or terminal[0].get("threadId") != thread
            or terminal[0].get("turnId") != turn
            or terminal[0].get("status") != "completed"
            or terminal[0].get("error_code") is not None):
        raise ValueError("matching observed completed event required")
    raw = _read(folder / RECEIPT)
    digest = hashlib.sha256(raw).hexdigest()
    if closed.get("collection_receipt_sha256") != digest:
        raise ValueError("durable receipt digest mismatch")
    receipt = json.loads(raw)
    if (not isinstance(receipt, dict)
            or set(receipt) != {
                "schema", "scope", "request", "terminal_status", "files"}
            or receipt["schema"] != "dradar.host_collection_receipt.v1"
            or receipt["scope"] != scope
            or receipt["request"] != {"op": "collect"}
            or receipt["terminal_status"] != "completed"):
        raise ValueError("current collection receipt ownership mismatch")
    expected_files = {"model.patch", "COLLECTION.json"} | set(
        scope["deliverable_names"])
    records = receipt["files"]
    if not isinstance(records, dict) or set(records) != expected_files:
        raise ValueError("complete required collection receipt missing")
    destination = _directory(folder / "collected", private=True)
    data = {}
    for name, record in records.items():
        if (not isinstance(record, dict) or set(record) != {"sha256", "size"}
                or type(record["size"]) is not int or record["size"] < 0):
            raise ValueError("invalid collection file receipt")
        content = _read(destination / name)
        if (len(content) != record["size"]
                or hashlib.sha256(content).hexdigest() != record["sha256"]):
            raise ValueError("original collected file digest mismatch")
        data[name] = content
    collected = json.loads(data["COLLECTION.json"])
    _manifest_matches(collected, scope, data["model.patch"], "completed")
    if collected.get("model_turn_error") is not None:
        raise ValueError("completed collection contains a model error")
    collection_safety(collected)
    return collected, digest
