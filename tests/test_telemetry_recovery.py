import json
import time

import httpx
import pytest

from dradar.api_client import ApiClient, ApiError


@pytest.mark.parametrize('kind', ['heartbeat', 'events'])
@pytest.mark.parametrize('fault', ['busy', 'disconnect', 'malformed', 'server_error'])
def test_telemetry_replays_one_frozen_identity(kind, fault):
    body = ({'session_id': 'a'*32, 'seq': 7} if kind == 'heartbeat'
            else [{'event_id': 'b'*32}])
    seen, times = [], []
    def handler(request):
        seen.append(json.loads(request.read()))
        times.append(time.monotonic())
        if len(seen) == 1:
            if kind == 'heartbeat':
                body['seq'] = 8
            else:
                body[0]['event_id'] = 'c'*32
            if fault == 'busy':
                return httpx.Response(503, headers={'Retry-After': '0.01'},
                                     json={'code': 'mutation_busy', 'retry_after_seconds': 0.01})
            if fault == 'disconnect':
                raise httpx.ReadError('synthetic')
            if fault == 'malformed':
                return httpx.Response(200, content=b'{')
            return httpx.Response(500, json={})
        return httpx.Response(200, json={'accepted': True} if kind == 'heartbeat'
                              else {'acknowledged_event_ids': ['b'*32]})
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    (api.runner_heartbeat if kind == 'heartbeat' else api.flight_events)(body)
    assert len(seen) == 2 and seen[0] == seen[1]
    if fault == 'busy':
        assert times[1] - times[0] >= 0.01


@pytest.mark.parametrize('status', [403, 409, 429])
def test_http_refusal_does_not_start_another_retry_loop(status):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={'detail': 'refused'})
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as caught:
        api.runner_heartbeat({'session_id': 'a'*32, 'seq': 7})
    assert caught.value.status_code == status and len(seen) == 1


def test_refusal_after_lost_ack_reports_original_uncertainty(capsys):
    seen = []
    def handler(request):
        seen.append(request.read())
        if len(seen) == 1:
            raise httpx.ReadError('synthetic lost acknowledgement')
        return httpx.Response(403, json={'detail': 'stopped'})
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as caught:
        api.runner_heartbeat({'session_id': 'a'*32, 'seq': 7})
    assert len(seen) == 2 and seen[0] == seen[1]
    assert caught.value.write_outcome['status'] == 'unknown_unreconciled'
    assert 'unknown_unreconciled' in capsys.readouterr().out


def test_retry_budget_and_invalid_identity_do_not_leak_payloads():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(500, json={})
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as caught:
        api.runner_heartbeat({'session_id': 'unsafe-private-value', 'seq': 7})
    assert len(seen) == 1
    assert caught.value.write_outcome['request_identity'] == 'unknown'
    assert 'unsafe-private-value' not in json.dumps(caught.value.write_outcome)
    seen.clear()
    with pytest.raises(ApiError) as caught:
        api.runner_heartbeat({'session_id': 'a'*32, 'seq': 7})
    assert len(seen) == 2 and caught.value.retry_exhausted


@pytest.mark.parametrize('kind', ['heartbeat', 'events'])
@pytest.mark.parametrize('fault', ['busy', 'disconnect', 'timeout'])
def test_exhaustion_preserves_failure_semantics_and_identity(kind, fault, capsys):
    seen = []
    def handler(request):
        seen.append(request.read())
        if fault == 'disconnect':
            raise httpx.ReadError('private transport text')
        if fault == 'timeout':
            raise httpx.ReadTimeout('private transport text')
        return httpx.Response(503, headers={'Retry-After': '.001'},
                             json={'code': 'mutation_busy', 'retry_after_seconds': .001})
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as caught:
        if kind == 'heartbeat':
            api.runner_heartbeat({'session_id': 'a'*32, 'seq': 7})
        else:
            api.flight_events([{'event_id': 'b'*32}])
    assert len(seen) == 2 and seen[0] == seen[1]
    expected_status = 503 if fault == 'busy' else None
    assert caught.value.status_code == expected_status
    assert caught.value.write_outcome['last_http_status'] == expected_status
    assert caught.value.write_outcome['failure_type'] == {
        'busy': 'http_error', 'disconnect': 'transport_error', 'timeout': 'timeout',
    }[fault]
    assert caught.value.write_outcome['status'] == 'unknown_unreconciled'
    assert 'private transport text' not in capsys.readouterr().out


def test_repeated_flight_flush_cannot_reset_one_events_budget(tmp_path):
    from dradar.flight_recorder import FlightRecorder
    requests = []
    def handler(request):
        requests.append(json.loads(request.read()))
        raise httpx.ReadError('synthetic')
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    recorder = FlightRecorder(tmp_path, client=api)
    event = recorder.record('phase_changed', component='heartbeat', batch_id='a'*32,
                            session_id='b'*32, attributes={'previous_phase': 'preparing', 'phase': 'building'})
    assert recorder.flush(batch_id='a'*32, session_id='b'*32) == 0
    assert recorder.flush(batch_id='a'*32, session_id='b'*32) == 0
    # A changed batch must not grant the old event two more writes either.
    recorder.record('phase_changed', component='heartbeat', batch_id='a'*32,
                    session_id='b'*32, attributes={'previous_phase': 'building', 'phase': 'running'})
    assert recorder.flush(batch_id='a'*32, session_id='b'*32) == 0
    assert len(requests) == 4
    assert all(event['event_id'] in [e['event_id'] for e in r['events']] for r in requests[:2])
    assert all(event['event_id'] not in [e['event_id'] for e in r['events']] for r in requests[2:])
    assert event['event_id'] in {e['event_id'] for e in recorder._load(recorder.pending_path)}


def test_new_client_and_recorder_cannot_reopen_persisted_event_budget(tmp_path):
    from dradar.flight_recorder import FlightRecorder
    requests = []
    def handler(request):
        requests.append(json.loads(request.read()))
        raise httpx.ReadError('synthetic')
    def recorder():
        return FlightRecorder(tmp_path, client=ApiClient('https://example.invalid', 'synthetic',
            transport=httpx.MockTransport(handler)))
    original = recorder()
    event = original.record('phase_changed', component='heartbeat', batch_id='a'*32,
        session_id='b'*32, attributes={'previous_phase': 'preparing', 'phase': 'building'})
    assert original.flush(batch_id='a'*32, session_id='b'*32) == 0
    replacement = recorder()
    assert replacement.flush(batch_id='a'*32, session_id='b'*32) == 0
    assert len(requests) == 2
    assert event['event_id'] in {item['event_id'] for item in replacement._load(replacement.pending_path)}
    fresh = replacement.record('phase_changed', component='heartbeat', batch_id='a'*32,
        session_id='b'*32, attributes={'previous_phase': 'building', 'phase': 'running'})
    assert replacement.flush(batch_id='a'*32, session_id='b'*32) == 0
    assert len(requests) == 4
    assert [item['event_id'] for item in requests[-1]['events']] == [fresh['event_id']]


def test_unreadable_event_budget_keeps_pending_and_does_not_send(tmp_path):
    from dradar.flight_recorder import FlightRecorder
    def handler(request):
        pytest.fail('unreadable budget must not issue a new request')
    recorder = FlightRecorder(tmp_path, client=ApiClient('https://example.invalid', 'synthetic',
        transport=httpx.MockTransport(handler)))
    event = recorder.record('phase_changed', component='heartbeat', batch_id='a'*32,
        session_id='b'*32, attributes={'previous_phase': 'preparing', 'phase': 'building'})
    recorder.retry_reservations_path.write_text('{')
    assert recorder.flush(batch_id='a'*32, session_id='b'*32) == 0
    assert event['event_id'] in {item['event_id'] for item in recorder._load(recorder.pending_path)}
