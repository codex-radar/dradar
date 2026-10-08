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
        require(entry["source_commit"] == batch["source_commit"], "source commit differs")
    return batch, next(e for e in batch["rows"] if e["display_number"] == args.number)


def source(entry, out):
    repo = out / "official-source"
    repo.mkdir()
    run(["git", "init", "--quiet", str(repo)])
    run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/harbor-framework/terminal-bench-science.git"])
    run(["git", "-C", str(repo), "fetch", "--filter=blob:none", "--depth=1", "origin", entry["source_commit"]], timeout=600)
    task = entry["source_task_path"]
    run(["git", "-C", str(repo), "sparse-checkout", "init", "--no-cone"])
    run(["git", "-C", str(repo), "sparse-checkout", "set", "--no-cone", "/LICENSE", "/" + task + "/environment/", "/" + task + "/task.toml", "/" + task + "/instruction.md"])
    run(["git", "-C", str(repo), "checkout", "--detach", entry["source_commit"]], timeout=600)
    require(run(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip() == entry["source_commit"], "actual source checkout differs")
    context = repo / task / "environment"
    listed = run(["git", "-C", str(repo), "ls-tree", "-r", "-z", entry["source_commit"], "--", task + "/environment"]).split(b"\0")
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
    require(sha((context / "Dockerfile").read_bytes()) == entry["dockerfile_sha256"], "original Dockerfile differs")
    digest = hashlib.sha256()
    for name in ("instruction.md", "task.toml"):
        data = (repo / task / name).read_bytes()
        digest.update(name.encode()); digest.update(data if b"\0" in data else data.replace(b"\r\n", b"\n"))
    for name in sorted(expected):
        data = (context / name).read_bytes()
        digest.update(name.encode()); digest.update(data if b"\0" in data else data.replace(b"\r\n", b"\n"))
    require(digest.hexdigest() == entry["task_content_hash"], "fixed DRadar original task content binding differs; do not silently replace task")
    write(out / "SOURCE_RECEIPT.json", {"source_commit": entry["source_commit"], "task": entry["task_id"], "task_content_hash": digest.hexdigest(), "context": expected, "agent_context_only": True, "tests_solution_authoring_in_context": False})
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
    context = source(entry, args.out)
    tag = "agent-" + entry["source_commit"][:12] + "-" + entry["task_content_hash"][:12] + "-" + args.reviewed[:12]
    reference = entry["target_image"] + ":" + tag
    environment = dict(os.environ)
    # Source-build provenance metadata is explicit; no upstream prebuilt image
    # identity is claimed. Original Dockerfile and context remain byte exact.
    with (args.out / "BUILD.log").open("wb") as log:
        proc = subprocess.run(["docker", "buildx", "build", "--platform", "linux/amd64", "--provenance=false", "--sbom=false", "--push", "--metadata-file", str(args.out / "BUILD_METADATA.json"), "--file", str(context / "Dockerfile"), "--tag", reference, "--label", "org.opencontainers.image.source=https://github.com/codex-radar/dradar", "--label", "org.opencontainers.image.revision=" + args.reviewed, str(context)], env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=int(entry["build_timeout_sec"]))
    require(proc.returncode == 0, "official-source Agent build failed; see preserved BUILD.log")
    image = inspect(reference, args.out, environment)
    metadata = json.loads((args.out / "BUILD_METADATA.json").read_text())
    require(metadata["containerimage.digest"] == image["digest"], "built and registry digest differ")
    write(args.out / "BUILD_RECEIPT.json", {"status": "BUILT_AND_PUSHED_PUBLIC_UNVERIFIED", "number": entry["display_number"], "task_id": entry["task_id"], "task_content_hash": entry["task_content_hash"], "source_commit": entry["source_commit"], "image_kind": "official-source-build-not-upstream-prebuilt", "target_image": entry["target_image"], "tag": tag, "image": image, "ci_commit": args.reviewed, "run_id": os.environ["GITHUB_RUN_ID"], "added_metadata_labels": ["org.opencontainers.image.source", "org.opencontainers.image.revision"], "model_calls": 0, "official_grader_calls": 0})


def anonymous(args, entry):
    previous = args.out / "build-receipt"
    run(["gh", "run", "download", args.build_run, "--repo", "codex-radar/dradar", "--name", "science19-build-" + args.number, "--dir", str(previous)], timeout=120)
    receipt = json.loads((previous / "BUILD_RECEIPT.json").read_text())
    require(receipt["ci_commit"] == args.reviewed and receipt["task_id"] == entry["task_id"] and receipt["task_content_hash"] == entry["task_content_hash"], "build receipt binding differs")
    package = json.loads(run(["gh", "api", "orgs/codex-radar/packages/container/" + entry["target_image"].split("/")[-1]], timeout=90))
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
            reference = entry["target_image"] + "@" + receipt["image"]["digest"]
            actual = inspect(reference, args.out, environment)
            require(actual == receipt["image"], "anonymous manifest/config/complete rootfs differs")
            with (args.out / "ANONYMOUS_PULL.log").open("wb") as pull_log:
                pull = subprocess.run(docker + ["pull", "--platform", "linux/amd64", reference], env=environment, stdout=pull_log, stderr=subprocess.STDOUT, timeout=1800)
            require(pull.returncode == 0, "full anonymous pull failed")
            image = json.loads(run(docker + ["image", "inspect", reference], env=environment))[0]
            require(image["Id"] == actual["config_digest"] and image["RootFS"]["Layers"] == actual["rootfs_diff_ids"], "complete pulled filesystem identity differs")
            probe = run(docker + ["run", "--rm", "--network", "none", "--entrypoint", "sh", reference, "-c", "test ! -e /tests && test ! -e /solution && id && pwd"], env=environment, timeout=60).decode()
            write(args.out / "ANONYMOUS_RECEIPT.json", {"status": "FULL_ANONYMOUS_PULL_AND_NO_MODEL_ENV_PROBE_PASSED", "number": args.number, "task_id": entry["task_id"], "target_image": reference, "image": actual, "empty_auth_config": True, "fresh_docker_state": True, "full_pull": True, "environment_probe": probe, "model_calls": 0, "official_grader_calls": 0, "run_id": os.environ["GITHUB_RUN_ID"]})
        finally:
            if daemon is not None:
                run(["sudo", "-n", "kill", "-TERM", str(daemon.pid)], timeout=15)
                daemon.wait(timeout=30)
                write(args.out / "DAEMON_EXIT.json", {"pid": daemon.pid, "returncode": daemon.returncode, "physical_exit_confirmed": True})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("build", "anonymous"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reviewed", required=True)
    parser.add_argument("--batch-hash", required=True)
    parser.add_argument("--number", required=True)
    parser.add_argument("--build-run", default="")
    args = parser.parse_args()
    args.out = args.out.absolute(); args.out.mkdir(parents=True, exist_ok=False)
    try:
        _, entry = gate(args)
        (build if args.phase == "build" else anonymous)(args, entry)
    except BaseException as error:
        write(args.out / "FAILURE.json", {"phase": args.phase, "number": args.number, "error_type": type(error).__name__, "error": str(error), "model_calls": 0, "official_grader_calls": 0})
        raise


if __name__ == "__main__":
    main()
