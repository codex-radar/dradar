"""A successful Kiro CLI exit cannot attest a requested effort by itself."""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


def _embedded_source(name: str) -> str:
    path = Path(__file__).parents[1] / "src/dradar/pier_kiro.py"
    module = ast.parse(path.read_text())
    assignment = next(
        node for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name
                for target in node.targets)
    )
    return ast.literal_eval(assignment.value)


def test_native_effort_must_match_requested_effort(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sid = "sess_123456"
    session = home / ".kiro/sessions/workspace" / sid / "session.json"
    session.parent.mkdir(parents=True)
    stream = tmp_path / "stream.jsonl"
    stream.write_text("\n".join(json.dumps(event) for event in (
        {"type": "configSelected", "data": {"sessionId": sid,
                                           "model": "claude-opus-5.5", "effort": "high"}},
        {"type": "runFinished", "data": {"sessionId": sid, "status": "success"}},
    )) + "\n")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    script = _embedded_source("_VERIFY").replace("/logs/agent", str(log_dir))

    def run(observed: str) -> subprocess.CompletedProcess[str]:
        session.write_text(json.dumps({
            "id": sid, "modelId": "claude-opus-5.5", "effortLevel": observed,
            "workspacePaths": ["/app"], "rootPaths": ["/app"],
        }))
        return subprocess.run(
            [sys.executable, "-c", script, str(stream), str(home),
             "claude-opus-5.5", "high"],
            text=True, capture_output=True, check=False,
        )

    mismatch = run("medium")
    assert mismatch.returncode != 0
    assert "DRADAR_KIRO_ATTESTATION=effort_mismatch" in mismatch.stderr
    assert not (log_dir / "kiro-attestation.json").exists()

    matched = run("high")
    assert matched.returncode == 0, matched.stderr
    attested = json.loads((log_dir / "kiro-attestation.json").read_text())
    assert attested["requested_effort"] == attested["observed_effort"] == "high"


def test_bootstrap_uses_assignment_effort_in_private_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = home / ".local/share/kiro-cli/data.sqlite3"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE migrations (version INTEGER)")
        conn.execute("CREATE TABLE state (key TEXT)")
    credential = tmp_path / "credential.json"
    credential.write_text(json.dumps({
        "access_token": "dummy", "refresh_token": "dummy", "expires_at": "2099-01-01",
        "provider": "google", "profile_arn": "arn:aws:codewhisperer:test",
    }))
    result = subprocess.run(
        [sys.executable, "-c", _embedded_source("_BOOTSTRAP"),
         str(credential), str(home), "claude-opus-5.5", "max"],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    settings = json.loads((home / ".kiro/settings/cli.json").read_text())
    assert settings["chat.modelDefaults"]["claude-opus-5.5"]["effort"] == "max"
    assert not credential.exists()


def test_acp_native_session_and_credit_meter_bind_to_exact_stream(tmp_path: Path) -> None:
    sid = "sess_acp_meter"
    home = tmp_path / "home"
    session = home / ".kiro/sessions/workspace" / sid / "session.json"
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({
        "id": sid, "modelId": "claude-opus-5.5", "effortLevel": "high",
        "workspacePaths": ["/app"], "rootPaths": ["/app"],
    }))
    session.with_name("messages.jsonl").write_text(json.dumps({
        "payload": {"type": "usage_summary", "promptTurnSummaries": [
            {"usage": 0.4340017094527363, "unit": "credit"}]},
    }) + "\n")
    stream = tmp_path / "stream.jsonl"
    stream.write_text("\n".join(json.dumps(event) for event in (
        {"type": "configSelected", "data": {"sessionId": sid,
                                           "model": "claude-opus-5.5", "effort": "high"}},
        {"type": "runFinished", "data": {"sessionId": sid, "status": "success"}},
    )) + "\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    script = _embedded_source("_VERIFY").replace("/logs/agent", str(logs))
    result = subprocess.run([sys.executable, "-c", script, str(stream), str(home),
                             "claude-opus-5.5", "high"], text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads((logs / "kiro-attestation.json").read_text())["session_id"] == sid
    meter = json.loads((logs / "kiro-metering.json").read_text())
    assert meter["session_id"] == sid
    assert meter["metering_usage"] == [{"value": 0.4340017094527363,
                                         "unit": "credit"}]


def test_acp_stream_tool_lifecycle_reaches_trajectory_and_credit_sidecar(tmp_path: Path) -> None:
    from dradar.pier_kiro import KiroOpus55

    sid = "sess_tool"
    events = [
        {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "tool_call",
            "toolCallId": "call_1", "kind": "execute", "status": "pending"}}},
        {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "tool_call_update",
            "toolCallId": "call_1", "status": "completed"}}},
        {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "OK"}}}},
        {"type": "runFinished", "data": {"sessionId": sid, "status": "success"}},
    ]
    (tmp_path / "kiro-stream.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n")
    (tmp_path / "kiro-attestation.json").write_text(json.dumps({
        "session_id": sid, "observed_model": "claude-opus-5.5",
        "requested_effort": "high", "observed_effort": "high"}))
    (tmp_path / "kiro-metering.json").write_text(json.dumps({
        "schema": "dradar-kiro-native-metering-v1", "session_id": sid,
        "metering_usage": [{"value": 0.4340017094527363, "unit": "credit"}]}))
    fake = SimpleNamespace(logs_dir=tmp_path, _STREAM="kiro-stream.jsonl",
                           _effort="high", name=lambda: "kiro")
    context = SimpleNamespace()
    KiroOpus55.populate_context_post_run.__wrapped__(fake, context)
    trajectory = json.loads((tmp_path / "trajectory.json").read_text())
    usage = json.loads((tmp_path / "provider-usage.json").read_text())
    assert [step["message"] for step in trajectory["steps"]] == [
        "Kiro ACP tool execute pending", "Kiro ACP tool execute completed", "OK"]
    assert usage["verified_thinking_effort"] == "high"
    assert usage["kiro_credits"] == 0.4340017094527363
    assert usage["kiro_estimated_usd"] == 0.017360068378109453
