"""Build only declared dependency gaps on immutable task images; no CLI or models."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

NODE='docker.io/library/node@sha256:efd0ab5780c2d9ab1f0f869571a00d5edb17793bff4cce4a2792e3eb0ffc7562'
NODE_VERSION='v22.23.3'
TREE=r'''
import hashlib,json,os,pathlib,stat
root=pathlib.Path('/app');assert root.resolve()==root
digest=hashlib.sha256();count=0
for directory,dirs,files in os.walk(root,followlinks=False):
 for name in sorted(dirs+files):
  p=pathlib.Path(directory)/name;s=p.lstat();row=[str(p.relative_to(root)),stat.S_IFMT(s.st_mode),stat.S_IMODE(s.st_mode)]
  if p.is_symlink():row.append(os.readlink(p))
  elif p.is_file():
   h=hashlib.sha256()
   with p.open('rb') as f:
    for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
   row.extend([s.st_size,h.hexdigest()])
  digest.update((json.dumps(row,separators=(',',':'))+'\n').encode());count+=1
print(json.dumps({'content_and_modes_sha256':digest.hexdigest(),'entries':count}))
'''
PROBE=r'''
import json,os,shutil,ssl,subprocess,sys,tempfile
assert sys.version_info>=(3,10),sys.version
assert shutil.which('dradar') is None,'DRadar CLI must not be required or embedded'
assert shutil.which('bash') and shutil.which('node') and shutil.which('npm')
c=ssl.create_default_context(); assert c.check_hostname and c.verify_mode==ssl.CERT_REQUIRED
assert len(c.get_ca_certs())>0,'No default trusted roots'
versions={}
for command in ('node','npm','bash'):
 p=subprocess.run([command,'--version'],capture_output=True,text=True,timeout=10)
 assert p.returncode==0,(command,p.stderr);versions[command]=p.stdout[:300]
for directory in (os.getcwd(),os.environ['HOME'],'/tmp'):
 with tempfile.TemporaryFile(dir=directory,prefix='.dradar-image-qa-') as f:f.write(b'probe')
print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'cwd':os.getcwd(),'home':os.environ['HOME'],
 'python':sys.version,'python_path':sys.executable,'root_count':len(c.get_ca_certs()),
 'versions':versions,'dradar_executable':None,'models':0},sort_keys=True))
'''

def run(command, *, output=None, timeout=1800):
    result=subprocess.run(command,capture_output=True,timeout=timeout)
    if output:
        output.with_suffix('.stdout').write_bytes(result.stdout)
        output.with_suffix('.stderr').write_bytes(result.stderr)
    if result.returncode:
        raise RuntimeError('%s exited %s: %s'%(command[0],result.returncode,result.stderr.decode(errors='replace')[-2000:]))
    return result.stdout

def write(path,value):path.write_text(json.dumps(value,indent=2)+'\n')

def dockerfile(row):
    lines=[]
    if row['install_node']:lines.append('FROM '+NODE+' AS node_tools')
    lines+=['FROM '+row['source_image'],'USER 0:0']
    if row['install_python_or_ca']:
        lines += ['RUN apt-get update && apt-get install -y --no-install-recommends --no-upgrade python3 ca-certificates']
    if row.get('restore_default_ca_link'):
        lines += ['RUN mkdir -p /usr/lib/ssl && test ! -e /usr/lib/ssl/cert.pem && test ! -L /usr/lib/ssl/cert.pem && ln -s /etc/ssl/certs/ca-certificates.crt /usr/lib/ssl/cert.pem']
    if row['install_node']:
        lines += ['COPY --from=node_tools /usr/local/bin/node /opt/dradar-node/bin/node',
            'COPY --from=node_tools /usr/local/lib/node_modules/npm /opt/dradar-node/lib/node_modules/npm',
            'RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /opt/dradar-node/bin/npm && ln -s ../lib/node_modules/npm/bin/npx-cli.js /opt/dradar-node/bin/npx',
            'ENV PATH="/opt/dradar-node/bin:${PATH}"']
    if row.get('prepare_project_owner'):
        uid=row['runtime']['uid'];gid=row['runtime']['gid']
        assert row['runtime']['cwd']=='/app' and uid==65534 and gid==65534
        lines+=['RUN test ! -L /app && chown -R 65534:65534 /app']
    # Keep the image's declared user string and inherited cwd/HOME/entrypoint.
    lines+=['USER '+(row['original_user'] or '0:0')]
    return '\n'.join(lines)+'\n'

def main():
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['build','accept']);p.add_argument('--number',required=True)
    p.add_argument('--reviewed',required=True);p.add_argument('--batch-hash',required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--candidate',default='');args=p.parse_args()
    root=Path(__file__).resolve().parent;raw=(root/'BATCH.json').read_bytes();batch=json.loads(raw)
    assert hashlib.sha256(raw).hexdigest()==args.batch_hash
    assert re.fullmatch('[0-9a-f]{40}',args.reviewed)
    assert os.environ['GITHUB_SHA']==args.reviewed
    assert os.environ['GITHUB_ACTOR']=='SecurityMind'
    assert os.environ['GITHUB_REPOSITORY']=='codex-radar/dradar'
    assert os.environ['GITHUB_REF']=='refs/heads/codex/runtime-images-210'
    assert os.environ['GITHUB_EVENT_NAME']=='workflow_dispatch'
    row=next(x for x in batch['rows'] if x['number']==args.number)
    assert re.fullmatch(r'ghcr\.io/codex-radar/[a-z0-9-]+@sha256:[0-9a-f]{64}',row['source_image'])
    assert row['install_node'] or row['install_python_or_ca']
    args.out.mkdir(parents=True,exist_ok=False);out=args.out
    if args.phase=='accept':
        anonymous=out/'anonymous-docker';anonymous.mkdir()
        (anonymous/'config.json').write_text('{"auths":{}}')
        os.environ['DOCKER_CONFIG']=str(anonymous)
    record={'schema':'dradar.prepared-runtime-image/1','source_commit':args.reviewed,
        'batch_sha256':args.batch_hash,'row':row,'phase':args.phase,'started_at':time.time(),
        'model_calls':0,'production_reference_changed':False,'verifier_changed':False}
    write(out/'INPUT.json',record)
    try:
        if args.phase=='build':
            run(['docker','pull',row['source_image']],output=out/'SOURCE_PULL')
            info=json.loads(run(['docker','image','inspect',row['source_image']]))[0]
            assert info['Architecture']=='amd64' and info['Os']=='linux'
            assert row['source_image'] in info['RepoDigests']
            assert info['Config'].get('User','')==row['original_user']
            if row.get('prepare_project_owner'):
                record['source_project']=json.loads(run(['docker','run','--rm','--network','none',
                    '--entrypoint','python3',row['source_image'],'-c',TREE],output=out/'SOURCE_PROJECT',timeout=90))
            text=dockerfile(row);(out/'Dockerfile').write_text(text)
            tag=row['source_image'].split('@')[0]+':deps210-'+args.reviewed
            run(['docker','buildx','build','--platform','linux/amd64','--provenance=false','--load',
                '--label','org.opencontainers.image.source=https://github.com/codex-radar/dradar',
                '--label','org.opencontainers.image.revision='+args.reviewed,
                '--label','com.codexradar.source-image='+row['source_image'],
                '--label','com.codexradar.preparation-schema=dradar.prepared-runtime-image/1',
                '--tag',tag,str(out)],output=out/'BUILD',timeout=2400)
            candidate=tag
        else:
            assert args.candidate.startswith(row['source_image'].split('@')[0]+'@sha256:')
            assert re.fullmatch(r'ghcr\.io/codex-radar/[a-z0-9-]+@sha256:[0-9a-f]{64}',args.candidate)
            candidate=args.candidate
            # This job has not built or repaired any task container.
            run(['docker','pull',candidate],output=out/'ANONYMOUS_PULL')
        probe=json.loads(run(['docker','run','--rm','--network','none','--cpus','1','--memory','512m',
            '--pids-limit','128','--entrypoint','python3',candidate,'-c',PROBE],output=out/'PROBE',timeout=90))
        assert all(probe[k]==row['runtime'][k] for k in ('uid','gid','cwd','home'))
        if row['install_node']:assert probe['versions']['node'].strip()==NODE_VERSION
        if row['git_required']:
            run(['docker','run','--rm','--network','none','--entrypoint','git',candidate,'--version'],output=out/'GIT',timeout=30)
        config=json.loads(run(['docker','image','inspect',candidate]))[0]
        if row.get('prepare_project_owner'):
            record['prepared_project']=json.loads(run(['docker','run','--rm','--network','none',
                '--entrypoint','python3',candidate,'-c',TREE],output=out/'PREPARED_PROJECT',timeout=90))
            if args.phase=='build':assert record['prepared_project']==record['source_project'],'Original project bytes or modes changed'
        if args.phase=='build':
            assert config['RootFS']['Layers'][:len(info['RootFS']['Layers'])]==info['RootFS']['Layers']
            for key in ('WorkingDir','Entrypoint','Cmd'):
                assert config['Config'].get(key)==info['Config'].get(key),(key,'original execution configuration changed')
            source_env=dict(value.split('=',1) for value in info['Config'].get('Env') or [])
            new_env=dict(value.split('=',1) for value in config['Config'].get('Env') or [])
            assert {k:v for k,v in source_env.items() if k!='PATH'}=={k:v for k,v in new_env.items() if k!='PATH'}
            assert config['Config'].get('User')==(row['original_user'] or '0:0')
        run(['docker','run','--rm','--network','none','--entrypoint','/bin/sh',candidate,'-c',
            'if command -v dpkg-query >/dev/null 2>&1; then dpkg-query -W; else exit 0; fi'],
            output=out/'PACKAGE_VERSIONS',timeout=60)
        write(out/'IMAGE_CONFIG.json',config)
        if args.phase=='build':
            run(['docker','push',candidate],output=out/'PUSH',timeout=2400)
            config=json.loads(run(['docker','image','inspect',candidate]))[0]
            digests=[x for x in config.get('RepoDigests',[]) if x.startswith(row['source_image'].split('@')[0]+'@sha256:')]
            assert len(digests)==1,digests
            candidate=digests[0]
        manifest=run(['docker','manifest','inspect',candidate],output=out/'MANIFEST')
        # Bind registry manifest bytes separately; Docker may pretty-print it.
        record.update(status='DEPENDENCIES_VERIFIED_NO_MODEL',candidate=candidate,
            probe=probe,image_id=config['Id'],repo_digests=config.get('RepoDigests',[]),
            manifest=json.loads(manifest),dockerfile_sha256=hashlib.sha256(dockerfile(row).encode()).hexdigest(),
            finished_at=time.time())
        write(out/'RECEIPT.json',record)
    except BaseException as exc:
        record.update(status='FAILED_FIRST_ERROR_PRESERVED',error=repr(exc),finished_at=time.time())
        write(out/'FAILURE.json',record);raise

if __name__=='__main__':main()
