"""Stable entry point: bounded discovery, safe activation, one verified handoff."""
from __future__ import annotations
import os
import subprocess
import sys
from .local_config import HOME
from .ota.integration import (_run_windows_candidate, load_trusted_keys, ota_root,
                              activate_prepared_update, runloop_safe_point)
from .ota.state import UpdateController, UpdateLock
from .ota.activity import active_invocations, register_invocation
from .ota.discovery import discover_update, start_periodic_discovery


def _activate_if_idle(root):
    if not active_invocations(root):
        activate_prepared_update(runloop_safe_point(home=HOME), home=HOME)


def main() -> int:
    from .ota import discovery
    discovery.LAUNCH_METHOD = "launcher"
    # Only bypasses discovery in an already verified child/self-test. It never
    # authorizes a pathname or bypasses verification of a downloaded artifact.
    self_test = os.environ.pop("DRADAR_OTA_SELF_TEST", None) == "1"
    if os.environ.pop("DRADAR_OTA_DISPATCH", None) == "1":
        discovery.LAUNCH_METHOD = "verified_child"
        if self_test and sys.argv[1:] == ["--version"]:
            from .cli import main as bundled_main
            return bundled_main()
        # The child owns an independent activity lease as well: if the outer
        # launcher crashes, its still-running child must continue blocking OTA.
        with UpdateLock(ota_root(HOME) / "launch.lock", timeout_seconds=1):
            child_activity = register_invocation(ota_root(HOME))
            child_activity.__enter__()
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
        with UpdateLock(root / "launch.lock", timeout_seconds=0):
            keys = load_trusted_keys(HOME)
            if keys and not active_invocations(root):
                controller = UpdateController(root, trusted_keys=keys)
                with controller.transaction():
                    controller.recover_on_launcher_start()
                _activate_if_idle(root)
            activity = register_invocation(root)
            activity.__enter__()
            if keys:
                try:
                    artifact = UpdateController(root, trusted_keys=keys).launch_artifact()
                except (OSError, ValueError, RuntimeError):
                    artifact = None
    except (OSError, ValueError, RuntimeError):
        # Do not start unregistered work that a concurrent updater might miss.
        # Registration retries briefly via the same launch gate, no networking.
        with UpdateLock(root / "launch.lock", timeout_seconds=1):
            if activity is None:
                activity = register_invocation(root)
                activity.__enter__()
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
