"""One synchronous task, durable requests, one launch fence, exact upload replay."""
from contextlib import contextmanager
import json
import threading
from ..assignment_lock import lock
from ..v2.locks import exclusive
from ..v2.client import ProtocolError
from ..v2.protocol import envelope, assignment as validate_assignment, result_receipt
from ..v2.results import save_completion, recover_completion, upload_files
from . import PREFIX, PROTOCOL

class JobBlocked(RuntimeError):
    pass

def owner(a):
    return {k: a[k] for k in ('lease_id', 'owner_epoch')}

def stop(client, reason='user_stop'):
    j = client.journal
    run = j.value('run')
    if not run or j.value('protocol') != PROTOCOL:
        raise JobBlocked('Original single-task STATE required')
    with exclusive(j.root / 'locks' / 'launch.lock'):
        j.bind('local_stop', 'true')
        if reason != 'no_work':
            j.bind('local_interrupt', 'true')
    saved = next((r for r in j.requests() if r.operation == 'run:stop'), None)
    request = saved or j.prepare('run:stop', f'{PREFIX}/runs/{run}/stop', {'reason': reason})
    reply = envelope(client.send(request), request.request_id)
    if reply.get('status') != 'stopped':
        raise ProtocolError('stop receipt missing')
    return reply

class Job:
    def __init__(self, client, runtime=None):
        self.client, self.journal, self.runtime = client, client.journal, runtime
        self.journal.bind('protocol', PROTOCOL)
        self.run_id = self.journal.identity('run')
        self._guard = threading.RLock()
        self.phase = 'preparing'

    @contextmanager
    def ownership(self):
        with lock(self.journal.root / 'locks', 'single-job'):
            yield self

    def initialize(self, configuration):
        if set(configuration) != {'benchmark', 'model', 'effort', 'agent'} or configuration['agent'] != 'codex':
            raise JobBlocked('Exact approved single-task harness selection required')
        raw = json.dumps(configuration, sort_keys=True, separators=(',', ':'))
        self.journal.bind('configuration', raw)
        request = self.journal.prepare('run:create', PREFIX + '/runs',
                                      {'run_id': self.run_id, **configuration})
        reply = envelope(self.client.send(request), request.request_id)
        run = reply.get('run', {})
        if reply.get('status') != 'accepted' or run.get('run_id') != self.run_id or any(run.get(k) != v for k, v in configuration.items()):
            raise ProtocolError('create receipt selection/identity mismatch')
        return reply

    def checked(self, value):
        if not isinstance(value, dict) or 'device_id' in value or 'slot_id' in value:
            raise ProtocolError('single-task assignment must not contain a machine or slot')
        checked = validate_assignment(value, run_id=self.run_id)
        cfg = json.loads(self.journal.value('configuration') or '{}')
        if any(checked['task'].get(k) != cfg[k] for k in ('model', 'effort')) or checked['runner'].get('agent') != cfg.get('agent'):
            raise ProtocolError('assignment harness selection differs from approved selection')
        if checked['task']['benchmark'] != cfg.get('benchmark'):
            from ..v2.mixed_pool import POOL, MEMBER_HASHES
            task = checked['task']
            if cfg.get('benchmark') != POOL or MEMBER_HASHES.get((task['benchmark'], task['task_id'])) != task['task_content_hash']:
                raise ProtocolError('assignment outside requested library')
        return value

    def snapshot(self):
        value = envelope(self.client.get(f'{PREFIX}/runs/{self.run_id}'))
        run = value.get('run', {})
        if run.get('run_id') != self.run_id or any(k in run for k in ('device_id', 'concurrency', 'total_count')):
            raise ProtocolError('invalid single-task run scope')
        cfg = json.loads(self.journal.value('configuration') or '{}')
        if any(run.get(k) != v for k, v in cfg.items()):
            raise ProtocolError('server run differs from original selection')
        if not isinstance(value.get('assignments'), list) or len(value['assignments']) > 1:
            raise ProtocolError('one assignment per run required')
        value['assignments'] = [self.checked(a) for a in value['assignments']]
        return value

    def claim(self):
        # Always the same request, including busy/restart/lost ACK. No refill.
        # Retry replay can now allocate. Serialize it with the durable local
        # stop barrier, including pending requests whose first ACK was lost.
        with self.launch_guard():
            request = self.journal.prepare('claim:single', f'{PREFIX}/runs/{self.run_id}/claim', {})
            value = envelope(self.client.send(request), request.request_id)
        if value.get('status') == 'claimed':
            self.checked(value.get('assignment'))
        elif value.get('status') not in ('retry', 'no_work', 'stop'):
            raise ProtocolError('unknown recommendation decision')
        return value

    @contextmanager
    def launch_guard(self):
        with self._guard, exclusive(self.journal.root / 'locks' / 'launch.lock'):
            if self.journal.value('local_stop') == 'true':
                raise JobBlocked('This task was stopped; original evidence retained')
            yield

    @contextmanager
    def heartbeat(self, a):
        done = threading.Event()
        interval = (self.client._bootstrap or {}).get('heartbeat_seconds', 30)
        if type(interval) is not int or interval < 1:
            raise ProtocolError('positive heartbeat interval required')
        errors = []
        def pulse():
            while not done.wait(interval):
                try:
                    with self._guard:
                        saved = self.journal.execution(a['assignment_id'])
                        prefix = 'heartbeat:' + a['assignment_id'] + ':'
                        request = self.journal.pending_request(prefix)
                        if request is None:
                            request = self.journal.prepare(prefix + str(self.journal.next_sequence(prefix)),
                                f"{PREFIX}/assignments/{a['assignment_id']}/heartbeat",
                                {**owner(a), 'execution_id': saved['execution_id'] if saved else a.get('execution_id'),
                                 'phase': self.phase, 'progress': {'elapsed_ms': None, 'tokens': {
                                     'input': None, 'output': None, 'total': None, 'source': None,
                                     'missing_reason': 'live_usage_unavailable'}}})
                        reply = self.client.send(request)
                        if reply.get('status') != 'ok':
                            raise ProtocolError('heartbeat receipt missing')
                        if reply.get('stop_requested'):
                            stop(self.client)
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    self.journal.bind('local_interrupt', 'true')
                    self.journal.bind('local_stop', 'true')
                    return
        thread = threading.Thread(target=pulse, name='dradar-single-heartbeat', daemon=True)
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join()  # bounded by the transport's 30s timeout
        if errors:
            raise JobBlocked('Heartbeat failed; retained state/result may be replayed, never reexecuted')

    def upload(self, a):
        saved = self.journal.execution(a['assignment_id'])
        if not saved:
            raise JobBlocked('No original execution/result; upload-only cannot claim or start')
        internal = a
        if saved['result_json'] is None:
            payload = recover_completion(self.journal.root / 'artifacts', internal, saved['execution_id'])
            if payload is None:
                raise JobBlocked('Execution exit/result unknown; inspect original runtime, never relaunch')
            self.journal.save_result(a['assignment_id'], saved['execution_id'], payload)
            saved = self.journal.execution(a['assignment_id'])
        payload = json.loads(saved['result_json'])
        files = upload_files(self.journal.root / 'artifacts', a['assignment_id'], payload)
        limit = (self.client._bootstrap or self.client.bootstrap())['limits']['max_result_bytes']
        if sum(f['size_bytes'] for f in payload['artifacts']) > limit:
            raise JobBlocked('Result exceeds Server limit; evidence retained')
        wire = payload
        request = self.journal.prepare('result:' + a['assignment_id'],
            f"{PREFIX}/assignments/{a['assignment_id']}/result", wire)
        receipt=result_receipt(self.client.send_result(request, files), request.request_id, a['assignment_id'], wire)
        return {**receipt,'outcome':payload['outcome']}

    def upload_only(self):
        snap = self.snapshot()
        if not snap['assignments']:
            raise JobBlocked('No assigned task to recover')
        return self.upload(snap['assignments'][0])

    def run(self):
        # Reconcile pending claim even if inventory already shows its commit.
        if self.journal.value('local_stop') != 'true' and self.journal.pending_request('claim:single'):
            self.claim()
        snap = self.snapshot()
        if not snap['assignments']:
            if self.journal.value('local_stop') == 'true' or snap['run']['state'] != 'active':
                raise JobBlocked('Stopped task cannot claim')
            decision = self.claim()
            if decision['status'] != 'claimed':
                return decision
            snap = self.snapshot()
        a = snap['assignments'][0]
        aid = a['assignment_id']
        if self.journal.execution(aid):
            return self.upload(a)
        if self.journal.value('local_stop') == 'true':
            raise JobBlocked('Stopped task cannot prepare or start; original evidence retained')
        if a['state'] == 'running':
            raise JobBlocked('Server start committed without local launch fence; execution unknown, no automatic relaunch')
        if a['state'] not in ('leased', 'running'):
            return {'status': 'ended', 'assignment_id': aid, 'state': a['state']}
        if self.runtime is None:
            raise JobBlocked('Existing local harness runtime required')
        eid = self.journal.identity('execution:' + aid)
        if a.get('execution_id') not in (None, eid):
            raise JobBlocked('Another execution owns this task')
        pending_release = next((r for r in self.journal.requests() if r.operation == 'release:' + aid), None)
        if pending_release:
            return self.client.send(pending_release)
        def authorize():
            with self.launch_guard():
                request = self.journal.prepare('start:' + aid, f'{PREFIX}/assignments/{aid}/start',
                                              {**owner(a), 'execution_id': eid})
                if request.response is not None:
                    raise JobBlocked('Prior start ACK exists without launch fence; never relaunch from a copied or interrupted STATE')
                reply = envelope(self.client.send(request), request.request_id)
                if reply.get('replayed') is True:
                    raise JobBlocked('Start already authorized elsewhere or ACK lost; retain original task, no automatic launch')
                accepted = self.checked(reply.get('assignment'))
                if (reply.get('status') != 'started' or accepted['assignment_id'] != aid
                        or owner(accepted) != owner(a) or accepted.get('execution_id') != eid
                        or accepted['state'] != 'running'):
                    raise ProtocolError('start receipt does not authorize this exact task')
                if not self.journal.begin_execution(aid, eid):
                    raise JobBlocked('Original execution already fenced; never relaunch')
                self.phase = 'running'
        with self.heartbeat(a):
            try:
                prepared = self.runtime.prepare(a)
            except Exception:
                if a['state'] == 'leased' and not any(r.operation == 'start:' + aid for r in self.journal.requests()):
                    request = self.journal.prepare('release:' + aid, f'{PREFIX}/assignments/{aid}/release',
                                                  {**owner(a), 'reason': 'preflight_failed'})
                    self.client.send(request)
                raise
            if hasattr(self.runtime, 'execute_with_barrier'):
                completion = self.runtime.execute_with_barrier(prepared, eid, authorize, self.launch_guard)
            else:
                authorize()
                with self.launch_guard():
                    pass
                completion = self.runtime.execute(prepared, eid)
            self.phase = 'saving'
            payload = save_completion(self.journal.root / 'artifacts', a, eid, completion)
            self.journal.save_result(aid, eid, payload)
        self.phase = 'uploading'
        return self.upload(a)
