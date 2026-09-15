"""Native OS process test: a disposable parent must survive gate polling."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


SCRIPT = r'''
import asyncio,json,os,sys,time,subprocess
from pathlib import Path
from dradar import worker_events as w
root=Path(sys.argv[2])
if sys.argv[1]=='worker':
    w.WORKER_START_WAIT_SEC=2
    try:
        asyncio.run(w.register_worker(profile='fixture'))
        result='authorized'
    except RuntimeError:
        result='blocked'
    (root/'worker-result.json').write_text(json.dumps({'result':result}))
    sys.exit(0)
identity={'schema':w.WORKER_START_SCHEMA,'nonce':'a'*32,'session_id':'native-session',
          'job':'native-job','parent_pid':os.getpid()}
env=os.environ.copy()
env.update({w.WORKER_START_ENV:json.dumps(dict(identity,path=str(root/'permit.json'))),
            w.WORKER_EVENT_FILE_ENV:str(root/'events.jsonl'),
            'DRADAR_RUNNER_SESSION_ID':'native-session'})
child=subprocess.Popen([sys.executable,__file__,'worker',str(root)],env=env)
(root/'child.json').write_text(json.dumps({'pid':child.pid}))
if sys.argv[1]=='parent-exit':
    sys.exit(0)
try:
    deadline=time.monotonic()+5
    while not (root/'events.jsonl').exists():
        assert child.poll() is None
        assert time.monotonic()<deadline
        time.sleep(.01)
    time.sleep(.2)  # allow multiple native parent-liveness queries before granting
    assert child.poll() is None
    tmp=root/'permit.tmp'
    tmp.write_text(json.dumps(dict(identity,expires_at=time.monotonic()+1)))
    tmp.replace(root/'permit.json')
    assert child.wait(timeout=5)==0
    assert json.loads((root/'worker-result.json').read_text())['result']=='authorized'
    (root/'parent-result.json').write_text(json.dumps({'survived':True,'pid':os.getpid()}))
finally:
    if child.poll() is None:
        child.kill()
        child.wait(timeout=5)
'''


@pytest.mark.parametrize('mode',['normal','parent-exit'])
def test_native_parent_liveness_and_permission(tmp_path,mode):
    script=tmp_path/'native-gate.py'
    script.write_text(SCRIPT)
    env=os.environ.copy()
    env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
    env['DRADAR_HOME']=str(tmp_path/'home')
    result=subprocess.run([sys.executable,str(script),mode,str(tmp_path)],env=env,
                          capture_output=True,text=True,timeout=10)
    assert result.returncode==0, result.stderr
    deadline=time.monotonic()+5
    while not (tmp_path/'worker-result.json').exists():
        assert time.monotonic()<deadline, 'worker did not fail closed after parent exit'
        time.sleep(.02)
    assert json.loads((tmp_path/'worker-result.json').read_text())['result']==(
        'authorized' if mode=='normal' else 'blocked')
    if mode=='normal':
        assert json.loads((tmp_path/'parent-result.json').read_text())['survived'] is True
