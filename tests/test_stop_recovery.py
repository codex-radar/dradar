import asyncio
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from dradar.api_client import ApiClient, ApiError
from dradar import stop_recovery
from dradar import local_config


def client(handler):
    return ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))


def receipt(data, status='committed'):
    result = {'schema_version': 1, 'status': status, 'request_id': data['request_id'][0],
              'assignment_id': data['assignment_id'][0], 'session_id': data['session_id'][0]}
    if status == 'committed':
        result['result'] = {'ok': True, 'defer_seconds': 300, 'retry_after': 'original'}
    else:
        result['retry_same_request_only'] = True
    return result


@pytest.mark.parametrize('fault', ['disconnect', 'malformed', 'server_error'])
def test_unknown_stop_reads_original_receipt_before_any_replay(fault):
    seen = []
    data = {}
    def handler(request):
        seen.append(request.method)
        if request.method == 'GET':
            return httpx.Response(200, json=receipt(data))
        data.update(parse_qs(request.read().decode(), keep_blank_values=True))
        if fault == 'disconnect':
            raise httpx.ReadError('synthetic lost ACK')
        if fault == 'malformed':
            return httpx.Response(200, content=b'{')
        return httpx.Response(500, json={'detail': 'synthetic'})
    result = client(handler).mark_stopped('a'*32, session_id='b'*32, owner_epoch=1)
    assert result['retry_after'] == 'original'
    assert seen == ['POST', 'GET']


def test_supported_pending_receipt_replays_identical_body():
    seen, data = [], {}
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json=receipt(data, 'unknown'))
        body = request.read()
        seen.append(body)
        data.update(parse_qs(body.decode(), keep_blank_values=True))
        return httpx.Response(500 if len(seen) == 1 else 200, json={'ok': True})
    assert client(handler).mark_stopped('a'*32, session_id='b'*32)['ok']
    assert len(seen) == 2 and seen[0] == seen[1]


def test_old_server_without_receipt_does_not_blindly_repeat_stop():
    seen = []
    def handler(request):
        seen.append(request.method)
        return httpx.Response(500 if request.method == 'POST' else 404, json={})
    with pytest.raises(ApiError) as caught:
        client(handler).mark_stopped('a'*32)
    assert seen == ['POST', 'GET']
    assert caught.value.retry_exhausted
    assert caught.value.write_outcome['status'] == 'unknown_unreconciled'


@pytest.mark.parametrize('second_status', [403, 503])
def test_later_refusal_never_erases_an_earlier_unknown(second_status):
    seen, data = [], {}
    def handler(request):
        seen.append(request.method)
        if request.method == 'GET':
            return httpx.Response(200, json=receipt(data, 'unknown'))
        data.update(parse_qs(request.read().decode(), keep_blank_values=True))
        if seen.count('POST') == 1:
            raise httpx.ReadError('synthetic lost receipt')
        return httpx.Response(second_status, headers={'Retry-After': '3'},
                             json={'code': 'mutation_busy' if second_status == 503 else 'stopped',
                                   'retry_after_seconds': 3})
    with pytest.raises(ApiError) as caught:
        client(handler).mark_stopped('a'*32)
    assert seen == ['POST', 'GET', 'POST', 'GET']
    assert caught.value.write_outcome['status'] == 'unknown_unreconciled'
    assert caught.value.write_outcome['reconciled'] is False


def test_new_client_reconciles_persisted_stop_before_any_write():
    data = {}
    def lost(request):
        if request.method == 'POST':
            data.update(parse_qs(request.read().decode(), keep_blank_values=True))
        raise httpx.ReadError('synthetic')
    with pytest.raises(ApiError):
        client(lost).mark_stopped('a'*32, session_id='b'*32)
    paths = list((local_config.HOME/'pending_stops').glob('*.json'))
    assert len(paths) == 1
    original = json.loads(paths[0].read_text())['body']['request_id']
    assert original == data['request_id'][0]
    def recovered(request):
        assert request.method == 'GET' and request.url.path.endswith('/'+original)
        return httpx.Response(200, json=receipt(data))
    assert client(recovered).mark_stopped('a'*32, session_id='b'*32)['ok'] is True
    assert not paths[0].exists()


def test_saved_stop_with_changed_payload_only_queries_original():
    data, calls = {}, []
    def handler(request):
        calls.append(request.method)
        if request.method == 'POST':
            data.update(parse_qs(request.read().decode(), keep_blank_values=True))
            raise httpx.ReadError('synthetic')
        return httpx.Response(200, json=receipt(data, 'unknown'))
    with pytest.raises(ApiError):
        client(handler).mark_stopped('a'*32, session_id='b'*32)
    calls.clear()
    with pytest.raises(ApiError):
        client(handler).mark_stopped('a'*32, session_id='b'*32, defer_seconds=900)
    assert calls == ['GET']


def test_busy_waits_and_outer_cleanup_cannot_restart_budget(monkeypatch, capsys):
    from dradar import runloop
    monkeypatch.setattr(stop_recovery.random, 'uniform', lambda a, b: b)
    times, bodies = [], []
    def handler(request):
        times.append(time.monotonic())
        bodies.append(request.read())
        return httpx.Response(503, headers={'Retry-After': '0.01'},
                             json={'code': 'mutation_busy', 'write_outcome': 'not_executed', 'retry_after_seconds': 0.01})
    assert not runloop._mark_stopped_quietly(client(handler), {'assignment_id': 'a'*32})
    assert len(times) == 2 and bodies[0] == bodies[1]
    assert times[1] - times[0] >= 0.011
    assert 'busy_not_executed' in capsys.readouterr().out


def test_total_attempt_timeout_includes_wait_for_response(monkeypatch):
    monkeypatch.setattr(stop_recovery, 'REQUEST_SECONDS', 0.05)
    monkeypatch.setattr(stop_recovery, 'TOTAL_SECONDS', 0.3)
    data = {}
    async def handler(request):
        if request.method == 'POST':
            data.update(parse_qs((await request.aread()).decode(), keep_blank_values=True))
            await asyncio.sleep(1)
        return httpx.Response(200, json=receipt(data))
    ready = client(handler)
    began = time.monotonic()
    assert ready.mark_stopped('a'*32)['ok']
    assert time.monotonic() - began < 0.3


def test_retry_after_is_not_clamped_or_shortened():
    seen = []
    def handler(request):
        seen.append(request.method)
        return httpx.Response(503, headers={'Retry-After': '60'},
                             json={'code': 'mutation_busy', 'write_outcome': 'not_executed', 'retry_after_seconds': 60})
    with pytest.raises(ApiError) as caught:
        client(handler).mark_stopped('a'*32)
    assert seen == ['POST']
    assert caught.value.write_outcome['status'] == 'busy_not_executed'


def test_known_busy_stays_known_across_new_clients(monkeypatch):
    monkeypatch.setattr(stop_recovery.random, 'uniform', lambda a, b: 0)
    calls = []
    def busy(request):
        calls.append(request.method)
        return httpx.Response(503, headers={'Retry-After': '0.01'},
            json={'code': 'mutation_busy', 'write_outcome': 'not_executed', 'retry_after_seconds': 0.01})
    for _ in range(5):
        with pytest.raises(ApiError) as caught:
            client(busy).mark_stopped('a'*32, session_id='b'*32)
        assert caught.value.write_outcome['status'] == 'busy_not_executed'
    assert calls == ['POST', 'POST']


def test_new_clients_share_original_stop_budget():
    data, bodies, reads = {}, [], []
    def handler(request):
        if request.method == 'GET':
            reads.append(str(request.url))
            return httpx.Response(200, json=receipt(data, 'unknown'))
        bodies.append(request.read())
        data.update(parse_qs(bodies[-1].decode(), keep_blank_values=True))
        raise httpx.ReadError('synthetic lost response')
    for _ in range(5):
        with pytest.raises(ApiError):
            client(handler).mark_stopped('a'*32, session_id='b'*32)
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert len(reads) == 3 and len(set(reads)) == 1


def test_reboot_cannot_reopen_expired_saved_budget():
    def lost(request):
        raise httpx.ReadError('synthetic')
    with pytest.raises(ApiError):
        client(lost).mark_stopped('a'*32, session_id='b'*32)
    path = next((local_config.HOME / 'pending_stops').glob('*.json'))
    saved = json.loads(path.read_text())
    saved['deadline'] = time.monotonic() + 10000  # older boot's clock value
    saved['wall_deadline'] = time.time() - 1
    path.write_text(json.dumps(saved))
    calls = []
    def unexpectedly_reopened(request):
        calls.append(request.method)
        return httpx.Response(500)
    with pytest.raises(ApiError):
        client(unexpectedly_reopened).mark_stopped('a'*32, session_id='b'*32)
    assert not calls
    assert json.loads(path.read_text())['body']['request_id'] == saved['body']['request_id']
