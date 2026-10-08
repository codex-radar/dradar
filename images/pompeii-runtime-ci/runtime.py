"""Publish a data-free generic runtime; reuse DeepSWE131 isolated anonymous pulls.

No task dataset/gold is read by this script. An authenticated package metadata
read is separate from registry traffic during the anonymous phase.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import posixpath
import re
import subprocess
import tarfile
import time

P = pathlib.Path
IMAGE = "ghcr.io/codex-radar/dradar-pompeii-runtime"
BRANCH = "refs/heads/codex/pompeii-generic-runtime-20261008"
BASE_REPO = "docker.io/library/node"
BASE_INDEX = "sha256:0e5f906573693feaa1e21057ebdcfdb5bd5021f050b2dc7c9deceb629c7da2a8"
BASE_CHILD = "sha256:c4d5523090a817b7aa86d2111241fdd4f66d1e27782b44160e6aa63b357ecb2d"
BASE_REF = BASE_REPO + ":22-bookworm@" + BASE_INDEX
FILES = {
    ".dockerignore": (29, "a80a9141c954548148b2acb4ef04adcc1a7cb0131a6c2cba19c5877531cc957c"),
    "Dockerfile": (777, "ad3917f216f3f6b05dbb2f75270d72bf60bb6b1fcf2327f7e78e1b483bddbb13"),
}
APT_MARKER = "git python3 ca-certificates"
SMOKE_COMMAND = 'node --version; npm --version; git --version; python3 --version; test -z "$(ls -A /app)"'
ROLE = "agent"
WORKDIR = "/app"
AGENT_SPEC = {"image": IMAGE, "base_repo": BASE_REPO, "base_index": BASE_INDEX,
              "base_child": BASE_CHILD, "base_ref": BASE_REF, "files": FILES,
              "apt_marker": APT_MARKER, "smoke": SMOKE_COMMAND, "working_dir": WORKDIR}
VERIFIER_SPEC = {
    "image": "ghcr.io/codex-radar/dradar-pompeii-verifier-runtime",
    "base_repo": "docker.io/library/python",
    "base_index": "sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f",
    "base_child": "sha256:2b4f19dae3a777dfc3b76730bda1e82e1f66ab2a2686fa93ca78edbfb4f04ffe",
    "base_ref": "docker.io/library/python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f",
    "files": {".dockerignore": (29, "a80a9141c954548148b2acb4ef04adcc1a7cb0131a6c2cba19c5877531cc957c"),
              "Dockerfile": (822, "27c3e80287e22c56097c2997f839f6c6f33cbb89c1a96e41010aa014f858c143")},
    "apt_marker": "git", "working_dir": "",
    "smoke": "python3 --version; git --version; python3 -c 'import sys; assert sys.version_info[:2] == (3, 12)'; test ! -e /tests",
}


def select_runtime(role):
    global IMAGE, BASE_REPO, BASE_INDEX, BASE_CHILD, BASE_REF, FILES, APT_MARKER, SMOKE_COMMAND, ROLE, WORKDIR
    spec = AGENT_SPEC if role == "agent" else VERIFIER_SPEC
    IMAGE, BASE_REPO, BASE_INDEX, BASE_CHILD, BASE_REF, FILES = (
        spec[k] for k in ["image", "base_repo", "base_index", "base_child", "base_ref", "files"])
    APT_MARKER, SMOKE_COMMAND, WORKDIR = (spec[k] for k in ["apt_marker", "smoke", "working_dir"])
    ROLE = role


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def run(command, env=None, timeout=1800, log=None, stdin=None):
    if log is None:
        result = subprocess.run(command, env=env, input=stdin, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
        require(result.returncode == 0, command[0] + " exited " + str(result.returncode)
                + ": " + result.stderr.decode(errors="replace")[-2500:])
        return result.stdout
    with log.open("wb") as stream:
        result = subprocess.run(command, env=env, input=stdin, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=timeout)
    require(result.returncode == 0, command[0] + " exited " + str(result.returncode)
            + "; see " + log.name)
    return b""


def context_identity(context):
    require(context.is_dir() and not context.is_symlink(), "context must be a real directory")
    require(sorted(p.name for p in context.iterdir()) == sorted(FILES), "context must contain exactly two files")
    rows = []
    for name, (size, digest) in sorted(FILES.items()):
        path = context / name
        require(path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1,
                "context file is not an independent regular file: " + name)
        raw = path.read_bytes()
        require(len(raw) == size and sha(raw) == "sha256:" + digest, "reviewed context differs: " + name)
        rows.append({"name": name, "sha256": digest, "size_bytes": size})
    dockerfile = (context / "Dockerfile").read_text()
    instructions = [line for line in dockerfile.splitlines() if line and not line.startswith("#")]
    require(not any(re.match(r"\s*(COPY|ADD)\b", line, re.I) for line in instructions), "COPY/ADD forbidden")
    digest = hashlib.sha256(json.dumps(rows, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    return {"schema": "dradar.runtime-context/1", "runtime_role": ROLE, "sha256": digest, "files": rows,
            "exact_file_allowlist_verified": True, "copy_add_instructions": 0,
            "build_context_contains_task_data_gold_credentials": False}


def gate(context, reviewed, context_sha256):
    require(re.fullmatch(r"[0-9a-f]{40}", reviewed), "full reviewed commit required")
    expected = {"GITHUB_SHA": reviewed, "GITHUB_REPOSITORY": "codex-radar/dradar",
                "GITHUB_ACTOR": "SecurityMind", "GITHUB_REF": BRANCH,
                "GITHUB_EVENT_NAME": "workflow_dispatch"}
    require(all(os.environ.get(k) == v for k, v in expected.items()), "CI identity/branch/commit differs")
    require(run(["git", "rev-parse", "HEAD"]).decode().strip() == reviewed, "checkout HEAD differs")
    require(not run(["git", "status", "--porcelain"]).strip(), "checkout is not clean")
    identity = context_identity(context)
    require(identity["sha256"] == context_sha256, "input context digest differs")
    return identity


def inspect(ref, auth, env):
    prefix = ["skopeo", "inspect", "--authfile", str(auth)]
    return (run(prefix + ["--raw", "docker://" + ref], env, 120),
            run(prefix + ["--config", "--raw", "docker://" + ref], env, 120))


def base_identity(auth, env, public):
    raw = run(["skopeo", "inspect", "--authfile", str(auth), "--raw", "docker://" + BASE_REF], env, 120)
    require(sha(raw) == BASE_INDEX, "base index digest differs")
    index = json.loads(raw)
    platforms = [m for m in index["manifests"] if m.get("platform", {}).get("os") == "linux"
                 and m.get("platform", {}).get("architecture") == "amd64"]
    require(len(platforms) == 1 and platforms[0]["digest"] == BASE_CHILD, "fixed amd64 child differs")
    manifest, config = inspect(BASE_REPO + "@" + BASE_CHILD, auth, env)
    require(sha(manifest) == BASE_CHILD, "base child manifest differs")
    m, c = json.loads(manifest), json.loads(config)
    require(m["config"]["digest"] == sha(config), "base config descriptor differs")
    require(c["os"] == "linux" and c["architecture"] == "amd64", "base platform differs")
    require(not c.get("config", {}).get("OnBuild"), "base contains unexpected ONBUILD instructions")
    (public / "BASE_INDEX.json").write_bytes(raw)
    (public / "BASE_MANIFEST.json").write_bytes(manifest)
    (public / "BASE_CONFIG.json").write_bytes(config)
    return m, c


def image_identity(manifest, config, base, reviewed, context_sha256, expected_digest=None):
    m, c = json.loads(manifest), json.loads(config)
    if expected_digest:
        require(sha(manifest) == expected_digest, "published manifest digest differs")
    require(m.get("config", {}).get("digest") == sha(config) and m["config"]["size"] == len(config),
            "published config descriptor differs")
    require(c["os"] == "linux" and c["architecture"] == "amd64", "only linux/amd64 is supported")
    require(c["rootfs"]["type"] == "layers", "rootfs type differs")
    prefix = base["rootfs"]["diff_ids"]
    require(c["rootfs"]["diff_ids"][:len(prefix)] == prefix, "base rootfs prefix differs")
    require(len(c["rootfs"]["diff_ids"]) > len(prefix) and len(m["layers"]) == len(c["rootfs"]["diff_ids"]),
            "runtime layer count differs")
    labels = c.get("config", {}).get("Labels", {})
    require(labels.get("org.opencontainers.image.revision") == reviewed
            and labels.get("io.codexradar.context-sha256") == context_sha256
            and labels.get("io.codexradar.source.kind") == "generic-runtime-without-dataset"
            and labels.get("io.codexradar.official-repair-image") == "false", "runtime provenance labels differ")
    require(c["config"].get("WorkingDir", "") == WORKDIR and c["config"].get("User", "") in ("", "root"),
            "runtime default user/cwd differs")
    return {"schema": "dradar.generic-runtime-identity/1", "image": IMAGE,
            "runtime_role": ROLE, "default_user": "root", "default_working_directory": WORKDIR,
            "build_commit": reviewed, "context_sha256": context_sha256,
            "manifest_digest": sha(manifest), "config_digest": sha(config),
            "layers": m["layers"], "rootfs_diff_ids": c["rootfs"]["diff_ids"],
            "supported_architectures": ["linux/amd64"], "base_index_digest": BASE_INDEX,
            "base_amd64_manifest_digest": BASE_CHILD, "base_rootfs_prefix_verified": True,
            "labels": labels, "official_pompeii_or_repair_image": False,
            "task_data_injected_at_build": False}


def audit_added_layers(archive, config, base):
    history = config.get("history", [])
    base_history = base.get("history", [])
    require(history[:len(base_history)] == base_history, "base image history prefix differs")
    added_history = history[len(base_history):]
    require(not any(re.search(r"\b(COPY|ADD)\b", h.get("created_by", ""), re.I) for h in added_history),
            "unexpected COPY/ADD in built image history")
    require(sum("apt-get update" in h.get("created_by", "") and APT_MARKER in
                h.get("created_by", "") for h in added_history) == 1, "expected apt-only RUN missing/duplicated")
    require(not any(h.get("created_by", "").startswith(("RUN ", "/bin/sh -c "))
                    and "apt-get update" not in h.get("created_by", "")
                    and "#(nop)" not in h.get("created_by", "")
                    for h in added_history), "unexpected executable build instruction")
    diff_ids = config["rootfs"]["diff_ids"]
    first = len(base["rootfs"]["diff_ids"])
    rows = []
    allowed_roots = {"bin", "dev", "etc", "lib", "lib64", "run", "sbin", "tmp", "usr", "var"}
    with tarfile.open(archive) as saved:
        saved_manifest = json.load(saved.extractfile("manifest.json"))
        require(len(saved_manifest) == 1, "image save must contain one image")
        layers = saved_manifest[0]["Layers"]
        require(len(layers) == len(diff_ids), "saved layer count differs")
        for position, name in enumerate(layers[first:], start=first):
            digest = hashlib.sha256()
            with saved.extractfile(name) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            require("sha256:" + digest.hexdigest() == diff_ids[position], "saved uncompressed layer hash differs")
            members = []
            with saved.extractfile(name) as stream, tarfile.open(fileobj=stream, mode="r|*") as layer:
                for member in layer:
                    path = P(member.name)
                    parts = tuple(p for p in path.parts if p != ".")
                    require(not path.is_absolute() and ".." not in parts and (parts or member.isdir()),
                            "unsafe added layer path")
                    require(not parts or parts[0] in allowed_roots or (ROLE == "agent" and parts == ("app",) and member.isdir()),
                            "non-OS content introduced outside the empty /app directory")
                    require(not member.isdev() and not member.isfifo(), "unexpected special added layer member")
                    item = {"path": member.name, "size_bytes": member.size, "type": member.type.decode("ascii")}
                    if member.isfile():
                        digest = hashlib.sha256()
                        with layer.extractfile(member) as content:
                            for chunk in iter(lambda: content.read(1024 * 1024), b""):
                                digest.update(chunk)
                        item["sha256"] = digest.hexdigest()
                    if member.issym() or member.islnk():
                        target = member.linkname if member.islnk() or member.linkname.startswith("/") else posixpath.join(
                            posixpath.dirname(member.name), member.linkname)
                        target_parts = P(posixpath.normpath(target).lstrip("/")).parts
                        require(target_parts and target_parts[0] in allowed_roots and ".." not in target_parts,
                                "added OS link points outside OS paths")
                        item["linkname"] = member.linkname
                    members.append(item)
            rows.append({"position": position, "diff_id": diff_ids[position], "members": members})
    return {"schema": "dradar.runtime-build-layer-proof/1", "base_history_prefix_verified": True,
            "added_history": added_history, "added_layers": rows, "copy_add_instructions": 0,
            "all_added_layer_members_audited": True, "added_app_files": 0,
            "runtime_role": ROLE,
            "allowed_added_content": "apt-generated OS paths" + (" and empty /app directory" if ROLE == "agent" else ""),
            "no_task_source_or_gold_introduced_by_this_build": True,
            "scope": "Reviewed two-file context and actual new layers; not a claim about third-party base internals"}


def package_metadata(env, public_required):
    name = IMAGE.rsplit("/", 1)[1]
    obj = json.loads(run(["gh", "api", "orgs/codex-radar/packages/container/" + name], env, 120))
    selected = {"name": obj.get("name"), "package_type": obj.get("package_type"),
                "visibility": obj.get("visibility"), "repository": (obj.get("repository") or {}).get("full_name")}
    require(selected["name"] == name and selected["package_type"] == "container",
            "package metadata differs")
    if public_required:
        require(selected["visibility"] == "public" and selected["repository"] == "codex-radar/dradar",
                "public visibility/repository link incomplete")
    return selected


def build(context, out, reviewed, context_hash):
    public, private = out / "public", out / "private"
    public.mkdir(parents=True); private.mkdir(mode=0o700)
    identity = gate(context, reviewed, context_hash)
    write(public / "BUILD_CONTEXT.json", identity)
    env = dict(os.environ)
    empty = private / "empty-auth.json"; empty.write_text('{"auths":{}}\n'); empty.chmod(0o600)
    _, base = base_identity(empty, env, public)
    docker_config = private / "docker-config"; docker_config.mkdir(mode=0o700)
    (docker_config / "config.json").write_text("{}\n")
    docker = ["docker", "--config", str(docker_config)]
    tag = (IMAGE + ":build-" + reviewed + "-run-" + os.environ["GITHUB_RUN_ID"]
           + "-attempt-" + os.environ["GITHUB_RUN_ATTEMPT"])
    receipt = {"status": "STARTED", "build_commit": reviewed, "context_sha256": context_hash,
               "image_tag": tag, "runtime_role": ROLE, "run_id": os.environ["GITHUB_RUN_ID"], "official_image": False,
               "model_calls": 0, "grader_calls": 0}
    try:
        run(docker + ["pull", "--platform", "linux/amd64", BASE_REF], env, 1200, public / "BASE_PULL.log")
        run(docker + ["build", "--platform", "linux/amd64", "--pull", "--build-arg", "BUILD_COMMIT=" + reviewed,
                      "--build-arg", "CONTEXT_SHA256=" + context_hash, "--tag", tag, str(context)],
            env, 1800, public / "BUILD.log")
        local = json.loads(run(docker + ["image", "inspect", tag], env, 60))[0]
        require(local["Os"] == "linux" and local["Architecture"] == "amd64", "built platform differs")
        archive = private / "runtime-save.tar"
        run(docker + ["image", "save", "--output", str(archive), tag], env, 600)
        with tarfile.open(archive) as saved:
            manifest = json.load(saved.extractfile("manifest.json"))[0]
            raw_config = saved.extractfile(manifest["Config"]).read()
        config = json.loads(raw_config)
        require(sha(raw_config) == local["Id"], "local config digest differs")
        require(config["rootfs"]["diff_ids"] == local["RootFS"]["Layers"], "local rootfs differs")
        write(public / "NO_SOURCE_DATA_LAYER_PROOF.json", audit_added_layers(archive, config, base))
        archive.unlink()
        run(docker + ["run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", tag, "-ec",
                      SMOKE_COMMAND],
            env, 120, public / "RUNTIME_SMOKE.log")
        run(docker + ["login", "--username", "SecurityMind", "--password-stdin", "ghcr.io"],
            env, 60, stdin=env["GH_TOKEN"].encode())
        run(docker + ["push", tag], env, 1200, public / "PUSH.log")
        raw_manifest, published_config = inspect(tag, docker_config / "config.json", env)
        require(published_config == raw_config, "published config differs from audited build")
        published = image_identity(raw_manifest, published_config, base, reviewed, context_hash)
        (public / "PUBLISHED_MANIFEST.json").write_bytes(raw_manifest)
        (public / "PUBLISHED_CONFIG.json").write_bytes(published_config)
        write(public / "PUBLISHED_IDENTITY.json", published)
        receipt.update(status="PUBLISHED_GENERIC_RUNTIME", immutable_image=IMAGE + "@" + sha(raw_manifest),
                       identity=published, package=package_metadata(env, False),
                       no_task_source_or_gold_introduced_by_build=True, smoke_exit_code=0,
                       anonymous_pull_verified=False)
    except BaseException as error:
        receipt.update(status="FAILED", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        (docker_config / "config.json").unlink(missing_ok=True)
        write(public / "BUILD_RECEIPT.json", receipt)
    print(json.dumps({"status": receipt["status"], "immutable_image": receipt["immutable_image"]}), flush=True)


def anonymous(context, out, reviewed, context_hash, digest):
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", digest or ""), "immutable manifest digest required")
    public, private = out / "public", out / "private"
    public.mkdir(parents=True); private.mkdir(mode=0o700)
    write(public / "BUILD_CONTEXT.json", gate(context, reviewed, context_hash))
    env = dict(os.environ)
    write(public / "PACKAGE_PUBLIC_READBACK.json", package_metadata(env, True))
    # Reused from the DeepSWE131 proof: API authorization ends before registry traffic.
    for key in ["GH_TOKEN", "GITHUB_TOKEN", "DOCKER_AUTH_CONFIG", "REGISTRY_AUTH_FILE", "DOCKER_CONTEXT", "DOCKER_HOST"]:
        env.pop(key, None)
    cfg = private / "empty-docker-config"; cfg.mkdir(mode=0o700)
    (cfg / "config.json").write_text("{}\n"); env["DOCKER_CONFIG"] = str(cfg)
    auth = private / "empty-registry-auth.json"; auth.write_text('{"auths":{}}\n'); auth.chmod(0o600)
    _, base = base_identity(auth, env, public)
    image = IMAGE + "@" + digest
    manifest, config = inspect(image, auth, env)
    expected = image_identity(manifest, config, base, reviewed, context_hash, digest)
    (public / "ANONYMOUS_MANIFEST.json").write_bytes(manifest)
    (public / "ANONYMOUS_CONFIG.json").write_bytes(config)
    write(public / "ANONYMOUS_IDENTITY.json", expected)
    data, exe, pidfile, sock = (private / n for n in ["fresh-data", "fresh-exec", "dockerd.pid", "dockerd.sock"])
    require(not data.exists() and not exe.exists(), "fresh Docker roots required")
    address = "unix://" + str(sock)
    docker = ["docker", "--host", address, "--config", str(cfg)]
    process, failure = None, None
    daemon = {"fresh_root": str(data), "run_id": os.environ["GITHUB_RUN_ID"], "daemon_exit_confirmed": False}
    receipt = {"status": "STARTED", "image": image, "build_commit": reviewed,
               "runtime_role": ROLE,
               "context_sha256": context_hash, "run_id": os.environ["GITHUB_RUN_ID"],
               "registry_credentials_used": False, "docker_login_executed": False,
               "fresh_isolated_daemon": True, "model_calls": 0, "grader_calls": 0,
               "official_image": False, "image_executed": False}
    with (public / "DOCKERD.log").open("wb") as log:
        try:
            process = subprocess.Popen(["sudo", "-n", "dockerd", "--data-root", str(data), "--exec-root", str(exe),
                                        "--pidfile", str(pidfile), "--host", address, "--bridge=none", "--iptables=false",
                                        "--ip-masq=false", "--ip-forward=false", "--storage-driver=overlay2"],
                                       env=env, stdout=log, stderr=subprocess.STDOUT)
            end = time.monotonic() + 60
            while time.monotonic() < end:
                require(process.poll() is None, "owned Docker daemon exited")
                try:
                    info = json.loads(run(docker + ["info", "--format", "{{json .}}"], env, 3))
                    if info["DockerRootDir"] == str(data):
                        break
                except Exception:
                    time.sleep(0.5)
            else:
                raise RuntimeError("owned Docker daemon not ready")
            require(not run(docker + ["image", "ls", "-aq"], env, 30).strip(), "fresh data root has images")
            require(not run(docker + ["container", "ls", "-aq"], env, 30).strip(), "fresh data root has containers")
            daemon.update(initial_images=0, initial_containers=0)
            require(json.loads((cfg / "config.json").read_text()) == {} and
                    json.loads(auth.read_text()) == {"auths": {}}, "anonymous auth not empty")
            run(docker + ["pull", "--platform", "linux/amd64", image], env, 1800, public / "FULL_ANONYMOUS_PULL.log")
            actual = json.loads(run(docker + ["image", "inspect", image], env, 60))[0]
            require(actual["Id"] == expected["config_digest"] and actual["RootFS"]["Layers"] == expected["rootfs_diff_ids"]
                    and image in actual["RepoDigests"] and actual["Os"] == "linux" and actual["Architecture"] == "amd64",
                    "complete pulled config/manifest/rootfs/platform differs")
            require(json.loads((cfg / "config.json").read_text()) == {} and
                    json.loads(auth.read_text()) == {"auths": {}}, "anonymous auth changed")
            receipt.update(status="ANONYMOUS_PULL_VERIFIED", full_pull_exit_code=0, empty_auth_before_after=True,
                           manifest_config_layers_rootfs_verified=True, first_pull_in_empty_data_root=True,
                           supported_architectures=["linux/amd64"])
        except BaseException as error:
            failure = error
            receipt.update(status="FAILED", error_type=type(error).__name__, error=str(error))
        finally:
            try:
                if process is not None and process.poll() is None:
                    pid = int(pidfile.read_text().strip())
                    parts = (P("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
                    require(b"--data-root" in parts and parts[parts.index(b"--data-root") + 1] == os.fsencode(data),
                            "refuse to stop unrelated daemon")
                    run(["sudo", "-n", "kill", "-TERM", str(pid)], env, 10)
                    process.wait(timeout=60)
                    require(not (P("/proc") / str(pid) / "cmdline").exists(), "daemon exit unconfirmed")
                daemon["daemon_exit_confirmed"] = True
            except BaseException as error:
                daemon["cleanup_error"] = str(error)
                failure = failure or error
                receipt["status"] = "FAILED"
            write(public / "DAEMON_EXIT_RECEIPT.json", daemon)
            write(public / "ANONYMOUS_PULL_RECEIPT.json", receipt)
    if failure:
        raise failure
    print(json.dumps({"status": receipt["status"], "image": image}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["context", "build", "anonymous"])
    parser.add_argument("--runtime", choices=["agent", "verifier"], default="agent")
    parser.add_argument("--context", type=P, required=True)
    parser.add_argument("--out", type=P)
    parser.add_argument("--reviewed")
    parser.add_argument("--context-sha256")
    parser.add_argument("--digest")
    args = parser.parse_args()
    select_runtime(args.runtime)
    if args.phase == "context":
        identity = context_identity(args.context)
        if args.context_sha256:
            require(identity["sha256"] == args.context_sha256, "input context digest differs")
        print(json.dumps(identity, indent=2))
        return
    require(args.out and args.reviewed and args.context_sha256, "CI receipt arguments required")
    if args.phase == "build":
        build(args.context, args.out, args.reviewed, args.context_sha256)
    else:
        anonymous(args.context, args.out, args.reviewed, args.context_sha256, args.digest)


if __name__ == "__main__":
    main()
