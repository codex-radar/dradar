"""Tests always import the SOURCE tree, never a site-packages copy.

Same invariant as dradar-server's conftest, for the same reason (learned the
hard way multiple times): a stale regular install in whatever venv runs
pytest makes the whole suite silently test old code with zero errors — and
on this machine editable installs are unreliable (the sandbox keeps stamping
macOS UF_HIDDEN on the generated .pth, which CPython's site.py then skips).
Pinning src/ at the front of sys.path removes the failure mode regardless of
how (or whether) the package is installed in the interpreter.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def _isolate_real_dradar_home(monkeypatch, tmp_path):
    """Unit tests must never inspect or refresh a volunteer's real secrets."""

    monkeypatch.setenv("DRADAR_HOME", str(tmp_path / "dradar-home"))


@pytest.fixture(autouse=True)
def _disable_live_egress_image_pull(monkeypatch):
    """The unit suite must never contact a container registry or Docker."""

    from dradar import egress

    monkeypatch.delenv(egress.EGRESS_PROXY_MODE_ENV, raising=False)
    monkeypatch.delenv(egress.EGRESS_PROXY_IMAGE_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(egress.DRADAR_HTTP_PROXY_ENV, raising=False)
    monkeypatch.delenv(egress.DRADAR_NO_PROXY_ENV, raising=False)
    monkeypatch.delenv(egress.DRADAR_CONTAINER_HTTP_PROXY_ENV, raising=False)
    monkeypatch.delenv(egress.DRADAR_CONTAINER_NO_PROXY_ENV, raising=False)
    runtime = {"DRADAR_EGRESS_PROXY_IMAGE": "sha256:" + "a" * 64}
    monkeypatch.setattr(
        egress,
        "prepare_egress_proxy_runtime",
        lambda *_args, **_kwargs: dict(runtime),
    )
    monkeypatch.setattr(
        egress,
        "egress_proxy_preflight",
        lambda *_args, **_kwargs: (True, ""),
    )
    monkeypatch.setattr(
        egress,
        "ensure_egress_runtime_ready",
        lambda *_args, **_kwargs: dict(runtime),
    )
    from dradar import image_cache
    monkeypatch.setattr(
        image_cache,
        "preflight_trial_builder",
        lambda *_args, **_kwargs: image_cache.TrialBuilderPreflight(
            True, 0, "base_image_metadata", None, "", (),
        ),
    )
