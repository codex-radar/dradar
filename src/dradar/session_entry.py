"""Auditable fixed-source entry; no inline loader and no OTA source switching.

uv resolves the explicit Git object before this console script runs. We check
installed provenance before exchanging a plan or preparing the environment.
Failures before uv has installed this package cannot be reported by this code.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import sys
from . import bootstrap_receipt
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def verified_source(revision: str) -> bool:
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        return False
    try:
        data = json.loads(importlib.metadata.distribution("dradar").read_text("direct_url.json") or "")
        source = urlsplit(data["url"])
        vcs = data["vcs_info"]
        return (source.scheme == "https" and source.netloc == "github.com" and
                source.path.rstrip("/") in {"/SecurityMind/dradar", "/codex-radar/dradar"} and
                not source.query and not source.fragment and
                vcs.get("vcs") == "git" and vcs.get("commit_id") == revision)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, importlib.metadata.PackageNotFoundError):
        return False


def _startup_report(server: str, run_code: str) -> str:
    """Reuse the existing bootstrap report schema; no raw error or arguments."""
    from .failure_reports import build_report, _store, _now
    from .local_config import HOME
    payload = build_report(source="bootstrap", phase="bootstrap",
                           failure_kind="bootstrap-failed", failure_code="cli-entrypoint-missing")
    # Exact existing bootstrap transport: capability only in its dedicated
    # header; it is never queued. Redirects cannot forward that capability.
    request = Request(server.rstrip("/") + "/api/v1/runner/failures",
                      data=json.dumps(payload, separators=(",", ":")).encode(),
                      headers={"X-DRadar-Run-Code": run_code, "Content-Type": "application/json",
                               "User-Agent": "dradar-bootstrap/1"}, method="POST")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=3) as response:
            if 200 <= response.status < 300:
                return "received"
    except Exception:
        pass
    try:
        _store(HOME, {**payload, "_attempts": 1, "_last_attempt_at": _now()})
        return "queued"
    except OSError:
        return "not-sent"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="dradar-session",
        description="Run and follow one website plan from an exact verified Git revision.")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--locale", choices=("zh-CN", "en-US"), default="zh-CN")
    parser.add_argument("--concurrency", type=int, choices=range(1, 41), metavar="N")
    parser.add_argument("--confirmation")
    parser.add_argument("--choice", choices=("install", "join_existing", "recover_stale", "use_recommended", "keep_requested", "cancel"))
    args = parser.parse_args(argv)
    try:
        server = urlsplit(args.server)
        server.port
    except ValueError:
        print("DRadar: invalid site / 站点格式无效。")
        return 2
    local_http = server.scheme == "http" and server.hostname in {"localhost", "127.0.0.1", "::1"}
    if ((server.scheme != "https" and not local_http) or not server.netloc or server.username or server.password or
            server.query or server.fragment or server.path not in {"", "/"} or
            re.fullmatch(r"[A-Za-z0-9_-]{16,256}", args.plan) is None):
        print("DRadar: invalid site or plan scope; no run was started / 站点或运行范围无效，未启动。")
        return 2
    if not verified_source(args.revision):
        print("DRadar: source verification failed; no run was started / 固定来源校验失败，未启动。")
        return 2
    try:
        from .cli import main as cli_main
    except (ImportError, OSError):
        print("DRadar: CLI entrypoint component missing / CLI 必需组件未能加载。", file=sys.stderr)
        if bootstrap_receipt.managed():
            return 1  # The already-loaded supervisor owns this startup report.
        try:
            report = _startup_report(args.server, args.plan)
        except Exception:
            report = "not-sent"
        print("DRadar startup report / 启动故障报告: " + report)
        return 1
    try:
        bootstrap_receipt.signal_ready(args.revision)
    except (OSError, ValueError):
        print("DRadar: bootstrap receipt failed; no plan operation started / 启动回执失败，未开始计划操作。", file=sys.stderr)
        return 1
    # Direct bundled entry avoids launcher.py's optional OTA version switch.
    command = ["run", "--follow", "--plan=" + args.plan, "--server=" + args.server,
               "--locale", args.locale]
    if args.concurrency is not None:
        command += ["--concurrency", str(args.concurrency)]
    if args.confirmation is not None:
        command += ["--confirmation", args.confirmation]
    if args.choice is not None:
        command += ["--choice", args.choice]
    return cli_main(command)


if __name__ == "__main__":
    raise SystemExit(main())
