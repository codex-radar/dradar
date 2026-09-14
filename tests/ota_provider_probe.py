"""Isolated external-system fixtures for real zipapp launcher tests.

Loaded as sitecustomize by the test subprocess. Product entry points, resource
loaders, capability computation and HTTP header construction remain unchanged.
No credentials, Docker, production endpoints or model calls are used.
"""
import atexit
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.environ['PROBE_ARTIFACT'])
from dradar import providers as p, doctor as d, runner
from dradar.ota import discovery
from dradar.api_client import ApiClient, CLIENT_CAPABILITIES_HEADER
import httpx

home = Path(os.environ['DRADAR_HOME'])
home.mkdir(parents=True, exist_ok=True)
ready = home / 'ready.json'
ready.write_text(json.dumps({'models': list(p.ANTIGRAVITY_RUNTIME_MODELS.values())}))

def stub(obj, name, value):
    patch.object(obj, name, value).start()

# Only replace host/auth/network prerequisites, never capability/resource logic.
for obj in (p, d):
    stub(obj, 'claude_subscription_error', lambda: None if os.environ.get('PROBE_AUTH', '1') == '1' else 'missing auth')
    stub(obj, 'prepare_antigravity_auth', lambda: None if os.environ.get('PROBE_AUTH', '1') == '1' else 'missing auth')
    stub(obj, 'claude_cli_path', lambda *a, **k: '/fixture/claude')
stub(p, 'antigravity_ready_path', lambda: ready)
stub(p, 'deepseek_api_key', lambda *a: 'fixture')
stub(p, 'grok_cli_path', lambda *a: None)
stub(p, 'kimi_cli_path', lambda *a: None)
stub(p, 'zcode_api_key', lambda *a: 'fixture')
stub(p, 'zcode_cli_error', lambda **k: None)
stub(p, 'codebuddy_executable', lambda *a: '/fixture/codebuddy')
stub(p, 'codebuddy_host_cli_status', lambda *a: (None, '2.0.0'))
stub(p, 'codebuddy_credential_status', lambda: (True, 'fixture'))
stub(p, 'codebuddy_runtime_image_error', lambda: None)
stub(discovery, 'discover_update', lambda *a, **k: None)
stub(discovery, 'start_periodic_discovery', lambda *a, **k: SimpleNamespace(set=lambda: None))
stub(d, '_load_config', lambda: {'server': 'https://fixture.invalid', 'token': 'fixture'})
stub(d, 'tasks_root_from_config', lambda *a: home)
stub(d, '_probe', lambda *a: True)
stub(d.shutil, 'which', lambda name: '/fixture/' + name)
stub(d.egress, 'egress_proxy_preflight', lambda *a: (True, ''))
stub(runner, 'ensure_pier', lambda: '/fixture/pier')
stub(runner, '_resolve_user_tool', lambda name: '/fixture/' + name)
stub(runner, '_pier_version', lambda *a: runner.PIER_VERSION)
stub(runner, '_pier_version_compatible', lambda *a: True)
stub(d, '_client', lambda cfg: SimpleNamespace(whoami=lambda: {'nickname': 'fixture'}))

if os.environ.get('PROBE_ALL'):
    stub(p, 'grok_cli_path', lambda *a: '/fixture/grok')
    stub(p, 'grok_auth_error', lambda: None)
    stub(p, 'kimi_cli_path', lambda *a: '/fixture/kimi')
    stub(p, 'kimi_auth_error', lambda: None)
    ready.write_text(json.dumps({'models': [value for group in p.ANTIGRAVITY_MODEL_RUNTIME_MODELS.values() for value in group.values()]}))
    from dradar import managed_auth_selection
    stub(managed_auth_selection, 'load_selection', lambda *a: object())
    stub(managed_auth_selection, 'trial_platform_ready', lambda *a: True)

@atexit.register
def record():
    captured = []
    def response(request):
        captured.append(dict(request.headers))
        return httpx.Response(200, json={'nickname': 'fixture'})
    client = ApiClient('https://fixture.invalid', '', transport=httpx.MockTransport(response))
    client.whoami()
    report = {'module': p.__file__, 'capabilities': list(client.capabilities),
              'header': captured[0].get(CLIENT_CAPABILITIES_HEADER.lower(), ''),
              'catalog_error': p.deepseek_catalog_error(),
              'files': {name: Path(p.__file__).with_name(name).is_file() for name in ('pier_claude.py', 'pier_antigravity.py')},
              'missing_plan_issue': d.plan_environment_issue({'harness': os.environ.get('PROBE_AGENT', 'claude-code')}) if os.environ.get('PROBE_MISSING') else None}
    try:
        catalog = runner._validated_deepseek_catalog(home)
        report['materialized_catalog'] = catalog.is_file()
    except Exception as exc:
        report['materialized_catalog_error'] = type(exc).__name__
    Path(os.environ['PROBE_OUTPUT']).write_text(json.dumps(report, indent=2))
