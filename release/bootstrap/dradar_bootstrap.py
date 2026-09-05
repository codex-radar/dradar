"""Fixed-scope startup supervisor, installed through explicit hash checking.

Adapted from the public bootstrap-v1 artifact with SHA-256
feb322713cacd7ae959da97f6e6dba566268edb1bf6feb8258564f1f4d195d3c.
Preserves its one isolated-cache Git retry and minimal report schema. A private
receipt replaces stdout-JSON guessing; human progress is streamed promptly.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

import dradar_bootstrap_receipt as receipt

VERSION = "0.1.0"
CAPTURE_LIMIT = 64 * 1024
FAILURES = {"uvx-source-resolution", "uvx-runtime-create", "cli-entrypoint-missing", "bootstrap-unknown"}
CHOICES = ("install", "join_existing", "recover_stale", "use_recommended", "keep_requested", "cancel")


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "DRadar bootstrap: invalid arguments; see --help. No command was run.\n")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kw):
        return None


def _arguments(argv):
    p = Parser(prog="dradar-start", description=__doc__)
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--revision", required=True)
    p.add_argument("--server", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--locale", choices=("zh-CN", "en-US"), default="zh-CN")
    p.add_argument("--concurrency", type=int, choices=range(1, 41), metavar="N")
    p.add_argument("--confirmation")
    p.add_argument("--choice", choices=CHOICES)
    args = p.parse_args(argv)
    try:
        server = urlsplit(args.server)
        server.port
        local = server.scheme == "http" and server.hostname in {"localhost", "127.0.0.1", "::1"}
        valid = ((server.scheme == "https" or local) and server.hostname and not server.username and
                 not server.password and not server.query and not server.fragment and server.path in {"", "/"})
    except ValueError:
        valid = False
    if (not valid or args.server != args.server.strip() or any(c.isspace() for c in args.server) or
            re.fullmatch(r"[0-9a-f]{40}", args.revision) is None or
            re.fullmatch(r"[A-Za-z0-9_-]{16,256}", args.plan) is None or
            (args.confirmation is None) != (args.choice is None) or
            (args.confirmation is not None and re.fullmatch(r"dsc_[0-9a-f]{32}", args.confirmation) is None)):
        p.error("invalid fixed scope")
    return args


def runtime_argv(args):
    # Caller data selects only a revision and one plan, never an executable,
    # repository, subcommand, arbitrary path or shell fragment.
    command = ["uvx", "--no-config", "--no-env-file", "--python", "3.13", "--from",
               "git+https://github.com/SecurityMind/dradar@" + args.revision,
               "dradar-session", "--revision=" + args.revision,
               "--server=" + args.server, "--plan=" + args.plan, "--locale=" + args.locale]
    if args.concurrency is not None:
        command.append("--concurrency=" + str(args.concurrency))
    if args.confirmation is not None:
        command.extend(["--confirmation=" + args.confirmation, "--choice=" + args.choice])
    return command


def _write(stream, data):
    binary = getattr(stream, "buffer", None)
    if binary is not None:
        binary.write(data)
    else:
        stream.write(data.decode("utf-8", "replace"))
    stream.flush()


def _relay(pipe, stream, tail, secret):
    pending = b""
    try:
        read = getattr(pipe, "read1", pipe.read)
        while chunk := read(8192):
            tail.extend(chunk)
            if len(tail) > CAPTURE_LIMIT:
                del tail[:-CAPTURE_LIMIT]
            pending += chunk
            # Flush complete lines immediately; retain a short suffix across
            # partial writes so a split capability cannot leak to the terminal.
            pending = pending.replace(secret, b"[redacted]")
            line = pending.rfind(b"\n") + 1
            count = max(line, len(pending) - len(secret) + 1, 0)
            if count:
                _write(stream, pending[:count])
                pending = pending[count:]
        if pending:
            _write(stream, pending.replace(secret, b"[redacted]"))
    finally:
        pipe.close()


def run_child(command, env, args):
    with tempfile.TemporaryDirectory(prefix="dradar-start-") as directory:
        nonce = secrets.token_hex(16)
        path = Path(directory) / f"ready-{nonce}.json"
        child_env = dict(env, **{receipt.PATH_ENV: str(path), receipt.NONCE_ENV: nonce})
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
            child_env.pop(key, None)
        child_env["PYTHONNOUSERSITE"] = "1"
        stdout_tail, stderr_tail = bytearray(), bytearray()
        try:
            child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=child_env, shell=False)
        except OSError:
            return 127, b"", False, True
        threads = [threading.Thread(target=_relay, args=(pipe, stream, tail, args.plan.encode()), daemon=True)
                   for pipe, stream, tail in ((child.stdout, sys.stdout, stdout_tail),
                                             (child.stderr, sys.stderr, stderr_tail))]
        for thread in threads:
            thread.start()
        try:
            code = child.wait()
        except KeyboardInterrupt:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            code = 130
        finally:
            for thread in threads:
                thread.join(timeout=2 if child.poll() is None else None)
        return code, bytes(stderr_tail), receipt.ready(path, nonce, args.revision), False


def classify(stderr, spawn_failed=False):
    if spawn_failed:
        return "cli-entrypoint-missing"
    text = stderr.decode("utf-8", "replace").lower()
    if any(marker in text for marker in (
        "git operation failed", "failed to fetch", "unable to update", "reference not found",
        "repository not found", "failed to resolve '--with' requirement", 'failed to resolve "--with" requirement',
    )) or ("failed to build" in text and "git+" in text):
        return "uvx-source-resolution"
    if any(marker in text for marker in ("failed to download python", "no interpreter found",
                                        "failed to create virtual environment", "failed to create environment")):
        return "uvx-runtime-create"
    if any(marker in text for marker in ("no executable", "executable not found", "cli entrypoint component missing")):
        return "cli-entrypoint-missing"
    return "bootstrap-unknown"


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def report(server, run_code, failure):
    system = sys.platform if sys.platform in {"linux", "darwin", "win32"} else "unknown"
    machine = platform.machine().lower()
    machine = machine if machine in {"x86_64", "amd64", "arm64", "aarch64", "i386", "i686"} else "unknown"
    payload = {
        "schema": "dradar-failure-report-v1", "report_key": uuid.uuid4().hex,
        "source": "bootstrap", "phase": "bootstrap", "failure_kind": "bootstrap-failed",
        "failure_code": failure if failure in FAILURES else "bootstrap-unknown",
        "client_version": "bootstrap-" + VERSION,
        "platform": f"{system}-{machine}",
        "occurred_at": _now(), "detail": {},
    }
    request = Request(server.rstrip("/") + "/api/v1/runner/failures",
                      data=json.dumps(payload, separators=(",", ":")).encode(), method="POST",
                      headers={"X-DRadar-Run-Code": run_code, "Content-Type": "application/json",
                               "User-Agent": "dradar-bootstrap/1"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=3) as response:
            if 200 <= response.status < 300:
                return "received"
    except Exception:
        pass
    try:
        directory = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar")) / "failure-reports"
        if directory.is_symlink():
            return "not-sent"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (payload["report_key"] + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({**payload, "_attempts": 1, "_last_attempt_at": _now()}, handle)
        return "queued"
    except OSError:
        return "not-sent"


def main(argv=None):
    args = _arguments(argv)
    command = runtime_argv(args)
    env = dict(os.environ)
    code, stderr, ready, spawn_failed = run_child(command, env, args)
    if ready or code == 130:
        return code
    failure = classify(stderr, spawn_failed)
    if failure == "uvx-source-resolution":
        print("DRadar: retrying CLI source once with an isolated cache / 正在用隔离缓存重试一次。", flush=True)
        with tempfile.TemporaryDirectory(prefix="dradar-uv-bootstrap-") as cache:
            code, stderr, ready, spawn_failed = run_child(command, dict(env, UV_CACHE_DIR=cache), args)
        if ready or code == 130:
            return code
        failure = classify(stderr, spawn_failed)
    result = report(args.server, args.plan, failure)
    print("DRadar startup report / 启动故障报告: " + result, flush=True)
    return code or 1


if __name__ == "__main__":
    raise SystemExit(main())
