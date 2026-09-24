import json
from types import SimpleNamespace

import pytest

from dradar import assignment_boundary, boundary_recovery, cli, runloop

A = "a" * 32
B = "b" * 32
C = "c" * 32


def assignment(aid):
    return {"assignment_id": aid, "task_id": f"task-{aid[0]}",
            "model": "grok-4.6", "effort": "low"}


def fixture(tmp_path, monkeypatch, *, server_ids=(A, B),
            server_status="expired", has_submission=False, wrong_benchmark=False):
    monkeypatch.setattr(boundary_recovery, "HOME", tmp_path)
    monkeypatch.setattr(boundary_recovery, "_load_config", lambda: {
        "server": "https://example.test", "token": "private", "benchmark": "deep-swe",
    })
    monkeypatch.setattr(boundary_recovery, "acquire_run_lock", lambda _home: None)
    monkeypatch.setattr(boundary_recovery, "_check_processes", lambda _home: None)

    class Client:
        benchmark_id = None

        def whoami(self):
            return {"nickname": "fixture-account"}

        def assignment_recovery_status(self, aid):
            if aid not in server_ids:
                raise boundary_recovery.ApiError("not found", status_code=404)
            return {**assignment(aid), "status": server_status,
                    "benchmark_id": ("other" if wrong_benchmark else self.benchmark_id),
                    "has_submission": has_submission}

    monkeypatch.setattr(boundary_recovery, "_client", lambda _cfg: Client())
    old = [assignment(A), assignment(B)]
    path = assignment_boundary.prepare(tmp_path, "deep-swe", old)
    for item in old:
        assignment_boundary.record_outcome(path, item, "failed")
        trial = tmp_path / "work" / "jobs" / f"a{item['assignment_id']}-fixture" / "trial"
        trial.mkdir(parents=True)
        (trial / "result.json").write_text(json.dumps({"finished_at": None, "n_completed": 0}))
        (trial / "logs.txt").write_text("local diagnostic")
        (trial / "artifacts").mkdir()
    args = SimpleNamespace(benchmark=None, accept_expired_assignment=[A, B])
    return path, args


def test_expired_failed_recovery_archives_boundary_and_keeps_jobs(tmp_path, monkeypatch):
    path, args = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: f"ACCEPT {A},{B}")

    assert boundary_recovery.cmd_boundary_recover(args) == 0
    assert not path.exists()
    archived = list(path.parent.glob(f"{path.stem}.recovered-*.json"))
    assert len(archived) == 1
    assert set(json.loads(archived[0].read_text())["expected"]) == {A, B}
    assert len(list((tmp_path / "work" / "jobs").rglob("result.json"))) == 2
    fresh = assignment_boundary.prepare(tmp_path, "deep-swe", [assignment(C)])
    assert fresh == path


def test_task_repository_files_are_not_mistaken_for_pier_artifacts(tmp_path, monkeypatch):
    path, args = fixture(tmp_path, monkeypatch)
    trial = next((tmp_path / "work" / "jobs").rglob("trial"))
    source = trial / "repo" / "artifacts"
    source.mkdir(parents=True)
    (source / "model.patch").write_text("task fixture, not Pier output")
    (source.parent / "state.json").write_text(json.dumps({"complete": True}))
    monkeypatch.setattr("builtins.input", lambda _prompt: f"ACCEPT {A},{B}")

    assert boundary_recovery.cmd_boundary_recover(args) == 0
    assert not path.exists()
    assert (source / "model.patch").is_file()


def test_cli_boundary_recover_requires_exact_ids(monkeypatch):
    captured = []
    monkeypatch.setattr(cli, "cmd_boundary_recover", lambda args: captured.append(args) or 0)
    assert cli.main(["boundary", "recover", "--accept-expired-assignment", A]) == 0
    assert captured[0].accept_expired_assignment == [A]


def test_legacy_forget_option_is_rejected_before_any_run():
    with pytest.raises(SystemExit, match="no longer an unchecked recovery shortcut"):
        runloop.cmd_go(SimpleNamespace(forget_assignment_boundary=True))


@pytest.mark.parametrize("problem", ["wrong-id", "wrong-account", "submitted", "released", "wrong-benchmark", "pending", "patch", "finished", "nested-finished", "complete-state", "process", "decline"])
def test_recovery_blocks_without_changing_ledger_or_jobs(tmp_path, monkeypatch, problem):
    server_ids = (A,) if problem == "wrong-account" else (A, B)
    path, args = fixture(
        tmp_path, monkeypatch, server_ids=server_ids,
        server_status="released" if problem == "released" else "expired",
        has_submission=problem == "submitted",
        wrong_benchmark=problem == "wrong-benchmark",
    )
    before = path.read_bytes()
    if problem == "wrong-id":
        args.accept_expired_assignment = [A, C]
    if problem == "pending":
        (tmp_path / "pending_uploads.json").write_text(json.dumps([{"assignment_id": A}]))
    if problem == "patch":
        next((tmp_path / "work" / "jobs").rglob("trial")).joinpath(
            "artifacts", "model.patch").write_text("diff")
    if problem == "finished":
        next((tmp_path / "work" / "jobs").rglob("result.json")).write_text(
            json.dumps({"finished_at": "2026-09-03T10:07:00Z"}))
    if problem == "nested-finished":
        next((tmp_path / "work" / "jobs").rglob("result.json")).write_text(
            json.dumps({"finished_at": None, "agent_execution": {
                "finished_at": "2026-09-03T10:07:00Z"}}))
    if problem == "complete-state":
        trial = next((tmp_path / "work" / "jobs").rglob("trial"))
        output = trial / ".dradar" / "host-output"
        output.mkdir(parents=True)
        (output / "state.json").write_text(json.dumps({"complete": True}))
        (output / "trajectory.json").write_text("{}")
    if problem == "process":
        monkeypatch.setattr(
            boundary_recovery, "_check_processes",
            lambda _home: (_ for _ in ()).throw(boundary_recovery.RecoveryBlocked("running")),
        )
    monkeypatch.setattr("builtins.input", lambda _prompt: (
        "no" if problem == "decline" else f"ACCEPT {A},{B}"))

    assert boundary_recovery.cmd_boundary_recover(args) == 1
    assert path.read_bytes() == before
    assert len(list((tmp_path / "work" / "jobs").rglob("result.json"))) == 2
    assert not list(path.parent.glob(f"{path.stem}.recovered-*.json"))


def test_recovery_rechecks_ledger_after_confirmation(tmp_path, monkeypatch):
    path, args = fixture(tmp_path, monkeypatch)

    def changed(_prompt):
        assignment_boundary.record_outcome(path, assignment(A), "interrupted")
        return f"ACCEPT {A},{B}"

    monkeypatch.setattr("builtins.input", changed)
    assert boundary_recovery.cmd_boundary_recover(args) == 1
    assert path.exists()
    assert not list(path.parent.glob(f"{path.stem}.recovered-*.json"))


@pytest.mark.parametrize("command", [
    "dradar go --pick task:model:low",
    "/opt/cli/bin/dradar resume --worker-child",
    "python -m dradar.cli resume --worker-child",
    "/usr/bin/python3 /dev/fd/9 resume --batch-id batch",
])
def test_process_inspection_detects_cli_and_ota_runners(tmp_path, monkeypatch, command):
    monkeypatch.setattr(boundary_recovery.fleet, "controller_is_active", lambda _home: False)

    def fake_run(args, **_kw):
        assert args[0] == "ps"
        return SimpleNamespace(stdout=f"999999 {command}\n")

    monkeypatch.setattr(boundary_recovery.subprocess, "run", fake_run)
    with pytest.raises(boundary_recovery.RecoveryBlocked, match="runner process"):
        boundary_recovery._check_processes(tmp_path)
