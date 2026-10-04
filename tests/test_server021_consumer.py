import copy
import json
from pathlib import Path

import pytest

from dradar.v2.host_contract import (AUTH_RUNTIME, EFFORTS, MODEL, MODEL_CAPABILITY,
                                    SERVER_CATALOG_VERSION, require_server_library)
from dradar.v2.presentation import assignment_view, run_view
from dradar.v2.protocol import assignment


SAMPLES = json.loads((Path(__file__).parent / 'fixtures/server022_consumer_samples.json').read_text())


@pytest.mark.parametrize('phase,grade,points,label', [
    ('submitted_pending_grade', 'pending', None, '待结算'),
    ('graded_pending_reward', 'graded', None, '待结算'),
    ('graded_reward_settled', 'graded', 6.0, '已结算'),
])
def test_exact_installed_server022_samples_keep_grade_and_points_separate(phase, grade, points, label):
    original = SAMPLES['assignments'][phase]
    value = copy.deepcopy(original)
    assignment(value, run_id=value['run_id'], device_id=value['device_id'])
    view = assignment_view(value)
    assert view['grading'] == grade
    assert view['earned_points'] == points
    assert view['reward'] == value['grading']['reward']
    assert view['score'] == value['grading']['score']
    assert view['passed'] == value['grading']['passed']
    assert view['flagged'] == value['grading']['flagged']
    assert view['reason_code'] == value['grading']['reason_code']
    assert view['graded_at'] == value['grading']['graded_at']
    if grade == 'graded':
        assert view['reward'] == 0 and view['passed'] is False
    assert view['points_status_text'] == label
    assert view['contribution']['points_base'] == value['contribution']['points_base']
    assert value == original
    assert 'lease_id' not in view and 'basis_source' not in view['contribution']
    # Synthetic run envelope around the captured Server assignment; exercise CLI status.
    snapshot = {'run': {'run_id': value['run_id'], 'state': 'completed',
                       'total_count': 1, 'concurrency': 1,
                       'counts': {'started': 1, 'submitted': 1, 'uncertain': 0}},
                'assignments': [value]}
    assert run_view(snapshot)['tasks'] == [view]


def test_claim_contract_deferred_is_visible_without_inventing_grade_or_points():
    value = copy.deepcopy(SAMPLES['assignments']['submitted_pending_grade'])
    value.pop('grading')
    value['state'] = 'leased'
    view = assignment_view(value)
    assert view['grading'] == 'unknown' and view['earned_points'] is None
    assert view['points_status_text'] == '待结算'


def test_unknown_points_stay_null_and_settled_zero_is_real_zero():
    value = copy.deepcopy(SAMPLES['assignments']['graded_reward_settled'])
    value['grading']['earned_points'] = 0
    assert assignment_view(value)['earned_points'] == 0
    assert assignment_view(value)['points_status_text'] == '已结算'
    value.pop('grading'); value.pop('contribution')
    view = assignment_view(value)
    assert view['earned_points'] is None and view['points_status_text'] == '未知'
    assert view['points_settlement_state'] == 'unknown'


def bootstrap():
    return {'contribution_policy': copy.deepcopy(SAMPLES['contribution_policy']),
            'library_catalog': {'catalog_version': SERVER_CATALOG_VERSION, 'collections': [{
                'benchmark': 'science-sr-pilot-20261003',
                'required_client_capabilities': [MODEL_CAPABILITY, AUTH_RUNTIME],
                'production_claim_enabled': True, 'missing_bindings': [],
                'model_effort_selections': [{'model': MODEL, 'effort': effort} for effort in EFFORTS],
            }]}}


def test_021_policy_accepts_unchanged_catalog_and_preserves_paused_gate():
    boot = bootstrap()
    for effort in EFFORTS:
        require_server_library(boot, 'science-sr-pilot-20261003', MODEL, effort)
    boot['library_catalog']['collections'][0]['production_claim_enabled'] = False
    with pytest.raises(ValueError, match='pending'):
        require_server_library(boot, 'science-sr-pilot-20261003', MODEL, 'low')


@pytest.mark.parametrize('change', ['old020', 'missing_null_field', 'wrong_policy'])
def test_catalog020_alone_cannot_claim_021_deferred_contract(change):
    boot = bootstrap()
    if change == 'old020':
        boot.pop('contribution_policy')
    elif change == 'missing_null_field':
        boot['contribution_policy'].pop('points_until_basis_resolved')
    else:
        boot['contribution_policy']['missing_reward_basis'] = 'block'
    with pytest.raises(ValueError, match='Server021 deferred contribution policy'):
        require_server_library(boot, 'science-sr-pilot-20261003', MODEL, 'low')
