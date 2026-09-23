import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).parents[1]


def build_artifact(destination):
    from dradar import __version__
    spec = importlib.util.spec_from_file_location('ota_build', ROOT / 'scripts/ota_release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._build_zipapp(ROOT, destination, version=__version__, sequence=46,
                         commit='a' * 40, tree='b' * 40, target=('linux', 'x86_64'))


def probe(artifact, directory, agent='claude-code', missing=False, auth=True, all_ready=False):
    directory.mkdir(parents=True)
    shutil.copyfile(ROOT / 'tests/ota_provider_probe.py', directory / 'sitecustomize.py')
    output = directory / 'result.json'
    env = {k: v for k, v in os.environ.items() if 'proxy' not in k.lower() and not k.startswith('DRADAR_')}
    env.update(PYTHONPATH=str(directory), DRADAR_HOME=str(directory / 'home'),
               PROBE_ARTIFACT=str(artifact), PROBE_OUTPUT=str(output),
               PROBE_AGENT=agent, PROBE_AUTH='1' if auth else '0')
    if all_ready:
        env['PROBE_ALL'] = '1'
    if missing:
        env['PROBE_MISSING'] = '1'
    command = [sys.executable, '-m', 'dradar.launcher'] if artifact.is_dir() else [sys.executable, str(artifact)]
    result = subprocess.run([*command, 'doctor', '--agent', agent],
                            env=env, cwd=directory, capture_output=True, text=True, timeout=30)
    assert output.exists(), result.stdout + result.stderr
    return result, json.loads(output.read_text())


@pytest.mark.parametrize('agent', ['claude-code', 'antigravity'])
def test_real_zipapp_launcher_capabilities_and_doctor(tmp_path, agent):
    artifact = tmp_path / 'candidate.pyz'
    build_artifact(artifact)
    result, report = probe(artifact, tmp_path / 'probe', agent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '.pyz/dradar/' in report['module']
    from dradar import providers as p
    expected = {p.CLAUDE_CAPABILITY, p.ANTIGRAVITY_CAPABILITY, p.DSH_FLASH_CAPABILITY,
                p.ZCODE_CAPABILITY, p.CODEBUDDY_CAPABILITY, p.DEEPSEEK_CAPABILITY,
                p.GPT6_CAPABILITY}
    assert expected <= set(report['capabilities'])
    assert p.ANTIGRAVITY_FLASH_38_CAPABILITY not in report['capabilities']
    assert p.GROK_CAPABILITY not in report['capabilities']
    assert p.KIMI_CAPABILITY not in report['capabilities']
    assert set(report['header'].split(',')) == set(report['capabilities'])
    assert report['catalog_error'] is None
    assert report['materialized_catalog'] is True


@pytest.mark.parametrize('agent,resource,capability', [
    ('claude-code', 'pier_claude.py', 'CLAUDE_CAPABILITY'),
    ('antigravity', 'pier_antigravity.py', 'ANTIGRAVITY_CAPABILITY'),
])
def test_missing_zip_resource_fails_doctor_and_preflight(tmp_path, agent, resource, capability):
    original = tmp_path / 'original.pyz'
    build_artifact(original)
    artifact = tmp_path / 'missing.pyz'
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(artifact, 'w') as target:
        for info in source.infolist():
            if info.filename != 'dradar/' + resource:
                target.writestr(info, source.read(info))
    result, report = probe(artifact, tmp_path / 'probe', agent, missing=True)
    assert result.returncode == 1, result.stdout + result.stderr
    from dradar import providers as p
    assert getattr(p, capability) not in report['capabilities']
    assert report['missing_plan_issue']['error_code'] == 'bundled_adapter_missing'
    assert 'Bundled provider adapter' in result.stdout


def test_zip_auth_failure_does_not_advertise_subscription(tmp_path):
    artifact = tmp_path / 'candidate.pyz'
    build_artifact(artifact)
    result, report = probe(artifact, tmp_path / 'probe', auth=False)
    from dradar import providers as p
    assert result.returncode == 1
    assert p.CLAUDE_CAPABILITY not in report['capabilities']
    assert p.ANTIGRAVITY_CAPABILITY not in report['capabilities']


def test_source_launcher_matches_zip_capabilities(tmp_path):
    artifact = tmp_path / 'candidate.pyz'
    build_artifact(artifact)
    source_result, source = probe(ROOT / 'src', tmp_path / 'source')
    zip_result, zipped = probe(artifact, tmp_path / 'zip')
    assert source_result.returncode == zip_result.returncode == 0
    assert source['capabilities'] == zipped['capabilities']
    assert source['version'] == zipped['version'] == '0.5.226'
    assert source['fleet_protocol_version'] == zipped['fleet_protocol_version'] == 10
    from dradar import providers as p
    assert p.GPT6_CAPABILITY in source['capabilities']


def test_public_launcher_selects_signed_gpt6_ota_payload(tmp_path, monkeypatch):
    """The ordinary launcher must hand off to the new signed payload."""
    import httpx
    from dradar import launcher
    from dradar.ota import discovery
    from test_ota_runtime import sign_document, signed_release

    artifact = tmp_path / 'candidate.pyz'
    build_artifact(artifact)
    body = artifact.read_bytes()
    document, trusted_keys = signed_release()
    document.pop('signature')
    document['release_id'] = 'dradar-cli-0.5.226-gpt6-fixture'
    document['version'] = '0.5.226'
    document['sequence'] = 47
    for item in document['artifacts']:
        item['size'] = len(body)
        item['sha256'] = hashlib.sha256(body).hexdigest()
    sign_document(document)

    probe_dir = tmp_path / 'probe'
    probe_dir.mkdir()
    shutil.copyfile(ROOT / 'tests/ota_provider_probe.py', probe_dir / 'sitecustomize.py')
    home = tmp_path / 'home'
    report_path = tmp_path / 'report.json'
    monkeypatch.setenv('PYTHONPATH', str(probe_dir))
    monkeypatch.setenv('DRADAR_HOME', str(home))
    monkeypatch.setenv('PROBE_ARTIFACT', str(artifact))
    monkeypatch.setenv('PROBE_OUTPUT', str(report_path))
    monkeypatch.setenv('PROBE_AGENT', 'claude-code')
    monkeypatch.setenv('PROBE_AUTH', '1')
    monkeypatch.setattr(launcher, 'HOME', home)
    monkeypatch.setattr(discovery, 'TRUSTED_KEYS', trusted_keys)
    # Model the current public 0.5.225 launcher discovering this release.
    monkeypatch.setattr(discovery, '__version__', '0.5.225')

    def response(request):
        if str(request.url) == discovery.STABLE_URL:
            return httpx.Response(200, json=document)
        return httpx.Response(200, content=body)

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        monkeypatch.setattr(
            launcher, 'discover_update',
            lambda directory: discovery.discover_update(
                directory, client=client, trusted_keys=trusted_keys,
            ),
        )
        monkeypatch.setattr(sys, 'argv', ['dradar', 'doctor', '--agent', 'claude-code'])
        assert launcher.main() == 0

    report = json.loads(report_path.read_text())
    from dradar.providers import GPT6_CAPABILITY
    assert '.pyz/dradar/' in report['module']
    assert report['version'] == '0.5.226'
    assert report['fleet_protocol_version'] == 10
    assert GPT6_CAPABILITY in report['capabilities']
    assert GPT6_CAPABILITY in report['header'].split(',')
    state = json.loads((home / 'ota/update-state.json').read_text())
    assert state['state'] == 'committed'


@pytest.mark.parametrize('resource,capabilities', [
    ('pier_dsh.py', ['DSH_FLASH_CAPABILITY', 'DSH_PRO_CAPABILITY', 'DSH_FLASH_41_CAPABILITY', 'DSH_VISION_CAPABILITY', 'DSH_VISION_TEXT_CAPABILITY']),
    ('pier_zcode.py', ['ZCODE_CAPABILITY']),
    ('pier_codebuddy.py', ['CODEBUDDY_CAPABILITY']),
    ('deepseek_codex_models.json', ['DEEPSEEK_CAPABILITY', 'DEEPSEEK_PRO_CAPABILITY', 'DEEPSEEK_FLASH_41_CAPABILITY']),
])
def test_other_missing_zip_resources_do_not_advertise(tmp_path, resource, capabilities):
    original = tmp_path / 'original.pyz'
    build_artifact(original)
    artifact = tmp_path / 'missing.pyz'
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(artifact, 'w') as target:
        for info in source.infolist():
            if info.filename != 'dradar/' + resource:
                target.writestr(info, source.read(info))
    result, report = probe(artifact, tmp_path / 'probe')
    assert result.returncode == 0, result.stdout + result.stderr
    from dradar import providers as p
    assert not {getattr(p, name) for name in capabilities} & set(report['capabilities'])


def test_corrupt_zip_catalog_still_fails_integrity(tmp_path):
    original = tmp_path / 'original.pyz'
    build_artifact(original)
    artifact = tmp_path / 'corrupt.pyz'
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(artifact, 'w') as target:
        for info in source.infolist():
            target.writestr(info, b'{"models": []}' if info.filename == 'dradar/deepseek_codex_models.json' else source.read(info))
    result, report = probe(artifact, tmp_path / 'probe')
    assert 'integrity check failed' in report['catalog_error']
    assert 'materialized_catalog' not in report
    from dradar import providers as p
    assert p.DEEPSEEK_CAPABILITY not in report['capabilities']


def test_all_supported_harness_capabilities_source_and_zip(tmp_path):
    artifact = tmp_path / 'candidate.pyz'
    build_artifact(artifact)
    from dradar import providers as p
    from dradar.managed_auth_selection import CAPABILITY, TRIAL_CAPABILITY
    expected = {CAPABILITY, TRIAL_CAPABILITY, p.GROK_CAPABILITY, p.GROK_47_CAPABILITY,
                p.KIMI_CAPABILITY, p.KIMI_K28_CAPABILITY,
                p.CLAUDE_CAPABILITY, p.CLAUDE_OPUS_55_CAPABILITY,
                p.ANTIGRAVITY_CAPABILITY, p.ANTIGRAVITY_FLASH_38_CAPABILITY,
                p.ZCODE_CAPABILITY, p.CODEBUDDY_CAPABILITY, p.DSH_FLASH_CAPABILITY,
                p.DSH_PRO_CAPABILITY, p.DSH_FLASH_41_CAPABILITY, p.DSH_VISION_CAPABILITY,
                p.DSH_VISION_TEXT_CAPABILITY, p.DEEPSEEK_CAPABILITY, p.DEEPSEEK_PRO_CAPABILITY,
                p.DEEPSEEK_FLASH_41_CAPABILITY, p.DEEPSEEK_FLASH_OFF_CAPABILITY,
                p.DEEPSEEK_PRO_OFF_CAPABILITY, p.DEEPSEEK_FLASH_41_OFF_CAPABILITY,
                p.TASK_PACKAGE_SYNC_CAPABILITY, p.GPT6_CAPABILITY}
    for name, source in [('source', ROOT / 'src'), ('zip', artifact)]:
        result, report = probe(source, tmp_path / name, all_ready=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert set(report['capabilities']) == expected
        assert set(report['header'].split(',')) == expected
