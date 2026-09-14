"""Real parent -> detached coordinator -> pool -> worker OTA process chain."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

import pytest

from dradar.ota import RolloutContext, SafePointSnapshot, UpdateRuntime
from dradar.ota.integration import COMPATIBILITY, _self_test, store_trusted_keys
from dradar.flight_recorder import FlightRecorder
from test_ota_runtime import signed_release, sign_document, Client, Response

ROOT = Path(__file__).parents[1]


def build(path):
    spec = importlib.util.spec_from_file_location('build_fleet_probe', ROOT / 'scripts/ota_release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._build_zipapp(ROOT, path, version='0.5.206', sequence=27,
                         commit='c8013b268335e06233514a7b4e07dc3e6e605af4',
                         tree='0' * 40, target=('linux', 'x86_64'))


def stage(home, artifact):
    body = artifact.read_bytes()
    doc, keys = signed_release()
    doc.pop('signature')
    doc.update(version='0.5.206', sequence=27, release_id='fleet-local-candidate')
    for item in doc['artifacts']:
        item.update(filename=f"candidate-{item['os']}-{item['arch']}.pyz", size=len(body), sha256=hashlib.sha256(body).hexdigest())
    doc = sign_document(doc)
    store_trusted_keys(keys, home)
    runtime = UpdateRuntime(home / 'ota', recorder=FlightRecorder(home), download_client=Client(Response([body])))
    assert runtime.prepare(doc, trusted_keys=keys, current_version='0.5.203', committed_sequence=0,
                           compatibility=COMPATIBILITY, rollout=RolloutContext(subject='fixture')).eligible
    runtime.activate_and_self_test(SafePointSnapshot(), _self_test)
    assert json.loads((home / 'ota/current.json').read_text())['version'] == '0.5.206'


def wait_file(path):
    deadline = time.monotonic() + 30
    while not path.exists():
        if time.monotonic() > deadline:
            logs = list(path.parent.rglob('*.log'))
            pytest.fail('timeout: ' + str(path) + '\n' + '\n'.join(p.read_text() for p in logs))
        time.sleep(.05)


@pytest.mark.parametrize('mode', ['ota', 'source'])
def test_real_fleet_descendants_retain_payload_after_parent_exits(tmp_path, mode):
    installed_archive = Path(os.environ.get('FLEET_203_ARTIFACT', ROOT / 'evidence/installed203.pyz'))
    if mode == 'ota' and not installed_archive.exists():
        pytest.skip('provide immutable released203 pyz via FLEET_203_ARTIFACT')
    installed = tmp_path / 'installed203'
    if mode == 'ota':
        assert hashlib.sha256(installed_archive.read_bytes()).hexdigest() == '33116fa2701abf3efa68f3cd4212c7a78b990fa63210e7b8be463dc23302ed6d'
        with zipfile.ZipFile(installed_archive) as bundle:
            bundle.extractall(installed)
    else:
        installed = ROOT / 'src'
    home = tmp_path / 'home'
    home.mkdir()
    hook = tmp_path / 'hook'
    hook.mkdir()
    shutil.copyfile(ROOT / 'tests/fleet_ota_probe.py', hook / 'sitecustomize.py')
    artifact = tmp_path / 'candidate.pyz'
    build(artifact)
    if mode == 'ota':
        stage(home, artifact)
    # Prevent network discovery while preserving genuine verification/dispatch.
    (home / 'ota').mkdir(exist_ok=True)
    (home / 'ota/discovery.json').write_text(json.dumps({'next_check_at': time.time() + 300}))
    env = {k: v for k, v in os.environ.items() if not k.startswith('DRADAR_')}
    env.update(DRADAR_HOME=str(home), FLEET_PROBE_ROOT=str(tmp_path),
               PYTHONPATH=os.pathsep.join((str(hook), str(installed))))
    command = [sys.executable, '-m', 'dradar.launcher', 'probe-parent']
    if mode == 'ota':
        installed_version = subprocess.run([sys.executable, '-m', 'dradar.cli', '--version'],
                                           env=env, cwd=tmp_path, capture_output=True, text=True,
                                           timeout=10, check=True)
        assert installed_version.stdout.strip() == '0.5.203'
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30, cwd=tmp_path)
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        (tmp_path / 'parent-exited').touch()
        wait_file(tmp_path / 'coordinator-after-parent.json')
        if mode == 'ota' and os.name == 'nt':
            held = Path(json.loads((tmp_path / 'coordinator-after-parent.json').read_text())['argv0'])
            with pytest.raises(OSError):
                with held.open('r+b') as stream:
                    stream.write(b'corrupt')
            with pytest.raises(OSError):
                held.unlink()
        (tmp_path / 'continue-pool').touch()
        wait_file(tmp_path / 'pool-exit')
        assert (tmp_path / 'pool-exit').read_text() == '0', '\n'.join(p.read_text() for p in home.rglob('*.log'))
        roles = ['parent', 'coordinator', 'coordinator-after-parent', 'pool', 'worker']
        reports = [json.loads((tmp_path / (role + '.json')).read_text()) for role in roles]
        for report in reports:
            assert report['version'] == '0.5.206'
            assert report['pythonpath'] == env['PYTHONPATH']
            if mode == 'ota':
                assert report['activity'] is True
            for name in ('fleet', 'runloop'):
                identity = report['dradar.' + name]
                assert identity['sha256'] == hashlib.sha256((ROOT / 'src/dradar' / (name + '.py')).read_text().encode()).hexdigest()
                if mode == 'ota':
                    assert 'installed203' not in identity['origin']
        assert len({r['pid'] for r in reports}) == 4
        for pid in {r['pid'] for r in reports}:
            wait_file(tmp_path / f'{pid}.exited')
        if mode == 'ota':
            assert json.loads((home / 'ota/current.json').read_text())['version'] == '0.5.206'
            from dradar.ota.activity import active_invocations
            from dradar.ota.state import UpdateLock
            deadline = time.monotonic() + 10
            while True:
                with UpdateLock(home / 'ota/launch.lock'):
                    active = active_invocations(home / 'ota')
                if not active or time.monotonic() >= deadline:
                    break
                time.sleep(.05)
            assert not active
            if os.name == 'nt':
                assert not held.exists()

    finally:
        state = home / 'fleet/state.json'
        if state.exists():
            pid = json.loads(state.read_text()).get('pid')
            if pid and not (tmp_path / f'{pid}.exited').exists():
                try: os.kill(pid, 15)
                except OSError as exc:
                    if os.name != 'nt' and not isinstance(exc, ProcessLookupError):
                        raise
                    if os.name == 'nt' and getattr(exc, 'winerror', None) != 87:
                        raise
