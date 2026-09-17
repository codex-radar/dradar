"""Host receipt gates the exact bytes that may enter pending/upload."""
import hashlib
import json
from pathlib import Path

import pytest
from dradar import runner
from dradar.artifact_boundary import UnsafeArtifact


@pytest.mark.parametrize('mutation', ['none', 'absent', 'other_run', 'writer_unknown', 'not_exported', 'tampered_patch', 'symlink'])
def test_agy_export_binding(tmp_path, mutation):
    tmp_path.chmod(0o700)
    (tmp_path / 'artifacts').mkdir()
    (tmp_path / '.dradar').mkdir()
    patch = tmp_path / 'artifacts/model.patch'
    patch.write_bytes(b'real bytes')
    receipt = tmp_path / '.dradar/agy-export.json'
    value = dict(schema='dradar-agy-export-v1', run_id='a'*32,
                 writer_stopped=True, exported=True, patch_sha256=hashlib.sha256(patch.read_bytes()).hexdigest())
    if mutation == 'other_run': value['run_id'] = 'b'*32
    if mutation == 'writer_unknown': value['writer_stopped'] = False
    if mutation == 'not_exported': value['exported'] = False
    if mutation == 'tampered_patch': patch.write_bytes(b'tampered')
    if mutation != 'absent': receipt.write_text(json.dumps(value))
    if mutation == 'symlink':
        receipt.rename(tmp_path / 'fake')
        receipt.symlink_to(tmp_path / 'fake')
    if mutation == 'none':
        runner._verify_antigravity_export(tmp_path, patch, {'_artifact_run_id': 'a'*32})
    else:
        with pytest.raises((runner.RunnerError, UnsafeArtifact)):
            runner._verify_antigravity_export(tmp_path, patch, {'_artifact_run_id': 'a'*32})
