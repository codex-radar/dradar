"""Inspect and voluntarily release assignment leases.

These commands are deliberately separate from the run loop: listing is
read-only, while release is an explicit, idempotent user action. A normal
release protects cells whose runner has started; ``--force`` is the escape
hatch for a genuinely stuck local process.
"""

import json
import sys
import uuid
from datetime import datetime

from .api_client import ApiError
from .assignment_state import assignment_state, state_summary
from .identity import _client
from .flight_recorder import FlightRecorder
from .local_config import HOME, _load_config


def _inventory(client) -> tuple[list[dict], list[dict]]:
    get_inventory = getattr(client, "get_assignment_inventory", None)
    data = get_inventory() if get_inventory is not None else client.get_assignment()
    active = data.get("active")
    if active is None:
        one = data.get("assignment")
        active = [one] if one else []
    return active, list(data.get("recent_inactive") or [])


def _active(client) -> list[dict]:
    return _inventory(client)[0]


def _all_inventory(client) -> tuple[list[dict], list[dict]]:
    """Return held assignments across every benchmark visible to this user.

    Assignment lookup is intentionally benchmark-scoped for the run loop, but
    ``leases`` and the interactive release picker are account-wide inventory
    commands.  Query each advertised benchmark without changing the saved
    benchmark or the scope used by a later ``resume``.

    Servers old enough to lack the benchmark catalog keep the legacy current-
    benchmark behavior.  A 403 for one catalog entry means that channel is not
    runnable by this identity and is skipped; other errors must remain visible
    rather than presenting a misleading partial inventory.
    """
    original_benchmark = getattr(client, "benchmark_id", None)
    try:
        try:
            catalog = client.benchmarks()
        except ApiError as exc:
            if exc.status_code == 404:
                return _inventory(client)
            raise

        benchmark_ids = [
            item.get("id")
            for item in (catalog.get("benchmarks") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        if original_benchmark and original_benchmark not in benchmark_ids:
            benchmark_ids.insert(0, original_benchmark)
        if not benchmark_ids:
            return _inventory(client)

        active: list[dict] = []
        recent_inactive: list[dict] = []
        seen_active: set[str] = set()
        seen_inactive: set[str] = set()
        for benchmark_id in benchmark_ids:
            client.benchmark_id = benchmark_id
            try:
                scoped, scoped_inactive = _inventory(client)
            except ApiError as exc:
                if exc.status_code == 403:
                    continue
                raise
            for item in scoped:
                assignment_id = item.get("assignment_id")
                if assignment_id and assignment_id in seen_active:
                    continue
                copy = dict(item)
                copy.setdefault("benchmark_id", benchmark_id)
                active.append(copy)
                if assignment_id:
                    seen_active.add(assignment_id)
            for item in scoped_inactive:
                assignment_id = item.get("assignment_id")
                if assignment_id and assignment_id in seen_inactive:
                    continue
                copy = dict(item)
                copy.setdefault("benchmark_id", benchmark_id)
                recent_inactive.append(copy)
                if assignment_id:
                    seen_inactive.add(assignment_id)
        recent_inactive.sort(
            key=lambda item: item.get("inactive_at") or "", reverse=True,
        )
        return active, recent_inactive[:10]
    finally:
        client.benchmark_id = original_benchmark


def _all_active(client) -> list[dict]:
    return _all_inventory(client)[0]


def _expiry(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return dt.astimezone().strftime("%m-%d %H:%M")


def _state(assignment: dict) -> str:
    return assignment_state(assignment)


def _print_active(active: list[dict]) -> None:
    for index, item in enumerate(active, 1):
        benchmark = item.get("benchmark_id")
        benchmark_label = f"  [{benchmark}]" if benchmark else ""
        print(
            f"  {index:>2}. {_state(item):9s}  "
            f"{item['task_id']}  {item['model']}@{item['effort']}"
            f"{benchmark_label}\n"
            f"      {item['assignment_id']}  expires {_expiry(item.get('expires_at'))}"
        )


def _print_recent_inactive(items: list[dict]) -> None:
    print("recent unsubmitted leases that are no longer held:")
    for item in items:
        benchmark = item.get("benchmark_id")
        benchmark_label = f"  [{benchmark}]" if benchmark else ""
        status = item.get("status") or "ended"
        reason = item.get("reason") or status
        print(
            f"  {status:9s}  {item.get('task_id', 'unknown')}  "
            f"{item.get('model', 'unknown')}@{item.get('effort', 'unknown')}"
            f"{benchmark_label}\n"
            f"      {item.get('assignment_id', 'unknown')}  "
            f"{reason} at {_expiry(item.get('inactive_at'))}"
        )


def cmd_leases(args) -> int:
    """List every live cell held by the current identity."""
    cfg = _load_config()
    client = _client(cfg)
    try:
        active, recent_inactive = _all_inventory(client)
    except ApiError as exc:
        sys.exit(f"lease check failed: {exc}")
    if getattr(args, "json", False):
        payload = {
            "schema_version": 1,
            "status": "ok",
            "active": active,
            "recent_inactive": recent_inactive,
            "summary": {
                "total": len(active),
                "running": sum(_state(item) == "running" for item in active),
                "waiting": sum(_state(item) == "waiting" for item in active),
                "paused": sum(_state(item) == "paused" for item in active),
                "stale": sum(_state(item) == "stale" for item in active),
            },
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    if not active and not recent_inactive:
        print("no active leases")
        return 0

    if active:
        running = sum(_state(item) == "running" for item in active)
        stale = sum(_state(item) == "stale" for item in active)
        print(f"holding {len(active)} cell(s): {state_summary(active)}")
        _print_active(active)
        print("\nrelease waiting cells: `dradar release <assignment-id>` or "
              "`dradar release --all`")
        if running:
            print("a running cell is protected; only use `--force` after its local "
                  "runner has definitely stopped")
        if stale:
            print("a stale cell has no live owner and is not automatically resumable; "
                  "do not start a duplicate local model process. Retry after the server "
                  "refreshes it, or use `dradar release --force` only after confirming "
                  "the original runner/container is gone")
    else:
        print("no active leases")
    if recent_inactive:
        print()
        _print_recent_inactive(recent_inactive)
    return 0


def _interactive_targets(active: list[dict]) -> list[str]:
    if not active:
        return []
    print("select leases to release:")
    _print_active(active)
    raw = input("numbers separated by commas, or 'all' (Enter cancels): ").strip().lower()
    if not raw:
        return []
    if raw == "all":
        return [item["assignment_id"] for item in active]
    try:
        indexes = list(dict.fromkeys(int(part.strip()) for part in raw.split(",")))
    except ValueError:
        sys.exit("invalid selection — use numbers such as 1,3")
    if not indexes or any(i < 1 or i > len(active) for i in indexes):
        sys.exit(f"selection must be between 1 and {len(active)}")
    return [active[i - 1]["assignment_id"] for i in indexes]


def cmd_release(args) -> int:
    """Release explicit IDs, all safe-to-release cells, or an interactive pick."""
    cfg = _load_config()
    client = _client(cfg)
    recorder = FlightRecorder(HOME, client)
    request_id = uuid.uuid4().hex
    explicit = list(dict.fromkeys(args.assignment_ids or ()))
    active: list[dict] = []

    if explicit and args.all:
        sys.exit("pass assignment IDs or --all, not both")

    if not explicit and not args.all:
        try:
            active = _all_active(client)
        except ApiError as exc:
            sys.exit(f"lease check failed: {exc}")
        if not active:
            print("no active leases")
            return 0
        explicit = _interactive_targets(active)
        if not explicit:
            print("cancelled")
            return 0

    if not args.yes:
        count = "all held" if args.all else str(len(explicit))
        warning = " including running work" if args.force else " (running work stays protected)"
        answer = input(f"release {count} lease(s){warning}? [y/N] ").strip().lower()
        if answer != "y":
            print("cancelled")
            return 1

    for assignment_id in explicit:
        recorder.try_record(
            "release_requested", component="release",
            assignment_id=assignment_id, request_id=request_id,
            reason_code="explicit_force" if args.force else "explicit_safe",
            attributes={
                "force": bool(args.force), "release_count": len(explicit),
            },
        )
    try:
        try:
            data = client.release_assignments(
                explicit or None, release_all=args.all, force=args.force,
                request_id=request_id,
            )
        except TypeError as exc:
            # Preserve developer/test adapters and rolling clients that expose
            # the pre-flight-recorder call signature. No network request can
            # have started when Python rejects an unexpected keyword.
            if "request_id" not in str(exc):
                raise
            data = client.release_assignments(
                explicit or None, release_all=args.all, force=args.force,
            )
    except ApiError as exc:
        for assignment_id in explicit:
            recorder.try_record(
                "release_failed", component="release",
                assignment_id=assignment_id, request_id=request_id,
                reason_code="api_error",
                attributes={
                    "force": bool(args.force), "http_status": exc.status_code or 0,
                },
            )
        recorder.flush()
        if exc.status_code == 404:
            sys.exit("release failed: assignment not found, or this server/CLI "
                     "release API is not deployed yet")
        sys.exit(f"release failed: {exc}")

    released = data.get("released") or []
    skipped = data.get("skipped") or []
    already = data.get("already_released") or []
    for item in released:
        recorder.try_record(
            "release_completed", component="release",
            request_id=data.get("request_id") or request_id,
            assignment_id=item.get("assignment_id"),
            reason_code="user_force" if args.force else "user",
            attributes={
                "force": bool(args.force),
                "was_running": bool(item.get("was_running")),
            },
        )
    recorder.flush()
    if released:
        print(f"released {len(released)} lease(s):")
        for item in released:
            print(f"  {item['task_id']}  {item['model']}@{item['effort']}  "
                  f"{item['assignment_id']}")
    if already:
        print(f"already released: {len(already)}")
    if skipped:
        print(f"kept {len(skipped)} lease(s):")
        for item in skipped:
            print(f"  {item['task_id']} — {item['reason']}")
        if any(item.get("reason") == "running" for item in skipped):
            print("stop the local runner first, then repeat with `--force` only "
                  "if the cell is truly stuck")
    print(f"still holding {data.get('held', '?')} lease(s)")
    return 0


__all__ = ["cmd_leases", "cmd_release"]
