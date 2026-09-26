"""Local stop intent independent of long run admission and network requests.

The stop marker is never cleared. An explicit run captures its current digest;
an automatic recheck may only reuse that same intent. The final Fleet spawn
and stop-marker publication share a short per-batch lifecycle lock.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import uuid

from .api_client import normalize_batch_id

BATCH_ENV = "DRADAR_RUN_INTENT_BATCH"
GENERATION_ENV = "DRADAR_RUN_INTENT_GENERATION"


class IntentStopped(RuntimeError):
    pass


def _paths(home: Path, batch: str):
    batch = normalize_batch_id(batch)
    if batch is None:
        raise IntentStopped("an exact batch is required for local run intent")
    root = home / "run-plans" / "intents"
    return root / f"{batch}.json", root / f"{batch}.stop", root / f"{batch}.lock"


def _stop_digest(path: Path) -> str:
    try:
        if path.is_symlink():
            raise IntentStopped("local stop record is not a regular file")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise IntentStopped("local stop record cannot be verified") from exc


def _read(path: Path) -> dict:
    try:
        if path.is_symlink():
            raise ValueError("symbolic link")
        value = json.loads(path.read_text())
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1
                or not isinstance(value.get("generation"), str)
                or len(value["generation"]) != 32
                or any(c not in "0123456789abcdef" for c in value["generation"])
                or not isinstance(value.get("stop_digest"), str)
                or (value["stop_digest"] != "absent" and (
                    len(value["stop_digest"]) != 64
                    or any(c not in "0123456789abcdef" for c in value["stop_digest"])
                ))):
            raise ValueError("unknown state")
        return value
    except (OSError, UnicodeError, ValueError) as exc:
        raise IntentStopped("local run intent cannot be verified; preserve it for review") from exc


def begin(home: Path, batch: str) -> str:
    """Only an explicit run can authorize a new local lifecycle."""
    from .run_plans import _atomic_json, _exclusive_lock
    path, stopped, lock = _paths(home, batch)
    with _exclusive_lock(lock):
        if path.exists() or path.is_symlink():
            existing = _read(path)  # Do not replace damaged intent as empty.
            if existing["stop_digest"] == _stop_digest(stopped):
                return existing["generation"]
        generation = uuid.uuid4().hex
        _atomic_json(path, {"schema_version": 1, "generation": generation,
                            "stop_digest": _stop_digest(stopped)})
        return generation


def current(home: Path, batch: str) -> str:
    path, stopped, _lock = _paths(home, batch)
    state = _read(path)
    if state["stop_digest"] != _stop_digest(stopped):
        raise IntentStopped("a newer stop cancelled this run; use an explicit run to resume")
    return state["generation"]


@contextmanager
def launch_guard(home: Path, batch: str, generation: str | None):
    from .run_plans import _exclusive_lock
    path, stopped, lock = _paths(home, batch)
    with _exclusive_lock(lock):
        # Compatible local account/Fleet callers without a plan intent are
        # unchanged. Once a scope has an intent, old requests cannot omit it.
        if generation is None and not path.exists() and not stopped.exists():
            yield
            return
        require(home, batch, generation)
        yield


def require(home: Path, batch: str, generation: str | None) -> None:
    if not generation or current(home, batch) != generation:
        raise IntentStopped("a newer run or stop cancelled this launch")


def stop(home: Path, batch: str) -> str | None:
    """Persist reduction and publish local drain without waiting for the API."""
    from .run_plans import _atomic_json, _exclusive_lock
    from .fleet import _request_pool_drain
    _path, stopped, lock = _paths(home, batch)
    with _exclusive_lock(lock):
        _atomic_json(stopped, {"schema_version": 1, "stop_id": uuid.uuid4().hex})
        return _request_pool_drain(home, batch, "this device was asked to stop")


def require_worker(home: Path) -> None:
    batch = os.environ.get(BATCH_ENV)
    generation = os.environ.get(GENERATION_ENV)
    if batch or generation:
        require(home, batch, generation)
