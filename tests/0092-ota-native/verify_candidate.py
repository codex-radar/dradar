"""Rebuild only the explicit reviewed patch against the exact public base."""
import hashlib,json,subprocess,sys
from pathlib import Path
root=Path(__file__).parent
candidate=Path(sys.argv[1]).resolve()
m=json.loads((root/'CANDIDATES.json').read_text())['cli']
patch=root/'cli.patch'
assert hashlib.sha256(patch.read_bytes()).hexdigest()==m['patch_sha256']
def git(*args):return subprocess.check_output(['git','-C',str(candidate),*args],text=True).strip()
assert git('rev-parse','HEAD')==m['base']
assert not git('status','--porcelain')
git('apply','--index',str(patch.resolve()))
assert git('write-tree')==m['tree']
for name,digest in m['files'].items():
 assert hashlib.sha256((candidate/name).read_bytes()).hexdigest()==digest,name
print(json.dumps({'base':m['base'],'tree':m['tree'],'patch_sha256':m['patch_sha256'],'files_verified':len(m['files'])}))
