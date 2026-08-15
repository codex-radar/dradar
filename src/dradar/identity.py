"""Volunteer identity: login/register/rename + GitHub device-flow binding.

Split out of cli.py to separate "who am I on this server" concerns from the
doctor (environment checks) and run-loop (held-batch/menu execution) concerns
that used to share one file.
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import pending
from .api_client import ApiClient, ApiError
from .assignment_state import state_summary
from .local_config import (
    DEFAULT_BENCHMARK, HOME, _load_config, _save_config, default_tasks_root,
    tasks_root_from_config,
)
from .taskpacks import TaskPackError, ensure_benchmark_task_pack


def _auto_register(cfg: dict) -> None:
    """Zero-friction identity: first `dradar go` without a token silently
    registers an anonymous handle. All the accountability machinery (points,
    caps, bans) stays intact — only the signup step disappears."""
    for _ in range(3):  # regenerate on the unlikely 409
        nickname = f"vol-{uuid.uuid4().hex[:6]}"
        try:
            ack = ApiClient(cfg["server"], "").register(nickname)
        except ApiError as exc:
            if exc.status_code == 409:
                continue
            sys.exit(f"auto-registration failed: {exc}")
        cfg["token"] = ack["token"]
        _save_config(cfg)
        print(f"auto-registered as {ack['nickname']} "
              "(want a custom name? grab a token on the radar page instead)")
        return
    sys.exit("auto-registration failed: could not find a free handle")


def _client(cfg: dict, auto_register: bool = False) -> ApiClient:
    if not cfg.get("server"):
        sys.exit("not configured — run: dradar login --server <url> --tasks-root <path>")
    if not cfg.get("token"):
        if auto_register:
            _auto_register(cfg)
        else:
            sys.exit("not logged in — run: dradar login --server <url> --token <token>")
    client = ApiClient(cfg["server"], cfg["token"])
    if cfg.get("benchmark"):
        client.benchmark_id = cfg["benchmark"]
    return client


def cmd_login(args) -> int:
    # login rewrites the config, so it tolerates a corrupt one — it's the
    # recovery command the corrupt-config error tells the volunteer to run.
    cfg = _load_config(fresh_on_corrupt=True)
    cfg["server"] = args.server or cfg.get("server")
    cfg["token"] = args.token or cfg.get("token")
    benchmark = getattr(args, "benchmark", None) or cfg.get(
        "benchmark", DEFAULT_BENCHMARK)
    cfg["benchmark"] = benchmark
    if not cfg.get("token") and getattr(args, "nickname", None):
        # tokenless first contact: self-serve registration
        if not cfg.get("server"):
            sys.exit("need --server to register")
        try:
            ack = ApiClient(cfg["server"], "").register(args.nickname)
        except ApiError as exc:
            sys.exit(f"registration failed: {exc}")
        cfg["token"] = ack["token"]
        print(f"registered as {ack['nickname']}")
    if args.tasks_root:
        resolved_root = str(Path(args.tasks_root).expanduser().resolve())
        cfg.setdefault("tasks_roots", {})[benchmark] = resolved_root
        if benchmark == DEFAULT_BENCHMARK:
            cfg["tasks_root"] = resolved_root
    elif benchmark == DEFAULT_BENCHMARK and not cfg.get("tasks_root"):
        # New installs stay out of ~/deep-swe.  An existing explicit path is
        # deliberately preserved: upgrading must not clone a duplicate repo
        # or silently abandon a volunteer's current checkout.
        cfg["tasks_root"] = str(default_tasks_root())
        cfg.setdefault("tasks_roots", {})[benchmark] = cfg["tasks_root"]
    elif benchmark != DEFAULT_BENCHMARK and not (
        cfg.get("tasks_roots") or {}).get(benchmark
    ):
        cfg.setdefault("tasks_roots", {})[benchmark] = str(
            default_tasks_root(benchmark))
    if not cfg.get("server"):
        sys.exit("need --server")
    if getattr(args, "github", False) and not cfg.get("token"):
        # cross-machine recovery: prove GitHub identity, get the bound token
        token = _github_device_token(ApiClient(cfg["server"], ""))
        try:
            ack = ApiClient(cfg["server"], "").github_whoami(token)
        except ApiError as exc:
            if exc.status_code == 404:
                sys.exit("no dradar account is linked to that GitHub yet — run "
                         "`dradar go` then `dradar link-github` on your original machine")
            sys.exit(f"github login failed: {exc}")
        cfg["token"] = ack["token"]
        print(f"recovered identity {ack['nickname']} via GitHub")
    if not cfg.get("token"):
        # server + tasks-root only: fine — identity auto-registers on first go
        _save_config(cfg)
        print(f"configured for {cfg['server']} (identity will be auto-created "
              "on your first `dradar go`)")
        return 0
    client = ApiClient(cfg["server"], cfg["token"])
    client.benchmark_id = benchmark
    try:
        me = client.whoami()
    except (ApiError, Exception) as exc:  # noqa: BLE001 - surface anything to the user
        sys.exit(f"login failed: {exc}")
    if benchmark != DEFAULT_BENCHMARK:
        tasks_root = tasks_root_from_config(cfg, benchmark)
        if not tasks_root.is_dir():
            try:
                ensure_benchmark_task_pack(client, benchmark, tasks_root)
            except TaskPackError as exc:
                sys.exit(f"login succeeded, but task-pack setup failed: {exc}")
    _save_config(cfg)
    print(f"logged in as {me['nickname']} @ {cfg['server']} "
          f"(benchmark: {benchmark})")
    return 0


def cmd_rename(args) -> int:
    cfg = _load_config()
    client = _client(cfg)
    try:
        ack = client.rename(args.nickname)
    except ApiError as exc:
        sys.exit(f"rename failed: {exc}")
    print(f"you are now {ack['nickname']} on the leaderboard (points kept)")
    return 0


def _relative_time(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    delta = datetime.now(timezone.utc) - then.astimezone(timezone.utc)
    secs = delta.total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


_STATUS_ICON = {"graded": None, "pending": "…", "grading": "…", "error": "⚠", "invalid": "⊘"}


def cmd_status(args) -> int:
    """Read-only: the ONLY place a volunteer can see their own grading
    outcome/points/flags (the public leaderboard hides anything not
    graded-clean). Does not mutate anything — pending local uploads are
    listed but not flushed here; that is `dradar retry-upload`'s job, kept
    separate so `status` is always safe and side-effect-free to run."""
    cfg = _load_config()
    client = _client(cfg)
    try:
        data = client.my_submissions()
    except ApiError as exc:
        if exc.status_code == 404:
            sys.exit("this server doesn't support `dradar status` yet (upgrade the server)")
        sys.exit(f"status check failed: {exc}")

    print(f"{data['nickname']} — {data['points']} points")
    subs = data.get("submissions") or []
    if not subs:
        print("no submissions yet — run `dradar go` to get started")
    for s in subs[:20]:
        icon = _STATUS_ICON.get(s["grade_status"])
        if icon is None:
            icon = "✓" if (s["reward"] or 0) >= 1.0 else "✗"
        flags = f"  (flagged: {', '.join(s['flags'])})" if s["flags"] else ""
        note = ""
        if s["grade_status"] == "error":
            note = "  — infra hiccup, the cell reopens automatically for a fresh attempt"
        elif s["grade_status"] == "invalid" and s.get("client_exception"):
            note = f"  — {s['client_exception']}"
        print(f"  {s['task_id']:42s} {s['model']}@{s['effort']:7s} "
              f"{s['grade_status']:8s} {icon}  {_relative_time(s['submitted_at'])}{flags}{note}")
    if len(subs) > 20:
        print(f"  ... and {len(subs) - 20} more (showing the 20 most recent)")

    local_pending = pending.load(HOME)
    if local_pending:
        print(f"\n{len(local_pending)} trial(s) ran but haven't uploaded yet "
              "— run `dradar retry-upload` to flush them")

    # Lease visibility belongs in status as a short summary even though
    # `dradar leases` owns the detailed view. This makes the recovery command
    # discoverable precisely when a volunteer is wondering why work appears
    # stuck. Keep it best-effort for compatibility with older servers.
    try:
        get_assignment = getattr(client, "get_assignment", None)
        lease_data = get_assignment() if get_assignment else {}
        active = lease_data.get("active")
        if active is None:
            one = lease_data.get("assignment")
            active = [one] if one else []
        if active:
            print(f"\n{len(active)} active lease(s): {state_summary(active)} "
                  "— inspect with `dradar leases`; "
                  "give back with `dradar release`")
    except ApiError:
        pass
    return 0


def _github_device_token(client: ApiClient) -> str:
    """Run the browser device flow, return a GitHub access token."""
    from . import github_device
    try:
        client_id = client.github_config()["client_id"]
    except ApiError as exc:
        sys.exit(f"github linking is not available on this server: {exc}")
    try:
        flow = github_device.start(client_id)
        print("\nto link your GitHub account:")
        print(f"  1. open  {flow['verification_uri']}")
        print(f"  2. enter code  {flow['user_code']}")
        print("waiting for you to authorize in the browser (Ctrl+C to cancel)...")
        return github_device.poll(
            client_id, flow["device_code"],
            int(flow.get("interval", 5)), int(flow.get("expires_in", 900)))
    except github_device.DeviceFlowError as exc:
        sys.exit(f"github authorization failed: {exc}")


def cmd_link_github(args) -> int:
    cfg = _load_config()
    client = _client(cfg, auto_register=True)
    token = _github_device_token(client)
    try:
        ack = client.github_link(token)
    except ApiError as exc:
        sys.exit(f"linking failed: {exc}")
    print(f"\n✓ linked GitHub: {ack['github_login']} — your leaderboard entry now "
          "shows your GitHub name and avatar, and `dradar login --github` restores "
          "this identity on any machine.")
    return 0


__all__ = ["_auto_register", "_client", "cmd_login", "cmd_rename",
           "_github_device_token", "cmd_link_github", "cmd_status"]
