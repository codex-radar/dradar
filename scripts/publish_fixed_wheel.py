"""Publish the reviewed four-library 0.5.295 wheel without changing any OTA channel pointer."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path

import httpx
from ota_release import R2Client

SHA256 = "95d0242e0b79341d4c6f253ac9f75b72997f3e6621639da7c2d21c2e38470d1d"
SIZE = 923953
ACCOUNT = "4d94f3bcb89bc16989d5ea715eaac061"
BUCKET = "dradar-cli-ota-production"


def publish(wheel, receipt, store, client, *, source_commit):
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("Expected the actual full reviewed main commit")
    if wheel.name != 'dradar-0.5.295-py3-none-any.whl':
        raise ValueError('Unexpected wheel filename')
    body = wheel.read_bytes()
    if len(body) != SIZE or hashlib.sha256(body).hexdigest() != SHA256:
        raise ValueError('Fixed wheel digest/size mismatch')
    key = f'releases/wheels/{SHA256}/{wheel.name}'
    url = f'https://updates.codexradar.com/{key}'
    response = store.put_new(key, body, content_type='application/octet-stream',
                             cache_control='public,max-age=31536000,immutable')
    if response.status_code not in (200, 201, 204, 412):
        raise RuntimeError(f'R2 conditional upload failed: HTTP {response.status_code}')
    # A 412 is safe only if the immutable existing object has exactly our bytes.
    if response.status_code == 412:
        existing = store.get(key)
        if existing.status_code != 200 or existing.content != body:
            raise RuntimeError('Existing immutable object mismatch')
    with client.stream('GET', url) as downloaded:
        if downloaded.status_code != 200:
            raise RuntimeError(f'Public verification failed: HTTP {downloaded.status_code}')
        digest = hashlib.sha256()
        size = 0
        for chunk in downloaded.iter_bytes():
            size += len(chunk)
            if size > SIZE:
                raise RuntimeError('Public object exceeds expected size')
            digest.update(chunk)
        if size != SIZE or digest.hexdigest() != SHA256:
            raise RuntimeError('Public object digest/size mismatch')
    result = {'status': 'HTTP_200_VERIFIED', 'artifact': {'url': url,
              'sha256': SHA256, 'size_bytes': SIZE}, 'object_key': key,
              'cli_commit': source_commit, 'channel_pointer_changed': False}
    receipt.write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--source-commit', required=True)
    args = parser.parse_args()
    store = R2Client(account_id=ACCOUNT, bucket=BUCKET,
        access_key_id=os.environ.get('DRADAR_OTA_R2_ACCESS_KEY_ID', ''),
        secret_access_key=os.environ.get('DRADAR_OTA_R2_SECRET_ACCESS_KEY', ''))
    try:
        with httpx.Client(follow_redirects=False, timeout=60) as client:
            publish(args.wheel, args.receipt, store, client, source_commit=args.source_commit)
    finally:
        store.close()


if __name__ == '__main__':
    main()
