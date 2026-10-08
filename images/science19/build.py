"""Build pinned official Science Agent contexts; separately verify anonymous pulls."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def require(ok, message):
    if not ok:
        raise ValueError(message)


def run(command, *, cwd=None, env=None, timeout=1800):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{command[0]} exited {result.returncode}: {result.stderr.decode(errors='replace')[-3000:]}")
    return result.stdout


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def gate(args):
    raw = (args.root / "BATCH.json").read_bytes()
    batch = json.loads(raw)
    require(sha(raw) == args.batch_hash, "fixed Science batch hash differs")
    require(os.environ.get("GITHUB_SHA") == args.reviewed, "reviewed commit differs")
    require(os.environ.get("GITHUB_ACTOR") == "SecurityMind", "publisher identity differs")
    require(os.environ.get("GITHUB_REPOSITORY") == "codex-radar/dradar", "repository differs")
    require(os.environ.get("GITHUB_REF") == "refs/heads/codex/science19-official-build-20261008", "branch differs")
    require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "explicit dispatch required")
    require(batch["schema"] == "dradar.science19-official-source-build/1", "batch schema differs")
    require(len(batch["rows"]) == 19 and [e["display_number"] for e in batch["rows"]] == [f"{n:03}" for n in range(46, 65)], "fixed19 membership differs")
    require(len({e["task_id"] for e in batch["rows"]}) == 19, "duplicate Science task")
    for entry in batch["rows"]:
        require(entry["target_image"] == "ghcr.io/codex-radar/dradar-env-science-" + entry["task_id"], "Science image scope differs")
        require(entry["target_verifier_image"] == "ghcr.io/codex-radar/dradar-verifier-science-" + entry["task_id"], "Science verifier scope differs")
        require(entry["source_commit"] == batch["source_commit"], "source commit differs")
    return batch, next(e for e in batch["rows"] if e["display_number"] == args.number)


def source(entry, out, *, role="agent"):
    repo = out / "official-source"
    repo.mkdir()
    run(["git", "init", "--quiet", str(repo)])
    run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/harbor-framework/terminal-bench-science.git"])
    run(["git", "-C", str(repo), "fetch", "--filter=blob:none", "--depth=1", "origin", entry["source_commit"]], timeout=600)
    task = entry["source_task_path"]
    directory = "environment" if role == "agent" else "tests"
    run(["git", "-C", str(repo), "sparse-checkout", "init", "--no-cone"])
    run(["git", "-C", str(repo), "sparse-checkout", "set", "--no-cone", "/LICENSE", "/" + task + "/" + directory + "/", "/" + task + "/task.toml", "/" + task + "/instruction.md"])
    run(["git", "-C", str(repo), "checkout", "--detach", entry["source_commit"]], timeout=600)
    require(run(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip() == entry["source_commit"], "actual source checkout differs")
    context = repo / task / directory
    listed = run(["git", "-C", str(repo), "ls-tree", "-r", "-z", entry["source_commit"], "--", task + "/" + directory]).split(b"\0")
    expected = {}
    for raw in listed:
        if not raw:
            continue
        record, name = raw.split(b"\t", 1)
        mode, kind, blob = record.decode().split()
        require(kind == "blob" and mode in ("100644", "100755"), "unsupported official context file type")
        path = repo / name.decode()
        require(path.is_file() and not path.is_symlink(), "official context file is missing or a link")
        data = path.read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        require(actual == blob, "official context Git blob differs")
        expected[path.relative_to(context).as_posix()] = {"git_blob": blob, "mode": mode, "size_bytes": len(data), "sha256": sha(data)}
    actual_files = {p.relative_to(context).as_posix() for p in context.rglob("*") if p.is_file()}
    require(actual_files == set(expected), "official build context has missing or extra files")
    require(sha((repo / task / "task.toml").read_bytes()) == entry["task_toml_sha256"], "original task config differs")
    require(sha((repo / task / "instruction.md").read_bytes()) == entry["statement_sha256"], "original statement differs")
    dockerfile_hash = entry["dockerfile_sha256"] if role == "agent" else entry["verifier_dockerfile_sha256"]
    require(sha((context / "Dockerfile").read_bytes()) == dockerfile_hash, "original Dockerfile differs")
    digest = hashlib.sha256()
    for name in ("instruction.md", "task.toml"):
        data = (repo / task / name).read_bytes()
        digest.update(name.encode()); digest.update(data if b"\0" in data else data.replace(b"\r\n", b"\n"))
    for name in sorted(expected):
        data = (context / name).read_bytes()
        digest.update(name.encode()); digest.update(data if b"\0" in data else data.replace(b"\r\n", b"\n"))
    if role == "agent":
        require(digest.hexdigest() == entry["task_content_hash"], "fixed DRadar original task content binding differs; do not silently replace task")
    write(out / "SOURCE_RECEIPT.json", {"source_commit": entry["source_commit"], "task": entry["task_id"], "task_content_hash": entry["task_content_hash"], "source_context_hash": digest.hexdigest(), "context": expected, "image_role": role, "build_context_directory": directory, "solution_authoring_in_context": False})
    (out / "UPSTREAM_LICENSE").write_bytes((repo / "LICENSE").read_bytes())
    return context


def inspect(reference, out, env):
    prefix = ["skopeo", "inspect"]
    if env.get("REGISTRY_AUTH_FILE"):
        prefix += ["--authfile", env["REGISTRY_AUTH_FILE"]]
    manifest = run(prefix + ["--raw", "docker://" + reference], env=env, timeout=90)
    config = run(prefix + ["--config", "--raw", "docker://" + reference], env=env, timeout=90)
    m, c = json.loads(manifest), json.loads(config)
    require(m["config"]["digest"] == "sha256:" + sha(config) and m["config"]["size"] == len(config), "registry image config digest differs")
    require(c["os"] == "linux" and c["architecture"] == "amd64", "published platform differs")
    (out / "MANIFEST.json").write_bytes(manifest); (out / "CONFIG.json").write_bytes(config)
    return {"digest": "sha256:" + sha(manifest), "config_digest": "sha256:" + sha(config), "layers": m["layers"], "rootfs_diff_ids": c["rootfs"]["diff_ids"], "workdir": c.get("config", {}).get("WorkingDir", ""), "user": c.get("config", {}).get("User", ""), "platform": "linux/amd64"}


def build(args, entry):
    role = "verifier" if args.phase == "verifier-build" else "agent"
    context = source(entry, args.out, role=role)
    tag = (role + "-" + entry["source_commit"][:12] + "-" + entry["task_content_hash"][:12]
           + "-" + args.reviewed[:12] + "-" + os.environ["GITHUB_RUN_ID"]
           + "-" + os.environ["GITHUB_RUN_ATTEMPT"])
    target = entry["target_image"] if role == "agent" else entry["target_verifier_image"]
    reference = target + ":" + tag
    environment = dict(os.environ)
    # Source-build provenance metadata is explicit; no upstream prebuilt image
    # identity is claimed. Original Dockerfile and context remain byte exact.
    with (args.out / "BUILD.log").open("wb") as log:
        proc = subprocess.run(["docker", "buildx", "build", "--platform", "linux/amd64", "--provenance=false", "--sbom=false", "--push", "--metadata-file", str(args.out / "BUILD_METADATA.json"), "--file", str(context / "Dockerfile"), "--tag", reference, "--label", "org.opencontainers.image.source=https://github.com/codex-radar/dradar", "--label", "org.opencontainers.image.revision=" + args.reviewed, str(context)], env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=int(entry["build_timeout_sec"] if role == "agent" else entry["verifier_build_budget_sec"]))
    require(proc.returncode == 0, "official-source Agent build failed; see preserved BUILD.log")
    image = inspect(reference, args.out, environment)
    metadata = json.loads((args.out / "BUILD_METADATA.json").read_text())
    require(metadata["containerimage.digest"] == image["digest"], "built and registry digest differ")
    write(args.out / "BUILD_RECEIPT.json", {"status": "BUILT_AND_PUSHED_PUBLIC_UNVERIFIED", "image_role": role, "number": entry["display_number"], "task_id": entry["task_id"], "task_content_hash": entry["task_content_hash"], "source_commit": entry["source_commit"], "image_kind": "official-source-build-not-upstream-prebuilt", "target_image": target, "tag": tag, "image": image, "ci_commit": args.reviewed, "run_id": os.environ["GITHUB_RUN_ID"], "added_metadata_labels": ["org.opencontainers.image.source", "org.opencontainers.image.revision"], "model_calls": 0, "official_grader_calls": 0})


def anonymous(args, entry):
    role = "verifier" if args.phase == "verifier-anonymous" else "agent"
    build_phase = "verifier-build" if role == "verifier" else "build"
    target = entry["target_image"] if role == "agent" else entry["target_verifier_image"]
    previous = args.out / "build-receipt"
    run(["gh", "run", "download", args.build_run, "--repo", "codex-radar/dradar", "--name", "science19-" + build_phase + "-" + args.number, "--dir", str(previous)], timeout=120)
    receipt = json.loads((previous / "BUILD_RECEIPT.json").read_text())
    build_commit = args.build_reviewed or args.reviewed
    source_run = json.loads(run(["gh", "api", "repos/codex-radar/dradar/actions/runs/" + args.build_run], timeout=90))
    require(source_run["head_sha"] == build_commit and source_run["head_branch"] == "codex/science19-official-build-20261008"
            and source_run["actor"]["login"] == "SecurityMind" and source_run["event"] == "workflow_dispatch", "actual source build run binding differs")
    require(receipt["ci_commit"] == build_commit and str(receipt["run_id"]) == args.build_run
            and receipt["source_commit"] == entry["source_commit"]
            and receipt["status"] == "BUILT_AND_PUSHED_PUBLIC_UNVERIFIED"
            and receipt["image_kind"] == "official-source-build-not-upstream-prebuilt"
            and receipt.get("image_role", "agent") == role and receipt["target_image"] == target
            and receipt["number"] == args.number and receipt["task_id"] == entry["task_id"]
            and receipt["task_content_hash"] == entry["task_content_hash"], "build receipt binding differs")
    package = json.loads(run(["gh", "api", "orgs/codex-radar/packages/container/" + target.split("/")[-1]], timeout=90))
    require(package["visibility"] == "public" and package.get("repository", {}).get("full_name") == "codex-radar/dradar", "package public visibility/repository association is unverified")
    write(args.out / "PUBLIC_PACKAGE.json", {k: package[k] for k in ("id", "name", "visibility", "html_url")})
    environment = dict(os.environ)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "DOCKER_AUTH_CONFIG", "REGISTRY_AUTH_FILE", "DOCKER_HOST", "DOCKER_CONTEXT"):
        environment.pop(name, None)
    config = args.out / "empty-docker-config"; config.mkdir(mode=0o700)
    (config / "config.json").write_text("{}\n")
    environment["DOCKER_CONFIG"] = str(config)
    empty_auth = args.out / "empty-registry-auth.json"
    empty_auth.write_text('{"auths":{}}\n'); empty_auth.chmod(0o600)
    environment["REGISTRY_AUTH_FILE"] = str(empty_auth)
    socket = args.out / "anonymous.sock"
    data = args.out / "fresh-docker-data"; executor = args.out / "fresh-exec"
    require(not data.exists() and not executor.exists(), "fresh anonymous Docker roots required")
    docker = ["docker", "--host", "unix://" + str(socket), "--config", str(config)]
    daemon = None
    success = None
    with (args.out / "DOCKERD.log").open("wb") as log:
        try:
            daemon = subprocess.Popen(["sudo", "-n", "dockerd", "--host", "unix://" + str(socket), "--data-root", str(data), "--exec-root", str(executor), "--pidfile", str(args.out / "dockerd.pid"), "--bridge=none", "--iptables=false", "--ip-masq=false", "--ip-forward=false", "--storage-driver=overlay2"], env=environment, stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                require(daemon.poll() is None, "anonymous daemon exited")
                try:
                    info = json.loads(run(docker + ["info", "--format", "{{json .}}"], env=environment, timeout=3))
                    if info["DockerRootDir"] == str(data):
                        break
                except Exception:
                    time.sleep(.5)
            else:
                raise RuntimeError("anonymous daemon readiness timed out")
            require(not run(docker + ["image", "ls", "-aq"], env=environment).strip(), "anonymous Docker state is not empty")
            reference = target + "@" + receipt["image"]["digest"]
            actual = inspect(reference, args.out, environment)
            require(actual == receipt["image"], "anonymous manifest/config/complete rootfs differs")
            with (args.out / "ANONYMOUS_PULL.log").open("wb") as pull_log:
                pull = subprocess.run(docker + ["pull", "--platform", "linux/amd64", reference], env=environment, stdout=pull_log, stderr=subprocess.STDOUT, timeout=1800)
            require(pull.returncode == 0, "full anonymous pull failed")
            image = json.loads(run(docker + ["image", "inspect", reference], env=environment))[0]
            require(image["Id"] == actual["config_digest"] and image["RootFS"]["Layers"] == actual["rootfs_diff_ids"], "complete pulled filesystem identity differs")
            probe_command = ("test ! -e /tests && test ! -e /solution && id && pwd" if role == "agent" else
                "set -eu; test -f /tests/test.sh; id; pwd; stat -c '%u:%g:%a' /tests /tests/test.sh; if command -v python3 >/dev/null 2>&1; then python3 --version; else printf 'python3_not_available\\n'; fi; if command -v Rscript >/dev/null 2>&1; then Rscript --version; fi")
            probe_command += "; printf '\\nDRADAR_RUNTIME_UID=%s\\nDRADAR_RUNTIME_GID=%s\\nDRADAR_RUNTIME_CWD=%s\\nDRADAR_RUNTIME_HOME=%s\\n' \"$(id -u)\" \"$(id -g)\" \"$(pwd)\" \"${HOME-}\""
            probe = run(docker + ["run", "--rm", "--network", "none", "--entrypoint", "sh", reference, "-c", probe_command], env=environment, timeout=60).decode()
            require(json.loads((config / "config.json").read_text()) == {} and json.loads(empty_auth.read_text()) == {"auths": {}}, "anonymous credentials changed")
            success = {"status": "FULL_ANONYMOUS_PULL_AND_NO_MODEL_ENV_PROBE_PASSED", "image_role": role, "number": args.number, "task_id": entry["task_id"], "target_image": reference, "image": actual, "empty_auth_config": True, "fresh_docker_state": True, "full_pull": True, "environment_probe": probe, "model_calls": 0, "official_grader_calls": 0, "run_id": os.environ["GITHUB_RUN_ID"]}
            success["observed_runtime"] = parse_runtime(probe)
        finally:
            if daemon is not None:
                pidfile = args.out / "dockerd.pid"
                require(pidfile.is_file(), "owned daemon PID is unconfirmed")
                pid = int(pidfile.read_text().strip())
                cmdline = Path("/proc") / str(pid) / "cmdline"
                if cmdline.exists():
                    parts = cmdline.read_bytes().split(b"\0")
                    require(b"--data-root" in parts and parts[parts.index(b"--data-root") + 1] == os.fsencode(data), "refuse to stop unrelated daemon")
                    run(["sudo", "-n", "kill", "-TERM", str(pid)], env=environment, timeout=15)
                daemon.wait(timeout=60)
                require(not cmdline.exists(), "owned daemon physical exit is unconfirmed")
                write(args.out / "DAEMON_EXIT.json", {"pid": pid, "returncode": daemon.returncode, "physical_exit_confirmed": True})
    if success is not None:
        success["daemon_exit_confirmed"] = True
        write(args.out / "ANONYMOUS_RECEIPT.json", success)


def parse_runtime(probe):
    values = {}
    for line in probe.splitlines():
        if line.startswith("DRADAR_RUNTIME_"):
            key, value = line.split("=", 1)
            require(key not in values, "duplicate runtime observation")
            values[key] = value
    require(set(values) == {"DRADAR_RUNTIME_UID", "DRADAR_RUNTIME_GID", "DRADAR_RUNTIME_CWD", "DRADAR_RUNTIME_HOME"}, "runtime observation is incomplete")
    return {"uid": int(values["DRADAR_RUNTIME_UID"]), "gid": int(values["DRADAR_RUNTIME_GID"]),
            "cwd": values["DRADAR_RUNTIME_CWD"], "home": values["DRADAR_RUNTIME_HOME"], "home_observed": True}


def runtime(args, entry):
    """Only add missing UID/GID/HOME observations for already qualified images.

    Full anonymous pull qualification is reused; this does not claim a new one.
    The fresh hosted runner may download the immutable image to run the probe.
    """
    previous = args.out / "build-receipt"
    run(["gh", "run", "download", args.build_run, "--repo", "codex-radar/dradar", "--name", "science19-build-" + args.number, "--dir", str(previous)], timeout=120)
    receipt = json.loads((previous / "BUILD_RECEIPT.json").read_text())
    source_run = json.loads(run(["gh", "api", "repos/codex-radar/dradar/actions/runs/" + args.build_run], timeout=90))
    require(source_run["head_sha"] == args.build_reviewed and source_run["actor"]["login"] == "SecurityMind"
            and source_run["head_branch"] == "codex/science19-official-build-20261008" and source_run["event"] == "workflow_dispatch", "runtime source build run differs")
    require(receipt["ci_commit"] == args.build_reviewed and str(receipt["run_id"]) == args.build_run
            and receipt["source_commit"] == entry["source_commit"] and receipt["task_id"] == entry["task_id"]
            and receipt["task_content_hash"] == entry["task_content_hash"] and receipt["target_image"] == entry["target_image"]
            and receipt["status"] == "BUILT_AND_PUSHED_PUBLIC_UNVERIFIED" and receipt.get("image_role", "agent") == "agent", "runtime image receipt differs")
    environment = dict(os.environ)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "DOCKER_AUTH_CONFIG", "REGISTRY_AUTH_FILE", "DOCKER_HOST", "DOCKER_CONTEXT"):
        environment.pop(name, None)
    config = args.out / "empty-docker-config"; config.mkdir(mode=0o700); (config / "config.json").write_text("{}\n")
    environment["DOCKER_CONFIG"] = str(config)
    reference = entry["target_image"] + "@" + receipt["image"]["digest"]
    command = "set -eu; printf 'DRADAR_RUNTIME_UID=%s\\nDRADAR_RUNTIME_GID=%s\\nDRADAR_RUNTIME_CWD=%s\\nDRADAR_RUNTIME_HOME=%s\\n' \"$(id -u)\" \"$(id -g)\" \"$(pwd)\" \"${HOME-}\""
    raw = run(["docker", "--config", str(config), "run", "--rm", "--platform", "linux/amd64", "--network", "none", "--entrypoint", "sh", reference, "-c", command], env=environment, timeout=1800).decode()
    write(args.out / "RUNTIME_RECEIPT.json", {"status": "ORIGINAL_AGENT_RUNTIME_OBSERVED_NO_MODEL", "number": args.number,
            "task_id": entry["task_id"], "target_image": reference, "observed_runtime": parse_runtime(raw),
            "probe_stdout": raw, "full_anonymous_qualification_reused_from_run": "37712944168", "build_run": args.build_run,
            "build_commit": args.build_reviewed, "probe_run": os.environ["GITHUB_RUN_ID"], "model_calls": 0, "official_grader_calls": 0})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("build", "anonymous", "verifier-build", "verifier-anonymous", "runtime"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reviewed", required=True)
    parser.add_argument("--batch-hash", required=True)
    parser.add_argument("--number", required=True)
    parser.add_argument("--build-run", default="")
    parser.add_argument("--build-reviewed", default="")
    args = parser.parse_args()
    args.out = args.out.absolute(); args.out.mkdir(parents=True, exist_ok=False)
    try:
        _, entry = gate(args)
        (runtime if args.phase == "runtime" else build if args.phase in ("build", "verifier-build") else anonymous)(args, entry)
    except BaseException as error:
        write(args.out / "FAILURE.json", {"phase": args.phase, "number": args.number, "error_type": type(error).__name__, "error": str(error), "model_calls": 0, "official_grader_calls": 0})
        raise


if __name__ == "__main__":
    main()
