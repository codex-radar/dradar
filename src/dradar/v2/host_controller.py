"""Owner-scoped native host auth and credential-free Docker executor controller.
Official OAuth/keyring stay on the dedicated Mac home. No model auto-start.
Only private owner control socket exposes transient device instructions.
"""
import asyncio,base64,datetime,fcntl,hashlib,io,json,os,re,secrets,signal,ssl,stat,subprocess,sys,tarfile,tomllib
from pathlib import Path
from urllib.parse import urlparse
import websockets
from dradar.v2.host_contract import validate_controller_config, confirmed_absence, validate_host_version
C=validate_controller_config(json.loads(Path(sys.argv[1]).read_text()));OUT=Path(C['evidence_dir']);HOME=Path(C['host_home'])
D=['docker','--context',C['docker_context']];NAME=C['container_name'];PNAME=NAME+'-official-egress'
PROXY=C['proxy_image_id']
POLICY='''http_port 127.0.0.1:8080
pid_filename /tmp/squid.pid
coredump_dir /tmp
auth_param basic program /usr/lib/squid/basic_ncsa_auth /tmp/squid.passwd
auth_param basic realm DRadarHost018
acl authenticated proxy_auth REQUIRED
acl CONNECT method CONNECT
acl tls_port port 443
acl official dstdomain -n auth.openai.com chatgpt.com
http_access deny !CONNECT
http_access deny !tls_port
http_access allow authenticated official
http_access deny all
cache deny all
access_log none
cache_log /dev/null
shutdown_lifetime 1 seconds
'''
SOCK=Path(C['control_socket'])
def docker(args,**kw):
 p=subprocess.run(D+args,capture_output=True,**kw)
 if p.returncode:raise RuntimeError('docker '+args[0]+' failed')
 return p.stdout
async def main():
 created=[];servers=[];children=set();proc=None;fd=None;reader_task=None;stderr_task=None
 remote_child=None;remote_connections={};deadline_task=None;remote_pending={};remote_counter=0;remote_write_lock=asyncio.Lock();probe_process_ids=set();probe_notifications={}
 pending={};counter=0;native_lock=asyncio.Lock();owner_lock=asyncio.Lock();stop=asyncio.Event()
 asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stop.set)
 transient_login=None;login_once=False;login_success=False;authorized=False;turn_used=False;owned_threads=set();owned_turns={};events=[];approval_requests={}
 report={'login_attempt':3,'status':'PREPARING','task':C['task'],'model_calls':0,'login_requests':0,'auth_copied':False,'original_home_read_or_modified':False,'home':str(HOME),'persistent_store':'direct-keyring','shared_identity_logged_out':False}
 def save():
  (OUT/'DEDICATED_STATUS.json').write_text(json.dumps(report,indent=2)+'\n')
 def note(value):print(json.dumps(value),flush=True)
 def sanitized_error(value):
  text=str(value)
  if transient_login:
   for secret in transient_login.values():
    if isinstance(secret,str) and secret:text=text.replace(secret,'[transient-login-redacted]')
  try:
   if token:text=text.replace(token,'[relay-credential-redacted]')
  except UnboundLocalError:pass
  text=re.sub(r'(?i)(access_token|refresh_token|id_token|user_code|device_code|authorization|api_key|client_secret)([\s\"\':=]+)([^\s,}]+)',r'\1\2[credential-redacted]',text)
  text=re.sub(r'(?i)Bearer\s+[^\s\"\']+','Bearer [credential-redacted]',text)
  text=re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}','[email-redacted]',text)
  text=re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+','[jwt-redacted]',text)
  text=re.sub(r'(https?://[^\s\"\'<>?#]+)[?#][^\s\"\'<>]*',r'\1?[query-redacted]',text)
  return text
 async def ws_bridge(ws):
  nonlocal remote_child
  child=await asyncio.create_subprocess_exec(*D,'exec','-i',NAME,C['executor_cli'],'exec-server','--listen','stdio://',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,limit=8388608);children.add(child)
  remote_connections[child]=True;remote_child=child
  report['official_executor_connections_started']=report.get('official_executor_connections_started',0)+1
  async def into():
   async for m in ws:
    if not isinstance(m,str) or len(m)>8388608:raise RuntimeError('invalid exec frame')
    obj=json.loads(m)
    if obj.get('method')=='process/start':
     params=obj['params'];safe={k:v for k,v in params.get('env',{}).items() if k in ['LANG','LC_ALL','TZ','PYTHONHASHSEED','OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','CODEX_THREAD_ID','CODEX_SESSION_ID']}
     safe.update(PATH=C['executor_path'],HOME=C['executor_home'],CODEX_HOME='/tmp/credential-free-executor');params['env']=safe
     params['envPolicy']={'inherit':'none','ignoreDefaultExcludes':False,'exclude':[],'set':safe,'includeOnly':[]}
     m=json.dumps(obj)
    async with remote_write_lock:
     child.stdin.write(m.encode()+b'\n');await child.stdin.drain()
  async def back():
   while line:=await child.stdout.readline():
    obj=json.loads(line);ident=obj.get('id')
    if ident in remote_pending and 'method' not in obj:
     remote_pending[ident].set_result(obj);continue
    if obj.get('params',{}).get('processId') in probe_process_ids:
     probe_notifications.setdefault(obj['params']['processId'],[]).append(obj);continue
    await ws.send(line.decode().rstrip('\r\n'))
  ts=[asyncio.create_task(into()),asyncio.create_task(back())]
  try:await asyncio.wait(ts,return_when=asyncio.FIRST_COMPLETED)
  finally:
   for t in ts:t.cancel()
   if child.stdin and not child.stdin.is_closing():child.stdin.close()
   try:await asyncio.wait_for(child.wait(),5)
   except asyncio.TimeoutError:child.terminate();await child.wait()
   report['exec_server_exit_code']=child.returncode
   detail=(await child.stderr.read(4000)).decode(errors='replace')
   report['credential_free_exec_server_startup_detail']=detail[:1000]
   children.discard(child);remote_connections.pop(child,None)
   if remote_child is child:remote_child=next(reversed(remote_connections),None)
 async def remote_call(method,params):
  nonlocal remote_counter
  if remote_child is None:raise RuntimeError("official remote connection absent")
  remote_counter+=1;ident="dradar-owner-preflight-"+str(remote_counter);fut=asyncio.get_running_loop().create_future();remote_pending[ident]=fut
  try:
   async with remote_write_lock:
    remote_child.stdin.write((json.dumps({"id":ident,"method":method,"params":params})+"\n").encode());await remote_child.stdin.drain()
   obj=await asyncio.wait_for(asyncio.shield(fut),15)
   if "error" in obj:raise RuntimeError("official remote "+method+": "+str(obj["error"])[:180])
   return obj["result"]
  finally:remote_pending.pop(ident,None)
 async def remote_exec(command):
  handle="dradar-preflight-"+secrets.token_hex(8);probe_process_ids.add(handle)
  await remote_call("process/start",{"processId":handle,"argv":command,"cwd":"file:///app","env":{"PATH":C["executor_path"],"HOME":C["executor_home"],"CODEX_HOME":"/tmp/credential-free-executor"},"tty":False,"arg0":None,"sandbox":None,"enforceManagedNetwork":False})
  seq=None;stdout=bytearray();stderr=bytearray();seen=set()
  try:
   for _ in range(20):
    result=await remote_call("process/read",{"processId":handle,"afterSeq":seq,"maxBytes":65536,"waitMs":500})
    chunks=result["chunks"]+[n['params'] for n in probe_notifications.get(handle,[]) if n.get('method')=='process/output']
    for chunk in chunks:
     if chunk['seq'] in seen:continue
     seen.add(chunk['seq']);target=stdout if chunk["stream"]=="stdout" else stderr;target.extend(base64.b64decode(chunk["chunk"]))
    seq=result["nextSeq"]
    if result["exited"] and result["closed"]:return {"exitCode":result["exitCode"],"stdout":stdout.decode(),"stderr":stderr.decode()}
   raise RuntimeError("fixed remote probe exceeded limit")
  finally:
   await remote_call("process/terminate",{"processId":handle})
   probe_process_ids.discard(handle);probe_notifications.pop(handle,None)
 async def proxy_bridge(r,w):
  sh='exec 3<>/dev/tcp/127.0.0.1/8080; cat <&3 & R=$!; trap \'kill "$R" 2>/dev/null || true\' EXIT; cat >&3'
  child=await asyncio.create_subprocess_exec(*D,'exec','-i',PNAME,'bash','-c',sh,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL);children.add(child)
  async def into():
   while data:=await r.read(65536):child.stdin.write(data);await child.stdin.drain()
  async def back():
   while data:=await child.stdout.read(65536):w.write(data);await w.drain()
  ts=[asyncio.create_task(into()),asyncio.create_task(back())]
  try:await asyncio.wait(ts,return_when=asyncio.FIRST_COMPLETED)
  finally:
   for t in ts:t.cancel()
   w.close()
   if child.stdin and not child.stdin.is_closing():child.stdin.close()
   try:await asyncio.wait_for(child.wait(),5)
   except asyncio.TimeoutError:child.terminate();await child.wait()
   children.discard(child)
 async def send(obj):
  async with native_lock:
   proc.stdin.write((json.dumps(obj)+'\n').encode());await proc.stdin.drain()
 async def read_native():
  nonlocal login_success,transient_login,deadline_task
  try:
   while raw:=await proc.stdout.readline():
    obj=json.loads(raw);ident=obj.get('id');method=obj.get('method','')
    if ident in pending and 'method' not in obj:
     pending[ident].set_result(obj);continue
    if method=='account/login/completed':
     params=obj.get('params',{});login_success=bool(params.get('success'))
     error=sanitized_error(params.get('error')) if params.get('error') is not None else None
     report['official_login_error']=error;report['login_completed_notification']=login_success
     report['status']='LOGIN_COMPLETED_VERIFY_ACCOUNT' if login_success else 'LOGIN_FAILED';transient_login=None
     save();note({'login_completed':login_success,'official_error':error})
    elif method=='account/updated':
     report['account_auth_mode']=obj.get('params',{}).get('authMode');save()
    elif method=='turn/completed':
     p=obj.get('params',{});t=p.get('turn',{});owned_turns[p.get('threadId')]=t.get('id');events.append({'method':method,'threadId':p.get('threadId'),'turnId':t.get('id'),'status':t.get('status'),'error_code':(t.get('error')or{}).get('code')});report['last_turn_status']=t.get('status');report['last_turn_error']=sanitized_error((t.get('error')or{}).get('message')) if t.get('error') else None
     if deadline_task:deadline_task.cancel();deadline_task=None
     report['status']='MODEL_TURN_TERMINAL';save();note({'model_turn_terminal':t.get('status'),'task':C['task'],'error':report['last_turn_error']})
    elif method=='item/completed':
     item=obj.get('params',{}).get('item',{})
     if item.get('type')=='commandExecution':
      report['model_tool_completions']=report.get('model_tool_completions',0)+1;report['latest_model_tool_exit_code']=item.get('exitCode');save()
     elif item.get('type')=='agentMessage':
      text=item.get('text','')
      if isinstance(text,str):report['last_model_message']=text;save()
    elif method=='thread/tokenUsage/updated':
     events.append({'method':method,'params':obj.get('params',{})})
    elif ident is not None and method:
     # Approval requests only; no automatic approval or external-auth refresh.
     if 'requestApproval' in method:approval_requests[ident]=obj;events.append({'approval_request':obj})
    # Reasoning, account values, config diagnostics and raw deltas never printed/saved.
   for fut in list(pending.values()):
    if not fut.done():fut.set_exception(RuntimeError('native app-server exited'))
   stop.set()
  except Exception:
   for fut in list(pending.values()):
    if not fut.done():fut.set_exception(RuntimeError('native protocol reader failed'))
   stop.set()
 async def drain_stderr():
  # Drain without logging raw auth/login diagnostics or credential values.
  while await proc.stderr.read(65536):pass
 async def call(method,params=None,allow_error=False,timeout=40):
  nonlocal counter
  counter+=1;ident=counter;fut=asyncio.get_running_loop().create_future();pending[ident]=fut
  try:
   await send({'id':ident,'method':method,'params':params or {}})
   obj=await asyncio.wait_for(asyncio.shield(fut),timeout)
   if 'error'in obj and not allow_error:
    detail=': '+obj['error'].get('message','')[:200] if report['login_requests']==0 and method in ['command/exec','fs/readFile','fs/writeFile','fs/remove'] else ''
    if method.startswith('account/login/'):
     detail=': '+sanitized_error(obj['error'].get('message',''));report['official_login_rpc_error']={'method':method,'code':obj['error'].get('code'),'message':detail};save()
    raise RuntimeError('native '+method+' rejected '+str(obj['error'].get('code'))+detail)
   return obj
  finally:pending.pop(ident,None)
 async def enforce_deadline(thread_id,turn_id):
  try:
   await asyncio.sleep(C['agent_timeout_sec']);report['deadline_interrupt_requested']=True;save()
   await call('turn/interrupt',{'threadId':thread_id,'turnId':turn_id},allow_error=True)
  except asyncio.CancelledError:pass
 async def code_mode_preflight():
  binary=C['host_companion']
  child=await asyncio.create_subprocess_exec(binary,'--listen','stdio',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env={'PATH':'/usr/bin:/bin'});children.add(child)
  proof={'model_calls':0,'host_companion':binary,'host_companion_sha256':hashlib.sha256(Path(binary).read_bytes()).hexdigest(),'same_official_docker_connection':True,'delegations':0}
  async def write(obj):
   data=json.dumps(obj).encode();child.stdin.write(len(data).to_bytes(4,'little')+data);await child.stdin.drain()
  async def read():
   size=int.from_bytes(await asyncio.wait_for(child.stdout.readexactly(4),15),'little');assert size<1048576
   return json.loads(await asyncio.wait_for(child.stdout.readexactly(size),15))
  try:
   await write({'type':'connection/hello','supportedVersions':[1],'requiredCapabilities':[],'optionalCapabilities':[]});assert (await read())['type']=='connection/ready'
   await write({'type':'operation/request','id':1,'request':{'method':'session/open','sessionId':'dradar-fixed-remote-preflight'}});assert (await read())['result']['status']=='ok'
   definition={'name':'exec_command','tool_name':{'name':'exec_command','namespace':'functions'},'description':'Fixed no-model Linux probe only','kind':'function','input_schema':{'type':'object','properties':{'cmd':{'const':'dradar-fixed-linux-probe'}},'required':['cmd'],'additionalProperties':False},'output_schema':None}
   await write({'type':'operation/request','id':2,'request':{'method':'session/execute','sessionId':'dradar-fixed-remote-preflight','request':{'tool_call_id':'fixed-remote-preflight','enabled_tools':[definition],'source':'const r=await tools.exec_command({cmd:"dradar-fixed-linux-probe"});text(r.stdout)','yield_time_ms':10000,'max_output_tokens':500}}})
   final=None
   for _ in range(12):
    obj=await read()
    if obj['type']=='delegate/request':
     request=obj['request'];assert request['type']=='tool/invoke';inv=request['invocation'];assert inv['tool_name']=={'name':'exec_command','namespace':'functions'} and inv['input']=={'cmd':'dradar-fixed-linux-probe'}
     result=await remote_exec(['python','-c',"import json,os,platform;from pathlib import Path;print(json.dumps({'platform':platform.system(),'uid':os.getuid(),'memory_max':Path('/sys/fs/cgroup/memory.max').read_text().strip()}))"])
     assert result['exitCode']==0;proof['delegations']+=1
     await write({'type':'delegate/response','id':obj['id'],'result':{'status':'ok','value':{'type':'tool/result','result':result}}})
    elif obj['type']=='execute/initialResponse':
     assert obj['result']['status']=='ok';value=obj['result']['value'];assert 'Result' in value and not value['Result'].get('error_text'),value
     final=value['Result'];break
   assert final is not None and proof['delegations']==1
   texts=[x['text'] for x in final['content_items'] if x['type']=='input_text'];actual=json.loads(''.join(texts));assert actual=={'platform':'Linux','uid':1000,'memory_max':str(C['memory_bytes'])}
   proof.update(actual_remote_result=actual,javascript_to_remote_tool_roundtrip_verified=True)
   await write({'type':'operation/request','id':3,'request':{'method':'session/shutdown','sessionId':'dradar-fixed-remote-preflight'}})
   for _ in range(8):
    obj=await read()
    if obj.get('id')==3:assert obj['result']['status']=='ok';break
   return proof
  finally:
   child.stdin.close()
   try:await asyncio.wait_for(child.wait(),5)
   except asyncio.TimeoutError:child.terminate();await child.wait()
   proof.update(companion_exit_code=child.returncode,companion_reaped=True);children.discard(child)
 async def account_type():
  result=(await call('account/read',{'refreshToken':False}))['result'];atype=(result.get('account')or{}).get('type');del result;return atype
 async def control(r,w):
  nonlocal transient_login,login_once,authorized,turn_used,login_success,deadline_task
  try:
   req=json.loads(await asyncio.wait_for(r.readline(),5))
   async with owner_lock:
    op=req.get('op')
    if op=='status':ans={k:v for k,v in report.items() if k not in ['remote_info']}
    elif op=='login:start':
     if login_once:raise RuntimeError('single device login already requested; no duplicate')
     if report['status']!='NO_MODEL_REMOTE_ONLY_READY':raise RuntimeError('runtime not ready for login')
     if await account_type() is not None:raise RuntimeError('dedicated identity already present; do not replace')
     login_once=True;report['login_requests']=1;save()
     result=(await call('account/login/start',{'type':'chatgptDeviceCode'}))['result']
     if result.get('type')!='chatgptDeviceCode' or urlparse(result.get('verificationUrl','')).hostname!='auth.openai.com':raise RuntimeError('unexpected official device flow')
     transient_login={k:result[k] for k in ['loginId','userCode','verificationUrl']};del result
     report['status']='WAITING_FOR_USER_DEVICE_LOGIN';save();ans={'device_instructions_ready':True,'model_calls':0};note(ans)
    elif op=='login:retry-authorized':
     if req.get('user_requested_new_code') is not True:raise RuntimeError('explicit current user request for replacement code required')
     if await account_type() is not None:raise RuntimeError('dedicated identity already authenticated; do not replace')
     if report['status'] not in ['WAITING_FOR_USER_DEVICE_LOGIN','LOGIN_FAILED','LOGIN_CANCELED']:raise RuntimeError('replacement outside existing login flow')
     if transient_login:
      result=await call('account/login/cancel',{'loginId':transient_login['loginId']},allow_error=True)
      if 'error' in result:raise RuntimeError('official cancellation failed; do not generate another code')
      transient_login=None
     login_once=False;login_success=False;report['login_attempt']=report.get('login_attempt',1)+1
     report['prior_official_login_error']=report.get('official_login_error');report['official_login_error']=None
     report['status']='NO_MODEL_REMOTE_ONLY_READY';save();ans={'prior_flow_ended':True,'ready_for_one_replacement':True,'model_calls':0}
    elif op=='login:instructions':
     if not transient_login:raise RuntimeError('no pending transient login instructions')
     ans=dict(transient_login)
    elif op=='login:cancel':
     if transient_login:await call('account/login/cancel',{'loginId':transient_login['loginId']});transient_login=None
     report['status']='LOGIN_CANCELED';save();ans={'login_canceled':True}
    elif op=='account:status':
     atype=await account_type();report['host_account_type']=atype
     if atype=='chatgpt':report['status']='AUTHENTICATED_REMOTE_ONLY_READY';login_success=True
     save();ans={'account_type':atype,'login_completed':login_success,'model_calls':report['model_calls']}
    elif op=='events':ans={'events':events[:]};events.clear()
    elif op=='stop':
     if turn_used and report.get('last_turn_status') not in ['completed','failed','interrupted']:raise RuntimeError('interrupt exact owned turn and collect before stop')
     if transient_login:
      await call('account/login/cancel',{'loginId':transient_login['loginId']},allow_error=True);transient_login=None
     ans={'stopping':True,'logout_performed':False};stop.set()
    elif op=='authorize-one-turn':
     if report['status']!='AUTHENTICATED_REMOTE_ONLY_READY' or turn_used:raise RuntimeError('single turn unavailable')
     if req.get('start_barrier_committed') is not True:raise RuntimeError('durable V2 start barrier required')
     from dradar.v2.journal import Journal
     fence=Journal(Path(C['journal_root'])).execution(C['assignment_id'])
     if not fence or fence['execution_id']!=C['execution_id']:raise RuntimeError('owned durable V2 execution fence missing')
     authorized=True;ans={'single_turn_latched':True}
    elif op=='rpc':
     method=req.get('method');params=req.get('params')or{}
     if method not in ['thread/start','thread/read','turn/start','turn/interrupt']:raise RuntimeError('outside owned runtime interface')
     if method=='thread/start':
      params.update(model='gpt-6.1-sol',modelProvider='host-native-chatgpt',approvalPolicy='on-request',approvalsReviewer='user',ephemeral=True,allowProviderModelFallback=False,cwd='/app',runtimeWorkspaceRoots=['/app'],environments=[{'environmentId':'remote','cwd':'/app','runtimeWorkspaceRoots':['/app']}])
      params.setdefault('config',{}).update(model_reasoning_effort=C['effort'],web_search='disabled')
     if method in ['thread/read','turn/start','turn/interrupt'] and params.get('threadId')not in owned_threads:raise RuntimeError('not owned thread')
     if method=='turn/start':
      if not authorized or turn_used:raise RuntimeError('fresh single-turn parent dispatch required')
      turn_used=True;report['model_calls']=1;save();params.update(model='gpt-6.1-sol',effort=C['effort'],approvalPolicy='on-request',sandboxPolicy={'type':'externalSandbox','networkAccess':'restricted'},environments=[{'environmentId':'remote','cwd':'/app','runtimeWorkspaceRoots':['/app']}])
     ans=await call(method,params,allow_error=True,timeout=300 if method=='command/exec' else 40)
     if method=='turn/start':
      if 'result' in ans:
       turn=ans['result']['turn'];owned_turns[params['threadId']]=turn['id'];report.update(status='ONE_MODEL_TURN_STARTED',model_thread_id=params['threadId'],model_turn_id=turn['id'],model_turn_start_ack=True,model_started_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),model='gpt-6.1-sol',reasoning=C['effort'],agent_timeout_sec=C['agent_timeout_sec'],input_sha256=hashlib.sha256(json.dumps(params.get('input',[]),sort_keys=True).encode()).hexdigest());save()
       if turn.get('status') not in ['completed','failed','interrupted']:deadline_task=asyncio.create_task(enforce_deadline(params['threadId'],turn['id']))
       note({'model_turn_start_ack':True,'task':C['task'],'thread_id':params['threadId'],'turn_id':turn['id'],'model':'gpt-6.1-sol','reasoning':C['effort'],'agent_timeout_sec':C['agent_timeout_sec']})
      else:report['turn_start_error']=sanitized_error(ans.get('error'));report['status']='MODEL_TURN_START_REJECTED';save()
     if method=='thread/start' and 'result'in ans:owned_threads.add(ans['result']['thread']['id'])
    elif op=='collect':
     if report.get('last_turn_status') not in ['completed','interrupted','failed']:raise RuntimeError('owned model turn must be terminal before collection')
     code=Path(C['collector_path']).read_bytes()
     if hashlib.sha256(code).hexdigest()!=C['collector_sha256']:raise RuntimeError('fixed public collector hash mismatch')
     result=await remote_exec(['python','-c',code.decode()])
     if result['exitCode']!=0:raise RuntimeError('public collector failed: '+result['stderr'][:400])
     collected=json.loads(result['stdout']);patch=base64.b64decode(collected.pop('patch_base64'))
     if hashlib.sha256(patch).hexdigest()!=collected['patch_sha256']:raise RuntimeError('collected patch hash mismatch')
     destination=OUT/'collected';destination.mkdir(exist_ok=True);(destination/'model.patch').write_bytes(patch)
     for name,data in collected.pop('deliverables_base64',{}).items():
      if name not in C['deliverable_names']:raise RuntimeError('unexpected deliverable')
      (destination/name).write_bytes(base64.b64decode(data))
     collected.update(task=C['task'],container_id=report['task_container_id'],model_turn_status=report['last_turn_status'],model_turn_error=report.get('last_turn_error'),model='gpt-6.1-sol',reasoning=C['effort'],agent_timeout_sec=C['agent_timeout_sec'],official_task_timeout_sec=C['official_task_timeout_sec'],verifier_timeout_sec=C['verifier_timeout_sec'],grading_done=False)
     (destination/'COLLECTION.json').write_text(json.dumps(collected,indent=2)+'\n');report['collected_manifest']=str(destination/'COLLECTION.json');save();ans=collected
    elif op=='approval:respond':
     obj=req['response']
     if obj.get('id') not in approval_requests or 'method'in obj or obj.get('result',{}).get('decision') not in ['accept','decline']:raise RuntimeError('exact outstanding approval and single-use decision required')
     approval_requests.pop(obj['id'])
     await send(obj);ans={'response_sent':True}
    else:raise RuntimeError('unknown owner operation')
   w.write((json.dumps(ans)+'\n').encode());await w.drain()
  except Exception as e:
   # Owner errors contain only local fixed strings or native method/code.
   w.write((json.dumps({'error':sanitized_error(e)[:180]})+'\n').encode());await w.drain()
  finally:w.close()
 try:
  assert HOME==Path.home()/'.local/share/dradar/codex-host-home' and HOME.resolve(strict=True)==HOME and not HOME.is_symlink()
  if (HOME/'auth.json').exists():raise RuntimeError('dedicated host file credential store forbidden')
  for key in ['host_cli','host_companion']:
   if hashlib.sha256(Path(C[key]).read_bytes()).hexdigest()!=C[key+'_sha256']:raise RuntimeError('official host binary binding mismatch')
  assert HOME.stat().st_uid==os.getuid() and stat.S_IMODE(HOME.stat().st_mode)==0o700
  version_probe=subprocess.run([C['host_cli'],'--version'],env={'PATH':'/usr/bin:/bin','HOME':str(Path.home()),'CODEX_HOME':str(HOME)},capture_output=True,text=True,timeout=10)
  validate_host_version(version_probe.returncode,version_probe.stdout);report['actual_host_cli_version']=version_probe.stdout.strip()
  conf=tomllib.loads((HOME/'config.toml').read_text());assert conf['cli_auth_credentials_store']=='keyring' and conf['features']['secret_auth_storage'] is False
  if (HOME/'environments.toml').exists():raise RuntimeError('unexpected dedicated environments config')
  fd=os.open(C['lock_path'],os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  if SOCK.exists():raise RuntimeError('owned control socket already exists; reconcile')
  for n in [NAME,PNAME]:
   if subprocess.run(D+['inspect',n],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:raise RuntimeError('exact owned container already exists')
  tid=docker(['create','--name',NAME,'--label','dradar.experiment=018-dedicated-host-auth','--label','dradar.task='+C['task'],'--pull','never','--network','none','--cpus',str(C['cpus']),'--memory',str(C['memory_bytes']),'--memory-swap',str(C['memory_bytes']),'--pids-limit','128','--user','1000','--cap-drop','ALL','--security-opt','no-new-privileges:true','--workdir','/app','--env','CODEX_HOME=/tmp/credential-free-executor','--entrypoint','sh',C['image_id'],'-c','mkdir -p /tmp/credential-free-executor;chmod 700 /tmp/credential-free-executor;exec sleep infinity']).decode().strip();created.append(tid);docker(['start',tid]);task=json.loads(docker(['inspect',tid]))[0]
  assert task['Image']==C['image_id'] and not task['Mounts'] and list(task['NetworkSettings']['Networks'])==['none'] and task['Config']['User']=='1000'
  assert task['HostConfig']['Memory']==C['memory_bytes'] and task['HostConfig']['NanoCpus']==C['cpus']*1000000000 and not task['HostConfig']['Privileged']
  report['task_container_id']=tid;report['task_boundary']={'network':'none','mounts':[],'memory_bytes':C['memory_bytes'],'cpus':C['cpus'],'gpu':0,'uid':1000,'cap_drop':['ALL'],'no_new_privileges':True}
  token=secrets.token_urlsafe(32)
  pid=docker(['create','--name',PNAME,'--label','dradar.experiment=018-dedicated-host-auth','--pull','never','--network','bridge','--cpus','0.25','--memory','256m','--memory-swap','256m','--pids-limit','64','--cap-drop','ALL','--security-opt','no-new-privileges:true','--entrypoint','sh',PROXY,'/tmp/start-018.sh']).decode().strip();created.append(pid)
  boot='#!/bin/sh\nset -eu\numask 077\nprintf "%s\\n" '+repr(token)+' | htpasswd -ci /tmp/squid.passwd agent >/dev/null\nexec squid -N -f /tmp/policy.conf\n';buf=io.BytesIO()
  with tarfile.open(fileobj=buf,mode='w')as tar:
   for n,d in [('policy.conf',POLICY),('start-018.sh',boot)]:
    b=d.encode();t=tarfile.TarInfo(n);t.size=len(b);t.mode=0o600;t.uid=13;t.gid=13;tar.addfile(t,io.BytesIO(b))
  docker(['cp','-a','-',pid+':/tmp'],input=buf.getvalue());docker(['start',pid])
  for _ in range(30):
   if subprocess.run(D+['exec',pid,'sh','-c','test -f /tmp/squid.pid'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:break
   await asyncio.sleep(.2)
  else:raise RuntimeError('official egress relay failed')
  ws=await websockets.serve(ws_bridge,'127.0.0.1',0,max_size=8388608);servers.append(ws);remote='ws://127.0.0.1:'+str(ws.sockets[0].getsockname()[1])
  tcp=await asyncio.start_server(proxy_bridge,'127.0.0.1',0);servers.append(tcp);port=tcp.sockets[0].getsockname()[1]
  async def check_connect(host,validate_tls=False):
   r,w=await asyncio.open_connection('127.0.0.1',port)
   credential=base64.b64encode(('agent:'+token).encode()).decode()
   w.write(('CONNECT '+host+':443 HTTP/1.1\r\nHost: '+host+':443\r\nProxy-Authorization: Basic '+credential+'\r\n\r\n').encode());await w.drain()
   head=await asyncio.wait_for(r.readuntil(b'\r\n\r\n'),15);code=int(head.split(b' ',2)[1])
   if validate_tls and code==200:await asyncio.wait_for(w.start_tls(ssl.create_default_context(),server_hostname=host),15)
   w.close()
   try:await asyncio.wait_for(w.wait_closed(),2)
   except (asyncio.TimeoutError,ssl.SSLError):w.transport.abort()
   return code
  denied=await check_connect('example.com');literal=await check_connect('1.1.1.1');allowed=await check_connect('auth.openai.com',True);model_endpoint=await check_connect('chatgpt.com',True)
  assert denied==403 and literal==403 and allowed==200 and model_endpoint==200
  report['official_egress_probe']={'example_com_connect':denied,'literal_ip_connect':literal,'auth_openai_com_connect':allowed,'auth_tls_validated':True,'chatgpt_connect':model_endpoint,'chatgpt_tls_validated':True,'domains':['auth.openai.com','chatgpt.com'],'port':443,'policy_sha256':hashlib.sha256(POLICY.encode()).hexdigest()}
  report['status']='NATIVE_INITIALIZING';save();note({'preflight_stage':'official_egress_verified','model_calls':0})
  proxy_url='http://agent:'+token+'@127.0.0.1:'+str(port)
  env={k:os.environ[k]for k in ['PATH','USER','LANG','TMPDIR']if k in os.environ};env.update(HOME=str(Path.home()),CODEX_HOME=str(HOME),CODEX_EXEC_SERVER_URL=remote,HTTPS_PROXY=proxy_url,HTTP_PROXY=proxy_url,NO_PROXY='localhost,127.0.0.1')
  overrides=['cli_auth_credentials_store="keyring"','features.secret_auth_storage=false','notify=[]','web_search="disabled"','features.apps=false','features.memories=false','features.multi_agent=false','features.multi_agent_v2=false','model_reasoning_summary="none"','check_for_update_on_startup=false','model_provider="host-native-chatgpt"','model_providers.host-native-chatgpt.name="DRadar native host ChatGPT"','model_providers.host-native-chatgpt.base_url="https://chatgpt.com/backend-api/codex"','model_providers.host-native-chatgpt.wire_api="responses"','model_providers.host-native-chatgpt.requires_openai_auth=true','model_providers.host-native-chatgpt.supports_websockets=false','model_providers.host-native-chatgpt.request_max_retries=0','model_providers.host-native-chatgpt.stream_max_retries=0']
  args=[C['host_cli'],'app-server','--listen','stdio://']
  for v in overrides:args+=['-c',v]
  proc=await asyncio.create_subprocess_exec(*args,cwd=str(HOME/'runner'),env=env,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,limit=8388608)
  report['native_app_server_pid']=proc.pid;reader_task=asyncio.create_task(read_native());stderr_task=asyncio.create_task(drain_stderr())
  await call('initialize',{'clientInfo':{'name':'dradar-dedicated-host-auth','version':'1'},'capabilities':{'experimentalApi':True}});await send({'method':'initialized','params':{}})
  report['host_account_type']=await account_type()
  if report['host_account_type']not in [None,'chatgpt']:raise RuntimeError('unexpected dedicated auth type')
  local=await call('environment/info',{'environmentId':'local'},allow_error=True)
  if 'error'not in local:raise RuntimeError('Mac local executor still exists')
  report['local_executor_unavailable']={'confirmed':True,'code':local['error']['code'],'reason':local['error'].get('message','')[:160]}
  remote_result=await call('environment/info',{'environmentId':'remote'},allow_error=True)
  if 'error' in remote_result:raise RuntimeError('remote environment: '+remote_result['error'].get('message','')[:200])
  report['remote_info']=remote_result['result']
  probe="import hashlib,json,os,platform,shutil,subprocess;from pathlib import Path;expected="+repr(C['public_files'])+";assert all(hashlib.sha256(Path(k).read_bytes()).hexdigest()==v for k,v in expected.items());p=Path('/sys/fs/cgroup');print(json.dumps({'uid':os.getuid(),'platform':platform.system(),'hostname':platform.node(),'memory_max':(p/'memory.max').read_text().strip(),'memory_events':(p/'memory.events').read_text().splitlines(),'public_files_verified':len(expected),'git_head':subprocess.check_output(['git','-C','/app','rev-parse','HEAD'],text=True).strip(),'git_status':subprocess.check_output(['git','-C','/app','status','--porcelain'],text=True).strip(),'codex_path':shutil.which('codex'),'codex_version':subprocess.check_output(['codex','--version'],text=True).strip(),'executor_auth_file_exists':Path('/tmp/credential-free-executor/auth.json').exists()}))"
  result=await remote_exec(['python','-c',probe])
  if result['exitCode']!=0:
   report['fixed_public_probe_stderr']=result['stderr'][:1400];save();raise RuntimeError('remote fixed Linux probe failed exit '+str(result['exitCode']))
  report['fixed_probe_stdout']=result['stdout'][:1800];save();runtime=json.loads(result['stdout']);assert runtime['platform']=='Linux' and runtime['uid']==1000 and runtime['memory_max']==str(C['memory_bytes']) and tid.startswith(runtime['hostname']) and not runtime['executor_auth_file_exists']
  if runtime['codex_version']!='codex-cli 0.160.0' or runtime['git_head']!=C['git_head'] or runtime['git_status']:raise RuntimeError('fixed image public baseline mismatch')
  report['runtime_preflight']=runtime
  marker=secrets.token_hex(16);path='file:///app/.dradar-018-remote-route-probe'
  await remote_call('fs/writeFile',{'path':path,'dataBase64':base64.b64encode(marker.encode()).decode(),'sandbox':None,'followSymlinks':False})
  read=await remote_call('fs/readFile',{'path':path,'sandbox':None,'followSymlinks':False});assert base64.b64decode(read['dataBase64']).decode()==marker
  await remote_call('fs/writeFile',{'path':path,'dataBase64':base64.b64encode((marker+'-write').encode()).decode(),'sandbox':None,'followSymlinks':False})
  verify=await remote_exec(['python','-c',"from pathlib import Path;assert Path('/app/.dradar-018-remote-route-probe').read_text()=="+repr(marker+'-write')]);assert verify['exitCode']==0
  await remote_call('fs/remove',{'path':path,'force':False,'recursive':False,'sandbox':None,'followSymlinks':False})
  verify=await remote_exec(['python','-c',"from pathlib import Path;assert not Path('/app/.dradar-018-remote-route-probe').exists()"]);assert verify['exitCode']==0
  thread=(await call('thread/start',{'model':'gpt-6.1-sol','modelProvider':'host-native-chatgpt','approvalPolicy':'on-request','approvalsReviewer':'user','ephemeral':True,'allowProviderModelFallback':False,'cwd':'/app','runtimeWorkspaceRoots':['/app'],'environments':[{'environmentId':'remote','cwd':'/app','runtimeWorkspaceRoots':['/app']}],'config':{'model_reasoning_effort':C['effort'],'web_search':'disabled'}}))['result']
  assert thread['approvalPolicy']=='on-request' and thread['approvalsReviewer']=='user'
  assert thread['thread']['environments']==[{'environmentId':'remote','cwd':'/app','runtimeWorkspaceRoots':['/app']}]
  owned_threads.add(thread['thread']['id']);report['no_model_thread_preflight']={'thread_id':thread['thread']['id'],'model':thread['model'],'environments':thread['thread']['environments'],'approvalPolicy':thread['approvalPolicy'],'approvalsReviewer':thread['approvalsReviewer'],'ephemeral':True,'turn_started':False}
  report['code_mode_preflight']=await code_mode_preflight();save()
  report.update(status='NO_MODEL_REMOTE_ONLY_READY',filesystem_remote_read_write_remove_verified=True,local_fallback=False,control_socket=str(SOCK),executor_env_filtered=True,controller_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),created_container_ids=created)
  save();control_server=await asyncio.start_unix_server(control,path=str(SOCK),limit=8388608);SOCK.chmod(0o600);servers.append(control_server)
  note({'status':report['status'],'model_calls':0,'login_requests':0,'local_executor_available':False,'remote_filesystem_verified':True})
  await stop.wait()
 except Exception as e:
  report.update(status='BLOCKED',error_type=type(e).__name__,error=sanitized_error(e));save();note({'status':'BLOCKED','error':report['error']})
 finally:
  if deadline_task:deadline_task.cancel()
  if proc:
   if proc.stdin and not proc.stdin.is_closing():proc.stdin.close()
   try:await asyncio.wait_for(proc.wait(),10)
   except asyncio.TimeoutError:proc.terminate();await proc.wait()
   report['native_app_server_exit_code']=proc.returncode;report['native_app_server_reaped']=True
  for s in servers:s.close()
  for s in servers:await s.wait_closed()
  for child in list(children):
   if child.returncode is None:child.terminate()
   await child.wait()
  for task in [reader_task,stderr_task]:
   if task:task.cancel()
  preserve=bool(turn_used and not report.get('collected_manifest'));retained=[]
  for cid in reversed(created):
   if preserve and cid==report.get('task_container_id'):
    subprocess.run(D+['stop','-t','5',cid],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);retained.append(cid)
   else:subprocess.run(D+['rm','-f',cid],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  report['retained_uncollected_container_ids']=retained
  report['cleanup_exact_container_ids']=created
  daemon=subprocess.run(D+['info','--format','{{.ServerVersion}}'],capture_output=True)
  absence=[]
  for cid in created:
   result=subprocess.run(D+['inspect',cid],capture_output=True)
   absence.append(confirmed_absence(daemon.returncode,daemon.stdout,result.returncode,result.stderr,cid))
  report['created_containers_absent']=daemon.returncode==0 and bool(daemon.stdout.strip()) and all(absence)
  if SOCK.exists() and SOCK.is_socket():SOCK.unlink()
  if fd is not None:fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
  report['controller_cleanup_completed']=True;save();note({'controller_cleanup_completed':True,'native_child_reaped':report.get('native_app_server_reaped',False),'created_containers_absent':report['created_containers_absent'],'logout_performed':False})
asyncio.run(main())
