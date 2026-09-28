"""Bounded start/stop recovery using the existing exact intent receipt."""
import asyncio
import copy
import json
import math
import random
import time

import httpx

from . import cancellation
from .api_client import ApiError
from .write_wait_policy import headers as writer_headers, confirms_not_executed

REQUEST_SECONDS = 120.0
RECEIPT_SECONDS = 3.0
TOTAL_SECONDS = 250.0
RETRY_SECONDS = 130.0
MAX_ATTEMPTS = 2


async def recover(api, operation, payload):
    from .plan_intents import fingerprint, _validate_receipt
    if operation not in {'start', 'stop'}:
        raise ValueError('unsupported plan intent')
    payload = copy.deepcopy(payload)
    rid = payload['intent_id']
    expected = fingerprint(operation, payload)
    record = {'operation': operation, 'request': payload, 'request_fingerprint': expected}
    budgets = getattr(api, '_plan_intent_budgets', None)
    if budgets is None:
        budgets = api._plan_intent_budgets = {}
    state = budgets.setdefault((operation, rid), {'deadline': time.monotonic() + TOTAL_SECONDS,
        'attempts': 0, 'fingerprint': expected, 'uncertain': False})
    def unresolved():
        error = ApiError('plan intent remains saved for exact reconciliation', code='plan_intent_unresolved')
        error.retry_exhausted = True
        error.write_outcome = {'phase': 'plan_' + operation, 'category': 'start' if operation == 'start' else 'exit',
            'request_id': rid, 'status': 'busy_not_executed' if state.get('known_busy') and not state['uncertain'] else 'unknown_unreconciled', 'local_result_retained': True,
            'next_commands': ['dradar progress --plan <saved-plan-code>']}
        print(json.dumps({'write_recovery': error.write_outcome}, sort_keys=True))
        return error
    if state['fingerprint'] != expected:
        raise unresolved()
    transport = api._explicit_transport
    if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
        raise unresolved()
    async with httpx.AsyncClient(base_url=api.server, headers=writer_headers(api),
                                cookies=api._client.cookies, transport=transport,
                                timeout=REQUEST_SECONDS, trust_env=True) as client:
        async def receipt():
            remaining = state['deadline'] - time.monotonic()
            if remaining <= 0:
                return None, False
            try:
                response = await asyncio.wait_for(client.get('/api/v1/run-plans/intents/' + rid,
                    params={'plan_id': payload['plan_id'], 'expected_fingerprint': expected}),
                    min(RECEIPT_SECONDS, remaining))
                body = response.json()
                if (response.status_code == 404 and isinstance(body, dict)
                        and body.get('intent_id') == rid and body.get('intent_status') == 'unknown'
                        and body.get('retry_same_intent_only') is True):
                    return None, True
                if (isinstance(body, dict) and body.get('intent_id') == rid
                        and body.get('plan_id') == payload['plan_id']
                        and body.get('operation') == operation and body.get('request_fingerprint') == expected
                        and body.get('intent_status') in {'applied', 'rejected', 'decision_required'}):
                    # The durable caller validates the complete receipt and
                    # current_effective before any provider may be launched.
                    if response.status_code in {200, 409}:
                        return _validate_receipt(body, record, replay=True), True
            except (httpx.HTTPError, TimeoutError, ValueError, ApiError):
                pass
            return None, False
        if state['uncertain']:
            result, supported = await receipt()
            if result is not None:
                return result
            if not supported:
                raise unresolved()
        while state['attempts'] < MAX_ATTEMPTS:
            if (operation == 'start' and cancellation.requested()) or (
                    state['deadline'] - time.monotonic() < REQUEST_SECONDS + RECEIPT_SECONDS):
                raise unresolved()
            state['attempts'] += 1
            response = None
            try:
                response = await asyncio.wait_for(client.post('/api/v1/run-plans/' + operation, json=payload), REQUEST_SECONDS)
                if response.status_code < 500:
                    result = api._check(response)
                    return _validate_receipt(result, record)
            except ApiError as exc:
                if isinstance(exc.payload, dict) and 'intent_status' in exc.payload:
                    raise
                if not state['uncertain'] and exc.code != 'intent_receipt_invalid':
                    raise
            except (httpx.HTTPError, TimeoutError, ValueError):
                pass
            state['known_busy'] = confirms_not_executed(response)
            state['uncertain'] = state['uncertain'] or not state['known_busy']
            state['deadline'] = min(state['deadline'],
                state.setdefault('retry_deadline', time.monotonic() + RETRY_SECONDS))
            result, supported = await receipt() if state['uncertain'] else (None, True)
            if result is not None:
                return result
            if not supported:
                raise unresolved()
            delay = 0.0
            if response is not None and response.status_code == 503:
                try:
                    body = response.json()
                    if isinstance(body, dict) and body.get('code') == 'mutation_busy':
                        delay = float(response.headers['Retry-After'])
                        declared = body['retry_after_seconds']
                        if type(declared) not in (int, float) or not math.isfinite(delay) or delay < 0 or delay != declared:
                            raise ValueError()
                        delay += random.uniform(0.0, min(1.0, delay * .1))
                except (KeyError, TypeError, ValueError):
                    raise unresolved()
            if (state['attempts'] >= MAX_ATTEMPTS
                    or time.monotonic() + delay + REQUEST_SECONDS + RECEIPT_SECONDS > state['deadline']):
                raise unresolved()
            until = time.monotonic() + delay
            while time.monotonic() < until:
                if operation == 'start' and cancellation.requested():
                    raise unresolved()
                await asyncio.sleep(min(.025, max(0, until - time.monotonic())))
        raise unresolved()
