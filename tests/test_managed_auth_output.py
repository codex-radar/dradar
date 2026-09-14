"""Real redirected encoders must not turn local auth status into a crash."""
import os,subprocess,sys
from pathlib import Path
import pytest

PROBE = '''
import socket, subprocess
from pathlib import Path
from dradar import cli
from dradar import managed_auth_selection as m
from dradar import auth_managed, managed_auth_install
from argparse import Namespace
def denied(*a, **k): raise AssertionError('network/auth/process forbidden')
socket.socket.connect=denied
socket.create_connection=denied
subprocess.run=denied
subprocess.Popen=denied
managed_auth_install.urllib.request.urlopen=denied
auth_managed._login=denied
for action in ('status','recover','revoke','use-native'):
    assert cli.main(['provider','codex-managed',action]) is None
assert m.readiness()==('native',False)
# Fixed failure diagnostic is the same public handler used for a rejected login.
from dradar.auth_refresh import RefreshUnavailable
def reject_acquire(*a): raise RefreshUnavailable('managed_runtime_unverified')
managed_auth_install.acquire=reject_acquire
assert cli.main(['provider','codex-managed','login'])==1
assert not m.selection_path().exists()
'''

@pytest.mark.parametrize('encoding', ['cp1252', 'utf-8'])
def test_public_inactive_commands_on_real_redirected_stream(tmp_path, encoding):
    root=Path(__file__).resolve().parents[1]
    env=dict(os.environ,PYTHONPATH=str(root/'src'),DRADAR_HOME=str(tmp_path/'home'),PYTHONIOENCODING=encoding,PYTHONDONTWRITEBYTECODE='1')
    env.pop('DRADAR_CODEX_MANAGED_CONFIG',None)
    result=subprocess.run([sys.executable,'-c',PROBE],env=env,capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr.decode('ascii','backslashreplace')
    output=result.stdout.decode(encoding)
    assert 'managed_runtime_unverified' in output
    assert not (tmp_path/'home/managed-auth/store').exists()
    if encoding=='utf-8':assert '普通官方凭据兼容模式' in output
    else:assert '\\u' in output
