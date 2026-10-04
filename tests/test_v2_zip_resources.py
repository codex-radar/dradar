"""Use the actual OTA carrier builder and fresh zip-only module entry; no model."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

def test_formal_zip_carrier_reads_fixed_contract_and_public_skill(tmp_path):
    source=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(source/'scripts'))
    from ota_release import _build_zipapp
    from dradar import __version__
    artifact=tmp_path/'candidate.pyz'
    _build_zipapp(source,artifact,version=__version__,sequence=114,
        commit='a'*40,tree='b'*40,target=('linux','x86_64'))
    with zipfile.ZipFile(artifact) as z:
        assert z.read('__main__.py')==b'from dradar.launcher import main\nraise SystemExit(main())\n'
        assert z.read('dradar/v2/final68_contract.json')==(source/'src/dradar/v2/final68_contract.json').read_bytes()
    probe=r'''
import hashlib,json,runpy,sys
from pathlib import Path
carrier=sys.argv[1]
sys.path.insert(0,carrier)
import dradar,dradar.v2.mixed_pool as mixed
from dradar.v2.skill_install import packaged_skill
assert dradar.__file__.startswith(carrier+'/')
assert mixed.__file__.startswith(carrier+'/')
assert len(mixed.MEMBERS)==68
assert mixed.members_digest(mixed.MEMBERS)==mixed.MEMBERS_SHA256
print(json.dumps({'version':dradar.__version__,'module':mixed.__file__,
    'members_sha256':mixed.MEMBERS_SHA256,'skill_sha256':hashlib.sha256(packaged_skill()).hexdigest()}))
sys.argv=['dradar.v2','schema']
runpy.run_module('dradar.v2',run_name='__main__')
'''
    env={**os.environ,'DRADAR_HOME':str(tmp_path/'isolated-home')}
    result=subprocess.run([sys.executable,'-I','-c',probe,str(artifact)],cwd=tmp_path,
        env=env,capture_output=True,text=True,timeout=30,check=True)
    proof,schema=[json.loads(line) for line in result.stdout.splitlines()]
    assert proof['version']==__version__
    assert proof['members_sha256']=='2ac966bdb44c57401a8518e2a8d83964d9b8c600e5fcce4f6091d1fa5ce8c5e9'
    assert schema['host_runtime']['mixed_pool']['task_count']==68
    assert schema['skill_sha256']==proof['skill_sha256']
    assert not (tmp_path/'isolated-home').exists()
