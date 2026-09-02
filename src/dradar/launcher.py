"""Stable console-script launcher for signed zipapp candidates."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from .local_config import HOME
from .ota.integration import load_trusted_keys, ota_root
from .ota.state import InvalidTransition, UpdateController


def main() -> int:
    keys = load_trusted_keys(HOME)
    if keys:
        controller = UpdateController(ota_root(HOME), trusted_keys=keys)
        try:
            with controller.transaction():
                controller.recover_on_launcher_start()
            artifact = controller.launch_artifact()
            try:
                if os.name != "nt":
                    fd = artifact.duplicate_fd()
                    os.set_inheritable(fd, True)
                    os.execv(
                        sys.executable,
                        [sys.executable, f"/dev/fd/{fd}", *sys.argv[1:]],
                    )
                with tempfile.NamedTemporaryFile(  # pragma: no cover - Windows CI
                    suffix=".pyz", delete=False
                ) as handle:
                    handle.write(artifact.read_bytes())
                    copy = Path(handle.name)
                os.execv(  # pragma: no cover
                    sys.executable, [sys.executable, str(copy), *sys.argv[1:]]
                )
            finally:
                artifact.close()
        except (InvalidTransition, OSError, ValueError):
            # Missing/corrupt/offline OTA state never makes the installed CLI
            # unavailable. Signed pointer validation already failed closed.
            pass
    from .cli import main as bundled_main

    return bundled_main()


if __name__ == "__main__":
    raise SystemExit(main())
