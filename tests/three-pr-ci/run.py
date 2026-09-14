"""Bounded native baseline/candidate checks; no production interfaces."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT = Path.cwd()
BASE = 'f417b8ee115533772cb9a8be0244b99958b105af'
SOURCE = '7dd03da47838614e759be1c8a7d6e69b7b8ea35d'
FILES = ['tests/test_pr_integration_boundaries.py', 'tests/test_antigravity_subscription.py', 'tests/test_runner_tools.py', 'tests/test_api_client.py', 'tests/test_egress.py', 'tests/test_machine.py', 'tests/test_image_cache.py']
BASE_TESTS = [
 'tests/test_runner_tools.py::test_artifact_task_overlay_adapts_verifier_collect_without_mutation',
 'tests/test_runner_tools.py::test_run_trial_maps_structured_zcode_quota_to_existing_quota_limit',
 'tests/test_egress.py::test_compose_uses_pinned_image_and_never_dynamic_build',
 'tests/test_egress.py::test_runtime_proxy_token_moves_into_private_compose_environment',
]
subprocess.run(['git','diff','--exit-code',SOURCE,'HEAD','--','src','tests/test_pr_integration_boundaries.py',*FILES,'pyproject.toml','.gitattributes'],check=True)
results = ROOT/'native-results'
results.mkdir()
env=dict(os.environ)
for key in list(env):
 if key.lower() in {'http_proxy','https_proxy','all_proxy','no_proxy'}:
  env.pop(key)
git = Path(shutil.which('git')).resolve()
if os.name == 'nt':
 roots = [root for root in git.parents
          if (root/'cmd'/'git.exe').is_file() and (root/'usr'/'bin'/'sh.exe').is_file()]
 assert len(roots) == 1, (git, roots)
 shell = roots[0]/'usr'/'bin'/'sh.exe'
 env['PATH'] = str(shell.parent)+os.pathsep+env['PATH']
 assert Path(shutil.which('sh',path=env['PATH'])).resolve() == shell.resolve()
else:
 shell=Path(shutil.which('sh')).resolve()
receipt=subprocess.run([str(shell),'-c','printf "DRADAR_NATIVE_SH_RECEIPT\\n"'],env=env,capture_output=True,text=True,check=True)
assert receipt.stdout == 'DRADAR_NATIVE_SH_RECEIPT\n'
identity={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [git,shell,Path(sys.executable),'tests/three-pr-ci/requirements.lock'] if isinstance(p,Path)}
identity['lock_sha256']=hashlib.sha256(Path('tests/three-pr-ci/requirements.lock').read_bytes()).hexdigest()
identity['source']=SOURCE
identity['ci_head']=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
identity['shell_receipt']=receipt.stdout.strip()
print('TOOL_IDENTITY '+json.dumps(identity),flush=True)
(results/'identity.json').write_text(json.dumps(identity,indent=2))
env['PYTHONPATH']=str(ROOT/'src')
candidate=subprocess.run([sys.executable,'-m','pytest','tests/test_egress.py','-k','private_windows_acl or compose_uses_pinned_image or runtime_proxy_token_moves','-q','--tb=short','--basetemp',str(results/'pytest')],env=env,capture_output=True,text=True)
print('CANDIDATE_EXIT '+str(candidate.returncode)+'\n'+candidate.stdout+'\n'+candidate.stderr,flush=True)
(results/'candidate.log').write_text(candidate.stdout+'\n'+candidate.stderr,encoding='utf-8')
if os.name == 'nt':
 for path in sorted((results/'pytest').rglob('docker-compose-egress-proxy.json')):
  acl_script = "$a=Get-Acl -LiteralPath $env:DRADAR_QA_ACL_FILE; @{owner=$a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value; expected_owner=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; rules=@($a.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])|ForEach-Object {@{sid=$_.IdentityReference.Value;type=$_.AccessControlType.ToString();rights=[int64]$_.FileSystemRights}})}|ConvertTo-Json -Depth 4 -Compress"
  acl=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',acl_script],env=dict(env,DRADAR_QA_ACL_FILE=str(path)),capture_output=True,text=True,check=True)
  print('DACL '+str(path.relative_to(results))+' '+acl.stdout.strip(),flush=True)
sys.exit(candidate.returncode)
