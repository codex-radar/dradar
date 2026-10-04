"""Current64 boundary tests: synthetic readiness, no HTTP/model/container calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from dradar.v2.mixed_pool import *
from dradar.v2.host_contract import (MODEL, EFFORTS, MODEL_CAPABILITY, AUTH_RUNTIME,
    CAPABILITY, MIXED_SCHEMA, MIXED_CONFIG_VERSION, SERVER_CONTRIBUTION_POLICY,
    MIXED_WIRE_CAPABILITIES, load_binding, policy_id)
from dradar.v2.scheduler import Controller, AssignmentMismatch
from dradar.v2.journal import Journal
from dradar.harness_policy import retired_combination, reject_retired_combination, current_catalog

def ready_bootstrap():
    """Synthetic enabled metadata, never proof that a real Server is enabled."""
    selections=[{'model':MODEL,'effort':e} for e in EFFORTS]
    rows=[]
    for b,source in SOURCES.items():
        rows.append({k:deepcopy(source[k]) for k in
                     ('collection_id','benchmark','selection_version','task_count')})
        rows[-1].update(required_client_capabilities=[MODEL_CAPABILITY,AUTH_RUNTIME],
            production_claim_enabled=True,missing_bindings=[],model_effort_selections=selections,
            public_task_hashes={t:h for (s,t),h in MEMBER_HASHES.items() if s==b},
            public_bundle={**source['public_bundle'],'archive_root_prefix':source['archive_root_prefix']})
    return {'capabilities':['on-demand-v2',POOL_CAPABILITY],
        'library_catalog':{'catalog_version':CATALOG_VERSION,'total_mapped_tasks':64,
            'collections':rows,'unified_pool':{'benchmark':POOL,'selection_version':CATALOG_VERSION,
            'task_count':64,'source_counts':CONTRACT['source_counts'],'single_start_entry':True,
            'members_sha256':MEMBERS_SHA256,'members':deepcopy(MEMBERS),
            'required_client_capabilities':[POOL_CAPABILITY,MODEL_CAPABILITY,AUTH_RUNTIME],
            'production_claim_enabled':True}},'contribution_policy':deepcopy(SERVER_CONTRIBUTION_POLICY),
        'benchmarks':[{'benchmark':POOL,'task_count':64,'source_benchmarks':list(SOURCES),
            'required_client_capabilities':[POOL_CAPABILITY], 'models':selections}],
        'limits':{'max_total_count':None,'max_concurrency':None}}

def synthetic_binding(root):
    """Structurally valid fixed members; image/collector values are synthetic."""
    tasks=[]
    for m in MEMBERS:
        b=m['source_benchmark'];pom=policy_id(b)=='pompeii-adjacency'
        tasks.append({'benchmark':b,'policy_id':policy_id(b),'task_id':m['task_id'],
            'task_content_hash':m['task_content_hash'],'source_root':str(root/b),
            'image_id':'sha256:'+'b'*64,'git_head':'c'*40,'cpus':2,'memory_bytes':8*1024**3,
            'agent_timeout_sec':7200 if pom else 10800,'official_task_timeout_sec':7200 if pom else 10800,
            'verifier_timeout_sec':300,'executor_cli':'/usr/local/bin/codex',
            'executor_path':'/usr/local/bin:/usr/bin:/bin','executor_home':'/home/app',
            'public_files':{'/app/public.txt':'d'*64},'collector_path':str(root/'collector.py'),
            'collector_sha256':'e'*64,'deliverable_names':['result.txt']})
    return {'schema':MIXED_SCHEMA,'runtime_config_version':MIXED_CONFIG_VERSION,
        'capability':CAPABILITY,'model':MODEL,'efforts':list(EFFORTS),
        'host':{'host_home':str(Path.home()/'.local/share/dradar/codex-host-home'),
        'host_cli':'/tmp/synthetic-official/bin/codex','host_cli_sha256':'e'*64,
        'host_companion':'/tmp/synthetic-official/bin/codex-code-mode-host','host_companion_sha256':'f'*64,
        'docker_context':'synthetic','proxy_image_id':'sha256:'+'1'*64},'tasks':tasks}

def assignment_value(c,m,n=0):
    b=m['source_benchmark']
    return {'assignment_id':f'{n+1:032x}','run_id':c.run_id,'device_id':c.device_id,
        'slot_id':0,'work_key':'work'+str(n),'lease_id':'lease'+str(n),'owner_epoch':1,
        'state':'leased','execution_id':None,'started_at':None,
        'task':{'benchmark':b,'task_id':m['task_id'],'task_content_hash':m['task_content_hash'],
            'task_bundle':deepcopy(SOURCES[b]['public_bundle']),'task_commit':None,
            'model':MODEL,'effort':'low'},
        'runner':{'agent':'codex','agent_version':'0.160.0','agent_version_verified':True,
            'auth_runtime':AUTH_RUNTIME,'provider':'openai','billing_mode':'subscription','est_minutes':1}}

@pytest.mark.parametrize('effort',EFFORTS)
def test_complete_fixed_selection_supports_each_existing_effort(effort):
    scope=selection_scope(ready_bootstrap(),MODEL,effort)
    assert members_digest(scope['members'])==MEMBERS_SHA256 and len(scope['members'])==64

@pytest.mark.parametrize('fault',['cap','catalog','count','members','member_hash','pool_pause',
    'source_pause','source_missing','source_hash','bundle','root_prefix','model_map','contribution',
    'extra_menu','missing_source','malformed_source'])
def test_incomplete_or_modified_pool_blocks_before_create(tmp_path,fault):
    b=ready_bootstrap();lib=b['library_catalog'];p=lib['unified_pool'];r=lib['collections'][0]
    if fault=='cap':b['capabilities'].remove(POOL_CAPABILITY)
    elif fault=='catalog':lib['catalog_version']='old-pilot'
    elif fault=='count':lib['total_mapped_tasks']=67
    elif fault=='members':p['members'].reverse()
    elif fault=='member_hash':p['members'][0]['task_content_hash']='0'*64
    elif fault=='pool_pause':p['production_claim_enabled']=False
    elif fault=='source_pause':r['production_claim_enabled']=False
    elif fault=='source_missing':r['missing_bindings']=['runtime']
    elif fault=='source_hash':r['public_task_hashes']={}
    elif fault=='bundle':r['public_bundle']['sha256']='0'*64
    elif fault=='root_prefix':r['public_bundle']['archive_root_prefix']='other'
    elif fault=='model_map':r['model_effort_selections']=[]
    elif fault=='contribution':b['contribution_policy']['points_until_basis_resolved']=0
    elif fault=='extra_menu':b['benchmarks'].append(deepcopy(b['benchmarks'][0]))
    elif fault=='missing_source':lib['collections'].pop()
    elif fault=='malformed_source':lib['collections'][0]=None
    j=Journal(tmp_path/'state');client=SimpleNamespace(journal=j,bootstrap=lambda:b,
        send=lambda *a:pytest.fail('run:create reached'))
    c=Controller(client,None,{'benchmark':POOL,'model':MODEL,'effort':'low',
        'agent':'codex','total_count':1,'concurrency':1})
    with c.ownership():
        with pytest.raises(ValueError):c.initialize()
    assert j.requests()==[] and not j.value('mixed_pool_scope')

def test_source_assignments_pin_members_without_changing_run_pool_or_budget(tmp_path):
    j=Journal(tmp_path/'state');config={'benchmark':POOL,'model':MODEL,'effort':'low',
        'agent':'codex','total_count':1,'concurrency':1}
    c=Controller(SimpleNamespace(journal=j),None,config)
    try:
        j.bind('mixed_pool_scope',json.dumps(selection_scope(ready_bootstrap(),MODEL,'low')))
        for n,b in enumerate(SOURCES):
            m=next(m for m in MEMBERS if m['source_benchmark']==b)
            a=assignment_value(c,m,n);assert c._assignment(a)==a
        assert c.configuration==config and not j.requests() and not c.futures
        a['owner_epoch']=2
        with pytest.raises(AssignmentMismatch):c._assignment(a)
        assert j.value('local_interrupt')=='true'
    finally:c.pool.shutdown()

@pytest.mark.parametrize('fault',['source','task','hash','bundle','model','effort','saved_members'])
def test_outside_scope_or_changed_selection_cannot_launch(tmp_path,fault):
    j=Journal(tmp_path/'state');c=Controller(SimpleNamespace(journal=j),None,
        {'benchmark':POOL,'model':MODEL,'effort':'low','agent':'codex','total_count':1,'concurrency':1})
    scope=selection_scope(ready_bootstrap(),MODEL,'low');a=assignment_value(c,MEMBERS[0])
    if fault=='saved_members':scope['members']=scope['members'][:-1]
    else:
        key={'source':'benchmark','task':'task_id','hash':'task_content_hash','bundle':'task_bundle',
             'model':'model','effort':'effort'}[fault]
        a['task'][key]={'task_bundle':{},'task_content_hash':'0'*64}.get(key,'outside')
    j.bind('mixed_pool_scope',json.dumps(scope))
    try:
        with pytest.raises(AssignmentMismatch):c._assignment(a)
        assert not c.futures and not j.requests()
    finally:c.pool.shutdown()

@pytest.mark.parametrize('fault',[None,'partial','duplicate','root_collapse','hash','schema','not_object'])
def test_mixed_binding_requires_exact64_and_four_roots(tmp_path,fault):
    b=synthetic_binding(tmp_path)
    if fault=='partial':b['tasks'].pop()
    elif fault=='duplicate':b['tasks'][-1]=deepcopy(b['tasks'][0])
    elif fault=='root_collapse':
        for t in b['tasks']:t['source_root']=str(tmp_path/'same')
    elif fault=='hash':b['tasks'][0]['task_content_hash']='0'*64
    elif fault=='schema':b['runtime_config_version']='host-remote-0160-v1'
    elif fault=='not_object':b=[]
    p=tmp_path/'binding.json';p.write_text(json.dumps(b));sha=hashlib.sha256(p.read_bytes()).hexdigest()
    if fault is None:assert load_binding(p,sha)==b
    else:
        with pytest.raises(ValueError):load_binding(p,sha)

def test_old_binding_offers_no_mixed_capability(tmp_path):
    from dradar.v2.client import Client
    c=Client('http://127.0.0.1:9','synthetic',Journal(tmp_path/'state'))
    try:
        c.offer_bound_host_runtime();assert POOL_CAPABILITY not in c.http.headers['X-DRadar-Capabilities']
        c.offer_bound_host_runtime(mixed=True)
        assert c.http.headers['X-DRadar-Capabilities']==','.join(MIXED_WIRE_CAPABILITIES)
    finally:c.close()

@pytest.mark.parametrize('fault',[None,'missing_root','marker','marker_hash','marker_source',
    'marker_symlink','missing_task','instruction','task_toml','input_symlink'])
def test_four_pack_presence_and_markers_required_before_new_work(tmp_path,fault):
    from dradar.taskpacks import MARKER
    b=synthetic_binding(tmp_path)
    for source in SOURCES:
        base=tmp_path/source;base.mkdir()
        (base/MARKER).write_text(json.dumps({'sha256':SOURCES[source]['public_bundle']['sha256'],'benchmark_id':source}))
    for task in b['tasks']:
        root=tmp_path/task['benchmark']/SOURCES[task['benchmark']]['archive_root_prefix']
        directory=root/task['task_id'];directory.mkdir(parents=True)
        (directory/'instruction.md').write_text('synthetic availability only; content hashes checked per assignment')
        (directory/'task.toml').write_text('[agent]\n')
    task=b['tasks'][-1];base=tmp_path/task['benchmark'];root=base/SOURCES[task['benchmark']]['archive_root_prefix']
    directory=root/task['task_id'];marker=base/MARKER
    if fault=='missing_root':
        for t in b['tasks']:
            if t['benchmark']==task['benchmark']:t['source_root']=str(tmp_path/'absent')
    elif fault=='marker':marker.unlink()
    elif fault=='marker_hash':marker.write_text(json.dumps({'sha256':'0'*64,'benchmark_id':task['benchmark']}))
    elif fault=='marker_source':marker.write_text(json.dumps({'sha256':SOURCES[task['benchmark']]['public_bundle']['sha256'],'benchmark_id':'other'}))
    elif fault=='marker_symlink':
        copy=tmp_path/'synthetic-marker';copy.write_bytes(marker.read_bytes());marker.unlink();marker.symlink_to(copy)
    elif fault=='missing_task':(directory/'instruction.md').unlink();(directory/'task.toml').unlink();directory.rmdir()
    elif fault=='instruction':(directory/'instruction.md').unlink()
    elif fault=='task_toml':(directory/'task.toml').unlink()
    elif fault=='input_symlink':
        (directory/'instruction.md').unlink();(directory/'instruction.md').symlink_to(directory/'task.toml')
    if fault is None:assert set(validate_public_roots(b))==set(SOURCES)
    else:
        with pytest.raises(ValueError):validate_public_roots(b)

def test_actual_client_initialize_pins_pool_before_create_without_claiming(tmp_path):
    import httpx
    from dradar.v2.client import Client
    boot=ready_bootstrap();boot.update(schema_version=2,server_time='2026-10-04T00:00:00Z',
        account={'account_id':'synthetic-account'})
    calls=[];run={}
    def server(req):
        calls.append((req.method,req.url.path))
        assert req.headers['X-DRadar-Capabilities']==','.join(MIXED_WIRE_CAPABILITIES)
        if req.url.path=='/api/v2/bootstrap':return httpx.Response(200,json=boot)
        envelope={'schema_version':2,'server_time':'2026-10-04T00:00:00Z'}
        if req.method=='POST':
            body=json.loads(req.content)
            assert set(body)=={'request_id','run_id','device_id','benchmark','model','effort','agent','total_count','concurrency'}
            assert body['benchmark']==POOL and body['total_count']==2
            assert json.loads(j.value('mixed_pool_scope'))['members_sha256']==MEMBERS_SHA256
            run.update({k:v for k,v in body.items() if k!='request_id'},state='active',
                remaining_to_start=2,counts={k:0 for k in ['started','leased','running','uncertain','submitted']})
            return httpx.Response(200,json={**envelope,'request_id':body['request_id'],'status':'accepted','run':run})
        return httpx.Response(200,json={**envelope,'run':run,'assignments':[]})
    j=Journal(tmp_path/'state');client=Client('http://localhost:9','synthetic',j,transport=httpx.MockTransport(server))
    client.offer_bound_host_runtime(mixed=True)
    c=Controller(client,None,{'benchmark':POOL,'model':MODEL,'effort':'low','agent':'codex','total_count':2,'concurrency':1})
    try:
        with c.ownership():assert c.initialize()['run']['benchmark']==POOL
        assert calls==[('GET','/api/v2/bootstrap'),('POST','/api/v2/runs'),('GET','/api/v2/runs/'+c.run_id)]
        assert len(j.requests())==1 and not c.futures
    finally:client.close()

def test_formal_entry_missing_roots_blocks_before_cap_offer_or_bootstrap(tmp_path,monkeypatch,capsys):
    import dradar.v2.commands as command
    binding=tmp_path/'binding.json';binding.write_text(json.dumps(synthetic_binding(tmp_path)))
    digest=hashlib.sha256(binding.read_bytes()).hexdigest();closed=[]
    monkeypatch.setattr(command,'runtime_config',lambda:{'server':'http://localhost:9','token':'synthetic'})
    def client_factory(server,token,journal):
        return SimpleNamespace(close=lambda:closed.append(True),
            offer_bound_host_runtime=lambda **kw:pytest.fail('unverified mixed capability offered'),
            bootstrap=lambda:pytest.fail('bootstrap reached'))
    assert command.main(['run','--state-root',str(tmp_path/'state'),'--tasks-root',str(tmp_path/'unused'),
        '--host-runtime-binding',str(binding),'--host-runtime-sha256',digest,
        '--benchmark',POOL,'--model',MODEL,'--effort','low','--total-count','1','--concurrency','1'],
        client_factory=client_factory)==3
    assert json.loads(capsys.readouterr().out)['status']=='blocked'
    assert closed==[True] and not Journal(tmp_path/'state').requests()

@pytest.mark.parametrize('effort',EFFORTS)
def test_exact_gpt55_retirement_keeps_adjacent_models_and_other_harnesses(effort):
    with pytest.raises(ValueError,match='gpt-5.5'):reject_retired_combination('codex','gpt-5.5')
    for agent,model in [('codex','gpt-5.6'),('codex',MODEL),('claude','gpt-5.5'),('kiro','gpt-5.5')]:
        assert not retired_combination(agent,model)
    value={'benchmarks':[{'benchmark':POOL,'models':[{'model':'gpt-5.5','effort':effort},
        {'model':MODEL,'effort':effort}]}],'ledger':[{'model':'gpt-5.5','reward':1}]}
    out=current_catalog(value);assert len(out['benchmarks'][0]['models'])==1
    assert out['aggregate_missing_reason']=='upstream_snapshot_contains_retired_codex_gpt_5_5'
    assert out['ledger']==value['ledger'] and len(value['benchmarks'][0]['models'])==2
