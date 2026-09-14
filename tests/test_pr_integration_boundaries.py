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
