"""Exact Server024 mixed selection; source tasks retain their original identity."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from importlib.resources import files

CONTRACT = json.loads(files('dradar.v2').joinpath('final68_contract.json').read_text(encoding='utf-8'))
POOL = CONTRACT['pool_benchmark']
POOL_CAPABILITY = CONTRACT['pool_capability']
CATALOG_VERSION = CONTRACT['catalog_version']
MEMBERS_SHA256 = CONTRACT['members_sha256']
MEMBERS = CONTRACT['members']
SOURCES = {x['benchmark']: x for x in CONTRACT['collections']}
MEMBER_HASHES = {(x['source_benchmark'], x['task_id']): x['task_content_hash'] for x in MEMBERS}

def members_digest(members):
    return hashlib.sha256(json.dumps(members, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()

if len(MEMBERS) != 68 or len(MEMBER_HASHES) != 68 or members_digest(MEMBERS) != MEMBERS_SHA256:
    raise ValueError('packaged final68 membership commitment mismatch')

def selection_scope(bootstrap, model, effort):
    try:
        return _selection_scope(bootstrap, model, effort)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('malformed Server024 mixed selection contract') from exc

def _selection_scope(bootstrap, model, effort):
    from .host_contract import (MODEL, EFFORTS, MODEL_CAPABILITY, AUTH_RUNTIME,
                                SERVER_CONTRIBUTION_POLICY)
    if model != MODEL or effort not in EFFORTS:
        raise ValueError('unsupported fixed final68 model/effort')
    if not {'on-demand-v2', POOL_CAPABILITY} <= set(bootstrap.get('capabilities', [])):
        raise ValueError('Server024 mixed capability required before new work')
    library = bootstrap.get('library_catalog') or {}
    if library.get('catalog_version') != CATALOG_VERSION or library.get('total_mapped_tasks') != 68:
        raise ValueError('exact Server024 final68 catalog required')
    policy = bootstrap.get('contribution_policy')
    if not isinstance(policy, dict) or any(k not in policy or policy[k] != v for k,v in SERVER_CONTRIBUTION_POLICY.items()):
        raise ValueError('deferred contribution policy mismatch')
    pool = library.get('unified_pool') or {}
    required = {POOL_CAPABILITY, MODEL_CAPABILITY, AUTH_RUNTIME}
    if (pool.get('benchmark') != POOL or pool.get('selection_version') != CATALOG_VERSION
            or pool.get('task_count') != 68 or pool.get('source_counts') != CONTRACT['source_counts']
            or pool.get('single_start_entry') is not True
            or pool.get('members_sha256') != MEMBERS_SHA256
            or pool.get('members') != MEMBERS or members_digest(pool.get('members')) != MEMBERS_SHA256
            or set(pool.get('required_client_capabilities', [])) != required):
        raise ValueError('exact mixed pool membership/capability binding required')
    if pool.get('production_claim_enabled') is not True:
        raise ValueError('Server final68 pool remains pending; no new claim')
    rows = library.get('collections')
    if not isinstance(rows,list) or len(rows) != 4 or {x.get('benchmark') for x in rows} != set(SOURCES):
        raise ValueError('complete four-source library binding required')
    for row in rows:
        source = SOURCES[row['benchmark']]
        if any(row.get(k) != source[k] for k in ('collection_id','benchmark','selection_version','task_count')):
            raise ValueError('source identity/count mismatch')
        if set(row.get('required_client_capabilities', [])) != {MODEL_CAPABILITY, AUTH_RUNTIME}:
            raise ValueError('source host capability mismatch')
        if row.get('production_claim_enabled') is not True or row.get('missing_bindings'):
            raise ValueError('Server source remains pending; no partial pool')
        if {'model': model,'effort': effort} not in row.get('model_effort_selections', []):
            raise ValueError('source model/effort unavailable')
        hashes = {t:h for (b,t),h in MEMBER_HASHES.items() if b == row['benchmark']}
        bundle = row.get('public_bundle') or {}
        if (row.get('public_task_hashes') != hashes
                or any(bundle.get(k) != v for k,v in source['public_bundle'].items())
                or bundle.get('archive_root_prefix') != source['archive_root_prefix']):
            raise ValueError('fixed source public package/hash mismatch')
    choices = [x for x in bootstrap.get('benchmarks', []) if x.get('benchmark') == POOL]
    if len(choices) != 1 or len(bootstrap.get('benchmarks', [])) != 1:
        raise ValueError('one exact mixed start entry required')
    choice = choices[0]
    if (choice.get('task_count') != 68 or choice.get('source_benchmarks') != list(SOURCES)
            or POOL_CAPABILITY not in choice.get('required_client_capabilities', [])
            or not any(x.get('model') == model and x.get('effort') == effort for x in choice.get('models', []))):
        raise ValueError('complete account-eligible mixed selection required')
    return {'catalog_version': CATALOG_VERSION, 'pool_benchmark': POOL,
            'members_sha256': MEMBERS_SHA256, 'members': MEMBERS}

def validate_assignment(scope, task):
    if (not isinstance(scope,dict) or scope.get('catalog_version') != CATALOG_VERSION
            or scope.get('pool_benchmark') != POOL or scope.get('members_sha256') != MEMBERS_SHA256
            or scope.get('members') != MEMBERS):
        raise ValueError('saved exact mixed selection scope required')
    key = (task.get('benchmark'), task.get('task_id'))
    if MEMBER_HASHES.get(key) != task.get('task_content_hash') or key not in MEMBER_HASHES:
        raise ValueError('assignment outside exact mixed source/task/hash scope')
    expected = SOURCES[key[0]]['public_bundle']
    if task.get('task_bundle') != expected:
        raise ValueError('assignment public bundle differs from fixed source')


def public_task_root(binding_task):
    """The archive marker stays at source root; native tasks use its fixed prefix."""
    from .host_contract import absolute
    source = SOURCES[binding_task['benchmark']]
    base = absolute(binding_task['source_root'])
    if base.resolve(strict=True) != base:
        raise ValueError('canonical immutable source root required')
    root = base / source['archive_root_prefix'] if source['archive_root_prefix'] else base
    if root.resolve(strict=True) != root:
        raise ValueError('canonical native public task root required')
    return root, base

def validate_public_roots(binding):
    try:
        return _validate_public_roots(binding)
    except (OSError,KeyError,TypeError,AttributeError) as exc:
        raise ValueError('complete canonical four-source public packs required before new work') from exc

def _validate_public_roots(binding):
    """All four packs must be present before new work; task bytes pin at prepare."""
    from ..taskpacks import MARKER
    roots={}
    for task in binding['tasks']:
        benchmark=task['benchmark']
        if benchmark not in roots:
            root,base=public_task_root(task)
            marker=base/MARKER
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_size>1048576:
                raise ValueError('regular fixed public archive marker required before new work')
            value=json.loads(marker.read_text())
            if (not isinstance(value,dict) or value.get('benchmark_id')!=benchmark
                    or value.get('sha256')!=SOURCES[benchmark]['public_bundle']['sha256']):
                raise ValueError('fixed source archive marker mismatch before new work')
            roots[benchmark]=root
        directory=roots[benchmark]/task['task_id']
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('complete68 public task directories required before new work')
        for name in ('instruction.md','task.toml'):
            p=directory/name
            if not p.is_file() or p.resolve(strict=True)!=p:
                raise ValueError('complete68 regular public task inputs required before new work')
    return roots
