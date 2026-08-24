"""Persistent, bounded continuous-refill plans shared by local workers.

The plan contains only public assignment metadata and counters.  It never
stores the account token, assignment nonce, patch, trajectory, or Codex data.
All workers under one DRADAR_HOME serialize plan updates through an OS lock.
"""

from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from .api_client import ApiError
from .providers import REFILL_HARNESS_PROVIDERS, validate_refill_scope

SCHEMA_VERSION = 1
PLAN_FILE = "refill-plan.json"
LOCK_FILE = "refill-plan.lock"
RUNNING_STATES = {"active", "draining"}
FAULTED_STATE = "faulted"
CHECKPOINT_FAULT_FAMILIES = frozenset({
    "checkpoint_invalid", "checkpoint_incompatible",
})
TIERS = ("plus", "pro-5x", "pro-20x")


class RefillError(RuntimeError):
    pass


class RefillCircuitOpen(RefillError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(home: Path) -> Path:
    return home / PLAN_FILE


@contextmanager
def _locked(home: Path) -> Iterator[None]:
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
    os.lseek(fd, 0, os.SEEK_SET)
    windows_lock = False
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows CI exercises callers
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            windows_lock = True
        yield
    finally:
        if windows_lock:  # pragma: no cover
            try:
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        os.close(fd)


def _load_unlocked(home: Path) -> dict | None:
    try:
        raw = json.loads(_path(home).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return None
    return raw


def load(home: Path) -> dict | None:
    with _locked(home):
        return _load_unlocked(home)


def resize_target(home: Path, refill_to: int) -> dict | None:
    """Atomically align a live refill queue with its worker-pool target.

    Runtime scale-down may use zero to drain without replacing completed work;
    initial plan creation remains positive-only.  The durable total-task cap
    always wins over a larger worker target.
    """

    if (
        isinstance(refill_to, bool)
        or not isinstance(refill_to, int)
        or refill_to < 0
    ):
        raise RefillError("live refill target must be a non-negative integer")
    with _locked(home):
        plan = _load_unlocked(home)
        if not plan or plan.get("status") not in RUNNING_STATES:
            return None
        try:
            max_tasks = int(plan["max_tasks"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RefillError("saved refill plan has no valid task cap") from exc
        target = min(refill_to, max_tasks)
        if plan.get("refill_to") != target:
            plan["refill_to"] = target
            _save_unlocked(home, plan)
        return dict(plan)


def _save_unlocked(home: Path, plan: dict) -> None:
    plan["updated_at"] = _now()
    path = _path(home)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(plan, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def open_circuit(
    home: Path,
    assignment: dict,
    failure_family: str,
) -> dict | None:
    """Latch a scoped refill plan after a checkpoint infrastructure fault.

    The DRADAR_HOME is already the account boundary.  Persist the remaining
    key dimensions so a restart, CLI upgrade, task change, or effort change
    cannot silently turn the same harness/provider fault back into claims.
    Only the explicit user-facing ``dradar refill stop`` command discards the
    saved plan and rearms a later campaign.
    """

    if failure_family not in CHECKPOINT_FAULT_FAMILIES:
        raise RefillError(f"unsupported refill circuit family: {failure_family}")
    harness = assignment.get("agent") or "codex"
    provider = assignment.get("provider")
    if not isinstance(harness, str) or not harness:
        return None
    if provider is not None and not isinstance(provider, str):
        provider = None
    expected_provider = REFILL_HARNESS_PROVIDERS.get(harness)
    if provider is None:
        provider = expected_provider
    elif expected_provider is not None and provider != expected_provider:
        return None
    with _locked(home):
        plan = _load_unlocked(home)
        if not plan or not plan.get("refill_harness"):
            return None
        if plan.get("refill_harness") != harness:
            return None
        now = _now()
        current = plan.get("circuit")
        same_fault = bool(
            isinstance(current, dict)
            and current.get("state") == "open"
            and current.get("volunteer_id") == plan.get("volunteer_id")
            and current.get("harness") == harness
            and current.get("provider") == provider
            and current.get("failure_family") == failure_family
        )
        if same_fault:
            current["observation_count"] = int(
                current.get("observation_count") or 1,
            ) + 1
            current["last_observed_at"] = now
        elif not (
            isinstance(current, dict) and current.get("state") == "open"
        ):
            plan["circuit"] = {
                "state": "open",
                "volunteer_id": plan.get("volunteer_id"),
                "harness": harness,
                "provider": provider,
                "failure_family": failure_family,
                "opened_at": now,
                "last_observed_at": now,
                "observation_count": 1,
                "assignment_id": assignment.get("assignment_id"),
            }
        plan["status"] = FAULTED_STATE
        plan["stop_reason"] = (
            f"{failure_family} circuit open for {harness}/"
            f"{provider or 'default'}; run `dradar refill stop` after the "
            "checkpoint fault is fixed to explicitly rearm"
        )
        _save_unlocked(home, plan)
        return plan


def _estimate_pct(assignment: dict, tier: str, windows: dict | None) -> float | None:
    plus_pct = assignment.get("est_quota_pct")
    if plus_pct is None:
        return None
    plus_pct = float(plus_pct)
    if tier == "plus":
        return plus_pct
    current = assignment.get("tier_windows_usd") or windows or {}
    plus_window, tier_window = current.get("plus"), current.get(tier)
    if not plus_window or not tier_window:
        return None
    return plus_pct * float(plus_window) / float(tier_window)


def _matches_scope(plan: dict, item: dict) -> bool:
    harness = plan.get("refill_harness")
    if not harness:
        return True
    agent = item.get("agent") or "codex"
    if agent != harness:
        return False
    model = plan.get("refill_model")
    effort = plan.get("refill_effort")
    return (
        (model is None or str(item.get("model", "")).lower() == model)
        and (effort is None or str(item.get("effort", "")).lower() == effort)
    )


def _reserve(plan: dict, assignment: dict, *, enforce_scope: bool = False) -> bool:
    if assignment.get("billing_mode") == "api":
        raise RefillError(
            "paid-API assignments are one-off runs; continuous refill stopped"
        )
    assignment_id = assignment.get("assignment_id")
    if not assignment_id or assignment_id in plan["assignments"]:
        return False
    if enforce_scope and not _matches_scope(plan, assignment):
        raise RefillError(
            "server returned an assignment outside the configured refill scope; "
            "continuous refill stopped"
        )
    windows = assignment.get("tier_windows_usd")
    if windows and not plan.get("tier_windows_usd"):
        plan["tier_windows_usd"] = windows
    estimate = _estimate_pct(
        assignment, plan["quota_tier"], plan.get("tier_windows_usd"))
    if plan.get("max_estimated_quota_pct") is not None and estimate is None:
        raise RefillError(
            f"no {plan['quota_tier']} quota estimate for {assignment.get('task_id', '?')}; "
            "continuous refill stopped before claiming more work"
        )
    plan["assignments"][assignment_id] = {
        "task_id": assignment.get("task_id"),
        "estimated_quota_pct": estimate,
    }
    return True


def _reserved_quota(plan: dict) -> float:
    return sum(
        float(item.get("estimated_quota_pct") or 0)
        for item in plan.get("assignments", {}).values()
    )


def _pending_seed_assignment_ids(plan: dict) -> list[str]:
    """Initial user-selected work still awaiting a confirmed submission.

    Plans created by older clients have no seed metadata. Preserve their
    rolling-refill behavior instead of guessing which already-reserved tasks
    were originally selected and which were auto-claimed later.
    """
    seed_ids = plan.get("seed_assignment_ids")
    if seed_ids is None:
        return []
    submitted = set(plan.get("submitted_seed_assignment_ids", []))
    return [assignment_id for assignment_id in seed_ids
            if assignment_id not in submitted]


def configure(
    home: Path,
    *,
    volunteer_id: str,
    refill_to: int,
    max_tasks: int,
    quota_tier: str,
    max_estimated_quota_pct: float | None,
    active: list[dict],
    refill_harness: str | None = None,
    refill_model: str | None = None,
    refill_effort: str | None = None,
    replace_existing: bool = False,
) -> dict:
    if refill_to < 1 or max_tasks < 1 or max_tasks < len(active):
        raise RefillError("max tasks must be at least the currently held task count")
    if quota_tier not in TIERS:
        raise RefillError(f"unknown quota tier: {quota_tier}")
    if refill_harness is None and (refill_model is not None or refill_effort is not None):
        raise RefillError("refill model/effort filters require a refill harness")
    if refill_harness is not None:
        try:
            refill_harness, refill_model, refill_effort = validate_refill_scope(
                refill_harness, refill_model, refill_effort,
            )
        except ValueError as exc:
            raise RefillError(str(exc)) from exc
    desired = {
        "volunteer_id": volunteer_id,
        "refill_to": refill_to,
        "max_tasks": max_tasks,
        "quota_tier": quota_tier,
        "max_estimated_quota_pct": max_estimated_quota_pct,
        "refill_harness": refill_harness,
        "refill_model": refill_model,
        "refill_effort": refill_effort,
    }
    with _locked(home):
        current = _load_unlocked(home)
        replaced_plan_id = None
        if current and isinstance(current.get("circuit"), dict):
            circuit = current["circuit"]
            if circuit.get("state") == "open":
                fault_scope = "/".join(
                    str(value) for value in (
                        circuit.get("harness"), circuit.get("provider"),
                    ) if value
                ) or "saved scope"
                raise RefillCircuitOpen(
                    f"refill circuit is open for {fault_scope} after "
                    f"{circuit.get('failure_family') or 'a checkpoint fault'}; "
                    "fix the checkpoint path, then run `dradar refill stop` "
                    "to explicitly rearm a new campaign"
                )
        if (current and not current.get("refill_harness")
                and refill_harness is not None
                and current.get("status") in RUNNING_STATES):
            legacy_keys = (
                "volunteer_id", "refill_to", "max_tasks", "quota_tier",
                "max_estimated_quota_pct",
            )
            if any(current.get(key) != desired[key] for key in legacy_keys):
                raise RefillError(
                    "an older refill plan is active with different limits; run "
                    "`dradar refill stop` before starting the scoped campaign"
                )
            # Additive schema migration: keep every historical reservation so
            # upgrading the CLI cannot reset max_tasks accounting mid-campaign.
            current.update({
                "refill_harness": refill_harness,
                "refill_model": refill_model,
                "refill_effort": refill_effort,
                "scope_migrated_from_legacy": True,
            })
        # A scoped subscription campaign's assignment ledger is its hard
        # spend boundary.  Never let a restart or changed argv silently reset
        # that count.  Automatic stops preserve it; an explicit `refill stop`
        # is the deliberate boundary between campaigns.
        if current and current.get("refill_harness"):
            if any(current.get(key) != value for key, value in desired.items()):
                raise RefillError(
                    "another scoped refill plan is saved with different limits or "
                    "scope; run `dradar refill stop` before starting a new campaign"
                )
            if current.get("status") == "stopped":
                if len(current.get("assignments", {})) >= max_tasks:
                    current["status"] = "draining"
                    current["stop_reason"] = "max_tasks reserved; draining queue"
                else:
                    current["status"] = "active"
                    current["stop_reason"] = None
        if current and current.get("status") in RUNNING_STATES:
            if any(current.get(key) != value for key, value in desired.items()):
                if not replace_existing:
                    raise RefillError(
                        "another refill plan is active with different limits"
                    )
                replaced_plan_id = current.get("plan_id")
                current = None
        if current and current.get("status") in RUNNING_STATES:
            plan = current
        else:
            seed_assignment_ids = list(dict.fromkeys(
                assignment.get("assignment_id")
                for assignment in active
                if assignment.get("assignment_id")
            ))
            plan = {
                "schema_version": SCHEMA_VERSION,
                "plan_id": uuid4().hex,
                "created_at": _now(),
                "updated_at": _now(),
                "status": "active",
                "stop_reason": None,
                "assignments": {},
                "tier_windows_usd": None,
                # The initial held batch is a user choice. Do not let rolling
                # refill work begin until every one of these assignments has
                # been confirmed submitted, even when several workers finish
                # at different times.
                "seed_assignment_ids": seed_assignment_ids,
                "submitted_seed_assignment_ids": [],
                **desired,
            }
            if replaced_plan_id:
                plan["replaced_plan_id"] = replaced_plan_id
        for assignment in active:
            _reserve(plan, assignment)
        if len(plan["assignments"]) > max_tasks:
            plan["status"] = "stopped"
            plan["stop_reason"] = "held tasks exceeded max_tasks"
            _save_unlocked(home, plan)
            raise RefillError(plan["stop_reason"])
        cap = plan.get("max_estimated_quota_pct")
        if cap is not None and _reserved_quota(plan) > float(cap) + 1e-9:
            plan["status"] = "stopped"
            plan["stop_reason"] = "held tasks exceeded estimated quota limit"
            _save_unlocked(home, plan)
            raise RefillError(plan["stop_reason"])
        _save_unlocked(home, plan)
        return plan


def _scoped_candidates(table: dict, plan: dict) -> list[dict]:
    """Discover exact-claim candidates from the authoritative public board.

    Subscription harnesses intentionally do not enter ``/suggest``.  The
    table supplies their canonical agent/model/effort namespace and current
    open state; the exact claim endpoint remains authoritative for races,
    account limits and leases.
    """

    cells = table.get("cells")
    combos = table.get("combos")
    if not isinstance(cells, dict) or not isinstance(combos, list):
        raise RefillError(
            "server table lacks the cell/combination data required for scoped refill"
        )
    harness = plan.get("refill_harness")
    model_filter = plan.get("refill_model")
    effort_filter = plan.get("refill_effort")
    combo_agents: dict[tuple[str, str], str] = {}
    matching_combos: set[tuple[str, str]] = set()
    for combo in combos:
        if not isinstance(combo, dict):
            continue
        model = combo.get("model")
        effort = combo.get("effort")
        if not isinstance(model, str) or not isinstance(effort, str):
            continue
        key = (model.lower(), effort.lower())
        agent = combo.get("agent") or "codex"
        if isinstance(agent, str):
            combo_agents[key] = agent
        if (
            agent == harness
            and (model_filter is None or key[0] == model_filter)
            and (effort_filter is None or key[1] == effort_filter)
        ):
            matching_combos.add(key)
    if not matching_combos:
        detail = "/".join(
            value for value in (harness, model_filter, effort_filter) if value
        )
        raise RefillError(
            f"server table exposes no combination matching refill scope {detail}"
        )

    windows = table.get("tier_windows_usd")
    plus_window = windows.get("plus") if isinstance(windows, dict) else None
    candidates: list[dict] = []
    for raw_key, raw_value in cells.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, dict):
            continue
        try:
            task_id, model, effort = raw_key.split("|", 2)
        except ValueError:
            continue
        key = (model.lower(), effort.lower())
        if key not in matching_combos or raw_value.get("st") != "open":
            continue
        # Preserve the existing invariant that paid-API work is one-off.  A
        # Codex-scoped board may contain manual DeepSeek API cells alongside
        # subscription cells; table discovery must never broaden refill into
        # billable API claims that `/suggest` deliberately excluded.
        if raw_value.get("billing_mode") == "api":
            continue
        agent = raw_value.get("agent") or combo_agents.get(key) or "codex"
        if agent != harness:
            continue
        candidate = dict(raw_value)
        candidate.update(task_id=task_id, model=model, effort=effort, agent=agent)
        if isinstance(windows, dict):
            candidate["tier_windows_usd"] = windows
        cost = candidate.get("cost")
        if (
            candidate.get("est_quota_pct") is None
            and isinstance(cost, (int, float)) and not isinstance(cost, bool)
            and isinstance(plus_window, (int, float)) and plus_window > 0
        ):
            candidate["est_quota_pct"] = float(cost) / float(plus_window) * 100
        candidates.append(candidate)
    def price_key(candidate: dict) -> tuple[int, float, str, str, str]:
        """Cheapest authoritative estimate first, with deterministic ties.

        A missing or malformed estimate must never jump ahead of priced work.
        ``est_quota_pct`` is a useful secondary estimate when a provider cell
        has no dollar-equivalent cost, but it is intentionally ranked after
        every directly priced candidate because the units are not comparable.
        """

        cost = candidate.get("cost")
        if (isinstance(cost, (int, float)) and not isinstance(cost, bool)
                and math.isfinite(float(cost)) and float(cost) >= 0):
            bucket, price = 0, float(cost)
        else:
            quota = candidate.get("est_quota_pct")
            if (isinstance(quota, (int, float)) and not isinstance(quota, bool)
                    and math.isfinite(float(quota)) and float(quota) >= 0):
                bucket, price = 1, float(quota)
            else:
                bucket, price = 2, 0.0
        return (
            bucket,
            price,
            str(candidate.get("task_id", "")),
            str(candidate.get("model", "")),
            str(candidate.get("effort", "")),
        )

    return sorted(candidates, key=price_key)


def mark_submitted(home: Path, assignment_id: str) -> int:
    """Persist a successful seed submission and return the remaining count.

    The submission itself is authoritative on the server. This local marker
    only opens the auto-refill barrier, and is written under the same lock as
    refill decisions so parallel workers cannot claim early.
    """
    with _locked(home):
        plan = _load_unlocked(home)
        if not plan or plan.get("status") not in RUNNING_STATES:
            return 0
        seed_ids = plan.get("seed_assignment_ids")
        if seed_ids is None or assignment_id not in seed_ids:
            return len(_pending_seed_assignment_ids(plan))
        submitted = plan.setdefault("submitted_seed_assignment_ids", [])
        if assignment_id not in submitted:
            submitted.append(assignment_id)
            _save_unlocked(home, plan)
        return len(_pending_seed_assignment_ids(plan))


def stop(
    home: Path, reason: str = "user stopped", *, discard: bool = False,
) -> dict | None:
    with _locked(home):
        plan = _load_unlocked(home)
        if not plan:
            return None
        circuit_open = bool(
            isinstance(plan.get("circuit"), dict)
            and plan["circuit"].get("state") == "open"
        )
        if circuit_open and not discard:
            plan["status"] = FAULTED_STATE
        else:
            plan["status"] = "stopped"
            plan["stop_reason"] = reason
        if plan.get("refill_harness") and not discard:
            _save_unlocked(home, plan)
            return plan
        # A stopped plan has no recovery work left to coordinate. Keeping the
        # file only exposes an internal implementation detail and previously
        # let stale state confuse the next campaign.
        _path(home).unlink(missing_ok=True)
        return plan


def is_running(home: Path) -> bool:
    plan = load(home)
    return bool(plan and plan.get("status") in RUNNING_STATES)


def complete_if_empty(home: Path, held: int) -> None:
    if held:
        return
    with _locked(home):
        plan = _load_unlocked(home)
        # An active worker may have completed the last held task server-side
        # and still be between that response and mark_submitted/refill_once.
        # Deleting an active plan here lets an idle sibling win that race and
        # prevents the first post-seed refill. Only a plan already known to be
        # draining has no future claim work and can be removed safely.
        if (plan and plan.get("status") == "draining"
                and not plan.get("refill_harness")):
            _path(home).unlink(missing_ok=True)


def refill_once(home: Path, client) -> dict:
    """Reconcile server-held work and atomically refill toward the plan target.

    The lock deliberately spans the bounded HTTP calls.  It elects exactly one
    local worker as coordinator at each refill boundary and prevents a herd of
    parallel workers from all observing the same shortfall.
    """
    with _locked(home):
        plan = _load_unlocked(home)
        if not plan or plan.get("status") not in RUNNING_STATES:
            return {"status": plan.get("status") if plan else "none", "claimed": 0}
        if plan.get("status") == "draining":
            return {"status": "draining", "claimed": 0,
                    "planned": len(plan.get("assignments", {})),
                    "reason": plan.get("stop_reason")}
        data = client.get_assignment()
        active = data.get("active")
        if active is None:
            one = data.get("assignment")
            active = [one] if one else []
        try:
            for assignment in active:
                _reserve(plan, assignment)
        except RefillError as exc:
            plan["status"] = "stopped"
            plan["stop_reason"] = str(exc)
            _save_unlocked(home, plan)
            return {"status": "stopped", "claimed": 0, "reason": str(exc)}

        planned = len(plan["assignments"])
        quota_cap = plan.get("max_estimated_quota_pct")
        if planned > int(plan["max_tasks"]):
            plan["status"] = "stopped"
            plan["stop_reason"] = "held queue grew beyond max_tasks; refill stopped"
            _save_unlocked(home, plan)
            return {"status": "stopped", "claimed": 0,
                    "held": len(active), "planned": planned,
                    "reason": plan["stop_reason"]}
        if quota_cap is not None and _reserved_quota(plan) > float(quota_cap) + 1e-9:
            plan["status"] = "stopped"
            plan["stop_reason"] = "held queue grew beyond estimated quota cap; refill stopped"
            _save_unlocked(home, plan)
            return {"status": "stopped", "claimed": 0,
                    "held": len(active), "planned": planned,
                    "reason": plan["stop_reason"]}
        seed_pending = _pending_seed_assignment_ids(plan)
        if seed_pending:
            # A user-selected batch is a priority barrier, not merely older
            # FIFO work. Waiting here prevents a fast worker from starting an
            # auto-claimed task while a slower selected task is still running.
            _save_unlocked(home, plan)
            return {"status": plan["status"], "claimed": 0,
                    "held": len(active), "planned": planned,
                    "seed_pending": len(seed_pending)}
        slots_left = max(0, int(plan["max_tasks"]) - planned)
        missing = max(0, int(plan["refill_to"]) - len(active))
        wanted = min(missing, slots_left)
        if wanted == 0:
            if slots_left == 0:
                plan["status"] = "draining"
                plan["stop_reason"] = "max_tasks reserved; draining queue"
            _save_unlocked(home, plan)
            return {"status": plan["status"], "claimed": 0,
                    "held": len(active), "planned": planned}

        claimed = 0
        # Ask for a few alternates so a stale recommendation or one expensive
        # cell does not prevent a bounded refill. The server still clamps this
        # request to the account's claim limit.
        if plan.get("refill_harness"):
            try:
                suggestions = _scoped_candidates(client.table(), plan)
            except RefillError as exc:
                plan["status"] = "stopped"
                plan["stop_reason"] = str(exc)
                _save_unlocked(home, plan)
                return {"status": "stopped", "claimed": 0,
                        "held": len(active), "planned": planned,
                        "reason": str(exc)}
        else:
            suggestions = client.suggest(max(wanted, wanted * 3)).get("cells") or []
        quota_blocked = False
        missing_quota_estimate = False
        for cell in suggestions:
            if claimed >= wanted:
                break
            estimate = _estimate_pct(
                cell, plan["quota_tier"], plan.get("tier_windows_usd"))
            if quota_cap is not None:
                if estimate is None:
                    missing_quota_estimate = True
                    continue
                if _reserved_quota(plan) + estimate > float(quota_cap) + 1e-9:
                    quota_blocked = True
                    continue
            try:
                ack = client.claim_assignment(
                    cell["task_id"], cell["model"], cell["effort"])
            except ApiError as exc:
                if exc.status_code == 409:
                    continue
                raise
            assignment = ack.get("assignment")
            if assignment:
                try:
                    accepted = _reserve(plan, assignment, enforce_scope=True)
                except RefillError as exc:
                    plan["status"] = "stopped"
                    plan["stop_reason"] = str(exc)
                    _save_unlocked(home, plan)
                    return {"status": "stopped", "claimed": claimed,
                            "held": len(active) + claimed,
                            "planned": len(plan["assignments"]),
                            "reason": str(exc)}
                if accepted:
                    claimed += 1
                    _save_unlocked(home, plan)  # crash-safe after every accepted claim

        if claimed == 0 and missing_quota_estimate:
            # This is a server/client data-contract failure, not an exhausted
            # user quota.  Fail closed and say why instead of silently entering
            # a misleading draining state that can never recover by itself.
            plan["status"] = "stopped"
            plan["stop_reason"] = (
                f"recommendations lack {plan['quota_tier']} quota conversion data; "
                "refill stopped before claiming work"
            )
        elif claimed == 0 and quota_blocked:
            plan["status"] = "draining"
            plan["stop_reason"] = "no recommended task fits the estimated quota left"
        _save_unlocked(home, plan)
        result = {"status": plan["status"], "claimed": claimed,
                "held": len(active) + claimed,
                "planned": len(plan["assignments"]),
                "reserved_quota_pct": _reserved_quota(plan),
                "reason": plan.get("stop_reason")}
        if (claimed == 0 and plan.get("refill_harness")
                and plan.get("status") == "active"
                and not missing_quota_estimate and not quota_blocked):
            result["waiting_for_inventory"] = True
            result["reason"] = (
                "no currently claimable open cells match the configured refill scope"
            )
        return result
