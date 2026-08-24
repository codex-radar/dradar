"""dradar volunteer CLI: argparse wiring for login / doctor / go / resume /
rename / link-github.

The actual command implementations live in sibling modules, split by concern:
  identity.py  - login/register/rename/GitHub device-flow binding
  doctor.py    - environment preflight checks
  runloop.py   - the go/resume held-batch-and-menu execution loop
  leases.py    - inspect/release held cells, including stuck-run recovery
  local_config.py - the shared ~/.dradar/config.json + constants

This file owns only the argparse tree + `main()`, this package's
console-script entry point (see pyproject.toml). Import everything else from
the module that defines it — the single-file-era courtesy re-exports were
dropped once a grep of the public consumers showed nothing reaching through
`dradar.cli`.
"""

import argparse
import sys

from . import __version__
from .capacity import cmd_capacity
from .cells import cmd_cells
from .doctor import cmd_doctor
from .identity import cmd_link_github, cmd_login, cmd_rename, cmd_status
from .image_cache import cmd_config_set, cmd_config_show
from .leases import cmd_leases, cmd_release
from .provider_config import cmd_provider_setup, cmd_provider_status
from .runloop import (
    cmd_checkpoint_discard, cmd_checkpoints, cmd_cleanup, cmd_go,
    cmd_refill_status, cmd_refill_stop, cmd_retry_upload,
)
from .session_archive import cmd_sessions_prune

__all__ = ["main"]


def _workers_value(value: str) -> int | str:
    if value.lower() == "auto":
        return "auto"
    try:
        return int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer or 'auto'") from exc


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative number") from exc
    if parsed < 0 or parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise argparse.ArgumentTypeError("must be a non-negative finite number")
    return parsed


def _pass_rate(value: str) -> float:
    parsed = _nonnegative_float(value)
    if parsed > 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1 (0.5 = 50%)")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dradar", description="DRadar crowdtest client")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="save server + token (or register with --nickname)")
    p_login.add_argument("--server")
    p_login.add_argument("--token")
    p_login.add_argument("--nickname", help="register a new account instead of using a token")
    p_login.add_argument(
        "--tasks-root",
        help="path to this benchmark's local task directory",
    )
    p_login.add_argument(
        "--benchmark", default=None,
        help="benchmark channel (default: deep-swe)",
    )
    p_login.add_argument("--github", action="store_true",
                         help="recover your identity via GitHub (device flow)")
    p_login.set_defaults(func=cmd_login)

    p_doc = sub.add_parser("doctor", help="preflight checks")
    p_doc.add_argument(
        "--agent", choices=("dsh-minimal", "grok-build", "kimi-code", "zcode"), default=None,
        help="check only the dependencies required by this agent",
    )
    p_doc.set_defaults(func=cmd_doctor)

    p_capacity = sub.add_parser(
        "capacity", help="recommend a safe local worker count from Docker resources")
    p_capacity.set_defaults(func=cmd_capacity)

    p_cells = sub.add_parser(
        "cells", help="browse and filter the full public cell table (read-only)")
    p_cells.add_argument(
        "--model", action="append", metavar="MODEL",
        help="only this model; repeat or comma-separate for several")
    p_cells.add_argument(
        "--benchmark", default=None,
        help="benchmark channel (default: saved channel)",
    )
    p_cells.add_argument(
        "--effort", action="append", metavar="EFFORT",
        help="only this reasoning effort; repeat or comma-separate for several")
    state = p_cells.add_mutually_exclusive_group()
    state.add_argument(
        "--available", action="store_true", help="show only currently open cells")
    state.add_argument(
        "--state", action="append",
        choices=("open", "leased", "running", "queued", "cooldown"),
        help="only this state; repeat for several")
    p_cells.add_argument("--task", metavar="TEXT", help="task ID contains this text")
    p_cells.add_argument("--min-multiplier", type=float, metavar="X")
    p_cells.add_argument("--min-tests", type=_nonnegative_int, metavar="N")
    p_cells.add_argument("--max-tests", type=_nonnegative_int, metavar="N")
    p_cells.add_argument(
        "--min-minutes", type=_nonnegative_float, metavar="N",
        help="minimum estimated runtime in minutes",
    )
    p_cells.add_argument(
        "--max-minutes", type=_nonnegative_float, metavar="N",
        help="maximum estimated runtime in minutes",
    )
    p_cells.add_argument(
        "--min-cost", type=_nonnegative_float, metavar="USD",
        help="minimum estimated model cost in USD",
    )
    p_cells.add_argument(
        "--max-cost", type=_nonnegative_float, metavar="USD",
        help="maximum estimated model cost in USD",
    )
    p_cells.add_argument(
        "--price-band", choices=("off-peak", "peak"), default="off-peak",
        help="DeepSeek estimate band used by output, filters, and sorting (default: off-peak)",
    )
    p_cells.add_argument(
        "--min-pass-rate", type=_pass_rate, metavar="RATE",
        help="minimum historical pass rate, from 0 to 1 (0.5 = 50%%)",
    )
    p_cells.add_argument(
        "--max-pass-rate", type=_pass_rate, metavar="RATE",
        help="maximum historical pass rate, from 0 to 1 (0.5 = 50%%)",
    )
    p_cells.add_argument("--min-priority", type=int, metavar="N")
    p_cells.add_argument(
        "--sort", default="multiplier",
        choices=("multiplier", "tests", "pass-rate", "minutes", "cost",
                 "priority", "task", "model", "effort", "state"),
        help="sort field (default: multiplier; numeric fields highest first)")
    p_cells.add_argument(
        "--reverse", action="store_true", help="reverse the natural sort direction")
    display_count = p_cells.add_mutually_exclusive_group()
    display_count.add_argument(
        "--limit", type=_nonnegative_int, default=20, metavar="N",
        help="maximum rows to show (default: 20)")
    display_count.add_argument(
        "--all", action="store_true", help="show every matching row")
    output = p_cells.add_mutually_exclusive_group()
    output.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON")
    output.add_argument(
        "--format", choices=("table", "pick"), default="table",
        help="human table or copyable `dradar go --pick` commands (default: table)")
    p_cells.set_defaults(func=cmd_cells)

    p_ren = sub.add_parser("rename", help="change your leaderboard name (points stay)")
    p_ren.add_argument("nickname")
    p_ren.set_defaults(func=cmd_rename)

    p_st = sub.add_parser("status", help="see your own recent submissions, points, and flags")
    p_st.set_defaults(func=cmd_status)

    p_ls = sub.add_parser(
        "leases", help="list cells you currently hold and whether each is running or waiting")
    p_ls.set_defaults(func=cmd_leases)

    p_rel = sub.add_parser(
        "release", help="give held cells back immediately (running work is protected by default)")
    p_rel.add_argument("assignment_ids", nargs="*", metavar="ASSIGNMENT_ID")
    p_rel.add_argument("--all", action="store_true", help="release all waiting cells")
    p_rel.add_argument(
        "--force", action="store_true",
        help="also release running cells; stop the local runner first",
    )
    p_rel.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p_rel.set_defaults(func=cmd_release, lease_hint=True)

    p_gh = sub.add_parser("link-github",
                          help="bind your GitHub account (name + avatar on the board, cross-machine recovery)")
    p_gh.set_defaults(func=cmd_link_github)

    p_retry = sub.add_parser(
        "retry-upload",
        help="flush any trials that ran but failed to upload (also runs automatically before `go`)")
    p_retry.set_defaults(func=cmd_retry_upload, lease_hint=True)

    p_cleanup = sub.add_parser(
        "cleanup", help="safely remove settled local task files and DRadar images")
    p_cleanup.add_argument(
        "--dry-run", action="store_true",
        help="show what can be removed without deleting anything",
    )
    p_cleanup.add_argument(
        "--include-kept", action="store_true",
        help="also remove task files explicitly protected by --keep",
    )
    p_cleanup.add_argument(
        "--docker", action="store_true",
        help="also remove safe image-cache entries recorded by DRadar",
    )
    p_cleanup.add_argument(
        "--all-task-images", action="store_true",
        help="with --docker, also include legacy label-validated Pier task images",
    )
    p_cleanup.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p_cleanup.set_defaults(func=cmd_cleanup)

    p_config = sub.add_parser("config", help="inspect or change local DRadar settings")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)
    p_config_show = config_sub.add_parser("show", help="show non-sensitive local settings")
    p_config_show.set_defaults(func=cmd_config_show)
    p_config_set = config_sub.add_parser("set", help="change a supported local setting")
    p_config_set.add_argument(
        "key", choices=("image-cache-mode", "image-cache-limit-gb"),
    )
    p_config_set.add_argument("value")
    p_config_set.set_defaults(func=cmd_config_set)

    p_provider = sub.add_parser(
        "provider", help="configure local credentials for an optional model provider")
    provider_sub = p_provider.add_subparsers(
        dest="provider_command", required=True)
    p_provider_setup = provider_sub.add_parser(
        "setup", help="securely configure a provider credential or OAuth session")
    p_provider_setup.add_argument(
        "provider", choices=("deepseek", "grok", "kimi", "zcode")
    )
    p_provider_setup.set_defaults(func=cmd_provider_setup)
    p_provider_status = provider_sub.add_parser(
        "status", help="check provider readiness without displaying credentials")
    p_provider_status.add_argument(
        "provider", choices=("deepseek", "grok", "kimi", "zcode")
    )
    p_provider_status.add_argument(
        "--live", action="store_true",
        help="also verify the configured credential against the provider API",
    )
    p_provider_status.set_defaults(func=cmd_provider_status)

    p_refill = sub.add_parser("refill", help="inspect or stop continuous auto-refill")
    refill_sub = p_refill.add_subparsers(dest="refill_command", required=True)
    p_refill_status = refill_sub.add_parser("status", help="show the local refill plan")
    p_refill_status.set_defaults(func=cmd_refill_status)
    p_refill_stop = refill_sub.add_parser("stop", help="stop claiming new refill tasks")
    p_refill_stop.set_defaults(func=cmd_refill_stop)

    p_cp_list = sub.add_parser(
        "checkpoints", help="list resumable local checkpoints and their disk usage")
    p_cp_list.set_defaults(func=cmd_checkpoints)

    p_checkpoint = sub.add_parser("checkpoint", help="manage a local checkpoint")
    checkpoint_sub = p_checkpoint.add_subparsers(dest="checkpoint_command", required=True)
    p_cp_discard = checkpoint_sub.add_parser(
        "discard", help="delete a checkpoint and reopen its assignment cell")
    p_cp_discard.add_argument("checkpoint_id", metavar="ID")
    p_cp_discard.set_defaults(func=cmd_checkpoint_discard, lease_hint=True)

    p_sessions = sub.add_parser(
        "sessions", help="manage opt-in local Codex session archives")
    sessions_sub = p_sessions.add_subparsers(
        dest="sessions_command", required=True)
    p_sessions_prune = sessions_sub.add_parser(
        "prune", help="show or delete archived Codex sessions")
    p_sessions_prune.add_argument(
        "-y", "--yes", action="store_true",
        help="delete the archives (default: report disk usage only)",
    )
    p_sessions_prune.set_defaults(func=cmd_sessions_prune)

    for name, help_, is_resume in (
        ("go", "fetch an assignment and run it", False),
        ("resume", "continue the active assignment (no-op if none)", True),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
        p.add_argument(
            "--benchmark", default=None,
            help="benchmark channel (default: saved channel)",
        )
        p.add_argument("--keep", action="store_true", help="keep local job dir after upload")
        p.add_argument(
            "--archive-session", action="store_true",
            help="after a successful upload, privately archive Codex session "
                 "transcripts under ~/.dradar/history (off by default)",
        )
        p.add_argument(
            "--allow-task-drift", action="store_true",
            help="run even if your deep-swe checkout differs from the server's pinned version",
        )
        p.add_argument("--dev-agent", help=argparse.SUPPRESS)  # oracle/nop for pipeline tests
        p.add_argument(
            "--parallel", action="store_true",
            help="allow a second dradar on this machine (implies -y): sessions "
                 "split the held cells via server-side checkout, but they still "
                 "share this machine's CPU/RAM — expect slower individual runs",
        )
        p.add_argument(
            "--workers", type=_workers_value, default=1, metavar="N|auto",
            help="run up to N tasks concurrently, or use 'auto' for a "
                 "conservative Docker-based recommendation (default: 1; maximum: 40)",
        )
        p.add_argument(
            "--worker-target-file", metavar="PATH",
            help="dynamically resize a fixed worker pool by atomically writing "
                 "a number from 0 through --workers to PATH; 0 drains the pool, "
                 "and scale-down lets in-flight tasks finish",
        )
        p.add_argument("--worker-child", action="store_true", help=argparse.SUPPRESS)
        p.add_argument(
            "--refill", action="store_true",
            help="keep replenishing the held queue (requires a quota or task limit)",
        )
        p.add_argument(
            "--refill-to", type=int, metavar="N",
            help="target number of held/running tasks while refill is active",
        )
        p.add_argument(
            "--refill-harness", metavar="HARNESS",
            help="restrict every auto-refill claim to one harness (for example "
                 "kimi-code, zcode, grok-build, or codex; paid-API DSH remains "
                 "one-off)",
        )
        p.add_argument(
            "--refill-model", metavar="MODEL",
            help="optionally narrow --refill-harness to one model",
        )
        p.add_argument(
            "--refill-effort", metavar="EFFORT",
            help="optionally narrow --refill-harness to one effort level",
        )
        p.add_argument(
            "--max-tasks", type=int, metavar="N",
            help="total-task cap for this refill plan (required for Kimi Code, "
                 "ZCode, and Grok scoped refill)",
        )
        p.add_argument(
            "--max-estimated-quota-pct", type=float, metavar="PCT",
            help="estimated 7-day quota cap for the selected tier",
        )
        p.add_argument(
            "--quota-tier", choices=("plus", "pro-5x", "pro-20x"), default="plus",
            help="subscription tier used by --max-estimated-quota-pct (default: plus)",
        )
        if name == "go":
            # Free-pick instances normally require a prior web claim; these let
            # `go` claim straight from the CLI instead (no-op if you're already
            # holding cells) -- for headless/Agent use with no web step at all.
            p.add_argument(
                "--auto", nargs="?", const=5, type=int, default=None, metavar="N",
                help="top up the held batch to N cells (default 5) via the same "
                     "weighted-random suggester as the radar page's 雷达随机推荐 "
                     "button, then run; the server enforces account-specific limits",
            )
            p.add_argument(
                "--pick", action="append", metavar="TASK:MODEL:EFFORT",
                help="nothing held? claim this exact cell instead of auto-picking "
                     "(repeatable), then run — e.g. "
                     "--pick abs-module-cache-flags:gpt-5.6-sol:low",
            )
        else:
            p.add_argument(
                "--assignment", metavar="ID",
                help="resume only this assignment's local checkpoint",
            )
        p.set_defaults(func=cmd_go, resume=is_resume, lease_hint=True)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (KeyboardInterrupt, EOFError):
        # single choke point for every command: Ctrl-C during a run (the
        # batch banner promises it's safe) or EOF from piped/non-tty stdin
        # hitting input() must not dump a raw traceback. 130 = SIGINT.
        # The lease reassurance is only true of the lease-touching commands —
        # cancelling `dradar login`'s device flow must not send a brand-new
        # volunteer chasing `dradar resume` on an unconfigured machine.
        if getattr(args, "lease_hint", False):
            print("\ninterrupted — any held leases stay active; use `dradar leases` "
                  "to inspect them, `dradar resume` to continue, or `dradar release` "
                  "to give them back")
        else:
            print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
