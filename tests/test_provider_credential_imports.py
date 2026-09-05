"""Explicit imports preserve authority sources and keep secrets out of artifacts."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dradar import credential_files, provider_config, providers, runner

MARKER = 'INERT_CREDENTIAL_SENTINEL_0061'


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else value.encode())
    path.chmod(0o600)
    return path


def claude_payload():
    return {'claudeAiOauth': {
        'accessToken': 'sk-ant-oat01-' + MARKER,
        'refreshToken': 'refresh-' + MARKER,
        'expiresAt': 1900000000000,
        'scopes': ['user:profile', 'user:inference'],
        'subscriptionType': 'pro', 'rateLimitTier': 'default_claude_pro',
    }}


def agy_source(path):
    private(path / 'antigravity-cli' / 'antigravity-oauth-token', json.dumps({
        'auth_method': 'consumer', 'token': {'access_token': MARKER, 'refresh_token': MARKER,
        'token_type': 'Bearer', 'expiry': '2030-01-01T00:00:00Z'}}))
    private(path / 'config' / 'config.json', json.dumps({'userSettings': {
        'projectId': 'local-project', 'mcpServers': {'do-not-import': MARKER}}}))
    private(path / 'antigravity-cli' / 'conversation.json', MARKER)
    return path


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home=tmp_path/'managed'
    monkeypatch.setenv('DRADAR_HOME',str(home))
    return home


@pytest.mark.parametrize('kind', ['link','parent_link','wide','fifo','foreign_owner'])
def test_unsafe_sources_rejected(tmp_path, monkeypatch, kind):
    source=private(tmp_path/'source'/'key',MARKER)
    if kind=='link':
        link=tmp_path/'link';link.symlink_to(source);source=link
    elif kind=='parent_link':
        link=tmp_path/'parent';link.symlink_to(source.parent,target_is_directory=True);source=link/source.name
    elif kind=='wide': source.chmod(0o644)
    elif kind=='fifo':
        source.unlink();os.mkfifo(source)
    else: monkeypatch.setattr(credential_files.os,'getuid',lambda: source.stat().st_uid+1)
    with pytest.raises((ValueError,OSError)):
        credential_files.read_private_credential(source)


def test_claude_import_preserves_native_config_and_legacy_source(tmp_path,isolated):
    source=private(tmp_path/'source'/'.credentials.json',json.dumps(claude_payload()))
    before=source.read_bytes(),source.stat().st_mtime_ns,source.stat().st_mode
    legacy=providers.store_claude_oauth_token('sk-ant-oat01-'+MARKER*2)
    assert provider_config._import_claude_config(source.parent)==0
    imported=providers.claude_config_path()
    assert json.loads(imported.read_text())==claude_payload()
    assert imported.stat().st_mode & 0o077 == 0
    assert legacy.read_text().strip()=='sk-ant-oat01-'+MARKER*2
    assert (source.read_bytes(),source.stat().st_mtime_ns,source.stat().st_mode)==before
    assert providers.claude_subscription_path()==imported
    assert providers.claude_subscription_error() is None
    imported.write_text('{}')
    assert providers.claude_subscription_error() is not None  # no silent old-login fallback


@pytest.mark.parametrize('invalid', [b'not json',b'{"apiKey":"inert-api-key"}', b'{"claudeAiOauth":{}}'])
def test_bad_claude_import_retains_previous(tmp_path,isolated,invalid):
    old=private(providers.claude_config_path(),json.dumps(claude_payload()))
    before=old.read_bytes()
    source=private(tmp_path/'source'/'.credentials.json',invalid)
    with pytest.raises(ValueError):provider_config._import_claude_config(source.parent)
    assert old.read_bytes()==before


def test_atomic_failure_preserves_old_bytes(tmp_path,monkeypatch):
    target=private(tmp_path/'private'/'key','old')
    def fail(*_):raise OSError('inert write failure')
    monkeypatch.setattr(credential_files.os,'replace',fail)
    with pytest.raises(OSError):credential_files.atomic_private_credential(target,b'new')
    assert target.read_bytes()==b'old'
    assert list(target.parent.iterdir())==[target]


def test_zcode_validates_coding_endpoint_before_commit(tmp_path,isolated,monkeypatch):
    source=private(tmp_path/'source-key',MARKER)
    old=private(providers.zcode_secret_path(),'previous')
    monkeypatch.setattr(provider_config,'zcode_cli_path',lambda:'/official/zcode.cjs')
    monkeypatch.setattr(provider_config,'zcode_cli_error',lambda _:None)
    installed=[]
    monkeypatch.setattr(provider_config,'store_zcode_cli',lambda p:installed.append(p))
    monkeypatch.setattr(provider_config,'_live_zcode_status',lambda key:1)
    assert provider_config._import_zcode_key(source)==1
    assert old.read_text()=='previous' and installed==[]
    monkeypatch.setattr(provider_config,'_live_zcode_status',lambda key:0 if key==MARKER else 1)
    assert provider_config._import_zcode_key(source)==0
    assert old.read_text().strip()==MARKER and source.read_text()==MARKER
    assert provider_config._ZCODE_MODELS_URL=='https://open.bigmodel.cn/api/coding/paas/v4/models'


def test_antigravity_import_is_minimal_and_never_logs_in(tmp_path,isolated,monkeypatch):
    source=agy_source(tmp_path/'official')
    secret=(source/'antigravity-cli/antigravity-oauth-token').read_bytes()
    monkeypatch.setattr(provider_config.shutil,'which',lambda _:'/docker')
    monkeypatch.setattr(provider_config,'_ensure_antigravity_linux_cli',lambda _:'/official/antigravity')
    assert provider_config._import_antigravity_config(source)==0
    target=providers.antigravity_auth_path()
    assert (target/'antigravity-cli/antigravity-oauth-token').read_bytes()==secret
    assert (source/'antigravity-cli/antigravity-oauth-token').read_bytes()==secret
    assert not (target/'antigravity-cli/conversation.json').exists()
    assert json.loads((target/'config/config.json').read_text())=={'userSettings':{'projectId':'local-project'}}
    assert providers.antigravity_auth_error() is None


def test_antigravity_failure_restores_previous_auth_and_proof(tmp_path,isolated,monkeypatch):
    monkeypatch.setattr(provider_config.shutil,'which',lambda _:'/docker')
    monkeypatch.setattr(provider_config,'_ensure_antigravity_linux_cli',lambda _:'/official/antigravity')
    source=agy_source(tmp_path/'official')
    assert provider_config._import_antigravity_config(source)==0
    target=providers.antigravity_auth_path()/'antigravity-cli/antigravity-oauth-token'
    before=target.read_bytes();ready=providers.antigravity_ready_path();proof=ready.read_bytes()
    real=provider_config.atomic_private_credential
    failures=[]
    def fail_once(path,data):
        if path==ready and not failures:
            failures.append(True);raise OSError('inert proof failure')
        return real(path,data)
    monkeypatch.setattr(provider_config,'atomic_private_credential',fail_once)
    with pytest.raises(OSError):provider_config._import_antigravity_config(source)
    assert target.read_bytes()==before and ready.read_bytes()==proof


def test_claude_runner_uses_native_file_argument(tmp_path,isolated,monkeypatch):
    monkeypatch.setattr(runner.shutil,'which',lambda _:'/usr/bin/pier')
    source=private(tmp_path/'source'/'.credentials.json',json.dumps(claude_payload()))
    provider_config._import_claude_config(source.parent)
    tasks=tmp_path/'tasks';(tasks/'task-1').mkdir(parents=True)
    assignment={'assignment_id':'test-1','task_id':'task-1','agent':providers.CLAUDE_AGENT,
        'provider':providers.CLAUDE_PROVIDER,'model':providers.CLAUDE_SONNET_MODEL,'effort':'high',
        'agent_version':providers.CLAUDE_CLI_VERSION,'est_minutes':5}
    argv=runner.build_pier_command(assignment,tasks,tmp_path/'jobs','job',tmp_path/'work',provider_auth_path=providers.claude_subscription_path())
    assert f'oauth_config_file={providers.claude_config_path()}' in argv
    assert not any(x.startswith('oauth_token_file=') for x in argv)
    assert MARKER not in repr(argv)


@pytest.mark.parametrize('finish',['normal','agent_error','cleanup_failure'])
def test_native_claude_credentials_never_enter_collected_logs(tmp_path,monkeypatch,finish):
    from dradar.pier_claude import ClaudeCodeSubscription
    from pier.agents.installed.claude_code import ClaudeCode
    source=private(tmp_path/'source'/'.credentials.json',json.dumps(claude_payload()))
    container=tmp_path/'container';(container/'tmp').mkdir(parents=True)
    logs=container/'logs/agent';logs.mkdir(parents=True)
    traces=[]
    def mapped(value):return str(container)+value
    class Environment:
        default_user=None
        def agent_process_env(self,env):return env
        async def upload_file(self,src,target):
            shutil.copyfile(src,mapped(target))
        async def exec(self,command,cwd=None,env=None,timeout_sec=None,user=None):
            traces.append((command,env))
            if command.endswith('run-inert-model'):
                assert env and env['CLAUDE_CONFIG_DIR'].startswith('/tmp/dradar-claude-auth-')
                assert not any(credential_files.is_claude_metered_auth(k) for k in env)
                assert 'CLAUDE_CODE_OAUTH_TOKEN' not in env
                cfg=Path(mapped(env['CLAUDE_CONFIG_DIR']))
                assert json.loads((cfg/'.credentials.json').read_text())==claude_payload()
                project=cfg/'projects/-app/session';project.mkdir(parents=True)
                (project/'events.jsonl').write_text('{"type":"assistant","message":"normal session"}\n')
                return SimpleNamespace(return_code=0,stdout='',stderr='')
            if 'rm -rf -- /tmp/dradar-claude-auth-' in command and finish=='cleanup_failure':
                return SimpleNamespace(return_code=1,stdout='',stderr='inert cleanup failure')
            local=command.replace('/tmp/dradar-claude-auth-',mapped('/tmp/dradar-claude-auth-')).replace('/logs/agent',mapped('/logs/agent'))
            r=subprocess.run(['bash','-c',local],capture_output=True,text=True)
            return SimpleNamespace(return_code=r.returncode,stdout=r.stdout,stderr=r.stderr)
    async def native_run(self,instruction,environment,context):
        assert self._get_env('ANTHROPIC_API_KEY') is None
        await self.exec_as_agent(environment,'run-inert-model',env={
            'CLAUDE_CONFIG_DIR':'/logs/agent/sessions','ANTHROPIC_API_KEY':'METERED_SENTINEL',
            'AWS_ACCESS_KEY_ID':'METERED_SENTINEL','CLAUDE_CODE_USE_VERTEX':'1',
            'CLAUDE_CODE_OAUTH_TOKEN':'OTHER_LOGIN_SENTINEL'})
        if finish=='agent_error':raise RuntimeError('inert model error')
    monkeypatch.setattr(ClaudeCode,'run',native_run)
    monkeypatch.setattr('dradar.pier_claude.emit_worker_registered',lambda **_:None)
    monkeypatch.setenv('ANTHROPIC_API_KEY','METERED_SENTINEL')
    agent=ClaudeCodeSubscription(logs,oauth_config_file=str(source),model_name='claude-sonnet-5',
        extra_env={'ANTHROPIC_API_KEY':'METERED_SENTINEL','CLAUDE_CODE_USE_VERTEX':'1'})
    if finish=='normal':asyncio.run(agent.run('ordinary task',Environment(),None))
    else:
        with pytest.raises((RuntimeError,ValueError)):asyncio.run(agent.run('ordinary task',Environment(),None))
    assert list((logs/'sessions/projects').rglob('events.jsonl'))
    assert all(MARKER.encode() not in f.read_bytes() for f in logs.rglob('*') if f.is_file())
    assert not list(logs.rglob('.credentials.json'))
    assert MARKER not in repr(traces) and 'METERED_SENTINEL' not in repr(traces)
    remaining=list((container/'tmp').rglob('.credentials.json'))
    assert bool(remaining)==(finish=='cleanup_failure')
    assert json.loads(source.read_text())==claude_payload()
