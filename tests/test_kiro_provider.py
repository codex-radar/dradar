"""Kiro's private refresh snapshot must permit concurrent task runs."""

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

from dradar import kiro_provider


def test_kiro_access_preflight_fails_closed_without_browser(monkeypatch):
    monkeypatch.setattr(kiro_provider, "kiro_status", lambda: (True, "ready"))
    monkeypatch.setattr(kiro_provider, "kiro_cli_path", lambda: Path("/fake/kiro-cli"))
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"models": [
            {"model_id": "auto"}]}))

    monkeypatch.setattr(kiro_provider.subprocess, "run", fake_run)
    ready, issue = kiro_provider.kiro_access_status()
    assert not ready and "Opus 5.5" in issue
    assert calls[0][1]["env"]["BROWSER"] == "/usr/bin/false"
    monkeypatch.setattr(kiro_provider.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout='{"models":null}'))
    assert kiro_provider.kiro_access_status()[0] is False


def test_concurrent_kiro_sessions_merge_only_newest_same_profile(tmp_path, monkeypatch):
    db = tmp_path / "data.sqlite3"
    expires = datetime.now(timezone.utc) + timedelta(minutes=20)
    token = {
        "access_token": "test-access",
        "refresh_token": "test-refresh",
        "expires_at": expires.isoformat(),
        "provider": "github",
        "profile_arn": "arn:aws:codewhisperer:us-east-1:123456789012:profile/test",
    }
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO auth_kv VALUES (?, ?)",
                     ("kirocli:social:token", json.dumps(token)))
    os.chmod(db, 0o600)
    monkeypatch.setattr(kiro_provider, "kiro_auth_db", lambda: db)
    monkeypatch.setattr(kiro_provider, "kiro_status", lambda: (True, "ready"))
    monkeypatch.setattr(kiro_provider, "kiro_cli_path", lambda: Path("/fake/kiro-cli"))
    monkeypatch.setattr(kiro_provider.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps({
                            "models": [{"model_id": "claude-opus-5.5"}]})))
    barrier = Barrier(2)

    def run(index):
        with kiro_provider.kiro_subscription_session(tmp_path) as source:
            assert source.stat().st_mode & 0o077 == 0
            barrier.wait(timeout=3)
            refreshed = json.loads(source.read_text())
            refreshed["expires_at"] = (expires + timedelta(hours=index)).isoformat()
            refreshed["refresh_token"] = f"refreshed-{index}"
            source.write_text(json.dumps(refreshed))
            return source

    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(run, (1, 2)))
    assert all(not path.exists() for path in paths)
    with sqlite3.connect(db) as conn:
        latest = json.loads(conn.execute(
            "SELECT value FROM auth_kv WHERE key='kirocli:social:token'"
        ).fetchone()[0])
    assert latest["expires_at"] == (expires + timedelta(hours=2)).isoformat()
    assert latest["refresh_token"] == "refreshed-2"
