import json
import time

import httpx
import pytest

from dradar import local_config, plan_observation_recovery
from dradar.api_client import ApiClient, ApiError

PLAN = 'a'*32
ADMISSION = 'b'*32


def invoke(api, operation):
    if operation == 'progress':
        return api.run_plan_progress(PLAN)
    return api.heartbeat_run_plan(plan_id=PLAN, current_start_intent_id=ADMISSION,
                                  expected_intent_revision=1, expected_generation=0)


def result(operation):
    return ({'schema_version': 1, 'plan': {'plan_id': PLAN}, 'envelope': {'status': 'running'}}
            if operation == 'progress' else {'schema_version': 1, 'plan_id': PLAN,
                'touched': True, 'starts_new_work': False})


def receipt(request, operation, *, committed=False):
    return httpx.Response(200, json={'schema_version': 1, 'operation': 'plan_'+operation,
        'request_id': request.url.path.rsplit('/', 1)[-1], 'status': 'committed' if committed else 'unknown',
        'retry_same_request_only': True, **({'result': result(operation)} if committed else {})})


@pytest.mark.parametrize('operation', ['heartbeat', 'progress'])
@pytest.mark.parametrize('fault', ['busy', 'lost'])
def test_observation_original_identity_reconciles_before_replay(operation, fault):
    requests, timestamps = [], []
    def handler(request):
        requests.append((request.method, request.headers.get('X-DRadar-Write-ID'), request.read()))
        if request.method == 'GET':
            return receipt(request, operation, committed=fault == 'lost')
        timestamps.append(time.monotonic())
        if len(timestamps) == 1:
            if fault == 'lost':
                raise httpx.ReadError('synthetic')
            return httpx.Response(503, headers={'Retry-After': '.01'},
                                  json={'code': 'mutation_busy', 'retry_after_seconds': .01})
        return httpx.Response(200, json=result(operation))
    api = ApiClient('https://example.invalid', 'drp_synthetic', transport=httpx.MockTransport(handler))
    assert invoke(api, operation) == result(operation)
    assert [r[0] for r in requests] == (['POST', 'GET'] if fault == 'lost' else ['POST', 'GET', 'POST'])
    if fault == 'busy':
        assert requests[0] == requests[2] and timestamps[1] - timestamps[0] >= .01
    assert not list((local_config.HOME/'pending_plan_observations').glob('*.json'))


def test_observation_budget_survives_new_client_until_explicit_progress():
    ids, reads = [], []
    def handler(request):
        if request.method == 'GET':
            reads.append(request.url.path)
            return receipt(request, 'heartbeat')
        ids.append(request.headers['X-DRadar-Write-ID'])
        raise httpx.ReadError('synthetic')
    def client(token):
        return ApiClient('https://example.invalid', token, transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError): invoke(client('drp_first'), 'heartbeat')
    assert len(reads) == 2
    with pytest.raises(ApiError): invoke(client('drp_renewed'), 'heartbeat')
    assert len(reads) == 2
    assert len(ids) == 2 and len(set(ids)) == 1
    api = client('drp_renewed')
    api._explicit_write_recovery = True
    observed = plan_observation_recovery.reconcile_pending(api, PLAN)
    assert observed[0]['status'] == 'unknown_unreconciled'
    assert len(ids) == 4 and len(set(ids)) == 1
    assert len(reads) == 5  # Reconcile the saved identity before explicit replay.
    plan_observation_recovery.reconcile_pending(api, PLAN)
    assert len(ids) == 4  # Same command cannot reset again.
    assert len(reads) == 5
    saved, = (local_config.HOME/'pending_plan_observations').glob('*.json')
    entry = json.loads(saved.read_text())
    assert entry['attempts'] == 2 and entry['finished'] is True and 'drp_' not in saved.read_text()


def test_expired_observation_budget_does_not_restart_receipt_reads():
    calls = []
    def handler(request):
        calls.append(request.method)
        raise httpx.ReadError('synthetic')
    api = ApiClient('https://example.invalid', 'drp_synthetic', transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError): invoke(api, 'heartbeat')
    saved, = (local_config.HOME/'pending_plan_observations').glob('*.json')
    entry = json.loads(saved.read_text())
    entry.update(finished=False, deadline=time.monotonic()-1)
    saved.write_text(json.dumps(entry))
    before = list(calls)
    with pytest.raises(ApiError): invoke(api, 'heartbeat')
    assert calls == before
    assert json.loads(saved.read_text())['finished'] is True


def test_baseline_without_receipts_keeps_unknown_without_blind_replay():
    methods = []
    def handler(request):
        methods.append(request.method)
        if request.method == 'POST': raise httpx.ReadError('synthetic')
        return httpx.Response(422, json={})
    api = ApiClient('https://example.invalid', 'drp_synthetic', transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as caught:
        invoke(api, 'progress')
    assert methods == ['POST', 'GET']
    assert caught.value.write_outcome['status'] == 'unknown_unreconciled'
