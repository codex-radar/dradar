"""No model/process/network: exercise the real parent loop with a bounded fake."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import json
import hashlib
import pytest
from dradar.v2 import host_runtime as host
from dradar.v2.journal import Journal

class Clock:
    def __init__(self):self.now=0
    def monotonic(self):return self.now
    def sleep(self,_):self.now+=100

class Controller:
    def __init__(self,folder,*,terminal=False,exit_proved=True):
        self.folder=folder;self.terminal=terminal;self.exit_proved=exit_proved
        self.calls=[];self.polls=0;self.max_polls=10;self.probe_guard_fired=False;self.closed=False
    def request(self,p):
        self.calls.append(p)
        if p['op']=='rpc':return {'result':{'turn':{'id':'one-turn'}}}
        if p['op']=='events':return {'events':[]}
        if p['op']=='status':
            self.polls+=1
            if self.polls>self.max_polls:
                self.probe_guard_fired=True
                raise RuntimeError('test-only bounded observation guard')
            return {'last_turn_status':'completed' if self.terminal else 'running'}
        if p['op']=='collect':
            collected=self.folder/'collected';collected.mkdir()
            data=b'';digest=hashlib.sha256(data).hexdigest()
            result={'public_inputs_unchanged':True,'missing_deliverables':[],
              'public_inputs_sha256':{'/app/input':'a'*64},'outputs':{'answer':{'sha256':digest,'bytes':0}},
              'unexpected_workspace_changes':[],'patch_sha256':digest}
            for name,content in [('model.patch',data),('COLLECTION.json',json.dumps(result).encode())]:
                p=collected/name;p.write_bytes(content);p.chmod(0o600)
            return result
        return {}
    def close(self):
        self.closed=True
        return {k:self.exit_proved for k in ('native_app_server_reaped','created_containers_absent','owned_container_names_absent')}

@pytest.fixture
def setup(tmp_path,monkeypatch):
    def make(*,local=False,terminal=False,exit_proved=True):
        clock=Clock();monkeypatch.setattr(host,'time',clock)
        folder=tmp_path/'host';folder.mkdir();task=tmp_path/'task';task.mkdir();(task/'instruction.md').write_text('synthetic only')
        controller=Controller(folder,terminal=terminal,exit_proved=exit_proved)
        journal=Journal(tmp_path/'journal')
        if local:journal.bind('local_interrupt','true')
        runtime=object.__new__(host.HostCodexRuntime);runtime.controllers={'assignment':controller};runtime.journal=journal;runtime.tasks_root=tmp_path
        prepared={'assignment_id':'assignment','host_folder':str(folder),'task_id':'task','effort':'low','agent_timeout_sec':1,
          'host_preflight':{'no_model_thread_preflight':{'thread_id':'thread'}}}
        return runtime,prepared,controller,clock
    return make

@pytest.mark.parametrize('local',[False,True])
def test_parent_deadline_closes_ack_only_controller_without_second_start(setup,local):
    runtime,p,c,clock=setup(local=local);barriers=[]
    result=runtime.execute_with_barrier(p,'execution',lambda:barriers.append(True),nullcontext)
    assert not c.probe_guard_fired, 'parent waited indefinitely until the synthetic observation guard'
    assert c.closed and c.polls<=2 and barriers==[True]
    assert sum(x['op']=='authorize-one-turn' for x in c.calls)==1
    assert sum(x.get('method')=='turn/start' for x in c.calls)==1
    assert result.outcome in ('failed','interrupted') and result.exit_confirmed
    assert result.outcome!='completed' and not result.files


def test_unproved_exit_still_blocks_completion(setup):
    runtime,p,c,clock=setup(exit_proved=False)
    with pytest.raises(host.RuntimeUnavailable,match='retain fence'):
        runtime.execute_with_barrier(p,'execution',lambda:None,nullcontext)
    assert c.closed
    assert not c.probe_guard_fired


def test_normal_terminal_retains_collection(setup):
    runtime,p,c,clock=setup(terminal=True)
    result=runtime.execute_with_barrier(p,'execution',lambda:None,nullcontext)
    assert result.outcome=='completed' and result.exit_confirmed and c.polls==1
    assert set(result.files)=={'patch','runner_result','trajectory'}
    assert result.tokens['total'] is None

@pytest.mark.parametrize('missing',['native_app_server_reaped','created_containers_absent','owned_container_names_absent'])
def test_each_exit_proof_is_required_and_real_fence_retained(setup,missing):
    runtime,p,c,clock=setup();assert runtime.journal.begin_execution('assignment','execution')
    before=runtime.journal.execution('assignment')
    c.close=lambda:{k:k!=missing for k in ('native_app_server_reaped','created_containers_absent','owned_container_names_absent')}
    with pytest.raises(host.RuntimeUnavailable,match='retain fence'):
        runtime.execute_with_barrier(p,'execution',lambda:None,nullcontext)
    assert runtime.journal.execution('assignment')==before
    assert not runtime.journal.begin_execution('assignment','execution')

@pytest.mark.parametrize('interrupt_at,expected_max',[(0,60.3),(65,75.4)])
def test_interrupt_ack_time_counts_and_cannot_extend_original_deadline(setup,interrupt_at,expected_max):
    runtime,p,c,clock=setup();p['agent_timeout_sec']=10;c.max_polls=1000
    clock.sleep=lambda amount:setattr(clock,'now',clock.now+amount)
    runtime.journal.value=lambda key:'true' if key=='local_interrupt' and clock.now>=interrupt_at else None
    request=c.request
    def delayed(value):
        if value.get('method')=='turn/interrupt':clock.now+=10
        return request(value)
    c.request=delayed
    result=runtime.execute_with_barrier(p,'execution',lambda:None,nullcontext)
    assert not c.probe_guard_fired and clock.now<=expected_max
    assert result.outcome=='interrupted'
    assert sum(x.get('method')=='turn/interrupt' for x in c.calls)==1


def test_lost_interrupt_ack_retains_local_intent(setup):
    runtime,p,c,clock=setup(local=True);request=c.request
    def lost(value):
        if value.get('method')=='turn/interrupt':raise RuntimeError('synthetic lost ACK')
        return request(value)
    c.request=lost
    result=runtime.execute_with_barrier(p,'execution',lambda:None,nullcontext)
    assert result.outcome=='interrupted' and c.closed


def test_prepare_uses_fixed_task_timeout_instead_of_display_estimate(tmp_path,monkeypatch):
    runtime=object.__new__(host.HostCodexRuntime);runtime.journal=Journal(tmp_path/'journal');runtime.controllers={}
    collector=tmp_path/'collector';collector.write_bytes(b'synthetic');collector.chmod(0o600)
    task={'benchmark':'tb4','task_id':'task','task_content_hash':'a'*64,'collector_path':str(collector),
      'collector_sha256':hashlib.sha256(b'synthetic').hexdigest(),'agent_timeout_sec':37,'official_task_timeout_sec':999}
    runtime.binding={'schema':'synthetic-nonmixed','host':{},'tasks':[task],'runtime_config_version':'synthetic'}
    prepared={'assignment_id':'assignment','benchmark_id':'tb4','task_id':'task','task_content_hash':'a'*64,'effort':'low',
      'agent':'codex','agent_version':host.VERSION,'agent_version_verified':True,'auth_runtime':host.AUTH_RUNTIME,
      'provider':host.PROVIDER,'billing_mode':host.BILLING_MODE,'model':host.MODEL}
    monkeypatch.setattr(host.CodexRuntime,'prepare',lambda *a,**kw:prepared)
    captured=[]
    def factory(cfg,folder):
        captured.append(cfg)
        return SimpleNamespace(start=lambda:{},request=lambda _: {'account_type':'chatgpt'},close=lambda:{})
    runtime.controller_factory=factory
    runner=dict.fromkeys(('agent','agent_version','agent_version_verified','auth_runtime','provider','billing_mode','est_minutes'))
    runner['est_minutes']=999
    result=runtime.prepare({'runner':runner})
    assert result['agent_timeout_sec']==captured[0]['agent_timeout_sec']==37
