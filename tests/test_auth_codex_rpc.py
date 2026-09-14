import hashlib
from pathlib import Path
import sys
import pytest
from dradar.auth_codex_rpc import CodexAccountRpc, AccountRpcError
import os
pytestmark = pytest.mark.skipif(os.name == 'nt', reason='POSIX host-auth implementation; Windows admission is tested separately')


def fixture_cli(tmp_path, behavior='ok'):
    script = tmp_path/'fake-codex'
    script.write_text('#!' + sys.executable + '\n' + '''import sys,json,time,os
from pathlib import Path
assert sys.argv[1:] == ['app-server']
assert 'OPENAI_API_KEY' not in os.environ
log = Path(os.environ['CODEX_HOME'])/'calls'
for line in sys.stdin:
    request=json.loads(line)
    with log.open('a') as f: f.write(json.dumps(request)+'\\n')
    if request['method']=='initialize':
        print(json.dumps({'id':1,'result':{}}),flush=True)
    if request['method']=='account/read':
        BEHAVIOR
        break
'''.replace('BEHAVIOR', {
        'ok': "print(json.dumps({'id':2,'result':{'account':{'type':'chatgpt','email':'SECRET'}}}),flush=True)",
        'error': "print(json.dumps({'id':2,'error':{'message':'SECRET'}}),flush=True)",
        'timeout': 'time.sleep(10)',
        'flood': "print('SECRET'*50000,flush=True)",
    }[behavior]))
    script.chmod(0o700)
    return CodexAccountRpc(script, hashlib.sha256(script.read_bytes()).hexdigest())


def test_only_account_rpc_and_handshake_no_quota_or_generation(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','SECRET')
    assert fixture_cli(tmp_path).account_read(tmp_path,refresh=True) == 'chatgpt'
    import json
    calls=[json.loads(x) for x in (tmp_path/'calls').read_text().splitlines()]
    assert [x['method'] for x in calls] == ['initialize','initialized','account/read']
    assert calls[-1]['params'] == {'refreshToken':True}


@pytest.mark.parametrize('behavior,code',[('error','account_rpc_failed'),('timeout','account_rpc_timeout'),('flood','account_rpc_output_limit')])
def test_bounded_failure_never_leaks_or_retries(tmp_path,behavior,code):
    rpc=fixture_cli(tmp_path,behavior)
    with pytest.raises(AccountRpcError,match=code) as e:
        rpc.account_read(tmp_path,refresh=True,timeout=3)
    assert 'SECRET' not in str(e.value)
    assert (tmp_path/'calls').read_text().count('account/read') == 1


def test_wrong_executable_pin_fails_before_launch(tmp_path):
    rpc=fixture_cli(tmp_path)
    (tmp_path/'fake-codex').write_text('changed')
    with pytest.raises(AccountRpcError,match='pin_mismatch'):
        rpc.account_read(tmp_path,refresh=True)
    assert not (tmp_path/'calls').exists()


def test_real_stdio_adapter_composes_with_host_gate_and_projection(tmp_path):
    import base64,time
    from dradar.auth_authority import select_authority
    from dradar.auth_host_session import HostAccessSession
    from dradar.auth_transaction import refresh_codex_staged
    path=tmp_path/'auth.json'
    def token(exp): return 'fake.'+base64.urlsafe_b64encode(json.dumps({'exp':exp}).encode()).decode().rstrip('=')+'.fake'
    import json
    path.write_text(json.dumps({'tokens':{'access_token':token(1),'refresh_token':'old-fake-RT','account_id':'fake-account'}}));path.chmod(0o600)
    rpc=fixture_cli(tmp_path)
    script=tmp_path/'fake-codex'
    text=script.read_text().replace("print(json.dumps({'id':2,'result':{'account':{'type':'chatgpt','email':'SECRET'}}}),flush=True)","payload=json.loads(Path('auth.json').read_text()); payload['tokens']['access_token']="+repr(token(time.time()+3600))+"; payload['tokens']['refresh_token']='new-fake-RT'; Path('auth.json').write_text(json.dumps(payload)); print(json.dumps({'id':2,'result':{'account':{'type':'chatgpt'}}}),flush=True)")
    script.write_text(text)
    rpc=CodexAccountRpc(script,hashlib.sha256(script.read_bytes()).hexdigest())
    key=b'fixture-local-key'*3
    authority=select_authority('codex',[path],local_key=key)
    session=HostAccessSession(authority,key,tmp_path/'gate',lambda:refresh_codex_staged(authority,key,tmp_path/'gate'/authority.store_id/'native',rpc),lambda:None)
    assert session.prepare().usable()
    assert json.loads(path.read_text())['tokens']['refresh_token']=='new-fake-RT'
    assert next(tmp_path.rglob('calls')).read_text().count('account/read')==1
    assert session.prepare().usable()
    assert next(tmp_path.rglob('calls')).read_text().count('account/read')==1


def test_transport_environment_preserved_without_provider_identity(monkeypatch):
    from dradar.auth_codex_rpc import _network_environment
    monkeypatch.setenv('HTTPS_PROXY','http://fixture-proxy.invalid:80')
    monkeypatch.setenv('NO_PROXY','localhost')
    monkeypatch.setenv('OPENAI_API_KEY','SECRET')
    monkeypatch.setenv('CODEX_ACCESS_TOKEN','SECRET')
    monkeypatch.setenv('CODEX_HOME','/wrong-home')
    values=_network_environment()
    assert values['HTTPS_PROXY']=='http://fixture-proxy.invalid:80'
    assert values['NO_PROXY']=='localhost'
    assert not {'OPENAI_API_KEY','CODEX_ACCESS_TOKEN','CODEX_HOME'} & set(values)
