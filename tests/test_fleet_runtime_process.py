"""No-model loopback checks of real zipapp Fleet admission and pool startup."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import pytest

ROOT = Path(__file__).resolve().parents[1]
CAP = 'kimi-code-k3-subscription-oauth-node-concurrent-v3'


def build(source, destination):
    spec = importlib.util.spec_from_file_location('runtime_release', source / 'scripts/ota_release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._build_zipapp(source, destination, version='0.5.208', sequence=29,
                         commit='39e7567dc4e76db12748bb17a0ea9b12d6c68294',
                         tree='0' * 40, target=('darwin', 'arm64'))


@pytest.fixture
def runtime(tmp_path):
    source = Path(os.environ.get('FLEET_RUNTIME_SOURCE', ROOT))
    artifact = tmp_path / 'candidate.pyz'
    build(source, artifact)
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    dradar_home = home / '.dradar'
    dradar_home.mkdir(mode=0o700)
    task_repo = tmp_path / 'existing-user-repo'
    task_repo.mkdir()
    (task_repo / 'keep.txt').write_text('unchanged')
    observations = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            path = urlparse(self.path).path
            batch = parse_qs(urlparse(self.path).query).get('batch_id', [None])[0]
            caps = self.headers.get('X-DRadar-Capabilities', '').split(',')
            observations.append((path, batch, CAP in caps))
            if self.headers.get('Authorization') != 'Bearer local-fixture':
                self.send(403, {'detail': 'wrong fixture account'})
            elif path == '/api/v1/whoami':
                self.send(200, {'concurrent_limit': 4, 'claim_limit': 4})
            elif path == '/api/v1/assignment':
                # Odd batches require Kimi; even batches represent another lane.
                needs_kimi = int(batch[-1], 16) % 2 == 1
                if needs_kimi != (CAP in caps):
                    self.send(426, {'detail': 'fixture capability gate'})
                else:
                    self.send(200, {'active': [{'assignment_id': 'fixture-task',
                        'batch_id': batch, 'task_id': 'fixture', 'agent': 'codex',
                        'model': 'gpt-5.4', 'effort': 'high'}], 'free_pick': True})
            else:
                self.send(404, {'detail': 'fixture endpoint unavailable'})
        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(length) or b'{}')
            caps = self.headers.get('X-DRadar-Capabilities', '').split(',')
            observations.append(('POST ' + urlparse(self.path).path, body.get('batch_id'), CAP in caps))
            self.send(200, {'ok': True, 'stop_requested': False})
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (dradar_home / 'config.json').write_text(json.dumps({
        'server': f'http://127.0.0.1:{server.server_port}', 'token': 'local-fixture',
        'tasks_root': str(task_repo / 'tasks'), 'benchmark': 'deep-swe'}))
    # Minimal child environment: no real credential homes, proxies, Docker,
    # provider secrets or OTA discovery; all network is the fixture server.
    env = {'HOME': str(home), 'DRADAR_HOME': str(dradar_home),
           'PATH': str(Path(sys.executable).parent), 'DRADAR_OTA_DISPATCH': '1',
           'NO_PROXY': '127.0.0.1,localhost', 'PYTHONIOENCODING': 'utf-8'}
    if os.name == 'nt':
        env.update({key: os.environ[key] for key in ('SystemRoot', 'TEMP', 'TMP') if key in os.environ})
        env['USERPROFILE'] = str(home)
    coordinator_log = (tmp_path / 'coordinator.log').open('w', encoding='utf-8')
    coordinator = subprocess.Popen([sys.executable, str(artifact), 'fleet', 'serve', '--internal'],
        env={**env, 'DRADAR_FLEET_LAUNCH_ID': 'runtime-fixture'},
        stdout=coordinator_log, stderr=subprocess.STDOUT)
    state_path = dradar_home / 'fleet/state.json'
    deadline = time.monotonic() + 10
    while not state_path.exists():
        assert coordinator.poll() is None
        assert time.monotonic() < deadline
        time.sleep(.05)
    executable = tmp_path / 'kimi'
    executable.write_text('#!/bin/sh\nexit 90\n')
    executable.chmod(0o700)
    credential = tmp_path / 'canonical/credentials/kimi-code.json'
    credential.parent.mkdir(parents=True, mode=0o700)
    credential.write_text(json.dumps({'access_token': 'fixture-only',
        'refresh_token': 'fixture-only', 'token_type': 'Bearer'}))
    credential.chmod(0o600)
    bindings = {'KIMI_CLI_PATH': str(executable), 'KIMI_CREDENTIAL_PATH': str(credential)}
    def add(number, binding=None, workers="1"):
        batch = f'{number:032x}'
        completed = subprocess.run([sys.executable, str(artifact), 'fleet', 'add',
            '--batch-id', batch, '--workers', workers], env={**env, **(binding or {})},
            text=True, capture_output=True, timeout=30)
        return batch, completed
    try:
        yield add, observations, dradar_home, bindings, env, artifact
    finally:
        try:
            before_cleanup = {'coordinator_returncode': coordinator.poll(),
                              'observations': list(observations),
                              'recorded_at': time.time()}
            try:
                # Copy complete process evidence out of the hidden home for CI uploads.
                logs = dradar_home / 'fleet/logs'
                pool_logs = {}
                if logs.exists():
                    for log in logs.glob('*.log'):
                        pool_logs[log.name] = log.read_text(encoding='utf-8', errors='replace')
                before_cleanup['pool_logs'] = pool_logs
                if state_path.exists():
                    before_cleanup['state'] = json.loads(state_path.read_text())
            except (OSError, ValueError) as exc:
                before_cleanup['capture_error'] = repr(exc)
            try:
                (tmp_path / 'lifecycle.json').write_text(json.dumps(before_cleanup, indent=2))
            except OSError:
                pass
        finally:
            if coordinator.poll() is None:
                coordinator.terminate()
            coordinator.wait(timeout=10)
            coordinator_log.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        assert (task_repo / 'keep.txt').read_text() == 'unchanged'
        assert not (task_repo / 'tasks').exists()
        assert not any('/claim' in row[0] or '/checkout' in row[0] for row in observations)


def _assert_pool_binding(observations, batch, expected, result, *, timeout=5):
    # fleet.add may return its startup failure before the pool's finally runs.
    # A background preparing heartbeat is best effort and can be skipped when
    # close wins the scheduling race. The synchronous close is emitted only by
    # the actual pool, after bind_batch, through the same real ApiClient headers.
    deadline = time.monotonic() + timeout
    while not any(row[0] == 'POST /api/v1/runner/close' and row[1] == batch
                  for row in observations):
        if time.monotonic() >= deadline:
            break
        time.sleep(.01)
    lane = [row for row in observations if row[1] == batch and row[0] in (
        '/api/v1/assignment', 'POST /api/v1/runner/heartbeat', 'POST /api/v1/runner/close')]
    details = (observations, result.stdout, result.stderr)
    assert any(row[0] == '/api/v1/assignment' for row in lane), details
    assert any(row[0] == 'POST /api/v1/runner/close' for row in lane), details
    assert all(row[2] == expected for row in lane), details


@pytest.mark.parametrize('close', [None, ('POST /api/v1/runner/close', 'exact', False),
                                  ('POST /api/v1/runner/close', 'foreign', True)])
def test_binding_observation_requires_exact_correct_pool_close(close):
    from types import SimpleNamespace
    observations = [('/api/v1/assignment', 'exact', True)]
    if close is not None:
        observations.append(close)
    with pytest.raises(AssertionError):
        _assert_pool_binding(observations, 'exact', True,
                             SimpleNamespace(stdout='', stderr=''), timeout=0)


@pytest.mark.parametrize("workers", ["1", "auto"])
def test_old_coordinator_new_binding_reaches_pool(runtime, workers):
    add, observations, home, bindings, *_ = runtime
    batch, result = add(1, bindings, workers)
    _assert_pool_binding(observations, batch, True, result)
    state = json.loads((home / 'fleet/state.json').read_text())
    assert batch in state['batches']
    assert 'KIMI_CREDENTIAL_PATH' not in json.dumps(state)


def test_missing_bad_binding_and_wrong_account_fail_closed(runtime):
    add, observations, home, bindings, env, artifact = runtime
    batch, result = add(1)
    assert '426' in result.stderr + result.stdout
    invalid = Path(bindings['KIMI_CREDENTIAL_PATH'])
    invalid.write_text('{}')
    batch2, result = add(3, bindings)
    assert '426' in result.stderr + result.stdout
    config = home / 'config.json'
    cfg = json.loads(config.read_text()); cfg['token'] = 'wrong-local-fixture'
    config.write_text(json.dumps(cfg))
    batch3, result = add(5, bindings)
    assert '403' in result.stderr + result.stdout
    state = json.loads((home / 'fleet/state.json').read_text())
    assert not state['batches']


def test_concurrent_lanes_do_not_share_binding(runtime):
    add, observations, home, bindings, *_ = runtime
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: add(*item), [(1, bindings), (2, {})]))
    for batch, result in results:
        _assert_pool_binding(observations, batch, int(batch, 16) % 2 == 1, result)


@pytest.mark.parametrize('kind', ['unknown', 'missing', 'public', 'symlink', 'relative', 'capability'])
def test_raw_request_rejects_untrusted_runtime_before_api(runtime, kind):
    if os.name == 'nt' and kind == 'public':
        pytest.skip('POSIX mode-bit permissions are not a Windows ACL check')
    _, observations, home, bindings, *_ = runtime
    root = home / 'fleet'
    bad = dict(bindings)
    if kind == 'unknown':
        bad['PYTHONPATH'] = '/untrusted'
    elif kind == 'missing':
        bad['KIMI_CREDENTIAL_PATH'] += '.missing'
    elif kind == 'public':
        Path(bad['KIMI_CREDENTIAL_PATH']).chmod(0o644)
    elif kind == 'symlink':
        path = Path(bad['KIMI_CREDENTIAL_PATH'])
        link = path.with_name('link.json'); link.symlink_to(path)
        bad['KIMI_CREDENTIAL_PATH'] = str(link)
    elif kind == 'relative':
        bad['KIMI_CREDENTIAL_PATH'] = 'credentials/kimi-code.json'
    else:
        bad['X-DRadar-Capabilities'] = CAP
    state = json.loads((root / 'state.json').read_text())
    request = {'request_id': 'bad-runtime', 'controller_id': state['controller_id'],
        'controller_protocol_version': state['controller_protocol_version'],
        'runtime_executable': sys.executable, 'runtime_environment': bad,
        'command': 'add', 'batch_id': '0' * 31 + '1', 'workers': 1}
    staged = root / 'bad-runtime.tmp'
    staged.write_text(json.dumps(request))
    staged.replace(root / 'requests/bad-runtime.json')
    response = root / 'responses/bad-runtime.json'
    deadline = time.monotonic() + 10
    while not response.exists():
        assert time.monotonic() < deadline
        time.sleep(.05)
    result = json.loads(response.read_text())
    assert not result['ok']
    assert 'invalid provider runtime paths' in result['error']
    assert observations == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group probe")
def test_preflight_timeout_reaps_capacity_probe_descendant(monkeypatch, tmp_path):
    from dradar import fleet, child_entrypoint
    pidfile = tmp_path / 'capacity-probe.pid'
    docker = tmp_path / 'docker'
    docker.write_text(
        f'#!{sys.executable}\nimport os,time\n'
        f'open({str(pidfile)!r}, "w").write(str(os.getpid()))\ntime.sleep(30)\n'
    )
    docker.chmod(0o700)
    monkeypatch.setattr(os, 'environ', {'HOME': str(tmp_path), 'PATH': str(tmp_path)})
    script = f'''import sys
sys.path.insert(0, {str(ROOT / 'src')!r})
from dradar import fleet
class Client:
 def set_batch_id(self, value): pass
 def whoami(self): return {{'concurrent_limit': 4}}
 def get_assignment(self): return {{'active': [{{}}]}}
fleet._load_config = lambda: {{}}
fleet._client = lambda cfg: Client()
fleet._resolve_workers('auto', '1' * 32, {{'batches': {{}}}})
'''
    monkeypatch.setattr(child_entrypoint, 'command', lambda *_: [sys.executable, '-c', script])
    monkeypatch.setattr(child_entrypoint, 'popen_options', lambda _env: {})
    # Allow imports to finish on a loaded native CI host before timing out
    # the fake Docker probe (which sleeps for 30 seconds).
    monkeypatch.setattr(fleet, 'REQUEST_TIMEOUT_SECONDS', 10)
    try:
        with pytest.raises(fleet.FleetError, match='could not inspect'):
            fleet._resolve_workers_in_runtime(1, '1' * 32, {'batches': {}}, None, sys.executable, {})
        assert pidfile.exists()
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.01)
        else:
            pytest.fail('capacity probe survived inspection timeout')
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 9)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('outcome', ['success', 'nonzero', 'timeout'])
def test_windows_cleanup_failure_is_not_reported_as_confirmed(monkeypatch, outcome):
    from types import SimpleNamespace
    from dradar import fleet
    calls = []
    class Process:
        pid = 12345
        def poll(self): return None
        def kill(self): calls.append('kill-direct')
        def wait(self, timeout): calls.append('reap-direct')
    monkeypatch.setattr(fleet, 'os', SimpleNamespace(name='nt', environ={'SystemRoot': 'C:/Windows'}))
    def taskkill(command, **kwargs):
        assert command[1:] == ['/PID', '12345', '/T', '/F']
        assert kwargs['timeout'] == 3
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(command, 3)
        return SimpleNamespace(returncode=0 if outcome == 'success' else 1)
    monkeypatch.setattr(subprocess, 'run', taskkill)
    if outcome == 'success':
        fleet._kill_runtime_inspection(Process())
    else:
        with pytest.raises((fleet.FleetError, subprocess.TimeoutExpired)):
            fleet._kill_runtime_inspection(Process())
    assert calls == ['kill-direct', 'reap-direct']
