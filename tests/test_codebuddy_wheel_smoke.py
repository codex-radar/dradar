"""Artifact-level smoke tests for a standard, Pier-free CodeBuddy install."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="module")
def codebuddy_artifacts(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the wheel artifact smoke test")
    output = tmp_path_factory.mktemp("codebuddy-wheel")
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(output.glob("dradar-*.whl"))
    assert len(wheels) == 1
    release_spec = importlib.util.spec_from_file_location(
        "dradar_ota_release", ROOT / "scripts" / "ota_release.py",
    )
    assert release_spec is not None and release_spec.loader is not None
    release = importlib.util.module_from_spec(release_spec)
    release_spec.loader.exec_module(release)
    zipapp = output / "dradar-smoke.pyz"
    version = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    release._build_zipapp(
        ROOT,
        zipapp,
        version=version,
        sequence=1,
        commit="1" * 40,
        tree="2" * 40,
        target=("linux", "x86_64"),
    )
    return wheels[0], zipapp


@pytest.mark.parametrize("python_name", ["python3.11", "python3.13"])
def test_standard_wheel_prepares_first_codebuddy_image_without_pier(
    codebuddy_artifacts: tuple[Path, Path],
    python_name: str,
    tmp_path: Path,
) -> None:
    uv = shutil.which("uv")
    python = shutil.which(python_name)
    if uv is None or python is None:
        pytest.skip(f"{python_name} is unavailable")
    environment = tmp_path / "venv"
    wheel, zipapp = codebuddy_artifacts
    subprocess.run(
        [uv, "venv", "--python", python, str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    environment_python = environment / "bin" / "python"
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(environment_python),
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    smoke = r'''
import importlib.metadata
import importlib.util
import sys
from types import SimpleNamespace

artifact = sys.argv[1]
if artifact:
    sys.path.insert(0, artifact)
assert importlib.util.find_spec("pier") is None
assert "datacurve-pier" not in {
    distribution.metadata["Name"].lower()
    for distribution in importlib.metadata.distributions()
}

from dradar import codebuddy_provider

issues = iter(["image missing", None])
codebuddy_provider.codebuddy_runtime_image_error = lambda _docker: next(issues)
seen = {}
def build(command, **kwargs):
    seen.update(command=command, **kwargs)
    return SimpleNamespace(returncode=0, stdout="", stderr="")
codebuddy_provider.subprocess.run = build
assert codebuddy_provider.ensure_codebuddy_runtime_image("docker") == (
    codebuddy_provider.CODEBUDDY_CONTAINER_IMAGE
)
assert seen["command"][1] == "build"
assert "codebuddy-code_Linux_${asset}.tar.gz" in seen["input"]

codebuddy_provider.codebuddy_runtime_image_error = lambda _docker: None
def unexpected(*_args, **_kwargs):
    raise AssertionError("existing image must skip the build path")
codebuddy_provider.subprocess.run = unexpected
assert codebuddy_provider.ensure_codebuddy_runtime_image("docker") == (
    codebuddy_provider.CODEBUDDY_CONTAINER_IMAGE
)
'''
    for artifact in ("", str(zipapp)):
        subprocess.run(
            [str(environment_python), "-I", "-c", smoke, artifact],
            check=True,
            capture_output=True,
            text=True,
        )
