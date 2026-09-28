"""A successful Kiro CLI exit cannot attest a requested effort by itself."""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


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
    stream.write_text(json.dumps({
        "type": "runFinished", "data": {"sessionId": sid, "status": "success"},
    }) + "\n")
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
