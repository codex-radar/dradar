"""Kiro's private refresh snapshot never overwrites a newer host login."""

import json
import os
import sqlite3
import pytest
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


def test_concurrent_kiro_sessions_use_compare_and_swap(tmp_path, monkeypatch):
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
        try:
            with kiro_provider.kiro_subscription_session(tmp_path) as source:
                assert source.stat().st_mode & 0o077 == 0
                barrier.wait(timeout=3)
                refreshed = json.loads(source.read_text())
                refreshed["expires_at"] = (expires + timedelta(hours=index)).isoformat()
                refreshed["refresh_token"] = f"refreshed-{index}"
                source.write_text(json.dumps(refreshed))
            return "merged", source
        except kiro_provider.KiroCredentialMergeConflict as exc:
            return "conflict", exc.recovery_path

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, (1, 2)))
    assert sorted(status for status, _ in outcomes) == ["conflict", "merged"]
    for status, path in outcomes:
        assert path.exists() == (status == "conflict")
        if status == "conflict":
            assert path.stat().st_mode & 0o077 == 0
    with sqlite3.connect(db) as conn:
        latest = json.loads(conn.execute(
            "SELECT value FROM auth_kv WHERE key='kirocli:social:token'"
        ).fetchone()[0])
    assert latest["refresh_token"] in {"refreshed-1", "refreshed-2"}


def test_host_relogin_during_run_preserves_private_recovery_copy(tmp_path, monkeypatch):
    db = tmp_path / "data.sqlite3"
    expires = datetime.now(timezone.utc) + timedelta(minutes=20)
    original = {"access_token": "original-access", "refresh_token": "original-refresh",
                "expires_at": expires.isoformat(), "provider": "github",
                "profile_arn": "arn:aws:codewhisperer:us-east-1:123456789012:profile/test"}
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO auth_kv VALUES (?, ?)",
                     ("kirocli:social:token", json.dumps(original)))
    os.chmod(db, 0o600)
    monkeypatch.setattr(kiro_provider, "kiro_auth_db", lambda: db)
    monkeypatch.setattr(kiro_provider, "kiro_status", lambda: (True, "ready"))
    monkeypatch.setattr(kiro_provider, "kiro_cli_path", lambda: Path("/fake/kiro-cli"))
    monkeypatch.setattr(kiro_provider.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps({
                            "models": [{"model_id": "claude-opus-5.5"}]})))
    source = None
    try:
        with kiro_provider.kiro_subscription_session(tmp_path) as source:
            refreshed = {**original, "refresh_token": "task-refresh",
                         "expires_at": (expires + timedelta(hours=1)).isoformat()}
            source.write_text(json.dumps(refreshed))
            relogin = {**original, "refresh_token": "new-login",
                       "expires_at": (expires + timedelta(hours=2)).isoformat()}
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE auth_kv SET value=? WHERE key=?",
                             (json.dumps(relogin), "kirocli:social:token"))
    except kiro_provider.KiroCredentialMergeConflict as exc:
        assert exc.recovery_path == source
    else:
        raise AssertionError("concurrent host login must block refresh merge")
    assert source is not None and source.exists()
    assert source.stat().st_mode & 0o077 == 0
    with sqlite3.connect(db) as conn:
        current = json.loads(conn.execute(
            "SELECT value FROM auth_kv WHERE key='kirocli:social:token'"
        ).fetchone()[0])
    assert current["refresh_token"] == "new-login"


def test_refresh_copy_survives_sqlite_write_failure(tmp_path, monkeypatch):
    db = tmp_path / "data.sqlite3"
    expires = datetime.now(timezone.utc) + timedelta(minutes=20)
    original = {"access_token": "original-access", "refresh_token": "original-refresh",
                "expires_at": expires.isoformat(), "provider": "github",
                "profile_arn": "arn:aws:codewhisperer:us-east-1:123456789012:profile/test"}
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO auth_kv VALUES (?, ?)",
                     ("kirocli:social:token", json.dumps(original)))
    os.chmod(db, 0o600)
    monkeypatch.setattr(kiro_provider, "kiro_auth_db", lambda: db)
    monkeypatch.setattr(kiro_provider, "kiro_status", lambda: (True, "ready"))
    monkeypatch.setattr(kiro_provider, "kiro_cli_path", lambda: Path("/fake/kiro-cli"))
    monkeypatch.setattr(kiro_provider.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps({
                            "models": [{"model_id": "claude-opus-5.5"}]})))
    real_connect = sqlite3.connect

    def fail_write(database, *args, **kwargs):
        if str(database) == str(db):
            raise sqlite3.OperationalError("synthetic write failure")
        return real_connect(database, *args, **kwargs)

    source = None
    with pytest.raises(kiro_provider.KiroCredentialMergeConflict) as error:
        with kiro_provider.kiro_subscription_session(tmp_path) as source:
            refreshed = {**original, "refresh_token": "new-refresh",
                         "expires_at": (expires + timedelta(hours=1)).isoformat()}
            source.write_text(json.dumps(refreshed))
            monkeypatch.setattr(kiro_provider.sqlite3, "connect", fail_write)
    assert error.value.recovery_path == source
    assert source is not None and source.exists()
    assert source.stat().st_mode & 0o077 == 0
    assert json.loads(source.read_text())["refresh_token"] == "new-refresh"
    assert json.loads(real_connect(db).execute(
        "SELECT value FROM auth_kv WHERE key='kirocli:social:token'").fetchone()[0]
    )["refresh_token"] == "original-refresh"


def test_pending_native_return_preserves_private_snapshot(tmp_path, monkeypatch):
    db = tmp_path / "data.sqlite3"
    original = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
                "expires_at": "2099-01-01T00:00:00Z", "provider": "github",
                "profile_arn": "arn:aws:codewhisperer:us-east-1:123456789012:profile/test"}
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO auth_kv VALUES (?, ?)",
                     ("kirocli:social:token", json.dumps(original)))
    os.chmod(db, 0o600)
    monkeypatch.setattr(kiro_provider, "kiro_auth_db", lambda: db)
    monkeypatch.setattr(kiro_provider, "kiro_status", lambda: (True, "ready"))
    monkeypatch.setattr(kiro_provider, "kiro_cli_path", lambda: Path("/fake/kiro-cli"))
    monkeypatch.setattr(kiro_provider.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps({
                            "models": [{"model_id": "claude-opus-5.5"}]})))
    source = None
    with pytest.raises(kiro_provider.KiroCredentialReturnFailure) as error:
        with kiro_provider.kiro_subscription_session(tmp_path) as source:
            marker = source.with_name(source.name + ".return-pending")
            marker.write_text("synthetic pending return")
            os.chmod(marker, 0o600)
    assert error.value.recovery_path == source
    assert source is not None and source.exists()
    assert source.stat().st_mode & 0o077 == 0
    assert json.loads(source.read_text())["refresh_token"] == "synthetic-refresh"
    with sqlite3.connect(db) as conn:
        current = json.loads(conn.execute(
            "SELECT value FROM auth_kv WHERE key='kirocli:social:token'").fetchone()[0])
    assert current == original


@pytest.mark.parametrize("version, ready", [
    ("2.26.0", True), ("2.24.1", False), ("2.26.1", False),
    ("2.26.0-preview", False), ("2.24.1 2.26.0", False),
    ("12.26.0", False),
])
def test_kiro_host_version_is_exact_before_auth(monkeypatch, version, ready):
    monkeypatch.setattr(kiro_provider, "kiro_cli_path", lambda: Path("/fake/kiro-cli"))
    monkeypatch.setattr(kiro_provider.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(stdout=f"kiro-cli {version}\n"))
    auth_reads = []
    monkeypatch.setattr(kiro_provider, "social_token", lambda: auth_reads.append(True))
    actual, _ = kiro_provider.kiro_status()
    assert actual is ready
    assert bool(auth_reads) is ready
