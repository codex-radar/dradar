from argparse import Namespace
import json
import pytest
from test_auth_managed import runtime
from dradar import local_config
from dradar.managed_auth_selection import cmd_managed_auth, load_selection, selection_path
from dradar.auth_refresh import RefreshUnavailable

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(local_config,'HOME',tmp_path/'client')
    monkeypatch.delenv('DRADAR_CODEX_MANAGED_CONFIG',raising=False)


def command(action, runtime=None):
    cmd_managed_auth(Namespace(managed_auth_command=action,codex_bin=runtime))


def test_default_status_does_not_create_managed_store(capsys):
    command('status')
    assert '兼容模式' in capsys.readouterr().out
    assert not selection_path().exists()


def test_login_select_status_revoke_retains_source_then_use_native(runtime, capsys):
    command('login',runtime)
    selected=load_selection();assert selected is not None
    _,store,authority,executable=selected
    before=authority.read()
    command('status');assert 'AT 当前可用' in capsys.readouterr().out
    command('revoke')
    assert authority.read()==before
    with pytest.raises(RefreshUnavailable,match='managed_authority_revoked'):
        load_selection()
    command('status');assert '不可用' in capsys.readouterr().out
    command('use-native')
    assert load_selection() is None and authority.read()==before


def test_pending_blocks_readiness_but_recovery_command_can_enter(runtime,monkeypatch):
    command('login',runtime)
    _,store,authority,_=load_selection()
    pending=store.root/'gates'/authority.store_id/'pending.json'
    pending.parent.mkdir(parents=True,mode=0o700)
    pending.write_text('{"state":"pending"}');pending.chmod(0o600)
    with pytest.raises(RefreshUnavailable,match='recovery_required'):load_selection()
    seen=[]
    monkeypatch.setattr(type(store),'recover',lambda self,authority,executable:seen.append(authority.store_id))
    command('recover');assert seen==[authority.store_id]
    assert pending.exists()
