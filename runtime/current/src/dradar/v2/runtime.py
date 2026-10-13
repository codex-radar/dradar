"""Narrow Codex/Pier adapter; v2 start happens at the actual worker gate.

No paid calls occur in tests: run_trial is injectable. Existing task/version,
credential, nonce, artifact-boundary and physical cleanup checks are retained.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from .journal import Journal
from .results import Completion
from .client import ProtocolError
from ..manifest import task_content_hash
TASK_BUNDLE_MARKER = ".dradar-task-bundle.json"

class RuntimeUnavailable(RuntimeError):
    pass

def normalize_assignment(a: dict) -> dict:
    task, runner = a["task"], a.get("runner")
    from ..harness_policy import reject_retired_combination
    reject_retired_combination((runner or {}).get("agent","codex"),task.get("model"),(runner or {}).get("provider"))
    keys = {"agent", "agent_version", "agent_version_verified", "auth_runtime", "provider", "billing_mode", "est_minutes"}
    if not isinstance(runner, dict) or not keys <= runner.keys() or runner["agent"] != "codex" or type(runner["agent_version_verified"]) is not bool or not isinstance(runner["agent_version"], str):
        raise RuntimeUnavailable("verified Codex runner descriptor required")
    task_id = task["task_id"]
    if not isinstance(task_id, str) or Path(task_id).name != task_id or task_id in {".", ".."}:
        raise RuntimeUnavailable("task identifier is not supported by the existing runner")
    if not re.fullmatch(r"[a-f0-9]{32}", a["assignment_id"]):
        raise RuntimeUnavailable("existing runner requires a 32-hex assignment identity")
    bundle = task.get("task_bundle")
    if bundle is not None and (not isinstance(bundle, dict) or set(bundle) != {"url", "sha256", "bytes", "format"} or bundle["format"] != "tar.gz" or type(bundle["bytes"]) is not int or bundle["bytes"] <= 0):
        raise RuntimeUnavailable("existing task-bundle descriptor required")
    return {**runner, "assignment_id": a["assignment_id"], "owner_epoch": a["owner_epoch"],
            "task_id": task_id, "benchmark_id": task["benchmark"], "model": task["model"],
            "effort": task["effort"], "task_content_hash": task["task_content_hash"],
            "deep_swe_commit": task.get("task_commit")}

class TaskNotReady(RuntimeUnavailable):
    """Selected task fails before any controller/paid execution is created."""
    def __init__(self,code):
        if code not in {'runtime_binding_missing','task_inputs_unavailable','collector_unavailable','image_unavailable'}:
            raise ValueError('fixed task readiness reason required')
        self.code=code
        super().__init__('selected task not ready: '+code)

class CodexRuntime:
    def __init__(self, journal: Journal, tasks_root: Path, *, run_trial=None, managed_auth_config: Path | None = None, build_cache_mode="shared", public_image_options=None):
        self.journal, self.tasks_root = journal, Path(tasks_root)
        self._run_trial = run_trial
        self.managed_auth_config = managed_auth_config
        self.build_cache_mode = build_cache_mode
        self.public_image_options = public_image_options

    def prepare(self, a: dict, *, tasks_root=None, bundle_root=None) -> dict:
        root = Path(tasks_root) if tasks_root is not None else self.tasks_root
        marker_root = Path(bundle_root) if bundle_root is not None else root
        normalized = normalize_assignment(a)
        if normalized["auth_runtime"] is not None and self.managed_auth_config is None:
            raise RuntimeUnavailable("explicit configured authentication runtime required; no silent credential switch")
        if not root.is_dir() or root.is_symlink():
            raise RuntimeUnavailable("verified local task package is required")
        task = root / normalized["task_id"]
        if task.is_symlink() or not (task / "instruction.md").is_file() or not (task / "task.toml").is_file():
            raise RuntimeUnavailable("task package is incomplete")
        if any(p.is_symlink() for p in task.rglob("*")):
            raise RuntimeUnavailable("unvalidated task symlink")
        if task_content_hash(root, normalized["task_id"]) != normalized["task_content_hash"]:
            raise RuntimeUnavailable("immutable task content hash mismatch")
        bundle = a["task"].get("task_bundle")
        if bundle is not None:
            MARKER = TASK_BUNDLE_MARKER
            try:
                marker_path = marker_root / MARKER
                if marker_path.is_symlink() or not marker_path.is_file():
                    raise RuntimeUnavailable('regular verified archive task-pack marker required')
                marker = json.loads(marker_path.read_text())
            except (OSError, ValueError) as exc:
                raise RuntimeUnavailable("verified archive task-pack marker required") from exc
            if marker.get("sha256") != bundle["sha256"] or marker.get("benchmark_id") != normalized["benchmark_id"]:
                raise RuntimeUnavailable("archive task-pack digest mismatch")
        elif self._run_trial is None:
            from .task_git import local_deep_swe_commit
            if local_deep_swe_commit(root) != normalized["deep_swe_commit"]:
                raise RuntimeUnavailable("immutable Git task commit mismatch; update the isolated task cache")
        return normalized

