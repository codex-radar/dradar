import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dradar import cli, refill, runloop
from dradar.api_client import ApiError
from dradar.codebuddy_provider import (
    CODEBUDDY_AGENT,
    CODEBUDDY_MODEL,
    CODEBUDDY_PROVIDER,
)
from dradar.providers import (
    GROK_AGENT,
    KIMI_AGENT,
    KIMI_PROVIDER,
    REFILL_HARNESS_PROVIDERS,
    ZCODE_AGENT,
    ZCODE_PROVIDER,
    normalize_refill_harness,
    validate_refill_scope,
)


WINDOWS = {"plus": 10.0, "pro-5x": 50.0, "pro-20x": 200.0}


def _assignment(aid: str, *, agent=KIMI_AGENT, model="k3", effort="low"):
    return {
        "assignment_id": aid,
        "task_id": f"task-{aid}",
        "agent": agent,
        "model": model,
        "effort": effort,
        "billing_mode": "subscription",
        "est_quota_pct": 1.0,
        "tier_windows_usd": WINDOWS,
    }


def _table(*cells):
    combos = [
        {"agent": KIMI_AGENT, "model": "k3", "effort": effort,
         "billing_mode": "subscription", "manual_only": True}
        for effort in ("low", "high", "max")
    ] + [
        {"agent": ZCODE_AGENT, "model": model, "effort": effort,
         "billing_mode": "subscription", "manual_only": True}
        for model in ("glm-5.3", "glm-5.3-flash")
        for effort in ("low", "high", "max")
    ] + [
        {"agent": GROK_AGENT, "model": "grok-4.6", "effort": effort,
         "billing_mode": "subscription", "manual_only": True}
        for effort in ("low", "medium", "high", "xhigh")
    ] + [
        {"agent": CODEBUDDY_AGENT, "model": CODEBUDDY_MODEL, "effort": effort,
         "billing_mode": "subscription", "manual_only": True}
        for effort in ("low", "high", "max")
    ] + [{"model": "gpt-5.6-sol", "effort": "low"}]
    rows = {}
    for task_id, agent, model, effort, state in cells:
        value = {"st": state, "cost": 0.5}
        if agent != "codex":
            value["agent"] = agent
        rows[f"{task_id}|{model}|{effort}"] = value
    return {"combos": combos, "cells": rows, "tier_windows_usd": WINDOWS}


class ScopedClient:
    def __init__(self, table, active=None, conflicts=()):
        self.board = table
        self.active = list(active or [])
        self.conflicts = set(conflicts)
        self.claimed = []
        self.suggest_calls = []
        self._lock = threading.Lock()

    def get_assignment(self):
        with self._lock:
            return {"active": list(self.active), "free_pick": True}

    def table(self):
        return self.board

    def suggest(self, n):
        self.suggest_calls.append(n)
        return {"cells": [{"task_id": "codex-fallback",
                            "model": "gpt-5.6-sol", "effort": "low"}]}

    def claim_assignment(self, task_id, model, effort):
        with self._lock:
            if task_id in self.conflicts:
                raise ApiError("cell already taken", status_code=409)
            cell = self.board["cells"][f"{task_id}|{model}|{effort}"]
            assignment = _assignment(
                f"a-{task_id}", agent=cell.get("agent", "codex"),
                model=model, effort=effort,
            )
            assignment["task_id"] = task_id
            self.active.append(assignment)
            self.claimed.append((task_id, model, effort))
            return {"assignment": assignment}


def _configure(home: Path, *, harness=KIMI_AGENT, model="k3", effort="low",
               refill_to=3, max_tasks=20, active=None, order="cost"):
    return refill.configure(
        home, volunteer_id="v1", refill_to=refill_to, max_tasks=max_tasks,
        quota_tier="plus", max_estimated_quota_pct=None,
        active=list(active or []), refill_harness=harness,
        refill_model=model, refill_effort=effort, refill_order=order,
    )


def test_kimi_low_scoped_refill_claims_only_kimi_low(tmp_path: Path):
    client = ScopedClient(_table(
        ("k-low-1", KIMI_AGENT, "k3", "low", "open"),
        ("k-low-2", KIMI_AGENT, "k3", "low", "open"),
        ("k-high", KIMI_AGENT, "k3", "high", "open"),
        ("codex", "codex", "gpt-5.6-sol", "low", "open"),
    ))
    _configure(tmp_path, refill_to=2)

    result = refill.refill_once(tmp_path, client)

    assert result["claimed"] == 2
    assert client.claimed == [
        ("k-low-1", "k3", "low"), ("k-low-2", "k3", "low")]
    assert client.suggest_calls == []


def test_scoped_refill_claims_cheapest_candidates_first(tmp_path: Path):
    board = _table(
        ("expensive", KIMI_AGENT, "k3", "low", "open"),
        ("unpriced", KIMI_AGENT, "k3", "low", "open"),
        ("cheap", KIMI_AGENT, "k3", "low", "open"),
        ("middle", KIMI_AGENT, "k3", "low", "open"),
    )
    board["cells"]["expensive|k3|low"]["cost"] = 3.0
    board["cells"]["unpriced|k3|low"].pop("cost")
    board["cells"]["cheap|k3|low"]["cost"] = 0.1
    board["cells"]["middle|k3|low"]["cost"] = 1.0
    client = ScopedClient(board)
    _configure(tmp_path, refill_to=4)

    result = refill.refill_once(tmp_path, client)

    assert result["claimed"] == 4
    assert [task_id for task_id, _model, _effort in client.claimed] == [
        "cheap", "middle", "expensive", "unpriced",
    ]


def test_scoped_refill_can_claim_least_run_candidates_first(tmp_path: Path):
    board = _table(
        ("twice", KIMI_AGENT, "k3", "low", "open"),
        ("never-b", KIMI_AGENT, "k3", "low", "open"),
        ("unknown", KIMI_AGENT, "k3", "low", "open"),
        ("never-a", KIMI_AGENT, "k3", "low", "open"),
    )
    board["cells"]["twice|k3|low"]["n"] = 2
    board["cells"]["never-b|k3|low"]["n"] = 0
    board["cells"]["never-a|k3|low"]["n"] = 0
    client = ScopedClient(board)
    _configure(tmp_path, refill_to=4, order="least-run")

    result = refill.refill_once(tmp_path, client)

    assert result["claimed"] == 4
    assert [task_id for task_id, _model, _effort in client.claimed] == [
        "never-a", "never-b", "twice", "unknown",
    ]


def test_no_kimi_inventory_never_falls_back_to_codex(tmp_path: Path):
    client = ScopedClient(_table(
        ("k-busy", KIMI_AGENT, "k3", "low", "leased"),
        ("codex", "codex", "gpt-5.6-sol", "low", "open"),
    ))
    _configure(tmp_path)

    result = refill.refill_once(tmp_path, client)

    assert result["claimed"] == 0
    assert result["status"] == "active"
    assert result["waiting_for_inventory"] is True
    assert client.claimed == []
    assert client.suggest_calls == []


def test_codex_scoped_discovery_never_claims_paid_api_cells(tmp_path: Path):
    board = _table(("paid", "codex", "gpt-5.6-sol", "low", "open"))
    board["cells"]["paid|gpt-5.6-sol|low"]["billing_mode"] = "api"
    client = ScopedClient(board)
    _configure(
        tmp_path, harness="codex", model="gpt-5.6-sol", effort="low",
        refill_to=1,
    )

    result = refill.refill_once(tmp_path, client)

    assert result["waiting_for_inventory"] is True
    assert client.claimed == []


def test_scoped_refill_skips_409_and_claims_an_alternate(tmp_path: Path):
    client = ScopedClient(_table(
        ("stale", KIMI_AGENT, "k3", "low", "open"),
        ("fresh", KIMI_AGENT, "k3", "low", "open"),
    ), conflicts={"stale"})
    _configure(tmp_path, refill_to=1)

    result = refill.refill_once(tmp_path, client)

    assert result["claimed"] == 1
    assert client.claimed == [("fresh", "k3", "low")]


def test_all_scoped_409s_wait_without_fallback(tmp_path: Path):
    client = ScopedClient(_table(
        ("stale", KIMI_AGENT, "k3", "low", "open"),
    ), conflicts={"stale"})
    _configure(tmp_path, refill_to=1)

    result = refill.refill_once(tmp_path, client)

    assert result["waiting_for_inventory"] is True
    assert client.claimed == []
    assert client.suggest_calls == []


def test_server_cannot_silently_return_a_cross_harness_assignment(tmp_path: Path):
    class BadAckClient(ScopedClient):
        def claim_assignment(self, task_id, model, effort):
            assignment = _assignment(
                "wrong", agent="codex", model="gpt-5.6-sol", effort="low")
            return {"assignment": assignment}

    client = BadAckClient(_table(
        ("wanted", KIMI_AGENT, "k3", "low", "open"),
    ))
    _configure(tmp_path, refill_to=1)

    result = refill.refill_once(tmp_path, client)

    assert result["status"] == "stopped"
    assert "outside the configured refill scope" in result["reason"]
    assert refill.load(tmp_path)["assignments"] == {}


@pytest.mark.parametrize(
    ("alias", "agent"),
    [("kimi", KIMI_AGENT), ("KIMI_CODE", KIMI_AGENT),
     ("grok", GROK_AGENT), ("grok-build", GROK_AGENT),
     ("codebuddy", CODEBUDDY_AGENT), ("HY4", CODEBUDDY_AGENT),
     ("zcode", ZCODE_AGENT)],
)
def test_harness_aliases_resolve_to_provider_wire_values(alias, agent):
    assert normalize_refill_harness(alias) == agent


@pytest.mark.parametrize(
    ("harness", "model", "effort", "message"),
    [("kimi-code", "glm-5.3", "low", "supports model"),
     ("zcode", "glm-5.3", "xhigh", "supports effort"),
     ("grok", "grok-4.6", "max", "supports effort"),
     ("codebuddy", "hy4", "low", "supports model"),
     ("codebuddy", CODEBUDDY_MODEL, "medium", "supports effort")],
)
def test_subscription_scope_rejects_model_or_effort_mismatch(
    harness, model, effort, message,
):
    with pytest.raises(ValueError, match=message):
        validate_refill_scope(harness, model, effort)


@pytest.mark.parametrize(
    ("harness", "model", "effort", "agent"),
    [("zcode", "glm-5.3", "max", ZCODE_AGENT),
     ("zcode", "glm-5.3-flash", "high", ZCODE_AGENT),
     ("grok", "grok-4.6", "xhigh", GROK_AGENT)],
)
def test_zcode_and_grok_scoped_refill(harness, model, effort, agent, tmp_path):
    client = ScopedClient(_table(("wanted", agent, model, effort, "open")))
    _configure(tmp_path, harness=harness, model=model, effort=effort, refill_to=1)

    assert refill.refill_once(tmp_path, client)["claimed"] == 1
    assert client.claimed == [("wanted", model, effort)]


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_codebuddy_scoped_refill_claims_only_codebuddy(
    effort: str, tmp_path: Path,
):
    client = ScopedClient(_table(
        ("wanted", CODEBUDDY_AGENT, CODEBUDDY_MODEL, effort, "open"),
        ("other", KIMI_AGENT, "k3", effort, "open"),
    ))
    _configure(
        tmp_path, harness="codebuddy", model=CODEBUDDY_MODEL,
        effort=effort, refill_to=1,
    )

    result = refill.refill_once(tmp_path, client)

    assert result["claimed"] == 1
    assert client.claimed == [("wanted", CODEBUDDY_MODEL, effort)]
    assert refill.load(tmp_path)["refill_harness"] == CODEBUDDY_AGENT
    assert REFILL_HARNESS_PROVIDERS[CODEBUDDY_AGENT] == CODEBUDDY_PROVIDER


def test_parallel_scoped_workers_share_one_atomic_target(tmp_path: Path):
    client = ScopedClient(_table(*[
        (f"k-{i}", KIMI_AGENT, "k3", "low", "open") for i in range(8)
    ]))
    _configure(tmp_path, refill_to=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _n: refill.refill_once(tmp_path, client), range(2)))

    assert sum(result["claimed"] for result in results) == 5
    assert len(client.claimed) == 5
    assert len(refill.load(tmp_path)["assignments"]) == 5


def test_scoped_seed_barrier_still_blocks_every_auto_claim(tmp_path: Path):
    seeds = [_assignment("seed-1"), _assignment("seed-2")]
    client = ScopedClient(_table(
        ("next", KIMI_AGENT, "k3", "low", "open"),
    ), active=seeds)
    _configure(tmp_path, refill_to=2, active=seeds)

    assert refill.refill_once(tmp_path, client)["seed_pending"] == 2
    client.active.pop(0)
    refill.mark_submitted(tmp_path, "seed-1")
    assert refill.refill_once(tmp_path, client)["seed_pending"] == 1
    assert client.claimed == []


def test_scoped_plan_ignores_other_harness_assignments_at_configuration(
    tmp_path: Path,
):
    wanted = _assignment("wanted")
    unrelated = [
        _assignment("zcode", agent=ZCODE_AGENT,
                    model="glm-5.3-flash", effort="high"),
        _assignment("codex", agent="codex",
                    model="gpt-5.6-luna", effort="high"),
    ]

    plan = _configure(
        tmp_path, refill_to=2, max_tasks=2,
        active=[wanted, *unrelated],
    )

    assert plan["seed_assignment_ids"] == ["wanted"]
    assert set(plan["assignments"]) == {"wanted"}


def test_scoped_reconcile_counts_only_its_harness_active_assignments(
    tmp_path: Path,
):
    wanted = _assignment("wanted")
    unrelated = [
        _assignment("zcode", agent=ZCODE_AGENT,
                    model="glm-5.3-flash", effort="high"),
        _assignment("codex", agent="codex",
                    model="gpt-5.6-luna", effort="high"),
    ]
    client = ScopedClient(_table(
        ("next", KIMI_AGENT, "k3", "low", "open"),
    ), active=[wanted, *unrelated])
    _configure(tmp_path, refill_to=2, max_tasks=3, active=[wanted])
    refill.mark_submitted(tmp_path, "wanted")

    result = refill.refill_once(tmp_path, client)

    assert result["claimed"] == 1
    assert result["held"] == 2
    assert client.claimed == [("next", "k3", "low")]
    assert set(refill.load(tmp_path)["assignments"]) == {
        "wanted", "a-next",
    }


def test_scoped_plan_persists_scope_and_conflicts_safely(tmp_path: Path):
    first = _configure(tmp_path)
    stored = refill.load(tmp_path)
    assert stored["refill_harness"] == KIMI_AGENT
    assert stored["refill_model"] == "k3"
    assert stored["refill_effort"] == "low"

    with pytest.raises(refill.RefillError, match="different limits"):
        _configure(tmp_path, effort="high")
    assert refill.load(tmp_path)["plan_id"] == first["plan_id"]


def test_scoped_automatic_stop_preserves_count_across_restart(tmp_path: Path):
    active = [_assignment("seed")]
    first = _configure(tmp_path, active=active, max_tasks=3, refill_to=1)
    refill.stop(tmp_path, "provider quota exhausted")

    stopped = refill.load(tmp_path)
    assert stopped["status"] == "stopped"
    assert len(stopped["assignments"]) == 1

    resumed = _configure(tmp_path, active=active, max_tasks=3, refill_to=1)
    assert resumed["plan_id"] == first["plan_id"]
    assert resumed["status"] == "active"
    assert len(resumed["assignments"]) == 1


def test_checkpoint_fault_circuit_survives_restart_and_scope_variants(
    tmp_path: Path,
) -> None:
    assignment = _assignment(
        "bad", agent=ZCODE_AGENT, model="glm-5.3", effort="high",
    )
    assignment["provider"] = ZCODE_PROVIDER
    first = _configure(
        tmp_path, harness=ZCODE_AGENT, model="glm-5.3", effort="high",
        active=[assignment], refill_to=1, max_tasks=20,
    )

    faulted = refill.open_circuit(
        tmp_path, assignment, "checkpoint_invalid",
    )

    assert faulted is not None
    assert faulted["plan_id"] == first["plan_id"]
    assert faulted["status"] == refill.FAULTED_STATE
    assert faulted["circuit"]["state"] == "open"
    assert faulted["circuit"]["volunteer_id"] == "v1"
    assert faulted["circuit"]["harness"] == ZCODE_AGENT
    assert faulted["circuit"]["provider"] == ZCODE_PROVIDER
    assert faulted["circuit"]["failure_family"] == "checkpoint_invalid"
    assert faulted["circuit"]["observation_count"] == 1
    assert faulted["circuit"]["assignment_id"] == "bad"

    class NoNetworkClient:
        def __getattr__(self, name):
            raise AssertionError(f"faulted refill used network method {name}")

    assert refill.refill_once(tmp_path, NoNetworkClient()) == {
        "status": refill.FAULTED_STATE,
        "claimed": 0,
    }
    with pytest.raises(refill.RefillError, match="circuit is open"):
        _configure(
            tmp_path, harness=ZCODE_AGENT, model="glm-5.3", effort="low",
            active=[], refill_to=1, max_tasks=20,
        )
    assert refill.load(tmp_path)["plan_id"] == first["plan_id"]


def test_checkpoint_fault_circuit_is_atomic_and_does_not_cross_harness(
    tmp_path: Path,
) -> None:
    zcode = _assignment(
        "zcode", agent=ZCODE_AGENT, model="glm-5.3", effort="low",
    )
    zcode["provider"] = ZCODE_PROVIDER
    _configure(
        tmp_path, harness=ZCODE_AGENT, model="glm-5.3", effort="low",
        active=[zcode], refill_to=1,
    )
    kimi = _assignment("kimi")
    kimi["provider"] = KIMI_PROVIDER
    assert refill.open_circuit(
        tmp_path, kimi, "checkpoint_invalid",
    ) is None
    assert refill.load(tmp_path)["status"] == "active"

    wrong_provider = dict(zcode, provider=KIMI_PROVIDER)
    assert refill.open_circuit(
        tmp_path, wrong_provider, "checkpoint_invalid",
    ) is None
    assert refill.load(tmp_path)["status"] == "active"

    with ThreadPoolExecutor(max_workers=4) as pool:
        latched = list(pool.map(
            lambda _n: refill.open_circuit(
                tmp_path, zcode, "checkpoint_invalid",
            ),
            range(4),
        ))

    assert all(plan and plan["status"] == refill.FAULTED_STATE for plan in latched)
    circuit = refill.load(tmp_path)["circuit"]
    assert circuit["observation_count"] == 4
    assert circuit["harness"] == ZCODE_AGENT
    assert circuit["provider"] == ZCODE_PROVIDER


def test_explicit_refill_stop_rearms_checkpoint_faulted_campaign(
    tmp_path: Path,
) -> None:
    assignment = _assignment(
        "bad", agent=ZCODE_AGENT, model="glm-5.3", effort="low",
    )
    assignment["provider"] = ZCODE_PROVIDER
    _configure(
        tmp_path, harness=ZCODE_AGENT, model="glm-5.3", effort="low",
        active=[assignment], refill_to=1,
    )
    refill.open_circuit(tmp_path, assignment, "checkpoint_invalid")

    refill.stop(tmp_path, "stopped by user", discard=True)
    assert refill.load(tmp_path) is None
    rearmed = _configure(
        tmp_path, harness=ZCODE_AGENT, model="glm-5.3", effort="low",
        active=[], refill_to=1,
    )
    assert rearmed["status"] == "active"
    assert "circuit" not in rearmed


def test_codebuddy_false_success_circuit_persists_until_explicit_rearm(
        tmp_path: Path) -> None:
    assignment = _assignment(
        "codebuddy-empty",
        agent=CODEBUDDY_AGENT,
        model=CODEBUDDY_MODEL,
        effort="max",
    )
    assignment["provider"] = CODEBUDDY_PROVIDER
    configured = _configure(
        tmp_path,
        harness=CODEBUDDY_AGENT,
        model=CODEBUDDY_MODEL,
        effort="max",
        active=[assignment],
        refill_to=1,
    )

    faulted = refill.open_circuit(
        tmp_path, assignment, "provider_false_success",
    )

    assert faulted is not None
    assert faulted["plan_id"] == configured["plan_id"]
    assert faulted["status"] == refill.FAULTED_STATE
    assert faulted["circuit"]["failure_family"] == "provider_false_success"
    assert faulted["circuit"]["harness"] == CODEBUDDY_AGENT
    assert faulted["circuit"]["provider"] == CODEBUDDY_PROVIDER
    assert refill.refill_once(tmp_path, object()) == {
        "status": refill.FAULTED_STATE,
        "claimed": 0,
    }

    refill.stop(tmp_path, "provider repaired", discard=True)
    assert refill.load(tmp_path) is None


def test_scoped_plan_cannot_reset_count_by_changing_cap(tmp_path: Path):
    active = [_assignment("seed")]
    first = _configure(tmp_path, active=active, max_tasks=3, refill_to=1)
    refill.stop(tmp_path, "provider quota exhausted")

    with pytest.raises(refill.RefillError, match="refill stop"):
        refill.configure(
            tmp_path, volunteer_id="v1", refill_to=1, max_tasks=30,
            quota_tier="plus", max_estimated_quota_pct=None, active=active,
            refill_harness=KIMI_AGENT, refill_model="k3",
            refill_effort="low", replace_existing=True,
        )
    assert refill.load(tmp_path)["plan_id"] == first["plan_id"]
    assert len(refill.load(tmp_path)["assignments"]) == 1


def test_scoped_max_tasks_remains_draining_after_queue_empties_and_restart(
    tmp_path: Path,
):
    active = [_assignment("only")]
    first = _configure(tmp_path, active=active, max_tasks=1, refill_to=1)
    refill.mark_submitted(tmp_path, "only")
    client = ScopedClient(_table(), active=[])
    result = refill.refill_once(tmp_path, client)
    assert result["status"] == "draining"

    refill.complete_if_empty(tmp_path, held=0)
    persisted = refill.load(tmp_path)
    assert persisted["plan_id"] == first["plan_id"]
    assert persisted["status"] == "draining"

    same = _configure(tmp_path, active=[], max_tasks=1, refill_to=1)
    assert same["plan_id"] == first["plan_id"]
    assert refill.refill_once(tmp_path, client)["claimed"] == 0


def test_explicit_refill_stop_is_the_only_scoped_campaign_reset(tmp_path: Path):
    _configure(tmp_path)
    refill.stop(tmp_path, "user stopped", discard=True)
    assert refill.load(tmp_path) is None


def test_legacy_unscoped_plan_remains_compatible(tmp_path: Path):
    refill.configure(
        tmp_path, volunteer_id="v1", refill_to=1, max_tasks=2,
        quota_tier="plus", max_estimated_quota_pct=None, active=[],
    )
    plan = refill.load(tmp_path)
    for key in ("refill_harness", "refill_model", "refill_effort"):
        plan.pop(key)
    (tmp_path / refill.PLAN_FILE).write_text(json.dumps(plan))

    same = refill.configure(
        tmp_path, volunteer_id="v1", refill_to=1, max_tasks=2,
        quota_tier="plus", max_estimated_quota_pct=None, active=[],
    )
    assert same["plan_id"] == plan["plan_id"]


def test_legacy_plan_can_adopt_scope_without_resetting_task_count(tmp_path: Path):
    active = [_assignment("old-1"), _assignment("old-2")]
    legacy = refill.configure(
        tmp_path, volunteer_id="v1", refill_to=2, max_tasks=30,
        quota_tier="plus", max_estimated_quota_pct=None, active=active,
    )

    migrated = _configure(
        tmp_path, active=active, refill_to=2, max_tasks=30,
    )

    assert migrated["plan_id"] == legacy["plan_id"]
    assert len(migrated["assignments"]) == 2
    assert migrated["refill_harness"] == KIMI_AGENT
    assert migrated["scope_migrated_from_legacy"] is True


def test_legacy_plan_with_different_limit_refuses_scoped_reset(tmp_path: Path):
    active = [_assignment("old")]
    refill.configure(
        tmp_path, volunteer_id="v1", refill_to=1, max_tasks=1000,
        quota_tier="plus", max_estimated_quota_pct=None, active=active,
    )

    with pytest.raises(refill.RefillError, match="older refill plan"):
        _configure(tmp_path, active=active, refill_to=1, max_tasks=30)
    assert refill.load(tmp_path)["max_tasks"] == 1000


def test_cli_parses_stable_scoped_refill_flags(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_go", lambda args: seen.append(args) or 0)
    assert cli.main([
        "resume", "-y", "--workers", "3", "--refill", "--refill-to", "3",
        "--max-tasks", "30", "--refill-harness", "kimi-code",
        "--refill-model", "k3", "--refill-effort", "low",
        "--refill-order", "least-run",
    ]) == 0
    args = seen[0]
    assert (args.workers, args.max_tasks, args.refill_harness,
            args.refill_model, args.refill_effort, args.refill_order) == (
                3, 30, "kimi-code", "k3", "low", "least-run")


def test_resume_help_documents_stable_scope_flags(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["resume", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    for flag in ("--refill-harness", "--refill-model", "--refill-effort",
                 "--refill-order", "--max-tasks"):
        assert flag in output
    assert "codebuddy" in output


def test_worker_command_forwards_complete_scoped_plan():
    args = argparse.Namespace(
        keep=False, archive_session=False, allow_task_drift=False,
        dev_agent=None, benchmark=None, refill=True, max_tasks=30,
        refill_to=3, max_estimated_quota_pct=None, quota_tier="plus",
        refill_harness=KIMI_AGENT, refill_model="k3", refill_effort="low",
        refill_order="least-run",
    )
    command = runloop._worker_command(args)
    assert command[-8:] == [
        "--refill-harness", KIMI_AGENT, "--refill-model", "k3",
        "--refill-effort", "low", "--refill-order", "least-run",
    ]


def _run_args(**overrides):
    values = dict(
        workers=3, yes=True, keep=False, allow_task_drift=False,
        dev_agent=None, refill=True, refill_to=3, max_tasks=30,
        max_estimated_quota_pct=None, quota_tier="plus", auto=None, pick=None,
        assignment=None, parallel=False, worker_child=False, resume=True,
        worker_target_file=None, archive_session=False,
        refill_harness="kimi-code", refill_model="k3", refill_effort="low",
        refill_order=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_refill_model_requires_harness_before_runtime_setup():
    with pytest.raises(SystemExit, match="require --refill-harness"):
        runloop.cmd_go(_run_args(refill_harness=None))


def test_subscription_scoped_refill_requires_explicit_total_task_limit():
    with pytest.raises(SystemExit, match="explicit --max-tasks"):
        runloop.cmd_go(_run_args(
            max_tasks=None, max_estimated_quota_pct=10,
        ))


def test_codebuddy_scoped_refill_requires_explicit_total_task_limit():
    with pytest.raises(SystemExit, match="explicit --max-tasks"):
        runloop.cmd_go(_run_args(
            refill_harness="codebuddy", refill_model=CODEBUDDY_MODEL,
            refill_effort="low", max_tasks=None,
            max_estimated_quota_pct=10,
        ))


def test_paid_api_dsh_scoped_refill_is_rejected_before_claiming():
    with pytest.raises(SystemExit, match="paid-API Harness"):
        runloop.cmd_go(_run_args(
            refill_harness="dsh-minimal", refill_model=None,
            refill_effort=None,
        ))
