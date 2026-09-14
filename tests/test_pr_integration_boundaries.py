"""Controlled fixtures: no native agent, account, signal or Docker operation."""
import ast
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
from dradar import image_cache, runner


def adapter_tree():
    return ast.parse(Path(runner.__file__).with_name('pier_antigravity.py').read_text())


def breaker(tmp_path):
    node = next(n for n in adapter_tree().body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'ANTIGRAVITY_LOOP_BREAKER_SCRIPT' for t in n.targets))
    namespace = {'__name__': 'fixture'}
    exec(ast.literal_eval(node.value), namespace)
    hits = []
    instance = namespace['LoopBreaker'](on_break=lambda *a: hits.append(a), stderr_stream=io.StringIO(), marker_path=tmp_path/'marker')
    return instance, hits


def event(index, parameters=None, tool='view_file'):
    return json.dumps({'event': 'step_update', 'step_update': {'state': 'ACTIVE', 'step_index': index, 'tool_name': tool, 'tool_info': {'parameters': parameters}}})


def test_repeated_active_is_one_step(tmp_path):
    b, hits = breaker(tmp_path)
    for _ in range(20):
        b.process_line(event(7, {'path': '/app/a'}))
    assert not hits
    assert b.repeat_count == 1
    for i in range(8, 12):
        b.process_line(event(i, {'path': '/app/a'}))
    assert len(hits) == 1


def test_incomplete_updates_do_not_count(tmp_path):
    b, hits = breaker(tmp_path)
    for i in range(20):
        b.process_line(event(i))
        b.process_line(event(None, {'path': '/app/a'}))
    assert not hits
    assert b.repeat_count == 0


def test_write_resets_read_repeat(tmp_path):
    b, hits = breaker(tmp_path)
    for i in range(4):
        b.process_line(event(i, {}))
    b.process_line(event(4, {}, 'replace_file_content'))
    for i in range(5, 9):
        b.process_line(event(i, {}))
    assert not hits


@pytest.mark.parametrize('producer,filter_status,marker,expected', [(0,0,False,0),(130,0,False,130),(130,0,True,130),(1,0,True,1),(0,7,True,7),(0,0,True,1)])
def test_pipeline_preserves_failures(tmp_path, producer, filter_status, marker, expected):
    node = next(n.value for n in ast.walk(adapter_tree()) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id=='pipeline_cmd' for t in n.targets))
    trigger = tmp_path/'trigger'
    watcher = tmp_path/'filter.py'
    watcher.write_text('import sys\nfrom pathlib import Path\nsys.stdout.write(sys.stdin.read())\n'+(f'Path({str(trigger)!r}).touch()\n' if marker else '')+f'sys.exit({filter_status})\n')
    values = {'breaker_setup': f'dradar_breaker={shlex.quote(str(watcher))}; ', 'command': f"bash -c 'exit {producer}'", 'stderr': str(tmp_path/'stderr'), 'stream': str(tmp_path if producer == 0 and expected == 1 else tmp_path/'stream'), 'shlex': shlex}
    shell = eval(compile(ast.Expression(node), '<actual-pipeline>', 'eval'), values).replace('cd /app', 'cd '+shlex.quote(str(tmp_path))).replace('/tmp/dradar-loop-breaker-triggered', str(trigger)).replace('python3 -u', shlex.quote(Path(sys.executable).as_posix())+' -u')
    result = subprocess.run(['bash', '-o', 'pipefail', '-c', shell], capture_output=True)
    assert result.returncode == expected, result.stderr


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
