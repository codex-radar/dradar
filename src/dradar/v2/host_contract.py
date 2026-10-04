"""Explicit local binding for credential-free Codex 0.160 remote execution.

The existing file-auth/Pier provider is deliberately a separate capability.
No credentials are read, copied, inferred or installed by this module.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import stat

VERSION = '0.160.0'
CAPABILITY = 'codex-gpt6-1-sol-host-remote-v1'
AUTH_RUNTIME = 'codex-host-keyring-remote-v1'
MODEL = 'gpt-6.1-sol'
from ..gpt6 import GPT6_EFFORTS
EFFORTS = GPT6_EFFORTS[MODEL]
PROVIDER = 'openai'
BILLING_MODE = 'subscription'
MODEL_CAPABILITY = 'codex-gpt6-1-sol-v1'
from .mixed_pool import CATALOG_VERSION as SERVER_CATALOG_VERSION
SERVER_CONTRIBUTION_POLICY = {
    'missing_reward_basis': 'defer',
    'points_until_basis_resolved': None,
    'deferred_state': 'deferred_reward_missing_basis',
    'known_basis_policy': 'existing_codex_frozen_reward',
}
WIRE_CAPABILITIES = ('on-demand-v2', MODEL_CAPABILITY, AUTH_RUNTIME)
MIXED_WIRE_CAPABILITIES = WIRE_CAPABILITIES + ('on-demand-v2-mixed-pool-v1',)
MIXED_SCHEMA = 'dradar.codex_host_binding.v2'
MIXED_CONFIG_VERSION = 'host-remote-0160-final64-v1'
SCHEMA = 'dradar.codex_host_binding.v1'
CONFIG_VERSION = 'host-remote-0160-v1'
BENCHMARK_POLICIES = {
    'deepswe15-20261003-v4': 'deep-swe',
    'pompeii16-20261003-v2': 'pompeii-adjacency',
    'tb4-scc-pilot-20261003': 'tb4',
    'science-sr-pilot-20261003': 'science',
    'tb4-selected17-20261004-v2': 'tb4',
    'science-selected20-20261004-v2': 'science',
}

from .mixed_pool import SOURCES as _CURRENT_SOURCES
for _source in _CURRENT_SOURCES.values():
    _policy={'deepswe':'deep-swe','pompeii':'pompeii-adjacency','tb4':'tb4','science':'science'}[_source['collection_id']]
    BENCHMARK_POLICIES[_source['benchmark']]=_policy

def digest(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None

def absolute(value):
    if not isinstance(value,str) or not value or '\x00' in value:
        raise ValueError('absolute local binding path required')
    p=Path(value)
    if not p.is_absolute() or '..' in p.parts:
        raise ValueError('absolute local binding path required')
    return p

def policy_id(benchmark):
    return BENCHMARK_POLICIES.get(benchmark, benchmark)

def require_server_library(bootstrap, benchmark, model, effort):
    """Exact current64 contract; paused metadata cannot authorize work."""
    from .mixed_pool import POOL, selection_scope
    if benchmark == POOL:
        return selection_scope(bootstrap, model, effort)
    library = bootstrap.get('library_catalog')
    if not isinstance(library, dict) or library.get('catalog_version') != SERVER_CATALOG_VERSION:
        raise ValueError('current64 library catalog binding required; no guessed capability')
    policy = bootstrap.get('contribution_policy')
    if not isinstance(policy, dict) or any(
        key not in policy or policy[key] != value
        for key, value in SERVER_CONTRIBUTION_POLICY.items()
    ):
        raise ValueError('Server021 deferred contribution policy required; upgrade the Server before new claims')
    rows = [c for c in library.get('collections', []) if c.get('benchmark') == benchmark]
    if len(rows) != 1:
        raise ValueError('exact versioned Server library required')
    row = rows[0]
    if set(row.get('required_client_capabilities', [])) != {MODEL_CAPABILITY, AUTH_RUNTIME}:
        raise ValueError('Server library host remote capability mismatch')
    if row.get('production_claim_enabled') is not True or row.get('missing_bindings'):
        raise ValueError('Server library remains pending; no new claim')
    if model != MODEL or effort not in EFFORTS or {'model': model, 'effort': effort} not in row.get('model_effort_selections', []):
        raise ValueError('selection outside explicitly configured Server model/effort mapping')
    return row

def private_json(path):
    """No symlink traversal; local configuration contains only public bindings."""
    p=absolute(str(path))
    if p.resolve(strict=True) != p:
        raise ValueError('binding path must be canonical')
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as f:
        info=os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():
            raise ValueError('owned regular binding file required')
        raw=f.read(1048577)
        if len(raw)>1048576:raise ValueError('binding file too large')
    return raw

def load_binding(path, expected_sha256):
    if not digest(expected_sha256):raise ValueError('trusted binding SHA256 required')
    raw=private_json(path)
    if hashlib.sha256(raw).hexdigest()!=expected_sha256:
        raise ValueError('host runtime binding digest mismatch')
    c=json.loads(raw)
    if not isinstance(c,dict):raise ValueError('host runtime binding must be an object')
    mixed = c.get('schema') == MIXED_SCHEMA
    expected_schema = MIXED_SCHEMA if mixed else SCHEMA
    expected_version = MIXED_CONFIG_VERSION if mixed else CONFIG_VERSION
    if set(c)!={'schema','runtime_config_version','capability','model','efforts','host','tasks'} or (
        c['schema']!=expected_schema or c['runtime_config_version']!=expected_version
        or c['capability']!=CAPABILITY or c['model']!=MODEL or c['efforts']!=list(EFFORTS)):
        raise ValueError('unverified host runtime capability/model/effort')
    h=c['host']
    if set(h)!={'host_home','host_cli','host_cli_sha256','host_companion','host_companion_sha256','docker_context','proxy_image_id'}:
        raise ValueError('explicit host binary and egress binding required')
    for key in ['host_home','host_cli','host_companion']:absolute(h[key])
    if absolute(h['host_home'])!=Path.home()/'.local/share/dradar/codex-host-home':
        raise ValueError('dedicated host identity required; original home forbidden')
    if not all(digest(h[k])for k in ['host_cli_sha256','host_companion_sha256']):
        raise ValueError('official host binary digests required')
    if Path(h['host_cli']).with_name('codex-code-mode-host')!=Path(h['host_companion']):
        raise ValueError('same official package companion required')
    if not isinstance(h['docker_context'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]+',h['docker_context']):
        raise ValueError('explicit Docker context required')
    if not isinstance(h['proxy_image_id'],str)or not re.fullmatch(r'sha256:[a-f0-9]{64}',h['proxy_image_id']):
        raise ValueError('immutable official egress image required')
    if not isinstance(c['tasks'],list):raise ValueError('explicit public task bindings required')
    seen=set()
    for t in c['tasks']:
        required={'benchmark','policy_id','task_id','task_content_hash','image_id','git_head','cpus','memory_bytes','agent_timeout_sec','official_task_timeout_sec','verifier_timeout_sec','executor_cli','executor_path','executor_home','public_files','collector_path','collector_sha256','deliverable_names'}
        if mixed:required = required | {'source_root'}
        if set(t)!=required:raise ValueError('incomplete or extra public task binding fields')
        if mixed:
            from .mixed_pool import MEMBER_HASHES, SOURCES
            if MEMBER_HASHES.get((t['benchmark'],t['task_id'])) != t['task_content_hash'] or t['benchmark'] not in SOURCES:
                raise ValueError('runtime task outside fixed final64 membership')
            absolute(t['source_root'])
        if t['benchmark'] not in BENCHMARK_POLICIES or t['policy_id']!=policy_id(t['benchmark']):
            raise ValueError('unknown versioned benchmark or policy mismatch')
        if not isinstance(t['task_id'],str)or not re.fullmatch(r'[A-Za-z0-9_-]+',t['task_id']):
            raise ValueError('single public task identifier required')
        key=(t['benchmark'],t['task_id'])
        if key in seen:raise ValueError('duplicate public task binding')
        seen.add(key)
        if not digest(t['task_content_hash'])or not digest(t['collector_sha256']):raise ValueError('public input/collector digest missing')
        if not isinstance(t['image_id'],str)or not re.fullmatch(r'sha256:[a-f0-9]{64}',t['image_id']):raise ValueError('immutable task image required')
        if not isinstance(t['git_head'],str)or not re.fullmatch(r'[a-f0-9]{40}',t['git_head']):raise ValueError('immutable workspace baseline required')
        for k in ['cpus','memory_bytes','agent_timeout_sec','official_task_timeout_sec','verifier_timeout_sec']:
            if type(t[k]) is not int or t[k]<=0:raise ValueError('positive exact resource/time policy required')
        if t['cpus']>16 or t['memory_bytes']>64*1024**3 or t['agent_timeout_sec']>28800:
            raise ValueError('bounded approved runtime policy required')
        # Preserve the actual Codex Pompeii hard agent deadline. Other resources
        # come from the verified public pack/019 binding, never from a display name.
        if t['policy_id']=='pompeii-adjacency' and t['agent_timeout_sec']!=7200:
            raise ValueError('Pompeii Codex policy requires original7200s agent deadline')
        if t['policy_id']=='pompeii-adjacency' and t['official_task_timeout_sec'] not in [5400,7200]:
            raise ValueError('unreviewed Pompeii public timeout policy')
        for k in ['collector_path','executor_cli','executor_home']:absolute(t[k])
        if not isinstance(t['executor_path'],str)or any(not part.startswith('/')for part in t['executor_path'].split(':')):
            raise ValueError('explicit Linux executable PATH required')
        if not isinstance(t['public_files'],dict)or not t['public_files']:
            raise ValueError('fixed image public input hashes required')
        if any(not str(k).startswith('/app/')or not digest(v) for k,v in t['public_files'].items()):
            raise ValueError('public image inputs must be fixed /app paths')
        names=t['deliverable_names']
        if not isinstance(names,list)or not names or len(set(names))!=len(names)or any(not re.fullmatch(r'[A-Za-z0-9_.-]+',n)or n in ['.','..']for n in names):
            raise ValueError('bounded output allowlist required')
    if mixed:
        from .mixed_pool import MEMBER_HASHES, SOURCES
        if seen != set(MEMBER_HASHES):raise ValueError('complete exact64 runtime binding required')
        roots = {b:{t['source_root'] for t in c['tasks'] if t['benchmark']==b} for b in SOURCES}
        if any(len(r)!=1 for r in roots.values()) or len({next(iter(r)) for r in roots.values()})!=4:
            raise ValueError('four distinct fixed source roots required')
    return c

def validate_controller_config(c):
    # The controller is launched only by the installed adapter, not a Server URL
    # or command string. It refuses alternate auth/model/egress configurations.
    if c.get('capability')!=CAPABILITY or c.get('runtime_config_version') not in {CONFIG_VERSION, MIXED_CONFIG_VERSION}:
        raise ValueError('unbound controller capability')
    if c.get('model')!=MODEL or c.get('effort') not in EFFORTS or c.get('max_model_parallel')!=2:
        raise ValueError('unverified model or parallel policy')
    for key in ['evidence_dir','control_socket','lock_path']:absolute(c[key])
    if not re.fullmatch(r'dradar-host-[a-f0-9]{24}',c.get('container_name','')):
        raise ValueError('owned exact container scope required')
    return c


def confirmed_absence(daemon_returncode, daemon_version, inspect_returncode, inspect_stderr, container_id):
    """A reachable daemon and an exact missing-object response, never any error."""
    if daemon_returncode!=0 or not daemon_version.strip() or inspect_returncode==0:
        return False
    if not isinstance(container_id,str)or not re.fullmatch(r'[a-f0-9]{64}',container_id):
        return False
    if isinstance(inspect_stderr,bytes):inspect_stderr=inspect_stderr.decode('utf-8',errors='replace')
    pattern=r'(?:error(?:(?: response from daemon))?: )?no such (?:object|container): '+re.escape(container_id)
    return re.fullmatch(pattern,inspect_stderr.strip().lower()) is not None


def validate_host_version(returncode, stdout):
    if returncode!=0 or stdout.strip()!='codex-cli '+VERSION:
        raise ValueError('actual official host runtime version mismatch; no fallback')
