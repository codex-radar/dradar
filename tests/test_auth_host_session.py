import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
import time
import pytest
from dradar.auth_authority import select_authority
from dradar.auth_host_session import HostAccessSession
from dradar.auth_refresh import RefreshUnavailable
import os
pytestmark = pytest.mark.skipif(os.name == 'nt', reason='POSIX host-auth implementation; Windows admission is tested separately')

KEY=b'fake-local-key'*3

def write(path, future):
    token='fake.'+base64.urlsafe_b64encode(json.dumps({'exp':time.time()+future}).encode()).decode().rstrip('=')+'.fake'
    path.write_text(json.dumps({'tokens':{'access_token':token,'refresh_token':'SECRET-RT','account_id':'fake-account'}}));path.chmod(0o600)

class Agent:
    async def exec_as_agent(self,*args,**kwargs): return SimpleNamespace(return_code=0,stdout='1000')
    async def exec_as_root(self,*args,**kwargs): return SimpleNamespace(return_code=0,stdout='')

class Environment:
    def __init__(self): self.uploads=[]
    async def upload_file(self,source,destination): self.uploads.append((source.read_bytes(),destination))

def test_authority_refresh_projection_delivery_adoption_and_request(tmp_path):
    path=tmp_path/'auth.json';write(path,-1)
    authority=select_authority('codex',[path],local_key=KEY)
    refreshed=[];events=[]
    def renew(): refreshed.append(True);write(path,3600)
    session=HostAccessSession(authority,KEY,tmp_path/'gate',renew,lambda:None,events.append)
    material=session.prepare();env=Environment()
    destination,evidence=asyncio.run(session.deliver(material,Agent(),env))
    assert len(refreshed)==1
    payload=json.loads(env.uploads[0][0]);assert set(payload)=={'access_token'}
    assert b'SECRET-RT' not in env.uploads[0][0]
    assert evidence.adopted=='unknown' and evidence.request=='unknown'
    # Fake runtime consumes the delivered file, then correlates the exact generation.
    assert payload['access_token']==material.token
    evidence.adopted_generation(material.revision)
    evidence.request_result(material.revision,accepted=True)
    assert evidence.request=='accepted'
    assert 'SECRET' not in str(events) and destination not in str(events)
    assert not list(tmp_path.glob('**/*access.json'))

def test_unverified_native_lock_refuses_before_intent_and_token_mutation(tmp_path):
    path=tmp_path/'auth.json';write(path,-1);before=path.read_bytes()
    authority=select_authority('codex',[path],local_key=KEY)
    def unsupported(): raise RefreshUnavailable('native_writer_contract_unverified')
    session=HostAccessSession(authority,KEY,tmp_path/'gate',lambda:pytest.fail('renew'),unsupported)
    with pytest.raises(RefreshUnavailable,match='native_writer_contract_unverified'): session.prepare()
    assert path.read_bytes()==before
    assert not list(tmp_path.glob('**/pending.json'))

def test_observer_failure_does_not_change_primary_result(tmp_path):
    path=tmp_path/'auth.json';write(path,3600)
    authority=select_authority('codex',[path],local_key=KEY)
    def observer(event): raise OSError('telemetry down')
    session=HostAccessSession(authority,KEY,tmp_path/'gate',lambda:pytest.fail('renew'),lambda:None,observer)
    assert session.prepare().usable()


def test_relogin_to_another_principal_never_silently_rebinds(tmp_path):
    path=tmp_path/'auth.json';write(path,3600)
    authority=select_authority('codex',[path],local_key=KEY)
    session=HostAccessSession(authority,KEY,tmp_path/'gate',lambda:None,lambda:None)
    payload=json.loads(path.read_text());payload['tokens']['account_id']='other-account'
    path.write_text(json.dumps(payload))
    with pytest.raises(RefreshUnavailable): session.prepare()


def test_delivery_rejects_substituted_token_even_with_same_revision(tmp_path):
    from dataclasses import replace
    path=tmp_path/'auth.json';write(path,3600)
    authority=select_authority('codex',[path],local_key=KEY)
    session=HostAccessSession(authority,KEY,tmp_path/'gate',lambda:None,lambda:None)
    material=session.prepare();env=Environment()
    with pytest.raises(RefreshUnavailable):asyncio.run(session.deliver(replace(material,token='fake-substituted-refresh'),Agent(),env))
    assert not env.uploads


def test_rejected_generation_renews_once_and_peer_reuses_result(tmp_path):
    path=tmp_path/'auth.json';write(path,3600)
    authority=select_authority('codex',[path],local_key=KEY)
    calls=[]
    session=HostAccessSession(authority,KEY,tmp_path/'gate',lambda:pytest.fail('ordinary renewal'),lambda:None)
    rejected=session.prepare().revision
    def renew():
        calls.append(True);write(path,7200)
    session.renew_rejected=renew
    first=session.prepare(rejected_revision=rejected)
    second=session.prepare(rejected_revision=rejected)
    assert first.revision != rejected and second==first
    assert calls==[True]


def test_rejected_generation_without_adapter_contract_does_not_write_intent(tmp_path):
    path=tmp_path/'auth.json';write(path,3600)
    authority=select_authority('codex',[path],local_key=KEY)
    session=HostAccessSession(authority,KEY,tmp_path/'gate',lambda:pytest.fail('renew'),lambda:None)
    with pytest.raises(RefreshUnavailable,match='rejected_renewal_unsupported'):
        session.prepare(rejected_revision=session.prepare().revision)
    assert not list(tmp_path.glob('**/pending.json'))
