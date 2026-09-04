#!/usr/bin/env python3
"""Dependency-free bridge for failures that happen before ``dradar`` starts.

Launchers invoke this after a failed uv/uvx subprocess while preserving that
subprocess exit code. The helper accepts a bounded class, never stderr or a
command. Failed delivery queues the same scrubbed payload for the CLI to drain.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib import request


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def queue(home: Path, report: dict) -> None:
    directory = home / "failure-reports"
    directory.mkdir(parents=True, exist_ok=True)
    key = report["report_key"]
    report["_attempts"] = 1
    report["_last_attempt_at"] = now()
    temporary = directory / f".{key}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(report, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, directory / f"{key}.json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--failure-code", required=True, choices=(
        "uvx-source-resolution", "uvx-runtime-create",
        "cli-entrypoint-missing", "bootstrap-unknown",
    ))
    parser.add_argument("--client-version", default="unknown")
    parser.add_argument("--config", type=Path, default=Path.home() / ".dradar" / "config.json")
    args = parser.parse_args()
    home = args.config.parent
    report = {
        "schema": "dradar-failure-report-v1",
        "report_key": uuid.uuid4().hex,
        "source": "bootstrap",
        "phase": "bootstrap",
        "failure_kind": "bootstrap-failed",
        "failure_code": args.failure_code,
        "client_version": args.client_version[:64],
        "platform": f"{sys.platform}-{platform.machine() or 'unknown'}"[:64].lower(),
        "occurred_at": now(),
        "detail": {},
    }
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        server = str(config["server"]).rstrip("/")
        token = str(config["token"])
        body = json.dumps(report, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            server + "/api/v1/runner/failures", data=body, method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=3) as response:
            if response.status >= 300:
                raise OSError("failure report rejected")
    except Exception:
        try:
            queue(home, report)
        except OSError:
            print("failure report: send failed", file=sys.stderr)
            return 1
        print("failure report: send failed; queued for retry", file=sys.stderr)
        return 0
    print("failure report: received", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
