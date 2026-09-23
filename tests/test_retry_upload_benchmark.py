"""Cross-benchmark retry uses the saved scope without changing login state."""

from types import SimpleNamespace

import pytest

from dradar import cli, pending, runloop
from dradar.api_client import ApiClient


BATCH = "550e8400e29b41d4a716446655440000"
SERVER = "https://synthetic.invalid"


def _client(token="synthetic-token", benchmark="deep-swe", server=SERVER):
    return ApiClient(server, token, capabilities=(), benchmark_id=benchmark)


def _entry(client, *, batch_id=BATCH):
    return {
        "assignment_id": "synthetic-assignment",
        "batch_id": batch_id,
        "scope_fingerprint": runloop._pending_scope_fingerprint(
            client, batch_id=batch_id,
        ),
    }


def _setup(tmp_path, monkeypatch, *, config_benchmark="pompeii-adjacency"):
    from dradar import failure_reports

    monkeypatch.setattr(runloop, "HOME", tmp_path)
    cfg = {"server": SERVER, "token": "synthetic-token", "benchmark": config_benchmark}
    monkeypatch.setattr(runloop, "_load_config", lambda: cfg)
    monkeypatch.setattr(
        failure_reports, "flush_pending",
        lambda *_args: {"received": 0, "send_failed": 0},
    )
    return cfg


def test_retry_upload_cli_accepts_benchmark_without_changing_config(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_retry_upload", lambda args: seen.append(args.benchmark) or 0)
    assert cli.main(["retry-upload", "--benchmark", "deep-swe"]) == 0
    assert seen == ["deep-swe"]


def test_default_pompeii_keeps_deep_swe_and_explicit_selection_retries(
    tmp_path, monkeypatch, capsys,
):
    cfg = _setup(tmp_path, monkeypatch)
    entry = _entry(_client())
    pending.record(tmp_path, entry)
    touched = []

    def upload(_client_arg, saved):
        touched.append(saved["assignment_id"])
        pending.remove(
            tmp_path, saved["assignment_id"],
            scope_fingerprint=saved["scope_fingerprint"],
        )
        return "submitted"

    monkeypatch.setattr(runloop, "_upload_trial", upload)
    assert runloop.cmd_retry_upload(SimpleNamespace(benchmark=None)) == 1
    assert touched == []
    assert pending.load(tmp_path) == [entry]
    diagnostic = capsys.readouterr().out
    assert "selected benchmark 'pompeii-adjacency' differs" in diagnostic
    assert "saved upload benchmark 'deep-swe'" in diagnostic

    assert runloop.cmd_retry_upload(SimpleNamespace(benchmark="deep-swe")) == 0
    assert touched == ["synthetic-assignment"]
    assert pending.load(tmp_path) == []
    assert cfg["benchmark"] == "pompeii-adjacency"
    assert runloop.cmd_retry_upload(SimpleNamespace(benchmark="deep-swe")) == 0
    assert touched == ["synthetic-assignment"]  # idempotent local retry


@pytest.mark.parametrize("change", [
    "wrong_benchmark", "wrong_account", "wrong_server", "wrong_batch",
    "tampered_scope", "missing_scope", "invalid_batch",
])
def test_wrong_identity_or_tampered_row_stays_local(
    tmp_path, monkeypatch, change,
):
    _setup(tmp_path, monkeypatch)
    client = _client()
    entry = _entry(client)
    if change == "wrong_benchmark":
        selected = "pompeii-adjacency"
    else:
        selected = "deep-swe"
    if change == "wrong_account":
        entry = _entry(_client(token="different-token"))
    elif change == "wrong_server":
        entry = _entry(_client(server="https://elsewhere.invalid"))
    elif change == "wrong_batch":
        entry["batch_id"] = "6ba7b8109dad11d180b400c04fd430c8"
    elif change == "tampered_scope":
        entry["scope_fingerprint"] = "0" * 64
    elif change == "missing_scope":
        entry.pop("scope_fingerprint")
    elif change == "invalid_batch":
        entry["batch_id"] = "not-a-uuid"
    pending.record(tmp_path, entry)
    before = (tmp_path / "pending_uploads.json").read_bytes()
    monkeypatch.setattr(
        runloop, "_upload_trial",
        lambda *_args: pytest.fail("foreign or altered upload must stay local"),
    )
    assert runloop.cmd_retry_upload(SimpleNamespace(benchmark=selected)) == 1
    assert (tmp_path / "pending_uploads.json").read_bytes() == before


def test_network_failure_keeps_pending_bytes_for_same_benchmark(
    tmp_path, monkeypatch,
):
    _setup(tmp_path, monkeypatch)
    entry = _entry(_client())
    pending.record(tmp_path, entry)
    before = (tmp_path / "pending_uploads.json").read_bytes()
    calls = []
    monkeypatch.setattr(
        runloop, "_upload_trial",
        lambda _client_arg, saved: calls.append(saved) or "upload-failed",
    )
    assert runloop.cmd_retry_upload(SimpleNamespace(benchmark="deep-swe")) == 1
    assert len(calls) == 1
    assert (tmp_path / "pending_uploads.json").read_bytes() == before


def test_empty_benchmark_does_not_attempt_upload(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    pending.record(tmp_path, _entry(_client()))
    monkeypatch.setattr(
        runloop, "_upload_trial",
        lambda *_args: pytest.fail("empty benchmark must stay local"),
    )
    assert runloop.cmd_retry_upload(SimpleNamespace(benchmark="  ")) == 2
    assert len(pending.load(tmp_path)) == 1


def test_benchmark_hint_does_not_recommend_ordinary_retry_for_blocked_row(
    tmp_path, monkeypatch, capsys,
):
    _setup(tmp_path, monkeypatch)
    pending.record(tmp_path, {
        **_entry(_client()), "upload_blocked": "owner_superseded",
    })
    monkeypatch.setattr(
        runloop, "_upload_trial",
        lambda *_args: pytest.fail("wrong benchmark must stay local"),
    )
    assert runloop.cmd_retry_upload(SimpleNamespace(benchmark=None)) == 1
    output = capsys.readouterr().out
    assert "saved upload benchmark 'deep-swe'" in output
    assert "blocked and requires explicit review" in output
    assert "retryable rows need `dradar retry-upload --benchmark deep-swe`" not in output


def test_benchmark_hint_reports_retryable_and_blocked_rows_separately(
    tmp_path, monkeypatch, capsys,
):
    _setup(tmp_path, monkeypatch)
    first = _entry(_client())
    second = {**_entry(_client()), "assignment_id": "blocked-assignment",
              "upload_blocked": "owner_superseded"}
    pending.record(tmp_path, first)
    pending.record(tmp_path, second)
    monkeypatch.setattr(
        runloop, "_upload_trial",
        lambda *_args: pytest.fail("wrong benchmark must stay local"),
    )
    assert runloop.cmd_retry_upload(SimpleNamespace(benchmark=None)) == 1
    output = capsys.readouterr().out
    assert "retryable rows need `dradar retry-upload --benchmark deep-swe`" in output
    assert "blocked and requires explicit review" in output
    assert pending.load(tmp_path) == [first, second]
