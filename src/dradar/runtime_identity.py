"""Read-only Linux identities used by explicit, fail-closed crash recovery."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys


def process_identity(pid):
    if sys.platform != "linux" or type(pid) is not int or pid <= 0:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return {"pid": pid, "start_ticks": int(stat[19]),
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "host_id": hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()}
    except FileNotFoundError:
        return None


def docker_identity():
    # A reachable different daemon is not evidence that the original exited.
    def query(args):
        return subprocess.run(["docker", *args], capture_output=True, text=True,
                              check=True, timeout=10).stdout.strip()
    context = os.environ.get("DOCKER_CONTEXT") or query(["context", "show"])
    endpoint = (os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None)
    if not endpoint:
        rows = json.loads(query(["context", "inspect", context]))
        endpoint = rows[0]["Endpoints"]["docker"]["Host"]
    daemon_id = query(["info", "--format", "{{.ID}}"])
    if not endpoint or not daemon_id:
        raise ValueError("Docker identity is unavailable")
    return {"endpoint": endpoint, "daemon_id": daemon_id}
