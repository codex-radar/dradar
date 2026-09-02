"""Docker recovery is fully mocked: these tests never alter a real engine."""

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from dradar import docker_runtime


def _done(argv=(), *, code=0, out="", err=""):
    return subprocess.CompletedProcess(list(argv), code, out, err)


@pytest.mark.parametrize(
    ("family", "context", "endpoint", "expected"),
    [
        ("macos", "orbstack", "unix:///Users/a/.orbstack/run/docker.sock", "orbstack"),
        ("macos", "colima", "unix:///Users/a/.colima/default/docker.sock", "colima"),
        ("macos", "rancher-desktop", "unix:///Users/a/.rd/docker.sock", "rancher-desktop"),
        ("macos", "desktop-linux", "unix:///Users/a/.docker/run/docker.sock", "docker-desktop"),
        ("windows", "desktop-linux", "npipe:////./pipe/dockerDesktopLinuxEngine", "docker-desktop"),
        ("wsl", "desktop-linux", "unix:///var/run/docker.sock", "docker-desktop"),
        ("linux", "rootless", "unix:///run/user/1000/docker.sock", "rootless-docker"),
        ("linux", "docker-desktop", "unix:///home/a/.docker/desktop/docker.sock", "docker-desktop"),
        ("linux", "rancher-desktop", "unix:///home/a/.rd/docker.sock", "rancher-desktop"),
        ("linux", "default", "unix:///var/run/docker.sock", "system-docker"),
    ],
)
def test_context_and_socket_select_expected_provider_kind(
    family, context, endpoint, expected,
):
    assert docker_runtime._kind_from_context(family, context, endpoint) == expected


def test_unknown_context_fails_closed_instead_of_switching(monkeypatch):
    monkeypatch.setattr(
        docker_runtime, "_docker_context",
        lambda _docker: ("company-remote", "tcp://docker.example:2376"),
    )
    monkeypatch.setattr(
        docker_runtime, "_installed_providers_without_context",
        lambda _family: [docker_runtime.Provider("orbstack", "OrbStack", ("orb", "start"))],
    )
    monkeypatch.setattr(docker_runtime, "docker_ready", lambda _docker: False)
    monkeypatch.setattr(docker_runtime, "_which", lambda name: "/usr/bin/docker" if name == "docker" else None)

    result = docker_runtime.ensure_docker(family="macos")

    assert result.code == "docker_context_selection_required"
    assert result.requires_user_action is True
    assert result.attempted_start is False


def test_remote_docker_host_never_starts_or_installs_local_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(
        docker_runtime, "_docker_context",
        lambda _docker: ("default", "tcp://remote.example:2376"),
    )
    monkeypatch.setattr(docker_runtime, "_installed_providers_without_context", lambda _family: [])
    monkeypatch.setattr(docker_runtime, "_which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(docker_runtime, "docker_ready", lambda _docker: False)
    monkeypatch.setattr(
        docker_runtime, "_run",
        lambda *_args, **_kwargs: pytest.fail("remote context must fail closed"),
    )

    result = docker_runtime.ensure_docker(
        allow_install=True, home=tmp_path, family="linux",
    )

    assert result.code == "docker_context_selection_required"
    assert result.attempted_install is False
    assert result.attempted_start is False


@pytest.mark.parametrize(
    ("family", "context", "endpoint", "expected"),
    [
        ("macos", "orbstack", "unix:///Users/a/.orbstack/run/docker.sock", "orbstack"),
        ("windows", "desktop-linux", "npipe:////./pipe/dockerDesktopLinuxEngine", "docker-desktop"),
        ("wsl", "desktop-linux", "unix:///var/run/docker.sock", "docker-desktop"),
        ("linux", "rootless", "unix:///run/user/1000/docker.sock", "rootless-docker"),
        ("linux", "default", "unix:///var/run/docker.sock", "system-docker"),
    ],
)
def test_discovery_prioritizes_current_context_provider(
    monkeypatch, family, context, endpoint, expected,
):
    provider = docker_runtime.Provider(expected, "selected", ("safe-start",))
    monkeypatch.setattr(
        docker_runtime, "_docker_context", lambda _docker: (context, endpoint),
    )
    monkeypatch.setattr(
        docker_runtime, "_mac_provider", lambda kind: provider if kind == expected else None,
    )
    monkeypatch.setattr(
        docker_runtime, "_windows_program",
        lambda kind: "C:/Program Files/Docker/Docker/Docker Desktop.exe" if kind == expected else None,
    )
    monkeypatch.setattr(
        docker_runtime, "_linux_provider", lambda kind: provider if kind == expected else None,
    )

    selected, selected_context, kind = docker_runtime.discover_provider(
        "/usr/bin/docker", family=family,
    )

    assert selected is not None
    assert selected.key == expected
    assert selected_context == context
    assert kind == expected


def test_standalone_docker_cli_without_engine_asks_before_install(tmp_path, monkeypatch):
    installer = docker_runtime.Installer(
        docker_runtime.Provider("system-docker", "Docker Engine", ("start",)),
        "Docker Engine", ("trusted-installer", "install"), "trusted source",
    )
    monkeypatch.setattr(docker_runtime, "_which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(docker_runtime, "docker_ready", lambda _docker: False)
    monkeypatch.setattr(
        docker_runtime, "discover_provider", lambda *_args, **_kwargs: (None, "default", "system-docker"),
    )
    monkeypatch.setattr(docker_runtime, "_installed_providers_without_context", lambda _family: [])
    monkeypatch.setattr(docker_runtime, "recommended_installer", lambda _family: installer)
    monkeypatch.setattr(
        docker_runtime, "_run",
        lambda *_args, **_kwargs: pytest.fail("install must wait for approval"),
    )

    result = docker_runtime.ensure_docker(home=tmp_path, family="linux")

    assert result.code == "docker_install_confirmation_required"
    assert result.attempted_install is False


def test_wsl_default_socket_uniquely_maps_to_installed_desktop(
    tmp_path, monkeypatch,
):
    desktop = docker_runtime.Provider(
        "docker-desktop", "Docker Desktop", ("Docker Desktop.exe",),
    )
    readiness = iter((False, False, True))
    calls = []
    monkeypatch.setattr(docker_runtime, "_which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(
        docker_runtime, "_docker_context",
        lambda _docker: ("default", "unix:///var/run/docker.sock"),
    )
    monkeypatch.setattr(
        docker_runtime, "_provider_for_kind",
        lambda _family, kind: None if kind == "system-docker" else pytest.fail("unexpected kind"),
    )
    monkeypatch.setattr(
        docker_runtime, "_installed_providers_without_context", lambda _family: [desktop],
    )
    monkeypatch.setattr(docker_runtime, "docker_ready", lambda _docker: next(readiness))
    monkeypatch.setattr(docker_runtime, "READINESS_DELAYS", (0.0,))
    monkeypatch.setattr(
        docker_runtime, "_run", lambda argv, **_kwargs: calls.append(argv) or _done(argv),
    )

    result = docker_runtime.ensure_docker(home=tmp_path, family="wsl")

    assert result.ready is True
    assert result.provider == "docker-desktop"
    assert calls == [["Docker Desktop.exe"]]


def test_wsl_default_socket_with_multiple_integrations_asks_user(
    tmp_path, monkeypatch,
):
    providers = [
        docker_runtime.Provider("docker-desktop", "Docker Desktop", ("docker-desktop",)),
        docker_runtime.Provider("rancher-desktop", "Rancher Desktop", ("rancher",)),
    ]
    calls = []
    monkeypatch.setattr(docker_runtime, "_which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(docker_runtime, "docker_ready", lambda _docker: False)
    monkeypatch.setattr(
        docker_runtime, "_docker_context",
        lambda _docker: ("default", "unix:///var/run/docker.sock"),
    )
    monkeypatch.setattr(docker_runtime, "_provider_for_kind", lambda _family, _kind: None)
    monkeypatch.setattr(
        docker_runtime, "_installed_providers_without_context", lambda _family: providers,
    )
    monkeypatch.setattr(
        docker_runtime, "_run", lambda argv, **_kwargs: calls.append(argv) or _done(argv),
    )

    result = docker_runtime.ensure_docker(home=tmp_path, family="wsl")

    assert result.code == "docker_context_selection_required"
    assert result.requires_user_action is True
    assert result.attempted_start is False
    assert calls == []


@pytest.mark.parametrize("family", ["macos", "windows", "wsl", "linux"])
def test_uninstalled_environment_only_asks_without_running_installer(
    tmp_path, monkeypatch, family,
):
    calls = []
    provider = docker_runtime.Provider("recommended", "Recommended Docker", ("start",))
    installer = docker_runtime.Installer(
        provider, "Recommended Docker", ("trusted-installer", "install"),
        "trusted source",
    )
    monkeypatch.setattr(docker_runtime, "_which", lambda _name: None)
    monkeypatch.setattr(docker_runtime, "_installed_without_context", lambda _family: None)
    monkeypatch.setattr(docker_runtime, "recommended_installer", lambda _family: installer)
    monkeypatch.setattr(docker_runtime, "_run", lambda *args, **kwargs: calls.append(args) or _done())

    result = docker_runtime.ensure_docker(home=tmp_path, family=family)

    assert result.code == "docker_install_confirmation_required"
    assert result.install_required is True
    assert result.attempted_install is False
    assert calls == []


@pytest.mark.parametrize(
    ("family", "tool", "forbidden"),
    [
        ("macos", "brew", {"curl", "sh", "bash"}),
        ("windows", "winget.exe", {"curl", "powershell", "cmd"}),
        ("wsl", "winget.exe", {"curl", "powershell", "cmd", "sh"}),
        ("linux", "apt-get", {"curl", "sh", "bash"}),
    ],
)
def test_recommended_installers_use_structured_trusted_argv(
    monkeypatch, family, tool, forbidden,
):
    tools = {
        tool: f"/trusted/{tool}",
        "systemctl": "/usr/bin/systemctl",
        "pkexec": "/usr/bin/pkexec",
    }
    monkeypatch.setattr(docker_runtime, "_which", tools.get)
    monkeypatch.setattr(docker_runtime, "_mac_provider", lambda _kind: None)
    monkeypatch.setattr(docker_runtime, "_windows_program", lambda _kind: None)
    monkeypatch.setattr(docker_runtime.os, "geteuid", lambda: 1000, raising=False)

    installer = docker_runtime.recommended_installer(family)

    assert installer is not None
    assert installer.argv[0] == f"/trusted/{tool}" or installer.argv[0] == "/usr/bin/pkexec"
    assert not forbidden.intersection(installer.argv)
    assert all(" " not in item or item.startswith("C:/") for item in installer.argv)
    if family in {"windows", "wsl"}:
        assert "--accept-source-agreements" not in installer.argv
        assert "--accept-package-agreements" not in installer.argv


def test_installed_orbstack_is_started_once_and_waited_until_ready(tmp_path, monkeypatch):
    readiness = iter((False, False, False, True))
    starts = []
    provider = docker_runtime.Provider("orbstack", "OrbStack", ("/usr/bin/orb", "start"))
    monkeypatch.setattr(docker_runtime, "_which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(docker_runtime, "docker_ready", lambda _docker: next(readiness))
    monkeypatch.setattr(
        docker_runtime, "discover_provider", lambda *_args, **_kwargs: (provider, "orbstack", "orbstack"),
    )
    monkeypatch.setattr(docker_runtime, "READINESS_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(
        docker_runtime, "_run",
        lambda argv, **_kwargs: starts.append(argv) or _done(argv),
    )

    result = docker_runtime.ensure_docker(home=tmp_path, family="macos")

    assert result.ready is True
    assert result.provider == "orbstack"
    assert starts == [["/usr/bin/orb", "start"]]


def test_wsl_desktop_installed_without_cli_in_path_starts_automatically(
    tmp_path, monkeypatch,
):
    provider = docker_runtime.Provider(
        "docker-desktop", "Docker Desktop",
        ("/mnt/c/Program Files/Docker/Docker/Docker Desktop.exe",),
    )
    calls = []
    monkeypatch.setattr(docker_runtime, "_which", lambda _name: None)
    monkeypatch.setattr(
        docker_runtime, "_installed_without_context", lambda _family: provider,
    )
    monkeypatch.setattr(
        docker_runtime, "_installed_docker_path",
        lambda family, kind: (
            "/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe"
            if (family, kind) == ("wsl", "docker-desktop") else None
        ),
    )
    monkeypatch.setattr(docker_runtime, "docker_ready", lambda _docker: True)
    monkeypatch.setattr(docker_runtime, "READINESS_DELAYS", (0.0,))
    monkeypatch.setattr(
        docker_runtime, "_run",
        lambda argv, **_kwargs: calls.append(argv) or _done(argv),
    )

    result = docker_runtime.ensure_docker(home=tmp_path, family="wsl")

    assert result.ready is True
    assert result.attempted_start is True
    assert result.install_required is False
    assert calls == [[
        "/mnt/c/Program Files/Docker/Docker/Docker Desktop.exe",
    ]]


def test_concurrent_runs_share_one_start_and_other_waits(tmp_path, monkeypatch):
    provider = docker_runtime.Provider("colima", "Colima", ("colima", "start"))
    running = threading.Event()
    initial = threading.Barrier(2)
    starts = []
    ready_calls = {"count": 0}
    ready_lock = threading.Lock()

    def ready(_docker):
        with ready_lock:
            ready_calls["count"] += 1
            count = ready_calls["count"]
        if count <= 2:
            initial.wait(timeout=3)
            return False
        return running.is_set()

    def run(argv, **_kwargs):
        starts.append(argv)
        running.set()
        return _done(argv)

    monkeypatch.setattr(docker_runtime, "_which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(docker_runtime, "docker_ready", ready)
    monkeypatch.setattr(
        docker_runtime, "discover_provider", lambda *_args, **_kwargs: (provider, "colima", "colima"),
    )
    monkeypatch.setattr(docker_runtime, "READINESS_DELAYS", (0.0,))
    monkeypatch.setattr(docker_runtime, "_run", run)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: docker_runtime.ensure_docker(home=tmp_path, family="macos"),
            range(2),
        ))

    assert all(result.ready for result in results)
    assert starts == [["colima", "start"]]


def test_permission_rejection_requires_user_action_without_retry(tmp_path, monkeypatch):
    provider = docker_runtime.Provider(
        "system-docker", "Docker Engine", ("sudo", "-n", "systemctl", "start", "docker"),
    )
    calls = []
    monkeypatch.setattr(docker_runtime, "_which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(docker_runtime, "docker_ready", lambda _docker: False)
    monkeypatch.setattr(
        docker_runtime, "discover_provider", lambda *_args, **_kwargs: (provider, "default", "system-docker"),
    )
    monkeypatch.setattr(
        docker_runtime, "_run",
        lambda argv, **_kwargs: calls.append(argv) or _done(argv, code=1, err="sudo: a password is required"),
    )

    result = docker_runtime.ensure_docker(home=tmp_path, family="linux")

    assert result.code == "docker_start_rejected"
    assert result.requires_user_action is True
    assert len(calls) == 1


def test_start_timeout_is_bounded_and_not_retried(tmp_path, monkeypatch):
    provider = docker_runtime.Provider("orbstack", "OrbStack", ("orb", "start"))
    calls = []
    monkeypatch.setattr(docker_runtime, "_which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(docker_runtime, "docker_ready", lambda _docker: False)
    monkeypatch.setattr(
        docker_runtime, "discover_provider", lambda *_args, **_kwargs: (provider, "orbstack", "orbstack"),
    )

    def timeout(argv, **_kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 20)

    monkeypatch.setattr(docker_runtime, "_run", timeout)
    result = docker_runtime.ensure_docker(home=tmp_path, family="macos")

    assert result.code == "docker_start_command_timeout"
    assert result.requires_user_action is False
    assert calls == [["orb", "start"]]


def test_approved_install_verifies_version_then_starts_once(tmp_path, monkeypatch):
    provider = docker_runtime.Provider("orbstack", "OrbStack", ("orb", "start"))
    installer = docker_runtime.Installer(
        provider, "OrbStack", ("brew", "install", "--cask", "orbstack"),
        "Homebrew official cask",
    )
    calls = []
    monkeypatch.setattr(docker_runtime, "_which", lambda _name: None)
    monkeypatch.setattr(docker_runtime, "_installed_without_context", lambda _family: None)
    monkeypatch.setattr(docker_runtime, "recommended_installer", lambda _family: installer)
    monkeypatch.setattr(docker_runtime, "_installed_docker_path", lambda *_args: "/trusted/docker")
    monkeypatch.setattr(docker_runtime, "_docker_version", lambda path: path == "/trusted/docker")
    monkeypatch.setattr(docker_runtime, "discover_provider", lambda *_args, **_kwargs: (provider, "orbstack", "orbstack"))
    monkeypatch.setattr(docker_runtime, "docker_ready", lambda _docker: True)
    monkeypatch.setattr(docker_runtime, "READINESS_DELAYS", (0.0,))
    monkeypatch.setattr(
        docker_runtime, "_run", lambda argv, **_kwargs: calls.append(argv) or _done(argv),
    )

    result = docker_runtime.ensure_docker(
        allow_install=True, home=tmp_path, family="macos",
    )

    assert result.ready is True
    assert result.attempted_install is True
    assert calls == [
        ["brew", "install", "--cask", "orbstack"],
        ["orb", "start"],
    ]


def test_post_install_verification_rejects_untrusted_docker_path(monkeypatch):
    monkeypatch.setattr(docker_runtime, "_which", lambda name: "/tmp/untrusted/docker" if name == "docker" else None)
    monkeypatch.setattr(docker_runtime, "_exists", lambda _path: False)

    assert docker_runtime._installed_docker_path("linux", "system-docker") is None


def test_start_coordination_timeout_fails_closed_without_command(tmp_path, monkeypatch):
    class BusyLock:
        def acquire(self, *, timeout):
            assert timeout == docker_runtime.LOCK_WAIT_SECONDS
            return False

        def release(self):
            pytest.fail("unacquired lock must not be released")

    calls = []
    monkeypatch.setattr(docker_runtime, "_THREAD_LOCK", BusyLock())
    monkeypatch.setattr(docker_runtime, "_which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(docker_runtime, "docker_ready", lambda _docker: False)
    monkeypatch.setattr(
        docker_runtime, "_run",
        lambda argv, **_kwargs: calls.append(argv) or _done(argv),
    )

    result = docker_runtime.ensure_docker(home=tmp_path, family="linux")

    assert result.code == "docker_start_lock_failed"
    assert result.ready is False
    assert result.attempted_start is False
    assert result.attempted_install is False
    assert calls == []
