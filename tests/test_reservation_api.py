"""Reservation wire authority and read-only recovery contracts."""
import json

import httpx
import pytest

from dradar.api_client import ApiClient, ApiError
from dradar.providers import RUNNER_RESERVATION_CAPABILITY


def client_with(handler):
    return ApiClient('https://api.example.test','drp_test',transport=httpx.MockTransport(handler))


def cleanup(**changes):
    payload={'schema_version':1,'session_id':'runner-session-id','batch_id':'a'*32,
        'device_generation':0,'evidence_id':'b'*32,'exit_state':'confirmed',
        'process_tree':'confirmed_absent','owned_containers':'confirmed_absent',
        'execution_manifest_sha256':'c'*64}
    payload.update(changes)
    return payload


def historical(**changes):
    payload={'schema_version':1,'quarantine_id':'a'*64,'snapshot_sha256':'b'*64,
        'evidence_id':'c'*32,'managed_process_inventory':'confirmed_absent',
        'owned_container_inventory':'confirmed_absent','historical_scope_verified':True,
        'execution_manifest_sha256':'d'*64}
    payload.update(changes)
    return payload


@pytest.mark.parametrize("response", [{}, {"schema_version": 1, "capabilities": []},
    {"schema_version": 1, "capabilities": ["runner-reservation-v1"], "stop_generation_cas": 1, "close_releases_capacity": False},
    {"schema_version": 1, "capabilities": ["runner-reservation-v1"], "stop_generation_cas": True, "close_releases_capacity": True},
])
def test_incompatible_server_never_authorizes_generic_execution(response):
    seen = []
    client = client_with(lambda request: seen.append(request) or httpx.Response(200, json=response))
    with pytest.raises(ApiError) as failure:
        client.require_runner_reservation_protocol()
    assert failure.value.code == "runner_reservation_upgrade_required"
    assert [item.method for item in seen] == ["GET"]


def test_reads_have_exact_scope_and_default_protocol_capability():
    seen=[]
    client=client_with(lambda request: (seen.append(request) or httpx.Response(200,json={'ok':True})))
    client.run_plan_capabilities()
    client.claim_request_receipt('saved-request-id-0001',expected_fingerprint='a'*64)
    client.runner_session_receipt('saved-session-id',batch_id='b'*32)
    client.runner_reservations(limit=200,after='saved-session-id',quarantine_after='c'*64)
    assert [r.method for r in seen]==['GET']*4
    assert [r.url.path for r in seen]==['/api/v1/run-plans/capabilities',
        '/api/v1/claim-requests/saved-request-id-0001',
        '/api/v1/runner/sessions/saved-session-id/receipt','/api/v1/runner/reservations']
    assert dict(seen[1].url.params)=={'expected_fingerprint':'a'*64}
    assert dict(seen[2].url.params)=={'batch_id':'b'*32}
    assert dict(seen[3].url.params)=={'limit':'200','after':'saved-session-id','quarantine_after':'c'*64}
    assert all(RUNNER_RESERVATION_CAPABILITY in r.headers['X-DRadar-Capabilities'].split(',') for r in seen)
    assert all(r.headers['Authorization']=='Bearer drp_test' for r in seen)


def test_mutations_preserve_exact_caller_generation_and_evidence():
    seen=[]
    client=client_with(lambda request: (seen.append(request) or httpx.Response(200,json={'ok':True})))
    client.start_run_plan(plan_id='plan-id',logical_session_id='logical-id',concurrency_mode='fixed',concurrency=1,expected_generation=3)
    client.stop_run_plan(plan_id='plan-id',scope='this_device',expected_generation=3)
    released=cleanup(device_id='local-device-id')
    reconciled=historical(device_id='local-device-id')
    client.release_runner_capacity(released)
    client.reconcile_legacy_runner_capacity(reconciled)
    assert all(r.method=='POST' for r in seen)
    assert json.loads(seen[0].content)['expected_generation']==3
    assert json.loads(seen[1].content)['expected_generation']==3
    assert json.loads(seen[2].content)==released
    assert json.loads(seen[3].content)==reconciled
    assert [r.url.path for r in seen[2:]]==['/api/v1/runner/release-capacity','/api/v1/runner/reconcile-legacy']


@pytest.mark.parametrize('value',[True,False,'0',0.0,-1,[],{}])
def test_generation_is_strict_before_any_request(value):
    seen=[]
    client=client_with(lambda request: (seen.append(request) or httpx.Response(200,json={})))
    with pytest.raises(ValueError):
        client.start_run_plan(plan_id='plan',logical_session_id='session',concurrency_mode='fixed',expected_generation=value)
    with pytest.raises(ValueError):
        client.stop_run_plan(plan_id='plan',scope='this_device',expected_generation=value)
    with pytest.raises(ValueError):
        client.release_runner_capacity(cleanup(device_generation=value))
    assert seen==[]


@pytest.mark.parametrize('changes',[
    {'schema_version':True},{'schema_version':'1'},{'schema_version':2},
    {'evidence_id':'A'*32},{'evidence_id':'short'},{'execution_manifest_sha256':None},
    {'exit_state':'unknown'},{'process_tree':'unknown'},{'owned_containers':'running'},
    {'device_id':True},{'session_id':123},{'batch_id':'short'},{'unexpected':True},
])
def test_release_refuses_invented_or_unknown_cleanup(changes):
    seen=[]
    client=client_with(lambda request: (seen.append(request) or httpx.Response(200,json={})))
    with pytest.raises(ValueError):
        client.release_runner_capacity(cleanup(**changes))
    assert seen==[]


@pytest.mark.parametrize('value',[1,0,'true',False,None])
def test_legacy_verification_requires_explicit_boolean_true(value):
    seen=[]
    client=client_with(lambda request: (seen.append(request) or httpx.Response(200,json={})))
    with pytest.raises(ValueError):
        client.reconcile_legacy_runner_capacity(historical(historical_scope_verified=value))
    assert seen==[]


@pytest.mark.parametrize('status,body',[
    (404,{'code':'claim_request_unknown','status':'unknown','retry_same_request_only':True}),
    (409,{'code':'claim_request_conflict'}),
    (401,{'detail':'account bearer required'}),
    (429,{'detail':'rate limited'}),
])
def test_claim_receipt_errors_never_fall_back_to_claim_or_fresh_request(status,body):
    seen=[]
    client=client_with(lambda request: (seen.append(request) or httpx.Response(status,json=body)))
    client._sleep=lambda _:pytest.fail('receipt must remain one bounded read')
    with pytest.raises(ApiError) as error:
        client.claim_request_receipt('saved-request-id-0001')
    assert error.value.status_code==status and error.value.payload==body
    assert len(seen)==1 and seen[0].method=='GET'
    assert seen[0].url.path=='/api/v1/claim-requests/saved-request-id-0001'


def test_lost_release_ack_requires_explicit_receipt_and_keeps_evidence_id():
    seen=[]
    def handler(request):
        seen.append(request)
        if request.method=='POST':
            raise httpx.ReadTimeout('synthetic lost ACK')
        return httpx.Response(200,json={'closed':True,'capacity_released':True,'release_evidence_id':'b'*32})
    client=client_with(handler)
    client._sleep=lambda _:pytest.fail('cleanup never auto-retries')
    payload=cleanup()
    with pytest.raises(ApiError):
        client.release_runner_capacity(payload)
    assert len(seen)==1
    receipt=client.runner_session_receipt(payload['session_id'],batch_id=payload['batch_id'])
    assert receipt['release_evidence_id']==payload['evidence_id']
    assert [r.method for r in seen]==['POST','GET']
    assert json.loads(seen[0].content)==payload


@pytest.mark.parametrize('value',[True,0,201,'100',100.0])
def test_inventory_limit_is_strict(value):
    client=client_with(lambda _:pytest.fail('invalid inventory limit reached HTTP'))
    with pytest.raises(ValueError):
        client.runner_reservations(limit=value)


def test_receipt_params_are_validated_without_network():
    client=client_with(lambda _:pytest.fail('invalid receipt scope reached HTTP'))
    for value in (True,123,None,'short'):
        with pytest.raises(ValueError):
            client.claim_request_receipt(value)
    with pytest.raises(ValueError):
        client.claim_request_receipt('saved-request-id-0001',expected_fingerprint='wrong')
    with pytest.raises(ValueError):
        client.runner_session_receipt('saved-session-id',batch_id=True)


def test_read_transport_failure_is_one_get_without_mutation():
    seen=[]
    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout('synthetic read timeout')
    client=client_with(handler)
    client._sleep=lambda _:pytest.fail('read must remain bounded')
    with pytest.raises(ApiError):
        client.runner_reservations()
    assert len(seen)==1 and seen[0].method=='GET'
