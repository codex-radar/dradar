"""Terminal startup reporting must not erase checkout/return safety evidence."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dradar import runloop
from test_workers import _args, _patch_pool_setup

TAIL = 'pool ended before startup acknowledgement'


@pytest.mark.parametrize('state', ['assignment-1', 'waiting:assignment-1', 'unknown-phase',
                                   'preparing:runner_session_capacity_reached'])
def test_tail_preserves_existing_stage(monkeypatch, tmp_path, state):
    path = tmp_path / 'worker.started'
    path.write_text(state)
    monkeypatch.setenv(runloop._POOL_WORKER_ACTIVITY_ENV, str(path))
    runloop._publish_fleet_startup_failure(_args(worker_child=True), TAIL)
    assert path.read_text() == state


def test_real_checkout_marker_survives_tail(monkeypatch, tmp_path):
    path = tmp_path / 'worker.started'
    path.write_text('preparing')
    monkeypatch.setenv(runloop._POOL_WORKER_ACTIVITY_ENV, str(path))
    assert runloop._record_supervised_worker_checkout(_args(worker_child=True), 'assignment-1')
    runloop._publish_fleet_startup_failure(_args(worker_child=True), TAIL)
    assert path.read_text() == 'assignment-1'


@pytest.mark.parametrize('state', ['preparing', 'preparing:worker-entrypoint-failed'])
def test_genuine_precheckout_failure_remains_classified(monkeypatch, tmp_path, state):
    path = tmp_path / 'worker.started'
    path.write_text(state)
    monkeypatch.setenv(runloop._POOL_WORKER_ACTIVITY_ENV, str(path))
    runloop._publish_fleet_startup_failure(_args(worker_child=True), ModuleNotFoundError('fixture'))
    assert path.read_text() == 'preparing:startup-dependency-missing'


def test_missing_marker_is_not_invented_as_safe_precheckout(monkeypatch, tmp_path):
    path = tmp_path / 'missing'
    monkeypatch.setenv(runloop._POOL_WORKER_ACTIVITY_ENV, str(path))
    runloop._publish_fleet_startup_failure(_args(worker_child=True), TAIL)
    assert not path.exists()


@pytest.mark.parametrize('kind', ['acknowledged-return', 'unconfirmed-stop', 'quarantined'])
def test_parent_replacement_uses_return_proof_and_keeps_historical_failure(monkeypatch, tmp_path, kind):
    _patch_pool_setup(monkeypatch, active_count=1)
    monkeypatch.setattr(runloop, 'HOME', tmp_path)
    monkeypatch.setattr(runloop.time, 'sleep', lambda _: None)
    monkeypatch.setattr(runloop, '_pool_backfill_delay', lambda _: 0)
    monkeypatch.setattr(runloop, '_POOL_BACKFILL_REFRESH_SECONDS', 0)
    monkeypatch.setattr(runloop, '_pending_assignment_ids_for_client', lambda *_a, **_k: set())
    row = dict(assignment_id='assignment-1', leased_at=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat(),
               started_at=None, execution_state='waiting', runner_state='waiting', heartbeat_running=False,
               runner_phase=None)
    active = [row]
    class Client:
        def get_assignment(self):
            return {'active': active}
        def mark_stopped(self, *_a, **_k):
            if kind != 'acknowledged-return':
                raise runloop.ApiError('stop unconfirmed', status_code=409)
            return {'ok': True}
    client = Client()
    monkeypatch.setattr(runloop, '_client', lambda *_a, **_k: client)
    spawned = []
    class Process:
        def __init__(self, command, env, **kwargs):
            self.pid = len(spawned) + 100
            self.returncode = 1 if not spawned else 0
            if kind == 'quarantined':
                self.returncode = runloop._WORKER_SLOT_QUARANTINED_EXIT_CODE
            with monkeypatch.context() as child:
                child.setenv(runloop._POOL_WORKER_ACTIVITY_ENV, env[runloop._POOL_WORKER_ACTIVITY_ENV])
                runloop._record_supervised_worker_checkout(_args(worker_child=True), 'assignment-1')
                if not spawned:
                    if kind != 'quarantined':
                        runloop._mark_stopped_quietly(client, 'assignment-1', defer_seconds=0)
                else:
                    active.clear()
                runloop._publish_fleet_startup_failure(_args(worker_child=True), TAIL)
            spawned.append(self)
        def poll(self):
            return self.returncode
    monkeypatch.setattr(runloop.subprocess, 'Popen', Process)
    assert runloop._run_worker_pool(_args(workers=1)) == 1
    assert len(spawned) == (2 if kind == 'acknowledged-return' else 1)
