"""Host renewal coordination; never stores, transports, or logs OAuth tokens.

This gate coalesces DRadar processes only. A vendor adapter MUST separately
use the official writer contract; our lock is not proof of vendor cooperation.
The durable pending record is deliberately retained on ambiguous outcomes.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
import json
from pathlib import Path
import re
import stat
import time
from typing import Callable, Iterator

from .credential_files import private_directory, read_private_credential


class RefreshUnavailable(RuntimeError):
    """Fixed diagnostic codes only; underlying vendor output is not attached."""


@dataclass(frozen=True)
class AccessState:
    # Adapter-assigned opaque revision of the authoritative store, NOT a token,
    # email, path or unkeyed token digest. Never exported in telemetry.
    revision: str = field(repr=False)
    usable: bool

    def __post_init__(self):
        if not re.fullmatch(r'[a-f0-9]{32}', self.revision) or type(self.usable) is not bool:
            raise ValueError('invalid access state')


@dataclass(frozen=True)
class RefreshResult:
    state: AccessState = field(repr=False)
    outcome: str


@contextmanager
def _lock(path: Path, timeout: float) -> Iterator[None]:
    if path.is_symlink():
        raise RefreshUnavailable('unsafe_lock')
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    locked = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RefreshUnavailable('unsafe_lock')
        if os.name != 'nt' and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise RefreshUnavailable('unsafe_lock')
        if info.st_size == 0:
            os.write(fd, b'\0')
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                pass
            except OSError as exc:
                if exc.errno not in (11, 13, 35):
                    raise RefreshUnavailable('lock_unavailable') from None
            if time.monotonic() >= deadline:
                raise RefreshUnavailable('lock_timeout')
            time.sleep(min(.02, max(0, deadline - time.monotonic())))
        yield
    finally:
        if locked:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _sync_directory(path: Path) -> None:
    # Windows directory durability is not established by this POSIX path.
    # This coordinator is intentionally not enabled there pending native QA.
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class HostRefreshGate:
    """Per-authority gate used by an explicitly selected vendor adapter.

    chain_id is a locally assigned stable ID for one authoritative login store.
    Discovery must resolve aliases to the same ID; a same-email second login is
    a different chain. This class cannot infer chains from secret contents.
    No runtime activation or vendor adapter is implied by constructing a gate.
    """
    def __init__(self, root: Path, chain_id: str):
        if not re.fullmatch(r'[a-f0-9]{32}', chain_id):
            raise ValueError('invalid authority identifier')
        self._root = root / chain_id

    def ensure(self, read: Callable[[], AccessState],
               renew: Callable[[], None], *, timeout: float = 30,
               before_renew: Callable[[], None] | None = None) -> RefreshResult:
        """Read under the gate; renew only an unusable, non-pending state.

        renew must run the fixed official command with its own bounded timeout,
        respect the native CLI lock, and return only after durable persistence.
        It must never invoke quota queries, models, login UI, or task execution.
        An exception leaves pending intact even if new vendor credentials exist.
        Recovery is a separate adapter-specific audit, never an automatic retry.
        """
        if os.name == 'nt':
            raise RefreshUnavailable('durability_unverified')
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise ValueError('invalid lock timeout')
        private_directory(self._root)
        with _lock(self._root / 'gate.lock', timeout):
            pending = self._root / 'pending.json'
            if pending.exists() or pending.is_symlink():
                raise RefreshUnavailable('recovery_required')
            try:
                before = read()
                if not isinstance(before, AccessState):
                    raise ValueError('invalid adapter result')
            except Exception:
                raise RefreshUnavailable('source_unavailable') from None
            if before.usable:
                return RefreshResult(before, 'reused')
            # Capability refusal happens before any potentially rotating call.
            if before_renew is not None:
                before_renew()
            # Persist intent BEFORE invoking a potentially rotating operation.
            # The keyed opaque prior revision is private recovery metadata.
            data = json.dumps({'schema': 'dradar.refresh_intent.v2',
                               'state': 'pending', 'before': before.revision},
                              sort_keys=True).encode() + b'\n'
            fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'wb') as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            _sync_directory(self._root)
            try:
                renew()
                after = read()
                if not isinstance(after, AccessState) or not after.usable or after.revision == before.revision:
                    raise ValueError('renewal not evidenced')
            except Exception:
                raise RefreshUnavailable('recovery_required') from None
            # Reject changes to our intent, even though official credentials are
            # owned by the adapter. Never overwrite or delete vendor credentials.
            if read_private_credential(pending) != data:
                raise RefreshUnavailable('recovery_required')
            pending.unlink()
            _sync_directory(self._root)
            return RefreshResult(after, 'refreshed')

    def recover(self, read: Callable[[], AccessState],
                audit: Callable[[AccessState], bool], *, timeout: float = 30) -> RefreshResult:
        """Clear an intent only after an adapter audits the changed authority.

        audit must prove identity continuity, durable vendor storage, and that
        no other native writer can restore the prior snapshot. A mere parsed
        expiry or successful file copy is NOT such proof. No refresh is issued.
        v1/corrupt intent or unchanged state remains blocked for manual audit.
        """
        if os.name == 'nt':
            raise RefreshUnavailable('durability_unverified')
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise ValueError('invalid lock timeout')
        private_directory(self._root)
        with _lock(self._root / 'gate.lock', timeout):
            pending = self._root / 'pending.json'
            try:
                raw = read_private_credential(pending)
                intent = json.loads(raw)
                if (not isinstance(intent, dict) or set(intent) != {'schema', 'state', 'before'}
                        or intent['schema'] != 'dradar.refresh_intent.v2'
                        or intent['state'] != 'pending'
                        or not isinstance(intent['before'], str)
                        or not re.fullmatch(r'[a-f0-9]{32}', intent['before'])):
                    raise ValueError()
                current = read()
                if not isinstance(current, AccessState) or not current.usable or current.revision == intent['before']:
                    raise ValueError()
                if audit(current) is not True:
                    raise ValueError()
                # Audit may involve a child process; re-read after it and refuse
                # any generation/intent change before removing the gate.
                if read() != current or read_private_credential(pending) != raw:
                    raise ValueError()
            except Exception:
                raise RefreshUnavailable('recovery_required') from None
            pending.unlink()
            _sync_directory(self._root)
            return RefreshResult(current, 'recovered')
