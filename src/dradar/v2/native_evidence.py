"""Validate current host evidence without inventing legacy agent fields.

These are consistency checks, as in the legacy adapter; they are not a
cryptographic attestation. Server-side assignment/runtime/library checks and
the official verifier remain authoritative.
"""
from hashlib import sha256
from pathlib import PurePosixPath
import re

ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
DIGEST = re.compile(r'^[0-9a-f]{64}$')

def native_identity(result, events, model, effort, *, task, runner, patch):
    if runner.get('agent') != 'codex' or runner.get('provider') != 'openai' or runner.get('auth_runtime') != 'codex-host-keyring-remote-v1' or runner.get('agent_version') != '0.160.0' or runner.get('agent_version_verified') is not True:
        raise ValueError('native host evidence requires the bound verified runner')
    if any(k in result for k in ('config', 'agent_info')):
        raise ValueError('ambiguous native and legacy identity')
    if (result.get('model'), result.get('reasoning'), result.get('task')) != (model, effort, task['task_id']):
        raise ValueError('native host run identity mismatch')
    if result.get('model_turn_status') != 'completed' or result.get('model_turn_error') is not None:
        raise ValueError('native model turn did not complete')
    if not isinstance(events, list) or not events:
        raise ValueError('native host terminal events are required')
    pair = None
    terminal = None
    usage = None
    for event in events:
        if not isinstance(event, dict):
            raise ValueError('native event must be an object')
        method = event.get('method')
        if terminal is not None:
            raise ValueError('native event follows terminal event')
        if method == 'thread/tokenUsage/updated':
            params = event.get('params')
            if not isinstance(params, dict):
                raise ValueError('native usage params missing')
            ids = (params.get('threadId'), params.get('turnId'))
            observed = params.get('tokenUsage')
            if not isinstance(observed, dict) or not isinstance(observed.get('total'), dict):
                raise ValueError('native token observation missing')
            counters = observed['total']
            for k in ('inputTokens', 'outputTokens', 'totalTokens'):
                if type(counters.get(k)) is not int or counters[k] < 0:
                    raise ValueError('invalid native token observation')
            if counters['inputTokens'] + counters['outputTokens'] != counters['totalTokens']:
                raise ValueError('native token total mismatch')
            if usage is not None and any(counters[k] < usage[k] for k in ('inputTokens', 'outputTokens', 'totalTokens')):
                raise ValueError('native cumulative usage moved backwards')
            usage = counters
        elif method == 'turn/completed':
            ids = (event.get('threadId'), event.get('turnId'))
            if event.get('status') != 'completed' or event.get('error_code') is not None:
                raise ValueError('native terminal event is not completed')
            terminal = event
        else:
            raise ValueError('unknown native identity event')
        if any(not isinstance(value, str) or not ID.fullmatch(value) for value in ids):
            raise ValueError('native event identity missing')
        if pair is not None and pair != ids:
            raise ValueError('native events name different thread or turn')
        pair = ids
    if terminal is None or usage is None:
        raise ValueError('matching native terminal and usage required')
    if not isinstance(patch, bytes) or result.get('patch_sha256') != sha256(patch).hexdigest() or type(result.get('patch_bytes')) is not int or result['patch_bytes'] != len(patch):
        raise ValueError('native patch differs from collection evidence')
    return {'schema':'codex-native-host-identity/1', 'model':model, 'effort':effort,
            'task_id':task['task_id'], 'thread_id':pair[0], 'turn_id':pair[1],
            'terminal_status':'completed', 'patch_sha256':sha256(patch).hexdigest(),
            'usage':{k:usage[k] for k in ('inputTokens','outputTokens','totalTokens')}}

def collection_safety(result):
    """Scratch additions do not invalidate required, protected output collection.

    Tracked modifications, deletions, unsafe paths and missing/changed protected
    inputs still fail. Scratch bytes are never added to the submitted patch.
    """
    if result.get('public_inputs_unchanged') is not True or result.get('missing_deliverables') != []:
        raise ValueError('protected inputs changed or required output missing')
    hashes = result.get('public_inputs_sha256')
    outputs = result.get('outputs')
    if not isinstance(hashes, dict) or not hashes or not isinstance(outputs, dict) or not outputs:
        raise ValueError('protected input and output hashes required')
    for name, digest in hashes.items():
        if not isinstance(name, str) or not name.startswith('/app/') or '..' in PurePosixPath(name).parts or '\\' in name or any(ord(c)<32 for c in name) or not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ValueError('invalid protected input hash evidence')
    for name, item in outputs.items():
        if not isinstance(name, str) or not name or name=='.' or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name or any(ord(c)<32 for c in name) or not isinstance(item, dict) or not isinstance(item.get('sha256'), str) or not DIGEST.fullmatch(item['sha256']) or type(item.get('bytes')) is not int or item['bytes'] < 0:
            raise ValueError('invalid collected output evidence')
    changes = result.get('unexpected_workspace_changes')
    if not isinstance(changes, list):
        raise ValueError('workspace change evidence required')
    for line in changes:
        if not isinstance(line, str) or not line.startswith('?? '):
            raise ValueError('tracked workspace modification remains invalid')
        name = line[3:]
        path = PurePosixPath(name)
        if not name or path.is_absolute() or '..' in path.parts or '\\' in name or any(ord(c) < 32 for c in name):
            raise ValueError('unsafe scratch path')
        if '/app/'+name in hashes:
            raise ValueError('scratch conflicts with protected input')
    return {'schema':'required-output-collection/1', 'ignored_untracked_scratch':list(changes),
            'public_inputs_unchanged':True, 'missing_deliverables':[]}
