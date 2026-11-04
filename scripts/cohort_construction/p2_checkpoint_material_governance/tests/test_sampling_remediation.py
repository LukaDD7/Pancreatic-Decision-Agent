"""V061-AUD008 and GAP03: fictitious packets, no models or patient data."""
from copy import deepcopy

import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.sampling import (
    freeze_sampling_plan, select_packets, summarize_human_sample,
)
from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError, canonical_sha256


@pytest.fixture
def packets():
    return [dict(packet_id=pid, packet_sha256=char * 64, stratum_id=sid)
            for pid, char, sid in [('op-a', '1', 'operative'), ('op-b', '2', 'operative'),
                                   ('sup-a', '3', 'support'), ('sup-b', '4', 'support'),
                                   ('sup-c', '5', 'support')]]


@pytest.fixture
def specification():
    return dict(plan_id='fictional-v02', protocol_version='synthetic-protocol',
        population_version='synthetic-v1', seed=17, sample_size=2,
        strata=[dict(stratum_id='operative', n=1), dict(stratum_id='support', n=1)],
        critical_defect_limit=0, ordinary_defect_limit=1, confidence_level=0.95,
        unresolved_policy='not_accept', reviewer_independence_policy='declaration required',
        expansion_policy='freeze expansion before additional sampling')


@pytest.fixture
def plan(specification, packets):
    return freeze_sampling_plan(specification, packets)


def response(plan, a, **changes):
    result=dict(packet_id=a['packet_id'], packet_sha256=a['packet_sha256'],
                reviewer_type='human', reviewer_id='fictional-reviewer-2',
                reviewed_at='2026-10-29T12:01:00+08:00', independence_attested=True,
                frozen_plan_sha256=plan['frozen_plan_sha256'],
                assignment_manifest_sha256=plan['assignment_manifest_sha256'],
                status='HUMAN_SAMPLE_ACCEPT', critical_defects=0, ordinary_defects=0)
    result.update(changes)
    return result


def test_AUD008_frozen_population_and_seeded_assignment_cannot_be_substituted(plan, packets):
    assert select_packets(plan, list(reversed(packets))) == plan['assignment_manifest']
    assert canonical_sha256(plan['population_manifest']) == plan['population_manifest_sha256']
    assert canonical_sha256(plan['assignment_manifest']) == plan['assignment_manifest_sha256']
    changed=deepcopy(packets)
    changed[-1]['packet_sha256']='6'*64
    with pytest.raises(ContractError, match='POPULATION_CHANGED'):
        select_packets(plan, changed)
    with pytest.raises(ContractError, match='POPULATION_CHANGED'):
        select_packets(plan, packets[:-1])
    duplicate=deepcopy(packets)+[deepcopy(packets[0])]
    with pytest.raises(ContractError, match='ONE_STRATUM'):
        select_packets(plan, duplicate)


@pytest.mark.parametrize('mutation', ['wrong_quota', 'same_stratum_substitution', 'reorder', 'hash', 'stratum'])
def test_AUD008_assignment_binding_rejects_count_preserving_changes(plan, packets, mutation):
    assignments=deepcopy(plan['assignment_manifest'])
    selected={a['packet_id'] for a in assignments}
    if mutation=='wrong_quota':
        assignments[1]=next(deepcopy(p) for p in packets if p['stratum_id']=='operative' and p['packet_id'] not in selected)
    elif mutation=='same_stratum_substitution':
        assignments[1]=next(deepcopy(p) for p in packets if p['stratum_id']=='support' and p['packet_id'] not in selected)
    elif mutation=='reorder': assignments.reverse()
    elif mutation=='hash': assignments[0]['packet_sha256']='0'*64
    else: assignments[0]['stratum_id']='support'
    with pytest.raises(ContractError, match='ASSIGNMENT_BINDING_MISMATCH'):
        summarize_human_sample(plan, assignments, [])


def test_AUD008_recomputed_outer_hash_does_not_hide_manifest_disagreement(plan):
    plan['assignment_manifest'].reverse()
    plan['assignment_manifest_sha256']=canonical_sha256(plan['assignment_manifest'])
    plan['frozen_plan_sha256']=canonical_sha256({k:v for k,v in plan.items() if k!='frozen_plan_sha256'})
    with pytest.raises(ContractError, match='FROZEN_MANIFEST_MISMATCH'):
        summarize_human_sample(plan, plan['assignment_manifest'], [])


@pytest.mark.parametrize('field,value,error', [
    ('frozen_plan_sha256', '0'*64, 'TASK_BINDING'),
    ('assignment_manifest_sha256', '0'*64, 'TASK_BINDING'),
    ('reviewer_id', '', 'REVIEWER_ID'),
    ('reviewed_at', '2026-10-29T12:00:00', 'REVIEW_TIME'),
    ('reviewer_type', 'llm_material_review', 'ACTUAL_INDEPENDENT'),
    ('independence_attested', False, 'ACTUAL_INDEPENDENT'),
    ('stratum_id', 'different', 'STRATUM_BINDING'),
])
def test_AUD008_recovery_requires_actual_reviewer_declarations_and_task_binding(plan, field, value, error):
    assignments=plan['assignment_manifest']
    with pytest.raises(ContractError, match=error):
        summarize_human_sample(plan, assignments, [response(plan, assignments[0], **{field:value})])


def test_GAP03_layer_denominators_packet_defects_and_no_population_inference(plan):
    assignments=plan['assignment_manifest']
    responses=[response(plan, assignments[0], ordinary_defects=3, status='HUMAN_SAMPLE_REVISE'),
               response(plan, assignments[1])]
    result=summarize_human_sample(plan, assignments, responses)
    assert [(s['population_N'], s['assigned_n'], s['received']) for s in result['strata']]==[(2,1,1),(3,1,1)]
    assert result['ordinary_defects']==3
    assert result['ordinary_defect_packets']==1
    assert result['defect_denominator']==2
    assert result['ordinary_defect_packet_rate_wilson'][0] < 0.5 < result['ordinary_defect_packet_rate_wilson'][1]
    assert result['strata'][0]['ordinary_defect_packets']==1
    assert result['strata'][0]['defect_denominator']==1
    assert result['descriptive_sample_summary_complete'] is True
    assert result['statistical_validation_complete'] is False
    assert result['population_inference_permitted'] is False
    assert result['automatic_release_permitted'] is False
    assert result['clinical_release_permitted'] is False
    assert result['statistical_design']['evaluation_unit']=='checkpoint_packet'
    assert result['statistical_design']['estimation_target']=='quota_sample_descriptive'
    assert result['statistical_design']['population_inference']=='not_authorized'
    assert result['identity_independence_verification']=='declarations_checked_identity_not_authenticated'


def test_GAP03_missing_layer_response_is_not_success_or_completed_validation(plan):
    assignments=plan['assignment_manifest']
    result=summarize_human_sample(plan, assignments, [response(plan, assignments[0])])
    assert result['strata'][1]['received']==0
    assert result['strata'][1]['not_received']==1
    assert result['strata'][1]['defect_denominator']==0
    assert result['strata'][1]['ordinary_defect_packet_rate_wilson'] is None
    assert result['descriptive_sample_summary_complete'] is False
    assert result['statistical_validation_complete'] is False
    assert result['meets_frozen_count_thresholds'] is False
    assert result['human_review_state']=='INCOMPLETE'
    assert result['automated_review_state']=='NOT_EVALUATED_BY_HUMAN_SAMPLE_SUMMARY'


def test_AUD008_legacy_unbound_plan_cannot_be_used_as_new_freeze(specification):
    with pytest.raises(ContractError, match='FROZEN_POPULATION_REQUIRED'):
        freeze_sampling_plan(specification)
