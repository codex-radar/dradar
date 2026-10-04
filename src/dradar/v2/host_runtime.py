"""V2 adapter for the explicit official host-keyring/remote-only capability.

One model turn per durable execution; no file-auth or local executor fallback.
Public evidence is collected before physical cleanup and normal V2 upload.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import importlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from .host_contract import (AUTH_RUNTIME, BILLING_MODE, CAPABILITY, CONFIG_VERSION,
                           MODEL, PROVIDER, VERSION, EFFORTS, load_binding, private_json)
from .runtime import CodexRuntime, RuntimeUnavailable, TaskNotReady
from .results import Completion

TERMINAL={'completed','failed','interrupted'}

def control_request(path, value, timeout=45):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(json.dumps(value).encode()+b'\n')
        with connection.makefile('rb') as f:
            raw=f.readline(8388609)
    if not raw.endswith(b'\n') or len(raw)>8388608:
        raise RuntimeUnavailable('invalid owned controller response')
    result=json.loads(raw)
    if 'error' in result:
        raise RuntimeUnavailable('owned host controller rejected operation')
    return result

class PrivateController:
    def __init__(self, configuration, folder):
        self.folder=Path(folder)
        self.configuration=configuration
        self.socket_dir=Path(tempfile.mkdtemp(prefix='dradar-host-',dir='/tmp'))
        self.socket_dir.chmod(0o700)
        configuration['control_socket']=str(self.socket_dir/'control.sock')
        configuration['evidence_dir']=str(self.folder)
        self.path=self.folder/'controller.json'
        self.path.write_text(json.dumps(configuration,sort_keys=True)+'\n')
        self.path.chmod(0o600)
        self.proc=None

    def start(self):
        # No inherited platform API keys/proxies or global Codex configuration.
        env={k:os.environ[k]for k in ['PATH','HOME','LANG','TMPDIR']if k in os.environ}
        env['PYTHONPATH']=str(Path(__file__).resolve().parents[2])
        self.proc=subprocess.Popen([sys.executable,'-m','dradar.v2.host_controller',str(self.path)],
            cwd=self.folder,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        deadline=time.monotonic()+150
        while time.monotonic()<deadline:
            if self.proc.poll() is not None:
                raise RuntimeUnavailable('host preflight failed; original evidence retained')
            if Path(self.configuration['control_socket']).exists():
                status=self.request({'op':'status'})
                if status['status']=='NO_MODEL_REMOTE_ONLY_READY':return status
            time.sleep(.1)
        raise RuntimeUnavailable('host preflight exceeded bounded preparation time')

    def request(self, value):
        return control_request(self.configuration['control_socket'],value)

    def close(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                try:self.request({'op':'stop'})
                except Exception:pass
                try:self.proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    # SIGTERM is handled by the controller: interrupt/stop and
                    # retain the task container whenever collection is absent.
                    self.proc.terminate()
                    try:self.proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        raise RuntimeUnavailable('controller exit unknown; keep original fence')
            if self.proc.poll() is None:
                raise RuntimeUnavailable('controller exit unknown; keep original fence')
        p=self.folder/'DEDICATED_STATUS.json'
        status=json.loads(private_json(p)) if p.exists() else {}
        try:self.socket_dir.rmdir()
        except OSError:pass
        return status

class HostCodexRuntime(CodexRuntime):
    def __init__(self,journal,tasks_root,*,host_runtime_binding,host_runtime_sha256,
                 controller_factory=PrivateController,**kwargs):
        if controller_factory is PrivateController:
            try:
                ws = importlib.import_module('websockets')
                if ws.__version__ != '15.0.1':
                    raise ImportError('host-codex dependency version mismatch')
            except (ImportError, AttributeError) as exc:
                raise RuntimeUnavailable('install the reviewed CLI host-codex extra (websockets15.0.1) in the actual CLI interpreter before creating a run; no automatic install/fallback') from exc
        if kwargs.get('public_image_options'):
            raise RuntimeUnavailable('host runtime uses exact preinstalled image binding; no alternate builder')
        self.binding_path=Path(host_runtime_binding).resolve(strict=True)
        self.binding=load_binding(self.binding_path,host_runtime_sha256)
        journal.bind('host_runtime_binding_sha256',host_runtime_sha256)
        self.controller_factory=controller_factory
        self.controllers={}
        # Superclass is reused ONLY for the immutable public-pack checks. The
        # managed file-auth runner is never called in this explicit mode.
        super().__init__(journal,tasks_root,managed_auth_config=self.binding_path)

    def claim_task_candidates(self):
        from .host_contract import MIXED_SCHEMA
        if self.binding['schema']!=MIXED_SCHEMA:return None
        return [{k:t[k] for k in ('benchmark','task_id','task_content_hash')} for t in self.binding['tasks']]

    def prepare(self,a):
        if set(a.get('runner') or {}) != {'agent','agent_version','agent_version_verified','auth_runtime','provider','billing_mode','est_minutes'}:
            raise RuntimeUnavailable('Server runner must retain exact seven-field contract')
        from .host_contract import MIXED_SCHEMA
        roots = None
        if self.binding['schema'] == MIXED_SCHEMA:
            from .host_contract import selected_task_binding
            try:task=selected_task_binding(self.binding,a['task'])
            except (ValueError,KeyError,TypeError) as exc:raise TaskNotReady('runtime_binding_missing') from exc
            from .mixed_pool import validate_selected_public_root
            try:roots=validate_selected_public_root(task)
            except (ValueError,OSError,KeyError,TypeError) as exc:raise TaskNotReady('task_inputs_unavailable') from exc
        try:prepared=super().prepare(a,**({'tasks_root':roots[0],'bundle_root':roots[1]} if roots else {}))
        except (RuntimeUnavailable,OSError) as exc:
            if roots:raise TaskNotReady('task_inputs_unavailable') from exc
            raise
        if roots:prepared['tasks_root']=str(roots[0])
        if any(prepared.get(k)!=v for k,v in {
            'agent':'codex','agent_version':VERSION,'agent_version_verified':True,
            'auth_runtime':AUTH_RUNTIME,'provider':PROVIDER,'billing_mode':BILLING_MODE,
            'model':MODEL}.items()):
            raise RuntimeUnavailable('Server runner does not match explicit host0.160 capability')
        if prepared['effort'] not in EFFORTS:
            raise RuntimeUnavailable('unsupported Sol effort; preserve existing mapped levels')
        if a['slot_id'] not in [0,1]:
            raise RuntimeUnavailable('host runtime max2 model slots; no silent concurrency change')
        matches=[t for t in self.binding['tasks'] if (t['benchmark'],t['task_id'])==
                 (prepared['benchmark_id'],prepared['task_id'])]
        if len(matches)!=1 or matches[0]['task_content_hash']!=prepared['task_content_hash']:
            raise RuntimeUnavailable('versioned benchmark/public package runtime binding missing')
        task=matches[0]
        # Public collection code is owner-provided and digest-bound, never part
        # of a model patch or a Server command to execute on the host.
        try:
            if hashlib.sha256(private_json(task['collector_path'])).hexdigest()!=task['collector_sha256']:
                raise ValueError('fixed public collector binding mismatch')
        except (ValueError,OSError) as exc:
            if roots:raise TaskNotReady('collector_unavailable') from exc
            raise RuntimeUnavailable('fixed public collector binding mismatch') from exc
        if roots:
            # Inspect only the leased task image, never pull/build unrelated images.
            probe=subprocess.run(['docker','--context',self.binding['host']['docker_context'],'image','inspect',task['image_id'],'--format','{{.Id}}'],capture_output=True,text=True,timeout=10)
            if probe.returncode:
                if probe.stderr.strip().lower() in {'error: no such image: '+task['image_id'],'error response from daemon: no such image: '+task['image_id']}:
                    raise TaskNotReady('image_unavailable')
                raise RuntimeUnavailable('Docker image readiness unknown; preserve host safety stop')
            if probe.stdout.strip()!=task['image_id']:raise TaskNotReady('image_unavailable')
        aid=prepared['assignment_id']
        folder=self.journal.root/'runtime'/aid/'host'
        folder.mkdir(parents=True,mode=0o700,exist_ok=False)
        folder.chmod(0o700)
        cfg={**self.binding['host'],**task,'task':prepared['task_id'],
             'model':MODEL,'effort':prepared['effort'],'capability':CAPABILITY,
             'runtime_config_version':self.binding['runtime_config_version'],'max_model_parallel':2,
             'container_name':'dradar-host-'+hashlib.sha256((str(self.journal.root)+aid).encode()).hexdigest()[:24],
             'lock_path':str(Path(self.binding['host']['host_home'])/'runner'/f'host-remote-slot-{a["slot_id"]}.lock'),
             'journal_root':str(self.journal.root),'assignment_id':aid,
             'execution_id':self.journal.identity('execution:'+aid)}
        controller=self.controller_factory(cfg,folder)
        try:
            status=controller.start()
            auth=controller.request({'op':'account:status'})
            if auth.get('account_type')!='chatgpt':
                raise RuntimeUnavailable('official user device login required in dedicated host; no credential fallback')
            prepared={**prepared,'host_folder':str(folder),'host_preflight':status}
            self.controllers[aid]=controller
            return prepared
        except BaseException:
            closed=controller.close()
            if closed.get('created_containers_absent') is not True:
                raise RuntimeUnavailable('preflight cleanup unconfirmed; retained exact evidence')
            raise

    def execute_with_barrier(self,prepared,execution_id,barrier,launch_guard):
        aid=prepared['assignment_id'];controller=self.controllers.pop(aid)
        folder=Path(prepared['host_folder']);events=[];usage=None;started=None
        thread=prepared['host_preflight']['no_model_thread_preflight']['thread_id']
        turn=None;collection=None;terminal=None;interrupted=False;failure=None
        try:
            # The inherited scheduler verifies fresh lease/start ACK and writes
            # its one-use launch fence before this permission reaches app-server.
            barrier()
            with launch_guard():
                controller.request({'op':'authorize-one-turn','start_barrier_committed':True})
                started=time.monotonic()
                prompt=(Path(prepared.get('tasks_root',self.tasks_root))/prepared['task_id']/'instruction.md').read_text()
                response=controller.request({'op':'rpc','method':'turn/start','params':{
                    'threadId':thread,'model':MODEL,'effort':prepared['effort'],
                    'input':[{'type':'text','text':prompt,'text_elements':[]}]}})
            if 'error' in response or 'result' not in response:
                raise RuntimeUnavailable('official single turn rejected; no automatic retry')
            turn=response['result']['turn']['id']
            while True:
                batch=controller.request({'op':'events'}).get('events',[])
                for e in batch:
                    if 'approval_request' in e:
                        # Operator must review this exact one-time request via
                        # host-approval. Never acceptForSession or auto-approve.
                        approval=folder/'pending-approval.json'
                        approval.write_text(json.dumps(e['approval_request'])+'\n');approval.chmod(0o600)
                        self.journal.bind('host_approval:'+aid,str(approval))
                    else:
                        events.append(e)
                        if e.get('method')=='thread/tokenUsage/updated':
                            p=e['params']
                            if p.get('threadId')==thread and p.get('turnId')==turn:
                                usage=p['tokenUsage']['total']
                status=controller.request({'op':'status'})
                if status.get('last_turn_status') in TERMINAL:
                    terminal=status['last_turn_status']
                    final_events=controller.request({'op':'events'}).get('events',[])
                    events.extend(e for e in final_events if 'approval_request' not in e)
                    for e in final_events:
                        if e.get('method')=='thread/tokenUsage/updated':
                            p=e['params']
                            if p.get('threadId')==thread and p.get('turnId')==turn:usage=p['tokenUsage']['total']
                    break
                if self.journal.value('local_interrupt')=='true' and not interrupted:
                    controller.request({'op':'rpc','method':'turn/interrupt','params':{'threadId':thread,'turnId':turn}})
                    interrupted=True
                time.sleep(.2)
            collection=controller.request({'op':'collect'})
            (folder/'public-events.json').write_text(json.dumps(events)+'\n')
        except BaseException as exc:
            failure=exc
        finally:
            # SIGTERM/owned-stop preserve uncollected containers. Absence must
            # be independently proved before scheduler can free/upload the slot.
            closed=controller.close()
            (folder/'CLEANUP.json').write_text(json.dumps(closed,sort_keys=True)+'\n')
        if closed.get('native_app_server_reaped') is not True or closed.get('created_containers_absent') is not True:
            raise RuntimeUnavailable('physical exit or artifact collection unknown; retain fence')
        if failure is not None:
            # Before barrier/ACK, propagate: no invented elapsed/start/outcome.
            if started is None:raise failure
            return Completion('failed',True,completed_at=datetime.now(timezone.utc).isoformat(),
                elapsed_ms=int((time.monotonic()-started)*1000),
                failure={'code':'host_runtime_failed','message':'官方宿主运行失败，原成果及退出证据已保留'})
        unchanged=collection.get('public_inputs_unchanged',collection.get('training_unchanged'))
        if unchanged is not True or collection.get('unexpected_workspace_changes') or collection.get('missing_deliverables'):
            outcome='failed'
        else:outcome=terminal
        files={}
        collected=folder/'collected'
        for name,target in [('patch','model.patch'),('runner_result','COLLECTION.json')]:
            data=private_json(collected/target)
            if name=='patch' and hashlib.sha256(data).hexdigest()!=collection['patch_sha256']:
                raise RuntimeUnavailable('collected patch changed; retain raw evidence')
            files[name]=collected/target
        files['trajectory']=folder/'public-events.json'
        tokens={'input':None,'output':None,'total':None,'source':None,'missing_reason':'official_terminal_usage_unavailable'}
        # A usage notification is accepted only with the matching official
        # terminal event. No estimate, cached reuse, or cost fallback.
        terminal_event=any(e.get('method')=='turn/completed'and e.get('turnId')==turn and e.get('threadId')==thread for e in events)
        if usage and terminal_event and all(type(usage.get(k))is int and usage[k]>=0 for k in ['inputTokens','outputTokens','totalTokens']):
            if usage['inputTokens']+usage['outputTokens']==usage['totalTokens']:
                tokens={'input':usage['inputTokens'],'output':usage['outputTokens'],'total':usage['totalTokens'],'source':'official_app_server_terminal_usage','missing_reason':None}
        return Completion(outcome,True,files,datetime.now(timezone.utc).isoformat(),
                          int((time.monotonic()-started)*1000),tokens,
                          None if outcome=='completed' else {'code':'host_turn_'+outcome,'message':'模型未正常完成或公共输入/成果不符合固定契约'})
