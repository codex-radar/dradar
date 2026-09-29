import asyncio
import hashlib
import json
import time

import httpx
import pytest

from dradar.api_client import ApiClient, ApiError
from dradar import session_exit_recovery


def api(handler):
    return ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))


@pytest.mark.parametrize('operation', ['close', 'release-capacity'])
@pytest.mark.parametrize('fault', ['disconnect', 'malformed', '500'])
def test_exit_reconciles_exact_receipt_without_repeating(operation, fault):
    body = {'session_id': 'a'*32, 'batch_id': 'b'*32, 'seq': 5, 'reason': 'completed'}
    if operation == 'release-capacity':
        body = {'session_id': 'a'*32, 'batch_id': 'b'*32,
                'device_generation': 2, 'evidence_id': 'c'*32}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == 'GET':
            return httpx.Response(200, json={'session_id': body['session_id'], 'batch_id': body['batch_id'],
                'closed': True, 'capacity_released': operation == 'release-capacity', 'device_generation': 2,
                'release_evidence_id': 'c'*32, 'release_evidence_sha256': digest})
        if fault == 'disconnect':
            raise httpx.ReadError('synthetic lost receipt')
        if fault == 'malformed':
            return httpx.Response(200, content=b'{')
        return httpx.Response(500)
    client = api(handler)
    result = asyncio.run(session_exit_recovery.recover(client, '/api/v1/runner/'+operation, body))
    assert result['ok'] is True and calls == ['POST', 'GET']
    assert asyncio.run(session_exit_recovery.recover(client, '/api/v1/runner/'+operation, body)) == result
    assert calls == ['POST', 'GET']


def test_outer_cleanup_cannot_restart_attempt_count(monkeypatch, capsys):
    monkeypatch.setattr(session_exit_recovery.random, 'uniform', lambda a, b: 0)
    bodies, timestamps = [], []
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(404)
        bodies.append(request.read())
        timestamps.append(time.monotonic())
        return httpx.Response(503, headers={'Retry-After': '.01'},
                             json={'code': 'mutation_busy', 'write_outcome': 'not_executed', 'retry_after_seconds': .01})
    body = {'session_id': 'a'*32, 'batch_id': 'b'*32, 'seq': 2, 'reason': 'error'}
    for _ in range(5):
        with pytest.raises(ApiError):
            api(handler).runner_close(body)
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert timestamps[1] - timestamps[0] >= .01
    assert 'busy_not_executed' in capsys.readouterr().out


def test_explicit_signed_replay_uses_same_close_body_once(monkeypatch, tmp_path):
    monkeypatch.setattr(session_exit_recovery.local_config, 'HOME', tmp_path)
    monkeypatch.setattr(session_exit_recovery.random, 'uniform', lambda a, b: 0)
    body = {'session_id': 'a'*32, 'batch_id': 'b'*32,
            'seq': 18, 'reason': 'paused'}
    requests = []
    permit = {'value': False}
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={
                'session_id': body['session_id'], 'batch_id': body['batch_id'],
                'closed': False, 'capacity_released': False,
                'device_generation': 3,
            })
        requests.append(json.loads(request.read()))
        if permit['value']:
            return httpx.Response(200, json={'ok': True, 'closed': True,
                                              'capacity_released': False})
        return httpx.Response(503, headers={'Retry-After': '.01'},
                             json={'code': 'mutation_busy',
                                   'write_outcome': 'not_executed',
                                   'retry_after_seconds': .01})
    client = api(handler)
    for _ in range(2):
        with pytest.raises(ApiError):
            client.runner_close(body)
    assert len(requests) == 2 and requests == [body, body]
    journal = next((tmp_path / 'pending_session_exits').glob('*.json'))
    state = json.loads(journal.read_text())
    state.update(receipt_reads=3, uncertain=True, last_busy=False)
    session_exit_recovery._save(journal, state)
    permit['value'] = True
    with pytest.raises(ApiError):
        client.runner_close(body)
    assert len(requests) == 2
    assert client.runner_close(body, explicit_replay_once=True)['closed'] is True
    assert requests == [body, body, body]
    assert client.runner_close(body, explicit_replay_once=True)['closed'] is True
    assert len(requests) == 3
    saved = json.loads(journal.read_text())
    assert saved['explicit_replay_prior']['attempts'] == 2
    assert saved['explicit_replay_prior']['receipt_reads'] == 3


def test_explicit_replay_failure_cannot_obtain_second_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(session_exit_recovery.local_config, 'HOME', tmp_path)
    monkeypatch.setattr(session_exit_recovery.random, 'uniform', lambda a, b: 0)
    body = {'session_id': 'a'*32, 'batch_id': 'b'*32,
            'seq': 18, 'reason': 'paused'}
    posts = []
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={
                'session_id': body['session_id'], 'batch_id': body['batch_id'],
                'closed': False, 'capacity_released': False,
                'device_generation': 3,
            })
        posts.append(json.loads(request.read()))
        return httpx.Response(503, headers={'Retry-After': '.01'},
                             json={'code': 'mutation_busy',
                                   'write_outcome': 'not_executed',
                                   'retry_after_seconds': .01})
    client = api(handler)
    with pytest.raises(ApiError):
        client.runner_close(body)
    assert len(posts) == 2
    with pytest.raises(ApiError):
        client.runner_close(body, explicit_replay_once=True)
    assert len(posts) == 3
    for _ in range(2):
        with pytest.raises(ApiError):
            client.runner_close(body, explicit_replay_once=True)
    assert posts == [body, body, body]
    journal = next((tmp_path / 'pending_session_exits').glob('*.json'))
    state = json.loads(journal.read_text())
    assert state['explicit_replay_rounds'] == 1
    assert state['explicit_replay_prior']['attempts'] == 2
    assert state['explicit_replay_prior']['receipt_reads'] == 0
    with pytest.raises(ApiError, match='differs'):
        client.runner_close({**body, 'seq': 19}, explicit_replay_once=True)
    assert len(posts) == 3


def test_release_receipt_with_other_evidence_never_confirms_success():
    def handler(request):
        if request.method == 'POST':
            raise httpx.ReadError('synthetic')
        return httpx.Response(200, json={'session_id': 'a'*32, 'batch_id': 'b'*32,
            'closed': True, 'capacity_released': True, 'device_generation': 2,
            'release_evidence_id': 'd'*32, 'release_evidence_sha256': 'e'*64})
    with pytest.raises(ApiError) as caught:
        asyncio.run(session_exit_recovery.recover(api(handler), '/api/v1/runner/release-capacity',
            {'session_id': 'a'*32, 'batch_id': 'b'*32, 'device_generation': 2, 'evidence_id': 'c'*32}))
    assert caught.value.write_outcome['status'] == 'unknown_unreconciled'


def test_unvalidated_identity_is_never_printed(capsys):
    def handler(request):
        raise httpx.ReadError('synthetic')
    with pytest.raises(ApiError):
        api(handler).runner_close({'session_id': 'private-value', 'batch_id': 'private-batch',
                                   'seq': 'private-seq', 'reason': 'error'})
    output = capsys.readouterr().out
    assert 'private-' not in output
    assert 'unknown' in output
