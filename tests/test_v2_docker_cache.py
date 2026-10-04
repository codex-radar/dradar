import json
import subprocess
from pathlib import Path

import pytest

from dradar.shared_cache import CacheError, GcPolicy, TaskResource, sha256_bytes
from dradar.v2.docker_cache import (ATTEMPT, KEY, OWNER, DockerCache, DockerCacheError,
                                    PublicBuild, DockerStorageError, _task_token)

DIGEST = 'sha256:' + 'a' * 64
IMAGE = 'sha256:' + 'b' * 64
BASE = 'sha256:' + 'e' * 64
CONTAINER = 'c' * 64
REF = 'registry.example/public@' + DIGEST


class Docker:
    def __init__(self):
        self.images = {}
        self.containers = {}
        self.calls = []
        self.daemon = 'synthetic-daemon'
        self.fail_list = False

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        args = argv[3:] if argv[1] == '--context' else argv[1:]
        value = ''
        if args == ['context', 'show']:
            value = 'test'
        elif args[:2] == ['context', 'inspect']:
            value = json.dumps([{'Endpoints': {'docker': {'Host': 'unix:///synthetic.sock', 'SkipTLSVerify': False}}}])
        elif args[:1] == ['info']:
            value = json.dumps(self.daemon)
        elif args[:2] == ['buildx', 'inspect']:
            value = 'Driver: docker\nEndpoint: test\n'
        elif args[:2] == ['buildx', 'version']:
            value = 'github.com/docker/buildx v0.33.0 fixture'
        elif args[:1] == ['version']:
            value = json.dumps('29.4.0')
        elif args[:2] == ['image', 'inspect']:
            image = self.images.get(args[2])
            if image is None:
                return subprocess.CompletedProcess(argv, 1, '', 'Error: No such image: missing')
            value = json.dumps([image])
        elif args[0] == 'pull':
            row = image_row()
            row['Id'] = BASE
            self.images[args[-1]] = self.images[BASE] = row
        elif args[0] == 'build':
            tag = args[args.index('--tag') + 1]
            labels = dict(v.split('=', 1) for i, v in enumerate(args) if i and args[i-1] == '--label')
            row = image_row(labels=labels, tags=[tag])
            self.images[tag] = self.images[IMAGE] = row
        elif args[0] == 'create':
            labels = dict(v.split('=', 1) for i, v in enumerate(args) if i and args[i-1] == '--label')
            self.containers[CONTAINER] = {'Id': CONTAINER, 'Config': {'Labels': labels},
                                          'State': {'Running': False, 'Status': 'created'}}
            value = CONTAINER
        elif args[:2] == ['container', 'inspect']:
            if args[2] not in self.containers:
                return subprocess.CompletedProcess(argv, 1, '', 'No such container: missing')
            value = json.dumps([self.containers[args[2]]])
        elif args[:2] == ['container', 'ls']:
            if self.fail_list:
                return subprocess.CompletedProcess(argv, 1, '', 'sensitive backend error')
            value = '\n'.join(self.containers)
        elif args[:2] == ['container', 'rm']:
            if self.containers[args[2]]['State']['Running']:
                return subprocess.CompletedProcess(argv, 1, '', 'running')
            del self.containers[args[2]]
        elif args[:2] == ['image', 'rm']:
            row = self.images[args[2]]
            self.images = {k:v for k,v in self.images.items() if v is not row}
        else:
            raise AssertionError(args)
        assert kwargs['timeout'] > 0 and kwargs['capture_output'] and kwargs['text']
        return subprocess.CompletedProcess(argv, 0, value, '')

    def commands(self, command):
        return [c for c in self.calls if c[3:3+len(command)] == command]


def image_row(*, labels=None, tags=None):
    return {'Id': IMAGE, 'Size': 1234, 'Os': 'linux', 'Architecture': 'arm64',
            'RepoDigests': [REF], 'RepoTags': tags or ['public:latest'],
            'Config': {'Labels': labels or {}}}


def spec(**kwargs):
    values = dict(files={'Dockerfile': ('FROM ' + REF + '\nCOPY fixture /fixture\n').encode(),
                        'fixture': b'public fixture'}, base_references=(REF,),
                  platform='linux/arm64', public_inputs=True)
    values.update(kwargs)
    return PublicBuild(**values)


@pytest.fixture
def adapter(tmp_path):
    docker = Docker()
    cache = DockerCache(tmp_path / 'cache', run=docker.run)
    return cache, docker


def test_pinned_pull_once_and_never_adopts_user_image_gc(adapter):
    cache, docker = adapter
    a = cache.ensure_pinned_image(REF, platform='linux/arm64', attempt='one')
    b = cache.ensure_pinned_image(REF, platform='linux/arm64', attempt='two')
    assert not a.reused and b.reused and a.artifact == b.artifact
    assert len(docker.commands(['pull'])) == 1
    assert cache.collect(GcPolicy(target_bytes=0)) == ()
    cache.release_attempt('one', confirm_inactive=lambda _: True)
    cache.release_attempt('two', confirm_inactive=lambda _: True)
    assert cache.collect(GcPolicy(target_bytes=0))[0].reason == 'backend_protected'
    assert not docker.commands(['image', 'rm'])


@pytest.mark.parametrize('reference', ['public:latest', 'public@sha256:short', '-bad@' + DIGEST])
def test_mutable_and_invalid_refs_fail_before_docker(adapter, reference):
    cache, docker = adapter
    calls = len(docker.calls)
    with pytest.raises(ValueError):
        cache.ensure_pinned_image(reference, platform='linux/arm64', attempt='one')
    assert len(docker.calls) == calls


def test_wrong_platform_fails_without_publishing(adapter):
    cache, docker = adapter
    with pytest.raises(CacheError):
        cache.ensure_pinned_image(REF, platform='linux/amd64', attempt='one')
    assert json.loads(cache.cache.path.read_text())['entries'] == {}


def test_public_build_reuse_input_invalidation_and_owned_gc(adapter):
    cache, docker = adapter
    a = cache.ensure_public_build(spec(), attempt='one')
    b = cache.ensure_public_build(spec(), attempt='two')
    assert a.artifact == b.artifact and b.reused
    assert len(docker.commands(['build'])) == 1
    command = docker.commands(['build'])[0]
    assert '--network=none' in command and '--pull=false' in command
    assert command[command.index('--builder')+1] == 'test'
    assert cache.collect(GcPolicy(target_entries=0)) == ()
    cache.release_attempt('one', confirm_inactive=lambda _: True)
    cache.release_attempt('two', confirm_inactive=lambda _: True)
    assert sum(r.removed for r in cache.collect(GcPolicy(target_entries=0))) == 1
    assert len(docker.commands(['image', 'rm'])) == 1
    assert all('prune' not in c and '--force' not in c for c in docker.calls)


def test_build_inputs_change_key(adapter):
    cache, docker = adapter
    a = cache.ensure_public_build(spec(), attempt='one')
    cache.release_attempt('one', confirm_inactive=lambda _: True)
    other = spec(files={'Dockerfile': ('FROM ' + REF + '\n').encode(), 'fixture': b'changed'})
    b = cache.ensure_public_build(other, attempt='two')
    assert a.key != b.key and len(docker.commands(['build'])) == 2


@pytest.mark.parametrize('change', [
    {'public_inputs': False},
    {'base_references': ('public:latest',)},
    {'files': {'Dockerfile': ('FROM ' + REF + '\nARG SECRET\n').encode()}},
    {'files': {'Dockerfile': ('FROM ' + REF + '\nADD https://x/y /y\n').encode()}},
    {'files': {'Dockerfile': ('FROM ' + REF + '\nCOPY --from=foreign:latest /x /x\n').encode()}},
    {'files': {'Dockerfile': ('FROM ' + REF).encode(), '../secret': b'bad'}},
])
def test_unsafe_builds_refused_before_commands(adapter, change):
    cache, docker = adapter
    calls = len(docker.calls)
    with pytest.raises(ValueError):
        cache.ensure_public_build(spec(**change), attempt='one')
    assert len(docker.calls) == calls


def test_task_container_lease_ownership_output_and_cleanup(adapter, tmp_path):
    cache, docker = adapter
    lease = cache.ensure_public_build(spec(), attempt='one')
    output = tmp_path / 'out'
    output.mkdir()
    resource = cache.create_task_container(lease, attempt='one', command=['true'], output=output)
    create = docker.commands(['create'])[0]
    assert lease.artifact.object_id in create and '--pull=never' in create
    assert resource.object_id == CONTAINER
    assert docker.containers[CONTAINER]['Config']['Labels'][ATTEMPT] == _task_token('one')
    with pytest.raises(ValueError):
        cache.create_task_container(lease, attempt='one', command=['true'], output=output)
    with pytest.raises(CacheError):
        cache.release_attempt('one', confirm_inactive=lambda _: True)
    docker.containers[CONTAINER]['State'] = {'Running': True, 'Status': 'running'}
    assert not cache.cleanup_container(resource)
    assert not docker.commands(['container', 'rm'])
    docker.containers[CONTAINER]['State'] = {'Running': False, 'Status': 'exited'}
    (output / 'result').write_text('pending')
    assert cache.cleanup_container(resource)
    assert (output / 'result').read_text() == 'pending'
    assert cache.release_attempt('one', confirm_inactive=lambda _: True) == 2
    assert cache.ensure_public_build(spec(), attempt='two').reused


def test_foreign_attempt_or_released_lease_cannot_create(adapter, tmp_path):
    cache, docker = adapter
    lease = cache.ensure_public_build(spec(), attempt='one')
    for attempt in ('foreign', 'one'):
        if attempt == 'one':
            cache.release_attempt('one', confirm_inactive=lambda _: True)
        output = tmp_path / attempt
        output.mkdir()
        with pytest.raises(DockerCacheError):
            cache.create_task_container(lease, attempt=attempt, command=['true'], output=output)
    assert not docker.commands(['create'])


def test_unknown_exit_or_docker_inventory_retains_refs(adapter):
    cache, docker = adapter
    cache.ensure_public_build(spec(), attempt='one')
    with pytest.raises(CacheError):
        cache.release_attempt('one', confirm_inactive=lambda _: None)
    docker.fail_list = True
    with pytest.raises(CacheError):
        cache.release_attempt('one', confirm_inactive=lambda _: True)
    assert cache.collect(GcPolicy(target_bytes=0)) == ()


def test_foreign_labels_and_stopped_container_gc_protected(adapter):
    cache, docker = adapter
    lease = cache.ensure_public_build(spec(), attempt='one')
    cache.release_attempt('one', confirm_inactive=lambda _: True)
    docker.containers[CONTAINER] = {'State': {'Running': False}}
    assert cache.collect(GcPolicy(target_bytes=0))[0].reason == 'backend_protected'
    docker.containers.clear()
    docker.images[IMAGE]['RepoTags'].append('user-image:keep')
    assert cache.collect(GcPolicy(target_bytes=0))[0].reason == 'backend_protected'
    docker.images[IMAGE]['RepoTags'].pop()
    docker.images[IMAGE]['Config']['Labels'][OWNER] = 'foreign'
    assert cache.collect(GcPolicy(target_bytes=0))[0].reason == 'backend_protected'
    assert not docker.commands(['image', 'rm'])


def test_changed_daemon_and_foreign_cleanup_fail_closed(adapter):
    cache, docker = adapter
    lease = cache.ensure_public_build(spec(), attempt='one')
    foreign = TaskResource('container', CONTAINER, 'one')
    docker.containers[CONTAINER] = {'Id': CONTAINER, 'Config': {'Labels': {}},
                                  'State': {'Running': False, 'Status': 'exited'}}
    assert not cache.cleanup_container(foreign)
    docker.daemon = 'replacement'
    with pytest.raises(DockerCacheError):
        cache.ensure_public_build(spec(), attempt='two')
    with pytest.raises(DockerCacheError):
        cache.collect(GcPolicy(target_bytes=0))
    assert not docker.commands(['container', 'rm'])


def test_gc_backend_errors_are_not_exposed(adapter):
    cache, docker = adapter
    cache.ensure_public_build(spec(), attempt='one')
    cache.release_attempt('one', confirm_inactive=lambda _: True)
    docker.fail_list = True
    result = cache.collect(GcPolicy(target_bytes=0))[0]
    assert not result.removed and result.reason == 'backend_protected'
    assert 'sensitive' not in repr(result)


def test_default_arm64_variant_accepts_real_local_build_metadata(adapter):
    cache, docker = adapter
    # Registry metadata carries v8, while local BuildKit may omit Variant.
    lease = cache.ensure_public_build(spec(platform='linux/arm64/v8'), attempt='one')
    assert lease.artifact.object_id == IMAGE
    assert not cache._platform(image_row(), 'linux/arm64/v9')


def test_non_context_builder_refused_before_build(adapter):
    cache, docker = adapter
    original = docker.run
    def wrong_builder(argv, **kwargs):
        if argv[3:5] == ['buildx', 'inspect']:
            return subprocess.CompletedProcess(argv, 0, 'Driver: docker-container\nEndpoint: foreign\n', '')
        return original(argv, **kwargs)
    cache.run = wrong_builder
    with pytest.raises(DockerCacheError, match='context-local'):
        cache.ensure_public_build(spec(), attempt='one')
    assert not docker.commands(['build']) and not docker.commands(['pull'])


def test_build_timeout_keeps_base_references_for_authoritative_recovery(adapter):
    cache, docker = adapter
    original = docker.run
    def failed_build(argv, **kwargs):
        if argv[3:4] == ['build']:
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'], output='private backend data')
        return original(argv, **kwargs)
    cache.run = failed_build
    with pytest.raises(CacheError, match='backend operation failed'):
        cache.ensure_public_build(spec(), attempt='one')
    assert cache.collect(GcPolicy(target_entries=0)) == ()
    assert 'private backend data' not in cache.cache.path.read_text()
    with pytest.raises(CacheError):
        cache.release_attempt('one', confirm_inactive=lambda _: False)
    assert cache.release_attempt('one', confirm_inactive=lambda _: True) == 1


def test_timeout_blocks_same_build_retry_until_original_exit_proven(adapter):
    cache, docker = adapter
    original = docker.run
    failed = []
    def failed_build(argv, **kwargs):
        if argv[3:4] == ['build']:
            failed.append(argv)
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        return original(argv, **kwargs)
    cache.run = failed_build
    with pytest.raises(CacheError):
        cache.ensure_public_build(spec(), attempt='one')
    assert len(cache.blocked_builds()) == 1
    key = cache.blocked_builds()[0]
    cache.run = original
    with pytest.raises(CacheError):
        cache.ensure_public_build(spec(), attempt='two')
    assert len(failed) == 1 and not docker.commands(['build'])
    with pytest.raises(DockerCacheError):
        cache.resolve_inactive_build(key, confirm_inactive=lambda _: None)
    cache.resolve_inactive_build(key, confirm_inactive=lambda token: token == key)
    assert cache.blocked_builds() == ()
    cache.ensure_public_build(spec(), attempt='three')
    assert len(docker.commands(['build'])) == 1


def test_original_image_completed_after_timeout_is_recovered_without_rebuild(adapter):
    cache, docker = adapter
    original = docker.run
    def completed_but_unknown(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[3:4] == ['build']:
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        return result
    cache.run = completed_but_unknown
    with pytest.raises(CacheError):
        cache.ensure_public_build(spec(), attempt='one')
    assert cache.blocked_builds()
    cache.run = original
    lease = cache.ensure_public_build(spec(), attempt='two')
    assert lease.artifact.object_id == IMAGE
    assert len(docker.commands(['build'])) == 1 and cache.blocked_builds() == ()


def test_corrupt_build_ledger_never_resets_unknown_state(adapter):
    cache, docker = adapter
    state = cache.cache.root / 'docker-builds.json'
    cache._build_state('a' * 64, action='reserve')
    state.write_text('{"duplicate":true,"duplicate":true}')
    with pytest.raises(CacheError, match='backend operation failed'):
        cache.ensure_public_build(spec(), attempt='one')
    assert not docker.commands(['build'])
    assert state.read_text() == '{"duplicate":true,"duplicate":true}'



def lease_fields(cache, lease, attempt="one"):
    return dict(token=lease.token, attempt=attempt, repository_digest=REF,
                platform=lease.key.platform, object_id=lease.artifact.object_id,
                daemon_id=cache.daemon_id, context=cache.context, endpoint="unix:///synthetic.sock")


def test_live_binding_lease_validator_checks_metadata_and_docker_domain(adapter):
    cache, docker = adapter
    lease = cache.ensure_pinned_image(REF, platform="linux/arm64", attempt="one")
    fields = lease_fields(cache, lease)
    metadata = cache.cache.path.read_bytes()
    assert cache.validate_image_lease(**fields) is True
    assert cache.cache.path.read_bytes() == metadata
    for field, value in [("token", "a"*32), ("attempt", "foreign"),
                         ("repository_digest", "other.example/public@"+DIGEST),
                         ("platform", "linux/amd64"), ("object_id", IMAGE),
                         ("daemon_id", "foreign"), ("context", "foreign"),
                         ("endpoint", "unix:///foreign.sock")]:
        assert cache.validate_image_lease(**{**fields, field:value}) is False
    cache.release_attempt("one", confirm_inactive=lambda _: True)
    assert cache.validate_image_lease(**fields) is False


def test_lease_validator_rejects_stale_daemon_or_image_provenance(adapter):
    cache, docker = adapter
    lease = cache.ensure_pinned_image(REF, platform="linux/arm64", attempt="one")
    fields = lease_fields(cache, lease)
    docker.images[BASE]["RepoDigests"] = []
    assert cache.validate_image_lease(**fields) is False
    docker.images[BASE]["RepoDigests"] = [REF]
    docker.daemon = "replacement"
    assert cache.validate_image_lease(**fields) is False


@pytest.mark.parametrize("detail", ["failed to register layer: no space left on device private-token",
                                    "ENOSPC during image import private-token"])
def test_disk_full_pull_remains_storage_error_through_scrubbed_backend(adapter, detail):
    cache, docker = adapter
    original = docker.run
    def exhausted(argv, **kwargs):
        if argv[3:4] == ["pull"]:
            return subprocess.CompletedProcess(argv, 1, "", detail)
        return original(argv, **kwargs)
    cache.run = exhausted
    with pytest.raises(DockerStorageError) as failure:
        cache.ensure_pinned_image(REF, platform="linux/arm64", attempt="one")
    assert failure.value.code == "storage_enospc"
    assert "private-token" not in str(failure.value) and "network" not in str(failure.value)
    assert not docker.commands(["create"]) and not docker.commands(["build"])
    assert json.loads(cache.cache.path.read_text())["entries"] == {}


def test_host_enospc_metadata_and_build_context_are_storage_errors(adapter, monkeypatch):
    import errno
    cache, docker = adapter
    def exhausted(*args, **kwargs):
        raise OSError(errno.ENOSPC, "private filesystem path")
    monkeypatch.setattr(cache.cache, "_save", exhausted)
    with pytest.raises(DockerStorageError) as failure:
        cache.ensure_pinned_image(REF, platform="linux/arm64", attempt="one")
    assert failure.value.code == "storage_enospc" and "private" not in str(failure.value)
    assert not docker.commands(["pull"])


def test_network_failure_is_not_falsely_reported_as_disk_full(adapter):
    cache, docker = adapter
    original = docker.run
    def network_error(argv, **kwargs):
        if argv[3:4] == ["pull"]:
            return subprocess.CompletedProcess(argv, 1, "", "network unreachable private-token")
        return original(argv, **kwargs)
    cache.run = network_error
    with pytest.raises(CacheError) as failure:
        cache.ensure_pinned_image(REF, platform="linux/arm64", attempt="one")
    assert not isinstance(failure.value, DockerStorageError)
    assert "private-token" not in str(failure.value)
