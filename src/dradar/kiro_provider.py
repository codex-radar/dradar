"""Local, subscription-only Kiro CLI credential and model contract.

Kiro's macOS IDE cache is not a CLI login.  The official CLI owns the
credential in its SQLite auth store; this module only reads that store for a
single Pier run.  It never starts an OAuth browser flow or accepts an API key.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

KIRO_AGENT = "kiro"
KIRO_PROVIDER = "kiro-subscription"
KIRO_MODEL = "kiro-claude-opus-5.5"
KIRO_REQUEST_MODEL = "claude-opus-5.5"
KIRO_CLI_VERSION = "2.26.0"
KIRO_CAPABILITY = "kiro-claude-opus-5-5-v1"
KIRO_SUPPORTED_EFFORTS = frozenset({"high"})
_SOCIAL_TOKEN_KEY = "kirocli:social:token"


class KiroCredentialMergeConflict(RuntimeError):
    """A refreshed private copy could not safely replace the host session."""

    def __init__(self, recovery_path: Path):
        self.recovery_path = recovery_path
        super().__init__("Kiro host credential changed or refresh was invalid; "
                         "owner-only recovery copy retained")


class KiroCredentialReturnFailure(RuntimeError):
    """Pier could not prove that the private container credential returned."""

    def __init__(self, recovery_path: Path):
        self.recovery_path = recovery_path
        super().__init__("Kiro private credential return was not confirmed; "
                         "owner-only host snapshot retained for manual recovery")


def kiro_cli_path() -> Path | None:
    found = shutil.which("kiro-cli")
    return Path(found).resolve() if found else None


def kiro_auth_db() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home()))
        return root / "kiro-cli" / "data.sqlite3"
    if os.uname().sysname == "Darwin":
        return Path.home() / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3"
    return Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3"


def social_token() -> dict:
    """Read an owner-private native Kiro social session for official refresh."""
    path = kiro_auth_db()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("Kiro auth store must be a regular file")
    if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("Kiro auth store must be owner-only")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM auth_kv WHERE key=?", (_SOCIAL_TOKEN_KEY,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError("Kiro CLI social session is missing")
    value = json.loads(row[0])
    if not isinstance(value, dict):
        raise ValueError("Kiro CLI social session is malformed")
    required = ("access_token", "refresh_token", "expires_at", "provider", "profile_arn")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise ValueError("Kiro CLI social session is incomplete")
    if value["provider"] not in {"google", "github"}:
        raise ValueError("unsupported Kiro social provider")
    if not value["profile_arn"].startswith("arn:aws:codewhisperer:"):
        raise ValueError("Kiro profile identity is invalid")
    expires = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
    if expires.tzinfo is None:
        raise ValueError("Kiro access token expiry is invalid")
    return {key: value[key] for key in required}


def kiro_status() -> tuple[bool, str]:
    cli = kiro_cli_path()
    if cli is None:
        return False, "official Kiro CLI is not installed"
    try:
        version = subprocess.run([str(cli), "--version"], capture_output=True,
                                 text=True, timeout=8, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return False, "Kiro CLI version is unavailable"
    if version.strip() != f"kiro-cli {KIRO_CLI_VERSION}":
        return False, f"Kiro CLI {KIRO_CLI_VERSION} is required"
    try:
        social_token()
    except (OSError, sqlite3.Error, json.JSONDecodeError, ValueError) as exc:
        return False, str(exc)
    return True, "Kiro CLI social session is locally present; model access is checked before each run"


def kiro_access_status() -> tuple[bool, str]:
    """Check Opus 5.5 entitlement without starting an OAuth browser."""
    ready, issue = kiro_status()
    if not ready:
        return False, issue
    try:
        result = subprocess.run(
            [str(kiro_cli_path()), "chat", "--list-models", "--format", "json"],
            capture_output=True, text=True, timeout=25,
            env={**os.environ, "BROWSER": "/usr/bin/false"},
        )
        catalog = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False, "Kiro authenticated model catalog is unavailable"
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if (result.returncode != 0 or not isinstance(models, list)
            or not any(isinstance(item, dict)
                       and item.get("model_id") == KIRO_REQUEST_MODEL
                       for item in models)):
        return False, "Kiro account cannot access Claude Opus 5.5"
    return True, "official Kiro account can access Claude Opus 5.5"


@contextmanager
def kiro_subscription_session(work_dir: Path):
    """Stage one owner-only run copy, merging a native refresh on return.

    The official CLI refreshes the host session before the snapshot.  Model
    access is checked with its authenticated catalog.  A container may then
    refresh independently; its newer token is accepted only for the same
    profile/provider and never copied into task artifacts.
    """
    import fcntl

    ready, issue = kiro_status()
    if not ready:
        raise ValueError(issue)
    db = kiro_auth_db()
    lock = db.with_name("dradar-kiro-session.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    source: Path | None = None
    with os.fdopen(fd, "rb+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            env = {**os.environ, "BROWSER": "/usr/bin/false"}
            try:
                result = subprocess.run(
                    [str(kiro_cli_path()), "chat", "--list-models", "--format", "json"],
                    capture_output=True, text=True, timeout=25, env=env,
                )
                catalog = json.loads(result.stdout)
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
                raise ValueError("Kiro authenticated model catalog is unavailable") from exc
            models = catalog.get("models") if isinstance(catalog, dict) else None
            if result.returncode != 0 or not isinstance(models, list) or not any(
                isinstance(item, dict) and item.get("model_id") == KIRO_REQUEST_MODEL
                for item in models
            ):
                raise ValueError("Kiro account cannot access Claude Opus 5.5")
            original = social_token()
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT value FROM auth_kv WHERE key=?",
                                   (_SOCIAL_TOKEN_KEY,)).fetchone()
            finally:
                conn.close()
            original_raw = row[0] if row else None
            observed = json.loads(original_raw) if original_raw else None
            if (not isinstance(observed, dict)
                    or any(observed.get(key) != value
                           for key, value in original.items())):
                raise ValueError("Kiro credential changed during snapshot")
            work_dir.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=".kiro-session-", suffix=".json", dir=work_dir)
            source = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(original, stream, separators=(",", ":"))
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        try:
            yield source
        finally:
            # Assume the private copy may contain the only refreshed token.
            # Release it only after proving unchanged content or a host commit.
            retain_recovery = True
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    if source.with_name(source.name + ".return-pending").exists():
                        raise KiroCredentialReturnFailure(source)
                    refreshed = json.loads(source.read_text(encoding="utf-8"))
                    changed = refreshed != original
                    valid = (isinstance(refreshed, dict)
                             and refreshed.get("provider") == original["provider"]
                             and refreshed.get("profile_arn") == original["profile_arn"]
                             and all(isinstance(refreshed.get(key), str) and refreshed[key]
                                     for key in original))
                    if changed and valid:
                        valid = (datetime.fromisoformat(
                            refreshed["expires_at"].replace("Z", "+00:00"))
                            > datetime.fromisoformat(
                                original["expires_at"].replace("Z", "+00:00")))
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                    changed = True
                    valid = False
                if changed:
                    # A refreshed token may have invalidated the snapshot. Keep
                    # its owner-only recovery copy until the host commit succeeds,
                    # including SQLite open/write/commit failures.
                    retain_recovery = True
                    if not valid:
                        raise KiroCredentialMergeConflict(source)
                    try:
                        conn = sqlite3.connect(db)
                        try:
                            updated = conn.execute(
                                "UPDATE auth_kv SET value=? WHERE key=? AND value=?",
                                (json.dumps(refreshed, separators=(",", ":")),
                                 _SOCIAL_TOKEN_KEY, original_raw),
                            )
                            if updated.rowcount != 1:
                                raise KiroCredentialMergeConflict(source)
                            conn.commit()
                            retain_recovery = False
                        finally:
                            conn.close()
                    except sqlite3.Error as exc:
                        raise KiroCredentialMergeConflict(source) from exc
                else:
                    retain_recovery = False
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                if retain_recovery:
                    if source.exists():
                        os.chmod(source, 0o600)
                else:
                    source.unlink(missing_ok=True)
