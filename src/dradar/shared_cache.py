"""Small, fail-closed coordinator for immutable image and public blob reuse.

POSIX hosts only; use one private local root per OS user and Docker daemon.
The caller owns all Docker/download operations. Hooks must be bounded, must not
re-enter this coordinator, and must never share writable task state. This module
stores only hashes, sizes, timestamps, and opaque reference tokens, never URLs,
commands, task names, credentials, or artifact contents.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal, Sequence

SCHEMA = 1
LOCK_STRIPES = 256  # Fixed inode set; never unlink a lock while waiters exist.
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_PLATFORM = re.compile(r"[a-z0-9][a-z0-9_./-]{0,79}\Z")


class CacheError(RuntimeError):
    """A safe, bounded diagnostic; backend exception contents are not retained."""


class CacheFull(CacheError):
    pass


class CacheCorrupt(CacheError):
    pass


class CacheLockTimeout(CacheError):
    pass


def _integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= 2**63 - 1


def _seconds(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _digest(value: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("expected a lowercase, full sha256 digest")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate metadata key")
        result[name] = value
    return result


def sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class ArtifactKey:
    kind: Literal["image", "blob"]
    digest: str
    platform: str

    def __post_init__(self) -> None:
        if self.kind not in ("image", "blob"):
            raise ValueError("cache supports only immutable images and public blobs")
        _digest(self.digest)
        if not isinstance(self.platform, str) or not _PLATFORM.fullmatch(self.platform):
            raise ValueError("invalid platform")

    @property
    def token(self) -> str:
        payload = ["dradar-immutable-cache-v1", self.kind, self.digest, self.platform]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()

    @classmethod
    def image_build(cls, *, dockerfile_digest: str, context_digest: str,
                    base_digests: Sequence[str], platform: str,
                    toolchain_digest: str) -> ArtifactKey:
        """Hash every output-affecting build input; raw secret values are forbidden.

        context_digest must cover the effective context, and toolchain_digest
        must cover builder version/options and non-secret build arguments.
        Base images must be pinned digests, never mutable tags. Secret-dependent
        outputs and task checkpoints are not eligible for this shared cache.
        """
        if not base_digests:
            raise ValueError("at least one pinned base image digest is required")
        values = [_digest(dockerfile_digest), _digest(context_digest),
                  [_digest(item) for item in base_digests], _digest(toolchain_digest)]
        return cls("image", sha256_bytes(json.dumps(values, separators=(",", ":")).encode()), platform)


@dataclass(frozen=True)
class Artifact:
    object_id: str  # Exact Docker image ID or checksum-addressed public blob ID.
    size_bytes: int  # Estimate only: shared Docker layers can make this an overcount.

    def __post_init__(self) -> None:
        _digest(self.object_id)
        if not _integer(self.size_bytes):
            raise ValueError("invalid artifact size")


@dataclass(frozen=True)
class CacheLimits:
    max_entries: int = 4096
    max_references_per_entry: int = 256
    max_metadata_bytes: int = 4 * 1024 * 1024
    lock_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not all(_integer(v, 1) for v in (
            self.max_entries, self.max_references_per_entry, self.max_metadata_bytes
        )) or not _seconds(self.lock_timeout_seconds):
            raise ValueError("invalid cache limits")


@dataclass(frozen=True)
class Lease:
    key: ArtifactKey
    token: str
    artifact: Artifact
    reused: bool


@dataclass(frozen=True)
class GcPolicy:
    """Explicit goals; an empty policy is a no-op, and active refs always win."""
    target_bytes: int | None = None
    target_entries: int | None = None
    min_idle_seconds: float = 0
    max_idle_seconds: float | None = None

    def __post_init__(self) -> None:
        if any(v is not None and not _integer(v) for v in (self.target_bytes, self.target_entries)):
            raise ValueError("invalid GC target")
        if not _seconds(self.min_idle_seconds) or (
            self.max_idle_seconds is not None and not _seconds(self.max_idle_seconds)
        ):
            raise ValueError("invalid GC age")


@dataclass(frozen=True)
class GcCandidate:
    kind: str
    artifact: Artifact
    versions: tuple[tuple[str, str], ...]  # All aliases, with anti-ABA generations.
    last_used: float


@dataclass(frozen=True)
class GcResult:
    removed: bool
    reason: str


class SharedArtifactCache:
    def __init__(self, root: Path, *, limits: CacheLimits | None = None,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if os.name != "posix":
            raise CacheError("shared cache locking requires a POSIX host")
        self.root = Path(root)
        self.limits = limits or CacheLimits()
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = self.root.lstat()
        if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
            raise CacheError("cache root must be a private directory owned by this user")
        self.path = self.root / "metadata.json"

    def _now(self) -> float:
        result = self.clock()
        if not _seconds(result):
            raise CacheError("invalid cache clock")
        return float(result)

    @contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        import fcntl
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
        fd = os.open(self.root / name, flags, 0o600)
        try:
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid() or status.st_mode & 0o077:
                raise CacheError("unsafe cache lock file")
            end = self.monotonic() + self.limits.lock_timeout_seconds
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = end - self.monotonic()
                    if remaining <= 0:
                        raise CacheLockTimeout("timed out waiting for cache lock") from None
                    self.sleep(min(0.05, remaining))
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _artifact_lock(self, token: str):
        # Hash striping bounds lock-file growth, including failed builds. Same
        # artifact always uses the same lock; rare collisions just serialize.
        return self._lock(f"artifact-{int(token[:8], 16) % LOCK_STRIPES:03d}.lock")

    def _load(self) -> dict:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"schema": SCHEMA, "entries": {}}
        try:
            with os.fdopen(fd, "rb") as stream:
                status = os.fstat(stream.fileno())
                if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid() or status.st_mode & 0o077:
                    raise CacheCorrupt("unsafe cache metadata file")
                raw = stream.read(self.limits.max_metadata_bytes + 1)
            if len(raw) > self.limits.max_metadata_bytes:
                raise CacheCorrupt("cache metadata exceeds configured limit")
            data = json.loads(raw, object_pairs_hook=_unique_object)
            self._validate(data)
            return data
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise CacheCorrupt("invalid cache metadata; refusing to discard protection") from None

    def _validate(self, data: dict) -> None:
        if not isinstance(data, dict) or set(data) != {"schema", "entries"} or type(data["schema"]) is not int or data["schema"] != SCHEMA:
            raise ValueError()
        entries = data["entries"]
        if not isinstance(entries, dict) or len(entries) > self.limits.max_entries:
            raise ValueError()
        for token, item in entries.items():
            if set(item) != {"kind", "digest", "platform", "state", "object_id", "size_bytes", "created", "used", "version", "refs"}:
                raise ValueError()
            key = ArtifactKey(item["kind"], item["digest"], item["platform"])
            if token != key.token or not _TOKEN.fullmatch(item["version"]):
                raise ValueError()
            if not _seconds(item["created"]) or not _seconds(item["used"]):
                raise ValueError()
            refs = item["refs"]
            if not isinstance(refs, dict) or len(refs) > self.limits.max_references_per_entry:
                raise ValueError()
            for lease_id, owner_hash in refs.items():
                if not _TOKEN.fullmatch(lease_id) or not _HEX.fullmatch(owner_hash):
                    raise ValueError()
            if item["state"] == "ready":
                Artifact(item["object_id"], item["size_bytes"])
            elif item["state"] != "building" or item["object_id"] is not None or item["size_bytes"] != 0 or refs:
                raise ValueError()

    def _save(self, data: dict) -> None:
        self._validate(data)
        raw = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(raw) > self.limits.max_metadata_bytes:
            raise CacheFull("cache metadata byte limit reached; explicit maintenance is required")
        temporary = self.root / ".metadata.next"
        # A fixed staging inode bounds crash leftovers. All writers hold the
        # metadata lock, so a killed writer cannot strand unbounded temp files.
        fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                status = os.fstat(stream.fileno())
                if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid() or status.st_mode & 0o077 or status.st_nlink != 1:
                    raise CacheCorrupt("unsafe cache staging file")
                stream.truncate(0)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _owner(task_id: str) -> str:
        if not isinstance(task_id, str) or not task_id or len(task_id) > 512:
            raise ValueError("a nonempty task-attempt identity of at most 512 characters is required")
        return hashlib.sha256(task_id.encode()).hexdigest()

    @staticmethod
    def _artifact(item: dict) -> Artifact:
        return Artifact(item["object_id"], item["size_bytes"])

    @staticmethod
    def _hook(check: Callable, *args):
        try:
            return check(*args)
        except Exception:
            raise CacheError("cache backend operation failed") from None

    def ensure_and_acquire(self, key: ArtifactKey, task_id: str, *,
                           build: Callable[[ArtifactKey], Artifact],
                           validate: Callable[[ArtifactKey, Artifact], bool]) -> Lease:
        """Serialize a download/build and acquire a durable, non-expiring ref.

        validate MUST check immutable identity and digest/build ownership. It is
        called on every hit and immediately before publication. build must be
        idempotent after interruption. Hook diagnostics are intentionally hidden.
        Release only after all corresponding task resources have stopped.
        """
        owner = self._owner(task_id)
        token = key.token
        with self._artifact_lock(token):
            with self._lock("metadata.lock"):
                data = self._load()
                item = data["entries"].get(token)
                if item and item["state"] == "ready":
                    if len(item["refs"]) >= self.limits.max_references_per_entry:
                        raise CacheFull("cache reference limit reached")
                    artifact = self._artifact(item)
                    if self._hook(validate, key, artifact) is True:
                        return self._acquire(data, key, owner, artifact, reused=True)
                    if item["refs"]:
                        raise CacheError("invalid artifact still has protected task references")
                if item is None and len(data["entries"]) >= self.limits.max_entries:
                    raise CacheFull("cache entry limit reached; explicit maintenance is required")
                now = self._now()
                version = uuid.uuid4().hex
                data["entries"][token] = {
                    "kind": key.kind, "digest": key.digest, "platform": key.platform,
                    "state": "building", "object_id": None, "size_bytes": 0,
                    "created": now, "used": now, "version": version, "refs": {},
                }
                self._save(data)  # Reserve bounded metadata before slow work.
            try:
                artifact = self._hook(build, key)
                if not isinstance(artifact, Artifact):
                    raise CacheError("backend returned an invalid artifact")
                with self._lock("metadata.lock"):
                    data = self._load()
                    # GC can remove an alias while this build is outside the
                    # metadata lock. Revalidate here before making the ref live.
                    if self._hook(validate, key, artifact) is not True:
                        raise CacheError("artifact verification failed")
                    item = data["entries"][token]
                    item.update(state="ready", object_id=artifact.object_id,
                                size_bytes=artifact.size_bytes)
                    return self._acquire(data, key, owner, artifact, reused=False)
            except BaseException:
                # If this cleanup fails, leave the reservation for the next
                # same-key ensure. Never turn corruption into an empty ledger.
                try:
                    with self._lock("metadata.lock"):
                        data = self._load()
                        item = data["entries"].get(token)
                        if item and item["state"] == "building" and item["version"] == version:
                            del data["entries"][token]
                            self._save(data)
                except Exception:
                    pass
                raise

    def _acquire(self, data: dict, key: ArtifactKey, owner: str,
                 artifact: Artifact, *, reused: bool) -> Lease:
        item = data["entries"][key.token]
        lease_id = uuid.uuid4().hex
        item["refs"][lease_id] = owner
        item["used"] = max(item["used"], self._now())
        item["version"] = uuid.uuid4().hex
        self._save(data)
        return Lease(key, lease_id, artifact, reused)

    def release(self, lease: Lease) -> bool:
        """Idempotently release this handle after task shutdown is confirmed."""
        with self._lock("metadata.lock"):
            data = self._load()
            item = data["entries"].get(lease.key.token)
            if not item or lease.token not in item["refs"]:
                return False
            del item["refs"][lease.token]
            item["used"] = max(item["used"], self._now())
            item["version"] = uuid.uuid4().hex
            self._save(data)
            return True

    def release_task(self, task_id: str, *, confirm_inactive: Callable[[str], bool]) -> int:
        """Crash recovery: retain refs unless the caller confirms task shutdown.

        The task ID must uniquely identify an execution attempt and must never
        be reused. confirm_inactive must consult the authoritative task/runtime
        state, not just a PID or elapsed time.
        """
        owner = self._owner(task_id)
        with self._lock("metadata.lock"):
            data = self._load()
            if self._hook(confirm_inactive, task_id) is not True:
                raise CacheError("task inactivity was not confirmed; references retained")
            count = 0
            for item in data["entries"].values():
                refs = {k: v for k, v in item["refs"].items() if v != owner}
                if len(refs) != len(item["refs"]):
                    count += len(item["refs"]) - len(refs)
                    item.update(refs=refs, used=max(item["used"], self._now()), version=uuid.uuid4().hex)
            if count:
                self._save(data)
            return count

    @staticmethod
    def _groups(data: dict) -> dict:
        groups: dict[tuple[str, str], list] = {}
        for token, item in data["entries"].items():
            if item["state"] == "ready":
                groups.setdefault((item["kind"], item["object_id"]), []).append((token, item))
        return groups

    @staticmethod
    def _candidate(identity: tuple[str, str], group: list) -> GcCandidate:
        return GcCandidate(identity[0], Artifact(identity[1], max(i["size_bytes"] for _, i in group)),
                           tuple(sorted((t, i["version"]) for t, i in group)),
                           max(i["used"] for _, i in group))

    def plan_gc(self, policy: GcPolicy) -> tuple[GcCandidate, ...]:
        """Read-only plan: LRU within explicit byte/count/age goals; no deletion.

        Count covers metadata entries, bytes are estimated unique object sizes.
        In-progress builds and every referenced alias are protected. A plan may
        not meet the requested budget when eligible objects are insufficient.
        """
        with self._lock("metadata.lock"):
            data = self._load()
        groups = self._groups(data)
        remaining_count = len(data["entries"])
        remaining_bytes = sum(max(i["size_bytes"] for _, i in g) for g in groups.values())
        candidates = [(self._candidate(identity, group), group) for identity, group in groups.items()]
        candidates.sort(key=lambda pair: (pair[0].last_used, pair[0].kind, pair[0].artifact.object_id))
        now, selected = self._now(), []
        for candidate, group in candidates:
            age = now - candidate.last_used
            if any(i["refs"] for _, i in group) or age < policy.min_idle_seconds:
                continue
            over_budget = ((policy.target_bytes is not None and remaining_bytes > policy.target_bytes)
                           or (policy.target_entries is not None and remaining_count > policy.target_entries))
            over_age = policy.max_idle_seconds is not None and age >= policy.max_idle_seconds
            if not over_budget and not over_age:
                continue
            selected.append(candidate)
            remaining_count -= len(group)
            remaining_bytes -= candidate.artifact.size_bytes
        return tuple(selected)

    def remove_candidate(self, candidate: GcCandidate, *,
                         can_remove: Callable[[str, Artifact], bool],
                         remove: Callable[[str, Artifact], bool]) -> GcResult:
        """Explicit deletion of exactly one planned immutable object.

        Rechecks every alias/reference/generation, then calls can_remove and
        remove while the metadata lock prevents new refs. The adapter must
        verify DRadar cache ownership and zero Docker container references,
        and remove only the exact object, never force-remove or globally prune.
        A missing object is an idempotent success only if the adapter verifies it.
        """
        with self._lock("metadata.lock"):
            data = self._load()
            identity = (candidate.kind, candidate.artifact.object_id)
            group = self._groups(data).get(identity)
            if not group:
                return GcResult(False, "absent")
            if any(i["refs"] for _, i in group):
                return GcResult(False, "protected")
            if self._candidate(identity, group) != candidate:
                return GcResult(False, "stale")
            if self._hook(can_remove, candidate.kind, candidate.artifact) is not True:
                return GcResult(False, "backend_protected")
            if self._hook(remove, candidate.kind, candidate.artifact) is not True:
                return GcResult(False, "backend_retained")
            for token, _ in group:
                del data["entries"][token]
            self._save(data)
            return GcResult(True, "removed")


@dataclass(frozen=True)
class TaskResource:
    kind: Literal["container", "network", "volume", "workspace"]
    object_id: str
    task_id: str
    shared: bool = False


@dataclass(frozen=True)
class TaskCleanupResult:
    removed: tuple[TaskResource, ...]
    retained: tuple[TaskResource, ...]

    @property
    def complete(self) -> bool:
        return not self.retained


def cleanup_task_resources(task_id: str, resources: Sequence[TaskResource], *,
                           inspect: Callable[[TaskResource], TaskResource | None],
                           remove: Callable[[TaskResource], bool]) -> TaskCleanupResult:
    """Remove only caller-enumerated task-exclusive objects after fresh checks.

    Never enumerates global Docker state, touches images, or releases cache refs.
    An inspect result of None means verified absent. An adapter must inspect exact ownership
    labels/path identity, not merely infer ownership from a prefix. A workspace
    adapter must require a task-private path and must not traverse symlinks.
    """
    SharedArtifactCache._owner(task_id)
    removed, retained = [], []
    for resource in resources:
        if resource.task_id != task_id or resource.shared or resource.kind not in ("container", "network", "volume", "workspace"):
            retained.append(resource)
            continue
        try:
            current = inspect(resource)
            if current is None:
                continue
            if current != resource or remove(resource) is not True:
                retained.append(resource)
            else:
                removed.append(resource)
        except Exception:
            retained.append(resource)  # Fail closed; do not return secret-bearing errors.
    return TaskCleanupResult(tuple(removed), tuple(retained))
