import json,time,base64
from pathlib import Path
import pytest
from dradar.auth_authority import select_authority
from dradar.auth_transaction import refresh_codex_staged
from dradar.auth_refresh import RefreshUnavailable
import os
pytestmark = pytest.mark.skipif(os.name == 'nt', reason='POSIX host-auth implementation; Windows admission is tested separately')

KEY=b'fixture-key'*4

def data(exp, refresh='old-fixture', account='fixture-account'):
    token='fake.'+base64.urlsafe_b64encode(json.dumps({'exp':exp}).encode()).decode().rstrip('=')+'.fake'
    return json.dumps({'tokens':{'access_token':token,'refresh_token':refresh,'account_id':account}}).encode()

def source(tmp):
    path=tmp/'source.json';path.write_bytes(data(1));path.chmod(0o600)
    return select_authority('codex',[path],local_key=KEY)

class Rpc:
    def __init__(self, action):self.action=action
    def account_read(self, home, **kwargs):self.action(home/'auth.json');return 'chatgpt'

def test_failed_native_process_retains_partial_rotation_and_original(tmp_path):
    authority=source(tmp_path);before=authority.read();stage=tmp_path/'stage'
    def partial(path):path.write_bytes(b'{"rotated":"fixture');raise RuntimeError('native failure')
    with pytest.raises(RuntimeError):refresh_codex_staged(authority,KEY,stage,Rpc(partial))
    assert authority.read()==before
    assert (next(stage.iterdir())/'candidate-home'/'auth.json').read_bytes()==b'{"rotated":"fixture'
    with pytest.raises(RefreshUnavailable,match='recovery_required'):
        refresh_codex_staged(authority,KEY,stage,Rpc(lambda _:pytest.fail('retry')))

def test_publish_failure_retains_complete_new_candidate(tmp_path,monkeypatch):
    from dradar import auth_transaction as module
    authority=source(tmp_path);before=authority.read();stage=tmp_path/'stage'
    new=data(time.time()+3600,'new-fixture')
    original=module.atomic_private_credential
    def fail(path,content):
        if path==authority.path:raise OSError('publish failure')
        original(path,content)
    monkeypatch.setattr(module,'atomic_private_credential',fail)
    with pytest.raises(OSError):refresh_codex_staged(authority,KEY,stage,Rpc(lambda path:path.write_bytes(new)))
    assert authority.read()==before
    assert (next(stage.iterdir())/'validated-candidate.json').read_bytes()==new

def test_external_authority_change_never_overwritten(tmp_path):
    authority=source(tmp_path);stage=tmp_path/'stage';external=data(time.time()+3600,'external-fixture')
    def rotate(path):
        path.write_bytes(data(time.time()+3600,'new-fixture'))
        authority.path.write_bytes(external)
    with pytest.raises(RefreshUnavailable,match='authority_changed'):refresh_codex_staged(authority,KEY,stage,Rpc(rotate))
    assert authority.read()==external

def test_success_publishes_new_state_without_rolling_back(tmp_path):
    authority=source(tmp_path);stage=tmp_path/'stage';new=data(time.time()+3600,'new-fixture')
    refresh_codex_staged(authority,KEY,stage,Rpc(lambda path:path.write_bytes(new)))
    assert authority.read()==new
    assert (next(stage.iterdir())/'validated-candidate.json').read_bytes()==new


def test_later_generation_can_refresh_without_deleting_prior_evidence(tmp_path):
    authority=source(tmp_path);stage=tmp_path/'stage'
    for index in range(2):
        refresh_codex_staged(authority,KEY,stage,Rpc(lambda path:path.write_bytes(data(time.time()+3600,str(index)))))
    assert len(list(stage.iterdir()))==2


def test_forward_recovery_after_publish_failure_never_repeats_oauth(tmp_path,monkeypatch):
    from dradar import auth_transaction as module
    authority=source(tmp_path);stage=tmp_path/'stage';new=data(time.time()+3600,'new-fixture')
    original=module.atomic_private_credential
    def fail(path,content):
        if path==authority.path:raise OSError('publication failed')
        original(path,content)
    monkeypatch.setattr(module,'atomic_private_credential',fail)
    with pytest.raises(OSError):refresh_codex_staged(authority,KEY,stage,Rpc(lambda path:path.write_bytes(new)))
    before=next(stage.iterdir()).name
    monkeypatch.setattr(module,'atomic_private_credential',original)
    assert module.recover_codex_staged(authority,KEY,stage,before).usable
    assert authority.read()==new


def test_remote_rotation_then_source_cas_conflict_cannot_be_auto_recovered(tmp_path):
    from dradar.auth_transaction import recover_codex_staged
    authority=source(tmp_path);stage=tmp_path/'stage';external=data(time.time()+3600,'external-fixture')
    def rotate(path):
        path.write_bytes(data(time.time()+3600,'our-new-fixture'))
        authority.path.write_bytes(external)
    with pytest.raises(RefreshUnavailable):refresh_codex_staged(authority,KEY,stage,Rpc(rotate))
    before=next(stage.iterdir()).name
    with pytest.raises(RefreshUnavailable):recover_codex_staged(authority,KEY,stage,before)
    assert authority.read()==external
    assert (stage/before/'validated-candidate.json').exists()
    assert json.loads((stage/before/'transaction.json').read_text())['stage']=='source-conflict'


def test_unreadable_external_source_is_recorded_as_conflict(tmp_path):
    authority=source(tmp_path);stage=tmp_path/'stage'
    def rotate(path):
        path.write_bytes(data(time.time()+3600,'new-fixture'))
        authority.path.unlink()
    with pytest.raises(RefreshUnavailable):refresh_codex_staged(authority,KEY,stage,Rpc(rotate))
    transaction=json.loads((next(stage.iterdir())/'transaction.json').read_bytes())
    assert transaction['stage']=='source-conflict'


def test_source_change_after_gate_snapshot_refuses_before_native_refresh(tmp_path):
    from dradar.auth_access import project_access
    authority=source(tmp_path);before=project_access('codex',authority.read(),local_key=KEY).revision
    replacement=data(time.time()+3600,'external-fixture')
    authority.path.write_bytes(replacement)
    with pytest.raises(RefreshUnavailable,match='authority_changed'):
        refresh_codex_staged(authority,KEY,tmp_path/'stage',Rpc(lambda _:pytest.fail('native refresh')),expected_revision=before)
    assert authority.read()==replacement
    assert not (tmp_path/'stage').exists()


@pytest.mark.parametrize('change', ['overwrite', 'replace', 'missing', 'read-error'])
def test_prepared_source_change_refuses_native_and_keeps_pending(tmp_path, monkeypatch, change):
    """Regression for independent QA P1: staging is not the final source check."""
    from dradar import auth_transaction as module
    from dradar.auth_access import project_access
    from dradar.auth_refresh import HostRefreshGate, AccessState
    import os

    authority = source(tmp_path)
    original_bytes = authority.read()
    before = project_access('codex', original_bytes, local_key=KEY).revision
    external = data(time.time() + 3600, 'external-fixture')
    gate_root = tmp_path / 'gate'
    gate = HostRefreshGate(gate_root, authority.store_id)
    stage = gate_root / authority.store_id / 'native'
    real_write = module.atomic_private_credential
    real_read = type(authority).read
    armed = {'value': False}
    calls = []

    def read(selected):
        if change == 'read-error' and armed['value'] and selected.path == authority.path:
            raise PermissionError('fixture read failure')
        return real_read(selected)

    def write(path, content):
        real_write(path, content)
        if path.name == 'transaction.json' and json.loads(content)['stage'] == 'prepared':
            if change == 'overwrite':
                real_write(authority.path, external)
            elif change == 'replace':
                replacement = tmp_path / 'replacement.json'
                real_write(replacement, external)
                os.replace(replacement, authority.path)
            elif change == 'missing':
                authority.path.unlink()
            armed['value'] = True

    def native(path):
        calls.append(True)
        path.write_bytes(data(time.time() + 3600, 'our-new-fixture'))

    monkeypatch.setattr(type(authority), 'read', read)
    monkeypatch.setattr(module, 'atomic_private_credential', write)
    with pytest.raises(RefreshUnavailable):
        gate.ensure(lambda: AccessState(before, False), lambda: refresh_codex_staged(
            authority, KEY, stage, Rpc(native), expected_revision=before))
    assert calls == []
    assert (gate_root / authority.store_id / 'pending.json').exists()
    attempt = stage / before
    assert (attempt / 'candidate-home' / 'auth.json').read_bytes() == original_bytes
    assert not (attempt / 'validated-candidate.json').exists()
    assert json.loads((attempt / 'transaction.json').read_bytes())['stage'] == 'source-conflict-before-native'
    if change == 'missing':
        assert not authority.path.exists()
    else:
        expected = original_bytes if change == 'read-error' else external
        assert authority.path.read_bytes() == expected


@pytest.mark.parametrize('remaining, expected_force', [(3600, True), (120, False), (-1, False)])
def test_rejected_token_uses_pinned_native_expiry_contract(tmp_path, remaining, expected_force):
    authority=source(tmp_path)
    authority.path.write_bytes(data(time.time()+remaining))
    calls=[]
    class RejectedRpc:
        def account_read(self, home, *, refresh):
            calls.append(refresh)
            (home/'auth.json').write_bytes(data(time.time()+7200,'renewed-fixture'))
            return 'chatgpt'
    refresh_codex_staged(authority,KEY,tmp_path/'stage',RejectedRpc(),rejected=True)
    assert calls==[expected_force]
