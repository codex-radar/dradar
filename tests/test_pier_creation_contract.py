"""Real upstream mkdir and mock upload, under an outer process umask 002."""
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != 'posix', reason='POSIX creation contract')
def test_real_pier_private_creation_does_not_change_parent_umask(tmp_path):
    pytest.importorskip('pier.models.trial.paths')
    child = r'''
import json, socket, sys
from pathlib import Path
from pier.models.trial.paths import TrialPaths
from dradar import runloop
from dradar.artifact_boundary import TrialFiles
root = Path(sys.argv[1])
trial = root / 'trial'
TrialPaths(trial_dir=trial).mkdir()
(trial / 'artifacts/model.patch').write_text('diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n')
(trial / 'agent/sessions').mkdir()
(trial / 'agent/sessions/fixture.jsonl').write_text('{"type":"session_meta","payload":{"id":"fixture"}}\n')
runloop.HOME = root / 'home'
def denied(*args, **kwargs):
    raise AssertionError('network forbidden')
socket.socket.connect = denied
class Client:
    def submit(self, *args, **kwargs):
        return {'submission_id':'fixture', 'grade_status':'pending'}
with TrialFiles(trial) as files:
    assert files.read('artifacts/model.patch')
status = runloop._upload_trial(Client(), {'assignment_id':'fixture', 'nonce':'fixture', 'trial_dir':str(trial), 'task_id':'fixture', 'keep':True})
assert status == 'submitted'
print(json.dumps({'trial_mode': trial.stat().st_mode & 0o777, 'status':status}))
'''
    outer = r'''
import json, subprocess, sys
from pathlib import Path
from dradar.runner import _pier_process_options
root = Path(sys.argv[1])
before = root / 'outer-before'
before.mkdir()
result = subprocess.run([sys.executable, '-c', sys.argv[2], str(root)], check=True, capture_output=True, text=True, **_pier_process_options())
after = root / 'outer-after'
after.mkdir()
print(json.dumps({'before':before.stat().st_mode & 0o777, 'after':after.stat().st_mode & 0o777, 'child':json.loads(result.stdout.splitlines()[-1])}))
'''
    result = subprocess.run(
        [sys.executable, '-c', outer, str(tmp_path), child],
        check=True, capture_output=True, text=True, umask=0o002,
    )
    data = json.loads(result.stdout)
    assert data == {'before':0o775, 'after':0o775, 'child':{'trial_mode':0o700, 'status':'submitted'}}
