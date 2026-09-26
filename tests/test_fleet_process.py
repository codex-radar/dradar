"""Process-level checks for the Agent -> Fleet -> detached runner boundary."""

import json
import os
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


BATCH_ID = "550e8400e29b41d4a716446655440000"


def test_detached_runner_reports_preparation_failure_without_touching_user_files(
    tmp_path,
):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def _send(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            requests.append(("GET", path))
            if path == "/api/v1/run-plans/capabilities":
                self._send({"schema_version": 1, "capabilities": ["runner-reservation-v1"],
                            "stop_generation_cas": True, "close_releases_capacity": False})
                return
            if path == "/api/v1/run-plans/identity":
                self._send({"concurrent_limit": 2, "claim_limit": 2})
                return
            if path == "/api/v1/assignment":
                self._send({
                    "active": [{
                        "assignment_id": "assignment-a",
                        "batch_id": BATCH_ID,
                        "task_id": "task-a",
                        "agent": "codex",
                        "model": "gpt-5.4",
                        "effort": "high",
                    }],
                    "free_pick": True,
                })
                return
            self.send_error(404)

        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            requests.append(("POST", path))
            if path == "/api/v1/runner/heartbeat":
                self._send({"stop_requested": False})
                return
            self._send({"ok": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        home = tmp_path / "dradar-home"
        home.mkdir(mode=0o700)
        configured_repo = tmp_path / "user-task-files"
        configured_repo.mkdir()
        sentinel = configured_repo / "my-edit.txt"
        sentinel.write_text("keep me exactly\n")
        (home / "config.json").write_text(json.dumps({
            "tasks_root": str(configured_repo / "tasks"),
        }))
        credentials = home / "plan.json"
        credentials.write_text(json.dumps({
            "credential_kind": "run_plan_v1",
            "server": f"http://127.0.0.1:{server.server_port}",
            "token": "drp_process_test_only",
            "benchmark": "deep-swe",
            "batch_id": BATCH_ID,
            "logical_session_id": "drl_process_test_only",
            "credential_generation": 0,
            "plan_id": "plan-process-test",
            "plan": {"points_tier": "plus"},
        }))
        credentials.chmod(0o600)
        source = Path(__file__).parent.parent / "src"
        script = textwrap.dedent(f"""
            import json, os, signal, time
            from pathlib import Path
            from dradar import fleet

            try:
                fleet.add_batch(
                    batch_id={BATCH_ID!r},
                    workers=1,
                    retry=True,
                    credentials_file=Path({str(credentials)!r}),
                    plan_id="plan-process-test",
                )
            except fleet.FleetStartupError as exc:
                print(json.dumps({{
                    "code": exc.code,
                    "message": exc.user_message,
                }}, ensure_ascii=False))
            else:
                raise SystemExit("detached runner was falsely reported as started")
            state = fleet._read_json(fleet._state_path(fleet.HOME)) or {{}}
            pid = state.get("pid")
            if isinstance(pid, int):
                os.kill(pid, signal.SIGTERM)
                for _ in range(100):
                    if not fleet._pid_alive(pid):
                        break
                    time.sleep(0.02)
        """)
        env = {
            **os.environ,
            "DRADAR_HOME": str(home),
            "PYTHONPATH": str(source),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }

        completed = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        payload = json.loads(completed.stdout.strip())

        assert payload["code"] == "task_environment_update_failed"
        assert "已有本地文件没有被修改" in payload["message"]
        assert all(
            word not in payload["message"]
            for word in ("Fleet", "batch", "provider", "refill")
        )
        assert sentinel.read_text() == "keep me exactly\n"
        assert not (configured_repo / "tasks").exists()
        assert ("POST", "/api/v1/run-plans/stop") in requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
