"""Only an explicit zero-write promise can settle a bounded busy outcome."""
import httpx
import pytest
from dradar.api_client import ApiClient, ApiError


@pytest.mark.parametrize('kind', ['claim', 'stop', 'close', 'upload', 'progress', 'heartbeat', 'events'])
def test_known_busy_never_queries_an_unknown_receipt(kind):
    methods = []
    def handler(request):
        if request.url.path == '/api/v1/whoami':
            return httpx.Response(200, json={'volunteer_id': 'd'*32})
        methods.append(request.method)
        assert request.method == 'POST'
        return httpx.Response(503, headers={'Retry-After': '0.001'},
            json={'code': 'mutation_busy', 'retry_after_seconds': 0.001, 'write_outcome': 'not_executed'})
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    operations = {
        'claim': lambda: api.claim_assignment('t1', 'm', 'e'),
        'stop': lambda: api.mark_stopped('a'*32, session_id='b'*32),
        'close': lambda: api.runner_close({'session_id':'b'*32, 'batch_id':'c'*32, 'seq':1, 'reason':'error'}),
        'upload': lambda: api.register_submission_upload_intent('a'*32, 'nonce', 'b'*32, 1, 'c'*64),
        'progress': lambda: api.run_plan_progress('a'*32),
        'heartbeat': lambda: api.runner_heartbeat({'session_id':'b'*32, 'batch_id':'c'*32, 'seq':1}),
        'events': lambda: api.flight_events([{'event_id':'a'*32}]),
    }
    with pytest.raises(ApiError) as caught:
        operations[kind]()
    assert caught.value.write_outcome['status'] == 'busy_not_executed'
    assert methods == ['POST', 'POST']


@pytest.mark.parametrize('prior_unknown', [False, True])
def test_exhausted_event_reentry_preserves_certainty_without_more_posts(prior_unknown):
    requests = []
    def handler(request):
        requests.append(request)
        if prior_unknown and len(requests) == 1:
            raise httpx.ReadError('synthetic lost acknowledgement')
        return httpx.Response(503, headers={'Retry-After': '0.001'},
            json={'code': 'mutation_busy', 'retry_after_seconds': 0.001,
                  'write_outcome': 'not_executed'})
    api = ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))
    expected = 'unknown_unreconciled' if prior_unknown else 'busy_not_executed'
    for _ in range(2):
        with pytest.raises(ApiError) as caught:
            api.flight_events([{'event_id': 'a'*32}])
        assert caught.value.write_outcome['status'] == expected
    assert len(requests) == 2


def test_registration_json_preserves_valid_identity_and_strips_untrusted_fields():
    from dradar.run_plans import _api_error_response
    error = ApiError('registration busy')
    error.write_outcome = {'status': 'busy_not_executed', 'phase': 'started',
        'request_identity': {'assignment_id': 'a'*32, 'session_id': 'b'*32,
                             'worker_event_id': 'c'*32, 'token': 'must-not-leak'}}
    result = _api_error_response(error)
    assert result['write_outcome']['phase'] == 'started'
    assert result['write_outcome']['request_identity'] == {
        'assignment_id': 'a'*32, 'session_id': 'b'*32, 'worker_event_id': 'c'*32}
    assert 'must-not-leak' not in str(result)
