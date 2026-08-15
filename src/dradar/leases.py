"""Inspect and voluntarily release assignment leases.

These commands are deliberately separate from the run loop: listing is
read-only, while release is an explicit, idempotent user action. A normal
release protects cells whose runner has started; ``--force`` is the escape
hatch for a genuinely stuck local process.
"""

import sys
from datetime import datetime

from .api_client import ApiError
from .assignment_state import assignment_state, state_summary
from .identity import _client
from .local_config import _load_config


def _active(client) -> list[dict]:
    data = client.get_assignment()
    active = data.get("active")
    if active is None:
        one = data.get("assignment")
        active = [one] if one else []
    return active


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
        print(
            f"  {index:>2}. {_state(item):9s}  "
            f"{item['task_id']}  {item['model']}@{item['effort']}\n"
            f"      {item['assignment_id']}  expires {_expiry(item.get('expires_at'))}"
        )


def cmd_leases(args) -> int:
    """List every live cell held by the current identity."""
    cfg = _load_config()
    client = _client(cfg)
    try:
        active = _active(client)
    except ApiError as exc:
        sys.exit(f"lease check failed: {exc}")
    if not active:
        print("no active leases")
        return 0

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
        print("a stale cell has no usable checkpoint and is not actually resumable; "
              "do not start a duplicate local model process. Retry after the server "
              "refreshes it, or use `dradar release --force` only after confirming "
              "the original runner/container is gone")
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
    explicit = list(dict.fromkeys(args.assignment_ids or ()))
    active: list[dict] = []

    if explicit and args.all:
        sys.exit("pass assignment IDs or --all, not both")

    if not explicit and not args.all:
        try:
            active = _active(client)
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

    try:
        data = client.release_assignments(
            explicit or None, release_all=args.all, force=args.force)
    except ApiError as exc:
        if exc.status_code == 404:
            sys.exit("release failed: assignment not found, or this server/CLI "
                     "release API is not deployed yet")
        sys.exit(f"release failed: {exc}")

    released = data.get("released") or []
    skipped = data.get("skipped") or []
    already = data.get("already_released") or []
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
