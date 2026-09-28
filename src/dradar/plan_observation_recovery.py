"""Keep each plan heartbeat/progress identity until its receipt is known."""
import asyncio
import copy
import hashlib
import json
import math
import random
import re
import time
import uuid

import httpx

from . import local_config
from .api_client import ApiError
from .write_wait_policy import headers as writer_headers, confirms_not_executed
from .acquisition_recovery import _operation_lock, _save, _saved_deadline

REQUEST_SECONDS = 120.0
RECEIPT_SECONDS = 3.0
TOTAL_SECONDS = 250.0
RETRY_SECONDS = 130.0
MAX_ATTEMPTS = 2


def recover(api, operation, payload):
    if operation not in {'plan_heartbeat', 'plan_progress'}:
        raise ValueError('unsupported plan observation')
    payload = copy.deepcopy(payload)
    deadline = time.monotonic() + TOTAL_SECONDS
    scope = hashlib.sha256(json.dumps([api.server, payload['plan_id'], operation]).encode()).hexdigest()
    root = local_config.HOME / 'pending_plan_observations'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / (scope + '.json')
    def check():
        if time.monotonic() >= deadline:
            raise ApiError('plan observation budget expired', code='plan_observation_unresolved')
    with _operation_lock(root / (scope + '.lock'), check):
        if path.exists():
            try:
                entry = json.loads(path.read_text())
                if (entry.get('scope') != scope or entry.get('schema_version') != 1
                        or not isinstance(entry.get('body'), dict)
                        or type(entry.get('sent')) is not bool
                        or not isinstance(entry.get('request_id'), str)
                        or not re.fullmatch(r'[0-9a-f]{32}', entry['request_id'])):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ApiError('saved plan observation is unreadable; kept unchanged', code='plan_observation_journal_invalid')
        else:
            entry = {'schema_version': 1, 'scope': scope, 'body': payload,
                     'request_id': uuid.uuid4().hex, 'sent': False, 'attempts': 0, 'deadline': deadline}
            _save(path, entry)
        attempts = entry.get('attempts', MAX_ATTEMPTS if entry['sent'] else 0)
        original_deadline = entry.get('deadline', 0.0 if entry['sent'] else deadline)
        if (type(attempts) is not int or not 0 <= attempts <= MAX_ATTEMPTS
                or type(original_deadline) not in (int, float) or not math.isfinite(original_deadline)):
            raise ApiError('saved plan retry budget is invalid; kept unchanged', code='plan_observation_journal_invalid')
        entry.update(attempts=attempts, deadline=original_deadline)
        if type(entry.get('finished', False)) is not bool:
            raise ApiError('saved plan retry budget is invalid; kept unchanged', code='plan_observation_journal_invalid')
        if ('retry_deadline' in entry and (type(entry['retry_deadline']) not in (int, float)
                or not math.isfinite(entry['retry_deadline']))):
            raise ApiError('saved plan retry deadline is invalid; kept unchanged', code='plan_observation_journal_invalid')
        explicit = getattr(api, '_explicit_write_recovery', False) is True
        used = getattr(api, '_explicit_plan_recovery_used', None)
        if used is None:
            used = api._explicit_plan_recovery_used = set()
        if explicit and scope not in used:
            used.add(scope)
            entry.update(attempts=0, deadline=deadline, finished=False)
            entry.pop('retry_deadline', None)
            entry.pop('wall_deadline', None)
            _save(path, entry)
        return asyncio.run(_recover(api, operation, payload, entry, path, _saved_deadline(entry, deadline)))


async def _recover(api, operation, requested, entry, path, deadline):
    rid, original = entry['request_id'], entry['body']
    uncertain = entry.get('uncertain', entry['sent'])
    def unresolved():
        # Periodic callers and new client instances must not reopen even a
        # read-only recovery loop once this operation's budget has ended.
        entry['finished'] = True
        _save(path, entry)
        error = ApiError('plan observation remains saved for reconciliation', code='plan_observation_unresolved')
        error.retry_exhausted = True
        error.write_outcome = {'phase': operation, 'category': 'heartbeat' if operation == 'plan_heartbeat' else 'progress',
            'request_id': rid, 'status': 'busy_not_executed' if entry.get('known_busy') and not uncertain else 'unknown_unreconciled', 'local_result_retained': True,
            'next_commands': ['dradar progress --plan <saved-plan-code>']}
        print(json.dumps({'write_recovery': error.write_outcome}, sort_keys=True))
        return error
    if entry.get('finished') or time.monotonic() >= deadline:
        raise unresolved()
    def valid(result):
        if not isinstance(result, dict) or result.get('schema_version') != 1:
            return False
        if operation == 'plan_heartbeat':
            return (result.get('plan_id') == original['plan_id'] and result.get('touched') is True
                    and result.get('starts_new_work') is False)
        return (isinstance(result.get('plan'), dict) and result['plan'].get('plan_id') == original['plan_id']
                and isinstance(result.get('envelope'), dict))
    def accept(result):
        path.unlink()
        return result
    def bound_recovery():
        nonlocal deadline
        # Persist the clock starting at the first ambiguous response, rather
        # than giving an immediate busy the full initial request allowance.
        deadline = min(deadline, entry.setdefault('retry_deadline', time.monotonic() + RETRY_SECONDS))
        _save(path, entry)
    if uncertain:
        bound_recovery()
    transport = api._explicit_transport
    if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
        raise unresolved()
    async with httpx.AsyncClient(base_url=api.server, headers=writer_headers(api),
                                cookies=api._client.cookies, transport=transport,
                                timeout=REQUEST_SECONDS, trust_env=True) as client:
        async def query():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, False
            try:
                response = await asyncio.wait_for(client.get('/api/v1/write-receipts/' + operation + '/' + rid),
                                                  min(RECEIPT_SECONDS, remaining))
                receipt = api._check(response)
                if (isinstance(receipt, dict) and receipt.get('schema_version') == 1
                        and receipt.get('operation') == operation and receipt.get('request_id') == rid):
                    if receipt.get('status') == 'committed' and valid(receipt.get('result')):
                        return accept(receipt['result']), True
                    if receipt.get('status') == 'unknown' and receipt.get('retry_same_request_only') is True:
                        return None, True
            except (httpx.HTTPError, TimeoutError, ValueError, ApiError):
                pass
            return None, False
        if uncertain:
            result, supported = await query() if uncertain else (None, True)
            if result is not None:
                return result
            if not supported or requested != original:
                raise unresolved()
        write_deadline = min(deadline, entry['deadline'])
        for attempt in range(entry['attempts'], MAX_ATTEMPTS):
            if write_deadline - time.monotonic() < REQUEST_SECONDS + RECEIPT_SECONDS:
                raise unresolved()
            entry['sent'] = True
            entry['attempts'] += 1
            entry['uncertain'] = True
            _save(path, entry)
            entry['uncertain'] = uncertain
            response = None
            try:
                response = await asyncio.wait_for(client.post('/api/v1/run-plans/' + operation.removeprefix('plan_'),
                    json=original, headers={'X-DRadar-Write-ID': rid}), REQUEST_SECONDS)
                if response.status_code < 500:
                    result = api._check(response)
                    if valid(result):
                        return accept(result)
            except ApiError:
                if not uncertain:
                    path.unlink()
                    raise
            except (httpx.HTTPError, TimeoutError, ValueError):
                pass
            entry['known_busy'] = confirms_not_executed(response)
            uncertain = uncertain or not entry['known_busy']
            entry['uncertain'] = uncertain
            bound_recovery()
            write_deadline = min(write_deadline, deadline)
            result, supported = await query() if uncertain else (None, True)
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
                        delay += random.uniform(0.0, min(1.0, delay*.1))
                except (KeyError, TypeError, ValueError):
                    raise unresolved()
            if attempt + 1 == MAX_ATTEMPTS or time.monotonic() + delay + REQUEST_SECONDS + RECEIPT_SECONDS > write_deadline:
                raise unresolved()
            await asyncio.sleep(delay)
    raise unresolved()


def reconcile_pending(api, plan_id):
    """Explicit progress may recover original observations with a new budget."""
    results = []
    for operation in ('plan_heartbeat', 'plan_progress'):
        scope = hashlib.sha256(json.dumps([api.server, plan_id, operation]).encode()).hexdigest()
        path = local_config.HOME / 'pending_plan_observations' / (scope + '.json')
        if not path.exists():
            continue
        try:
            entry = json.loads(path.read_text())
            original = entry['body']
            if original.get('plan_id') != plan_id:
                raise ValueError()
            recover(api, operation, original)
            results.append({'phase': operation, 'status': 'committed'})
        except ApiError as exc:
            results.append(getattr(exc, 'write_outcome', {'phase': operation, 'status': 'unknown_unreconciled'}))
        except (OSError, ValueError, TypeError, KeyError):
            results.append({'phase': operation, 'status': 'unknown_unreconciled'})
    return results
