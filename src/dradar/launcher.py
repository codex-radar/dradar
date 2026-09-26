"""Stable entry point: bounded discovery, safe activation, one verified handoff."""
from __future__ import annotations
import os
import subprocess
import sys
from .local_config import HOME
from .ota.integration import (_run_windows_candidate, load_trusted_keys, ota_root,
                              activate_prepared_update, runloop_safe_point)
from .ota.state import UpdateController, UpdateLock, UpdateLockBusy
from .ota.activity import active_invocations, register_invocation
from .ota.discovery import discover_update, start_periodic_discovery


# Fleet can start many descendants at once. A launch must wait for its turn
# through the update gate; expiry fails closed before any CLI work begins.
_LAUNCH_LOCK_TIMEOUT_SECONDS = 60


def _launch_lock(root):
    return UpdateLock(root / "launch.lock", timeout_seconds=_LAUNCH_LOCK_TIMEOUT_SECONDS)


def _activate_if_idle(root):
    if not active_invocations(root):
        activate_prepared_update(runloop_safe_point(home=HOME), home=HOME)


def main() -> int:
    # A signed zipapp can perform one explicit upload recovery while an older
    # committed bundle is held behind a durable pending-upload safe point.
    # This path never activates the zipapp or changes the OTA pointers.
    if sys.argv[1:2] == ["recover-upload"]:
        from .ota.recovery import main as recovery_main
        return recovery_main(sys.argv[2:])
    from .ota import discovery
    from .child_entrypoint import retain_inherited_windows_payload
    retain_inherited_windows_payload()
    discovery.LAUNCH_METHOD = "launcher"
    # Only bypasses discovery in an already verified child/self-test. It never
    # authorizes a pathname or bypasses verification of a downloaded artifact.
    self_test = os.environ.pop("DRADAR_OTA_SELF_TEST", None) == "1"
    verified_child = os.environ.pop("DRADAR_OTA_DISPATCH", None) == "1"
    source_child = os.environ.pop("DRADAR_OTA_SOURCE_CHILD", None) == "1"
    if verified_child or source_child:
        # Source workers inherit the installed payload, not a signed artifact.
        # Both kinds hold activity independently if their supervisor crashes.
        discovery.LAUNCH_METHOD = "verified_child" if verified_child else "launcher"
        if verified_child and self_test and sys.argv[1:] == ["--version"]:
            from .cli import main as bundled_main
            return bundled_main()
        # The child owns an independent activity lease as well: if the outer
        # launcher crashes, its still-running child must continue blocking OTA.
        try:
            with _launch_lock(ota_root(HOME)):
                child_activity = register_invocation(ota_root(HOME))
                child_activity.__enter__()
        except (UpdateLockBusy, OSError) as exc:
            print(f"DRadar could not register this CLI process for a safe update: {exc}", file=sys.stderr)
            return 75
        try:
            from .cli import main as bundled_main
            return bundled_main()
        finally:
            child_activity.__exit__(None, None, None)
    root = ota_root(HOME)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        from .cli import main as bundled_main
        return bundled_main()
    discover_update(HOME)
    activity = None
    artifact = None
    try:
        with _launch_lock(root):
            # Select the signed runtime under the same gate as registration.
            # A contender must not skip selection and silently run old code.
            keys = None
            try:
                keys = load_trusted_keys(HOME)
                if keys and not active_invocations(root):
                    controller = UpdateController(root, trusted_keys=keys)
                    with controller.transaction(
                        timeout_seconds=_LAUNCH_LOCK_TIMEOUT_SECONDS,
                    ):
                        controller.recover_on_launcher_start()
                    _activate_if_idle(root)
            except UpdateLockBusy:
                # Never mistake a busy update transaction for corrupt state:
                # skipping signed selection here would run bundled old code.
                raise
            except (OSError, ValueError, RuntimeError):
                keys = None
            activity = register_invocation(root)
            activity.__enter__()
            if keys:
                try:
                    artifact = UpdateController(root, trusted_keys=keys).launch_artifact()
                except (OSError, ValueError, RuntimeError):
                    artifact = None
    except (UpdateLockBusy, OSError) as exc:
        print(f"DRadar could not register this CLI process for a safe update: {exc}", file=sys.stderr)
        return 75
    periodic_stop = start_periodic_discovery(HOME)
    try:
        if artifact is not None:
            if os.name == "nt":
                return _run_windows_candidate(artifact.read_bytes(), sys.argv[1:])
            fd = artifact.duplicate_fd()
            try:
                return subprocess.run(
                    [sys.executable, f"/dev/fd/{fd}", *sys.argv[1:]],
                    pass_fds=(fd,), check=False,
                    env={**os.environ, "DRADAR_OTA_DISPATCH": "1"},
                ).returncode
            finally:
                os.close(fd)
        from .cli import main as bundled_main
        return bundled_main()
    finally:
        periodic_stop.set()
        if artifact is not None:
            artifact.close()
        if activity is not None:
            activity.__exit__(None, None, None)
        try:
            with UpdateLock(root / "launch.lock", timeout_seconds=0):
                _activate_if_idle(root)
        except (OSError, ValueError, RuntimeError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
