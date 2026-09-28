"""Bounded cleanup of one exact stop, without granting execution permission."""
import asyncio
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


TOTAL_SECONDS = 35.0
REQUEST_SECONDS = 8.0
MAX_ATTEMPTS = 2
MAX_RECEIPT_READS = 3


def _failed(reason, request_id, *, outcome, reconciled=False):
    error = ApiError(reason, code="stop_recovery_incomplete")
    # Prevent outer best-effort cleanup from starting a fresh retry budget.
    error.retry_exhausted = True
    error.write_outcome = {
        "phase": "assignment_stopped", "category": "exit", "request_id": request_id,
        "status": outcome, "reconciled": reconciled,
        "local_result_retained": True,
        "next_commands": ["dradar status --json"],
    }
    return error


def recover(api, data):
    from .acquisition_recovery import _operation_lock, _save, _saved_deadline
    # Assignment/session IDs survive credential refresh. Authorization stays
    # server-side; no token or account identifier is stored in this journal.
    scope = hashlib.sha256(json.dumps([api.server, data.get('assignment_id'),
        data.get('session_id'), data.get('owner_epoch'), data.get('resume_generation')],
        separators=(',', ':')).encode()).hexdigest()
    root = local_config.HOME / 'pending_stops'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / (scope + '.json')
    deadline = time.monotonic() + TOTAL_SECONDS
    def check():
        if time.monotonic() >= deadline:
            raise _failed('stop journal wait budget exhausted', 'unknown', outcome='unknown_unreconciled')
    with _operation_lock(root / (scope + '.lock'), check):
        resume = path.exists()
        original = dict(data)
        if resume:
            try:
                saved = json.loads(path.read_text())
                original = saved['body']
                if (saved.get('schema_version') != 1 or saved.get('scope') != scope
                        or not isinstance(original, dict)
                        or any(original.get(key) != data.get(key) for key in
                               ('assignment_id', 'session_id', 'owner_epoch', 'resume_generation'))
                        or not isinstance(original.get('request_id'), str)
                        or not re.fullmatch(r'[0-9a-f]{32}', original['request_id'])):
                    raise ValueError()
            except (ValueError, KeyError, TypeError, AttributeError):
                raise _failed('saved stop identity is unreadable; retained unchanged', 'unknown',
                              outcome='unknown_unreconciled')
        else:
            saved = {'schema_version': 1, 'scope': scope, 'body': original,
                     'server': api.server,
                     'attempts': 0, 'receipt_reads': 0, 'deadline': deadline, 'uncertain': False}
            _save(path, saved)
        saved.setdefault('attempts', MAX_ATTEMPTS)
        saved.setdefault('receipt_reads', 0)
        saved.setdefault('deadline', 0.0)
        if (type(saved['attempts']) is not int or not 0 <= saved['attempts'] <= MAX_ATTEMPTS
                or type(saved['receipt_reads']) is not int or not 0 <= saved['receipt_reads'] <= MAX_RECEIPT_READS
                or type(saved['deadline']) not in (int, float) or not math.isfinite(saved['deadline'])):
            raise _failed('saved stop budget is invalid; retained unchanged', original['request_id'],
                          outcome='unknown_unreconciled')
        deadline = _saved_deadline(saved, deadline)
        same = {k: v for k, v in original.items() if k != 'request_id'} == {
            k: v for k, v in data.items() if k != 'request_id'}
        try:
            result = asyncio.run(stop(api, original, deadline=deadline, resume=resume, allow_replay=same,
                                      state=saved, save=lambda: _save(path, saved)))
        except ApiError as exc:
            # A first definitive refusal proves no stop was accepted. An
            # unknown or saved request must keep its original identity.
            if not resume and exc.status_code is not None and 400 <= exc.status_code < 500:
                path.unlink(missing_ok=True)
            raise
        path.unlink(missing_ok=True)
        return result


async def stop(api, data, *, deadline=None, resume=False, allow_replay=True, state=None, save=lambda: None):
    deadline = time.monotonic() + TOTAL_SECONDS if deadline is None else deadline
    if state is None:
        state = {'attempts': 0, 'receipt_reads': 0}
    request_id = data["request_id"]
    outcome, reconciled = "unknown_unreconciled", False
    uncertain = state.get('uncertain', resume)
    if state.get('last_busy') is True and not uncertain:
        outcome = 'busy_not_executed'
    transport = api._explicit_transport
    if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
        raise _failed("custom transport does not support bounded cleanup", request_id,
                      outcome=outcome)
    async with httpx.AsyncClient(base_url=api.server, headers=writer_headers(api),
                                cookies=api._client.cookies, timeout=REQUEST_SECONDS,
                                transport=transport, trust_env=True) as client:
        async def request(method, path, **kwargs):
            remaining = deadline - time.monotonic()
            if remaining < REQUEST_SECONDS:
                raise _failed("stop recovery budget exhausted", request_id,
                              outcome=outcome, reconciled=reconciled)
            # HTTPX timeouts cover individual I/O phases; wait_for also caps
            # the whole attempt, including slow streams and proxy setup.
            return await asyncio.wait_for(client.request(method, path, **kwargs), REQUEST_SECONDS)

        async def query():
            if state['receipt_reads'] >= MAX_RECEIPT_READS:
                return None, False
            state['receipt_reads'] += 1
            save()
            try:
                response = await request('GET',
                    f"/api/v1/assignments/{quote(data['assignment_id'], safe='')}/stop-receipts/{request_id}")
                receipt = api._check(response)
                bound = (isinstance(receipt, dict) and type(receipt.get('schema_version')) is int
                         and receipt['schema_version'] == 1
                         and receipt.get('request_id') == request_id
                         and receipt.get('assignment_id') == data['assignment_id'])
                if bound and receipt.get('status') == 'committed':
                    result = receipt.get('result')
                    if ((receipt.get('session_id') or '') == data['session_id']
                            and isinstance(result, dict) and result.get('ok') is True):
                        return result, True
                if (bound and receipt.get('status') == 'unknown'
                        and receipt.get('retry_same_request_only') is True):
                    return None, True
            except (httpx.HTTPError, TimeoutError, ValueError, ApiError):
                pass
            return None, False

        if resume and uncertain:
            result, supported = await query()
            if result is not None:
                return result
            if not supported or not allow_replay:
                raise _failed('original stop remains unresolved; request retained', request_id,
                              outcome='unknown_unreconciled')
        if not allow_replay:
            raise _failed('original stop payload retained', request_id, outcome=outcome)

        for attempt in range(state['attempts'], MAX_ATTEMPTS):
            response = None
            try:
                state['attempts'] += 1
                state['uncertain'] = True
                save()
                state['uncertain'] = uncertain
                response = await request("POST", "/api/v1/assignment/stopped", data=data)
                if response.status_code < 500:
                    result = api._check(response)
                    if isinstance(result, dict) and result.get("ok") is True:
                        return result
                    # Unparseable or incomplete success receipts are unknown.
            except ApiError:
                if not uncertain:
                    raise
                # A later refusal cannot settle a previous lost receipt.
            except (httpx.HTTPError, TimeoutError, ValueError):
                pass
            busy = False
            if response is not None and response.status_code == 503:
                try:
                    body = response.json()
                    busy = isinstance(body, dict) and body.get("code") == "mutation_busy"
                except ValueError:
                    pass
            delay = 0.0
            if busy and not confirms_not_executed(response):
                uncertain = True
            if busy:
                state['last_busy'] = True
                state['uncertain'] = uncertain
                save()
                if not uncertain:
                    outcome, reconciled = "busy_not_executed", False
                try:
                    delay = float(response.headers["Retry-After"])
                    declared = body["retry_after_seconds"]
                    if (type(declared) not in (int, float) or not math.isfinite(delay)
                            or delay < 0 or delay != declared):
                        raise ValueError()
                except (KeyError, TypeError, ValueError):
                    raise _failed("invalid stop retry interval", request_id,
                                  outcome=outcome)
                delay += random.uniform(0.0, min(1.0, delay * 0.1))
            if not busy or uncertain:
                uncertain = True
                state['uncertain'] = True
                state['last_busy'] = busy
                save()
                outcome, reconciled = "unknown_unreconciled", False
                result, replay_supported = await query()
                if result is not None:
                    return result
                if not replay_supported:
                    raise _failed("stop receipt could not be confirmed", request_id,
                                  outcome=outcome)
            if attempt + 1 == MAX_ATTEMPTS or time.monotonic() + delay + REQUEST_SECONDS > deadline:
                raise _failed("stop recovery budget exhausted", request_id,
                              outcome=outcome, reconciled=reconciled)
            await asyncio.sleep(delay)
    raise _failed('original stop recovery budget ended; identity retained', request_id,
                  outcome=outcome, reconciled=reconciled)
