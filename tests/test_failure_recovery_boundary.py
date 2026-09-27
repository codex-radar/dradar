"""Failures stop new work; only an explicit recovery may start it again."""
import json
import threading

import pytest

from dradar import run_intent, runloop
from test_go_menu import ASSIGNMENT, _args, _fake_art
from test_diagnosis import InvalidAckClient
from test_checkout import CheckoutClient, _cell


def test_transport_upload_is_preserved_but_runtime_cannot_continue(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    abort = tmp_path / 'pool.stop'
    monkeypatch.setattr(runloop, 'HOME', home)
    monkeypatch.setenv(runloop._POOL_ABORT_ENV, str(abort))
    art = _fake_art(tmp_path, rc=1, result_data={
        'exception_info': {'exception_type': 'NonZeroAgentExitCodeError',
            'exception_message': 'reqwest error stream: error sending request for url (https://cli-chat-proxy.grok.com/v1/responses)'},
        'agent_result': {},
    })
    monkeypatch.setattr(runloop, 'run_trial', lambda *a, **k: art)
    client = InvalidAckClient({})
    submit = client.submit
    def checked_submit(*a, **kw):
        assert runloop._pool_stop_directive() == (False, 'runtime failure: provider transport failed')
        return submit(*a, **kw)
    monkeypatch.setattr(client, 'submit', checked_submit)
    assignment = dict(ASSIGNMENT)
    outcome = runloop._run_and_submit(client, assignment, tmp_path, _args(), None)
    assert outcome == 'provider-transport-failed'
    assert client.submissions[0]['outcome'] == 'interrupted'
    assert client.submissions[0]['meta']['failure_code'] == 'provider_stream_failed'
    assert assignment['_confirmed_upload_outcome'] == 'interrupted'
    recorded = []
    monkeypatch.setattr(runloop, '_assignment_boundary_path', lambda args: tmp_path/'boundary')
    monkeypatch.setattr(runloop.assignment_boundary, 'record_outcome', lambda p, a, o: recorded.append(o))
    assert runloop._record_assignment_boundary(_args(), assignment, outcome)
    assert recorded == ['interrupted']
    with pytest.raises(run_intent.IntentStopped):
        with run_intent.worker_launch_guard(home):
            pytest.fail('a later model must not launch')


def test_checkout_in_flight_returns_ownership_without_starting(tmp_path, monkeypatch):
    abort = tmp_path/'pool.stop'
    monkeypatch.setenv(runloop._POOL_ABORT_ENV, str(abort))
    monkeypatch.setattr(runloop, '_check_version_pin', lambda *a, **k: None)
    monkeypatch.setattr(runloop, '_run_and_submit', lambda *a, **k: pytest.fail('model started after drain'))
    cell = _cell('waiting')
    client = CheckoutClient({'active': [cell], 'free_pick': True}, [{'assignment': cell}])
    checkout = client.checkout
    def late_checkout(**kwargs):
        abort.write_text('drain:runtime failure: sibling failed')
        return checkout(**kwargs)
    client.checkout = late_checkout
    assert runloop._run_checkout_loop(_args(), client, tmp_path, [cell]) == 0
    assert client.stopped == ['waiting']
    assert len(client.checkout_exclusions) == 1


def test_failed_runner_publishes_drain_before_returning_assignment(tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, 'HOME', tmp_path)
    monkeypatch.setenv(runloop._POOL_ABORT_ENV, str(tmp_path/'pool.stop'))
    monkeypatch.setattr(runloop.image_cache, 'remove_trial_builder', lambda *a, **k: (True, None))
    def failure(*a, **k):
        raise runloop.CodexInstallError('task image unavailable')
    monkeypatch.setattr(runloop, 'run_trial', failure)
    def stopped(*a, **k):
        assert runloop._pool_stop_directive() == (False, 'runtime failure: runner failed')
        return {'ok': True}
    client = InvalidAckClient({})
    client.mark_stopped = stopped
    assert runloop._run_and_submit(client, dict(ASSIGNMENT), tmp_path, _args(), None) == 'failed'


def test_stop_publication_is_atomic_and_final_launch_shares_exact_lock(tmp_path, monkeypatch):
    batch = 'b'*32
    generation = run_intent.begin(tmp_path, batch)
    abort = tmp_path/'pool.stop'
    monkeypatch.setattr(runloop, 'HOME', tmp_path)
    monkeypatch.setenv(run_intent.BATCH_ENV, batch)
    monkeypatch.setenv(run_intent.GENERATION_ENV, generation)
    monkeypatch.setenv(runloop._POOL_ABORT_ENV, str(abort))
    replace = runloop.os.replace
    def checked_replace(source, destination):
        assert not abort.exists()
        assert source.read_text() == 'drain:runtime failure: test'
        return replace(source, destination)
    monkeypatch.setattr(runloop.os, 'replace', checked_replace)
    started = threading.Event()
    done = threading.Event()
    def publish():
        started.set()
        runloop._signal_pool_abort('runtime failure: test', interrupt_siblings=False)
        done.set()
    with run_intent.worker_launch_guard(tmp_path):
        thread = threading.Thread(target=publish)
        thread.start()
        assert started.wait(1)
        assert not done.wait(.05)
    thread.join(2)
    assert done.is_set()
    with pytest.raises(run_intent.IntentStopped):
        with run_intent.worker_launch_guard(tmp_path):
            pytest.fail('new provider permission after stop')
    # Another account/plan uses a different scope and remains usable.
    monkeypatch.setattr(runloop.os, 'replace', replace)
    other = 'c'*32
    monkeypatch.setenv(run_intent.BATCH_ENV, other)
    monkeypatch.setenv(run_intent.GENERATION_ENV, run_intent.begin(tmp_path, other))
    monkeypatch.setenv(runloop._POOL_ABORT_ENV, str(tmp_path/'other.stop'))
    with run_intent.worker_launch_guard(tmp_path):
        pass


@pytest.mark.parametrize("reason", ["runtime failure: provider transport failed", "account quota exhausted"])
def test_failure_drain_keeps_inflight_workers_and_returns_failure(monkeypatch, reason):
    from test_workers import _patch_pool_setup, _ScriptedProcess, _args as pool_args
    _patch_pool_setup(monkeypatch, active_count=2)
    monkeypatch.setattr(runloop.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(runloop, '_signal_workers', lambda *a: pytest.fail('must not kill paid work'))
    monkeypatch.setattr(runloop, '_pool_ready_work_count', lambda *a, **k: pytest.fail('must not backfill'))
    calls = []
    def popen(command, env, **kwargs):
        mark = None
        if not calls:
            mark = lambda: runloop.Path(env[runloop._POOL_ABORT_ENV]).write_text('drain:' + reason)
        process = _ScriptedProcess(command, env, [1] if not calls else [None, None, 0], on_poll=mark, **kwargs)
        calls.append(process)
        return process
    monkeypatch.setattr(runloop.subprocess, 'Popen', popen)
    assert runloop._run_worker_pool(pool_args(workers=2)) == 1
    assert len(calls) == 2
    assert calls[1].polls == []


def test_marker_write_failure_uses_existing_intent_fence(tmp_path, monkeypatch, capsys):
    from dradar import fleet
    batch = 'd'*32
    generation = run_intent.begin(tmp_path, batch)
    monkeypatch.setattr(runloop, 'HOME', tmp_path)
    monkeypatch.setenv(run_intent.BATCH_ENV, batch)
    monkeypatch.setenv(run_intent.GENERATION_ENV, generation)
    marker = tmp_path/'pool.stop'
    monkeypatch.setenv(runloop._POOL_ABORT_ENV, str(marker))
    original_open = runloop.Path.open
    def broken_marker(path, *args, **kwargs):
        if path.name.startswith('.pool.stop.'):
            raise OSError('injected marker directory failure')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(runloop.Path, 'open', broken_marker)
    monkeypatch.setattr(fleet, '_request_pool_drain', lambda *a, **k: 'marker still unavailable')
    runloop._signal_pool_abort('runtime failure: test', interrupt_siblings=False)
    assert not marker.exists()
    assert runloop._pool_stop_directive() == (False, 'this run intent was stopped')
    with pytest.raises(run_intent.IntentStopped):
        with run_intent.worker_launch_guard(tmp_path):
            pytest.fail('fallback did not fence new launch')
    assert 'could not publish' in capsys.readouterr().out
    # Fleet may repeat reduction for this same generation, never a newer one.
    run_intent.stop(tmp_path, batch, expected_generation=generation, allow_already_stopped=True)
    new_generation = run_intent.begin(tmp_path, batch)
    with pytest.raises(run_intent.IntentStopped):
        run_intent.stop(tmp_path, batch, expected_generation=generation, allow_already_stopped=True)
    run_intent.require(tmp_path, batch, new_generation)


def test_paid_sibling_upload_does_not_refill_after_pool_drain(tmp_path, monkeypatch):
    marker = tmp_path/'pool.stop'
    monkeypatch.setenv(runloop._POOL_ABORT_ENV, str(marker))
    monkeypatch.setattr(runloop, '_check_version_pin', lambda *a, **k: None)
    monkeypatch.setattr(runloop.refill_plan, 'is_running', lambda *a: True)
    monkeypatch.setattr(runloop.refill_plan, 'refill_once', lambda *a, **k: pytest.fail('refill after drain'))
    settled = []
    monkeypatch.setattr(runloop.refill_plan, 'mark_submitted', lambda home, aid: settled.append(aid))
    monkeypatch.setattr(runloop.refill_plan, 'stop', lambda *a: None)
    def paid_run(*a, **k):
        marker.write_text('drain:runtime failure: sibling failed')
        return 'submitted'
    monkeypatch.setattr(runloop, '_run_and_submit', paid_run)
    cell = _cell('paid-sibling')
    client = CheckoutClient({'active': [cell], 'free_pick': True}, [{'assignment': cell}])
    args = _args()
    args.refill = True
    assert runloop._run_checkout_loop(args, client, tmp_path, [cell]) == 0
    assert settled == ['paid-sibling']
    assert len(client.checkout_exclusions) == 1
