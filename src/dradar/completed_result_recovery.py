"""Explicit completed-result recovery for one exited cleanup quarantine.

Inspection is read-only. Execution preserves the original fence until the
server grants one exact content-bound upload and acknowledges submission.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

from . import assignment_boundary, cleanup_recovery, pending
from .artifact_boundary import UnsafeArtifact, read_trial_file
from .identity import _client
from .local_config import HOME, _load_config
from .ota.discovery import TRUSTED_KEYS
from .ota.integration import ota_root
from .ota.state import UpdateController
from .providers import (
    HONEY_CHILD_AGENT_ACCESS, HONEY_EXECUTION_SECURITY_PROFILE,
    HONEY_INNER_PERMISSION_MODE, HONEY_OUTER_ISOLATION,
    ZCODE_RUN_CONFIG_VERSION, ZCODE_RUNTIME_PROFILE,
)
from .runner import trial_artifact_paths


class CompletedResultRecoveryBlocked(RuntimeError):
    pass


# Verified against the original signed 0.5.281 source at 7c6c47805ef4.
# A future runner profile change must not be retroactively attributed to it.
_ORIGINAL_ZCODE_PROFILE = {
    "model_config_version": "zcode-protocol-glm-5.3-family-full-container-v3",
    "model_runtime_profile": "pier-zcode-glm-5.3-family-api-key-full-container-v3",
    "honey_execution_security_profile": "full-container-tools-outer-boundary-v1",
    "honey_inner_permission_mode": "full-auto-approve",
    "honey_child_agent_access": "native-enabled",
    "honey_outer_isolation": "pier-docker-exact-egress-minimal-credentials-v1",
}


def inspect(
    *, assignment_id: str, benchmark: str, batch_id: str,
    session_id: str, home: Path = HOME,
) -> dict:
    """Bind one finished trial and its bytes to the exact exited owner.

    This is deliberately stricter than ordinary retry-upload. It applies
    only to a quarantine with an already released original session, and it
    leaves that quarantine untouched even when all checks pass.
    """
    exit_proof = cleanup_recovery.inspect(
        assignment_id=assignment_id, benchmark=benchmark, batch_id=batch_id,
        session_id=session_id, home=home, allow_result=True,
    )
    if exit_proof["status"] != "ready":
        raise CompletedResultRecoveryBlocked("original exit is not ready for review")
    row = cleanup_recovery._exact_row(home, assignment_id)
    if row.get("upload_blocked") != "cleanup_unconfirmed":
        raise CompletedResultRecoveryBlocked("original cleanup quarantine is no longer uploadable")
    job = Path(row["job_dir"])
    trials = [path for path in job.iterdir()
              if path.is_dir() and "__" in path.name]
    if len(trials) != 1 or not trials[0].name.startswith(row["task_id"] + "__"):
        raise CompletedResultRecoveryBlocked("original finished trial is ambiguous")
    trial = trials[0]
    try:
        patch_path, trajectory_path, result_path = trial_artifact_paths(trial)
        if trajectory_path is None or result_path is None:
            raise CompletedResultRecoveryBlocked("completed patch, trajectory and result are required")
        artifact_paths = {
            "patch": patch_path, "trajectory": trajectory_path,
            "result": result_path,
        }
        artifact_bytes = {
            name: read_trial_file(trial, path.relative_to(trial))
            for name, path in artifact_paths.items()
        }
        if any(not data for data in artifact_bytes.values()):
            raise CompletedResultRecoveryBlocked("completed artifact is empty")
        result = json.loads(artifact_bytes["result"])
    except (UnsafeArtifact, OSError, ValueError, UnicodeError,
            json.JSONDecodeError) as exc:
        raise CompletedResultRecoveryBlocked("original artifact cannot be verified") from exc
    if (not isinstance(result, dict)
            or result.get("task_id") != row["task_id"]
            or not isinstance(result.get("finished_at"), str)
            or not result["finished_at"]
            or not isinstance(result.get("agent_execution"), dict)
            or not isinstance(result["agent_execution"].get("finished_at"), str)
            or not result["agent_execution"]["finished_at"]
            or result.get("exception_info")
            or not isinstance(result.get("agent_result"), dict)):
        raise CompletedResultRecoveryBlocked("original trial does not prove a completed agent result")
    meta = _original_meta(home, benchmark, batch_id, assignment_id, trial)
    return {
        **exit_proof,
        "result_status": "completed_local_result",
        "artifact_sha256": {
            name: hashlib.sha256(data).hexdigest()
            for name, data in artifact_bytes.items()
        },
        "artifact_bytes": {
            name: len(data) for name, data in artifact_bytes.items()
        },
        "trial_name": trial.name,
        "source_client_version": meta["dradar_version"],
        "source_agent_version": meta["zcode_cli_version"],
    }


def _source_version(home: Path) -> str:
    """Use the original installation's verified committed release pointer."""
    return UpdateController(ota_root(home), trusted_keys=TRUSTED_KEYS).committed_pointer().version


def _original_meta(home: Path, benchmark: str, batch_id: str,
                   assignment_id: str, trial: Path) -> dict:
    """Rebuild only facts proved by the original signed ZCode run and result."""
    state, _ = assignment_boundary.inspect_snapshot(
        assignment_boundary.state_path(home, benchmark, batch_id))
    expected = state["expected"][assignment_id]
    if (benchmark != "deep-swe" or expected.get("model") != "glm-5.3"
            or expected.get("effort") != "high"):
        raise CompletedResultRecoveryBlocked("this recovery supports only the verified ZCode GLM-5.3 high trial")
    try:
        raw = read_trial_file(trial, Path("result.json"))
        result = json.loads(raw)
        agent_info = result["agent_info"]
        agent_cfg = result["config"]["agent"]
        kwargs = agent_cfg["kwargs"]
        version = agent_info["version"]
        model_info = agent_info.get("model_info")
        if (agent_info.get("name") != "zcode"
                or not re.fullmatch(r"\d+\.\d+\.\d+", version)
                or not isinstance(model_info, dict)
                or model_info.get("name") != expected["model"]
                or agent_cfg.get("import_path") != "_dradar_pier_zcode:ZCodeBigModel"
                or agent_cfg.get("model_name") != expected["model"]
                or kwargs.get("reasoning_effort") != expected["effort"]
                or kwargs.get("version") != version
                or not kwargs.get("api_key_file")):
            raise CompletedResultRecoveryBlocked("original ZCode agent identity is unverified")
    except (KeyError, TypeError, ValueError, OSError, UnsafeArtifact,
            UnicodeError, json.JSONDecodeError) as exc:
        raise CompletedResultRecoveryBlocked("original ZCode agent identity is unverified") from exc
    source_version = _source_version(home)
    if source_version != "0.5.281":
        raise CompletedResultRecoveryBlocked("original signed CLI version is unverified")
    running_profile = {
        "model_config_version": ZCODE_RUN_CONFIG_VERSION,
        "model_runtime_profile": ZCODE_RUNTIME_PROFILE,
        "honey_execution_security_profile": HONEY_EXECUTION_SECURITY_PROFILE,
        "honey_inner_permission_mode": HONEY_INNER_PERMISSION_MODE,
        "honey_child_agent_access": HONEY_CHILD_AGENT_ACCESS,
        "honey_outer_isolation": HONEY_OUTER_ISOLATION,
    }
    if running_profile != _ORIGINAL_ZCODE_PROFILE:
        raise CompletedResultRecoveryBlocked("original and recovery runtime profiles differ")
    agent_result = result["agent_result"]
    return {
        "dradar_version": source_version,
        "zcode_cli_version": version,
        **_ORIGINAL_ZCODE_PROFILE,
        "coding_plan_api_key": True,
        "zcode_protocol_version": 1,
        "zcode_native_efforts": ["low", "high", "max"],
        # The original provider sidecar reports incomplete usage. The common
        # uploader may fill these only from a verified complete sidecar.
        "cost_usd": None,
        "n_input_tokens": None,
        "n_cache_tokens": None,
        "n_output_tokens": None,
        "n_agent_steps": agent_result.get("n_agent_steps"),
        "exception_info": False,
    }


def execute(*, assignment_id: str, benchmark: str, batch_id: str,
            session_id: str, inventory_sha256: str, home: Path = HOME) -> dict:
    """Persist one exact request, then let the ordinary scrubbed uploader send it."""
    if not re.fullmatch(r"[0-9a-f]{64}", inventory_sha256):
        raise CompletedResultRecoveryBlocked("exact preflight inventory digest is required")
    proof = inspect(assignment_id=assignment_id, benchmark=benchmark,
                    batch_id=batch_id, session_id=session_id, home=home)
    if proof["inventory_sha256"] != inventory_sha256:
        raise CompletedResultRecoveryBlocked("original job changed since preflight")
    from . import runloop
    if home != runloop.HOME:
        raise CompletedResultRecoveryBlocked("recovery home differs from the running CLI")
    original = cleanup_recovery._exact_row(home, assignment_id)
    trial = Path(original["job_dir"]) / proof["trial_name"]
    meta = _original_meta(home, benchmark, batch_id, assignment_id, trial)
    client = _client({**_load_config(), "benchmark": benchmark})
    client.set_batch_id(batch_id)
    saved = original.get("completed_result_recovery")
    binding = {
        "inventory_sha256": inventory_sha256,
        "artifact_sha256": proof["artifact_sha256"],
        "trial_name": proof["trial_name"],
        "release_evidence_id": proof["release_evidence_id"],
        "release_evidence_sha256": proof["release_evidence_sha256"],
        "source_client_version": meta["dradar_version"],
        "source_agent_version": meta["zcode_cli_version"],
    }
    if saved is None:
        view = client.get_assignment()
        active = view.get("active")
        if not isinstance(active, list):
            raise CompletedResultRecoveryBlocked("current assignment inventory is unavailable")
        matches = [item for item in active if isinstance(item, dict)
                   and item.get("assignment_id") == assignment_id]
        if len(matches) != 1:
            raise CompletedResultRecoveryBlocked("original assignment is no longer uniquely leased")
        current = matches[0]
        if (current.get("batch_id") != batch_id
                or current.get("nonce") != original["nonce"]
                or current.get("task_id") != original["task_id"]
                or current.get("model") != "glm-5.3"
                or current.get("effort") != "high"
                or type(current.get("owner_epoch")) is not int):
            raise CompletedResultRecoveryBlocked("current assignment identity differs")
        epoch = current["owner_epoch"]
        if (epoch == original["owner_epoch"]
                and current.get("started_at") is not None
                and current.get("execution_state") in {"running", "paused"}):
            mode = "source"
            upload_session = session_id
            upload_epoch = epoch
        elif (epoch == original["owner_epoch"] + 1
              and current.get("execution_state") == "waiting"
              and current.get("runner_state") == "waiting"
              and current.get("started_at") is None):
            mode = "salvage"
            upload_session = "salvage-" + uuid.uuid4().hex
            upload_epoch = epoch + 1
        else:
            raise CompletedResultRecoveryBlocked("a later owner or live runner prevents recovery")
        saved = {**binding, "request_id": uuid.uuid4().hex,
                 "mode": mode, "upload_session_id": upload_session,
                 "upload_owner_epoch": upload_epoch,
                 "expected_owner_epoch": epoch}
    # The common uploader may create a byte-identical staged patch after the
    # first attempt, changing the directory inventory. Fresh preflight binds
    # that inventory; the saved artifact hashes and v3 payload intent must
    # still match before a retry can contact the server.
    elif (not isinstance(saved, dict) or
          any(saved.get(key) != value for key, value in binding.items()
              if key != "inventory_sha256") or
          not re.fullmatch(r"[0-9a-f]{32}", saved.get("request_id", "")) or
          saved.get("mode") not in {"source", "salvage"} or
          not isinstance(saved.get("upload_session_id"), str) or
          type(saved.get("upload_owner_epoch")) is not int or
          type(saved.get("expected_owner_epoch")) is not int or
          (saved["mode"] == "source" and (
              saved["upload_session_id"] != session_id or
              saved["upload_owner_epoch"] != original["owner_epoch"] or
              saved["expected_owner_epoch"] != original["owner_epoch"])) or
          (saved["mode"] == "salvage" and (
              not re.fullmatch(r"salvage-[0-9a-f]{32}", saved["upload_session_id"]) or
              saved["expected_owner_epoch"] != original["owner_epoch"] + 1 or
              saved["upload_owner_epoch"] != saved["expected_owner_epoch"] + 1))):
        raise CompletedResultRecoveryBlocked("saved recovery identity changed")
    row = {**original, "completed_result_recovery": saved,
           "trial_dir": str(trial), "outcome": "completed", "meta": meta,
           "omit_trajectory_bundle": True}
    if original.get("upload_intent") is not None:
        row["upload_intent"] = original["upload_intent"]
    pending.replace_exact(home, original, row)
    result = runloop._upload_trial(
        client, row, upload_only_recovery=True, cleanup_result_recovery=True)
    return {"schema_version": 1, "status": result,
            "assignment_id": assignment_id, "batch_id": batch_id,
            "request_id": saved["request_id"], "mutated": True}


def cmd_recover(args) -> int:
    common = {"assignment_id": args.assignment_id, "benchmark": args.benchmark,
              "batch_id": args.batch_id, "session_id": args.runner_session_id}
    try:
        result = (execute(**common, inventory_sha256=args.inventory_sha256)
                  if args.execute else inspect(**common))
    except Exception as exc:
        result = {"schema_version": 1, "status": "blocked", "reason": str(exc),
                  "mutated": None if args.execute else False}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"ready", "submitted"} else 1


__all__ = ["CompletedResultRecoveryBlocked", "inspect", "execute", "cmd_recover"]
