"""Must run on real Windows; no platform mocking and no external account calls."""
import os
import sys
import subprocess
from pathlib import Path


def test_real_windows_host():
    assert sys.platform=='win32' and os.name=='nt'


def test_real_replacement_denying_handle_survives_child_lifetime(tmp_path):
    from dradar.ota.integration import _locked_windows_candidate
    from contextlib import ExitStack
    replacement=tmp_path/'replacement.pyz';replacement.write_bytes(b'changed')
    with _locked_windows_candidate(b'verified fixture') as path:
        # A separate native process tries to replace/delete the open candidate.
        program='''import os,sys
from pathlib import Path
p=Path(sys.argv[1]);r=Path(sys.argv[2])
for operation in (lambda:os.replace(r,p),lambda:p.unlink()):
 try:operation()
 except PermissionError:pass
 else:raise AssertionError('candidate handle allowed replacement')
print('denied-both')
'''
        result=subprocess.run([sys.executable,'-c',program,str(path),str(replacement)],capture_output=True,text=True,timeout=10)
        assert result.returncode==0,result.stderr
        assert result.stdout.strip()=='denied-both'
        assert path.read_bytes()==b'verified fixture'
    assert not path.exists()


def test_real_windows_activity_lock_covers_live_process(tmp_path):
    from dradar.ota.activity import active_invocations
    from dradar.ota.state import UpdateLock
    program='''from pathlib import Path
from dradar.ota.activity import register_invocation
import sys
with register_invocation(Path(sys.argv[1])):
 print('ready',flush=True)
 sys.stdin.readline()
'''
    child=subprocess.Popen([sys.executable,'-c',program,str(tmp_path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='ready'
        with UpdateLock(tmp_path/'launch.lock'):assert active_invocations(tmp_path)
        child.communicate('\n',timeout=10)
        assert child.returncode==0
        with UpdateLock(tmp_path/'launch.lock'):assert not active_invocations(tmp_path)
    finally:
        if child.poll() is None:child.terminate();child.wait(timeout=10)


def test_parent_exit_does_not_hide_a_live_registered_child(tmp_path):
    import time
    from dradar.ota.activity import active_invocations
    from dradar.ota.state import UpdateLock
    child_code='''from pathlib import Path
from dradar.ota.activity import register_invocation
import time,sys
root=Path(sys.argv[1])
with register_invocation(root/'ota'):
 (root/'ready').write_text('ready')
 deadline=time.monotonic()+10
 while not (root/'finish').exists() and time.monotonic()<deadline:time.sleep(.02)
(root/'done').write_text('done')
'''
    parent_code='''import subprocess,sys
from pathlib import Path
root=Path(sys.argv[1])
subprocess.Popen([sys.executable,'-c',sys.argv[2],str(root)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
'''
    result=subprocess.run([sys.executable,'-c',parent_code,str(tmp_path),child_code],timeout=5)
    assert result.returncode==0
    try:
        deadline=time.monotonic()+5
        while not (tmp_path/'ready').exists() and time.monotonic()<deadline:time.sleep(.02)
        assert (tmp_path/'ready').exists()
        with UpdateLock(tmp_path/'ota/launch.lock'):assert active_invocations(tmp_path/'ota')
    finally:(tmp_path/'finish').write_text('finish')
    deadline=time.monotonic()+5
    while not (tmp_path/'done').exists() and time.monotonic()<deadline:time.sleep(.02)
    assert (tmp_path/'done').exists()
    with UpdateLock(tmp_path/'ota/launch.lock'):assert not active_invocations(tmp_path/'ota')


def test_real_windows_failed_self_test_rolls_back_to_bundled(tmp_path):
    import hashlib,io,json,zipfile
    from dradar.flight_recorder import FlightRecorder
    from dradar.ota.integration import _self_test
    from dradar.ota.runtime import UpdateRuntime
    from dradar.ota.state import SafePointSnapshot,UpdateState
    from dradar.ota.manifest import PlatformTarget,RolloutContext
    from test_ota_runtime import signed_release,sign_document,Client,Response,compatibility
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:z.writestr('__main__.py','raise SystemExit(9)\n')
    body=stream.getvalue();document,keys=signed_release();document.pop('signature')
    for item in document['artifacts']:
        item['size']=len(body);item['sha256']=hashlib.sha256(body).hexdigest()
    document=sign_document(document)
    runtime=UpdateRuntime(tmp_path/'ota',recorder=FlightRecorder(tmp_path),download_client=Client(Response([body])))
    assert runtime.prepare(document,trusted_keys=keys,current_version='0.5.203',committed_sequence=0,compatibility=compatibility(),rollout=RolloutContext(subject='native-fixture'),target=PlatformTarget('windows','x86_64')).eligible
    assert runtime.activate_and_self_test(SafePointSnapshot(),_self_test)==UpdateState.ROLLED_BACK
    assert json.loads((tmp_path/'ota/current.json').read_text())['legacy_fallback'] is True


def test_real_windows_failed_self_test_preserves_committed_lkg(tmp_path):
    import hashlib, io, json, zipfile
    from dradar.flight_recorder import FlightRecorder
    from dradar.ota.integration import _self_test
    from dradar.ota.runtime import UpdateRuntime
    from dradar.ota.state import SafePointSnapshot, UpdateState
    from dradar.ota.manifest import PlatformTarget, RolloutContext
    from test_ota_runtime import signed_release, sign_document, Client, Response, compatibility

    root = tmp_path / 'ota'
    def candidate(sequence, version, exit_code):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('__main__.py', f'raise SystemExit({exit_code})\n')
        body = stream.getvalue()
        document, keys = signed_release()
        document.pop('signature')
        document.update(sequence=sequence, version=version, release_id=f'native-{sequence}')
        for item in document['artifacts']:
            item.update(size=len(body), sha256=hashlib.sha256(body).hexdigest())
        runtime = UpdateRuntime(root, recorder=FlightRecorder(tmp_path), download_client=Client(Response([body])))
        decision = runtime.prepare(sign_document(document), trusted_keys=keys,
            current_version='0.5.203' if sequence == 600 else '0.6.0',
            committed_sequence=0 if sequence == 600 else 600,
            compatibility=compatibility(), rollout=RolloutContext(subject='native-lkg'),
            target=PlatformTarget('windows', 'x86_64'))
        assert decision.eligible
        return runtime

    good = candidate(600, '0.6.0', 0)
    assert good.activate_and_self_test(SafePointSnapshot(), _self_test) == UpdateState.COMMITTED
    previous = json.loads((root / 'current.json').read_text())
    assert previous['sequence'] == 600 and '\\' not in previous['artifact']
    saved = {name: (root / name).read_bytes() for name in ('current.json', 'last-known-good.json')}
    artifact = root / previous['artifact']
    original = artifact.read_bytes()
    bad = candidate(601, '0.6.1', 9)
    assert bad.activate_and_self_test(SafePointSnapshot(), _self_test) == UpdateState.ROLLED_BACK
    for name, content in saved.items():
        assert (root / name).read_bytes() == content
    assert artifact.read_bytes() == original
    with bad.controller.launch_artifact() as verified:
        assert verified.read_bytes() == original
