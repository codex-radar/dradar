"""Native Windows liveness and old/new byte-zero lease exclusion."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import pytest
from dradar import fleet

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='native Windows process/mandatory-lock contract')
ROOT = Path(__file__).parents[1]


def wait_file(path):
    deadline = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail('fixture did not become ready: ' + str(path))
        time.sleep(.02)


def test_readonly_liveness_never_signals_live_child(monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    monkeypatch.setattr(fleet.os, 'kill', lambda *a: pytest.fail('liveness sent a signal'))
    try:
        for _ in range(10):
            assert fleet._pid_alive(child.pid)
            assert child.poll() is None
        assert not fleet._pid_alive(0)
        assert not fleet._pid_alive(True)
        assert not fleet._pid_alive(2**40)
        child.terminate()
        child.wait(timeout=10)
        assert not fleet._pid_alive(child.pid)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.parametrize('legacy_holder', [True, False])
def test_legacy_and_new_controllers_share_original_lock(tmp_path, legacy_holder):
    installed_archive = Path(os.environ.get('FLEET_203_ARTIFACT', ROOT / 'evidence/installed203.pyz'))
    assert installed_archive.exists(), 'native fixture must provide released203 archive'
    installed = tmp_path / 'installed203'
    with zipfile.ZipFile(installed_archive) as bundle:
        bundle.extractall(installed)
    home = tmp_path / 'home'
    ready, stop = tmp_path / 'ready', tmp_path / 'stop'
    script = '''
import sys,time
from pathlib import Path
from dradar import fleet
home,ready,stop=map(Path,sys.argv[1:])
fleet._prepare_dirs(home)
state=fleet._initial_state('fixture-controller',None)
state['status']='active'
fleet._write_state(home,state)
with fleet._controller_lease(home,'fixture-controller'):
    ready.touch()
    deadline=time.monotonic()+20
    while not stop.exists() and time.monotonic()<deadline: time.sleep(.02)
'''
    env = {**os.environ, 'PYTHONPATH': str(installed if legacy_holder else ROOT / 'src')}
    child = subprocess.Popen([sys.executable, '-c', script, str(home), str(ready), str(stop)],
                             env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        wait_file(ready)
        lock = fleet._root(home) / fleet.CONTROLLER_LOCK_FILE
        # The old mandatory locked JSON is unreadable: this is baseline proof,
        # not a simulated PermissionError or a signal sent to a real user PID.
        with pytest.raises(OSError):
            lock.read_text()
        if legacy_holder:
            assert not fleet.controller_is_active(home)  # no readable v8 identity
            with pytest.raises(fleet.FleetControllerUpdatePending):
                fleet.prepare_new_batch_runtime(home)
            with pytest.raises(fleet.FleetControllerUpdatePending):
                fleet._ensure_controller(home)
        else:
            assert fleet.controller_is_active(home)
            assert not fleet._controller_lease_matches(home, 'stale-controller')
            identity = lock.with_suffix('.identity.json')
            record = json.loads(identity.read_text())
            original = dict(record)
            record['pid'] += 1
            identity.write_text(json.dumps(record))
            assert not fleet.controller_is_active(home)
            identity.write_text(json.dumps(original))
            assert fleet.controller_is_active(home)
        # New contender cannot bypass old byte0, nor can old203 bypass new.
        contender = '''
import sys
from pathlib import Path
from dradar import fleet
try:
    with fleet._controller_lease(Path(sys.argv[1]),'contender'):
        raise SystemExit('unsafe dual ownership')
except fleet.FleetBusy:
    print('exclusive')
'''
        contender_env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src' if legacy_holder else installed)}
        result = subprocess.run([sys.executable, '-c', contender, str(home)], env=contender_env,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'exclusive'
        stop.touch()
        child.wait(timeout=10)
        assert not fleet.controller_is_active(home)
        with fleet._controller_lease(home, 'new-after-exit'):
            assert fleet._lock_is_held(lock)
    finally:
        stop.touch()
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


def test_windows_access_denied_does_not_claim_process_death(monkeypatch):
    import ctypes
    from types import SimpleNamespace
    from unittest.mock import Mock
    kernel = SimpleNamespace(OpenProcess=Mock(return_value=None),
                             WaitForSingleObject=Mock(), CloseHandle=Mock())
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: kernel)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 5)
    assert fleet._windows_pid_alive(1234) is True
    assert kernel.OpenProcess.call_args.args == (0x00100000, False, 1234)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 87)
    assert fleet._windows_pid_alive(1234) is False
    assert not kernel.WaitForSingleObject.called
    assert not kernel.CloseHandle.called
