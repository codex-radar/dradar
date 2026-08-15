"""Provider keys stay out of argv, config.json, logs, and the server."""

import json
import os
from types import SimpleNamespace

import httpx
import pytest

from dradar import provider_config
from dradar.providers import (
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_OPENCODE_API_KEY_ENV,
    create_deepseek_auth_json,
    create_provider_auth_json,
    deepseek_api_key,
    deepseek_credential_source,
    deepseek_secret_error,
    deepseek_secret_path,
    opencode_api_key,
    opencode_credential_source,
    opencode_secret_path,
    store_deepseek_api_key,
    store_opencode_api_key,
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

    rc = provider_config.cmd_provider_setup(SimpleNamespace(provider="deepseek"))

    assert rc == 0
    assert deepseek_api_key() == "new-file-secret"
    assert deepseek_credential_source() == "file"
    output = capsys.readouterr().out
    assert "stale-environment-secret" not in output
    assert "new-file-secret" not in output


def test_status_reports_source_without_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    store_deepseek_api_key("sentinel-provider-secret")

    rc = provider_config.cmd_provider_status(SimpleNamespace(provider="deepseek"))

    assert rc == 0
    output = capsys.readouterr().out
    assert str(deepseek_secret_path()) in output
    assert "sentinel-provider-secret" not in output


def test_opencode_setup_and_status_use_their_own_secret_path(
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
        lambda _prompt: "sentinel-opencode-token",
    )

    rc = provider_config.cmd_provider_setup(
        SimpleNamespace(provider="opencode-go")
    )

    assert rc == 0
    assert opencode_api_key() == "sentinel-opencode-token"
    assert deepseek_api_key() is None
    assert "sentinel-opencode-token" not in capsys.readouterr().out

    rc = provider_config.cmd_provider_status(
        SimpleNamespace(provider="opencode-go")
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert str(opencode_secret_path()) in output
    assert "sentinel-opencode-token" not in output


def test_provider_auth_json_never_mixes_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))

    # OpenCode Go run: only the opencode token may enter its auth.json.
    monkeypatch.setenv(DEEPSEEK_OPENCODE_API_KEY_ENV, "sentinel-opencode-token")
    monkeypatch.delenv(DEEPSEEK_API_KEY_ENV, raising=False)
    path = create_provider_auth_json(tmp_path, "opencode-go")
    assert json.loads(path.read_text()) == {
        "OPENAI_API_KEY": "sentinel-opencode-token",
    }

    # Fail closed: an official key alone can never satisfy an opencode run.
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sentinel-official-secret")
    monkeypatch.delenv(DEEPSEEK_OPENCODE_API_KEY_ENV, raising=False)
    with pytest.raises(ValueError, match="OpenCode Go API key is not configured"):
        create_provider_auth_json(tmp_path, "opencode-go")

    # Official run: only the official key may enter its auth.json, and an
    # opencode token alone can never satisfy it.
    monkeypatch.setenv(DEEPSEEK_OPENCODE_API_KEY_ENV, "sentinel-opencode-token")
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sentinel-official-secret")
    path = create_provider_auth_json(tmp_path, "deepseek")
    assert json.loads(path.read_text()) == {
        "OPENAI_API_KEY": "sentinel-official-secret",
    }
    monkeypatch.delenv(DEEPSEEK_API_KEY_ENV, raising=False)
    with pytest.raises(ValueError, match="DeepSeek API key is not configured"):
        create_provider_auth_json(tmp_path, "deepseek")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_opencode_secret_symlink_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path / "home"))
    target = tmp_path / "outside-secret"
    target.write_text("must-not-be-read\n")
    path = opencode_secret_path()
    path.parent.mkdir(parents=True)
    path.symlink_to(target)

    assert opencode_api_key() is None
    assert opencode_credential_source() is None
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

    monkeypatch.setattr(provider_config.httpx, "get", get)
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
        provider_config.httpx, "get",
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

    monkeypatch.setattr(provider_config.httpx, "get", fail)
    assert provider_config.cmd_provider_status(
        SimpleNamespace(provider="deepseek", live=True)) == 1
    transport = capsys.readouterr().out
    assert "network/proxy" in transport
    assert "ConnectError" in transport
    assert "sentinel-provider-secret" not in transport
