import csv
import io
import subprocess
from pathlib import Path

import pytest

from dradar import grok_probe as g

IMAGE = "sha256:" + "a" * 64


def result(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def test_resolved_daemon_is_not_overridden_by_context_environment(monkeypatch):
    def run(argv, **kwargs):
        assert "DOCKER_CONTEXT" not in kwargs["env"]
        assert "DOCKER_HOST" not in kwargs["env"]
        assert kwargs["env"]["HOME"] == "docker-config-home"
        return result()

    monkeypatch.setattr(g.subprocess, "run", run)
    g._run(
        ["docker", "--host", "unix:///selected", "info"],
        env={
            "DOCKER_CONTEXT": "other",
            "DOCKER_HOST": "tcp://other",
            "HOME": "docker-config-home",
        },
    )


@pytest.mark.parametrize(
    "endpoint,arch",
    [
        ("unix:///var/run/docker.sock", "amd64"),
        ("unix:///Users/example/.orbstack/run/docker.sock", "aarch64"),
        ("npipe:////./pipe/dockerDesktopLinuxEngine", "x86_64"),
    ],
)
def test_daemon_selection_matches_supported_local_runtime(monkeypatch, endpoint, arch):
    monkeypatch.setattr(g.shutil, "which", lambda *a, **k: "docker")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return result(stdout="linux " + arch)

    monkeypatch.setattr(g, "_run", run)
    command, selected = g.local_daemon({"DOCKER_HOST": endpoint})
    assert command == ["docker", "--host", endpoint]
    assert selected in g.LINUX_SHA256
    assert calls[0][:3] == command


@pytest.mark.parametrize(
    "endpoint",
    ["ssh://remote", "tcp://127.0.0.1:2375", "https://daemon", "unix:///a\ninvalid"],
)
def test_remote_daemon_remains_rejected_as_in_shared_oauth_runtime(
    monkeypatch, endpoint
):
    monkeypatch.setattr(g.shutil, "which", lambda *a, **k: "docker")
    monkeypatch.setattr(
        g, "_run", lambda *a, **k: pytest.fail("must not contact rejected daemon")
    )
    with pytest.raises(g.ProbeUnavailable, match="local Linux"):
        g.local_daemon({"DOCKER_HOST": endpoint})


def test_context_takes_precedence_over_ambient_docker_host(monkeypatch):
    monkeypatch.setattr(g.shutil, "which", lambda *a, **k: "docker")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return result(stdout="unix:///chosen" if "context" in argv else "linux arm64")

    monkeypatch.setattr(g, "_run", run)
    command, arch = g.local_daemon(
        {"DOCKER_CONTEXT": "chosen", "DOCKER_HOST": "tcp://ignored"}
    )
    assert calls[0][1:4] == ["context", "inspect", "chosen"]
    assert command == ["docker", "--host", "unix:///chosen"]
    assert arch == "aarch64"


@pytest.mark.parametrize("override", ["http://docker-reachable:7897", "direct"])
def test_existing_container_proxy_override_is_authoritative(override):
    env = g._proxy_env(
        {
            "HTTPS_PROXY": "http://127.0.0.1:1234",
            "all_proxy": "socks5://localhost:1234",
            "DRADAR_CONTAINER_HTTP_PROXY": override,
            "DRADAR_CONTAINER_NO_PROXY": "fixture.local",
        }
    )
    assert env["NO_PROXY"] == "fixture.local"
    if override == "direct":
        assert "HTTPS_PROXY" not in env and "all_proxy" not in env
    else:
        assert env["HTTPS_PROXY"] == env["all_proxy"] == override


@pytest.mark.parametrize("failure", [None, "timeout", "cancel"])
def test_probe_binds_whole_coordination_directory_and_cleans_only_own_container(
    tmp_path, monkeypatch, failure
):
    parent = tmp_path / "path with spaces,comma"
    parent.mkdir()
    auth = parent / "custom.json"
    auth.write_text("{}")
    monkeypatch.setattr(
        g,
        "local_daemon",
        lambda env: (["docker", "--host", "unix:///chosen"], "aarch64"),
    )
    monkeypatch.setattr(g, "_prepare_image", lambda *a: IMAGE)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "run" in argv:
            if "--entrypoint" in argv:
                return result(stdout="1000:1000")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 55)
            if failure == "cancel":
                raise KeyboardInterrupt
            return result(stdout="grok-4.6")
        if "inspect" in argv:
            return result(1, stderr="Error: No such container: fixture")
        return result()

    monkeypatch.setattr(g, "_run", run)
    env = {
        "HOME": "original-docker-config-home",
        "GROK_AUTH": "foreign-account",
        "XAI_API_KEY": "secret-api",
        "HTTPS_PROXY": "http://user:pass@127.0.0.1:7897",
    }
    if failure:
        with pytest.raises(
            subprocess.TimeoutExpired if failure == "timeout" else KeyboardInterrupt
        ):
            g.run_probe(auth, tmp_path, env)
    else:
        assert g.run_probe(auth, tmp_path, env).stdout == "grok-4.6"
    metadata, _ = calls[0]
    assert metadata[metadata.index("--network") + 1] == "none"
    assert metadata[metadata.index("--mount") + 1].endswith(",readonly")
    argv, kwargs = calls[1]
    assert argv[argv.index("--user") + 1] == "1000:1000"
    name = argv[argv.index("--name") + 1]
    assert name.startswith("dradar-grok-probe-")
    mount = next(csv.reader(io.StringIO(argv[argv.index("--mount") + 1])))
    assert mount == ["type=bind", f"source={parent.resolve()}", f"target={g.AUTH_ROOT}"]
    assert f"GROK_AUTH_PATH={g.AUTH_ROOT}/custom.json" in argv
    assert "HTTPS_PROXY" in argv
    assert all(
        secret not in " ".join(argv)
        for secret in ("secret-api", "foreign-account", "user:pass")
    )
    assert kwargs["env"]["HTTPS_PROXY"] == "http://user:pass@host.docker.internal:7897"
    assert kwargs["env"]["HOME"] == "original-docker-config-home"
    assert calls[2][0][-3:] == ["rm", "--force", name]
    assert calls[3][0][-3:] == [name, "--format", "{{.State.Running}}"]
    assert calls[4][0][-3:] == ["rm", "--force", name + "-owner"]
    assert "--read-only" in argv and "--init" in argv
    assert "timeout --kill-after=5s 40s /opt/grok models" in argv[-1]
    assert "chown" not in argv[-1] and "rm " not in argv[-1]


def test_daemon_failure_is_not_misreported_as_confirmed_cleanup(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    monkeypatch.setattr(g, "local_daemon", lambda env: (["docker"], "aarch64"))
    monkeypatch.setattr(g, "_prepare_image", lambda *a: IMAGE)
    monkeypatch.setattr(
        g, "_run", lambda *a, **k: result(1, stderr="Cannot connect to daemon")
    )
    with pytest.raises(g.ProbeUnavailable, match="cleanup is unconfirmed"):
        g.run_probe(auth, tmp_path, {})


def test_warm_image_cache_does_not_download_or_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "_image_id", lambda *a: IMAGE)
    monkeypatch.setattr(
        g, "_download_binary", lambda *a: pytest.fail("warm cache downloaded")
    )
    assert g._prepare_image(["docker"], "aarch64", tmp_path / "auth.json", {}) == IMAGE
    assert not (tmp_path / "runtime").exists()


def test_image_build_context_has_no_credentials(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text("never-copy-this")
    ids = iter([None, None, IMAGE])
    monkeypatch.setattr(g, "_image_id", lambda *a: next(ids))

    def download(path, *a):
        path.write_text("inert-pinned-binary")

    monkeypatch.setattr(g, "_download_binary", download)

    def build(argv, **kwargs):
        context = Path(argv[-1])
        assert {p.name for p in context.iterdir()} == {
            "grok",
            "ca.pem",
            "Dockerfile",
        }
        assert "never-copy-this" not in "".join(
            p.read_text(errors="replace") for p in context.iterdir()
        )
        assert kwargs["timeout"] == 300
        return result()

    monkeypatch.setattr(g, "_run", build)
    assert g._prepare_image(["docker"], "aarch64", auth, {}) == IMAGE


def test_preparation_failure_never_starts_credential_container(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    monkeypatch.setattr(g, "local_daemon", lambda env: (["docker"], "aarch64"))

    def fail(*a):
        raise g.ProbeUnavailable("Grok readiness image build failed")

    monkeypatch.setattr(g, "_prepare_image", fail)
    monkeypatch.setattr(g, "_run", lambda *a, **k: pytest.fail("must not create probe"))
    with pytest.raises(g.ProbeUnavailable, match="image build failed"):
        g.run_probe(auth, tmp_path, {})


def test_probe_and_actual_task_runtime_pin_identical_official_binaries():
    import ast

    source = Path(g.__file__).with_name("pier_grok.py").read_text()
    tree = ast.parse(source)
    found = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"GROK_CLI_VERSION", "GROK_LINUX_SHA256"}
    }
    assert found["GROK_CLI_VERSION"] == g.VERSION
    assert found["GROK_LINUX_SHA256"] == g.LINUX_SHA256


@pytest.mark.parametrize(
    "owner", ["", "1000", "-1:0", "0:4294967295", "root:root", "1000:1000\n1:1"]
)
def test_invalid_daemon_owner_fails_before_native_auth(tmp_path, monkeypatch, owner):
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    monkeypatch.setattr(g, "local_daemon", lambda env: (["docker"], "aarch64"))
    monkeypatch.setattr(g, "_prepare_image", lambda *a: IMAGE)

    def run(argv, **kwargs):
        if "run" in argv:
            assert "--entrypoint" in argv, "native auth must not start"
            return result(stdout=owner)
        return result(1, stderr="No such container")

    monkeypatch.setattr(g, "_run", run)
    with pytest.raises(g.ProbeUnavailable, match="directory ownership"):
        g.run_probe(auth, tmp_path, {})
