import importlib
import importlib.util
import io
import json
from pathlib import Path

import pytest
from dradar import bootstrap_receipt

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_bootstrap_test', ROOT / 'scripts/build_bootstrap.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

@pytest.fixture
def bootstrap(monkeypatch, tmp_path):
    manifest = builder.build(ROOT, tmp_path / 'wheel')
    monkeypatch.syspath_prepend(str(tmp_path / 'wheel' / manifest['filename']))
    return importlib.import_module('dradar_bootstrap')

def arguments():
    return ['--revision=' + 'a' * 40, '--server=https://api.example.invalid',
            '--plan=inert_plan_code_123456789', '--locale=en-US']

def test_wheel_is_deterministic_and_dependency_free(tmp_path):
    import zipfile
    first = builder.build(ROOT, tmp_path / 'one')
    second = builder.build(ROOT, tmp_path / 'two')
    assert first == second
    with zipfile.ZipFile(tmp_path / 'one' / first['filename']) as wheel:
        assert wheel.testzip() is None
        assert b'Requires-Dist' not in wheel.read('dradar_bootstrap-0.1.0.dist-info/METADATA')
        assert wheel.read('dradar_bootstrap_receipt.py') == (ROOT / 'src/dradar/bootstrap_receipt.py').read_bytes()
    with pytest.raises(FileExistsError): builder.build(ROOT, tmp_path / 'one')

def test_git_retry_uses_same_fixed_argv_and_one_private_cache(bootstrap, monkeypatch):
    calls = []
    def child(command, env, args):
        calls.append((command, env))
        return (1, b'Git operation failed', False, False) if len(calls) == 1 else (0, b'', True, False)
    monkeypatch.setattr(bootstrap, 'run_child', child)
    monkeypatch.setattr(bootstrap, 'report', lambda *_: pytest.fail('success must not report'))
    assert bootstrap.main(arguments()) == 0
    assert len(calls) == 2 and calls[0][0] == calls[1][0]
    assert calls[1][1]['UV_CACHE_DIR'] != calls[0][1].get('UV_CACHE_DIR')
    assert not Path(calls[1][1]['UV_CACHE_DIR']).exists()
    assert calls[0][0][:6] == ['uvx', '--no-config', '--no-env-file', '--python', '3.13', '--from']
    assert calls[0][0][7] == 'dradar-session'

def test_two_git_failures_report_once(bootstrap, monkeypatch):
    calls, reports = [], []
    monkeypatch.setattr(bootstrap, 'run_child', lambda *args: calls.append(args) or (1, b'failed to fetch', False, False))
    monkeypatch.setattr(bootstrap, 'report', lambda *args: reports.append(args) or 'received')
    assert bootstrap.main(arguments()) == 1
    assert len(calls) == 2
    assert reports == [('https://api.example.invalid', 'inert_plan_code_123456789', 'uvx-source-resolution')]

@pytest.mark.parametrize('code,stderr', [(2, b'Please confirm'), (1, b'Git operation failed in a task'), (130, b'')])
def test_loaded_runtime_exit_is_not_a_bootstrap_failure(bootstrap, monkeypatch, code, stderr):
    calls = []
    monkeypatch.setattr(bootstrap, 'run_child', lambda *args: calls.append(args) or (code, stderr, True, False))
    monkeypatch.setattr(bootstrap, 'report', lambda *_: pytest.fail('loaded runtime must not report'))
    assert bootstrap.main(arguments()) == code and len(calls) == 1

def test_zero_exit_without_receipt_does_not_claim_success(bootstrap, monkeypatch):
    reports = []
    monkeypatch.setattr(bootstrap, 'run_child', lambda *_: (0, b'', False, False))
    monkeypatch.setattr(bootstrap, 'report', lambda *args: reports.append(args) or 'queued')
    assert bootstrap.main(arguments()) == 1 and reports[0][-1] == 'bootstrap-unknown'

@pytest.mark.parametrize('extra', [['--source=unknown'], ['--revision=main'], ['--server=https://other.invalid/path'], ['--choice=install']])
def test_invalid_scope_cannot_execute_or_report(bootstrap, monkeypatch, extra):
    monkeypatch.setattr(bootstrap, 'run_child', lambda *_: pytest.fail('invalid scope cannot execute'))
    monkeypatch.setattr(bootstrap, 'report', lambda *_: pytest.fail('invalid scope cannot report'))
    with pytest.raises(SystemExit) as exc: bootstrap.main(arguments() + extra)
    assert exc.value.code == 2

def test_report_queue_contains_no_capability_or_exception(bootstrap, monkeypatch, tmp_path):
    class Failed:
        def open(self, *_args, **_kw): raise OSError('inert private error')
    monkeypatch.setattr(bootstrap, 'build_opener', lambda _: Failed())
    monkeypatch.setenv('DRADAR_HOME', str(tmp_path))
    assert bootstrap.report('https://api.example.invalid', 'inert_private_capability', 'uvx-source-resolution') == 'queued'
    path = next((tmp_path / 'failure-reports').glob('*.json'))
    raw = path.read_text()
    assert 'inert_private_capability' not in raw and 'inert private error' not in raw
    payload = json.loads(raw)
    assert payload['detail'] == {} and payload['failure_code'] == 'uvx-source-resolution'
    assert set(payload) == {'schema', 'report_key', 'source', 'phase', 'failure_kind', 'failure_code',
                           'client_version', 'platform', 'occurred_at', 'detail', '_attempts', '_last_attempt_at'}
    assert path.stat().st_mode & 0o777 == 0o600
    assert bootstrap.NoRedirect().redirect_request(None) is None

def test_streaming_relay_redacts_capabilities_across_writes(bootstrap):
    secret = b'inert_private_plan_code'
    class Pipe:
        chunks = iter([b'Progress\n' + secret[:8], secret[8:] + b'\n', b''])
        def read(self, _): pytest.fail('must use read1')
        def read1(self, _): return next(self.chunks)
        def close(self): pass
    output = io.StringIO()
    bootstrap._relay(Pipe(), output, bytearray(), secret)
    assert output.getvalue() == 'Progress\n[redacted]\n'

def receipt_env(monkeypatch, tmp_path):
    directory = tmp_path / 'dradar-start-fixture'
    directory.mkdir(mode=0o700)
    nonce = 'b' * 32
    path = directory / f'ready-{nonce}.json'
    monkeypatch.setenv(bootstrap_receipt.PATH_ENV, str(path))
    monkeypatch.setenv(bootstrap_receipt.NONCE_ENV, nonce)
    return path, nonce

def test_private_receipt_binds_nonce_and_revision(monkeypatch, tmp_path):
    path, nonce = receipt_env(monkeypatch, tmp_path)
    assert bootstrap_receipt.signal_ready('a' * 40)
    assert bootstrap_receipt.ready(path, nonce, 'a' * 40)
    assert not bootstrap_receipt.ready(path, nonce, 'c' * 40)
    assert not bootstrap_receipt.ready(path, 'c' * 32, 'a' * 40)
    with pytest.raises(FileExistsError): bootstrap_receipt.signal_ready('a' * 40)
    assert path.stat().st_mode & 0o777 == 0o600

def test_invalid_receipt_environment_cannot_signal_readiness(monkeypatch, tmp_path):
    monkeypatch.delenv(bootstrap_receipt.PATH_ENV, raising=False)
    monkeypatch.delenv(bootstrap_receipt.NONCE_ENV, raising=False)
    assert bootstrap_receipt.signal_ready('a' * 40) is False
    path, _ = receipt_env(monkeypatch, tmp_path)
    monkeypatch.setenv(bootstrap_receipt.NONCE_ENV, 'invalid')
    with pytest.raises(ValueError): bootstrap_receipt.signal_ready('a' * 40)
    assert not path.exists()
