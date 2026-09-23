"""Signed, exact-row upload recovery keeps OTA and ownership fences intact."""

import hashlib
import io
import json
import sys
import zipfile
import urllib.parse

import httpx
import pytest

from dradar import pending, runloop
from dradar.api_client import ApiClient
from dradar.ota import recovery
from dradar.ota.activity import register_invocation
from dradar.ota.manifest import ManifestError, PlatformTarget
from dradar.ota.state import UpdateLock, _atomic_json
from test_ota_runtime import TRUSTED_KEYS, seed_lkg, sign_document, signed_release
from test_pending_upload import _entry, _make_trial_dir


ASSIGNMENT = "a" * 32
BATCH = "550e8400e29b41d4a716446655440000"
SERVER = "https://synthetic.invalid"


def _saved_row(token="synthetic-token", benchmark="deep-swe", batch=BATCH):
    client = ApiClient(SERVER, token, capabilities=(), benchmark_id=benchmark)
    return {
        "assignment_id": ASSIGNMENT,
        "batch_id": batch,
        "runner_session_id": "original-session",
        "owner_epoch": 1,
        "scope_fingerprint": runloop._pending_scope_fingerprint(client, batch_id=batch),
    }


def test_exact_row_recovery_and_no_global_benchmark_change(tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    cfg = {"server": SERVER, "token": "synthetic-token", "benchmark": "pompeii-adjacency"}
    monkeypatch.setattr(runloop, "_load_config", lambda: cfg)
    own = _saved_row()
    other = {**_saved_row(), "assignment_id": "b" * 32}
    pending.record(tmp_path, own)
    pending.record(tmp_path, other)
    calls = []

    def upload(_client, row, **kwargs):
        assert kwargs == {"upload_only_recovery": True}
        calls.append(row["assignment_id"])
        pending.remove(tmp_path, row["assignment_id"], scope_fingerprint=row["scope_fingerprint"])
        return "submitted"

    monkeypatch.setattr(runloop, "_upload_trial", upload)
    assert runloop.recover_one_pending_upload(
        assignment_id=ASSIGNMENT, benchmark="deep-swe", batch_id=BATCH,
        runner_session_id="original-session",
    ) == 0
    assert calls == [ASSIGNMENT]
    assert pending.load(tmp_path) == [other]
    assert cfg["benchmark"] == "pompeii-adjacency"
    assert runloop.recover_one_pending_upload(
        assignment_id=ASSIGNMENT, benchmark="deep-swe", batch_id=BATCH,
    ) == 2
    assert calls == [ASSIGNMENT]


@pytest.mark.parametrize("change", [
    "benchmark", "account", "server", "batch", "session", "owner_block", "duplicate",
])
def test_recovery_rejects_wrong_scope_before_upload(tmp_path, monkeypatch, change):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    cfg = {"server": SERVER, "token": "synthetic-token", "benchmark": "pompeii-adjacency"}
    if change == "account":
        cfg["token"] = "wrong-token"
    if change == "server":
        cfg["server"] = "https://other.invalid"
    monkeypatch.setattr(runloop, "_load_config", lambda: cfg)
    row = _saved_row()
    if change == "owner_block":
        row["upload_blocked"] = "owner_superseded"
    pending.record(tmp_path, row)
    if change == "duplicate":
        pending.record(tmp_path, {**row, "scope_fingerprint": "0" * 64})
    before = (tmp_path / "pending_uploads.json").read_bytes()
    monkeypatch.setattr(runloop, "_upload_trial", lambda *_: pytest.fail("upload was reached"))
    assert runloop.recover_one_pending_upload(
        assignment_id=ASSIGNMENT,
        benchmark="pompeii-adjacency" if change == "benchmark" else "deep-swe",
        batch_id="6ba7b8109dad11d180b400c04fd430c8" if change == "batch" else BATCH,
        runner_session_id="wrong-session" if change == "session" else "original-session",
    ) != 0
    assert (tmp_path / "pending_uploads.json").read_bytes() == before


def test_network_failure_retains_pending_row(tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_load_config", lambda: {
        "server": SERVER, "token": "synthetic-token", "benchmark": "pompeii-adjacency",
    })
    pending.record(tmp_path, _saved_row())
    before = (tmp_path / "pending_uploads.json").read_bytes()
    monkeypatch.setattr(runloop, "_upload_trial", lambda *_, **__: "upload-failed")
    assert runloop.recover_one_pending_upload(
        assignment_id=ASSIGNMENT, benchmark="deep-swe", batch_id=BATCH,
    ) == 1
    assert (tmp_path / "pending_uploads.json").read_bytes() == before


def test_legacy_row_without_owner_or_session_keeps_server_fence(tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_load_config", lambda: {
        "server": SERVER, "token": "synthetic-token", "benchmark": "pompeii-adjacency",
    })
    row = _saved_row()
    row.pop("owner_epoch")
    row.pop("runner_session_id")
    pending.record(tmp_path, row)
    seen = []
    monkeypatch.setattr(runloop, "_upload_trial", lambda client, entry, **_: seen.append(entry) or "upload-failed")
    assert runloop.recover_one_pending_upload(
        assignment_id=ASSIGNMENT, benchmark="deep-swe", batch_id=BATCH,
    ) == 1
    assert seen == [row]
    assert pending.load(tmp_path) == [row]


@pytest.mark.parametrize("owner_rejected", [False, True])
def test_recovery_real_upload_intent_contract(tmp_path, monkeypatch, owner_rejected):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    job_dir = tmp_path / "work" / "jobs" / f"a{ASSIGNMENT}"
    trial_dir = _make_trial_dir(job_dir, "task")
    (trial_dir / "artifacts" / "result.json").write_text('{"synthetic":true}')
    source_before = {
        str(path.relative_to(job_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in trial_dir.rglob("*") if path.is_file()
    }
    row = _entry(
        trial_dir, assignment_id=ASSIGNMENT, batch_id=BATCH,
        benchmark_id="deep-swe", runner_session_id="original-session",
        owner_epoch=1, ledger_version=3, job_dir=str(job_dir), keep=False,
    )
    pending.record(tmp_path, row)
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("submission-upload-intents"):
            form = urllib.parse.parse_qs(request.read().decode())
            assert form["session_id"] == ["original-session"]
            assert form["owner_epoch"] == ["1"]
            return (
                httpx.Response(409, json={"detail": "owner epoch superseded"})
                if owner_rejected else httpx.Response(200, json={"ok": True})
            )
        assert request.url.path.endswith("submissions")
        return httpx.Response(200, json={"submission_id": "synthetic-s1", "grade_status": "pending"})

    client = ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(handler), capabilities=(),
        benchmark_id="deep-swe", batch_id=BATCH,
    )
    monkeypatch.setattr(runloop, "_load_config", lambda: {
        "server": "https://api.example.com", "token": "drt_test",
        "benchmark": "pompeii-adjacency",
    })
    monkeypatch.setattr(runloop, "_client", lambda _cfg: client)
    monkeypatch.setattr(
        runloop, "_run_and_submit",
        lambda *_args, **_kwargs: pytest.fail("recovery must not run a model"),
    )
    result = runloop.recover_one_pending_upload(
        assignment_id=ASSIGNMENT, benchmark="deep-swe", batch_id=BATCH,
        runner_session_id="original-session",
    )
    if owner_rejected:
        assert result == 1
        assert paths == ["/api/v1/submission-upload-intents"]
        assert pending.load(tmp_path)[0]["assignment_id"] == ASSIGNMENT
    else:
        assert result == 0
        assert paths == ["/api/v1/submission-upload-intents", "/api/v1/submissions"]
        assert pending.load(tmp_path) == []
    assert job_dir.is_dir()
    assert {
        name: hashlib.sha256((job_dir / name).read_bytes()).hexdigest()
        for name in source_before
    } == source_before


@pytest.mark.parametrize("failure_stage", ["intent_410", "submit_413"])
def test_recovery_terminal_server_failure_keeps_ledger_and_artifacts(
    tmp_path, monkeypatch, failure_stage,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    row = _entry(
        trial_dir, assignment_id=ASSIGNMENT, batch_id=BATCH,
        benchmark_id="deep-swe", runner_session_id="original-session",
        owner_epoch=1, ledger_version=3,
    )
    pending.record(tmp_path, row)
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("submission-upload-intents"):
            return (httpx.Response(410, json={"detail": "expired"})
                    if failure_stage == "intent_410"
                    else httpx.Response(200, json={"ok": True}))
        return httpx.Response(413, json={"detail": "too large"})

    client = ApiClient(
        "https://api.example.com", "drt_test",
        transport=httpx.MockTransport(handler), capabilities=(),
        benchmark_id="deep-swe", batch_id=BATCH,
    )
    monkeypatch.setattr(runloop, "_load_config", lambda: {
        "server": "https://api.example.com", "token": "drt_test",
        "benchmark": "pompeii-adjacency",
    })
    monkeypatch.setattr(runloop, "_client", lambda _cfg: client)
    monkeypatch.setattr(client, "mark_stopped", lambda *_a, **_k: pytest.fail("no stop in upload-only recovery"))
    assert runloop.recover_one_pending_upload(
        assignment_id=ASSIGNMENT, benchmark="deep-swe", batch_id=BATCH,
        runner_session_id="original-session",
    ) == 1
    assert pending.load(tmp_path)[0]["assignment_id"] == ASSIGNMENT
    assert trial_dir.is_dir()
    assert paths == (["/api/v1/submission-upload-intents"] if failure_stage == "intent_410"
                     else ["/api/v1/submission-upload-intents", "/api/v1/submissions"])


def _signed_package(tmp_path, monkeypatch):
    root = tmp_path / "home"
    seed_lkg(root / "ota")
    identity = root / "flight-recorder" / "client_id"
    identity.parent.mkdir(parents=True)
    identity.write_text("c" * 32)
    target = PlatformTarget.current()
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr("dradar/_ota_build.json", json.dumps({
            "schema_version": 1, "version": "0.6.0", "sequence": 600,
            "target": {"os": target.os, "arch": target.arch},
        }))
    package = tmp_path / "candidate.pyz"
    package.write_bytes(payload.getvalue())
    document, _ = signed_release()
    document.pop("signature")
    for item in document["artifacts"]:
        if item["os"] == target.os and item["arch"] == target.arch:
            item["filename"] = package.name
            item["size"] = package.stat().st_size
            item["sha256"] = hashlib.sha256(package.read_bytes()).hexdigest()
    sign_document(document)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(document))
    monkeypatch.setattr(recovery, "TRUSTED_KEYS", TRUSTED_KEYS)
    monkeypatch.setattr(recovery, "__version__", "0.6.0")
    return root, manifest, package, document


def test_signed_package_and_tamper_negative_controls(tmp_path, monkeypatch):
    home, manifest, package, document = _signed_package(tmp_path, monkeypatch)
    _atomic_json(home / "ota" / "update-state.json", {
        "schema_version": 1,
        "state": "waiting_safe_point",
        "release": {
            "release_id": "older-staged", "version": "0.5.230", "sequence": 599,
            "artifact": "releases/older-staged/older.pyz",
        },
    })
    recovery._verify_package(manifest, package, home)
    before = (home / "ota" / "current.json").read_bytes()
    package.write_bytes(package.read_bytes() + b"tampered")
    with pytest.raises(ManifestError):
        recovery._verify_package(manifest, package, home)
    assert (home / "ota" / "current.json").read_bytes() == before
    package.write_bytes(package.read_bytes()[:-8])
    document["sequence"] = 601  # stale signature
    manifest.write_text(json.dumps(document))
    with pytest.raises(ManifestError):
        recovery._verify_package(manifest, package, home)


def test_signed_package_rejects_rollback_sequence(tmp_path, monkeypatch):
    home, manifest, package, document = _signed_package(tmp_path, monkeypatch)
    document.pop("signature")
    document["sequence"] = 599
    sign_document(document)
    manifest.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="anti_rollback_sequence"):
        recovery._verify_package(manifest, package, home)


def test_recovery_excludes_active_runner_before_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "HOME", tmp_path)
    root = tmp_path / "ota"
    monkeypatch.setattr(recovery, "_verify_package", lambda *_: pytest.fail("verification should not start"))
    monkeypatch.setattr(sys, "argv", ["candidate.pyz", "recover-upload"])
    with UpdateLock(root / "launch.lock"):
        active = register_invocation(root)
        active.__enter__()
    try:
        assert recovery.main([
            "--manifest", "manifest.json", "--assignment-id", ASSIGNMENT,
            "--benchmark", "deep-swe", "--batch-id", BATCH,
        ]) == 2
    finally:
        active.__exit__(None, None, None)
