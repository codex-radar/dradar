"""Opt-in Linux PID-namespace test of the actual CLI/Fleet/API claim path.

Run with DRADAR_FLEET_PROC_TEST=1 in an isolated PID namespace. The only
server is loopback and Docker is an empty-inventory fixture; no provider runs.
"""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("DRADAR_FLEET_PROC_TEST") != "1",
    reason="requires explicit isolated Linux PID namespace",
)
ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("foreign", [False, True])
def test_cli_claim_uses_real_argv_before_any_allocation(tmp_path, foreign):
    posts = []
    account, aid, batch = "a" * 32, "b" * 32, "c" * 32
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def send(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/v1/run-plans/capabilities":
                self.send({"schema_version": 1, "capabilities": [
                    "runner-reservation-v1", "explicit-pick-batch-v1"],
                    "stop_generation_cas": True, "close_releases_capacity": False})
            elif path == "/api/v1/whoami":
                self.send({"volunteer_id": account, "concurrent_limit": 2})
            elif path == "/api/v1/table":
                combo = {"model": "gpt-6-sol", "effort": "high", "agent": "codex", "provider": "openai"}
                self.send({"benchmark_id": "deep-swe", "combos": [combo],
                           "cells": {"fixture|gpt-6-sol|high": combo}})
            else:
                self.send_error(404)
        def do_POST(self):
            path = urlparse(self.path).path
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = {key: values[0] for key, values in parse_qs(raw.decode()).items()}
            posts.append((path, body))
            if path == "/api/v1/assignment/claim":
                self.send({"assignment": {"assignment_id": aid, "batch_id": batch,
                    "task_id": "fixture", "model": "gpt-6-sol", "effort": "high"},
                    "selection_batch_created": True, "selection_id": body["selection_id"]})
            else:
                self.send_error(404)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "server": f"http://127.0.0.1:{server.server_port}", "token": "fixture-only",
        "benchmark": "deep-swe"}))
    (home / "ota").mkdir()
    (home / "ota/discovery.json").write_text(json.dumps({"next_check_at": time.time() + 3600}))
    tools = tmp_path / "tools"
    tools.mkdir()
    docker = tools / "docker"
    docker.write_text("#!/bin/sh\n[ \"$1\" = ps ] && exit 0\nexit 97\n")
    docker.chmod(0o700)
    env = {"HOME": str(tmp_path), "DRADAR_HOME": str(home),
           "PATH": f"{tools}:/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src"),
           "NO_PROXY": "127.0.0.1,localhost", "PYTHONIOENCODING": "utf-8"}
    node = shutil.which("node")
    assert node, "Linux acceptance fixture requires native Node"
    if foreign:
        other_home = tmp_path / "other-home"
        other_home.mkdir()
        other = subprocess.Popen([sys.executable, "-m", "dradar.cli", "fleet", "serve", "--internal"],
            env={**env, "DRADAR_HOME": str(other_home), "DRADAR_FLEET_LAUNCH_ID": "foreign-fixture"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if (other_home / "fleet/state.json").exists():
                break
            assert other.poll() is None
            time.sleep(.05)
        assert (other_home / "fleet/state.json").exists()
    else:
        other = subprocess.Popen([node, "-e", "setInterval(() => {}, 1000)", "--",
            "user's unclosed \"quote\nwith dradar go --worker-child in a single argument"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    command = [sys.executable, "-m", "dradar.cli", "fleet", "claim", "--pick", "fixture:gpt-6-sol:high",
               "--window-id", "0258-fixture", "--workers", "1", "--max-new", "1",
               "--max-concurrent", "1", "--deadline", deadline]
    try:
        assert other.poll() is None
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=35)
        assert result.returncode == 0, result.stdout + result.stderr
        if foreign:
            assert "another DRadar runner process may be active" in result.stdout
            assert "claimed 0" in result.stdout
            assert posts == []
        else:
            assert "claimed 1" in result.stdout
            assert [p for p, _ in posts] == ["/api/v1/assignment/claim"]
            assert posts[0][1]["task_id"] == "fixture"
        assert "no model was started" in result.stdout
    finally:
        other.terminate()
        other.wait(timeout=10)
        state_path = home / "fleet/state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            # This fixture's detached controller is the only selected PID;
            # namespace execution guarantees no production process is visible.
            try:
                os.kill(state["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        server.shutdown()
        server.server_close()
