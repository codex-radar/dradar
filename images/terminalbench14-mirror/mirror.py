"""Copy fixed official images unchanged; verify identity or anonymous Docker pulls."""
from __future__ import annotations
import argparse, concurrent.futures, hashlib, json, os, pathlib, subprocess, sys, time, tarfile, tempfile
P = pathlib.Path

def require(ok, msg):
    if not ok: raise ValueError(msg)
def sha(b): return 'sha256:' + hashlib.sha256(b).hexdigest()
def write(p, obj): P(p).write_text(json.dumps(obj, indent=2)+'\n')
def run(cmd, env=None, timeout=1800):
    r=subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if r.returncode: raise RuntimeError(f'{cmd[0]} exited {r.returncode}: '+r.stderr.decode(errors='replace')[-3000:])
    return r.stdout

def identity(manifest, config, e):
    require(sha(manifest)==e['source_digest'], 'manifest digest differs')
    require(sha(config)==e['config_digest'], 'config digest differs')
    m,c=json.loads(manifest),json.loads(config)
    require(m['config']['digest']==e['config_digest'] and m['config']['size']==len(config), 'config descriptor differs')
    require(m['layers']==e['layers'], 'complete ordered layer descriptors differ')
    require(c['os']=='linux' and c['architecture']=='amd64', 'platform differs')
    require(c['rootfs']['type']=='layers' and c['rootfs']['diff_ids']==e['rootfs_diff_ids'], 'rootfs differs')
    return {'manifest_digest':sha(manifest),'config_digest':sha(config),'platform':'linux/amd64','layers':m['layers'],
            'rootfs_diff_ids':c['rootfs']['diff_ids'],'labels':c.get('config',{}).get('Labels'),
            'original_manifest_config_layers_unchanged':True,'image_built_committed_executed':False}

def gate(root, reviewed, fixed_hash):
    raw=(root/'BATCH.json').read_bytes(); b=json.loads(raw)
    require(hashlib.sha256(raw).hexdigest()==fixed_hash,'fixed batch hash differs')
    require(os.environ.get('GITHUB_SHA')==reviewed,'reviewed commit differs')
    require(os.environ.get('GITHUB_ACTOR')=='SecurityMind','actor differs')
    require(os.environ.get('GITHUB_REPOSITORY')=='codex-radar/dradar','repository differs')
    require(os.environ.get('GITHUB_REF')=='refs/heads/codex/terminalbench14-official-mirror-20261008','branch differs')
    require(os.environ.get('GITHUB_EVENT_NAME')=='workflow_dispatch','dispatch required')
    require(len(b['rows'])==28 and [e['display_number'] for e in b['rows']]==[f'{x:03}-{k}' for x in range(32,46) for k in ('env','verifier')], '14 fixed tasks and both official image roles required')
    require(len({e['task_id'] for e in b['rows']})==14 and b['schema']=='dradar.terminalbench14-official-mirror/1', 'exact Terminal-Bench 14 scope required')
    for e in b['rows']:
        require(e['kind'] in ('environment','verifier') and e['target_image']=='ghcr.io/codex-radar/dradar-'+('env' if e['kind']=='environment' else 'verifier')+'-'+e['task_id'], 'target scope differs')
        require(e['source_commit']=='452bf305c6daa62fc59061d22133a7cbc7c1572e' and e['source_registry']=='docker.io/harborframework/terminal-bench' and e['target_tag']=='tb4-v4.0.0-official-20261008', 'official source/release scope differs')
        require(e['source_reference']==e['source_registry']+'@'+e['source_digest'],'source pin differs')
    return b

def metadata(e, env, require_public=False):
    j=json.loads(run(['gh','api','orgs/codex-radar/packages/container/'+e['target_image'].split('/')[-1]],env,timeout=90))
    repo=j.get('repository') or {}
    v={'name':j.get('name'),'package_type':j.get('package_type'),'visibility':j.get('visibility'),'repository':repo.get('full_name')}
    require(v['name']==e['target_image'].split('/')[-1] and v['package_type']=='container','package metadata differs')
    if require_public:require(v['visibility']=='public' and v['repository']=='codex-radar/dradar','Public visibility / repository link incomplete')
    return v

def inspect(ref, auth, env):
    prefix=['skopeo','inspect','--authfile',str(auth)]
    return run(prefix+['--raw','docker://'+ref],env,90),run(prefix+['--config','--raw','docker://'+ref],env,90)

def mirror(b, out):
    out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tb14-registry-auth-') as secret_dir:
        auth=pathlib.Path(secret_dir)/'ghcr-auth.json'; empty=pathlib.Path(secret_dir)/'empty-source-auth.json'; empty.write_text('{"auths":{}}\n');empty.chmod(0o600)
        env=dict(os.environ)
        token=env['GH_TOKEN']
        p=subprocess.run(['skopeo','login','--authfile',str(auth),'--username','SecurityMind','--password-stdin','ghcr.io'],input=token.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        require(p.returncode==0,'temporary GHCR login failed'); auth.chmod(0o600)
        sources={};results={}
        def one(e, alternate=None):
            d=out/e['display_number'];d.mkdir()
            receipt={'display_number':e['display_number'],'task_id':e['task_id'],'status':'PENDING','source_reference':e['source_reference'],
                     'target_image':e['target_image'],'run_id':os.environ['GITHUB_RUN_ID'],'commit':os.environ['GITHUB_SHA'],
                     'started_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'source_for_copy':alternate or e['source_reference']}
            start=time.monotonic()
            try:
                sm,sc=inspect(e['source_reference'],empty,env);identity(sm,sc,e)
                (d/'SOURCE_MANIFEST.json').write_bytes(sm);write(d/'SOURCE_IDENTITY.json',identity(sm,sc,e))
                ref=e['target_image']+':'+e['target_tag']; copy_src=alternate or e['source_reference'];srcauth=auth if alternate else empty
                with (d/'COPY.log').open('wb') as log:
                    p=subprocess.run(['skopeo','copy','--all','--preserve-digests','--src-authfile',str(srcauth),'--dest-authfile',str(auth),
                        '--digestfile',str(d/'copy.digest'),'docker://'+copy_src,'docker://'+ref],env=env,stdout=log,stderr=subprocess.STDOUT,timeout=2400)
                require(p.returncode==0,'skopeo copy exited '+str(p.returncode)+'; see COPY.log')
                require((d/'copy.digest').read_text().strip()==e['source_digest'],'copied digest differs')
                tm,tc=inspect(e['target_image']+'@'+e['source_digest'],auth,env)
                require(sm==tm and sc==tc,'source/target raw bytes differ')
                (d/'TARGET_MANIFEST.json').write_bytes(tm);write(d/'TARGET_IDENTITY.json',identity(tm,tc,e))
                receipt.update(status='COPIED',target_digest=e['source_digest'],package=metadata(e,env),raw_manifest_equal=True,raw_config_equal=True)
            except BaseException as ex:receipt.update(status='FAILED',error_type=type(ex).__name__,error=str(ex))
            receipt['elapsed_seconds']=time.monotonic()-start;write(d/'COPY_RECEIPT.json',receipt)
            print(json.dumps({'number':e['display_number'],'status':receipt['status'],'elapsed_seconds':receipt['elapsed_seconds']}),flush=True)
            results[e['display_number']]=receipt
            return receipt
        # One seed populates Skopeo's persistent blob-location cache. Later copies
        # share that cache, allowing GHCR cross-repository reuse of common blobs.
        first=b['rows'][0]; r=one(first)
        if r['status']=='COPIED':sources[first['source_digest']]=first['target_image']+'@'+first['source_digest']
        unique=[];duplicates=[]
        for e in b['rows'][1:]:
            if e['source_digest'] in sources:duplicates.append(e)
            else:unique.append(e);sources[e['source_digest']]=e['target_image']+'@'+e['source_digest']
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:list(ex.map(one,unique))
            for e in duplicates:one(e,sources[e['source_digest']])
            write(out/'BATCH_COPY_RECEIPT.json',{'rows':[results[e['display_number']] for e in b['rows']],
                'max_copy_processes':3,'source_digest_duplicates_reused_from_ghcr':len(duplicates),
                'skopeo_blob_location_cache_shared':True,'network_bytes_measured':False})
            require(all(r['status']=='COPIED' for r in results.values()) and len(results)==28,'some copy rows failed')
        finally:
            auth.unlink(missing_ok=True)

def anonymous(b,out,group):
    out.mkdir(parents=True,exist_ok=True)
    rows=[e for e in b['rows'] if e['anonymous_group']==group];require(rows,'empty group')
    env=dict(os.environ);results=[]
    # Authenticated API reads are separate from all registry/Docker traffic.
    for e in rows:
        d=out/e['display_number'];d.mkdir();write(d/'PACKAGE_PUBLIC_READBACK.json',metadata(e,env,True))
    for k in ['GH_TOKEN','GITHUB_TOKEN','DOCKER_AUTH_CONFIG','REGISTRY_AUTH_FILE','DOCKER_CONTEXT','DOCKER_HOST']:env.pop(k,None)
    cfg=out/'empty-docker-config';cfg.mkdir(mode=0o700);(cfg/'config.json').write_text('{}\n');env['DOCKER_CONFIG']=str(cfg)
    auth=out/'empty-registry-auth.json';auth.write_text('{"auths":{}}\n');auth.chmod(0o600)
    data=out/'fresh-data';exe=out/'fresh-exec';pidfile=out/'dockerd.pid';sock=out/'dockerd.sock';address='unix://'+str(sock)
    require(not data.exists() and not exe.exists(),'fresh Docker root required')
    docker=['docker','--host',address,'--config',str(cfg)];process=None;seen=set();failure=None
    daemon_receipt={'fresh_root':str(data),'group':group,'runner':os.environ.get('RUNNER_NAME'),'run_id':os.environ['GITHUB_RUN_ID'],'daemon_exit_confirmed':False}
    with (out/'DOCKERD.log').open('wb') as log:
        try:
            process=subprocess.Popen(['sudo','-n','dockerd','--data-root',str(data),'--exec-root',str(exe),'--pidfile',str(pidfile),'--host',address,
                '--bridge=none','--iptables=false','--ip-masq=false','--ip-forward=false','--storage-driver=overlay2'],env=env,stdout=log,stderr=subprocess.STDOUT)
            end=time.monotonic()+60
            while time.monotonic()<end:
                require(process.poll() is None,'owned Docker daemon exited')
                try:
                    info=json.loads(run(docker+['info','--format','{{json .}}'],env,3))
                    if info['DockerRootDir']==str(data):break
                except Exception:time.sleep(.5)
            else:raise RuntimeError('owned daemon not ready')
            require(not run(docker+['image','ls','-aq'],env,30).strip(),'new data root is not empty')
            require(not run(docker+['container','ls','-aq'],env,30).strip(),'new data root has containers')
            daemon_receipt.update(initial_images=0,initial_containers=0)
            for e in rows:
                d=out/e['display_number'];start=time.monotonic(); image=e['target_image']+'@'+e['source_digest']
                r={'display_number':e['display_number'],'task_id':e['task_id'],'image':image,'status':'PUBLIC_VERIFIED',
                    'registry_credentials_used':False,'docker_login_executed':False,'fresh_isolated_daemon':True,
                    'prior_batch_image_count':len(results),'prior_matching_rootfs_layers':len(set(e['rootfs_diff_ids'])&seen),
                    'first_pull_in_empty_data_root':not results,'model_calls':0,'grader_calls':0,'image_executed':False,'run_id':os.environ['GITHUB_RUN_ID'],'commit':os.environ['GITHUB_SHA']}
                try:
                    require(json.loads((cfg/'config.json').read_text())=={} and json.loads(auth.read_text())=={'auths':{}},'anonymous auth not empty')
                    m,c=inspect(image,auth,env);identity(m,c,e);(d/'ANONYMOUS_MANIFEST.json').write_bytes(m)
                    with (d/'FULL_ANONYMOUS_PULL.log').open('wb') as pull_log:
                        p=subprocess.run(docker+['pull','--platform','linux/amd64',image],env=env,stdout=pull_log,stderr=subprocess.STDOUT,timeout=2400)
                    require(p.returncode==0,'complete anonymous docker pull exited '+str(p.returncode)+'; see FULL_ANONYMOUS_PULL.log')
                    a=json.loads(run(docker+['image','inspect',image],env,60))[0]
                    require(a['Id'] in (e['config_digest'], e['source_digest']) and a['RootFS']['Layers']==e['rootfs_diff_ids'] and image in a['RepoDigests'] and a['Architecture']=='amd64' and a['Os']=='linux','Docker full pull identity differs')
                    archive=d/'FULL_PULL_IMAGE.tar'
                    try:
                        run(docker+['image','save','--output',str(archive),image],env,600)
                        with tarfile.open(archive) as saved:
                            exported=json.load(saved.extractfile('manifest.json'))[0]
                            saved_config=saved.extractfile(exported['Config']).read()
                        require(sha(saved_config)==e['config_digest'] and saved_config==c, 'fresh full-pull saved config differs')
                    finally:
                        archive.unlink(missing_ok=True)
                    r['docker_image_id_semantics']='manifest' if a['Id']==e['source_digest'] else 'config'
                    r['saved_full_pull_config_sha256']=sha(saved_config)
                    write(d/'ANONYMOUS_IDENTITY.json',identity(m,c,e))
                    require(json.loads((cfg/'config.json').read_text())=={} and json.loads(auth.read_text())=={'auths':{}},'anonymous auth changed')
                    r.update(status='ANONYMOUS_PULL_VERIFIED',full_pull_exit_code=0,empty_auth_before_after=True,manifest_config_layers_rootfs_verified=True)
                    seen.update(e['rootfs_diff_ids'])
                except BaseException as ex:r.update(status='FAILED',error_type=type(ex).__name__,error=str(ex));failure=ex
                r['elapsed_seconds']=time.monotonic()-start;results.append(r);write(d/'ANONYMOUS_PULL_RECEIPT.json',r);print(json.dumps(r),flush=True)
            require(all(r['status']=='ANONYMOUS_PULL_VERIFIED' for r in results),'some anonymous rows failed')
        except BaseException as ex:failure=ex;daemon_receipt['error']=str(ex)
        finally:
            try:
                if process is not None and process.poll() is None:
                    pid=int(pidfile.read_text().strip());parts=(P('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')
                    require(b'--data-root' in parts and parts[parts.index(b'--data-root')+1]==os.fsencode(data),'refuse to stop unrelated daemon')
                    run(['sudo','-n','kill','-TERM',str(pid)],env,10);process.wait(timeout=60)
                    require(not (P('/proc')/str(pid)/'cmdline').exists(),'daemon exit unconfirmed')
                daemon_receipt['daemon_exit_confirmed']=True
            except BaseException as ex:daemon_receipt['cleanup_error']=str(ex);failure=failure or ex
            write(out/'DAEMON_EXIT_RECEIPT.json',daemon_receipt);write(out/'BATCH_ANONYMOUS_RECEIPT.json',{'rows':results})
    if failure:raise failure

def main():
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['mirror','anonymous']);p.add_argument('--root',type=P,required=True);p.add_argument('--out',type=P,required=True);p.add_argument('--reviewed',required=True);p.add_argument('--batch-hash',required=True);p.add_argument('--group',type=int,default=0);a=p.parse_args()
    b=gate(a.root,a.reviewed,a.batch_hash)
    if a.phase=='mirror':mirror(b,a.out)
    else:anonymous(b,a.out,a.group)
if __name__=='__main__':main()
