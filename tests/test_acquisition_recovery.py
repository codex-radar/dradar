import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from dradar import acquisition_recovery, local_config, run_intent
from dradar.api_client import ApiClient, ApiError


def client(handler):
    def authenticated(request):
        if request.url.path == '/api/v1/whoami':
            return httpx.Response(200, json={'volunteer_id': 'd'*32})
        return handler(request)
    return ApiClient('https://example.invalid', 'synthetic-private-token', transport=httpx.MockTransport(authenticated))


def receipt(request, status, result=None):
    operation, rid = request.url.path.split('/')[-2:]
    return httpx.Response(200, json={'schema_version': 1, 'operation': operation,
        'request_id': rid, 'status': status, 'retry_same_request_only': True,
        **({'result': result, 'execution_allowed': False} if result is not None else {})})


@pytest.mark.parametrize('kind', ['claim', 'checkout'])
def test_lost_receipt_is_queried_and_allocation_is_not_repeated(kind):
    seen = []
    result = {'assignment': {'assignment_id': 'a'*32}}
    def handler(request):
        seen.append(request.method)
        if request.method == 'POST':
            raise httpx.ReadError('synthetic lost ACK')
        return receipt(request, 'committed', result)
    api = client(handler)
    answer = api.claim_assignment('t1', 'm', 'e') if kind == 'claim' else api.checkout(session_id='b'*32)
    assert answer == result and seen == ['POST', 'GET']
    assert list((local_config.HOME/'pending_acquisitions').glob('*.json')) == []


def test_unknown_identity_survives_a_new_api_client_and_a_changed_selection():
    ids, methods = [], []
    def failed(request):
        methods.append(request.method)
        if request.method == 'POST':
            ids.append(parse_qs(request.read().decode())['request_id'][0])
        raise httpx.ReadError('synthetic')
    with pytest.raises(ApiError):
        client(failed).claim_assignment('t1', 'm', 'e')
    saved = list((local_config.HOME/'pending_acquisitions').glob('*.json'))
    assert len(saved) == 1 and 'synthetic-private-token' not in saved[0].read_text()
    def recovered(request):
        assert request.method == 'GET'
        assert request.url.path.endswith('/'+ids[0])
        return receipt(request, 'committed', {'assignment': {'assignment_id': 'a'*32, 'task_id': 't1'}})
    result = client(recovered).claim_assignment('t2', 'm', 'e')
    assert result['assignment']['task_id'] == 't1'
    assert methods == ['POST', 'GET'] and not saved[0].exists()


def test_preallocated_fleet_claim_identity_cannot_be_changed_after_send():
    original = 'a' * 32
    replacement = 'b' * 32
    methods = []
    def lost(request):
        methods.append(request.method)
        if request.method == 'POST':
            assert parse_qs(request.read().decode())['request_id'] == [original]
        return receipt(request, 'unknown') if request.method == 'GET' else httpx.Response(503)
    with pytest.raises(ApiError):
        client(lost).claim_assignment('t1', 'm', 'e', request_id=original)
    with pytest.raises(ApiError, match='saved allocation identity is unreadable'):
        client(lost).claim_assignment('t1', 'm', 'e', request_id=replacement)
    assert methods.count('POST') == 1


def test_first_server_rejection_proves_exact_request_was_not_claimed():
    request_id = 'c' * 32
    def reject(request):
        assert request.method == 'POST'
        return httpx.Response(409, json={'detail': 'cell exhausted', 'code': 'cell_exhausted'})
    with pytest.raises(ApiError) as caught:
        client(reject).claim_assignment('t1', 'm', 'e', request_id=request_id)
    assert caught.value.allocation_no_claim == {
        'operation': 'assignment_claim', 'request_id': request_id,
        'status': 'definitive_rejection',
    }
    assert list((local_config.HOME/'pending_acquisitions').glob('*.json')) == []


def test_repeated_clients_share_allocation_attempt_and_read_budgets():
    methods, identities = [], []
    def handler(request):
        methods.append(request.method)
        if request.method == 'POST':
            identities.append(parse_qs(request.read().decode())['request_id'][0])
            raise httpx.ReadError('synthetic')
        return receipt(request, 'unknown')
    for _ in range(5):
        with pytest.raises(ApiError) as caught:
            client(handler).claim_assignment('t1', 'm', 'e')
        assert caught.value.write_outcome['status'] == 'unknown_unreconciled'
    assert methods.count('POST') == 2
    assert methods.count('GET') == acquisition_recovery.MAX_RECEIPT_READS
    assert len(set(identities)) == 1


def test_leases_inspects_original_receipt_after_automatic_budget_ended():
    def lost(request):
        if request.method == 'GET':
            return receipt(request, 'unknown')
        raise httpx.ReadError('synthetic')
    for _ in range(4):
        with pytest.raises(ApiError): client(lost).claim_assignment('t1', 'm', 'e')
    seen = []
    def recovered(request):
        seen.append(request.method)
        assert request.method == 'GET'
        return receipt(request, 'committed', {'assignment': {'assignment_id': 'a'*32}})
    result = acquisition_recovery.inspect_pending(client(recovered))
    assert result[0]['status'] == 'unknown_reconciled'
    assert result[0]['execution_allowed'] is False
    assert seen == ['GET']
    assert not list((local_config.HOME/'pending_acquisitions').glob('*.json'))


def test_plan_token_renewal_keeps_original_checkout_identity():
    ids = []
    def lost(request):
        if request.method == 'POST':
            ids.append(parse_qs(request.read().decode())['request_id'][0])
        raise httpx.ReadError('synthetic')
    original = ApiClient('https://example.invalid', 'drp_old_synthetic', batch_id='c'*32,
                         transport=httpx.MockTransport(lost))
    with pytest.raises(ApiError):
        original.checkout(session_id='b'*32)
    def recovered(request):
        assert request.method == 'GET' and request.url.path.endswith('/'+ids[0])
        return receipt(request, 'committed', {'assignment': {'assignment_id': 'a'*32}})
    renewed = ApiClient('https://example.invalid', 'drp_new_synthetic', batch_id='c'*32,
                        transport=httpx.MockTransport(recovered))
    assert renewed.checkout(session_id='b'*32)['assignment']['assignment_id'] == 'a'*32


def test_busy_queries_support_before_replay_and_preserves_identity():
    bodies, times = [], []
    def handler(request):
        if request.method == 'GET':
            return receipt(request, 'unknown')
        bodies.append(request.read())
        times.append(time.monotonic())
        if len(bodies) == 1:
            return httpx.Response(503, headers={'Retry-After': '0.01'},
                                 json={'code': 'mutation_busy', 'retry_after_seconds': 0.01})
        return httpx.Response(200, json={'assignment': {'assignment_id': 'a'*32}})
    assert client(handler).checkout(session_id='b'*32)['assignment']
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert times[1] - times[0] >= 0.01


def test_stop_before_retry_cancels_new_write_but_retains_identity(monkeypatch, tmp_path):
    marker = tmp_path/'stop'
    monkeypatch.setenv(run_intent.POOL_STOP_ENV, str(marker))
    methods = []
    def handler(request):
        methods.append(request.method)
        if request.method == 'GET':
            marker.touch()
            return receipt(request, 'unknown')
        return httpx.Response(503, headers={'Retry-After': '0.01'},
                             json={'code': 'mutation_busy', 'retry_after_seconds': 0.01})
    with pytest.raises(ApiError) as caught:
        client(handler).checkout(session_id='b'*32)
    assert methods == ['POST', 'GET']
    assert caught.value.write_outcome['request_saved']
    assert len(list((local_config.HOME/'pending_acquisitions').glob('*.json'))) == 1


def test_query_of_committed_request_does_not_grant_permission_after_stop():
    allowed = [True]
    def handler(request):
        if request.method == 'POST':
            allowed[0] = False
            raise httpx.ReadError('synthetic')
        return receipt(request, 'committed', {'assignment': {'assignment_id': 'a'*32}})
    with pytest.raises(ApiError) as caught:
        client(handler).checkout(session_id='b'*32, retry_check=lambda: allowed[0])
    assert caught.value.write_outcome['status'] == 'committed'
    assert len(list((local_config.HOME/'pending_acquisitions').glob('*.json'))) == 1


def test_allocation_lock_does_not_block_flight_evidence(tmp_path):
    from dradar.flight_recorder import _exclusive_file_lock
    end = time.monotonic() + .2
    def bounded():
        assert time.monotonic() < end
    with acquisition_recovery._operation_lock(tmp_path/'allocation.lock', bounded):
        with _exclusive_file_lock(tmp_path/'flight.lock', check=bounded):
            bounded()


@pytest.mark.parametrize('plan_scoped', [False, True])
def test_claim_credentials_rotate_without_a_second_allocation(plan_scoped):
    identities, writes = [], []
    endpoint = '/api/v1/run-plans/identity' if plan_scoped else '/api/v1/whoami'
    principal = {'plan_id' if plan_scoped else 'volunteer_id': 'd'*32}
    def handler(request):
        if request.url.path == endpoint:
            identities.append(request.headers['authorization'])
            return httpx.Response(200, json=principal)
        if request.method == 'POST':
            writes.append(parse_qs(request.read().decode())['request_id'][0])
            raise httpx.ReadError('synthetic')
        if len(identities) == 1:
            raise httpx.ReadError('synthetic')
        assert request.url.path.endswith('/'+writes[0])
        return receipt(request, 'committed', {'assignment': {'assignment_id': 'a'*32}})
    prefix = 'drp_' if plan_scoped else 'drt_'
    for index in range(2):
        api = ApiClient('https://example.invalid', prefix+str(index),
                        transport=httpx.MockTransport(handler))
        if index == 0:
            with pytest.raises(ApiError):
                api.claim_assignment('t1', 'm', 'e')
        else:
            assert api.claim_assignment('t1', 'm', 'e')['assignment']['assignment_id'] == 'a'*32
    assert len(writes) == 1 and identities[0] != identities[1]


def test_unconfirmed_claim_identity_never_dispatches_allocation():
    seen = []
    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(503, json={})
    api = ApiClient('https://example.invalid', 'drt_synthetic', transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError, match='no allocation was dispatched'):
        api.claim_assignment('t1', 'm', 'e')
    assert seen == [('GET', '/api/v1/whoami')]
