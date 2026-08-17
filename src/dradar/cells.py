"""Read-only full-board browsing and filtering for ``dradar cells``."""

from __future__ import annotations

import json
import shlex
import sys
from typing import Any, Iterable

from .api_client import ApiClient, ApiError
from .local_config import _load_config


_TEXT_SORTS = {"task", "model", "effort", "state"}
_SORT_FIELDS = {
    "task": "task_id",
    "model": "model",
    "effort": "effort",
    "state": "st",
    "multiplier": "mult",
    "tests": "total_n",
    "pass-rate": "rate",
    "minutes": "min",
    "cost": "cost",
    "priority": "suggest_priority",
}


def _selected(values: Iterable[str] | None) -> set[str] | None:
    """Accept both repeatable flags and convenient comma-separated values."""
    if not values:
        return None
    selected = {
        item.strip().lower()
        for value in values
        for item in value.split(",")
        if item.strip()
    }
    return selected or None


def _rows(table: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    benchmark_id = table.get("benchmark_id")
    for key, value in (table.get("cells") or {}).items():
        try:
            task_id, model, effort = key.split("|", 2)
        except ValueError:
            continue
        row = {"task_id": task_id, "model": model, "effort": effort,
               "benchmark_id": benchmark_id}
        if isinstance(value, dict):
            row.update(value)
        rows.append(row)
    return rows


def _apply_price_band(rows: list[dict[str, Any]], price_band: str) -> None:
    """Use one explicit DeepSeek tariff for every read-only cost operation."""
    band_key = "peak" if price_band == "peak" else "off_peak"
    for row in rows:
        by_band = row.get("cost_by_band")
        if not isinstance(by_band, dict):
            continue
        value = by_band.get(band_key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        row["cost"] = float(value)
        row["price_band"] = price_band


def _filter_rows(rows: list[dict[str, Any]], args) -> list[dict[str, Any]]:
    models = _selected(args.model)
    efforts = _selected(args.effort)
    states = {"open"} if args.available else _selected(args.state)
    task_query = args.task.lower() if args.task else None

    filtered = []
    for row in rows:
        if models and str(row["model"]).lower() not in models:
            continue
        if efforts and str(row["effort"]).lower() not in efforts:
            continue
        if states and str(row.get("st", "")).lower() not in states:
            continue
        if task_query and task_query not in str(row["task_id"]).lower():
            continue
        mult = row.get("mult")
        tests = row.get("total_n")
        priority = row.get("suggest_priority", 0)
        if args.min_multiplier is not None and (
                mult is None or mult < args.min_multiplier):
            continue
        if args.min_tests is not None and (tests is None or tests < args.min_tests):
            continue
        if args.max_tests is not None and (tests is None or tests > args.max_tests):
            continue
        minutes = row.get("min")
        if (getattr(args, "min_minutes", None) is not None and
                (minutes is None or minutes < args.min_minutes)):
            continue
        if (getattr(args, "max_minutes", None) is not None and
                (minutes is None or minutes > args.max_minutes)):
            continue
        cost = row.get("cost")
        if (getattr(args, "min_cost", None) is not None and
                (cost is None or cost < args.min_cost)):
            continue
        if (getattr(args, "max_cost", None) is not None and
                (cost is None or cost > args.max_cost)):
            continue
        rate = row.get("rate")
        if (getattr(args, "min_pass_rate", None) is not None and
                (rate is None or rate < args.min_pass_rate)):
            continue
        if (getattr(args, "max_pass_rate", None) is not None and
                (rate is None or rate > args.max_pass_rate)):
            continue
        if args.min_priority is not None and priority < args.min_priority:
            continue
        filtered.append(row)
    return filtered


def _validate_ranges(args) -> None:
    ranges = (
        ("min-minutes", "max-minutes", getattr(args, "min_minutes", None),
         getattr(args, "max_minutes", None)),
        ("min-cost", "max-cost", getattr(args, "min_cost", None),
         getattr(args, "max_cost", None)),
        ("min-pass-rate", "max-pass-rate", getattr(args, "min_pass_rate", None),
         getattr(args, "max_pass_rate", None)),
    )
    for low_name, high_name, low, high in ranges:
        if low is not None and high is not None and low > high:
            sys.exit(f"--{low_name} cannot be greater than --{high_name}")


def _validate_priority_support(rows: list[dict[str, Any]], args) -> bool:
    """Reject priority operations when the server published no such signal.

    The table API omits ``suggest_priority`` for ordinary cells and only adds
    it when at least one cell has an explicit recommendation override.  An
    all-missing response therefore means "priority is unavailable", not that
    every cell has a meaningful priority of zero.
    """
    available = any("suggest_priority" in row for row in rows)
    if not available and (args.min_priority is not None or args.sort == "priority"):
        sys.exit(
            "this server does not publish recommendation priority data — "
            "remove --min-priority/--sort priority"
        )
    return available


def _sort_rows(rows: list[dict[str, Any]], field: str, reverse: bool) -> None:
    key = _SORT_FIELDS[field]
    populated = [row for row in rows if row.get(key) is not None]
    missing = [row for row in rows if row.get(key) is None]
    # Natural order: names A-Z, numeric signals highest-first. --reverse flips
    # that useful default.  Keep missing measurements at the end either way.
    descending = field not in _TEXT_SORTS
    if reverse:
        descending = not descending
    # Sort ties predictably A-Z even when the primary numeric field is
    # descending; reversing one compound tuple would reverse task IDs too.
    populated.sort(key=lambda row: (row["task_id"], row["model"], row["effort"]))
    populated.sort(
        key=lambda row: str(row[key]).lower() if field in _TEXT_SORTS else row[key],
        reverse=descending,
    )
    rows[:] = populated + missing


def _fmt_number(value: Any, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        rendered = f"{value:.2f}".rstrip("0").rstrip(".")
    else:
        rendered = str(value)
    return rendered + suffix


def _print_rows(rows: list[dict[str, Any]], *, show_priority: bool) -> None:
    priority_header = f" {'PRI':>4s}" if show_priority else ""
    print(f"{'STATE':8s} {'TASK':38s} {'MODEL':16s} {'EFFORT':7s} "
          f"{'MULT':>6s}{priority_header} {'TESTS':>5s} {'PASS':>6s} "
          f"{'MIN':>5s} {'COST':>7s}")
    for row in rows:
        task = str(row["task_id"])
        if len(task) > 38:
            task = task[:35] + "..."
        rate = row.get("rate")
        pass_rate = "-" if rate is None else f"{rate * 100:.0f}%"
        priority_value = (
            f" {_fmt_number(row.get('suggest_priority', 0)):>4s}"
            if show_priority else ""
        )
        print(
            f"{str(row.get('st', '?')):8.8s} {task:38s} "
            f"{str(row['model']):16.16s} {str(row['effort']):7.7s} "
            f"{_fmt_number(row.get('mult'), 'x'):>6s}{priority_value} "
            f"{_fmt_number(row.get('total_n')):>5s} {pass_rate:>6s} "
            f"{_fmt_number(row.get('min')):>5s} "
            f"{('-' if row.get('cost') is None else '$' + _fmt_number(row['cost'])):>7s}"
        )


def _print_pick_rows(rows: list[dict[str, Any]]) -> None:
    """Emit command-only output with complete, shell-safe cell identifiers."""
    for row in rows:
        spec = f"{row['task_id']}:{row['model']}:{row['effort']}"
        benchmark = row.get("benchmark_id")
        benchmark_arg = (
            f" --benchmark {shlex.quote(str(benchmark))}" if benchmark else "")
        print(f"dradar go{benchmark_arg} --pick {shlex.quote(spec)}")


def cmd_cells(args) -> int:
    """Fetch, filter and display the public board without claiming cells."""
    cfg = _load_config()
    if not cfg.get("server"):
        sys.exit("not configured — run: dradar login --server <url>")
    benchmark = getattr(args, "benchmark", None) or cfg.get("benchmark")
    client = ApiClient(cfg["server"], cfg.get("token", ""))
    if benchmark:
        client.benchmark_id = benchmark
    try:
        table = client.table()
    except ApiError as exc:
        if exc.status_code == 404:
            sys.exit("this server doesn't support `dradar cells` yet")
        sys.exit(f"could not load cells: {exc}")

    all_rows = _rows(table)
    price_band = getattr(args, "price_band", "off-peak")
    _apply_price_band(all_rows, price_band)
    priority_available = _validate_priority_support(all_rows, args)
    _validate_ranges(args)
    rows = _filter_rows(all_rows, args)
    _sort_rows(rows, args.sort, args.reverse)
    matched = len(rows)
    shown = rows if args.all else rows[:args.limit]

    if args.json:
        print(json.dumps({
            "total": len(all_rows),
            "matched": matched,
            "shown": len(shown),
            "price_band": price_band,
            "cells": shown,
        }, ensure_ascii=False, indent=2))
        return 0

    if args.format == "pick":
        _print_pick_rows(shown)
        return 0

    print(f"{len(all_rows)} total cells; {matched} matched; showing {len(shown)}")
    if shown:
        _print_rows(shown, show_priority=priority_available)
    else:
        print("no cells match these filters")
    if not args.all and matched > len(shown):
        print(f"... {matched - len(shown)} more; use --all or --limit N")
    print("read-only snapshot — a cell can be claimed by someone else before you pick it")
    return 0


__all__ = ["cmd_cells"]
