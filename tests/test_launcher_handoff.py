import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from dradar import boundary_recovery as recovery, launcher_handoff as handoff


@pytest.mark.parametrize('problem', [None, 'other', 'unmarked', 'changed', 'missing', 'duplicate', 'malformed', 'worker'])
def test_only_verified_direct_launcher_is_excluded(tmp_path, monkeypatch, problem):
    monkeypatch.setattr(recovery.fleet, 'controller_is_active', lambda _: False)
    monkeypatch.setattr(os, 'getpid', lambda: 200)
    monkeypatch.setattr(os, 'getppid', lambda: 100)
    argv = ['python', '/dev/fd/5', 'go', '--pick', 'task:model:low']
    monkeypatch.setattr(handoff, '_supervisor', None if problem == 'unmarked' else (100, handoff.argv_digest(argv)))
    rows = ['200 100 python /dev/fd/3 go --pick task:model:low', '100 1 ' + ' '.join(argv)]
    if problem == 'other':
        rows.append('300 1 ' + ' '.join(argv))
    elif problem == 'changed':
        rows[1] += ' --other'
    elif problem == 'missing':
        rows.pop()
    elif problem == 'duplicate':
        rows.append(rows[1])
    elif problem == 'malformed':
        rows.append('unknown process')
    elif problem == 'worker':
        rows.append('300 200 python /dev/fd/3 resume --worker-child')
    calls = []
    def run(args, **kw):
        calls.append(args)
        return SimpleNamespace(stdout='\n'.join(rows) if args[0] == 'ps' else '')
    monkeypatch.setattr(recovery.subprocess, 'run', run)
    if problem:
        with pytest.raises(recovery.RecoveryBlocked):
            recovery._check_processes(tmp_path)
    else:
        recovery._check_processes(tmp_path)
        assert calls[-1] == ['docker', 'ps', '-q']


@pytest.mark.parametrize('problem', [None, 'wrong-pid', 'wrong-args', 'worker', 'oversize', 'live-writer', 'not-verified'])
def test_one_use_pipe_validation(monkeypatch, problem):
    read, write = os.pipe()
    row = {'pid': os.getppid(), 'argv_sha256': handoff.argv_digest(['python', '/dev/fd/5', 'go']), 'args_sha256': handoff.argv_digest(sys.argv[1:]), 'worker': False}
    if problem == 'wrong-pid':
        row['pid'] += 1
    elif problem == 'wrong-args':
        row['args_sha256'] = handoff.argv_digest(['not-the-same'])
    elif problem == 'worker':
        row['worker'] = True
    body = json.dumps(row).encode() if problem != 'oversize' else b'x' * 4097
    os.write(write, body)
    if problem != 'live-writer':
        os.close(write)
    monkeypatch.setenv(handoff._ENV, str(read))
    handoff.consume(verified_child=problem != 'not-verified')
    assert (handoff.supervisor() is not None) == (problem is None)
    assert handoff._ENV not in os.environ
    if problem == 'live-writer':
        os.close(write)
    if problem == 'not-verified':
        os.close(read)
    else:
        with pytest.raises(OSError):
            os.fstat(read)


def test_real_child_consumes_parent_pipe(monkeypatch):
    args = ['go', '--pick', 'task:model:low']
    monkeypatch.setattr(sys, 'argv', ['dradar', *args])
    with handoff.handoff() as (fd, env):
        child = subprocess.run([sys.executable, '-c', '''
import json, os
from dradar import launcher_handoff as h
h.consume(verified_child=True)
print(json.dumps({'pid': os.getppid(), 'supervisor': h.supervisor(), 'marker': h._ENV in os.environ}))
''', *args], pass_fds=(fd,), env=env, capture_output=True, text=True, check=True)
    result = json.loads(child.stdout)
    assert result['supervisor'][0] == result['pid'] == os.getpid()
    assert result['supervisor'][1] == handoff.argv_digest(sys.orig_argv)
    assert not result['marker']
