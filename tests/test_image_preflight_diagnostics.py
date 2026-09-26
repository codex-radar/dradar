"""Offline privacy and control-flow contracts for image preflight diagnostics."""
import hashlib
import json
import subprocess

import pytest

from dradar import runner

PRIVATE = 'Bearer secret_token https://user:password@private.registry/image /Users/private/task'


def setup_task(tmp_path):
    path = tmp_path / 'task.toml'
    path.write_text('[environment]\ndocker_image="private.registry/secret:tag"\n')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invoke(monkeypatch, tmp_path, values):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        value = values[len(calls)-1]
        if isinstance(value, Exception):
            raise value
        code, output = value
        return subprocess.CompletedProcess(command, code, output, PRIVATE)
    monkeypatch.setattr(runner.subprocess, 'run', run)
    monkeypatch.delenv('DOCKER_DEFAULT_PLATFORM', raising=False)
    return calls


@pytest.mark.parametrize('value,result,exit_code', [
    (FileNotFoundError(PRIVATE), 'missing_command', None),
    (PermissionError(PRIVATE), 'os_error', None),
    (subprocess.TimeoutExpired(PRIVATE,20,output=PRIVATE,stderr=PRIVATE), 'timeout', None),
    ((17, PRIVATE), 'nonzero_exit', 17),
    ((-9, PRIVATE), 'nonzero_exit', -9),
    ((0, PRIVATE), 'json_invalid', 0),
])
def test_two_failed_probes(monkeypatch,tmp_path,value,result,exit_code,capsys):
    digest = setup_task(tmp_path)
    calls = invoke(monkeypatch,tmp_path,[value,value])
    with pytest.raises(runner.CodexInstallError) as caught:
        runner._codex_task_platforms(tmp_path)
    exc = caught.value
    assert exc.report_code == 'codex_task_image_unavailable'
    expected = {'task_toml_sha256':digest}
    for stage in ('local','remote'):
        expected[f'image_{stage}_result'] = result
        if exit_code is not None: expected[f'image_{stage}_exit_code'] = exit_code
    assert exc.report_detail == expected
    assert len(calls)==2 and all(c[1]['timeout']==20 for c in calls)
    assert calls[0][0][:3]==['docker','image','inspect']
    assert calls[1][0][:3]==['docker','buildx','imagetools']
    encoded=json.dumps(exc.report_detail)+str(exc)+str(capsys.readouterr())
    for secret in ('secret_token','private.registry','/Users/private','password'):
        assert secret not in encoded


@pytest.mark.parametrize('output',['null','[]','"private"','1'])
def test_json_structure_keeps_original_stop_contract(monkeypatch,tmp_path,output):
    setup_task(tmp_path)
    calls=invoke(monkeypatch,tmp_path,[(0,output)])
    with pytest.raises(runner.CodexInstallError) as caught:
        runner._codex_task_platforms(tmp_path)
    assert len(calls)==1
    assert caught.value.report_detail['image_local_result']=='structure_invalid'
    assert caught.value.report_detail['image_remote_result']=='not_attempted'


@pytest.mark.parametrize('fallback',[False,True])
def test_success_paths_unchanged(monkeypatch,tmp_path,fallback):
    setup_task(tmp_path)
    values=([(1,PRIVATE)] if fallback else [])+[(0,'{"os":"linux","architecture":"amd64"}')]
    calls=invoke(monkeypatch,tmp_path,values)
    monkeypatch.setattr(runner,'_image_preflight_detail',lambda *a:pytest.fail('success collected diagnostics'))
    assert runner._codex_task_platforms(tmp_path)==('linux-x64',)
    assert len(calls)==1+fallback


def test_hash_unreadable_is_unknown(tmp_path):
    assert runner._image_preflight_detail(None, {})=={'task_toml_sha256':'unknown'}


def test_diagnostic_collection_cannot_mask_failure(monkeypatch,tmp_path):
    setup_task(tmp_path)
    invoke(monkeypatch,tmp_path,[(1,PRIVATE),(1,PRIVATE)])
    def broken(*args): raise RuntimeError(PRIVATE)
    monkeypatch.setattr(runner,'_image_preflight_detail',broken)
    with pytest.raises(runner.CodexInstallError) as caught:
        runner._codex_task_platforms(tmp_path)
    assert caught.value.report_code=='codex_task_image_unavailable'
    assert caught.value.report_detail=={}
    assert PRIVATE not in str(caught.value)


def test_hash_bound_to_parsed_bytes(monkeypatch,tmp_path):
    digest=setup_task(tmp_path)
    def run(command,**kwargs):
        (tmp_path/'task.toml').write_text('changed')
        return subprocess.CompletedProcess(command,1,'',PRIVATE)
    monkeypatch.setattr(runner.subprocess,'run',run)
    with pytest.raises(runner.CodexInstallError) as caught: runner._codex_task_platforms(tmp_path)
    assert caught.value.report_detail['task_toml_sha256']==digest


def report():
    from dradar import failure_reports as f
    return f.build_report(source='cli',phase='runner',failure_kind='runner_failed',
        failure_code='codex_task_image_unavailable',detail={
        'task_id':'t1','image_local_result':'nonzero_exit','image_local_exit_code':-9,
        'image_remote_result':'timeout','task_toml_sha256':'a'*64})


def test_real_http_422_only_downgrade(tmp_path):
    import httpx
    from dradar import failure_reports as f
    from dradar.api_client import ApiClient
    for status,body,retries in [(422,{'detail':'failure report detail has unsupported fields'},2),
        (422,{'detail':'invalid image preflight detail'},1),(401,{'detail':'failure report detail has unsupported fields'},1),
        (500,{'detail':'failure report detail has unsupported fields'},1)]:
        calls=[]
        def handle(req):
            calls.append(json.loads(req.content))
            return httpx.Response(status if len(calls)==1 else 200,json=body if len(calls)==1 else {'status':'received'})
        client=ApiClient('https://local.invalid','unused')
        client._client.close()
        client._client=httpx.Client(base_url="https://local.invalid",transport=httpx.MockTransport(handle))
        try: f._send_compatible(client,report())
        except Exception: pass
        assert len(calls)==retries
        if retries==2:
            assert calls[1]['detail']=={'task_id':'t1'}
            assert {k:v for k,v in calls[0].items() if k!='detail'}=={k:v for k,v in calls[1].items() if k!='detail'}
        client._client.close()


def test_second_failure_queues_original(tmp_path):
    from dradar import failure_reports as f
    from dradar.api_client import ApiError
    class Client:
        def __init__(self):self.calls=[]
        def report_runner_failure(self,p):
            self.calls.append(p)
            raise ApiError('private text',status_code=422,payload={'detail':'failure report detail has unsupported fields'})
    client=Client();payload=report()
    assert f.submit_or_queue(client,tmp_path,payload)=='send-failed'
    assert len(client.calls)==2
    queued=f.pending(tmp_path)[0]
    assert queued['detail']==payload['detail'] and queued['report_key']==payload['report_key']
    assert 'private text' not in json.dumps(queued)


@pytest.mark.parametrize('key,value',[
    ('image_local_result',PRIVATE),('image_local_exit_code',True),
    ('image_remote_exit_code','999999999999999999'),('task_toml_sha256',PRIVATE),
])
def test_bad_fields_do_not_leave_client(key,value):
    from dradar import failure_reports as f
    detail=report()['detail'];detail[key]=value
    encoded=json.dumps(f.build_report(source='cli',phase='runner',failure_kind='runner_failed',
        failure_code='codex_task_image_unavailable',detail=detail))
    assert PRIVATE not in encoded
