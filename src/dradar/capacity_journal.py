"""Durable local execution evidence, separate from logical runner close.

Missing, damaged or incomplete evidence never becomes an empty execution list.
Only a sealed session whose every attempt was audited can release a reservation.
The server receives an evidence digest; the detailed inventory stays local.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

from .api_client import ApiError, normalize_batch_id


class CapacityEvidenceError(RuntimeError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _read(path: Path) -> dict:
    try:
        if path.is_symlink():
            raise ValueError("symbolic link")
        state = json.loads(path.read_text())
        if (not isinstance(state, dict) or type(state.get("schema_version")) is not int
                or state["schema_version"] != 1
                or state.get("state") not in {"open", "sealed"}
                or not isinstance(state.get("attempts"), dict)
                or not isinstance(state.get("session_id"), str)
                or not isinstance(state.get("server"), str)
                or type(state.get("released")) is not bool
                or "device_generation" not in state
                or (state["device_generation"] is not None and (
                    type(state["device_generation"]) is not int or state["device_generation"] < 0))
                or "batch_id" not in state
                or state["batch_id"] != normalize_batch_id(state["batch_id"])):
            raise ValueError("unknown execution journal")
        for attempt in state["attempts"].values():
            if not isinstance(attempt, dict) or not isinstance(attempt.get("scope"), dict) or not isinstance(attempt.get("events"), list):
                raise ValueError("invalid attempt")
            previous = "registered"
            execution_id = None
            for event in attempt["events"]:
                kind = event["event"]
                scope = event["scope"]
                identity = event["execution_id"]
                if (event.get("schema") != "dradar.execution_audit.v1"
                        or not isinstance(identity, str) or len(identity) != 32
                        or any(c not in "0123456789abcdef" for c in identity)
                        or execution_id not in (None, identity)
                        or scope.get("runner_session_id") != state["session_id"]
                        or any(scope.get(key) != attempt["scope"].get(key) for key in ("assignment_id", "task_id", "batch_id"))):
                    raise ValueError("invalid attempt identity")
                allowed = {"entered": {"registered"}, "launch_pending": {"entered"},
                           "spawned": {"launch_pending"}, "confirmed_absent": {"spawned"},
                           "never_started": {"entered", "launch_pending"}}
                if kind != "unknown" and previous not in allowed.get(kind, set()):
                    raise ValueError("invalid execution order")
                if kind == "confirmed_absent" and (event.get("process_group") != "absent" or event.get("exact_job_containers") != "absent"):
                    raise ValueError("missing exit facts")
                if kind == "never_started" and (event.get("execution_started") is not False or (previous == "launch_pending" and event.get("reason") != "popen_failed")):
                    raise ValueError("unconfirmed no-launch claim")
                previous, execution_id = kind, identity
            if attempt.get("status") != previous or attempt.get("execution_id") != execution_id:
                raise ValueError("attempt summary differs from evidence")
        if state.get("release_request"):
            request = state["release_request"]
            manifest = {key: state[key] for key in (
                "schema_version", "session_id", "server", "batch_id", "state", "attempts", "close_request",
            )}
            if (state["state"] != "sealed" or state.get("execution_manifest") != manifest
                    or request["session_id"] != state["session_id"] or request["batch_id"] != state["batch_id"]
                    or any(attempt["status"] not in {"confirmed_absent", "never_started"} for attempt in state["attempts"].values())
                    or request["execution_manifest_sha256"] != hashlib.sha256(_canonical(manifest)).hexdigest()):
                raise ValueError("sealed evidence changed")
        return state
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CapacityEvidenceError("Execution evidence cannot be verified; preserve the journal and reservation.") from exc


def _write(path: Path, state: dict) -> None:
    from .run_plans import _atomic_json
    try:
        _atomic_json(path, state)
    except OSError as exc:
        raise CapacityEvidenceError("Execution evidence could not be persisted; no further launch is permitted.") from exc


class CapacityJournal:
    def __init__(self, home: Path, *, session_id: str, server: str):
        if len(session_id) != 32 or any(c not in "0123456789abcdef" for c in session_id):
            raise CapacityEvidenceError("An exact local session identity is required.")
        self.path = home / "runner-reservations" / (session_id + ".json")
        self.lock = self.path.with_suffix(".lock")
        from .run_plans import _exclusive_lock
        with _exclusive_lock(self.lock):
            if self.path.exists() or self.path.is_symlink():
                raise CapacityEvidenceError("An execution journal already exists for this session.")
            _write(self.path, {
                "schema_version": 1, "session_id": session_id,
                "server": server.rstrip("/"), "batch_id": None,
                "device_generation": None, "state": "open", "attempts": {},
                "release_request": None, "released": False,
            })

    def _update(self, operation):
        from .run_plans import _exclusive_lock
        with _exclusive_lock(self.lock):
            state = _read(self.path)
            result = operation(state)
            _write(self.path, state)
            return result

    def bind(self, batch_id: str) -> None:
        batch_id = normalize_batch_id(batch_id)
        if batch_id is None:
            raise CapacityEvidenceError("An exact batch is required for execution evidence.")
        def update(state):
            if state["batch_id"] not in (None, batch_id):
                raise CapacityEvidenceError("A session cannot change its execution batch.")
            state["batch_id"] = batch_id
        self._update(update)

    def bind_generation(self, generation: object) -> None:
        if type(generation) is not int or generation < 0:
            raise CapacityEvidenceError("The reservation generation was not confirmed.")
        def update(state):
            if state["device_generation"] not in (None, generation):
                raise CapacityEvidenceError("A session cannot change its reservation generation.")
            state["device_generation"] = generation
        self._update(update)

    def begin_attempt(self, assignment: dict):
        execution_id = uuid.uuid4().hex
        binding = {key: assignment.get(key) for key in (
            "assignment_id", "task_id", "batch_id", "owner_epoch", "resume_generation",
        )}
        def begin(state):
            if state["state"] != "open" or state["batch_id"] != binding["batch_id"]:
                raise CapacityEvidenceError("This session is sealed or its execution scope changed.")
            if any(item.get("status") not in {"confirmed_absent", "never_started"}
                   for item in state["attempts"].values()):
                raise CapacityEvidenceError("An earlier execution exit is unknown; this slot cannot start another attempt.")
            if not isinstance(binding["assignment_id"], str) or not binding["assignment_id"]:
                raise CapacityEvidenceError("An execution must identify its assignment.")
            state["attempts"][execution_id] = {"scope": binding, "status": "registered", "events": []}
        self._update(begin)

        def observe(event: dict):
            if (not isinstance(event, dict) or event.get("schema") != "dradar.execution_audit.v1"
                    or event.get("event") not in {
                        "entered", "launch_pending", "spawned", "never_started", "confirmed_absent", "unknown",
                    }):
                raise CapacityEvidenceError("Unknown execution audit event; capacity remains reserved.")
            # Round-trip only local JSON evidence. No callback objects or raw
            # provider output can become part of the evidence digest.
            saved = json.loads(_canonical(event))
            def update(state):
                if state["state"] != "open":
                    raise CapacityEvidenceError("A sealed session cannot launch or change execution evidence.")
                attempt = state["attempts"][execution_id]
                scope = saved.get("scope")
                if not isinstance(scope, dict) or scope.get("runner_session_id") != state["session_id"] or any(
                    scope.get(key) != binding[key]
                    for key in ("assignment_id", "task_id", "batch_id")
                ):
                    raise CapacityEvidenceError("Execution audit scope does not match its durable attempt.")
                event_id = saved.get("execution_id")
                if (not isinstance(event_id, str) or len(event_id) != 32
                        or any(c not in "0123456789abcdef" for c in event_id)):
                    raise CapacityEvidenceError("Execution audit identity is missing or invalid.")
                if attempt.get("execution_id") not in (None, event_id):
                    raise CapacityEvidenceError("An attempt cannot change its execution identity.")
                previous = attempt["status"]
                allowed = {
                    "entered": {"registered"}, "launch_pending": {"entered"},
                    "spawned": {"launch_pending"}, "confirmed_absent": {"spawned"},
                    "never_started": {"entered", "launch_pending"},
                }
                if saved["event"] != "unknown" and previous not in allowed[saved["event"]]:
                    raise CapacityEvidenceError("Execution audit events are incomplete or out of order.")
                if saved["event"] == "confirmed_absent" and (
                    saved.get("process_group") != "absent"
                    or saved.get("exact_job_containers") != "absent"
                ):
                    raise CapacityEvidenceError("Exit evidence is incomplete.")
                if saved["event"] == "never_started" and (
                    saved.get("execution_started") is not False
                    or any(e["event"] == "spawned" for e in attempt["events"])
                    or (any(e["event"] == "launch_pending" for e in attempt["events"])
                        and saved.get("reason") != "popen_failed")
                ):
                    raise CapacityEvidenceError("A pending launch cannot be declared never started.")
                if attempt["status"] in {"confirmed_absent", "never_started"} and saved["event"] != "unknown":
                    raise CapacityEvidenceError("An audited attempt cannot launch again.")
                attempt["events"].append(saved)
                attempt["status"] = saved["event"]
                attempt["execution_id"] = event_id
            self._update(update)
        return observe

    def seal(self, *, close_seq: int, reason: str) -> bool:
        def update(state):
            if state["state"] == "sealed":
                return state.get("release_request") is not None
            state["state"] = "sealed"
            state["close_request"] = {"session_id": state["session_id"], "batch_id": state["batch_id"],
                                      "seq": close_seq, "reason": reason}
            if not state["batch_id"] or any(
                item.get("status") not in {"confirmed_absent", "never_started"}
                for item in state["attempts"].values()
            ):
                return False
            # Generation may be read back from the exact server receipt if
            # its first heartbeat acknowledgement was lost.
            manifest = {key: state[key] for key in (
                "schema_version", "session_id", "server", "batch_id", "state", "attempts", "close_request",
            )}
            state["execution_manifest"] = manifest
            state["release_request"] = {
                "schema_version": 1, "session_id": state["session_id"], "batch_id": state["batch_id"],
                "evidence_id": uuid.uuid4().hex, "exit_state": "confirmed",
                "process_tree": "confirmed_absent", "owned_containers": "confirmed_absent",
                "execution_manifest_sha256": hashlib.sha256(_canonical(manifest)).hexdigest(),
            }
            return True
        return self._update(update)


def _receipt(client, state: dict) -> dict:
    receipt = client.runner_session_receipt(state["session_id"], batch_id=state["batch_id"])
    if (not isinstance(receipt, dict)
            or receipt.get("session_id") != state["session_id"]
            or receipt.get("batch_id") != state["batch_id"]
            or type(receipt.get("device_generation")) is not int
            or receipt["device_generation"] < 0
            or type(receipt.get("closed")) is not bool
            or type(receipt.get("capacity_released")) is not bool
            or type(receipt.get("reservation_protocol")) is not int
            or receipt["reservation_protocol"] != 1):
        raise CapacityEvidenceError("The server did not confirm an exact reservation receipt.")
    if state["device_generation"] is not None and receipt["device_generation"] != state["device_generation"]:
        raise CapacityEvidenceError("The receipt belongs to a different reservation generation.")
    return receipt


def reconcile_file(path: Path, client) -> bool:
    """Retry the same durable evidence; unknown receipts never create new work."""
    from .run_plans import _exclusive_lock
    with _exclusive_lock(path.with_suffix(".lock")):
        state = _read(path)
        if state["state"] != "sealed" or not state.get("release_request"):
            return False
        if state["server"] != str(client.server).rstrip("/"):
            raise CapacityEvidenceError("The evidence belongs to another server.")
        request = state["release_request"]
        digest = hashlib.sha256(_canonical(state.get("execution_manifest"))).hexdigest()
        if request.get("execution_manifest_sha256") != digest:
            raise CapacityEvidenceError("Saved exit evidence changed; preserve it for review.")
        receipt = _receipt(client, state)
        if not receipt["closed"]:
            client.runner_close(state["close_request"])
            receipt = _receipt(client, state)
            if not receipt["closed"]:
                return False
        if "device_generation" not in request:
            request["device_generation"] = receipt["device_generation"]
            request["device_id"] = None
            state["device_generation"] = receipt["device_generation"]
            _write(path, state)  # Durable identical request before mutation.
        if not receipt["capacity_released"]:
            try:
                client.release_runner_capacity(request)
            except ApiError:
                # The request may have committed. The same read-only receipt
                # settles that ambiguity; a failed read preserves this file.
                pass
            receipt = _receipt(client, state)
        if not receipt["capacity_released"]:
            return False
        if (receipt.get("release_evidence_id") != request["evidence_id"]
                or receipt.get("release_evidence_sha256") != hashlib.sha256(_canonical(request)).hexdigest()):
            raise CapacityEvidenceError("Capacity was released with different evidence; review is required.")
        state["released"] = True
        _write(path, state)
        return True


def reconcile_saved(home: Path, client, *, batch_id: str) -> dict[str, int]:
    counts = {"released": 0, "pending": 0, "unknown": 0}
    directory = home / "runner-reservations"
    if directory.is_symlink():
        raise CapacityEvidenceError("The execution evidence directory cannot be a symbolic link.")
    for path in sorted(directory.glob("*.json")):
        state = _read(path)
        if state["server"] != str(client.server).rstrip("/") or state.get("batch_id") != batch_id:
            continue
        if state.get("released") is True:
            continue
        if state["state"] != "sealed" or not state.get("release_request"):
            counts["unknown"] += 1
            continue
        try:
            released = reconcile_file(path, client)
        except ApiError:
            released = False
        counts["released" if released else "pending"] += 1
    return counts
