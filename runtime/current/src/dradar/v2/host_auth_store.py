"""Official host storage selection; never open or copy credential contents."""
from __future__ import annotations

import os
from pathlib import Path
import stat


def host_auth_store(home: Path, configuration: dict) -> str:
    """Respect native Codex modes and its file default, without keyring gating.

    The caller separately validates the canonical private host directory.
    account/read remains responsible for proving an authenticated identity.
    For file/auto, check metadata only; absence is handled by native login.
    """
    mode = configuration.get('cli_auth_credentials_store', 'file')
    if mode not in ('file', 'keyring', 'auto', 'ephemeral'):
        raise ValueError('unknown official credential store')
    if mode in ('file', 'auto'):
        try:
            info = (home / 'auth.json').lstat()
        except FileNotFoundError:
            return mode
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('host credential requires owned regular file with mode0600')
    return mode
