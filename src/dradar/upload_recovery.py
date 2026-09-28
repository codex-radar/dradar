"""Replay one content-bound upload using its original immutable bytes."""
import asyncio
import copy
import json
import math
import random

import httpx

from .api_client import ApiError
from .write_wait_policy import headers as writer_headers, confirms_not_executed

REQUEST_SECONDS = 120.0
RETRY_SECONDS = 130.0
MAX_ATTEMPTS = 2


async def replay(api, path, intent_id, deadline, kwargs):
    if path not in {'/api/v1/submission-upload-intents', '/api/v1/submissions'}:
        raise ValueError('unsupported upload replay endpoint')
    # ApiClient.submit materializes file bytes before this call. Never reopen
    # paths on retry: a local edit must not change the original operation.
    kwargs = copy.deepcopy(kwargs)
    transport = api._explicit_transport
    budgets = getattr(api, '_upload_write_budgets', None)
    if budgets is None:
        budgets = api._upload_write_budgets = {}
    state = budgets.setdefault((path, intent_id), {'attempts': 0, 'deadline': deadline,
        'uncertain': intent_id in getattr(api, '_unresolved_upload_intents', set())})
    deadline = min(deadline, state['deadline'])
    uncertain = state['uncertain']
    def exhausted():
        error = ApiError('upload receipt remains unresolved; local result retained',
                         code='upload_recovery_incomplete', status_code=503 if state.get('known_busy') and not uncertain else None)
        error.retry_exhausted = True
        error.write_outcome = {
            'phase': 'upload_intent' if path.endswith('upload-intents') else 'submission_upload',
            'category': 'upload', 'request_id': intent_id, 'status': 'busy_not_executed' if state.get('known_busy') and not uncertain else 'unknown_unreconciled',
            'local_result_retained': True, 'next_commands': ['dradar retry-upload'],
        }
        print(json.dumps({'write_recovery': error.write_outcome}, sort_keys=True))
        return error
    if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
        raise exhausted()
    async with httpx.AsyncClient(base_url=api.server, headers=writer_headers(api),
                                cookies=api._client.cookies, transport=transport,
                                timeout=httpx.Timeout(30.0, read=120.0, write=None),
                                trust_env=True) as client:
        for attempt in range(MAX_ATTEMPTS):
            if state['attempts'] >= MAX_ATTEMPTS:
                raise exhausted()
            remaining = deadline - api._monotonic()
            if remaining <= 0:
                raise exhausted()
            response = None
            state['attempts'] += 1
            try:
                response = await asyncio.wait_for(client.post(path, **kwargs), min(REQUEST_SECONDS, remaining))
                if response.status_code < 500:
                    result = api._check(response)
                    valid = isinstance(result, dict) and (
                        (path.endswith('upload-intents') and result.get('ok') is True
                         and result.get('upload_intent_id', intent_id) == intent_id)
                        or (path.endswith('submissions') and isinstance(result.get('submission_id'), str)
                            and bool(result['submission_id']))
                    )
                    if valid:
                        return result
            except ApiError as exc:
                # The baseline's nonce-bound duplicate response is an
                # existing submission receipt understood by upload callers.
                settled_duplicate = exc.status_code == 409 and 'already submitted' in str(exc).lower()
                if uncertain and not settled_duplicate:
                    raise exhausted()
                raise
            except (httpx.HTTPError, TimeoutError, ValueError):
                pass
            delay = 0.0
            if response is not None and response.status_code == 503:
                try:
                    envelope = response.json()
                except ValueError:
                    envelope = None
                if isinstance(envelope, dict) and envelope.get('code') == 'deployment_maintenance':
                    # Preserve the existing deployment fence and its shared
                    # intent/submit deadline; it owns maintenance waiting.
                    if uncertain:
                        raise exhausted()
                    state['attempts'] -= 1
                    api._check(response)
                if isinstance(envelope, dict) and envelope.get('code') == 'mutation_busy':
                    try:
                        delay = float(response.headers['Retry-After'])
                        declared = envelope['retry_after_seconds']
                        if (type(declared) not in (int, float) or not math.isfinite(delay)
                                or delay < 0 or delay != declared):
                            raise ValueError()
                    except (KeyError, TypeError, ValueError):
                        raise exhausted()
                    delay += random.uniform(0.0, min(1.0, delay * 0.1))
            state['known_busy'] = confirms_not_executed(response)
            uncertain = state['uncertain'] = uncertain or not state['known_busy']
            # Keep the existing shared maintenance deadline, but once a
            # write becomes busy/unknown cap the recovery tail independently.
            deadline = state['deadline'] = min(deadline,
                state.setdefault('retry_deadline', api._monotonic() + RETRY_SECONDS))
            if attempt + 1 == MAX_ATTEMPTS or api._monotonic() + delay + REQUEST_SECONDS > deadline:
                raise exhausted()
            await asyncio.sleep(delay)
    raise AssertionError('unreachable')
