"""Built pyz, real launcher + go + worker subprocesses, loopback server only."""
import importlib.util
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from test_mixed_go import BATCHES, PICKS

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("workers", [1, 4])
@pytest.mark.parametrize("scenario", ["complete", "partial-claim", "spawn-fail", "late-child"])
def test_real_pyz_go_scopes_every_checkout_and_preserves_batch_boundary(tmp_path, scenario, workers):
    if scenario == 'late-child' and workers == 1:
        pytest.skip('late sibling requires multiple workers')
    if os.environ.get('PROBE_BASELINE_ROOT') and (workers != 4 or scenario != 'complete'):
        pytest.skip('baseline control is the reported four-worker case')
    spec = importlib.util.spec_from_file_location('build_0105', ROOT / 'scripts/ota_release.py')
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    artifact = tmp_path / 'candidate.pyz'
    release._build_zipapp(Path(os.environ.get('PROBE_BASELINE_ROOT', ROOT)), artifact, version='0.5.206', sequence=27,
                         commit='c8013b268335e06233514a7b4e07dc3e6e605af4', tree='b' * 40,
                         target=('windows' if os.name == 'nt' else 'macos' if sys.platform == 'darwin' else 'linux', 'arm64' if platform.machine().lower() in {'arm64', 'aarch64'} else 'x86_64'))
    state = {'active': [], 'claims': [], 'checkouts': [], 'completed': [], 'sessions': {}, 'overlap': False, 'peak': 0}
    require_overlap = workers > 1 and scenario in {'complete', 'late-child'} and not os.environ.get('PROBE_BASELINE_ROOT')
    state['model_ready_batches'] = []
    state['model_overlap'] = False
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            path = urlsplit(self.path)
            query = parse_qs(path.query)
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            data = json.loads(raw) if raw and 'application/json' in self.headers.get('Content-Type', '') else {k: v[0] for k,v in parse_qs(raw.decode()).items()}
            status = 200
            with lock:
                if path.path == '/api/v1/run-plans/capabilities':
                    # This loopback server represents the supported protocol;
                    # keep the real client's capability gate in the pyz path.
                    result = {'schema_version': 1,
                              'capabilities': ['runner-reservation-v1'],
                              'stop_generation_cas': True,
                              'close_releases_capacity': False}
                elif path.path == '/api/v1/assignment/claim':
                    state['claims'].append(data)
                    a = dict(data, agent='fixture', expires_at='2099-01-01T00:00:00Z', assignment_id=str(len(state['claims'])), batch_id=BATCHES[0 if data['model'] == 'gemini-test' else 1])
                    state['active'].append(a)
                    result = {'assignment': a}
                    if scenario == 'partial-claim' and len(state['claims']) == 3:
                        state['active'].remove(a)
                        status, result = 503, {'detail': 'injected claim failure'}
                elif path.path == '/fixture/model-ready':
                    row = next(a for a in state['active'] if a['assignment_id'] == data['assignment_id'])
                    assert row.get('started_at'), row
                    if row['batch_id'] not in state['model_ready_batches']:
                        state['model_ready_batches'].append(row['batch_id'])
                    state['model_overlap'] = set(state['model_ready_batches']) == set(BATCHES)
                    result = {'ready': state['model_overlap']}
                elif path.path == '/fixture/overlap-ready':
                    result = {'ready': state['model_overlap']}
                elif path.path == '/fixture/drained':
                    batch = query.get('batch_id', [None])[0]
                    result = {'drained': not any(a['batch_id'] == batch for a in state['active'])}
                elif path.path == '/api/v1/assignment':
                    batch = query.get('batch_id', [None])[0]
                    default = state['active'][-1]['batch_id'] if state['active'] else None
                    rows = [dict(a) for a in state['active'] if a['batch_id'] == (batch or default)]
                    result = {'active': rows, 'free_pick': True}
                    if batch and not rows:
                        status, result = 404, {'code': 'claim_batch_not_found', 'detail': 'active batch not found'}
                elif path.path == '/api/v1/runner/heartbeat':
                    state['sessions'][data['session_id']] = data
                    result = {'accepted': True}
                elif path.path == '/api/v1/runner/close':
                    result = {'accepted': True}
                elif path.path == '/api/v1/assignment/checkout':
                    batch = data.get('batch_id')
                    session = state['sessions'].get(data.get('session_id'))
                    assert session, data
                    if batch:
                        assert session.get('batch_id') == batch, (session, data)
                    default = state['active'][-1]['batch_id'] if state['active'] else None
                    rows = [a for a in state['active'] if a['batch_id'] == (batch or default) and not a.get('started_at')]
                    if rows:
                        a = rows[0]
                        a.update(started_at='2026-09-15T00:00:00Z', execution_state='running', heartbeat_running=True)
                        state['checkouts'].append({'assignment_id': a['assignment_id'], 'scope': batch, 'batch': a['batch_id']})
                        running = {a['batch_id'] for a in state['active'] if a.get('started_at')}
                        state['overlap'] |= len(running) == 2
                        state['peak'] = max(state['peak'], sum(bool(a.get('started_at')) for a in state['active']))
                        result = {'assignment': dict(a)}
                    else:
                        result = {'assignment': None}
                elif path.path == '/fixture/complete':
                    assert not require_overlap or state['model_overlap'], 'completion before both batch models reached the barrier'
                    state['completed'].append(data['assignment_id'])
                    state['active'] = [a for a in state['active'] if a['assignment_id'] != data['assignment_id']]
                    result = {'ok': True}
                else:
                    status, result = 404, {'detail': path.path}
            body = json.dumps(result).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    fixture = tmp_path / 'fixture'
    fixture.mkdir()
    shutil.copyfile(ROOT / 'tests/mixed_go_probe.py', fixture / 'sitecustomize.py')
    env = {k:v for k,v in os.environ.items() if not k.startswith('DRADAR_') and 'proxy' not in k.lower()}
    env.update(PYTHONPATH=str(fixture), DRADAR_HOME=str(tmp_path / 'home'), PROBE_ARTIFACT=str(artifact), PROBE_SERVER=f'http://127.0.0.1:{httpd.server_port}')
    if require_overlap:
        env['PROBE_OVERLAP_BARRIER'] = '1'
    if scenario == 'late-child':
        env['PROBE_LATE_CHILD_INDEX'] = '3'
    if scenario == 'spawn-fail':
        env['PROBE_SPAWN_FAIL'] = '1'
    argv = [sys.executable, str(artifact), 'go', '-y', '--workers', str(workers), '--keep']
    for pick in PICKS:
        argv += ['--pick', pick]
    try:
        result = subprocess.run(argv, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=35)
        if scenario == 'spawn-fail':
            assert result.returncode != 0, result.stdout + result.stderr
            assert len(state['claims']) == 4
            assert not state['checkouts']
            env.pop('PROBE_SPAWN_FAIL')
            for batch in BATCHES:
                result = subprocess.run([sys.executable, str(artifact), 'resume', '-y', '--workers', '2', '--batch-id', batch], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=35)
                assert result.returncode == 0, result.stdout + result.stderr
        if scenario == 'partial-claim':
            assert result.returncode != 0, result.stdout + result.stderr
            assert len(state['claims']) == 3
            assert not state['checkouts']
            assert BATCHES[0] in result.stdout
            result = subprocess.run([sys.executable, str(artifact), 'resume', '-y', '--workers', '2', '--batch-id', BATCHES[0]], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=35)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert result.returncode == 0, result.stdout + result.stderr
    assert state['peak'] <= (workers if scenario in {'complete', 'late-child'} else 2)
    if scenario == 'partial-claim':
        assert len(state['claims']) == 3  # resume never repeats a claim POST
        assert sorted(state['completed']) == ['1', '2']
        assert not state['active']
        return
    if require_overlap:
        assert state['model_overlap'], state
        assert set(state['model_ready_batches']) == set(BATCHES), state
    assert len(state['claims']) == 4
    assert sorted(state['completed']) == ['1', '2', '3', '4']
    if os.environ.get('PROBE_BASELINE_ROOT'):
        assert all(a['scope'] is None for a in state['checkouts'])
        assert not state['overlap'], state
    else:
        assert all(a['scope'] == a['batch'] for a in state['checkouts'])
        assert state['overlap'] == (workers > 1 and scenario in {'complete', 'late-child'}), state
    if os.environ.get('PROBE_REPORT'):
        Path(os.environ['PROBE_REPORT']).write_text(json.dumps({'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'fixture_note': 'Unsigned test build with synthetic tree metadata; no release artifact or provider run', 'state': state, 'stdout': result.stdout, 'stderr': result.stderr}, indent=2))
    assert not state['active']
