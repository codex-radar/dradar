"""Native OAuth start gates use synthetic credentials and never contact a provider."""
import json
from types import SimpleNamespace
import pytest
from dradar import credential_files as cf, providers, runloop


def config(home, expiry):
    path = providers.claude_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'claudeAiOauth': {'accessToken': 'sk-ant-oat01-inert-test',
        'refreshToken': 'inert-refresh', 'expiresAt': expiry,
        'scopes': ['user:inference'], 'subscriptionType': 'pro'}}
    path.write_text(json.dumps(payload)); path.chmod(0o600)
    return path


@pytest.mark.parametrize('delta,blocked', [(-1,True),(0,True),(1,True),(300,True),(301,False),(3600,False)])
def test_start_window(tmp_path, monkeypatch, delta, blocked):
    monkeypatch.setattr(cf.time, 'time', lambda: 1000)
    path=config(tmp_path,(1000+delta)*1000)
    before=path.read_bytes()
    assert bool(providers.claude_subscription_error(tmp_path)) is blocked
    assert path.read_bytes()==before
    assert providers.claude_subscription_error(tmp_path,check_expiry=False) is None


def test_completed_work_can_cross_expiry(tmp_path,monkeypatch):
    clock=[1000]
    monkeypatch.setattr(cf.time,'time',lambda:clock[0])
    config(tmp_path,2000000)
    with providers.claude_subscription_session(tmp_path/'session',home=tmp_path):
        clock[0]=3000
    assert 'expired' in providers.claude_subscription_error(tmp_path)


def test_claim_blocked_before_api(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('DRADAR_HOME',str(tmp_path))
    config(tmp_path,1000)
    client=SimpleNamespace(claim_assignment=lambda *a:pytest.fail('claimed expired auth'))
    assert runloop._claim_cell(client,'task',next(iter(providers.CLAUDE_MODELS)),'high') is None
    out=capsys.readouterr().out
    assert 'No automatic renewal' in out
    assert 'inert-refresh' not in out


def test_scoped_preflight_missing_and_expired(tmp_path,monkeypatch):
    monkeypatch.setenv('DRADAR_HOME',str(tmp_path))
    args=SimpleNamespace(refill_harness=providers.CLAUDE_AGENT)
    with pytest.raises(SystemExit):runloop._preflight_scoped_provider(args)
    config(tmp_path,1000)
    with pytest.raises(SystemExit,match='expired'):runloop._preflight_scoped_provider(args)
