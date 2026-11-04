"""Fictional, deterministic checks for design-based human sample acceptance."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_delivery_audit.sampling import (
    evaluate_human_sample, freeze_ready_sampling_design, freeze_statistical_sampling_plan,
    validate_human_sample_assessment,
)


def statistical_fixture(*, interval=False):
    population = [dict(unit_id='U-' + str(index), packet_id='PK-' + str(index),
        packet_sha256=canonical_sha256('packet-' + str(index)),
        episode_material_set_sha256=canonical_sha256('material-' + str(index)),
        delivery_input_sha256=canonical_sha256('delivery-' + str(index)),
        stratum_id='A' if index < 8 else 'B', patient_key='PATIENT-A' if index < 2 else 'PATIENT-' + str(index),
        episode_id='EPISODE-A' if index < 2 else 'EPISODE-' + str(index)) for index in range(10)]
    criteria = dict(estimands=[dict(estimand_id='LEAKAGE', defect_type='critical_leakage')],
        stratum_weights=dict(A=2, B=1), cluster_unit='patient',
        severity_thresholds=dict(LEAKAGE=dict(metric='upper_confidence_limit' if interval else 'point_estimate',
            maximum=0.25, confidence_level=0.95)),
        missing_and_unresolved=dict(policy='complete_response_required'),
        expansion_and_full_review_triggers=[dict(trigger_id='EXPAND-ON-FAIL', on='SAMPLING_FAILED',
            action='NEW_EXPANSION_PLAN_REQUIRED')])
    plan_identity = dict(plan_id='SYNTHETIC-STATISTICAL-PLAN', seed=23, allocations=dict(A=3, B=1))
    design = freeze_ready_sampling_design(design_id='SYNTHETIC-EXTERNAL-POLICY',
        pipeline_binding_sha256=canonical_sha256('pipeline'), population_manifest_sha256=canonical_sha256(population),
        plan_sha256=canonical_sha256(plan_identity), statistical_acceptance_criteria=criteria)
    plan = freeze_statistical_sampling_plan(design=design, population=population, **plan_identity)
    responses = []
    for index, unit in enumerate(plan['assignments']):
        responses.append(dict(**{key: unit[key] for key in ('assignment_id', 'unit_id', 'packet_id', 'packet_sha256',
            'episode_material_set_sha256', 'delivery_input_sha256', 'stratum_id', 'patient_key', 'episode_id')},
            design_sha256=design['design_sha256'], frozen_plan_sha256=plan['frozen_plan_sha256'],
            assignment_manifest_sha256=plan['assignment_manifest_sha256'],
            reviewer_id='SYNTHETIC-HUMAN-DECLARATION', reviewer_type='human', independence_attested=True,
            reviewed_at=datetime(2026, 10, 29, tzinfo=timezone.utc).isoformat(), status='HUMAN_SAMPLE_ACCEPT',
            defects=dict(critical_leakage=int(index == 1))))
    return design, plan, responses


def test_stratified_estimate_clustering_and_replay_not_raw_quota_rate():
    design, plan, responses = statistical_fixture()
    assessment = evaluate_human_sample(design=design, plan=plan, responses=responses)
    estimate = assessment['estimates'][0]
    assert estimate['point_estimate'] == pytest.approx(2 / 7)
    assert estimate['cluster_count'] == 4
    assert estimate['variance_df'] == 3
    assert assessment['status'] == 'SAMPLING_FAILED'
    assert assessment['trigger_actions'][0]['new_plan_required']
    assert validate_human_sample_assessment(assessment, design=design, plan=plan) == assessment
    forged = deepcopy(assessment); forged['status'] = 'SAMPLING_QUALIFIED'
    with pytest.raises(ValueError, match='REPLAY_MISMATCH'):
        validate_human_sample_assessment(forged, design=design, plan=plan)


def test_missing_unresolved_zero_variance_and_tampering_fail_closed():
    design, plan, responses = statistical_fixture(interval=True)
    pending = evaluate_human_sample(design=design, plan=plan, responses=responses[:-1])
    assert pending['status'] == 'SAMPLING_VALIDATION_PENDING'
    unresolved = deepcopy(responses); unresolved[0]['status'] = 'HUMAN_SAMPLE_UNRESOLVED'
    assert evaluate_human_sample(design=design, plan=plan, responses=unresolved)['status'] == 'SAMPLING_FAILED'
    for response in responses:
        response['defects']['critical_leakage'] = 0
    zero = evaluate_human_sample(design=design, plan=plan, responses=responses)
    assert zero['acceptance_checks'][0]['reason_code'] == 'ZERO_CLUSTER_VARIANCE_REQUIRES_FROZEN_RESOLUTION'
    wrong = deepcopy(responses); wrong[0]['patient_key'] = 'OTHER-PATIENT'
    with pytest.raises(ValueError, match='ASSIGNMENT_BINDING'):
        evaluate_human_sample(design=design, plan=plan, responses=wrong)
    with pytest.raises(ValueError, match='DUPLICATE'):
        evaluate_human_sample(design=design, plan=plan, responses=responses + [responses[0]])
    wrong_plan = deepcopy(plan); wrong_plan['assignments'][0]['patient_key'] = 'OTHER-PATIENT'
    with pytest.raises(ValueError, match='NOT_FROZEN_OR_CHANGED'):
        evaluate_human_sample(design=design, plan=wrong_plan, responses=responses)


def test_unresolved_policy_is_explicit_and_cannot_silently_pass():
    design, plan, responses = statistical_fixture(interval=True)
    unresolved = deepcopy(responses)
    for index, response in enumerate(unresolved):
        response['status'] = 'HUMAN_SAMPLE_UNRESOLVED' if index == 0 else 'HUMAN_SAMPLE_ACCEPT'
        response['defects']['critical_leakage'] = 0
    failed = evaluate_human_sample(design=design, plan=plan, responses=unresolved)
    assert failed['acceptance_checks'][0]['reason_code'] == 'UNRESOLVED_RESPONSE_NOT_QUALIFIED'
    changed_criteria = deepcopy(design['statistical_acceptance_criteria'])
    changed_criteria['missing_and_unresolved'] = dict(policy='unresolved_as_defect')
    as_defect_design = freeze_ready_sampling_design(design_id='SYNTHETIC-UNRESOLVED-AS-DEFECT',
        pipeline_binding_sha256=design['pipeline_binding_sha256'],
        population_manifest_sha256=design['population_manifest_sha256'], plan_sha256=design['plan_sha256'],
        statistical_acceptance_criteria=changed_criteria)
    as_defect_plan = freeze_statistical_sampling_plan(plan_id=plan['plan_id'], seed=plan['seed'],
        design=as_defect_design, population=plan['population_manifest'], allocations=plan['allocations'])
    for response in unresolved:
        response.update(design_sha256=as_defect_design['design_sha256'],
            frozen_plan_sha256=as_defect_plan['frozen_plan_sha256'],
            assignment_manifest_sha256=as_defect_plan['assignment_manifest_sha256'])
    assessment = evaluate_human_sample(design=as_defect_design, plan=as_defect_plan, responses=unresolved)
    assert assessment['estimates'][0]['observation_rows'][0]['defect_indicator'] == 1
    assert assessment['acceptance_checks'][0]['reason_code'] != 'UNRESOLVED_RESPONSE_NOT_QUALIFIED'


def test_single_and_multi_episode_population_bindings_are_distinct():
    design, plan, responses = statistical_fixture()
    assessment = evaluate_human_sample(design=design, plan=plan, responses=responses)
    assert assessment['population_scope'] == 'MULTI_EPISODE'
    single_population = [deepcopy(plan['population_manifest'][0])]
    single_population[0]['stratum_id'] = 'A'
    criteria = deepcopy(design['statistical_acceptance_criteria'])
    criteria['stratum_weights'] = dict(A=1)
    identity = dict(plan_id='SYNTHETIC-SINGLE', seed=1, allocations=dict(A=1))
    single_design = freeze_ready_sampling_design(design_id='SYNTHETIC-SINGLE-DESIGN',
        pipeline_binding_sha256=design['pipeline_binding_sha256'], population_manifest_sha256=canonical_sha256(single_population),
        plan_sha256=canonical_sha256(identity), statistical_acceptance_criteria=criteria)
    single_plan = freeze_statistical_sampling_plan(design=single_design, population=single_population, **identity)
    assert single_plan['population_manifest'][0]['episode_id'] == 'EPISODE-A'
def test_plan_requires_full_population_and_real_human_declaration():
    design, plan, responses = statistical_fixture()
    for response in responses:
        response['defects']['critical_leakage'] = 0
    accepted = evaluate_human_sample(design=design, plan=plan, responses=responses)
    assert accepted['status'] == 'SAMPLING_QUALIFIED'
    revised = deepcopy(responses)
    revised[0]['status'] = 'HUMAN_SAMPLE_REVISE'
    assert evaluate_human_sample(design=design, plan=plan, responses=revised)['status'] == 'SAMPLING_FAILED'
    assert not accepted['actual_reviewer_identity_authenticated']
    assert not accepted['clinical_release_permitted']
    no_human = deepcopy(responses); no_human[0]['reviewer_type'] = 'llm_episode_delivery_audit'
    with pytest.raises(ValueError, match='HUMAN_DECLARATION'):
        evaluate_human_sample(design=design, plan=plan, responses=no_human)
    short = plan['population_manifest'][:-1]
    with pytest.raises(ValueError, match='POPULATION_BINDING'):
        freeze_statistical_sampling_plan(plan_id=plan['plan_id'], seed=plan['seed'], design=design,
            population=short, allocations=plan['allocations'])
