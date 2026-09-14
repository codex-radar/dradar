"""Rebuild reviewed bytes independently of Windows checkout newline policy."""
import hashlib,json,subprocess,sys,tempfile
from pathlib import Path
root=Path(__file__).parent
candidate=Path(sys.argv[1]).resolve()
def tool_blob(name):
    return subprocess.check_output(['git','-C',str(root),'show','HEAD:tests/0092-ota-native/'+name])
m=json.loads(tool_blob('CANDIDATES.json'))['cli']
patch=tool_blob('cli.patch')
assert hashlib.sha256(patch).hexdigest()==m['patch_sha256']
def git(*args):
    return subprocess.check_output(['git','-C',str(candidate),'-c','core.autocrlf=false','-c','core.eol=lf',*args],text=True).strip()
assert git('rev-parse','HEAD')==m['base']
assert git('write-tree')==git('rev-parse','HEAD^{tree}')
assert not git('ls-files','--others','--exclude-standard')
# Reject actual preexisting edits, allowing only checkout newline conversion.
subprocess.run(['git','-C',str(candidate),'-c','core.autocrlf=true','diff','--quiet'],check=True)
git('checkout-index','--all','--force')
with tempfile.TemporaryDirectory(prefix='0092-qa-patch-') as directory:
    path=Path(directory)/'candidate.patch';path.write_bytes(patch)
    git('apply','--index',str(path))
assert git('write-tree')==m['tree']
for name,digest in m['files'].items():
    assert hashlib.sha256((candidate/name).read_bytes()).hexdigest()==digest,name
print(json.dumps({'base':m['base'],'tree':m['tree'],'patch_sha256':m['patch_sha256'],'files_verified':len(m['files']),'patch_source':'exact Git blob','checkout_bytes':'LF'}))
