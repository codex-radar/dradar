"""Read-only preflight for an explicitly reviewed server secret rejection.

Only added hunk content can change. This does not clear a persisted block,
authorize a model run, or weaken the server's credential scan.
"""

import hashlib
import re
from pathlib import Path

from .artifact_boundary import TrialFiles
from .scrub import patch_structure_is_valid, redact_patch_secrets, scan_secrets
from .submission_intent import UPLOAD_INTENT_VERSION, upload_intent_id


def review_patch(entry: dict, expected_sha256: str) -> dict:
    if (entry.get("upload_blocked") != "server_secret_guard"
            or entry.get("outcome") != "completed"
            or any(not isinstance(entry.get(k), str) or not entry[k]
                   for k in ("assignment_id", "nonce", "task_id", "trial_dir",
                             "runner_session_id", "batch_id"))
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "")
            or entry.get("patch_sha256") != expected_sha256
            or type(entry.get("owner_epoch")) is not int
            or entry["owner_epoch"] < 0):
        raise ValueError("exact completed server-secret rejection binding is missing")
    with TrialFiles(Path(entry["trial_dir"])) as files:
        source = files.read(".dradar/artifact-staging/model.patch.source")
        staged = files.read("artifacts/model.patch")
        files.verify()
    if (source != staged or hashlib.sha256(source).hexdigest() != expected_sha256
            or type(entry.get("patch_bytes")) is not int
            or len(source) != entry["patch_bytes"]):
        raise ValueError("preserved patch bytes differ from the reviewed digest")
    sanitized, labels, unsafe = redact_patch_secrets(source)
    if (not labels or unsafe or scan_secrets(sanitized)
            or not patch_structure_is_valid(sanitized)):
        raise ValueError("patch cannot be safely redacted in added hunk lines")
    sanitized_sha256 = hashlib.sha256(sanitized).hexdigest()
    saved = entry.get("upload_intent")
    manifest = saved.get("manifest") if isinstance(saved, dict) else None
    if (not isinstance(manifest, dict)
            or saved.get("id") != upload_intent_id(manifest)
            or any(manifest.get(k) != v for k, v in (
                ("version", UPLOAD_INTENT_VERSION),
                ("assignment_id", entry["assignment_id"]),
                ("session_id", entry.get("runner_session_id")),
                ("owner_epoch", entry["owner_epoch"]),
                ("outcome", "completed"),
            ))):
        raise ValueError("original content-bound upload identity is invalid")
    components = manifest.get("components")
    patch = components.get("model.patch") if isinstance(components, dict) else None
    if (not isinstance(patch, dict) or patch.get("present") is not True
            or (patch.get("sha256"), patch.get("size")) not in {
                (expected_sha256, len(source)),
                (sanitized_sha256, len(sanitized)),
            }):
        raise ValueError("saved intent is not bound to the reviewed patch")
    return {
        "source_patch_sha256": expected_sha256,
        "sanitized_patch_sha256": sanitized_sha256,
        "redacted_labels": labels,
    }


def verify_prepared_payload(entry: dict, manifest: dict, prior_manifest: dict) -> None:
    """Bind every component to the old intent, allowing only derived scrub.

    prior_manifest is built from the same verified snapshot and usage inputs,
    using the display scrubber before this password coverage change. The
    current manifest uses the stronger scrubber and reviewed added-line patch.
    """
    saved = entry.get("upload_intent")
    review = entry.get("secret_guard_review")
    original = review.get("original_upload_intent") if isinstance(review, dict) else saved
    if (not isinstance(original, dict)
            or original.get("id") != upload_intent_id(prior_manifest)
            or original.get("manifest") != prior_manifest
            or not isinstance(saved, dict)
            or saved not in (original, {"id": upload_intent_id(manifest), "manifest": manifest})):
        raise ValueError("reviewed result components differ from the original upload intent")
