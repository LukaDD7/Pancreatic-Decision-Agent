"""Replayable stratified, cluster-aware acceptance statistics.

All estimands, thresholds and missing-data policies come from an externally
frozen design. This module computes them; it never chooses clinical limits or
authenticates a reviewer. Fictional responses cannot demonstrate real quality.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import math
from statistics import NormalDist

from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256

FORMAT = 'frozen-ready-resource-sampling-design-0.1.0'
PLAN_FORMAT = 'ready-resource-statistical-sampling-plan-0.1.0'
ASSESSMENT_FORMAT = 'actual-human-sample-statistical-assessment-0.2.0'
REQUIRED_FLAGS = ('population_frozen', 'strata_and_quotas_frozen',
    'estimand_and_weights_frozen', 'patient_episode_clustering_frozen',
    'severity_specific_thresholds_frozen', 'missing_and_unresolved_policy_frozen',
    'expansion_and_full_review_triggers_frozen', 'descriptive_wilson_is_not_release_gate')
_REQUIRED_CRITERIA = ('estimands', 'stratum_weights', 'cluster_unit', 'severity_thresholds',
    'missing_and_unresolved', 'expansion_and_full_review_triggers')
_HASH_FIELDS = ('pipeline_binding_sha256', 'population_manifest_sha256', 'plan_sha256')
_POPULATION_FIELDS = {'unit_id', 'packet_id', 'packet_sha256', 'episode_material_set_sha256',
                      'delivery_input_sha256', 'stratum_id', 'patient_key', 'episode_id'}


def _hash(value, name):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('READY_SAMPLING_' + name.upper() + '_INVALID')
    return value


def _criteria(value):
    if not isinstance(value, dict) or any(key not in value for key in _REQUIRED_CRITERIA):
        raise ValueError('READY_SAMPLING_ACCEPTANCE_COMPONENT_MISSING')
    result = deepcopy(value)
    if not isinstance(result['estimands'], list) or not result['estimands']:
        raise ValueError('READY_ESTIMANDS_REQUIRED')
    ids = []
    for estimand in result['estimands']:
        if (not isinstance(estimand, dict)
                or not isinstance(estimand.get('estimand_id'), str) or not estimand['estimand_id']
                or not isinstance(estimand.get('defect_type'), str) or not estimand['defect_type']):
            raise ValueError('READY_ESTIMAND_IDENTITY_REQUIRED')
        ids.append(estimand['estimand_id'])
    if len(ids) != len(set(ids)):
        raise ValueError('READY_DUPLICATE_ESTIMAND')
    if not isinstance(result['stratum_weights'], dict) or not result['stratum_weights']:
        raise ValueError('READY_STRATUM_WEIGHTS_REQUIRED')
    for stratum, weight in result['stratum_weights'].items():
        if not isinstance(stratum, str) or type(weight) not in (int, float) or not math.isfinite(weight) or weight <= 0:
            raise ValueError('READY_STRATUM_WEIGHT_INVALID')
    if result['cluster_unit'] not in ('patient', 'episode', 'checkpoint'):
        raise ValueError('READY_CLUSTER_UNIT_UNSUPPORTED')
    if not isinstance(result['severity_thresholds'], dict):
        raise ValueError('READY_THRESHOLDS_REQUIRED')
    for estimand_id in ids:
        threshold = result['severity_thresholds'].get(estimand_id)
        if (not isinstance(threshold, dict)
                or threshold.get('metric') not in ('point_estimate', 'upper_confidence_limit')
                or type(threshold.get('maximum')) not in (int, float)
                or not math.isfinite(threshold['maximum']) or not 0 <= threshold['maximum'] <= 1
                or threshold.get('confidence_level') not in (0.9, 0.95, 0.99)):
            raise ValueError('READY_ESTIMAND_THRESHOLD_INVALID:' + estimand_id)
    missing = result['missing_and_unresolved']
    if not isinstance(missing, dict) or missing.get('policy') not in ('complete_response_required', 'unresolved_as_defect'):
        raise ValueError('READY_MISSING_POLICY_UNSUPPORTED')
    triggers = result['expansion_and_full_review_triggers']
    if not isinstance(triggers, list) or any(not isinstance(item, dict) or not isinstance(item.get('trigger_id'), str)
            or item.get('action') not in ('NEW_EXPANSION_PLAN_REQUIRED', 'FULL_REVIEW_REQUEST_REQUIRED')
            or item.get('on') not in ('SAMPLING_FAILED', 'SAMPLING_VALIDATION_PENDING') for item in triggers):
        raise ValueError('READY_EXPANSION_TRIGGERS_INVALID')
    return result


def freeze_ready_sampling_design(*, design_id, pipeline_binding_sha256,
                                 population_manifest_sha256, plan_sha256,
                                 statistical_acceptance_criteria):
    """Freeze externally selected estimands and acceptance policy, not results."""
    if not isinstance(design_id, str) or not design_id:
        raise ValueError('READY_SAMPLING_DESIGN_ID_REQUIRED')
    for name, value in zip(_HASH_FIELDS, (pipeline_binding_sha256, population_manifest_sha256, plan_sha256)):
        _hash(value, name)
    result = dict(format=FORMAT, design_id=design_id,
        pipeline_binding_sha256=pipeline_binding_sha256,
        population_manifest_sha256=population_manifest_sha256, plan_sha256=plan_sha256,
        statistical_acceptance_criteria=_criteria(statistical_acceptance_criteria),
        **{key: True for key in REQUIRED_FLAGS}, executed=False,
        clinical_release_permitted=False)
    result['design_sha256'] = canonical_sha256(result)
    return result


def _design(design, criteria):
    if (not isinstance(design, dict) or design.get('format') != FORMAT
            or design.get('design_sha256') != canonical_sha256({k: v for k, v in design.items() if k != 'design_sha256'})
            or any(design.get(key) is not True for key in REQUIRED_FLAGS)
            or design.get('executed') is not False or design.get('clinical_release_permitted') is not False):
        raise ValueError('READY_DESIGN_NOT_FROZEN_OR_CHANGED')
    if _criteria(design.get('statistical_acceptance_criteria')) != _criteria(criteria):
        raise ValueError('READY_STATISTICAL_DESIGN_MISMATCH')
    for name in _HASH_FIELDS:
        _hash(design.get(name), name)
    return design['statistical_acceptance_criteria']


def _population(population, criteria):
    if not isinstance(population, list) or not population:
        raise ValueError('READY_POPULATION_REQUIRED')
    result, ids, packets = [], set(), set()
    for unit in population:
        if not isinstance(unit, dict) or set(unit) != _POPULATION_FIELDS:
            raise ValueError('READY_POPULATION_FIELDS_MISMATCH')
        if any(not isinstance(unit.get(key), str) or not unit[key] for key in _POPULATION_FIELDS):
            raise ValueError('READY_POPULATION_IDENTITY_REQUIRED')
        for key in ('packet_sha256', 'episode_material_set_sha256', 'delivery_input_sha256'):
            _hash(unit[key], key)
        if unit['stratum_id'] not in criteria['stratum_weights']:
            raise ValueError('READY_POPULATION_STRATUM_NOT_IN_DESIGN')
        if unit['unit_id'] in ids or unit['packet_id'] in packets:
            raise ValueError('READY_DUPLICATE_POPULATION_UNIT')
        ids.add(unit['unit_id']); packets.add(unit['packet_id'])
        result.append(deepcopy(unit))
    if set(criteria['stratum_weights']) != {item['stratum_id'] for item in result}:
        raise ValueError('READY_DESIGN_STRATUM_NOT_IN_POPULATION')
    return sorted(result, key=lambda item: item['unit_id'])


def _assign(seed, population, allocations):
    selected = []
    for stratum, n in allocations.items():
        eligible = [unit for unit in population if unit['stratum_id'] == stratum]
        if type(n) is not int or n < 1 or n > len(eligible):
            raise ValueError('READY_STRATUM_ALLOCATION_INVALID')
        ordered = sorted(eligible, key=lambda unit: (hashlib.sha256(
            (str(seed) + ':' + unit['unit_id']).encode()).hexdigest(), unit['unit_id']))
        selected.extend(dict(assignment_id='ASSIGN-' + unit['unit_id'], **deepcopy(unit))
                        for unit in ordered[:n])
    return selected


def freeze_statistical_sampling_plan(*, plan_id, seed, design, population, allocations):
    criteria = _design(design, design['statistical_acceptance_criteria'])
    frozen_population = _population(population, criteria)
    if (not isinstance(plan_id, str) or not plan_id or type(seed) is not int
            or not isinstance(allocations, dict) or set(allocations) != set(criteria['stratum_weights'])):
        raise ValueError('READY_PLAN_OR_ALLOCATIONS_INVALID')
    if design['plan_sha256'] != canonical_sha256(dict(plan_id=plan_id, seed=seed, allocations=allocations)):
        raise ValueError('READY_PLAN_BINDING_MISMATCH')
    if design['population_manifest_sha256'] != canonical_sha256(frozen_population):
        raise ValueError('READY_POPULATION_BINDING_MISMATCH')
    assignments = _assign(seed, frozen_population, allocations)
    result = dict(format=PLAN_FORMAT, plan_id=plan_id, seed=seed, allocations=deepcopy(allocations),
        design_sha256=design['design_sha256'], pipeline_binding_sha256=design['pipeline_binding_sha256'],
        population_manifest=frozen_population, population_manifest_sha256=canonical_sha256(frozen_population),
        assignments=assignments, assignment_manifest_sha256=canonical_sha256(assignments),
        executed=False, clinical_release_permitted=False)
    result['frozen_plan_sha256'] = canonical_sha256(result)
    return result


def _plan(plan, design):
    criteria = _design(design, design['statistical_acceptance_criteria'])
    if (not isinstance(plan, dict) or plan.get('format') != PLAN_FORMAT
            or plan.get('frozen_plan_sha256') != canonical_sha256({k: v for k, v in plan.items() if k != 'frozen_plan_sha256'})
            or plan.get('design_sha256') != design.get('design_sha256')
            or plan.get('pipeline_binding_sha256') != design.get('pipeline_binding_sha256')
            or plan.get('executed') is not False):
        raise ValueError('READY_PLAN_NOT_FROZEN_OR_CHANGED')
    expected = freeze_statistical_sampling_plan(plan_id=plan['plan_id'], seed=plan['seed'],
        design=design, population=plan['population_manifest'], allocations=plan['allocations'])
    if expected != plan:
        raise ValueError('READY_PLAN_REPLAY_MISMATCH')
    return criteria, expected['assignments']


def _responses(responses, plan):
    if not isinstance(responses, list):
        raise ValueError('READY_RESPONSE_LIST_REQUIRED')
    assigned = {item['unit_id']: item for item in plan['assignments']}
    received = {}
    for response in responses:
        if not isinstance(response, dict):
            raise ValueError('READY_RESPONSE_OBJECT_REQUIRED')
        uid = response.get('unit_id')
        if uid not in assigned or uid in received:
            raise ValueError('READY_RESPONSE_ASSIGNMENT_OR_DUPLICATE_INVALID')
        expected = assigned[uid]
        for key in ('assignment_id', 'packet_id', 'packet_sha256', 'episode_material_set_sha256',
                    'delivery_input_sha256', 'stratum_id', 'patient_key', 'episode_id'):
            if response.get(key) != expected[key]:
                raise ValueError('READY_RESPONSE_ASSIGNMENT_BINDING_MISMATCH:' + key)
        for key, value in (('design_sha256', plan['design_sha256']),
                           ('frozen_plan_sha256', plan['frozen_plan_sha256']),
                           ('assignment_manifest_sha256', plan['assignment_manifest_sha256'])):
            if response.get(key) != value:
                raise ValueError('READY_RESPONSE_PLAN_BINDING_MISMATCH')
        if (response.get('reviewer_type') != 'human' or response.get('independence_attested') is not True
                or not isinstance(response.get('reviewer_id'), str) or not response['reviewer_id'].strip()):
            raise ValueError('READY_ACTUAL_HUMAN_DECLARATION_REQUIRED')
        try:
            moment = datetime.fromisoformat(response['reviewed_at'])
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError('timezone')
        except (KeyError, TypeError, ValueError):
            raise ValueError('READY_REVIEWED_AT_REQUIRED') from None
        if response.get('status') not in ('HUMAN_SAMPLE_ACCEPT', 'HUMAN_SAMPLE_REVISE', 'HUMAN_SAMPLE_UNRESOLVED'):
            raise ValueError('READY_HUMAN_STATUS_INVALID')
        defects = response.get('defects')
        if (not isinstance(defects, dict)
                or any(type(count) is not int or count < 0 for count in defects.values())):
            raise ValueError('READY_DEFECT_COUNTS_INVALID')
        received[uid] = deepcopy(response)
    return received


def _cluster(unit, criteria):
    return {'patient': unit['patient_key'], 'episode': unit['episode_id'], 'checkpoint': unit['unit_id']}[criteria['cluster_unit']]


def _estimate(estimand, assignments, responses, criteria):
    weighted, score_totals, weights = Counter(), Counter(), Counter()
    rows, missing, unresolved = [], 0, 0
    for unit in assignments:
        response = responses.get(unit['unit_id'])
        if response is None:
            missing += 1
            continue
        is_unresolved = response['status'] == 'HUMAN_SAMPLE_UNRESOLVED'
        unresolved += is_unresolved
        defective = response['status'] == 'HUMAN_SAMPLE_REVISE' \
            or (is_unresolved and criteria['missing_and_unresolved']['policy'] == 'unresolved_as_defect') \
            or response['defects'].get(estimand['defect_type'], 0) > 0
        weight = criteria['stratum_weights'][unit['stratum_id']]
        cluster = _cluster(unit, criteria)
        weighted[cluster] += weight
        score_totals[cluster] += weight * int(defective)
        weights[unit['stratum_id']] += weight
        rows.append(dict(unit_id=unit['unit_id'], stratum_id=unit['stratum_id'], cluster_id=cluster,
            sampling_weight=weight, defect_indicator=int(defective), unresolved=is_unresolved))
    denominator = sum(weights.values())
    point = sum(score_totals.values()) / denominator if denominator else None
    total_weight = sum(weighted.values())
    variance, df = None, 0
    if point is not None and total_weight:
        contributions = {cluster: score / total_weight - point * weight / total_weight
                         for cluster, score, weight in
                         ((key, score_totals[key], weighted[key]) for key in weighted)}
        values = list(contributions.values())
        df = len(values) - 1
        if df > 0:
            variance = len(values) / df * sum(value * value for value in values)
    return dict(estimand_id=estimand['estimand_id'], defect_type=estimand['defect_type'],
        assigned_n=len(assignments), received_n=len(rows), missing_n=missing, unresolved_n=unresolved,
        point_estimate=point, cluster_count=len(weighted), cluster_variance=variance,
        cluster_robust_standard_error=math.sqrt(variance) if variance is not None else None,
        variance_df=df, observation_rows=rows)


def _acceptance(estimate, threshold, criteria):
    if estimate['missing_n']:
        return False, 'MISSING_RESPONSE_NOT_QUALIFIED'
    if estimate['point_estimate'] is None:
        return False, 'ESTIMAND_NOT_IDENTIFIABLE'
    if estimate['unresolved_n'] and criteria['missing_and_unresolved']['policy'] == 'complete_response_required':
        return False, 'UNRESOLVED_RESPONSE_NOT_QUALIFIED'
    if threshold['metric'] == 'upper_confidence_limit' and estimate['variance_df'] < 1:
        return False, 'INSUFFICIENT_CLUSTER_DF'
    if threshold['metric'] == 'point_estimate':
        value = estimate['point_estimate']
    else:
        if estimate['cluster_variance'] == 0:
            return False, 'ZERO_CLUSTER_VARIANCE_REQUIRES_FROZEN_RESOLUTION'
        z = NormalDist().inv_cdf(threshold['confidence_level'])
        value = min(1.0, estimate['point_estimate'] + z * estimate['cluster_robust_standard_error'])
    estimate['acceptance_value'] = value
    estimate['acceptance_interval_method'] = ('none_point_estimate_threshold' if threshold['metric'] == 'point_estimate'
        else 'one_sided_normal_cluster_robust_linearization')
    return value <= threshold['maximum'], None


def evaluate_human_sample(*, design, plan, responses):
    """Execute the frozen design, preserving its complete replayable inputs."""
    criteria, assignments = _plan(plan, design)
    received = _responses(responses, plan)
    estimates, checks = [], []
    for estimand in criteria['estimands']:
        estimate = _estimate(estimand, assignments, received, criteria)
        estimates.append(estimate)
        passed, reason = _acceptance(estimate, criteria['severity_thresholds'][estimand['estimand_id']], criteria)
        checks.append(dict(estimand_id=estimand['estimand_id'], met=passed, reason_code=reason))
    if any(item['missing_n'] for item in estimates):
        status = 'SAMPLING_VALIDATION_PENDING'
    else:
        status = 'SAMPLING_QUALIFIED' if all(item['met'] for item in checks) else 'SAMPLING_FAILED'
    trigger_actions = [dict(trigger_id=item['trigger_id'], action=item['action'],
        original_plan_sha256=plan['frozen_plan_sha256'], new_plan_required=True,
        original_assignments_must_remain_immutable=True)
        for item in criteria['expansion_and_full_review_triggers'] if item['on'] == status]
    result = dict(format=ASSESSMENT_FORMAT, design_sha256=design['design_sha256'],
        pipeline_binding_sha256=design['pipeline_binding_sha256'],
        population_manifest_sha256=plan['population_manifest_sha256'], plan_sha256=design['plan_sha256'],
        frozen_plan_sha256=plan['frozen_plan_sha256'], assignment_manifest_sha256=plan['assignment_manifest_sha256'],
        episode_material_set_sha256=(next(iter({unit['episode_material_set_sha256'] for unit in plan['population_manifest']}))
            if len({unit['episode_material_set_sha256'] for unit in plan['population_manifest']}) == 1
            else canonical_sha256(sorted({unit['episode_material_set_sha256'] for unit in plan['population_manifest']}))),
        delivery_input_sha256=(next(iter({unit['delivery_input_sha256'] for unit in plan['population_manifest']}))
            if len({unit['delivery_input_sha256'] for unit in plan['population_manifest']}) == 1
            else canonical_sha256(sorted({unit['delivery_input_sha256'] for unit in plan['population_manifest']}))),
        population_material_set_sha256=canonical_sha256(sorted({unit['episode_material_set_sha256'] for unit in plan['population_manifest']})),
        population_delivery_input_sha256=canonical_sha256(sorted({unit['delivery_input_sha256'] for unit in plan['population_manifest']})),
        population_scope='SINGLE_EPISODE' if len({unit['episode_id'] for unit in plan['population_manifest']}) == 1 else 'MULTI_EPISODE',
        statistical_acceptance_criteria=deepcopy(criteria), status=status,
        human_sample_status='HUMAN_SAMPLE_ACCEPT' if status == 'SAMPLING_QUALIFIED' else (
            'HUMAN_SAMPLE_UNRESOLVED' if status == 'SAMPLING_VALIDATION_PENDING' else 'HUMAN_SAMPLE_REVISE'),
        human_responses=deepcopy(responses), human_result_sha256=canonical_sha256(received),
        estimates=estimates, acceptance_checks=checks, trigger_actions=trigger_actions,
        population_estimation_executed=True, actual_reviewer_identity_authenticated=False,
        clinical_release_permitted=False)
    result['assessment_sha256'] = canonical_sha256(result)
    return result


def validate_human_sample_assessment(assessment, *, design, plan):
    if not isinstance(assessment, dict) or assessment.get('format') != ASSESSMENT_FORMAT:
        raise ValueError('READY_REPLAYABLE_ASSESSMENT_REQUIRED')
    expected = evaluate_human_sample(design=design, plan=plan, responses=assessment.get('human_responses'))
    if expected != assessment:
        raise ValueError('READY_ASSESSMENT_REPLAY_MISMATCH')
    return assessment
