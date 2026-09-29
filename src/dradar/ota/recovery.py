"""Public, signed zipapp entries while normal OTA is blocked.

This is only the second half of the trust chain. The operator first uses an
already trusted CLI's public OTA path in a disposable home to verify and
obtain this package. We verify the signed package again against the real
home's committed anti-rollback baseline before any upload.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from pathlib import Path

from .. import __version__
from ..api_client import normalize_batch_id
from ..local_config import HOME
from .activity import active_invocations
from .discovery import TRUSTED_KEYS
from .download import open_verified_artifact
from .integration import COMPATIBILITY, ota_root
from .manifest import (
    ManifestError, PlatformTarget, RolloutContext, evaluate_manifest,
    verify_signed_manifest,
)
from .state import InvalidTransition, UpdateController, UpdateLock, UpdateState


def _assignment_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise argparse.ArgumentTypeError("assignment ID must be 32 lowercase hex characters")
    return value


def _batch_id(value: str) -> str:
    try:
        normalized = normalize_batch_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if normalized is None:
        raise argparse.ArgumentTypeError("batch ID is required")
    return normalized


def _existing_client_id(home: Path) -> str:
    path = home / "flight-recorder" / "client_id"
    if path.is_symlink() or not path.is_file():
        raise ValueError("existing OTA rollout identity is unavailable")
    value = path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("existing OTA rollout identity is invalid")
    return value


def _verify_package(manifest_path: Path, package_path: Path, home: Path) -> None:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("signed manifest must be a regular local file")
    if manifest_path.stat().st_size > 48 * 1024:
        raise ValueError("signed manifest exceeds the size limit")
    manifest = verify_signed_manifest(manifest_path.read_bytes(), TRUSTED_KEYS)
    root = ota_root(home)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("OTA root is unsafe")
    controller = UpdateController(root, trusted_keys=TRUSTED_KEYS)
    state = controller.state()
    if state and state["state"] in {
        UpdateState.ACTIVATED.value, UpdateState.SELF_TESTING.value,
        UpdateState.ROLLBACK_PENDING.value,
    }:
        raise ValueError("OTA activation or rollback is in progress")
    baseline = controller.committed_pointer()
    decision = evaluate_manifest(
        manifest,
        current_version=baseline.version,
        committed_sequence=baseline.sequence,
        compatibility=COMPATIBILITY,
        rollout=RolloutContext(subject=_existing_client_id(home)),
        target=PlatformTarget.current(),
    )
    if not decision.eligible or decision.artifact is None:
        raise ValueError(f"signed release is ineligible: {decision.reason}")
    if manifest.version != __version__:
        raise ValueError("running package version differs from signed release")
    if package_path.suffix != ".pyz":
        raise ValueError("recovery must run from a signed OTA zipapp")
    with open_verified_artifact(package_path, decision.artifact) as verified:
        # Read the already verified inode, rather than reopening the pathname.
        with zipfile.ZipFile(io.BytesIO(verified.read_bytes())) as bundle:
            metadata = json.loads(bundle.read("dradar/_ota_build.json"))
        if (
            metadata.get("schema_version") != 1
            or metadata.get("version") != manifest.version
            or metadata.get("sequence") != manifest.sequence
            or metadata.get("target") != {
                "os": decision.artifact.target.os,
                "arch": decision.artifact.target.arch,
            }
        ):
            raise ValueError("signed package build identity is inconsistent")
        verified.verify()
        if not verified.binding_is_current():
            raise ValueError("signed package changed during verification")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dradar.pyz recover-upload",
        description="Recover one saved upload from a preverified signed OTA package; never run a model or activate OTA.",
    )
    parser.add_argument("--manifest", required=True, metavar="SIGNED_JSON")
    parser.add_argument("--assignment-id", required=True, type=_assignment_id)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--batch-id", type=_batch_id)
    parser.add_argument("--runner-session-id")
    args = parser.parse_args(argv)
    if not args.benchmark.strip():
        parser.error("--benchmark must not be empty")
    if args.runner_session_id is not None and not args.runner_session_id.strip():
        parser.error("--runner-session-id must not be empty")
    try:
        root = ota_root(HOME)
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ValueError("OTA root is unsafe")
        # The ordinary launcher takes this lock to register every invocation.
        # Holding it through upload excludes an old runner and another recovery.
        with UpdateLock(root / "launch.lock", timeout_seconds=0):
            if active_invocations(root):
                raise ValueError("another DRadar invocation is active")
            _verify_package(Path(args.manifest).expanduser(), Path(sys.argv[0]), HOME)
            from ..runloop import recover_one_pending_upload
            return recover_one_pending_upload(
                assignment_id=args.assignment_id,
                benchmark=args.benchmark,
                batch_id=args.batch_id,
                runner_session_id=args.runner_session_id,
            )
    except (InvalidTransition, ManifestError, OSError, ValueError, RuntimeError,
            KeyError, zipfile.BadZipFile) as exc:
        print(f"upload recovery rejected before upload: {exc}", file=sys.stderr)
        return 2


def main_cleanup(argv: list[str] | None = None) -> int:
    """Use a newer signed bundle to retire one verified cleanup quarantine.

    This entry bypasses only the old CLI's pending-upload activation deadlock.
    It does not install or activate the bundle, and it takes the ordinary
    invocation lock so an in-flight runner cannot race the disposition.
    """
    parser = argparse.ArgumentParser(prog="dradar.pyz recover-cleanup")
    parser.add_argument("--manifest", required=True, metavar="SIGNED_JSON")
    parser.add_argument("--assignment-id", required=True, type=_assignment_id)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--batch-id", required=True, type=_batch_id)
    parser.add_argument("--runner-session-id", required=True, type=_assignment_id)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--inventory-sha256")
    args = parser.parse_args(argv)
    if not args.benchmark.strip():
        parser.error("--benchmark must not be empty")
    if args.execute and not re.fullmatch(r"[0-9a-f]{64}", args.inventory_sha256 or ""):
        parser.error("--execute requires the exact --inventory-sha256 from preflight")
    try:
        root = ota_root(HOME)
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ValueError("OTA root is unsafe")
        with UpdateLock(root / "launch.lock", timeout_seconds=0):
            if active_invocations(root):
                raise ValueError("another DRadar invocation is active")
            _verify_package(Path(args.manifest).expanduser(), Path(sys.argv[0]), HOME)
            from ..cleanup_recovery import cmd_recover
            return cmd_recover(args)
    except (InvalidTransition, ManifestError, OSError, ValueError, RuntimeError,
            KeyError, zipfile.BadZipFile) as exc:
        print(f"cleanup recovery rejected before state change: {exc}", file=sys.stderr)
        return 2


def main_session_exit(argv: list[str] | None = None) -> int:
    """One signed, explicit replay window for an exhausted original exit."""
    parser = argparse.ArgumentParser(prog="dradar.pyz recover-session-exit")
    parser.add_argument("--manifest", required=True, metavar="SIGNED_JSON")
    parser.add_argument("--session-id", required=True, type=_assignment_id)
    parser.add_argument("--batch-id", required=True, type=_batch_id)
    parser.add_argument("--plan", help="Original local run plan code when the session is plan scoped")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--journal-sha256")
    args = parser.parse_args(argv)
    if args.execute and not re.fullmatch(r"[0-9a-f]{64}", args.journal_sha256 or ""):
        parser.error("--execute requires the exact --journal-sha256 from preflight")
    try:
        root = ota_root(HOME)
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ValueError("OTA root is unsafe")
        with UpdateLock(root / "launch.lock", timeout_seconds=0):
            if active_invocations(root):
                raise ValueError("another DRadar invocation is active")
            _verify_package(Path(args.manifest).expanduser(), Path(sys.argv[0]), HOME)
            from ..legacy_capacity import _existing_client
            from .. import capacity_journal, session_recovery
            from types import SimpleNamespace

            client, scope, _ = _existing_client(
                SimpleNamespace(plan=args.plan, server=None))
            if scope.get("batch_id") and scope["batch_id"] != args.batch_id:
                raise ValueError("original plan belongs to another batch")
            journal = capacity_journal._read(
                HOME / "runner-reservations" / f"{args.session_id}.json")
            if journal["batch_id"] != args.batch_id:
                raise ValueError("original session belongs to another batch")
            receipt = client.runner_session_receipt(
                args.session_id, batch_id=args.batch_id)
            if (type(receipt.get("plan_scoped")) is not bool
                    or receipt["plan_scoped"] != (scope["kind"] == "plan")):
                raise ValueError("original session plan/account scope is unverified")
            result = session_recovery.recover(
                HOME, args.session_id, client, execute=args.execute,
                expected_digest=args.journal_sha256,
                explicit_replay_once=True,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result["status"] in {"ready", "released"} else 1
    except (InvalidTransition, ManifestError, OSError, ValueError, RuntimeError,
            KeyError, zipfile.BadZipFile) as exc:
        print(f"original session exit remains unresolved: {exc}", file=sys.stderr)
        return 2


__all__ = ["main", "main_cleanup", "main_session_exit"]
