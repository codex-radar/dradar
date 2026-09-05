"""Private local startup receipt shared with the dependency-free bootstrap.

This is readiness data, not a command channel. It is written after provenance
and imports succeed, before any plan operation. No capability is written.
"""

import json
import os
from pathlib import Path
import re
import stat

PATH_ENV = "DRADAR_BOOTSTRAP_RECEIPT"
NONCE_ENV = "DRADAR_BOOTSTRAP_NONCE"


def managed() -> bool:
    return PATH_ENV in os.environ or NONCE_ENV in os.environ


def _parent(path: Path, nonce: str) -> None:
    if (not re.fullmatch(r"[0-9a-f]{32}", nonce) or not path.is_absolute() or
            path.name != f"ready-{nonce}.json" or not path.parent.name.startswith("dradar-start-") or
            path.parent.is_symlink() or not path.parent.is_dir()):
        raise ValueError("invalid bootstrap receipt location")
    info = path.parent.stat()
    if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("bootstrap receipt directory is not private")


def signal_ready(revision: str) -> bool:
    if not managed():
        return False
    path = Path(os.environ.get(PATH_ENV, ""))
    nonce = os.environ.get(NONCE_ENV, "")
    _parent(path, nonce)
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("invalid bootstrap revision")
    body = json.dumps({"version": 1, "nonce": nonce, "revision": revision}, separators=(",", ":")).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
    return True


def ready(path: Path, nonce: str, revision: str) -> bool:
    try:
        _parent(path, nonce)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 512:
            return False
        if os.name != "nt" and (path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077):
            return False
        value = json.loads(path.read_bytes())
        return (isinstance(value, dict) and type(value.get("version")) is int and
                value == {"version": 1, "nonce": nonce, "revision": revision})
    except (OSError, ValueError, TypeError):
        return False
