"""Finite, controller-fenced personal claims for an existing local Fleet.

The coordinator grants one immutable foreground operation and remains free to
heartbeat and receive stops while the foreground uses the existing durable
acquisition journal.  It never stores credentials or duplicates leases.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import acquisition_recovery, assignment_boundary, boundary_recovery, fleet, run_intent
from .api_client import ApiError
from .identity import _client
from .local_config import DEFAULT_BENCHMARK, HOME, _load_config
from .runtime_identity import process_identity
from .codebuddy_provider import CODEBUDDY_AGENT
from .providers import (
    ANTIGRAVITY_AGENT, CLAUDE_AGENT, DEEPSEEK_PROVIDER, DSH_AGENT,
    GROK_AGENT, KIMI_AGENT, ZCODE_AGENT,
)


_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_TERMINAL = {"held", "not_claimed"}


def _cutoff(value: object) -> float:
    if not isinstance(value, str):
        raise fleet.FleetError("Fleet claim requires an ISO deadline with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise fleet.FleetError("invalid Fleet claim deadline") from exc
    if parsed.tzinfo is None:
        raise fleet.FleetError("Fleet claim deadline needs a timezone")
    return parsed.timestamp()


def _scope(account: str, benchmark: str, window_id: str) -> str:
    return "request-" + hashlib.sha256(
        json.dumps(["fleet-claim-v1", account, benchmark, window_id], separators=(",", ":")).encode()
    ).hexdigest()


def _owner(request: dict) -> dict:
    value = request.get("owner_identity")
    if not isinstance(value, dict) or value.get("pid") != request.get("owner_pid"):
        raise fleet.FleetError("Fleet claim foreground identity is missing")
    actual = process_identity(request["owner_pid"])
    if actual is None or actual != value:
        raise fleet.FleetError("Fleet claim foreground process changed")
    return actual


def _current_owner(operation: dict, request: dict) -> None:
    if not isinstance(operation, dict) or operation.get("status") != "active":
        raise fleet.FleetError("no active Fleet claim operation")
    if request.get("operation_id") != operation.get("operation_id"):
        raise fleet.FleetError("Fleet claim operation changed")
    secret = request.get("grant_secret")
    if (not isinstance(secret, str) or not _HEX32.fullmatch(secret)
            or hashlib.sha256(secret.encode()).hexdigest() != operation.get("secret_sha256")):
        raise fleet.FleetError("Fleet claim grant credential changed")
    if _owner(request) != operation.get("owner_identity"):
        raise fleet.FleetError("another foreground owns this Fleet claim")


def _response(home: Path, request: dict, payload: dict) -> None:
    fleet._response(home, str(request["request_id"]), payload)


def _begin(home: Path, state: dict, request: dict) -> dict:
    if request.get("controller_protocol_version") != fleet.CONTROLLER_PROTOCOL_VERSION:
        raise fleet.FleetControllerUpdatePending()
    owner = _owner(request)
    account = request.get("account_scope")
    window_id = request.get("window_id")
    benchmark = request.get("benchmark")
    picks = request.get("picks")
    harness = request.get("harness")
    workers = request.get("workers")
    max_new = request.get("max_new")
    max_workers = request.get("max_workers")
    deadline = request.get("deadline")
    if (not isinstance(account, str) or not _HEX32.fullmatch(account)
            or not isinstance(window_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{3,63}", window_id)
            or not isinstance(benchmark, str) or not benchmark
            or not isinstance(harness, str) or not harness
            or not isinstance(picks, list) or not picks or len(picks) > 40
            or any(not isinstance(pick, list) or len(pick) != 3
                   or any(not isinstance(part, str) or not part for part in pick)
                   for pick in picks)
            or len({tuple(pick) for pick in picks}) != len(picks)
            or type(workers) is not int or not 1 <= workers <= 40
            or type(max_new) is not int or not 1 <= max_new <= 10000
            or type(max_workers) is not int or not 1 <= max_workers <= 40):
        raise fleet.FleetError("invalid bounded Fleet claim scope")
    end = _cutoff(deadline)
    if time.time() >= end:
        raise fleet.FleetError("Fleet claim deadline has passed; no task claimed")
    window = state.get("claim_window")
    expected = {
        "window_id": window_id, "account_scope": account, "benchmark": benchmark,
        "max_new": max_new, "max_workers": max_workers,
        "deadline": deadline,
    }
    if any(isinstance(saved, dict) and saved.get("window_id") == window_id
           for saved in state.get("claim_history", [])):
        raise fleet.FleetError("a completed Fleet claim window ID cannot be reopened")
    if any(isinstance(saved, dict) and (
            saved.get("account_scope") != account or saved.get("benchmark") != benchmark)
            for saved in state.get("claim_history", [])):
        raise fleet.FleetError("Fleet claim account or benchmark differs from retained history")
    if isinstance(window, dict) and (
            window.get("account_scope") != account or window.get("benchmark") != benchmark):
        raise fleet.FleetError("Fleet claim account or benchmark differs from this HOME")
    if isinstance(window, dict) and window.get("window_id") != window_id:
        previous = window.get("operation")
        if (isinstance(previous, dict) or not window.get("stopped")
                and time.time() < _cutoff(window.get("deadline"))):
            raise fleet.FleetError("the earlier Fleet claim window is still open")
        state.setdefault("claim_history", []).append(window)
        window = None
    if window is None:
        scope = _scope(account, benchmark, window_id)
        generation = run_intent.begin(home, scope)
        window = {"schema_version": 1, **expected, "scope": scope,
                  "generation": generation, "stopped": False,
                  "operations": [], "operation": None}
        state["claim_window"] = window
    elif not isinstance(window, dict) or any(window.get(k) != v for k, v in expected.items()):
        raise fleet.FleetError("Fleet claim limits or identity differ from the saved window")
    if window.get("stopped"):
        raise fleet.FleetError("Fleet new claims were stopped; held batches remain")
    run_intent.require(home, window["scope"], window["generation"])
    active = window.get("operation")
    if isinstance(active, dict) and active.get("status") != "complete":
        raise fleet.FleetError("an earlier Fleet claim needs exact receipt reconciliation")
    spent = sum(
        1 for op in window.get("operations", [])
        for slot in op.get("slots", [])
        if slot.get("status") != "not_claimed"
    )
    if spent + len(picks) > max_new:
        raise fleet.FleetError("Fleet claim task budget exhausted; no task claimed")
    existing = fleet._active_batches(state)
    all_windows = [*state.get("claim_history", []), window]
    planned = {
        op.get("batch_id"): op.get("workers")
        for saved in all_windows if isinstance(saved, dict)
        for op in saved.get("operations", [])
        if op.get("batch_id") and type(op.get("workers")) is int
        and op.get("batch_id") not in state.get("batches", {})
    }
    reserved = sum(int(item.get("workers") or 0) for item in existing.values())
    if reserved + sum(planned.values()) + workers > max_workers:
        raise fleet.FleetError("Fleet claim worker ceiling exhausted; no task claimed")
    secret = uuid.uuid4().hex
    operation = {
        "operation_id": uuid.uuid4().hex,
        "secret_sha256": hashlib.sha256(secret.encode()).hexdigest(),
        "controller_id": state["controller_id"],
        "owner_identity": owner,
        "harness": harness,
        "benchmark": benchmark,
        "workers": workers,
        "selection_id": uuid.uuid4().hex,
        "slots": [{"pick": pick, "request_id": uuid.uuid4().hex,
                   "status": "prepared"} for pick in picks],
        "status": "active", "batch_id": None,
    }
    window["operation"] = operation
    window["operations"].append(operation)
    fleet._write_state(home, state)
    return {"ok": True, "operation": operation, "grant_secret": secret,
            "scope": window["scope"], "generation": window["generation"]}


def _slot(operation: dict, request: dict) -> dict:
    request_id = request.get("claim_request_id")
    for slot in operation.get("slots", []):
        if slot.get("request_id") == request_id:
            return slot
    raise fleet.FleetError("claim request identity is outside the grant")


def _mark_sent(home: Path, state: dict, request: dict) -> dict:
    window = state.get("claim_window") or {}
    op = window.get("operation")
    _current_owner(op, request)
    run_intent.require(home, window["scope"], window["generation"])
    slot = _slot(op, request)
    if slot.get("status") != "prepared":
        raise fleet.FleetError("claim request is already sent or settled")
    if any(previous.get("status") not in _TERMINAL
           for previous in op["slots"][:op["slots"].index(slot)]):
        raise fleet.FleetError("earlier pick remains unresolved")
    slot["status"] = "sent"
    fleet._write_state(home, state)
    return {"ok": True, "request_id": slot["request_id"]}


def _skip_prepared(home: Path, state: dict, request: dict) -> dict:
    window = state.get("claim_window") or {}
    op = window.get("operation")
    _current_owner(op, request)
    slot = _slot(op, request)
    if slot.get("status") != "prepared":
        raise fleet.FleetError("only an unsent Fleet pick can be skipped")
    slot["status"] = "not_claimed"
    fleet._write_state(home, state)
    return {"ok": True, "status": "not_claimed"}


def _result(home: Path, state: dict, request: dict) -> dict:
    window = state.get("claim_window") or {}
    op = window.get("operation")
    _current_owner(op, request)
    slot = _slot(op, request)
    status = request.get("status")
    if slot.get("status") in _TERMINAL:
        if slot.get("status") == status and slot.get("assignment") == request.get("assignment"):
            return {"ok": True, "already_recorded": True}
        raise fleet.FleetError("claim result conflicts with saved result")
    if slot.get("status") != "sent" or status not in {"held", "not_claimed", "unknown"}:
        raise fleet.FleetError("claim result has no sent request")
    if status == "held":
        assignment = request.get("assignment")
        if (not isinstance(assignment, dict)
                or any(assignment.get(key) != value for key, value in zip(
                    ("task_id", "model", "effort"), slot["pick"]
                )) or not isinstance(assignment.get("assignment_id"), str)
                or not isinstance(assignment.get("batch_id"), str)):
            raise fleet.FleetError("claim response does not match its exact pick")
        if op.get("batch_id") not in (None, assignment["batch_id"]):
            raise fleet.FleetError("claim response crossed the exact Harness batch")
        op["batch_id"] = assignment["batch_id"]
        slot["assignment"] = {key: assignment[key] for key in
                              ("assignment_id", "batch_id", "task_id", "model", "effort")}
    elif status == "not_claimed":
        proof = request.get("proof")
        if proof != {"operation": "assignment_claim", "request_id": slot["request_id"],
                     "status": "definitive_rejection"}:
            raise fleet.FleetError("missing exact authoritative no-claim proof")
    slot["status"] = status
    if status == "unknown":
        op["status"] = "needs_reconciliation"
    fleet._write_state(home, state)
    return {"ok": True, "status": status}


def _complete(home: Path, state: dict, request: dict) -> dict:
    window = state.get("claim_window") or {}
    op = window.get("operation")
    _current_owner(op, request)
    if any(slot.get("status") not in _TERMINAL for slot in op["slots"]):
        raise fleet.FleetError("claim operation has an unresolved request")
    if any(slot["status"] == "held" for slot in op["slots"]):
        if request.get("boundary_batch_id") != op.get("batch_id"):
            raise fleet.FleetError("exact batch boundary was not confirmed")
        _verify_boundary(home, window, op)
    op["status"] = "complete"
    op["owner_identity"] = None
    window["operation"] = None
    fleet._write_state(home, state)
    return {"ok": True, "batch_id": op.get("batch_id"),
            "claimed": sum(slot["status"] == "held" for slot in op["slots"])}


def _stop(home: Path, state: dict, request: dict) -> dict:
    window = state.get("claim_window")
    if not isinstance(window, dict):
        return {"ok": True, "already_stopped": True}
    run_intent.stop_request(home, window["scope"])
    window["stopped"] = True
    fleet._write_state(home, state)
    return {"ok": True, "stopped": True}


def _reconcile_result(home: Path, state: dict, request: dict) -> dict:
    window = state.get("claim_window") or {}
    op = window.get("operation")
    if (not isinstance(op, dict) or op.get("status") not in
            {"active", "needs_reconciliation"}
            or request.get("operation_id") != op.get("operation_id")):
        raise fleet.FleetError("no matching Fleet claim needs reconciliation")
    previous_owner = op.get("owner_identity")
    if isinstance(previous_owner, dict):
        pid = previous_owner.get("pid")
        if type(pid) is int and process_identity(pid) == previous_owner:
            raise fleet.FleetError("the original Fleet claim foreground is still active")
    slot = _slot(op, request)
    if slot.get("status") in _TERMINAL:
        return {"ok": True, "already_recorded": True}
    status = request.get("status")
    if status == "held":
        assignment = request.get("assignment")
        if (not isinstance(assignment, dict)
                or any(assignment.get(key) != value for key, value in zip(
                    ("task_id", "model", "effort"), slot["pick"]
                )) or not isinstance(assignment.get("assignment_id"), str)
                or not isinstance(assignment.get("batch_id"), str)
                or op.get("batch_id") not in (None, assignment["batch_id"])):
            raise fleet.FleetError("reconciled receipt crosses the saved claim scope")
        op["batch_id"] = assignment["batch_id"]
        slot["assignment"] = {key: assignment[key] for key in
                              ("assignment_id", "batch_id", "task_id", "model", "effort")}
        slot["status"] = "held"
    elif status == "not_claimed" and slot.get("status") == "prepared":
        # A foreground must first persist `sent` through this controller before
        # it can enter acquisition_recovery. A dead owner cannot send it now.
        slot["status"] = "not_claimed"
    else:
        raise fleet.FleetError("a missing receipt cannot settle a sent claim")
    fleet._write_state(home, state)
    return {"ok": True, "status": slot["status"]}


def _reconcile_complete(home: Path, state: dict, request: dict) -> dict:
    window = state.get("claim_window") or {}
    op = window.get("operation")
    if (not isinstance(op, dict) or op.get("status") not in
            {"active", "needs_reconciliation"}
            or request.get("operation_id") != op.get("operation_id")
            or any(slot.get("status") not in _TERMINAL for slot in op["slots"])):
        raise fleet.FleetError("Fleet claim still has an unresolved request")
    if any(slot["status"] == "held" for slot in op["slots"]):
        if request.get("boundary_batch_id") != op.get("batch_id"):
            raise fleet.FleetError("reconciled exact boundary is unconfirmed")
        _verify_boundary(home, window, op)
    op["status"] = "complete"
    op["owner_identity"] = None
    window["operation"] = None
    fleet._write_state(home, state)
    return {"ok": True, "batch_id": op.get("batch_id")}


def _verify_boundary(home: Path, window: dict, operation: dict) -> None:
    path = assignment_boundary.state_path(home, window["benchmark"], operation["batch_id"])
    try:
        saved, _digest = assignment_boundary.inspect_snapshot(path)
    except assignment_boundary.BoundaryError as exc:
        raise fleet.FleetError("exact held Fleet boundary is missing or unreadable") from exc
    if (saved.get("benchmark_id") != window["benchmark"]
            or saved.get("batch_id") != operation["batch_id"]):
        raise fleet.FleetError("exact held Fleet boundary identity differs")
    expected = saved.get("expected") or {}
    for slot in operation["slots"]:
        if slot["status"] != "held":
            continue
        assignment = slot["assignment"]
        if expected.get(assignment["assignment_id"]) != {
                key: assignment[key] for key in ("task_id", "model", "effort", "batch_id")
        }:
            raise fleet.FleetError("exact held Fleet boundary does not contain its claim")


def handle_request(home: Path, state: dict, processes: dict, request: dict) -> None:
    try:
        command = request.get("command")
        if command == "claim_begin":
            payload = _begin(home, state, request)
        elif command == "claim_mark_sent":
            payload = _mark_sent(home, state, request)
        elif command == "claim_result":
            payload = _result(home, state, request)
        elif command == "claim_skip_prepared":
            payload = _skip_prepared(home, state, request)
        elif command == "claim_complete":
            payload = _complete(home, state, request)
        elif command == "claim_stop":
            payload = _stop(home, state, request)
        elif command == "claim_reconcile_result":
            payload = _reconcile_result(home, state, request)
        elif command == "claim_reconcile_complete":
            payload = _reconcile_complete(home, state, request)
        else:
            payload = {"ok": False, "error": "unknown Fleet claim command"}
    except (fleet.FleetError, run_intent.IntentStopped, ValueError, KeyError) as exc:
        payload = {"ok": False, "error": str(exc)}
    _response(home, request, payload)


def _call(command: str, payload: dict) -> dict:
    response = fleet._request(command, payload, home=HOME)
    if not response.get("ok"):
        raise fleet.FleetError(str(response.get("error") or "Fleet claim failed"))
    return response


def _account(client) -> str:
    identity = client.whoami()
    volunteer = identity.get("volunteer_id") if isinstance(identity, dict) else None
    if not isinstance(volunteer, str) or not _HEX32.fullmatch(volunteer):
        raise fleet.FleetError("authenticated Fleet claim account is unconfirmed")
    return hashlib.sha256(
        json.dumps([client.server, volunteer], separators=(",", ":")).encode()
    ).hexdigest()[:32]


def _picks_and_harness(client, specs: list[str]) -> tuple[list[list[str]], str]:
    picks = []
    for spec in specs:
        parts = spec.split(":")
        if len(parts) != 3 or not all(parts):
            raise fleet.FleetError("--pick requires task_id:model:effort")
        picks.append(parts)
    if len({tuple(pick) for pick in picks}) != len(picks):
        raise fleet.FleetError("repeat each Fleet claim pick only once")
    table = client.table()
    if (not isinstance(table, dict) or table.get("benchmark_id") != client.benchmark_id
            or not isinstance(table.get("cells"), dict)
            or not isinstance(table.get("combos"), list)):
        raise fleet.FleetError("Server cell catalog cannot prove the selected Harness")
    harnesses = set()
    for task, model, effort in picks:
        cell = table["cells"].get(f"{task}|{model}|{effort}")
        matching = [combo for combo in table["combos"]
                    if isinstance(combo, dict)
                    and combo.get("model") == model and combo.get("effort") == effort]
        if not isinstance(cell, dict) or len(matching) != 1 or any(
                cell.get(key) != matching[0].get(key) for key in ("agent", "provider")):
            raise fleet.FleetError("Server cell catalog has ambiguous Harness metadata")
        raw_agent = cell.get("agent")
        if raw_agent is None and cell.get("provider") not in (None, "openai", DEEPSEEK_PROVIDER):
            raise fleet.FleetError("Server cell catalog has ambiguous Harness metadata")
        agent = raw_agent or "codex"
        if agent not in {"codex", CLAUDE_AGENT, DSH_AGENT, KIMI_AGENT,
                         GROK_AGENT, ZCODE_AGENT, ANTIGRAVITY_AGENT, CODEBUDDY_AGENT}:
            raise fleet.FleetError("Server cell catalog names an unknown Harness")
        harnesses.add(agent)
    if len(harnesses) != 1:
        raise fleet.FleetError("Fleet claim accepts one Harness per request; split exact batches")
    return picks, next(iter(harnesses))


def _grant_snapshot(operation_id: str, claim_request_id: str, owner: dict,
                    *, allow_prepared: bool = False) -> tuple[dict, dict, dict]:
    state = fleet._read_json(fleet._state_path(HOME))
    window = state.get("claim_window") if isinstance(state, dict) else None
    operation = window.get("operation") if isinstance(window, dict) else None
    slot = next((item for item in operation.get("slots", [])
                 if item.get("request_id") == claim_request_id), None) \
        if isinstance(operation, dict) else None
    if (not fleet.controller_is_active(HOME) or not isinstance(operation, dict)
            or operation.get("operation_id") != operation_id
            or operation.get("controller_id") != state.get("controller_id")
            or operation.get("status") != "active"
            or operation.get("owner_identity") != owner
            or process_identity(os.getpid()) != owner
            or not isinstance(slot, dict)
            or slot.get("status") not in ({"prepared", "sent"} if allow_prepared else {"sent"})
            or window.get("stopped") or time.time() >= _cutoff(window.get("deadline"))):
        raise fleet.FleetError("Fleet claim ownership, stop or deadline changed")
    run_intent.require(HOME, window["scope"], window["generation"])
    if boundary_recovery._pending_ids(HOME):
        raise boundary_recovery.RecoveryBlocked("a pending upload appeared during Fleet claim")
    return state, operation, slot


def _fresh_admission(client, operation_id: str, claim_request_id: str,
                     owner: dict, *, allow_prepared: bool = False) -> None:
    _grant_snapshot(operation_id, claim_request_id, owner,
                    allow_prepared=allow_prepared)
    if boundary_recovery._pending_ids(HOME):
        raise boundary_recovery.RecoveryBlocked("a pending upload remains")
    path = assignment_boundary.state_path(HOME, client.benchmark_id)
    if path.is_symlink():
        raise fleet.FleetError("personal assignment boundary is a symlink")
    if path.exists():
        state, digest = assignment_boundary.snapshot(path)
        if not assignment_boundary._report(state, set()).complete:
            boundary_recovery.historical_unknown_allows_claim(
                client, state, digest, path, HOME,
                fleet_claim_operation=operation_id,
            )
            return
    boundary_recovery._check_processes(HOME, fleet_claim_operation=operation_id)
    if boundary_recovery._pending_ids(HOME):
        raise boundary_recovery.RecoveryBlocked("a pending upload appeared during review")


def _record_boundary(client, assignment: dict, batch_id: str) -> None:
    path = assignment_boundary.state_path(HOME, client.benchmark_id, batch_id)
    if path.exists():
        assignment_boundary.add_expected(path, [assignment], require_matching_metadata=True)
    else:
        saved = assignment_boundary.prepare(
            HOME, client.benchmark_id, [assignment], batch_id=batch_id,
            require_matching_metadata=True,
        )
        if saved != path:
            raise fleet.FleetError("exact Fleet claim boundary was not saved")


def _sent_or_unknown(operation_id: str, claim_request_id: str) -> bool:
    state = fleet._read_json(fleet._state_path(HOME)) or {}
    op = (state.get("claim_window") or {}).get("operation") or {}
    if op.get("operation_id") != operation_id:
        return True
    slot = next((item for item in op.get("slots", [])
                 if item.get("request_id") == claim_request_id), None)
    return not isinstance(slot, dict) or slot.get("status") != "prepared"


def cmd_fleet_claim(args) -> int:
    if os.name == "nt" or process_identity(os.getpid()) is None:
        raise SystemExit("Fleet claim needs Linux process ownership evidence")
    fleet.prepare_new_batch_runtime(HOME)
    cfg = _load_config()
    benchmark = args.benchmark or cfg.get("benchmark") or DEFAULT_BENCHMARK
    cfg["benchmark"] = benchmark
    client = _client(cfg, auto_register=True)
    client.benchmark_id = benchmark
    client.require_runner_reservation_protocol()
    capabilities = client.run_plan_capabilities()
    if "explicit-pick-batch-v1" not in capabilities.get("capabilities", []):
        raise SystemExit("Server upgrade required for exact Fleet selection")
    picks, harness = _picks_and_harness(client, args.pick)
    owner = process_identity(os.getpid())
    if owner is None:
        raise SystemExit("Fleet claim foreground identity changed")
    grant = _call("claim_begin", {
        "window_id": args.window_id, "account_scope": _account(client), "benchmark": benchmark,
        "picks": picks, "harness": harness, "workers": args.workers,
        "max_new": args.max_new, "max_workers": args.max_concurrent,
        "deadline": args.deadline, "owner_pid": os.getpid(),
        "owner_identity": owner,
    })
    operation = grant["operation"]
    operation_id = operation["operation_id"]
    grant_secret = grant["grant_secret"]
    client.new_pick_batch = True
    client.pick_selection_id = operation["selection_id"]
    batch_id = None
    uncertain = False
    for slot in operation["slots"]:
        rid = slot["request_id"]
        shared = {"operation_id": operation_id, "claim_request_id": rid,
                  "owner_pid": os.getpid(), "owner_identity": owner,
                  "grant_secret": grant_secret}
        sent = False
        try:
            _fresh_admission(client, operation_id, rid, owner, allow_prepared=True)
            _call("claim_mark_sent", shared)
            sent = True
            _fresh_admission(client, operation_id, rid, owner)
            data = client.claim_assignment(
                *slot["pick"], request_id=rid,
                retry_check=lambda: _grant_snapshot(operation_id, rid, owner) and True,
            )
            assignment = data.get("assignment") if isinstance(data, dict) else None
            if (not isinstance(assignment, dict)
                    or (batch_id is not None and assignment.get("batch_id") != batch_id)):
                raise fleet.FleetError("claim response crossed the exact Fleet batch")
            if not isinstance(assignment, dict) or not assignment.get("batch_id"):
                raise fleet.FleetError("claim response lacks an exact batch")
            batch_id = assignment["batch_id"]
            _call("claim_result", {**shared, "status": "held", "assignment": assignment})
            try:
                _record_boundary(client, assignment, batch_id)
            except (fleet.FleetError, assignment_boundary.BoundaryError) as exc:
                # The held result is already durable. Leave the operation open
                # for exact boundary repair, never overwrite it as unknown.
                print(f"  held claim {rid[:8]} needs boundary recovery: {exc}")
                uncertain = True
                break
        except ApiError as exc:
            sent = sent or _sent_or_unknown(operation_id, rid)
            if not sent:
                _call("claim_skip_prepared", shared)
                print(f"  {':'.join(slot['pick'])}: not claimed ({exc})")
                break
            proof = getattr(exc, "allocation_no_claim", None)
            if proof == {"operation": "assignment_claim", "request_id": rid,
                          "status": "definitive_rejection"}:
                _call("claim_result", {**shared, "status": "not_claimed", "proof": proof})
                print(f"  {':'.join(slot['pick'])}: not claimed ({exc})")
                break
            # Other API errors can occur after a committed response (for
            # example an auth-runtime binding check).
            _call("claim_result", {**shared, "status": "unknown"})
            uncertain = True
            print(f"  claim {rid[:8]} requires exact reconciliation: {exc}")
            break
        except (fleet.FleetError, boundary_recovery.RecoveryBlocked,
                assignment_boundary.BoundaryError, run_intent.IntentStopped) as exc:
            sent = sent or _sent_or_unknown(operation_id, rid)
            if not sent:
                _call("claim_skip_prepared", shared)
                print(f"  {':'.join(slot['pick'])}: not claimed ({exc})")
                break
            # Once marked sent, an exception cannot establish non-execution.
            _call("claim_result", {**shared, "status": "unknown"})
            uncertain = True
            print(f"  claim {rid[:8]} requires exact reconciliation: {exc}")
            break
    if uncertain:
        print("Fleet claim remains unknown; use `dradar fleet claim-recover`. No replacement was claimed.")
        return 1
    # Remaining prepared slots, if any, were never allowed into the send path.
    state = fleet._read_json(fleet._state_path(HOME)) or {}
    active = (state.get("claim_window") or {}).get("operation") or {}
    for slot in active.get("slots", []):
        if slot.get("status") == "prepared":
            _call("claim_skip_prepared", {
                "operation_id": operation_id, "claim_request_id": slot["request_id"],
                "owner_pid": os.getpid(), "owner_identity": owner,
                "grant_secret": grant_secret,
            })
    completed = _call("claim_complete", {
        "operation_id": operation_id, "owner_pid": os.getpid(),
        "owner_identity": owner, "grant_secret": grant_secret,
        "boundary_batch_id": batch_id,
    })
    print(f"Fleet claimed {completed.get('claimed', 0)} task(s) in exact batch "
          f"{completed.get('batch_id') or 'none'}; no model was started.")
    if completed.get("batch_id"):
        print(f"Add only this batch with `dradar fleet add --batch-id {batch_id} --workers {args.workers}`")
    return 0


def cmd_fleet_claim_recover(args) -> int:
    fleet.prepare_new_batch_runtime(HOME)
    fleet._ensure_controller(HOME)
    state = fleet._read_json(fleet._state_path(HOME)) or {}
    window = state.get("claim_window") or {}
    operation = window.get("operation")
    if not isinstance(operation, dict):
        print("no unresolved Fleet claim operation")
        return 0
    old_owner = operation.get("owner_identity")
    if isinstance(old_owner, dict) and process_identity(old_owner.get("pid")) == old_owner:
        raise SystemExit("original Fleet claim foreground is still active")
    cfg = _load_config()
    cfg["benchmark"] = window.get("benchmark")
    client = _client(cfg)
    client.benchmark_id = window.get("benchmark")
    if _account(client) != window.get("account_scope"):
        raise SystemExit("saved Fleet claim belongs to a different account or Server")
    batch_id = operation.get("batch_id")
    unresolved = []
    for slot in operation.get("slots", []):
        status = slot.get("status")
        rid = slot.get("request_id")
        if status == "prepared":
            _call("claim_reconcile_result", {
                "operation_id": operation["operation_id"],
                "claim_request_id": rid, "status": "not_claimed",
            })
        elif status in {"sent", "unknown"}:
            try:
                receipt = client._get(f"/api/v1/write-receipts/assignment_claim/{rid}")
            except ApiError:
                receipt = None
            assignment = (receipt.get("result") or {}).get("assignment") \
                if isinstance(receipt, dict) and receipt.get("status") == "committed" \
                and receipt.get("operation") == "assignment_claim" \
                and receipt.get("request_id") == rid else None
            if isinstance(assignment, dict):
                _call("claim_reconcile_result", {
                    "operation_id": operation["operation_id"],
                    "claim_request_id": rid, "status": "held",
                    "assignment": assignment,
                })
                batch_id = assignment["batch_id"]
            else:
                unresolved.append(rid)
    state = fleet._read_json(fleet._state_path(HOME)) or {}
    operation = (state.get("claim_window") or {}).get("operation") or {}
    for slot in operation.get("slots", []):
        if slot.get("status") == "held":
            _record_boundary(client, slot["assignment"], slot["assignment"]["batch_id"])
    if unresolved:
        print("original Fleet claim request(s) still have unknown outcomes: "
              + ", ".join(rid[:8] for rid in unresolved))
        print("No new claim or model start was authorized; retry receipt reconciliation later.")
        return 1
    held = {slot["request_id"]: slot["assignment"]
            for slot in operation.get("slots", []) if slot.get("status") == "held"}
    if held:
        acquisition_recovery.clear_reconciled_claim(client, held)
    completed = _call("claim_reconcile_complete", {
        "operation_id": operation["operation_id"],
        "boundary_batch_id": batch_id,
    })
    print(f"Fleet claim reconciled in exact batch {completed.get('batch_id') or 'none'}; "
          "no model was started.")
    return 0


def cmd_fleet_claim_stop(args) -> int:
    state = fleet._read_json(fleet._state_path(HOME)) or {}
    window = state.get("claim_window")
    if not isinstance(window, dict):
        print("no local Fleet claim window to stop")
        return 0
    run_intent.stop_request(HOME, window["scope"])
    try:
        _call("claim_stop", {})
    except fleet.FleetError as exc:
        print(f"new claims were locally stopped; controller acknowledgement is pending: {exc}")
        return 1
    print("future Fleet claims stopped; original in-flight requests remain for receipt reconciliation")
    return 0
