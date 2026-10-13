"""Exact current64 mixed selection; source tasks retain their original identity."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from importlib.resources import files

CONTRACT = json.loads(files('dradar.v2').joinpath('final64_contract.json').read_text(encoding='utf-8'))
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

if len(MEMBERS) != 64 or len(MEMBER_HASHES) != 64 or members_digest(MEMBERS) != MEMBERS_SHA256:
    raise ValueError('packaged final64 membership commitment mismatch')







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




def validate_selected_public_root(task):
    """Only the selected task's archive marker and inputs; full bytes pin at prepare."""
    MARKER = ".dradar-task-bundle.json"
    root,base=public_task_root(task)
    marker=base/MARKER
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size>1048576:
        raise ValueError('selected source regular archive marker required')
    value=json.loads(marker.read_text())
    benchmark=task['benchmark']
    if not isinstance(value,dict) or value.get('benchmark_id')!=benchmark or value.get('sha256')!=SOURCES[benchmark]['public_bundle']['sha256']:
        raise ValueError('selected source archive marker mismatch')
    directory=root/task['task_id']
    if directory.is_symlink() or not directory.is_dir():raise ValueError('selected public task directory required')
    for name in ('instruction.md','task.toml'):
        p=directory/name
        if not p.is_file() or p.resolve(strict=True)!=p:raise ValueError('selected regular public inputs required')
    return root,base
