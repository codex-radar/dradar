"""External-runtime replacements for the real pyz go/worker process test.

Selection, API, supervisor, locks, telemetry, checkout and boundary stay real.
Only Docker/Pier/model execution and OTA network discovery are replaced.
"""
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.environ['PROBE_ARTIFACT'])
from dradar import runloop as r, launcher
from dradar.api_client import ApiClient
from dradar.ota import discovery

home = Path(os.environ['DRADAR_HOME'])
server = os.environ['PROBE_SERVER']
r._load_config = lambda: {'server': server, 'token': 'fixture', 'tasks_root': str(home / 'tasks')}
r._client = lambda cfg, **kw: ApiClient(server, 'fixture', capabilities=())
r._preflight_scoped_provider = lambda *a: None
r._maintain_image_cache = lambda *a, **kw: True
r.sweep_orphan_compose = lambda *a: None
r.ensure_tasks_root = lambda *a: None
r.ensure_pier = lambda: None
r._ensure_egress_runtime = lambda **kw: None
r._version_pinned_tasks_root = lambda commit, root, drift: (root, commit)
r._allow_claim_after_empty_submission = lambda *a, **kw: True
r._allow_explicit_empty_submission_retry = lambda *a, **kw: True
r._empty_submission_blocked_ids = lambda *a, **kw: set()
r.image_cache.preflight_trial_builder = lambda *a: r.image_cache.TrialBuilderPreflight(True, 0, 'fixture', None, '', ())
r._try_ota_idle_activation = lambda **kw: None
launcher.discover_update = discovery.discover_update = lambda *a, **kw: None
launcher.start_periodic_discovery = lambda *a, **kw: SimpleNamespace(set=lambda: None)

def model(client, assignment, *a, **kw):
    # Keep both harnesses in flight long enough to prove overlap.
    time.sleep(0.4)
    client._post('/fixture/complete', data={'assignment_id': assignment['assignment_id']})
    return 'submitted'
r._run_and_submit = model

if os.environ.get('PROBE_SPAWN_FAIL'):
    original_popen = r.subprocess.Popen
    def fail_spawn(command, *args, **kwargs):
        if '--worker-child' in command:
            raise OSError('injected worker spawn failure')
        return original_popen(command, *args, **kwargs)
    r.subprocess.Popen = fail_spawn

# Deterministic race injection: the selected child cannot make its first
# inventory read until its sibling has drained the exact batch on the server.
# This does not change the response or extend simulated model duration.
if os.environ.get('PROBE_LATE_CHILD_INDEX') == os.environ.get('DRADAR_WORKER_INDEX') and os.environ.get('DRADAR_WORKER_INDEX'):
    class LateClient(ApiClient):
        waited = False
        def get_assignment(self):
            if not self.waited:
                deadline = time.monotonic() + 15
                while not self._get('/fixture/drained?batch_id=' + str(self.batch_id)).get('drained'):
                    if time.monotonic() >= deadline:
                        raise RuntimeError('fixture late-child barrier timed out')
                    time.sleep(0.02)
                self.waited = True
            return super().get_assignment()
    r._client = lambda cfg, **kw: LateClient(server, 'fixture', capabilities=())
