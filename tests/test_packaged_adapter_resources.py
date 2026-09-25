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


def test_stale_worker_helper_replaced_from_zipapp(package_sources, tmp_path):
    probe = r'''
import importlib.resources, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from dradar import runner
home = Path(sys.argv[2]); home.mkdir(mode=0o700)
cached = home / '_dradar_worker_events.py'
cached.write_text('def emit_worker_registered(**kwargs): return True\n')
cached.chmod(0o600)
runner._ensure_worker_event_module(home)
expected = importlib.resources.files('dradar').joinpath('worker_events.py').read_bytes()
assert cached.read_bytes() == expected and b'start_deadline' in expected
sys.path.insert(0, str(home))
import _dradar_worker_events as worker
assert 'start_deadline' in worker.WorkerRegistered.__dataclass_fields__
print('stale-helper-replaced PASS')
'''
    result = subprocess.run(
        [sys.executable, '-c', probe, str(package_sources['zipapp']), str(tmp_path/'output')],
        env=dict(os.environ, PYTHONPATH='', PYTHONDONTWRITEBYTECODE='1',
                 DRADAR_HOME=str(tmp_path/'fixture-home')),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'stale-helper-replaced PASS' in result.stdout


DEEPSEEK_CONSTRUCTOR_PROBE = r'''
import sys, socket, importlib, importlib.resources
from pathlib import Path
source, mode, destination = sys.argv[1:]
def denied(*args, **kwargs):
    raise AssertionError('constructor regression must not access network')
socket.socket.connect = denied
socket.create_connection = denied
sys.path.insert(0, source)
from dradar import runner
from dradar.deepseek_catalog_pin import DEEPSEEK_CATALOG_SHA256
home = Path(destination)
home.mkdir(mode=0o700)
resources = importlib.resources.files('dradar')
catalog = home / 'models.json'
catalog.write_bytes(resources.joinpath('deepseek_codex_models.json').read_bytes())
if mode == 'materialized':
    module_path = runner._ensure_deepseek_agent_module(home)
    sys.path.insert(0, str(home))
    module = importlib.import_module('_dradar_pier_deepseek')
    pin = importlib.import_module('_dradar_deepseek_catalog_pin')
    assert Path(pin.__file__).parent == home
else:
    module = importlib.import_module('dradar.pier_deepseek')
    assert module.__file__.startswith(source)
assert module._CATALOG_SHA256 == DEEPSEEK_CATALOG_SHA256
agent = module.DeepSeekCodex(logs_dir=home/'logs', model_name='deepseek-flash',
    version='0.149.0', model_catalog_json_file=str(catalog), extra_env={})
assert agent.network_allowlist().domains == ['api.deepseek.com']
catalog.write_bytes(catalog.read_bytes() + b'\n')
try:
    module.DeepSeekCodex(logs_dir=home/'bad-logs', model_name='deepseek-flash',
        version='0.149.0', model_catalog_json_file=str(catalog), extra_env={})
except ValueError as error:
    assert 'integrity check failed' in str(error)
else:
    raise AssertionError('tampered catalog accepted')
print('actual-constructor-and-tamper-rejection PASS')
'''


@pytest.mark.parametrize('delivery', ['package', 'materialized'])
@pytest.mark.parametrize('mode', ['source', 'zipapp'])
def test_deepseek_constructor_uses_packaged_pin_and_rejects_tampering(
    package_sources, tmp_path, delivery, mode,
):
    pytest.importorskip('pier')
    result = subprocess.run(
        [sys.executable, '-c', DEEPSEEK_CONSTRUCTOR_PROBE,
         str(package_sources[mode]), delivery, str(tmp_path/'output')],
        env=dict(os.environ, PYTHONPATH='', PYTHONDONTWRITEBYTECODE='1',
                 DRADAR_HOME=str(tmp_path/'fixture-home')),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'actual-constructor-and-tamper-rejection PASS' in result.stdout


def test_catalog_checkout_retains_pinned_bytes_with_autocrlf(tmp_path):
    import hashlib
    from dradar.deepseek_catalog_pin import DEEPSEEK_CATALOG_SHA256
    root = Path(__file__).resolve().parents[1]
    catalog = tmp_path / 'src/dradar/deepseek_codex_models.json'
    catalog.parent.mkdir(parents=True)
    catalog.write_bytes((root/'src/dradar/deepseek_codex_models.json').read_bytes())
    (tmp_path/'.gitattributes').write_bytes((root/'.gitattributes').read_bytes())
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    git = ['git', '-C', str(tmp_path), '-c', 'core.autocrlf=true']
    subprocess.run([*git, 'add', '.gitattributes', 'src/dradar/deepseek_codex_models.json'], check=True)
    catalog.unlink()
    subprocess.run([*git, 'checkout-index', '-a', '-f'], check=True)
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() == DEEPSEEK_CATALOG_SHA256
