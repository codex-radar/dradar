"""Platform-aware doctor: right fix hints for macOS / Linux / WSL2 / Windows."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dradar import doctor


_REAL_DOCKER_HUB_PREFLIGHT = doctor._docker_hub_preflight


@pytest.fixture(autouse=True)
def _stub_external_provider_state(monkeypatch, tmp_path):
    """Unit tests must not depend on live registry or provider state."""

    monkeypatch.setattr(
        doctor, "_docker_hub_preflight", lambda _docker, _platform: (True, ""),
    )
    monkeypatch.setattr(
        doctor, "managed_codebuddy_home", lambda: tmp_path / "no-codebuddy",
    )


def test_platform_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert doctor._platform() == "macos"


def test_platform_windows_native(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert doctor._platform() == "windows"


def test_platform_wsl_detected_via_proc_version(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "linux")
    proc = tmp_path / "version"
    proc.write_text("Linux version 5.15.153.1-microsoft-standard-WSL2 ...")
    assert doctor._platform(proc_version=proc) == "wsl"


def test_platform_bare_linux(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "linux")
    proc = tmp_path / "version"
    proc.write_text("Linux version 6.8.0-45-generic (buildd@lcy02) ...")
    assert doctor._platform(proc_version=proc) == "linux"


def test_platform_linux_without_proc_version(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "linux")
    assert doctor._platform(proc_version=tmp_path / "missing") == "linux"


def test_docker_hub_preflight_uses_buildkit_without_pulling_layers(
    monkeypatch,
):
    seen = {}

    def run(cmd, **kwargs):
        seen.update(cmd=cmd, kwargs=kwargs)
        assert Path(cmd[-1]).is_dir()
        return SimpleNamespace(returncode=0, stdout="Check complete", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", run)

    assert _REAL_DOCKER_HUB_PREFLIGHT("/usr/bin/docker", "linux") == (True, "")
    assert seen["cmd"][:-1] == [
        "/usr/bin/docker", "build", "--check", "--pull", "--file", "-",
    ]
    assert seen["kwargs"]["input"] == (
        "FROM docker.io/library/ubuntu:24.04\n"
    )
    assert seen["kwargs"]["timeout"] == 20


def test_docker_hub_preflight_falls_back_for_older_docker(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "build":
            return SimpleNamespace(
                returncode=125, stdout="", stderr="unknown flag: --check",
            )
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", run)

    assert _REAL_DOCKER_HUB_PREFLIGHT("docker", "linux") == (True, "")
    assert calls[1] == [
        "docker", "manifest", "inspect", "docker.io/library/ubuntu:24.04",
    ]


def test_docker_hub_preflight_reports_dns_tls_interception(monkeypatch):
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                "x509: certificate is valid for *.twimg.com, not "
                "registry-1.docker.io"
            ),
        ),
    )

    ready, hint = _REAL_DOCKER_HUB_PREFLIGHT("docker", "macos")

    assert ready is False
    assert "wrong TLS endpoint" in hint
    assert "OrbStack/Docker Desktop" in hint
    assert "twimg" not in hint


def test_docker_hub_preflight_reports_timeout_without_leaking_output(
    monkeypatch,
):
    def timeout(*_args, **_kwargs):
        raise doctor.subprocess.TimeoutExpired("docker", timeout=20)

    monkeypatch.setattr(doctor.subprocess, "run", timeout)

    ready, hint = _REAL_DOCKER_HUB_PREFLIGHT("docker", "linux")

    assert ready is False
    assert "authentication/registry access failed" in hint
    assert "Docker daemon proxy and DNS" in hint


def _run_doctor(monkeypatch, capsys, plat: str) -> tuple[int, str]:
    monkeypatch.setattr(doctor, "_platform", lambda: plat)
    monkeypatch.setattr(doctor, "deepseek_opted_in", lambda: False)
    # No tools on PATH and no config: every check FAILs, printing every hint.
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_load_config", lambda: {})
    rc = doctor.cmd_doctor(SimpleNamespace())
    return rc, capsys.readouterr().out


def test_doctor_linux_hints_are_linux(monkeypatch, capsys):
    rc, out = _run_doctor(monkeypatch, capsys, "linux")
    assert rc == 1
    assert "docs.docker.com/engine/install" in out
    assert "usermod -aG docker" in out
    assert "npm install -g @openai/codex" in out
    assert "OrbStack" not in out and "brew" not in out


def test_doctor_wsl_hints_mention_docker_desktop_integration(monkeypatch, capsys):
    rc, out = _run_doctor(monkeypatch, capsys, "wsl")
    assert rc == 1
    assert "WSL integration" in out
    assert "OrbStack" not in out


def test_doctor_macos_hints_unchanged(monkeypatch, capsys):
    rc, out = _run_doctor(monkeypatch, capsys, "macos")
    assert rc == 1
    assert "brew install --cask orbstack" in out
    assert "brew install codex" in out


def test_doctor_native_windows_runs_real_preflight_with_native_hints(
    monkeypatch, capsys,
):
    rc, out = _run_doctor(monkeypatch, capsys, "windows")
    assert rc == 1
    assert "native Windows support is experimental" in out
    assert "docker CLI" in out
    assert "Docker.DockerDesktop" in out
    assert "chatgpt.com/codex/install.ps1" in out
    assert "wsl --install" not in out
    assert "Ubuntu" not in out


@pytest.mark.parametrize(
    "api_result,expected",
    [(0, "unavailable"), (1, "available")],
)
def test_windows_virtualization_probe_uses_official_feature_flag(
    monkeypatch, api_result, expected,
):
    seen = []

    class Probe:
        argtypes = None
        restype = None

        def __call__(self, feature):
            seen.append(feature)
            return api_result

    kernel32 = type("Kernel32", (), {"IsProcessorFeaturePresent": Probe()})()
    monkeypatch.setattr(
        doctor.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel32,
        raising=False,
    )

    assert doctor._windows_virtualization_state() == expected
    assert seen == [21]


def test_windows_virtualization_probe_fails_closed_to_unknown(monkeypatch):
    def unavailable_api(*_args, **_kwargs):
        raise OSError("kernel32 unavailable")

    monkeypatch.setattr(
        doctor.ctypes, "WinDLL", unavailable_api, raising=False,
    )

    assert doctor._windows_virtualization_state() == "unknown"


def test_windows_docker_failure_skips_dependency_downloads(
    monkeypatch, capsys, tmp_path,
):
    tasks_root = tmp_path / "deep-swe" / "tasks"
    monkeypatch.setattr(doctor, "_platform", lambda: "windows")
    monkeypatch.setattr(doctor, "_load_config", lambda: {})
    monkeypatch.setattr(doctor, "tasks_root_from_config", lambda _cfg: tasks_root)
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: "C:/Program Files/Docker/docker.exe"
        if name == "docker" else None,
    )
    monkeypatch.setattr(
        doctor,
        "_probe",
        lambda cmd: cmd[-1] == "version",
    )
    monkeypatch.setattr(
        doctor, "_windows_virtualization_state", lambda: "unavailable",
    )
    monkeypatch.setattr(
        doctor.runner,
        "ensure_pier",
        lambda: pytest.fail("Pier must not be installed while Docker is blocked"),
    )
    monkeypatch.setattr(
        doctor.runner,
        "ensure_tasks_root",
        lambda _path: pytest.fail(
            "task repository must not be downloaded while Docker is blocked"
        ),
    )

    rc = doctor.cmd_doctor(SimpleNamespace())
    out = capsys.readouterr().out

    assert rc == 1
    assert "firmware virtualization is not available to the OS" in out
    assert "[skip] pier bootstrap" in out
    assert "[skip] tasks_root download" in out
    assert "no benchmark repository downloaded" in out
    assert not tasks_root.exists()


def test_windows_healthy_docker_preserves_existing_bootstrap(
    monkeypatch, capsys, tmp_path,
):
    tasks_root = tmp_path / "deep-swe" / "tasks"
    state = {"pier_ready": False, "pier_calls": 0, "task_calls": 0}
    egress_image_calls = []

    def which(name):
        if name == "docker":
            return "C:/Program Files/Docker/docker.exe"
        if name == "pier" and state["pier_ready"]:
            return "C:/Users/test/.local/bin/pier.exe"
        return None

    def ensure_pier():
        state["pier_calls"] += 1
        state["pier_ready"] = True

    def ensure_tasks_root(path):
        state["task_calls"] += 1
        path.mkdir(parents=True)

    monkeypatch.setattr(doctor, "_platform", lambda: "windows")
    monkeypatch.setattr(doctor, "_load_config", lambda: {})
    monkeypatch.setattr(doctor, "tasks_root_from_config", lambda _cfg: tasks_root)
    monkeypatch.setattr(doctor.shutil, "which", which)
    monkeypatch.setattr(doctor, "_probe", lambda _cmd: True)
    monkeypatch.setattr(
        doctor.egress,
        "egress_proxy_preflight",
        lambda docker, platform: (
            egress_image_calls.append((docker, platform)) or (True, "")
        ),
    )
    monkeypatch.setattr(
        doctor,
        "_windows_virtualization_state",
        lambda: pytest.fail("healthy Docker must not run the fallback probe"),
    )
    monkeypatch.setattr(doctor.runner, "ensure_pier", ensure_pier)
    monkeypatch.setattr(
        doctor.runner, "_pier_version", lambda _path: doctor.runner.PIER_VERSION,
    )
    monkeypatch.setattr(
        doctor.runner, "_pier_version_compatible", lambda _version: True,
    )
    monkeypatch.setattr(doctor.runner, "ensure_tasks_root", ensure_tasks_root)

    rc = doctor.cmd_doctor(SimpleNamespace())
    out = capsys.readouterr().out

    assert rc == 1  # no agent auth or server login in this isolated test
    assert state == {"pier_ready": True, "pier_calls": 1, "task_calls": 1}
    assert egress_image_calls == [
        ("C:/Program Files/Docker/docker.exe", "windows"),
    ]
    assert "[ok ] docker daemon" in out
    assert (
        "[ok ] container network for Pier — pinned egress image (amd64/arm64)"
        in out
    )
    assert "docker resources" not in out
    assert "[ok ] pier" in out
    assert "[ok ] tasks_root" in out
    assert "[skip]" not in out


def test_doctor_checks_runtime_without_static_resource_or_disk_estimates(
    monkeypatch, capsys, tmp_path,
):
    tasks_root = tmp_path / "tasks"
    tasks_root.mkdir()
    monkeypatch.setattr(doctor, "_platform", lambda: "macos")
    monkeypatch.setattr(doctor, "_load_config", lambda: {})
    monkeypatch.setattr(doctor, "tasks_root_from_config", lambda _cfg: tasks_root)
    monkeypatch.setattr(doctor, "deepseek_opted_in", lambda: False)
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in {"docker", "pier"} else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _cmd: True)
    monkeypatch.setattr("dradar.capacity.docker_resources", lambda: (
        pytest.fail("doctor must not run the optional resource estimate")
    ))
    disk_probes = []
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda path: (
        disk_probes.append(path) or SimpleNamespace(free=9 * 1024 ** 3)
    ))
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(
        doctor.runner, "_pier_version", lambda _path: doctor.runner.PIER_VERSION,
    )
    monkeypatch.setattr(
        doctor.runner, "_pier_version_compatible", lambda _version: True,
    )

    doctor.cmd_doctor(SimpleNamespace())
    out = capsys.readouterr().out

    assert "[ok ] docker daemon" in out
    assert "[ok ] docker compose plugin" in out
    assert "docker resources" not in out
    assert "disk free" not in out
    assert disk_probes == []


def test_doctor_blocks_deepseek_when_official_catalog_is_invalid(
    monkeypatch, capsys, tmp_path,
):
    tasks_root = tmp_path / "tasks"
    tasks_root.mkdir()
    monkeypatch.setattr(doctor, "_platform", lambda: "linux")
    monkeypatch.setattr(doctor, "_load_config", lambda: {})
    monkeypatch.setattr(doctor, "tasks_root_from_config", lambda _cfg: tasks_root)
    monkeypatch.setattr(doctor, "deepseek_opted_in", lambda: True)
    monkeypatch.setattr(doctor, "deepseek_api_key", lambda: "configured")
    monkeypatch.setattr(
        doctor,
        "deepseek_catalog_error",
        lambda: "DeepSeek model catalog integrity check failed",
    )
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"docker", "pier"} else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _cmd: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(
        doctor.runner, "_pier_version", lambda _path: doctor.runner.PIER_VERSION,
    )
    monkeypatch.setattr(
        doctor.runner, "_pier_version_compatible", lambda _version: True,
    )

    rc = doctor.cmd_doctor(SimpleNamespace())
    out = capsys.readouterr().out

    assert rc == 1
    assert "[FAIL] DeepSeek Codex models.json — official catalog" in out
    assert "integrity check failed" in out
    assert "DeepSeek V4 Flash / Pro — Codex provider ready" not in out


def test_dsh_scoped_doctor_uses_public_uvx_without_host_pier(
    monkeypatch, capsys, tmp_path,
):
    tasks_root = tmp_path / "tasks"
    tasks_root.mkdir()
    monkeypatch.setattr(doctor, "_platform", lambda: "linux")
    monkeypatch.setattr(
        doctor, "_load_config",
        lambda: {"server": "https://example.test", "token": "hidden"},
    )
    monkeypatch.setattr(doctor, "tasks_root_from_config", lambda _cfg: tasks_root)
    monkeypatch.setattr(doctor, "deepseek_opted_in", lambda: True)
    monkeypatch.setattr(doctor, "deepseek_api_key", lambda: "configured")
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in {"docker", "uvx"} else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _cmd: True)
    monkeypatch.setattr(
        doctor.shutil, "disk_usage",
        lambda _path: SimpleNamespace(free=100_000_000_000),
    )
    monkeypatch.setattr(
        doctor, "_client",
        lambda _cfg: SimpleNamespace(whoami=lambda: {"nickname": "tester"}),
    )
    monkeypatch.setattr(
        doctor.runner, "ensure_pier",
        lambda: pytest.fail("DSH scoped doctor must not install host Pier"),
    )
    monkeypatch.setattr(
        doctor, "deepseek_catalog_error",
        lambda: pytest.fail("DSH does not consume the Codex model catalog"),
    )

    rc = doctor.cmd_doctor(SimpleNamespace(agent="dsh-minimal"))
    out = capsys.readouterr().out

    assert rc == 0
    assert "[ok ] uvx — isolated public DSH runner" in out
    assert "[ok ] DeepSeek V4 Flash / Pro / Vision — DSH Minimal agent ready" in out
    assert "SecurityMind" not in out
    assert "[ok ] pier" not in out.lower()


def test_dsh_scoped_doctor_reports_its_own_retry_command(
    monkeypatch, capsys, tmp_path,
):
    tasks_root = tmp_path / "tasks"
    tasks_root.mkdir()
    monkeypatch.setattr(doctor, "_platform", lambda: "linux")
    monkeypatch.setattr(doctor, "_load_config", lambda: {})
    monkeypatch.setattr(doctor, "tasks_root_from_config", lambda _cfg: tasks_root)
    monkeypatch.setattr(doctor, "deepseek_api_key", lambda: None)
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _cmd: True)
    monkeypatch.setattr(
        doctor.shutil, "disk_usage",
        lambda _path: SimpleNamespace(free=100_000_000_000),
    )

    rc = doctor.cmd_doctor(SimpleNamespace(agent="dsh-minimal"))
    out = capsys.readouterr().out

    assert rc == 1
    assert "[FAIL] uvx — isolated public DSH runner" in out
    assert "[FAIL] DeepSeek API key" in out
    assert "re-run: dradar doctor --agent dsh-minimal" in out
    assert "SecurityMind" not in out


def test_zcode_scoped_doctor_ignores_other_configured_provider_slots(
    monkeypatch, capsys, tmp_path,
):
    tasks_root = tmp_path / "tasks"
    tasks_root.mkdir()
    monkeypatch.setattr(doctor, "_platform", lambda: "linux")
    monkeypatch.setattr(
        doctor, "_load_config",
        lambda: {"server": "https://example.test", "token": "hidden"},
    )
    monkeypatch.setattr(doctor, "tasks_root_from_config", lambda _cfg: tasks_root)
    monkeypatch.setattr(doctor, "deepseek_opted_in", lambda: False)
    monkeypatch.setattr(doctor, "zcode_api_key", lambda: "configured")
    monkeypatch.setattr(doctor, "zcode_cli_path", lambda: "/pinned/zcode.cjs")
    monkeypatch.setattr(doctor, "zcode_cli_error", lambda _path: None)
    monkeypatch.setattr(
        doctor, "grok_cli_path",
        lambda: pytest.fail("ZCode scope must not inspect Grok"),
    )
    monkeypatch.setattr(
        doctor, "kimi_cli_path",
        lambda: pytest.fail("ZCode scope must not inspect Kimi"),
    )
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _cmd: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(doctor.runner, "_resolve_user_tool", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor.runner, "_pier_version", lambda _path: doctor.runner.PIER_VERSION)
    monkeypatch.setattr(doctor.runner, "_pier_version_compatible", lambda _version: True)
    monkeypatch.setattr(
        doctor.shutil, "disk_usage",
        lambda _path: SimpleNamespace(free=100_000_000_000),
    )
    monkeypatch.setattr(
        doctor, "_client",
        lambda _cfg: SimpleNamespace(whoami=lambda: {"nickname": "tester"}),
    )

    rc = doctor.cmd_doctor(SimpleNamespace(agent="zcode"))
    out = capsys.readouterr().out

    assert rc == 0
    assert "ZCode GLM-5.3 — Coding Plan provider ready" in out
    assert "Grok" not in out
    assert "Kimi" not in out


def test_antigravity_scoped_doctor_does_not_require_codex_or_claude(
    monkeypatch, capsys, tmp_path,
):
    tasks_root = tmp_path / "tasks"
    tasks_root.mkdir()
    monkeypatch.setattr(doctor, "_platform", lambda: "linux")
    monkeypatch.setattr(
        doctor, "_load_config",
        lambda: {"server": "https://example.test", "token": "hidden"},
    )
    monkeypatch.setattr(doctor, "tasks_root_from_config", lambda _cfg: tasks_root)
    monkeypatch.setattr(doctor, "deepseek_opted_in", lambda: False)
    monkeypatch.setattr(doctor, "antigravity_auth_path", lambda: tmp_path / "agy")
    monkeypatch.setattr(doctor, "prepare_antigravity_auth", lambda: None)
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _cmd: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(
        doctor.runner, "_resolve_user_tool", lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        doctor.runner, "_pier_version", lambda _path: doctor.runner.PIER_VERSION,
    )
    monkeypatch.setattr(
        doctor.runner, "_pier_version_compatible", lambda _version: True,
    )
    monkeypatch.setattr(
        doctor.shutil, "disk_usage",
        lambda _path: SimpleNamespace(free=100_000_000_000),
    )
    monkeypatch.setattr(
        doctor, "_client",
        lambda _cfg: SimpleNamespace(whoami=lambda: {"nickname": "tester"}),
    )

    rc = doctor.cmd_doctor(SimpleNamespace(agent="antigravity"))
    out = capsys.readouterr().out

    assert rc == 0
    assert "Antigravity subscription OAuth" in out
    assert "Gemini 3.7 Flash" in out
    assert "codex CLI" not in out
    assert "claude CLI" not in out
    assert "all checks passed" in out
