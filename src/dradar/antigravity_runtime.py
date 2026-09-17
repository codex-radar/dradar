"""Linux AGY supervisor, sent as trusted source to the current Pier environment.

A subreaper owns *all* model descendants, including double forks / new sessions.
Only ECHILD after killing and reaping them permits export. No task hooks run.
The per-run stop file is created before launch and retained until teardown, so a
late docker exec cannot start an unobserved model after a cancellation request.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import ctypes
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

STOP_SECONDS = 2.0
EXPORT_SECONDS = 4.0


def stop_children(deadline: float) -> None:
    while True:
        # Reap first. ECHILD is the kernel's proof that this subreaper has no
        # remaining descendants; a PID/session-name match alone is insufficient.
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("AGY writer shutdown unconfirmed")
        children = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
        for child in children:
            try:
                os.kill(int(child), signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.02)


def supervise(config: dict) -> int:
    control = Path(config["control"])
    artifacts = Path(config["artifacts"])
    patch = artifacts / "model.patch"
    temporary = artifacts / (".dradar-" + config["run_id"] + ".patch.tmp")
    status = {"schema": "dradar-agy-export-v1", "run_id": config["run_id"],
              "writer_stopped": False, "exported": False}
    # The model may be root inside its disposable container. Protect the
    # supervisor's memory/fds, and refuse containers capable of bypassing this.
    caps = int(next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines()
                    if line.startswith("CapBnd:")), 16)
    if caps & ((1 << 19) | (1 << 21)):  # SYS_PTRACE / SYS_ADMIN
        raise RuntimeError("AGY supervisor requires an unprivileged container")
    if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:
        raise RuntimeError("AGY supervisor cannot protect its receipt")
    key = secrets.token_bytes(32)
    # This first frame is emitted before any untrusted process is started.
    # Later task/TTY output cannot substitute another key or forge the MAC.
    print(json.dumps({"key": key.hex(), "run_id": config["run_id"]}), flush=True)
    os.umask(0o077)
    artifacts.mkdir(parents=True, exist_ok=True)
    patch.unlink(missing_ok=True)
    try:
        # Linux PR_SET_CHILD_SUBREAPER: orphaned grandchildren reparent here,
        # not to container init, even if they have called setsid().
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise RuntimeError("AGY subreaper unavailable")
        git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        git_env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        git = ["/usr/bin/git", "--no-replace-objects", "-c", "safe.directory=" + config["workspace"],
               "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-C", config["workspace"]]
        base = subprocess.check_output(git + ["rev-parse", "--verify",
            config["base"] + "^{commit}"], env=git_env, timeout=EXPORT_SECONDS, text=True).strip()
        if re.fullmatch(r"[0-9a-f]{40}", base) is None:
            raise ValueError("AGY baseline is not a full commit")
        git_identity = os.stat(Path(config["workspace"]) / ".git")
        status["base_commit"] = base
        if (control / "stop").exists():
            return 125
        with open(config["stdout"], "wb") as stdout, open(config["stderr"], "wb") as stderr:
            proc = subprocess.Popen(config["argv"], cwd=config["workspace"],
                                    stdout=stdout, stderr=stderr, start_new_session=True)
            while proc.poll() is None and not (control / "stop").exists():
                time.sleep(0.05)
            return_code = proc.poll()
            status["cancelled"] = (control / "stop").exists()
            status["agent_return_code"] = return_code
            stop_children(time.monotonic() + STOP_SECONDS)
            status["writer_stopped"] = True
        identity = os.stat(Path(config["workspace"]) / ".git")
        if (identity.st_dev, identity.st_ino) != (git_identity.st_dev, git_identity.st_ino):
            raise RuntimeError("AGY source repository replaced")
        deadline = time.monotonic() + EXPORT_SECONDS
        def run_git(args, **kwargs):
            return subprocess.run(git + args, env=git_env, check=True,
                timeout=max(0.001, deadline - time.monotonic()), **kwargs)
        # diff also applies clean/process filters while reading worktree files.
        # Disable every configured driver before add/diff; --no-textconv alone
        # does not suppress these filters. Never run a repository-owned hook.
        filters = subprocess.run(git + ["config", "--null", "--name-only", "--get-regexp",
            r"^filter\..*\.(clean|smudge|process|required)$"], env=git_env,
            capture_output=True, timeout=max(.001, deadline - time.monotonic()))
        if filters.returncode not in (0, 1) or len(filters.stdout) > 65536:
            raise RuntimeError("AGY filter configuration cannot be bounded")
        for name in filters.stdout.decode().split("\0"):
            if name:
                driver = name.rsplit(".", 1)[0]
                for setting in ("clean=", "smudge=", "process=", "required=false"):
                    git.extend(["-c", driver + "." + setting])
        run_git(["add", "-N", "--", "."], stdout=subprocess.DEVNULL)
        with temporary.open("xb") as output:
            run_git(["diff", "--no-ext-diff", "--no-textconv", "--binary", base, "--"], stdout=output)
        # No exporter/filter children may outlive publication either.
        stop_children(time.monotonic() + STOP_SECONDS)
        if temporary.stat().st_size > 64 * 1024 * 1024:
            raise RuntimeError("AGY patch exceeds artifact size limit")
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        if time.monotonic() > deadline:
            raise TimeoutError("AGY export budget exceeded")
        os.replace(temporary, patch)
        status["patch_sha256"] = digest
        status["exported"] = True
        return return_code if return_code is not None else 130
    except Exception as exc:
        status["error"] = type(exc).__name__
        patch.unlink(missing_ok=True)
        return 125
    finally:
        try:
            stop_children(time.monotonic() + STOP_SECONDS)
        except Exception:
            status["writer_stopped"] = False
            status["exported"] = False
            patch.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        payload = json.dumps(status, sort_keys=True)
        print(json.dumps({"payload": payload, "mac": hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()}), flush=True)


if __name__ == "__main__":
    sys.exit(supervise(json.loads(sys.argv[1])))
