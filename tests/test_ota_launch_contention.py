"""Real zipapp descendants must register through a busy update gate."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib

import pytest

from dradar.ota.activity import active_invocations
from dradar.ota.state import UpdateLock


ROOT = Path(__file__).parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


@pytest.fixture(scope="module")
def candidate(tmp_path_factory):
    path = tmp_path_factory.mktemp("ota-launch") / "candidate.pyz"
    spec = importlib.util.spec_from_file_location("ota_release_launch_test", ROOT / "scripts/ota_release.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    release._build_zipapp(
        ROOT, path, version=VERSION, sequence=72,
        commit="0" * 40, tree="0" * 40,
        target=("linux", "x86_64"),
    )
    return path


@pytest.mark.parametrize("workers", [10, 60])
def test_real_zipapp_children_wait_for_launch_gate(tmp_path, candidate, workers):
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("DRADAR_")}
    env.update(DRADAR_HOME=str(home), DRADAR_OTA_DISPATCH="1")
    children = []
    try:
        with UpdateLock(home / "ota" / "launch.lock"):
            for _ in range(workers):
                children.append(subprocess.Popen(
                    [sys.executable, str(candidate), "--version"],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ))
            # Longer than the old one-second registration budget.
            time.sleep(2)
        for child in children:
            stdout, stderr = child.communicate(timeout=90)
            assert child.returncode == 0, stderr
            assert stdout.strip() == VERSION
        with UpdateLock(home / "ota" / "launch.lock"):
            assert not active_invocations(home / "ota")
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


def test_exhausted_launch_gate_fails_before_cli_work(tmp_path, monkeypatch, capsys):
    from dradar import launcher

    monkeypatch.setattr(launcher, "HOME", tmp_path)
    monkeypatch.setattr(launcher, "_LAUNCH_LOCK_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setenv("DRADAR_OTA_DISPATCH", "1")
    with UpdateLock(tmp_path / "ota" / "launch.lock"):
        assert launcher.main() == 75
    assert "could not register" in capsys.readouterr().err
    with UpdateLock(tmp_path / "ota" / "launch.lock"):
        assert not active_invocations(tmp_path / "ota")
