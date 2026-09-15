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


def test_update_lock_contender_never_writes_during_holder_metadata_window(tmp_path, monkeypatch):
    """Hold byte0 over an empty file, reproducing the precise native race."""
    from dradar.ota.state import UpdateLock, UpdateLockBusy
    lock_path, ready, stop = (tmp_path / name for name in ('launch.lock', 'ready', 'stop'))
    script = '''
import os,sys,time
from pathlib import Path
from dradar.ota.state import UpdateLock
path,ready,stop=map(Path,sys.argv[1:])
with UpdateLock(path) as lock:
    os.ftruncate(lock._fd,0)
    ready.touch()
    deadline=time.monotonic()+15
    while not stop.exists() and time.monotonic()<deadline: time.sleep(.02)
'''
    child = subprocess.Popen([sys.executable, '-c', script, str(lock_path), str(ready), str(stop)],
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        wait_file(ready)
        assert lock_path.stat().st_size == 0
        installed = tmp_path / 'installed203'
        archive = Path(os.environ.get('FLEET_203_ARTIFACT', ROOT / 'evidence/installed203.pyz'))
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(installed)
        baseline = '''
import sys
from pathlib import Path
from dradar import __version__
from dradar.ota.state import UpdateLock
assert __version__ == '0.5.203'
try:
    with UpdateLock(Path(sys.argv[1])): raise SystemExit('unsafe acquired')
except PermissionError:
    print('baseline-prelock-write-rejected')
'''
        before = subprocess.run([sys.executable, '-c', baseline, str(lock_path)],
                                env={**os.environ, 'PYTHONPATH': str(installed)},
                                capture_output=True, text=True, timeout=10)
        assert before.returncode == 0, before.stderr
        assert before.stdout.strip() == 'baseline-prelock-write-rejected'
        opened = []
        original_open = os.open
        def capture_open(*a, **k):
            fd = original_open(*a, **k)
            opened.append(fd)
            return fd
        with monkeypatch.context() as patch:
            patch.setattr(os, 'open', capture_open)
            with pytest.raises(UpdateLockBusy):
                with UpdateLock(lock_path, timeout_seconds=.1):
                    pytest.fail('contender bypassed holder')
        assert opened
        for fd in opened:
            with pytest.raises(OSError): os.fstat(fd)
        assert lock_path.stat().st_size == 0
        assert child.poll() is None
        stop.touch()
        child.wait(timeout=10)
        # The same empty file now supports a genuine lock and metadata write.
        with UpdateLock(lock_path):
            assert lock_path.stat().st_size > 0
        with UpdateLock(tmp_path / 'cold-empty.lock'):
            pass
    finally:
        stop.touch()
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


@pytest.mark.parametrize('timeout', [False, True])
def test_runtime_inspection_native_windows_normal_and_tree_timeout(tmp_path, monkeypatch, timeout):
    """Run real helper/descendant PIDs through Windows taskkill, without models."""
    from dradar import child_entrypoint
    identity = tmp_path / 'inspection-pids.json'
    script = '''
import json,os,subprocess,sys,time
from pathlib import Path
path,mode=sys.argv[1:3]
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)' if mode=='timeout' else 'pass'])
Path(path).write_text(json.dumps({'helper':os.getpid(),'child':child.pid}))
child.wait()
print(json.dumps({'ok':True,'result':[1,[],{'account_limit':4}]}))
'''
    monkeypatch.setattr(child_entrypoint, 'command',
                        lambda *_: [sys.executable, '-c', script, str(identity), 'timeout' if timeout else 'normal'])
    monkeypatch.setattr(child_entrypoint, 'popen_options', lambda env: {})
    monkeypatch.setattr(fleet, 'REQUEST_TIMEOUT_SECONDS', 8)
    # An unrelated local fixture must remain alive throughout cleanup.
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        call = lambda: fleet._resolve_workers_in_runtime(
            1, '1' * 32, {'batches': {}}, None, sys.executable, {})
        if timeout:
            with pytest.raises(fleet.FleetError, match='could not inspect'):
                call()
        else:
            assert call() == (1, [], {'account_limit': 4})
        pids = json.loads(identity.read_text())
        deadline = time.monotonic() + 2
        while any(fleet._pid_alive(pid) for pid in pids.values()) and time.monotonic() < deadline:
            time.sleep(.02)
        assert not any(fleet._pid_alive(pid) for pid in pids.values())
        assert unrelated.poll() is None
    finally:
        if identity.exists():
            taskkill = str(Path(os.environ['SystemRoot']) / 'System32/taskkill.exe')
            for pid in json.loads(identity.read_text()).values():
                if fleet._pid_alive(pid):
                    subprocess.run([taskkill, '/PID', str(pid), '/T', '/F'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.parametrize("release", [True, False])
def test_atomic_state_replace_with_native_reader(tmp_path, release, monkeypatch):
    import concurrent.futures
    import threading
    import ctypes
    from ctypes import wintypes
    target = tmp_path / "state.json"
    target.write_text('{"generation": 1}')
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # Real readable handle: share reads/writes, deliberately deny delete/replace.
    handle = kernel.CreateFileW(str(target), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    denied = threading.Event()
    real_replace = os.replace
    def observed_replace(source, destination):
        try:
            return real_replace(source, destination)
        except OSError as exc:
            if exc.winerror in {5, 32, 33}:
                denied.set()
            raise
    monkeypatch.setattr(fleet.os, "replace", observed_replace)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        try:
            started = time.monotonic()
            future = executor.submit(fleet._atomic_json, target, {"generation": 2})
            if release:
                assert denied.wait(timeout=2), "native replacement did not observe denied sharing"
                assert not future.done()
                assert json.loads(target.read_text()) == {"generation": 1}
                assert kernel.CloseHandle(handle)
                handle = None
                future.result(timeout=3)
                assert json.loads(target.read_text()) == {"generation": 2}
            else:
                with pytest.raises(OSError) as caught:
                    future.result(timeout=3)
                assert caught.value.winerror in {5, 32, 33}
                assert time.monotonic() - started >= .9
                assert json.loads(target.read_text()) == {"generation": 1}
            assert not list(tmp_path.glob(".state.json.*.tmp"))
        finally:
            if handle is not None:
                kernel.CloseHandle(handle)
