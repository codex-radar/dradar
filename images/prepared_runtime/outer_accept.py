"""Fresh public-helper and model-tool preparation boundary; never starts a model."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time

def sha(raw):return hashlib.sha256(raw).hexdigest()

def command(argv,out,*,timeout=120):
    p=subprocess.run(argv,capture_output=True,timeout=timeout)
    out.with_suffix('.stdout').write_bytes(p.stdout);out.with_suffix('.stderr').write_bytes(p.stderr)
    if p.returncode:raise RuntimeError('%s exited %s; fixed first error retained in %s'%(argv[0],p.returncode,out))
    return p.stdout

def download_helper(descriptor,target,out):
    target.mkdir()
    raw=command(['curl','--fail','--silent','--show-error','--max-time','45',
        'https://api.codexradar.com'+descriptor['url']],out,timeout=55)
    assert len(raw)==descriptor['size_bytes'] and sha(raw)==descriptor['sha256']
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for item in archive.getmembers():
            assert item.isfile() and '/' not in item.name and item.name not in ('.','..')
        archive.extractall(target,filter='data')
    manifest=(target/'MANIFEST.json').read_bytes();assert sha(manifest)==descriptor['manifest_sha256']
    for item in json.loads(manifest)['files']:
        data=(target/item['name']).read_bytes();assert len(data)==item['size_bytes'] and sha(data)==item['sha256']
    target.chmod(0o755)
    for item in target.iterdir():item.chmod(0o644)

def perform(row,candidate,out):
    out.mkdir()
    pins=json.loads(Path(__file__).with_name('PUBLIC_HELPER_PINS.json').read_bytes())
    binding=pins['images'][row['number']];lib=binding['library'];descriptors=pins['libraries'][lib]
    current=out/'current';ca7=out/'ca7'
    download_helper(descriptors['collector'],current,out/'DOWNLOAD_COLLECTOR')
    download_helper(descriptors['ca7'],ca7,out/'DOWNLOAD_CA7')
    evidence=out/'evidence';evidence.mkdir();evidence.chmod(0o700)
    uid=row['runtime']['uid'];gid=row['runtime']['gid']
    command(['sudo','-n','chown',str(uid)+':'+str(gid),str(evidence)],out/'OWN_EVIDENCE')
    name='dradar210-public-boundary-'+row['number']
    result={'schema':'dradar.public-preparation-boundary/1','candidate':candidate,
        'original_source_image':row['source_image'],'task_contract_sha256':binding['task_contract_sha256'],
        'collector':descriptors['collector'],'ca7':descriptors['ca7'],'model_calls':0,'POSTs':0,
        'real_model_e2e_proven':False,'production_reference_changed':False,
        'prepared_candidate_not_yet_formal_reference':True,'started_at':time.time()}
    created=False
    try:
        command(['docker','create','--name',name,'--cpus','1','--memory','1g','--pids-limit','256',
            '--label','dradar.ticket=210',
            '--mount','type=bind,src='+str(current.resolve())+',dst=/dradar-qa-current,readonly',
            '--mount','type=bind,src='+str(ca7.resolve())+',dst=/dradar-qa-ca7,readonly',
            '--mount','type=bind,src='+str(evidence.resolve())+',dst=/dradar-evidence',
            '--entrypoint','sleep',candidate,'infinity'],out/'CREATE')
        created=True;command(['docker','start',name],out/'START')
        prefix=['docker','exec','--user',str(uid)+':'+str(gid),'--workdir',row['runtime']['cwd'],name]
        observed=json.loads(command(prefix+['python3','-c',
            "import json,os,shutil,sys;assert shutil.which('dradar') is None;assert sys.version_info>=(3,10);print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'cwd':os.getcwd(),'home':os.environ['HOME'],'python_path':sys.executable,'python_version':sys.version,'dradar_executable':None}))"],out/'IDENTITY'))
        assert all(observed[k]==row['runtime'][k] for k in ('uid','gid','cwd','home'))
        result['observed']=observed
        command(prefix+['python3','/dradar-qa-current/helper.py','verify-tools',
            '--manifest-sha256',descriptors['collector']['manifest_sha256']],out/'VERIFY_COLLECTOR')
        command(prefix+['python3','/dradar-qa-ca7/helper.py','verify-tools',
            '--manifest-sha256',descriptors['ca7']['manifest_sha256']],out/'VERIFY_CA7')
        command(prefix+['python3','/dradar-qa-ca7/helper.py','prepare-ca',
            '--manifest-sha256',descriptors['ca7']['manifest_sha256'],
            '--task-contract-sha256',binding['task_contract_sha256'],
            '--project-dir',row['runtime']['cwd'],'--output','/dradar-evidence/ca-preflight'],out/'PREPARE_CA',timeout=150)
        # Run the normal public model-tool installation as the declared user.
        command(prefix+['npm','install','--prefix','/dradar-evidence/codex','--cache',
            '/dradar-evidence/npm-cache','--no-audit','--no-fund','@openai/codex@latest'],out/'PREPARE_CODEX',timeout=360)
        codex='/dradar-evidence/codex/node_modules/.bin/codex'
        # These parse/start the real installed tool, but never execute a prompt.
        result['codex_version']=command(prefix+['env','CODEX_HOME=/dradar-evidence/codex-home',
            codex,'--version'],out/'CODEX_VERSION',timeout=30).decode().strip()
        command(prefix+['env','CODEX_HOME=/dradar-evidence/codex-home',codex,'exec','--help'],out/'CODEX_EXEC_HELP',timeout=30)
        result['codex_executable']=codex
        result['model_start_boundary']='Real installed Codex executable and exec argument parser, no prompt/model invocation'
        result.update(status='PUBLIC_PREPARATION_BOUNDARY_PASSED_NO_MODEL',finished_at=time.time())
    except BaseException as exc:
        result.update(status='FIRST_ERROR_PRESERVED',error=repr(exc),finished_at=time.time());raise
    finally:
        if created:
            cleanup=subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=30)
            (out/'CONTAINER_CLEANUP.stdout').write_bytes(cleanup.stdout)
            (out/'CONTAINER_CLEANUP.stderr').write_bytes(cleanup.stderr)
            result['owned_container_removed']=cleanup.returncode==0
        # Only this job's freshly created evidence directory is reassigned for export.
        command(['sudo','-n','chown','-R',str(os.getuid())+':'+str(os.getgid()),str(evidence)],out/'EXPORT_EVIDENCE')
        receipt=evidence/'ca-preflight/CA_PREFLIGHT.json'
        if receipt.exists():
            ca=json.loads(receipt.read_bytes());result['ca_preflight']=ca
            if result.get('status')=='PUBLIC_PREPARATION_BOUNDARY_PASSED_NO_MODEL':
                assert ca['ready_for_model_start'] is True and ca['install_attempted'] is False
                assert ca['commands']==[] and ca['model_calls']==0 and ca['POSTs']==0
        lock=evidence/'codex/package-lock.json'
        if lock.exists():
            result['npm_lock_sha256']=sha(lock.read_bytes())
            result['codex_package']=json.loads(lock.read_bytes())['packages'].get('node_modules/@openai/codex')
        (out/'OUTER_RECEIPT.json').write_text(json.dumps(result,indent=2)+'\n')
    return result
