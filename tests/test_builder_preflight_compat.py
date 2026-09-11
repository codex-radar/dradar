"""Builder compatibility probes never use the volunteer's Docker daemon."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dradar import image_cache


_PREFLIGHT = image_cache.preflight_trial_builder


@pytest.mark.parametrize("source", ["daemon", "inherited"])
@pytest.mark.parametrize("value", ["", " \n", "{", "{}", "false", "1", '"mirror"',
                                   "[null]", "[1]", "[true]", json.dumps(["https://m.example"] * 9)])
def test_mirror_snapshots_reject_invalid_metadata(monkeypatch, source, value):
    monkeypatch.delenv(image_cache.BUILDER_MIRRORS_ENV, raising=False)
    if source == "inherited":
        monkeypatch.setenv(image_cache.BUILDER_MIRRORS_ENV, value)

    def docker(command, **kwargs):
        assert source == "daemon", "inherited snapshots must not requery Docker"
        return subprocess.CompletedProcess(command, 0, value, "")

    monkeypatch.setattr(image_cache, "_run_docker", docker)
    with pytest.raises(image_cache.DockerUnavailable):
        image_cache.docker_registry_mirrors()


@pytest.mark.parametrize("value", ["null", " null\n", "[]", " []\n"])
def test_daemon_without_mirrors_uses_direct_registry(monkeypatch, value):
    monkeypatch.delenv(image_cache.BUILDER_MIRRORS_ENV, raising=False)
    monkeypatch.setattr(image_cache, "_run_docker", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, value, ""))
    assert image_cache.docker_registry_mirrors() == ()


def test_inherited_snapshot_is_strict_and_deduplicated(monkeypatch):
    monkeypatch.setattr(image_cache, "_run_docker", lambda *a, **k:
                        pytest.fail("must reuse the parent's snapshot"))
    monkeypatch.setenv(image_cache.BUILDER_MIRRORS_ENV, "null")
    with pytest.raises(image_cache.DockerUnavailable):
        image_cache.docker_registry_mirrors()
    monkeypatch.setenv(image_cache.BUILDER_MIRRORS_ENV, json.dumps([
        "https://a.example/", "https://b.example", "https://a.example",
    ]))
    assert image_cache.docker_registry_mirrors() == ("https://a.example", "https://b.example")


@pytest.fixture
def probe(monkeypatch):
    state = SimpleNamespace(
        help=subprocess.CompletedProcess([], 0, "Options:\n      --check  Check build\n", ""),
        results=[subprocess.CompletedProcess([], 0, "", "")],
        builds=[], removed=[], cleanup=(True, None), create=None,
    )

    def prepare(*args, **kwargs):
        if isinstance(state.create, BaseException):
            raise state.create
        return state.create or image_cache.TrialBuilderLease("probe", True)

    def docker(command, **kwargs):
        if command == ["buildx", "build", "--help"]:
            if isinstance(state.help, BaseException):
                raise state.help
            return state.help
        state.builds.append(command)
        dockerfile = Path(command[command.index("--file") + 1])
        assert dockerfile.read_bytes() == f"FROM {image_cache.BUILDER_PREFLIGHT_IMAGE}\n".encode()
        assert "--builder" in command and "--pull" in command
        assert not any(flag in command for flag in ("--load", "--push", "--tag"))
        outcome = state.results.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def remove(*args, **kwargs):
        state.removed.append(args)
        if isinstance(state.cleanup, BaseException):
            raise state.cleanup
        return state.cleanup

    monkeypatch.setattr(image_cache, "preflight_trial_builder", _PREFLIGHT)
    monkeypatch.setattr(image_cache, "prepare_trial_builder", prepare)
    monkeypatch.setattr(image_cache, "remove_trial_builder", remove)
    monkeypatch.setattr(image_cache, "_run_docker", docker)
    return state


@pytest.mark.parametrize("supports_check", [True, False])
def test_preflight_selects_supported_capability_without_export(tmp_path, probe, supports_check):
    if not supports_check:
        probe.help = subprocess.CompletedProcess([], 0, "Options:\n      --pull  Pull base\n", "")
    result = _PREFLIGHT(tmp_path, registry_mirrors=())
    assert result.ok
    assert len(probe.builds) == 1
    assert ("--check" in probe.builds[0]) == supports_check
    if not supports_check:
        assert "--output=type=cacheonly" in probe.builds[0]
    assert len(probe.removed) == 1


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("fallback_rc", [0, 1])
def test_explicit_check_rejection_falls_back_once(tmp_path, probe, stream, fallback_rc):
    first = {"stdout": "", "stderr": "", stream: "ERROR: unknown flag: --check\n"}
    probe.results = [subprocess.CompletedProcess([], 125, **first),
                     subprocess.CompletedProcess([], fallback_rc, "", "fallback result")]
    result = _PREFLIGHT(tmp_path, registry_mirrors=())
    assert result.ok == (fallback_rc == 0)
    assert result.returncode == fallback_rc
    assert len(probe.builds) == 2
    assert "--check" not in probe.builds[1]
    assert "--output=type=cacheonly" in probe.builds[1]
    assert len(probe.removed) == 1


@pytest.mark.parametrize("diagnostic", [
    "network is unreachable", "i/o timeout", "no such host", "x509: certificate error",
    "unauthorized: authentication required", "toomanyrequests", "manifest unknown",
    "load metadata: unknown flag: --check in registry response",
])
def test_registry_failures_do_not_trigger_compatibility_retry(tmp_path, probe, diagnostic):
    probe.results = [subprocess.CompletedProcess([], 1, "", diagnostic)]
    result = _PREFLIGHT(tmp_path, registry_mirrors=())
    assert not result.ok and result.stage == "base_image_metadata"
    assert diagnostic in result.detail
    assert len(probe.builds) == len(probe.removed) == 1


@pytest.mark.parametrize("error", [
    subprocess.CompletedProcess([], 1, "", "capability query failed"),
    image_cache.DockerUnavailable("capability query unavailable"),
])
def test_capability_failure_is_not_treated_as_old_buildx(tmp_path, probe, error):
    probe.help = error
    result = _PREFLIGHT(tmp_path, registry_mirrors=())
    assert not result.ok and result.stage == "builder_capability"
    assert probe.builds == []
    assert len(probe.removed) == 1


@pytest.mark.parametrize("stage", ["create", "context", "build"])
def test_local_errors_always_cleanup_the_probe(tmp_path, monkeypatch, probe, stage):
    error = PermissionError("synthetic local storage failure")
    if stage == "create":
        probe.create = error
    elif stage == "context":
        monkeypatch.setattr(image_cache.tempfile, "TemporaryDirectory", lambda **kwargs:
                            (_ for _ in ()).throw(error))
    else:
        probe.results = [error]
    result = _PREFLIGHT(tmp_path, registry_mirrors=())
    assert not result.ok
    assert "synthetic local storage failure" in result.detail
    assert len(probe.removed) == 1


def test_interrupt_cleans_up_then_propagates(tmp_path, probe):
    probe.results = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        _PREFLIGHT(tmp_path, registry_mirrors=())
    assert len(probe.removed) == 1


@pytest.mark.parametrize("primary_failed", [True, False])
@pytest.mark.parametrize("cleanup_raises", [True, False])
def test_cleanup_failure_preserves_primary_diagnostic(tmp_path, probe, primary_failed, cleanup_raises):
    if primary_failed:
        probe.results = [subprocess.CompletedProcess([], 7, "", "i/o timeout")]
    detail = "cleanup failed https://user:secret@example.invalid/"
    probe.cleanup = OSError(detail) if cleanup_raises else (False, detail)
    result = _PREFLIGHT(tmp_path, registry_mirrors=())
    assert not result.ok
    assert "cleanup failed" in result.cleanup_detail
    assert "secret" not in result.cleanup_detail
    if primary_failed:
        assert result.returncode == 7
        assert result.stage == "base_image_metadata"
        assert result.failure_code == "registry_timeout"
        assert "i/o timeout" in result.detail
    else:
        assert result.stage == "builder_cleanup"
        assert result.failure_code == "builder_cleanup_failed"


@pytest.mark.parametrize("error", ["permission denied", "Cannot connect to the Docker daemon", ""])
def test_cleanup_inspection_failure_is_not_proof_of_absence(tmp_path, monkeypatch, error):
    monkeypatch.setattr(image_cache, "_run_docker", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, "", error))
    removed, detail = image_cache.remove_trial_builder(tmp_path, "probe")
    assert not removed and detail


def test_cleanup_accepts_only_exact_missing_builder(tmp_path, monkeypatch):
    name = image_cache.trial_builder_name(tmp_path, "probe")
    monkeypatch.setattr(image_cache, "_run_docker", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, "", f'ERROR: no builder "{name}" found'))
    assert image_cache.remove_trial_builder(tmp_path, "probe") == (True, None)
