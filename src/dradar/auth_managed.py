"""Admission gate for dedicated, newly provisioned Codex login stores.

Default/user-selected official CLI stores are never adopted into this mode.
A dedicated store is created empty and populated by a pinned official login.
All participating refresh writers must use its host gate. Other official CLI
processes using their normal homes do not receive this store's refresh token.
Manually pointing unrelated processes at this private home is outside this
controlled contract; owner-only permissions are not a same-user security jail.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import subprocess
import shutil
import uuid

from .auth_access import project_access
from .auth_authority import Authority, select_authority
from .auth_refresh import RefreshUnavailable, _sync_directory, _lock
from .credential_files import private_directory, read_private_credential, atomic_private_credential

# Official npm @openai/codex@0.154.0-darwin-arm64, independently checked against
# the npm distribution integrity. Other binaries/versions are not admitted.
_CODEX_PINS = {('Darwin', 'arm64'): '4f85982624b3898c8991cb80c0981b2aa71070e3537046c9a95950318a95afcc'}


def _check_runtime(executable: Path) -> str:
    expected = _CODEX_PINS.get((platform.system(), platform.machine()))
    if expected is None:
        raise RefreshUnavailable('managed_runtime_unverified')
    try:
        if not executable.is_absolute() or executable.is_symlink():
            raise ValueError()
        with executable.open('rb') as source:
            actual = hashlib.file_digest(source, 'sha256').hexdigest()
        if actual != expected:
            raise ValueError()
    except (OSError, ValueError):
        raise RefreshUnavailable('managed_runtime_pin_mismatch') from None
    return actual


def _login(executable: Path, home: Path) -> None:
    # This function is an explicit interactive login action, never called by
    # discovery, status, run startup, or automatic refresh. Tokens stay in HOME.
    from .auth_codex_rpc import _network_environment
    try:
        result = subprocess.run([str(executable), '-c', 'cli_auth_credentials_store="file"', 'login', '--device-auth'],
            cwd=home, env={**_network_environment(), 'PATH': os.defpath, 'HOME': str(home), 'CODEX_HOME': str(home)},
            timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        raise RefreshUnavailable('managed_login_incomplete') from None
    if result.returncode != 0:
        raise RefreshUnavailable('managed_login_incomplete')


@dataclass
class ManagedAuthStore:
    root: Path = field(repr=False)

    def _key(self, *, create: bool = False) -> bytes:
        if create:
            private_directory(self.root)
        path = self.root / 'control.key'
        if create:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'wb') as target:
                    target.write(os.urandom(32)); target.flush(); os.fsync(target.fileno())
                _sync_directory(self.root)
        try:
            key = read_private_credential(path)
            if len(key) != 32:
                raise ValueError()
            return key
        except (OSError, ValueError):
            raise RefreshUnavailable('managed_authority_unavailable') from None

    @staticmethod
    def _bytes(payload: dict) -> bytes:
        return json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()

    def _runtime(self, executable: Path) -> Path:
        """Pin a private executable copy before exposing any managed HOME."""
        digest = _check_runtime(executable)
        private_directory(self.root)
        private_directory(self.root / 'runtimes')
        directory = self.root / 'runtimes' / digest
        private_directory(directory)
        target = directory / 'codex'
        if target.exists() or target.is_symlink():
            _check_runtime(target)
            return target
        temporary = directory / ('stage-' + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        try:
            with os.fdopen(fd, 'wb') as out, executable.open('rb') as source:
                shutil.copyfileobj(source, out)
                out.flush(); os.fsync(out.fileno())
            # Source upgrades during the copy cannot admit different bytes.
            _check_runtime(temporary)
            os.replace(temporary, target); _sync_directory(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def login(self, executable: Path) -> Authority:
        """Explicitly start fresh login; never import or copy an existing RT."""
        executable = self._runtime(executable)
        digest = _check_runtime(executable)
        key = self._key(create=True)
        homes = self.root / 'authorities'; private_directory(homes)
        home = homes / uuid.uuid4().hex; home.mkdir(mode=0o700); _sync_directory(homes)
        atomic_private_credential(home / 'setup.pending', b'new-login-in-progress\n')
        atomic_private_credential(home / 'config.toml', b'cli_auth_credentials_store = "file"\n')
        _sync_directory(home)
        _login(executable, home)
        authority = select_authority('codex', [home / 'auth.json'], local_key=key)
        material = project_access('codex', authority.read(), local_key=key)
        if material.principal is None or not material.usable():
            raise RefreshUnavailable('managed_login_unverified')
        payload = {'schema': 'dradar.managed_authority.v1', 'origin': 'fresh-official-login',
                   'store_id': authority.store_id, 'principal': material.principal, 'runtime_sha256': digest}
        mac = hmac.new(key, self._bytes(payload), hashlib.sha256).hexdigest()
        atomic_private_credential(home / 'custody.json', self._bytes({'payload': payload, 'mac': mac}))
        _sync_directory(home)
        (home / 'setup.pending').unlink(); _sync_directory(home)
        return authority

    def guard(self, authority: Authority, executable: Path) -> ManagedAuthGuard:
        guard = ManagedAuthGuard(self, authority, executable)
        guard()
        return guard

    def session(self, authority: Authority, executable: Path, *, observe=None):
        """Admit only a receipt-bound store, even when its current AT is valid."""
        from .auth_codex_rpc import CodexAccountRpc
        from .auth_host_session import codex_host_session
        executable = self._runtime(executable)
        guard = self.guard(authority, executable)
        session = codex_host_session(authority, self._key(), self.root / 'gates',
            CodexAccountRpc(executable, _check_runtime(executable)), guard, observe)
        session.check_session_contract = guard
        return session

    def recover(self, authority: Authority, executable: Path):
        """Explicit forward recovery, with no login or provider request."""
        from .auth_transaction import recover_codex_staged
        self.guard(authority, executable)()
        root = self.root / 'gates' / authority.store_id
        private_directory(root)
        with _lock(root / 'gate.lock', 30):
            pending = root / 'pending.json'
            try:
                raw = read_private_credential(pending)
                intent = json.loads(raw)
                if (set(intent) != {'schema', 'state', 'before'}
                        or intent['schema'] != 'dradar.refresh_intent.v2'
                        or intent['state'] != 'pending'):
                    raise ValueError()
            except (OSError, ValueError, TypeError, KeyError):
                raise RefreshUnavailable('recovery_required') from None
            state = recover_codex_staged(authority, self._key(), root / 'native', intent['before'])
            if read_private_credential(pending) != raw:
                raise RefreshUnavailable('recovery_required')
            pending.unlink(); _sync_directory(root)
            return state


@dataclass(frozen=True)
class ManagedAuthGuard:
    store: ManagedAuthStore = field(repr=False)
    authority: Authority = field(repr=False)
    executable: Path = field(repr=False)

    def __call__(self) -> None:
        digest = _check_runtime(self.executable)
        key = self.store._key()
        home = self.authority.path.parent
        revoked = home / 'revoked.json'
        if revoked.exists() or revoked.is_symlink():
            raise RefreshUnavailable('managed_authority_revoked')
        if (self.authority.provider != 'codex' or self.authority.path.name != 'auth.json'
                or home.parent != self.store.root / 'authorities'
                or (home / 'setup.pending').exists() or (home / 'setup.pending').is_symlink()):
            raise RefreshUnavailable('managed_custody_unverified')
        try:
            envelope = json.loads(read_private_credential(home / 'custody.json'))
            payload = envelope['payload']
            mac = hmac.new(key, self.store._bytes(payload), hashlib.sha256).hexdigest()
            if not isinstance(envelope['mac'], str) or not hmac.compare_digest(mac, envelope['mac']):
                raise ValueError()
            current = project_access('codex', self.authority.read(), local_key=key)
            selected = select_authority('codex', [self.authority.path], local_key=key)
            expected = {'schema': 'dradar.managed_authority.v1', 'origin': 'fresh-official-login',
                        'store_id': selected.store_id, 'principal': current.principal, 'runtime_sha256': digest}
            if payload != expected or self.authority.store_id != selected.store_id or current.principal is None:
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise RefreshUnavailable('managed_custody_unverified') from None
