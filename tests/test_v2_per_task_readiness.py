"""Selected-task delta; synthetic API, no model/live Docker/auth."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import hashlib,json,pytest
from test_v2_final68 import synthetic_binding,ready_bootstrap,assignment_value
from dradar.v2.host_contract import load_binding,selected_task_binding,PER_TASK_CAPABILITY,PER_TASK_POLICY
from dradar.v2.mixed_pool import MEMBERS,POOL,SOURCES,MEMBERS_SHA256,selection_scope,validate_selected_public_root
from dradar.v2.journal import Journal
from dradar.v2.scheduler import Controller,ExecutionBlocked,PreflightFailed
from dradar.v2.host_runtime import HostCodexRuntime
from dradar.v2.runtime import TaskNotReady,RuntimeUnavailable
from dradar.v2.client import TransportUnknown

def ident(t):return {k:t[k] for k in ['benchmark','task_id','task_content_hash']}
def write_binding(tmp_path,b):
 p=tmp_path/'binding.json';p.write_text(json.dumps(b));return p,hashlib.sha256(p.read_bytes()).hexdigest()

def test_partial_binding_and_unrelated_runtime_missing_do_not_block_selected_task(tmp_path):
 b=synthetic_binding(tmp_path);first=b['tasks'][0];other=b['tasks'][-1];other.pop('image_id');other['source_root']='/unrelated-missing'
 b['tasks']=[first,other];p,sha=write_binding(tmp_path,b);loaded=load_binding(p,sha)
 assert selected_task_binding(loaded,ident(first))==first
 with pytest.raises(ValueError):selected_task_binding(loaded,ident(other))
 b['tasks']=[first];p,sha=write_binding(tmp_path,b);assert len(load_binding(p,sha)['tasks'])==1
 with pytest.raises(ValueError):load_binding(p,'0'*64)

@pytest.mark.parametrize('field,value',[('image_id','latest'),('collector_sha256','bad'),('cpus',0),('executor_cli','relative'),('public_files',{}),('agent_timeout_sec',30000),('task_content_hash','0'*64)])
def test_selected_task_requires_original_immutable_full_resource_binding(tmp_path,field,value):
 b=synthetic_binding(tmp_path);t=b['tasks'][0];original=ident(t);t[field]=value;b['tasks']=[t]
 with pytest.raises(ValueError):selected_task_binding(b,original)

@pytest.mark.parametrize('fault',['input','marker','marker_source','symlink'])
def test_only_selected_root_is_checked_and_selected_bad_inputs_rejected(tmp_path,fault):
 from dradar.taskpacks import MARKER
 b=synthetic_binding(tmp_path);t=b['tasks'][0];base=Path(t['source_root']);base.mkdir()
 (base/MARKER).write_text(json.dumps({'benchmark_id':t['benchmark'],'sha256':SOURCES[t['benchmark']]['public_bundle']['sha256']}))
 d=base/t['task_id'];d.mkdir();(d/'instruction.md').write_text('synthetic');(d/'task.toml').write_text('synthetic')
 assert validate_selected_public_root(t)==(base,base) and not Path(b['tasks'][-1]['source_root']).exists()
 if fault=='input':(d/'instruction.md').unlink()
 elif fault=='marker':(base/MARKER).unlink()
 elif fault=='marker_source':(base/MARKER).write_text('{}')
 elif fault=='symlink':(d/'instruction.md').unlink();(d/'instruction.md').symlink_to(d/'task.toml')
 with pytest.raises((ValueError,OSError)):validate_selected_public_root(t)

def test_initialization_defers_all_public_roots_and_images_until_actual_assignment(tmp_path,monkeypatch):
 b=synthetic_binding(tmp_path);b['tasks']=b['tasks'][:1];p,sha=write_binding(tmp_path,b)
 monkeypatch.setattr('dradar.v2.host_runtime.subprocess.run',lambda *a,**k:pytest.fail('Docker before selected task'))
 r=HostCodexRuntime(Journal(tmp_path/'state'),tmp_path/'no-global-root',host_runtime_binding=p,host_runtime_sha256=sha,controller_factory=lambda *a:pytest.fail('controller launched'))
 assert r.claim_task_candidates()==[ident(b['tasks'][0])]
 a={'runner':{'agent':'codex','agent_version':'0.160.0','agent_version_verified':True,'auth_runtime':'codex-host-keyring-remote-v1','provider':'openai','billing_mode':'subscription','est_minutes':1},'task':{**ident(b['tasks'][0]),'model':'gpt-6.1-sol','effort':'low'}}
 with pytest.raises(TaskNotReady,match='task_inputs_unavailable'):r.prepare(a)
 assert not r.controllers and not (r.journal.root/'runtime').exists()

def test_versioned_policy_avoids_unrelated_source_pause_but_keeps_global_activation(tmp_path):
 b=ready_bootstrap();b['capabilities'].append(PER_TASK_CAPABILITY);b['runtime_readiness_policy']=deepcopy(PER_TASK_POLICY)
 b['library_catalog']['collections'][-1].update(production_claim_enabled=False,missing_bindings=['released_host_keyring_runtime'])
 assert selection_scope(b,'gpt-6.1-sol','low')['members_sha256']==MEMBERS_SHA256
 b['library_catalog']['unified_pool']['production_claim_enabled']=False
 with pytest.raises(ValueError):selection_scope(b,'gpt-6.1-sol','low')
 b['library_catalog']['unified_pool']['production_claim_enabled']=True;b['runtime_readiness_policy']={}
 with pytest.raises(ValueError):selection_scope(b,'gpt-6.1-sol','low')

def controller(tmp_path):
 j=Journal(tmp_path/'state');b=ready_bootstrap();b['capabilities'].append(PER_TASK_CAPABILITY);b['runtime_readiness_policy']=deepcopy(PER_TASK_POLICY)
 keys=[{'benchmark':m['source_benchmark'],'task_id':m['task_id'],'task_content_hash':m['task_content_hash']} for m in MEMBERS[:2]]
 r=SimpleNamespace(claim_task_candidates=lambda:deepcopy(keys),prepare=lambda a:(_ for _ in ()).throw(TaskNotReady('image_unavailable')))
 client=SimpleNamespace(journal=j,bootstrap=lambda:b);c=Controller(client,r,{'benchmark':POOL,'model':'gpt-6.1-sol','effort':'low','agent':'codex','total_count':1,'concurrency':1})
 j.bind('mixed_pool_scope',json.dumps(selection_scope(b,'gpt-6.1-sol','low')));a=assignment_value(c,MEMBERS[0]);events=[]
 def get(path):return {'schema_version':2,'server_time':'now','assignment':deepcopy(a)}
 def send(req):
  events.append((req.operation,req.request_id));assert req.operation.startswith('release:') and req.body['reason']=='preflight_failed';a['state']='released'
  return {'schema_version':2,'server_time':'now','request_id':req.request_id,'status':'released','assignment':deepcopy(a)}
 client.get=get;client.send=send;return c,a,b,events

def test_task_unready_release_never_starts_or_stops_other_task_and_preserves_scope(tmp_path):
 c,a,b,events=controller(tmp_path)
 try:
  with pytest.raises(TaskNotReady):c._work(deepcopy(a))
  assert c.accepting and not c._stop_event.is_set() and not c.journal.value('local_stop')
  assert not c.journal.execution(a['assignment_id']) and not any(r.operation.startswith('start:') for r in c.journal.requests())
  c._reconcile_releases(0)
  assert c.journal.value('release_reconciled:'+a['assignment_id'])=='true' and not c.blocked
  scope=c._claim_body(0)['runtime_task_scope'];assert len(scope['tasks'])==1 and scope['tasks'][0]['task_id']==MEMBERS[1]['task_id']
  assert scope['members_sha256']==MEMBERS_SHA256 and c.runtime_unavailable_tasks()[0]['reason']=='image_unavailable'
  assert c.configuration['total_count']==1 and len(json.loads(c.journal.value('mixed_pool_scope'))['members'])==64
 finally:c.pool.shutdown()

def test_lost_release_ack_replays_exact_request_and_keeps_slot_unresolved(tmp_path):
 c,a,b,events=controller(tmp_path);original=c.client.send;seen=[]
 def lose(req):seen.append(req.request_id);raise TransportUnknown('lost ack')
 c.client.send=lose
 try:
  with pytest.raises(TransportUnknown):c._work(deepcopy(a))
  req=c.journal.pending_request('release:'+a['assignment_id']);assert req and len(c._task_candidates())==1
  assert c.accepting and not c._stop_event.is_set() and not c.journal.execution(a['assignment_id'])
  c.client.send=original;c._reconcile_releases(0)
  assert c.journal.value('release_reconciled:'+a['assignment_id'])=='true' and events[0][1]==seen[0]==req.request_id
 finally:c.pool.shutdown()

def test_common_host_failure_retains_original_batch_safety_stop(tmp_path):
 c,a,b,events=controller(tmp_path);c.runtime.prepare=lambda a:(_ for _ in ()).throw(RuntimeUnavailable('host auth unknown'))
 try:
  with pytest.raises(PreflightFailed):c._work(deepcopy(a))
  assert c.journal.value('local_stop')=='true' and not c.accepting and not c.runtime_unavailable_tasks()
 finally:c.pool.shutdown()

def test_old_server_blocks_before_create_with_concrete_upgrade_gap(tmp_path):
 c,a,b,events=controller(tmp_path);b['capabilities'].remove(PER_TASK_CAPABILITY)
 try:
  with c.ownership():
   with pytest.raises(ExecutionBlocked,match='runtime_task_scope'):c.initialize()
  assert not events and not any(r.operation=='run:create' for r in c.journal.requests())
 finally:c.pool.shutdown()

@pytest.mark.parametrize('outcome',['missing','daemon_error','ready'])
def test_selected_image_inspection_is_specific_and_safe(tmp_path,monkeypatch,outcome):
 from dradar.v2.runtime import CodexRuntime
 from test_v2_host_runtime import FakeController
 b=synthetic_binding(tmp_path);b['tasks']=b['tasks'][:1];t=b['tasks'][0]
 collector=tmp_path/'collector.py';collector.write_text('synthetic owner collector');t['collector_path']=str(collector);t['collector_sha256']=hashlib.sha256(collector.read_bytes()).hexdigest()
 p,sha=write_binding(tmp_path,b);r=HostCodexRuntime(Journal(tmp_path/'image-state'),tmp_path,host_runtime_binding=p,host_runtime_sha256=sha,controller_factory=FakeController)
 monkeypatch.setattr('dradar.v2.mixed_pool.validate_selected_public_root',lambda task:(tmp_path,tmp_path))
 prepared={'agent':'codex','agent_version':'0.160.0','agent_version_verified':True,'auth_runtime':'codex-host-keyring-remote-v1','provider':'openai','billing_mode':'subscription','model':'gpt-6.1-sol','effort':'low','benchmark_id':t['benchmark'],'task_id':t['task_id'],'task_content_hash':t['task_content_hash'],'assignment_id':'a'*32}
 monkeypatch.setattr(CodexRuntime,'prepare',lambda *a,**kw:dict(prepared))
 calls=[]
 def inspect(argv,**kw):
  calls.append(argv);assert argv==['docker','--context','synthetic','image','inspect',t['image_id'],'--format','{{.Id}}']
  return SimpleNamespace(returncode=0 if outcome=='ready' else 1,stdout=t['image_id'] if outcome=='ready' else '',stderr=('Error: No such image: '+t['image_id']) if outcome=='missing' else 'daemon unavailable')
 monkeypatch.setattr('dradar.v2.host_runtime.subprocess.run',inspect)
 a={'slot_id':0,'task':{**ident(t),'model':'gpt-6.1-sol','effort':'low'},'runner':{k:v for k,v in prepared.items() if k in ['agent','agent_version','agent_version_verified','auth_runtime','provider','billing_mode']}}
 a['runner']['est_minutes']=1
 if outcome=='missing':
  with pytest.raises(TaskNotReady,match='image_unavailable'):r.prepare(a)
 elif outcome=='daemon_error':
  with pytest.raises(RuntimeUnavailable,match='unknown') as error:r.prepare(a)
  assert not isinstance(error.value,TaskNotReady)
 else:assert r.prepare(a)['task_id']==t['task_id']
 assert len(calls)==1 and (bool(r.controllers) is (outcome=='ready'))

def test_masked_task_failure_refills_same_slot_with_usable_task_and_one_start(tmp_path):
 import httpx
 from test_v2_scheduler import Server,Runtime,pump
 from dradar.v2.client import Client
 class PerTaskServer(Server):
  def __init__(self):super().__init__();self.masks=[]
  def __call__(self,req):
   if req.url.path=='/api/v2/bootstrap':
    b=ready_bootstrap();b['capabilities'].append(PER_TASK_CAPABILITY);b['runtime_readiness_policy']=deepcopy(PER_TASK_POLICY);b['account']={'account_id':'synthetic'};b['heartbeat_seconds']=30
    return self.response(b)
   scope=None
   if req.url.path.endswith('/claim'):
    body=json.loads(req.content);scope=body['runtime_task_scope'];self.masks.append(deepcopy(scope))
    assert scope['members_sha256']==MEMBERS_SHA256 and len(scope['tasks']) in [1,2]
   response=super().__call__(req)
   if scope and response.json().get('status')=='claimed':
    task=scope['tasks'][0];a=self.assignments[response.json()['assignment']['assignment_id']]
    a['task']={**task,'model':'gpt-6.1-sol','effort':'low','task_commit':None,'task_bundle':deepcopy(SOURCES[task['benchmark']]['public_bundle'])}
    a['runner']={'agent':'codex','agent_version':'0.160.0','agent_version_verified':True,'auth_runtime':'codex-host-keyring-remote-v1','provider':'openai','billing_mode':'subscription','est_minutes':1}
    rid=json.loads(req.content)['request_id'];self.receipts[rid]['assignment']=deepcopy(a)
    return self.response(self.receipts[rid],rid)
   return response
 class PerTaskRuntime(Runtime):
  def claim_task_candidates(self):return [{'benchmark':m['source_benchmark'],'task_id':m['task_id'],'task_content_hash':m['task_content_hash']} for m in MEMBERS[:2]]
  def prepare(self,a):
   if a['task']['task_id']==MEMBERS[0]['task_id']:raise TaskNotReady('image_unavailable')
   return super().prepare(a)
 server=PerTaskServer();r=PerTaskRuntime(tmp_path);r.server=server
 client=Client('http://localhost:9','synthetic',Journal(tmp_path/'run'),transport=httpx.MockTransport(server))
 c=Controller(client,r,{'benchmark':POOL,'model':'gpt-6.1-sol','effort':'low','agent':'codex','total_count':1,'concurrency':1})
 try:
  with c.ownership():
   c.initialize();pump(c,lambda:server.run['counts']['submitted']==1)
   assert server.run['state']=='active' and not c._stop_event.is_set() and not c.journal.value('local_stop')
   assert len(r.calls)==server.run['counts']['started']==server.run['counts']['submitted']==1
   assert [len(s['tasks']) for s in server.masks]==[2,1]
   assert not any(path.endswith('/stop') for method,path in server.calls)
   assert c.journal.value('release_reconciled:a0')=='true' and not c.blocked
 finally:client.close()

def test_server_ignoring_durable_candidate_mask_is_protocol_fault_not_task_skip(tmp_path):
 from dradar.v2.scheduler import AssignmentMismatch
 c,a,b,events=controller(tmp_path)
 body=c._claim_body(0);body['runtime_task_scope']['tasks']=body['runtime_task_scope']['tasks'][1:]
 req=c.journal.prepare('claim:0:1','/api/v2/runs/'+c.run_id+'/claim',body)
 c.journal.acknowledge(req,{'schema_version':2,'server_time':'now','request_id':req.request_id,'status':'claimed','assignment':deepcopy(a)})
 try:
  with pytest.raises(AssignmentMismatch):c._assignment(a)
  assert c.journal.value('local_stop')=='true' and not c.runtime_unavailable_tasks()
 finally:c.pool.shutdown()
