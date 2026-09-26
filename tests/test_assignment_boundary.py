import json
from types import SimpleNamespace

import pytest

from dradar import assignment_boundary
from dradar import runloop


def _assignment(assignment_id: str, task_id: str | None = None) -> dict:
    return {
        "assignment_id": assignment_id,
        "task_id": task_id or f"task-{assignment_id}",
        "model": "glm-5.3-flash",
        "effort": "low",
    }


def test_boundary_detects_unresolved_assignment_disappearing(tmp_path):
    active = [_assignment("a1"), _assignment("a2")]
    path = assignment_boundary.prepare(tmp_path, "bench", active)

    with pytest.raises(
        assignment_boundary.BoundaryError,
        match="disappeared.*a2",
    ):
        assignment_boundary.prepare(tmp_path, "bench", [active[0]])

    assert path is not None and path.is_file()


def test_old_terminal_without_local_outcomes_keeps_guard_and_explains_proof(
        tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    old = [
        {**_assignment("old-claude-1"), "batch_id": "old-batch"},
        {**_assignment("old-claude-2"), "batch_id": "old-batch"},
    ]
    path = assignment_boundary.prepare(tmp_path, "deep-swe", old)
    before = path.read_bytes()
    args = SimpleNamespace(
        refill=False, fleet_pool=False, assignment=None,
        expect_assignment=None, forget_assignment_boundary=False,
    )
    client = SimpleNamespace(batch_id=None)

    with pytest.raises(SystemExit, match="dradar boundary recover") as stopped:
        runloop._prepare_assignment_boundary(
            args, client, "deep-swe", [_assignment("new-gemini")],
        )

    assert "No model was started" in str(stopped.value)
    assert "expired without submission" in str(stopped.value)
    assert path.read_bytes() == before


def test_exact_batch_resume_ignores_unrelated_released_legacy_boundary(
        tmp_path, monkeypatch):
    """A retired batch must not prevent resuming a different held batch."""
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    old = {**_assignment("old-released"), "batch_id": "old-batch",
           "benchmark_id": "deep-swe"}
    held = {**_assignment("held-waiting"), "batch_id": "target-batch",
            "benchmark_id": "deep-swe"}
    legacy = assignment_boundary.prepare(tmp_path, "deep-swe", [old])
    target = assignment_boundary.prepare(
        tmp_path, "deep-swe", [held], batch_id="target-batch",
        expected_ids=[held["assignment_id"]],
    )
    assignment_boundary.record_outcome(target, held, "failed")
    old_bytes, target_bytes = legacy.read_bytes(), target.read_bytes()
    args = SimpleNamespace(
        resume=True, batch_id="target-batch", refill=False, fleet_pool=False,
        assignment=None, expect_assignment=[held["assignment_id"]],
        forget_assignment_boundary=False,
    )
    client = SimpleNamespace(batch_id="target-batch")

    path = runloop._prepare_assignment_boundary(
        args, client, "deep-swe", [held],
    )

    assert path == target
    assert legacy.read_bytes() == old_bytes
    assert target.read_bytes() == target_bytes


def _exact_resume_args(batch_id: str, assignment_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        resume=True, batch_id=batch_id, refill=False, fleet_pool=False,
        assignment=None, expect_assignment=[assignment_id],
        forget_assignment_boundary=False,
    )


def test_exact_batch_resume_keeps_same_batch_legacy_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    old = {**_assignment("missing"), "benchmark_id": "deep-swe",
           "batch_id": "target-batch"}
    held = {**_assignment("held"), "benchmark_id": "deep-swe",
            "batch_id": "target-batch"}
    legacy = assignment_boundary.prepare(tmp_path, "deep-swe", [old])
    before = legacy.read_bytes()

    with pytest.raises(SystemExit, match="assignment boundary check failed"):
        runloop._prepare_assignment_boundary(
            _exact_resume_args("target-batch", "held"),
            SimpleNamespace(batch_id="target-batch"), "deep-swe", [held],
        )

    assert legacy.read_bytes() == before
    assert not assignment_boundary.state_path(
        tmp_path, "deep-swe", "target-batch",
    ).exists()


def test_exact_batch_resume_blocks_unknown_legacy_attribution(tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    old = {**_assignment("old"), "benchmark_id": "deep-swe"}
    held = {**_assignment("held"), "benchmark_id": "deep-swe",
            "batch_id": "target-batch"}
    legacy = assignment_boundary.prepare(tmp_path, "deep-swe", [old])
    before = legacy.read_bytes()

    with pytest.raises(SystemExit, match="unknown batch attribution"):
        runloop._prepare_assignment_boundary(
            _exact_resume_args("target-batch", "held"),
            SimpleNamespace(batch_id="target-batch"), "deep-swe", [held],
        )
    assert legacy.read_bytes() == before


def test_exact_batch_resume_rejects_changed_saved_task_identity(
        tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    held = {**_assignment("held", "original-task"),
            "benchmark_id": "deep-swe", "batch_id": "target-batch"}
    target = assignment_boundary.prepare(
        tmp_path, "deep-swe", [held], batch_id="target-batch",
    )
    before = target.read_bytes()

    with pytest.raises(SystemExit, match="saved assignment identity differs"):
        runloop._prepare_assignment_boundary(
            _exact_resume_args("target-batch", "held"),
            SimpleNamespace(batch_id="target-batch"), "deep-swe",
            [{**held, "task_id": "different-task"}],
        )
    assert target.read_bytes() == before


def test_exact_batch_resume_rejects_external_inherited_boundary(
        tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    old = {**_assignment("old"), "batch_id": "old-batch"}
    held = {**_assignment("held"), "batch_id": "target-batch",
            "benchmark_id": "deep-swe"}
    legacy = assignment_boundary.prepare(tmp_path, "deep-swe", [old])
    original = legacy.read_bytes()
    monkeypatch.setenv(runloop._ASSIGNMENT_BOUNDARY_ENV, str(legacy))

    with pytest.raises(SystemExit, match="cannot inherit an external boundary"):
        runloop._prepare_assignment_boundary(
            _exact_resume_args("target-batch", "held"),
            SimpleNamespace(batch_id="target-batch"), "deep-swe", [held],
        )
    assert legacy.read_bytes() == original
    assert not assignment_boundary.state_path(
        tmp_path, "deep-swe", "target-batch",
    ).exists()


def test_exact_batch_worker_inherits_only_admitted_boundary(
        tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    held = {**_assignment("held"), "batch_id": "target-batch",
            "benchmark_id": "deep-swe"}
    target = assignment_boundary.prepare(
        tmp_path, "deep-swe", [held], batch_id="target-batch",
    )
    monkeypatch.setenv(runloop._ASSIGNMENT_BOUNDARY_ENV, str(target))
    args = _exact_resume_args("target-batch", "held")
    args.worker_child = True
    assert runloop._prepare_assignment_boundary(
        args, SimpleNamespace(batch_id="target-batch"), "deep-swe", [held],
    ) == target

    args._assignment_boundary_path = None
    other = assignment_boundary.prepare(
        tmp_path, "deep-swe", [{**held, "batch_id": "other-batch"}],
        batch_id="other-batch",
    )
    monkeypatch.setenv(runloop._ASSIGNMENT_BOUNDARY_ENV, str(other))
    with pytest.raises(SystemExit, match="does not admit the requested batch"):
        runloop._prepare_assignment_boundary(
            args, SimpleNamespace(batch_id="target-batch"), "deep-swe", [held],
        )


def test_exact_batch_checkout_does_not_replace_saved_task_identity(tmp_path):
    held = {**_assignment("held", "original-task"),
            "benchmark_id": "deep-swe", "batch_id": "target-batch"}
    path = assignment_boundary.prepare(
        tmp_path, "deep-swe", [held], batch_id="target-batch",
        expected_ids=["held"],
    )
    before = path.read_bytes()

    with pytest.raises(assignment_boundary.BoundaryError,
                       match="saved assignment identity differs"):
        assignment_boundary.add_expected(
            path, [{**held, "task_id": "swapped-task", "owner_epoch": 2}],
            require_matching_metadata=True,
        )

    assert path.read_bytes() == before
    assignment_boundary.add_expected(
        path, [{**held, "owner_epoch": 2}],
        require_matching_metadata=True,
    )


def test_submitted_assignment_may_leave_active_leases(tmp_path):
    active = [_assignment("a1"), _assignment("a2")]
    path = assignment_boundary.prepare(tmp_path, "bench", active)
    assignment_boundary.record_outcome(path, active[0], "submitted")

    report = assignment_boundary.reconcile(path, [active[1]])

    assert report is not None
    assert report.missing_ids == frozenset()
    assert report.settled_ids == frozenset({"a1"})
    assert not report.complete


def test_complete_boundary_is_removed(tmp_path):
    active = [_assignment("a1"), _assignment("a2")]
    path = assignment_boundary.prepare(tmp_path, "bench", active)
    assignment_boundary.record_outcome(path, active[0], "submitted")
    assignment_boundary.record_outcome(path, active[1], "interrupted")
    report = assignment_boundary.reconcile(path, [])

    assert report is not None and report.complete
    assignment_boundary.finish_if_complete(path, report)
    assert path is not None and not path.exists()


def test_explicit_boundary_requires_every_expected_id(tmp_path):
    with pytest.raises(
        assignment_boundary.BoundaryError,
        match="not active.*a2",
    ):
        assignment_boundary.prepare(
            tmp_path,
            "bench",
            [_assignment("a1")],
            expected_ids=["a1", "a2"],
        )


def test_explicit_boundary_rejects_unlisted_active_assignment(tmp_path):
    with pytest.raises(
        assignment_boundary.BoundaryError,
        match="outside the explicit boundary.*a2",
    ):
        assignment_boundary.prepare(
            tmp_path,
            "bench",
            [_assignment("a1"), _assignment("a2")],
            expected_ids=["a1"],
        )


def test_strict_boundary_cannot_be_extended_after_creation(tmp_path):
    path = assignment_boundary.prepare(
        tmp_path,
        "bench",
        [_assignment("a1")],
        expected_ids=["a1"],
    )

    with pytest.raises(
        assignment_boundary.BoundaryError,
        match="outside the explicit boundary.*a2",
    ):
        assignment_boundary.add_expected(path, [_assignment("a2")])


def test_forget_replaces_only_the_named_benchmark_boundary(tmp_path):
    old = [_assignment("old")]
    path = assignment_boundary.prepare(tmp_path, "bench", old)
    replacement = [_assignment("new")]

    new_path = assignment_boundary.prepare(
        tmp_path,
        "bench",
        replacement,
        expected_ids=["new"],
        forget_existing=True,
    )

    assert new_path == path
    payload = json.loads(path.read_text())
    assert set(payload["expected"]) == {"new"}
    assert "nonce" not in path.read_text()


def test_same_benchmark_uses_independent_boundaries_per_exact_batch(tmp_path):
    first = assignment_boundary.prepare(
        tmp_path, "bench", [_assignment("a1")], batch_id="batch-a",
    )
    second = assignment_boundary.prepare(
        tmp_path, "bench", [_assignment("b1")], batch_id="batch-b",
    )

    assert first is not None and second is not None and first != second
    assert json.loads(first.read_text())["batch_id"] == "batch-a"
    assert json.loads(second.read_text())["batch_id"] == "batch-b"

    assignment_boundary.record_outcome(first, _assignment("a1"), "submitted")
    assert assignment_boundary.reconcile(first, []).complete
    assert assignment_boundary.reconcile(second, [_assignment("b1")]).complete is False


def test_corrupt_boundary_fails_closed_without_explicit_forget(tmp_path):
    path = assignment_boundary.state_path(tmp_path, "bench")
    path.parent.mkdir(parents=True)
    path.write_text("not-json")

    with pytest.raises(assignment_boundary.BoundaryError, match="unreadable"):
        assignment_boundary.prepare(tmp_path, "bench", [_assignment("a1")])


def test_runloop_reconciliation_reports_a_lost_assignment(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    active = [_assignment("a1"), _assignment("a2")]
    args = SimpleNamespace(
        refill=False,
        expect_assignment=None,
        forget_assignment_boundary=False,
    )

    class Client:
        def get_assignment(self):
            return {"active": [active[0]]}

    path = runloop._prepare_assignment_boundary(
        args, Client(), "bench", active,
    )

    assert runloop._finish_assignment_boundary(Client(), path) is False
    assert "a2" in capsys.readouterr().out
    assert path is not None and path.is_file()


@pytest.mark.parametrize("code", ("claim_batch_not_found", None))
def test_finished_exact_batch_404_completes_boundary(tmp_path, code):
    assignment = _assignment("a1")
    path = assignment_boundary.prepare(tmp_path, "bench", [assignment])
    assignment_boundary.record_outcome(path, assignment, "submitted")

    class Client:
        batch_id = "550e8400e29b41d4a716446655440000"

        def get_assignment(self):
            raise runloop.ApiError(
                "server returned 404: active claim batch not found",
                status_code=404, code=code,
            )

    assert runloop._finish_assignment_boundary(Client(), path) is True
    assert path is not None and not path.exists()


def test_unrelated_batch_404_keeps_boundary(tmp_path, capsys):
    assignment = _assignment("a1")
    path = assignment_boundary.prepare(tmp_path, "bench", [assignment])

    class Client:
        batch_id = "550e8400e29b41d4a716446655440000"

        def get_assignment(self):
            raise runloop.ApiError(
                "server returned 404: unrelated route missing",
                status_code=404,
            )

    assert runloop._finish_assignment_boundary(Client(), path) is False
    assert path is not None and path.is_file()
    assert "boundary was kept" in capsys.readouterr().out


def test_worker_child_does_not_reconcile_parent_owned_boundary(
        tmp_path, monkeypatch):
    path = tmp_path / "shared-boundary.json"
    monkeypatch.setenv(runloop._ASSIGNMENT_BOUNDARY_ENV, str(path))
    reconciled = []
    monkeypatch.setattr(
        runloop, "_finish_assignment_boundary",
        lambda _client, _path: reconciled.append(_path) or False,
    )
    args = SimpleNamespace(worker_child=True)

    assert runloop._finish_invocation_assignment_boundary(
        args, object(), path,
    ) is True
    assert reconciled == []


def test_standalone_invocation_still_reconciles_its_boundary(
        tmp_path, monkeypatch):
    path = tmp_path / "owned-boundary.json"
    reconciled = []
    monkeypatch.delenv(runloop._ASSIGNMENT_BOUNDARY_ENV, raising=False)
    monkeypatch.setattr(
        runloop, "_finish_assignment_boundary",
        lambda _client, _path: reconciled.append(_path) or True,
    )
    args = SimpleNamespace(worker_child=True)

    assert runloop._finish_invocation_assignment_boundary(
        args, object(), path,
    ) is True
    assert reconciled == [path]
