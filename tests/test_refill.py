import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dradar import refill, runloop


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


def test_paid_api_assignment_cannot_enter_continuous_refill(tmp_path: Path):
    assignment = {
        **_assignment("deepseek"),
        "provider": "deepseek",
        "billing_mode": "api",
        "est_quota_pct": None,
        "tier_windows_usd": None,
    }
    with pytest.raises(refill.RefillError, match="one-off runs"):
        _configure(tmp_path, [assignment])


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
