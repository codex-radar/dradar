"""One execution attempt exposes facts for the caller's recovery decision."""
import json
import zipfile

import pytest

from dradar import runloop
from dradar.telemetry import RunnerTelemetry
from test_go_menu import ASSIGNMENT, SubmitClient, _args


@pytest.mark.parametrize("failure", ["build", "transport"])
def test_failure_diagnostics_bind_the_single_attempt_without_raw_output(
    tmp_path, monkeypatch, failure,
):
    home = tmp_path / "home"
    monkeypatch.setattr(runloop, "HOME", home)
    monkeypatch.setattr(runloop.image_cache, "remove_trial_builder", lambda *a, **k: (True, None))
    client = SubmitClient({})
    client.mark_stopped = lambda *a, **k: {"ok": True}
    recorder = RunnerTelemetry(client, home=home)
    batch = "b" * 32
    recorder.bind_batch(batch)
    assignment = {**ASSIGNMENT, "assignment_id": "a" * 32,
                  "batch_id": batch, "agent": "zcode"}
    calls = []
    secret = "Bearer " + "s" * 48

    def fail(*args, **kwargs):
        calls.append(assignment["assignment_id"])
        if failure == "build":
            raise runloop.BuildFlakeError(secret)
        raise runloop.RunnerError(secret, failure_diagnostic={
            "schema": "dradar-runner-failure-v1",
            "failure_code": "agent_no_artifact",
            "zcode_provider_failure_reason": "network_error",
        })

    monkeypatch.setattr(runloop, "run_trial", fail)
    outcome = runloop._run_and_submit(client, assignment, tmp_path, _args(), None,
                                     telemetry=recorder)
    assert outcome == ("environment-build-failed" if failure == "build" else "failed")
    assert calls == [assignment["assignment_id"]]
    assert client.submissions == []
    bundle = recorder.flight_recorder.export_diagnostic_bundle(tmp_path / "diagnostic.zip")
    with zipfile.ZipFile(bundle) as archive:
        raw = archive.read("events.jsonl").decode()
        events = [json.loads(line) for line in raw.splitlines()]
    assert secret not in raw
    event = next(e for e in events if e["event_type"] == (
        "build_failed" if failure == "build" else "provider_failed"))
    assert event["assignment_id"] == assignment["assignment_id"]
    assert event["batch_id"] == batch
    assert event["session_id"] == recorder.session_id
    assert event["occurred_at"]
    assert event["reason_code"] == ("build_flake" if failure == "build" else "transport_error")
    assert event["attributes"] == ({"attempt": 1, "phase": "building"}
                                   if failure == "build" else {"attempt": 1})


def test_build_stop_without_ack_retains_unknown_instead_of_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop.image_cache, "remove_trial_builder", lambda *a, **k: (True, None))
    monkeypatch.setattr(runloop, "_mark_stopped_quietly", lambda *a, **k: False)
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise runloop.BuildFlakeError("controlled build failure")
    monkeypatch.setattr(runloop, "run_trial", fail)
    assert runloop._run_and_submit(SubmitClient({}), dict(ASSIGNMENT), tmp_path,
                                  _args(), None) == "cleanup-unconfirmed"
    assert calls == [True]
