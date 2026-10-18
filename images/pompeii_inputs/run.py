"""Build immutable original-input images, or verify anonymous clean pulls."""
from pathlib import Path
from hashlib import sha256
import argparse,json,os,subprocess,tarfile,tempfile

ROOT=Path(__file__).parent
PACKAGE='ghcr.io/codex-radar/dradar-pompeii-runtime'
HELPER_SHA='feb7cca4c1e9171b26ee0e9fbfc6a6e7109f96ee84fe660d2450b7a76d5ae5f8'
HELPER_BYTES=14530
VERIFY=r'''
import os,json,sys,subprocess
from pathlib import Path
from hashlib import sha256
files=json.loads(sys.argv[1]);root=Path('/app')
assert os.getuid()==0 and os.getgid()==0 and os.getcwd()=='/app' and os.environ['HOME']=='/root'
actual=sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and '.git' not in p.relative_to(root).parts)
assert actual==sorted(f['path'] for f in files),(actual,files)
for f in files:
 p=root/f['path'];assert not p.is_symlink() and p.stat().st_size==f['bytes'] and sha256(p.read_bytes()).hexdigest()==f['sha256'],str(p)
def git(*a):return subprocess.check_output(['git',*a],text=True).strip()
head=git('rev-parse','HEAD');assert git('rev-parse','pompeii-base')==head
assert git('status','--porcelain')=='' and git('rev-list','--count','HEAD')=='1'
assert len(git('ls-files').splitlines())==len(files)
assert json.loads((root/'model_answer.json').read_text())=={'edges':[]}
probe=root/'.write-check';probe.write_text('isolated write check');probe.unlink()
assert os.access('/root',os.W_OK)
print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'cwd':os.getcwd(),'home':os.environ['HOME'],'git_head':head,'git_tag':'pompeii-base','files':files,'no_input_mounts':True}))
'''


def run(args,out,label):
 p=subprocess.run(args,capture_output=True,timeout=600)
 (out/(label+'.stdout')).write_bytes(p.stdout);(out/(label+'.stderr')).write_bytes(p.stderr)
 if p.returncode:raise RuntimeError(label+' failed: '+str(p.returncode))
 return p.stdout


def helper_check(image,identity,helper,out):
 cid=run(['docker','run','-d','--network','none','--entrypoint','sleep',image,'300'],out,'create').decode().strip()
 try:
  run(['docker','exec',cid,'mkdir','-p','/dradar-tools','/dradar-evidence'],out,'dirs')
  run(['docker','cp',str(helper),cid+':/dradar-tools/helper'],out,'helper-copy')
  prefix=['docker','exec',cid,'python3','/dradar-tools/helper/helper.py']
  binding=json.loads(run(prefix+['bind-auth','--root','/','--output','/dradar-evidence/auth.json'],out,'bind'))
  task=next(r for r in json.loads((helper/'tasks.json').read_bytes())['tasks'] if r['task_id']==identity['task_id'])
  rec={'task':identity,'collect':{'command':task['collect_command'],'helper':{}}}
  (out/'recommendation.json').write_text(json.dumps(rec))
  (out/'files.json').write_text('[]')
  for name in ('recommendation.json','files.json'):run(['docker','cp',str(out/name),cid+':/dradar-evidence/'+name],out,'copy-'+name)
  auth=['--root','/','--auth-bindings','/dradar-evidence/auth.json','--auth-bindings-sha256',binding['sha256']]
  run(prefix+['capture',*auth,'--recommendation','/dradar-evidence/recommendation.json','--output','/dradar-evidence/capture'],out,'capture')
  packed=json.loads(run(prefix+['pack',*auth,'--capture','/dradar-evidence/capture','--files','/dradar-evidence/files.json','--output','/dradar-evidence/bundle.tar.gz'],out,'pack'))
  run(['docker','cp',cid+':/dradar-evidence/capture',str(out/'capture')],out,'export-capture')
  run(['docker','cp',cid+':/dradar-evidence/bundle.tar.gz',str(out/'bundle.tar.gz')],out,'export-bundle')
  raw=(out/'bundle.tar.gz').read_bytes()
  assert sha256(raw).hexdigest()==packed['bundle_sha256'] and len(raw)==packed['size_bytes']
  return {'helper_sha256':HELPER_SHA,'pack':packed,'model_calls':0,'formal_claims':0,'uploads':0}
 finally:
  run(['docker','rm','-f',cid],out,'remove')


def main():
 p=argparse.ArgumentParser();p.add_argument('phase',choices=['build','accept']);p.add_argument('--out',required=True,type=Path);p.add_argument('--reviewed',required=True);p.add_argument('--build-receipt',type=Path)
 a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
 assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()==a.reviewed
 rows=json.loads((ROOT/'contexts/contexts.json').read_bytes())['rows']
 identities={r['task_id']:r for r in json.loads((ROOT/'source_identities.json').read_bytes())}
 receipts=[]
 if a.phase=='accept':
  assert not (Path(os.environ['DOCKER_CONFIG'])/'config.json').exists(),'anonymous Docker config required'
  built={r['task']['task_id']:r for r in json.loads(a.build_receipt.read_bytes())['rows']}
  helper=a.out/'helper';helper.mkdir()
  archive=a.out/'helper.tar.gz'
  run(['curl','-fSs','--connect-timeout','10','--max-time','30','https://api.codexradar.com/api/v2/agent/helpers/pompeii/'+HELPER_SHA+'.tar.gz','-o',str(archive)],a.out,'helper-download')
  assert archive.stat().st_size==HELPER_BYTES and sha256(archive.read_bytes()).hexdigest()==HELPER_SHA
  with tarfile.open(archive) as tar:
   assert all(m.isfile() and '/' not in m.name and m.name not in ('.','..') for m in tar.getmembers())
   tar.extractall(helper,filter='data')
 for row in rows:
  out=a.out/row['task_id'];out.mkdir()
  if a.phase=='build':
   tag=PACKAGE+':'+row['task_id']+'-inputs-'+a.reviewed[:12]
   run(['docker','build','--network','none','-t',tag,str(ROOT/'contexts'/row['task_id'])],out,'build')
   run(['docker','push',tag],out,'push')
   inspect=json.loads(run(['docker','image','inspect',tag],out,'image'))[0]
   image=next(d for d in inspect['RepoDigests'] if d.startswith(PACKAGE+'@'))
  else:
   image=built[row['task_id']]['image']
   run(['docker','pull',image],out,'anonymous-pull')
   inspect=json.loads(run(['docker','image','inspect',image],out,'image'))[0]
  observed=json.loads(run(['docker','run','--rm','--network','none','--entrypoint','python3',image,'-c',VERIFY,json.dumps(row['files'])],out,'content-check'))
  assert not inspect['Config'].get('Volumes') and inspect['Architecture']=='amd64' and inspect['Os']=='linux'
  result={'task':identities[row['task_id']],'image':image,'config_digest':inspect['Id'],'runtime':{k:observed[k] for k in ('uid','gid','cwd','home')},
          'task_input':{'kind':'image_embedded_original_inputs','source_bundle_sha256':row['source_bundle_sha256'],
          'task_content_hash':row['task_content_hash'],'files':observed['files'],'git_head':observed['git_head'],'git_tag':'pompeii-base',
          'anonymous_pull_verified':a.phase=='accept','helper_verified':False,'no_input_mounts':True}}
  if a.phase=='accept':
   result['helper_check']=helper_check(image,identities[row['task_id']],helper,out)
   result['task_input']['helper_verified']=True
  receipts.append(result)
  (a.out/'receipt.json').write_text(json.dumps({'schema':'dradar.pompeii-images/1','phase':a.phase,'commit':a.reviewed,'rows':receipts},indent=2)+'\n')
  print(row['task_id'],image,flush=True)
 assert len(receipts)==16

if __name__=='__main__':main()
