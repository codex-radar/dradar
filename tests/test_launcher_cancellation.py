"""The real launcher wait must not kill its worker during finalization."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(os.name == 'nt', reason='POSIX launcher')
@pytest.mark.parametrize('whole_group', [False, True])
def test_launcher_waits_for_worker_durable_cleanup_after_repeat_sigint(tmp_path, whole_group):
    worker = tmp_path/'worker.py'
    worker.write_text('''import signal,sys,time
from pathlib import Path
from dradar import cancellation
root=Path(sys.argv[1])
with cancellation.scope():
 try:
  (root/'ready').write_text('ready')
  while True: time.sleep(.01)
 except KeyboardInterrupt:
  cancellation.protect_finalization()
  time.sleep(.4)
  (root/'closed').write_text('durable')
''')
    code = '''import os,sys
from dradar.launcher import _run_posix_candidate
raise SystemExit(_run_posix_candidate([sys.executable,sys.argv[1],sys.argv[2]],pass_fds=(),env=os.environ.copy()))
'''
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]/'src')}
    parent = subprocess.Popen([sys.executable,'-c',code,str(worker),str(tmp_path)],env=env,
                              start_new_session=True)
    try:
        deadline=time.monotonic()+10
        while not (tmp_path/'ready').exists() and time.monotonic()<deadline:
            time.sleep(.01)
        assert (tmp_path/'ready').exists()
        (os.killpg if whole_group else os.kill)(parent.pid,signal.SIGINT)
        time.sleep(.1)
        (os.killpg if whole_group else os.kill)(parent.pid,signal.SIGINT)
        assert parent.wait(timeout=10)==130
        assert (tmp_path/'closed').read_text()=='durable'
    finally:
        if parent.poll() is None:
            os.killpg(parent.pid,signal.SIGKILL)
            parent.wait()
