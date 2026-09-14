"""Subprocess fixture: replace external work only, never process launch helpers.

Installed via sitecustomize. Imports still resolve naturally from argv[0] and
PYTHONPATH, so a fallback to installed203 is visible in every process report.
"""
import atexit
import hashlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(os.environ['FLEET_PROBE_ROOT'])
# Registered before launcher handle finalizers; this marker runs after them.
atexit.register(lambda: (ROOT / f'{os.getpid()}.exited').touch())
BATCH = '550e8400e29b41d4a716446655440000'


def record(role):
    import dradar
    from dradar import fleet, runloop
    from dradar.ota.activity import active_invocations
    result = {'role': role, 'pid': os.getpid(), 'version': dradar.__version__,
              'argv0': sys.argv[0], 'pythonpath': os.environ.get('PYTHONPATH'),
              'activity': active_invocations(fleet.HOME / 'ota')}
    for module in (fleet, runloop):
        source = module.__loader__.get_source(module.__name__)
        normalized = source.replace('\r\n', '\n').replace('\r', '\n')
        result[module.__name__] = {
            'origin': module.__file__,
            'sha256': hashlib.sha256(normalized.encode()).hexdigest(),
            'raw_source_sha256': hashlib.sha256(source.encode()).hexdigest(),
            'normalization': 'universal-newlines',
        }
    (ROOT / (role + '.json')).write_text(json.dumps(result, indent=2))


def wait_for(name):
    deadline = time.monotonic() + 25
    while not (ROOT / name).exists():
        if time.monotonic() > deadline:
            raise RuntimeError('fixture gate timed out: ' + name)
        time.sleep(.03)


def install(cli):
    original = cli.main
    def main(*args, **kwargs):
        from dradar import fleet, runloop
        argv = sys.argv[1:]
        if argv == ['--version']:
            return original(*args, **kwargs)
        if argv == ['probe-parent']:
            record('parent')
            fleet._ensure_controller()
            return 0
        if argv[:2] == ['fleet', 'serve']:
            def controller_loop(home, state):
                record('coordinator')
                # Test harness waits for BOTH outer launcher and parent to exit.
                wait_for('parent-exited')
                record('coordinator-after-parent')
                wait_for('continue-pool')
                process, log = fleet._spawn_pool(home, state, BATCH, 1)
                try:
                    rc = process.wait(timeout=25)
                    (ROOT / 'pool-exit').write_text(str(rc))
                finally:
                    if process.poll() is None:
                        process.kill()
                    log.close()
                return rc
            fleet._controller_loop = controller_loop
            return original(*args, **kwargs)
        if '--worker-child' in argv:
            record('worker')
            # No assignment/model executed: this is a local readiness fixture.
            Path(os.environ[runloop._POOL_WORKER_ACTIVITY_ENV]).write_text('0' * 32)
            return 0
        if '--fleet-pool' in argv:
            record('pool')
            noop = lambda *a, **k: None
            runloop._run_config = lambda *a, **k: {'benchmark': runloop.DEFAULT_BENCHMARK}
            runloop._client = lambda *a, **k: SimpleNamespace(capabilities=(), get_assignment=lambda: {'active': []})
            runloop._scope_client_to_batch = noop
            runloop._selected_tasks_root = lambda *a: ROOT / 'tasks'
            for name in ('_ensure_selected_tasks_root', 'ensure_pier', '_ensure_egress_runtime',
                         '_mark_pending_scope_required', '_retry_pending_uploads'):
                setattr(runloop, name, noop)
            runloop.FlightRecorder = lambda *a: SimpleNamespace(try_record=noop, flush=noop)
            runloop._prepare_batch = lambda *a: ([{'assignment_id': 'fixture-only'}], False)
            runloop._prepare_assignment_boundary = lambda *a: None
            runloop._finish_assignment_boundary = lambda *a, **k: True
            runloop._pool_backfill_v2_enabled = lambda: False
            runloop._try_ota_idle_activation = noop
            values = dict(workers=1, yes=True, keep=False, allow_task_drift=False,
                          dev_agent=None, refill=False, refill_to=None, max_tasks=None,
                          max_estimated_quota_pct=None, quota_tier='plus', auto=None,
                          pick=None, assignment=None, parallel=False, worker_child=False,
                          resume=True, worker_target_file=None, archive_session=False,
                          batch_id=BATCH, fleet_pool=True)
            return runloop._run_worker_pool(SimpleNamespace(**values))
        raise RuntimeError('unexpected fixture command ' + repr(argv))
    cli.main = main


class Loader(importlib.abc.Loader):
    def __init__(self, delegate): self.delegate = delegate
    def create_module(self, spec): return self.delegate.create_module(spec)
    def exec_module(self, module):
        self.delegate.exec_module(module)
        if module.__name__ == 'dradar.cli':
            install(module)
        elif module.__name__ == 'dradar.runloop':
            original_go = module.cmd_go
            def go(args):
                import dradar.cli as cli
                install(cli)
                return cli.main()
            module.cmd_go = go
        elif module.__name__ == 'dradar.fleet':
            original_loop = module._controller_loop
            def loop(home, state):
                record('coordinator')
                wait_for('parent-exited')
                record('coordinator-after-parent')
                wait_for('continue-pool')
                process, log = module._spawn_pool(home, state, BATCH, 1)
                try:
                    rc = process.wait(timeout=25)
                    (ROOT / 'pool-exit').write_text(str(rc))
                finally:
                    if process.poll() is None:
                        process.kill()
                    log.close()
                return rc
            module._controller_loop = loop
    def __getattr__(self, name): return getattr(self.delegate, name)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in ('dradar.cli', 'dradar.runloop', 'dradar.fleet'):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            spec.loader = Loader(spec.loader)
            return spec
        return None


sys.meta_path.insert(0, Finder())
