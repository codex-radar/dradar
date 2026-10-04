"""A small slot controller. Runtime is injected; no legacy runloop import."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, Future
from contextlib import contextmanager
import json
import copy
import math
from pathlib import Path
import threading
import time
from typing import Protocol
from ..assignment_lock import lock
from .client import Client, ProtocolError, TransportUnknown
from .journal import Journal, JournalConflict
from .protocol import envelope, assignment, owner, result_receipt, result_hash
from .results import Completion, save_completion, upload_files, recover_completion
from .runtime import TaskNotReady
from .locks import exclusive

class Runtime(Protocol):
    def prepare(self, assignment: dict) -> object:
        """Validate and prepare an isolated environment; no paid execution."""
    def execute(self, prepared: object, execution_id: str) -> Completion:
        """Invoke exactly once and confirm physical exit before returning."""

class ExecutionBlocked(RuntimeError):
    pass

class AssignmentMismatch(ProtocolError):
    pass

class UnstartedLeaseEnded(RuntimeError):
    def __init__(self, current):
        super().__init__('authoritative never-started lease ended')
        self.assignment = current

class PreflightFailed(RuntimeError):
    pass

def stop_saved_run(client: Client, reason="user_stop") -> dict:
    """A second CLI can signal the exact controller without starting another."""
    if reason not in {"user_stop", "no_work", "local_error"}:
        raise ValueError("invalid stop reason")
    journal = client.journal
    run_id, device_id = journal.value("run"), journal.value("device")
    if not run_id or not device_id:
        raise ExecutionBlocked("saved run required")
    with exclusive(journal.root / "locks" / "launch.lock"):
        journal.bind("local_stop", "true")
        if reason != "no_work":
            journal.bind("local_interrupt", "true")
    req = next((r for r in journal.requests() if r.operation == "run:stop"), None)
    if req is None:
        req = journal.prepare("run:stop", f"/api/v2/runs/{run_id}/stop", {"device_id": device_id, "reason": reason})
    reply = envelope(client.send(req), req.request_id)
    if reply.get("status") != "stopped":
        raise ProtocolError("run stop not acknowledged")
    return reply

class Controller:
    def __init__(self, client: Client, runtime: Runtime, configuration: dict, *, installation: Journal | None = None):
        self.client, self.runtime, self.journal = client, runtime, client.journal
        if set(configuration) != {"benchmark", "model", "effort", "agent", "total_count", "concurrency"} or configuration["agent"] != "codex":
            raise ValueError("exact v0 Codex run configuration required")
        for key in ("total_count", "concurrency"):
            if type(configuration[key]) is not int or configuration[key] < 1:
                raise ValueError("positive integer run limits required")
        self.configuration = dict(configuration)
        from .mixed_pool import is_historical_pool
        self.historical_mixed_scope = is_historical_pool(configuration['benchmark'],json.loads(self.journal.value('mixed_pool_scope') or 'null'))
        self.journal.bind("configuration", json.dumps(configuration, sort_keys=True, separators=(",", ":")))
        installation = installation or Journal(self.journal.root.parent / "installation")
        current_device = installation.identity("device")
        saved_device = self.journal.value("device")
        if saved_device is None:
            self.journal.bind("device", current_device)
        self.device_id = self.journal.value("device")
        self.launch_allowed = self.device_id == current_device and not self.historical_mixed_scope
        self.run_id = self.journal.identity("run")
        self.pool = ThreadPoolExecutor(max_workers=configuration["concurrency"], thread_name_prefix="dradar-v2")
        self.futures: dict[str, Future] = {}
        self.blocked: dict[str, str] = {}
        self.retry_at: dict[str, float] = {}
        self.phases: dict[str, str] = {}
        self._phase_lock = threading.Lock()
        self._heartbeat_at: dict[str, float] = {}
        self._observed_start: dict[str, float] = {}
        self.accepting = True
        self.shortfall_reason = None
        self._locked = False
        self._launch_lock = threading.RLock()
        self._stop_event = threading.Event()

    @contextmanager
    def ownership(self):
        with lock(self.journal.root / "locks", "controller"):
            self._locked = True
            try:
                yield self
            finally:
                # Retain the controller lock while already launched tasks drain.
                self.pool.shutdown(wait=True)
                self._locked = False

    def _require_lock(self):
        if not self._locked:
            raise ExecutionBlocked("one controller lock per saved run is required")

    def _task_candidates(self):
        if not hasattr(self.runtime,'claim_task_candidates'):return None
        candidates=self.runtime.claim_task_candidates()
        if candidates is None:return None
        from .mixed_pool import MEMBERS, MEMBER_HASHES
        by={}
        for t in candidates:
            if not isinstance(t,dict) or set(t)!={'benchmark','task_id','task_content_hash'}:
                raise ExecutionBlocked('exact runtime task candidate identity required')
            key=(t['benchmark'],t['task_id'])
            if key in by or MEMBER_HASHES.get(key)!=t['task_content_hash']:
                raise ExecutionBlocked('runtime task candidate outside fixed64 scope')
            if self.journal.value('runtime_unavailable:'+key[0]+':'+key[1]) is None:by[key]=t
        return [by[(m['source_benchmark'],m['task_id'])] for m in MEMBERS if (m['source_benchmark'],m['task_id']) in by]

    def _claim_body(self,slot):
        body={'device_id':self.device_id,'slot_id':slot}
        candidates=self._task_candidates()
        if candidates is not None:
            from .mixed_pool import CATALOG_VERSION,MEMBERS_SHA256
            body['runtime_task_scope']={'catalog_version':CATALOG_VERSION,'members_sha256':MEMBERS_SHA256,'tasks':candidates}
        return body

    def runtime_unavailable_tasks(self):
        from .mixed_pool import MEMBERS
        rows=[]
        for m in MEMBERS:
            value=self.journal.value('runtime_unavailable:'+m['source_benchmark']+':'+m['task_id'])
            if value:rows.append(json.loads(value))
        return rows

    def initialize(self) -> dict:
        from ..harness_policy import reject_retired_combination
        reject_retired_combination(self.configuration['agent'],self.configuration['model'])
        self._require_lock()
        if self.historical_mixed_scope:
            raise ExecutionBlocked('historical68 permits only stop/progress/upload-only; no new claim/start/execute')
        if not self.launch_allowed:
            raise ExecutionBlocked("copied journal permits upload-only until original controller exit is confirmed")
        boot = self.client.bootstrap()
        candidates=self._task_candidates()
        if candidates is not None:
            from .host_contract import PER_TASK_CAPABILITY,PER_TASK_POLICY
            if PER_TASK_CAPABILITY not in boot.get('capabilities',[]) or boot.get('runtime_readiness_policy')!=PER_TASK_POLICY:
                raise ExecutionBlocked('Server upgrade required: per-assignment runtime_task_scope claim filter; no unfiltered fallback')
            if not candidates:raise ExecutionBlocked('no bound task candidates; preserve task-specific readiness evidence')
        limits = boot.get("limits", {})
        for field, key in (("total_count", "max_total_count"), ("concurrency", "max_concurrency")):
            cap = limits.get(key)
            if cap is not None and (type(cap) is not int or self.configuration[field] > cap):
                raise ValueError("requested settings exceed explicit candidate limits")
        choices = [(b["benchmark"], m["model"], m["effort"]) for b in boot.get("benchmarks", []) for m in b.get("models", [])]
        if (self.configuration["benchmark"], self.configuration["model"], self.configuration["effort"]) not in choices:
            raise ValueError("requested benchmark/model/effort is unavailable")
        from .mixed_pool import POOL, selection_scope
        if self.configuration['benchmark'] == POOL:
            scope = selection_scope(boot,self.configuration['model'],self.configuration['effort'])
            self.journal.bind('mixed_pool_scope',json.dumps(scope,sort_keys=True,separators=(',',':')))
        req = self.journal.prepare("run:create", "/api/v2/runs", {"run_id": self.run_id, "device_id": self.device_id, **self.configuration})
        response = envelope(self.client.send(req), req.request_id)
        if response.get("status") != "accepted":
            raise ProtocolError("run creation not accepted")
        return self.snapshot()

    def _assignment(self, value, *, slot_id=None, assignment_id=None):
        """Pin exact selection and immutable owner/task/runner scope before launch."""
        try:
            a = assignment(value, run_id=self.run_id, device_id=self.device_id, slot_id=slot_id)
            if assignment_id is not None and a['assignment_id'] != assignment_id:
                raise ProtocolError('assignment identity changed')
            if a['slot_id'] >= self.configuration['concurrency']:
                raise ProtocolError('assignment outside local slot range')
            if any(a['task'][k] != self.configuration[k] for k in ('model','effort')):
                raise ProtocolError('assignment changed requested model/effort')
            from .mixed_pool import POOL, validate_assignment
            if self.configuration['benchmark'] == POOL or self.historical_mixed_scope:
                scope = json.loads(self.journal.value('mixed_pool_scope') or 'null')
                validate_assignment(scope,a['task'],allow_historical=self.historical_mixed_scope)
            elif a['task']['benchmark'] != self.configuration['benchmark']:
                raise ProtocolError('assignment changed requested source benchmark')
            task_id = a['task']['task_id']
            if Path(task_id).name != task_id or task_id in {'.','..'} or '\\' in task_id:
                raise ProtocolError('unsupported task selection')
            runner = a.get('runner')
            required = {'agent','agent_version','agent_version_verified','auth_runtime','provider','billing_mode','est_minutes'}
            if (not isinstance(runner,dict) or not required <= runner.keys()
                    or runner['agent'] != self.configuration['agent']
                    or not isinstance(runner['agent_version'],str) or not runner['agent_version']
                    or type(runner['agent_version_verified']) is not bool
                    or any(runner[k] is not None and (not isinstance(runner[k],str) or not runner[k]) for k in ('auth_runtime','provider','billing_mode'))):
                raise ProtocolError('invalid requested runner descriptor')
            minutes = runner['est_minutes']
            if minutes is not None and (type(minutes) not in (int,float) or not math.isfinite(minutes) or minutes < 0):
                raise ProtocolError('invalid runner estimate')
            # A versioned per-task Server must honor the exact durable claim mask.
            for req in self.journal.requests():
                original=(req.response or {}).get('assignment')
                if req.operation.startswith('claim:') and original and original.get('assignment_id')==a['assignment_id']:
                    candidate_scope=req.body.get('runtime_task_scope')
                    identity={k:a['task'][k] for k in ('benchmark','task_id','task_content_hash')}
                    if candidate_scope is not None and identity not in candidate_scope['tasks']:
                        raise ProtocolError('Server assignment outside original runtime task candidate filter')
            keys = ('assignment_id','run_id','device_id','slot_id','work_key','lease_id','owner_epoch','task','runner')
            scope = {k:a[k] for k in keys}
            key = 'assignment_scope:' + a['assignment_id']
            if self.journal.value(key) is None:
                # Recover the first claim pin from older saved journals too.
                for req in self.journal.requests():
                    original = (req.response or {}).get('assignment')
                    if req.operation.startswith('claim:') and original and original.get('assignment_id') == a['assignment_id']:
                        self.journal.bind(key,json.dumps({k:original[k] for k in keys},sort_keys=True,separators=(',',':')))
                        break
            self.journal.bind(key,json.dumps(scope,sort_keys=True,separators=(',',':')))
            return a
        except (ProtocolError,JournalConflict,KeyError,TypeError,ValueError) as exc:
            self._stop_event.set()
            self.accepting = False
            self.journal.bind('local_stop','true')
            self.journal.bind('local_interrupt','true')
            raise AssignmentMismatch('assignment does not match exact requested and saved scope') from exc

    def _unstarted_terminal(self, a):
        aid = a['assignment_id']
        return (a['state'] in {'expired','released'} and 'execution_id' in a and a['execution_id'] is None
                and a.get('started_at') is None and self.journal.execution(aid) is None
                and not any(r.operation == 'start:'+aid for r in self.journal.requests())
                and not self.journal.audits(aid))

    def _reconcile_releases(self, now):
        for req in self.journal.requests():
            if not req.operation.startswith('release:') or self.journal.value('release_reconciled:'+req.operation.split(':',1)[1]) == 'true':
                continue
            aid = req.operation.split(':',1)[1]
            if now < self.retry_at.get(aid,0):
                continue
            try:
                reply = envelope(self.client.send(req),req.request_id)
                a = self._assignment(reply.get('assignment'))
                if (reply.get('status') != 'released' or a['assignment_id'] != aid
                        or owner(a) != {k:req.body[k] for k in ('device_id','lease_id','owner_epoch')}
                        or not self._unstarted_terminal(a)):
                    raise ProtocolError('release ACK does not confirm original never-started owner')
                current = self._assignment(envelope(self.client.get(f'/api/v2/assignments/{aid}'))['assignment'])
                if not self._unstarted_terminal(current):
                    raise ProtocolError('authoritative release state not terminal and never-started')
                self.journal.bind('release_reconciled:'+aid,'true')
                self.blocked.pop(aid,None)
                if req.body['reason'] == 'preflight_failed' and self.journal.value('task_unavailable:'+aid) is None:
                    self.blocked[aid] = 'preflight_failed'
                    self.shortfall_reason = 'local_error'
                    self.stop('local_error')
            except TransportUnknown as exc:
                self.retry_at[aid] = now + exc.retry_after_seconds

    def snapshot(self) -> dict:
        value = envelope(self.client.get(f"/api/v2/runs/{self.run_id}"))
        run = value.get("run")
        if not isinstance(run, dict) or run.get("run_id") != self.run_id or run.get("device_id") != self.device_id:
            raise ProtocolError("run owner identity mismatch")
        counts = run.get("counts")
        if not isinstance(counts, dict) or any(type(counts.get(k)) is not int or counts[k] < 0 for k in ("started", "leased", "running", "uncertain", "submitted")):
            raise ProtocolError("authoritative integer run counters required")
        if (type(run.get("remaining_to_start")) is not int
                or run["remaining_to_start"] != max(0, self.configuration["total_count"] - counts["started"])
                or counts["started"] + counts["leased"] > self.configuration["total_count"]):
            raise ProtocolError("run budget counters inconsistent")
        if any(run.get(key) != value for key, value in self.configuration.items()):
            raise ProtocolError("server changed exact requested configuration")
        if run.get("state") not in {"active", "stopped"} or not isinstance(value.get("assignments"), list):
            raise ProtocolError("run state or assignment inventory missing")
        for item in value["assignments"]:
            self._assignment(item)
            if item["slot_id"] >= self.configuration["concurrency"]:
                raise ProtocolError("server assignment is outside local slot range")
        if run["state"] == "stopped":
            self._stop_event.set()
            if run.get("stop_reason") != "no_work":
                self.journal.bind("local_interrupt", "true")
        if run["state"] == "stopped" or run.get("remaining_to_start") == 0:
            self.accepting = False
        return value

    def progress_snapshot(self) -> dict:
        """Merge durable local observations for display only; never wire updates."""
        snap = copy.deepcopy(self.snapshot())
        for a in snap["assignments"]:
            aid = a["assignment_id"]
            saved = self.journal.execution(aid)
            if saved and saved["result_json"]:
                result = json.loads(saved["result_json"])
                if a.get('completion_correction'):
                    self._accepted_correction(a, result)
                    a['original_reported_outcome'] = result['outcome']
                else:
                    a["outcome"] = result["outcome"]
                a["progress"] = {"elapsed_ms": result["elapsed_ms"], "tokens": result["tokens"]}
            elif aid in self._observed_start:
                a["progress"] = {**(a.get("progress") or {}), "elapsed_ms": int((time.monotonic() - self._observed_start[aid]) * 1000)}
            if aid in self.phases:
                a["phase"] = self.phases[aid]
        snap['runtime_unavailable_tasks']=self.runtime_unavailable_tasks()
        return snap

    def _accepted_correction(self, a: dict, original: dict):
        """The authenticated accepted commitment supersedes display only."""
        proof = a['completion_correction']
        old = next((r for r in self.journal.requests() if r.operation == 'result:' + a['assignment_id']), None)
        corrected = {**original, 'outcome': 'completed', 'failure': None}
        expected = {'schema': 'codex-native-completion-correction/1',
                    'original_request_id': old.request_id if old else None,
                    'original_result_sha256': original['result_sha256'],
                    'corrected_result_sha256': result_hash(corrected),
                    'original_outcome': 'failed', 'accepted_outcome': 'completed'}
        if (original['outcome'] != 'failed' or a.get('outcome') != 'completed'
                or a.get('state') != 'submitted' or a.get('execution_id') != original['execution_id']
                or a.get('result_sha256') != expected['corrected_result_sha256']
                or not isinstance(proof, dict) or any(proof.get(k) != v for k, v in expected.items())):
            raise ProtocolError('accepted correction differs from original frozen result')
        return proof

    def _next(self, prefix: str, path: str, body: dict):
        pending = self.journal.pending_request(prefix)
        return pending if pending else self.journal.prepare(prefix + str(self.journal.next_sequence(prefix)), path, body)

    def stop(self, reason: str = "user_stop") -> dict:
        self._require_lock()
        if reason not in {"user_stop", "no_work", "local_error"}:
            raise ValueError("invalid run stop reason")
        with self._launch_lock:
            self._stop_event.set()
            self.accepting = False
        return stop_saved_run(self.client, reason)

    def _phase(self, aid: str, phase: str):
        with self._phase_lock:
            self.phases[aid] = phase

    @contextmanager
    def launch_guard(self):
        with self._launch_lock, exclusive(self.journal.root / "locks" / "launch.lock"):
            if self._stop_event.is_set() or self.journal.value("local_stop") == "true":
                raise ExecutionBlocked("local stop won before provider permission")
            yield

    def _work(self, initial: dict):
        if self.historical_mixed_scope:
            raise ExecutionBlocked('historical68 execution stopped; retain original recovery evidence')
        from ..harness_policy import reject_retired_combination
        reject_retired_combination(self.configuration['agent'], self.configuration['model'])
        initial = self._assignment(initial,slot_id=initial['slot_id'])
        aid = initial["assignment_id"]
        if self.journal.execution(aid) is not None:
            raise ExecutionBlocked("local launch fence exists; inspect original execution")
        if self.journal.pending_request(f"release:{aid}"):
            raise ExecutionBlocked("original release outcome must be reconciled")
        current = self._assignment(envelope(self.client.get(f"/api/v2/assignments/{aid}"))["assignment"], slot_id=initial["slot_id"], assignment_id=aid)
        if current["lease_id"] != initial["lease_id"] or current["owner_epoch"] != initial["owner_epoch"]:
            raise ExecutionBlocked("assignment ownership changed")
        if self._unstarted_terminal(current):
            raise UnstartedLeaseEnded(current)
        execution_id = self.journal.identity("execution:" + aid)
        if current["state"] not in {"leased", "running"} or (current["state"] == "running" and current.get("execution_id") != execution_id):
            raise ExecutionBlocked("assignment is not a fresh lease or original authorized execution")
        self._phase(aid, "preparing")
        self._observed_start[aid] = time.monotonic()
        try:
            prepared = self.runtime.prepare(current)
        except Exception as exc:
            # Task-only failures happen before controller/paid execution. Keep
            # their exact release unresolved until confirmed; shared faults freeze.
            task_local=(isinstance(exc,TaskNotReady) and current['state']=='leased'
                        and current.get('execution_id') is None and current.get('started_at') is None
                        and self.journal.execution(aid) is None
                        and not any(r.operation=='start:'+aid for r in self.journal.requests())
                        and not self.journal.audits(aid) and self._task_candidates() is not None)
            if task_local:
                task=current['task'];key='runtime_unavailable:'+task['benchmark']+':'+task['task_id']
                if self.journal.value(key) is None:
                    self.journal.bind(key,json.dumps({'benchmark':task['benchmark'],'task_id':task['task_id'],
                        'task_content_hash':task['task_content_hash'],'reason':exc.code,'status':'not_ready_before_start'}))
                self.journal.bind('task_unavailable:'+aid,exc.code)
            else:
                self._stop_event.set()
                self.accepting = False
                self.journal.bind('local_stop','true')
                self.journal.bind('local_interrupt','true')
            if current["state"] == "leased":
                req = self.journal.prepare(f"release:{aid}", f"/api/v2/assignments/{aid}/release", {**owner(current), "reason": "preflight_failed"})
                reply = envelope(self.client.send(req), req.request_id)
                if reply.get("status") != "released":
                    raise ProtocolError("preflight release not acknowledged")
            if task_local:raise exc
            raise PreflightFailed("environment preparation failed before paid execution") from exc
        def authorize():
            with self.launch_guard():
                req = self.journal.prepare(f"start:{aid}", f"/api/v2/assignments/{aid}/start", {**owner(current), "execution_id": execution_id})
                reply = envelope(self.client.send(req), req.request_id)
                accepted = self._assignment(reply.get("assignment"), slot_id=initial["slot_id"], assignment_id=aid)
                if (reply.get("status") != "started" or accepted["assignment_id"] != aid
                        or accepted["lease_id"] != current["lease_id"] or accepted["owner_epoch"] != current["owner_epoch"]
                        or accepted["execution_id"] != execution_id or accepted["state"] != "running"):
                    raise ExecutionBlocked("start ACK does not authorize this exact original execution")
                if not self.journal.begin_execution(aid, execution_id):
                    raise ExecutionBlocked("original execution is already fenced")
                self._phase(aid, "running")
        if hasattr(self.runtime, "execute_with_barrier"):
            completion = self.runtime.execute_with_barrier(prepared, execution_id, authorize, self.launch_guard)
        else:
            authorize()
            with self.launch_guard():
                # The injected synthetic runtime crosses its simulated launch
                # point synchronously; real runtimes use the nonce guard above.
                pass
            completion = self.runtime.execute(prepared, execution_id)
        self._phase(aid, "saving")
        payload = save_completion(self.journal.root / "artifacts", current, execution_id, completion)
        self.journal.save_result(aid, execution_id, payload)
        return payload

    def _upload(self, a: dict) -> dict | None:
        saved = self.journal.execution(a["assignment_id"])
        if not saved:
            return None
        if saved["result_json"] is None:
            recovered = recover_completion(self.journal.root / "artifacts", a, saved["execution_id"])
            if recovered is None:
                return None
            self.journal.save_result(a["assignment_id"], saved["execution_id"], recovered)
            saved = self.journal.execution(a["assignment_id"])
        payload = json.loads(saved["result_json"])
        if a.get('completion_correction'):
            self._accepted_correction(a, payload)
            return None
        if any(r.operation.startswith('completion-correction:' + a['assignment_id'] + ':') for r in self.journal.requests()):
            raise ExecutionBlocked('explicit correction pending; replay supplement-result and preserve original failed request')
        self._phase(a["assignment_id"], "uploading")
        limit = (self.client._bootstrap or self.client.bootstrap()).get("limits", {}).get("max_result_bytes")
        if limit is not None and sum(f["size_bytes"] for f in payload["artifacts"]) > limit:
            raise ExecutionBlocked("saved result exceeds advertised upload limit; evidence retained")
        files = upload_files(self.journal.root / "artifacts", a["assignment_id"], payload)
        req = self.journal.prepare(f"result:{a['assignment_id']}", f"/api/v2/assignments/{a['assignment_id']}/result", payload)
        reply = self.client.send_result(req, files)
        result_receipt(reply, req.request_id, a["assignment_id"], payload)
        a["state"] = "submitted"
        self.blocked.pop(a["assignment_id"], None)
        return reply

    def upload_only(self) -> list[dict]:
        """No create, claim, start, heartbeat or runtime.prepare/execute."""
        self._require_lock()
        return [receipt for a in self.snapshot()["assignments"] if (receipt := self._upload(a)) is not None]

    def _heartbeat(self, a: dict, now: float):
        aid = a["assignment_id"]
        interval = self.client._bootstrap.get("heartbeat_seconds", 30)
        if now < self._heartbeat_at.get(aid, 0):
            return
        with self._phase_lock:
            phase = self.phases.get(aid, "preparing")
        saved = self.journal.execution(aid)
        execution_id = saved["execution_id"] if saved else a.get("execution_id")
        elapsed = int((time.monotonic() - self._observed_start[aid]) * 1000) if aid in self._observed_start else None
        body = {**owner(a), "execution_id": execution_id, "phase": phase,
                "progress": {"elapsed_ms": elapsed, "tokens": {"input": None, "output": None, "total": None, "source": None, "missing_reason": "live_usage_unavailable"}}}
        req = self._next(f"heartbeat:{aid}:", f"/api/v2/assignments/{aid}/heartbeat", body)
        reply = envelope(self.client.send(req), req.request_id)
        if reply.get("status") != "ok":
            raise ProtocolError("heartbeat not acknowledged")
        if reply.get("stop_requested") is True:
            # The boolean alone cannot distinguish natural no_work drain from
            # user/local_error cancellation. Reconcile the authoritative reason.
            stopped = self.snapshot()['run']
            if stopped['state'] != 'stopped':
                raise ProtocolError('heartbeat stop has no authoritative stopped run')
        self._heartbeat_at[aid] = now + interval

    def tick(self, *, now: float | None = None) -> dict:
        self._require_lock()
        if self.historical_mixed_scope:
            raise ExecutionBlocked('historical68 is recovery-only; no new scheduling')
        if not self.launch_allowed:
            raise ExecutionBlocked("copied journal is upload-only")
        now = time.monotonic() if now is None else now
        unresolved_slots = set()
        self._reconcile_releases(now)
        # A lost claim ACK is resolved before adopting its server inventory;
        # restarting does not replace the request or create a second claim.
        for req in self.journal.requests():
            if not req.operation.startswith("claim:") or req.response is not None:
                continue
            slot = int(req.operation.split(":")[1])
            if now < self.retry_at.get(f"slot:{slot}", 0):
                unresolved_slots.add(slot)
                continue
            try:
                reply = envelope(self.client.send(req), req.request_id)
                if reply.get("status") not in {"claimed", "retry", "no_work", "stop"}:
                    raise ProtocolError("unknown recovered claim decision")
                if reply["status"] == "retry":
                    delay = reply.get("retry_after_seconds")
                    if type(delay) is not int or delay <= 0:
                        raise ProtocolError("retry delay missing")
                    self.retry_at[f"slot:{slot}"] = now + delay
                elif reply["status"] in {"no_work", "stop"}:
                    self.accepting = False
                    self.shortfall_reason = reply.get("reason")
                    if reply["status"] == "no_work": self.stop("no_work")
            except TransportUnknown as exc:
                self.retry_at[f"slot:{slot}"] = now + exc.retry_after_seconds
                unresolved_slots.add(slot)
        snap = self.snapshot()
        if self.journal.value("local_stop") == "true":
            self.accepting = False
            self._stop_event.set()
        assignments = snap["assignments"]
        for a in assignments:
            aid = a["assignment_id"]
            future = self.futures.get(aid)
            if future and not future.done():
                try:
                    self._heartbeat(a, now)
                except TransportUnknown as exc:
                    self._heartbeat_at[aid] = now + exc.retry_after_seconds
                continue
            if future:
                del self.futures[aid]
                try:
                    future.result()
                except TransportUnknown as exc:
                    self.retry_at[aid] = now + exc.retry_after_seconds
                except UnstartedLeaseEnded as exc:
                    a.update(exc.assignment)
                    self.blocked.pop(aid,None)
                except AssignmentMismatch:
                    self.blocked[aid] = 'assignment_mismatch'
                    self.shortfall_reason = 'local_error'
                    self.stop('local_error')
                except TaskNotReady:
                    if self.journal.value('release_reconciled:'+aid)!='true':
                        self.blocked[aid]='task_runtime_unavailable'
                except PreflightFailed:
                    self.blocked[aid] = "preflight_failed"
                    self.shortfall_reason = "local_error"
                    self.stop("local_error")
                except Exception as exc:
                    self.blocked[aid] = "storage_enospc" if getattr(exc,"code",None)=="storage_enospc" else "execution_or_exit_unknown"
                    if self.blocked[aid] == "storage_enospc":
                        self.stop("local_error")
            if (self._unstarted_terminal(a) and self.blocked.get(aid) == 'execution_or_exit_unknown'
                    and not self.journal.pending_request('release:'+aid)):
                self.blocked.pop(aid,None)
            saved = self.journal.execution(aid)
            if self.journal.pending_request('release:'+aid):
                continue
            if saved and saved["result_json"] is None:
                recovered = recover_completion(self.journal.root / "artifacts", a, saved["execution_id"])
                if recovered is not None:
                    self.journal.save_result(aid, saved["execution_id"], recovered)
                    saved = self.journal.execution(aid)
            if saved and saved["result_json"] is not None:
                if now < self.retry_at.get(aid, 0):
                    continue
                try:
                    self._upload(a)
                except TransportUnknown as exc:
                    self.blocked[aid] = "upload_ack_unknown"
                    self.retry_at[aid] = now + exc.retry_after_seconds
                continue
            if (a['state'] in {'released','expired'} and any(r.operation == 'start:'+aid for r in self.journal.requests())):
                self.blocked.setdefault(aid,'execution_or_exit_unknown')
            elif a['state'] == 'running' and snap['run']['state'] == 'stopped' and not saved:
                self.blocked.setdefault(aid,'start_authorization_unresolved_after_stop')
            elif saved or a["state"] == "uncertain":
                self.blocked.setdefault(aid, "execution_or_exit_unknown")
            elif a["state"] in {"leased", "running"} and aid not in self.blocked and now >= self.retry_at.get(aid, 0) and snap["run"]["state"] == "active" and a["slot_id"] not in unresolved_slots:
                self.futures[aid] = self.pool.submit(self._work, a)
        occupied = {a["slot_id"] for a in assignments if a["state"] in {"leased", "running", "uncertain"} or a["assignment_id"] in self.futures or a["assignment_id"] in self.blocked}
        occupied.update(unresolved_slots)
        if self.accepting:
            from ..harness_policy import reject_retired_combination
            reject_retired_combination(self.configuration['agent'], self.configuration['model'])
            for slot in range(self.configuration["concurrency"]):
                if slot in occupied or now < self.retry_at.get(f"slot:{slot}", 0):
                    continue
                req = self._next(f"claim:{slot}:", f"/api/v2/runs/{self.run_id}/claim", self._claim_body(slot))
                try:
                    reply = envelope(self.client.send(req), req.request_id)
                except TransportUnknown as exc:
                    self.retry_at[f"slot:{slot}"] = now + exc.retry_after_seconds
                    continue
                status = reply.get("status")
                if status == "claimed":
                    try:
                        a = self._assignment(reply.get("assignment"), slot_id=slot)
                    except AssignmentMismatch:
                        self.blocked[f'slot:{slot}'] = 'assignment_mismatch'
                        self.shortfall_reason = 'local_error'
                        self.stop('local_error')
                        break
                    if a["state"] in {"leased", "running"}:
                        assignments.append(a)
                        self.futures[a["assignment_id"]] = self.pool.submit(self._work, a)
                elif status == "retry":
                    delay = reply.get("retry_after_seconds")
                    if type(delay) is not int or delay <= 0:
                        raise ProtocolError("retry must expose a positive wait")
                    self.retry_at[f"slot:{slot}"] = now + delay
                elif status in {"no_work", "stop"}:
                    self.accepting = False
                    self.shortfall_reason = reply.get("reason")
                    if status == "no_work":
                        self.stop("no_work")
                    break
                else:
                    raise ProtocolError("unknown claim decision")
        # Exhausting the start budget only stops new claims. It does not
        # settle a lost ACK, retry wait, saved upload, or unresolved execution.
        unresolved = {a['assignment_id']:a['state'] for a in assignments
                      if a['state'] in {'leased','running','uncertain'}}
        for req in self.journal.requests():
            if req.response is None and not req.operation.startswith('heartbeat:'):
                unresolved[req.operation] = 'mutation_ack_unknown'
            elif req.operation.startswith('release:') and self.journal.value('release_reconciled:'+req.operation.split(':',1)[1]) != 'true':
                unresolved[req.operation] = 'release_state_unknown'
        return {"run": snap["run"], "accepting": self.accepting, "active_local": len(self.futures),
                "blocked": dict(self.blocked), "unresolved": unresolved,
                "settled": not unresolved and not self.futures and not self.blocked,
                "shortfall_reason": self.shortfall_reason,"runtime_unavailable_tasks":self.runtime_unavailable_tasks()}
