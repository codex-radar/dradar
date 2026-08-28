"""Provider keys stay out of argv, config.json, logs, and the server."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dradar import provider_config
from dradar.providers import (
    DEEPSEEK_API_KEY_ENV,
    antigravity_settings_payload,
    create_deepseek_auth_json,
    deepseek_api_key,
    deepseek_credential_source,
    deepseek_secret_error,
    deepseek_secret_path,
    store_deepseek_api_key,
)


@pytest.fixture(autouse=True)
def _isolate_provider_environment(monkeypatch, tmp_path):
    monkeypatch.delenv(DEEPSEEK_API_KEY_ENV, raising=False)
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path / "dradar-home"))


def test_file_backed_key_is_atomic_private_and_overrides_stale_environment(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    path = store_deepseek_api_key("  file-secret  ")

    assert path == tmp_path / "secrets" / "deepseek_api_key"
    assert path.read_text() == "file-secret\n"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert deepseek_api_key() == "file-secret"
    assert deepseek_credential_source() == "file"

    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "environment-secret")
    assert deepseek_api_key() == "file-secret"
    assert deepseek_credential_source() == "file"


def test_environment_key_is_the_fallback_when_no_private_file(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "environment-secret")

    assert deepseek_api_key() == "environment-secret"
    assert deepseek_credential_source() == "environment"


def test_runtime_auth_json_is_private_and_contains_no_secret_in_its_name(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sentinel-runtime-secret")

    path = create_deepseek_auth_json(tmp_path)

    assert "sentinel-runtime-secret" not in str(path)
    assert json.loads(path.read_text()) == {
        "OPENAI_API_KEY": "sentinel-runtime-secret",
    }
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_too_broad_secret_file_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    path = deepseek_secret_path()
    path.parent.mkdir(parents=True)
    path.write_text("unsafe-secret\n")
    path.chmod(0o644)

    assert "chmod 600" in (deepseek_secret_error(path) or "")
    assert deepseek_api_key() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_secret_symlink_is_rejected_without_reading_target(tmp_path, monkeypatch):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path / "home"))
    target = tmp_path / "outside-secret"
    target.write_text("must-not-be-read\n")
    path = deepseek_secret_path()
    path.parent.mkdir(parents=True)
    path.symlink_to(target)

    assert "not a symlink" in (deepseek_secret_error(path) or "")
    assert deepseek_api_key() is None


def test_replacing_key_leaves_no_predictable_temporary_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    path = store_deepseek_api_key("first-secret")
    store_deepseek_api_key("second-secret")

    assert path.read_text() == "second-secret\n"
    assert list(path.parent.glob(".deepseek_api_key.*")) == []


def test_setup_requires_a_real_interactive_terminal(monkeypatch, capsys):
    monkeypatch.setattr(
        provider_config.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(
        provider_config.getpass,
        "getpass",
        lambda _prompt: pytest.fail("must not read a secret without a TTY"),
    )

    rc = provider_config.cmd_provider_setup(SimpleNamespace(provider="deepseek"))

    assert rc == 2
    output = capsys.readouterr().out
    assert "provider setup deepseek" in output
    assert "paste the API key into Codex/chat" in output


def test_setup_uses_hidden_input_and_never_echoes_key(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    monkeypatch.setattr(
        provider_config.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(
        provider_config.getpass,
        "getpass",
        lambda _prompt: "sentinel-provider-secret",
    )
    monkeypatch.setattr(provider_config, "_live_deepseek_status", lambda _key: 0)

    rc = provider_config.cmd_provider_setup(SimpleNamespace(provider="deepseek"))

    assert rc == 0
    assert deepseek_api_key() == "sentinel-provider-secret"
    assert "sentinel-provider-secret" not in capsys.readouterr().out


def test_setup_key_is_active_even_with_a_stale_inherited_environment(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "stale-environment-secret")
    monkeypatch.setattr(
        provider_config.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(
        provider_config.getpass,
        "getpass",
        lambda _prompt: "new-file-secret",
    )
    monkeypatch.setattr(provider_config, "_live_deepseek_status", lambda _key: 0)

    rc = provider_config.cmd_provider_setup(SimpleNamespace(provider="deepseek"))

    assert rc == 0
    assert deepseek_api_key() == "new-file-secret"
    assert deepseek_credential_source() == "file"
    output = capsys.readouterr().out
    assert "stale-environment-secret" not in output
    assert "new-file-secret" not in output


def test_setup_keeps_rejected_key_but_refuses_ready_state(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    monkeypatch.setattr(
        provider_config.sys, "stdin", SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr(
        provider_config.getpass, "getpass", lambda _prompt: "rejected-secret",
    )
    monkeypatch.setattr(provider_config, "_live_deepseek_status", lambda _key: 1)

    assert provider_config.cmd_provider_setup(
        SimpleNamespace(provider="deepseek")
    ) == 1
    assert deepseek_api_key() == "rejected-secret"
    output = capsys.readouterr().out
    assert "not ready for a task" in output
    assert "rejected-secret" not in output


def test_status_reports_source_without_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    store_deepseek_api_key("sentinel-provider-secret")

    rc = provider_config.cmd_provider_status(SimpleNamespace(provider="deepseek"))

    assert rc == 0
    output = capsys.readouterr().out
    assert str(deepseek_secret_path()) in output
    assert "sentinel-provider-secret" not in output


def test_live_status_verifies_auth_and_required_models(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    store_deepseek_api_key("sentinel-provider-secret")
    seen = {}

    def get(url, *, headers, timeout, follow_redirects):
        seen.update(
            url=url, authorization=headers["Authorization"],
            timeout=timeout, follow_redirects=follow_redirects,
        )
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"data": [
                {"id": "deepseek-v4-flash"}, {"id": "deepseek-v4-pro"},
            ]},
        )

    monkeypatch.setattr(provider_config, "_provider_httpx_get", get)
    rc = provider_config.cmd_provider_status(
        SimpleNamespace(provider="deepseek", live=True))

    assert rc == 0
    assert seen == {
        "url": "https://api.deepseek.com/models",
        "authorization": "Bearer sentinel-provider-secret",
        "timeout": 10.0,
        "follow_redirects": False,
    }
    output = capsys.readouterr().out
    assert "verified live" in output
    assert "sentinel-provider-secret" not in output


def test_live_status_distinguishes_rejected_key_from_transport_failure(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    store_deepseek_api_key("sentinel-provider-secret")
    monkeypatch.setattr(
        provider_config, "_provider_httpx_get",
        lambda *args, **kwargs: SimpleNamespace(status_code=401),
    )
    assert provider_config.cmd_provider_status(
        SimpleNamespace(provider="deepseek", live=True)) == 1
    rejected = capsys.readouterr().out
    assert "HTTP 401" in rejected
    assert "provider setup deepseek" in rejected
    assert "sentinel-provider-secret" not in rejected

    def fail(*args, **kwargs):
        raise httpx.ConnectError("sentinel-provider-secret")

    monkeypatch.setattr(provider_config, "_provider_httpx_get", fail)
    assert provider_config.cmd_provider_status(
        SimpleNamespace(provider="deepseek", live=True)) == 1
    transport = capsys.readouterr().out
    assert "network/proxy" in transport
    assert "ConnectError" in transport
    assert "sentinel-provider-secret" not in transport


@pytest.mark.parametrize(
    ("provider", "ensure_name", "auth_error_name", "version"),
    [
        ("grok", "_ensure_grok_cli", "grok_auth_error", "1.0.3"),
        ("kimi", "_ensure_kimi_cli", "kimi_auth_error", "0.36.1"),
    ],
)
def test_subscription_setup_prepares_runtime_before_requesting_interactive_oauth(
    provider, ensure_name, auth_error_name, version, monkeypatch, capsys,
):
    monkeypatch.setattr(
        provider_config.sys, "stdin", SimpleNamespace(isatty=lambda: False),
    )
    monkeypatch.setattr(provider_config, ensure_name, lambda: f"/managed/{provider}")
    monkeypatch.setattr(
        provider_config, auth_error_name, lambda *args, **kwargs: "missing OAuth",
    )

    rc = provider_config.cmd_provider_setup(SimpleNamespace(provider=provider))

    assert rc == 2
    output = capsys.readouterr().out
    assert f"CLI {version} is ready" in output
    assert f"provider setup {provider}" in output


@pytest.mark.parametrize(
    ("provider", "ensure_name", "auth_error_name", "live_name"),
    [
        ("grok", "_ensure_grok_cli", "grok_auth_error", "grok_live_error"),
        ("kimi", "_ensure_kimi_cli", "kimi_auth_error", "kimi_live_error"),
    ],
)
def test_existing_valid_subscription_is_reused_without_new_login(
    provider, ensure_name, auth_error_name, live_name, monkeypatch, capsys,
):
    monkeypatch.setattr(provider_config, ensure_name, lambda: f"/managed/{provider}")
    monkeypatch.setattr(
        provider_config, auth_error_name, lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(provider_config, live_name, lambda _cli: None)
    monkeypatch.setattr(
        provider_config.sys,
        "stdin",
        SimpleNamespace(
            isatty=lambda: pytest.fail("ready credential must not request a TTY"),
        ),
    )
    monkeypatch.setattr(
        provider_config.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("ready credential must not re-login"),
    )

    assert provider_config.cmd_provider_setup(SimpleNamespace(provider=provider)) == 0
    assert "already ready" in capsys.readouterr().out


def test_antigravity_setup_mounts_a_verified_ca_bundle_readonly(
    tmp_path, monkeypatch,
):
    executable = tmp_path / "antigravity"
    executable.write_bytes(b"official-binary")
    ca_bundle = Path(provider_config.certifi.where()).resolve()

    command, _env = provider_config._antigravity_container_command(
        "/usr/bin/docker", executable, ["models"], interactive=False,
    )

    assert "SSL_CERT_FILE=/tmp/dradar-ca-certificates.crt" in command
    mounts = [
        command[index + 1]
        for index, item in enumerate(command[:-1])
        if item == "--mount"
    ]
    assert (
        f"type=bind,source={ca_bundle.resolve()},"
        "target=/tmp/dradar-ca-certificates.crt,readonly"
    ) in mounts
    assert command[-3:] == [
        "debian:bookworm-slim", "/opt/antigravity", "models",
    ]


@pytest.mark.parametrize("port", [7890, 7897, 8080, 10809, 65535])
def test_antigravity_setup_bridges_host_loopback_proxy_into_docker(
    port, tmp_path, monkeypatch,
):
    executable = tmp_path / "antigravity"
    executable.write_bytes(b"official-binary")
    source = {
        "HTTP_PROXY": f"http://localhost:{port}",
        "HTTPS_PROXY": f"http://user:p%40ss@127.0.0.1:{port}",
        "ALL_PROXY": f"socks5://[::1]:{port}",
        "NO_PROXY": "localhost,127.0.0.1",
    }
    monkeypatch.setattr(
        provider_config,
        "provider_subprocess_env",
        lambda: source,
    )

    command, env = provider_config._antigravity_container_command(
        "/usr/bin/docker", executable, ["models"], interactive=False,
    )

    assert env["HTTP_PROXY"] == f"http://host.docker.internal:{port}"
    assert env["HTTPS_PROXY"] == (
        f"http://user:p%40ss@host.docker.internal:{port}"
    )
    assert env["ALL_PROXY"] == f"socks5://host.docker.internal:{port}"
    assert env["NO_PROXY"] == "localhost,127.0.0.1"
    add_host = command.index("--add-host")
    assert command[add_host + 1] == "host.docker.internal:host-gateway"
    assert f"http://user:p%40ss@host.docker.internal:{port}" not in command
    assert source == {
        "HTTP_PROXY": f"http://localhost:{port}",
        "HTTPS_PROXY": f"http://user:p%40ss@127.0.0.1:{port}",
        "ALL_PROXY": f"socks5://[::1]:{port}",
        "NO_PROXY": "localhost,127.0.0.1",
    }


def test_antigravity_setup_leaves_remote_proxy_outside_docker_mapping(
    tmp_path, monkeypatch,
):
    executable = tmp_path / "antigravity"
    executable.write_bytes(b"official-binary")
    source = {
        "HTTP_PROXY": "http://proxy.corp.example:43128",
        "HTTPS_PROXY": "http://proxy.corp.example:43128",
        "NO_PROXY": "localhost,127.0.0.1",
    }
    monkeypatch.setattr(
        provider_config, "provider_subprocess_env", lambda: source,
    )

    command, env = provider_config._antigravity_container_command(
        "/usr/bin/docker", executable, ["models"], interactive=False,
    )

    assert env == source
    assert "--add-host" not in command
    assert "host.docker.internal:host-gateway" not in command


@pytest.mark.parametrize("kind", ["missing", "directory", "empty", "invalid"])
def test_antigravity_setup_rejects_an_unusable_ca_bundle(
    kind, tmp_path, monkeypatch,
):
    ca_bundle = tmp_path / "ca-bundle"
    if kind == "directory":
        ca_bundle.mkdir()
    elif kind == "empty":
        ca_bundle.touch()
    elif kind == "invalid":
        ca_bundle.write_text("not a PEM certificate bundle\n", encoding="ascii")
    monkeypatch.setattr(provider_config.certifi, "where", lambda: str(ca_bundle))

    with pytest.raises(ValueError, match="trusted CA bundle"):
        provider_config._antigravity_ca_bundle()


def test_antigravity_live_check_restores_the_fail_closed_settings(
    tmp_path, monkeypatch,
):
    executable = tmp_path / "antigravity"
    executable.write_bytes(b"official-binary")
    provider_config.restore_antigravity_settings()

    def run(*_args, **_kwargs):
        settings = (
            provider_config.antigravity_auth_path()
            / "antigravity-cli" / "settings.json"
        )
        payload = json.loads(settings.read_text(encoding="utf-8"))
        payload.pop("allowNonWorkspaceAccess")
        settings.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(
                provider_config.ANTIGRAVITY_RUNTIME_MODELS.values()
            ),
            stderr="",
        )

    monkeypatch.setattr(provider_config.subprocess, "run", run)

    assert provider_config._antigravity_models_live(
        "/usr/bin/docker", executable,
    ) is None
    settings = (
        provider_config.antigravity_auth_path()
        / "antigravity-cli" / "settings.json"
    )
    assert json.loads(settings.read_text(encoding="utf-8")) == (
        antigravity_settings_payload()
    )


@pytest.mark.parametrize("provider", ["grok", "kimi"])
def test_interrupted_reauthentication_preserves_previous_credential(
    provider, tmp_path, monkeypatch,
):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path / "dradar"))
    monkeypatch.setattr(
        provider_config.sys, "stdin", SimpleNamespace(isatty=lambda: True),
    )
    if provider == "grok":
        path = provider_config.grok_auth_path()
        payload = {
            "https://auth.x.ai::client": {
                "auth_mode": "oauth",
                "key": "old-access",
                "refresh_token": "old-refresh",
            },
        }
        monkeypatch.setattr(provider_config, "_ensure_grok_cli", lambda: "/grok")
        monkeypatch.setattr(
            provider_config, "grok_live_error", lambda _cli: "revoked OAuth",
        )
    else:
        path = provider_config.kimi_auth_path()
        payload = {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "token_type": "Bearer",
        }
        monkeypatch.setattr(provider_config, "_ensure_kimi_cli", lambda: "/kimi")
        monkeypatch.setattr(
            provider_config, "kimi_live_error", lambda _cli: "revoked OAuth",
        )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload))
    if os.name != "nt":
        path.chmod(0o600)
    original = path.read_bytes()
    monkeypatch.setattr(
        provider_config.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=130),
    )

    assert provider_config.cmd_provider_setup(SimpleNamespace(provider=provider)) == 130
    assert path.read_bytes() == original


def test_grok_auto_install_is_isolated_and_versioned(tmp_path, monkeypatch):
    target = tmp_path / "dradar/providers/grok/runtime/1.0.3/bin/grok"
    seen = {}
    installer = b"#!/bin/bash\nexit 0\n"
    monkeypatch.setattr(provider_config, "managed_grok_cli_path", lambda: target)
    monkeypatch.setattr(provider_config.shutil, "which", lambda name: "/bin/bash")
    monkeypatch.setattr(
        provider_config,
        "_GROK_INSTALLER_SHA256",
        provider_config.hashlib.sha256(installer).hexdigest(),
    )
    monkeypatch.setattr(
        provider_config,
        "_provider_httpx_get",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200, content=installer,
        ),
    )

    def run(cmd, *, env, check):
        seen.update(cmd=cmd, env=env)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("managed-grok")
        target.chmod(0o700)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(provider_config.subprocess, "run", run)
    monkeypatch.setattr(
        provider_config, "_grok_cli_version", lambda executable: "1.0.3",
    )

    assert provider_config._install_managed_grok_cli() == str(target)
    assert seen["cmd"][-1] == "1.0.3"
    assert seen["env"]["GROK_BIN_DIR"] == str(target.parent)
    assert seen["env"]["HOME"].startswith(str(target.parent.parent))


def test_grok_auto_install_rejects_changed_installer(tmp_path, monkeypatch, capsys):
    target = tmp_path / "dradar/providers/grok/runtime/1.0.3/bin/grok"
    monkeypatch.setattr(provider_config, "managed_grok_cli_path", lambda: target)
    monkeypatch.setattr(provider_config.shutil, "which", lambda name: "/bin/bash")
    monkeypatch.setattr(
        provider_config,
        "_provider_httpx_get",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200, content=b"#!/bin/bash\necho changed\n",
        ),
    )
    monkeypatch.setattr(
        provider_config.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("unreviewed installer must not execute"),
    )

    assert provider_config._install_managed_grok_cli() is None
    assert "refusing to execute" in capsys.readouterr().out


def test_provider_http_client_uses_os_proxy_and_honors_no_proxy(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            seen.update(url=url, request=kwargs)
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(provider_config.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        provider_config,
        "provider_subprocess_env",
        lambda: {
            "HTTPS_PROXY": "http://127.0.0.1:18080",
            "NO_PROXY": "open.bigmodel.cn",
        },
    )

    provider_config._provider_httpx_get(
        "https://auth.x.ai/oauth2/device/code",
        timeout=7.0,
        follow_redirects=True,
    )
    assert seen["client"] == {
        "proxy": "http://127.0.0.1:18080",
        "trust_env": False,
        "timeout": 7.0,
        "follow_redirects": True,
    }
    provider_config._provider_httpx_get("https://open.bigmodel.cn/models")
    assert seen["client"]["proxy"] is None


def test_grok_login_inherits_os_proxy_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path / "dradar"))
    monkeypatch.setattr(
        provider_config.sys, "stdin", SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr(provider_config, "_ensure_grok_cli", lambda: "/managed/grok")
    monkeypatch.setattr(
        provider_config,
        "provider_subprocess_env",
        lambda: {
            "HTTPS_PROXY": "http://127.0.0.1:18080",
            "GROK_HOME": "/ambient/grok-home",
            "XAI_API_KEY": "must-not-leak",
        },
    )
    seen = {}

    def run(cmd, *, env):
        seen.update(cmd=cmd, env=env)
        auth = provider_config.Path(env["HOME"]) / ".grok" / "auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({
            "https://auth.x.ai::client": {
                "auth_mode": "oauth",
                "key": "access",
                "refresh_token": "refresh",
            },
        }))
        if os.name != "nt":
            auth.chmod(0o600)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(provider_config.subprocess, "run", run)
    monkeypatch.setattr(provider_config, "grok_live_error", lambda _cli: None)

    assert provider_config._setup_grok_subscription() == 0
    assert seen["cmd"] == ["/managed/grok", "login", "--device-auth"]
    assert seen["env"]["HTTPS_PROXY"] == "http://127.0.0.1:18080"
    assert "GROK_HOME" not in seen["env"]
    assert "XAI_API_KEY" not in seen["env"]


def test_kimi_auto_install_uses_reviewed_official_binary(tmp_path, monkeypatch):
    target = tmp_path / "dradar/providers/kimi/runtime/0.36.1/bin/kimi"
    binary = b"official-kimi-node-bundle"
    monkeypatch.setattr(provider_config, "managed_kimi_cli_path", lambda: target)
    monkeypatch.setattr(
        provider_config.platform, "system", lambda: "Linux",
    )
    monkeypatch.setattr(provider_config.platform, "machine", lambda: "x86_64")
    monkeypatch.setitem(
        provider_config.KIMI_BINARY_SHA256,
        "linux-x64",
        provider_config.hashlib.sha256(binary).hexdigest(),
    )
    seen = {}
    def get(url, **kwargs):
        seen.update(url=url, kwargs=kwargs)
        return SimpleNamespace(status_code=200, content=binary)
    monkeypatch.setattr(provider_config, "_provider_httpx_get", get)
    monkeypatch.setattr(
        provider_config, "_kimi_cli_version", lambda executable: "0.36.1",
    )

    assert provider_config._install_managed_kimi_cli() == str(target)
    assert target.read_bytes() == binary
    assert seen["url"].endswith("/kimi-code-linux-x64")


def test_kimi_login_uses_node_code_home_contract(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path / "dradar"))
    monkeypatch.setenv("KIMI_CODE_HOME", "/ambient/legacy-home")
    monkeypatch.setattr(
        provider_config.sys, "stdin", SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr(provider_config, "_ensure_kimi_cli", lambda: "/managed/kimi")
    issues = iter(["missing OAuth", None])
    monkeypatch.setattr(
        provider_config, "kimi_auth_error", lambda *args, **kwargs: next(issues),
    )
    monkeypatch.setattr(provider_config, "kimi_live_error", lambda _cli: None)
    monkeypatch.setattr(
        provider_config,
        "provider_subprocess_env",
        lambda: {
            "HTTPS_PROXY": "http://127.0.0.1:18080",
            "KIMI_CODE_HOME": "/ambient/legacy-home",
            "KIMI_API_KEY": "must-not-leak",
        },
    )
    seen = {}

    def run(cmd, *, env):
        seen.update(cmd=cmd, env=env)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(provider_config.subprocess, "run", run)

    assert provider_config._setup_kimi_subscription() == 0
    assert seen["cmd"] == ["/managed/kimi", "login"]
    assert seen["env"]["KIMI_CODE_HOME"] == str(provider_config.kimi_home())
    assert seen["env"]["HTTPS_PROXY"] == "http://127.0.0.1:18080"
    assert "KIMI_API_KEY" not in seen["env"]
