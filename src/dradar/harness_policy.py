"""Current harness combinations, independent of benchmark and history storage.

Retired Codex/DeepSeek IDs remain available to decode old evidence, but must
never appear in current menus or accept new configuration/claim/execution.
"""
from __future__ import annotations
import copy

RETIRED_CODEX_DEEPSEEK_MODELS=frozenset({
 'deepseek-v4-flash','deepseek-v4-pro','deepseek-v4.1-flash','deepseek-flash',
})
RETIRED_CODEX_CAPABILITIES=frozenset({
 'codex-deepseek-v4-flash-v2','codex-deepseek-v4-pro-v1','codex-deepseek-v4.1-flash-v1',
 'codex-deepseek-v4-flash-off-v1','codex-deepseek-v4-pro-off-v1','codex-deepseek-v4.1-flash-off-v1',
})
CODEX_HARNESSES=frozenset({'codex','openai'})

def retired_combination(agent='codex',model=None,provider=None):
    if agent in [None,'']:agent='codex'
    if not isinstance(agent,str)or agent.strip().lower().replace('_','-')not in CODEX_HARNESSES:
        return False
    m=model.strip().lower()if isinstance(model,str)else''
    p=provider.strip().lower()if isinstance(provider,str)else''
    return m=='gpt-5.5' or p=='deepseek' or m in RETIRED_CODEX_DEEPSEEK_MODELS or m.startswith('deepseek-')

def reject_retired_combination(agent='codex',model=None,provider=None):
    if retired_combination(agent,model,provider):
        if isinstance(model,str) and model.strip().lower()=='gpt-5.5':
            raise ValueError('Codex Harness gpt-5.5 is retired for new work; preserve historical results')
        raise ValueError('Codex Harness no longer supports DeepSeek on any benchmark; history is retained outside current display/statistics')

def current_catalog(value):
    """Copy current model/menu/cell rows; never mutate durable history/inventory.

    The Server owns authoritative aggregate calculations. If an older snapshot
    included retired rows, aggregate coverage is explicitly unknown locally;
    removing visible rows must not leave a misleading full-library score.
    """
    result=copy.deepcopy(value)
    excluded=0
    retired_gpt55=False
    retired_ids=set()
    for row in value.get('configs',[]):
        if isinstance(row,dict)and retired_combination(row.get('agent',row.get('harness','codex')),row.get('model'),row.get('provider')):
            retired_ids.update(row[k] for k in ['id','config_id'] if isinstance(row.get(k),str))
    for row in result.get('rows',[]):
        if isinstance(row,dict)and isinstance(row.get('cells'),dict):
            row['cells']={k:v for k,v in row['cells'].items()if k not in retired_ids}
    for key in ['model_scores','model_stats']:
        if isinstance(result.get(key),dict):result[key]={k:v for k,v in result[key].items()if k not in retired_ids}
    def filter_rows(rows):
        nonlocal excluded, retired_gpt55
        if not isinstance(rows,list):return rows
        kept=[]
        for row in rows:
            if isinstance(row,dict) and (row.get('config_id') in retired_ids or retired_combination(row.get('agent',row.get('harness','codex')),row.get('model'),row.get('provider'))):
                if isinstance(row.get('model'),str) and row['model'].strip().lower()=='gpt-5.5':
                    retired_gpt55=True
                excluded+=1;continue
            kept.append(row)
        return kept
    for key in ['models','menu','cells','configs']:
        if key in result:result[key]=filter_rows(result[key])
    for b in result.get('benchmarks',[]):
        if isinstance(b,dict)and 'models'in b:b['models']=filter_rows(b['models'])
    if excluded:
        result['harness_policy_excluded_rows']=excluded
        for key in ['coverage','statistics','scores','totals']:
            if key in result:result[key]=None
        result['aggregate_missing_reason']=('upstream_snapshot_contains_retired_codex_gpt_5_5' if retired_gpt55
            else 'upstream_snapshot_contains_retired_codex_deepseek')
    return result
