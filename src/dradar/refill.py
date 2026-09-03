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
from .providers import (
    REFILL_HARNESS_PROVIDERS,
    normalize_refill_harness,
    validate_refill_scope,
)

SCHEMA_VERSION = 1
PLAN_FILE = "refill-plan.json"
LOCK_FILE = "refill-plan.lock"
PLAN_SCOPE_ENV = "DRADAR_REFILL_PLAN_SCOPE"
SCOPED_PLAN_DIR = "refill-plans"
RUNNING_STATES = {"active", "draining"}
FAULTED_STATE = "faulted"
REFILL_FAULT_FAMILIES = frozenset({
    "checkpoint_invalid", "checkpoint_incompatible", "provider_not_ready",
    "empty_submission",
})
TIERS = ("plus", "pro-5x", "pro-20x")
REFILL_ORDERS = ("cost", "least-run")


class RefillError(RuntimeError):
    pass


class RefillCircuitOpen(RefillError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(home: Path) -> Path:
    scope = os.environ.get(PLAN_SCOPE_ENV, "").strip().lower()
    if scope:
        if not all(character in "0123456789abcdef" for character in scope):
            raise RefillError("invalid scoped refill plan identifier")
        return home / SCOPED_PLAN_DIR / f"{scope}.json"
    return home / PLAN_FILE


def _lock_path(home: Path) -> Path:
    scope = os.environ.get(PLAN_SCOPE_ENV, "").strip().lower()
    if scope:
        if not all(character in "0123456789abcdef" for character in scope):
            raise RefillError("invalid scoped refill plan identifier")
        return home / SCOPED_PLAN_DIR / f"{scope}.lock"
    return home / LOCK_FILE


@contextmanager
def _locked(home: Path) -> Iterator[None]:
    home.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(home)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
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
    """Latch a scoped refill plan after a provider or historical runner fault.

    The DRADAR_HOME is already the account boundary.  Persist the remaining
    key dimensions so a restart, CLI upgrade, task change, or effort change
    cannot silently turn the same harness/provider fault back into claims.
    Only the explicit user-facing ``dradar refill stop`` command discards the
    saved plan and rearms a later campaign.
    """

    if failure_family not in REFILL_FAULT_FAMILIES:
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
            and current.get("batch_id") == assignment.get("batch_id")
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
                "batch_id": assignment.get("batch_id"),
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
            "underlying fault is fixed to explicitly rearm"
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
    refill_order: str = "cost",
    server_campaign_id: str | None = None,
    points_tier: str | None = None,
    replace_existing: bool = False,
) -> dict:
    if refill_to < 1 or max_tasks < 1:
        raise RefillError("refill target and max tasks must be positive")
    if quota_tier not in TIERS:
        raise RefillError(f"unknown quota tier: {quota_tier}")
    if refill_harness is None and (refill_model is not None or refill_effort is not None):
        raise RefillError("refill model/effort filters require a refill harness")
    if refill_order not in REFILL_ORDERS:
        raise RefillError(f"unknown refill order: {refill_order}")
    if refill_harness is None and refill_order != "cost":
        raise RefillError("non-default refill order requires a refill harness")
    if points_tier is not None and points_tier not in TIERS:
        raise RefillError(f"unknown plan points tier: {points_tier}")
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
        "refill_order": refill_order,
    }
    if server_campaign_id is not None:
        desired["server_campaign_id"] = server_campaign_id
        if points_tier is not None:
            desired["points_tier"] = points_tier
    # A scoped plan owns only its Harness/model/effort lane.  The account may
    # legitimately hold work for other independent Harness campaigns; treating
    # those assignments as seeds would block this plan until the entire account
    # drained and would also consume its max-tasks budget.  Keep unscoped plans
    # unchanged, but isolate scoped accounting from the first persisted write.
    if refill_harness is not None:
        active = [assignment for assignment in active
                  if _matches_scope(desired, assignment)]
    if max_tasks < len(active):
        raise RefillError("max tasks must be at least the currently held task count")
    with _locked(home):
        current = _load_unlocked(home)
        # Plans written before ordering became configurable are exactly the
        # historical cheapest-first behavior.  Normalize them in memory so a
        # CLI upgrade does not manufacture a conflicting plan.
        if current is not None and "refill_order" not in current:
            current["refill_order"] = "cost"
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
                    f"{circuit.get('failure_family') or 'a runtime fault'}; "
                    "fix the reported problem, then run `dradar refill stop` "
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
            # A run-plan campaign can be shared by several machines. Each
            # machine sees the whole exact batch but only observes its own
            # submissions locally, so a machine-local seed ledger can never be
            # authoritative there. The server campaign owns that global
            # barrier. Legacy/local refill keeps the original durable ledger.
            seed_assignment_ids = (
                []
                if server_campaign_id is not None else
                list(dict.fromkeys(
                    assignment.get("assignment_id")
                    for assignment in active
                    if assignment.get("assignment_id")
                ))
            )
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
                "seed_barrier": (
                    "server" if server_campaign_id is not None else "local"
                ),
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
                and math.isfinite(float(cost)) and float(cost) > 0):
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

    if plan.get("refill_order", "cost") == "least-run":
        def least_run_key(candidate: dict) -> tuple[int, float, str, str, str]:
            count = candidate.get("n")
            if (
                isinstance(count, (int, float))
                and not isinstance(count, bool)
                and math.isfinite(float(count))
                and float(count) >= 0
            ):
                bucket, runs = 0, float(count)
            else:
                bucket, runs = 1, 0.0
            return (
                bucket,
                runs,
                str(candidate.get("task_id", "")),
                str(candidate.get("model", "")),
                str(candidate.get("effort", "")),
            )

        return sorted(candidates, key=least_run_key)
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


def _authoritative_campaign_snapshot(plan: dict, client) -> dict | None:
    """Read and validate the global campaign state for a run-plan refill.

    A scoped campaign is shared across devices, while the JSON file guarded by
    this module is necessarily machine-local.  Seed completion, held queue
    size, target and total budget therefore come from the server or fail
    closed; the local ledger remains useful only for crash-safe metadata about
    assignments this machine has observed.
    """

    campaign_id = plan.get("server_campaign_id")
    if not campaign_id:
        return None
    response = client.refill_campaign_status(campaign_id)
    campaign = response.get("campaign") if isinstance(response, dict) else None
    # Compare the server's wire name with the local canonical name. This is a
    # defensive protocol check only: the CLI still rejects continuous refill
    # for paid-API DSH before campaign setup, so this does not enable it.
    try:
        campaign_harness = normalize_refill_harness(
            campaign.get("harness") if isinstance(campaign, dict) else "",
        )
    except (AttributeError, ValueError):
        campaign_harness = None
    valid_statuses = {"active", "draining", "stopped", "completed"}
    integer_fields = ("refill_to", "max_tasks", "planned", "held", "seed_pending")
    valid = bool(
        isinstance(campaign, dict)
        and campaign.get("batch_id") == campaign_id
        and campaign.get("status") in valid_statuses
        and all(
            isinstance(campaign.get(key), int)
            and not isinstance(campaign.get(key), bool)
            and campaign[key] >= (1 if key in {"refill_to", "max_tasks"} else 0)
            for key in integer_fields
        )
        and campaign["refill_to"] <= campaign["max_tasks"]
        and campaign["planned"] <= campaign["max_tasks"]
        and campaign["held"] <= campaign["planned"]
        and campaign["seed_pending"] <= campaign["planned"]
        and campaign_harness == plan.get("refill_harness")
        and campaign.get("model") == plan.get("refill_model")
        and campaign.get("effort") == plan.get("refill_effort")
    )
    if not valid:
        raise RefillError("server returned an invalid exact refill campaign status")
    return campaign


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
        if plan.get("status") == "draining" and not plan.get("server_campaign_id"):
            return {"status": "draining", "claimed": 0,
                    "planned": len(plan.get("assignments", {})),
                    "reason": plan.get("stop_reason")}
        try:
            data = client.get_assignment()
        except ApiError as exc:
            # An exact seed batch is momentarily empty after its last selected
            # task submits and before the first server-budgeted refill claim.
            # Treat only that authenticated campaign gap as an empty queue;
            # every other 404 remains a real boundary failure.
            if plan.get("server_campaign_id") and exc.status_code == 404:
                data = {"active": []}
            else:
                raise
        active = data.get("active")
        if active is None:
            one = data.get("assignment")
            active = [one] if one else []
        if plan.get("refill_harness"):
            active = [assignment for assignment in active
                      if _matches_scope(plan, assignment)]
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
        campaign = _authoritative_campaign_snapshot(plan, client)
        if campaign is not None:
            campaign_status = campaign["status"]
            planned = campaign["planned"]
            if campaign_status in {"stopped", "completed"}:
                plan["status"] = campaign_status
                plan["stop_reason"] = campaign.get("stop_reason")
                _save_unlocked(home, plan)
                return {
                    "status": campaign_status,
                    "claimed": 0,
                    "held": campaign["held"],
                    "planned": planned,
                    "reason": campaign.get("stop_reason"),
                }
            if campaign["seed_pending"]:
                # The exact server campaign counts submissions from every
                # admitted device. Never gate this path on the machine-local
                # submitted_seed_assignment_ids list.
                plan["status"] = campaign_status
                _save_unlocked(home, plan)
                return {
                    "status": campaign_status,
                    "claimed": 0,
                    "held": campaign["held"],
                    "planned": planned,
                    "seed_pending": campaign["seed_pending"],
                }
            if campaign_status == "draining":
                plan["status"] = "draining"
                plan["stop_reason"] = campaign.get("stop_reason")
                _save_unlocked(home, plan)
                return {
                    "status": "draining",
                    "claimed": 0,
                    "held": campaign["held"],
                    "planned": planned,
                    "reason": campaign.get("stop_reason"),
                }
            plan["status"] = "active"
            plan["stop_reason"] = None
            held_for_target = campaign["held"]
            target_for_refill = campaign["refill_to"]
            max_tasks_for_refill = campaign["max_tasks"]
        else:
            held_for_target = len(active)
            target_for_refill = int(plan["refill_to"])
            max_tasks_for_refill = int(plan["max_tasks"])

        seed_pending = (
            [] if campaign is not None else _pending_seed_assignment_ids(plan)
        )
        if seed_pending:
            # A user-selected batch is a priority barrier, not merely older
            # FIFO work. Waiting here prevents a fast worker from starting an
            # auto-claimed task while a slower selected task is still running.
            _save_unlocked(home, plan)
            return {"status": plan["status"], "claimed": 0,
                    "held": len(active), "planned": planned,
                    "seed_pending": len(seed_pending)}
        slots_left = max(0, max_tasks_for_refill - planned)
        missing = max(0, target_for_refill - held_for_target)
        wanted = min(missing, slots_left)
        if wanted == 0:
            if slots_left == 0:
                plan["status"] = "draining"
                plan["stop_reason"] = "max_tasks reserved; draining queue"
            _save_unlocked(home, plan)
            return {"status": plan["status"], "claimed": 0,
                    "held": held_for_target, "planned": planned}

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
        server_target_satisfied = False
        server_seed_pending = 0
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
                campaign_id = plan.get("server_campaign_id")
                if campaign_id:
                    ack = client.claim_assignment(
                        cell["task_id"], cell["model"], cell["effort"],
                        refill_campaign_id=campaign_id,
                        tier=plan.get("points_tier"),
                    )
                else:
                    ack = client.claim_assignment(
                        cell["task_id"], cell["model"], cell["effort"],
                    )
            except ApiError as exc:
                if exc.code == "empty_submission_circuit_open":
                    plan["status"] = FAULTED_STATE
                    plan["stop_reason"] = str(exc)
                    break
                if exc.code in {
                    "refill_limit_reached", "refill_campaign_not_active",
                    "refill_campaign_faulted",
                }:
                    plan["status"] = (
                        "draining"
                        if exc.code in {
                            "refill_limit_reached",
                        } else "stopped"
                    )
                    plan["stop_reason"] = str(exc)
                    break
                if exc.code in {
                    "refill_seed_pending", "refill_target_satisfied",
                }:
                    server_target_satisfied = (
                        exc.code == "refill_target_satisfied"
                    )
                    if exc.code == "refill_seed_pending":
                        payload = exc.payload if isinstance(exc.payload, dict) else {}
                        remote_campaign = payload.get("campaign")
                        if isinstance(remote_campaign, dict):
                            value = remote_campaign.get("seed_pending")
                            if isinstance(value, int) and not isinstance(value, bool):
                                server_seed_pending = max(1, value)
                        if server_seed_pending == 0:
                            server_seed_pending = 1
                    break
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
                            "held": held_for_target + claimed,
                            "planned": (
                                planned + claimed if campaign is not None else
                                len(plan["assignments"])
                            ),
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
                "held": held_for_target + claimed,
                "planned": (
                    planned + claimed if campaign is not None else
                    len(plan["assignments"])
                ),
                "reserved_quota_pct": _reserved_quota(plan),
                "reason": plan.get("stop_reason")}
        if server_target_satisfied:
            result["target_satisfied"] = True
        if server_seed_pending:
            result["seed_pending"] = server_seed_pending
        if (claimed == 0 and plan.get("refill_harness")
                and plan.get("status") == "active"
                and not missing_quota_estimate and not quota_blocked
                and not server_target_satisfied and not server_seed_pending):
            result["waiting_for_inventory"] = True
            result["reason"] = (
                "no currently claimable open cells match the configured refill scope"
            )
        return result
