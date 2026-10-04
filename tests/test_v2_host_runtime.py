"""Narrow independent boundary assertions; no host OAuth or real model calls."""
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import pytest
from dradar import runner
from dradar.manifest import task_content_hash
from dradar.taskpacks import MARKER
from dradar.v2.host_contract import *
from dradar.v2.host_runtime import HostCodexRuntime
from dradar.v2.journal import Journal
from dradar.v2.runtime import RuntimeUnavailable

PATCH=b'diff --git a/result.txt b/result.txt\nnew file mode 100644\n--- /dev/null\n+++ b/result.txt\n@@ -0,0 +1 @@\n+synthetic\n'

@pytest.fixture
def case(tmp_path):
    tasks=tmp_path/'tasks';task=tasks/'task';task.mkdir(parents=True)
    (task/'instruction.md').write_text('synthetic public instruction only')
    (task/'task.toml').write_text('[agent]\ntimeout_sec=7200\n')
    benchmark='pompeii16-20261003-v2';sha='a'*64
    (tasks/MARKER).write_text(json.dumps({'sha256':sha,'benchmark_id':benchmark}))
    collector=tmp_path/'public-collector.py';collector.write_text('print("fixed public collector")')
    t={'benchmark':benchmark,'policy_id':'pompeii-adjacency','task_id':'task',
       'task_content_hash':task_content_hash(tasks,'task'),'image_id':'sha256:'+'b'*64,
       'git_head':'c'*40,'cpus':2,'memory_bytes':4*1024**3,'agent_timeout_sec':7200,
       'official_task_timeout_sec':7200,'verifier_timeout_sec':600,
       'executor_cli':'/usr/local/bin/codex','executor_path':'/usr/local/bin:/usr/bin:/bin',
       'executor_home':'/home/app','public_files':{'/app/public.txt':'d'*64},
       'collector_path':str(collector),'collector_sha256':hashlib.sha256(collector.read_bytes()).hexdigest(),
       'deliverable_names':['result.txt']}
    binding={'schema':SCHEMA,'runtime_config_version':CONFIG_VERSION,'capability':CAPABILITY,
      'model':MODEL,'efforts':list(EFFORTS),'host':{'host_home':str(Path.home()/'.local/share/dradar/codex-host-home'),
      'host_cli':'/tmp/official/bin/codex','host_cli_sha256':'e'*64,
      'host_companion':'/tmp/official/bin/codex-code-mode-host','host_companion_sha256':'f'*64,
      'docker_context':'orbstack','proxy_image_id':'sha256:'+'1'*64},'tasks':[t]}
    p=tmp_path/'binding.json';p.write_text(json.dumps(binding));digest=hashlib.sha256(p.read_bytes()).hexdigest()
    a={'assignment_id':'2'*32,'owner_epoch':1,'slot_id':0,'task':{'task_id':'task','benchmark':benchmark,
       'model':MODEL,'effort':'low','task_content_hash':t['task_content_hash'],'task_commit':None,
       'task_bundle':{'url':'https://example.invalid/public.tar.gz','sha256':sha,'bytes':1,'format':'tar.gz'}},
       'runner':{'agent':'codex','agent_version':VERSION,'agent_version_verified':True,
                 'auth_runtime':AUTH_RUNTIME,'provider':PROVIDER,'billing_mode':BILLING_MODE,'est_minutes':1,
                 }}
    return tmp_path,tasks,binding,p,digest,a

class FakeController:
    instances=[];auth='chatgpt';exit_known=True;stop_after_start=False;changed=False;usage_complete=True;scratch=[]
    def __init__(self,c,folder):
        self.c=c;self.folder=folder;self.calls=[];self.turn=None;self.interrupted=False
        self.__class__.instances.append(self)
    def start(self):
        self.calls.append('environment_ready')
        return {'no_model_thread_preflight':{'thread_id':'owned-thread'}}
    def request(self,r):
        self.calls.append(r)
        op=r['op']
        if op=='account:status':return {'account_type':self.auth}
        if op=='authorize-one-turn':
            assert Journal(Path(self.c['journal_root'])).execution(self.c['assignment_id'])
            assert r['start_barrier_committed'] is True
            return {'single_turn_latched':True}
        if op=='rpc':
            if r['method']=='turn/start':
                assert any(isinstance(v,dict)and v['op']=='authorize-one-turn'for v in self.calls)
                self.turn='owned-turn';return {'result':{'turn':{'id':self.turn,'status':'inProgress'}}}
            if r['method']=='turn/interrupt':
                assert r['params']=={'threadId':'owned-thread','turnId':'owned-turn'}
                self.interrupted=True;return {'result':{}}
        if op=='events':
            if self.stop_after_start and not self.interrupted:
                Journal(Path(self.c['journal_root'])).bind('local_interrupt','true')
                return {'events':[]}
            if not self.turn:return {'events':[]}
            events=[{'method':'turn/completed','threadId':'owned-thread','turnId':self.turn,'status':'interrupted'if self.interrupted else'completed'}]
            if self.usage_complete:
                events.insert(0,{'method':'thread/tokenUsage/updated','params':{'threadId':'owned-thread','turnId':self.turn,'tokenUsage':{'total':{'inputTokens':10,'outputTokens':2,'totalTokens':12}}}})
            return {'events':events}
        if op=='status':return {'last_turn_status':'interrupted' if self.interrupted else None if self.stop_after_start else 'completed'}
        if op=='collect':
            d=self.folder/'collected';d.mkdir();(d/'model.patch').write_bytes(PATCH)
            c={'patch_sha256':hashlib.sha256(PATCH).hexdigest(),'public_inputs_unchanged':not self.changed,'unexpected_workspace_changes':list(self.scratch),'missing_deliverables':[], 'public_inputs_sha256':{'/app/public.txt':'d'*64}, 'outputs':{'result.txt':{'sha256':hashlib.sha256(b'synthetic\n').hexdigest(),'bytes':10}}}
            (d/'COLLECTION.json').write_text(json.dumps(c));return c
        raise AssertionError(r)
    def close(self):
        self.calls.append('physical_cleanup')
        return {'native_app_server_reaped':True,'created_containers_absent':self.exit_known,'shared_identity_logged_out':False}

@pytest.fixture(autouse=True)
def reset():
    FakeController.instances=[];FakeController.auth='chatgpt';FakeController.exit_known=True
    FakeController.stop_after_start=False;FakeController.changed=False;FakeController.usage_complete=True
    FakeController.scratch=[]

def runtime(case):
    root,tasks,b,p,sha,a=case
    j=Journal(root/'state')
    return HostCodexRuntime(j,tasks,host_runtime_binding=p,host_runtime_sha256=sha,controller_factory=FakeController),j,a

def execute(rt,j,a,barrier=None):
    prepared=rt.prepare(a);eid=j.identity('execution:'+a['assignment_id'])
    def allowed():assert j.begin_execution(a['assignment_id'],eid)
    @contextmanager
    def guard():yield
    return rt.execute_with_barrier(prepared,eid,barrier or allowed,guard)

def test_real_adapter_orders_environment_ack_fence_turn_collect_cleanup(case):
    rt,j,a=runtime(case);out=execute(rt,j,a);f=FakeController.instances[0]
    assert out.outcome=='completed'and out.exit_confirmed
    assert out.tokens['total']==12 and out.tokens['source']=='official_app_server_terminal_usage'
    assert list(out.files)==['patch','runner_result','trajectory']
    ops=[v.get('op')if isinstance(v,dict)else v for v in f.calls]
    assert ops.index('environment_ready')<ops.index('authorize-one-turn')<ops.index('rpc')<ops.index('collect')<ops.index('physical_cleanup')
    assert sum(isinstance(v,dict)and v.get('method')=='turn/start'for v in f.calls)==1
    assert out.completed_at.endswith('Z')

def test_safe_untracked_scratch_does_not_fail_new_completion(case):
    FakeController.scratch=['?? infer.py','?? engine/__pycache__/apply.pyc']
    rt,j,a=runtime(case);out=execute(rt,j,a)
    assert out.outcome=='completed' and out.failure is None and out.completed_at.endswith('Z')
    assert json.loads(out.files['runner_result'].read_text())['unexpected_workspace_changes']==FakeController.scratch

@pytest.mark.parametrize('scratch',[' M tracked.py',' D tracked.py','?? ../outside','?? /tmp/file','?? public.txt'])
def test_new_completion_still_rejects_unsafe_or_protected_mutation(case,scratch):
    FakeController.scratch=[scratch]
    rt,j,a=runtime(case);out=execute(rt,j,a)
    assert out.outcome=='failed' and out.exit_confirmed

def test_archive_marker_symlink_blocks_before_controller(case):
    root,tasks,_,_,_,_=case
    marker=tasks/MARKER;copy=root/'synthetic-marker.json';copy.write_bytes(marker.read_bytes())
    marker.unlink();marker.symlink_to(copy)
    rt,j,a=runtime(case)
    with pytest.raises(RuntimeUnavailable,match='regular verified'):rt.prepare(a)
    assert not FakeController.instances and not j.requests()

@pytest.mark.parametrize('effort',EFFORTS)
def test_all_existing_sol_efforts_reach_controller_without_low_override(case,effort):
    rt,j,a=runtime(case);a['task']['effort']=effort
    out=execute(rt,j,a);f=FakeController.instances[0]
    assert out.outcome=='completed'and f.c['effort']==effort
    request=next(v for v in f.calls if isinstance(v,dict)and v.get('method')=='turn/start')
    assert request['params']['effort']==effort

def test_start_barrier_rejection_never_starts_turn(case):
    rt,j,a=runtime(case)
    def rejected():raise RuntimeError('start ACK rejected')
    with pytest.raises(RuntimeError,match='ACK rejected'):execute(rt,j,a,rejected)
    assert not any(isinstance(v,dict)and v.get('method')=='turn/start' for v in FakeController.instances[0].calls)
    assert j.execution(a['assignment_id']) is None

def test_stop_interrupts_owned_turn_then_collects_and_confirms_exit(case):
    FakeController.stop_after_start=True
    rt,j,a=runtime(case);out=execute(rt,j,a)
    assert out.outcome=='interrupted'and out.exit_confirmed
    ops=FakeController.instances[0].calls
    assert any(isinstance(v,dict)and v.get('method')=='turn/interrupt'for v in ops)
    assert out.files['patch'].read_bytes()==PATCH

def test_missing_exit_proof_keeps_durable_fence_and_never_uploads(case):
    FakeController.exit_known=False
    rt,j,a=runtime(case)
    with pytest.raises(RuntimeUnavailable,match='retain fence'):execute(rt,j,a)
    assert j.execution(a['assignment_id']) is not None
    assert (j.root/'runtime'/a['assignment_id']/'host/collected/model.patch').read_bytes()==PATCH

def test_missing_auth_does_not_copy_or_launch_or_create_fence(case):
    FakeController.auth=None
    rt,j,a=runtime(case)
    with pytest.raises(RuntimeUnavailable,match='official user device'):rt.prepare(a)
    assert j.execution(a['assignment_id'])is None
    assert FakeController.instances[0].calls[-1]=='physical_cleanup'
    assert not any(isinstance(v,dict)and v.get('method')=='turn/start'for v in FakeController.instances[0].calls)

@pytest.mark.parametrize('field,value',[('agent_version','0.159.2'),('agent_version_verified',False),('auth_runtime','file-auth'),('provider','fallback'),('billing_mode','api'),('capability','old-cap'),('runtime_config_version','old-runtime')])
def test_server_descriptor_mismatch_fails_before_controller(case,field,value):
    rt,j,a=runtime(case);a['runner'][field]=value
    with pytest.raises(RuntimeUnavailable,match='Server runner'):rt.prepare(a)
    assert not FakeController.instances

def test_incomplete_usage_remains_null(case):
    FakeController.usage_complete=False
    rt,j,a=runtime(case);out=execute(rt,j,a)
    assert out.tokens=={'input':None,'output':None,'total':None,'source':None,'missing_reason':'official_terminal_usage_unavailable'}

def test_changed_public_input_is_infrastructure_failure_not_reward_zero(case):
    FakeController.changed=True
    rt,j,a=runtime(case);out=execute(rt,j,a)
    assert out.outcome=='failed' and out.failure['code']=='host_turn_failed'
    assert 'reward'not in out.files['runner_result'].read_text()

def test_runtime_binding_hash_and_frozen_pompeii_policy(case):
    _,_,b,p,sha,_=case
    assert load_binding(p,sha)['tasks'][0]['agent_timeout_sec']==7200
    with pytest.raises(ValueError,match='digest mismatch'):load_binding(p,'0'*64)
    b['tasks'][0]['agent_timeout_sec']=2400;p.write_text(json.dumps(b))
    with pytest.raises(ValueError,match='Pompeii'):load_binding(p,hashlib.sha256(p.read_bytes()).hexdigest())

def test_unknown_alias_and_model_effort_are_not_silently_mapped(case):
    _,_,b,p,sha,_=case
    b['efforts']=['low'];p.write_text(json.dumps(b))
    with pytest.raises(ValueError,match='capability/model/effort'):load_binding(p,hashlib.sha256(p.read_bytes()).hexdigest())
    assert policy_id('pompeii-maybe')=='pompeii-maybe'
    assert policy_id('pompeii16-20261003-v2')=='pompeii-adjacency'

def test_versioned_pompeii_reuses_original_prompt_and_timeout_algorithm(tmp_path,monkeypatch):
    original={'benchmark_id':'pompeii-adjacency','agent':'codex','est_minutes':5}
    versioned={**original,'benchmark_id':'pompeii16-20261003-v2'}
    assert runner._ensure_codex_submission_prompt(tmp_path/'old',original['benchmark_id']).read_bytes()==runner._ensure_codex_submission_prompt(tmp_path/'new',versioned['benchmark_id']).read_bytes()
    assert runner._effective_trial_timeout_sec(original)==runner._effective_trial_timeout_sec(versioned)
    monkeypatch.setattr(runner,'_task_agent_timeout_sec',lambda _:5400)
    assert runner._agent_timeout_multiplier(original,Path('synthetic'))==runner._agent_timeout_multiplier(versioned,Path('synthetic'))
    monkeypatch.setattr(runner,'_task_environment_build_timeout_sec',lambda _:30)
    assert runner._effective_run_timeout_sec(original,Path('synthetic'),1)==runner._effective_run_timeout_sec(versioned,Path('synthetic'),1)

def test_declared_schema_exposes_new_capability_without_changing_legacy(case,capsys):
    from dradar.v2.commands import main
    from dradar import __version__
    assert main(['schema'])==0
    value=json.loads(capsys.readouterr().out)
    assert value['host_runtime']['capability']==CAPABILITY
    assert value['host_runtime']['version']=='0.160.0'
    assert value['host_runtime']['max_parallel']==2
    assert __version__=='0.5.296'
    from dradar.gpt6 import GPT61_CODEX_VERSION
    assert GPT61_CODEX_VERSION=='0.159.2'

@pytest.mark.parametrize('stderr',[
 'error: no such object: '+'a'*64,
 'Error response from daemon: No such container: '+'a'*64,
])
def test_exact_missing_container_errors_from_actual_docker_and_standard_cli(stderr):
    assert confirmed_absence(0,b'29.4.0\n',1,stderr,'a'*64)

@pytest.mark.parametrize('daemon_code,version,inspect_code,error',[
 (1,b'',1,'error: no such object: '+'a'*64),
 (0,b'29.4.0',1,'permission denied'),
 (0,b'29.4.0',1,'cannot connect to Docker daemon'),
 (0,b'29.4.0',0,''),
 (0,b'29.4.0',1,'error: no such object: '+'b'*64),
])
def test_daemon_or_permission_error_never_proves_absence(daemon_code,version,inspect_code,error):
    assert not confirmed_absence(daemon_code,version,inspect_code,error,'a'*64)

def test_operator_approval_is_exact_single_use_and_not_session(case,monkeypatch,capsys):
    from dradar.v2.commands import main
    import dradar.v2.host_runtime as module
    root,*_=case;j=Journal(root/'operator-state');aid='3'*32
    j.begin_execution(aid,'4'*32)
    folder=j.root/'runtime'/aid/'host';folder.mkdir(parents=True)
    pending=folder/'pending-approval.json';pending.write_text(json.dumps({'id':7,'method':'item/commandExecution/requestApproval','params':{'command':'synthetic public command'}}))
    j.bind('host_approval:'+aid,str(pending))
    (folder/'controller.json').write_text(json.dumps({'control_socket':'/tmp/synthetic-never-connected.sock'}))
    calls=[]
    def fake(path,req):calls.append(req);return {'response_sent':True}
    monkeypatch.setattr(module,'control_request',fake)
    prefix=['host-approval','--state-root',str(j.root),'--assignment-id',aid,'--decision','accept']
    assert main(prefix+['--request-id','8'])==3 and not calls
    assert main(prefix+['--request-id','7'])==0
    assert calls==[{'op':'approval:respond','response':{'id':7,'result':{'decision':'accept'}}}]
    assert not pending.exists()
    assert main(prefix+['--request-id','7'])==3 and len(calls)==1
    with pytest.raises(SystemExit):main(prefix[:-1]+['acceptForSession','--request-id','7'])

@pytest.mark.parametrize('with_binding',[False,True])
def test_new_benchmark_without_full_matching_handshake_never_creates_run(case,monkeypatch,capsys,with_binding):
    import dradar.v2.commands as commands
    root,tasks,b,p,sha,a=case
    monkeypatch.setattr(commands,'runtime_config',lambda:{'server':'http://127.0.0.1:9999','token':'synthetic-test-only'})
    calls=[]
    class Client:
        def __init__(self,*args):calls.append('client')
        def offer_bound_host_runtime(self):calls.append('offer_host')
        def bootstrap(self):
            calls.append('bootstrap')
            return {'capabilities':['on-demand-v2'],'benchmarks':[{'benchmark':a['task']['benchmark'],'models':[{'model':MODEL,'effort':'low'}]}],
                    'limits':{'max_concurrency':2,'max_total_count':1}}
        def close(self):calls.append('close')
    argv=['run','--state-root',str(root/'cli-state'),'--tasks-root',str(tasks),'--benchmark',a['task']['benchmark'],
          '--model',MODEL,'--effort','low','--total-count','1','--concurrency','1']
    if with_binding:argv+=['--host-runtime-binding',str(p),'--host-runtime-sha256',sha]
    assert commands.main(argv,client_factory=Client)==3
    assert calls==(['client','offer_host','bootstrap','close'] if with_binding else ['client','bootstrap','close'])
    assert json.loads(capsys.readouterr().out)['status']=='blocked'

@pytest.mark.parametrize('code,text',[(0,'codex-cli 0.159.2\n'),(1,'codex-cli 0.160.0\n'),(0,'codex-cli 0.160.0\nunverified wrapper\n')])
def test_wrong_actual_host_binary_version_fails_closed_before_setup(code,text):
    with pytest.raises(ValueError,match='version mismatch'):validate_host_version(code,text)

def test_actual_pinned_host_version_parser():
    validate_host_version(0,'codex-cli 0.160.0\n')

def test_deepseek_model_codex_combination_is_not_added_or_activated(case):
    _,_,b,p,_,_=case
    b['model']='deepseek-v4';p.write_text(json.dumps(b))
    with pytest.raises(ValueError,match='capability/model/effort'):load_binding(p,hashlib.sha256(p.read_bytes()).hexdigest())
    assert policy_id('deepswe15-20261003-v4')=='deep-swe'
    assert 'deepseek'not in CAPABILITY

def test_server020_explicit_five_effort_mapping_and_pending_gate():
    row={'benchmark':'science-sr-pilot-20261003','required_client_capabilities':[MODEL_CAPABILITY,AUTH_RUNTIME],
         'production_claim_enabled':True,'missing_bindings':[],
         'model_effort_selections':[{'model':MODEL,'effort':e}for e in EFFORTS]}
    from dradar.v2.host_contract import SERVER_CONTRIBUTION_POLICY
    boot={'library_catalog':{'catalog_version':SERVER_CATALOG_VERSION,'collections':[row]}}
    boot['contribution_policy']=SERVER_CONTRIBUTION_POLICY.copy()
    for e in EFFORTS:assert require_server_library(boot,row['benchmark'],MODEL,e)==row
    row['production_claim_enabled']=False
    with pytest.raises(ValueError,match='pending'):require_server_library(boot,row['benchmark'],MODEL,'low')
    row['production_claim_enabled']=True
    with pytest.raises(ValueError,match='mapping'):require_server_library(boot,row['benchmark'],MODEL,'ultra')
    boot['library_catalog']['catalog_version']='unknown'
    with pytest.raises(ValueError,match='binding required'):require_server_library(boot,row['benchmark'],MODEL,'low')

def test_wire_caps_are_explicitly_offered_only_with_trusted_host_binding(tmp_path):
    from dradar.v2.client import Client
    j=Journal(tmp_path/'state');c=Client('http://127.0.0.1:9999','synthetic',j)
    try:
        assert AUTH_RUNTIME not in c.http.headers['X-DRadar-Capabilities'].split(',')
        c.offer_bound_host_runtime()
        assert c.http.headers['X-DRadar-Capabilities']==','.join(WIRE_CAPABILITIES)
    finally:c.close()

def test_missing_actual_host_dependency_blocks_before_run_and_controller(case,monkeypatch):
    import dradar.v2.host_runtime as module
    root,tasks,_,p,sha,_=case;j=Journal(root/'dependency-state')
    def unavailable(name):raise ImportError('synthetic missing dependency')
    monkeypatch.setattr(module.importlib,'import_module',unavailable)
    with pytest.raises(RuntimeUnavailable,match='actual CLI interpreter'):
        HostCodexRuntime(j,tasks,host_runtime_binding=p,host_runtime_sha256=sha)
    assert not j.requests()and not FakeController.instances


def test_server024_pin_rejects_old_pilot_and_unknown_versions():
    row={'benchmark':'science-sr-pilot-20261003','required_client_capabilities':[MODEL_CAPABILITY,AUTH_RUNTIME],
         'production_claim_enabled':True,'missing_bindings':[],
         'model_effort_selections':[{'model':MODEL,'effort':e}for e in EFFORTS]}
    for version in ['dradar-four-library-server018-20261004','unbound-future-catalog']:
        boot={'library_catalog':{'catalog_version':version,'collections':[row]}}
        with pytest.raises(ValueError,match='current64 library catalog binding'):
            require_server_library(boot,row['benchmark'],MODEL,'low')
    from dradar.v2.mixed_pool import CATALOG_VERSION
    assert SERVER_CATALOG_VERSION==CATALOG_VERSION and SERVER_CATALOG_VERSION!='dradar-four-library-server024-20261004-final68'
