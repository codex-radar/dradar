"""Explicit status reads can reconcile saved exits after auto recovery ends."""
import asyncio
import hashlib
import json
import re
import time

import httpx

from . import local_config
from .api_client import ApiError
from .acquisition_recovery import _operation_lock, _save


def _id(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) is not None


def inspect_exits(api):
    deadline = time.monotonic() + 30
    async def inspect():
        results = []
        async with httpx.AsyncClient(base_url=api.server, headers=api._client.headers,
                cookies=api._client.cookies, transport=api._explicit_transport, timeout=3.0) as client:
            for folder in ('pending_stops', 'pending_session_exits'):
                for path in sorted((local_config.HOME / folder).glob('*.json')):
                    def check():
                        if time.monotonic() >= deadline:
                            raise ApiError('status receipt budget ended')
                    try:
                        with _operation_lock(path.with_suffix('.lock'), check):
                            entry = json.loads(path.read_text())
                            if entry.get('schema_version') != 1 or entry.get('server') != api.server:
                                continue
                            body = entry['body']
                            stopping = folder == 'pending_stops'
                            sid, batch = body.get('session_id'), body.get('batch_id')
                            if stopping:
                                aid, rid = body.get('assignment_id'), body.get('request_id')
                                if not (_id(aid) and _id(rid)):
                                    continue
                                target = f'/api/v1/assignments/{aid}/stop-receipts/{rid}'
                                params = {}
                                identity = {'request_id': rid}
                                phase = 'assignment_stopped'
                            else:
                                if entry.get('result') is not None:
                                    continue
                                if not (_id(sid) and _id(batch)):
                                    continue
                                phase = entry.get('path', '').rsplit('/', 1)[-1]
                                if phase not in {'close', 'release-capacity'}:
                                    continue
                                target = f'/api/v1/runner/sessions/{sid}/receipt'
                                params = {'batch_id': batch}
                                identity = {'request_identity': {'session_id': sid, 'batch_id': batch}}
                            result = {'phase': phase, **identity, 'status': 'unknown_unreconciled',
                                      'request_saved': True, 'execution_allowed': False}
                            try:
                                check()
                                response = await asyncio.wait_for(client.get(target, params=params),
                                                                  min(3.0, deadline-time.monotonic()))
                                receipt = api._check(response)
                                committed = False
                                if stopping:
                                    committed = (receipt.get('schema_version') == 1
                                        and receipt.get('status') == 'committed' and receipt.get('request_id') == rid
                                        and receipt.get('assignment_id') == aid
                                        and (receipt.get('session_id') or '') == (sid or '')
                                        and isinstance(receipt.get('result'), dict) and receipt['result'].get('ok') is True)
                                    accepted = receipt.get('result')
                                elif receipt.get('session_id') == sid and receipt.get('batch_id') == batch:
                                    if phase == 'close':
                                        committed = receipt.get('closed') is True
                                        accepted = {'ok': True, 'closed': True, 'already_closed': True,
                                            'capacity_released': receipt.get('capacity_released') is True,
                                            'action': 'refresh' if body.get('reason') == 'completed' else 'continue'}
                                    else:
                                        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                                        committed = (receipt.get('capacity_released') is True
                                            and receipt.get('device_generation') == body.get('device_generation')
                                            and receipt.get('release_evidence_id') == body.get('evidence_id')
                                            and receipt.get('release_evidence_sha256') == digest)
                                        accepted = {'ok': True, 'capacity_released': True, 'idempotent_replay': True,
                                            'release_evidence_id': body.get('evidence_id')}
                                if committed:
                                    if stopping:
                                        path.unlink()
                                    else:
                                        entry['result'] = accepted
                                        _save(path, entry)
                                    result.update(status='unknown_reconciled', reconciliation='committed', request_saved=False)
                            except (httpx.HTTPError, TimeoutError, ValueError, ApiError, AttributeError):
                                pass
                            results.append(result)
                    except ApiError:
                        return results
                    except (OSError, ValueError, TypeError, KeyError, AttributeError):
                        continue
        return results
    return asyncio.run(inspect())
