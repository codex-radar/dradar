"""Native Windows fixtures: only this test's ordinary temporary files."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from dradar import artifact_boundary as boundary

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='requires native Windows')


@pytest.fixture
def trial(tmp_path):
    root = tmp_path / 'trial'
    (root / 'agent/sessions').mkdir(parents=True)
    (root / 'agent/sessions/normal.jsonl').write_bytes(b'{"type":"fixture"}\n')
    return root


def test_normal_read_snapshot_host_write(trial):
    boundary.preflight_artifact_platform()
    assert boundary.read_trial_file(trial, 'agent/sessions/normal.jsonl')
    with boundary.snapshot_agent(trial) as snapshot:
        assert not snapshot.is_relative_to(trial)
        assert (snapshot / 'agent/sessions/normal.jsonl').read_bytes() == b'{"type":"fixture"}\n'
    with boundary.TrialFiles(trial) as files:
        files.write_host('.dradar/host-output/fixture.json', b'{}')
    assert boundary.read_trial_file(trial, '.dradar/host-output/fixture.json') == b'{}'


@pytest.mark.parametrize('name', ['../file', 'agent/normal.jsonl:stream', 'agent/name.', 'agent/name ', 'agent/CON', 'C:/file'])
def test_invalid_path_rejected(trial, name):
    with pytest.raises(boundary.UnsafeArtifact, match='outside_trial|unsafe_windows_name'):
        boundary.read_trial_file(trial, name)


def test_directory_hardlink_and_junction_rejected(trial):
    with pytest.raises(boundary.UnsafeArtifact, match='unsafe_file_type'):
        boundary.read_trial_file(trial, 'agent/sessions')
    os.link(trial / 'agent/sessions/normal.jsonl', trial / 'agent/second.jsonl')
    with pytest.raises(boundary.UnsafeArtifact, match='unsafe_file_type'):
        boundary.read_trial_file(trial, 'agent/second.jsonl')
    target = trial / 'agent/ordinary'
    target.mkdir()
    (target / 'fixture').write_bytes(b'fixture')
    link = trial / 'agent/junction'
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(target)],
                            capture_output=True, text=True)
    assert result.returncode == 0, 'fixture junction creation unavailable'
    with pytest.raises(boundary.UnsafeArtifact, match='windows_reparse_point'):
        boundary.read_trial_file(trial, 'agent/junction/fixture')


def test_read_handle_pins_file_and_parent(trial):
    source = trial / 'agent/sessions/normal.jsonl'
    with boundary.TrialFiles(trial) as files:
        files.read('agent/sessions/normal.jsonl')
        with pytest.raises(PermissionError):
            source.write_bytes(b'replacement fixture')
        with pytest.raises(PermissionError):
            source.parent.rename(source.parent.with_name('renamed'))
        files.verify()
    assert source.read_bytes() == b'{"type":"fixture"}\n'


def test_limits(trial, monkeypatch):
    with pytest.raises(boundary.UnsafeArtifact, match='file_limit'):
        boundary.read_trial_file(trial, 'agent/sessions/normal.jsonl', max_bytes=1)
    monkeypatch.setattr(boundary, 'MAX_ENTRIES', 1)
    with pytest.raises(boundary.UnsafeArtifact, match='entry_limit'):
        with boundary.snapshot_agent(trial):
            pass


def test_host_output_isolation_and_replacement(trial):
    with boundary.TrialFiles(trial) as files:
        with pytest.raises(boundary.UnsafeArtifact):
            files.write_host('agent/trajectory.json', b'{}')
        files.write_host('.dradar/host-output/fixture.json', b'first')
    with boundary.TrialFiles(trial) as files:
        files.write_host('.dradar/host-output/fixture.json', b'second')
    assert boundary.read_trial_file(trial, '.dradar/host-output/fixture.json') == b'second'


def test_patch_staging_and_mock_upload(trial, monkeypatch):
    from dradar import runloop, artifact_staging
    (trial / 'artifacts').mkdir()
    patch = b'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n'
    (trial / 'artifacts/model.patch').write_bytes(patch)
    stage = artifact_staging.ensure_staged_patch(trial)
    assert stage.data == patch
    monkeypatch.setattr(runloop, 'HOME', trial.parent / 'home')
    class Client:
        def submit(self, *args, **kwargs):
            return {'submission_id':'fixture', 'grade_status':'pending'}
    assert runloop._upload_trial(Client(), {'assignment_id':'fixture', 'nonce':'fixture',
        'task_id':'fixture', 'trial_dir':str(trial), 'keep':True}) == 'submitted'


def test_fixture_acl_with_other_writers_is_rejected(trial):
    assert boundary.read_trial_file(trial, 'agent/sessions/normal.jsonl')
    # Change only this disposable fixture's ACL, never an account/system path.
    result = subprocess.run(['icacls.exe', str(trial), '/grant', '*S-1-1-0:(OI)(CI)M'],
                            capture_output=True, text=True)
    assert result.returncode == 0
    with pytest.raises(boundary.UnsafeArtifact, match='trial_not_host_private'):
        boundary.read_trial_file(trial, 'agent/sessions/normal.jsonl')


def test_windows_runtime_layout_and_no_posix_owner(trial):
    from dradar.pier_runtime_safety import RuntimeSafety, RuntimeSafetyError
    safety = RuntimeSafety(trial / 'agent')
    assert safety.host_uid is None and safety.host_gid is None
    safety.prepare_host_layout()
    class NoExec:
        async def exec(self, **kwargs):
            raise AssertionError('must not guess a POSIX owner for Windows')
    # The Windows branch must complete without awaiting any environment I/O.
    # Drive it directly: ProactorEventLoop would create a loopback socketpair,
    # deliberately prohibited by this offline fixture's network guard.
    with pytest.raises(StopIteration):
        safety.return_runtime_tree_to_host_owner(NoExec(), '/logs/agent/fixture').send(None)
    with pytest.raises(RuntimeSafetyError):
        safety.return_runtime_tree_to_host_owner(NoExec(), '/outside').send(None)
