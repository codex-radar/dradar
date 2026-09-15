"""Opt-in macOS contract check: official pinned CLI + inert loopback IdP only.

Run with --binary <downloaded grok-1.0.13-macos-aarch64> --output <private dir>.
Seatbelt denies all external networking. Never uses the invoking user's HOME,
credentials, API keys, settings, or model endpoint. Model readiness is NOT tested.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dradar import providers

PIN = "8669e0fdadceec25b8c159c355f427ffbd82583525d774b6ab1522197ea83b80"
PROFILE = '(version 1)(allow default)(deny network*)(allow network-outbound (remote ip "localhost:*"))'


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_suffix(".stage")
    stage.write_text(json.dumps(payload))
    stage.chmod(0o600)
    stage.replace(path)


def wait_for(check, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.025)
    raise AssertionError("bounded condition was not reached")


class IdP:
    def __init__(self):
        self.posts = []
        self.failure = False
        self.delay = 0.4
        self.guard = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, payload, status=200):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    pass

            def do_GET(self):
                if self.path != "/.well-known/openid-configuration":
                    self.reply({}, 404)
                    return
                self.reply(
                    {
                        "issuer": owner.issuer,
                        "authorization_endpoint": owner.issuer + "/authorize",
                        "token_endpoint": owner.issuer + "/token",
                        "jwks_uri": owner.issuer + "/jwks",
                        "response_types_supported": ["code"],
                        "subject_types_supported": ["public"],
                        "id_token_signing_alg_values_supported": ["RS256"],
                    }
                )

            def do_POST(self):
                assert self.path == "/token"
                form = urllib.parse.parse_qs(
                    self.rfile.read(int(self.headers["Content-Length"])).decode()
                )
                token = form.get("refresh_token", [""])[0]
                with owner.guard:
                    duplicate = token in owner.posts
                    owner.posts.append(token)
                time.sleep(owner.delay)
                if duplicate or owner.failure:
                    self.reply({"error": "invalid_grant"}, 400)
                else:
                    self.reply(
                        {
                            "access_token": "INERT-NEW-AT",
                            "refresh_token": "INERT-NEW-RT",
                            "token_type": "Bearer",
                            "expires_in": 7200,
                        }
                    )

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = "http://127.0.0.1:" + str(self.server.server_port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def payload(self, scope=None):
        return {
            scope or self.issuer + "::inert-client": {
                "key": "INERT-OLD-AT",
                "refresh_token": "INERT-OLD-RT",
                "auth_mode": "oidc",
                "create_time": "2026-01-01T00:00:00Z",
                "expires_at": "2026-01-01T01:00:00Z",
                "user_id": "inert-user",
                "email": None,
                "oidc_issuer": self.issuer,
                "oidc_client_id": "inert-client",
            }
        }

    def env(self):
        return {
            "PATH": "/usr/bin:/bin",
            "GROK_OAUTH2_ISSUER": self.issuer,
            "GROK_OAUTH2_CLIENT_ID": "inert-client",
            "GROK_TELEMETRY_ENABLED": "0",
            "GROK_TELEMETRY_MIXPANEL_ENABLED": "0",
            "GROK_TELEMETRY_TRACE_UPLOAD": "0",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    assert sys.platform == "darwin", "this harness requires macOS Seatbelt"
    binary = args.binary.resolve(strict=True)
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == PIN
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    launcher = root / "grok-sandbox"
    import shlex

    launcher.write_text(
        "#!/bin/sh\nexec /usr/bin/sandbox-exec -p "
        + shlex.quote(PROFILE)
        + " "
        + shlex.quote(str(binary))
        + ' "$@"\n'
    )
    launcher.chmod(0o700)
    report = {
        "binary_sha256": PIN,
        "external_network": "denied by Seatbelt",
        "real_credentials": False,
        "real_models": False,
        "layer": "host-native primitive only; not the Docker transport",
        "native_source_mapping": "same-version public snapshot, not exact binary source",
        "cases": [],
    }
    original_env = providers.provider_subprocess_env
    original_path = providers.grok_auth_path
    original_process = providers._grok_probe_process

    def host_native(credential, work, env):
        home = work / "native-home"
        home.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [str(launcher), "models"],
            env={**env, "HOME": str(home), "GROK_AUTH_PATH": str(credential)},
            cwd=home,
            capture_output=True,
            text=True,
            timeout=35,
            check=False,
        )

    providers._grok_probe_process = host_native
    try:
        for case in (
            "eight_mixed",
            "lock_then_adopt",
            "refresh_failure",
            "cancel_waiter",
            "kill_waiter",
            "wrong_scope",
        ):
            idp = IdP()
            base = root / case
            auth = base / "grok/auth.json"
            write(
                auth,
                idp.payload(
                    "foreign-issuer::foreign-client" if case == "wrong_scope" else None
                ),
            )
            providers.provider_subprocess_env = idp.env
            providers.grok_auth_path = lambda *_, auth=auth: auth
            before = auth.read_bytes()

            def native(index, base=base, auth=auth, idp=idp):
                home = base / f"runtime-{index}"
                home.mkdir(parents=True)
                # Directory alias models the runtime bind mount: auth AND lock
                # are the same underlying objects; this is not a Docker test.
                (home / ".grok").symlink_to(auth.parent, target_is_directory=True)
                env = {
                    **idp.env(),
                    "HOME": str(home),
                    "GROK_AUTH_PATH": str(home / ".grok/auth.json"),
                }
                return subprocess.run(
                    [str(launcher), "models"],
                    env=env,
                    cwd=home,
                    capture_output=True,
                    text=True,
                    timeout=35,
                    check=False,
                )

            def probe(index, auth=auth):
                if index % 2:
                    return providers.grok_live_error(launcher)
                return providers.grok_live_error(launcher, auth)

            try:
                if case == "eight_mixed":
                    idp.delay = 2
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        futures = [
                            pool.submit(probe if i < 4 else native, i) for i in range(8)
                        ]
                        [f.result(timeout=40) for f in futures]
                    assert idp.posts == ["INERT-OLD-RT"], (
                        "refresh token was spent more than once"
                    )
                    assert (
                        next(iter(json.loads(auth.read_text()).values()))[
                            "refresh_token"
                        ]
                        == "INERT-NEW-RT"
                    )
                elif case in ("cancel_waiter", "kill_waiter", "lock_then_adopt"):
                    lockpath = auth.with_name("auth.json.lock")
                    with lockpath.open("w+") as held:
                        fcntl.flock(held, fcntl.LOCK_EX)
                        inode = lockpath.stat().st_ino
                        home = base / "waiter"
                        home.mkdir()
                        env = {
                            **idp.env(),
                            "HOME": str(home),
                            "GROK_AUTH_PATH": str(auth),
                        }
                        child = subprocess.Popen(
                            [str(launcher), "models"],
                            env=env,
                            cwd=home,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                        log = home / ".grok/logs/unified.jsonl"
                        try:
                            wait_for(
                                lambda log=log: (
                                    log.exists()
                                    and "auth lock: attempting acquire"
                                    in log.read_text()
                                )
                            )
                            assert idp.posts == [], "native bypassed held flock"
                            if case == "lock_then_adopt":
                                successor = idp.payload()
                                entry = next(iter(successor.values()))
                                entry.update(
                                    key="INERT-NEW-AT",
                                    refresh_token="INERT-NEW-RT",
                                    create_time="2026-09-15T00:00:00Z",
                                    expires_at="2099-01-01T00:00:00Z",
                                )
                                write(auth, successor)
                                fcntl.flock(held, fcntl.LOCK_UN)
                                child.wait(timeout=30)
                                assert idp.posts == [], (
                                    "waiter did not adopt committed successor"
                                )
                                assert auth.read_bytes() != before
                            else:
                                os.killpg(
                                    child.pid,
                                    signal.SIGTERM
                                    if case == "cancel_waiter"
                                    else signal.SIGKILL,
                                )
                                child.wait(timeout=10)
                                assert auth.read_bytes() == before
                        finally:
                            if child.poll() is None:
                                os.killpg(child.pid, signal.SIGKILL)
                                child.wait()
                    assert lockpath.stat().st_ino == inode, (
                        "lock file was replaced/unlinked"
                    )
                    if case != "lock_then_adopt":
                        native(0)
                        assert idp.posts == ["INERT-OLD-RT"], (
                            "successor cannot refresh after cancellation/crash"
                        )
                else:
                    idp.failure = case == "refresh_failure"
                    probe(0)
                    if case == "wrong_scope":
                        assert idp.posts == [], "foreign scope was consumed"
                        assert auth.read_bytes() == before
                    else:
                        assert len(idp.posts) >= 1
                        # Native owns terminal failure state; DRadar must not restore
                        # the pre-probe bytes after the native decision.
                        assert not (base / "native-home/.grok/auth.json").exists()
                report["cases"].append(
                    {
                        "name": case,
                        "passed": True,
                        "token_posts": len(idp.posts),
                        "shared_lock_exists": auth.with_name("auth.json.lock").exists(),
                    }
                )
            finally:
                idp.server.shutdown()
                idp.server.server_close()
            print(case, "PASS", flush=True)
    finally:
        providers.provider_subprocess_env = original_env
        providers.grok_auth_path = original_path
        providers._grok_probe_process = original_process
        (root / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
