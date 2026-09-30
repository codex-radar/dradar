"""Explicit reviewed recovery never turns a terminal block into auto-retry."""
import hashlib
import json
import sys
import urllib.parse
from pathlib import Path

import httpx
import pytest

from dradar import artifact_staging, pending, runloop
from dradar.api_client import ApiClient
from dradar.ota import recovery
from dradar.scrub import redact_patch_secrets, scan_secrets, scrub_json_bytes
from dradar.submission_intent import submission_payload_manifest, upload_intent_id
from test_pending_upload import _entry
from test_recover_upload import ASSIGNMENT, BATCH, _signed_package

PATCH = (b'diff --git a/auth.go b/auth.go\n--- a/auth.go\n+++ b/auth.go\n'
         b'@@ -1 +1,2 @@\n package fixture\n+Password: "synthetic-secret-value",\n')
SHA = hashlib.sha256(PATCH).hexdigest()


def saved(tmp_path, monkeypatch, patch=PATCH):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_load_config", lambda: {
        "server": "https://api.example.com", "token": "drt_test", "benchmark": "deep-swe",
    })
    trial = tmp_path / "trial"
    (trial / "artifacts").mkdir(parents=True)
    (trial / "artifacts/model.patch").write_bytes(patch)
    row = _entry(trial, assignment_id=ASSIGNMENT, batch_id=BATCH,
                 benchmark_id="deep-swe", runner_session_id="b" * 32,
                 owner_epoch=1, ledger_version=3, upload_blocked="server_secret_guard")
    row.update(artifact_staging.ensure_staged_patch(trial, row).ledger_fields)
    manifest = submission_payload_manifest(
        assignment_id=ASSIGNMENT, session_id=row["runner_session_id"], owner_epoch=1,
        outcome="completed", meta={}, patch=trial / "artifacts/model.patch",
        trajectory=None, result=None, trajectory_bundle=None,
    )
    row["upload_intent"] = {"id": upload_intent_id(manifest), "manifest": manifest}
    pending.record(tmp_path, row)
    return row


def recover(sha=SHA):
    return runloop.recover_one_pending_upload(
        assignment_id=ASSIGNMENT, benchmark="deep-swe", batch_id=BATCH,
        runner_session_id="b" * 32, reviewed_secret_guard_sha256=sha,
    )


@pytest.mark.parametrize("field", ["password", "passwd", "Password"])
def test_password_fields_are_scanned_scrubbed_and_structural_json_redacted(field):
    value = "synthetic-secret-value"
    assert "KEY-ASSIGN" in scan_secrets(f'{field}: "{value}"'.encode())
    assert json.loads(scrub_json_bytes(json.dumps({field: value}).encode()))[field] == "[REDACTED]"
    out, labels, unsafe = redact_patch_secrets(PATCH.replace(b"Password", field.encode()))
    assert labels == ["KEY-ASSIGN"] and unsafe == []
    assert scan_secrets(out) == [] and value.encode() not in out


def test_legal_code_and_bad_utf8_roundtrip():
    good = PATCH.replace(b'Password: "synthetic-secret-value"', b'Password: os.Getenv("PASSWORD")')
    assert scan_secrets(good) == []
    out, labels, unsafe = redact_patch_secrets(good + b"+// tail \x80\xff\n")
    assert out == good + b"+// tail \x80\xff\n" and labels == unsafe == []


@pytest.mark.parametrize("refusal", ["default", "digest", "unsafe_context", "missing_hit", "identity", "source_changed", "other_block", "cleanup"])
def test_review_refusals_preserve_ledger_and_do_not_upload(tmp_path, monkeypatch, refusal):
    patch = PATCH
    if refusal == "unsafe_context":
        patch = PATCH.replace(b'+Password:', b' Password:')
    elif refusal == "missing_hit":
        patch = PATCH.replace(b'synthetic-secret-value', b'short')
    row = saved(tmp_path, monkeypatch, patch)
    if refusal == "identity":
        row["upload_intent"]["id"] = "0" * 64
    elif refusal == "other_block":
        row["upload_blocked"] = "unsafe_artifact"
    elif refusal == "cleanup":
        row["record_kind"] = "cleanup_quarantine"
    elif refusal == "source_changed":
        Path(row["patch_source_path"]).write_bytes(PATCH + b"+changed\n")
    pending.record(tmp_path, row)
    before = (tmp_path / "pending_uploads.json").read_bytes()
    monkeypatch.setattr(runloop, "_upload_trial", lambda *a, **k: pytest.fail("upload was reached"))
    sha = None if refusal == "default" else "0" * 64 if refusal == "digest" else hashlib.sha256(patch).hexdigest()
    assert recover(sha) != 0
    assert (tmp_path / "pending_uploads.json").read_bytes() == before


@pytest.mark.parametrize("failure", [None, "intent_409", "submit_503", "submit_422"])
def test_review_replaces_original_content_intent_and_preserves_failure_block(tmp_path, monkeypatch, failure):
    row = saved(tmp_path, monkeypatch)
    original_intent = row["upload_intent"]
    raw_before = {k: Path(row[k]).read_bytes() for k in ("patch_source_path", "patch_staged_path")}
    calls = []
    sanitized, _, _ = redact_patch_secrets(PATCH)

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("submission-upload-intents"):
            fields = urllib.parse.parse_qs(request.content.decode())
            assert fields["session_id"] == ["b" * 32] and fields["owner_epoch"] == ["1"]
            new = pending.load(tmp_path)[0]
            assert fields["upload_intent_id"] == [new["upload_intent"]["id"]]
            assert new["upload_intent"] != original_intent
            assert new["secret_guard_review"]["original_upload_intent"] == original_intent
            assert new["upload_blocked"] == "server_secret_guard"
            assert new["upload_intent"]["manifest"]["components"]["model.patch"]["sha256"] == hashlib.sha256(sanitized).hexdigest()
            return httpx.Response(409 if failure == "intent_409" else 200, json={"detail": "owner superseded"} if failure == "intent_409" else {"ok": True})
        assert request.url.path.endswith("submissions")
        assert sanitized in request.content and b"synthetic-secret-value" not in request.content
        status = 503 if failure == "submit_503" else 422 if failure == "submit_422" else 200
        return httpx.Response(status, json={"detail": "patch appears to contain secrets (KEY-ASSIGN); not stored"} if failure == "submit_422"
                             else {"submission_id": "fixture-submission", "grade_status": "pending"})

    client = ApiClient("https://api.example.com", "drt_test", capabilities=(), benchmark_id="deep-swe", batch_id=BATCH,
                       transport=httpx.MockTransport(handler))
    monkeypatch.setattr(runloop, "_client", lambda _: client)
    monkeypatch.setattr(runloop, "_run_and_submit", lambda *a, **k: pytest.fail("model was started"))
    rc = recover()
    assert rc == (0 if failure is None else 1)
    assert calls == ["/api/v1/submission-upload-intents"] + ([] if failure == "intent_409" else ["/api/v1/submissions"] * (2 if failure == "submit_503" else 1))
    assert {k: Path(row[k]).read_bytes() for k in raw_before} == raw_before
    if failure is None:
        assert pending.load(tmp_path) == []
    else:
        remaining = pending.load(tmp_path)[0]
        assert remaining["upload_blocked"]
        before = (tmp_path / "pending_uploads.json").read_bytes()
        assert recover(None) == 1
        assert (tmp_path / "pending_uploads.json").read_bytes() == before
        if failure == "submit_503":
            assert recover() == 1


def test_signed_zipapp_review_entry_bypasses_activation_but_not_trust(tmp_path, monkeypatch):
    home, manifest, package, _ = _signed_package(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "HOME", home)
    monkeypatch.setattr(sys, "argv", [str(package), "recover-upload"])
    before = {p.name: p.read_bytes() for p in (home / "ota").glob("*.json")}
    calls = []
    monkeypatch.setattr(runloop, "recover_one_pending_upload", lambda **k: calls.append(k) or 0)
    args = ["--manifest", str(manifest), "--assignment-id", ASSIGNMENT,
            "--benchmark", "deep-swe", "--batch-id", BATCH,
            "--runner-session-id", "b" * 32, "--review-server-secret-guard", "--patch-sha256", SHA]
    assert recovery.main(args) == 0
    assert calls[0]["reviewed_secret_guard_sha256"] == SHA
    assert {p.name: p.read_bytes() for p in (home / "ota").glob("*.json")} == before
    document = json.loads(manifest.read_text()); document["version"] = "tampered"
    manifest.write_text(json.dumps(document))
    assert recovery.main(args) == 2 and len(calls) == 1


def test_review_flags_require_exact_scope_before_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "_verify_package", lambda *a: pytest.fail("verification reached"))
    with pytest.raises(SystemExit) as err:
        recovery.main(["--manifest", "none", "--assignment-id", ASSIGNMENT,
                       "--benchmark", "deep-swe", "--review-server-secret-guard", "--patch-sha256", SHA])
    assert err.value.code == 2
