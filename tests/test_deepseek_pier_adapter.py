import importlib.util
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest

import dradar
from dradar.providers import deepseek_catalog_path


OFFICIAL_BASE_URL = "https://api.deepseek.com/"
OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
UNKNOWN_BASE_URL = "https://third-party.example/v1"


def _load_adapter(monkeypatch):
    calls = []

    def package(name):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
        return module

    package("pier")
    package("pier.agents")
    package("pier.agents.installed")
    package("pier.environments")
    package("pier.models")
    package("pier.models.agent")

    network = ModuleType("pier.agents.network")

    def allowlist_from_urls(urls, *, default_domains):
        calls.append((urls, default_domains))
        return "sentinel-allowlist"

    network.allowlist_from_urls = allowlist_from_urls
    monkeypatch.setitem(sys.modules, network.__name__, network)

    codex = ModuleType("pier.agents.installed.codex")

    class FakeCodex:
        _REMOTE_CODEX_HOME = PurePosixPath("/tmp/codex-home")

        def __init__(self, *args, **kwargs):
            pass

    codex.Codex = FakeCodex
    monkeypatch.setitem(sys.modules, codex.__name__, codex)

    environment = ModuleType("pier.environments.base")
    environment.BaseEnvironment = object
    monkeypatch.setitem(sys.modules, environment.__name__, environment)

    context = ModuleType("pier.models.agent.context")
    context.AgentContext = object
    monkeypatch.setitem(sys.modules, context.__name__, context)

    model_network = ModuleType("pier.models.agent.network")
    model_network.NetworkAllowlist = object
    monkeypatch.setitem(sys.modules, model_network.__name__, model_network)

    source = Path(dradar.__file__).with_name("pier_deepseek.py")
    spec = importlib.util.spec_from_file_location("_test_pier_deepseek", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls


def test_adapter_rejects_unknown_provider_url(monkeypatch):
    module, _calls = _load_adapter(monkeypatch)

    with pytest.raises(ValueError, match="unsupported DeepSeek provider URL"):
        module.DeepSeekCodex(
            model_catalog_json_file=str(deepseek_catalog_path()),
            provider_base_url=UNKNOWN_BASE_URL,
        )


@pytest.mark.parametrize(
    "provider_base_url",
    [OFFICIAL_BASE_URL, OPENCODE_GO_BASE_URL],
)
def test_adapter_accepts_only_the_two_authorized_deepseek_urls(
    monkeypatch,
    provider_base_url,
):
    module, calls = _load_adapter(monkeypatch)
    adapter = module.DeepSeekCodex(
        model_catalog_json_file=str(deepseek_catalog_path()),
        provider_base_url=provider_base_url,
    )

    assert adapter.network_allowlist() == "sentinel-allowlist"
    assert calls == [([provider_base_url], [])]


def test_allowlist_uses_constructor_snapshot_not_local_config(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        '{"deepseek_endpoint":"opencode-go"}'
    )
    module, calls = _load_adapter(monkeypatch)
    adapter = module.DeepSeekCodex(
        model_catalog_json_file=str(deepseek_catalog_path()),
        provider_base_url=OPENCODE_GO_BASE_URL,
    )

    assert adapter.network_allowlist() == "sentinel-allowlist"
    assert calls == [([OPENCODE_GO_BASE_URL], [])]
