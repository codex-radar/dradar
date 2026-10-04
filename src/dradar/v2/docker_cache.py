"""Docker boundary for immutable shared images and task-private containers.

No global prune, force removal, credential mounts, mutable base tags, or task
output deletion. Pulled/user images can be reused but are never adopted by GC.
The caller must keep each attempt ID unique, persist its exact container ID,
and confirm runtime inactivity before releasing crashed references.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import subprocess
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

from ..shared_cache import (Artifact, ArtifactKey, CacheError, CacheLimits,
                           GcPolicy, GcResult, Lease, SharedArtifactCache,
                           TaskResource, cleanup_task_resources, sha256_bytes)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REFERENCE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._/:\-]*@sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{64}\Z")
OWNER = "io.dradar.v2.cache-owner"
KEY = "io.dradar.v2.cache-key"
ATTEMPT = "io.dradar.v2.task-attempt"


class DockerCacheError(CacheError):
    """Bounded diagnostic that does not expose Docker output or input secrets."""
    code = "docker_failed"


class DockerStorageError(DockerCacheError):
    """Positive storage exhaustion evidence; never relabelled as a network error."""
    code = "storage_enospc"


def _task_token(attempt: str) -> str:
    if not isinstance(attempt, str) or not attempt or len(attempt) > 1024:
        raise ValueError("unique task attempt identity required")
    return hashlib.sha256(attempt.encode()).hexdigest()


@dataclass(frozen=True)
class PublicBuild:
    """A caller-approved public snapshot; bytes never enter cache metadata.

    All effective context files must be supplied. No secret-dependent output,
    mutable base, build argument, external ADD, or writable cache mount is allowed.
    This intentionally supports only literal digest-pinned FROM instructions.
    """
    files: Mapping[str, bytes]
    base_references: tuple[str, ...]
    platform: str
    public_inputs: bool
    dockerfile: str = "Dockerfile"

    def snapshot(self) -> dict[str, bytes]:
        if self.public_inputs is not True:
            raise ValueError("public build inputs must be explicitly verified")
        result = {}
        for name, content in self.files.items():
            p = PurePosixPath(name)
            if (not isinstance(name, str) or not name or p.is_absolute()
                    or any(part in {".", ".."} for part in name.split("/"))
                    or "\\" in name or "\x00" in name or type(content) is not bytes):
                raise ValueError("regular public context files required")
            result[name] = content
        if self.dockerfile not in result or not self.base_references:
            raise ValueError("Dockerfile and pinned base images required")
        if not all(_REFERENCE.fullmatch(ref) for ref in self.base_references):
            raise ValueError("all base references must be full manifest digest pins")
        text = result[self.dockerfile].decode("utf-8").replace("\\\n", "")
        # Refuse alternate Dockerfile frontends and substitutions; the fixed
        # Docker builder is the sole toolchain and no secret values are accepted.
        if (re.search(r"(?im)^\s*(?:#\s*(?:syntax|escape)=|ARG\b|ADD\b)", text)
                or "--mount" in text or "--from" in text or "--network" in text):
            raise ValueError("external frontend, ARG, ADD and mounts are unsupported")
        bases = []
        for line in text.splitlines():
            if re.match(r"(?i)^\s*FROM\b", line):
                words = line.split()
                if len(words) not in (2, 4) or (len(words) == 4 and words[2].upper() != "AS"):
                    raise ValueError("literal digest-pinned FROM instructions required")
                bases.append(words[1])
        if tuple(bases) != self.base_references:
            raise ValueError("ordered Dockerfile base pins must match the build identity")
        return result


def _context_digest(files: Mapping[str, bytes]) -> str:
    encoded = [[name, sha256_bytes(content)] for name, content in sorted(files.items())]
    return sha256_bytes(json.dumps(encoded, separators=(",", ":")).encode())


class DockerCache:
    def __init__(self, root: Path, *, context: str | None = None,
                 run: Callable = subprocess.run, timeout_seconds: int = 120,
                 limits: CacheLimits | None = None):
        if type(timeout_seconds) is not int or timeout_seconds < 1:
            raise ValueError("positive Docker timeout required")
        self.run, self.timeout = run, timeout_seconds
        self._errors = threading.local()
        if context is None:
            context = self._command(["context", "show"], scoped=False).stdout.strip()
        if not isinstance(context, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", context):
            raise DockerCacheError("invalid Docker context")
        self.context = context
        self.daemon_id = self._daemon()
        identity = json.dumps([os.getuid(), self.context, self.daemon_id], separators=(",", ":"))
        self.namespace = hashlib.sha256(identity.encode()).hexdigest()
        self.tag_prefix = "dradar-v2-cache-" + self.namespace[:20]
        try:
            self.cache = SharedArtifactCache(Path(root) / self.namespace, limits=limits)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise self._storage_error() from None
            raise DockerCacheError("cache directory unavailable") from None

    def _command(self, args: Sequence[str], *, scoped: bool = True, missing: bool = False):
        argv = ["docker"] + (["--context", self.context] if scoped else []) + list(args)
        try:
            proc = self.run(argv, capture_output=True, text=True, check=False, timeout=self.timeout)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise self._storage_error() from None
            raise DockerCacheError("Docker command unavailable") from None
        except subprocess.TimeoutExpired:
            raise DockerCacheError("Docker command timed out; state retained") from None
        if proc.returncode != 0:
            detail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).lower()
            if "enospc" in detail or "no space left on device" in detail:
                raise self._storage_error() from None
            if missing and re.search(r"No such (?:image|container|object):", proc.stderr or "", re.I):
                return None
            raise DockerCacheError("Docker command failed; state retained")
        return proc

    def _storage_error(self) -> DockerStorageError:
        self._errors.storage = True
        return DockerStorageError("Docker/cache storage exhausted; preparation blocked")

    def _guard_storage(self, operation: Callable, *args):
        try:
            return operation(*args)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise self._storage_error() from None
            raise

    def _ensure(self, key: ArtifactKey, attempt: str, *, build: Callable, validate: Callable) -> Lease:
        # The generic coordinator scrubs backend exception messages. Preserve
        # only the positive, bounded storage classification across that boundary.
        # Thread-local state prevents another task's error changing this result.
        self._errors.storage = False
        try:
            return self.cache.ensure_and_acquire(
                key, attempt,
                build=lambda item: self._guard_storage(build, item),
                validate=lambda item, artifact: self._guard_storage(validate, item, artifact),
            )
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise self._storage_error() from None
            raise DockerCacheError("cache metadata unavailable; state retained") from None
        except CacheError:
            if getattr(self._errors, "storage", False):
                raise self._storage_error() from None
            raise

    def validate_image_lease(self, *, token: str, attempt: str, repository_digest: str,
                             platform: str, object_id: str, daemon_id: str,
                             context: str, endpoint: str) -> bool:
        """Live Binding verifier: exact durable owner/key/object and Docker domain.

        Use with the binding package's async LeaseCheck via asyncio.to_thread.
        No PID/age inference, constant-true hook, tag, config ID as manifest pin,
        or supplied binding alone can establish an active image reference.
        Validation never pulls, creates, releases, or rewrites cache metadata.
        The acquire platform must exactly match the recorded binding platform.
        """
        try:
            if (not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token)
                    or not isinstance(repository_digest, str) or not _REFERENCE.fullmatch(repository_digest)
                    or not isinstance(object_id, str) or not _DIGEST.fullmatch(object_id)
                    or daemon_id != self.daemon_id or context != self.context
                    or not isinstance(endpoint, str) or not endpoint):
                return False
            key = ArtifactKey("image", repository_digest.rsplit("@", 1)[1], platform)
            owner = self.cache._owner(attempt)
            self._check_daemon()
            domains = self._json(["context", "inspect", self.context])
            if not isinstance(domains, list) or len(domains) != 1:
                return False
            docker = domains[0].get("Endpoints", {}).get("docker", {})
            if docker.get("Host") != endpoint or docker.get("SkipTLSVerify", False) is not False:
                return False
            with self.cache._lock("metadata.lock"):
                item = self.cache._load()["entries"].get(key.token)
                if (not item or item.get("state") != "ready" or item.get("kind") != "image"
                        or item.get("digest") != key.digest or item.get("platform") != key.platform
                        or item.get("object_id") != object_id or item["refs"].get(token) != owner):
                    return False
                image = self._image(object_id)
                return bool(image and image.get("Id") == object_id
                            and self._platform(image, platform)
                            and repository_digest in image.get("RepoDigests", []))
        except (CacheError, OSError, ValueError, TypeError, KeyError, AttributeError):
            return False

    def _json(self, args: Sequence[str], *, missing: bool = False):
        proc = self._command(args, missing=missing)
        if proc is None:
            return None
        try:
            return json.loads(proc.stdout)
        except (ValueError, TypeError):
            raise DockerCacheError("invalid Docker response; state retained") from None

    def _daemon(self) -> str:
        value = self._json(["info", "--format", "{{json .ID}}"])
        if not isinstance(value, str) or not value or len(value) > 256:
            raise DockerCacheError("Docker daemon identity unavailable")
        return value

    def _check_daemon(self):
        if self._daemon() != self.daemon_id:
            raise DockerCacheError("Docker daemon identity changed; state retained")

    def _image(self, reference: str) -> dict | None:
        rows = self._json(["image", "inspect", reference], missing=True)
        if rows is None:
            return None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise DockerCacheError("invalid image inventory")
        return rows[0]

    @staticmethod
    def _platform(image: dict, platform: str) -> bool:
        actual = image.get("Os", "") + "/" + image.get("Architecture", "")
        variant = image.get("Variant")
        # Docker omits the default v8 variant on locally-built arm64 images.
        if not variant and image.get("Architecture") == "arm64":
            variant = "v8"
        if variant:
            actual += "/" + variant
        return actual == platform or (len(platform.split("/")) == 2 and actual.rsplit("/", 1)[0] == platform)

    def _artifact(self, image: dict) -> Artifact:
        try:
            return Artifact(image["Id"], image["Size"])
        except (KeyError, ValueError, TypeError):
            raise DockerCacheError("invalid immutable image identity") from None

    def ensure_pinned_image(self, reference: str, *, platform: str, attempt: str) -> Lease:
        if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
            raise ValueError("full immutable registry manifest pin required")
        key = ArtifactKey("image", reference.rsplit("@", 1)[1], platform)
        self._check_daemon()

        def validate(item, artifact):
            self._check_daemon()
            image = self._image(artifact.object_id)
            return bool(image and image.get("Id") == artifact.object_id
                        and self._platform(image, item.platform)
                        and any(isinstance(d, str) and d.endswith("@" + item.digest)
                                for d in image.get("RepoDigests", [])))

        def build(item):
            image = self._image(reference)
            if image is None:
                self._command(["pull", "--platform", platform, reference])
                image = self._image(reference)
            if image is None:
                raise DockerCacheError("pinned image missing after pull")
            return self._artifact(image)

        return self._ensure(key, attempt, build=build, validate=validate)

    def _build_state(self, token: str | None = None, *, action: str = "read"):
        """Bounded durable fence for Docker work surviving a coordinator crash.

        Entries never expire. A timeout does not prove remote BuildKit exited.
        Keep this ledger separate from the coordinator's retryable reservation.
        """
        path = self.cache.root / "docker-builds.json"
        with self.cache._lock("docker-build-state.lock"):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                raw = b"{}"
            else:
                with os.fdopen(fd, "rb") as stream:
                    status = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                            or status.st_mode & 0o077):
                        raise DockerCacheError("unsafe build state; retained")
                    raw = stream.read(self.cache.limits.max_metadata_bytes + 1)
            try:
                def unique(pairs):
                    result = {}
                    for k, v in pairs:
                        if k in result:
                            raise ValueError("duplicate")
                        result[k] = v
                    return result
                data = json.loads(raw, object_pairs_hook=unique)
                if (len(raw) > self.cache.limits.max_metadata_bytes or not isinstance(data, dict)
                        or len(data) > self.cache.limits.max_entries
                        or any(not _ID.fullmatch(k) or v is not True for k, v in data.items())):
                    raise ValueError("invalid")
            except (ValueError, TypeError):
                raise DockerCacheError("invalid build state; retained") from None
            if action == "read":
                return tuple(sorted(data))
            if not isinstance(token, str) or not _ID.fullmatch(token):
                raise ValueError("build key token required")
            if action == "reserve":
                if token in data:
                    raise DockerCacheError("original Docker build outcome unknown; retry blocked")
                if len(data) >= self.cache.limits.max_entries:
                    raise DockerCacheError("build state full; unknown builds retained")
                data[token] = True
            elif action == "clear":
                data.pop(token, None)
            else:
                raise ValueError("invalid build state action")
            encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
            if len(encoded) > self.cache.limits.max_metadata_bytes:
                raise DockerCacheError("build state full; unknown builds retained")
            staging = self.cache.root / "docker-builds.tmp"
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                status = os.fstat(stream.fileno())
                if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                        or status.st_mode & 0o077):
                    raise DockerCacheError("unsafe build staging; retained")
                stream.truncate(0)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, path)
            directory_fd = os.open(self.cache.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def blocked_builds(self) -> tuple[str, ...]:
        return self._build_state()

    def resolve_inactive_build(self, key_token: str, *, confirm_inactive: Callable[[str], bool]):
        """Operator/runtime proof of original build exit; no PID/age-only recovery."""
        self._check_daemon()
        if confirm_inactive(key_token) is not True:
            raise DockerCacheError("original Docker build inactivity unconfirmed")
        self._build_state(key_token, action="clear")

    def ensure_public_build(self, spec: PublicBuild, *, attempt: str) -> Lease:
        files = spec.snapshot()
        self._check_daemon()
        version = self._json(["version", "--format", "{{json .Server.Version}}"])
        if not isinstance(version, str) or not version:
            raise DockerCacheError("Docker builder version unavailable")
        builder = self._command(["buildx", "inspect", self.context]).stdout
        drivers = re.findall(r"(?m)^Driver:\s+(\S+)\s*$", builder)
        endpoints = re.findall(r"(?m)^Endpoint:\s+(\S+)\s*$", builder)
        if drivers != ["docker"] or endpoints != [self.context]:
            raise DockerCacheError("context-local Docker builder required")
        buildx_version = self._command(["buildx", "version"]).stdout.strip()
        if not buildx_version or len(buildx_version) > 512:
            raise DockerCacheError("Docker build toolchain unavailable")
        options = ["docker-build-v1", version, buildx_version, spec.dockerfile,
                   "network=none", "pull=false", "builder=" + self.context]
        key = ArtifactKey.image_build(
            dockerfile_digest=sha256_bytes(files[spec.dockerfile]),
            context_digest=_context_digest(files),
            base_digests=[ref.rsplit("@", 1)[1] for ref in spec.base_references],
            platform=spec.platform,
            toolchain_digest=sha256_bytes(json.dumps(options).encode()),
        )
        tag = self.tag_prefix + ":" + key.token

        def validate(item, artifact):
            self._check_daemon()
            image = self._image(artifact.object_id)
            labels = (image or {}).get("Config", {}).get("Labels") or {}
            return bool(image and image.get("Id") == artifact.object_id
                        and self._platform(image, item.platform)
                        and labels.get(OWNER) == self.namespace and labels.get(KEY) == item.token)

        def build(item):
            image = self._image(tag)
            if image is not None:
                existing = self._artifact(image)
                if not validate(item, existing):
                    raise DockerCacheError("cache tag belongs to an unknown image")
                self._build_state(item.token, action="clear")
                return existing  # Recover build committed to Docker before metadata.
            self._build_state(item.token, action="reserve")
            with tempfile.TemporaryDirectory(prefix="dradar-v2-public-build-") as directory:
                for name, content in files.items():
                    path = Path(directory) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                self._command(["build", "--builder", self.context, "--pull=false", "--network=none", "--platform", spec.platform,
                               "--label", OWNER + "=" + self.namespace,
                               "--label", KEY + "=" + item.token, "--tag", tag,
                               "--file", str(Path(directory) / spec.dockerfile), directory])
            image = self._image(tag)
            if image is None:
                raise DockerCacheError("built image missing")
            artifact = self._artifact(image)
            if not validate(item, artifact):
                raise DockerCacheError("built image identity unconfirmed; state retained")
            self._build_state(item.token, action="clear")
            return artifact

        # Acquire bases outside the derived-image hook (nested coordinator
        # calls can collide on a lock stripe). Keep references with this unique
        # attempt through authoritative teardown, including build timeout/crash.
        for reference in spec.base_references:
            self.ensure_pinned_image(reference, platform=spec.platform, attempt=attempt)
        return self._ensure(key, attempt, build=build, validate=validate)

    def create_task_container(self, lease: Lease, *, attempt: str,
                              command: Sequence[str], output: Path) -> TaskResource:
        """Create without starting. The runtime persists ID before paid work.

        Only a fresh, empty real output directory is mounted. No home/auth/env
        or shared mutable volumes are accepted; Docker creates a new write layer.
        Output is always retained by cleanup, including pending upload artifacts.
        """
        token = _task_token(attempt)
        if lease.key.kind != "image":
            raise ValueError("image lease required")
        if not command or any(not isinstance(word, str) or "\x00" in word for word in command):
            raise ValueError("explicit container argv required")
        output = Path(output)
        if (not output.is_absolute() or not output.is_dir() or output.is_symlink()
                or any(parent.is_symlink() for parent in output.parents)
                or any(output.iterdir()) or "," in str(output)):
            raise ValueError("fresh task-private output directory required")
        self._check_daemon()
        # Ensure caller cannot reuse another attempt's handle, or a released
        # lease, while coordinating with GC under the metadata lock.
        with self.cache._lock("metadata.lock"):
            data = self.cache._load()
            item = data["entries"].get(lease.key.token)
            if (not item or item["refs"].get(lease.token) != self.cache._owner(attempt)
                    or item.get("object_id") != lease.artifact.object_id):
                raise DockerCacheError("active image lease for this attempt required")
            marker = output / ".dradar-v2-output-owner.json"
            try:
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump({"cache_owner": self.namespace, "attempt": token}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                raise DockerCacheError("output already reserved or unknown; state retained") from None
            proc = self._command(["create", "--pull=never", "--network=none",
                                  "--label", OWNER + "=" + self.namespace,
                                  "--label", ATTEMPT + "=" + token,
                                  "--mount", f"type=bind,src={output},dst=/output",
                                  lease.artifact.object_id, *command])
            object_id = proc.stdout.strip()
            if not _ID.fullmatch(object_id):
                raise DockerCacheError("container creation identity unknown; lease retained")
        return TaskResource("container", object_id, attempt)

    def cleanup_container(self, resource: TaskResource) -> bool:
        """Stopped exact task container only; failure/unknown retains its lease."""
        if resource.kind != "container" or resource.shared or not _ID.fullmatch(resource.object_id):
            return False
        token = _task_token(resource.task_id)

        def inspect(value):
            self._check_daemon()
            rows = self._json(["container", "inspect", value.object_id], missing=True)
            if rows is None:
                return None
            if not isinstance(rows, list) or len(rows) != 1:
                raise DockerCacheError("container ownership unknown")
            row = rows[0]
            labels = row.get("Config", {}).get("Labels") or {}
            if (row.get("Id") != value.object_id or labels.get(OWNER) != self.namespace
                    or labels.get(ATTEMPT) != token or row.get("State", {}).get("Running") is not False
                    or row.get("State", {}).get("Status") not in {"created", "exited", "dead"}):
                raise DockerCacheError("container exit or ownership unknown")
            return value

        def remove(value):
            # Never force; a restart concurrent with inspection fails closed.
            self._command(["container", "rm", value.object_id])
            return True

        return cleanup_task_resources(resource.task_id, [resource], inspect=inspect, remove=remove).complete

    def release_attempt(self, attempt: str, *, confirm_inactive: Callable[[str], bool]) -> int:
        """Caller supplies authoritative stopped-runtime proof, including crash recovery."""
        self._check_daemon()
        token = _task_token(attempt)

        def verified(identity):
            if confirm_inactive(identity) is not True:
                return False
            self._check_daemon()
            proc = self._command(["container", "ls", "--all", "--quiet", "--no-trunc",
                                  "--filter", "label=" + ATTEMPT + "=" + token])
            return not proc.stdout.strip()

        return self.cache.release_task(attempt, confirm_inactive=verified)

    def collect(self, policy: GcPolicy) -> tuple[GcResult, ...]:
        self._check_daemon()

        def can_remove(kind, artifact):
            self._check_daemon()
            if kind != "image":
                return False
            image = self._image(artifact.object_id)
            if image is None:
                return False  # Unknown prior ownership is never adopted.
            labels = image.get("Config", {}).get("Labels") or {}
            tags = image.get("RepoTags")
            if (image.get("Id") != artifact.object_id or labels.get(OWNER) != self.namespace
                    or not _ID.fullmatch(labels.get(KEY, ""))
                    or not isinstance(tags, list) or any(not t.startswith(self.tag_prefix + ":") for t in tags)):
                return False
            proc = self._command(["container", "ls", "--all", "--quiet", "--no-trunc",
                                  "--filter", "ancestor=" + artifact.object_id])
            return not proc.stdout.strip()  # Includes stopped container references.

        def safe_check(kind, artifact):
            try:
                return can_remove(kind, artifact)
            except DockerCacheError:
                return False

        def remove(kind, artifact):
            # Fresh second safety check immediately before exact, non-force rm.
            if not safe_check(kind, artifact):
                return False
            try:
                self._command(["image", "rm", artifact.object_id])
            except DockerCacheError:
                return False
            return True

        return tuple(self.cache.remove_candidate(c, can_remove=safe_check, remove=remove)
                     for c in self.cache.plan_gc(policy))
