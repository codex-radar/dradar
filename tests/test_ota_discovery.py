import json
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from dradar.ota import discovery
from dradar.ota.activity import active_invocations, register_invocation
from dradar.ota.state import UpdateLock
from test_ota_runtime import signed_release, TRUSTED_KEYS, BODY


def transport(document, calls, fail=False):
    def handle(request):
        calls.append(str(request.url))
        if fail:
            raise httpx.ConnectError('fixture only',request=request)
        if str(request.url)==discovery.STABLE_URL:
            return httpx.Response(200,json=document)
        return httpx.Response(200,content=BODY)
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_discovery_downloads_verified_candidate_without_activation(tmp_path):
    doc,keys=signed_release()[:2]
    calls=[]
    with transport(doc,calls) as client:
        assert discovery.discover_update(tmp_path,client=client,trusted_keys=keys)=='prepared'
        assert discovery.discover_update(tmp_path,client=client,trusted_keys=keys)=='cached'
    assert len(calls)==2
    assert not (tmp_path/'ota/current.json').exists()
    assert json.loads((tmp_path/'ota/update-state.json').read_text())['state']=='waiting_safe_point'


def test_offline_keeps_current_bundle_and_throttles(tmp_path):
    calls=[]
    with transport({},calls,fail=True) as client:
        assert discovery.discover_update(tmp_path,client=client)=='unavailable'
        assert discovery.discover_update(tmp_path,client=client)=='cached'
    assert calls==[discovery.STABLE_URL]
    assert not (tmp_path/'ota/current.json').exists()


def test_untrusted_manifest_never_downloads_code(tmp_path):
    doc,keys=signed_release()[:2];calls=[]
    with transport(doc,calls) as client:
        assert discovery.discover_update(tmp_path,client=client)=='unavailable'
    assert calls==[discovery.STABLE_URL]


def test_concurrent_discovery_does_not_send_request(tmp_path):
    calls=[]
    with UpdateLock(tmp_path/'ota/discovery.lock'):
        with transport({},calls) as client:
            assert discovery.discover_update(tmp_path,client=client)=='unavailable'
    assert not calls


def test_activity_liveness_and_crash_style_release(tmp_path):
    with UpdateLock(tmp_path/'launch.lock'):
        lease=register_invocation(tmp_path);lease.__enter__()
        assert active_invocations(tmp_path)
        lease.__exit__(None,None,None)
        assert not active_invocations(tmp_path)


def test_deadline_rejects_slow_chunks():
    tick=[0]
    class Response:
        def raise_for_status(self):pass
        def iter_bytes(self,n):
            tick[0]=10
            yield b'late'
    class Client:
        @contextmanager
        def stream(self,*a,**k):yield Response()
    client=discovery.DeadlineClient(Client(),3,lambda:tick[0])
    with client.stream('GET','https://fixture.invalid') as response:
        with pytest.raises((TimeoutError,httpx.HTTPError)):list(response.iter_bytes())


def test_launcher_auto_discovery_real_zip_single_handoff(tmp_path,monkeypatch):
    import io,zipfile,hashlib,sys
    from dradar import launcher
    from dradar.ota import integration
    from test_ota_runtime import sign_document
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('__main__.py', 'import os,sys\nassert os.environ.get("DRADAR_OTA_DISPATCH")=="1"\nprint("candidate-once")\n')
    body=out.getvalue();doc,keys=signed_release()
    doc.pop('signature')
    for item in doc['artifacts']:
        item['size']=len(body);item['sha256']=hashlib.sha256(body).hexdigest()
    doc=sign_document(doc);calls=[]
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200,json=doc) if str(request.url)==discovery.STABLE_URL else httpx.Response(200,content=body)
    monkeypatch.setattr(launcher,'HOME',tmp_path)
    monkeypatch.setattr(discovery,'TRUSTED_KEYS',keys)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(launcher,'discover_update',lambda home:discovery.discover_update(home,client=client,trusted_keys=keys))
        monkeypatch.setattr(sys,'argv',['dradar','--version'])
        assert launcher.main()==0
    assert len(calls)==2
    assert json.loads((tmp_path/'ota/update-state.json').read_text())['state']=='committed'
    assert not active_invocations(tmp_path/'ota')


def test_launcher_dispatch_does_not_discover(tmp_path,monkeypatch):
    from dradar import launcher,cli
    monkeypatch.setattr(launcher,'HOME',tmp_path)
    monkeypatch.setenv('DRADAR_OTA_DISPATCH','1')
    monkeypatch.setattr(launcher,'discover_update',lambda *_:pytest.fail('recursive discovery'))
    monkeypatch.setattr(cli,'main',lambda:17)
    assert launcher.main()==17


def test_launcher_does_not_activate_while_another_invocation_is_alive(tmp_path,monkeypatch):
    from dradar import launcher,cli
    monkeypatch.setattr(launcher,'HOME',tmp_path)
    monkeypatch.setattr(launcher,'discover_update',lambda *_:'pending')
    monkeypatch.setattr(launcher,'activate_prepared_update',lambda *a,**k:pytest.fail('active work switched'))
    monkeypatch.setattr(cli,'main',lambda:0)
    with register_invocation(tmp_path/'ota'):
        assert launcher.main()==0


def test_unsafe_pending_upload_blocks_activation(tmp_path):
    from dradar.ota.integration import runloop_safe_point
    (tmp_path/'pending_uploads.json').symlink_to(tmp_path/'missing')
    assert not runloop_safe_point(home=tmp_path).ready


def test_real_new_pyz_entry_uses_dispatch_once(tmp_path,monkeypatch):
    import io,zipfile,hashlib,sys
    from dradar import launcher, cli
    from dradar.ota import integration
    import subprocess
    from test_ota_runtime import sign_document
    source=Path(__file__).parents[1]/'src'
    marker=tmp_path/'fixture-dispatch.jsonl'
    fixture_cli=("import json,os,sys\nfrom pathlib import Path\n"
                 "def main():\n"
                 f"    with Path({str(marker)!r}).open('a') as stream:\n"
                 "        stream.write(json.dumps({'self_test':getattr(sys.modules['__main__'],'fixture_self_test',False),'pid':os.getpid()})+'\\n')\n"
                 "    print('real-new-entry-once')\n    return 0\n")
    def markers():
        return [json.loads(line) for line in marker.read_text().splitlines()] if marker.exists() else []
    content=io.BytesIO()
    with zipfile.ZipFile(content,'w') as z:
        for f in (source/'dradar').rglob('*'):
            if f.is_file() and '__pycache__' not in f.parts:
                relative=f.relative_to(source).as_posix()
                if relative=='dradar/cli.py':
                    z.writestr(relative,fixture_cli)
                else:z.write(f,relative)
        z.writestr('__main__.py','import os\nfixture_self_test=os.environ.get("DRADAR_OTA_SELF_TEST")=="1"\nfrom dradar.launcher import main\nraise SystemExit(main())\n')
    assert zipfile.ZipFile(io.BytesIO(content.getvalue())).read('dradar/cli.py') == fixture_cli.encode()
    phases=[]
    original_prepare=discovery.UpdateRuntime.prepare
    def traced_prepare(runtime,*args,**kwargs):
        try:
            return original_prepare(runtime,*args,**kwargs)
        except Exception as exc:
            phases.append({'prepare_error':type(exc).__name__,'detail':str(exc)})
            raise
    monkeypatch.setattr(discovery.UpdateRuntime,'prepare',traced_prepare)
    def snapshot():
        return {name: json.loads((tmp_path/'ota'/name).read_text())
                for name in ('update-state.json','pending.json','current.json')
                if (tmp_path/'ota'/name).is_file()}
    def bundled_fallback():
        pytest.fail('unexpected parent bundled fallback: '+repr({'phases':phases,'state':snapshot()}))
    monkeypatch.setattr(cli,'main',bundled_fallback)
    original_windows_run=integration._run_windows_candidate
    def traced_windows_run(data, arguments, **kwargs):
        def capture(*args, **options):
            result=subprocess.run(*args, **options, capture_output=True, text=True)
            phases.append({'self_test':kwargs.get('self_test'), 'exit':result.returncode,
                           'stdout':result.stdout[-4000:], 'stderr':result.stderr[-4000:]})
            return result
        return original_windows_run(data,arguments,runner=capture,**kwargs)
    monkeypatch.setattr(integration,'_run_windows_candidate',traced_windows_run)
    body=content.getvalue();doc,keys=signed_release();doc.pop('signature')
    for item in doc['artifacts']:
        item['size']=len(body);item['sha256']=hashlib.sha256(body).hexdigest()
    doc=sign_document(doc);calls=[]
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200,json=doc) if str(request.url)==discovery.STABLE_URL else httpx.Response(200,content=body)
    monkeypatch.setattr(launcher,'HOME',tmp_path)
    monkeypatch.setenv('DRADAR_HOME',str(tmp_path))
    monkeypatch.setattr(discovery,'TRUSTED_KEYS',keys)
    monkeypatch.setattr(sys,'argv',['dradar','--version'])
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        def traced_discover(home):
            result=discovery.discover_update(home,client=client,trusted_keys=keys)
            phases.append({'discovery':result,'state':snapshot()})
            assert result in {'prepared','cached'}, repr(phases)
            return result
        monkeypatch.setattr(launcher,'discover_update',traced_discover)
        phases.append({'invocation':1})
        assert launcher.main()==0
        assert snapshot()['update-state.json']['state']=='committed', repr(phases)
        assert [m['self_test'] for m in markers()]==[True,False], repr(phases)
        phases.append({'invocation':2})
        assert launcher.main()==0
    assert len(calls)==2
    assert [m['self_test'] for m in markers()]==[True,False,False], repr(phases)


def test_launcher_unsafe_ota_root_preserves_bundled_cli(tmp_path,monkeypatch):
    from dradar import launcher,cli
    target=tmp_path/'target';target.mkdir();(tmp_path/'ota').symlink_to(target)
    monkeypatch.setattr(launcher,'HOME',tmp_path)
    monkeypatch.setattr(launcher,'discover_update',lambda *_:pytest.fail('unsafe root discovery'))
    monkeypatch.setattr(cli,'main',lambda:12)
    assert launcher.main()==12
    assert list(target.iterdir())==[]


def test_other_process_liveness_blocks_activation(tmp_path):
    import subprocess,sys,os
    root=tmp_path/'ota'
    program='from pathlib import Path\nfrom dradar.ota.activity import register_invocation\nimport sys\nwith register_invocation(Path(sys.argv[1])):\n print("ready",flush=True)\n sys.stdin.readline()\n'
    env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1]/'src')}
    child=subprocess.Popen([sys.executable,'-c',program,str(root)],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='ready'
        with UpdateLock(root/'launch.lock'):assert active_invocations(root)
        child.communicate('\n',timeout=5)
        with UpdateLock(root/'launch.lock'):assert not active_invocations(root)
    finally:
        if child.poll() is None:child.terminate();child.wait(timeout=5)


def test_real_http_trickle_is_interrupted_at_network_chunk_boundary():
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import threading
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Length','100');self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b'x');self.wfile.flush();time.sleep(.03)
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(trust_env=False) as client:
            start=time.monotonic();bounded=discovery.DeadlineClient(client,start+.15,time.monotonic)
            with bounded.stream('GET',f'http://127.0.0.1:{server.server_port}') as response:
                with pytest.raises((TimeoutError,httpx.HTTPError)):list(response.iter_bytes())
            assert time.monotonic()-start < .6
    finally:server.shutdown();server.server_close()


def test_real_http_trickled_headers_cannot_extend_budget():
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import threading
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            try:
                for char in b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx":
                    self.wfile.write(bytes([char]));self.wfile.flush();time.sleep(.03)
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(trust_env=False) as client:
            start=time.monotonic();bounded=discovery.DeadlineClient(client,start+.15,time.monotonic)
            with pytest.raises((TimeoutError,httpx.HTTPError)):
                with bounded.stream('GET',f'http://127.0.0.1:{server.server_port}') as response:list(response.iter_bytes())
            assert time.monotonic()-start < .6
    finally:server.shutdown();server.server_close()


@pytest.mark.parametrize('arguments', [
    ['go', '-y', '--refill', '--refill-harness', 'kimi-code'],
    ['resume', '-y', '--parallel'],
    ['fleet', 'serve', '--internal'],
])
def test_public_module_entry_uses_launcher_for_all_command_paths(tmp_path, arguments):
    """Exercise actual module entry/parser, with no model work or live network."""
    import os, subprocess, sys
    program = r'''
import builtins, runpy, sys, threading
from pathlib import Path
from dradar import launcher, cli
from dradar.ota import discovery
from dradar.ota.activity import active_invocations
home = launcher.HOME
seen = []
launcher.discover_update = lambda home: seen.append('discover')
stop = threading.Event()
launcher.start_periodic_discovery = lambda home: stop
launcher._activate_if_idle = lambda root: seen.append('activate')
def work(args):
    assert active_invocations(home / 'ota')
    assert discovery.LAUNCH_METHOD == 'launcher'
    seen.append(args.command)
    return 0
cli.cmd_go = cli.cmd_fleet_serve = work
builtins.input = lambda *a: (_ for _ in ()).throw(AssertionError('OTA asked permission'))
sys.argv = ['dradar.cli', *sys.argv[1:]]
try:
    runpy.run_module('dradar.cli', run_name='__main__')
except SystemExit as e:
    assert e.code == 0
assert seen == ['discover', 'activate', sys.argv[1], 'activate'], seen
assert stop.is_set()
assert not active_invocations(home / 'ota')
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith('DRADAR_OTA_')}
    env.update(DRADAR_HOME=str(tmp_path / 'home'),
               PYTHONPATH=str(Path(__file__).parents[1] / 'src'))
    result = subprocess.run([sys.executable, '-c', program, *arguments], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('self_test', [False, True])
def test_source_child_is_registered_without_recursive_ota(tmp_path, monkeypatch, self_test):
    from dradar import launcher, cli, child_entrypoint
    import os, sys
    monkeypatch.setattr(launcher, 'HOME', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['dradar.cli', '--version'])
    env = {}
    child_entrypoint.popen_options(env)
    assert env['DRADAR_OTA_SOURCE_CHILD'] == '1'
    monkeypatch.setenv('DRADAR_OTA_SOURCE_CHILD', env['DRADAR_OTA_SOURCE_CHILD'])
    if self_test:
        monkeypatch.setenv('DRADAR_OTA_SELF_TEST', '1')
    monkeypatch.setattr(launcher, 'discover_update', lambda *_: pytest.fail('recursive discovery'))
    monkeypatch.setattr(launcher, 'start_periodic_discovery', lambda *_: pytest.fail('duplicate timer'))
    monkeypatch.setattr(launcher, '_activate_if_idle', lambda *_: pytest.fail('child activation'))
    def work():
        assert active_invocations(tmp_path / 'ota')
        assert discovery.LAUNCH_METHOD == 'launcher'
        assert 'DRADAR_OTA_SOURCE_CHILD' not in os.environ
        # Grandchildren must acquire their own activity lease too.
        grandchild_env = dict(os.environ)
        child_entrypoint.popen_options(grandchild_env)
        assert grandchild_env['DRADAR_OTA_SOURCE_CHILD'] == '1'
        assert 'DRADAR_OTA_DISPATCH' not in grandchild_env
        return 23
    monkeypatch.setattr(cli, 'main', work)
    assert launcher.main() == 23
    assert not active_invocations(tmp_path / 'ota')


def test_source_child_keeps_activity_after_supervisor_releases(tmp_path):
    import os, subprocess, sys
    from dradar import child_entrypoint
    program = r'''
from dradar import cli, launcher
from dradar.ota.activity import active_invocations
def work():
    assert active_invocations(launcher.HOME / 'ota')
    print('ready', flush=True)
    input()
    return 0
cli.main = work
launcher.discover_update = lambda *_: (_ for _ in ()).throw(AssertionError('recursive discovery'))
raise SystemExit(launcher.main())
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith('DRADAR_OTA_')}
    env.update(DRADAR_HOME=str(tmp_path),
               PYTHONPATH=str(Path(__file__).parents[1] / 'src'))
    child_entrypoint.popen_options(env)
    with register_invocation(tmp_path / 'ota'):
        child = subprocess.Popen([sys.executable, '-c', program], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        assert child.stdout.readline().strip() == 'ready'
    try:
        assert active_invocations(tmp_path / 'ota')
    finally:
        out, err = child.communicate('\n', timeout=10)
    assert child.returncode == 0, out + err
    assert not active_invocations(tmp_path / 'ota')
