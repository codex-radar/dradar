"""Synthetic signals and durable handoff only: no provider, Docker or API."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dradar import cancellation, failure_reports, pending, runloop
from dradar.runner import RunnerCleanupUnconfirmedError
from test_go_menu import ASSIGNMENT, SubmitClient, _args, _fake_art
from private_artifact_fixture import private_trial


def test_scope_interrupts_once_and_restores_handler():
    old = signal.getsignal(signal.SIGINT)
    with cancellation.scope() as state:
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGINT)
        assert state.requested and state.finalizing
    assert signal.getsignal(signal.SIGINT) == old


def test_windows_break_handler_uses_same_contract(monkeypatch):
    installed = {}
    monkeypatch.setattr(signal, 'SIGBREAK', 987, raising=False)
    monkeypatch.setattr(signal, 'getsignal', lambda sig: 'old')
    monkeypatch.setattr(signal, 'signal', lambda sig, handler: installed.update({sig: handler}))
    with cancellation.scope() as state:
        with pytest.raises(KeyboardInterrupt):
            installed[987](987, None)
        installed[987](987, None)
        assert state.requested
    assert installed[987] == 'old'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX subprocess signal evidence')
def test_real_worker_cleanup_longer_than_old_ten_second_grace(tmp_path):
    ready, done = tmp_path / 'ready', tmp_path / 'done'
    code = '''import signal,sys,time
from pathlib import Path
ready,done=map(Path,sys.argv[1:])
def stop(*_):
 signal.signal(signal.SIGINT, signal.SIG_IGN)
 time.sleep(10.5)
 done.write_text("durable")
 sys.exit(0)
signal.signal(signal.SIGINT,stop)
ready.write_text("ready")
while True: time.sleep(.01)
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(ready), str(done)])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        runloop._signal_workers([child])
        assert child.wait(timeout=2) == 0
        assert done.read_text() == 'durable'
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_pool_timeout_is_finite_and_only_signals_supplied_children(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(runloop.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(runloop.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    class Child:
        def __init__(self): self.calls=[]
        def poll(self): return None
        def send_signal(self, sig): self.calls.append(sig)
        def terminate(self): self.calls.append('term')
        def kill(self): self.calls.append('kill')
    own, foreign = Child(), Child()
    runloop._signal_workers([own])
    assert own.calls == [signal.SIGINT if os.name != 'nt' else signal.CTRL_BREAK_EVENT, 'term', 'kill']
    assert foreign.calls == []
    assert 125 <= clock[0] < 125.2


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    private_trial(tmp_path / "trial")
    monkeypatch.setattr(runloop, 'HOME', tmp_path / 'home')
    monkeypatch.setattr(runloop.image_cache, 'remove_trial_builder', lambda *a, **k: (True, None))
    monkeypatch.setattr(runloop.image_cache, 'record_trial_images', lambda *a, **k: None)
    monkeypatch.setattr(runloop, '_report_failure_quietly', lambda *a, **k: None)
    return tmp_path


def test_completed_result_is_durable_before_slow_cleanup_and_repeat_interrupt(isolated, monkeypatch):
    art = _fake_art(isolated)
    client = SubmitClient({})
    assignment = dict(ASSIGNMENT, batch_id='batch-one', owner_epoch=7, resume_generation=3)
    monkeypatch.setattr(runloop, 'run_trial', lambda *a, **k: art)
    # Raise after validating the durable handoff; a crash must leave the same row.
    def crash(*a, **k):
        row = pending.load(runloop.HOME)[0]
        assert row['owner_epoch'] == 7 and row['batch_id'] == 'batch-one'
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGINT)
        raise RuntimeError('synthetic cleanup crash')
    monkeypatch.setattr(runloop.image_cache, 'cleanup_trial_resources', crash)
    with pytest.raises(RuntimeError, match='synthetic cleanup'):
        runloop._run_and_submit(client, assignment, isolated, _args(), 'abc')
    row = pending.load(runloop.HOME)[0]
    assert row['outcome'] == 'completed' and art.patch.exists()
    assert runloop._upload_trial(client, row) == 'submitted'
    assert len(client.submissions) == 1 and pending.load(runloop.HOME) == []


def test_interrupted_result_uses_original_upload_path_and_exits(isolated, monkeypatch):
    art = _fake_art(isolated, rc=124)
    def run(*a, **k):
        cancellation.protect_finalization(cancelled=True)
        return art
    monkeypatch.setattr(runloop, 'run_trial', run)
    client = SubmitClient({})
    with pytest.raises(KeyboardInterrupt):
        runloop._run_and_submit(client, dict(ASSIGNMENT), isolated, _args(), 'abc')
    assert [x['outcome'] for x in client.submissions] == ['interrupted']
    assert pending.load(runloop.HOME) == []


def test_unconfirmed_cleanup_fences_without_fabricating_artifacts(isolated, monkeypatch, capsys):
    job = isolated / 'home/work/jobs/exact-job'
    def run(*a, **k):
        cancellation.protect_finalization(cancelled=True)
        raise RunnerCleanupUnconfirmedError('unknown writer', job_dir=job)
    monkeypatch.setattr(runloop, 'run_trial', run)
    client = SubmitClient({})
    assert runloop._run_and_submit(client, dict(ASSIGNMENT), isolated, _args(), 'abc') == 'cleanup-unconfirmed'
    row = pending.load(runloop.HOME)[0]
    assert row['record_kind'] == 'cleanup_quarantine'
    assert row['upload_blocked'] == 'cleanup_unconfirmed'
    assert 'trial_dir' not in row and 'outcome' not in row
    assert runloop._upload_trial(client, row) == 'upload-blocked'
    assert runloop._run_and_submit(client, dict(ASSIGNMENT), isolated, _args(), 'abc') == 'pending-upload'
    assert client.submissions == []
    output = capsys.readouterr().out
    assert 'result is unknown' in output
    assert 'durable completed result pending upload' not in output


def test_retry_upload_keeps_quarantine_and_retries_only_real_result(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, 'HOME', tmp_path)
    quarantine = {
        'assignment_id': 'unknown',
        'batch_id': '12345678123456781234567812345678',
        'upload_blocked': 'cleanup_unconfirmed',  # old 242 row
    }
    result = {
        'assignment_id': 'finished',
        'batch_id': '12345678123456781234567812345678',
        'trial_dir': str(tmp_path / 'trial'),
        'outcome': 'completed',
    }
    pending.record(tmp_path, quarantine)
    pending.record(tmp_path, result)
    monkeypatch.setattr(failure_reports, 'flush_pending',
                        lambda *_args: {'received': 0, 'send_failed': 0})
    attempted = []
    monkeypatch.setattr(runloop, '_upload_trial',
                        lambda _client, entry: attempted.append(entry['assignment_id']) or 'upload-failed')
    assert runloop._retry_pending_uploads(SimpleNamespace()) == ['upload-failed']
    assert attempted == ['finished']
    assert pending.load(tmp_path) == [quarantine, result]
    capsys.readouterr()

    monkeypatch.setattr(runloop, '_load_config', lambda: {})
    monkeypatch.setattr(runloop, '_client', lambda _cfg: SimpleNamespace())
    assert runloop.cmd_retry_upload(SimpleNamespace(benchmark=None, request_salvage=None)) == 1
    output = capsys.readouterr().out
    assert 'results are unknown' in output
    assert '1 still pending and retryable' in output
    assert attempted == ['finished', 'finished']


def test_quarantine_kind_cannot_be_salvaged_even_with_owner_superseded(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, 'HOME', tmp_path)
    row = {
        'assignment_id': 'unknown', 'task_id': 'task',
        'record_kind': 'cleanup_quarantine',
        'upload_blocked': 'owner_superseded',
        'trial_dir': str(tmp_path / 'trial'), 'outcome': 'completed',
    }
    pending.record(tmp_path, row)
    class MustStayLocal:
        def __getattr__(self, name):
            pytest.fail(f'quarantine must not call client.{name}')
    assert runloop._upload_trial(MustStayLocal(), row, request_salvage=True) == 'upload-blocked'
    assert pending.load(tmp_path) == [row]
    assert 'result is unknown' in capsys.readouterr().out
    monkeypatch.setattr(runloop, '_load_config', lambda: {})
    monkeypatch.setattr(runloop, '_client', lambda _cfg: MustStayLocal())
    assert runloop.cmd_retry_upload(SimpleNamespace(
        benchmark=None, request_salvage='unknown', yes=True,
    )) == 2
    assert pending.load(tmp_path) == [row]
    assert 'salvage and retry-upload cannot clear' in capsys.readouterr().out


def test_kind_only_quarantine_stops_scoped_refill_before_retry(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(runloop, 'HOME', tmp_path)
    row = {'assignment_id': 'unknown', 'record_kind': 'cleanup_quarantine'}
    monkeypatch.setattr(runloop, '_scoped_fleet_refill', lambda _args: True)
    monkeypatch.setattr(runloop, '_pool_abort_reason', lambda: None)
    monkeypatch.setattr(runloop, '_run_config', lambda _args: {
        'run_plan_id': 'plan', 'run_plan_logical_session_id': 'drl_session',
        'run_plan_credential_generation': 0, 'run_plan_intent_revision': 1,
        'run_plan_current_start_intent_id': 'a' * 32,
    })
    monkeypatch.setattr(runloop, '_pending_uploads_for_client_batch',
                        lambda _client, _batch: [row])
    monkeypatch.setattr(runloop, '_retry_pending_uploads',
                        lambda *_a, **_k: pytest.fail('quarantine must not retry'))
    class Client:
        def heartbeat_run_plan(self, **kwargs):
            assert kwargs == {'plan_id': 'plan', 'current_start_intent_id': 'a' * 32,
                              'expected_intent_revision': 1, 'expected_generation': 0}
            return {'touched': True, 'starts_new_work': False, 'plan_id': 'plan',
                    'current_start_intent_id': 'a' * 32, 'device_intent_revision': 1}
        def run_plan_progress(self, plan_id):
            assert plan_id == 'plan'
            return {'envelope': {'agent_action': 'continue'}}
        def start_run_plan(self, **_kwargs):
            pytest.fail('continuation must not create a new admission')
    with pytest.raises(SystemExit, match='result is unknown'):
        runloop._wait_for_scoped_refill_work(
            SimpleNamespace(batch_id='batch'), Client(), desired_workers=1,
        )


def test_acquisition_describes_unknown_result_and_fences_model(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, 'HOME', tmp_path)
    pending.record(tmp_path, {
        'assignment_id': 'unknown', 'record_kind': 'cleanup_quarantine',
    })
    class Client:
        def get_assignment(self):
            return {'active': [{'assignment_id': 'unknown'}], 'free_pick': True}
    client = Client()
    assert runloop._acquire_batch(client, True) == ([], True)
    output = capsys.readouterr().out
    assert 'unknown result' in output
    assert 'completed assignment' not in output
    pending.record(tmp_path, {
        'assignment_id': 'unheld_result', 'trial_dir': str(tmp_path / 'trial'),
        'outcome': 'completed',
    })
    assert runloop._run_batch(SimpleNamespace(), client, tmp_path,
                              [{'assignment_id': 'unknown'}]) == 1
    output = capsys.readouterr().out
    assert 'unknown result' in output
    assert 'durable pending results' not in output


def test_interrupted_artifact_durable_before_failure_reporting(isolated, monkeypatch):
    art = _fake_art(isolated, rc=124)
    monkeypatch.setattr(runloop, 'run_trial', lambda *a, **k: art)
    client = SubmitClient({})
    def blocked_report(*a, **kw):
        if kw.get('phase') != 'agent':
            return
        row = pending.load(runloop.HOME)[0]
        assert row['outcome'] == 'interrupted'
        assert Path(row['patch_source_path']).read_bytes() == art.patch.read_bytes()
        assert Path(row['patch_staged_path']).is_file()
        raise RuntimeError('synthetic long failure report')
    monkeypatch.setattr(runloop, '_report_failure_quietly', blocked_report)
    with pytest.raises(RuntimeError, match='long failure report'):
        runloop._run_and_submit(client, dict(ASSIGNMENT), isolated, _args(), 'abc')
    assert client.submissions == []
    assert runloop._upload_trial(client, pending.load(runloop.HOME)[0]) == 'interrupted'



def test_staging_creates_and_revalidates_private_host_directories(isolated):
    from dradar.artifact_boundary import TrialFiles
    from dradar.artifact_staging import ensure_staged_patch, SOURCE_RELATIVE
    art = _fake_art(isolated)
    assert not (art.trial_dir / ".dradar").exists()
    staged = ensure_staged_patch(art.trial_dir)
    assert staged.source.read_bytes() == art.patch.read_bytes()
    with TrialFiles(art.trial_dir) as boundary:
        # Reopen the directories using the same private-owner checks used by
        # production creation, including the already-existing (183) branch.
        boundary.parent(".dradar/artifact-staging/revalidation", create=True)
        assert boundary.read(SOURCE_RELATIVE) == art.patch.read_bytes()


@pytest.mark.skipif(os.name != "nt", reason="native Windows owner/ACL boundary")
def test_staging_rejects_existing_foreign_owner_without_repair(isolated):
    import ctypes
    from dradar.artifact_boundary import TrialFiles, UnsafeArtifact
    from dradar.artifact_boundary_win import WinAPI, SECURITY_ATTRIBUTES
    from dradar.artifact_staging import ensure_staged_patch
    art = _fake_art(isolated)
    original_patch = art.patch.read_bytes()
    foreign = art.trial_dir / ".dradar"
    api = WinAPI(UnsafeArtifact)
    descriptor = ctypes.c_void_p()
    # The elevated Windows CI token can create an Administrators-owned
    # synthetic directory. The DACL remains narrow; it is the owner that is
    # deliberately foreign to the current user SID.
    sddl = f"O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{api.user})"
    assert api.from_sddl(sddl, 1, ctypes.byref(descriptor), None)
    try:
        attributes = SECURITY_ATTRIBUTES(
            ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False,
        )
        assert api.mkdir(str(foreign), ctypes.byref(attributes))
    finally:
        api.free(descriptor)
    for _ in range(2):
        with pytest.raises(UnsafeArtifact, match="trial_not_host_private"):
            ensure_staged_patch(art.trial_dir)
        # No implicit takeover: the original foreign owner still fails.
        with TrialFiles(art.trial_dir) as boundary:
            handle = boundary._open(foreign, directory=True)
            with pytest.raises(UnsafeArtifact, match="trial_not_host_private"):
                boundary.api.private(handle)
    assert not (foreign / "artifact-staging").exists()
    assert art.patch.read_bytes() == original_patch



def test_nested_upload_snapshots_are_private_and_preserve_bytes(isolated):
    from dradar.artifact_boundary import TrialFiles, snapshot_agent
    art = _fake_art(isolated)
    payload = b'{"answer":true}'
    (art.trial_dir / "result.json").write_bytes(payload)
    nested = Path("agent/sessions/child/events.jsonl")
    (art.trial_dir / nested).parent.mkdir(parents=True)
    (art.trial_dir / nested).write_bytes(payload + b"\n")
    with snapshot_agent(art.trial_dir, include_result=True) as first:
        with TrialFiles(first) as boundary:
            boundary.parent(nested, create=True)
            assert boundary.read(nested) == payload + b"\n"
        # The uploader snapshots once, then bundle construction snapshots the
        # returned directory again. Both complete trees must pass the guard.
        with snapshot_agent(first, include_result=True) as second:
            with TrialFiles(second) as boundary:
                boundary.parent(nested, create=True)
                assert boundary.read("result.json") == payload
                assert boundary.read(nested) == payload + b"\n"
        assert not second.exists()
    assert not first.exists()
    assert (art.trial_dir / "result.json").read_bytes() == payload


def test_private_post_run_creates_valid_empty_agent_snapshot(isolated):
    from types import SimpleNamespace
    from dradar.artifact_boundary import TrialFiles, private_post_run
    art = _fake_art(isolated)
    assert not (art.trial_dir / "agent").exists()
    provider = SimpleNamespace(logs_dir=art.trial_dir / "agent")
    seen = []
    @private_post_run
    def post_run(self, context):
        with TrialFiles(self.logs_dir.parent) as boundary:
            boundary.parent("agent/probe", create=True)
        seen.append(self.logs_dir.parent)
        return "observed"
    assert post_run(provider, None) == "observed"
    assert provider.logs_dir == art.trial_dir / "agent"
    assert not seen[0].exists()
    with TrialFiles(art.trial_dir) as boundary:
        boundary.parent(".dradar/host-output/state.json", create=True)
        assert boundary.read(".dradar/host-output/state.json")
