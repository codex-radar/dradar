import base64,hashlib,json,time
from pathlib import Path
import pytest
from dradar import auth_managed as module
from dradar.auth_managed import ManagedAuthStore
from dradar.auth_authority import select_authority
from dradar.auth_refresh import RefreshUnavailable
import os
pytestmark = pytest.mark.skipif(os.name == 'nt', reason='POSIX host-auth implementation; Windows admission is tested separately')

@pytest.fixture
def runtime(tmp_path,monkeypatch):
    path=tmp_path/'fake-official';path.write_bytes(b'fixture');path.chmod(0o700)
    monkeypatch.setattr(module,'_CODEX_PINS',{(module.platform.system(),module.platform.machine()):hashlib.sha256(path.read_bytes()).hexdigest()})
    def login(executable,home):
        assert not (home/'auth.json').exists()
        token='fake.'+base64.urlsafe_b64encode(json.dumps({'exp':time.time()+3600}).encode()).decode().rstrip('=')+'.fake'
        module.atomic_private_credential(home/'auth.json',json.dumps({'tokens':{'access_token':token,'refresh_token':'fake-new-grant','account_id':'fake-account'}}).encode())
    monkeypatch.setattr(module,'_login',login)
    return path

def test_only_newly_provisioned_home_receives_custody_receipt(tmp_path,runtime):
    store=ManagedAuthStore(tmp_path/'managed')
    authority=store.login(runtime)
    store.guard(authority,runtime)()
    assert store.session(authority,runtime).prepare().usable()
    assert 'fake-new-grant' not in (authority.path.parent/'custody.json').read_text()

def test_existing_default_credentials_cannot_be_adopted(tmp_path,runtime):
    store=ManagedAuthStore(tmp_path/'managed');created=store.login(runtime)
    external=tmp_path/'auth.json';external.write_bytes(created.read());external.chmod(0o600)
    other=select_authority('codex',[external],local_key=store._key())
    with pytest.raises(RefreshUnavailable,match='managed_custody_unverified'):store.session(other,runtime)

def test_copied_receipt_cannot_admit_copied_store(tmp_path,runtime):
    store=ManagedAuthStore(tmp_path/'managed');authority=store.login(runtime)
    copy=authority.path.parent.parent/'copied';copy.mkdir(mode=0o700)
    for name in ('auth.json','custody.json'):
        module.atomic_private_credential(copy/name,(authority.path.parent/name).read_bytes())
    copied=select_authority('codex',[copy/'auth.json'],local_key=store._key())
    with pytest.raises(RefreshUnavailable):store.guard(copied,runtime)

def test_runtime_change_and_principal_change_refuse_admission(tmp_path,runtime):
    store=ManagedAuthStore(tmp_path/'managed');authority=store.login(runtime)
    payload=json.loads(authority.read());payload['tokens']['account_id']='other-account'
    module.atomic_private_credential(authority.path,json.dumps(payload).encode())
    with pytest.raises(RefreshUnavailable):store.guard(authority,runtime)
    runtime.write_bytes(b'changed')
    with pytest.raises(RefreshUnavailable):store.guard(authority,runtime)


def test_incomplete_fresh_login_preserves_candidate_and_never_issues_receipt(tmp_path,runtime,monkeypatch):
    def incomplete(executable,home):
        module.atomic_private_credential(home/'auth.json',b'{"partial":"fake-new-grant')
        raise RefreshUnavailable('managed_login_incomplete')
    monkeypatch.setattr(module,'_login',incomplete)
    store=ManagedAuthStore(tmp_path/'managed')
    with pytest.raises(RefreshUnavailable):store.login(runtime)
    home=next((store.root/'authorities').iterdir())
    assert (home/'setup.pending').exists() and (home/'auth.json').exists()
    assert not (home/'custody.json').exists()


def test_active_session_rechecks_custody_before_reusing_valid_access(tmp_path,runtime):
    store=ManagedAuthStore(tmp_path/'managed');authority=store.login(runtime)
    session=store.session(authority,runtime)
    receipt=authority.path.parent/'custody.json'
    data=json.loads(receipt.read_bytes());data['payload']['origin']='copied-default-home'
    module.atomic_private_credential(receipt,json.dumps(data).encode())
    with pytest.raises(RefreshUnavailable):session.prepare()


def test_managed_recovery_only_moves_forward_after_publication_failure(tmp_path,runtime,monkeypatch):
    from dradar import auth_transaction,auth_codex_rpc
    store=ManagedAuthStore(tmp_path/'managed');authority=store.login(runtime)
    payload=json.loads(authority.read())
    payload['tokens']['access_token']='fake.'+base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip('=')+'.fake'
    module.atomic_private_credential(authority.path,json.dumps(payload).encode())
    calls=[]
    class Rpc:
        def __init__(self,executable,digest):self._executable=executable;self._digest=digest
        def account_read(self,home,**kwargs):
            calls.append(True)
            data=json.loads((home/'auth.json').read_bytes())
            data['tokens']['refresh_token']='fake-rotated-grant'
            data['tokens']['access_token']='fake.'+base64.urlsafe_b64encode(json.dumps({'exp':time.time()+3600}).encode()).decode().rstrip('=')+'.fake'
            module.atomic_private_credential(home/'auth.json',json.dumps(data).encode())
            return 'chatgpt'
    monkeypatch.setattr(auth_codex_rpc,'CodexAccountRpc',Rpc)
    original=auth_transaction.atomic_private_credential
    def fail(path,data):
        if path==authority.path:raise OSError('publish failure')
        original(path,data)
    monkeypatch.setattr(auth_transaction,'atomic_private_credential',fail)
    with pytest.raises(RefreshUnavailable):store.session(authority,runtime).prepare()
    pending=store.root/'gates'/authority.store_id/'pending.json'
    assert pending.exists()
    monkeypatch.setattr(auth_transaction,'atomic_private_credential',original)
    assert store.recover(authority,runtime).usable
    assert not pending.exists()
    assert store.session(authority,runtime).prepare().usable()
    assert len(calls)==1
    assert json.loads(authority.read())['tokens']['refresh_token']=='fake-rotated-grant'


def test_active_session_uses_private_pin_not_mutable_global_runtime(tmp_path,runtime):
    store=ManagedAuthStore(tmp_path/'managed');authority=store.login(runtime)
    session=store.session(authority,runtime)
    runtime.write_bytes(b'global official CLI upgraded')
    assert session.prepare().usable()
    assert session.check_session_contract.executable != runtime
    assert session.check_session_contract.executable.read_bytes()==b'fixture'


def test_native_host_factory_rejects_noop_policy_bypass(tmp_path,runtime):
    from dradar.auth_host_session import codex_host_session
    from dradar.auth_codex_rpc import CodexAccountRpc
    store=ManagedAuthStore(tmp_path/'managed');authority=store.login(runtime)
    with pytest.raises(RefreshUnavailable,match='managed_custody_unverified'):
        codex_host_session(authority,store._key(),tmp_path/'gate',CodexAccountRpc(runtime,hashlib.sha256(runtime.read_bytes()).hexdigest()),lambda:None)


def test_guard_cannot_authorize_an_unrelated_rpc_executable(tmp_path,runtime):
    from dradar.auth_host_session import codex_host_session
    from dradar.auth_codex_rpc import CodexAccountRpc
    store=ManagedAuthStore(tmp_path/'managed');authority=store.login(runtime)
    guard=store.guard(authority,store._runtime(runtime))
    with pytest.raises(RefreshUnavailable,match='managed_runtime_pin_mismatch'):
        codex_host_session(authority,store._key(),store.root/'gates',CodexAccountRpc(runtime,hashlib.sha256(runtime.read_bytes()).hexdigest()),guard)


def test_factory_cannot_split_authority_into_task_local_locks(tmp_path,runtime):
    from dradar.auth_host_session import codex_host_session
    from dradar.auth_codex_rpc import CodexAccountRpc
    store=ManagedAuthStore(tmp_path/'managed');authority=store.login(runtime)
    cached=store._runtime(runtime);guard=store.guard(authority,cached)
    with pytest.raises(RefreshUnavailable,match='managed_custody_unverified'):
        codex_host_session(authority,store._key(),tmp_path/'per-task-gates',CodexAccountRpc(cached,hashlib.sha256(cached.read_bytes()).hexdigest()),guard)
