"""Replay only the two telemetry protocols with existing durable identities."""
import asyncio
import copy
import json
import hashlib
import threading
import math
import random
import re
import time

import httpx

from .api_client import ApiError
from .write_wait_policy import headers as writer_headers, confirms_not_executed

REQUEST_SECONDS = 8.0
TOTAL_SECONDS = 35.0
MAX_ATTEMPTS = 2
_BUDGET_LOCK = threading.Lock()


def eligible_events(api, events):
    """Keep exhausted events locally while allowing newer evidence to flow."""
    now = time.monotonic()
    with _BUDGET_LOCK:
        budgets = getattr(api, '_telemetry_budgets', {})
        return [event for event in events
                if (state := budgets.get(('/api/v1/runner/flight-events', event.get('event_id')))) is None
                or (state['attempts'] < MAX_ATTEMPTS and state['deadline'] - now >= REQUEST_SECONDS)]


def retain_pending_event_budgets(api, pending):
    # The bounded durable recorder is authoritative for automatic replay.
    # Forget state only once an event has left that pending set.
    ids = {event.get('event_id') for event in pending}
    with _BUDGET_LOCK:
        budgets = getattr(api, '_telemetry_budgets', {})
        for key in list(budgets):
            if key[0] == '/api/v1/runner/flight-events' and key[1] not in ids:
                del budgets[key]


def _identity(path, body):
    if path == '/api/v1/runner/heartbeat':
        sid, seq = body.get('session_id'), body.get('seq')
        if isinstance(sid, str) and re.fullmatch(r'[0-9a-f]{32}', sid) and type(seq) is int and seq >= 0:
            return {'session_id': sid, 'seq': seq}
    elif path == '/api/v1/runner/flight-events':
        events = body.get('events')
        if isinstance(events, list) and events and all(
            isinstance(e, dict) and isinstance(e.get('event_id'), str)
            and re.fullmatch(r'[0-9a-f]{32}', e['event_id']) for e in events
        ):
            return {'event_ids': [e['event_id'] for e in events]}
    return None


async def replay(api, path, body):
    if path not in {'/api/v1/runner/heartbeat', '/api/v1/runner/flight-events'}:
        raise ValueError('unsupported telemetry replay endpoint')
    body = copy.deepcopy(body)
    identity = _identity(path, body)
    # A flight recorder may flush the same pending event again after every
    # heartbeat. Budget each identity, not each invocation or batch shape.
    now = time.monotonic()
    with _BUDGET_LOCK:
        budgets = getattr(api, '_telemetry_budgets', None)
        if budgets is None:
            budgets = api._telemetry_budgets = {}
        if path.endswith('heartbeat'):
            budgets = {}
        items = body['events'] if identity and 'event_ids' in identity else [body]
        states = []
        for item in items:
            key = ((path, item['event_id']) if identity and 'event_ids' in identity
                   else (path, body.get('session_id'), body.get('seq')))
            digest = hashlib.sha256(json.dumps(item, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            state = budgets.setdefault(key, {'deadline': now + TOTAL_SECONDS, 'attempts': 0,
                'digest': digest, 'uncertain': False, 'last_http_status': None,
                'failure_type': 'not_dispatched'})
            states.append((state, digest))
    deadline = min(state['deadline'] for state, _ in states)
    uncertain = any(state['uncertain'] for state, _ in states)
    outcome = ('busy_not_executed' if not uncertain and all(
        state['attempts'] > 0 and state['last_http_status'] == 503
        for state, _ in states) else 'unknown_unreconciled')
    last_http_status = states[0][0]['last_http_status']
    failure_type = states[0][0]['failure_type']
    def exhausted():
        error = ApiError('telemetry write remains unresolved', code='telemetry_recovery_incomplete',
                         status_code=last_http_status)
        error.retry_exhausted = True
        error.write_outcome = {'category': 'telemetry', 'phase': path.rsplit('/', 1)[1],
                               'request_identity': identity or 'unknown', 'status': outcome,
                               'local_result_retained': True, 'next_commands': ['dradar status --json'],
                               'last_http_status': last_http_status, 'failure_type': failure_type}
        print(json.dumps({'write_recovery': error.write_outcome}, sort_keys=True))
        return error
    if any(state['digest'] != digest for state, digest in states):
        raise exhausted()
    transport = api._explicit_transport
    if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
        raise exhausted()
    async with httpx.AsyncClient(base_url=api.server, headers=writer_headers(api),
                                cookies=api._client.cookies, timeout=REQUEST_SECONDS,
                                transport=transport, trust_env=True) as client:
        for attempt in range(MAX_ATTEMPTS):
            if deadline - time.monotonic() < REQUEST_SECONDS:
                raise exhausted()
            with _BUDGET_LOCK:
                if any(state['attempts'] >= MAX_ATTEMPTS for state, _ in states):
                    raise exhausted()
                for state, _ in states:
                    state['attempts'] += 1
            response = None
            last_http_status = None
            try:
                response = await asyncio.wait_for(client.post(path, json=body), REQUEST_SECONDS)
                last_http_status = response.status_code
                failure_type = 'http_error'
                if response.status_code < 500:
                    result = api._check(response)
                    valid = isinstance(result, dict) and (
                        (path.endswith('heartbeat') and type(result.get('accepted')) is bool)
                        or (path.endswith('flight-events')
                            and isinstance(result.get('acknowledged_event_ids'), list))
                    )
                    if valid:
                        return result
                    failure_type = 'response_invalid'
            except ApiError:
                if uncertain:
                    raise exhausted()
                raise
            except (httpx.TimeoutException, TimeoutError):
                failure_type = 'timeout'
            except httpx.HTTPError:
                failure_type = 'transport_error'
            except ValueError:
                failure_type = 'response_invalid'
            uncertain = uncertain or not confirms_not_executed(response)
            outcome = 'unknown_unreconciled' if uncertain else 'busy_not_executed'
            with _BUDGET_LOCK:
                for state, _ in states:
                    state.update(uncertain=uncertain, last_http_status=last_http_status,
                                 failure_type=failure_type)
            delay = 0.0
            if response is not None and response.status_code == 503:
                try:
                    envelope = response.json()
                except ValueError:
                    envelope = None
                if isinstance(envelope, dict) and envelope.get('code') == 'mutation_busy':
                    # Baseline flight ingestion can perform earlier maintenance;
                    # use only the original idempotent identity even for busy.
                    try:
                        delay = float(response.headers['Retry-After'])
                        declared = envelope['retry_after_seconds']
                        if (type(declared) not in (int, float) or not math.isfinite(delay)
                                or delay < 0 or delay != declared):
                            raise ValueError()
                    except (KeyError, TypeError, ValueError):
                        raise exhausted()
                    delay += random.uniform(0.0, min(1.0, delay * 0.1))
            if identity is None or attempt + 1 == MAX_ATTEMPTS:
                raise exhausted()
            if time.monotonic() + delay + REQUEST_SECONDS > deadline:
                raise exhausted()
            await asyncio.sleep(delay)
    raise AssertionError('unreachable')
