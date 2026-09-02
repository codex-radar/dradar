import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "x86_64", "macos/x86_64"),
        ("Darwin", "arm64", "macos/arm64"),
        ("Linux", "AMD64", "linux/x86_64"),
        ("Linux", "aarch64", "linux/arm64"),
        ("Windows", "x86_64", "windows/x86_64"),
        ("Windows", "ARM64", "windows/arm64"),
    ],
)
def test_platform_detection_in_isolated_process(system, machine, expected):
    script = """
import os
from unittest.mock import patch
from dradar.ota.manifest import PlatformTarget
with patch('platform.system', return_value=os.environ['SIM_SYSTEM']), \\
     patch('platform.machine', return_value=os.environ['SIM_MACHINE']):
    target = PlatformTarget.current()
print(f'{target.os}/{target.arch}')
"""
    source = str(Path(__file__).resolve().parents[1] / "src")
    pythonpath = os.environ.get("PYTHONPATH")
    environment = dict(
        os.environ,
        SIM_SYSTEM=system,
        SIM_MACHINE=machine,
        PYTHONPATH=f"{source}{os.pathsep}{pythonpath}" if pythonpath else source,
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
