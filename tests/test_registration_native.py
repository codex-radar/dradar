"""Native OS registration contracts, real child/socket and no model/service.

The HTTP fixture simulates the close/start contract separately established
against the real server in QA. It is not a production server or billing test.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from dradar import cancellation, runner, worker_events
from dradar.api_client import ApiError
from dradar.flight_recorder import _exclusive_file_lock
from dradar.registration import RegistrationWindow
from dradar.runloop import _mark_stopped_quietly
from test_registration_recovery import fixture


def wait_until(predicate, timeout=5):
    until = time.monotonic() + timeout
    while not (value := predicate()):
        assert time.monotonic() < until, 'native fixture did not become ready'
        time.sleep(.01)
    return value


def completed_worker_event(sidecar, session_id, child):
    assert child.poll() is None, f'native worker exited before readiness: {child.returncode}'
    try:
        text = sidecar.read_text()
    except (FileNotFoundError, PermissionError, UnicodeDecodeError):
        return None
    # Creation can be observed before the child's append is complete. The
    # fixture must wait for an actual event, just as the product reader does.
    if not text.endswith('\n'):
        return None
    event = worker_events.parse_worker_event(text.splitlines()[-1])
    return event if event and event.session_id == session_id else None


@pytest.mark.parametrize('fault', [
    'normal', 'delay_first', 'wrong_ack', 'start_disconnect',
    'close_disconnect', 'cancel_late',
])
def test_real_worker_gate_socket_cancel_and_cleanup(tmp_path, monkeypatch, fault):
    with fixture(tmp_path, monkeypatch, fault) as (_, api, telemetry, assignment, state):
        sidecar, gate, marker = (tmp_path/name for name in ('ready.jsonl', 'gate.json', 'crossed'))
        sidecar.touch()  # deterministic empty-file-before-append window
        identity = dict(schema=worker_events.WORKER_START_SCHEMA, nonce='f'*32,
                        session_id=telemetry.session_id, job='native', parent_pid=os.getpid())
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
        env.update({worker_events.WORKER_EVENT_FILE_ENV: str(sidecar),
                    worker_events.WORKER_START_ENV: json.dumps(dict(identity, path=str(gate.resolve()))),
                    'DRADAR_RUNNER_SESSION_ID': telemetry.session_id})
        child = subprocess.Popen([sys.executable, '-c',
            'import asyncio,pathlib,sys,time; from dradar.worker_events import register_worker; '
            'asyncio.run(register_worker()); pathlib.Path(sys.argv[1]).write_text("crossed"); '
            'time.sleep(.5)', str(marker)], env=env, **runner._pier_process_options())
        cancel_thread = None
        try:
            event = wait_until(lambda: completed_worker_event(sidecar, telemetry.session_id, child))
            assert event and event.start_deadline
            window = RegistrationWindow(event.start_deadline, lambda: child.poll() is None)
            with cancellation.scope() as stop:
                if fault == 'cancel_late':
                    def cancel():
                        if state['start_received'].wait(5): stop.requested = True
                    cancel_thread = threading.Thread(target=cancel)
                    cancel_thread.start()
                if fault in ('normal', 'delay_first'):
                    window.bind(api, telemetry, assignment)
                    runner._materialize_shared_file(gate, json.dumps(dict(
                        identity, expires_at=window.deadline)).encode(), check=window.check)
                    window.finish()
                    assert child.wait(timeout=5) == 0 and marker.exists()
                    assert state['started']
                else:
                    with pytest.raises(KeyboardInterrupt if fault == 'cancel_late' else ApiError):
                        window.bind(api, telemetry, assignment)
                    assert not gate.exists() and not marker.exists()
            if fault not in ('normal', 'delay_first'):
                confirmed = _mark_stopped_quietly(api, assignment)
                assert confirmed == (fault != 'close_disconnect')
                assert state['started'] == (fault == 'close_disconnect')
                if fault == 'close_disconnect':
                    assert assignment['_registration_start_uncertain']
                    assert '/api/v1/assignment/stopped' not in state['paths']
                if fault == 'cancel_late':
                    assert state['late_done'].wait(5) and state['late_status'] == 409
                runner._terminate_pier_process_tree(child)
                assert child.poll() is not None
                # Actual Windows TerminateProcess/reap, without pretending to
                # verify descendants that the product itself cannot audit.
                if os.name == 'nt':
                    with pytest.raises(runner.RunnerError, match='cannot be confirmed'):
                        runner._confirm_pier_process_tree_stopped(child)
                else:
                    runner._confirm_pier_process_tree_stopped(child)
            assert state['hb'] <= 2 and state['flight'] <= 2
            assert state['paths'].count('/api/v1/assignment/started') <= 1
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            if cancel_thread:
                cancel_thread.join(timeout=5)
                assert not cancel_thread.is_alive()


LOCK_HOLDER = r'''
import os,pathlib,sys
f=open(sys.argv[1],'a+b')
f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
if os.name=='nt':
    import msvcrt
    msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
else:
    import fcntl
    fcntl.flock(f.fileno(),fcntl.LOCK_EX)
pathlib.Path(sys.argv[2]).write_text('locked')
sys.stdin.buffer.read(1)
f.seek(0)
if os.name=='nt': msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
else: fcntl.flock(f.fileno(),fcntl.LOCK_UN)
f.close()
'''


def test_native_external_lock_contention_deadline_and_release(tmp_path):
    lock_path, ready = tmp_path/'recorder.lock', tmp_path/'locked'
    holder = subprocess.Popen([sys.executable, '-c', LOCK_HOLDER, str(lock_path), str(ready)],
                              stdin=subprocess.PIPE)
    try:
        wait_until(ready.exists)
        window = RegistrationWindow(time.monotonic()+120, lambda: True)
        start = time.monotonic()
        window.deadline = start+.2
        with pytest.raises(ApiError, match='budget'):
            with _exclusive_file_lock(lock_path, check=window.check):
                pytest.fail('cross-process lock did not exclude registration')
        assert .18 <= time.monotonic()-start < 2
        holder.communicate(b'x', timeout=5)
        assert holder.returncode == 0
        fresh = RegistrationWindow(time.monotonic()+120, lambda: True)
        with _exclusive_file_lock(lock_path, check=fresh.check):
            pass  # proves native lock was released and failed waiter closed fd
    finally:
        if holder.poll() is None: holder.kill()
        holder.wait(timeout=5)
