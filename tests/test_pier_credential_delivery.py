from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from dradar.pier_credential_delivery import (
    CredentialDeliveryError, credential_upload_environment, inject_private_files,
)

pytestmark = pytest.mark.skipif(os.name == 'nt' or shutil.which('bash') is None,
                                reason='POSIX container filesystem simulator')


class Container:
    default_user = None

    def __init__(self, root):
        self.root = root
        (root/'tmp').mkdir(parents=True)
        self.uploads = []
        self.commands = []
        self.fail_upload_at = None
        self.before_upload = None

    def mapped(self, remote):
        return self.root/remote.lstrip('/')

    async def upload_file(self, source, remote):
        if self.before_upload: self.before_upload(source, remote)
        self.uploads.append((Path(source), remote))
        if self.fail_upload_at == len(self.uploads):
            self.mapped(remote).write_bytes(b'partial')
            raise OSError('inert upload failure')
        shutil.copyfile(source, self.mapped(remote))

    def agent_process_env(self, env): return env


class Agent:
    async def exec_as_agent(self, environment, command, **kwargs):
        assert command == 'id -u'
        return SimpleNamespace(return_code=0, stdout=str(os.getuid()), stderr='')

    async def exec_as_root(self, environment, command, **kwargs):
        environment.commands.append(command)
        command = command.replace('/tmp', str(environment.root/'tmp'))
        result = subprocess.run(['bash','-c',command],capture_output=True,text=True)
        return SimpleNamespace(return_code=result.returncode, stdout=result.stdout, stderr=result.stderr)


def private(path, contents=b'credential-sentinel'):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(contents); path.chmod(0o600)
    return path


def test_private_file_batch_does_not_change_source_or_expose_content(tmp_path):
    first=private(tmp_path/'host'/'auth.json')
    second=private(tmp_path/'host'/'storage.info',b'local-state-sentinel')
    env=Container(tmp_path/'container')
    destinations=['/tmp/dradar-auth-task/auth.json','/tmp/dradar-auth-task/storage/item.info']
    asyncio.run(inject_private_files(Agent(),env,list(zip([first,second],destinations))))
    for source,destination in zip([first,second],destinations):
        assert env.mapped(destination).read_bytes()==source.read_bytes()
        assert env.mapped(destination).stat().st_mode & 0o777==0o600
        assert env.mapped(destination).parent.stat().st_mode & 0o777==0o700
    assert b'credential-sentinel' not in repr(env.commands).encode()
    assert all(path not in (first,second) and not path.exists() for path,_ in env.uploads)


@pytest.mark.parametrize('destination',[
    '/logs/agent/auth.json','/workspace/auth.json','/tmp/auth.json',
    '/tmp/dradar-auth/../logs/auth','/tmp//dradar-auth/key',
    '/tmp/dradar-auth/key\nunsafe','relative/key',
])
def test_rejects_destinations_outside_private_namespace_before_any_io(tmp_path,destination):
    source=private(tmp_path/'host'/'auth')
    env=Container(tmp_path/'container')
    with pytest.raises(CredentialDeliveryError):
        asyncio.run(inject_private_files(Agent(),env,[(source,destination)]))
    assert not env.uploads and not env.commands


@pytest.mark.parametrize('link_kind',['parent','file'])
def test_container_links_never_redirect_credentials_to_logs(tmp_path,link_kind):
    source=private(tmp_path/'host'/'auth')
    env=Container(tmp_path/'container')
    logs=env.root/'logs';logs.mkdir()
    auth_root=env.mapped('/tmp/dradar-auth-task')
    if link_kind=='parent':auth_root.symlink_to(logs,target_is_directory=True)
    else:
        auth_root.mkdir();(auth_root/'auth').symlink_to(logs/'leak')
    with pytest.raises(CredentialDeliveryError):
        asyncio.run(inject_private_files(Agent(),env,[(source,'/tmp/dradar-auth-task/auth')]))
    assert not env.uploads and not list(logs.iterdir())


def test_refuses_to_overwrite_preexisting_remote_credential(tmp_path):
    source=private(tmp_path/'host'/'auth')
    env=Container(tmp_path/'container')
    target=private(env.mapped('/tmp/dradar-auth-task/auth'),b'newer-credential')
    with pytest.raises(CredentialDeliveryError):
        asyncio.run(inject_private_files(Agent(),env,[(source,'/tmp/dradar-auth-task/auth')]))
    assert target.read_bytes()==b'newer-credential' and not env.uploads


def test_partial_transfer_is_cleaned_without_deleting_host_sources(tmp_path):
    sources=[private(tmp_path/'host'/str(i)) for i in range(2)]
    env=Container(tmp_path/'container');env.fail_upload_at=2
    remotes=['/tmp/dradar-auth-task/a','/tmp/dradar-auth-task/b']
    with pytest.raises(OSError,match='inert upload'):
        asyncio.run(inject_private_files(Agent(),env,list(zip(sources,remotes))))
    assert all(source.read_bytes()==b'credential-sentinel' for source in sources)
    assert all(not env.mapped(remote).exists() for remote in remotes)
    assert env.mapped('/tmp/dradar-auth-task').is_dir()


def test_snapshot_is_consistent_when_host_changes_after_validation(tmp_path):
    source=private(tmp_path/'host'/'auth',b'initial')
    env=Container(tmp_path/'container')
    env.before_upload=lambda *args:source.write_bytes(b'newer-host-value')
    asyncio.run(inject_private_files(Agent(),env,[(source,'/tmp/dradar-auth-task/auth')]))
    assert env.mapped('/tmp/dradar-auth-task/auth').read_bytes()==b'initial'
    assert source.read_bytes()==b'newer-host-value'


def test_source_symlink_is_rejected_before_container_operations(tmp_path):
    target=private(tmp_path/'host'/'actual')
    source=tmp_path/'host'/'link';source.symlink_to(target)
    env=Container(tmp_path/'container')
    with pytest.raises(ValueError,match='symbolic links'):
        asyncio.run(inject_private_files(Agent(),env,[(source,'/tmp/dradar-auth-task/auth')]))
    assert not env.commands and not env.uploads


def test_stock_pier_proxy_routes_only_the_selected_credential(tmp_path):
    source=private(tmp_path/'host'/'auth')
    artifact=private(tmp_path/'host'/'artifact',b'ordinary output')
    env=Container(tmp_path/'container');(env.root/'logs').mkdir()
    wrapped=credential_upload_environment(env,Agent(),[source])
    async def run():
        await wrapped.upload_file(source,'/tmp/codex-secrets/auth.json')
        await wrapped.upload_file(artifact,'/logs/artifact')
    asyncio.run(run())
    assert wrapped.default_user is env.default_user
    assert env.mapped('/tmp/codex-secrets/auth.json').read_bytes()==source.read_bytes()
    assert env.uploads[-1][0]==artifact
    assert env.mapped('/logs/artifact').read_bytes()==b'ordinary output'


def test_codex_adapter_routes_stock_auth_upload_through_private_transport(tmp_path,monkeypatch):
    from dradar import pier_codex
    source=private(tmp_path/'host'/'auth.json',b'{"tokens":"inert"}')
    env=Container(tmp_path/'container')
    async def baseline(_environment): pass
    async def run(self,instruction,environment,context):
        await environment.upload_file(source,'/tmp/codex-secrets/auth.json')
    monkeypatch.setattr(pier_codex,'verify_task_baseline',baseline)
    monkeypatch.setattr(pier_codex,'emit_worker_registered',lambda **kwargs:None)
    monkeypatch.setattr(pier_codex.Codex,'_resolve_auth_json_path',lambda self:source)
    monkeypatch.setattr(pier_codex.Codex,'run',run)
    monkeypatch.setattr(pier_codex.CodexRegistered,'exec_as_agent',Agent.exec_as_agent)
    monkeypatch.setattr(pier_codex.CodexRegistered,'exec_as_root',Agent.exec_as_root)
    asyncio.run(object.__new__(pier_codex.CodexRegistered).run('task',env,None))
    assert env.mapped('/tmp/codex-secrets/auth.json').read_bytes()==source.read_bytes()
    assert env.uploads[0][0]!=source
    assert env.mapped('/tmp/codex-secrets').stat().st_mode & 0o777==0o700


def test_generated_pier_bundle_contains_transport_and_file_validation(tmp_path):
    from dradar.runner import _ensure_codex_agent_module
    _ensure_codex_agent_module(tmp_path)
    for filename in ['_dradar_pier_credential_delivery.py','_dradar_credential_files.py']:
        path=tmp_path/filename
        assert path.is_file()
        compile(path.read_bytes(),str(path),'exec')
