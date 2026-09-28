"""Bounded reconciliation of logical close and exact physical-exit evidence."""
import asyncio
import copy
import hashlib
import json
import math
import random
import re
import time
from urllib.parse import quote

import httpx

from .api_client import ApiError
from .write_wait_policy import headers as writer_headers, confirms_not_executed
from . import local_config
from .acquisition_recovery import _operation_lock, _save, _saved_deadline

REQUEST_SECONDS = 8.0
TOTAL_SECONDS = 35.0
MAX_ATTEMPTS = 2
MAX_RECEIPT_READS = 3


async def recover(api, path, payload):
    if path not in {'/api/v1/runner/close', '/api/v1/runner/release-capacity'}:
        raise ValueError('unsupported session exit')
    payload = copy.deepcopy(payload)
    deadline = time.monotonic() + TOTAL_SECONDS
    scope = hashlib.sha256(json.dumps([api.server, path, payload.get('session_id'),
        payload.get('batch_id'), payload.get('evidence_id')], separators=(',', ':')).encode()).hexdigest()
    root = local_config.HOME / 'pending_session_exits'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = root / (scope + '.json')
    def check():
        if time.monotonic() >= deadline:
            raise ApiError('session exit journal wait expired', code='session_exit_unresolved')
    with _operation_lock(journal.with_suffix('.lock'), check):
        if journal.exists():
            try:
                state = json.loads(journal.read_text())
                if (state.get('schema_version') != 1 or state.get('scope') != scope
                        or not isinstance(state.get('body'), dict)
                        or type(state.get('attempts')) is not int or not 0 <= state['attempts'] <= MAX_ATTEMPTS
                        or type(state.get('receipt_reads')) is not int or not 0 <= state['receipt_reads'] <= MAX_RECEIPT_READS
                        or type(state.get('uncertain')) is not bool
                        or type(state.get('deadline')) not in (float, int) or not math.isfinite(state['deadline'])
                        or (state.get('result') is not None and not isinstance(state['result'], dict))):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ApiError('saved exit evidence is unreadable; retained unchanged', code='session_exit_journal_invalid')
        else:
            state = {'schema_version': 1, 'scope': scope, 'body': payload,
                     'server': api.server, 'path': path,
                     'deadline': deadline, 'attempts': 0, 'receipt_reads': 0,
                     'uncertain': False, 'result': None}
            _save(journal, state)
        if state['result'] is not None:
            return copy.deepcopy(state['result'])
        state['deadline'] = _saved_deadline(state, deadline)
        def save(*, before_write=False):
            _save(journal, {**state, **({'uncertain': True} if before_write else {})})
        try:
            return await _recover(api, path, state['body'], state, save, allow_replay=payload == state['body'])
        except ApiError as exc:
            if not state['uncertain'] and exc.status_code is not None and 400 <= exc.status_code < 500:
                journal.unlink(missing_ok=True)
            raise


async def _recover(api, path, payload, state, save, *, allow_replay):
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    identity = {}
    for field in ('session_id', 'batch_id', 'seq', 'evidence_id', 'device_generation'):
        if field not in payload:
            continue
        value = payload[field]
        valid = (type(value) is int and value >= 0) if field in {'seq', 'device_generation'} else (
            isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) is not None)
        identity[field] = value if valid else 'unknown'
    def unresolved():
        error = ApiError('session exit receipt remains unresolved; original evidence retained',
                         code='session_exit_unresolved')
        error.retry_exhausted = True
        status = 'busy_not_executed' if state.get('last_busy') is True and not state['uncertain'] else 'unknown_unreconciled'
        error.write_outcome = {'phase': path.rsplit('/', 1)[1], 'category': 'exit',
                               'request_identity': identity, 'status': status,
                               'local_result_retained': True, 'next_commands': ['dradar status --json']}
        print(json.dumps({'write_recovery': error.write_outcome}, sort_keys=True))
        return error
    transport = api._explicit_transport
    if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
        raise unresolved()
    async with httpx.AsyncClient(base_url=api.server, headers=writer_headers(api),
                                cookies=api._client.cookies, transport=transport,
                                timeout=REQUEST_SECONDS, trust_env=True) as client:
        async def call(method, target, **kwargs):
            if state['deadline'] - time.monotonic() < REQUEST_SECONDS:
                raise unresolved()
            return await asyncio.wait_for(client.request(method, target, **kwargs), REQUEST_SECONDS)

        def accept(result):
            state['result'] = copy.deepcopy(result)
            save()
            return result

        async def query():
            if state['receipt_reads'] >= MAX_RECEIPT_READS:
                return None
            state['receipt_reads'] += 1
            save()
            sid, batch = payload.get('session_id'), payload.get('batch_id')
            if not isinstance(sid, str) or not isinstance(batch, str):
                return None
            try:
                response = await call('GET', '/api/v1/runner/sessions/' + quote(sid, safe='') + '/receipt',
                                      params={'batch_id': batch})
                receipt = api._check(response)
                if not isinstance(receipt, dict) or receipt.get('session_id') != sid or receipt.get('batch_id') != batch:
                    return None
                if path.endswith('/close') and receipt.get('closed') is True:
                    return accept({'ok': True, 'already_closed': True, 'closed': True,
                                   'capacity_released': receipt.get('capacity_released') is True,
                                   'action': 'refresh' if payload.get('reason') == 'completed' else 'continue'})
                if (path.endswith('/release-capacity') and receipt.get('capacity_released') is True
                        and receipt.get('device_generation') == payload.get('device_generation')
                        and receipt.get('release_evidence_id') == payload.get('evidence_id')
                        and receipt.get('release_evidence_sha256') == digest):
                    return accept({'ok': True, 'capacity_released': True, 'idempotent_replay': True,
                                   'release_evidence_id': payload['evidence_id']})
            except (httpx.HTTPError, TimeoutError, ValueError, ApiError):
                pass
            return None

        if state['uncertain']:
            result = await query()
            if result is not None:
                return result
        if not allow_replay:
            raise unresolved()
        while state['attempts'] < MAX_ATTEMPTS:
            if state['deadline'] - time.monotonic() < 2 * REQUEST_SECONDS:
                raise unresolved()
            response = None
            state['attempts'] += 1
            save(before_write=True)
            try:
                response = await call('POST', path, json=payload)
                if response.status_code < 500:
                    result = api._check(response)
                    if (isinstance(result, dict) and result.get('ok') is True
                            and result.get('closed' if path.endswith('/close') else 'capacity_released') is True):
                        return accept(result)
            except ApiError:
                if not state['uncertain']:
                    raise
            except (httpx.HTTPError, TimeoutError, ValueError):
                pass
            busy = False
            if response is not None and response.status_code == 503:
                try:
                    envelope = response.json()
                    busy = isinstance(envelope, dict) and envelope.get('code') == 'mutation_busy'
                except ValueError:
                    pass
            state['uncertain'] = state['uncertain'] or not confirms_not_executed(response)
            state['last_busy'] = busy
            save()
            if state['uncertain']:
                result = await query()
                if result is not None:
                    return result
            delay = 0.0
            if busy:
                try:
                    envelope = response.json()
                    if isinstance(envelope, dict) and envelope.get('code') == 'mutation_busy':
                        delay = float(response.headers['Retry-After'])
                        declared = envelope['retry_after_seconds']
                        if type(declared) not in (int, float) or not math.isfinite(delay) or delay < 0 or delay != declared:
                            raise ValueError()
                        delay += random.uniform(0.0, min(1.0, delay * .1))
                except (KeyError, TypeError, ValueError):
                    raise unresolved()
            if state['attempts'] == MAX_ATTEMPTS or time.monotonic() + delay + 2 * REQUEST_SECONDS > state['deadline']:
                raise unresolved()
            await asyncio.sleep(delay)
        raise unresolved()
