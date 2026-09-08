"""Private file injection shared by Pier authentication adapters.

This transport contains no login or token-refresh implementation. It snapshots
only explicitly selected files, never uploads an entire user configuration
home, and keeps secrets outside task artifacts even on a partial failure.
"""
from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path, PurePosixPath

try:
    from _dradar_credential_files import read_private_credential
except ModuleNotFoundError as exc:
    if exc.name != '_dradar_credential_files':
        raise
    from dradar.credential_files import read_private_credential


class CredentialDeliveryError(ValueError):
    pass


def _destination(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (not path.is_absolute() or str(path) != value or '..' in path.parts
            or any(char in value for char in ('\x00', '\r', '\n'))
            or len(path.parts) < 4 or path.parts[1] != 'tmp'
            or not re.fullmatch(r'(?:dradar-[A-Za-z0-9_.-]+|codex-secrets|dsh-secrets)', path.parts[2])):
        raise CredentialDeliveryError('credential destination must be a private task authentication path')
    return path


async def _checked(agent, environment, command):
    result = await agent.exec_as_root(environment, command=command, timeout_sec=30)
    if result.return_code != 0:
        # Do not include stdout/stderr: a backend may echo sensitive file data.
        raise CredentialDeliveryError('container credential preparation failed')


async def inject_private_files(agent, environment, files):
    """Inject explicit (local_path, remote_path) pairs as a private batch.

    Existing remote files and symlink components are refused. Host source
    files are never modified. The provider retains ownership of successful
    session cleanup and of any later native refresh / persistence logic.
    """
    files = list(files)
    if not files or len(files) > 272:
        raise CredentialDeliveryError('invalid credential file count')
    destinations = [_destination(str(remote)) for _, remote in files]
    if len(set(destinations)) != len(destinations):
        raise CredentialDeliveryError('duplicate credential destination')
    for left in destinations:
        if any(left in right.parents for right in destinations if left != right):
            raise CredentialDeliveryError('credential file conflicts with another destination directory')
    # Snapshot before touching the container; reads enforce owner-only files
    # and reject links, special files and oversized credentials.
    snapshots = [read_private_credential(Path(source)) for source, _ in files]
    if sum(map(len, snapshots)) > 16 * 1024 * 1024:
        raise CredentialDeliveryError('credential batch is too large')
    uid_result = await agent.exec_as_agent(environment, command='id -u', timeout_sec=30)
    uid = (uid_result.stdout or '').strip()
    if uid_result.return_code != 0 or not re.fullmatch(r'[0-9]+', uid):
        raise CredentialDeliveryError('cannot identify the container credential owner')
    parents = {parent for path in destinations for parent in path.parents if parent != PurePosixPath('/')}
    ordered = sorted(parents, key=lambda path: (len(path.parts), str(path)))
    checks = ['umask 077']
    for parent in ordered:
        quoted = shlex.quote(str(parent))
        checks.append(f'test ! -L {quoted}')
        checks.append(f'if [ -e {quoted} ]; then test -d {quoted}; else mkdir -- {quoted}; fi')
    for path in destinations:
        quoted = shlex.quote(str(path))
        checks += [f'test ! -e {quoted}', f'test ! -L {quoted}']
    private_parents = [path for path in ordered if len(path.parts) > 2]
    checks.append('chmod 700 ' + ' '.join(shlex.quote(str(path)) for path in private_parents))
    await _checked(agent, environment, ' && '.join(checks))
    completed = False
    try:
        with tempfile.TemporaryDirectory(prefix='dradar-credential-upload-') as raw:
            temporary = Path(raw).resolve()
            for index, (data, destination) in enumerate(zip(snapshots, destinations)):
                source = temporary / str(index)
                fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as out:
                    out.write(data)
                await environment.upload_file(source, str(destination))
        targets = ' '.join(shlex.quote(str(path)) for path in destinations)
        directories = ' '.join(shlex.quote(str(path)) for path in private_parents)
        await _checked(agent, environment, f'chmod 600 {targets} && chown {uid} {targets} {directories}')
        completed = True
    finally:
        if not completed:
            # These exact destinations did not exist at preflight. Never remove
            # a parent directory, user source or shared native auth store.
            targets = ' '.join(shlex.quote(str(path)) for path in destinations)
            try:
                await _checked(agent, environment, f'rm -f -- {targets}')
            except Exception:
                # Any residue is inside a 0700 authentication directory, outside
                # collected logs. The original delivery error remains primary.
                pass


class _CredentialUploadEnvironment:
    """Wrap only selected stock-Pier auth uploads; delegate all other calls."""
    def __init__(self, environment, agent, sources):
        self._environment = environment
        self._agent = agent
        self._sources = frozenset(Path(path).absolute() for path in sources)

    def __getattr__(self, name):
        return getattr(self._environment, name)

    async def upload_file(self, source, destination):
        if Path(source).absolute() in self._sources:
            await inject_private_files(self._agent, self._environment, [(source, str(destination))])
        else:
            await self._environment.upload_file(source, destination)


def credential_upload_environment(environment, agent, sources):
    return _CredentialUploadEnvironment(environment, agent, sources) if sources else environment
