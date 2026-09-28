"""Offline ACP contract tests: exact config, tool permission and cancellation."""

from __future__ import annotations

import json
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
for line in sys.stdin:
    msg=json.loads(line);method=msg.get('method');params=msg.get('params',{})
    record({'method':method,'params':params if method!='session/prompt' else {'sessionId':params.get('sessionId'),'prompt':params.get('prompt')},
            'outcome':msg.get('result',{}).get('outcome') if msg.get('id')==99 else None})
    if method=='initialize':
        send({'id':msg['id'],'result':{'protocolVersion':1,'agentCapabilities':{}}})
    elif method=='session/new':
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
        if mode=='model_set_missing_model' and params['configId']=='model':
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
        send({'id':msg['id'],'result':result})
        if mode=='handshake_post_drift' and params['configId']=='effortLevel':
            effort='medium'
            send({'method':'session/update','params':{'sessionId':'sess_test','update':{
                'sessionUpdate':'config_option_update','configOptions':options()}}})
        if mode=='handshake_kiro_commands' and params['configId']=='effortLevel':
            send({'method':'_kiro.dev/commands/available','params':{'commands':[]}})
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
    ("model_set_missing_model", "config_model_set_model_missing"),
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
