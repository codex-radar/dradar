"""Exercise child startup encoding with the same redirected log boundary as Pier."""
import os
import subprocess
import sys

import pytest

from dradar.runner import _pier_process_env


@pytest.mark.parametrize("legacy_encoding", ["gbk", "cp936", "ascii"])
def test_pier_summary_uses_utf8_even_with_legacy_parent(tmp_path, monkeypatch, legacy_encoding):
    monkeypatch.setenv("PYTHONIOENCODING", legacy_encoding)
    monkeypatch.setenv("PYTHONUTF8", "0")
    text = "trial complete • 完成 ✓"
    program = "import sys; print(" + repr(text) + "); print(" + repr(text) + ", file=sys.stderr)"
    log = tmp_path / "pier.log"
    # Reproduce the reported failure without a Windows host or a model call:
    # the failing boundary is Python's encoding of redirected stdout.
    with log.open("wb") as stream:
        failed = subprocess.run([sys.executable, "-c", program], stdout=stream,
                                stderr=subprocess.STDOUT, env=dict(os.environ), timeout=10)
    assert failed.returncode != 0
    assert b"UnicodeEncodeError" in log.read_bytes()

    env = _pier_process_env({"agent": "kimi-code"})
    with log.open("wb") as stream:
        completed = subprocess.run([sys.executable, "-c", program], stdout=stream,
                                   stderr=subprocess.STDOUT, env=env, timeout=10)
    assert completed.returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == [text, text]
    assert os.environ["PYTHONIOENCODING"] == legacy_encoding
    assert os.environ["PYTHONUTF8"] == "0"


def test_utf8_output_does_not_hide_child_failure(tmp_path):
    log = tmp_path / "pier.log"
    with log.open("wb") as stream:
        failed = subprocess.run(
            [sys.executable, "-c", "import sys; print('•'); sys.exit(7)"],
            stdout=stream, stderr=subprocess.STDOUT,
            env=_pier_process_env({"agent": "kimi-code"}), timeout=10,
        )
    assert failed.returncode == 7
    assert log.read_text(encoding="utf-8").strip() == "•"
