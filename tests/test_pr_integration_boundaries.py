"""Controlled fixtures: no native agent, account, signal or Docker operation."""
import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest
from dradar import image_cache, runner


def test_shared_builder_is_not_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('DRADAR_ISOLATE_BUILDER', 'off')
    lease = image_cache.prepare_trial_builder(tmp_path, assignment_id='fixture')
    assert lease.name is None
    assert lease.isolated is False


@pytest.mark.parametrize('script', [runner.DSH_PRE_ARTIFACTS_SCRIPT, runner.ANTIGRAVITY_PRE_ARTIFACTS_SCRIPT])
def test_collector_dubious_owner_scoped_trust(tmp_path, script):
    repo = tmp_path/'repo with spaces'
    repo.mkdir()
    config = tmp_path/'isolated.gitconfig'
    env = dict(os.environ, GIT_CONFIG_GLOBAL=str(config), GIT_CONFIG_NOSYSTEM='1')
    def git(*args):
        return subprocess.run(['git', *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()
    git('init')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (repo/'a').write_text('before\n')
    git('add', '.')
    git('commit', '-m', 'base')
    base=git('rev-parse','HEAD')
    (repo/'a').write_text('after\n')
    git('commit','-am','change')
    # Git's own ownership test seam, not a claim of native mismatched-owner Docker QA.
    env['GIT_TEST_ASSUME_DIFFERENT_OWNER']='1'
    denied=subprocess.run(['git','status'],cwd=repo,env=env,capture_output=True)
    assert denied.returncode != 0
    assert b'dubious ownership' in denied.stderr
    output=tmp_path/'artifacts'
    hook=script.replace('cd /app','cd '+shlex.quote(repo.as_posix())).replace('/logs/artifacts',shlex.quote(output.as_posix())).replace('__DRADAR_BASE_COMMIT__',base)
    result=subprocess.run(['sh','-c',hook],env=env,capture_output=True)
    assert result.returncode == 0, result.stderr
    assert '+after' in (output/'model.patch').read_text()
    assert not config.exists()
    # Subsequent unconfigured commands remain untrusted.
    assert subprocess.run(['git','status'],cwd=repo,env=env,capture_output=True).returncode != 0


@pytest.mark.parametrize('method, attempts', [('GET', 4), ('HEAD', 4), ('POST', 1)])
def test_transport_retries_are_bounded_and_read_only(method, attempts):
    import httpx
    from dradar.api_client import ApiClient, ApiError
    calls=[]
    def transport(request):
        calls.append(request)
        raise httpx.ConnectError('fixture', request=request)
    client=ApiClient('https://fixture.invalid','fixture',transport=httpx.MockTransport(transport))
    sleeps=[]
    client._sleep=sleeps.append
    with pytest.raises(ApiError):
        client._request(method,'/fixture')
    assert len(calls)==attempts
    assert sleeps==([1.0,2.0,3.0] if attempts==4 else [])


def test_optional_capability_get_does_not_retry_transport():
    import httpx
    from dradar.api_client import ApiClient, ApiError
    calls=[]; sleeps=[]
    def fail(request):
        calls.append(request)
        raise httpx.ConnectError('fixture',request=request)
    client=ApiClient('https://fixture.invalid','fixture',transport=httpx.MockTransport(fail))
    client._sleep=sleeps.append
    with pytest.raises(ApiError):client.flight_event_capabilities()
    assert len(calls)==1 and sleeps==[]
    assert calls[0].extensions['timeout']['read']==1.0


def test_managed_negotiation_transport_failure_is_one_short_attempt(monkeypatch):
    import httpx
    from dradar.api_client import ApiClient, ApiError
    from dradar import managed_auth_selection
    monkeypatch.setattr(managed_auth_selection,'selection_requested',lambda:True)
    calls=[];sleeps=[]
    def fail(request):
        calls.append(request)
        raise httpx.ConnectError('fixture',request=request)
    client=ApiClient('https://fixture.invalid','fixture',transport=httpx.MockTransport(fail))
    client._sleep=sleeps.append
    with pytest.raises(ApiError):client._negotiate_managed_auth_runtime()
    assert len(calls)==1 and sleeps==[]
    assert calls[0].extensions['timeout']['read']==3.0


def test_close_optional_capability_failure_does_not_backoff(tmp_path):
    import httpx
    from dradar.api_client import ApiClient
    from dradar.telemetry import RunnerTelemetry
    from dradar.flight_recorder import FlightRecorder
    calls=[];sleeps=[]
    def respond(request):
        calls.append(request)
        if request.method=='GET':raise httpx.ConnectError('fixture',request=request)
        return httpx.Response(200,json={'acknowledged_event_ids':[],'ok':True})
    client=ApiClient('https://fixture.invalid','fixture',transport=httpx.MockTransport(respond))
    client._sleep=sleeps.append
    recorder=FlightRecorder(tmp_path,client)
    telemetry=RunnerTelemetry(client,jitter=False,home=tmp_path)
    telemetry.bind_batch('b'*32)
    recorder.record('auth_observed',component='provider',batch_id='b'*32,session_id=telemetry.session_id,attributes={'provider':'codex','auth_stage':'execution','auth_status':'confirmed'})
    telemetry.close('completed')
    gets=[r for r in calls if r.method=='GET']
    assert len(gets)==1 and sleeps==[]
    assert gets[0].url.path=='/api/v1/runner/flight-event-capabilities'
