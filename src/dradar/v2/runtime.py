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
            from ..taskpacks import MARKER
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
            from ..runner import local_deep_swe_commit
            if local_deep_swe_commit(root) != normalized["deep_swe_commit"]:
                raise RuntimeUnavailable("immutable Git task commit mismatch; update the isolated task cache")
        return normalized

    def execute_with_barrier(self, prepared: dict, execution_id: str, barrier, launch_guard) -> Completion:
        from .. import runner
        assignment = {**prepared, "_runner_session_id": execution_id, "_runner_attempt": 1}
        aid = assignment["assignment_id"]
        evidence = []
        started = None
        def stop_requested():
            return self.journal.value("local_interrupt") == "true"
        authorized = False
        image = None
        if self.public_image_options is not None:
            from .runtime_cache import PublicImagePreparation
            image = PublicImagePreparation(self.journal, **self.public_image_options)
        def observe(event):
            self.journal.record_audit(aid, execution_id, event)
            evidence.append(event)
        def ready(event):
            nonlocal started, authorized
            if authorized:
                raise ProtocolError("worker attempted to open the paid execution gate twice")
            if image is not None:
                image.verify()
            barrier()
            authorized = True
            started = time.monotonic()
        work = self.journal.root / "runtime" / aid / "work"
        work.mkdir(parents=True, mode=0o700, exist_ok=True)
        run_trial = self._run_trial or runner.run_trial
        try:
            art = run_trial(assignment, self.tasks_root, work,
                            on_worker_registered=ready, execution_observer=observe,
                            provider_launch_guard=launch_guard,
                            managed_auth_config=self.managed_auth_config,
                            build_cache_mode=self.build_cache_mode,
                            execution_stop_requested=stop_requested,
                            **({"public_image_preparer": image} if image is not None else {}))
        except Exception:
            # A post-start failure can be retained only with exact exit proof.
            if not authorized or not any(e.get("event") == "confirmed_absent" for e in evidence):
                raise
            return Completion("interrupted" if stop_requested() else "failed", True, completed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                              elapsed_ms=int((time.monotonic() - started) * 1000),
                              failure={"code": "runner_failed", "message": "执行失败，原运行目录与退出证据已保留"})
        if not authorized or not any(e.get("event") == "confirmed_absent" for e in evidence):
            raise RuntimeUnavailable("runtime exit is not independently confirmed; keep slot unresolved")
        if image is not None:
            try:
                image.release_after_exit(confirmed_absent=True)
            except Exception:
                pass  # Retain unknown cache references; never weaken execution evidence.
        # Existing runner validates finalized private output; copy through its
        # safe reader, never follow agent-controlled result paths directly.
        from ..artifact_boundary import read_trial_file
        collected = self.journal.root / "runtime" / aid / "collected"
        collected.mkdir(mode=0o700, exist_ok=True)
        files = {}
        for name, path in (("patch", art.patch), ("trajectory", art.trajectory), ("runner_result", art.result)):
            if path is not None:
                data = read_trial_file(art.trial_dir, Path(path).relative_to(art.trial_dir))
                target = collected / name
                with target.open("xb") as output:
                    output.write(data)
                files[name] = target
        bundle = runner.build_codex_trajectory_bundle(art.trial_dir)
        tokens = {"input": None, "output": None, "total": None, "source": None, "missing_reason": "codex_usage_unavailable"}
        if bundle is not None:
            target = collected / "trajectory_bundle"
            target.write_text(json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
            files["trajectory_bundle"] = target
            if bundle.get("complete") is True:
                usage = bundle["aggregate_usage"]
                inp, out = usage["n_input_tokens"], usage["n_output_tokens"]
                if type(inp) is int and type(out) is int and inp >= 0 and out >= 0:
                    tokens = {"input": inp, "output": out, "total": inp + out, "source": "codex_session_usage", "missing_reason": None}
        successful = art.returncode == 0 and not runner._result_exception_text(art.result).strip()
        return Completion("interrupted" if stop_requested() else "completed" if successful else "failed", True, files,
                          completed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                          elapsed_ms=int((time.monotonic() - started) * 1000), tokens=tokens,
                          failure=None if successful else {"code": "runner_failed", "message": "Codex运行未成功，成果已保留"})
