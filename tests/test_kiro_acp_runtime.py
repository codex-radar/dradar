"""Offline ACP contract tests: exact config, tool permission and cancellation."""

from __future__ import annotations

import json
import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


RUNTIME = Path(__file__).parents[1] / "src/dradar/kiro_acp_runtime.py"
MODEL = "claude-opus-5.5"
NATIVE_PROMPT = ("In the empty local /app directory, use the terminal to run pwd once, "
                 "then reply exactly OK. Do not inspect files, access the network, "
                 "or write anything.")


def _fake_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake-kiro"
    script.write_text("#!" + sys.executable + "\n" + r'''
import json,os,sys
from pathlib import Path
mode=os.environ['FAKE_ACP_MODE']
trace=Path(os.environ['FAKE_ACP_TRACE'])
model='auto';effort='medium';pending=None;permission_reply_count=0
def send(value):
    print(json.dumps({'jsonrpc':'2.0',**value}),flush=True)
def options():
    return [{'id':'model','currentValue':model,'type':'select',
             'options':[{'value':'auto'},{'value':'claude-opus-5.5'}]},
            {'id':'effortLevel','currentValue':effort,'type':'select',
             'options':[{'value':x} for x in ('low','medium','high','xhigh','max')]}]
def record(value):
    with trace.open('a') as out:out.write(json.dumps(value)+'\n')
def lifecycle():
    sid='foreign' if mode=='lifecycle_foreign' else 'sess_test'
    hooks={'sessionId':sid,'hooks':[]};tools={'sessionId':sid,'tags':[]}
    roster={'upserted':[{'sessionId':sid,'status':'idle'}],'deleted':[]}
    info={'kind':'context_usage','usagePercentage':0,'contextUsage':{'usagePercentage':0}}
    if mode=='lifecycle_hooks_shape':hooks['hooks']={}
    if mode=='lifecycle_tools_shape':tools['tags']='bad'
    if mode=='lifecycle_error':hooks['error']='SYNTHETIC_FAILURE'
    if mode=='lifecycle_failed_status':hooks['status']='failed'
    if mode=='lifecycle_roster_failed':roster['upserted'][0]['status']='failed'
    if mode=='lifecycle_roster_deleted':roster['deleted']=[sid]
    if mode=='lifecycle_roster_failure':roster['upserted'][0]['provisioningFailure']={'code':'backend'}
    if mode=='lifecycle_context_error':info={'kind':'display_error','displayError':{'message':'SYNTHETIC_FAILURE'}}
    if mode=='lifecycle_context_hidden_error':info['displayError']={'message':'SYNTHETIC_FAILURE'}
    if mode=='lifecycle_context_invalid':info['usagePercentage']=float('nan')
    if mode=='lifecycle_context_mismatch':info['contextUsage']['usagePercentage']=1
    for method,payload in [('_kiro/hooks/didChange',hooks),('_kiro/tools/didChange',tools),('_kiro/sessions/changed',roster)]:
        send({'method':method,'params':payload})
    send({'method':'session/update','params':{'sessionId':sid,'update':{'sessionUpdate':'session_info_update','_meta':{'kiro':info}}}})
for line in sys.stdin:
    msg=json.loads(line);method=msg.get('method');params=msg.get('params',{})
    record({'method':method,'params':params if method!='session/prompt' else {'sessionId':params.get('sessionId'),'prompt':params.get('prompt')},
            'outcome':msg.get('result',{}).get('outcome') if msg.get('id')==99 else None})
    if method=='initialize':
        if mode=='startup_exit':sys.exit(1)
        if mode=='startup_invalid_json':
            print('SYNTHETIC_SECRET_NOT_JSON',flush=True)
            continue
        send({'id':msg['id'],'result':{'protocolVersion':1,'agentCapabilities':{}}})
    elif method=='session/new':
        if mode=='startup_surrogate_method':
            send({'method':'_unknown/'+chr(0xd800),'params':{}})
            continue
        if mode=='startup_surrogate_kind':
            send({'method':'session/update','params':{'sessionId':'sess_test','update':{'sessionUpdate':chr(0xd800)}}})
            continue
        if mode.startswith('early_config_'):
            initial=options()
            if mode=='early_config_duplicate':initial.append(initial[0])
            if mode=='early_config_shape':initial='bad'
            send({'method':'session/update','params':{
                'sessionId':'foreign' if mode=='early_config_foreign' else 'sess_test',
                'update':{'sessionUpdate':'config_option_update','configOptions':initial}}})
        if mode.startswith('lifecycle_'):lifecycle()
        if mode.startswith('passive_'):
            # Actual approved diagnostic method order; params below are a
            # synthetic success fixture, not a retained provider payload.
            for notice in ('_kiro/governance/state','_kiro/mcp/status',
                           '_kiro/powers/items_changed','_kiro/steering/documents_changed',
                           '_kiro/progressive_context/items_changed'):
                payload={'sessionId':'foreign' if mode in ('passive_foreign','passive_foreign_deferred') else 'sess_test'}
                if notice=='_kiro/governance/state':payload.update(isEnterprise=False,features={})
                if notice=='_kiro/mcp/status':payload['servers']=[]
                if notice=='_kiro/powers/items_changed':payload.update(status='success',powers=[])
                if notice=='_kiro/steering/documents_changed':payload.update(status='success',documents=[])
                if notice=='_kiro/progressive_context/items_changed':payload.update(status='success',items=[])
                if mode=='passive_progressive_failed' and 'items' in payload:payload.update(status='failed',error='SYNTHETIC_FAILURE')
                if mode=='passive_progressive_shape' and 'items' in payload:payload['items']={}
                if mode=='passive_progressive_errors' and 'items' in payload:payload['errors']=['SYNTHETIC_FAILURE']
                if mode=='passive_powers_failed' and 'powers' in payload:payload.update(status='failed',error='SYNTHETIC_FAILURE')
                if mode=='passive_powers_errors' and 'powers' in payload:payload['errors']=['SYNTHETIC_FAILURE']
                if mode=='passive_documents_failed' and 'documents' in payload:payload.update(status='failed',error='SYNTHETIC_FAILURE')
                if mode=='passive_governance_failed' and 'features' in payload:payload['disabledReason']='api_failure'
                if mode=='passive_governance_admin' and 'features' in payload:payload['disabledReason']='admin_disabled'
                send({'method':notice,'params':payload})
            if mode=='passive_request':
                send({'id':88,'method':'_kiro/mcp/status','params':{}})
                continue
            if mode=='passive_error':send({'method':'_kiro/policy/error','params':{}})
            # Separate standard-protocol fixture. The real failing core
            # update kind is unknown and is NOT claimed to be this event.
            if mode!='passive_foreign_deferred':
                send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                    'sessionUpdate':'available_commands_update',
                    'availableCommands':[] if mode!='passive_bad_commands' else 'bad'}}})
        if mode=='handshake_pre_session_commands':
            send({'method':'_kiro.dev/commands/available','params':{'commands':[]}})
        if mode=='handshake_pre_session_unknown':
            send({'method':'_unknown.dev/event','params':{}})
        if mode=='handshake_pre_session_tool':
            send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                'sessionUpdate':'tool_call','toolCallId':'call_early','kind':'execute',
                'status':'pending','rawInput':{'command':'pwd'}}}})
            continue
        if mode=='handshake_pre_permission':
            send({'id':99,'method':'session/request_permission','params':{'sessionId':'sess_test',
                'toolCall':{'toolCallId':'call_early','kind':'execute','rawInput':{'command':'pwd'}},
                'options':[{'optionId':'allow-1','kind':'allow_once'}]}})
            continue
        initial=options()
        if mode=='late_model':initial=[x for x in initial if x['id']!='model']
        send({'id':msg['id'],'result':{'sessionId':'sess_test','configOptions':initial}})
    elif method=='session/set_config_option':
        if mode=='handshake_pre_tool' and params['configId']=='model':
            send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                'sessionUpdate':'tool_call','toolCallId':'call_early','kind':'execute',
                'status':'pending','rawInput':{'command':'pwd'}}}})
            continue
        if params['configId']=='model':model=params['value']
        if params['configId']=='effortLevel' and mode!='bad_effort':effort=params['value']
        selected=options()
        if (mode=='model_set_missing_model' or mode.startswith('registry_')) and params['configId']=='model':
            selected=[x for x in selected if x['id']!='model']
        if mode=='model_set_duplicate_model' and params['configId']=='model':
            selected.append(selected[0])
        if mode=='model_set_missing_effort' and params['configId']=='model':
            selected=[x for x in selected if x['id']!='effortLevel']
        if mode=='effort_set_missing_model' and params['configId']=='effortLevel':
            selected=[x for x in selected if x['id']!='model']
        if mode=='effort_set_missing_effort' and params['configId']=='effortLevel':
            selected=[x for x in selected if x['id']!='effortLevel']
        result={} if mode=='model_set_options_missing' and params['configId']=='model' else {'configOptions':selected}
        if mode.startswith('effort_pre_') and params['configId']=='effortLevel':
            current=options()
            if mode=='effort_pre_wrong_model':current[0]['currentValue']='auto'
            if mode=='effort_pre_duplicate':current.append(current[0])
            if mode=='effort_pre_missing_effort':current=current[:1]
            if mode=='effort_pre_missing_model':current=current[1:]
            send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                'sessionUpdate':'config_option_update','configOptions':current}}})
        if mode.startswith('registry_pre_') and params['configId']=='model':
            if mode=='registry_pre_ready_missing_ready':
                for current in (options(),options()[1:],options()):
                    send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                        'sessionUpdate':'config_option_update','configOptions':current}}})
            if mode=='registry_pre_permission':
                send({'id':99,'method':'session/request_permission','params':{'sessionId':'sess_test',
                    'options':[{'optionId':'allow-1','kind':'allow_once'}]}})
                continue
            if mode=='registry_pre_unknown':
                send({'method':'_unknown/event','params':{}})
            else:
                current=options()
                if mode=='registry_pre_duplicate':current.append(current[0])
                if mode=='registry_pre_wrong_model':current[0]['currentValue']='auto'
                if mode=='registry_pre_missing_effort':current=current[:1]
                if mode=='registry_pre_unoffered':current[0]['options']=[{'value':'auto'}]
                send({'method':'session/update','params':{
                    'sessionId':'foreign' if mode=='registry_pre_foreign' else 'sess_test',
                    'update':{'sessionUpdate':'tool_call' if mode=='registry_pre_tool' else 'config_option_update',
                              'kind':'execute','configOptions':current}}})
        if (mode=='registry_before_response' or mode.startswith('registry_pre_')) and params['configId']=='model':
            send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                'sessionUpdate':'config_option_update','configOptions':options()}}})
        send({'id':msg['id'],'result':result})
        if mode=='final_ack_cancel' and params['configId']=='effortLevel':
            import signal
            os.kill(os.getppid(),signal.SIGTERM)
        if mode.startswith('registry_') and mode!='registry_before_response' and params['configId']=='model':
            import time
            time.sleep(0.05)
            if mode=='registry_lifecycle':lifecycle()
            if mode=='registry_passive':
                send({'method':'_kiro/mcp/status','params':{'sessionId':'sess_test','servers':[]}})
                send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                    'sessionUpdate':'available_commands_update','availableCommands':[]}}})
            current=options()
            if mode=='registry_wrong_model':current[0]['currentValue']='auto'
            if mode=='registry_missing_effort':current=current[:1]
            if mode=='registry_duplicate':current.append(current[0])
            if mode=='registry_unoffered':current[0]['options']=[{'value':'auto'}]
            update={'sessionUpdate':'config_option_update','configOptions':current}
            if mode=='registry_tool':update={'sessionUpdate':'tool_call','kind':'execute'}
            send({'method':'session/update','params':{
                'sessionId':'foreign' if mode=='registry_foreign' else 'sess_test','update':update}})
        if mode in ('handshake_post_drift','final_ack_drift') and params['configId']=='effortLevel':
            effort='medium'
            send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                'sessionUpdate':'config_option_update','configOptions':options()}}})
        if mode=='handshake_kiro_commands' and params['configId']=='effortLevel':
            send({'method':'_kiro.dev/commands/available','params':{'commands':[]}})
        if mode=='lifecycle_final' and params['configId']=='effortLevel':lifecycle()
        if mode=='passive_final' and params['configId']=='effortLevel':
            send({'method':'_kiro/steering/documents_changed','params':{'sessionId':'sess_test','status':'success','documents':[]}})
            send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                'sessionUpdate':'available_commands_update','availableCommands':[]}}})
        if mode=='handshake_unknown_notification' and params['configId']=='effortLevel':
            send({'method':'_kiro.dev/unknown','params':{}})
        if mode=='handshake_unknown_update' and params['configId']=='effortLevel':
            send({'method':'session/notification','params':{'sessionId':'sess_test',
                'update':{'sessionUpdate':'unknown_side_effect'}}})
        if mode=='handshake_unknown_request' and params['configId']=='effortLevel':
            send({'id':88,'method':'client/unknown','params':{}})
    elif method=='session/prompt':
        assert params['prompt'] in ([{'type':'text','text':'Reply with exactly OK.'}],
                                    [{'type':'text','text':'In the empty local /app directory, use the terminal to run pwd once, then reply exactly OK. Do not inspect files, access the network, or write anything.'}])
        pending=msg['id']
        if mode=='cancel':continue
        if mode in ('drift_restore','config_confirm','config_missing_effort'):
            if mode=='drift_restore':
                model='auto';effort='medium'
            current=options()
            if mode=='config_missing_effort':
                current=[x for x in current if x['id']!='effortLevel']
            send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                'sessionUpdate':'config_option_update','configOptions':current}}})
            if mode=='drift_restore':
                model='claude-opus-5.5';effort='high'
                send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                    'sessionUpdate':'config_option_update','configOptions':options()}}})
        send({'method':'session/update','params':{'sessionId':'sess_test','update':{
            'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'OK'}}}})
        if mode in ('tool','tool_kiro_id','probe_pwd','probe_bad','probe_mac',
                    'probe_background','probe_extra_fields','probe_wrong_shape',
                    'probe_no_permission','probe_second_tool','probe_permission_bad',
                    'probe_no_completion','probe_missing_background',
                    'probe_string_shape','probe_second_permission',
                    'probe_no_allow_once','probe_completion_before_permission',
                    'probe_initial_completed','probe_tool_failed',
                    'probe_failed_after_completion','native_autoallow',
                    'native_permission','native_extra_tool'):
            raw={'command':'pwd','run_in_background':False}
            if mode=='probe_bad':raw={'command':'echo unsafe'}
            if mode=='probe_mac':raw={'command':'pwd','description':'Print working directory',
                                      'run_in_background':False}
            if mode=='probe_background':raw={'command':'pwd','run_in_background':True}
            if mode=='probe_extra_fields':raw={'command':'pwd','run_in_background':False,
                                               'extra':'unsafe'}
            if mode=='probe_wrong_shape':raw=['pwd']
            if mode=='probe_missing_background':raw={'command':'pwd'}
            if mode=='probe_string_shape':raw='pwd'
            send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                'sessionUpdate':'tool_call','toolCallId':'call_1',
                'status':'completed' if mode in ('probe_initial_completed','native_autoallow') else 'pending',
                'kind':'execute','rawInput':raw}}})
            if mode=='native_autoallow':
                send({'id':pending,'result':{'stopReason':'end_turn'}})
                continue
            if mode=='probe_completion_before_permission':
                send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                    'sessionUpdate':'tool_call_update','toolCallId':'call_1',
                    'status':'completed','kind':'execute'}}})
            if mode=='probe_tool_failed':
                send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                    'sessionUpdate':'tool_call_update','toolCallId':'call_1',
                    'status':'failed','kind':'execute'}}})
            if mode in ('probe_second_tool','native_extra_tool'):
                send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                    'sessionUpdate':'tool_call','toolCallId':'call_2','status':'pending',
                    'kind':'execute','rawInput':raw}}})
                continue
            if mode=='probe_no_permission':
                send({'id':pending,'result':{'stopReason':'end_turn'}})
                continue
            allow={'id':'allow-1','kind':'allow_once'} if mode=='tool_kiro_id' else {'optionId':'allow-1','kind':'allow_once'}
            send({'id':99,'method':'session/request_permission','params':{'sessionId':'sess_test',
                'toolCall':{'toolCallId':'call_1','kind':'execute',
                            'rawInput':{'command':'echo unsafe'} if mode=='probe_permission_bad' else raw},
                'options':([allow] if mode!='probe_no_allow_once' else []) +
                          [{'optionId':'reject-1','kind':'reject_once'}]}})
        else:
            send({'id':pending,'result':{'stopReason':'end_turn'}})
    elif method=='session/cancel':
        send({'id':pending,'result':{'stopReason':'cancelled'}})
    elif msg.get('id')==99:
        permission_reply_count+=1
        if mode=='handshake_pre_permission':
            assert msg['result']['outcome']=={'outcome':'cancelled'}
            continue
        if mode in ('probe_bad','probe_permission_bad','probe_no_allow_once') or (mode=='probe_second_permission'
                                                          and permission_reply_count==2):
            assert msg['result']['outcome']=={'outcome':'cancelled'}
            send({'id':pending,'result':{'stopReason':'cancelled'}})
            continue
        assert msg['result']['outcome']=={'outcome':'selected','optionId':'allow-1'}
        if mode=='probe_second_permission':
            send({'id':99,'method':'session/request_permission','params':{'sessionId':'sess_test',
                'toolCall':{'toolCallId':'call_1','kind':'execute',
                            'rawInput':{'command':'pwd','run_in_background':False}},
                'options':[{'optionId':'allow-1','kind':'allow_once'}]}})
            continue
        if mode!='probe_no_completion':
            send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                'sessionUpdate':'tool_call_update','toolCallId':'call_1',
                'status':'completed','kind':'execute'}}})
        if mode=='probe_failed_after_completion':
            send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                'sessionUpdate':'tool_call_update','toolCallId':'call_1',
                'status':'failed','kind':'execute'}}})
        send({'id':pending,'result':{'stopReason':'end_turn'}})
''' )
    script.chmod(0o700)
    return script


def _args(tmp_path: Path, mode: str) -> tuple[list[str], dict[str, str], Path, Path]:
    cli = _fake_cli(tmp_path)
    stream = tmp_path / "stream.jsonl"
    trace = tmp_path / "trace.jsonl"
    env = {**os.environ, "FAKE_ACP_MODE": mode, "FAKE_ACP_TRACE": str(trace),
           "BROWSER": "/usr/bin/false"}
    return ([sys.executable, str(RUNTIME), str(cli), str(stream), MODEL, "high",
             "Reply with exactly OK."], env, stream, trace)


def _native_args(tmp_path: Path, mode: str) -> tuple[list[str], dict[str, str], Path, Path]:
    args, env, stream, trace = _args(tmp_path, mode)
    args[-1] = NATIVE_PROMPT
    env["DRADAR_KIRO_PROBE_NATIVE_LOCAL"] = "1"
    return args, env, stream, trace


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_acp_selects_exact_model_and_effort_before_prompt(tmp_path: Path) -> None:
    args, env, stream, trace = _args(tmp_path, "normal")
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    events = _events(stream)
    assert [event["type"] for event in events] == [
        "configSelected", "sessionUpdate", "runFinished"]
    assert events[0]["data"] == {"sessionId": "sess_test", "model": MODEL, "effort": "high"}
    assert events[-1]["data"]["status"] == "success"
    methods = [entry["method"] for entry in _events(trace)]
    assert methods == ["initialize", "session/new", "session/set_config_option",
                       "session/set_config_option", "session/prompt"]


@pytest.mark.parametrize("mode,code", [
    ("model_set_options_missing", "config_model_set_options_missing"),
    ("model_set_missing_model", "config_model_registry_timeout"),
    ("model_set_duplicate_model", "config_model_set_model_duplicate"),
    ("model_set_missing_effort", "config_model_set_effort_missing"),
    ("effort_set_missing_model", "config_effort_set_model_missing"),
    ("effort_set_missing_effort", "config_effort_set_effort_missing"),
])
def test_acp_reports_sanitized_missing_config_stage_without_prompt(
    tmp_path: Path, mode: str, code: str,
) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert result.stderr.strip() == "DRADAR_KIRO_ACP=" + code
    assert "session/prompt" not in [entry["method"] for entry in _events(trace)]
    assert not any(event["type"] == "runFinished" for event in _events(stream))


def test_acp_handshake_only_confirms_config_without_inference(tmp_path: Path) -> None:
    args, env, stream, trace = _args(tmp_path, "normal")
    result = subprocess.run(args + ["--handshake-only"], env=env,
                            text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert [event["type"] for event in _events(stream)] == ["configSelected", "configHandshake"]
    assert _events(stream)[-1]["data"]["ignoredKiroNotifications"] == 0
    assert "session/prompt" not in [entry["method"] for entry in _events(trace)]


def test_handshake_ignores_only_documented_passive_kiro_notification(tmp_path: Path) -> None:
    args, env, stream, trace = _args(tmp_path, "handshake_kiro_commands")
    result = subprocess.run(args + ["--handshake-only"], env=env,
                            text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert [event["type"] for event in _events(stream)] == ["configSelected", "configHandshake"]
    assert _events(stream)[-1]["data"]["ignoredKiroNotifications"] == 1
    assert "session/prompt" not in [entry["method"] for entry in _events(trace)]


def test_handshake_counts_documented_notification_before_session_response(tmp_path: Path) -> None:
    args, env, stream, trace = _args(tmp_path, "handshake_pre_session_commands")
    result = subprocess.run(args + ["--handshake-only"], env=env,
                            text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert _events(stream)[-1]["data"]["ignoredKiroNotifications"] == 1
    assert "session/prompt" not in [entry["method"] for entry in _events(trace)]


@pytest.mark.parametrize("mode,code", [
    ("handshake_pre_permission", "handshake_permission_denied"),
    ("handshake_pre_session_tool", "handshake_unexpected_tool"),
    ("handshake_pre_tool", "handshake_unexpected_tool"),
    ("handshake_post_drift", "config_drift"),
    ("handshake_unknown_notification", "handshake_unexpected_message"),
    ("handshake_unknown_update", "handshake_unexpected_update"),
    ("handshake_unknown_request", "unsupported_client_request"),
    ("handshake_pre_session_unknown", "handshake_unexpected_message"),
])
def test_handshake_rejects_pre_prompt_tool_or_immediate_drift(
    tmp_path: Path, mode: str, code: str,
) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args + ["--handshake-only"], env=env,
                            text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert result.stderr.strip() == "DRADAR_KIRO_ACP=" + code
    assert "session/prompt" not in [entry["method"] for entry in _events(trace)]
    assert not any(event["type"] == "configHandshake" for event in _events(stream))
    if mode == "handshake_pre_permission":
        assert any(entry["outcome"] == {"outcome": "cancelled"}
                   for entry in _events(trace))


def test_acp_rejects_unacknowledged_effort_without_inference(tmp_path: Path) -> None:
    args, env, stream, trace = _args(tmp_path, "bad_effort")
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert "DRADAR_KIRO_ACP=config_not_selected" in result.stderr
    assert "session/prompt" not in [entry["method"] for entry in _events(trace)]
    assert not _events(stream)


def test_acp_accepts_late_model_selector_only_after_exact_ack(tmp_path: Path) -> None:
    args, env, stream, _trace = _args(tmp_path, "late_model")
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert _events(stream)[0]["data"]["model"] == MODEL


@pytest.mark.parametrize("mode", ["registry_delayed", "registry_before_response"])
def test_cold_registry_push_confirms_model_before_effort_and_prompt(tmp_path: Path, mode: str) -> None:
    # Source-derived Kiro 2.24.1 timing fixture, not a captured live response.
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert _events(stream)[0]["data"]["effort"] == "high"
    methods = [entry["method"] for entry in _events(trace)]
    assert methods.count("session/set_config_option") == 2
    assert methods.count("session/prompt") == 1


@pytest.mark.parametrize("mode,code", [
    ("registry_wrong_model", "model_not_selected"),
    ("registry_missing_effort", "config_model_set_effort_missing"),
    ("registry_duplicate", "config_model_set_model_duplicate"),
    ("registry_unoffered", "model_unavailable"),
    ("registry_foreign", "config_registry_unexpected_message"),
    ("registry_tool", "config_registry_unexpected_update"),
])
def test_registry_push_never_substitutes_missing_selection_evidence(tmp_path: Path, mode: str, code: str) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert result.stderr.strip() == "DRADAR_KIRO_ACP=" + code
    assert "session/prompt" not in [entry["method"] for entry in _events(trace)]


@pytest.mark.parametrize("mode,code", [
    ("registry_pre_tool", "handshake_unexpected_tool"),
    ("registry_pre_permission", "handshake_permission_denied"),
    ("registry_pre_foreign", "handshake_foreign_session"),
    ("registry_pre_unknown", "handshake_unexpected_message"),
    ("registry_pre_duplicate", "config_model_set_model_duplicate"),
    ("registry_pre_wrong_model", "model_not_selected"),
    ("registry_pre_missing_effort", "config_model_set_effort_missing"),
    ("registry_pre_unoffered", "model_unavailable"),
    ("registry_pre_ready_missing_ready", "config_model_set_model_missing"),
    ("effort_pre_wrong_model", "model_not_selected"),
    ("effort_pre_duplicate", "config_model_set_model_duplicate"),
    ("effort_pre_missing_effort", "config_model_set_effort_missing"),
    ("effort_pre_missing_model", "config_model_set_model_missing"),
    ("final_ack_drift", "config_drift"),
    ("final_ack_cancel", "cancelled"),
])
def test_cold_registry_rejects_pre_response_side_effects(tmp_path: Path, mode: str, code: str) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert result.stderr.strip() == "DRADAR_KIRO_ACP=" + code
    recorded = _events(trace)
    assert "session/prompt" not in [entry["method"] for entry in recorded]
    assert not any(event["type"] == "runFinished" for event in _events(stream))
    if mode == "registry_pre_permission":
        assert any(entry.get("outcome") == {"outcome": "cancelled"} for entry in recorded)


@pytest.mark.parametrize("mode,phase,method", [
    ("handshake_pre_session_unknown", "new_session", "_unknown.dev/event"),
    ("registry_pre_unknown", "model_set", "_unknown/event"),
    ("handshake_unknown_notification", "final_confirmation", "_kiro.dev/unknown"),
])
def test_unknown_notification_records_only_phase_and_method_hash(tmp_path: Path, mode: str, phase: str, method: str) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args + ["--handshake-only"], env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    events = [e["data"] for e in _events(stream) if e["type"] == "handshakeRejected"]
    assert events == [{"phase": phase, "methodType": "string",
                       "methodSha256": hashlib.sha256(method.encode()).hexdigest()}]
    assert method not in stream.read_text()
    assert "session/prompt" not in [entry["method"] for entry in _events(trace)]


@pytest.mark.parametrize("mode", ["handshake_pre_session_unknown", "registry_pre_unknown",
                                  "handshake_unknown_notification"])
def test_diagnostic_observes_extensions_but_never_prompts(tmp_path: Path, mode: str) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args + ["--handshake-diagnostic"], env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert any(e['type']=='handshakeNotice' for e in _events(stream))
    assert not any(e['type']=='runFinished' for e in _events(stream))
    assert 'session/prompt' not in [e['method'] for e in _events(trace)]


@pytest.mark.parametrize("mode", ["registry_pre_permission", "registry_pre_tool",
                                  "registry_pre_foreign", "final_ack_drift"])
def test_diagnostic_never_relaxes_core_config_or_side_effect_rules(tmp_path: Path, mode: str) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args + ["--handshake-diagnostic"], env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert 'session/prompt' not in [e['method'] for e in _events(trace)]
    assert not any(e.get('outcome',{}).get('outcome')=='selected' for e in _events(trace) if isinstance(e.get('outcome'),dict))


@pytest.mark.parametrize('mode',['passive_normal','registry_passive','passive_final','passive_governance_admin'])
def test_observed_passive_method_order_and_standard_command_advertisement(tmp_path: Path, mode: str) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert [e['method'] for e in _events(trace)].count('session/prompt') == 1


@pytest.mark.parametrize('mode,code', [
    ('passive_progressive_failed','handshake_metadata_failed'),
    ('passive_progressive_shape','handshake_metadata_shape_invalid'),
    ('passive_progressive_errors','handshake_metadata_failed'),
    ('passive_powers_failed','handshake_metadata_failed'),
    ('passive_powers_errors','handshake_metadata_failed'),
    ('passive_documents_failed','handshake_metadata_failed'),
    ('passive_governance_failed','handshake_metadata_failed'),
    ('passive_foreign','handshake_foreign_session'),
    ('passive_foreign_deferred','handshake_foreign_session'),
    ('passive_request','unsupported_client_request'),
    ('passive_error','handshake_unexpected_message'),
    ('passive_bad_commands','handshake_metadata_shape_invalid'),
])
def test_passive_metadata_cannot_hide_foreign_session_request_or_error(tmp_path: Path, mode: str, code: str) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert result.stderr.strip()=='DRADAR_KIRO_ACP='+code
    assert 'session/prompt' not in [e['method'] for e in _events(trace)]


def test_acp_fails_closed_on_transient_model_effort_drift(tmp_path: Path) -> None:
    args, env, stream, trace = _args(tmp_path, "drift_restore")
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert "DRADAR_KIRO_ACP=config_drift" in result.stderr
    assert any(event["type"] == "configDrift" for event in _events(stream))
    assert not any(event["type"] == "runFinished" for event in _events(stream))
    assert "session/cancel" in [entry["method"] for entry in _events(trace)]


def test_acp_records_matching_config_update_during_prompt(tmp_path: Path) -> None:
    args, env, stream, _trace = _args(tmp_path, "config_confirm")
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert any(event["type"] == "configConfirmed" for event in _events(stream))
    assert _events(stream)[-1]["type"] == "runFinished"


def test_acp_rejects_incomplete_config_update(tmp_path: Path) -> None:
    args, env, stream, _trace = _args(tmp_path, "config_missing_effort")
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert "DRADAR_KIRO_ACP=config_drift" in result.stderr
    assert not any(event["type"] == "runFinished" for event in _events(stream))


def test_acp_handles_tool_permission_once_and_streams_tool_lifecycle(tmp_path: Path) -> None:
    args, env, stream, trace = _args(tmp_path, "tool")
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    events = _events(stream)
    assert [event["data"]["update"]["sessionUpdate"] for event in events
            if event["type"] == "sessionUpdate"] == [
                "agent_message_chunk", "tool_call", "tool_call_update"]
    assert events[-1]["type"] == "runFinished"
    assert any(entry["method"] is None for entry in _events(trace))


def test_acp_handles_kiro_permission_id_alias(tmp_path: Path) -> None:
    args, env, stream, _trace = _args(tmp_path, "tool_kiro_id")
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert _events(stream)[-1]["type"] == "runFinished"


def test_probe_approves_only_one_literal_pwd(tmp_path: Path) -> None:
    args, env, stream, trace = _args(tmp_path, "probe_pwd")
    env["DRADAR_KIRO_PROBE_PWD_ONLY"] = "1"
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert _events(stream)[-1]["type"] == "runFinished"
    assert sum(entry["method"] is None for entry in _events(trace)) == 1


def test_probe_approves_native_mac_non_background_shape(tmp_path: Path) -> None:
    args, env, stream, trace = _args(tmp_path, "probe_mac")
    env["DRADAR_KIRO_PROBE_PWD_ONLY"] = "1"
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    checks = [event["data"] for event in _events(stream)
              if event["type"] == "probeInputCheck"]
    assert len(checks) == 2
    assert all(check["shape"] == "object" and check["commandIsPwd"]
               and check["fieldsAllowed"] and check["background"] == "false"
               for check in checks)
    assert sum(entry["method"] is None for entry in _events(trace)) == 1


@pytest.mark.parametrize("mode,code,shape,command_is_pwd,fields_allowed,background", [
    ("probe_bad", "probe_object_command_not_pwd", "object", False, True, "absent"),
    ("probe_background", "probe_background_forbidden", "object", True, True, "true"),
    ("probe_extra_fields", "probe_object_fields_unsupported", "object", True, False, "false"),
    ("probe_wrong_shape", "probe_input_shape_unsupported", "other", False, False, "absent"),
    ("probe_missing_background", "probe_background_unspecified", "object", True, True, "absent"),
    ("probe_string_shape", "probe_input_shape_unsupported", "string", True, False, "absent"),
])
def test_probe_rejects_unsafe_input_with_sanitized_diagnostic(
    tmp_path: Path, mode: str, code: str, shape: str,
    command_is_pwd: bool, fields_allowed: bool, background: str,
) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    env["DRADAR_KIRO_PROBE_PWD_ONLY"] = "1"
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert result.stderr.strip() == "DRADAR_KIRO_ACP=" + code
    check = next(event["data"] for event in _events(stream)
                 if event["type"] == "probeInputCheck")
    assert (check["shape"], check["commandIsPwd"], check["fieldsAllowed"],
            check["background"], check["toolCount"]) == (
                shape, command_is_pwd, fields_allowed, background, 1)
    assert "rawInput" not in stream.read_text()
    assert "unsafe" not in stream.read_text() + result.stderr
    assert not any(event["type"] == "runFinished" for event in _events(stream))
    assert not any(entry["method"] is None for entry in _events(trace))


@pytest.mark.parametrize("mode,code", [
    ("probe_second_tool", "probe_multiple_tools"),
    ("probe_second_permission", "probe_multiple_permissions"),
    ("probe_no_allow_once", "probe_allow_once_missing"),
    ("probe_completion_before_permission", "probe_completion_before_permission"),
    ("probe_initial_completed", "probe_completion_before_permission"),
    ("probe_tool_failed", "probe_tool_failed"),
    ("probe_failed_after_completion", "probe_tool_failed"),
    ("probe_no_permission", "probe_permission_count_invalid"),
    ("probe_permission_bad", "probe_object_command_not_pwd"),
    ("probe_no_completion", "probe_tool_not_completed"),
])
def test_probe_rejects_tool_and_permission_contract_violations(
    tmp_path: Path, mode: str, code: str,
) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    env["DRADAR_KIRO_PROBE_PWD_ONLY"] = "1"
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert result.stderr.strip() == "DRADAR_KIRO_ACP=" + code
    assert not any(event["type"] == "runFinished" for event in _events(stream))
    if mode == "probe_permission_bad":
        assert any(entry["outcome"] == {"outcome": "cancelled"}
                   for entry in _events(trace))
    if mode == "probe_no_allow_once":
        counts = next(event["data"] for event in _events(stream)
                      if event["type"] == "probeFailureCounts")
        assert counts == {"toolCount": 1, "permissionCount": 0}


def test_local_native_probe_accepts_default_autoallowed_tool_without_permission(
    tmp_path: Path,
) -> None:
    args, env, stream, trace = _native_args(tmp_path, "native_autoallow")
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert _events(stream)[-1]["type"] == "runFinished"
    assert not any(event["type"] == "localPermissionSelected" for event in _events(stream))
    assert not any(entry["method"] is None for entry in _events(trace))


def test_local_native_probe_limits_optional_permission_to_one_local_command(
    tmp_path: Path,
) -> None:
    args, env, stream, trace = _native_args(tmp_path, "native_permission")
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert _events(stream)[-1]["type"] == "runFinished"
    assert [event["data"]["permissionCount"] for event in _events(stream)
            if event["type"] == "localPermissionSelected"] == [1]
    assert sum(entry["method"] is None for entry in _events(trace)) == 1


@pytest.mark.parametrize("mode,code", [
    ("native_extra_tool", "local_multiple_tools_observed"),
    ("probe_permission_bad", "local_permission_not_target"),
])
def test_local_native_probe_reports_unexpected_tool_or_permission(
    tmp_path: Path, mode: str, code: str,
) -> None:
    args, env, stream, trace = _native_args(tmp_path, mode)
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert result.stderr.strip() == "DRADAR_KIRO_ACP=" + code
    assert not any(event["type"] == "runFinished" for event in _events(stream))
    if mode == "probe_permission_bad":
        assert any(entry["outcome"] == {"outcome": "cancelled"}
                   for entry in _events(trace))


def test_acp_cancels_prompt_and_leaves_no_success_marker(tmp_path: Path) -> None:
    args, env, stream, trace = _args(tmp_path, "cancel")
    proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if trace.exists() and any(entry["method"] == "session/prompt" for entry in _events(trace)):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("fake ACP prompt was not reached")
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=12)
        assert proc.returncode == 1
        assert "DRADAR_KIRO_ACP=" in err
        assert "session/cancel" in [entry["method"] for entry in _events(trace)]
        assert not any(event["type"] == "runFinished" for event in _events(stream))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)


@pytest.mark.parametrize("mode,expected", [("passive_normal", 0), ("passive_powers_failed", 1),
    ("handshake_unknown_update", 1), ("passive_request", 1)])
def test_strict_handshake_metadata_is_bounded_and_redacted(tmp_path: Path, mode: str, expected: int) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    env["DRADAR_KIRO_HANDSHAKE_METADATA"] = "1"
    result = subprocess.run(args + ["--handshake-only"], env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == expected
    events = _events(stream)
    envelopes = [e["data"] for e in events if e["type"] == "handshakeEnvelope"]
    assert 0 < len(envelopes) <= 96
    assert "SYNTHETIC_FAILURE" not in stream.read_text()
    assert "session/prompt" not in [e["method"] for e in _events(trace)]
    assert not any(e["type"] == "runFinished" for e in events)
    if expected:
        assert events[-1]["type"] == "handshakeFailure"
    if mode == "passive_powers_failed":
        assert any(e["status"] == "failed" and e["hasError"] for e in envelopes)
    if mode == "handshake_unknown_update":
        assert any(e["coreKindSha256"] == hashlib.sha256(b"unknown_side_effect").hexdigest() for e in envelopes)
    if mode == "passive_request":
        assert any(e["envelope"] == "request" for e in envelopes)


def test_metadata_environment_cannot_enable_diagnostics_during_inference(tmp_path: Path) -> None:
    args, env, stream, trace = _args(tmp_path, "passive_normal")
    env["DRADAR_KIRO_HANDSHAKE_METADATA"] = "1"
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0
    assert not any(e["type"] == "handshakeEnvelope" for e in _events(stream))


@pytest.mark.parametrize("mode", ["lifecycle_normal", "registry_lifecycle", "lifecycle_final"])
def test_source_proven_startup_catalogs_and_context_display(tmp_path: Path, mode: str) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert [e["method"] for e in _events(trace)].count("session/prompt") == 1


@pytest.mark.parametrize("mode", ["lifecycle_foreign", "lifecycle_hooks_shape", "lifecycle_tools_shape",
    "lifecycle_error", "lifecycle_failed_status", "lifecycle_roster_failed", "lifecycle_roster_deleted", "lifecycle_roster_failure",
    "lifecycle_context_error", "lifecycle_context_hidden_error", "lifecycle_context_invalid",
    "lifecycle_context_mismatch"])
def test_startup_metadata_cannot_hide_errors_or_foreign_session(tmp_path: Path, mode: str) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert "session/prompt" not in [e["method"] for e in _events(trace)]
    assert "SYNTHETIC_FAILURE" not in stream.read_text()


@pytest.mark.parametrize("mode,success", [("early_config_normal", True), ("early_config_foreign", False),
    ("early_config_duplicate", False), ("early_config_shape", False)])
def test_initial_registry_before_new_response_defers_exact_session_binding(tmp_path: Path, mode: str, success: bool) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == (0 if success else 1)
    assert ("session/prompt" in [e["method"] for e in _events(trace)]) is success
    if not success:
        events = _events(stream)
        assert events[-1]["type"] == "handshakeFailure"
        assert events[-1]["data"]["phase"] == "new_session"
        assert events[-2]["type"] == "handshakeEnvelope"
        if mode == "early_config_foreign":
            assert events[-2]["data"]["sessionsMatchPending"] is False
        assert "sess_test" not in stream.read_text()
        assert "foreign" not in stream.read_text()


def test_real_mode_failure_records_only_last_sanitized_pre_prompt_envelope(tmp_path: Path) -> None:
    args, env, stream, trace = _args(tmp_path, "passive_powers_failed")
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    envelopes = [e["data"] for e in _events(stream) if e["type"] == "handshakeEnvelope"]
    assert len(envelopes) == 1
    assert envelopes[0]["status"] == "failed" and envelopes[0]["hasError"]
    assert "SYNTHETIC_FAILURE" not in stream.read_text()
    assert "session/prompt" not in [e["method"] for e in _events(trace)]


@pytest.mark.parametrize("mode", ["handshake_pre_session_unknown", "passive_foreign_deferred",
    "registry_foreign", "effort_pre_wrong_model", "final_ack_drift"])
def test_every_protocol_startup_failure_has_phase_and_only_session_relations(tmp_path: Path, mode: str) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    failures = [e for e in _events(stream) if e["type"] == "handshakeFailure"]
    assert len(failures) == 1
    envelopes = [e["data"] for e in _events(stream) if e["type"] == "handshakeEnvelope"]
    assert len(envelopes) == 1
    assert type(envelopes[0]["currentSessionKnown"]) is bool
    assert envelopes[0]["sessionsMatchCurrent"] in (True, False, None)
    assert "sessionId" not in envelopes[0]
    assert "sess_test" not in stream.read_text()
    assert "session/prompt" not in [e["method"] for e in _events(trace)]


def test_spawn_failure_persists_fixed_phase_before_cleanup(tmp_path: Path) -> None:
    args, env, stream, trace = _args(tmp_path, "normal")
    args[2] = str(tmp_path / "missing-cli")
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert result.stderr.strip() == "DRADAR_KIRO_ACP=process_error"
    assert _events(stream) == [{"type": "handshakeFailure", "data": {"phase": "spawn"}}]


@pytest.mark.parametrize("mode", ["startup_exit", "startup_invalid_json"])
def test_no_valid_startup_message_still_records_fixed_failure(tmp_path: Path, mode: str) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    events = _events(stream)
    assert events[-1] == {"type": "handshakeFailure", "data": {"phase": "initialize"}}
    assert "SYNTHETIC_SECRET" not in stream.read_text()
    if mode == "startup_invalid_json":
        assert events[-2]["data"]["envelope"] == "other"
        assert events[-2]["data"]["methodSha256"] is None


@pytest.mark.parametrize("mode", ["startup_surrogate_method", "startup_surrogate_kind"])
def test_surrogate_protocol_names_fail_with_sanitized_diagnostics(tmp_path: Path, mode: str) -> None:
    args, env, stream, trace = _args(tmp_path, mode)
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=12)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert result.stderr.startswith("DRADAR_KIRO_ACP=handshake_unexpected_")
    assert _events(stream)[-1] == {"type": "handshakeFailure", "data": {"phase": "new_session"}}
    assert "session/prompt" not in [e["method"] for e in _events(trace)]
