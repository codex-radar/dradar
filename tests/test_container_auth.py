from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import pytest

from dradar.container_auth import (
    AuthAdapter, AuthBinding, AuthCapabilities, AuthRegistry, AuthRequest,
    ContainerAuthError, default_registry,
)

CASES = [
    ('codex', 'openai', 'auth.json', 'file', 'CODEX_AUTH_JSON_PATH', 'file-copy', 'container-only'),
    ('codex', 'deepseek', 'auth.json', 'file', 'CODEX_AUTH_JSON_PATH', 'file-copy', 'unchanged'),
    ('claude-code', 'anthropic-subscription', '.credentials.json', 'file', 'oauth_config_file', 'file-copy', 'container-only'),
    ('claude-code', 'anthropic-subscription', 'oauth-token', 'file', 'oauth_token_file', 'process-token', 'unchanged'),
    ('kimi-code', 'kimi-subscription', 'kimi-code.json', 'file', 'auth_json_file', 'shared-directory', 'shared-store'),
    ('grok-build', 'xai-subscription', 'auth.json', 'file', 'auth_json_file', 'shared-directory', 'shared-store'),
    ('antigravity', 'google-antigravity-subscription', '.gemini', 'directory', 'auth_home_dir', 'shared-directory', 'shared-store'),
    ('codebuddy', 'codebuddy-subscription', 'login', 'directory', 'auth_dir', 'directory-copy', 'validated-merge'),
    ('zcode', 'bigmodel-coding-plan', 'key', 'file', 'api_key_file', 'file-copy', 'unchanged'),
    ('dsh-minimal', 'deepseek', 'key', 'file', 'api_key_file', 'file-copy', 'unchanged'),
]


@pytest.mark.parametrize('harness,provider,name,kind,arg,delivery,persistence', CASES)
def test_existing_harness_contracts_and_no_secret_in_diagnostics(tmp_path, harness, provider, name, kind, arg, delivery, persistence):
    source = tmp_path / name
    if kind == 'file': source.write_text('secret-credential-content')
    else: source.mkdir()
    binding = default_registry().bind_existing(harness, provider, source)
    assert binding is not None
    assert f'{arg}={source}' in binding.pier_args()
    assert binding.capabilities.delivery == delivery
    assert binding.capabilities.persistence == persistence
    assert str(source) not in repr(binding)
    assert str(source) not in str(binding.summary())
    assert 'secret-credential-content' not in ' '.join(binding.pier_args())
    assert ('shared_oauth=true' in binding.pier_args()) == (delivery == 'shared-directory')


@pytest.mark.parametrize('harness,provider,name,kind,arg,delivery,persistence', CASES)
def test_wrong_source_type_fails_before_container_launch(tmp_path, harness, provider, name, kind, arg, delivery, persistence):
    source = tmp_path / name
    if kind == 'file': source.mkdir()
    else: source.write_text('not-a-directory')
    with pytest.raises(ContainerAuthError):
        default_registry().bind_existing(harness, provider, source)


def test_source_selection_never_falls_back_to_another_account(tmp_path):
    with pytest.raises(ContainerAuthError):
        default_registry().bind_existing('codex', 'unconfigured-provider', tmp_path/'auth')
    with pytest.raises(ContainerAuthError):
        default_registry().bind_existing('unknown-harness', None, tmp_path/'auth')
    assert default_registry().bind_existing('unknown-harness', None, None) is None


@pytest.mark.parametrize('fail', [False, True])
def test_temporary_keys_are_cleaned_on_success_and_failure(tmp_path, fail):
    created = []
    original = tmp_path/'original-key'; original.write_text('user-key')
    def create(work):
        path = work/'task-key'; path.write_text(original.read_text()); created.append(path)
        return path
    registry = default_registry()
    def run():
        with registry.session(AuthRequest('zcode', None, tmp_path), {'create_zcode_api_key_file': create}) as binding:
            assert binding.source.read_text() == 'user-key'
            if fail: raise RuntimeError('task failed')
    if fail:
        with pytest.raises(RuntimeError, match='task failed'): run()
    else: run()
    assert original.read_text() == 'user-key'
    assert len(created) == 1 and not created[0].exists()


@pytest.mark.parametrize('failure', [RuntimeError('runtime failed'), KeyboardInterrupt()])
def test_native_context_receives_original_failure_and_retains_rotation(tmp_path, failure):
    source = tmp_path/'auth'; source.write_text('old')
    received = []
    @contextmanager
    def native(work):
        try: yield source
        except BaseException as error:
            received.append(error)
            # Simulate provider-owned reconciliation, preserving a rotated token.
            assert source.read_text() == 'rotated'
            raise
    with pytest.raises(type(failure)):
        with default_registry().session(AuthRequest('grok-build', None, tmp_path), {'grok_subscription_session': native}) as binding:
            binding.source.write_text('rotated')
            raise failure
    assert received == [failure]
    assert source.read_text() == 'rotated'


def test_two_tasks_share_native_store_without_serializing_entire_run(tmp_path):
    source = tmp_path/'auth'; source.write_text('old')
    entered = []
    @contextmanager
    def native(work):
        entered.append(work)
        yield source
    registry = default_registry()
    hooks = {'kimi_subscription_session': native}
    with registry.session(AuthRequest('kimi-code', None, tmp_path/'a'), hooks) as first:
        with registry.session(AuthRequest('kimi-code', None, tmp_path/'b'), hooks) as second:
            first.source.write_text('new')
            assert second.source.read_text() == 'new'
            assert len(entered) == 2
    assert source.read_text() == 'new'


def test_new_harness_registers_without_changing_runner_auth_branches(tmp_path, monkeypatch):
    import dradar.runner as runner
    source = tmp_path/'native-login'; source.write_text('secret')
    events = []
    @contextmanager
    def acquire(request, hooks):
        events.append('acquired')
        try: yield source
        finally: events.append('released')
    def bind(path):
        return AuthBinding('future-agent', 'future-subscription', path, 'file',
            AuthCapabilities('file-copy', 'native-cli', 'container-only'), 'native_auth_file')
    registry = AuthRegistry()
    registry.register(AuthAdapter('future-agent', 'future-subscription', acquire, bind), default=True)
    monkeypatch.setattr(runner, 'AUTH_REGISTRY', registry)
    monkeypatch.setattr(runner, '_resolve_user_tool', lambda name: '/usr/bin/pier')
    tasks = tmp_path/'tasks'; (tasks/'task').mkdir(parents=True)
    with registry.session(AuthRequest('future-agent', None, tmp_path), {}) as binding:
        command = runner.build_pier_command({'agent':'future-agent','task_id':'task','model':'model'},
            tasks, tmp_path/'jobs', 'job', tmp_path/'home', provider_auth_path=binding.source)
        assert f'native_auth_file={source}' in command
        assert 'secret' not in ' '.join(command)
    assert events == ['acquired', 'released']


def test_shared_delivery_uses_the_existing_mount_validator(tmp_path):
    source = tmp_path/'auth'; source.write_text('credential')
    binding = default_registry().bind_existing('grok-build', None, source)
    called=[]
    def mounts(harness, path):
        called.append((harness,path)); return 'validated-json'
    args=binding.environment_args('safe:Environment',mounts)
    assert called==[('grok-build',source)]
    assert args==['--environment-import-path','safe:Environment','--ek','shared_oauth_mounts_json=validated-json']


def test_binding_cannot_silently_change_harness_or_provider(tmp_path):
    source = tmp_path/'auth'; source.write_text('credential')
    registry=AuthRegistry()
    @contextmanager
    def acquire(request,hooks): yield source
    bad=AuthAdapter('one','provider',acquire,lambda path:AuthBinding('other','provider',path,'file',AuthCapabilities('file-copy','static','unchanged'),'key_file'))
    registry.register(bad,default=True)
    with pytest.raises(ContainerAuthError,match='match'):
        with registry.session(AuthRequest('one',None,tmp_path),{}): pass
    with pytest.raises(ContainerAuthError,match='already registered'):registry.register(bad)
