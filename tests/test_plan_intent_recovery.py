import json
import time

import httpx
import pytest

from dradar import plan_intents, cancellation
from dradar.api_client import ApiClient, ApiError
from test_plan_intents import START, STOP, receipt, files


@pytest.mark.parametrize('operation', ['start', 'stop'])
@pytest.mark.parametrize('fault', ['busy', 'lost', 'malformed'])
def test_plan_intent_reconciles_original_identity_before_retry(tmp_path, operation, fault):
    posts, sent_at, methods = [], [], []
    def handler(request):
        methods.append(request.method)
        if request.method == 'GET':
            body = receipt(operation, posts[0], replay=True)
            if fault == 'busy':
                return httpx.Response(404, json={'schema_version': 1, 'intent_id': posts[0]['intent_id'],
                    'intent_status': 'unknown', 'applied': None, 'retry_same_intent_only': True})
            return httpx.Response(200, json=body)
        posts.append(json.loads(request.read()));sent_at.append(time.monotonic())
        if len(posts) == 1:
            if fault == 'busy':
                return httpx.Response(503, headers={'Retry-After': '.01'},
                    json={'code': 'mutation_busy', 'retry_after_seconds': .01})
            if fault == 'lost':
                raise httpx.ReadError('synthetic')
            return httpx.Response(200, json={'intent_id': posts[0]['intent_id']})
        return httpx.Response(200, json=receipt(operation, posts[-1]))
    api = ApiClient('https://example.invalid', 'drp_synthetic', transport=httpx.MockTransport(handler))
    result = plan_intents.execute(tmp_path, api, operation=operation, request=START if operation == 'start' else STOP,
                                  expected_revision=7, local_intent='synthetic-local-action')
    assert result['intent_status'] == 'applied'
    saved = json.loads(files(tmp_path)[0].read_text())
    assert saved['status'] == 'received'
    assert methods[:2] == ['POST', 'GET']
    if fault == 'busy':
        assert posts[0] == posts[1] and len(posts) == 2
        assert sent_at[1] - sent_at[0] >= .01
    else:
        assert len(posts) == 1


def test_start_cancelled_during_reconciliation_never_posts_again(tmp_path, monkeypatch):
    posts = []
    cancelled = [False]
    monkeypatch.setattr(cancellation, 'requested', lambda: cancelled[0])
    def handler(request):
        if request.method == 'POST':
            posts.append(json.loads(request.read()))
            return httpx.Response(503, headers={'Retry-After': '.01'},
                                  json={'code': 'mutation_busy', 'retry_after_seconds': .01})
        cancelled[0] = True
        return httpx.Response(404, json={'schema_version': 1, 'intent_id': posts[0]['intent_id'],
            'intent_status': 'unknown', 'applied': None, 'retry_same_intent_only': True})
    api = ApiClient('https://example.invalid', 'drp_synthetic', transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as caught:
        plan_intents.execute(tmp_path, api, operation='start', request=START, expected_revision=7,
                             local_intent='synthetic-local-action')
    assert len(posts) == 1
    assert caught.value.write_outcome['status'] == 'unknown_unreconciled'
    assert json.loads(files(tmp_path)[0].read_text())['status'] == 'pending'
