"""Offline Codex account/read fixtures for auth_mode-less GPT-6 credentials."""
import asyncio
import base64
import json
import sys
import time

import pytest

from dradar import pier_codex
from dradar.auth_codex_rpc import AccountRpcError


def _jwt(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "fixture." + body + ".signature"


def _auth(tmp_path, *, mode=None, expiry=None, account="account-a", api_key=None):
    path = tmp_path / "auth.json"
    value = {
        "tokens": {
            "account_id": account,
            "id_token": _jwt({"sub": "stable-user-a", "email": "user@example.invalid",
                              "https://api.openai.com/auth": {"chatgpt_account_id": "account-a"}}),
            "access_token": _jwt({"exp": expiry or int(time.time()) + 3600,
                                  "https://api.openai.com/auth": {"chatgpt_account_id": "account-a"}}),
            "refresh_token": "fixture-refresh",
        },
    }
    if mode != "missing":
        value["auth_mode"] = mode
    if api_key:
        value["OPENAI_API_KEY"] = api_key
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def _official_fixture(tmp_path, monkeypatch, account):
    executable = tmp_path / "fake-codex"
    marker = tmp_path / "rpc-called"
    response = {"id": 2, "result": {"account": account, "requiresOpenaiAuth": True}}
    executable.write_text(
        "#!" + sys.executable + "\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "assert sys.argv[1:] == ['app-server']\n"
        "for line in sys.stdin:\n"
        " request = json.loads(line)\n"
        " if request['method'] == 'initialize':\n"
        "  print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
        " if request['method'] == 'account/read':\n"
        f"  Path({str(marker)!r}).write_text('called')\n"
        f"  print(json.dumps({response!r}), flush=True)\n"
        "  break\n"
    )
    executable.chmod(0o700)
    monkeypatch.setattr(pier_codex.shutil, "which", lambda name: str(executable) if name == "codex" else None)
    return marker


@pytest.mark.parametrize("mode", [None, "missing"])
def test_official_pro_account_admits_same_snapshot_without_rewriting_source(tmp_path, monkeypatch, mode):
    path = _auth(tmp_path, mode=mode)
    original = path.read_bytes()
    marker = _official_fixture(tmp_path, monkeypatch,
                               {"type": "chatgpt", "email": "user@example.invalid", "planType": "pro"})
    agent = pier_codex.CodexRegistered(logs_dir=tmp_path, model_name="gpt-6-luna", version="0.155.1")
    assert agent.verify_gpt6_subscription_auth(path) == original
    assert marker.read_text() == "called"
    assert path.read_bytes() == original


@pytest.mark.parametrize("account", [
    None,
    {"type": "apiKey"},
    {"type": "chatgpt", "email": "other@example.invalid", "planType": "pro"},
    {"type": "chatgpt", "email": "user@example.invalid", "planType": "free"},
    {"type": "chatgpt", "email": "user@example.invalid", "planType": "unknown"},
])
def test_missing_login_api_key_mismatch_and_ineligible_plan_fail_closed(tmp_path, monkeypatch, account):
    path = _auth(tmp_path)
    _official_fixture(tmp_path, monkeypatch, account)
    agent = pier_codex.CodexRegistered(logs_dir=tmp_path, model_name="gpt-6-sol", version="0.155.1")
    with pytest.raises(RuntimeError, match="subscription authentication") as error:
        agent.verify_gpt6_subscription_auth(path)
    assert "user@example.invalid" not in str(error.value)


@pytest.mark.parametrize("mode,key,account", [
    ("apikey", None, "account-a"),
    ("api_key", None, "account-a"),
    (None, "fixture-api-key", "account-a"),
    (None, None, "account-b"),
])
def test_local_api_key_or_inconsistent_stable_account_never_calls_rpc(tmp_path, monkeypatch, mode, key, account):
    path = _auth(tmp_path, mode=mode, api_key=key, account=account)
    marker = _official_fixture(tmp_path, monkeypatch,
                               {"type": "chatgpt", "email": "user@example.invalid", "planType": "pro"})
    agent = pier_codex.CodexRegistered(logs_dir=tmp_path, model_name="gpt-6-sol", version="0.155.1")
    with pytest.raises(RuntimeError, match="subscription authentication"):
        agent.verify_gpt6_subscription_auth(path)
    assert not marker.exists()


def test_expiring_login_requires_native_renewal_without_disposable_refresh(tmp_path, monkeypatch):
    path = _auth(tmp_path, expiry=int(time.time()) + 60)
    marker = _official_fixture(tmp_path, monkeypatch,
                               {"type": "chatgpt", "email": "user@example.invalid", "planType": "pro"})
    agent = pier_codex.CodexRegistered(logs_dir=tmp_path, model_name="gpt-6-luna", version="0.155.1")
    with pytest.raises(RuntimeError, match="needs renewal with the official Codex CLI"):
        agent.verify_gpt6_subscription_auth(path)
    assert not marker.exists()


def test_rpc_failure_stops_before_any_worker_or_model_action(tmp_path, monkeypatch):
    path = _auth(tmp_path)
    agent = pier_codex.CodexRegistered(
        logs_dir=tmp_path, model_name="gpt-6-sol", version="0.155.1",
        extra_env={"CODEX_AUTH_JSON_PATH": str(path)},
    )
    def unavailable(*args, **kwargs):
        raise AccountRpcError("account_rpc_timeout")
    monkeypatch.setattr(agent, "_verify_native_gpt6_subscription", unavailable)
    called = []
    async def action(*args, **kwargs):
        called.append("started")
    monkeypatch.setattr(agent, "verify_gpt6_runtime", action)
    monkeypatch.setattr(pier_codex, "verify_task_baseline", action)
    monkeypatch.setattr(pier_codex, "register_worker", action)
    monkeypatch.setattr(pier_codex.Codex, "run", action)
    with pytest.raises(RuntimeError, match="check is unavailable"):
        asyncio.run(agent.run("fixture", object(), None))
    assert called == []


def test_legacy_model_does_not_invoke_gpt6_guard(tmp_path):
    agent = pier_codex.CodexRegistered(logs_dir=tmp_path, model_name="gpt-5.5", version="0.154.0")
    assert agent.verify_gpt6_subscription_auth(None) is None
