import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dradar import refill, runloop
from dradar.api_client import ApiError
from dradar.codebuddy_provider import (
    CODEBUDDY_AGENT,
    CODEBUDDY_MODEL,
    CODEBUDDY_PROVIDER,
)


WINDOWS = {"plus": 10.0, "pro-5x": 50.0, "pro-20x": 200.0}


def _assignment(aid: str, pct: float = 1.0) -> dict:
    return {
        "assignment_id": aid, "task_id": f"task-{aid}", "model": "m",
        "effort": "e", "est_quota_pct": pct, "tier_windows_usd": WINDOWS,
        "agent": "codex", "expires_at": "2099-01-01T00:00:00Z",
        "deep_swe_commit": None,
    }


class RefillClient:
    def __init__(self, active=None, candidates=20):
        self.active = list(active or [])
        self.cells = [
            {"task_id": f"new-{i}", "model": "m", "effort": "e",
             "est_quota_pct": 1.0}
            for i in range(candidates)
        ]
        self.claimed = []
        self._lock = threading.Lock()

    def whoami(self):
        return {"volunteer_id": "v1", "claim_limit": 20, "concurrent_limit": 10}

    def get_assignment(self):
        with self._lock:
            return {"active": list(self.active), "free_pick": True}

    def suggest(self, n):
        with self._lock:
            held = {a["task_id"] for a in self.active}
            used = set(self.claimed)
            return {"cells": [c for c in self.cells
                              if c["task_id"] not in held and c["task_id"] not in used][:n]}

    def claim_assignment(self, task_id, model, effort):
        with self._lock:
            aid = f"a-{task_id}"
            assignment = _assignment(aid)
            assignment.update(task_id=task_id, model=model, effort=effort)
            self.active.append(assignment)
            self.claimed.append(task_id)
            return {"assignment": assignment}


class LoopClient(RefillClient):
    def __init__(self, active=None, candidates=20):
        super().__init__(active, candidates)
        self.checked_out = set()

    def checkout(self, exclude_assignment_ids=None, session_id=None):
        with self._lock:
            excluded = set(exclude_assignment_ids or ())
            assignment = next(
                (a for a in self.active
                 if a["assignment_id"] not in self.checked_out
                 and a["assignment_id"] not in excluded),
                None,
            )
            if assignment:
                self.checked_out.add(assignment["assignment_id"])
            return {"assignment": assignment, "held": len(self.active),
                    "unstarted": max(0, len(self.active) - len(self.checked_out))}

    def submit_locally(self, assignment_id):
        with self._lock:
            self.active = [a for a in self.active if a["assignment_id"] != assignment_id]
            self.checked_out.discard(assignment_id)


def _configure(home: Path, active, **overrides):
    values = dict(
        volunteer_id="v1", refill_to=2, max_tasks=5, quota_tier="plus",
        max_estimated_quota_pct=None, active=active,
    )
    values.update(overrides)
    return refill.configure(home, **values)


def test_plan_persists_only_bounded_public_metadata(tmp_path: Path):
    _configure(tmp_path, [_assignment("a1")])
    raw = (tmp_path / "refill-plan.json").read_text().lower()
    plan = refill.load(tmp_path)
    assert "a1" in raw and "max_tasks" in raw
    assert plan["seed_assignment_ids"] == ["a1"]
    assert plan["submitted_seed_assignment_ids"] == []
    for secret in ("token", "nonce", "password", "auth.json"):
        assert secret not in raw


def test_fleet_batches_keep_independent_local_refill_ledgers(
    tmp_path: Path, monkeypatch,
):
    batch_a = "550e8400e29b41d4a716446655440000"
    batch_b = "6ba7b8109dad11d180b400c04fd430c8"
    monkeypatch.setenv(refill.PLAN_SCOPE_ENV, batch_a)
    first = _configure(
        tmp_path, [_assignment("a1")], server_campaign_id=batch_a,
    )
    monkeypatch.setenv(refill.PLAN_SCOPE_ENV, batch_b)
    second = _configure(
        tmp_path, [_assignment("b1")], server_campaign_id=batch_b,
    )

    assert first["plan_id"] != second["plan_id"]
    assert refill.load(tmp_path)["server_campaign_id"] == batch_b
    monkeypatch.setenv(refill.PLAN_SCOPE_ENV, batch_a)
    assert refill.load(tmp_path)["server_campaign_id"] == batch_a
    assert {
        path.name for path in (tmp_path / refill.SCOPED_PLAN_DIR).glob("*.json")
    } == {f"{batch_a}.json", f"{batch_b}.json"}


def test_server_fault_stops_scoped_local_refill_plan(tmp_path: Path):
    batch_id = "550e8400e29b41d4a716446655440000"

    class FaultClient(RefillClient):
        def refill_campaign_status(self, requested_batch_id):
            return {
                "campaign": {
                    "batch_id": requested_batch_id,
                    "status": "active",
                    "harness": "codex",
                    "model": "m",
                    "effort": "e",
                    "refill_to": 2,
                    "max_tasks": 5,
                    "planned": 0,
                    "held": 0,
                    "seed_pending": 0,
                    "stop_reason": None,
                },
            }

        def table(self):
            return {
                "combos": [{"model": "m", "effort": "e"}],
                "cells": {"new-0|m|e": {"st": "open", "cost": 1.0}},
                "tier_windows_usd": WINDOWS,
            }

        def claim_assignment(
            self, task_id, model, effort, *, refill_campaign_id=None,
            tier=None,
        ):
            raise ApiError(
                "campaign assignment failed", status_code=409,
                code="refill_campaign_faulted",
            )

    client = FaultClient([])
    _configure(
        tmp_path, [], refill_harness="codex", refill_model="m",
        refill_effort="e", server_campaign_id=batch_id,
    )

    result = refill.refill_once(tmp_path, client)

    assert result["status"] == "stopped"
    assert result["claimed"] == 0
    assert "campaign assignment failed" in result["reason"]
    assert refill.load(tmp_path)["status"] == "stopped"


@pytest.mark.parametrize("points_tier", refill.TIERS)
def test_shared_campaign_uses_global_seed_barrier_and_authorized_points_tier(
    tmp_path: Path, monkeypatch, points_tier: str,
):
    batch_id = "550e8400e29b41d4a716446655440000"
    monkeypatch.setenv(refill.PLAN_SCOPE_ENV, batch_id)
    seeds = [
        {**_assignment("seed-a"), "batch_id": batch_id},
        {**_assignment("seed-b"), "batch_id": batch_id},
    ]

    class SharedCampaignClient:
        def __init__(self):
            self.active = list(seeds)
            self.campaign = {
                "batch_id": batch_id,
                "status": "active",
                "harness": "codex",
                "model": "m",
                "effort": "e",
                "refill_to": 2,
                "max_tasks": 4,
                "planned": 2,
                "held": 2,
                "seed_pending": 1,
                "stop_reason": None,
            }
            self.claim_tiers = []

        def get_assignment(self):
            return {"active": list(self.active), "free_pick": True}

        def refill_campaign_status(self, requested_batch_id):
            assert requested_batch_id == batch_id
            return {"campaign": dict(self.campaign)}

        def table(self):
            return {
                "combos": [{"agent": "codex", "model": "m", "effort": "e"}],
                "cells": {
                    "new-a|m|e": {"st": "open", "cost": 1.0},
                    "new-b|m|e": {"st": "open", "cost": 2.0},
                },
                "tier_windows_usd": WINDOWS,
            }

        def claim_assignment(
            self, task_id, model, effort, *, refill_campaign_id=None, tier=None,
        ):
            assert refill_campaign_id == batch_id
            self.claim_tiers.append(tier)
            assignment = {
                **_assignment(f"claimed-{task_id}"),
                "task_id": task_id,
                "model": model,
                "effort": effort,
                "batch_id": batch_id,
            }
            self.active.append(assignment)
            self.campaign["planned"] += 1
            self.campaign["held"] += 1
            return {"assignment": assignment}

    client = SharedCampaignClient()
    homes = (tmp_path / "machine-a", tmp_path / "machine-b")
    for home in homes:
        refill.configure(
            home,
            volunteer_id="v1",
            refill_to=2,
            max_tasks=4,
            quota_tier="plus",
            max_estimated_quota_pct=None,
            active=seeds,
            refill_harness="codex",
            refill_model="m",
            refill_effort="e",
            server_campaign_id=batch_id,
            points_tier=points_tier,
        )
        saved = refill.load(home)
        assert saved["seed_barrier"] == "server"
        assert saved["seed_assignment_ids"] == []

    # Neither machine has a complete local seed ledger. The shared server
    # barrier alone keeps refill closed until the other device submits too.
    waiting = refill.refill_once(homes[1], client)
    assert waiting["seed_pending"] == 1
    assert client.claim_tiers == []

    client.active = []
    client.campaign.update(held=0, seed_pending=0)
    result = refill.refill_once(homes[1], client)

    assert result["claimed"] == 2
    assert client.claim_tiers == [points_tier, points_tier]
    assert client.campaign["planned"] == 4


def test_shared_campaign_status_mismatch_fails_closed(tmp_path: Path, monkeypatch):
    batch_id = "550e8400e29b41d4a716446655440000"
    monkeypatch.setenv(refill.PLAN_SCOPE_ENV, batch_id)
    refill.configure(
        tmp_path,
        volunteer_id="v1",
        refill_to=1,
        max_tasks=2,
        quota_tier="plus",
        max_estimated_quota_pct=None,
        active=[],
        refill_harness="codex",
        refill_model="m",
        refill_effort="e",
        server_campaign_id=batch_id,
        points_tier="pro-20x",
    )

    class Client:
        def get_assignment(self):
            return {"active": [], "free_pick": True}

        def refill_campaign_status(self, _batch_id):
            return {"campaign": {
                "batch_id": batch_id,
                "status": "active",
                "harness": "grok",
            }}

    with pytest.raises(refill.RefillError, match="invalid exact refill campaign"):
        refill.refill_once(tmp_path, Client())


def test_shared_campaign_accepts_server_dsh_wire_alias_for_local_scope():
    batch_id = "550e8400e29b41d4a716446655440000"
    plan = {
        "server_campaign_id": batch_id,
        "refill_harness": "dsh-minimal",
        "refill_model": "dsh-deepseek-v4-flash",
        "refill_effort": "high",
    }

    class Client:
        def refill_campaign_status(self, requested_batch_id):
            assert requested_batch_id == batch_id
            return {"campaign": {
                "batch_id": batch_id,
                "status": "active",
                "harness": "dsh",
                "model": "dsh-deepseek-v4-flash",
                "effort": "high",
                "refill_to": 1,
                "max_tasks": 2,
                "planned": 1,
                "held": 1,
                "seed_pending": 0,
                "stop_reason": None,
            }}

    snapshot = refill._authoritative_campaign_snapshot(plan, Client())

    assert snapshot["harness"] == "dsh"


def test_live_resize_updates_refill_target_without_resetting_budget(
        tmp_path: Path):
    original = _configure(tmp_path, [], refill_to=3, max_tasks=5)

    drained = refill.resize_target(tmp_path, 0)
    assert drained["refill_to"] == 0
    assert drained["plan_id"] == original["plan_id"]
    assert drained["assignments"] == original["assignments"]

    capped = refill.resize_target(tmp_path, 40)
    assert capped["refill_to"] == 5
    assert capped["max_tasks"] == 5


@pytest.mark.parametrize("target", (-1, True, 1.5))
def test_live_resize_rejects_invalid_targets(tmp_path: Path, target):
    _configure(tmp_path, [])
    with pytest.raises(refill.RefillError, match="non-negative integer"):
        refill.resize_target(tmp_path, target)


def test_selected_batch_must_all_submit_before_refill(tmp_path: Path):
    initial = [_assignment("a1"), _assignment("a2"), _assignment("a3")]
    client = RefillClient(initial)
    _configure(tmp_path, initial, refill_to=3, max_tasks=6)

    initial_check = refill.refill_once(tmp_path, client)
    assert initial_check["claimed"] == 0
    assert initial_check["seed_pending"] == 3

    for expected_remaining in (2, 1):
        submitted = client.active.pop(0)
        assert refill.mark_submitted(
            tmp_path, submitted["assignment_id"],
        ) == expected_remaining
        result = refill.refill_once(tmp_path, client)
        assert result["claimed"] == 0
        assert result["seed_pending"] == expected_remaining
        assert client.claimed == []

    submitted = client.active.pop(0)
    assert refill.mark_submitted(tmp_path, submitted["assignment_id"]) == 0
    result = refill.refill_once(tmp_path, client)
    assert result["claimed"] == 3
    assert client.claimed == ["new-0", "new-1", "new-2"]


def test_idle_worker_cannot_delete_plan_during_final_submission_window(
    tmp_path: Path,
):
    selected = _assignment("a1")
    client = RefillClient([selected])
    _configure(tmp_path, [selected], refill_to=1, max_tasks=2)

    client.active.clear()  # server accepted the upload; local marker is next
    refill.complete_if_empty(tmp_path, held=0)
    assert refill.load(tmp_path) is not None

    assert refill.mark_submitted(tmp_path, selected["assignment_id"]) == 0
    result = refill.refill_once(tmp_path, client)
    assert result["claimed"] == 1


def test_paid_api_assignment_requires_explicit_cost_budget(tmp_path: Path):
    assignment = {
        **_assignment("deepseek"),
        "provider": "deepseek",
        "billing_mode": "api",
        "est_quota_pct": None,
        "tier_windows_usd": None,
        "cost_usd": 0.75,
    }
    with pytest.raises(refill.RefillError, match="estimated USD limit"):
        _configure(tmp_path, [assignment])
    plan = _configure(
        tmp_path, [assignment], max_tasks=3,
        max_estimated_cost_usd=2.0,
    )
    assert plan["max_estimated_cost_usd"] == 2.0
    assert plan["assignments"]["deepseek"]["estimated_cost_usd"] == 0.75


def test_paid_api_seed_cannot_cross_cost_budget(tmp_path: Path):
    assignment = {
        **_assignment("deepseek"),
        "billing_mode": "api",
        "cost_usd": 1.25,
    }
    with pytest.raises(refill.RefillError, match="exceed the estimated USD limit"):
        _configure(
            tmp_path, [assignment], max_tasks=3,
            max_estimated_cost_usd=1.0,
        )


@pytest.mark.parametrize(
    "quote", [None, float("nan"), float("inf"), float("-inf"), 0, -0.01, True],
)
def test_paid_api_seed_quote_must_be_positive_finite_numeric(
    tmp_path: Path, quote,
):
    assignment = {
        **_assignment("deepseek"),
        "billing_mode": "api",
        "cost_usd": quote,
    }
    with pytest.raises(refill.RefillError, match="valid server cost quote"):
        _configure(
            tmp_path, [assignment], max_tasks=3,
            max_estimated_cost_usd=2.0,
        )


@pytest.mark.parametrize("limit", [0, -1, float("nan"), float("inf")])
def test_paid_api_cost_budget_must_be_positive_and_finite(
    tmp_path: Path, limit: float,
):
    with pytest.raises(refill.RefillError, match="finite and greater than zero"):
        _configure(tmp_path, [], max_estimated_cost_usd=limit)


def test_paid_api_assignment_is_not_offered_interactive_refill(
    monkeypatch,
):
    assignment = {
        **_assignment("deepseek"),
        "provider": "deepseek",
        "billing_mode": "api",
    }
    args = argparse.Namespace(refill=False, yes=False)
    monkeypatch.setattr(
        "builtins.input",
        lambda *_args: pytest.fail("paid API must not show the refill prompt"),
    )
    assert runloop._setup_refill(
        args, object(), [assignment], free_pick=True,
    ) == [assignment]


def test_refill_reserves_a_hard_total_and_naturally_drains(tmp_path: Path):
    client = RefillClient([_assignment("a1"), _assignment("a2")])
    _configure(tmp_path, client.active, refill_to=2, max_tasks=3)
    first = client.active.pop(0)
    refill.mark_submitted(tmp_path, first["assignment_id"])
    result = refill.refill_once(tmp_path, client)
    assert result["claimed"] == 0
    assert result["seed_pending"] == 1

    second = client.active.pop(0)
    refill.mark_submitted(tmp_path, second["assignment_id"])
    result = refill.refill_once(tmp_path, client)
    assert result["claimed"] == 1
    assert len(refill.load(tmp_path)["assignments"]) == 3

    client.active.pop(0)  # auto task submitted; task cap is fully reserved
    result = refill.refill_once(tmp_path, client)
    assert result["claimed"] == 0
    assert result["status"] == "draining"


def test_estimated_quota_cap_prevents_an_expensive_refill(tmp_path: Path):
    client = RefillClient([_assignment("a1", pct=2.0)])
    _configure(
        tmp_path, client.active, refill_to=2, max_tasks=5,
        max_estimated_quota_pct=2.5,
    )
    submitted = client.active.pop(0)
    refill.mark_submitted(tmp_path, submitted["assignment_id"])
    result = refill.refill_once(tmp_path, client)
    assert result["claimed"] == 0
    assert result["status"] == "draining"
    assert client.claimed == []


def test_missing_pro_tier_conversion_stops_with_explicit_reason(tmp_path: Path):
    client = RefillClient([])
    _configure(
        tmp_path, [], refill_to=2, max_tasks=5,
        quota_tier="pro-20x", max_estimated_quota_pct=5.0,
    )

    result = refill.refill_once(tmp_path, client)

    assert result["claimed"] == 0
    assert result["status"] == "stopped"
    assert "lack pro-20x quota conversion data" in result["reason"]
    assert client.claimed == []


def test_parallel_workers_share_one_atomic_refill_target(tmp_path: Path):
    client = RefillClient([])
    _configure(tmp_path, [], refill_to=5, max_tasks=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _n: refill.refill_once(tmp_path, client), range(2)))
    assert sum(r["claimed"] for r in results) == 5
    assert len(client.active) == 5
    assert len(refill.load(tmp_path)["assignments"]) == 5


def test_stopped_plan_never_claims_again(tmp_path: Path):
    client = RefillClient([])
    _configure(tmp_path, [], refill_to=2, max_tasks=5)
    refill.stop(tmp_path, "test stop")
    assert refill.refill_once(tmp_path, client)["claimed"] == 0
    assert client.claimed == []
    assert refill.load(tmp_path) is None


def test_mismatched_plan_requires_explicit_safe_replacement(tmp_path: Path):
    active = [_assignment("a1")]
    old = _configure(
        tmp_path, active, refill_to=1, max_tasks=5,
        max_estimated_quota_pct=2,
    )
    values = dict(
        volunteer_id="v1", refill_to=2, max_tasks=5, quota_tier="plus",
        max_estimated_quota_pct=4, active=active,
    )

    with pytest.raises(refill.RefillError, match="different limits"):
        refill.configure(tmp_path, **values)

    new = refill.configure(tmp_path, **values, replace_existing=True)
    assert new["plan_id"] != old["plan_id"]
    assert new["replaced_plan_id"] == old["plan_id"]
    assert new["refill_to"] == 2
    assert new["max_estimated_quota_pct"] == 4
    assert set(new["assignments"]) == {"a1"}


def test_web_added_tasks_cannot_silently_push_plan_past_hard_cap(tmp_path: Path):
    initial = [_assignment("a1")]
    client = RefillClient(initial + [_assignment("a2"), _assignment("a3")])
    _configure(tmp_path, initial, refill_to=2, max_tasks=2)
    result = refill.refill_once(tmp_path, client)
    assert result["status"] == "stopped"
    assert "beyond max_tasks" in result["reason"]
    assert client.claimed == []


def test_setup_clamps_target_to_server_claim_limit(tmp_path: Path, monkeypatch, capsys):
    client = RefillClient([_assignment("a1")])
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    args = argparse.Namespace(
        refill=True, refill_to=50, auto=None, yes=True, max_tasks=3,
        quota_tier="plus", max_estimated_quota_pct=None,
    )
    active = runloop._setup_refill(args, client, client.active, True)
    assert len(active) == 1
    assert client.claimed == []
    plan = refill.load(tmp_path)
    assert plan["refill_to"] == 3
    assert "using 3" in capsys.readouterr().out


def test_setup_allows_bounded_exact_codebuddy_refill(
    tmp_path: Path, monkeypatch,
):
    selected = {
        **_assignment("hy4-seed"),
        "agent": CODEBUDDY_AGENT,
        "provider": CODEBUDDY_PROVIDER,
        "model": CODEBUDDY_MODEL,
        "effort": "high",
        "billing_mode": "subscription",
    }
    client = RefillClient([selected])
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    args = argparse.Namespace(
        refill=True, refill_to=1, auto=None, yes=True, max_tasks=5,
        quota_tier="plus", max_estimated_quota_pct=None,
        refill_harness=CODEBUDDY_AGENT, refill_model=CODEBUDDY_MODEL,
        refill_effort="high", refill_order="cost", parallel=False,
        fleet_pool=False,
    )

    active = runloop._setup_refill(args, client, [selected], True)

    assert active == [selected]
    assert client.claimed == []
    plan = refill.load(tmp_path)
    assert plan["status"] == "active"
    assert plan["seed_assignment_ids"] == [selected["assignment_id"]]
    assert plan["submitted_seed_assignment_ids"] == []
    assert plan["max_tasks"] == 5
    assert (
        plan["refill_harness"], plan["refill_model"], plan["refill_effort"]
    ) == (CODEBUDDY_AGENT, CODEBUDDY_MODEL, "high")


def test_fleet_setup_registers_server_authoritative_seed_campaign(
    tmp_path: Path, monkeypatch,
):
    batch_id = "550e8400e29b41d4a716446655440000"
    selected = {
        **_assignment("seed"),
        "batch_id": batch_id,
        "agent": "kimi-code",
        "model": "k3",
        "effort": "high",
    }

    class Client(RefillClient):
        def __init__(self):
            super().__init__([selected])
            self.configured = []

        def configure_refill_campaign(self, **values):
            self.configured.append(values)
            return {"campaign": {"batch_id": values["batch_id"]}}

        def refill_campaign_status(self, requested_batch_id):
            return {
                "campaign": {
                    "batch_id": requested_batch_id,
                    "status": "active",
                    "harness": "kimi-code",
                    "model": "k3",
                    "effort": "high",
                    "refill_to": 1,
                    "max_tasks": 3,
                    "planned": 1,
                    "held": 1,
                    "seed_pending": 1,
                    "stop_reason": None,
                },
            }

    client = Client()
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setenv(refill.PLAN_SCOPE_ENV, batch_id)
    args = argparse.Namespace(
        refill=True, refill_to=1, auto=None, yes=True, max_tasks=3,
        quota_tier="plus", max_estimated_quota_pct=None,
        refill_harness="kimi-code", refill_model="k3", refill_effort="high",
        refill_order="cost", parallel=False, fleet_pool=True,
        batch_id=batch_id,
    )

    active = runloop._setup_refill(args, client, [selected], True)

    assert active == [selected]
    assert client.configured == [{
        "batch_id": batch_id,
        "harness": "kimi-code",
        "model": "k3",
        "effort": "high",
        "refill_to": 1,
        "max_tasks": 3,
    }]
    assert refill.load(tmp_path)["server_campaign_id"] == batch_id


def test_normal_setup_replaces_stale_plan_before_refilling(
    tmp_path: Path, monkeypatch, capsys,
):
    active = [_assignment("a1")]
    client = RefillClient(active)
    old = _configure(
        tmp_path, active, refill_to=1, max_tasks=5,
        max_estimated_quota_pct=2,
    )
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    args = argparse.Namespace(
        refill=True, refill_to=2, auto=None, yes=True, max_tasks=5,
        quota_tier="plus", max_estimated_quota_pct=4, parallel=False,
    )

    refreshed = runloop._setup_refill(args, client, active, True)

    plan = refill.load(tmp_path)
    assert len(refreshed) == 1
    assert client.claimed == []
    assert plan["plan_id"] != old["plan_id"]
    assert plan["replaced_plan_id"] == old["plan_id"]
    assert plan["max_estimated_quota_pct"] == 4
    assert "replaced a stale earlier refill configuration" in capsys.readouterr().out


def test_manual_parallel_setup_cannot_replace_another_plan(
    tmp_path: Path, monkeypatch,
):
    active = [_assignment("a1")]
    client = RefillClient(active)
    old = _configure(
        tmp_path, active, refill_to=1, max_tasks=5,
        max_estimated_quota_pct=2,
    )
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    args = argparse.Namespace(
        refill=True, refill_to=2, auto=None, yes=True, max_tasks=5,
        quota_tier="plus", max_estimated_quota_pct=4, parallel=True,
    )

    with pytest.raises(refill.RefillError, match="different limits"):
        runloop._setup_refill(args, client, active, True)

    assert refill.load(tmp_path)["plan_id"] == old["plan_id"]
    assert client.claimed == []


def test_interactive_refill_only_asks_user_for_quota_cap(
    tmp_path: Path, monkeypatch, capsys,
):
    client = RefillClient([_assignment("a1")])
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    answers = iter(("y", "", "", "5", "y"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    args = argparse.Namespace(
        refill=False, refill_to=None, auto=None, yes=False, max_tasks=None,
        quota_tier="plus", max_estimated_quota_pct=None,
    )

    active = runloop._setup_refill(args, client, client.active, True)

    assert active == client.active
    assert args.max_tasks == runloop.DEFAULT_REFILL_TASK_SAFETY_CAP
    assert args.max_estimated_quota_pct == 5
    out = capsys.readouterr().out
    assert "internal task safety cap" in out


def test_invalid_new_setup_cannot_stop_existing_shared_plan(
    tmp_path: Path, monkeypatch,
):
    active = [_assignment("a1")]
    client = RefillClient(active)
    _configure(tmp_path, active, refill_to=1, max_tasks=3)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_acquire_batch", lambda *_a, **_k: (active, True))
    monkeypatch.setattr(
        runloop, "_setup_refill",
        lambda *_a, **_k: (_ for _ in ()).throw(
            refill.RefillError("held queue target must be a positive integer")
        ),
    )
    monkeypatch.setattr(runloop, "_run_checkout_loop", lambda *_a, **_k: 0)
    args = argparse.Namespace(
        yes=True, pick=None, auto=None, refill=True, resume=True,
    )

    assert runloop._go_menu(args, {}, client, tmp_path) == 0
    plan = refill.load(tmp_path)
    assert plan["status"] == "active"
    assert plan["stop_reason"] is None


def test_non_submitted_outcome_stops_shared_plan(tmp_path: Path, monkeypatch):
    from test_checkout import CheckoutClient, _cell
    from test_go_menu import _args

    assignment = _cell("bad")
    client = CheckoutClient(
        {"active": [assignment], "free_pick": True},
        [{"assignment": assignment, "held": 1, "unstarted": 0}],
    )
    _configure(tmp_path, [assignment], refill_to=1, max_tasks=3)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    monkeypatch.setattr(runloop, "_run_and_submit", lambda *a, **kw: "failed")
    args = _args()
    args.refill = True
    assert runloop._run_checkout_loop(args, client, tmp_path, [assignment]) == 1
    assert refill.load(tmp_path) is None


def test_successful_task_does_not_refill_when_local_docker_cleanup_failed(
    tmp_path: Path, monkeypatch,
):
    from test_go_menu import _args

    first = _assignment("a1")
    first.update(
        agent="codex", expires_at="2099-01-01T00:00:00Z",
        deep_swe_commit=None,
    )
    client = LoopClient([first])
    _configure(tmp_path, [first], refill_to=1, max_tasks=3)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)

    def run(_client, assignment, _tasks_root, args, *_a, **_kw):
        client.submit_locally(assignment["assignment_id"])
        args._docker_cleanup_blocked = "题目镜像未能删除"
        return "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    args = _args()
    args.refill = True

    assert runloop._run_checkout_loop(args, client, tmp_path, [first]) == 0
    assert client.claimed == []
    assert refill.load(tmp_path) is None


def test_checkout_loop_refills_until_hard_cap_then_drains(
    tmp_path: Path, monkeypatch,
):
    from test_go_menu import _args

    first = _assignment("a1")
    first.update(agent="codex", expires_at="2099-01-01T00:00:00Z",
                 deep_swe_commit=None)
    client = LoopClient([first])
    _configure(tmp_path, [first], refill_to=1, max_tasks=3)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    monkeypatch.setattr(runloop, "_disk_allows_refill", lambda _cfg: True)
    ran = []

    def run(_client, assignment, *_a, **_kw):
        ran.append(assignment["assignment_id"])
        client.submit_locally(assignment["assignment_id"])
        return "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    args = _args()
    args.refill = True
    assert runloop._run_checkout_loop(args, client, tmp_path, [first]) == 0
    assert len(ran) == 3
    assert len(client.claimed) == 2
    assert refill.load(tmp_path) is None


def test_long_running_worker_periodically_maintains_image_cache(
    tmp_path: Path, monkeypatch,
):
    from test_go_menu import _args

    first = _assignment("a1")
    first.update(agent="codex", expires_at="2099-01-01T00:00:00Z",
                 deep_swe_commit=None)
    client = LoopClient([first])
    _configure(tmp_path, [first], refill_to=1, max_tasks=1)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    monkeypatch.setattr(runloop, "_load_config", lambda: {"image_cache_mode": "balanced"})
    monkeypatch.setattr(runloop, "_disk_allows_refill", lambda _cfg: True)
    monkeypatch.setattr(
        runloop.image_cache, "claim_periodic_maintenance", lambda *_a, **_k: True,
    )
    maintenance = []
    monkeypatch.setattr(
        runloop, "_maintain_image_cache",
        lambda _client, cfg, *, phase: maintenance.append((cfg, phase)) or True,
    )

    def run(_client, assignment, *_a, **_kw):
        client.submit_locally(assignment["assignment_id"])
        return "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    args = _args()
    args.refill = True
    args.worker_child = True

    assert runloop._run_checkout_loop(args, client, tmp_path, [first]) == 0
    assert maintenance == [({"image_cache_mode": "balanced"}, "during worker pool")]


def test_two_workers_keep_draining_after_total_claim_cap(
    tmp_path: Path, monkeypatch,
):
    """Regression for the live 2-running + 1-waiting idle-slot incident."""
    from test_go_menu import _args

    initial = [_assignment("a1"), _assignment("a2"), _assignment("a3")]
    for assignment in initial:
        assignment.update(
            agent="codex", expires_at="2099-01-01T00:00:00Z",
            deep_swe_commit=None,
        )
    client = LoopClient(initial)
    _configure(tmp_path, initial, refill_to=3, max_tasks=5)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    monkeypatch.setattr(runloop, "_disk_allows_refill", lambda _cfg: True)
    running = 0
    peak_running = 0
    completed = []
    auto_started_before_seed_complete = []
    state_lock = threading.Lock()
    both_started = threading.Barrier(2)

    def run(_client, assignment, *_a, **_kw):
        nonlocal running, peak_running
        with state_lock:
            if (assignment["assignment_id"].startswith("a-new-")
                    and not {"a1", "a2", "a3"}.issubset(completed)):
                auto_started_before_seed_complete.append(assignment["assignment_id"])
            running += 1
            peak_running = max(peak_running, running)
        # Synchronize only the first pair. Later tasks should be consumed by
        # whichever worker becomes free without serializing the whole test.
        if assignment["assignment_id"] in {"a1", "a2"}:
            both_started.wait(5)
        with state_lock:
            completed.append(assignment["assignment_id"])
            running -= 1
        client.submit_locally(assignment["assignment_id"])
        return "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)

    def worker():
        args = _args()
        args.refill = True
        return runloop._run_checkout_loop(args, client, tmp_path, initial)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _n: worker(), range(2)))

    assert results == [0, 0]
    assert peak_running == 2
    assert len(completed) == 5
    assert len(set(completed)) == 5
    assert len(client.claimed) == 2
    assert auto_started_before_seed_complete == []
    assert client.active == []
    assert refill.load(tmp_path) is None
