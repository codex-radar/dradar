"""Retirement boundaries with synthetic data only; no credentials/network/model."""
import copy
from types import SimpleNamespace
import pytest
from dradar.harness_policy import *
from dradar.api_client import ApiClient
from dradar.providers import normalize_capabilities, validate_refill_scope, DSH_AGENT
from dradar.v2.selection import Catalog
from dradar.v2.scheduler import Controller
from dradar.v2.journal import Journal
from dradar.v2.runtime import normalize_assignment
from dradar import runner

@pytest.mark.parametrize('benchmark',['deep-swe','pompeii-adjacency','tb4','science','future-library'])
@pytest.mark.parametrize('model',sorted(RETIRED_CODEX_DEEPSEEK_MODELS))
def test_global_retirement_rejects_claim_before_http_or_auth(benchmark,model):
    api=object.__new__(ApiClient);api.benchmark_id=benchmark
    api._negotiate_managed_auth_runtime=lambda **kw:pytest.fail('auth negotiation reached')
    api._post=lambda *a,**kw:pytest.fail('HTTP reached')
    with pytest.raises(ValueError,match='any benchmark'):
        api.claim_assignment('synthetic',model,'max')
    with pytest.raises(ValueError,match='any benchmark'):
        api.configure_refill_campaign(batch_id='synthetic',harness='codex',model=model,effort='max',refill_to=1,max_tasks=1)

def test_native_dsh_combination_and_deepswe_library_are_preserved():
    assert not retired_combination(DSH_AGENT,'dsh-deepseek-v4-flash','deepseek')
    assert not retired_combination('codex','gpt-6.1-sol','host-native-chatgpt')
    assert validate_refill_scope('dsh-minimal','dsh-deepseek-v4-flash','high')[0]==DSH_AGENT
    value={'benchmarks':[{'benchmark':'deepswe15-20261003-v4','models':[{'model':'gpt-6.1-sol','effort':'high'},{'agent':DSH_AGENT,'model':'dsh-deepseek-v4-flash','provider':'deepseek','effort':'high'}]}]}
    assert current_catalog(value)==value

def test_current_grid_models_and_statistics_exclude_retired_without_mutating_ledger():
    retired={'id':'old-ds-codex-max','agent':'codex','model':'deepseek-v4-pro','effort':'max'}
    retained={'id':'sol-high','agent':'codex','model':'gpt-6.1-sol','effort':'high'}
    value={'configs':[retired,retained],'models':[retired,retained],
           'rows':[{'task_id':'one','cells':{'old-ds-codex-max':{'score':1},'sol-high':{'score':.5}}}],
           'model_scores':{'old-ds-codex-max':1,'sol-high':.5},'model_stats':{'old-ds-codex-max':{},'sol-high':{}},
           'coverage':32,'statistics':{'cost':100},'scores':{'all':.9},'totals':{'done':32},
           'ledger':[{'historical_config_id':'old-ds-codex-max','cost':100}]}
    original=copy.deepcopy(value);out=current_catalog(value)
    assert value==original
    assert out['configs']==[retained] and out['models']==[retained]
    assert set(out['rows'][0]['cells'])=={'sol-high'}
    assert set(out['model_scores'])==set(out['model_stats'])=={'sol-high'}
    assert all(out[k]is None for k in ['coverage','statistics','scores','totals'])
    assert out['ledger']==value['ledger']
    assert out['aggregate_missing_reason']=='upstream_snapshot_contains_retired_codex_deepseek'

def test_legacy_injected_bootstrap_cannot_show_retired_choices():
    value={'limits':{'max_concurrency':2,'max_total_count':1},'benchmarks':[{'benchmark':'science','models':[{'model':'deepseek-v4-flash','effort':'max'},{'model':'gpt-6.1-sol','effort':'high'}]}]}
    cat=Catalog.from_bootstrap(value)
    assert cat.models_by_benchmark['science']==('gpt-6.1-sol',)
    assert len(value['benchmarks'][0]['models'])==2

def test_explicit_capability_headers_cannot_reenable_retired_lanes():
    caps=normalize_capabilities([*RETIRED_CODEX_CAPABILITIES,'dsh-deepseek-v4-flash-v1','codex-gpt6-1-sol-host-remote-v1'])
    assert not set(caps)&RETIRED_CODEX_CAPABILITIES
    assert 'dsh-deepseek-v4-flash-v1'in caps
    assert 'codex-gpt6-1-sol-host-remote-v1'in caps

def test_old_saved_run_can_be_opened_for_recovery_but_not_initialized(tmp_path):
    j=Journal(tmp_path/'state');api=SimpleNamespace(journal=j,bootstrap=lambda:pytest.fail('bootstrap reached'))
    c=Controller(api,None,{'benchmark':'science','model':'deepseek-v4-pro','effort':'max','agent':'codex','total_count':1,'concurrency':1})
    try:
        assert c.journal.value('configuration') and c.futures=={}
        with pytest.raises(ValueError,match='any benchmark'):c.initialize()
        assert not any(r.operation=='run:create'for r in j.requests())
    finally:c.pool.shutdown()

@pytest.mark.parametrize('operation,path',[
    ('run:create','/api/v2/runs'),
    ('claim:0:0','/api/v2/runs/old-run/claim'),
    ('start:old-assignment','/api/v2/assignments/old-assignment/start'),
])
def test_saved_retired_journal_cannot_bypass_write_boundary(tmp_path,operation,path):
    from dradar.v2.client import Client
    j=Journal(tmp_path/'state')
    config={'agent':'codex','model':'deepseek-v4-pro'}
    import json
    j.bind('configuration',json.dumps(config))
    client=object.__new__(Client);client.journal=j;client._bootstrap=None
    client.bootstrap=lambda:pytest.fail('bootstrap reached')
    req=j.prepare(operation,path,config if operation=='run:create'else{})
    with pytest.raises(ValueError,match='any benchmark'):client.send(req)
    assert j.requests()[0].response is None

def test_runtime_rejects_before_execution_fence_or_provider_setup(tmp_path):
    a={'assignment_id':'a'*32,'agent':'codex','model':'deepseek-v4.1-flash','provider':'deepseek','benchmark_id':'science'}
    with pytest.raises(runner.RunnerError,match='any benchmark'):
        runner._run_trial(a,tmp_path,tmp_path)
    with pytest.raises(ValueError,match='any benchmark'):
        normalize_assignment({'task':{'model':'deepseek-v4-pro'},'runner':{'agent':'codex','provider':'deepseek'}})
