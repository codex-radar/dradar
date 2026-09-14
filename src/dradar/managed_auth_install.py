"""Pinned public native runtime acquisition, only for explicit managed login."""
from __future__ import annotations
import base64
import hashlib
from pathlib import Path
import platform
import tarfile
import tempfile
import time
import urllib.request

from .auth_managed import ManagedAuthStore, _check_runtime
from .auth_refresh import RefreshUnavailable
from .credential_files import private_directory

URL = 'https://registry.npmjs.org/@openai/codex/-/codex-0.154.0-darwin-arm64.tgz'
INTEGRITY = 'HP/vJCH/t2hB9Kg6hotN9UglClJ6/z584fal5lEP14C9gNAgAQS4/kTQC7l5V+BA3TqwDPwINSjul28cX8AYXg=='
DIGEST = '4f85982624b3898c8991cb80c0981b2aa71070e3537046c9a95950318a95afcc'
MEMBER = 'package/vendor/aarch64-apple-darwin/bin/codex'


def acquire(store: ManagedAuthStore) -> Path:
    if (platform.system(), platform.machine()) != ('Darwin','arm64'):
        raise RefreshUnavailable('managed_runtime_unverified')
    cached = store.root/'runtimes'/DIGEST/'codex'
    if cached.exists() or cached.is_symlink():
        _check_runtime(cached)
        return cached
    private_directory(store.root)
    with tempfile.TemporaryDirectory(prefix='runtime-',dir=store.root) as raw:
        directory=Path(raw).resolve()
        archive=directory/'package.tgz'
        digest=hashlib.sha512();size=0;deadline=time.monotonic()+120
        try:
            with urllib.request.urlopen(URL,timeout=30) as response, archive.open('xb') as target:
                archive.chmod(0o600)
                while chunk := response.read(1024*1024):
                    size+=len(chunk)
                    if size>160*1024*1024 or time.monotonic()>deadline:
                        raise RefreshUnavailable('managed_runtime_download_invalid')
                    digest.update(chunk);target.write(chunk)
            if digest.digest()!=base64.b64decode(INTEGRITY):
                raise RefreshUnavailable('managed_runtime_download_invalid')
            with tarfile.open(archive,'r:gz') as package:
                member=package.getmember(MEMBER)
                if not member.isfile() or member.size!=222655232:
                    raise RefreshUnavailable('managed_runtime_download_invalid')
                source=package.extractfile(member)
                if source is None:
                    raise RefreshUnavailable('managed_runtime_download_invalid')
                executable=directory/'codex'
                with source, executable.open('xb') as target:
                    executable.chmod(0o700)
                    while chunk:=source.read(1024*1024):target.write(chunk)
            _check_runtime(executable)
            return store._runtime(executable)
        except (OSError, ValueError, tarfile.TarError, KeyError):
            raise RefreshUnavailable('managed_runtime_download_failed') from None
