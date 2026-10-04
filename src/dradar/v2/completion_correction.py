"""Explicit no-model supplement of one preserved native completion.

Only authenticated reads and the official correction upload are performed.
The frozen result, its request and all original evidence remain immutable.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from .artifacts import ArtifactError, Artifacts, _sync_dir
from .client import ProtocolError
from .locks import exclusive
from .native_evidence import native_identity, collection_safety
from .protocol import assignment, envelope, owner, opaque, result_hash, correction_receipt
from .results import recover_completion, upload_files
from ..assignment_lock import lock

SCHEMA = 'codex-native-completion-correction/1'

def preserved_bytes(path: Path) -> bytes:
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ArtifactError('original evidence must be a regular preserved file')
    return path.read_bytes()

def finite_json(data):
    return json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ArtifactError('nonfinite evidence')))

def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ArtifactError('explicit original evidence SHA256 required')
    return value

def _metadata(root: Path, aid: str, oldhash: str, raw: str):
    directory = root / 'artifacts' / 'completion-corrections'
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ArtifactError('correction metadata path must not contain symlinks')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest = directory / (aid + '-' + oldhash + '.json')
    if dest.exists() or dest.is_symlink():
        if preserved_bytes(dest) != raw.encode():
            raise ArtifactError('immutable correction metadata conflict')
        return
    temporary = directory / ('.saving-' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw.encode()); out.flush(); os.fsync(out.fileno())
        # Atomic create, never replace an existing correction (including races).
        try:
            os.link(temporary, dest)
        except FileExistsError:
            if preserved_bytes(dest) != raw.encode():
                raise ArtifactError('immutable correction metadata conflict')
        _sync_dir(directory)
    finally:
        temporary.unlink(missing_ok=True)

def prepare_supplement(client, aid: str, expected_result_sha256: str, expected_exit_sha256: str):
    try:
        return _prepare_supplement(client, aid, expected_result_sha256, expected_exit_sha256)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ArtifactError('original completion evidence is incomplete or malformed') from exc

def _prepare_supplement(client, aid: str, expected_result_sha256: str, expected_exit_sha256: str):
    """Validate original evidence and save the independent request before upload.

    Caller holds the controller and launch locks. This method never touches
    execution.result_json or prepares the original failed upload as accepted.
    """
    opaque(aid); _digest(expected_result_sha256); _digest(expected_exit_sha256)
    journal = client.journal
    rid, device, account = (journal.value(k) for k in ('run', 'device', 'account'))
    if not all((rid, device, account)) or journal.value('local_stop') != 'true':
        raise ProtocolError('original stopped run and bound account required')
    saved = journal.execution(aid)
    if not saved or not saved['result_json']:
        raise ArtifactError('original frozen execution result required')
    original_request = next((r for r in journal.requests() if r.operation == 'result:' + aid), None)
    if original_request is None or original_request.response is not None:
        raise ArtifactError('unaccepted original failed request required')
    original = original_request.body
    payload = {k: v for k, v in original.items() if k != 'request_id'}
    if (json.loads(saved['result_json']) != payload or payload['outcome'] != 'failed'
            or not isinstance(payload.get('failure'), dict) or payload['failure'].get('code') != 'host_turn_failed'
            or payload['exit_confirmed'] is not True or payload['result_sha256'] != expected_result_sha256
            or result_hash(payload) != expected_result_sha256 or payload['execution_id'] != saved['execution_id']
            or original_request.path != f'/api/v2/assignments/{aid}/result'):
        raise ArtifactError('original failed payload, request or execution mismatch')
    start = next((r for r in journal.requests() if r.operation == 'start:' + aid), None)
    if not start or not start.response or start.response.get('status') != 'started':
        raise ArtifactError('original authoritative start receipt required')
    a = assignment(start.response.get('assignment'), run_id=rid, device_id=device)
    if (a['assignment_id'] != aid or a.get('execution_id') != saved['execution_id']
            or owner(a) != owner(payload)):
        raise ArtifactError('original start ownership differs from frozen result')
    recovered = recover_completion(journal.root / 'artifacts', a, saved['execution_id'])
    if recovered != payload:
        raise ArtifactError('original frozen metadata differs from journal')
    canonical_metadata = json.dumps({'assignment_id': aid, 'payload': payload}, sort_keys=True,
                                    separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
    if preserved_bytes(journal.root / 'artifacts' / 'metadata' / (aid + '.json')) != canonical_metadata:
        raise ArtifactError('original frozen metadata bytes changed')
    Artifacts(journal.root / 'artifacts' / 'raw').inspect(aid, saved['execution_id'])
    files = upload_files(journal.root / 'artifacts', aid, payload)
    if set(files) != {'patch', 'runner_result', 'trajectory'}:
        raise ArtifactError('original native evidence manifest required')
    collected = finite_json(preserved_bytes(files['runner_result']))
    events = finite_json(preserved_bytes(files['trajectory']))
    patch = preserved_bytes(files['patch'])
    facts = native_identity(collected, events, a['task']['model'], a['task']['effort'],
                            task=a['task'], runner=a['runner'], patch=patch)
    collection_safety(collected)
    host = journal.root / 'runtime' / aid / 'host'
    if finite_json(preserved_bytes(host / 'collected' / 'COLLECTION.json')) != collected:
        raise ArtifactError('saved native collection changed')
    if preserved_bytes(host / 'collected' / 'model.patch') != patch:
        raise ArtifactError('original collected patch changed')
    for name, record in collected['outputs'].items():
        output = preserved_bytes(host / 'collected' / name)
        if len(output) != record['bytes'] or hashlib.sha256(output).hexdigest() != record['sha256']:
            raise ArtifactError('original required output changed')
    exit_bytes = preserved_bytes(host / 'CLEANUP.json')
    if hashlib.sha256(exit_bytes).hexdigest() != expected_exit_sha256:
        raise ArtifactError('original exit evidence hash mismatch')
    cleanup = finite_json(exit_bytes)
    if (cleanup.get('model_calls') != 1 or type(cleanup.get('model_calls')) is not int
            or cleanup.get('native_app_server_reaped') is not True
            or type(cleanup.get('native_app_server_exit_code')) is not int or cleanup['native_app_server_exit_code'] != 0
            or cleanup.get('created_containers_absent') is not True
            or cleanup.get('controller_cleanup_completed') is not True
            or cleanup.get('retained_uncollected_container_ids') != []
            or cleanup.get('last_turn_status') != 'completed' or cleanup.get('last_turn_error') is not None
            or cleanup.get('model_thread_id') != facts['thread_id'] or cleanup.get('model_turn_id') != facts['turn_id']
            or cleanup.get('model') != a['task']['model'] or cleanup.get('reasoning') != a['task']['effort']
            or cleanup.get('task') != a['task']['task_id']
            or cleanup.get('actual_host_cli_version') != 'codex-cli 0.160.0'):
        raise ArtifactError('same native execution completion and physical exit required')
    containers = cleanup.get('created_container_ids')
    if (not isinstance(containers, list) or not containers or len(containers) != len(set(containers))
            or any(not isinstance(c, str) or not re.fullmatch('[a-f0-9]{64}', c) for c in containers)
            or set(cleanup.get('cleanup_exact_container_ids', [])) != set(containers)
            or collected.get('container_id') not in containers
            or cleanup.get('task_container_id') != collected.get('container_id')):
        raise ArtifactError('all exact owned containers must have exit evidence')
    expected_tokens = {'input': facts['usage']['inputTokens'], 'output': facts['usage']['outputTokens'],
                       'total': facts['usage']['totalTokens'], 'source': 'official_app_server_terminal_usage', 'missing_reason': None}
    if payload['tokens'] != expected_tokens:
        raise ArtifactError('frozen usage differs from original native terminal observation')
    bootstrap = client.bootstrap()
    if bootstrap.get('account', {}).get('account_id') != account:
        raise ProtocolError('correction account differs from original account')
    snap = envelope(client.get('/api/v2/runs/' + opaque(rid)))
    run = snap.get('run', {})
    if run.get('run_id') != rid or run.get('device_id') != device or run.get('state') != 'stopped':
        raise ProtocolError('correction requires same authoritative stopped run')
    current = assignment(envelope(client.get('/api/v2/assignments/' + aid)).get('assignment'), run_id=rid, device_id=device)
    if (current['assignment_id'] != aid or current.get('execution_id') != saved['execution_id']
            or owner(current) != owner(payload) or current['task'] != a['task'] or current.get('runner') != a['runner']):
        raise ProtocolError('current assignment differs from original execution scope')
    operation = 'completion-correction:' + aid + ':' + expected_result_sha256
    existing = next((r for r in journal.requests() if r.operation == operation), None)
    if current['state'] == 'submitted':
        if (not existing or current.get('result_sha256') != existing.body['corrected_result']['result_sha256']
                or current.get('completion_correction', {}).get('corrected_request_id') != existing.request_id):
            raise ProtocolError('assignment already accepted a conflicting result')
    elif current['state'] not in {'running', 'uncertain'}:
        raise ProtocolError('original started assignment cannot accept a correction')
    corrected = copy.deepcopy(payload)
    corrected.update(outcome='completed', failure=None)
    corrected['result_sha256'] = result_hash(corrected)
    body = {'device_id': device, 'correction_schema': SCHEMA, 'original_result': original,
            'corrected_result': corrected, 'exit_evidence_json': exit_bytes.decode('utf-8'),
            'exit_evidence_sha256': expected_exit_sha256}
    limit = bootstrap.get('limits', {}).get('max_result_bytes')
    sized = {**body, 'request_id': '0' * 32, 'corrected_result': {**corrected, 'request_id': '0' * 32}}
    metadata_size = len(json.dumps(sized, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())
    if metadata_size > 65536:
        raise ArtifactError('correction metadata exceeds official part limit')
    if limit is not None and metadata_size + sum(r['size_bytes'] for r in payload['artifacts']) > limit:
        raise ArtifactError('preserved artifacts exceed current upload budget')
    request = journal.prepare_correction(operation, f'/api/v2/assignments/{aid}/result-correction', body)
    _metadata(journal.root, aid, expected_result_sha256, request.body_json)
    return request, files

def supplement_result(client, aid, expected_result_sha256, expected_exit_sha256):
    """One bounded official upload; unknown ACK preserves byte-identical replay."""
    # Same controller/launch fences as normal runtime; no scheduling object.
    with lock(client.journal.root / 'locks', 'controller'):
        with exclusive(client.journal.root / 'locks' / 'launch.lock'):
            request, files = prepare_supplement(client, aid, expected_result_sha256, expected_exit_sha256)
            reply = client.send_result(request, files)
            correction_receipt(reply, request.request_id, aid, request.body)
            return reply
