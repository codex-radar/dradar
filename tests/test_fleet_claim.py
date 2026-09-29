from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import os

import pytest

from dradar import boundary_recovery, fleet, fleet_claim, runtime_identity


def _deadline():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _request(owner, *, picks=None, workers=8, max_new=27, max_workers=20):
    return {
        "controller_protocol_version": fleet.CONTROLLER_PROTOCOL_VERSION,
        "owner_pid": owner["pid"], "owner_identity": owner,
        "window_id": "ticket-0236", "account_scope": "f" * 32,
        "benchmark": "deep-swe", "harness": "codex",
        "picks": picks or [["task-1", "gpt-6-sol", "high"]],
        "workers": workers, "max_new": max_new,
        "max_workers": max_workers, "deadline": _deadline(),
    }


def test_finite_claim_window_serializes_batches_and_concurrency(tmp_path, monkeypatch):
    fleet._prepare_dirs(tmp_path)
    owner = {"pid": 321, "start_ticks": 5, "boot_id": "boot", "host_id": "host"}
    monkeypatch.setattr(fleet_claim, "process_identity", lambda pid: owner if pid == 321 else None)
    monkeypatch.setattr(fleet_claim.run_intent, "begin", lambda *_: "a" * 32)
    monkeypatch.setattr(fleet_claim.run_intent, "require", lambda *_: None)
    state = fleet._initial_state("controller-1", None)
    first = _request(owner)
    grant = fleet_claim._begin(tmp_path, state, first)
    op = grant["operation"]
    with pytest.raises(fleet.FleetError, match="earlier Fleet claim"):
        fleet_claim._begin(tmp_path, state, first)
    slot = op["slots"][0]
    control = {"operation_id": op["operation_id"], "claim_request_id": slot["request_id"],
               "owner_pid": 321, "owner_identity": owner,
               "grant_secret": grant["grant_secret"]}
    fleet_claim._mark_sent(tmp_path, state, control)
    assignment = {"assignment_id": "1" * 32, "batch_id": "2" * 32,
                  "task_id": "task-1", "model": "gpt-6-sol", "effort": "high"}
    fleet_claim._result(tmp_path, state, {**control, "status": "held", "assignment": assignment})
    fleet_claim.assignment_boundary.prepare(tmp_path, "deep-swe", [assignment],
                                             batch_id="2" * 32)
    fleet_claim._complete(tmp_path, state, {**control, "boundary_batch_id": "2" * 32})
    second = {**first, "harness": "grok", "picks": [["task-2", "grok-4.7", "low"]]}
    second_grant = fleet_claim._begin(tmp_path, state, second)
    assert len(state["claim_window"]["operations"]) == 2
    second_grant["operation"]["batch_id"] = "3" * 32
    second_grant["operation"]["status"] = "complete"
    state["claim_window"]["operation"] = None
    with pytest.raises(fleet.FleetError, match="worker ceiling"):
        fleet_claim._begin(tmp_path, state, {**second, "workers": 5})


def test_unknown_claim_keeps_budget_and_stop_does_not_erase_it(tmp_path, monkeypatch):
    fleet._prepare_dirs(tmp_path)
    owner = {"pid": 321, "start_ticks": 5, "boot_id": "boot", "host_id": "host"}
    live = {"value": True}
    monkeypatch.setattr(fleet_claim, "process_identity",
                        lambda pid: owner if pid == 321 and live["value"] else None)
    monkeypatch.setattr(fleet_claim.run_intent, "begin", lambda *_: "a" * 32)
    monkeypatch.setattr(fleet_claim.run_intent, "require", lambda *_: None)
    monkeypatch.setattr(fleet_claim.run_intent, "stop_request", lambda *_: None)
    state = fleet._initial_state("controller-1", None)
    req = _request(owner, max_new=1)
    grant = fleet_claim._begin(tmp_path, state, req)
    op = grant["operation"]
    slot = op["slots"][0]
    control = {"operation_id": op["operation_id"], "claim_request_id": slot["request_id"],
               "owner_pid": 321, "owner_identity": owner,
               "grant_secret": grant["grant_secret"]}
    fleet_claim._mark_sent(tmp_path, state, control)
    fleet_claim._result(tmp_path, state, {**control, "status": "unknown"})
    with pytest.raises(fleet.FleetError, match="earlier Fleet claim"):
        fleet_claim._begin(tmp_path, state, req)
    fleet_claim._stop(tmp_path, state, {})
    assert slot["status"] == "unknown"
    live["value"] = False
    with pytest.raises(fleet.FleetError, match="missing receipt"):
        fleet_claim._reconcile_result(tmp_path, state, {
            "operation_id": op["operation_id"], "claim_request_id": slot["request_id"],
            "status": "not_claimed",
        })
    assignment = {"assignment_id": "1" * 32, "batch_id": "2" * 32,
                  "task_id": "task-1", "model": "gpt-6-sol", "effort": "high"}
    fleet_claim._reconcile_result(tmp_path, state, {
        "operation_id": op["operation_id"], "claim_request_id": slot["request_id"],
        "status": "held", "assignment": assignment,
    })
    fleet_claim.assignment_boundary.prepare(tmp_path, "deep-swe", [assignment],
                                             batch_id="2" * 32)
    fleet_claim._reconcile_complete(tmp_path, state, {
        "operation_id": op["operation_id"], "boundary_batch_id": "2" * 32,
    })
    assert slot["status"] == "held"
    live["value"] = True
    with pytest.raises(fleet.FleetError, match="stopped"):
        fleet_claim._begin(tmp_path, state, req)


def test_controller_restart_preserves_sent_request_for_reconciliation(tmp_path, monkeypatch):
    fleet._prepare_dirs(tmp_path)
    owner = {"pid": 321, "start_ticks": 5, "boot_id": "boot", "host_id": "host"}
    monkeypatch.setattr(fleet_claim, "process_identity", lambda pid: owner if pid == 321 else None)
    monkeypatch.setattr(fleet_claim.run_intent, "begin", lambda *_: "a" * 32)
    monkeypatch.setattr(fleet_claim.run_intent, "require", lambda *_: None)
    state = fleet._initial_state("controller-1", None)
    req = _request(owner)
    grant = fleet_claim._begin(tmp_path, state, req)
    op = grant["operation"]
    slot = op["slots"][0]
    fleet_claim._mark_sent(tmp_path, state, {
        "operation_id": op["operation_id"], "claim_request_id": slot["request_id"],
        "owner_pid": 321, "owner_identity": owner,
        "grant_secret": grant["grant_secret"],
    })
    restarted = fleet._initial_state("controller-2", state)
    saved = restarted["claim_window"]["operation"]
    assert saved is restarted["claim_window"]["operations"][0]
    assert saved["slots"][0]["request_id"] == slot["request_id"]
    assert saved["slots"][0]["status"] == "sent"
    assert saved["status"] == "needs_reconciliation"
    with pytest.raises(fleet.FleetError, match="earlier Fleet claim"):
        fleet_claim._begin(tmp_path, restarted, req)


def test_unknown_legacy_agent_with_unrelated_provider_is_not_guessed_as_codex():
    class Client:
        benchmark_id = "deep-swe"

        def table(self):
            cell = {"agent": None, "provider": "unrelated-provider"}
            return {"benchmark_id": self.benchmark_id,
                    "cells": {"task-1|model|high": cell},
                    "combos": [{"model": "model", "effort": "high", **cell}]}

    with pytest.raises(fleet.FleetError, match="ambiguous Harness"):
        fleet_claim._picks_and_harness(Client(), ["task-1:model:high"])


def test_current_server_harness_ids_are_accepted_without_short_aliases():
    class Client:
        benchmark_id = "deep-swe"

        def table(self):
            grok = {"agent": "grok-build", "provider": "xai-subscription"}
            dsh = {"agent": "dsh-minimal", "provider": "deepseek"}
            return {"benchmark_id": self.benchmark_id,
                    "cells": {"t1|grok-4.7|low": grok,
                              "t2|dsh-deepseek-v4-flash|high": dsh},
                    "combos": [{"model": "grok-4.7", "effort": "low", **grok},
                               {"model": "dsh-deepseek-v4-flash", "effort": "high", **dsh}]}

    assert fleet_claim._picks_and_harness(Client(), ["t1:grok-4.7:low"])[1] == "grok-build"
    assert fleet_claim._picks_and_harness(Client(), ["t2:dsh-deepseek-v4-flash:high"])[1] == "dsh-minimal"


def test_historical_window_id_cannot_be_reopened(tmp_path, monkeypatch):
    fleet._prepare_dirs(tmp_path)
    owner = {"pid": 321, "start_ticks": 5, "boot_id": "boot", "host_id": "host"}
    monkeypatch.setattr(fleet_claim, "process_identity", lambda pid: owner if pid == 321 else None)
    monkeypatch.setattr(fleet_claim.run_intent, "begin", lambda *_: "a" * 32)
    state = fleet._initial_state("controller-1", None)
    old = _request(owner)
    state["claim_history"] = [{"window_id": old["window_id"], "stopped": True}]
    with pytest.raises(fleet.FleetError, match="cannot be reopened"):
        fleet_claim._begin(tmp_path, state, old)


def test_new_window_keeps_unstarted_held_batch_worker_reservation(tmp_path, monkeypatch):
    fleet._prepare_dirs(tmp_path)
    owner = {"pid": 321, "start_ticks": 5, "boot_id": "boot", "host_id": "host"}
    monkeypatch.setattr(fleet_claim, "process_identity", lambda pid: owner if pid == 321 else None)
    monkeypatch.setattr(fleet_claim.run_intent, "begin", lambda *_: "a" * 32)
    monkeypatch.setattr(fleet_claim.run_intent, "require", lambda *_: None)
    monkeypatch.setattr(fleet_claim.run_intent, "stop_request", lambda *_: None)
    state = fleet._initial_state("controller-1", None)
    first = _request(owner, workers=8, max_workers=20)
    op = fleet_claim._begin(tmp_path, state, first)["operation"]
    op.update(batch_id="2" * 32, status="complete", owner_identity=None)
    state["claim_window"]["operation"] = None
    fleet_claim._stop(tmp_path, state, {})
    second = {**first, "window_id": "ticket-0236-next", "workers": 13}
    with pytest.raises(fleet.FleetError, match="worker ceiling"):
        fleet_claim._begin(tmp_path, state, second)


def test_second_foreground_cannot_replay_live_grant_or_claim_missing_boundary(tmp_path, monkeypatch):
    fleet._prepare_dirs(tmp_path)
    owner = {"pid": 321, "start_ticks": 5, "boot_id": "boot", "host_id": "host"}
    monkeypatch.setattr(fleet_claim, "process_identity", lambda pid: owner if pid == 321 else None)
    monkeypatch.setattr(fleet_claim.run_intent, "begin", lambda *_: "a" * 32)
    monkeypatch.setattr(fleet_claim.run_intent, "require", lambda *_: None)
    state = fleet._initial_state("controller-1", None)
    grant = fleet_claim._begin(tmp_path, state, _request(owner))
    op = grant["operation"]
    slot = op["slots"][0]
    replay = {"operation_id": op["operation_id"], "claim_request_id": slot["request_id"],
              "owner_pid": 321, "owner_identity": owner}
    with pytest.raises(fleet.FleetError, match="grant credential"):
        fleet_claim._mark_sent(tmp_path, state, replay)
    owned = {**replay, "grant_secret": grant["grant_secret"]}
    fleet_claim._mark_sent(tmp_path, state, owned)
    assignment = {"assignment_id": "1" * 32, "batch_id": "2" * 32,
                  "task_id": "task-1", "model": "gpt-6-sol", "effort": "high"}
    fleet_claim._result(tmp_path, state, {**owned, "status": "held", "assignment": assignment})
    with pytest.raises(fleet.FleetError, match="boundary"):
        fleet_claim._complete(tmp_path, state, {**owned, "boundary_batch_id": "2" * 32})


def test_sent_claim_can_finish_on_exact_definitive_rejection(tmp_path, monkeypatch):
    fleet._prepare_dirs(tmp_path)
    owner = {"pid": 321, "start_ticks": 5, "boot_id": "boot", "host_id": "host"}
    monkeypatch.setattr(fleet_claim, "process_identity", lambda pid: owner if pid == 321 else None)
    monkeypatch.setattr(fleet_claim.run_intent, "begin", lambda *_: "a" * 32)
    monkeypatch.setattr(fleet_claim.run_intent, "require", lambda *_: None)
    state = fleet._initial_state("controller-1", None)
    grant = fleet_claim._begin(tmp_path, state, _request(owner))
    op = grant["operation"]
    slot = op["slots"][0]
    owned = {"operation_id": op["operation_id"], "claim_request_id": slot["request_id"],
             "owner_pid": 321, "owner_identity": owner,
             "grant_secret": grant["grant_secret"]}
    fleet_claim._mark_sent(tmp_path, state, owned)
    proof = {"operation": "assignment_claim", "request_id": slot["request_id"],
             "status": "definitive_rejection"}
    fleet_claim._result(tmp_path, state, {**owned, "status": "not_claimed", "proof": proof})
    assert fleet_claim._complete(tmp_path, state, owned)["claimed"] == 0


def test_fleet_process_proof_allows_owned_tree_but_rejects_foreign_runner(
    tmp_path, monkeypatch,
):
    from dradar import launcher_handoff
    owner = {"pid": os.getpid(), "start_ticks": 10, "boot_id": "boot", "host_id": "host"}
    pool = {"pid": 101, "start_ticks": 11, "boot_id": "boot", "host_id": "host"}
    state = {"pid": 100, "controller_id": "controller-1",
             "claim_window": {"operation": {
                 "operation_id": "op", "status": "active", "controller_id": "controller-1",
                 "owner_identity": owner}},
             "batches": {"batch-1": {"status": "running", "pid": 101,
                                      "process_identity": pool}}}
    monkeypatch.setattr(boundary_recovery.fleet, "_read_json", lambda *_: state)
    monkeypatch.setattr(boundary_recovery.fleet, "controller_is_active", lambda *_: True)
    monkeypatch.setattr(runtime_identity, "process_identity",
                        lambda pid: {os.getpid(): owner, 101: pool}.get(pid))
    monkeypatch.setattr(launcher_handoff, "supervisor", lambda: None)
    foreign = {"value": False}
    def run(command, **_kwargs):
        if command[0] == "ps":
            lines = ["100 1 python -m dradar.cli fleet serve",
                     "101 100 python -m dradar.cli resume",
                     "102 101 python -m dradar.cli go --worker-child",
                     f"{os.getpid()} {os.getppid()} python -m dradar.cli fleet claim"]
            if foreign["value"]:
                lines.append("999 1 python -m dradar.cli go")
            return SimpleNamespace(stdout="\n".join(lines))
        assert command[:3] == ["docker", "ps", "-q"]
        return SimpleNamespace(stdout="")
    monkeypatch.setattr(boundary_recovery.subprocess, "run", run)
    boundary_recovery._check_processes(tmp_path, fleet_claim_operation="op")
    foreign["value"] = True
    with pytest.raises(boundary_recovery.RecoveryBlocked, match="another DRadar runner"):
        boundary_recovery._check_processes(tmp_path, fleet_claim_operation="op")
