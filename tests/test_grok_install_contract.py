"""Load the runner's standalone bundle using the actual pinned Pier models."""
import importlib.util
import sys

import pytest


def test_real_pier_install_spec_from_runner_bundle(tmp_path, monkeypatch):
    pytest.importorskip('pier.models.agent.install')
    from pier.models.agent.install import AgentInstallSpec
    from dradar.runner import _ensure_grok_agent_module

    path = _ensure_grok_agent_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = importlib.util.spec_from_file_location('_grok_install_contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = object.__new__(module.GrokBuild)
    agent._version = None
    install = agent.install_spec()
    assert isinstance(install, AgentInstallSpec)
    assert install.version == '1.0.40'
    assert len(install.steps) == 1
    assert install.steps[0].user == 'root'
    assert install.steps[0].run == module._install_command()
    assert install.cache_key.endswith('linux-runtime-v5')
    assert '/opt/grok-runtime/bin/grok --version' in install.verification_command
    assert agent.get_version_command() == '/opt/grok-runtime/bin/grok --version'
