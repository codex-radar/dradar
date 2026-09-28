import json
from types import SimpleNamespace

import httpx
import pytest

from dradar import identity, local_config, write_recovery_status
from dradar.api_client import ApiClient, ApiError


def client(handler):
    return ApiClient('https://example.invalid', 'synthetic', transport=httpx.MockTransport(handler))


def test_explicit_status_reconciles_after_exit_auto_budget_ends(monkeypatch, capsys):
    body = {'session_id': 'a'*32, 'batch_id': 'b'*32, 'seq': 2, 'reason': 'error'}
    calls = []
    def lost(request):
        calls.append(request.method)
        raise httpx.ReadError('synthetic response loss')
    with pytest.raises(ApiError):
        client(lost).runner_close(body)
    path = next((local_config.HOME / 'pending_session_exits').glob('*.json'))
    saved = json.loads(path.read_text())
    saved['deadline'] = 0
    saved['wall_deadline'] = 0
    path.write_text(json.dumps(saved))
    calls.clear()
    def receipt(request):
        calls.append(request.method)
        return httpx.Response(200, json={'session_id': body['session_id'], 'batch_id': body['batch_id'],
                                        'closed': True, 'capacity_released': False})
    api = client(receipt)
    monkeypatch.setattr(api, 'my_submissions', lambda: {'nickname': 'synthetic', 'points': 0})
    monkeypatch.setattr(identity, '_load_config', lambda: {})
    monkeypatch.setattr(identity, '_client', lambda cfg: api)
    capsys.readouterr()
    assert identity.cmd_status(SimpleNamespace(json=True)) == 0
    output = json.loads(capsys.readouterr().out)
    assert calls == ['GET']
    assert output['write_recovery'][0]['status'] == 'unknown_reconciled'
    assert output['write_recovery'][0]['execution_allowed'] is False
    assert api.runner_close(body)['closed'] is True
    assert calls == ['GET']


def test_status_cannot_confirm_release_from_other_evidence():
    body = {'session_id': 'a'*32, 'batch_id': 'b'*32, 'device_generation': 2, 'evidence_id': 'c'*32,
            'schema_version': 1, 'execution_manifest_sha256': 'd'*64, 'exit_state': 'confirmed',
            'process_tree': 'confirmed_absent', 'owned_containers': 'confirmed_absent'}
    def lost(request):
        raise httpx.ReadError('synthetic')
    with pytest.raises(ApiError):
        client(lost).release_runner_capacity(body)
    def foreign(request):
        assert request.method == 'GET'
        return httpx.Response(200, json={'session_id': body['session_id'], 'batch_id': body['batch_id'],
            'closed': True, 'capacity_released': True, 'device_generation': 2,
            'release_evidence_id': 'd'*32, 'release_evidence_sha256': 'e'*64})
    result = write_recovery_status.inspect_exits(client(foreign))
    assert result[0]['status'] == 'unknown_unreconciled' and result[0]['request_saved']


def test_status_parser_accepts_recovery_json(monkeypatch):
    from dradar import cli
    seen = []
    monkeypatch.setattr(cli, 'cmd_status', lambda args: seen.append(args.json) or 0)
    assert cli.main(['status', '--json']) == 0
    assert seen == [True]
