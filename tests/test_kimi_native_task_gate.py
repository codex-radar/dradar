"""Independent QA: real installed Pier class; only Docker/process IO is faked.
Run alone in the actual Pier venv; legacy modules may install Pier stubs.
"""
import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from pier.environments.docker.docker import DockerEnvironment
from pier.environments.base import ExecResult
from pier.models.task.config import EnvironmentConfig
from pier.models.trial.paths import TrialPaths
from dradar import pier_shared_oauth_docker as gate_module


def fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(gate_module,'_require_local_shared_daemon',lambda:None)
    (tmp_path/'Dockerfile').write_text('FROM scratch\n')
    mounts=[]
    for name in ('credentials','oauth'):
        p=tmp_path/name;p.mkdir(mode=0o700)
        mounts.append({'type':'bind','source':str(p.resolve()),'target':'/tmp/dradar-kimi-home/'+name})
    env=gate_module.SharedOAuthDockerEnvironment(environment_dir=tmp_path,environment_name='qa',session_id='fixture',trial_paths=TrialPaths(tmp_path/'trial'),task_env_config=EnvironmentConfig(),shared_oauth_mounts_json=mounts)
    assert isinstance(env,DockerEnvironment)
    assert DockerEnvironment in type(env).__mro__
    cid='a'*64
    container={'Id':cid,'Image':'sha256:'+'b'*64,'State':{'Running':True},'Config':{'Env':[]},'Mounts':[{'Type':'bind','Source':m['source'],'Destination':m['target'],'RW':True} for m in mounts]}
    native={'arch':'x86_64','cli':'c'*64,'config':'d'*64,'auth_regular':True,'lock_regular':True,'home':'/tmp/dradar-kimi-user','kimi_home':'/tmp/dradar-kimi-home','lock_override':False}
    calls=[];proofs=[]
    async def compose(*args,**kw):return ExecResult(stdout=cid+'\n',return_code=0)
    async def docker(args,timeout=30):
        calls.append(args)
        if args[0]=='inspect':return ExecResult(stdout=json.dumps([container]),return_code=0)
        if '/usr/bin/python3' in args:return ExecResult(stdout=json.dumps(native),return_code=0)
        return ExecResult(stdout='synthetic model boundary',return_code=0)
    async def record(proof):proofs.append(proof)
    monkeypatch.setattr(env,'_run_docker_compose_command',compose)
    monkeypatch.setattr(env,'_native_docker',docker)
    kw=dict(command='fixture-model-command',env={'HOME':'/tmp/dradar-kimi-user','KIMI_CODE_HOME':'/tmp/dradar-kimi-home'},context={'assignment_id':'e'*32,'task_id':'fixture','attempt_id':'attempt1'},config_sha256='d'*64,cli_hashes={'x86_64':'c'*64},on_verified=record)
    return env,kw,container,native,calls,proofs

@pytest.mark.parametrize('kind',['image_lock','extra_lock','mount','shadow','cli','config','home','callback'])
def test_rejection_never_dispatches_model(tmp_path,monkeypatch,kind):
    env,kw,c,n,calls,proofs=fixture(tmp_path,monkeypatch)
    if kind=='image_lock':c['Config']['Env']=['KIMI_DISABLE_OAUTH_LOCK=0']
    if kind=='extra_lock':kw['env']['KIMI_DISABLE_OAUTH_LOCK']=''
    if kind=='mount':c['Mounts'][0]['RW']=False
    if kind=='shadow':c['Mounts'].append({'Destination':'/tmp/dradar-kimi-home/oauth/kimi-code'})
    if kind=='cli':n['cli']='other'
    if kind=='config':n['config']='other'
    if kind=='home':kw['env']['HOME']='/wrong'
    if kind=='callback':
        async def reject(proof):raise OSError('cannot persist evidence')
        kw['on_verified']=reject
    with pytest.raises((ValueError,OSError)):asyncio.run(env.exec_kimi_model(**kw))
    assert not any('/bin/bash' in a for a in calls)


def test_real_pier_merge_initial_and_resume_each_inspect(tmp_path,monkeypatch):
    env,kw,c,n,calls,proofs=fixture(tmp_path,monkeypatch)
    env._persistent_env={'QA_PERSISTENT':'kept'}
    asyncio.run(env.exec_kimi_model(**kw))
    kw['command']='fixture-resume-command'
    asyncio.run(env.exec_kimi_model(**kw))
    assert sum(a[0]=='inspect' for a in calls)==2
    models=[a for a in calls if '/bin/bash' in a]
    assert len(models)==2 and all('QA_PERSISTENT=kept' in a for a in models)
    assert all('a'*64 in a for a in models)
    assert len(proofs)==2 and proofs[0]['dispatch_nonce']!=proofs[1]['dispatch_nonce']
    c['Config']['Env']=['KIMI_DISABLE_OAUTH_LOCK=true']
    with pytest.raises(ValueError):asyncio.run(env.exec_kimi_model(**kw))
    assert len([a for a in calls if '/bin/bash' in a])==2


def test_cancel_preserves_cancellation_and_reaps_process(tmp_path,monkeypatch):
    env,*_=fixture(tmp_path,monkeypatch)
    class Process:
        returncode=None
        pid=999999
        reaped=False
        async def communicate(self):
            if self.returncode is None:raise asyncio.CancelledError()
            self.reaped=True
            return (b'',None)
        def terminate(self):self.returncode=-15
        def kill(self):self.returncode=-9
        async def wait(self):self.reaped=True;return self.returncode
    process=Process()
    monkeypatch.setattr(gate_module.asyncio,'create_subprocess_exec',AsyncMock(return_value=process))
    with pytest.raises(asyncio.CancelledError):asyncio.run(gate_module.SharedOAuthDockerEnvironment._native_docker(env,['exec','fixture']))
    assert process.reaped

@pytest.mark.parametrize('kind',['ps_error','short_id','short_image','identity','missing_context'])
def test_identity_rejection_zero_dispatch(tmp_path,monkeypatch,kind):
    env,kw,c,n,calls,proofs=fixture(tmp_path,monkeypatch)
    if kind in ('ps_error','short_id'):
        async def compose(*args,**kw):return ExecResult(stdout='a'* (12 if kind=='short_id' else 64),return_code=1 if kind=='ps_error' else 0)
        monkeypatch.setattr(env,'_run_docker_compose_command',compose)
    if kind=='short_image':c['Image']='sha256:abc'
    if kind=='identity':c['Id']='b'*64
    if kind=='missing_context':kw['context'].pop('assignment_id')
    with pytest.raises(ValueError):asyncio.run(env.exec_kimi_model(**kw))
    assert not any('/bin/bash' in a for a in calls)
    assert not proofs


def test_kimi_run_initial_and_resume_route_through_same_gate():
    """Source-wiring assertion, not an end-to-end Kimi.run claim."""
    import ast
    tree=ast.parse((Path(__file__).parents[1]/'src/dradar/pier_kimi.py').read_text())
    cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='KimiCode')
    run=next(x for x in cls.body if isinstance(x,ast.AsyncFunctionDef) and x.name=='run')
    nested={x.name:x for x in run.body if isinstance(x,ast.AsyncFunctionDef)}
    for name in ('run_initial','run_resume'):
        calls=[x.func.id for x in ast.walk(nested[name]) if isinstance(x,ast.Call) and isinstance(x.func,ast.Name)]
        assert 'execute_model' in calls
    assert any(isinstance(x,ast.Call) and isinstance(x.func,ast.Name) and x.func.id=='gate' for x in ast.walk(nested['execute_model']))


def test_actual_kimi_run_initial_resume_exit_code_contract(tmp_path,monkeypatch):
    """Real Kimi/Pier constructors and run callbacks, synthetic Docker boundary."""
    import importlib
    import sys
    import dradar.pier_runtime_safety as safety
    import dradar.kimi_recovery as recovery
    monkeypatch.setitem(sys.modules,'_dradar_pier_runtime_safety',safety)
    monkeypatch.setitem(sys.modules,'_dradar_kimi_recovery',recovery)
    kimi=importlib.import_module('dradar.pier_kimi')
    env,kw,c,n,calls,proofs=fixture(tmp_path,monkeypatch)
    auth=tmp_path/'synthetic-auth.json';auth.write_text(json.dumps({'access_token':'test-access-only','refresh_token':'test-refresh-only'}))
    cli=tmp_path/'synthetic-kimi';cli.write_text('fixture')
    logs=tmp_path/'agent';logs.mkdir()
    agent=kimi.KimiCode(logs_dir=logs,model_name='kimi-k2.8-preview',auth_json_file=str(auth),kimi_cli_file=str(cli),reasoning_effort='low',shared_oauth=True,task_execution_context_json=kw['context'])
    monkeypatch.setattr(kimi,'verify_task_baseline',AsyncMock())
    monkeypatch.setattr(kimi,'register_worker',AsyncMock())
    monkeypatch.setattr(env,'upload_file',AsyncMock())
    monkeypatch.setattr(env,'exec',AsyncMock(return_value=ExecResult(stdout='',return_code=0)))
    observed=[]
    async def model(**kwargs):
        observed.append(kwargs)
        return ExecResult(stdout='',return_code=313)
    monkeypatch.setattr(env,'exec_kimi_model',model)
    async def drive(**callbacks):
        for name,args in [('run_initial',()),('run_resume',('session-fixture','continue'))]:
            with pytest.raises(Exception) as error:await callbacks[name](*args)
            assert recovery.pier_exit_code(error.value)==313
        return (1,'session-fixture')
    monkeypatch.setattr(kimi,'run_with_kimi_resume',drive)
    asyncio.run(agent.run('synthetic instruction',env,None))
    assert len(observed)==2
    assert '--session' not in observed[0]['command'] and '--session' in observed[1]['command']
    assert all(item['context']==kw['context'] for item in observed)
