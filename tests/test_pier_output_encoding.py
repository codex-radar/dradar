"""Exercise child startup encoding with the same redirected log boundary as Pier."""
import os
import subprocess
import sys
from pathlib import Path
from contextlib import nullcontext

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


def test_run_trial_log_boundary_with_legacy_parent(tmp_path, monkeypatch):
    """Exercise the real runner's header, child fd output and log-tail reader."""
    from dradar import runner

    monkeypatch.setenv("PYTHONIOENCODING", "gbk")
    monkeypatch.setenv("PYTHONUTF8", "0")
    original_open = Path.open
    original_read_text = Path.read_text

    def legacy_log_open(path, mode="r", buffering=-1, encoding=None,
                        errors=None, newline=None):
        if path.suffix == ".log" and "b" not in mode and encoding is None:
            encoding = "gbk"
        return original_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", legacy_log_open)

    def legacy_log_read(path, encoding=None, errors=None, **kwargs):
        if path.suffix == ".log" and encoding is None:
            encoding = "gbk"
        return original_read_text(path, encoding=encoding, errors=errors, **kwargs)

    monkeypatch.setattr(Path, "read_text", legacy_log_read)
    text = "trial failed • 完成 ✓"

    def child_command(assignment, tasks_root, jobs_dir, job_name, home,
                      dev_agent=None, **kwargs):
        trial = jobs_dir / job_name / "task__t0"
        if os.name == "nt":
            # Elevated Windows CI may default new directory ownership to the
            # Administrators group. Give this fixture an explicit current-user
            # owner and protected DACL, as the real artifact boundary requires.
            import ctypes
            from dradar.artifact_boundary import TrialFiles, UnsafeArtifact
            from dradar.artifact_boundary_win import WinAPI, SECURITY_ATTRIBUTES

            trial.parent.mkdir(parents=True)
            api = WinAPI(UnsafeArtifact)
            descriptor = ctypes.c_void_p()
            sddl = f"O:{api.user}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{api.user})"
            assert api.from_sddl(sddl, 1, ctypes.byref(descriptor), None)
            try:
                attributes = SECURITY_ATTRIBUTES(
                    ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False,
                )
                assert api.mkdir(str(trial), ctypes.byref(attributes))
            finally:
                api.free(descriptor)
            # Validate fixture ownership before launching the real child.
            with TrialFiles(trial):
                pass
        program = (
            "import pathlib, sys; "
            "p = pathlib.Path(sys.argv[1]) / 'artifacts'; "
            "p.mkdir(parents=True); "
            "(p / 'model.patch').write_bytes(b'diff'); "
            "print(" + repr(text) + "); "
            "print(" + repr(text) + ", file=sys.stderr); sys.exit(7)"
        )
        return [sys.executable, "-c", program, str(trial)]

    monkeypatch.setattr(runner, "build_pier_command", child_command)
    monkeypatch.setattr(runner, "resolve_latest_codex_cli_version",
                        lambda *a, **k: "0.145.0")
    monkeypatch.setattr(runner.image_cache, "prepare_trial_builder",
                        lambda *a, **k: runner.image_cache.TrialBuilderLease(None, False))
    monkeypatch.setattr(runner.AUTH_REGISTRY, "session",
                        lambda *a, **k: nullcontext(None))
    # This fixture launches plain Python, not Pier. Do not inject the global
    # conftest's Pier-specific sitecustomize into that child interpreter.
    monkeypatch.setattr(runner.egress, "prepare_egress_proxy_runtime",
                        lambda *a, **k: {})
    artifact = runner.run_trial(
        {"assignment_id": "encoding", "task_id": "fixture", "agent": "codex",
         "model": "gpt-5.5", "effort": "medium", "agent_version": "0.145.0"},
        tmp_path, tmp_path,
    )
    assert artifact.returncode == 7
    assert text in artifact.log_path.read_text(encoding="utf-8").splitlines()[0]
    assert runner._tail(artifact.log_path, 2).splitlines() == [text, text]
