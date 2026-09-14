"""Native unsupported-host and materialized-dependency contract; no accounts."""
from pathlib import Path
import os,sys,tempfile,socket,subprocess,platform,importlib,json,hashlib,contextlib,io
root=Path.cwd();tmp=Path(tempfile.mkdtemp(prefix='0092-native-contract-'))
os.environ['DRADAR_HOME']=str(tmp/'home');os.environ.pop('DRADAR_CODEX_MANAGED_CONFIG',None)
sys.path.insert(0,str(root/'src'))
assert platform.system() in ('Linux','Windows'), 'this bounded probe targets unsupported hosts only'
calls=[]
def denied(*a,**k):calls.append(True);raise AssertionError('external/auth/process call forbidden')
socket.socket.connect=denied;socket.create_connection=denied
from dradar import cli,managed_auth_selection as selection,auth_managed,managed_auth_install,runner
assert str(Path(cli.__file__).resolve()).startswith(str((root/'src').resolve()))
subprocess.run=denied;subprocess.Popen=denied
managed_auth_install.urllib.request.urlopen=denied;auth_managed._login=denied
from argparse import Namespace
for action in ('status','recover','revoke','use-native'):
 result=selection.cmd_managed_auth(Namespace(managed_auth_command=action,codex_bin=None))
 assert result is None
 assert selection.load_selection() is None
assert selection.readiness()==('native',False)
assert selection.cmd_managed_auth(Namespace(managed_auth_command='login',codex_bin=None))==1
assert not selection.selection_path().exists()
assert not (tmp/'home/managed-auth/store').exists()
out=io.StringIO()
with contextlib.redirect_stdout(out):
 try:cli.main(['--version'])
 except SystemExit as e:assert e.code==0
assert out.getvalue().strip()=='0.5.202'
bundle=tmp/'bundle';bundle.mkdir()
package,bridge=runner._ensure_codex_managed_module(bundle)
resources=importlib.resources.files('dradar')
allowed={p.read_bytes() for p in resources.iterdir() if p.is_file() and p.name.endswith(('.py','.cjs'))}|{b''}
for p in bundle.rglob('*'):
 if p.is_file():assert p.read_bytes() in allowed
sys.path.insert(0,str(bundle))
module=importlib.import_module(package+'.pier_codex_managed')
agent=module.CodexManaged(logs_dir=tmp/'logs',model_name='openai/fixture-only',version='0.154.0',managed_config_file=str(tmp/'unused'),managed_bridge_file=str(bridge),managed_package=package)
assert not calls
print('NATIVE_CONTRACT_RESULT '+json.dumps({'os':platform.system(),'arch':platform.machine(),'unsupported_managed_rejected':True,'default_native_preserved':True,'inactive_commands_no_auth':True,'materialized_dependencies_imported':True,'version':'0.5.202','network_or_auth_calls':len(calls)}))
