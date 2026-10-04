"""Exactly four current exclusions plus historical recovery; synthetic protocol only."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from dradar.v2.mixed_pool import (CONTRACT,POOL,MEMBERS,MEMBER_HASHES,MEMBERS_SHA256,SOURCES,
    LEGACY68_CONTRACT,LEGACY68_POOL,validate_assignment,selection_scope,members_digest,is_historical_pool)
from dradar.v2.host_contract import MODEL,load_binding
from dradar.v2.journal import Journal
from dradar.v2.scheduler import Controller,ExecutionBlocked,AssignmentMismatch
from dradar.v2.results import Completion,save_completion
from test_v2_final68 import ready_bootstrap,synthetic_binding,assignment_value

REMOVED={'heat-pump-warranty','intrastat-meldung','live-database-cutover','protein-active-learning'}


def old_scope():
    return {'catalog_version':LEGACY68_CONTRACT['catalog_version'],'pool_benchmark':LEGACY68_POOL,
            'members_sha256':LEGACY68_CONTRACT['members_sha256'],'members':deepcopy(LEGACY68_CONTRACT['members'])}


def old_task(task_id):
    member=next(m for m in LEGACY68_CONTRACT['members'] if m['task_id']==task_id)
    source=next(s for s in LEGACY68_CONTRACT['collections'] if s['benchmark']==member['source_benchmark'])
    return {'benchmark':member['source_benchmark'],'task_id':task_id,'task_content_hash':member['task_content_hash'],
            'task_bundle':deepcopy(source['public_bundle'])}


def test_exact64_are_old68_minus_only_authorized_four_with_unchanged_task_bytes():
    assert len(MEMBERS)==len(MEMBER_HASHES)==64 and members_digest(MEMBERS)==MEMBERS_SHA256
    assert CONTRACT['source_counts']=={'deepswe':15,'pompeii':16,'tb4':14,'science':19}
    old={(m['collection_id'],m['task_id']):m['task_content_hash'] for m in LEGACY68_CONTRACT['members']}
    new={(m['collection_id'],m['task_id']):m['task_content_hash'] for m in MEMBERS}
    assert new=={k:v for k,v in old.items() if k[1] not in REMOVED}
    assert {k[1] for k in old.keys()-new.keys()}==REMOVED
    assert all(m['task_id']not in REMOVED for m in MEMBERS)


@pytest.mark.parametrize('task_id',sorted(REMOVED))
def test_removed_task_cannot_enter_current_scope_but_exact_historical_read_is_kept(task_id):
    task=old_task(task_id);scope=selection_scope(ready_bootstrap(),MODEL,'low')
    with pytest.raises(ValueError):validate_assignment(scope,task)
    with pytest.raises(ValueError):validate_assignment(old_scope(),task)
    validate_assignment(old_scope(),task,allow_historical=True)
    corrupted=deepcopy(task);corrupted['task_content_hash']='0'*64
    with pytest.raises(ValueError):validate_assignment(old_scope(),corrupted,allow_historical=True)


@pytest.mark.parametrize('fault',['old_count','four_reintroduced','partial','old_catalog'])
def test_old_or_partial_metadata_cannot_create_current64_run(tmp_path,fault):
    b=ready_bootstrap();p=b['library_catalog']['unified_pool']
    if fault=='old_count':b['library_catalog']['total_mapped_tasks']=68;p['task_count']=68
    elif fault=='four_reintroduced':
        p['members']=deepcopy(LEGACY68_CONTRACT['members']);p['members_sha256']=members_digest(p['members'])
    elif fault=='partial':p['members'].pop()
    elif fault=='old_catalog':b['library_catalog']['catalog_version']=LEGACY68_CONTRACT['catalog_version']
    j=Journal(tmp_path/'state');c=Controller(SimpleNamespace(journal=j,bootstrap=lambda:b,
        send=lambda *a:pytest.fail('run:create reached')),None,
        {'benchmark':POOL,'model':MODEL,'effort':'low','agent':'codex','total_count':1,'concurrency':1})
    with c.ownership():
        with pytest.raises(ValueError):c.initialize()
    assert not j.requests() and not c.futures


def test_current_runtime_binding_needs_only64_and_rejects_reinserted_native_task(tmp_path):
    binding=synthetic_binding(tmp_path);p=tmp_path/'binding'
    p.write_text(json.dumps(binding));sha=hashlib.sha256(p.read_bytes()).hexdigest()
    assert len(load_binding(p,sha)['tasks'])==64
    removed=next(m for m in LEGACY68_CONTRACT['members'] if m['task_id'] in REMOVED)
    extra=deepcopy(binding['tasks'][-1]);extra.update(benchmark=removed['source_benchmark'],task_id=removed['task_id'],task_content_hash=removed['task_content_hash'])
    binding['tasks'].append(extra);p.write_text(json.dumps(binding))
    with pytest.raises(ValueError):load_binding(p,hashlib.sha256(p.read_bytes()).hexdigest())


def test_old68_scope_cannot_reinitialize_tick_or_prepare(tmp_path):
    j=Journal(tmp_path/'state');j.bind('mixed_pool_scope',json.dumps(old_scope()))
    c=Controller(SimpleNamespace(journal=j,bootstrap=lambda:pytest.fail('bootstrap reached')),
        SimpleNamespace(prepare=lambda *a:pytest.fail('prepare reached')),
        {'benchmark':LEGACY68_POOL,'model':MODEL,'effort':'low','agent':'codex','total_count':1,'concurrency':1})
    assert c.historical_mixed_scope and not c.launch_allowed
    with c.ownership():
        with pytest.raises(ExecutionBlocked,match='historical68'):c.initialize()
        with pytest.raises(ExecutionBlocked):c.tick()
        with pytest.raises(ExecutionBlocked,match='historical68'):c._work({})
    assert not j.requests() and not c.futures


def test_historical_progress_upload_and_stop_preserve_original_hash_with_no_runtime(tmp_path):
    from dradar.v2.protocol import result_hash
    j=Journal(tmp_path/'state');j.bind('mixed_pool_scope',json.dumps(old_scope()));requests=[];run={};value={}
    client=SimpleNamespace(journal=j,_bootstrap={"limits":{"max_result_bytes":1048576}})
    c=Controller(client,None,{'benchmark':LEGACY68_POOL,'model':MODEL,'effort':'low','agent':'codex','total_count':1,'concurrency':1})
    member=next(m for m in LEGACY68_CONTRACT['members'] if m['task_id']=='heat-pump-warranty')
    a=assignment_value(c,MEMBERS[0]);a['task']=dict(old_task(member['task_id']),model=MODEL,effort='low',task_commit=None)
    a.update(state='running',execution_id='e',started_at='2026-10-04T00:00:00Z')
    patch=tmp_path/'patch';patch.write_bytes(b'');assert j.begin_execution(a['assignment_id'],'e')
    payload=save_completion(j.root/'artifacts',a,'e',Completion('completed',True,{'patch':patch},elapsed_ms=0))
    j.save_result(a['assignment_id'],'e',payload)
    counts={'started':1,'leased':0,'running':1,'uncertain':0,'submitted':0}
    run.update(c.configuration,run_id=c.run_id,device_id=c.device_id,state='active',remaining_to_start=0,counts=counts)
    def get(path):
        requests.append(('GET',path));return {'schema_version':2,'server_time':'now','run':run,'assignments':[a]}
    def result(req,files):
        requests.append(('RESULT',req.path));assert req.body['result_sha256']==payload['result_sha256'] and set(files)=={'patch'}
        return {'schema_version':2,'server_time':'now','request_id':req.request_id,'status':'submitted',
            'assignment_id':a['assignment_id'],'execution_id':'e','result_sha256':payload['result_sha256'],'submission_id':'s','grading_state':'queued'}
    def stop(req):
        requests.append(('STOP',req.path));assert req.path.endswith('/stop');return {'schema_version':2,'server_time':'now','request_id':req.request_id,'status':'stopped'}
    client.get=get;client.send_result=result;client.send=stop
    try:
        assert c.progress_snapshot()['assignments'][0]['progress']['elapsed_ms']==0
        with c.ownership():
            assert len(c.upload_only())==1
            c.stop()
        assert not c.futures and c.runtime is None
        assert j.execution(a['assignment_id'])['result_json'] and result_hash(payload)==payload['result_sha256']
        assert [k for k,p in requests]==['GET','GET','RESULT','STOP']
        assert (j.root/'artifacts/raw'/a['assignment_id']/'patch').exists()
    finally:c.pool.shutdown()


def test_schema_exposes64_without_native_first_release_dependency(capsys):
    from dradar.v2.commands import main
    assert main(['schema'])==0
    mixed=json.loads(capsys.readouterr().out)['host_runtime']['mixed_pool']
    assert mixed['task_count']==64 and mixed['members_sha256']==MEMBERS_SHA256
    assert mixed['runtime_config_version']=='host-remote-0160-final64-v1'
    assert 'on-demand-v2-native-services-v1' not in mixed['wire_capabilities']


def test_paused_current64_configuration_blocks_before_run_creation(tmp_path):
    b=ready_bootstrap();b['library_catalog']['unified_pool']['production_claim_enabled']=False
    j=Journal(tmp_path/'paused')
    c=Controller(SimpleNamespace(journal=j,bootstrap=lambda:b,send=lambda *a:pytest.fail('run:create reached')),None,
        {'benchmark':POOL,'model':MODEL,'effort':'low','agent':'codex','total_count':1,'concurrency':1})
    with c.ownership():
        with pytest.raises(ValueError,match='pending'):c.initialize()
    assert not j.requests() and not c.futures
    assert CONTRACT['provenance']['claims_enabled_at_capture'] is False
