"""Ordinary go selection → real API contract → exact worker admission, no model."""
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from dradar import assignment_boundary, cli, runloop
from dradar.api_client import ApiClient, ApiError
from test_workers import _args, _patch_pool_setup

BATCHES = ['a' * 32, 'b' * 32]
PICKS = ['one:gemini-test:low', 'two:gemini-test:low', 'three:glm-test:low', 'four:glm-test:low']


class Server:
    def __init__(self, fail_claim=None):
        self.active = []
        self.claims = []
        self.reads = []
        self.fail_claim = fail_claim

    def request(self, req):
        if req.url.path == '/api/v1/run-plans/capabilities':
            return httpx.Response(200, json={'schema_version': 1, 'capabilities': ['runner-reservation-v1'],
                                            'stop_generation_cas': True, 'close_releases_capacity': False})
        if req.url.path.endswith('/claim'):
            data = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.claims.append(data)
            if len(self.claims) == self.fail_claim:
                return httpx.Response(503, json={'detail': 'injected failure'})
            batch = BATCHES[0 if data['model'] == 'gemini-test' else 1]
            a = dict(data, assignment_id=str(len(self.claims)), batch_id=batch)
            self.active.append(a)
            return httpx.Response(200, json={'assignment': a})
        if req.url.path == '/api/v1/assignment':
            batch = req.url.params.get('batch_id')
            self.reads.append(batch)
            # The production default is a single batch, never a union.
            selected = batch or (self.active[-1]['batch_id'] if self.active else None)
            rows = [a for a in self.active if a['batch_id'] == selected]
            if batch and not rows:
                return httpx.Response(404, json={'detail': {'code': 'claim_batch_not_found', 'message': 'active claim batch not found'}})
            return httpx.Response(200, json={'active': rows, 'free_pick': True})
        raise AssertionError(req.url)


def setup(monkeypatch, tmp_path, server):
    prepare = runloop._prepare_batch
    _patch_pool_setup(monkeypatch)
    monkeypatch.setattr(runloop, '_prepare_batch', prepare)
    monkeypatch.setattr(runloop, 'HOME', tmp_path)
    monkeypatch.setattr(runloop, '_POOL_SUPERVISOR_POLL_SECONDS', 0)
    monkeypatch.setattr(runloop, '_POOL_BACKFILL_REFRESH_SECONDS', 0)
    monkeypatch.setattr(runloop, '_preflight_scoped_provider', lambda *_: None)
    monkeypatch.setattr(runloop, '_allow_claim_after_empty_submission', lambda *_a, **_k: True)
    monkeypatch.setattr(runloop, '_pending_assignment_ids_for_client', lambda *_a, **_k: set())
    client = ApiClient('https://fixture.invalid', 'fixture', transport=httpx.MockTransport(server.request), capabilities=())
    monkeypatch.setattr(runloop, '_client', lambda *_a, **_k: client)
    return client


@pytest.mark.parametrize('workers', [1, 2, 4])
def test_go_launches_every_returned_batch_with_global_worker_limit(monkeypatch, tmp_path, workers, capsys):
    server = Server()
    setup(monkeypatch, tmp_path, server)
    live, spawned, completed = [], [], []

    class Worker:
        def __init__(self, command, env, **kw):
            assert '--pick' not in command
            assert '--auto' not in command
            assert '--keep' in command
            self.batch = command[command.index('--batch-id') + 1]
            self.returncode = None
            self.pid = 100 + len(spawned)
            self.env = env
            self.polls = 0
            spawned.append(self.batch)
            live.append(self)
            assert len(live) <= workers

        def poll(self):
            if self.returncode is not None:
                return self.returncode
            self.polls += 1
            if self.polls < 2:
                return None
            rows = [a for a in server.active if a['batch_id'] == self.batch]
            if rows:
                a = rows[0]
                server.active.remove(a)
                completed.append(a['assignment_id'])
                Path(self.env[runloop._POOL_WORKER_ACTIVITY_ENV]).write_text(a['assignment_id'])
            self.returncode = 0
            live.remove(self)
            return 0

    monkeypatch.setattr(runloop.subprocess, 'Popen', Worker)
    # Real argparse and cmd_go; test fixtures replace runtime preparation only.
    argv = ['go', '-y', '--workers', str(workers), '--keep']
    for pick in PICKS:
        argv += ['--pick', pick]
    assert cli.main(argv) == 0
    assert len(server.claims) == 4
    assert set(spawned) == set(BATCHES)
    assert sorted(completed) == ['1', '2', '3', '4']
    assert not server.active
    if workers >= 2:
        assert spawned[:2] == BATCHES
    else:
        assert 'queue for a free slot' in capsys.readouterr().out


def test_partial_claim_failure_reports_receipts_and_never_starts(monkeypatch, tmp_path, capsys):
    server = Server(fail_claim=3)
    setup(monkeypatch, tmp_path, server)
    monkeypatch.setattr(runloop.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('must not start after failed selection'))
    with pytest.raises(SystemExit):
        runloop.cmd_go(_args(workers=4, auto=None, pick=PICKS))
    assert len(server.claims) == 3
    assert len(server.active) == 2
    assert BATCHES[0] in capsys.readouterr().out


def test_invalid_later_pick_does_not_partially_claim(monkeypatch):
    class Client:
        def claim_assignment(self, *_):
            pytest.fail('parse all choices first')
    with pytest.raises(SystemExit):
        runloop._claim_picks(Client(), [PICKS[0], 'bad'])


def test_union_reads_exact_batches_and_propagates_nonterminal_errors(monkeypatch):
    server = Server()
    client = ApiClient('https://fixture.invalid', 'fixture', transport=httpx.MockTransport(server.request), capabilities=())
    union = runloop._BatchInventory(client, BATCHES)
    assert union.get_assignment()['active'] == []
    assert server.reads == BATCHES
    assert client.batch_id is None
    def fail():
        raise ApiError('unavailable', status_code=503)
    monkeypatch.setattr(union.clients[BATCHES[1]], 'get_assignment', fail)
    with pytest.raises(ApiError):
        union.get_assignment()


def test_serial_mixed_selection_still_requires_execution_confirmation(monkeypatch, tmp_path):
    server = Server()
    client = setup(monkeypatch, tmp_path, server)
    monkeypatch.setattr('builtins.input', lambda _: 'n')
    monkeypatch.setattr(runloop.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('user declined execution'))
    args = _args(workers=1, yes=False, auto=None, pick=PICKS)
    assert runloop._go_menu(args, {}, client, tmp_path) == 1
    assert len(server.active) == 4


# Regression cases first identified by independent QA.
def test_waiting_work_reuses_slot_while_sibling_is_running(monkeypatch, tmp_path):
    server = Server()
    setup(monkeypatch, tmp_path, server)
    monkeypatch.setattr(runloop, "_ensure_egress_runtime", lambda **kw: None)
    monkeypatch.setattr(runloop.image_cache, "preflight_trial_builder", lambda *a: runloop.image_cache.TrialBuilderPreflight(True, 0, "fixture", None, "", ()))
    spawned = []
    first_finished = False
    replacement_before_finish = []
    class Worker:
        def __init__(self, command, env, **kw):
            self.batch = command[command.index('--batch-id') + 1]
            self.pid = 100 + len(spawned)
            self.returncode = None
            self.polls = 0
            self.row = next(a for a in server.active if a['batch_id'] == self.batch and not a.get('started_at'))
            self.row.update(started_at='2026-09-15T00:00:00Z', execution_state='running', heartbeat_running=True)
            Path(env[runloop._POOL_WORKER_ACTIVITY_ENV]).write_text(self.row['assignment_id'])
            if self.batch == BATCHES[0] and spawned:
                replacement_before_finish.append(not first_finished)
            spawned.append(self)
        def poll(self):
            nonlocal first_finished
            self.polls += 1
            if self is spawned[0] and self.polls < 8:
                return None
            if self.returncode is None:
                server.active.remove(self.row)
                self.returncode = 0
                if self is spawned[0]:
                    first_finished = True
            return self.returncode
    monkeypatch.setattr(runloop.subprocess, 'Popen', Worker)
    assert runloop.cmd_go(_args(workers=2, auto=None, pick=PICKS[:3])) == 0
    assert replacement_before_finish == [True], 'vacant slot stayed idle until first batch worker completed'


def test_exact_batch_recovery_after_mixed_spawn_failure(monkeypatch, tmp_path):
    import pytest
    prepare_boundary = runloop._prepare_assignment_boundary
    finish_boundary = runloop._finish_assignment_boundary
    server = Server()
    setup(monkeypatch, tmp_path, server)
    monkeypatch.setattr(runloop, "_prepare_assignment_boundary", prepare_boundary)
    monkeypatch.setattr(runloop, "_finish_assignment_boundary", finish_boundary)
    monkeypatch.setattr(runloop, '_ensure_egress_runtime', lambda **kw: None)
    monkeypatch.setattr(runloop.image_cache, 'preflight_trial_builder', lambda *a: runloop.image_cache.TrialBuilderPreflight(True, 0, 'fixture', None, '', ()))
    def fail_spawn(*a, **kw):
        raise OSError('injected process creation failure')
    monkeypatch.setattr(runloop.subprocess, 'Popen', fail_spawn)
    assert runloop.cmd_go(_args(workers=2, auto=None, pick=PICKS)) == 1
    assert len(server.claims) == 4
    assert len(server.active) == 4
    # This is the exact recovery command printed by the product. A start
    # failure is allowed; a false missing-lease assertion before spawn is not.
    try:
        runloop.cmd_go(_args(workers=2, auto=None, pick=None, resume=True, batch_id=BATCHES[0]))
    except SystemExit as exc:
        pytest.fail(f'advertised exact-batch recovery blocked: {exc}')
    assert len(server.claims) == 4


def test_mixed_interrupt_signals_children_without_reclaim(monkeypatch, tmp_path):
    import pytest
    server = Server()
    setup(monkeypatch, tmp_path, server)
    monkeypatch.setattr(runloop, '_ensure_egress_runtime', lambda **kw: None)
    monkeypatch.setattr(runloop.image_cache, 'preflight_trial_builder', lambda *a: runloop.image_cache.TrialBuilderPreflight(True, 0, 'fixture', None, '', ()))
    children, signalled = [], []
    class Worker:
        def __init__(self, command, env, **kw):
            self.pid = 100 + len(children)
            children.append(self)
        def poll(self):
            raise KeyboardInterrupt()
    monkeypatch.setattr(runloop.subprocess, 'Popen', Worker)
    monkeypatch.setattr(runloop, '_signal_workers', lambda children: signalled.extend(children))
    with pytest.raises(KeyboardInterrupt):
        runloop.cmd_go(_args(workers=2, auto=None, pick=PICKS))
    assert len(children) == 2
    assert signalled == children
    assert len(server.claims) == len(server.active) == 4


def test_saved_mixed_boundary_rejects_execution_of_unadmitted_batch(monkeypatch, tmp_path):
    import pytest
    from dradar import assignment_boundary
    from dradar.api_client import ApiClient
    import httpx
    server = Server()
    server.active = [
        {'assignment_id': '1', 'task_id': 'one', 'batch_id': BATCHES[0]},
        {'assignment_id': '2', 'task_id': 'two', 'batch_id': BATCHES[1]},
        {'assignment_id': '3', 'task_id': 'other', 'batch_id': 'c' * 32},
    ]
    monkeypatch.setattr(runloop, 'HOME', tmp_path)
    assignment_boundary.prepare(tmp_path, 'bench', server.active[:2])
    client = ApiClient('https://fixture.invalid', 'fixture', transport=httpx.MockTransport(server.request), capabilities=(), batch_id='c' * 32)
    with pytest.raises(SystemExit):
        runloop._prepare_assignment_boundary(_args(batch_id='c' * 32), client, 'bench', server.active[2:])
    assert client.batch_id == 'c' * 32


def test_non_yes_mixed_decline_keeps_claims_and_prompts_once(monkeypatch, tmp_path):
    import pytest
    server = Server()
    client = setup(monkeypatch, tmp_path, server)
    prompts = []
    def decline(prompt):
        prompts.append(prompt)
        return ''
    monkeypatch.setattr('builtins.input', decline)
    monkeypatch.setattr(runloop.subprocess, 'Popen', lambda *a, **kw: pytest.fail('declined worker execution'))
    assert runloop._go_menu(_args(workers=1, yes=False, auto=None, pick=PICKS), {}, client, tmp_path) == 1
    assert sum('held tasks across their exact batches' in p for p in prompts) == 1
    assert any('4 held tasks' in prompt for prompt in prompts)
    assert len(server.claims) == len(server.active) == 4
