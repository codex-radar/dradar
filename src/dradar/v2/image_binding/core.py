"""Controller-facing validation; Docker commands are explicitly injectable.

This module never resolves tags, pulls, acquires/releases leases, or opens a paid gate.
The existing coordinator/controller must supply immutable binding and live lease checks.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Awaitable, Callable


class BindingError(RuntimeError):
    """A scrubbed, bounded error suitable for the controller journal."""


class BackendOperationError(BindingError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def classify_backend_failure(detail: str) -> str:
    # Classification only; never retain or return arbitrary Docker diagnostics.
    lowered = detail.lower()
    return "storage_enospc" if "enospc" in lowered or "no space left on device" in lowered else "docker_compose_failed"


HEX64 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
REPO_DIGEST = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
SCOPE = re.compile(r"^[a-zA-Z0-9_.-]{8,128}$")
PLATFORMS = {"linux/amd64", "linux/arm64", "linux/arm64/v8", "linux/arm/v7", "linux/arm/v6"}
MAX_RECORD = 65536


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BindingError(message)


def normalize_platform(value: str) -> str:
    # No host-architecture guessing. arm64 and arm64/v8 name the same selected ISA.
    require(value in PLATFORMS, "unsupported explicit Linux platform")
    return "linux/arm64" if value == "linux/arm64/v8" else value


def full_image_id(value: object) -> str:
    require(isinstance(value, str) and bool(IMAGE_ID.fullmatch(value)), "invalid full image ID")
    return value


def private_path(path: Path, *, exists: bool = True) -> Path:
    require(path.is_absolute(), "private path must be absolute")
    require(path.resolve() == path, "symlink or noncanonical private path")
    if exists:
        s = path.stat()
        require(s.st_uid == os.getuid(), "private file owner mismatch")
        require(stat.S_ISREG(s.st_mode) and not s.st_mode & 0o077, "private file permissions mismatch")
    parent = path.parent.stat()
    require(parent.st_uid == os.getuid() and not parent.st_mode & 0o077, "private directory permissions mismatch")
    return path


def read_private_json(path: Path) -> dict:
    private_path(path)
    require(path.stat().st_size <= MAX_RECORD, "record too large")
    try:
        result = json.loads(path.read_text())
    except (ValueError, UnicodeError):
        raise BindingError("invalid record JSON") from None
    require(isinstance(result, dict), "record must be an object")
    return result


def atomic_private_json(path: Path, value: dict, *, exclusive: bool = False) -> None:
    private_path(path, exists=False)
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    require(len(data) <= MAX_RECORD, "record too large")
    fd, temp = tempfile.mkstemp(prefix=".binding-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temp, path)  # Atomic no-clobber reservation; no partial JSON.
        else:
            os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError:
        raise BindingError("attempt already reserved") from None
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def tree_hash(root: Path) -> str:
    """Candidate's explicit task-tree hash, not DRadar's package-hash algorithm."""
    require(root.is_dir() and root.resolve() == root, "invalid task root")
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "task symlink unsupported")
        if path.is_dir():
            continue
        require(path.is_file(), "nonregular task file")
        name = path.relative_to(root).as_posix().encode()
        data = path.read_bytes()
        h.update(len(name).to_bytes(8, "big") + name + len(data).to_bytes(8, "big") + data)
    return h.hexdigest()


@dataclasses.dataclass(frozen=True)
class Binding:
    schema: int
    attempt_id: str
    nonce: str
    original_package_hash: str
    effective_task_hash: str
    selected_task_hash: str
    original_image_ref: str
    repository_digest: str
    platform: str
    base_image_id: str
    base_lease_token: str
    daemon_id: str
    context_name: str
    endpoint: str
    builder_name: str
    session_id: str
    project: str
    selected_task_root: str
    environment_dir: str
    job_root: str
    trial_dir: str
    state_path: str
    proof_path: str
    index_digest: str | None = None
    manifest_digest: str | None = None
    builder_inspect_sha256: str = ""

    @classmethod
    def load(cls, path: Path) -> Binding:
        try:
            result = cls(**read_private_json(path))
        except TypeError:
            raise BindingError("binding schema mismatch") from None
        result.validate()
        return result

    def validate(self) -> None:
        require(self.schema == 1 and type(self.schema) is int, "unsupported binding schema")
        for name in ("attempt_id", "nonce"):
            require(isinstance(getattr(self, name), str) and bool(SCOPE.fullmatch(getattr(self, name))), "invalid scope")
        for name in ("original_package_hash", "effective_task_hash", "selected_task_hash"):
            require(isinstance(getattr(self, name), str) and bool(HEX64.fullmatch(getattr(self, name))), "invalid content hash")
        require(isinstance(self.repository_digest, str) and bool(REPO_DIGEST.fullmatch(self.repository_digest)), "invalid repository digest")
        full_image_id(self.base_image_id)
        require(isinstance(self.builder_inspect_sha256, str) and bool(HEX64.fullmatch(self.builder_inspect_sha256)), "missing original builder policy fingerprint")
        normalize_platform(self.platform)
        for name in ("original_image_ref", "base_lease_token", "daemon_id", "context_name", "endpoint", "builder_name", "session_id", "project"):
            value = getattr(self, name)
            require(isinstance(value, str) and 0 < len(value) <= 1024 and not any(c.isspace() for c in value), "invalid binding identity")
        project = re.sub(r"[^a-z0-9_-]", "-", self.session_id.lower())
        if not re.match(r"^[a-z0-9]", project):
            project = "0" + project
        require(self.project == project, "session/project mismatch")
        for name in ("index_digest", "manifest_digest"):
            value = getattr(self, name)
            require(value is None or bool(IMAGE_ID.fullmatch(value)), "invalid manifest digest")
        if self.manifest_digest:
            require(self.repository_digest.endswith("@" + self.manifest_digest), "repository/selected-manifest mismatch")
        for name in ("selected_task_root", "environment_dir", "job_root", "trial_dir", "state_path", "proof_path"):
            value = getattr(self, name)
            require(isinstance(value, str) and Path(value).is_absolute() and Path(value).resolve() == Path(value), "noncanonical scope path")
        require(Path(self.environment_dir).is_relative_to(Path(self.selected_task_root)), "environment outside task root")
        require(Path(self.trial_dir).is_relative_to(Path(self.job_root)), "trial outside job root")
        for name in ("state_path", "proof_path"):
            p = Path(getattr(self, name))
            require(p.is_relative_to(Path(self.job_root)) and not p.is_relative_to(Path(self.selected_task_root)), "record scope mismatch")
            private_path(p, exists=False)
        require(self.proof_path != self.state_path, "state/proof collision")

    def identity(self) -> dict:
        return dataclasses.asdict(self)

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.identity(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def builder_inspect_hash(output: str) -> str:
    """Hash bounded original driver/node/policy evidence, excluding activity time.

    Plain `buildx inspect NAME` is the supported pinned CLI/cache source interface.
    Never persist the raw Driver Options, which can contain private configuration.
    """
    require(isinstance(output, str) and len(output) <= MAX_RECORD, "invalid builder inspection")
    lines = [" ".join(line.split()) for line in output.splitlines() if line.strip() and not line.strip().startswith("Last Activity:")]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


@dataclasses.dataclass(frozen=True)
class CommandResult:
    return_code: int
    stdout: str = ""


Command = Callable[[list[str]], Awaitable[CommandResult]]
LeaseCheck = Callable[[Binding], Awaitable[bool]]


async def docker_command(argv: list[str]) -> CommandResult:
    """Bounded inspect-only subprocess used by production validation."""
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 20)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    require(len(stdout) <= 1024 * 1024, "Docker inspection output too large")
    return CommandResult(process.returncode, stdout.decode("utf-8", errors="strict"))


class Inspector:
    def __init__(self, binding: Binding, command: Command = docker_command, environment=None):
        self.binding, self.command = binding, command
        self.environment = os.environ if environment is None else environment

    async def json(self, *args: str) -> object:
        result = await self.command(["docker", *args])
        require(result.return_code == 0, "Docker inspection failed")
        require(len(result.stdout) <= 1024 * 1024, "Docker inspection output too large")
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise BindingError("invalid Docker inspection JSON") from None

    async def same_daemon(self) -> None:
        b = self.binding
        require(not any(self.environment.get(k) for k in ("DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")), "Docker endpoint environment override")
        # Plain docker compose in the pinned parent must use exactly this context.
        result = await self.command(["docker", "context", "show"])
        require(result.return_code == 0 and result.stdout.strip() == b.context_name, "Docker context changed")
        context = await self.json("context", "inspect", b.context_name)
        require(isinstance(context, list) and len(context) == 1, "ambiguous Docker context")
        try:
            endpoint = context[0]["Endpoints"]["docker"]
            require(endpoint["Host"] == b.endpoint and not endpoint.get("SkipTLSVerify", False), "Docker endpoint changed or TLS bypass")
        except (KeyError, TypeError):
            raise BindingError("invalid Docker context identity") from None
        info = await self.json("info", "--format", "{{json .}}")
        require(isinstance(info, dict) and info.get("ID") == b.daemon_id and info.get("OSType") == "linux", "Docker daemon changed")
        require(self.environment.get("BUILDX_BUILDER") == b.builder_name, "builder identity changed")
        builder = await self.command(["docker", "buildx", "inspect", b.builder_name])
        require(builder.return_code == 0, "original builder inspection failed")
        require(builder_inspect_hash(builder.stdout) == b.builder_inspect_sha256, "original builder policy changed")
        lines = [line.strip() for line in builder.stdout.splitlines()]
        require(lines and lines[0].startswith("Name:") and lines[0].split(":", 1)[1].strip() == b.builder_name, "builder name mismatch")
        drivers = [line.split(":", 1)[1].strip() for line in lines if line.startswith("Driver:")]
        require(drivers == ["docker-container"], "unexpected original builder driver")
        endpoints = [line.split(":", 1)[1].strip() for line in lines if line.startswith("Endpoint:")]
        require(endpoints and all(endpoint in (b.endpoint, b.context_name) for endpoint in endpoints), "builder endpoint is not original daemon context")
        statuses = [line.split(":", 1)[1].strip() for line in lines if line.startswith("Status:")]
        require(statuses and all(status == "running" for status in statuses), "original builder is not running")

    async def image(self, selector: str, *, base: bool = False, platform: str | None = None) -> dict:
        data = await self.json("image", "inspect", selector)
        require(isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict), "ambiguous image inspection")
        image = data[0]
        full_image_id(image.get("Id"))
        require(not image.get("Config", {}).get("Volumes"), "anonymous image volumes unsupported")
        observed = "/".join(str(image.get(k, "")) for k in ("Os", "Architecture"))
        variant = image.get("Variant")
        if variant and image.get("Architecture") != "amd64":
            observed += "/" + str(variant)
        require(normalize_platform(observed) == normalize_platform(platform or self.binding.platform), "image platform mismatch")
        if selector.startswith("sha256:"):
            require(image["Id"] == selector, "image ID changed")
        if base:
            require(image["Id"] == self.binding.base_image_id, "base image ID mismatch")
            refs = image.get("RepoDigests")
            require(isinstance(refs, list) and all(isinstance(x, str) and REPO_DIGEST.fullmatch(x) for x in refs), "invalid RepoDigests")
            require(refs.count(self.binding.repository_digest) == 1, "missing or ambiguous base RepoDigest")
        return image

    async def project_empty(self) -> None:
        r = await self.command(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.binding.project}"])
        require(r.return_code == 0 and not r.stdout.strip(), "project already occupied or unverifiable")
        for resource in ("network", "volume"):
            r = await self.command(["docker", resource, "ls", "-q", "--filter", f"label=com.docker.compose.project={self.binding.project}"])
            require(r.return_code == 0 and not r.stdout.strip(), "project resources already occupied or unverifiable")

    async def container(self, container_id: str, image_id: str, compose_paths: list[str]) -> dict:
        require(isinstance(container_id, str) and bool(HEX64.fullmatch(container_id)), "invalid full container ID")
        data = await self.json("container", "inspect", container_id)
        require(isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict), "ambiguous container inspection")
        c = data[0]
        require(c.get("Id") == container_id and c.get("Image") == image_id, "container/image identity mismatch")
        require(c.get("State", {}).get("Running") is True, "container is not running")
        labels = c.get("Config", {}).get("Labels", {})
        require(isinstance(labels, dict) and labels.get("com.docker.compose.project") == self.binding.project and labels.get("com.docker.compose.service") == "main", "container owner labels mismatch")
        require(labels.get("com.docker.compose.oneoff") == "False", "unexpected oneoff container")
        require(labels.get("com.docker.compose.project.working_dir") == self.binding.environment_dir, "container working-dir mismatch")
        require(labels.get("com.docker.compose.project.config_files") == ",".join(compose_paths), "container compose config mismatch")
        mounts = c.get("Mounts")
        require(isinstance(mounts, list) and mounts, "missing task-private bind mounts")
        for mount in mounts:
            require(isinstance(mount, dict) and mount.get("Type") == "bind", "non-private container mount")
            source = Path(mount.get("Source", ""))
            require(source.is_absolute() and source.resolve() == source and source.is_relative_to(Path(self.binding.trial_dir)), "foreign or noncanonical container bind mount")
            for record in (self.binding.state_path, self.binding.proof_path):
                require(not Path(record).is_relative_to(source), "private record exposed in container mount")
        await self.image(image_id)
        return c

    async def project_resources(self, expected_images: dict[str, str], compose_paths: list[str]) -> dict:
        """Exact project resources captured from fresh ownership evidence."""
        b = self.binding
        result = await self.command(["docker", "ps", "-aq", "--no-trunc", "--filter", f"label=com.docker.compose.project={b.project}"])
        require(result.return_code == 0, "project inventory failed")
        ids = result.stdout.split()
        require(len(ids) == len(set(ids)) == len(expected_images), "unexpected project container inventory")
        services = {}
        for cid in ids:
            require(bool(HEX64.fullmatch(cid)), "non-full project container ID")
            data = await self.json("container", "inspect", cid)
            require(isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict), "invalid project container inspection")
            c = data[0]
            labels = c.get("Config", {}).get("Labels", {})
            service = labels.get("com.docker.compose.service")
            require(service in expected_images and service not in services, "unknown or duplicate project service")
            require(c.get("Id") == cid and c.get("Image") == expected_images[service] and c.get("State", {}).get("Running") is True, "project service identity mismatch")
            require(labels.get("com.docker.compose.project") == b.project and labels.get("com.docker.compose.oneoff") == "False", "project service ownership mismatch")
            require(labels.get("com.docker.compose.project.working_dir") == b.environment_dir and labels.get("com.docker.compose.project.config_files") == ",".join(compose_paths), "project service config ownership mismatch")
            if service == "main":
                await self.container(cid, expected_images[service], compose_paths)
            else:
                require(c.get("Mounts") == [], "unexpected auxiliary mount")
                # The reviewed egress shim supplies a native immutable proxy,
                # which can differ from the benchmark's emulated platform.
                paths = [Path(path) for path in compose_paths
                         if Path(path).name == "docker-compose-egress-proxy.json"]
                require(service == "pier-egress-proxy" and len(paths) == 1,
                        "unknown auxiliary platform configuration")
                require(paths[0].resolve() == paths[0] and paths[0].is_relative_to(Path(b.trial_dir)),
                        "foreign auxiliary configuration")
                auxiliary = json.loads(paths[0].read_text())["services"][service]
                platform = auxiliary.get("platform")
                if platform is not None:
                    require(not auxiliary.get("build") and full_image_id(auxiliary.get("image")) == expected_images[service],
                            "auxiliary immutable selector mismatch")
                    normalize_platform(platform)
                else:
                    require(bool(auxiliary.get("build")), "missing auxiliary explicit platform")
                    platform = b.platform
                await self.image(expected_images[service], platform=platform)
            services[service] = {"container_id": cid, "image_id": expected_images[service]}
        result = await self.command(["docker", "network", "ls", "-q", "--no-trunc", "--filter", f"label=com.docker.compose.project={b.project}"])
        require(result.return_code == 0, "network inventory failed")
        networks = {}
        for nid in result.stdout.split():
            require(bool(HEX64.fullmatch(nid)) and nid not in networks, "invalid network identity")
            data = await self.json("network", "inspect", nid)
            require(isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict), "invalid network inspection")
            n = data[0]; labels = n.get("Labels", {})
            allowed = {"default", "pier-egress-internal"} if "pier-egress-proxy" in services else {"default"}
            require(n.get("Id") == nid and labels.get("com.docker.compose.project") == b.project and labels.get("com.docker.compose.network") in allowed, "network owner mismatch")
            attached = n.get("Containers", {})
            require(isinstance(attached, dict) and set(attached) <= set(ids), "foreign container on project network")
            networks[nid] = {"name": n.get("Name"), "logical_name": labels["com.docker.compose.network"]}
        volumes = await self.command(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={b.project}"])
        require(volumes.return_code == 0 and not volumes.stdout.strip(), "unexpected project volume")
        return {"services": services, "networks": networks}


def compose_file_hashes(paths: list[str]) -> dict[str, str]:
    result = {}
    for name in paths:
        p = Path(name)
        require(p.is_absolute() and p.resolve() == p and p.is_file(), "compose file missing or noncanonical")
        require(p.stat().st_size <= 1024 * 1024, "compose file too large")
        result[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    require(len(result) == len(paths), "duplicate compose file")
    return result


def state_record(binding: Binding, phase: str, **extra: object) -> dict:
    return {"schema": 1, "binding_fingerprint": binding.fingerprint(), "attempt_id": binding.attempt_id,
            "nonce": binding.nonce, "phase": phase, "base_lease_token": binding.base_lease_token,
            "release_authorized": False, "updated_ns": time.time_ns(), **extra}


async def verify_paid_image_proof(binding_path: Path, *, expected_attempt_id: str,
                                  expected_nonce: str, lease_check: LeaseCheck,
                                  command: Command = docker_command, environment=None) -> dict:
    """Call independently in existing registered-worker callback before paid intent.

    It throws on any missing/stale evidence. Success returns evidence, not permission.
    A live durable coordinator lease check is mandatory and cannot default to true.
    """
    b = Binding.load(binding_path)
    require(b.attempt_id == expected_attempt_id and b.nonce == expected_nonce, "stale binding scope")
    require(await lease_check(b) is True, "base lease is not live")
    p = read_private_json(Path(b.proof_path))
    s = read_private_json(Path(b.state_path))
    require(p.get("binding_fingerprint") == b.fingerprint() and s.get("binding_fingerprint") == b.fingerprint(), "stale proof binding")
    require(p.get("phase") == "started_proved" and s.get("phase") == "started_proved", "incomplete or quarantined proof")
    require(p.get("schema") == 1 and p.get("attempt_id") == b.attempt_id and p.get("nonce") == b.nonce, "stale proof scope")
    require(p.get("quarantined") is False and s.get("quarantined") is False, "quarantined proof")
    require(tree_hash(Path(b.selected_task_root)) == b.selected_task_hash, "selected task changed before paid gate")
    image = full_image_id(p.get("runtime_image_id"))
    require(s.get("runtime_image_id") == image and s.get("container_id") == p.get("container_id"), "proof/state disagreement")
    build_completed = p.get("build_completed")
    require(type(build_completed) is bool and s.get("build_completed") is build_completed, "build evidence disagreement")
    if build_completed:
        context = Path(b.trial_dir) / "agent-build-context"
        dockerfile = context / "Dockerfile"
        data = dockerfile.read_bytes()
        require(data.decode().splitlines()[0] == "FROM " + b.repository_digest, "generated FROM changed")
        require(hashlib.sha256(data).hexdigest() == p.get("generated_dockerfile_hash") == s.get("generated_dockerfile_hash"), "generated Dockerfile evidence mismatch")
        require(tree_hash(context) == p.get("generated_context_hash") == s.get("generated_context_hash"), "generated installer context evidence mismatch")
    else:
        require(image == b.base_image_id and p.get("generated_dockerfile_hash") is None and p.get("generated_context_hash") is None, "direct image proof mismatch")
    require(normalize_platform(p.get("platform", "")) == normalize_platform(b.platform), "proof platform mismatch")
    paths = p.get("compose_paths")
    require(isinstance(paths, list) and paths and all(isinstance(x, str) and Path(x).is_absolute() and Path(x).resolve() == Path(x) for x in paths), "invalid proof compose paths")
    require(any(Path(x).is_relative_to(Path(b.trial_dir)) for x in paths), "missing task-private compose configuration")
    hashes = p.get("compose_file_hashes")
    require(isinstance(hashes, dict) and set(paths) <= set(hashes) and hashes == s.get("compose_file_hashes"), "compose content proof mismatch")
    require(compose_file_hashes(list(hashes)) == hashes, "compose configuration content changed")
    resources = p.get("project_resources")
    require(isinstance(resources, dict) and resources == s.get("project_resources"), "project resource proof mismatch")
    services = resources.get("services", {})
    require(isinstance(services, dict) and "main" in services and set(services) <= {"main", "pier-egress-proxy"}, "project service proof mismatch")
    require(services["main"] == {"container_id": p.get("container_id"), "image_id": image}, "main project proof mismatch")
    expected_images = {service: full_image_id(record.get("image_id")) for service, record in services.items()}
    i = Inspector(b, command, environment)
    await i.same_daemon()
    await i.image(b.repository_digest, base=True)
    await i.container(p.get("container_id"), image, paths)
    require(await i.project_resources(expected_images, paths) == resources, "project resources changed")
    await i.same_daemon()
    # A mutation between reads invalidates proof; nonce gate stays controller-owned.
    require(read_private_json(Path(b.state_path)) == s and read_private_json(Path(b.proof_path)) == p, "proof changed during verification")
    require(await lease_check(b) is True, "base lease changed during verification")
    return p
