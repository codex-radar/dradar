"""Defensive contracts using isolated fictitious files; no runner or network."""
import os
from pathlib import Path

import pytest

from dradar import artifact_boundary as boundary


@pytest.fixture
def trial(tmp_path):
    root = tmp_path / 'trial'
    (root / 'agent' / 'sessions').mkdir(parents=True)
    (root / 'agent' / 'sessions' / 'normal.jsonl').write_text('{"type":"fixture"}\n')
    return root


def _make_dsh_home(trial, *, fifo=False, junk=0):
    home = trial / 'agent' / 'dsh-home'
    (home / '.dsh' / 'state').mkdir(parents=True, exist_ok=True)
    (home / 'dsh-usage.json').write_text('{"schema":"dsh-provider-usage-v2"}')
    (home / 'dsh-outcome.json').write_text('{"schema":"dradar-dsh-outcome-v1"}')
    # The agent's own third-party runtime state: never consumed, never uploaded.
    (home / '.credentials.yaml').write_text('k: v')
    if fifo:
        os.mkfifo(home / '.dsh' / 'daemon.sock')
    for index in range(junk):
        (home / '.dsh' / 'state' / f'f{index}.json').write_text('{}')
    return home


def test_dsh_home_runtime_state_never_blocks_the_upload_snapshot(trial):
    """Regress 0.5.196+: DSH mounts its Node home at agent/dsh-home, and the
    strict whole-tree walk failed every DSH upload once that home contained a
    socket/fifo or unbounded cache entries (all completed solves lost). The
    walk now prunes that subtree and re-adds only the two DRadar artifacts."""
    _make_dsh_home(trial, fifo=True, junk=5000)
    with boundary.snapshot_agent(trial) as snapshot:
        collected = sorted(
            str(path.relative_to(snapshot))
            for path in snapshot.rglob('*') if path.is_file()
        )
    assert collected == [
        'agent/dsh-home/dsh-outcome.json',
        'agent/dsh-home/dsh-usage.json',
        'agent/sessions/normal.jsonl',
    ]


def test_snapshot_still_fail_closed_outside_dsh_home(trial):
    _make_dsh_home(trial)
    os.mkfifo(trial / 'agent' / 'rogue.sock')
    with pytest.raises(boundary.UnsafeArtifact, match='unsafe_file_type'):
        boundary.snapshot_agent(trial).__enter__()


def test_dsh_home_artifacts_are_byte_verified(trial):
    _make_dsh_home(trial, fifo=True)
    with boundary.snapshot_agent(trial) as snapshot:
        assert (snapshot / 'agent/dsh-home/dsh-usage.json').read_bytes() == (
            b'{"schema":"dsh-provider-usage-v2"}'
        )
        assert not (snapshot / 'agent/dsh-home/.credentials.yaml').exists()


def test_private_snapshot_preserves_bytes_and_is_outside_trial(trial):
    with boundary.snapshot_agent(trial) as snapshot:
        assert not snapshot.is_relative_to(trial)
        assert snapshot.stat().st_mode & 0o077 == 0
        assert (snapshot / 'agent/sessions/normal.jsonl').read_bytes() == b'{"type":"fixture"}\n'
    assert not snapshot.exists()


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'hardlink', 'fifo'])
def test_reject_nonordinary_input(trial, kind):
    path = trial / 'agent' / 'input'
    if kind == 'symlink':
        path.symlink_to('sessions/normal.jsonl')
    elif kind == 'directory':
        path.mkdir()
    elif kind == 'hardlink':
        os.link(trial / 'agent/sessions/normal.jsonl', path)
    else:
        os.mkfifo(path)
    with pytest.raises(boundary.UnsafeArtifact):
        boundary.read_trial_file(trial, 'agent/input')


def test_parent_links_and_relative_escape_rejected(trial):
    (trial / 'agent' / 'alias').symlink_to('sessions', target_is_directory=True)
    for relative in ('agent/alias/normal.jsonl', '../file', '/file'):
        with pytest.raises(boundary.UnsafeArtifact):
            boundary.read_trial_file(trial, relative)


def test_size_and_tree_limits(trial, monkeypatch):
    with pytest.raises(boundary.UnsafeArtifact, match='file_limit'):
        boundary.read_trial_file(trial, 'agent/sessions/normal.jsonl', max_bytes=1)
    monkeypatch.setattr(boundary, 'MAX_TREE_BYTES', 1)
    with pytest.raises(boundary.UnsafeArtifact, match='tree_limit'):
        with boundary.snapshot_agent(trial):
            pytest.fail('must not yield unverified bytes')


def test_entry_limit(trial, monkeypatch):
    monkeypatch.setattr(boundary, 'MAX_ENTRIES', 1)
    with pytest.raises(boundary.UnsafeArtifact, match='entry_limit'):
        with boundary.snapshot_agent(trial):
            pass


def test_changed_file_rejected(trial, monkeypatch):
    original = boundary._fingerprint
    calls = 0
    def changing(value):
        nonlocal calls
        calls += 1
        return original(value) + (calls,)
    monkeypatch.setattr(boundary, '_fingerprint', changing)
    with pytest.raises(boundary.UnsafeArtifact, match='file_changed'):
        boundary.read_trial_file(trial, 'agent/sessions/normal.jsonl')


def test_host_output_isolated(trial):
    with boundary.TrialFiles(trial) as files:
        files.write_host('.dradar/host-output/trajectory.json', b'{}')
        with pytest.raises(boundary.UnsafeArtifact, match='output_not_host_owned'):
            files.write_host('agent/trajectory.json', b'{}')
    assert boundary.read_trial_file(trial, '.dradar/host-output/trajectory.json') == b'{}'
    assert not (trial / 'agent/trajectory.json').exists()


def test_unsupported_platform_fails_closed(trial, monkeypatch):
    monkeypatch.setattr(boundary.os, 'supports_dir_fd', set())
    with pytest.raises(boundary.UnsafeArtifact, match='platform_boundary_unavailable'):
        boundary.read_trial_file(trial, 'agent/sessions/normal.jsonl')


def test_changes_after_read_invalidate_snapshot(trial):
    with boundary.TrialFiles(trial) as files:
        files.read('agent/sessions/normal.jsonl')
        (trial / 'agent/sessions/normal.jsonl').write_text('{"type":"updated"}\n')
        with pytest.raises(boundary.UnsafeArtifact, match='snapshot_changed'):
            files.verify()


def test_new_entries_invalidate_enumeration(trial):
    with boundary.TrialFiles(trial) as files:
        files.files('agent')
        (trial / 'agent/added.json').write_text('{}')
        with pytest.raises(boundary.UnsafeArtifact, match='snapshot_changed'):
            files.verify()


def test_upload_boundary_refusal_never_calls_network(trial, monkeypatch):
    from dradar import runloop, pending
    (trial / 'artifacts').mkdir()
    (trial / 'artifacts/model.patch').write_bytes(b'diff --git a/a b/a\n-old\n+new\n')
    (trial / 'agent/bad-log').symlink_to('sessions/normal.jsonl')
    monkeypatch.setattr(runloop, 'HOME', trial.parent)
    class NoNetwork:
        def __getattr__(self, name):
            raise AssertionError('network must not be used')
    entry = {'assignment_id': 'fixture', 'nonce': 'fixture', 'trial_dir': str(trial),
             'task_id': 'fixture', 'keep': True}
    assert runloop._upload_trial(NoNetwork(), entry) == 'upload-blocked'
    assert pending.load(trial.parent)[0]['upload_blocked'] == 'unsafe_artifact'


def test_bundle_boundary_refusal_is_not_incomplete(trial):
    from dradar.runner import build_codex_trajectory_bundle
    (trial / 'agent/bad-log').symlink_to('sessions/normal.jsonl')
    with pytest.raises(boundary.UnsafeArtifact):
        build_codex_trajectory_bundle(trial)


def test_host_output_rejects_uncontrolled_parent(trial):
    (trial / '.dradar').mkdir(mode=0o777)
    (trial / '.dradar').chmod(0o777)
    with boundary.TrialFiles(trial) as files:
        with pytest.raises(boundary.UnsafeArtifact, match='output_not_host_private'):
            files.write_host('.dradar/host-output/trajectory.json', b'{}')


def test_codex_postrun_uses_private_input_and_output(trial, monkeypatch):
    import importlib.util
    import sys
    import types
    import dradar
    from types import SimpleNamespace
    base_module = types.ModuleType('pier.agents.installed.codex')
    observed = []
    class Codex:
        def populate_context_post_run(self, context):
            observed.append(self.logs_dir)
            assert not self.logs_dir.is_relative_to(trial)
            assert (self.logs_dir / 'sessions/normal.jsonl').read_bytes()
            (self.logs_dir / 'trajectory.json').write_bytes(b'{"usage":7,"steps":[{"message":"fixture"}]}')
            context.usage = 7
    base_module.Codex = Codex
    monkeypatch.setitem(sys.modules, 'pier.agents.installed.codex', base_module)
    spec = importlib.util.spec_from_file_location(
        'fixture_codex', Path(dradar.__file__).with_name('pier_codex.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    adapter = module.CodexRegistered()
    adapter.logs_dir = trial / 'agent'
    context = SimpleNamespace()
    adapter.populate_context_post_run(context)
    assert context.usage == 7
    assert adapter.logs_dir == trial / 'agent'
    assert not observed[0].exists()
    assert not (trial / 'agent/trajectory.json').exists()
    assert boundary.read_trial_file(trial, '.dradar/host-output/trajectory.json') == b'{"usage":7,"steps":[{"message":"fixture"}]}'


def test_failed_post_run_cannot_reuse_previous_outputs(trial):
    from types import SimpleNamespace
    class Adapter:
        logs_dir = trial / 'agent'
        @boundary.private_post_run
        def populate(self, context):
            (self.logs_dir / 'trajectory.json').write_bytes(b'{"steps":[{"message":"fixture"}]}')
            if context.fail:
                raise RuntimeError('fixture conversion failure')
    adapter = Adapter()
    adapter.populate(SimpleNamespace(fail=False))
    assert boundary.preferred_log_path(trial, 'trajectory.json') is not None
    with pytest.raises(RuntimeError, match='fixture conversion failure'):
        adapter.populate(SimpleNamespace(fail=True))
    with pytest.raises(boundary.UnsafeArtifact, match='post_run_not_finalized'):
        boundary.preferred_log_path(trial, 'trajectory.json')
    assert adapter.logs_dir == trial / 'agent'


def test_upload_consumes_one_log_snapshot(trial, monkeypatch):
    import json
    from dradar import runloop, artifact_staging
    (trial / 'artifacts').mkdir()
    (trial / 'artifacts/model.patch').write_bytes(b'diff --git a/a b/a\n-old\n+new\n')
    trajectory = trial / 'agent/trajectory.json'
    trajectory.write_text('{"fixture":"before"}')
    original_stage = artifact_staging.ensure_staged_patch
    def stage_then_change(root, entry):
        result = original_stage(root, entry)
        trajectory.write_text('{"fixture":"after"}')
        return result
    monkeypatch.setattr(artifact_staging, 'ensure_staged_patch', stage_then_change)
    monkeypatch.setattr(runloop, 'HOME', trial.parent)
    class Client:
        def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
                   outcome='completed', resume_generation=None, **kwargs):
            assert json.loads(trajectory.read_bytes()) == {'fixture': 'before'}
            return {'submission_id': 'fixture', 'grade_status': 'pending'}
    entry = {'assignment_id': 'fixture', 'nonce': 'fixture', 'trial_dir': str(trial),
             'task_id': 'fixture', 'keep': True}
    assert runloop._upload_trial(Client(), entry) == 'submitted'


def test_silent_conversion_failure_is_not_complete(trial):
    import json
    class Adapter:
        logs_dir = trial / 'agent'
        @boundary.private_post_run
        def populate(self, context):
            return None
    Adapter().populate(None)
    state = json.loads(boundary.read_trial_file(trial, '.dradar/host-output/state.json'))
    assert state == {'complete': False, 'outputs': [], 'reason': 'required_trajectory_missing'}
    with pytest.raises(boundary.UnsafeArtifact):
        boundary.preferred_log_path(trial, 'trajectory.json')


def test_absent_conversion_input_remains_allowed(tmp_path):
    import json
    root = tmp_path / 'trial'
    (root / 'agent').mkdir(parents=True)
    class Adapter:
        logs_dir = root / 'agent'
        @boundary.private_post_run
        def populate(self, context):
            return None
    Adapter().populate(None)
    state = json.loads(boundary.read_trial_file(root, '.dradar/host-output/state.json'))
    assert state['complete'] is True and state['reason'] == 'no_conversion_input'
    assert boundary.preferred_log_path(root, 'trajectory.json') is None


def test_platform_guard_precedes_go_and_direct_runner_work(tmp_path, monkeypatch):
    from dradar import runner, runloop
    from types import SimpleNamespace
    monkeypatch.setattr(boundary.os, 'supports_dir_fd', set())
    # Empty inputs would fail later if the platform guard did not run first.
    with pytest.raises(SystemExit, match='no agent was started'):
        runloop.cmd_go(SimpleNamespace())
    with pytest.raises(runner.RunnerError, match='no agent was started'):
        runner.run_trial({}, tmp_path, tmp_path)


def test_dsh_upload_uses_same_snapshot_as_bundle(trial, monkeypatch):
    from dradar import runloop
    (trial / 'artifacts').mkdir()
    (trial / 'artifacts/model.patch').write_bytes(b'diff --git a/a b/a\n-old\n+new\n')
    roots = []
    monkeypatch.setattr(runloop, 'HOME', trial.parent)
    monkeypatch.setattr(runloop, 'build_codex_trajectory_bundle', lambda root: roots.append(root) or None)
    monkeypatch.setattr(runloop, 'build_kimi_trajectory_bundle', lambda root: None)
    def usage(root):
        assert root == roots[0] and root != trial
        return None
    monkeypatch.setattr(runloop, '_dsh_trial_usage', usage)
    class Client:
        def submit(self, *args, **kwargs):
            return {'submission_id': 'fixture', 'grade_status': 'pending'}
    entry = {'assignment_id': 'fixture', 'nonce': 'fixture', 'trial_dir': str(trial),
             'task_id': 'fixture', 'keep': True, 'meta': {'dsh_version': 'fixture'}}
    assert runloop._upload_trial(Client(), entry) == 'submitted'


def test_missing_required_trajectory_blocks_upload_with_reason(trial, monkeypatch):
    from dradar import runloop, pending
    class Adapter:
        logs_dir = trial / 'agent'
        @boundary.private_post_run
        def populate(self, context):
            return None  # Mirrors a converter that logs a failure and returns.
    Adapter().populate(None)
    (trial / 'artifacts').mkdir()
    (trial / 'artifacts/model.patch').write_bytes(b'diff --git a/a b/a\n-old\n+new\n')
    monkeypatch.setattr(runloop, 'HOME', trial.parent)
    class NoNetwork:
        def __getattr__(self, name):
            raise AssertionError('failed conversion must not contact the server')
    entry = {'assignment_id': 'fixture', 'nonce': 'fixture', 'trial_dir': str(trial),
             'task_id': 'fixture', 'keep': True}
    assert runloop._upload_trial(NoNetwork(), entry) == 'upload-blocked'
    record = pending.load(trial.parent)[0]
    assert record['upload_blocked'] == 'unsafe_artifact'
    assert record['artifact_boundary_reason'] == 'required_trajectory_missing'
    # No built-in retry route clears the block or contacts the server.
    assert runloop._upload_trial(NoNetwork(), record) == 'upload-blocked'
    assert runloop._upload_trial(NoNetwork(), record, request_salvage=True) == 'upload-blocked'


def test_enumeration_change_detected_even_with_frozen_timestamps(trial, monkeypatch):
    # Directory timestamps may have coarse granularity. Freeze that entire
    # fingerprint contract; entry names/identities must independently catch change.
    monkeypatch.setattr(boundary, '_fingerprint', lambda info: (info.st_dev, info.st_ino))
    with boundary.TrialFiles(trial) as files:
        files.files('agent')
        (trial / 'agent/new.json').write_text('{}')
        with pytest.raises(boundary.UnsafeArtifact, match='snapshot_changed'):
            files.verify()


def test_unchanged_enumeration_reverification_is_repeatable(trial):
    with boundary.TrialFiles(trial) as files:
        files.files('agent')
        files.verify()
        files.verify()


def test_unsafe_existing_root_is_not_repaired_or_accepted(trial):
    trial.chmod(0o775)  # This fixture owns the directory; no runtime chmod.
    with pytest.raises(boundary.UnsafeArtifact, match='trial_not_host_private'):
        boundary.read_trial_file(trial, 'agent/sessions/normal.jsonl')
    assert trial.stat().st_mode & 0o777 == 0o775


def test_equal_length_content_change_with_frozen_metadata(trial, monkeypatch):
    monkeypatch.setattr(boundary, '_fingerprint', lambda info: (info.st_dev, info.st_ino))
    with boundary.TrialFiles(trial) as files:
        files.read('agent/sessions/normal.jsonl')
        (trial / 'agent/sessions/normal.jsonl').write_text('{"type":"updated"}\n')
        with pytest.raises(boundary.UnsafeArtifact, match='snapshot_changed'):
            files.verify()
