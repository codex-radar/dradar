"""Create synthetic trial roots with the real host-private boundary intact."""
import os
from pathlib import Path


def private_trial(path: Path) -> None:
    if os.name != "nt":
        path.mkdir(parents=True, mode=0o700)
        return
    # Same explicit owner/protected DACL fixture as test_pier_output_encoding.
    # Elevated Windows CI otherwise defaults the owner to Administrators.
    import ctypes
    from dradar.artifact_boundary import TrialFiles, UnsafeArtifact
    from dradar.artifact_boundary_win import WinAPI, SECURITY_ATTRIBUTES

    path.parent.mkdir(parents=True, exist_ok=True)
    api = WinAPI(UnsafeArtifact)
    descriptor = ctypes.c_void_p()
    sddl = f"O:{api.user}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{api.user})"
    assert api.from_sddl(sddl, 1, ctypes.byref(descriptor), None)
    try:
        attributes = SECURITY_ATTRIBUTES(
            ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False,
        )
        assert api.mkdir(str(path), ctypes.byref(attributes))
    finally:
        api.free(descriptor)
    with TrialFiles(path):
        pass
