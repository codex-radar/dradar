"""Versioned K2.8 routing and cross-model evidence rejection."""
import ast
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import tomllib
import pytest
from dradar import providers, runner


def helpers():
    tree = ast.parse(Path(providers.__file__).with_name('pier_kimi.py').read_text())
    nodes = [n for n in tree.body if
        isinstance(n, ast.FunctionDef) and n.name in {'_usage_instant', '_kimi_usage_facts', 'kimi_model_config'}
        or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {'KIMI_CONFIG', 'KIMI_RUNTIME_MODELS'} for t in n.targets)]
    ns = {'Any': Any, 'datetime': datetime, 'timezone': timezone}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'adapter', 'exec'), ns)
    return ns


@pytest.mark.parametrize('effort', ['low', 'high', 'max'])
def test_k28_assignment_and_official_config(effort):
    runner._validate_kimi_assignment({'provider': providers.KIMI_PROVIDER, 'model': providers.KIMI_K28_MODEL, 'effort': effort})
    config = tomllib.loads(helpers()['kimi_model_config'](providers.KIMI_K28_MODEL))
    assert config['default_model'] == 'kimi-code/kimi-for-coding'
    model = config['models'][config['default_model']]
    assert model['model'] == 'kimi-for-coding'
    assert model['support_efforts'] == ['low', 'high', 'max']
    assert model['max_context_size'] == 1048576
    assert 'image_in' in model['capabilities']
    assert 'k3' not in config['models']


def wire(model='kimi-for-coding', alias='kimi-code/kimi-for-coding'):
    return [
        {'type': 'metadata'}, {'type': 'turn.prompt', 'time': 100},
        {'type': 'llm.request', 'model': model, 'modelAlias': alias, 'turnStep': '0.1', 'time': 101, 'thinkingEffort':'high'},
        {'type': 'usage.record', 'usageScope': 'turn', 'model': alias, 'time': 102,
         'usage': {'inputOther': 10, 'inputCacheCreation': 3, 'inputCacheRead': 20, 'output': 7}},
        {'type': 'turn.ended', 'turnId': 0, 'reason': 'completed', 'time': 103},
    ]


def test_request_route_and_usage_identity_are_explicit():
    facts = helpers()['_kimi_usage_facts']
    good = facts(wire(), model=providers.KIMI_K28_MODEL, expected_effort='high')
    assert good['complete'] is True
    assert (good['n_input_tokens'], good['n_cache_tokens'], good['n_output_tokens']) == (33, 20, 7)
    assert good['model'] == providers.KIMI_K28_MODEL
    assert good['observed_model'] is None
    assert good['observed_model_status'] == 'not_exposed_by_runtime'
    assert good['model_identity_basis'] == 'official_subscription_route'
    assert good['requested_runtime_model'] == 'kimi-for-coding'
    for records in [wire('k3'), wire(alias='kimi-code/k3'), wire('unknown-provider-id'), wire()[:-1]]:
        assert facts(records, model=providers.KIMI_K28_MODEL, expected_effort='high')['complete'] is False
    assert facts(wire())['complete'] is False
    assert facts(wire('k3', 'kimi-code/k3'))['complete'] is True


@pytest.mark.parametrize('effort', ['low', 'high', 'max'])
def test_k28_wire_effort_must_match_each_request(effort):
    facts=helpers()['_kimi_usage_facts']
    records=wire()
    records[2]['thinkingEffort']=effort
    assert facts(records,model=providers.KIMI_K28_MODEL,expected_effort=effort)['complete']
    for wrong in ['low','high','max',None,'medium']:
        if wrong == effort: continue
        records[2]['thinkingEffort']=wrong
        result=facts(records,model=providers.KIMI_K28_MODEL,expected_effort=effort)
        assert not result['complete']
        assert not result['thinking_effort_verified']
