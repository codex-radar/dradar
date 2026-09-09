"""Trusted package resources must work from source and the actual OTA zipapp."""
from pathlib import Path
import importlib.util
import os
import subprocess
import sys

import pytest


HELPERS = (
    '_ensure_worker_event_module', '_ensure_codex_agent_module',
    '_ensure_claude_agent_module', '_ensure_grok_agent_module',
    '_ensure_kimi_agent_module', '_ensure_codebuddy_agent_module',
    '_ensure_deepseek_agent_module', '_ensure_antigravity_agent_module',
    '_ensure_zcode_agent_module', '_ensure_dsh_agent_module',
    '_ensure_runtime_safety_module', '_ensure_shared_oauth_environment_module',
    '_ensure_pier_sitecustomize',
)

PROBE = r'''
import sys, socket, importlib.resources
from pathlib import Path
source, helper, destination = sys.argv[1:]
def denied(*args, **kwargs):
    raise AssertionError('resource fixture must remain offline')
socket.socket.connect = denied
socket.create_connection = denied
sys.path.insert(0, source)
from dradar import runner
assert runner.__file__.startswith(source)
home = Path(destination)
home.mkdir(mode=0o700)
result = getattr(runner, helper)(home)
assert result.parent == home and result.is_file()
resources = importlib.resources.files('dradar')
source_bytes = {p.read_bytes() for p in resources.iterdir() if p.is_file() and p.name.endswith('.py')}
outputs = list(home.iterdir())
assert outputs
for path in outputs:
    assert path.is_file() and not path.is_symlink()
    assert path.read_bytes() in source_bytes
if (home / '_dradar_worker_events.py').exists():
    assert (home / '_dradar_artifact_boundary.py').read_bytes() == resources.joinpath('artifact_boundary.py').read_bytes()
    assert (home / '_dradar_artifact_boundary_win.py').read_bytes() == resources.joinpath('artifact_boundary_win.py').read_bytes()
print(helper, 'PASS', len(outputs))
'''


@pytest.fixture(scope='module')
def package_sources(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('resource_fixture_release', root/'scripts/ota_release.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    artifact = tmp_path_factory.mktemp('ota-package')/'candidate.pyz'
    tool._build_zipapp(root, artifact, version='0.5.194', sequence=16,
                      commit='0'*40, tree='0'*40, target=('macos','arm64'))
    return {'source': root/'src', 'zipapp': artifact}


@pytest.mark.parametrize('helper', HELPERS)
@pytest.mark.parametrize('mode', ['source', 'zipapp'])
def test_adapter_resources_are_materialized_without_network(package_sources, tmp_path, helper, mode):
    result = subprocess.run(
        [sys.executable, '-c', PROBE, str(package_sources[mode]), helper, str(tmp_path/'output')],
        env=dict(os.environ, PYTHONPATH='', PYTHONDONTWRITEBYTECODE='1',
                 DRADAR_HOME=str(tmp_path/'fixture-home')),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS' in result.stdout
