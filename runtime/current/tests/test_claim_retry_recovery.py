"""Single-task retry recovery; synthetic in-process HTTP transport only."""
import json
import httpx
import pytest
from dradar.single.client import Client
from dradar.v2.client import TransportUnknown
from dradar.v2.journal import Journal, JournalConflict

PATH='/api/v2/single/runs/synthetic/claim'
BOOT={'schema_version':2,'server_time':'now','capabilities':['single-task-v1'],
      'account':{'account_id':'synthetic'},'execution_contract':{
          'max_assignments_per_run':1,'account_machine_binding':False,'auto_refill':False}}

def receipt(req,status='retry'):
    return {'schema_version':2,'server_time':'now','request_id':json.loads(req.content)['request_id'],
            'status':status,'reason':'temporarily_unavailable' if status=='retry' else 'synthetic',
            'retry_after_seconds':1 if status=='retry' else None}

def client(root,handler):
    def dispatch(req):
        if req.url.path.endswith('/bootstrap'):return httpx.Response(200,json=BOOT)
        return handler(req)
    return Client('http://localhost:1','synthetic-token',Journal(root),transport=httpx.MockTransport(dispatch))

def test_retry_restart_lost_ack_and_terminal_cache(tmp_path):
    sent=[]
    def server(req):
        sent.append(req.content)
        if len(sent)==2:raise httpx.ReadTimeout('lost success ACK',request=req)
        return httpx.Response(200,json=receipt(req,'retry' if len(sent)==1 else 'claimed'))
    first=client(tmp_path,server)
    assert first.mutate('claim:single',PATH,{})['status']=='retry'
    original=first.journal.requests()[0];first.close()
    resumed=client(tmp_path,server)
    with pytest.raises(TransportUnknown):resumed.mutate('claim:single',PATH,{})
    assert resumed.mutate('claim:single',PATH,{})['status']=='claimed'
    assert resumed.mutate('claim:single',PATH,{})['status']=='claimed'
    assert len(sent)==3 and len(set(sent))==1
    assert json.loads(resumed.journal.value('claim-retry:'+original.request_id))['status']=='retry'
    with pytest.raises(JournalConflict):resumed.journal.acknowledge(original,receipt(httpx.Request('POST','http://localhost',content=sent[0])))
    resumed.close()

def test_pre_upgrade_retry_is_recovered_without_new_id(tmp_path):
    journal=Journal(tmp_path);req=journal.prepare('claim:single',PATH,{})
    old={'schema_version':2,'server_time':'old','request_id':req.request_id,'status':'retry','reason':'temporarily_unavailable','retry_after_seconds':1}
    with journal._db() as db:db.execute('UPDATE requests SET response_json=?',(json.dumps(old),))
    sent=[]
    def server(wire):sent.append(wire.content);return httpx.Response(200,json=receipt(wire,'claimed'))
    c=client(tmp_path,server)
    assert c.mutate('claim:single',PATH,{})['status']=='claimed'
    assert json.loads(sent[0])['request_id']==req.request_id
    assert json.loads(c.journal.value('claim-retry:'+req.request_id))==old
    with pytest.raises(JournalConflict):c.mutate('claim:single',PATH,{'task_id':'different'})
    c.close()

@pytest.mark.parametrize('status',['no_work','stop','claimed'])
def test_definitive_receipts_do_not_send_again(tmp_path,status):
    sent=[]
    def server(req):sent.append(1);return httpx.Response(200,json=receipt(req,status))
    c=client(tmp_path,server)
    assert c.mutate('claim:single',PATH,{})['status']==status
    assert c.mutate('claim:single',PATH,{})['status']==status
    assert len(sent)==1
    c.close()

def test_other_operation_retry_is_not_reopened(tmp_path):
    j=Journal(tmp_path);req=j.prepare('start:a','/api/v2/single/assignments/a/start',{})
    value={'schema_version':2,'server_time':'now','request_id':req.request_id,'status':'retry','reason':'temporarily_unavailable'}
    j.acknowledge(req,value)
    with pytest.raises(JournalConflict):j.acknowledge(req,{**value,'status':'started'})

@pytest.mark.parametrize('allocated_before_stop',[False,True])
def test_pending_retry_and_failed_stop_never_claim_or_prepare_after_local_stop(tmp_path,allocated_before_stop):
    from dradar.single.job import Job, JobBlocked, stop
    j=Journal(tmp_path);run=j.identity('run');j.bind('configuration',json.dumps({'benchmark':'synthetic','model':'synthetic-model','effort':'low','agent':'codex'}))
    a={'assignment_id':'a','run_id':run,'lease_id':'l','work_key':'w','owner_epoch':1,'execution_id':None,'state':'leased',
       'task':{'benchmark':'synthetic','model':'synthetic-model','effort':'low','task_id':'t','task_content_hash':'a'*64,'task_commit':'b'*40},'runner':{'agent':'codex'}}
    calls=[];prepared=[];allocated=[allocated_before_stop]
    def server(req):
        calls.append(req.url.path)
        if req.url.path.endswith('/stop'):raise httpx.ConnectError('stop not delivered',request=req)
        if req.url.path.endswith('/claim'):
            allocated[0]=True
            return httpx.Response(200,json={**receipt(req,'claimed'),'assignment':a})
        return httpx.Response(200,json={'schema_version':2,'server_time':'now',
            'run':{'run_id':run,'state':'active','benchmark':'synthetic','model':'synthetic-model','effort':'low','agent':'codex'},
            'assignments':[a] if allocated[0] else []})
    c=client(tmp_path,server);job=Job(c,runtime=type('Runtime',(),{'prepare':lambda self,a:prepared.append(a)})())
    pending=j.prepare('claim:single',f'/api/v2/single/runs/{run}/claim',{})  # first retry ACK lost
    with pytest.raises(TransportUnknown):stop(c)
    assert j.value('local_stop')=='true'
    with pytest.raises(JobBlocked):Job(c,job.runtime).run()
    with pytest.raises(JobBlocked):job.claim()
    assert not any(path.endswith('/claim') for path in calls) and not prepared
    assert allocated[0]==allocated_before_stop and j.pending_request('claim:single').request_id==pending.request_id
    c.close()

def test_stopped_original_execution_can_still_upload_without_claim(tmp_path,monkeypatch):
    from dradar.single.job import Job
    c=client(tmp_path,lambda _:pytest.fail('unexpected network'))
    job=Job(c);c.journal.bind('local_stop','true');c.journal.begin_execution('a','e')
    monkeypatch.setattr(job,'snapshot',lambda:{'run':{'state':'stopped'},'assignments':[{'assignment_id':'a'}]})
    monkeypatch.setattr(job,'upload',lambda a:{'status':'original_upload'})
    assert job.run()=={'status':'original_upload'}
    c.close()
