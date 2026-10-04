from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from dradar.shared_cache import (
    Artifact, ArtifactKey, CacheCorrupt, CacheError, CacheFull, CacheLimits,
    CacheLockTimeout, GcPolicy, SharedArtifactCache, TaskResource,
    cleanup_task_resources, sha256_bytes,
)


def key(value="base", kind="image", platform="linux/amd64"):
    return ArtifactKey(kind, sha256_bytes(value.encode()), platform)


def artifact(value="image", size=100):
    return Artifact(sha256_bytes(value.encode()), size)


class Clock:
    def __init__(self, now=1000):
        self.now = now

    def __call__(self):
        return self.now


class Backend:
    def __init__(self):
        self.items = {}
        self.builds = []
        self.removals = []

    def build(self, item):
        self.builds.append(item)
        result = Artifact(item.digest, 100)
        self.items[result.object_id] = result
        return result

    def validate(self, item, value):
        return self.items.get(value.object_id) == value

    def can_remove(self, kind, value):
        return True

    def remove(self, kind, value):
        self.removals.append(value)
        self.items.pop(value.object_id, None)
        return True


@pytest.fixture
def fixture(tmp_path):
    clock = Clock()
    cache = SharedArtifactCache(tmp_path / "cache", clock=clock)
    backend = Backend()
    return cache, backend, clock


def acquire(cache, backend, item=None, task="task-attempt-1"):
    return cache.ensure_and_acquire(item or key(), task, build=backend.build, validate=backend.validate)


def remove(cache, backend, candidate):
    return cache.remove_candidate(candidate, can_remove=backend.can_remove, remove=backend.remove)


def test_hit_reuses_verified_content_across_tasks_and_restart(fixture):
    cache, backend, clock = fixture
    first = acquire(cache, backend)
    second = acquire(SharedArtifactCache(cache.root, clock=clock), backend, task="task-attempt-2")
    assert not first.reused and second.reused
    assert first.artifact == second.artifact
    assert first.token != second.token
    assert len(backend.builds) == 1


def test_keys_cover_kind_platform_and_build_inputs():
    assert key() != key(platform="linux/arm64")
    assert key().token != key(kind="blob").token
    args = dict(dockerfile_digest=key("dockerfile").digest, context_digest=key("context").digest,
                base_digests=[key("base").digest], platform="linux/amd64", toolchain_digest=key("builder").digest)
    first = ArtifactKey.image_build(**args)
    assert first == ArtifactKey.image_build(**args)
    for field in ("dockerfile_digest", "context_digest", "toolchain_digest"):
        assert first != ArtifactKey.image_build(**{**args, field: key("changed").digest})
    assert first != ArtifactKey.image_build(**{**args, "base_digests": [key("other-base").digest]})
    assert first != ArtifactKey.image_build(**{**args, "platform": "linux/arm64"})


@pytest.mark.parametrize("make", [
    lambda: ArtifactKey("workspace", key().digest, "linux/amd64"),
    lambda: ArtifactKey("image", "latest", "linux/amd64"),
    lambda: ArtifactKey("image", "sha256:" + "A" * 64, "linux/amd64"),
    lambda: ArtifactKey("image", key().digest, "https://user:secret@host"),
    lambda: Artifact(key().digest, -1),
    lambda: Artifact(key().digest, True),
    lambda: CacheLimits(max_entries=0),
    lambda: CacheLimits(lock_timeout_seconds=float("nan")),
    lambda: GcPolicy(target_bytes=-1),
    lambda: GcPolicy(max_idle_seconds=float("inf")),
])
def test_input_validation(make):
    with pytest.raises(ValueError):
        make()


def test_never_stores_task_name_or_backend_secret(fixture):
    cache, backend, _ = fixture
    sensitive_name = "task name with private user text"
    lease = acquire(cache, backend, task=sensitive_name)
    content = cache.path.read_text()
    assert sensitive_name not in content
    assert lease.token in content
    def fail(item):
        raise RuntimeError("SECRET_BACKEND_TOKEN")
    with pytest.raises(CacheError) as exc:
        cache.ensure_and_acquire(key("error"), "other", build=fail, validate=backend.validate)
    assert "SECRET" not in str(exc.value)
    assert "SECRET" not in cache.path.read_text()


def test_no_automatic_cleanup_or_expiry(fixture):
    cache, backend, clock = fixture
    lease = acquire(cache, backend)
    clock.now += 10**9
    assert cache.plan_gc(GcPolicy(target_bytes=0, max_idle_seconds=0)) == ()
    assert not backend.removals
    assert cache.release(lease)
    assert not cache.release(lease)
    assert cache.plan_gc(GcPolicy()) == ()
    assert lease.artifact.object_id in backend.items


def test_budget_lru_min_age_and_age_goal(fixture):
    cache, backend, clock = fixture
    first = acquire(cache, backend, key("1"))
    cache.release(first)
    clock.now += 100
    second = acquire(cache, backend, key("2"))
    cache.release(second)
    clock.now += 10
    policy = GcPolicy(target_bytes=0, min_idle_seconds=50)
    plan = cache.plan_gc(policy)
    assert [x.artifact for x in plan] == [first.artifact]
    assert not backend.removals
    assert [x.artifact for x in cache.plan_gc(GcPolicy(target_bytes=100))] == [first.artifact]
    assert [x.artifact for x in cache.plan_gc(GcPolicy(target_entries=1))] == [first.artifact]
    assert [x.artifact for x in cache.plan_gc(GcPolicy(max_idle_seconds=50))] == [first.artifact]


def test_release_resets_idle_age_and_clock_rollback_is_safe(fixture):
    cache, backend, clock = fixture
    first = acquire(cache, backend)
    clock.now += 100
    cache.release(first)
    clock.now -= 200
    assert cache.plan_gc(GcPolicy(target_bytes=0)) == ()
    clock.now += 201
    assert cache.plan_gc(GcPolicy(target_bytes=0, min_idle_seconds=2)) == ()


def test_reacquire_invalidates_old_gc_plan_even_after_release(fixture):
    cache, backend, _ = fixture
    lease = acquire(cache, backend)
    cache.release(lease)
    candidate, = cache.plan_gc(GcPolicy(target_bytes=0))
    second = acquire(cache, backend)
    assert remove(cache, backend, candidate).reason == "protected"
    cache.release(second)
    assert remove(cache, backend, candidate).reason == "stale"
    assert not backend.removals
    fresh, = cache.plan_gc(GcPolicy(target_bytes=0))
    assert remove(cache, backend, fresh).removed
    assert remove(cache, backend, fresh).reason == "absent"


def test_backend_protection_and_removal_fail_closed(fixture):
    cache, backend, _ = fixture
    lease = acquire(cache, backend)
    cache.release(lease)
    candidate, = cache.plan_gc(GcPolicy(target_entries=0))
    result = cache.remove_candidate(candidate, can_remove=lambda *_: False, remove=backend.remove)
    assert result.reason == "backend_protected" and not backend.removals
    result = cache.remove_candidate(candidate, can_remove=backend.can_remove, remove=lambda *_: False)
    assert result.reason == "backend_retained"
    assert cache.plan_gc(GcPolicy(target_entries=0)) == (candidate,)
    def fail(*_):
        raise RuntimeError("PRIVATE_DOCKER_ERROR")
    with pytest.raises(CacheError, match="backend operation failed"):
        cache.remove_candidate(candidate, can_remove=backend.can_remove, remove=fail)
    assert cache.plan_gc(GcPolicy(target_entries=0)) == (candidate,)


def test_aliases_deduplicate_budget_and_protect_shared_object(fixture):
    cache, _, clock = fixture
    output = artifact()
    kwargs = dict(build=lambda _: output, validate=lambda *_: True)
    first = cache.ensure_and_acquire(key("recipe1"), "a", **kwargs)
    second = cache.ensure_and_acquire(key("recipe2"), "b", **kwargs)
    cache.release(first)
    assert cache.plan_gc(GcPolicy(target_bytes=0)) == ()
    cache.release(second)
    assert cache.plan_gc(GcPolicy(target_bytes=100)) == ()
    candidate, = cache.plan_gc(GcPolicy(target_entries=0))
    assert len(candidate.versions) == 2
    removed = []
    assert cache.remove_candidate(candidate, can_remove=lambda *_: True,
                                  remove=lambda *args: removed.append(args) is None).removed
    assert len(removed) == 1
    assert json.loads(cache.path.read_text())["entries"] == {}


def test_new_alias_invalidates_gc_plan(fixture):
    cache, _, _ = fixture
    kwargs = dict(build=lambda _: artifact(), validate=lambda *_: True)
    first = cache.ensure_and_acquire(key("a"), "a", **kwargs)
    cache.release(first)
    candidate, = cache.plan_gc(GcPolicy(target_bytes=0))
    alias = cache.ensure_and_acquire(key("b"), "b", **kwargs)
    cache.release(alias)
    assert cache.remove_candidate(candidate, can_remove=lambda *_: True,
                                  remove=lambda *_: pytest.fail("must not delete")).reason == "stale"


def test_failed_build_or_verification_is_not_published(fixture):
    cache, backend, _ = fixture
    def fail(_):
        raise OSError("network")
    with pytest.raises(CacheError):
        cache.ensure_and_acquire(key(), "a", build=fail, validate=backend.validate)
    assert json.loads(cache.path.read_text())["entries"] == {}
    with pytest.raises(CacheError, match="verification"):
        cache.ensure_and_acquire(key(), "a", build=lambda _: artifact(), validate=lambda *_: False)
    assert json.loads(cache.path.read_text())["entries"] == {}
    assert not acquire(cache, backend).reused


def test_missing_artifact_rebuild_requires_no_refs(fixture):
    cache, backend, _ = fixture
    first = acquire(cache, backend)
    backend.items.clear()
    with pytest.raises(CacheError, match="protected task references"):
        acquire(cache, backend, task="b")
    assert len(backend.builds) == 1
    cache.release(first)
    assert not acquire(cache, backend, task="b").reused
    assert len(backend.builds) == 2


def test_bounds_fail_without_deleting_or_starting_build(tmp_path):
    backend = Backend()
    cache = SharedArtifactCache(tmp_path / "cache", limits=CacheLimits(max_entries=1, max_references_per_entry=1))
    first = acquire(cache, backend)
    with pytest.raises(CacheFull, match="reference"):
        acquire(cache, backend)
    with pytest.raises(CacheFull, match="entry"):
        acquire(cache, backend, key("new"))
    assert len(backend.builds) == 1 and not backend.removals
    cache.release(first)
    assert acquire(cache, backend).reused


def test_metadata_bytes_bounded_before_build(tmp_path):
    backend = Backend()
    cache = SharedArtifactCache(tmp_path / "cache", limits=CacheLimits(max_metadata_bytes=32))
    with pytest.raises(CacheFull):
        acquire(cache, backend)
    assert not backend.builds
    assert not cache.path.exists()


@pytest.mark.parametrize("corruption", [b"not json", b'{"schema":99,"entries":{}}',
                                        b'{"schema":1,"entries":{"broken":{}}}'])
def test_corruption_fails_closed_without_reset(fixture, corruption):
    cache, backend, _ = fixture
    acquire(cache, backend)
    cache.path.write_bytes(corruption)
    with pytest.raises(CacheCorrupt):
        acquire(cache, backend, key("new"))
    with pytest.raises(CacheCorrupt):
        cache.plan_gc(GcPolicy(target_bytes=0))
    assert cache.path.read_bytes() == corruption
    assert len(backend.builds) == 1


def test_oversized_metadata_is_not_read_as_empty(fixture):
    cache, backend, _ = fixture
    acquire(cache, backend)
    cache.path.write_bytes(b" " * (cache.limits.max_metadata_bytes + 1))
    with pytest.raises(CacheCorrupt, match="exceeds"):
        acquire(cache, backend)


def test_private_root_and_no_symlink_metadata(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(CacheError, match="private"):
        SharedArtifactCache(unsafe)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(CacheError, match="private"):
        SharedArtifactCache(link)
    cache = SharedArtifactCache(tmp_path / "safe")
    target = tmp_path / "target"
    target.write_text("untouched")
    cache.path.symlink_to(target)
    with pytest.raises(OSError):
        acquire(cache, Backend())
    assert target.read_text() == "untouched"


def test_task_recovery_requires_authoritative_confirmation(fixture):
    cache, backend, _ = fixture
    acquire(cache, backend, key("1"), "a")
    acquire(cache, backend, key("2"), "a")
    remaining = acquire(cache, backend, key("1"), "b")
    with pytest.raises(CacheError, match="inactivity"):
        cache.release_task("a", confirm_inactive=lambda _: False)
    assert cache.plan_gc(GcPolicy(target_bytes=0)) == ()
    assert cache.release_task("a", confirm_inactive=lambda name: name == "a") == 2
    assert cache.release_task("a", confirm_inactive=lambda _: True) == 0
    candidate, = cache.plan_gc(GcPolicy(target_bytes=0))
    assert candidate.artifact.object_id != remaining.artifact.object_id


def test_task_cleanup_never_touches_shared_or_foreign_or_images():
    allowed = TaskResource("container", "container-id", "task")
    shared = TaskResource("volume", "shared-data", "task", shared=True)
    foreign = TaskResource("network", "network-id", "other")
    image = TaskResource("image", "image-id", "task")
    removed = []
    result = cleanup_task_resources("task", [allowed, shared, foreign, image], inspect=lambda x: x,
                                    remove=lambda x: removed.append(x) is None)
    assert result.removed == (allowed,)
    assert result.retained == (shared, foreign, image)
    assert removed == [allowed] and not result.complete


def test_task_cleanup_rechecks_owner_missing_and_errors():
    changed = TaskResource("container", "changed", "task")
    absent = TaskResource("container", "absent", "task")
    error = TaskResource("volume", "error", "task")
    def inspect(value):
        if value == changed:
            return TaskResource("container", "changed", "other")
        if value == absent:
            return None
        raise RuntimeError("SECRET")
    result = cleanup_task_resources("task", [changed, absent, error], inspect=inspect,
                                    remove=lambda _: pytest.fail("must not remove"))
    assert result.retained == (changed, error)
    assert not result.removed


def _process_acquire(root, start, ready, task, value="shared", unblock=None):
    cache = SharedArtifactCache(Path(root), limits=CacheLimits(lock_timeout_seconds=10))
    item = key(value)
    ready.put("started")
    start.wait(10)
    def build(_):
        with (Path(root) / "builds.txt").open("a") as stream:
            stream.write(value + "\n")
        if unblock is not None:
            ready.put("building")
            if not unblock.wait(10):
                raise TimeoutError()
        else:
            time.sleep(0.1)
        (Path(root) / (item.token + ".artifact")).write_text("ready")
        return Artifact(item.digest, 1)
    lease = cache.ensure_and_acquire(item, task, build=build,
        validate=lambda *_: (Path(root) / (item.token + ".artifact")).exists())
    ready.put((lease.reused, lease.token))


def test_processes_build_once_and_preserve_both_refs(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start, ready = ctx.Event(), ctx.Queue()
    root = tmp_path / "cache"
    processes = [ctx.Process(target=_process_acquire, args=(str(root), start, ready, str(n))) for n in range(2)]
    for process in processes:
        process.start()
    try:
        assert [ready.get(timeout=10), ready.get(timeout=10)] == ["started", "started"]
        start.set()
        results = [ready.get(timeout=10), ready.get(timeout=10)]
        assert sorted(x[0] for x in results) == [False, True]
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        assert (root / "builds.txt").read_text().splitlines() == ["shared"]
        entry, = json.loads((root / "metadata.json").read_text())["entries"].values()
        assert len(entry["refs"]) == 2
        assert SharedArtifactCache(root).plan_gc(GcPolicy(target_bytes=0)) == ()
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(10)


def test_unrelated_builds_can_progress_concurrently(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start, ready, unblock = ctx.Event(), ctx.Queue(), ctx.Event()
    names = ["first", "second"]
    assert int(key(names[0]).token[:8], 16) % 256 != int(key(names[1]).token[:8], 16) % 256
    root = tmp_path / "cache"
    processes = [ctx.Process(target=_process_acquire, args=(str(root), start, ready, n, n, unblock)) for n in names]
    for process in processes:
        process.start()
    try:
        assert [ready.get(timeout=10), ready.get(timeout=10)] == ["started", "started"]
        start.set()
        assert [ready.get(timeout=10), ready.get(timeout=10)] == ["building", "building"]
        unblock.set()
        assert not ready.get(timeout=10)[0]
        assert not ready.get(timeout=10)[0]
    finally:
        unblock.set()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(10)
            assert process.exitcode == 0


def test_killed_builder_releases_os_lock_and_same_key_recovers(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start, ready, unblock = ctx.Event(), ctx.Queue(), ctx.Event()
    root = tmp_path / "cache"
    process = ctx.Process(target=_process_acquire, args=(str(root), start, ready, "old", "shared", unblock))
    process.start()
    try:
        assert ready.get(timeout=10) == "started"
        start.set()
        assert ready.get(timeout=10) == "building"
        cache = SharedArtifactCache(root)
        assert cache.plan_gc(GcPolicy(target_bytes=0, max_idle_seconds=0)) == ()
    finally:
        process.terminate()
        process.join(10)
    lease = acquire(SharedArtifactCache(root), Backend(), key("shared"), "new")
    assert not lease.reused


def test_lock_timeout_is_bounded(fixture):
    cache, _, _ = fixture
    timer = Clock(0)
    other = SharedArtifactCache(cache.root, limits=CacheLimits(lock_timeout_seconds=0.1),
                                monotonic=timer, sleep=lambda seconds: setattr(timer, "now", timer.now + seconds))
    with cache._artifact_lock(key().token):
        with pytest.raises(CacheLockTimeout):
            with other._artifact_lock(key().token):
                pytest.fail("lock must not be acquired")
    assert timer.now == pytest.approx(0.1)


def test_duplicate_fields_and_boolean_schema_are_corruption(fixture):
    cache, backend, _ = fixture
    acquire(cache, backend)
    for bad in ('{"schema":true,"entries":{}}', '{"schema":1,"entries":{},"entries":{}}'):
        cache.path.write_text(bad)
        with pytest.raises(CacheCorrupt):
            cache.plan_gc(GcPolicy(target_bytes=0))


def test_crash_staging_is_reused_and_bounded(fixture):
    cache, backend, _ = fixture
    staging = cache.root / ".metadata.next"
    staging.write_text("leftover interrupted write" * 1000)
    staging.chmod(0o600)
    acquire(cache, backend)
    assert not staging.exists()
    assert not list(cache.root.glob(".metadata-*"))
    assert len(cache.path.read_bytes()) < 2000


def test_gc_then_inflight_alias_publication_revalidates(fixture):
    cache, backend, _ = fixture
    first = acquire(cache, backend, key("old"))
    cache.release(first)
    candidate, = cache.plan_gc(GcPolicy(target_bytes=0))
    # Simulate an adapter obtaining the same Docker image for a new build key,
    # followed by eviction of the old idle alias before publication.
    def build(_):
        assert remove(cache, backend, candidate).removed
        return first.artifact
    with pytest.raises(CacheError, match="verification"):
        cache.ensure_and_acquire(key("new"), "new-task", build=build, validate=backend.validate)
    assert json.loads(cache.path.read_text())["entries"] == {}


def test_lease_persists_across_owner_process_exit(fixture):
    cache, backend, clock = fixture
    acquire(cache, backend)
    reloaded = SharedArtifactCache(cache.root, clock=lambda: clock.now + 10**10)
    assert reloaded.plan_gc(GcPolicy(target_bytes=0, max_idle_seconds=0)) == ()


def test_tasks_share_image_but_cleanup_leaves_cache(fixture):
    cache, backend, _ = fixture
    first = acquire(cache, backend, task="task-1")
    second = acquire(cache, backend, task="task-2")
    container = TaskResource("container", "container-1", "task-1")
    result = cleanup_task_resources("task-1", [container], inspect=lambda r: r, remove=lambda r: True)
    assert result.complete
    assert cache.release_task("task-1", confirm_inactive=lambda _: True) == 1
    assert first.artifact.object_id in backend.items
    assert cache.plan_gc(GcPolicy(target_bytes=0)) == ()
    assert cache.release(second)
    assert first.artifact.object_id in backend.items
