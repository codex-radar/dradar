import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from dradar import worker_events as w


def gate(tmp_path, monkeypatch):
    path = tmp_path / 'permit.json'
    identity = dict(schema=w.WORKER_START_SCHEMA, nonce='a'*32,
                    session_id='session-1234', job='job-1', parent_pid=os.getpid())
    monkeypatch.setenv(w.WORKER_START_ENV, json.dumps(dict(identity, path=str(path))))
    monkeypatch.setenv(w.WORKER_EVENT_FILE_ENV, str(tmp_path/'events'))
    monkeypatch.setenv('DRADAR_RUNNER_SESSION_ID', identity['session_id'])
    return path, identity


def test_gate_waits_then_accepts_exact_permission(tmp_path, monkeypatch):
    path, identity = gate(tmp_path, monkeypatch)
    async def scenario():
        task = asyncio.create_task(w.register_worker(profile='grok'))
        await asyncio.sleep(0)
        assert not task.done()
        assert (tmp_path/'events').is_file()
        path.write_text(json.dumps(dict(identity, expires_at=time.monotonic()+1)))
        await asyncio.wait_for(task, 1)
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value', [('nonce','b'*32), ('session_id','another-session'),
    ('job','other-job'), ('parent_pid',1), ('expires_at',0), ('expires_at',float('nan'))])
def test_gate_rejects_stale_or_foreign_permission(tmp_path, monkeypatch, field, value):
    path, identity = gate(tmp_path, monkeypatch)
    permit = dict(identity, expires_at=time.monotonic()+1)
    permit[field] = value
    path.write_text(json.dumps(permit))
    with pytest.raises(RuntimeError, match='mismatch'):
        asyncio.run(w.register_worker(profile='grok'))


def test_gate_cancel_then_late_ack_does_not_continue(tmp_path, monkeypatch):
    path, identity = gate(tmp_path, monkeypatch)
    calls=[]
    async def provider():
        await w.register_worker(profile='grok')
        calls.append(True)
    async def scenario():
        task=asyncio.create_task(provider())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        path.write_text(json.dumps(dict(identity, expires_at=time.monotonic()+1)))
        await asyncio.sleep(0)
    asyncio.run(scenario())
    assert not calls


def test_gate_parent_disappearance_fails_closed(tmp_path, monkeypatch):
    gate(tmp_path, monkeypatch)
    monkeypatch.setattr(w.os,'kill',lambda *a: (_ for _ in ()).throw(ProcessLookupError()))
    with pytest.raises(RuntimeError, match='parent unavailable'):
        asyncio.run(w.register_worker())


def test_gate_timeout_and_late_permission_fail_closed(tmp_path, monkeypatch):
    gate(tmp_path, monkeypatch)
    monkeypatch.setattr(w,'WORKER_START_WAIT_SEC',0.01)
    with pytest.raises(RuntimeError, match='expired'):
        asyncio.run(w.register_worker())


def test_gate_missing_is_not_legacy_when_runner_identity_exists(tmp_path, monkeypatch):
    monkeypatch.delenv(w.WORKER_START_ENV, raising=False)
    monkeypatch.setenv('DRADAR_RUNNER_SESSION_ID','session-1234')
    with pytest.raises(RuntimeError, match='gate missing'):
        asyncio.run(w.register_worker())


@pytest.mark.parametrize('stopped',[True,False])
def test_registration_failure_preserves_cause_and_requires_stop_ack(tmp_path,monkeypatch,stopped):
    from dradar import runloop as loop
    from test_go_menu import SubmitClient, ASSIGNMENT, _args
    monkeypatch.setattr(loop,'HOME',tmp_path)
    def fail(*a,**k):
        raise loop.RunnerError('registration not confirmed',report_code='worker-registration-unacknowledged')
    monkeypatch.setattr(loop,'run_trial',fail)
    monkeypatch.setattr(loop,'_mark_stopped_quietly',lambda *a,**k:stopped)
    reports=[]
    monkeypatch.setattr(loop,'_report_failure_quietly',lambda *a,**k:reports.append(k))
    outcome=loop._run_and_submit(SubmitClient({}),ASSIGNMENT,tmp_path,_args(),'abc')
    assert outcome == ('failed' if stopped else 'cleanup-unconfirmed')
    assert reports[-1]['failure_code']=='worker-registration-unacknowledged'


@pytest.mark.parametrize('allowed',[True,False])
def test_real_grok_adapter_waits_before_native_model_command(tmp_path,monkeypatch,allowed):
    import importlib,sys
    from types import SimpleNamespace
    from dradar import grok_recovery
    monkeypatch.setitem(sys.modules,'_dradar_grok_recovery',grok_recovery)
    adapter=importlib.import_module('dradar.pier_grok')
    path,identity=gate(tmp_path,monkeypatch)
    commands=[]
    async def baseline(environment): pass
    async def execute(environment,command,**kwargs):
        commands.append(command)
        return SimpleNamespace(return_code=0,stdout='',stderr='')
    monkeypatch.setattr(adapter,'verify_task_baseline',baseline)
    obj=object.__new__(adapter.GrokBuild)
    obj._shared_oauth=True
    obj._version=None
    obj.model_name='grok-4.6'
    obj._reasoning_effort='high'
    obj.build_process_env=lambda values:dict(values)
    obj.render_instruction=lambda instruction:instruction
    obj.exec_as_agent=execute
    obj.exec_as_root=execute
    async def scenario():
        task=asyncio.create_task(obj.run('inert fixture',SimpleNamespace(default_user=None),None))
        await asyncio.sleep(0)
        assert not commands
        assert not task.done()
        if allowed:
            path.write_text(json.dumps(dict(identity,expires_at=time.monotonic()+1)))
            await asyncio.wait_for(task,1)
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(scenario())
    assert sum('--output-format streaming-messages-json' in c for c in commands)==int(allowed)


@pytest.mark.parametrize('response', [None,{}, {'ok':False}])
def test_stop_response_without_positive_ack_keeps_activity(tmp_path,monkeypatch,response):
    from dradar import runloop as loop
    from types import SimpleNamespace
    marker=tmp_path/'activity'
    marker.write_text('assignment-1')
    monkeypatch.setenv(loop._POOL_WORKER_ACTIVITY_ENV,str(marker))
    monkeypatch.setattr(loop.time,'sleep',lambda _:None)
    assert loop._mark_stopped_quietly(SimpleNamespace(mark_stopped=lambda *a,**k:response),'assignment-1') is False
    assert marker.read_text()=='assignment-1'


def test_confirmed_stop_but_marker_failure_is_not_safe_return(monkeypatch):
    from dradar import runloop as loop
    from types import SimpleNamespace
    monkeypatch.setattr(loop,'_record_worker_returned_assignment',lambda _:False)
    assert loop._mark_stopped_quietly(SimpleNamespace(mark_stopped=lambda *a,**k:{'ok':True}),'assignment-1') is False


def test_signalled_but_surviving_process_group_is_not_cleanup_proof(monkeypatch):
    from dradar import runner as r
    from types import SimpleNamespace
    ticks=iter([0,3])
    monkeypatch.setattr(r.time,'monotonic',lambda:next(ticks))
    monkeypatch.setattr(r.os,'killpg',lambda *a:None)
    with pytest.raises(r.RunnerError,match='remains'):
        r._confirm_pier_process_tree_stopped(SimpleNamespace(pid=12345,poll=lambda:0))


def test_unknown_process_tree_cannot_authorize_retry(monkeypatch):
    from dradar import runner as r
    from types import SimpleNamespace
    with pytest.raises(r.RunnerError,match='cannot be confirmed'):
        r._confirm_pier_process_tree_stopped(SimpleNamespace(pid=None))


def test_cleanup_unknown_retains_registration_failure_and_never_stops_lease(tmp_path,monkeypatch):
    from dradar import runloop as loop
    from test_go_menu import SubmitClient,ASSIGNMENT,_args
    monkeypatch.setattr(loop,'HOME',tmp_path)
    def fail(*a,**k):
        try: raise loop.RunnerError('ACK lost',report_code='worker-registration-unacknowledged')
        except loop.RunnerError as cause:
            raise loop.RunnerCleanupUnconfirmedError('tree unknown') from cause
    monkeypatch.setattr(loop,'run_trial',fail)
    monkeypatch.setattr(loop,'_mark_stopped_quietly',lambda *a,**k:pytest.fail('unsafe stop'))
    reports=[]
    monkeypatch.setattr(loop,'_report_failure_quietly',lambda *a,**k:reports.append(k))
    assert loop._run_and_submit(SubmitClient({}),ASSIGNMENT,tmp_path,_args(),'abc')=='cleanup-unconfirmed'
    assert [x['failure_code'] for x in reports]==['worker-registration-unacknowledged','cleanup-unconfirmed']


def test_transient_retry_stop_without_ack_quarantines(monkeypatch,tmp_path):
    from dradar import runloop as loop
    from test_go_menu import SubmitClient,ASSIGNMENT,_args
    monkeypatch.setattr(loop,'HOME',tmp_path)
    monkeypatch.setattr(loop,'run_trial',lambda *a,**k:(_ for _ in ()).throw(loop.RunnerError('transport')))
    monkeypatch.setattr(loop,'_retryable_zcode_network_failure',lambda *a:True)
    monkeypatch.setattr(loop,'_mark_stopped_quietly',lambda *a,**k:False)
    monkeypatch.setattr(loop,'_report_failure_quietly',lambda *a,**k:None)
    assert loop._run_and_submit(SubmitClient({}),ASSIGNMENT,tmp_path,_args(),'abc')=='cleanup-unconfirmed'


@pytest.mark.parametrize('handle,status,alive',[(42,258,True),(42,0,False),(42,0xffffffff,False),(0,258,False)])
def test_windows_liveness_uses_only_read_only_process_handle(monkeypatch,handle,status,alive):
    import ctypes
    from types import SimpleNamespace
    calls=[]
    class Function:
        def __init__(self,name,result):self.name,self.result=name,result
        def __call__(self,*args):calls.append((self.name,args));return self.result
    kernel=SimpleNamespace(OpenProcess=Function('open',handle),WaitForSingleObject=Function('wait',status),CloseHandle=Function('close',1))
    monkeypatch.setattr(ctypes,'WinDLL',lambda *a,**k:kernel,raising=False)
    monkeypatch.setattr(w.os,'kill',lambda *a:pytest.fail('Windows liveness must never signal a process'))
    assert w._windows_parent_alive(321)==alive
    assert calls[0]==('open',(0x00100000,False,321))
    assert calls[1:]==([('wait',(42,0)),('close',(42,))] if handle else [])


def test_windows_dispatch_never_calls_os_kill(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(w,'os',SimpleNamespace(name='nt',kill=lambda *a:pytest.fail('unsafe kill')))
    monkeypatch.setattr(w,'_windows_parent_alive',lambda pid:pid==321)
    assert w._parent_alive(321)
    assert not w._parent_alive(322)
