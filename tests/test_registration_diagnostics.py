import json
from types import SimpleNamespace

import httpx
import pytest

from dradar import registration, failure_reports as reports
from dradar.api_client import ApiError
from dradar.registration import RegistrationWindow
from test_registration_recovery import fixture


def diagnostic(window, exc):
    window._snapshot(exc)
    return window._diagnostic


def test_clock_snapshot_omits_abnormal_values_and_freezes(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(registration.time, 'monotonic', lambda: clock[0])
    window = RegistrationWindow(130, lambda: True)
    window._observe(stage='start_preflight', ack='persisted')
    clock[0] = 116
    result = diagnostic(window, window._error('irrelevant', 'budget_expired'))
    assert result['registration_elapsed_ms'] == 16000
    assert result['registration_remaining_ms'] == 0
    clock[0] = 200
    window._snapshot(window._error('later', 'worker_exited'))
    window._close_diagnostic('transport_error')
    assert result['registration_failure_reason'] == 'budget_expired'
    assert result['registration_elapsed_ms'] == 16000
    assert result['registration_close_state'] == 'transport_error'
    for now in (99, 221, float('nan'), float('inf')):
        clock[0] = 100
        window = RegistrationWindow(130, lambda: True)
        clock[0] = now
        result = diagnostic(window, ApiError('no text emitted'))
        assert 'registration_elapsed_ms' not in result
        assert 'registration_remaining_ms' not in result


@pytest.mark.parametrize('kind,reason', [('deadline','budget_expired'), ('alive','worker_exited'), ('stop','stop_requested')])
def test_check_reasons(monkeypatch, kind, reason):
    clock = [100.0]
    monkeypatch.setattr(registration.time, 'monotonic', lambda: clock[0])
    window = RegistrationWindow(130, lambda: kind != 'alive')
    if kind == 'deadline': clock[0] = 115
    if kind == 'stop':
        window.telemetry = SimpleNamespace(stop_requested=True)
    with pytest.raises(ApiError) as caught: window.check()
    assert caught.value.registration_reason == reason


def test_insufficient_handoff_is_preflight_and_never_posts(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(registration.time, 'monotonic', lambda: clock[0])
    window = RegistrationWindow(130, lambda: True)
    window._observe(stage='start_preflight')
    clock[0] = 112
    with pytest.raises(ApiError) as caught:
        window._start_request('POST', '/api/v1/assignment/started')
    detail = diagnostic(window, caught.value)
    assert detail['registration_failure_stage'] == 'start_preflight'
    assert detail['registration_failure_reason'] == 'handoff_budget_insufficient'
    assert detail['registration_remaining_ms'] == 3000
    assert not window.started_sent


@pytest.mark.parametrize('fault,stage,reason,ack,close', [
    ('wrong_ack','flight','invalid_response','not_received','not_attempted'),
    ('429','heartbeat','http_rejected','not_received','not_attempted'),
    ('stop','heartbeat','stop_requested','not_received','not_attempted'),
    ('start_disconnect','start_request','transport_error','persisted','confirmed'),
    ('close_disconnect','start_request','transport_error','persisted','transport_error'),
])
def test_real_adapter_diagnostics(tmp_path, monkeypatch, fault, stage, reason, ack, close):
    with fixture(tmp_path, monkeypatch, fault) as (_, api, telemetry, assignment, state):
        window = RegistrationWindow(registration.time.monotonic()+30, lambda: True)
        with pytest.raises(ApiError): window.bind(api, telemetry, assignment)
        detail = telemetry.worker_registration_diagnostic
        assert detail['registration_failure_stage'] == stage
        assert detail['registration_failure_reason'] == reason
        assert detail['registration_ack_state'] == ack
        assert detail['registration_close_state'] == close
        assert state['paths'].count('/api/v1/assignment/started') <= 1
        if fault == 'close_disconnect': assert assignment['_registration_start_uncertain']
        assert reports._safe_detail('registration_elapsed_ms', detail['registration_elapsed_ms']) is not None


def test_ack_received_but_persistence_failed(tmp_path, monkeypatch):
    with fixture(tmp_path, monkeypatch) as (_, api, telemetry, assignment, state):
        monkeypatch.setattr(telemetry.flight_recorder, '_write_acknowledged_ids_unlocked',
                            lambda *a: (_ for _ in ()).throw(OSError('private path')))
        window = RegistrationWindow(registration.time.monotonic()+30, lambda: True)
        with pytest.raises(ApiError): window.bind(api, telemetry, assignment)
        d = telemetry.worker_registration_diagnostic
        assert (d['registration_ack_state'], d['registration_failure_stage'], d['registration_failure_reason']) == ('received','ack_persist','local_state_error')
        assert not state['started']
        assert 'private path' not in json.dumps(d)


def test_diagnostic_failure_does_not_block_close(tmp_path, monkeypatch):
    with fixture(tmp_path, monkeypatch, 'start_disconnect') as (_, api, telemetry, assignment, state):
        window = RegistrationWindow(registration.time.monotonic()+30, lambda: True)
        window._diagnostic = None
        monkeypatch.setattr(window, '_publish_diagnostic', lambda: (_ for _ in ()).throw(ValueError('broken diagnostic')))
        with pytest.raises(ApiError): window.bind(api, telemetry, assignment)
        assert window._fenced and '_registration_start_uncertain' not in assignment
        assert state['paths'].count('/api/v1/assignment/started') == 1


def payload():
    return reports.build_report(source='cli', phase='runner', failure_kind='runner_failed',
        failure_code='assignment-start-transport', detail={
            'task_id':'fixture', 'registration_result':'flight_target_acknowledged',
            'registration_failure_stage':'start_request', 'registration_failure_reason':'transport_error',
            'registration_ack_state':'persisted','registration_close_state':'confirmed',
            'registration_elapsed_ms':123, 'registration_remaining_ms':14877})


@pytest.mark.parametrize('rejection', ['failure report detail has unsupported fields','invalid registration failure detail'])
def test_one_downgrade_same_key(rejection):
    calls=[]
    class Client:
        def report_runner_failure(self,p):
            calls.append(p)
            if len(calls)==1: raise ApiError('refused',status_code=422,payload={'detail':rejection})
    record=payload();reports._send_compatible(Client(),record)
    assert len(calls)==2 and calls[0]['report_key']==calls[1]['report_key']
    assert calls[1]['detail']=={'task_id':'fixture'}
    assert 'registration_result' in record['detail']


def test_semantic_rejection_not_downgraded():
    for rejection in ('invalid registration diagnostic detail','invalid failure report detail registration_result'):
        calls=[]
        class Client:
            def report_runner_failure(self,p):
                calls.append(p);raise ApiError('refused',status_code=422,payload={'detail':rejection})
        with pytest.raises(ApiError):reports._send_compatible(Client(),payload())
        assert len(calls)==1


def test_failed_downgrade_queues_original(tmp_path):
    calls=[]
    class Client:
        def report_runner_failure(self,p):
            calls.append(p);raise ApiError('old server',status_code=422,payload={'detail':'failure report detail has unsupported fields'})
    record=payload();reports.submit_or_queue(Client(),tmp_path,record)
    assert len(calls)==2
    saved=json.loads(next((tmp_path/'failure-reports').glob('*.json')).read_text())
    assert saved['detail']==record['detail'] and saved['report_key']==record['report_key']


@pytest.mark.parametrize('body', [b'not-json', b'[]', b'null', b'"text"'])
def test_registration_response_parser_rejects_bad_body(body):
    from dradar.api_client import ApiClient
    window = RegistrationWindow(registration.time.monotonic()+30, lambda: True)
    window.api = SimpleNamespace(_check=lambda r: r.json())
    with pytest.raises(ApiError) as caught:
        window._check_response(httpx.Response(200, content=body))
    assert caught.value.registration_reason == 'invalid_response'


def test_clock_read_failure_omits_only_numbers(monkeypatch):
    window = RegistrationWindow(registration.time.monotonic()+30, lambda: True)
    monkeypatch.setattr(registration.time, 'monotonic', lambda: (_ for _ in ()).throw(OSError('clock unavailable')))
    result = diagnostic(window, window._error('worker exited', 'worker_exited'))
    assert result['registration_failure_reason'] == 'worker_exited'
    assert 'registration_elapsed_ms' not in result and 'registration_remaining_ms' not in result
