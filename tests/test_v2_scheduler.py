from contextlib import contextmanager
from copy import deepcopy
from email.parser import BytesParser
import json
import threading
import time
import httpx
import pytest
from dradar.v2.client import Client, TransportUnknown
from dradar.v2.journal import Journal
from dradar.v2.scheduler import Controller, ExecutionBlocked
from dradar.v2.results import Completion

CONFIG = {"benchmark": "synthetic", "model": "codex-test", "effort": "high", "agent": "codex", "total_count": 4, "concurrency": 2}

class Server:
    def __init__(self, available=100):
        self.lock = threading.RLock()
        self.available = available
        self.run = None
        self.assignments = {}
        self.receipts = {}
        self.calls = []
        self.start_drop = False
        self.result_drop = False
        self.claim_drop = False

    def response(self, data, req_id=None):
        value = {"schema_version": 2, "server_time": "2026-10-02T17:00:00Z", **deepcopy(data)}
        if req_id: value["request_id"] = req_id
        return httpx.Response(200, json=value)

    def __call__(self, req):
        with self.lock:
            path = req.url.path
            self.calls.append((req.method, path))
            if path == "/api/v2/bootstrap":
                return self.response({"capabilities": ["on-demand-v2"], "account": {"account_id": "synthetic"}, "benchmarks": [{"benchmark": "synthetic", "models": [{"model": "codex-test", "effort": "high"}]}], "limits": {"max_total_count": None, "max_concurrency": None}, "heartbeat_seconds": 30})
            if req.method == "GET":
                if "/assignments/" in path:
                    return self.response({"assignment": self.assignments[path.rsplit("/", 1)[1]]})
                return self.response({"run": self.run, "assignments": list(self.assignments.values())})
            if path.endswith("/result"):
                message = BytesParser().parsebytes(b"Content-Type: " + req.headers["content-type"].encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + req.content)
                parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True) for part in message.walk() if part.get_param("name", header="content-disposition")}
                body = json.loads(parts["metadata"])
            else:
                body = json.loads(req.content)
            rid = body["request_id"]
            if rid in self.receipts:
                return self.response({**self.receipts[rid], "replayed": True}, rid)
            if path == "/api/v2/runs":
                self.run = {k: v for k, v in body.items() if k != "request_id"}
                self.run.update(state="active", stop_reason=None, remaining_to_start=body["total_count"], counts={"started": 0, "leased": 0, "running": 0, "uncertain": 0, "submitted": 0})
                data = {"status": "accepted", "run": self.run}
            elif path.endswith("/claim"):
                if self.run["state"] == "stopped":
                    data = {"status": "stop", "reason": "run_stopped", "retry_after_seconds": None}
                elif self.run["counts"]["started"] >= self.run["total_count"]:
                    data = {"status": "stop", "reason": "total_reached", "retry_after_seconds": None}
                elif self.run["counts"]["started"] + self.run["counts"]["leased"] >= self.run["total_count"]:
                    data = {"status": "retry", "reason": "slot_busy", "retry_after_seconds": 5}
                elif len(self.assignments) >= self.available:
                    data = {"status": "no_work", "reason": "no_eligible_tasks", "retry_after_seconds": None}
                else:
                    aid = f"a{len(self.assignments)}"
                    a = {"assignment_id": aid, "run_id": self.run["run_id"], "device_id": body["device_id"], "slot_id": body["slot_id"], "work_key": "work_" + aid, "lease_id": "lease_" + aid, "owner_epoch": 1, "state": "leased", "execution_id": None, "started_at": None, "runner": {"agent":"codex","agent_version":"0.145.0","agent_version_verified":True,"auth_runtime":None,"provider":None,"billing_mode":"subscription","est_minutes":1}, "task": {"task_id": aid, "benchmark": "synthetic", "model": "codex-test", "effort": "high", "task_content_hash": "0" * 64, "task_commit": "0" * 40, "task_bundle": None}}
                    self.assignments[aid] = a
                    self.run["counts"]["leased"] += 1
                    data = {"status": "claimed", "assignment": a}
            elif path.endswith("/stop"):
                self.run["state"] = "stopped"
                self.run["stop_reason"] = body["reason"]
                for a in self.assignments.values():
                    if a["state"] == "leased": a["state"] = "released"
                data = {"status": "stopped", "run": self.run}
            else:
                aid = path.split("/")[-2]
                a = self.assignments[aid]
                if path.endswith("/start"):
                    if self.run["state"] == "stopped":
                        return httpx.Response(409, json={"error": {"code": "run_stopped"}})
                    a["execution_id"] = body["execution_id"]
                    a["state"] = "running"
                    self.run["counts"]["started"] += 1
                    self.run["counts"]["leased"] -= 1
                    self.run["remaining_to_start"] -= 1
                    data = {"status": "started", "assignment": a}
                elif path.endswith("/result"):
                    a["state"] = "submitted"
                    self.run["counts"]["submitted"] += 1
                    data = {"status": "submitted", "assignment_id": aid, "execution_id": body["execution_id"], "submission_id": "s_" + aid, "result_sha256": body["result_sha256"], "grading_state": "queued" if body["outcome"] == "completed" else "not_applicable"}
                elif path.endswith("/release"):
                    a["state"] = "released"
                    self.run["counts"]["leased"] -= 1
                    data = {"status": "released", "assignment": a}
                elif path.endswith("/heartbeat"):
                    data = {"status": "ok", "assignment": a, "stop_requested": self.run["state"] == "stopped"}
                else:
                    raise AssertionError(path)
            self.receipts[rid] = deepcopy({**data, "replayed": False})
            for suffix, attr in (("start", "start_drop"), ("result", "result_drop"), ("claim", "claim_drop")):
                if path.endswith("/" + suffix) and getattr(self, attr):
                    setattr(self, attr, False)
                    raise httpx.ReadTimeout("simulated lost ACK after commit", request=req)
            return self.response(self.receipts[rid], rid)

class Runtime:
    def __init__(self, root, *, gate=None, exit_confirmed=True, fail_prepare=False):
        self.root, self.gate, self.exit_confirmed = root, gate, exit_confirmed
        self.fail_prepare = fail_prepare
        self.calls = []
        self.server = None

    def prepare(self, a):
        if self.fail_prepare: raise RuntimeError("synthetic preflight failure")
        return a

    def execute(self, a, execution_id):
        assert self.server.assignments[a["assignment_id"]]["execution_id"] == execution_id
        self.calls.append(execution_id)
        if self.gate: assert self.gate.wait(3)
        path = self.root / (a["assignment_id"] + ".patch")
        path.write_bytes(b"")
        return Completion("completed", self.exit_confirmed, {"patch": path}, elapsed_ms=1)

@contextmanager
def controller(tmp_path, server, runtime, config=None):
    runtime.server = server
    client = Client("http://localhost:1234", "synthetic-token", Journal(tmp_path / "state"), transport=httpx.MockTransport(server))
    value = Controller(client, runtime, config or CONFIG)
    try:
        with value.ownership():
            yield value
    finally:
        client.close()

def pump(c, predicate, max_ticks=100):
    for n in range(max_ticks):
        c.tick(now=1000 + n * 5)
        if predicate(): return
        time.sleep(.005)
    raise AssertionError("controller did not reach expected synthetic condition")

def test_create_reserves_zero_tasks_and_no_paid_work(tmp_path):
    server, runtime = Server(), Runtime(tmp_path)
    with controller(tmp_path, server, runtime) as c:
        c.initialize()
        assert not server.assignments and not runtime.calls

def test_claims_only_free_slots_and_refills_after_accepted_results(tmp_path):
    gate = threading.Event()
    server, runtime = Server(), Runtime(tmp_path, gate=gate)
    with controller(tmp_path, server, runtime) as c:
        c.initialize()
        c.tick(now=1000)
        for n in range(10): c.tick(now=1001 + n)
        assert len(server.assignments) == 2
        gate.set()
        pump(c, lambda: server.run["counts"]["submitted"] == 4)
        assert len(runtime.calls) == 4 and len(set(runtime.calls)) == 4

def test_lost_start_ack_reconciles_original_execution_once(tmp_path):
    server, runtime = Server(), Runtime(tmp_path)
    server.start_drop = True
    cfg = {**CONFIG, "total_count": 1, "concurrency": 1}
    with controller(tmp_path, server, runtime, cfg) as c:
        c.initialize()
        pump(c, lambda: server.run["counts"]["submitted"] == 1)
        assert len(runtime.calls) == 1
        starts = [q for q in c.journal.requests() if q.operation.startswith("start:")]
        assert len(starts) == 1

def test_unknown_physical_exit_blocks_slot_without_refill(tmp_path):
    server, runtime = Server(), Runtime(tmp_path, exit_confirmed=False)
    cfg = {**CONFIG, "concurrency": 1}
    with controller(tmp_path, server, runtime, cfg) as c:
        c.initialize()
        pump(c, lambda: bool(c.blocked))
        for n in range(5): c.tick(now=3000 + n)
        assert len(runtime.calls) == 1 and len(server.assignments) == 1
        assert server.run["counts"]["submitted"] == 0
    with controller(tmp_path, server, runtime, cfg) as c:
        c.initialize()
        c.tick(now=4000)
        assert len(runtime.calls) == 1

def test_lost_result_ack_restart_upload_only_keeps_paid_count(tmp_path):
    server, runtime = Server(), Runtime(tmp_path)
    server.result_drop = True
    cfg = {**CONFIG, "total_count": 1, "concurrency": 1}
    with controller(tmp_path, server, runtime, cfg) as c:
        c.initialize()
        pump(c, lambda: bool(c.blocked))
        assert len(runtime.calls) == 1
    prior = len(server.calls)
    with controller(tmp_path, server, runtime, cfg) as c:
        c.upload_only()
    recovery = server.calls[prior:]
    assert not any(path.endswith(("/claim", "/start")) or path == "/api/v2/runs" for method, path in recovery if method == "POST")
    assert len(runtime.calls) == 1 and server.run["counts"]["submitted"] == 1

def test_preflight_failure_releases_without_paid_start(tmp_path):
    server, runtime = Server(), Runtime(tmp_path, fail_prepare=True)
    cfg = {**CONFIG, "concurrency": 1}
    with controller(tmp_path, server, runtime, cfg) as c:
        c.initialize()
        pump(c, lambda: bool(c.blocked))
        assert not runtime.calls and server.run["counts"]["started"] == 0
        assert next(iter(server.assignments.values()))["state"] == "released"

def test_no_work_reports_shortfall_and_stops_without_infinite_poll(tmp_path):
    server, runtime = Server(available=0), Runtime(tmp_path)
    with controller(tmp_path, server, runtime) as c:
        c.initialize()
        result = c.tick(now=1000)
        assert result["shortfall_reason"] == "no_eligible_tasks"
        assert server.run["state"] == "stopped"
        prior = sum(path.endswith("/claim") for _, path in server.calls)
        c.tick(now=5000)
        assert sum(path.endswith("/claim") for _, path in server.calls) == prior

def test_unlocked_controller_refuses_to_claim(tmp_path):
    client = Client("http://localhost:1", "synthetic", Journal(tmp_path))
    c = Controller(client, Runtime(tmp_path), CONFIG)
    with pytest.raises(ExecutionBlocked): c.tick()
    c.pool.shutdown()
    client.close()

def test_copied_journal_cannot_launch_on_different_installation(tmp_path):
    import shutil
    server, runtime = Server(), Runtime(tmp_path)
    cfg = {**CONFIG, "total_count": 1, "concurrency": 1}
    with controller(tmp_path, server, runtime, cfg) as c:
        c.initialize()
    other = tmp_path / "other-device"
    other.mkdir()
    shutil.copytree(tmp_path / "state", other / "state")
    with controller(other, server, runtime, cfg) as c:
        assert not c.launch_allowed
        with pytest.raises(ExecutionBlocked): c.initialize()
        with pytest.raises(ExecutionBlocked): c.tick()
        assert c.upload_only() == []
    assert not runtime.calls

def test_stop_before_start_never_invokes_runtime(tmp_path):
    server, runtime = Server(), Runtime(tmp_path)
    with controller(tmp_path, server, runtime) as c:
        c.initialize()
        c.stop()
        c.tick(now=1000)
        assert not runtime.calls and not server.assignments

def test_lost_claim_ack_recovers_one_assignment(tmp_path):
    server, runtime = Server(), Runtime(tmp_path)
    server.claim_drop = True
    cfg = {**CONFIG, "total_count": 1, "concurrency": 1}
    with controller(tmp_path, server, runtime, cfg) as c:
        c.initialize()
        pump(c, lambda: server.run["counts"]["submitted"] == 1)
        assert len(server.assignments) == 1 and len(runtime.calls) == 1

def test_start_ack_without_local_launch_fence_does_not_start_twice(tmp_path):
    server, runtime = Server(), Runtime(tmp_path)
    cfg = {**CONFIG, "total_count": 1, "concurrency": 1}
    with controller(tmp_path, server, runtime, cfg) as c:
        c.initialize()
        req = c.journal.prepare("claim:0:manual", f"/api/v2/runs/{c.run_id}/claim", {"device_id": c.device_id, "slot_id": 0})
        a = c.client.send(req)["assignment"]
        e = c.journal.identity("execution:" + a["assignment_id"])
        from dradar.v2.protocol import owner
        req = c.journal.prepare("start:" + a["assignment_id"], f"/api/v2/assignments/{a['assignment_id']}/start", {**owner(a), "execution_id": e})
        c.client.send(req)
        c.journal.begin_execution(a["assignment_id"], e)
    with controller(tmp_path, server, runtime, cfg) as c:
        c.initialize()
        c.tick(now=1000)
        assert c.blocked and not runtime.calls


def test_uncertain_inventory_without_local_launch_is_explicitly_blocked(tmp_path):
    server=Server(); rt=Runtime(tmp_path)
    with controller(tmp_path,server,rt) as c:
        c.initialize()
        # Own one lease without running a future, then observe authoritative uncertainty.
        reply=c.client.mutate("test:claim",f"/api/v2/runs/{c.run_id}/claim",{"device_id":c.device_id,"slot_id":0})
        a=server.assignments[reply["assignment"]["assignment_id"]]
        a["state"]="uncertain"
        c.accepting=False
        result=c.tick()
        assert a["assignment_id"] in result["blocked"] and rt.calls==[]


def test_storage_failure_stops_claiming_with_explicit_classification(tmp_path):
    from dradar.v2.docker_cache import DockerStorageError
    server=Server(); rt=Runtime(tmp_path)
    rt.execute=lambda *args: (_ for _ in ()).throw(DockerStorageError("synthetic storage exhaustion"))
    with controller(tmp_path,server,rt,{**CONFIG,"concurrency":1}) as c:
        c.initialize()
        pump(c,lambda:bool(c.blocked))
        assert list(c.blocked.values())==["storage_enospc"]
        assert c.journal.value("local_interrupt")=="true"
        assert server.run["state"]=="stopped"


def test_saved_local_observations_are_shown_after_result_ack(tmp_path):
    server=Server(); rt=Runtime(tmp_path)
    with controller(tmp_path,server,rt,{**CONFIG,"total_count":1,"concurrency":1}) as c:
        c.initialize()
        pump(c,lambda:server.run["counts"]["submitted"]==1)
        a=c.progress_snapshot()["assignments"][0]
        assert a["outcome"]=="completed" and a["progress"]["elapsed_ms"]==1
        assert a["progress"]["tokens"]["total"] is None

def test_no_work_heartbeat_does_not_interrupt_started_execution(tmp_path):
    gate=threading.Event(); server=Server(available=1); rt=Runtime(tmp_path,gate=gate)
    with controller(tmp_path,server,rt) as c:
        c.initialize()
        reply=c.client.mutate('fixture:claim',f'/api/v2/runs/{c.run_id}/claim',{'device_id':c.device_id,'slot_id':0})
        a=reply['assignment']; c.futures[a['assignment_id']]=c.pool.submit(c._work,a)
        try:
            for _ in range(100):
                if rt.calls:break
                time.sleep(.005)
            assert rt.calls
            c.tick(now=1000)  # Empty second slot stops claiming with no_work.
            assert server.run['stop_reason']=='no_work'
            c.tick(now=2000)  # stop_requested heartbeat must preserve natural drain.
            assert c.journal.value('local_interrupt') is None
        finally:gate.set()
        pump(c,lambda:server.run['counts']['submitted']==1)
        assert len(rt.calls)==1

def test_user_stop_heartbeat_still_interrupts_started_execution(tmp_path):
    gate=threading.Event(); server=Server();rt=Runtime(tmp_path,gate=gate)
    with controller(tmp_path,server,rt,{**CONFIG,'concurrency':1}) as c:
        c.initialize();c.tick(now=1000)
        try:
            for _ in range(100):
                if rt.calls:break
                time.sleep(.005)
            server.run.update(state='stopped',stop_reason='user_stop')
            c.tick(now=2000)
            assert c.journal.value('local_interrupt')=='true'
        finally:gate.set()

class ExpireBeforeGetServer(Server):
    def __call__(self,req):
        if req.method=='GET' and '/assignments/' in req.url.path:
            a=self.assignments[req.url.path.rsplit('/',1)[1]]
            if a['assignment_id']=='a0' and a['state']=='leased':
                a['state']='expired'; self.run['counts']['leased']-=1
        return super().__call__(req)

def test_expired_between_claim_and_get_refills_without_launch_or_unknown_block(tmp_path):
    server=ExpireBeforeGetServer();rt=Runtime(tmp_path)
    with controller(tmp_path,server,rt,{**CONFIG,'total_count':1,'concurrency':1}) as c:
        c.initialize()
        pump(c,lambda:server.run['counts']['submitted']==1)
        assert server.assignments['a0']['state']=='expired'
        assert not c.journal.execution('a0') and 'a0' not in c.blocked
        assert len(rt.calls)==1 and server.run['counts']['started']==1

def test_terminal_expiry_never_clears_existing_launch_fence(tmp_path):
    server=Server();rt=Runtime(tmp_path)
    with controller(tmp_path,server,rt,{**CONFIG,'concurrency':1}) as c:
        c.initialize()
        a=c.client.mutate('fixture:claim',f'/api/v2/runs/{c.run_id}/claim',{'device_id':c.device_id,'slot_id':0})['assignment']
        c.journal.begin_execution(a['assignment_id'],c.journal.identity('execution:'+a['assignment_id']))
        server.assignments[a['assignment_id']]['state']='expired'
        server.run['counts']['leased']=0
        c.tick(now=1000)
        assert a['assignment_id'] in c.blocked and not rt.calls
        assert len(server.assignments)==1

@pytest.mark.parametrize('field,value',[('model','unrequested-model'),('effort','low'),('benchmark','other-benchmark')])
def test_changed_run_selection_never_prepares_or_starts(tmp_path,field,value):
    class Changed(Server):
        def response(self,data,req_id=None):
            data=deepcopy(data)
            if data.get('status')=='claimed':
                data['assignment']['task'][field]=value
                self.assignments[data['assignment']['assignment_id']]['task'][field]=value
            return super().response(data,req_id)
    server=Changed();rt=Runtime(tmp_path)
    with controller(tmp_path,server,rt,{**CONFIG,'total_count':1,'concurrency':1}) as c:
        c.initialize()
        try:c.tick(now=1000)
        except Exception:pass
        for future in c.futures.values():
            try:future.result(timeout=2)
            except Exception:pass
        assert not rt.calls and server.run['counts']['started']==0
        assert not any(path.endswith('/start') for _,path in server.calls)

@pytest.mark.parametrize('stage', ['get','start'])
@pytest.mark.parametrize('part,field,value', [('task','task_id','swapped-task'),('task','model','unrequested-model'),('task','effort','low'),('runner','agent','other-agent'),('owner','work_key','swapped-work'),('owner','lease_id','swapped-lease'),('owner','assignment_id','swapped-assignment'),('owner','owner_epoch',2),('owner','device_id','swapped-device'),('owner','run_id','swapped-run'),('owner','slot_id',1)])
def test_assignment_substitution_at_get_or_start_never_executes(tmp_path,stage,part,field,value):
    class Changed(Server):
        def __call__(self,req):
            response=super().__call__(req)
            if ((stage=='get' and req.method=='GET' and '/assignments/' in req.url.path)
                    or (stage=='start' and req.url.path.endswith('/start'))):
                data=response.json();a=data['assignment']
                (a if part=='owner' else a[part])[field]=value
                return httpx.Response(200,json=data)
            return response
    server=Changed();rt=Runtime(tmp_path)
    with controller(tmp_path,server,rt,{**CONFIG,'total_count':1,'concurrency':1}) as c:
        c.initialize();c.tick(now=1000)
        for future in list(c.futures.values()):
            try:future.result(timeout=2)
            except Exception:pass
        assert not rt.calls
        assert not any(c.journal.execution(aid) for aid in server.assignments)

@pytest.mark.parametrize('commit_before_loss',[False,True])
def test_lost_preflight_release_ack_replays_exact_request_without_paid_execution(tmp_path,commit_before_loss):
    class ReleaseLost(Server):
        def __init__(self):super().__init__();self.release_bodies=[]
        def __call__(self,req):
            if req.url.path.endswith('/release'):
                self.release_bodies.append(req.content)
                if len(self.release_bodies)==1:
                    if commit_before_loss:super().__call__(req)
                    raise httpx.ReadTimeout('synthetic release loss',request=req)
            return super().__call__(req)
    server=ReleaseLost();rt=Runtime(tmp_path,fail_prepare=True)
    with controller(tmp_path,server,rt,{**CONFIG,'concurrency':1}) as c:
        c.initialize()
        pump(c,lambda:len(server.release_bodies)>=2)
        assert server.release_bodies[0]==server.release_bodies[1]
        assert len([r for r in c.journal.requests() if r.operation.startswith('release:')])==1
        assert c.journal.pending_request('release:a0') is None
        assert server.assignments['a0']['state']=='released'
        assert c.blocked.get('a0')!='execution_or_exit_unknown'
        assert not rt.calls and not c.journal.execution('a0')

def test_pending_start_terminal_snapshot_cannot_free_slot(tmp_path):
    from dradar.v2.protocol import owner
    server=Server();rt=Runtime(tmp_path)
    with controller(tmp_path,server,rt,{**CONFIG,'concurrency':1}) as c:
        c.initialize()
        a=c.client.mutate('fixture:claim',f'/api/v2/runs/{c.run_id}/claim',{'device_id':c.device_id,'slot_id':0})['assignment']
        c.journal.prepare('start:'+a['assignment_id'],f"/api/v2/assignments/{a['assignment_id']}/start",{**owner(a),'execution_id':c.journal.identity('execution:'+a['assignment_id'])})
        server.assignments[a['assignment_id']]['state']='expired';server.run['counts']['leased']=0
        result=c.tick(now=1000)
        assert a['assignment_id'] in result['blocked'] and not result['settled']
        assert len(server.assignments)==1 and not rt.calls

def test_release_ack_then_unknown_authoritative_get_resumes_on_restart(tmp_path):
    class Lost(Server):
        def __init__(self):super().__init__();self.release_bodies=[];self.drop_read=True
        def __call__(self,req):
            if req.url.path.endswith('/release'):
                self.release_bodies.append(req.content)
                if len(self.release_bodies)==1:raise httpx.ReadTimeout('first release lost',request=req)
            if req.method=='GET' and '/assignments/' in req.url.path and self.drop_read and self.release_bodies and self.assignments['a0']['state']=='released':
                self.drop_read=False;raise httpx.ReadTimeout('release committed; read unavailable',request=req)
            return super().__call__(req)
    server=Lost();rt=Runtime(tmp_path,fail_prepare=True);cfg={**CONFIG,'concurrency':1}
    with controller(tmp_path,server,rt,cfg) as c:
        c.initialize();c.tick(now=1000)
        for future in c.futures.values():
            try:future.result(timeout=2)
            except Exception:pass
        c._reconcile_releases(10000)
        assert c.journal.pending_request('release:a0') is None
        assert c.journal.value('release_reconciled:a0') is None
        assert c.journal.value('local_stop')=='true' and not c.accepting
    with controller(tmp_path,server,rt,cfg) as c:
        c.initialize();c.tick(now=20000)
        assert c.journal.value('release_reconciled:a0')=='true'
        assert not c.accepting and c.blocked['a0']=='preflight_failed'
        assert not rt.calls and len(server.assignments)==1
        assert len(server.release_bodies)==2 and server.release_bodies[0]==server.release_bodies[1]
