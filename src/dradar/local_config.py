"""Shared client-side state: the local config file and constants used across
the CLI's identity/doctor/run-loop modules. Deliberately dependency-free (no
imports from sibling dradar.* modules) so every other client module can
import from here without risking a cycle.
"""

import json
import os
import sys
from pathlib import Path

HOME = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
CONFIG_PATH = HOME / "config.json"


DEFAULT_BENCHMARK = "deep-swe"


def default_tasks_root(benchmark_id: str = DEFAULT_BENCHMARK) -> Path:
    """Hidden default checkout used when the volunteer did not choose one.

    Derive it at call time rather than import time so DRADAR_HOME overrides
    and tests that isolate HOME continue to affect every caller consistently.
    """
    if benchmark_id == DEFAULT_BENCHMARK:
        return HOME / "deep-swe" / "tasks"
    return HOME / "benchmarks" / benchmark_id / "tasks"


def tasks_root_from_config(
    cfg: dict, benchmark_id: str | None = None,
) -> Path:
    """Preserve an explicit/legacy checkout, otherwise use the hidden one."""
    selected = benchmark_id or cfg.get("benchmark") or DEFAULT_BENCHMARK
    configured = (cfg.get("tasks_roots") or {}).get(selected)
    if configured is None and selected == DEFAULT_BENCHMARK:
        configured = cfg.get("tasks_root")
    return (Path(configured).expanduser() if configured
            else default_tasks_root(selected))


def _load_config(fresh_on_corrupt: bool = False) -> dict:
    if CONFIG_PATH.is_file():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            # fresh_on_corrupt: `dradar login` is about to rewrite the file
            # anyway, so it must not die on a corrupt one — it IS the
            # recovery path the error below recommends.
            if fresh_on_corrupt:
                print(f"config at {CONFIG_PATH} was corrupt — starting fresh "
                      "(login will rewrite it)")
                return {}
            # every other command loads the config first; a raw traceback
            # here tells the volunteer nothing about how to get unstuck.
            sys.exit(
                f"config at {CONFIG_PATH} is corrupt — run `dradar login "
                "--github` to recover a linked identity (it rewrites the "
                "config), or grab a fresh token on the radar page and paste "
                "its login command"
            )
    return {}


def _save_config(cfg: dict) -> None:
    # Mirror pending._save: write-to-temp + atomic os.replace, so a kill
    # mid-write can never truncate the volunteer's ONLY copy of their token.
    # The temp file is created 0600 BEFORE any bytes land (the old
    # write-then-chmod left a brief world-readable window on the token).
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(cfg, indent=2) + "\n")
    os.chmod(tmp, 0o600)  # O_CREAT's mode is ignored when tmp pre-existed
    os.replace(tmp, CONFIG_PATH)


def runtime_config(credentials_file: str | os.PathLike[str] | None = None) -> dict:
    """Load ordinary config or overlay one private run-plan credential.

    The credentials-file path is safe to forward to supervised subprocesses;
    its ``drp_`` value is not.  Reject symlinks and permissive modes before
    reading so an internal argument cannot be turned into an arbitrary-file
    credential source.
    """
    if credentials_file is None:
        return _load_config()
    # A plan-scoped capability is deliberately sufficient on a fresh device.
    # A damaged unrelated long-term config must not force the user to copy a
    # drt_ credential before this exact plan can run. Preserve valid local task
    # paths when available, but fail independently from that optional file.
    try:
        cfg = _load_config()
    except SystemExit:
        cfg = {}
    path = Path(credentials_file).expanduser()
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError
        stat = path.stat()
        if os.name != "nt":
            if stat.st_mode & 0o077:
                raise ValueError
            if hasattr(os, "getuid") and stat.st_uid != os.getuid():
                raise ValueError
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("invalid private run-plan credentials file") from None
    if not isinstance(payload, dict) or payload.get("credential_kind") != "run_plan_v1":
        raise ValueError("invalid private run-plan credentials file")
    server = payload.get("server")
    token = payload.get("token")
    benchmark = payload.get("benchmark")
    batch_id = payload.get("batch_id")
    logical_session_id = payload.get("logical_session_id")
    plan = payload.get("plan")
    points_tier = plan.get("points_tier") if isinstance(plan, dict) else None
    if (
        not isinstance(server, str) or not server
        or not isinstance(token, str) or not token.startswith("drp_")
        or not isinstance(benchmark, str) or not benchmark
        or not isinstance(batch_id, str) or not batch_id
        or not isinstance(logical_session_id, str)
        or not logical_session_id.startswith("drl_")
        or points_tier not in {"plus", "pro-5x", "pro-20x"}
    ):
        raise ValueError("invalid private run-plan credentials file")
    generation = payload.get("credential_generation")
    if generation is not None and (type(generation) is not int or generation < 0):
        raise ValueError("invalid private run-plan credential generation")
    runtime = dict(cfg)
    runtime.update({
        "server": server,
        "token": token,
        "benchmark": benchmark,
        "run_plan_batch_id": batch_id,
        "run_plan_id": payload.get("plan_id"),
        "run_plan_credential_generation": generation,
        "run_plan_logical_session_id": logical_session_id,
        "run_plan_points_tier": points_tier,
    })
    return runtime
