from types import SimpleNamespace
import pytest


def test_windows_host_gate_refuses_before_files_or_callbacks(tmp_path,monkeypatch):
    from dradar import auth_refresh as module
    monkeypatch.setattr(module,'os',SimpleNamespace(name='nt'))
    with pytest.raises(module.RefreshUnavailable,match='durability_unverified'):
        module.HostRefreshGate(tmp_path,'a'*32).ensure(lambda:pytest.fail('read'),lambda:pytest.fail('refresh'))
    assert not list(tmp_path.iterdir())


def test_windows_rpc_refuses_before_process_launch(tmp_path,monkeypatch):
    from dradar import auth_codex_rpc as module
    monkeypatch.setattr(module,'os',SimpleNamespace(name='nt'))
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**k:pytest.fail('launch'))
    with pytest.raises(module.AccountRpcError,match='runtime_transport_unverified'):
        module.CodexAccountRpc(tmp_path/'fake','0'*64).account_read(tmp_path,refresh=True)
