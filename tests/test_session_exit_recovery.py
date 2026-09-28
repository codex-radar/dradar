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
