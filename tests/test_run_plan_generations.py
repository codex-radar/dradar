"""Intent generations never silently upgrade a stopped runner's authority."""
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from dradar import capacity_journal, fleet, identity, local_config, pending, run_intent, run_plans
from dradar.api_client import ApiClient
import test_run_plans as old


@pytest.fixture(autouse=True)
def isolated_generation_home(tmp_path, monkeypatch):
    monkeypatch.setattr(run_plans,'HOME',tmp_path)
    monkeypatch.setattr(local_config,'HOME',tmp_path)
    monkeypatch.setattr(local_config,'CONFIG_PATH',tmp_path/'config.json')
    monkeypatch.setattr(identity,'HOME',tmp_path)


def prepare(tmp_path,monkeypatch,client):
    return old._prepare_run(monkeypatch,tmp_path,plan=old._plan(),client=client,
                            snapshot=old._snapshot(available=2,auto_workers=2))


def test_new_start_sends_saved_credential_generation(tmp_path,monkeypatch,capsys):
    client=old.FakeClient(starts=[old._server_response(old._plan())])
    path,state=prepare(tmp_path,monkeypatch,client)
    state['credential_generation']=4
    client.whoami=lambda:{'schema_version':1,'plan_id':state['plan_id'],
        'device_generation':4,'credential_generation':4,'concurrent_limit':8}
    monkeypatch.setattr(fleet,'add_batch',lambda **kw:{'batch':{'status':'running','workers':kw['workers']}})
    assert run_plans.cmd_run_plan(old._args())==0
    assert client.start_calls[0]['expected_generation']==4
    assert json.loads(path.read_text())['credential_generation']==4


@pytest.mark.parametrize('field,value',[
    ('device_generation',True),('credential_generation','0'),('credential_generation',0.0),
    ('device_generation',None),('credential_generation',-1),('plan_id','another-plan'),
])
def test_identity_authority_is_exact_and_strict(tmp_path,monkeypatch,capsys,field,value):
    client=old.FakeClient()
    _,state=prepare(tmp_path,monkeypatch,client)
    response=client.whoami();response[field]=value
    client.whoami=lambda:response
    monkeypatch.setattr(fleet,'add_batch',lambda **_:pytest.fail('unconfirmed authority launched'))
    assert run_plans.cmd_run_plan(old._args())==1
    assert client.start_calls==[]
    assert json.loads(capsys.readouterr().out)['error_code'] in {'credential_generation_invalid','device_generation_unconfirmed'}


def test_old_server_reads_and_stop_remain_available_but_new_execution_fails(tmp_path,monkeypatch,capsys):
    client=old.FakeClient(progress=[old._server_response(old._plan())],
        stops=[old._server_response(old._plan(),old._envelope(status='stopped',agent_action='stop_runner'))])
    _,state=prepare(tmp_path,monkeypatch,client)
    state.pop('credential_generation')
    client.run_plan_capabilities=lambda:{}
    assert run_plans.cmd_run_plan(old._args())==1
    assert json.loads(capsys.readouterr().out)['error_code']=='runner_reservation_upgrade_required'
    assert run_plans.cmd_progress_plan(old._args())==0
    capsys.readouterr()
    monkeypatch.setattr(fleet,'stop_batch',lambda _:None)
    monkeypatch.setattr(capacity_journal,'reconcile_saved',lambda *_a,**_kw:pytest.fail('stop read capacity evidence'))
    assert run_plans.cmd_stop_plan(old._args(scope='this-device'))==0
    assert client.stop_calls[0]['expected_generation'] is None
    assert client.start_calls==[]


def test_automatic_recheck_does_not_exchange_to_a_new_live_generation(tmp_path,monkeypatch,capsys):
    client=old.FakeClient()
    path,state=prepare(tmp_path,monkeypatch,client)
    state.update(intent_generation=1,pending_recheck_generation=1)
    run_plans._atomic_json(path,state)
    client.whoami=lambda:{'schema_version':1,'plan_id':state['plan_id'],'device_generation':1,'credential_generation':0}
    # An automatic recheck must reuse an existing local run intent.
    generation=run_intent.begin(tmp_path,run_intent.request_scope(old.RUN_CODE))
    run_intent.associate_request(tmp_path,run_intent.request_scope(old.RUN_CODE),generation,old.BATCH_ID,automatic=False)
    monkeypatch.setattr(run_plans,'ApiClient',lambda *_a,**_kw:pytest.fail('automatic check exchanged'))
    assert run_plans.cmd_run_plan(old._args(recheck_generation=1))==1
    assert json.loads(capsys.readouterr().out)['error_code']=='stale_device_generation'
    assert client.start_calls==[]


def exchange_fixture(tmp_path,monkeypatch,*,global_stopped=False,cancel_on_exchange=False):
    plan=old._plan()
    path,state=old._state(tmp_path,plan)
    calls=[]
    new_token='drp_reexchanged_generation_one'
    def handler(request):
        calls.append((request.method,request.url.path,request.headers.get('Authorization'),request.content))
        bearer=request.headers.get('Authorization')
        if request.url.path=='/api/v1/run-plans/capabilities':
            return httpx.Response(200,json=old.FakeClient().run_plan_capabilities())
        if request.url.path=='/api/v1/run-plans/identity':
            return httpx.Response(200,json={'schema_version':1,'plan_id':plan['plan_id'],
                'device_generation':1,'credential_generation':1 if bearer=='Bearer '+new_token else 0,
                'concurrent_limit':8,'claim_limit':8})
        if request.url.path=='/api/v1/run-plans/exchange':
            if cancel_on_exchange:
                run_intent.stop(tmp_path,old.BATCH_ID)
            return httpx.Response(200,json={'schema_version':1,'plan':plan,'plan_access_token':new_token,
                'device_generation':1,'credential_generation':1,'access_expires_at':'2099-01-01T00:00:00Z'})
        if request.url.path=='/api/v1/run-plans/start':
            response=old._server_response(plan,old._envelope(status='stopped',agent_action='stop_runner') if global_stopped else None)
            return httpx.Response(200,json=response)
        pytest.fail('unexpected request '+request.url.path)
    def factory(server,token,**kw):
        return ApiClient(server,token,transport=httpx.MockTransport(handler),**kw)
    client=factory(state['server'],state['token'],benchmark_id=state['benchmark'],batch_id=state['batch_id'])
    monkeypatch.setattr(run_plans,'ApiClient',factory)
    monkeypatch.setattr(run_plans,'_state_and_client',lambda _:(old.RUN_CODE,path,state,client))
    monkeypatch.setattr(old.doctor,'plan_environment_issue',lambda _:None)
    monkeypatch.setattr(run_plans,'_capacity_snapshot',lambda *_a,**_kw:old._snapshot(available=2,auto_workers=2))
    monkeypatch.setattr(fleet,'batch_status',lambda _:None)
    monkeypatch.setattr(fleet,'prepare_new_batch_runtime',lambda **_:None)
    launched=[]
    monkeypatch.setattr(fleet,'add_batch',lambda **kw:(launched.append(kw) or {'batch':{'status':'running','workers':2}}))
    monkeypatch.setattr(fleet,'stop_batch',lambda _:None)
    return path,state,calls,launched


def test_explicit_resume_keeps_old_credentials_immutable_and_uses_new_generation(tmp_path,monkeypatch,capsys):
    path,state,calls,launched=exchange_fixture(tmp_path,monkeypatch)
    order=[]
    monkeypatch.setattr(capacity_journal,'reconcile_saved',lambda home,client,**kw:(order.append(client.account_scope) or {'released':0,'pending':0,'unknown':0}))
    old_scope=hashlib.sha256((state['server']+'\0'+state['token']).encode()).hexdigest()
    assert run_plans.cmd_run_plan(old._args())==0
    assert order and all(scope==old_scope for scope in order)
    original=json.loads(path.read_text())
    assert original['token']==old.PLAN_TOKEN and original['credential_generation']==0
    assert original['retired_for_new_execution'] is True
    assert len(launched)==1 and Path(launched[0]['credentials_file'])!=path
    current=json.loads(Path(launched[0]['credentials_file']).read_text())
    assert current['credential_generation']==1 and current['previous_credentials'][0]['token']==old.PLAN_TOKEN
    start=[json.loads(body) for method,url,_auth,body in calls if url.endswith('/start')]
    assert start[0]['expected_generation']==1
    assert local_config.runtime_config(path)['run_plan_credential_generation']==0
    assert local_config.runtime_config(launched[0]['credentials_file'])['run_plan_credential_generation']==1


def test_newer_stop_during_reexchange_cancels_without_overwriting_old_token(tmp_path,monkeypatch,capsys):
    path,state,calls,launched=exchange_fixture(tmp_path,monkeypatch,cancel_on_exchange=True)
    assert run_plans.cmd_run_plan(old._args())==1
    assert json.loads(capsys.readouterr().out)['error_code']=='run_cancelled_by_newer_intent'
    assert json.loads(path.read_text())['token']==old.PLAN_TOKEN
    assert not launched and not any(url.endswith('/start') for _,url,_,_ in calls)


def test_reexchange_cannot_override_server_global_stop(tmp_path,monkeypatch,capsys):
    path,state,calls,launched=exchange_fixture(tmp_path,monkeypatch,global_stopped=True)
    assert run_plans.cmd_run_plan(old._args())==0
    assert json.loads(capsys.readouterr().out)['status']=='stopped'
    assert not launched


@pytest.mark.parametrize('counts',[{'released':0,'pending':1,'unknown':0},{'released':0,'pending':0,'unknown':1}])
def test_unsettled_reservation_blocks_reexchange_and_new_execution(tmp_path,monkeypatch,capsys,counts):
    path,state,calls,launched=exchange_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(capacity_journal,'reconcile_saved',lambda *_a,**_kw:counts)
    assert run_plans.cmd_run_plan(old._args())==1
    assert json.loads(capsys.readouterr().out)['error_code']=='runner_exit_unconfirmed'
    assert not launched and not any(url.endswith('/exchange') for _,url,_,_ in calls)


def test_completed_result_blocks_exchange_and_offers_upload_only(tmp_path,monkeypatch,capsys):
    path,state,calls,launched=exchange_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(run_plans,'_exact_pending_uploads',lambda *_a,**_kw:[{'assignment_id':'assignment-0','batch_id':old.BATCH_ID}])
    assert run_plans.cmd_run_plan(old._args())==1
    result=json.loads(capsys.readouterr().out)
    assert result['error_code']=='completed_result_upload_pending'
    assert result['agent']['next_commands'][0]['args']==['--upload-only','--json']
    assert not launched and not any(url.endswith('/exchange') for _,url,_,_ in calls)


def test_fleet_fault_stop_uses_original_credential_generation(tmp_path,monkeypatch):
    path,state=old._state(tmp_path,old._plan())
    state['credential_generation']=2
    run_plans._atomic_json(path,state)
    client=old.FakeClient(stops=[{}])
    monkeypatch.setattr(fleet,'_client',lambda cfg:client)
    assert fleet._stop_run_plan_device({'credentials_file':str(path)},'local failure') is None
    assert client.stop_calls==[{'plan_id':state['plan_id'],'scope':'this_device','expected_generation':2}]


def test_journal_error_is_structured_but_does_not_block_stop(tmp_path,monkeypatch,capsys):
    client=old.FakeClient(stops=[old._server_response(old._plan(),old._envelope(status='stopped',agent_action='stop_runner'))])
    prepare(tmp_path,monkeypatch,client)
    def damaged(*_a,**_kw):
        raise capacity_journal.CapacityEvidenceError('synthetic damaged evidence')
    monkeypatch.setattr(capacity_journal,'reconcile_saved',damaged)
    assert run_plans.cmd_run_plan(old._args())==1
    assert json.loads(capsys.readouterr().out)['error_code']=='capacity_evidence_unreadable'
    monkeypatch.setattr(fleet,'stop_batch',lambda _:None)
    assert run_plans.cmd_stop_plan(old._args(scope='this-device'))==0
    assert client.stop_calls[0]['expected_generation']==0


def test_upload_only_replays_each_saved_token_scope_without_rebinding(tmp_path,monkeypatch,capsys):
    from dradar import runloop
    plan=old._plan()
    path,state=old._state(tmp_path,plan)
    prior=dict(state)
    state.update(token='drp_generation_one',credential_generation=1,previous_credentials=[prior])
    run_plans._atomic_json(path,state)
    def factory(server,token,**kw):
        return ApiClient(server,token,transport=httpx.MockTransport(lambda _:pytest.fail('only mocked saved upload allowed')),**kw)
    client=factory(state['server'],state['token'],benchmark_id=state['benchmark'],batch_id=state['batch_id'])
    original=factory(prior['server'],prior['token'],benchmark_id=prior['benchmark'],batch_id=prior['batch_id'])
    for aid,scoped in [('assignment-0',original),('assignment-1',client)]:
        fingerprint=pending.scope_fingerprint(server=scoped.server,account_scope=scoped.account_scope,
            benchmark_id=scoped.benchmark_id,batch_id=old.BATCH_ID)
        pending.record(tmp_path,{'assignment_id':aid,'batch_id':old.BATCH_ID,'scope_fingerprint':fingerprint})
    monkeypatch.setattr(run_plans,'ApiClient',factory)
    monkeypatch.setattr(run_plans,'_state_and_client',lambda _:(old.RUN_CODE,path,state,client))
    monkeypatch.setattr(runloop,'HOME',tmp_path)
    scopes=[]
    def replay(scoped,*,batch_id):
        scopes.append(scoped.account_scope)
        fingerprint=pending.scope_fingerprint(server=scoped.server,account_scope=scoped.account_scope,
            benchmark_id=scoped.benchmark_id,batch_id=batch_id)
        for entry in pending.load(tmp_path):
            if entry['scope_fingerprint']==fingerprint:
                pending.remove(tmp_path,entry['assignment_id'],scope_fingerprint=fingerprint)
    monkeypatch.setattr(runloop,'_retry_pending_uploads',replay)
    assert run_plans.cmd_run_plan(old._args(upload_only=True))==0
    assert set(scopes)=={original.account_scope,client.account_scope}
    assert pending.load(tmp_path)==[]
    assert json.loads(path.read_text())['previous_credentials'][0]['token']==old.PLAN_TOKEN


def test_expired_state_with_pending_evidence_is_never_scrubbed_or_reexchanged(tmp_path,monkeypatch):
    path,state=old._state(tmp_path,old._plan())
    # Put state under the production scanner's private directory.
    path=run_plans._state_path(state['plan_id'],tmp_path)
    state['access_expires_at']='2000-01-01T00:00:00Z'
    run_plans._atomic_json(path,state)
    pending.record(tmp_path,{'assignment_id':'assignment-0','batch_id':old.BATCH_ID})
    run_plans._cleanup_states(tmp_path)
    assert json.loads(path.read_text())['token']==old.PLAN_TOKEN
    monkeypatch.setattr(run_plans,'_exchange',lambda *_a,**_kw:pytest.fail('expired state silently exchanged'))
    _,actual_path,actual_state,_=run_plans._state_and_client(old._args(upload_only=True))
    assert actual_path==path and actual_state['token']==old.PLAN_TOKEN


def test_progress_reconciles_sealed_evidence_and_reports_remaining_unknown(tmp_path,monkeypatch,capsys):
    client=old.FakeClient(progress=[old._server_response(old._plan(),old._envelope(status='completed',agent_action='done'))])
    prepare(tmp_path,monkeypatch,client)
    called=[]
    monkeypatch.setattr(capacity_journal,'reconcile_saved',lambda *_a,**_kw:(called.append(True) or {'released':1,'pending':0,'unknown':1}))
    assert run_plans.cmd_progress_plan(old._args())==1
    response=json.loads(capsys.readouterr().out)
    assert response['error_code']=='runner_exit_unconfirmed'
    assert response['agent']['capacity_reconciliation']['released']==1
    assert called==[True]
