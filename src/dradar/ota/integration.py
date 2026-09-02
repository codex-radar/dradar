"""User-facing OTA diagnostics and run-loop safe-point integration."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from .. import __version__
from ..flight_recorder import FlightRecorder
from ..local_config import HOME
from .manifest import (
    CompatibilitySnapshot,
    ManifestError,
    PlatformTarget,
    RolloutContext,
    verify_signed_manifest,
)
from .runtime import UpdateRuntime
from .state import InvalidTransition, SafePointSnapshot, UpdateController, UpdateState

OTA_ROOT_NAME = "ota"
TRUSTED_KEYS_FILE = "trusted-keys.json"
COMPATIBILITY = CompatibilitySnapshot(
    launcher_version="1.0.0",
    runner_protocol=3,
    doctor_contract=1,
    provider_contract=1,
    ledger_schema=1,
    checkpoint_schema=0,
)


def ota_root(home: Path = HOME) -> Path:
    return home / OTA_ROOT_NAME


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_trusted_keys(home: Path = HOME) -> dict[str, bytes]:
    document = _read_json(ota_root(home) / TRUSTED_KEYS_FILE)
    if not document or document.get("schema_version") != 1:
        return {}
    raw = document.get("keys")
    if not isinstance(raw, dict):
        return {}
    keys: dict[str, bytes] = {}
    try:
        for key_id, value in raw.items():
            if not isinstance(key_id, str) or not isinstance(value, str):
                return {}
            decoded = base64.b64decode(value, validate=True)
            if len(decoded) != 32:
                return {}
            keys[key_id] = decoded
    except (ValueError, TypeError):
        return {}
    return keys


def _keys_from_args(values: list[str]) -> dict[str, bytes]:
    keys: dict[str, bytes] = {}
    for value in values:
        key_id, separator, raw_path = value.partition("=")
        path = Path(raw_path).expanduser()
        if not separator or not key_id or path.is_symlink():
            raise ValueError("--trusted-key must be KEY_ID=/safe/path")
        data = path.read_bytes()
        if len(data) != 32:
            raise ValueError(
                "trusted Ed25519 public keys must contain exactly 32 bytes"
            )
        keys[key_id] = data
    if not keys:
        raise ValueError("at least one --trusted-key is required")
    return keys


def store_trusted_keys(keys: dict[str, bytes], home: Path = HOME) -> None:
    _atomic_json(
        ota_root(home) / TRUSTED_KEYS_FILE,
        {
            "schema_version": 1,
            "keys": {
                key_id: base64.b64encode(value).decode("ascii")
                for key_id, value in sorted(keys.items())
            },
        },
    )


def pending_upload_count(home: Path = HOME) -> int:
    """Count durable uploads without mutating or replaying the ledger."""

    try:
        path = home / "pending_uploads.json"
        if not path.is_file() or path.is_symlink():
            return 0
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 1
    return len(entries) if isinstance(entries, list) else 1


def runloop_safe_point(
    *,
    home: Path = HOME,
    active_assignments: int = 0,
    checkouts_inflight: int = 0,
    uploads_inflight: int = 0,
    ledger_writes_inflight: int = 0,
    checkpoint_writes_inflight: int = 0,
    refill_accepting_new: bool = False,
    worker_supervisor_idle: bool = True,
) -> SafePointSnapshot:
    """Build the conservative barrier shared by serial and >=40-worker runs."""

    return SafePointSnapshot(
        active_assignments=active_assignments,
        checkouts_inflight=checkouts_inflight,
        uploads_inflight=uploads_inflight,
        durable_uploads_pending=pending_upload_count(home),
        ledger_writes_inflight=ledger_writes_inflight,
        checkpoint_writes_inflight=checkpoint_writes_inflight,
        refill_accepting_new=refill_accepting_new,
        worker_supervisor_idle=worker_supervisor_idle,
    )


def update_status(home: Path = HOME) -> dict[str, Any]:
    """Return a stable, non-secret status document for CLI and doctor."""

    root = ota_root(home)
    record = _read_json(root / "update-state.json")
    current = _read_json(root / "current.json")
    lkg = _read_json(root / "last-known-good.json")
    result: dict[str, Any] = {
        "root": str(root),
        "target": None,
        "state": "legacy" if record is None else record.get("state", "invalid"),
        "current_version": current.get("version") if current else None,
        "current_sequence": current.get("sequence") if current else None,
        "last_known_good_version": lkg.get("version") if lkg else None,
        "pending": (root / "pending.json").is_file(),
        "lock_present": (root / "update.lock").is_file(),
    }
    try:
        target = PlatformTarget.current()
        result["target"] = f"{target.os}/{target.arch}"
    except ManifestError:
        result["target"] = "unsupported"
    return result


def diagnose_update(home: Path = HOME) -> tuple[bool, tuple[str, ...]]:
    """Diagnose only local OTA state; never fetch, stage, or activate."""

    status = update_status(home)
    notes: list[str] = []
    if status["target"] == "unsupported":
        notes.append("unsupported OS/architecture")
    root = ota_root(home)
    if root.exists() and root.is_symlink():
        notes.append("OTA root is a symlink")
    if os.name != "nt" and root.exists():
        try:
            if root.stat().st_mode & 0o077:
                notes.append("OTA root permissions are broader than 0700")
        except OSError:
            notes.append("OTA root cannot be inspected")
    if status["state"] == "invalid":
        notes.append("persisted OTA state is invalid")
    if status["state"] == "legacy":
        notes.append(
            "legacy client has no signed OTA baseline; current version is preserved"
        )
    return not notes or notes == [
        "legacy client has no signed OTA baseline; current version is preserved"
    ], tuple(notes)


def pending_activation_state(home: Path = HOME) -> UpdateState | None:
    """Inspect a staged candidate without taking the mutation lock."""

    controller = UpdateController(ota_root(home))
    try:
        record = controller.state()
    except InvalidTransition:
        return None
    if not record:
        return None
    try:
        return UpdateState(record["state"])
    except (KeyError, TypeError, ValueError):
        return None


def cmd_update_status(args) -> int:
    status = update_status()
    if getattr(args, "json", False):
        print(json.dumps(status, sort_keys=True))
        return 0
    print(f"OTA target: {status['target']}")
    print(f"OTA state: {status['state']}")
    print(f"current: {status['current_version'] or 'installed legacy version'}")
    print(
        f"sequence: {status['current_sequence'] if status['current_sequence'] is not None else '–'}"
    )
    print(f"last known good: {status['last_known_good_version'] or '–'}")
    print(f"candidate pending: {'yes' if status['pending'] else 'no'}")
    return 0


def cmd_update_doctor(args) -> int:
    del args
    healthy, notes = diagnose_update()
    print(f"OTA diagnostics: {'PASS' if healthy else 'FAIL'}")
    for note in notes:
        print(f"  - {note}")
    return 0 if healthy else 1


def cmd_update_prepare(args) -> int:
    try:
        keys = _keys_from_args(args.trusted_key)
        manifest_path = Path(args.manifest).expanduser()
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("manifest must be a regular local file")
        raw_manifest = manifest_path.read_bytes()
        verify_signed_manifest(raw_manifest, keys)
    except (OSError, ValueError) as exc:
        print(f"update preparation rejected: {exc}", file=sys.stderr)
        return 2
    store_trusted_keys(keys)
    recorder = FlightRecorder(HOME)
    status = update_status()
    current_version = status["current_version"] or __version__
    sequence = status["current_sequence"] or 0
    try:
        with httpx.Client(timeout=60.0) as client:
            runtime = UpdateRuntime(
                ota_root(), recorder=recorder, download_client=client
            )
            decision = runtime.prepare(
                raw_manifest,
                trusted_keys=keys,
                current_version=current_version,
                committed_sequence=sequence,
                compatibility=COMPATIBILITY,
                rollout=RolloutContext(
                    subject=recorder.client_id,
                    ring=args.ring,
                    channel=args.channel,
                ),
            )
    except Exception as exc:  # noqa: BLE001 - CLI renders a bounded failure
        recorder.flush()
        print(
            f"update preparation failed closed: {type(exc).__name__}", file=sys.stderr
        )
        return 1
    recorder.flush()
    if not decision.eligible:
        print(f"update not prepared: {decision.reason}")
        return 0
    print("signed update verified and staged; activation waits for a safe point")
    return 0


def _self_test(candidate) -> bool:
    fd = candidate.duplicate_fd()
    try:
        if os.name != "nt":
            result = subprocess.run(
                [sys.executable, f"/dev/fd/{fd}", "--version"],
                pass_fds=(fd,),
                capture_output=True,
                timeout=30,
                check=False,
            )
        else:  # pragma: no cover - exercised on Windows CI
            with tempfile.NamedTemporaryFile(suffix=".pyz", delete=False) as handle:
                handle.write(candidate.read_bytes())
                temporary = Path(handle.name)
            try:
                result = subprocess.run(
                    [sys.executable, str(temporary), "--version"],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            finally:
                temporary.unlink(missing_ok=True)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        os.close(fd)


def activate_prepared_update(
    snapshot: SafePointSnapshot, *, home: Path = HOME
) -> UpdateState | None:
    """Single-flight activation; callers invoke only at natural idle edges."""

    if not snapshot.ready:
        return None
    keys = load_trusted_keys(home)
    if not keys or pending_activation_state(home) is not UpdateState.WAITING_SAFE_POINT:
        return None
    recorder = FlightRecorder(home)
    with httpx.Client(timeout=60.0) as client:
        runtime = UpdateRuntime(
            ota_root(home), recorder=recorder, download_client=client
        )
        runtime.controller.set_trusted_keys(keys)
        result = runtime.activate_and_self_test(snapshot, _self_test)
    recorder.flush()
    return result
