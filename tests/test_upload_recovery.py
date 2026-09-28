from pathlib import Path
import time
from urllib.parse import parse_qs

import httpx
import pytest

from dradar.api_client import ApiClient, ApiError


@pytest.mark.parametrize('kind', ['intent', 'submission'])
@pytest.mark.parametrize('fault', ['busy', 'disconnect', 'malformed', 'server_error'])
def test_upload_keeps_original_intent_and_bytes(tmp_path, kind, fault):
    patch = tmp_path / 'model.patch'
    patch.write_bytes(b'original synthetic patch')
    bodies, times = [], []
    intent_id = 'a'*64
    def handler(request):
        bodies.append(request.read())
        times.append(time.monotonic())
        if len(bodies) == 1:
            patch.write_bytes(b'changed after first request')
            if fault == 'busy':
                return httpx.Response(503, headers={'Retry-After': '0.01'},
                                     json={'code': 'mutation_busy', 'retry_after_seconds': 0.01})
            if fault == 'disconnect':
                raise httpx.ReadError('synthetic')
            if fault == 'malformed':
                return httpx.Response(200, content=b'{')
            return httpx.Response(500, json={})
        return httpx.Response(200, json=({'ok': True, 'upload_intent_id': intent_id}
                              if kind == 'intent' else {'submission_id': 'b'*32, 'grade_status': 'pending'}))
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    if kind == 'intent':
        assert api.register_submission_upload_intent('b'*32, 'nonce', 'c'*32, 1, intent_id) == intent_id
        assert bodies[0] == bodies[1]
    else:
        assert api.submit('b'*32, 'nonce', patch, None, None, {}, upload_intent_id=intent_id)['submission_id']
        # Multipart boundary bytes may differ, but all content remains bound
        # to the same intent and the original bytes materialized by submit.
        assert all(intent_id.encode() in body and b'original synthetic patch' in body for body in bodies)
        assert all(b'changed after first request' not in body for body in bodies)
    assert len(bodies) == 2 and patch.exists()
    if fault == 'busy':
        assert times[1] - times[0] >= 0.01


def test_unresolved_upload_reports_recovery_and_keeps_file(tmp_path):
    patch = tmp_path / 'model.patch'
    patch.write_bytes(b'synthetic retained result')
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={})
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as caught:
        api.submit('b'*32, 'nonce', patch, None, None, {}, upload_intent_id='a'*64)
    assert len(calls) == 2
    assert caught.value.write_outcome['request_id'] == 'a'*64
    assert caught.value.write_outcome['next_commands'] == ['dradar retry-upload']
    assert patch.read_bytes() == b'synthetic retained result'


def test_legacy_unbound_upload_does_not_gain_blind_retries(tmp_path):
    patch = tmp_path / 'model.patch'
    patch.write_bytes(b'synthetic')
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={})
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError):
        api.submit('b'*32, 'nonce', patch, None, None, {})
    assert len(calls) == 1 and patch.exists()


def test_refusal_after_unknown_upload_keeps_result_and_reports_uncertainty(tmp_path, capsys):
    patch = tmp_path/'model.patch'
    patch.write_bytes(b'synthetic result')
    calls = []
    def handler(request):
        calls.append(request.read())
        if len(calls) == 1:
            raise httpx.ReadError('synthetic lost acknowledgement')
        return httpx.Response(403, json={'detail': 'stopped'})
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as caught:
        api.submit('b'*32, 'nonce', patch, None, None, {}, upload_intent_id='a'*64)
    assert len(calls) == 2 and patch.read_bytes() == b'synthetic result'
    assert caught.value.write_outcome['status'] == 'unknown_unreconciled'
    assert 'unknown_unreconciled' in capsys.readouterr().out


def test_unknown_then_maintenance_cannot_reopen_automatic_upload_budget(tmp_path):
    patch = tmp_path/'model.patch'
    patch.write_bytes(b'synthetic')
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadError('synthetic')
        return httpx.Response(503, headers={'Retry-After': '1'}, json={
            'code': 'deployment_maintenance', 'detail': 'maintenance', 'retry_after_seconds': 1})
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as caught:
        api.submit('b'*32, 'nonce', patch, None, None, {}, upload_intent_id='a'*64)
    assert caught.value.write_outcome['status'] == 'unknown_unreconciled'
    assert len(calls) == 2
    with pytest.raises(ApiError):
        api.submit('b'*32, 'nonce', patch, None, None, {}, upload_intent_id='a'*64)
    assert len(calls) == 2 and patch.read_bytes() == b'synthetic'


def test_retry_after_cannot_extend_upload_recovery_tail(tmp_path):
    patch = tmp_path/'model.patch'
    patch.write_bytes(b'synthetic original')
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={'Retry-After': '15'}, json={
            'code': 'mutation_busy', 'retry_after_seconds': 15,
            'write_outcome': 'not_executed'})
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    # A 15-second delay plus a full 120-second attempt cannot fit the
    # 130-second recovery tail, even though maintenance has 360 seconds.
    with pytest.raises(ApiError) as caught:
        api.submit('b'*32, 'nonce', patch, None, None, {}, upload_intent_id='a'*64)
    assert caught.value.write_outcome['status'] == 'busy_not_executed'
    assert len(calls) == 1 and patch.read_bytes() == b'synthetic original'
