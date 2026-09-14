"""Explicit selection of one local credential authority; no login side effects."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
from pathlib import Path
from typing import Iterable

from .credential_files import read_private_credential


class AuthorityUnavailable(ValueError):
    pass


def _snapshot(path: Path) -> bytes:
    # A same-store atomic replacement between lstat and open is a transient
    # read race, not a reason to choose another account. Revalidate once only.
    for attempt in range(2):
        try:
            return read_private_credential(path)
        except ValueError as error:
            if attempt == 0 and str(error) == 'credential source changed while opening':
                continue
            raise
    raise AuthorityUnavailable('credential_source_unavailable')


@dataclass(frozen=True)
class Authority:
    provider: str
    path: Path = field(repr=False)
    store_id: str = field(repr=False)

    def read(self) -> bytes:
        try:
            return _snapshot(self.path)
        except (OSError, ValueError):
            raise AuthorityUnavailable('credential_source_unavailable') from None


def select_authority(provider: str, candidates: Iterable[Path], *,
                     local_key: bytes, explicit: Path | None = None) -> Authority:
    """Resolve only caller-supplied official locations, never scan a home.

    The HMAC identifies an authoritative STORE, not an OAuth token family.
    Copies at different paths cannot be proven to be independent chains. The
    renewal capability must separately establish that no uncoordinated copies
    of the same grant are active. Neither email nor token bytes are an ID.
    An explicit source never falls back when missing or invalid.
    """
    if provider not in {'codex', 'claude-code', 'kimi-code', 'grok-build',
                        'antigravity', 'codebuddy', 'zcode', 'deepseek'}:
        raise AuthorityUnavailable('unsupported_provider')
    if not isinstance(local_key, bytes) or len(local_key) < 32:
        raise AuthorityUnavailable('authority_key_unavailable')
    paths = [explicit] if explicit is not None else list(candidates)
    selected: dict[tuple[int, int], Path] = {}
    for path in paths:
        try:
            # Reject symlinks instead of quietly switching the selected source.
            # Callers may canonicalize a known OS HOME alias, never the file.
            absolute = path.absolute()
            if not absolute.exists() and not absolute.is_symlink() and explicit is None:
                continue
            _snapshot(absolute)
            if absolute.resolve(strict=True) != absolute:
                raise AuthorityUnavailable('credential_source_noncanonical')
            info = absolute.stat()
            if info.st_nlink != 1:
                raise AuthorityUnavailable('credential_source_linked')
            selected[(info.st_dev, info.st_ino)] = absolute
        except (OSError, ValueError):
            raise AuthorityUnavailable('credential_source_unavailable') from None
    if not selected:
        raise AuthorityUnavailable('login_required')
    if len(selected) != 1:
        raise AuthorityUnavailable('credential_source_ambiguous')
    path = next(iter(selected.values()))
    # Path identity survives atomic replacement by the official CLI. A re-login
    # at this path is serialized conservatively, not mistaken for a second store.
    identity = provider.encode() + b'\0' + str(path).encode()
    store_id = hmac.new(local_key, identity, hashlib.sha256).hexdigest()[:32]
    return Authority(provider, path, store_id)
