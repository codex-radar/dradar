"""Payload failures must never silently fall back to the installed CLI."""
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from dradar import child_entrypoint


@pytest.mark.parametrize('failure', ['missing', 'damaged', 'closed-fd', 'damaged-fd'])
def test_bad_payload_in_real_process_fails_closed(tmp_path, failure):
    if os.name == 'nt' and failure.endswith('fd'):
        pytest.skip('POSIX descriptor entry')
    path = tmp_path / 'candidate.pyz'
    with zipfile.ZipFile(path, 'w') as bundle:
        bundle.writestr('__main__.py', 'print("unexpected")')
    script = '''
import os, sys
from dradar.child_entrypoint import command, popen_options
path, failure = sys.argv[1:]
if failure.endswith('fd'):
    fd = os.open(path, os.O_RDONLY)
    sys.argv[0] = '/dev/fd/' + str(fd)
    if failure == 'closed-fd': os.close(fd)
    else:
        with open(path, 'wb') as handle: handle.write(b'bad')
else:
    sys.argv[0] = path
    if failure == 'missing': os.unlink(path)
    else:
        with open(path, 'wb') as handle: handle.write(b'bad')
try:
    command()
except OSError:
    print('failed-closed')
else:
    raise SystemExit('unsafe fallback')
'''
    result = subprocess.run([sys.executable, '-c', script, str(path), failure],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'failed-closed'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX descriptors')
def test_payload_and_machine_descriptors_are_both_inherited(tmp_path, monkeypatch):
    payload = tmp_path / 'payload.pyz'
    with zipfile.ZipFile(payload, 'w') as bundle:
        bundle.writestr('__main__.py', 'print("payload")')
    lock = tmp_path / 'run.lock'
    lock.write_text('lock')
    with payload.open('rb') as archive, lock.open('rb') as machine:
        monkeypatch.setattr(sys, 'argv', [f'/dev/fd/{archive.fileno()}'])
        env = dict(os.environ)
        options = child_entrypoint.popen_options(env, extra_fds=(machine.fileno(),))
        result = subprocess.run([sys.executable, '-c',
                                 'import os,sys; [os.fstat(int(x)) for x in sys.argv[1:]]; print(os.environ["DRADAR_OTA_DISPATCH"])',
                                 str(archive.fileno()), str(machine.fileno())],
                                env=env, **options, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == '1'
