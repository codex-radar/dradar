"""Immutable Pier egress image and host-proxy bridge contracts."""

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from dradar import egress, pier_sitecustomize


@pytest.fixture(autouse=True)
def _host_prepare_is_not_nested(monkeypatch):
    monkeypatch.setattr(egress, "_running_in_container", lambda: False)


_REAL_ENSURE_IMAGE = egress.ensure_egress_proxy_image
_REAL_PREPARE_RUNTIME = egress.prepare_egress_proxy_runtime
_TEST_IMAGE = "sha256:" + "a" * 64


def test_image_override_must_pin_the_official_repository(monkeypatch):
    monkeypatch.setenv(
        egress.EGRESS_PROXY_IMAGE_OVERRIDE_ENV,
        "ghcr.io/example/other@sha256:" + "a" * 64,
    )

    with pytest.raises(egress.EgressProxyError, match="official image repository"):
        egress.egress_proxy_image()


def test_legacy_mode_is_an_explicit_one_release_escape_hatch(monkeypatch):
    monkeypatch.setenv(
        egress.EGRESS_PROXY_MODE_ENV, egress.EGRESS_PROXY_LEGACY_MODE,
    )

    assert egress.egress_proxy_image() is None
    assert egress.pier_egress_environment() == {}


def test_loopback_http_proxy_is_bridged_into_docker(monkeypatch):
    monkeypatch.setattr(
        egress,
        "provider_subprocess_env", lambda: pytest.fail("not used for container config"),
    )
    monkeypatch.setenv(
        egress.DRADAR_HTTP_PROXY_ENV,
        "http://user:p%40ss@127.0.0.1:39127",
    )
    monkeypatch.setenv(egress.DRADAR_NO_PROXY_ENV, "localhost,127.0.0.1")

    runtime = egress.pier_egress_environment(_TEST_IMAGE)

    assert runtime["DRADAR_EGRESS_UPSTREAM_HOST"] == "host.docker.internal"
    assert runtime["DRADAR_EGRESS_UPSTREAM_PORT"] == "39127"
    assert runtime["DRADAR_EGRESS_UPSTREAM_USERNAME"] == "user"
    assert runtime["DRADAR_EGRESS_UPSTREAM_PASSWORD"] == "p@ss"
    assert runtime["DRADAR_EGRESS_BUILD_PROXY"] == (
        "http://user:p%40ss@host.docker.internal:39127"
    )


def test_no_proxy_configuration_does_not_invent_one(monkeypatch):
    for name in (
        egress.DRADAR_HTTP_PROXY_ENV, "HTTPS_PROXY", "https_proxy",
        "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)

    runtime = egress.pier_egress_environment(_TEST_IMAGE)

    assert runtime == {"DRADAR_EGRESS_PROXY_IMAGE": _TEST_IMAGE}


def test_remote_proxy_keeps_the_users_own_host_and_port(monkeypatch):
    monkeypatch.setenv(
        egress.DRADAR_HTTP_PROXY_ENV,
        "http://proxy.corp.example:43128",
    )

    runtime = egress.pier_egress_environment(_TEST_IMAGE)

    assert runtime["DRADAR_EGRESS_UPSTREAM_HOST"] == "proxy.corp.example"
    assert runtime["DRADAR_EGRESS_UPSTREAM_PORT"] == "43128"
    assert runtime["DRADAR_EGRESS_BUILD_PROXY"] == (
        "http://proxy.corp.example:43128"
    )


def test_container_proxy_override_does_not_change_host_proxy(monkeypatch):
    monkeypatch.setenv(
        egress.DRADAR_HTTP_PROXY_ENV,
        "http://127.0.0.1:43128",
    )
    monkeypatch.setenv(
        egress.DRADAR_CONTAINER_HTTP_PROXY_ENV,
        "http://docker-proxy.example:18080",
    )

    runtime = egress.pier_egress_environment(_TEST_IMAGE)
    download_proxy = egress._download_proxy_settings(
        "https://github.com/example", dict(egress.os.environ),
    )

    assert runtime["DRADAR_EGRESS_UPSTREAM_HOST"] == "docker-proxy.example"
    assert runtime["DRADAR_EGRESS_UPSTREAM_PORT"] == "18080"
    assert download_proxy == ("http://127.0.0.1:43128", False)


def test_container_no_proxy_override_is_isolated(monkeypatch):
    monkeypatch.setenv(
        egress.DRADAR_CONTAINER_HTTP_PROXY_ENV,
        "http://docker-proxy.example:18080",
    )
    monkeypatch.setenv(egress.DRADAR_NO_PROXY_ENV, "host-only.example")
    monkeypatch.setenv(
        egress.DRADAR_CONTAINER_NO_PROXY_ENV,
        "container-only.example",
    )

    runtime = egress.pier_egress_environment(_TEST_IMAGE)

    assert runtime["DRADAR_EGRESS_BUILD_NO_PROXY"] == "container-only.example"


def test_prepare_rejects_loopback_proxy_with_container_buildx(monkeypatch):
    monkeypatch.setenv(
        egress.DRADAR_HTTP_PROXY_ENV,
        "http://127.0.0.1:43128",
    )
    monkeypatch.setattr(
        egress, "ensure_egress_proxy_image", lambda *_args, **_kwargs: _TEST_IMAGE,
    )
    monkeypatch.setattr(
        egress,
        "_active_buildx_driver",
        lambda _docker: "docker-container",
    )

    with pytest.raises(egress.EgressProxyError) as caught:
        _REAL_PREPARE_RUNTIME("docker")

    hint = str(caught.value)
    assert "buildx docker-container" in hint
    assert egress.DRADAR_CONTAINER_HTTP_PROXY_ENV in hint
    assert "<docker-reachable-host>:<port>" in hint


def test_prepare_accepts_explicit_docker_reachable_proxy(monkeypatch):
    monkeypatch.setenv(
        egress.DRADAR_HTTP_PROXY_ENV,
        "http://127.0.0.1:43128",
    )
    monkeypatch.setenv(
        egress.DRADAR_CONTAINER_HTTP_PROXY_ENV,
        "http://docker-proxy.example:18080",
    )
    monkeypatch.setattr(
        egress, "ensure_egress_proxy_image", lambda *_args, **_kwargs: _TEST_IMAGE,
    )
    monkeypatch.setattr(
        egress,
        "_active_buildx_driver",
        lambda _docker: "docker-container",
    )

    runtime = _REAL_PREPARE_RUNTIME("docker")

    assert runtime["DRADAR_EGRESS_UPSTREAM_HOST"] == "docker-proxy.example"


def test_active_buildx_driver_reads_selected_json_record(monkeypatch):
    output = (
        '{"Current":false,"Driver":"docker","Name":"default"}\n'
        '{"Current":true,"Driver":"docker-container","Name":"selected"}'
    )
    monkeypatch.setattr(
        egress.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=output, stderr="",
        ),
    )

    assert egress._active_buildx_driver("docker") == "docker-container"


def test_usable_standard_http_proxy_wins_over_unrelated_socks_proxy(monkeypatch):
    monkeypatch.delenv(egress.DRADAR_HTTP_PROXY_ENV, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:39081")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:43128")

    runtime = egress.pier_egress_environment(_TEST_IMAGE)

    assert runtime["DRADAR_EGRESS_UPSTREAM_HOST"] == "proxy.example"
    assert runtime["DRADAR_EGRESS_UPSTREAM_PORT"] == "43128"


def test_release_download_uses_standard_proxy_contract_without_rewriting_it():
    assert egress._download_proxy_settings(
        "https://github.com/example",
        {"HTTPS_PROXY": "http://proxy.example:43128", "NO_PROXY": "github.com"},
    ) == (None, True)


def test_release_download_honors_dradar_no_proxy():
    assert egress._download_proxy_settings(
        "https://github.com/example",
        {
            egress.DRADAR_HTTP_PROXY_ENV: "http://127.0.0.1:43128",
            egress.DRADAR_NO_PROXY_ENV: ".github.com,localhost",
        },
    ) == (None, False)


def test_socks_proxy_fails_before_claim_or_model_start(monkeypatch):
    monkeypatch.setenv(
        egress.DRADAR_HTTP_PROXY_ENV, "socks5://127.0.0.1:39081",
    )

    with pytest.raises(egress.EgressProxyError, match="HTTP upstream proxy"):
        egress.pier_egress_environment(_TEST_IMAGE)


def test_missing_image_is_loaded_once_then_validated_non_root(monkeypatch):
    identities = iter([None, None])
    calls = []

    monkeypatch.setattr(egress, "_docker_arch", lambda _docker: "arm64")
    monkeypatch.setattr(
        egress, "_validated_release_image_id", lambda *_args: next(identities),
    )

    @contextmanager
    def unlocked():
        yield

    monkeypatch.setattr(egress, "_image_lock", unlocked)
    monkeypatch.setattr(
        egress, "_load_release_asset",
        lambda docker, arch, image: (
            calls.append((docker, arch, image)) or _TEST_IMAGE
        ),
    )

    image = _REAL_ENSURE_IMAGE("docker")

    expected_tag = egress._portable_image_tag("arm64")
    assert image == _TEST_IMAGE
    assert calls == [("docker", "arm64", expected_tag)]


def test_release_asset_uses_verified_local_docker_load(monkeypatch, tmp_path):
    archive = tmp_path / "egress.tar.gz"
    archive.write_bytes(b"verified archive")
    calls = []
    monkeypatch.setattr(egress, "_upstream_proxy_environment", lambda: {})
    monkeypatch.setattr(
        egress, "_download_release_asset", lambda *_args: archive,
    )
    monkeypatch.setattr(
        egress.subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append((command, kwargs))
            or SimpleNamespace(returncode=0, stdout="loaded", stderr="")
        ),
    )
    monkeypatch.setattr(
        egress, "_validated_release_image_id", lambda *_args: _TEST_IMAGE,
    )
    tag = egress._portable_image_tag("arm64")

    image = egress._load_release_asset("docker", "arm64", tag)

    assert image == _TEST_IMAGE
    assert calls[0][0] == ["docker", "load", "--input", str(archive)]
    assert calls[0][1]["timeout"] == 300


def test_pull_failure_is_sanitized(monkeypatch):
    monkeypatch.setenv(
        egress.EGRESS_PROXY_IMAGE_OVERRIDE_ENV,
        f"{egress.EGRESS_PROXY_IMAGE_REPOSITORY}@sha256:" + "b" * 64,
    )
    monkeypatch.setattr(egress, "_docker_arch", lambda _docker: "arm64")
    monkeypatch.setattr(egress, "_docker_image_details", lambda *_args: None)

    @contextmanager
    def unlocked():
        yield

    monkeypatch.setattr(egress, "_image_lock", unlocked)
    monkeypatch.setattr(
        egress.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="unauthorized token=do-not-print",
        ),
    )

    with pytest.raises(egress.EgressProxyError) as caught:
        _REAL_ENSURE_IMAGE("docker")

    assert "rejected the public image request" in str(caught.value)
    assert "do-not-print" not in str(caught.value)


def test_compose_uses_pinned_image_and_never_dynamic_build(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("DRADAR_EGRESS_PROXY_IMAGE", _TEST_IMAGE)
    monkeypatch.setenv("DRADAR_EGRESS_UPSTREAM_HOST", "host.docker.internal")
    monkeypatch.setenv("DRADAR_EGRESS_UPSTREAM_PORT", "39127")
    monkeypatch.setattr(pier_sitecustomize, "_running_in_container", lambda: False)
    path = tmp_path / "docker-compose-egress-proxy.json"
    allowlist = SimpleNamespace(domains=["api.openai.com"])

    pier_sitecustomize._write_docker_proxy_compose(
        path=path,
        proxy_dir=tmp_path / "must-not-exist",
        allowlist=allowlist,
        token="runtime-secret",
    )

    compose = json.loads(path.read_text())
    service = compose["services"]["pier-egress-proxy"]
    assert service["image"] == _TEST_IMAGE
    assert service["pull_policy"] == "never"
    assert "build" not in service
    assert service["environment"]["PROXY_TOKEN"] == "runtime-secret"
    assert service["extra_hosts"] == ["host.docker.internal:host-gateway"]
    assert compose["networks"]["pier-egress-internal"] == {"internal": True}
    assert not (tmp_path / "must-not-exist").exists()
    assert path.stat().st_mode & 0o077 == 0


def test_compose_keeps_exam_off_internet_and_uses_unix_socket_inside_a_container(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("DRADAR_EGRESS_PROXY_IMAGE", _TEST_IMAGE)
    monkeypatch.setattr(pier_sitecustomize, "_running_in_container", lambda: True)
    path = tmp_path / "docker-compose-egress-proxy.json"

    pier_sitecustomize._write_docker_proxy_compose(
        path=path,
        proxy_dir=tmp_path / "must-not-exist",
        allowlist=SimpleNamespace(domains=["cli-chat-proxy.grok.com"]),
        token="runtime-secret",
    )

    compose = json.loads(path.read_text())
    main = compose["services"]["main"]
    proxy = compose["services"]["pier-egress-proxy"]
    export = compose["services"]["pier-egress-sock-export"]
    forward = compose["services"]["pier-egress-forward"]
    squid_script = (tmp_path / "dradar-egress-loopback-squid.sh").read_text(
        encoding="utf-8",
    )
    export_script = (tmp_path / "dradar-proxy-export.py").read_text(encoding="utf-8")
    assert main["image"] == "${MAIN_IMAGE_NAME}"
    assert main["networks"] == ["pier-egress-internal"]
    assert "network_mode" not in main
    assert export["image"] == "python:3.12-alpine"
    assert forward["image"] == "python:3.12-alpine"
    assert export.get("pull_policy") == "never"
    assert forward.get("pull_policy") == "never"
    assert export.get("user") == "0:0"
    assert forward.get("user") == "0:0"
    assert compose["networks"]["pier-egress-internal"] == {"internal": True}
    assert proxy["networks"] == ["pier-egress-internal", "default"]
    assert "http_port 127.0.0.1:8080" in squid_script
    assert "http.sock" not in squid_script
    assert "chmod 1777" not in squid_script
    assert export["network_mode"] == "service:pier-egress-proxy"
    assert forward["network_mode"] == "service:main"
    assert "pier-egress-sock:/egress" in export["volumes"]
    assert "pier-egress-sock:/egress" in forward["volumes"]
    assert "/egress/http.sock" in export_script
    assert compose["volumes"] == {"pier-egress-sock": {}}
    assert (tmp_path / "dradar-proxy-forward.py").is_file()


def test_compose_nested_sidecar_uses_resolved_small_image(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("DRADAR_EGRESS_PROXY_IMAGE", _TEST_IMAGE)
    monkeypatch.setenv(
        pier_sitecustomize._NESTED_SIDECAR_IMAGE_ENV,
        "dradar-nested-sidecar:py3",
    )
    monkeypatch.setattr(pier_sitecustomize, "_running_in_container", lambda: True)
    path = tmp_path / "docker-compose-egress-proxy.json"

    pier_sitecustomize._write_docker_proxy_compose(
        path=path,
        proxy_dir=tmp_path / "must-not-exist",
        allowlist=SimpleNamespace(domains=["cli-chat-proxy.grok.com"]),
        token="runtime-secret",
    )

    compose = json.loads(path.read_text())
    assert compose["services"]["main"]["image"] == "${MAIN_IMAGE_NAME}"
    assert compose["services"]["pier-egress-sock-export"]["image"] == (
        "dradar-nested-sidecar:py3"
    )
    assert compose["services"]["pier-egress-forward"]["image"] == (
        "dradar-nested-sidecar:py3"
    )


def test_nested_sidecar_uses_local_alpine(monkeypatch):
    monkeypatch.setattr(
        egress, "_docker_image_exists",
        lambda _docker, image: image == egress.NESTED_SIDECAR_IMAGE,
    )
    monkeypatch.setattr(
        egress, "_pull_docker_image",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not pull")),
    )

    assert egress.ensure_nested_sidecar_image("docker", _TEST_IMAGE) == (
        egress.NESTED_SIDECAR_IMAGE
    )


def test_nested_sidecar_pulls_alpine_when_missing(monkeypatch):
    present: set[str] = set()

    def exists(_docker, image):
        return image in present

    def pull(_docker, image):
        present.add(image)
        return True

    monkeypatch.setattr(egress, "_docker_image_exists", exists)
    monkeypatch.setattr(egress, "_pull_docker_image", pull)
    monkeypatch.setattr(
        egress, "_build_python_sidecar_from_proxy", lambda *_a, **_k: False,
    )

    assert egress.ensure_nested_sidecar_image("docker", _TEST_IMAGE) == (
        egress.NESTED_SIDECAR_IMAGE
    )


def test_nested_sidecar_derives_python_from_proxy_when_hub_is_blocked(monkeypatch):
    present: set[str] = set()

    def exists(_docker, image):
        return image in present

    def build(_docker, _proxy, tag):
        present.add(tag)
        return True

    monkeypatch.setattr(egress, "_docker_image_exists", exists)
    monkeypatch.setattr(egress, "_pull_docker_image", lambda *_a, **_k: False)
    monkeypatch.setattr(egress, "_build_python_sidecar_from_proxy", build)

    assert egress.ensure_nested_sidecar_image("docker", _TEST_IMAGE) == (
        egress.NESTED_SIDECAR_LOCAL_TAG
    )


def test_nested_sidecar_fails_closed_when_no_python_image(monkeypatch):
    monkeypatch.setattr(egress, "_docker_image_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(egress, "_pull_docker_image", lambda *_a, **_k: False)
    monkeypatch.setattr(
        egress, "_build_python_sidecar_from_proxy", lambda *_a, **_k: False,
    )

    with pytest.raises(egress.EgressProxyError, match="small python image"):
        egress.ensure_nested_sidecar_image("docker", _TEST_IMAGE)


def test_prepare_injects_nested_sidecar_image_inside_a_container(monkeypatch):
    monkeypatch.setattr(egress, "_running_in_container", lambda: True)
    monkeypatch.setattr(
        egress, "ensure_egress_proxy_image",
        lambda *_args, **_kwargs: _TEST_IMAGE,
    )
    monkeypatch.setattr(
        egress, "_validate_build_proxy_compatibility", lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        egress, "ensure_nested_sidecar_image",
        lambda *_a, **_k: "python:3.12-alpine",
    )

    runtime = _REAL_PREPARE_RUNTIME("docker")

    assert runtime[egress.NESTED_SIDECAR_IMAGE_ENV] == "python:3.12-alpine"


def test_unix_forwarder_points_http_proxy_at_loopback(tmp_path: Path):
    path = tmp_path / "docker-compose-egress-proxy.json"
    path.write_text(json.dumps({
        "services": {
            "main": {},
            "pier-egress-forward": {"network_mode": "service:main"},
        },
    }))
    runtime = {
        "HTTP_PROXY": "http://agent:short-lived@pier-egress-proxy:8080",
        "HTTPS_PROXY": "http://agent:short-lived@pier-egress-proxy:8080",
    }

    pier_sitecustomize._finalize_docker_proxy_compose(path, runtime, None)

    environment = json.loads(path.read_text())["services"]["main"]["environment"]
    assert environment["HTTP_PROXY"] == "http://agent:short-lived@127.0.0.1:8080"
    assert environment["HTTPS_PROXY"] == "http://agent:short-lived@127.0.0.1:8080"


def test_pier_bootstrap_accepts_only_local_image_id_or_official_digest():
    assert pier_sitecustomize._image_is_immutable(_TEST_IMAGE)
    assert pier_sitecustomize._image_is_immutable(
        "ghcr.io/codex-radar/dradar-egress-proxy@sha256:" + "b" * 64
    )
    assert not pier_sitecustomize._image_is_immutable(
        "ghcr.io/example/other@sha256:" + "b" * 64
    )
    assert not pier_sitecustomize._image_is_immutable(
        "ghcr.io/codex-radar/dradar-egress-proxy:v1"
    )


def test_agent_build_proxy_is_added_only_when_configured(monkeypatch):
    monkeypatch.setenv(
        "DRADAR_EGRESS_BUILD_PROXY",
        "http://host.docker.internal:39127",
    )
    monkeypatch.setenv("DRADAR_EGRESS_UPSTREAM_HOST", "host.docker.internal")

    override = pier_sitecustomize._build_proxy_override()

    assert override["args"]["HTTPS_PROXY"] == (
        "http://host.docker.internal:39127"
    )
    assert override["extra_hosts"] == ["host.docker.internal=host-gateway"]


def test_runtime_proxy_token_moves_into_private_compose_environment(tmp_path):
    path = tmp_path / "docker-compose-egress-proxy.json"
    path.write_text(json.dumps({
        "services": {"main": {"networks": ["internal"]}},
    }))
    runtime = {
        "HTTP_PROXY": "http://agent:short-lived@pier-egress-proxy:8080",
        "HTTPS_PROXY": "http://agent:short-lived@pier-egress-proxy:8080",
    }
    build = {"args": {"HTTPS_PROXY": "http://host.docker.internal:43128"}}

    pier_sitecustomize._finalize_docker_proxy_compose(path, runtime, build)

    compose = json.loads(path.read_text())
    assert compose["services"]["main"]["environment"] == runtime
    assert compose["services"]["main"]["build"] == build
    assert path.stat().st_mode & 0o077 == 0


def test_runtime_probe_keeps_credentials_out_of_process_arguments(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == "port":
            return SimpleNamespace(
                returncode=0, stdout="127.0.0.1:43119\n", stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="container-id", stderr="")

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _url):
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(egress.subprocess, "run", run)
    monkeypatch.setattr(egress.httpx, "Client", Client)
    monkeypatch.setattr(egress, "provider_subprocess_env", lambda: {})
    runtime = {
        "DRADAR_EGRESS_UPSTREAM_HOST": "proxy.example",
        "DRADAR_EGRESS_UPSTREAM_PORT": "43128",
        "DRADAR_EGRESS_UPSTREAM_USERNAME": "user",
        "DRADAR_EGRESS_UPSTREAM_PASSWORD": "do-not-leak",
    }

    egress._probe_runtime_egress("docker", _TEST_IMAGE, runtime)

    run_command = calls[0][0]
    assert run_command[:4] == ["docker", "run", "--detach", "--rm"]
    assert "PROXY_TOKEN" in run_command
    assert "UPSTREAM_PROXY_PASSWORD" in run_command
    assert "do-not-leak" not in " ".join(run_command)
    assert calls[-1][0][0:3] == ["docker", "rm", "--force"]


def test_runtime_failure_explains_host_container_proxy_split(monkeypatch):
    monkeypatch.setattr(
        egress,
        "provider_subprocess_env",
        lambda: {"HTTPS_PROXY": "http://127.0.0.1:43128"},
    )

    hint = egress._runtime_proxy_failure_hint({})

    assert "host-side proxy is available" in hint
    assert egress.DRADAR_HTTP_PROXY_ENV in hint
    assert egress.DRADAR_CONTAINER_HTTP_PROXY_ENV in hint
    assert "relay" not in hint


def test_runtime_failure_rejects_guessed_hostnames_and_relays(monkeypatch):
    monkeypatch.setenv(
        egress.DRADAR_HTTP_PROXY_ENV,
        "http://127.0.0.1:43128",
    )
    runtime = {
        "DRADAR_EGRESS_UPSTREAM_HOST": "host.docker.internal",
        "DRADAR_EGRESS_UPSTREAM_PORT": "43128",
    }

    hint = egress._runtime_proxy_failure_hint(runtime)

    assert egress.DRADAR_CONTAINER_HTTP_PROXY_ENV in hint
    assert "do not guess Docker hostnames" in hint
    assert "do not guess" in hint and "relay containers" in hint


def test_runtime_failure_names_explicit_container_override(monkeypatch):
    monkeypatch.setenv(
        egress.DRADAR_CONTAINER_HTTP_PROXY_ENV,
        "http://docker-proxy.example:18080",
    )

    hint = egress._runtime_proxy_failure_hint({})

    assert egress.DRADAR_CONTAINER_HTTP_PROXY_ENV in hint
    assert "Docker bridge container" in hint
