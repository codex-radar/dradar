"""Local-only installed-wheel QA. Uses a loopback server and an inert UV shim."""
import argparse
import functools
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
REVISION = 'a' * 40
CODE = 'inert_plan_code_123456789'


def main(output, web_source=None):
    output.mkdir(parents=True, exist_ok=False)
    spec = importlib.util.spec_from_file_location('local_bootstrap_builder', ROOT / 'scripts/build_bootstrap.py')
    builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
    manifest = builder.build(ROOT, output / 'wheel')
    reports, user_agents = [], []
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            user_agents.append(self.headers.get('User-Agent'))
            return super().do_GET()
        def do_POST(self):
            assert self.path == '/api/v1/runner/failures'
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            reports.append({'body': body, 'scope_header_matches': self.headers.get('X-DRadar-Run-Code') == CODE})
            self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(output / 'wheel')))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith('_proxy')}
    env.update(NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost', UV_PYTHON_DOWNLOADS='never',
               UV_CACHE_DIR=str(output / 'install-cache'), PYTHONDONTWRITEBYTECODE='1')
    uv = shutil.which('uv')
    assert uv
    recipe = None
    start_template = None
    if web_source is not None:
        node = r"""
const fs = require('node:fs'), vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const source = html.slice(html.indexOf('function validRunPlanInvitation'), html.indexOf('function runPlanChoiceSummary'));
const context = {API: process.argv[2], window: {DRadarI18n: {isEnglish: () => true}}};
vm.runInNewContext(source, context);
const declaredRevision = context.DRADAR_STABLE_COMMIT;
context.DRADAR_STABLE_COMMIT = process.argv[3];
const invitation = {schema_version: 1, task_count: 5, run_code: process.argv[4],
 exchange_expires_at: '2099-01-01T00:00:00Z', plan_expires_at: '2099-01-02T00:00:00Z'};
process.stdout.write(JSON.stringify({recipe: context.bootstrapInstallRecipe(),
 start: context.sessionEntryArgv(invitation, true), prompt: context.runPlanAgentPrompt(invitation),
 declaredRevision, version: context.DRADAR_STABLE_VERSION}));
"""
        page = json.loads(subprocess.check_output(['node', '-e', node, str(web_source), base, REVISION, CODE], text=True))
        recipe, start_template = page['recipe'], page['start']
        assert recipe['requirement'].endswith('--hash=sha256:' + manifest['sha256'])
        assert '--require-hashes' in recipe['install'] and '--reinstall-package' in recipe['install']
        assert '-I' in start_template
        (output / 'copied-instruction.txt').write_text(page['prompt'])
    result = {'uv': subprocess.check_output([uv, '--version'], text=True).strip(), 'bootstrap': manifest, 'cases': []}
    try:
        for label, digest in [('bad-hash', '0' * 64), ('valid-hash', manifest['sha256'])]:
            venv = output / label
            create = [uv, 'venv', str(venv), '--python', sys.executable]
            if recipe:
                create = [str(venv) if item == '<BOOTSTRAP_ENV>' else item for item in recipe['create']]
                create[0] = uv
                create[create.index('--python') + 1] = sys.executable  # no runtime downloads in fixtures
            subprocess.run(create, env=env, capture_output=True, check=True)
            python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            lock = venv / 'bootstrap.lock'
            requirement = f'dradar-bootstrap @ {base}/{manifest["filename"]} --hash=sha256:{digest}'
            if recipe:
                parts = recipe['requirement'].split()
                assert len(parts) == 4 and parts[:2] == ['dradar-bootstrap', '@']
                assert parts[2].endswith('/' + manifest['filename'])
                parts[2] = base + '/' + manifest['filename']
                parts[3] = '--hash=sha256:' + digest
                requirement = ' '.join(parts)
            lock.write_text(requirement + '\n')
            install_command = [uv, 'pip', 'install', '--no-config', '--no-index', '--only-binary', ':all:',
                '--require-hashes', '--reinstall-package', 'dradar-bootstrap', '--python', str(python), '-r', str(lock)]
            if recipe:
                replace = {'<BOOTSTRAP_PYTHON>': str(python), '<BOOTSTRAP_LOCK>': str(lock)}
                install_command = [replace.get(item, item) for item in recipe['install']]
                install_command[0] = uv
            install = subprocess.run(install_command, env=env, capture_output=True, text=True, timeout=30)
            (output / (label + '-install.log')).write_text(install.stdout + install.stderr)
            assert (install.returncode == 0) == (label == 'valid-hash')
            result['cases'].append({'case': label, 'install_returncode': install.returncode})
        installed_python = output / 'valid-hash' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        # Reusing a same-version package must still enforce the requested hash.
        repeat_bad = [uv, 'pip', 'install', '--no-config', '--no-index', '--require-hashes',
                      '--reinstall-package', 'dradar-bootstrap', '--python', str(installed_python), '-r', str(output / 'bad-hash/bootstrap.lock')]
        failed_reinstall = subprocess.run(repeat_bad, env=env, capture_output=True, text=True, timeout=30)
        assert failed_reinstall.returncode != 0
        result['cached_bad_hash_rejected'] = True
        # The same fixed module entry proposed for the copied instructions.
        shadow = output / 'inert-cwd'; shadow.mkdir(); (shadow / 'dradar_bootstrap.py').write_text('# inert file; must not shadow the installed package\n')
        version = subprocess.run([uv, 'run', '--no-project', '--no-sync', '--no-config', '--no-env-file', '--python', str(installed_python),
                                  'python', '-I', '-m', 'dradar_bootstrap', '--version'], env=dict(env, PYTHONPATH=str(shadow)), cwd=shadow, capture_output=True, text=True, timeout=15, check=True)
        assert version.stdout.strip() == '0.1.0'
        result['isolated_module_entry_verified'] = True
        fixture_code = ROOT / 'tests/fixtures/session_runtime_fixture.py'
        for mode in ('retry-success', 'fail-twice', 'confirm', 'cancel', 'stream'):
            case = output / mode; case.mkdir(); (case / 'fixture-only').touch(); (case / 'bin').mkdir()
            shim = case / 'bin/uvx'
            shim.write_text('#!' + sys.executable + '\n' + '''import json, os, subprocess, sys
from pathlib import Path
root=Path(os.environ['DRADAR_FIXTURE_ROOT'])
counter=root/'attempts.json'
records=json.loads(counter.read_text()) if counter.exists() else []
a=sys.argv[1:]
assert a[:7]==['--no-config','--no-env-file','--python','3.13','--from','git+https://github.com/SecurityMind/dradar@'+('a'*40),'dradar-session']
records.append({'cache':os.environ.get('UV_CACHE_DIR'),'receipt':os.environ.get('DRADAR_BOOTSTRAP_RECEIPT')})
counter.write_text(json.dumps(records))
mode=os.environ['DRADAR_FIXTURE_MODE']
if mode=='fail-twice' or (mode=='retry-success' and len(records)==1):
    print('Git operation failed (inert fixture)',file=sys.stderr)
    raise SystemExit(1)
env=dict(os.environ,DRADAR_FIXTURE_ATTEMPT=str(len(records)))
raise SystemExit(subprocess.run([sys.executable,os.environ['DRADAR_FIXTURE_RUNTIME'],*a[7:]],env=env).returncode)
''')
            shim.chmod(0o700)
            case_env = dict(env, PATH=str(case / 'bin') + os.pathsep + env.get('PATH', ''),
                            DRADAR_HOME=str(case / 'home'), DRADAR_FIXTURE_ROOT=str(case), UV_OFFLINE='1',
                            DRADAR_FIXTURE_MODE='confirm' if mode == 'cancel' else mode,
                            DRADAR_FIXTURE_RUNTIME=str(fixture_code))
            command = [uv, 'run', '--no-project', '--no-sync', '--no-config', '--no-env-file', '--python', str(installed_python), 'python', '-I', '-m', 'dradar_bootstrap',
                       '--revision=' + REVISION, '--server=' + base, '--plan=' + CODE, '--locale=en-US']
            if start_template:
                command = [str(installed_python) if item == '<BOOTSTRAP_PYTHON>' else item for item in start_template]
                command[0] = uv
            before = len(reports)
            if mode == 'stream':
                process = subprocess.Popen(command, env=case_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
                lines = []
                while True:
                    line = process.stdout.readline()
                    assert line, 'expected streamed marker'
                    lines.append(line)
                    if 'DRADAR_STREAM_READY' in line: break
                assert process.poll() is None, 'short output must arrive before process completion'
                rest, err = process.communicate(timeout=10)
                completed = subprocess.CompletedProcess(command, process.returncode, ''.join(lines) + rest, err)
            else:
                completed = subprocess.run(command, env=case_env, input='', capture_output=True, text=True, timeout=20)
            (case / 'stdout.log').write_text(completed.stdout); (case / 'stderr.log').write_text(completed.stderr)
            assert CODE not in completed.stdout + completed.stderr
            attempts = json.loads((case / 'attempts.json').read_text())
            expected = 1 if mode == 'fail-twice' else 2 if mode in ('confirm', 'cancel') else 0
            assert completed.returncode == expected, (mode, completed.stdout, completed.stderr)
            if mode == 'fail-twice':
                assert len(attempts) == 2 and len(reports) == before + 1
                assert reports[-1]['scope_header_matches'] and reports[-1]['body']['failure_code'] == 'uvx-source-resolution'
            else:
                assert len(reports) == before
            if mode in ('retry-success', 'fail-twice'):
                assert attempts[1]['cache'] != attempts[0]['cache'] and not Path(attempts[1]['cache']).exists()
            if mode in ('confirm', 'cancel'):
                event = json.loads(next(line[len('DRADAR_CONFIRMATION '):] for line in completed.stdout.splitlines() if line.startswith('DRADAR_CONFIRMATION ')))
                selected = 'cancel' if mode == 'cancel' else 'install'
                resumed = subprocess.run(command + ['--confirmation=' + event['request'], '--choice=' + selected],
                                         env=case_env, input='', capture_output=True, text=True, timeout=20)
                assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
                assert len(reports) == before
                trace = json.loads((case / 'runtime-2.json').read_text())
                assert trace['starts'] == ([] if selected == 'cancel' else [5])
                repeated = subprocess.run(command + ['--confirmation=' + event['request'], '--choice=' + selected],
                                          env=case_env, input='', capture_output=True, text=True, timeout=20)
                assert repeated.returncode == 1 and len(reports) == before
            if mode == 'retry-success':
                trace = json.loads((case / 'runtime-2.json').read_text())
                assert trace['starts'] == [5] and trace['progress_reads'] == 1 and trace['receipt_written']
            assert all(not Path(a['receipt']).parent.exists() for a in json.loads((case / 'attempts.json').read_text()))
            result['cases'].append({'case': mode, 'initial_returncode': completed.returncode,
                                    'initial_attempts': len(attempts), 'reports': len(reports) - before, 'passed': True})
        if web_source is not None:
            result['web_source_sha256'] = hashlib.sha256(web_source.read_bytes()).hexdigest()
            result['web_declared_revision'] = page['declaredRevision']
            result['web_recipe_executed'] = True
        result['http_user_agents'] = sorted(set(user_agents))
        result['report_bodies'] = [item['body'] for item in reports]
        result['native_platform'] = sys.platform
        (output / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    finally:
        server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--web-source', type=Path)
    args = parser.parse_args()
    main(args.output_dir.resolve(), args.web_source.resolve() if args.web_source else None)
