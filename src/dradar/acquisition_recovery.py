"""Keep an uncertain allocation's identity until its original receipt settles."""
import asyncio
import copy
from contextlib import contextmanager
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import tempfile
import time
import uuid

import httpx

from . import cancellation, local_config, run_intent
from .api_client import ApiError
from .write_wait_policy import headers as writer_headers, confirms_not_executed

TOTAL_SECONDS = 250.0
REQUEST_SECONDS = 120.0
RECEIPT_SECONDS = 3.0
MAX_ATTEMPTS = 2
MAX_RECEIPT_READS = 3
RETRY_SECONDS = 130.0


async def _claim_account_scope(api):
    """Resolve a stable authenticated account before creating a claim ID."""
    transport = api._explicit_transport
    if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
        raise ApiError('cannot verify allocation account', code='acquisition_identity_unconfirmed')
    async with httpx.AsyncClient(base_url=api.server, headers=writer_headers(api),
                                cookies=api._client.cookies, transport=transport,
                                timeout=RECEIPT_SECONDS, trust_env=True) as client:
        try:
            path = '/api/v1/run-plans/identity' if api.plan_scoped else '/api/v1/whoami'
            response = await asyncio.wait_for(client.get(path), RECEIPT_SECONDS)
            identity = api._check(response)
            kind = 'plan_id' if api.plan_scoped else 'volunteer_id'
            principal = identity.get(kind) if isinstance(identity, dict) else None
            if not isinstance(principal, str) or not re.fullmatch(r'[0-9a-f]{32}', principal):
                raise ValueError()
            return hashlib.sha256(json.dumps([api.server, kind, principal], separators=(',', ':')).encode()).hexdigest()
        except (httpx.HTTPError, TimeoutError, ValueError, ApiError) as exc:
            raise ApiError('account identity unavailable; no allocation was dispatched',
                           code='acquisition_identity_unconfirmed') from exc


@contextmanager
def _operation_lock(path, check):
    """Lock only this operation; never hold the flight recorder's mutex."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'\0')
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == 'nt':
            import msvcrt
            acquire = lambda: msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(fd, fcntl.LOCK_UN)
        while not locked:
            check()
            try:
                acquire()
                locked = True
            except OSError as exc:
                if exc.errno not in {errno.EAGAIN, errno.EACCES}:
                    raise
                time.sleep(0.01)
        yield
    finally:
        if locked:
            release()
        os.close(fd)


def _save(path, value):
    if 'deadline' in value:
        # Monotonic clocks survive process changes but reset after reboot.
        # The wall-clock bound prevents a saved budget reopening on boot.
        limit = min(value['deadline'], value.get('retry_deadline', value['deadline']))
        wall = time.time() + max(0.0, limit - time.monotonic())
        value['wall_deadline'] = min(value.get('wall_deadline', wall), wall)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.acquisition-')
    try:
        with os.fdopen(fd, 'w') as file:
            json.dump(value, file, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _saved_deadline(value, current):
    wall = value.get('wall_deadline', 0.0)
    if type(wall) not in (int, float) or not math.isfinite(wall):
        return 0.0
    return min(current, value['deadline'], time.monotonic() + max(0.0, wall - time.time()))


def recover(api, operation, body, *, check=None):
    if operation not in {'assignment_claim', 'assignment_checkout'}:
        raise ValueError('unsupported allocation operation')
    body = copy.deepcopy(body)
    deadline = time.monotonic() + TOTAL_SECONDS
    session_id = body.get('session_id')
    # Plan access tokens rotate. A checkout's globally unique session and
    # batch remain stable, so changing credentials must not hide a pending
    # allocation. The original receipt still enforces authenticated scope.
    stable_session = (operation == 'assignment_checkout' and api.batch_id is not None
                      and isinstance(session_id, str) and re.fullmatch(r'[0-9a-f]{32}', session_id))
    identity_scope = api.server if stable_session else api.account_scope
    if operation == 'assignment_claim':
        identity_scope = asyncio.run(_claim_account_scope(api))
    scope = hashlib.sha256(json.dumps([identity_scope, api.batch_id, api.benchmark_id,
        operation, body.get('session_id')], separators=(',', ':')).encode()).hexdigest()
    root = local_config.HOME / 'pending_acquisitions'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / (scope + '.json')
    def permitted():
        try:
            run_intent.require_worker(local_config.HOME)
        except run_intent.IntentStopped:
            raise ApiError('allocation stopped before another write', code='acquisition_stopped')
        if cancellation.requested() or (check is not None and check() is False):
            raise ApiError('allocation stopped before another write', code='acquisition_stopped')
        if time.monotonic() >= deadline:
            raise ApiError('allocation budget expired', code='acquisition_budget_expired')
    with _operation_lock(root / (scope + '.lock'), permitted):
        if path.exists():
            try:
                entry = json.loads(path.read_text())
                if (entry.get('schema_version') != 1 or entry.get('scope') != scope
                        or entry.get('operation') != operation
                        or not isinstance(entry.get('body'), dict)
                        or not isinstance(entry.get('request_id'), str)
                        or not re.fullmatch(r'[0-9a-f]{32}', entry['request_id'])
                        or type(entry.get('sent')) is not bool):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ApiError('saved allocation identity is unreadable; kept unchanged', code='acquisition_journal_invalid')
        else:
            permitted()
            entry = {'schema_version': 1, 'scope': scope, 'operation': operation,
                     'request_id': uuid.uuid4().hex, 'body': body, 'sent': False,
                     'attempts': 0, 'receipt_reads': 0, 'deadline': deadline,
                     'server': api.server, 'batch_id': api.batch_id}
            _save(path, entry)
        if not entry['sent'] and entry['body'] != body:
            permitted()
            entry.update(body=body, request_id=uuid.uuid4().hex)
            _save(path, entry)
        entry.setdefault('attempts', MAX_ATTEMPTS if entry['sent'] else 0)
        entry.setdefault('receipt_reads', 0)
        entry.setdefault('deadline', 0.0 if entry['sent'] else deadline)
        if (type(entry['attempts']) is not int or not 0 <= entry['attempts'] <= MAX_ATTEMPTS
                or type(entry['receipt_reads']) is not int or not 0 <= entry['receipt_reads'] <= MAX_RECEIPT_READS
                or type(entry['deadline']) not in (int, float) or not math.isfinite(entry['deadline'])):
            raise ApiError('saved allocation budget is invalid; kept unchanged', code='acquisition_journal_invalid')
        deadline = _saved_deadline(entry, deadline)
        return asyncio.run(_recover(api, operation, body, entry, path, deadline, permitted))


async def _recover(api, operation, requested, entry, path, deadline, permitted):
    rid = entry['request_id']
    original = {**entry['body'], 'request_id': rid}
    uncertain = entry.get('uncertain', entry['sent'])
    outcome = ('busy_not_executed' if entry.get('known_busy') is True and not uncertain
               else 'unknown_unreconciled')
    def unresolved():
        error = ApiError('original allocation remains saved for reconciliation', code='acquisition_unresolved',
                         transport_phase=operation, transport_kind='outcome_unknown')
        error.retry_exhausted = True
        error.write_outcome = {'phase': operation, 'category': 'claim', 'request_id': rid,
                               'status': outcome, 'local_result_retained': True,
                               'request_saved': True, 'next_commands': ['dradar leases']}
        # Callers may stop the current command. The durable request survives;
        # a later formal invocation cannot silently choose a fresh identity.
        print(json.dumps({'write_recovery': error.write_outcome}, sort_keys=True))
        return error
    transport = api._explicit_transport
    if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
        raise unresolved()
    async with httpx.AsyncClient(base_url=api.server, headers=writer_headers(api),
                                cookies=api._client.cookies, transport=transport,
                                timeout=httpx.Timeout(30.0, read=120.0), trust_env=True) as client:
        def accept(result):
            nonlocal outcome
            outcome = 'committed'
            try:
                permitted()
            except ApiError:
                raise unresolved()
            path.unlink()
            return result

        async def query():
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or entry['receipt_reads'] >= MAX_RECEIPT_READS:
                    return None
                entry['receipt_reads'] += 1
                _save(path, entry)
                response = await asyncio.wait_for(client.get(f'/api/v1/write-receipts/{operation}/{rid}'),
                                                  min(RECEIPT_SECONDS, remaining))
                receipt = api._check(response)
                if (not isinstance(receipt, dict) or type(receipt.get('schema_version')) is not int
                        or receipt['schema_version'] != 1 or receipt.get('operation') != operation
                        or receipt.get('request_id') != rid):
                    return None
                return receipt
            except (httpx.HTTPError, TimeoutError, ValueError, ApiError):
                return None

        async def reconcile():
            nonlocal outcome
            receipt = await query()
            if receipt is not None and receipt.get('status') == 'committed':
                result = receipt.get('result')
                if isinstance(result, dict) and 'assignment' in result:
                    return accept(result), True
            # An explicit unknown contract proves the server supports same-ID
            # replay. It does not prove the pending original was uncommitted.
            if (receipt is not None and receipt.get('status') == 'unknown'
                    and receipt.get('retry_same_request_only') is True):
                outcome = 'unknown_unreconciled'
                return None, True
            return None, False

        if uncertain:
            result, supported = await reconcile()
            if result is not None:
                return result
            if not supported or requested != entry['body']:
                raise unresolved()

        for attempt in range(entry['attempts'], MAX_ATTEMPTS):
            try:
                permitted()
            except ApiError:
                raise unresolved()
            remaining = deadline - time.monotonic()
            if remaining < REQUEST_SECONDS + RECEIPT_SECONDS:
                raise unresolved()
            entry['sent'] = True
            entry['attempts'] += 1
            entry['uncertain'] = True
            _save(path, entry)
            entry['uncertain'] = uncertain
            response = None
            try:
                response = await asyncio.wait_for(client.post('/api/v1/assignment/'+operation.removeprefix('assignment_'),
                                                              data=original), REQUEST_SECONDS)
                if response.status_code < 500:
                    result = api._check(response)
                    if isinstance(result, dict) and 'assignment' in result:
                        return accept(result)
            except ApiError as exc:
                if exc.code == 'acquisition_unresolved':
                    raise
                if uncertain:
                    result, _ = await reconcile()
                    if result is not None:
                        return result
                    raise unresolved()
                # A first definitive rejection did not allocate anything.
                path.unlink(missing_ok=True)
                raise
            except (httpx.HTTPError, TimeoutError, ValueError):
                pass
            entry['known_busy'] = confirms_not_executed(response)
            uncertain = uncertain or not entry['known_busy']
            entry['uncertain'] = uncertain
            outcome = 'unknown_unreconciled' if uncertain else 'busy_not_executed'
            deadline = entry['deadline'] = min(deadline, time.monotonic() + RETRY_SECONDS)
            _save(path, entry)
            delay = 0.0
            if response is not None and response.status_code == 503:
                try:
                    envelope = response.json()
                    if isinstance(envelope, dict) and envelope.get('code') == 'mutation_busy':
                        delay = float(response.headers['Retry-After'])
                        declared = envelope['retry_after_seconds']
                        if (type(declared) not in (int, float) or not math.isfinite(delay)
                                or delay < 0 or delay != declared):
                            raise ValueError()
                        delay += random.uniform(0.0, min(1.0, delay*0.1))
                except (KeyError, TypeError, ValueError):
                    raise unresolved()
            result, supported = await reconcile() if uncertain else (None, True)
            if result is not None:
                return result
            if not supported or attempt + 1 == MAX_ATTEMPTS:
                raise unresolved()
            if time.monotonic() + delay + REQUEST_SECONDS + RECEIPT_SECONDS > deadline:
                raise unresolved()
            until = time.monotonic() + delay
            while time.monotonic() < until:
                try:
                    permitted()
                except ApiError:
                    raise unresolved()
                await asyncio.sleep(min(0.025, max(0, until-time.monotonic())))
    raise unresolved()


def inspect_pending(api):
    """The existing leases command may read exact receipts after auto recovery.

    Never allocate work here. A confirmed result clears only its old local
    request journal; unknown results retain the original identity and payload.
    """
    root = local_config.HOME / 'pending_acquisitions'
    if not root.exists() or not getattr(api, 'server', None):
        return []
    deadline = time.monotonic() + 30.0
    async def inspect():
        results = []
        async with httpx.AsyncClient(base_url=api.server, headers=api._client.headers,
                cookies=api._client.cookies, transport=api._explicit_transport,
                timeout=RECEIPT_SECONDS, trust_env=True) as client:
            for path in sorted(root.glob('*.json')):
                def check():
                    if time.monotonic() >= deadline:
                        raise ApiError('receipt inspection budget ended', code='acquisition_unresolved')
                try:
                    with _operation_lock(path.with_suffix('.lock'), check):
                        entry = json.loads(path.read_text())
                        operation, rid = entry.get('operation'), entry.get('request_id')
                        if (entry.get('server') != api.server or entry.get('schema_version') != 1
                                or operation not in {'assignment_claim', 'assignment_checkout'}
                                or not isinstance(rid, str) or not re.fullmatch(r'[0-9a-f]{32}', rid)
                                or (getattr(api, 'plan_scoped', False) and entry.get('batch_id') != api.batch_id)):
                            continue
                        result = {'phase': operation, 'category': 'claim', 'request_id': rid,
                                  'status': 'unknown_unreconciled', 'request_saved': True,
                                  'execution_allowed': False}
                        try:
                            remaining = deadline - time.monotonic()
                            response = await asyncio.wait_for(client.get(
                                f'/api/v1/write-receipts/{operation}/{rid}'), min(RECEIPT_SECONDS, remaining))
                            receipt = api._check(response)
                            if (isinstance(receipt, dict) and receipt.get('schema_version') == 1
                                    and receipt.get('request_id') == rid and receipt.get('operation') == operation
                                    and receipt.get('status') == 'committed'
                                    and isinstance(receipt.get('result'), dict)
                                    and 'assignment' in receipt['result']):
                                path.unlink()
                                result.update(status='unknown_reconciled', reconciliation='committed', request_saved=False)
                        except (httpx.HTTPError, TimeoutError, ValueError, ApiError):
                            pass
                        results.append(result)
                except ApiError:
                    break
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
        return results
    return asyncio.run(inspect())
