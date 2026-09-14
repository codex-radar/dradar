"""Persistent staging for official file-store refresh commands.

A failed native process must never delete the sole observable rotated state.
This does not recover a token that the upstream server issued but the official
CLI never wrote, nor does it coordinate unrelated native writers.
"""
from __future__ import annotations

from pathlib import Path
import os
import json

from .auth_access import project_access
from .auth_authority import Authority
from .auth_refresh import RefreshUnavailable, _sync_directory
from .credential_files import private_directory, read_private_credential, atomic_private_credential


def refresh_codex_staged(authority: Authority, local_key: bytes, root: Path, rpc, *, expected_revision: str | None = None, rejected: bool = False) -> None:
    """Called ONLY under the host gate after the native-writer policy check.

    Persistent candidate data survives native failure, partial JSON, account
    change, and publication failure. No directory is recursively cleaned. The
    caller's pending intent blocks reuse until a provider-specific recovery
    audit determines the authoritative generation. No backup RT is restored.
    """
    if os.name == 'nt':
        raise RefreshUnavailable('durability_unverified')
    if authority.provider != 'codex':
        raise RefreshUnavailable('provider_mismatch')
    before_bytes = authority.read()
    before = project_access('codex', before_bytes, local_key=local_key)
    if expected_revision is not None and before.revision != expected_revision:
        raise RefreshUnavailable('authority_changed')
    if before.principal is None:
        raise RefreshUnavailable('authority_identity_unverified')
    # One persistent attempt per prior generation: a later successful rotation
    # gets its own directory without overwriting earlier recovery evidence.
    private_directory(root)
    root = root / before.revision
    private_directory(root)
    candidate = root / 'candidate-home'
    # Never reuse an old staging HOME that might contain a newer refresh chain.
    try:
        candidate.mkdir(mode=0o700)
    except FileExistsError:
        raise RefreshUnavailable('recovery_required') from None
    _sync_directory(root)
    stage_auth = candidate / 'auth.json'
    atomic_private_credential(stage_auth, before_bytes)
    atomic_private_credential(candidate / 'config.toml', b'cli_auth_credentials_store = "file"\n')
    _sync_directory(candidate)
    transaction = {'schema': 'dradar.native_refresh.v1', 'before': before.revision,
                   'principal': before.principal, 'stage': 'prepared'}
    atomic_private_credential(root / 'transaction.json', json.dumps(transaction, sort_keys=True).encode())
    _sync_directory(root)
    # Staging/fsync can take time. Refuse a changed or unreadable authority
    # immediately before invoking native refresh, not only before publication.
    # This closes the staging gap, not the uncoordinated-writer race after a
    # check; controlled-writer admission remains mandatory.
    try:
        current_bytes = authority.read()
    except Exception:
        current_bytes = None
    if current_bytes != before_bytes:
        transaction['stage'] = 'source-conflict-before-native'
        atomic_private_credential(root / 'transaction.json', json.dumps(transaction, sort_keys=True).encode())
        _sync_directory(root)
        raise RefreshUnavailable('authority_changed')
    # In the pinned 0.154.0 contract, account/read invokes native proactive
    # refresh for an expiring AT. Forcing another refresh can rotate twice.
    # A rejected, unexpired generation needs explicit native renewal. Within
    # the pinned five-minute proactive window, native account/read already
    # renews. Crossing that window during RPC can cause native redundancy;
    # every rotation still stays in the same durable candidate and host gate.
    force = rejected and before.usable(margin=300)
    if rpc.account_read(candidate, refresh=force) != 'chatgpt':
        raise RefreshUnavailable('authority_mode_changed')
    after_bytes = read_private_credential(stage_auth)
    after = project_access('codex', after_bytes, local_key=local_key)
    if (not after.usable() or after.principal != before.principal
            or after.revision == before.revision):
        raise RefreshUnavailable('recovery_required')
    # Durably retain a complete post-refresh snapshot before touching authority.
    atomic_private_credential(root / 'validated-candidate.json', after_bytes)
    transaction.update(stage='validated', after=after.revision)
    atomic_private_credential(root / 'transaction.json', json.dumps(transaction, sort_keys=True).encode())
    _sync_directory(root)
    try:
        unchanged = authority.read() == before_bytes
    except Exception:
        unchanged = False
    if not unchanged:
        transaction['stage'] = 'source-conflict'
        atomic_private_credential(root / 'transaction.json', json.dumps(transaction, sort_keys=True).encode())
        _sync_directory(root)
        raise RefreshUnavailable('authority_changed')
    atomic_private_credential(authority.path, after_bytes)
    _sync_directory(authority.path.parent)
    transaction['stage'] = 'published'
    atomic_private_credential(root / 'transaction.json', json.dumps(transaction, sort_keys=True).encode())
    _sync_directory(root)
    # Keep the validated candidate until the caller commits its pending intent.
    # After success, archive/remove only through an explicit safe-point API.


def recover_codex_staged(authority: Authority, local_key: bytes, root: Path, before: str):
    """Forward-only recovery under the host gate; never issue a refresh RPC.

    A recorded external-source conflict is terminal for automatic recovery.
    Only an untouched pre-generation or this exact validated post-generation
    can be reconciled. Another same-account login is not sufficient evidence.
    """
    from .auth_refresh import AccessState
    import re
    if not isinstance(before, str) or not re.fullmatch(r'[a-f0-9]{32}', before):
        raise RefreshUnavailable('recovery_required')
    root = root / before
    try:
        transaction = json.loads(read_private_credential(root / 'transaction.json'))
        if (transaction.get('schema') != 'dradar.native_refresh.v1'
                or transaction.get('before') != before
                or transaction.get('stage') not in {'validated', 'published'}):
            raise ValueError()
        candidate_bytes = read_private_credential(root / 'validated-candidate.json')
        candidate = project_access('codex', candidate_bytes, local_key=local_key)
        current_bytes = authority.read()
        current = project_access('codex', current_bytes, local_key=local_key)
        if (not candidate.usable() or candidate.principal is None
                or candidate.principal != transaction.get('principal')
                or candidate.principal != current.principal
                or candidate.revision != transaction.get('after')
                or candidate.revision == before):
            raise ValueError()
        if current.revision not in {before, candidate.revision}:
            raise RefreshUnavailable('authority_conflict')
        if current.revision == before:
            # Recheck bytes immediately before forward publication. The
            # controlled-writer admission contract must still hold externally.
            if authority.read() != current_bytes:
                raise RefreshUnavailable('authority_conflict')
            atomic_private_credential(authority.path, candidate_bytes)
            _sync_directory(authority.path.parent)
        if authority.read() != candidate_bytes:
            raise RefreshUnavailable('authority_conflict')
        transaction['stage'] = 'published'
        atomic_private_credential(root / 'transaction.json', json.dumps(transaction, sort_keys=True).encode())
        _sync_directory(root)
        return AccessState(candidate.revision, True)
    except RefreshUnavailable:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise RefreshUnavailable('recovery_required') from None
