"""Exercise the real Node control driver against a local fake stdio peer."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / 'src/dradar/codex_managed_bridge.cjs'
pytestmark = pytest.mark.skipif(not shutil.which('node'), reason='Node required')


def private(path, value):
    temporary = path.with_name('.' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(path)


def until(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.025)
    raise AssertionError('fixture condition timed out')


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / 'control'
    root.mkdir(mode=0o700)
    executable = tmp_path / 'codex'
    executable.write_text('''#!/usr/bin/env node
const fs=require('fs'), readline=require('readline');
const root=process.env.FIXTURE_ROOT;
readline.createInterface({input:process.stdin}).on('line', raw=>{
 const m=JSON.parse(raw);
 fs.appendFileSync(root+'/methods',m.method+'\\n');
 let result={};
 if(m.method==='account/login/start') result={type:'chatgptAuthTokens'};
 if(m.method==='thread/start') result={thread:{id:'thread-1'}};
 if(m.method==='turn/start') result={turn:{id:'turn-1'}};
 if(m.id!==undefined) process.stdout.write(JSON.stringify({id:m.id,result})+'\\n');
});
''')
    executable.chmod(0o700)
    generation = uuid.uuid4().hex
    private(root/'request.json', dict(schema='dradar.managed_run.v1', instruction='fixture', model='fixture', effort='ultra'))
    private(root/'current.json', dict(generation=generation))
    private(root/f'at-{generation}.json', dict(generation=generation, access_token='fake.at.one', account_id='fake-account', expires_at=time.time()+3600))
    private(root/'heartbeat.json', dict(sequence=uuid.uuid4().hex))
    env = {**os.environ, 'PATH': str(tmp_path)+os.pathsep+os.environ['PATH'], 'FIXTURE_ROOT':str(root)}
    proc = subprocess.Popen(['node', str(BRIDGE), str(root/'request.json')], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        yield root, generation, proc
    finally:
        if proc.poll() is None:
            proc.terminate()
        stdout, stderr = proc.communicate(timeout=4)
        assert b'fake.at.' not in stdout + stderr


def methods(root):
    path=root/'methods'
    return path.read_text().splitlines() if path.exists() else []


def test_no_model_before_host_permit_and_generation_adoption(runtime):
    root, generation, proc = runtime
    until(lambda: (root/'status.json').exists())
    assert 'thread/start' not in methods(root)
    assert 'turn/start' not in methods(root)
    private(root/'start.json', dict(schema='dradar.managed_start.v1', generation=generation))
    until(lambda: 'turn/start' in methods(root))
    next_generation=uuid.uuid4().hex
    private(root/f'at-{next_generation}.json', dict(generation=next_generation, access_token='fake.at.two', account_id='fake-account', expires_at=time.time()+3600))
    private(root/'current.json', dict(generation=next_generation))
    until(lambda: json.loads((root/'status.json').read_text())['generation']==next_generation)
    assert methods(root).count('account/login/start')==2
    private(root/'stop.json', {})
    proc.wait(timeout=4)
    assert proc.returncode==1
    assert 'turn/interrupt' in methods(root)


def test_wrong_generation_permit_fails_before_model(runtime):
    root, _, proc = runtime
    private(root/'start.json', dict(schema='dradar.managed_start.v1', generation=uuid.uuid4().hex))
    proc.wait(timeout=4)
    assert proc.returncode==1
    assert 'turn/start' not in methods(root)


def test_host_disappears_interrupts_active_turn(runtime):
    root, generation, proc = runtime
    private(root/'start.json', dict(schema='dradar.managed_start.v1', generation=generation))
    until(lambda: 'turn/start' in methods(root))
    (root/'heartbeat.json').unlink()
    proc.wait(timeout=4)
    assert proc.returncode==1
    assert 'turn/interrupt' in methods(root)
