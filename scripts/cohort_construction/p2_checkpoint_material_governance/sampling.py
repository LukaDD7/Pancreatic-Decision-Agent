"""Freeze packet sampling and report independent human descriptive results.

Version 0.2 freezes the actual population and ordered random assignment. Its
Wilson intervals describe reviewed packets only: unequal quotas, missing
responses and possible patient clustering do not define a population estimator.
No clinical threshold, population inference or automatic release is supplied.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import re
from statistics import NormalDist

from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError, canonical_sha256


PLAN_FORMAT = 'human-material-sampling-plan-0.2.0'
_PACKET_FIELDS = {'packet_id', 'packet_sha256', 'stratum_id'}
_PLAN_FIELDS = {'plan_id', 'protocol_version', 'population_version', 'seed', 'sample_size',
                'strata', 'critical_defect_limit', 'ordinary_defect_limit', 'confidence_level',
                'unresolved_policy', 'reviewer_independence_policy', 'expansion_policy'}
_DESIGN = dict(evaluation_unit='checkpoint_packet',
               estimation_target='quota_sample_descriptive', population_inference='not_authorized',
               population_design='not_defined', weighting='not_defined',
               patient_or_checkpoint_clustering='not_defined',
               population_inference_permitted=False)


def _allocations(plan):
    if type(plan.get('sample_size')) is not int or plan['sample_size'] < 1:
        raise ContractError('SAMPLING_SAMPLE_SIZE_REQUIRED')
    if type(plan.get('seed')) is not int:
        raise ContractError('SAMPLING_INTEGER_SEED_REQUIRED')
    for key in ('critical_defect_limit', 'ordinary_defect_limit'):
        if type(plan.get(key)) is not int or plan[key] < 0:
            raise ContractError('SAMPLING_PREDEFINED_COUNT_LIMIT_REQUIRED')
    if plan.get('confidence_level') not in (0.9, 0.95, 0.99):
        raise ContractError('SAMPLING_CONFIDENCE_LEVEL_REQUIRED')
    for key in ('reviewer_independence_policy', 'expansion_policy', 'population_version', 'protocol_version', 'plan_id'):
        if not isinstance(plan.get(key), str) or not plan[key].strip():
            raise ContractError('SAMPLING_POLICY_TEXT_REQUIRED')
    if plan.get('unresolved_policy') not in ('not_accept', 'report_separately'):
        raise ContractError('SAMPLING_UNRESOLVED_POLICY_REQUIRED')
    if not isinstance(plan.get('strata'), list) or not plan['strata']:
        raise ContractError('SAMPLING_STRATA_REQUIRED')
    allocated = {}
    for stratum in plan['strata']:
        if (not isinstance(stratum, dict) or set(stratum) != {'stratum_id', 'n'}
                or not isinstance(stratum.get('stratum_id'), str) or not stratum['stratum_id'].strip()
                or type(stratum.get('n')) is not int or stratum['n'] < 0):
            raise ContractError('SAMPLING_STRATUM_ALLOCATION_REQUIRED')
        if stratum['stratum_id'] in allocated:
            raise ContractError('SAMPLING_STRATA_MUST_BE_DISJOINT')
        allocated[stratum['stratum_id']] = stratum['n']
    if sum(allocated.values()) != plan['sample_size']:
        raise ContractError('SAMPLING_ALLOCATION_TOTAL_MISMATCH')
    return allocated


def _population(packets, allocated):
    if not isinstance(packets, list):
        raise ContractError('SAMPLING_FROZEN_POPULATION_REQUIRED')
    result, seen = [], set()
    for packet in packets:
        if (not isinstance(packet, dict) or set(packet) != _PACKET_FIELDS
                or not isinstance(packet['packet_id'], str) or not packet['packet_id'].strip()
                or not isinstance(packet['packet_sha256'], str)
                or re.fullmatch(r'[0-9a-f]{64}', packet['packet_sha256']) is None
                or not isinstance(packet['stratum_id'], str) or packet['stratum_id'] not in allocated):
            raise ContractError('SAMPLING_POPULATION_PACKET_BINDING_INVALID')
        if packet['packet_id'] in seen:
            raise ContractError('SAMPLING_PACKET_MUST_HAVE_ONE_STRATUM')
        seen.add(packet['packet_id'])
        result.append(deepcopy(packet))
    counts = Counter(p['stratum_id'] for p in result)
    if any(counts[sid] < n for sid, n in allocated.items()):
        raise ContractError('SAMPLING_STRATUM_POPULATION_INSUFFICIENT')
    return sorted(result, key=lambda p: p['packet_id'])


def _assignment(plan, population):
    selected = []
    for stratum in plan['strata']:
        eligible = [p for p in population if p['stratum_id'] == stratum['stratum_id']]
        ordered = sorted(eligible, key=lambda p: (hashlib.sha256(
            f"{plan['seed']}:{p['packet_id']}".encode()).hexdigest(), p['packet_id']))
        selected.extend(deepcopy(ordered[:stratum['n']]))
    return selected


def freeze_sampling_plan(plan, packets=None):
    """Freeze protocol, full population and seeded assignments before review.

    Old 0.1 artifacts are retained as historical output, but cannot authorize a
    new summary: the population omitted by that contract must be frozen anew.
    """
    if not isinstance(plan, dict) or set(plan) != _PLAN_FIELDS:
        raise ContractError('SAMPLING_PLAN_FIELDS_MISMATCH')
    allocated = _allocations(plan)
    population = _population(packets, allocated)
    result = deepcopy(plan)
    result.update(format=PLAN_FORMAT, statistical_design=deepcopy(_DESIGN),
                  population_manifest=population, population_manifest_sha256=canonical_sha256(population))
    assignments = _assignment(result, population)
    result.update(assignment_manifest=assignments, assignment_manifest_sha256=canonical_sha256(assignments))
    result['frozen_plan_sha256'] = canonical_sha256(result)
    return result


def _validated_plan(plan):
    if (not isinstance(plan, dict) or plan.get('format') != PLAN_FORMAT
            or plan.get('frozen_plan_sha256') != canonical_sha256(
                {k: v for k, v in plan.items() if k != 'frozen_plan_sha256'})):
        raise ContractError('SAMPLING_PLAN_NOT_FROZEN_OR_CHANGED')
    expected_fields = _PLAN_FIELDS | {'format', 'statistical_design', 'population_manifest',
        'population_manifest_sha256', 'assignment_manifest', 'assignment_manifest_sha256', 'frozen_plan_sha256'}
    if set(plan) != expected_fields or plan['statistical_design'] != _DESIGN:
        raise ContractError('SAMPLING_FROZEN_DESIGN_MISMATCH')
    allocated = _allocations(plan)
    population = _population(plan['population_manifest'], allocated)
    assignments = _assignment(plan, population)
    if (population != plan['population_manifest']
            or canonical_sha256(population) != plan['population_manifest_sha256']
            or assignments != plan['assignment_manifest']
            or canonical_sha256(assignments) != plan['assignment_manifest_sha256']):
        raise ContractError('SAMPLING_FROZEN_MANIFEST_MISMATCH')
    return allocated, population, assignments


def select_packets(plan, packets):
    """Verify the whole supplied population and return its frozen assignment."""
    allocated, frozen_population, assignments = _validated_plan(plan)
    if _population(packets, allocated) != frozen_population:
        raise ContractError('SAMPLING_POPULATION_CHANGED')
    return deepcopy(assignments)


def _wilson(successes, n, confidence):
    if not n:
        return None
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    p, denominator = successes / n, 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denominator
    return [max(0, centre - margin), min(1, centre + margin)]


def _descriptive_counts(responses, assigned, population_n, confidence):
    n = len(responses)
    critical = sum(r['critical_defects'] for r in responses)
    ordinary = sum(r['ordinary_defects'] for r in responses)
    critical_packets = sum(r['critical_defects'] > 0 for r in responses)
    ordinary_packets = sum(r['ordinary_defects'] > 0 for r in responses)
    accepted = sum(r['status'] == 'HUMAN_SAMPLE_ACCEPT' for r in responses)
    return dict(population_N=population_n, assigned_n=assigned, received=n,
                not_received=assigned - n, accepted=accepted,
                revised=sum(r['status'] == 'HUMAN_SAMPLE_REVISE' for r in responses),
                unresolved=sum(r['status'] == 'HUMAN_SAMPLE_UNRESOLVED' for r in responses),
                critical_defects=critical, ordinary_defects=ordinary,
                defect_denominator_unit='reviewed_packet', defect_denominator=n,
                critical_defect_packets=critical_packets, ordinary_defect_packets=ordinary_packets,
                accept_rate_wilson=_wilson(accepted, n, confidence),
                critical_defect_packet_rate_wilson=_wilson(critical_packets, n, confidence),
                ordinary_defect_packet_rate_wilson=_wilson(ordinary_packets, n, confidence),
                interval_scope='descriptive_reviewed_packets_only_not_population_estimator',
                interval_assumption='unadjusted_binomial_diagnostic_independence_not_established')


def summarize_human_sample(plan, assignments, responses):
    """Verify frozen assignments; report quota sample without population claims."""
    allocated, population, frozen_assignments = _validated_plan(plan)
    if not isinstance(assignments, list) or assignments != frozen_assignments:
        raise ContractError('HUMAN_SAMPLE_ASSIGNMENT_BINDING_MISMATCH')
    # Equality is deliberately ordered: substitution, relabelling and reordering
    # are changes to the frozen allocation, even when overall n is unchanged.
    expected = {a['packet_id']: a for a in frozen_assignments}
    if not isinstance(responses, list):
        raise ContractError('HUMAN_SAMPLE_RESPONSE_LIST_REQUIRED')
    received = {}
    for response in responses:
        if not isinstance(response, dict):
            raise ContractError('HUMAN_SAMPLE_ASSIGNMENT_BINDING_MISMATCH')
        pid = response.get('packet_id')
        if not isinstance(pid, str) or pid not in expected or pid in received:
            raise ContractError('HUMAN_SAMPLE_ASSIGNMENT_BINDING_MISMATCH')
        if response.get('packet_sha256') != expected[pid]['packet_sha256']:
            raise ContractError('HUMAN_SAMPLE_PACKET_HASH_MISMATCH')
        if 'stratum_id' in response and response['stratum_id'] != expected[pid]['stratum_id']:
            raise ContractError('HUMAN_SAMPLE_STRATUM_BINDING_MISMATCH')
        if (response.get('frozen_plan_sha256') != plan['frozen_plan_sha256']
                or response.get('assignment_manifest_sha256') != plan['assignment_manifest_sha256']):
            raise ContractError('HUMAN_SAMPLE_TASK_BINDING_MISMATCH')
        if not isinstance(response.get('reviewer_id'), str) or not response['reviewer_id'].strip():
            raise ContractError('HUMAN_SAMPLE_REVIEWER_ID_REQUIRED')
        try:
            timestamp = datetime.fromisoformat(response['reviewed_at'])
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError('timezone required')
        except (KeyError, TypeError, ValueError):
            raise ContractError('HUMAN_SAMPLE_REVIEW_TIME_REQUIRED') from None
        if response.get('reviewer_type') != 'human' or response.get('independence_attested') is not True:
            raise ContractError('HUMAN_SAMPLE_ACTUAL_INDEPENDENT_REVIEW_REQUIRED')
        if response.get('status') not in ('HUMAN_SAMPLE_ACCEPT', 'HUMAN_SAMPLE_REVISE', 'HUMAN_SAMPLE_UNRESOLVED'):
            raise ContractError('HUMAN_SAMPLE_STATUS_INVALID')
        for key in ('critical_defects', 'ordinary_defects'):
            if type(response.get(key)) is not int or response[key] < 0:
                raise ContractError('HUMAN_SAMPLE_DEFECT_COUNT_REQUIRED')
        received[pid] = deepcopy(response)
    counts = _descriptive_counts(list(received.values()), len(assignments), len(population), plan['confidence_level'])
    strata = []
    population_counts = Counter(p['stratum_id'] for p in population)
    for sid, quota in allocated.items():
        observed = [received[a['packet_id']] for a in assignments
                    if a['stratum_id'] == sid and a['packet_id'] in received]
        strata.append(dict(stratum_id=sid, **_descriptive_counts(
            observed, quota, population_counts[sid], plan['confidence_level'])))
    complete = all(s['received'] == s['assigned_n'] for s in strata)
    meets = (complete and counts['critical_defects'] <= plan['critical_defect_limit']
             and counts['ordinary_defects'] <= plan['ordinary_defect_limit']
             and (plan['unresolved_policy'] == 'report_separately' or counts['unresolved'] == 0))
    return dict(format='human-material-sample-summary-0.2.0', plan_id=plan['plan_id'],
                frozen_plan_sha256=plan['frozen_plan_sha256'],
                population_manifest_sha256=plan['population_manifest_sha256'],
                assignment_manifest_sha256=plan['assignment_manifest_sha256'],
                human_responses_sha256=canonical_sha256(responses),
                identity_independence_verification='declarations_checked_identity_not_authenticated',
                assigned=len(assignments), **counts, strata=strata,
                human_review_state='COMPLETE' if complete else 'INCOMPLETE',
                automated_review_state='NOT_EVALUATED_BY_HUMAN_SAMPLE_SUMMARY',
                statistical_design=deepcopy(_DESIGN),
                meets_frozen_count_thresholds=meets,
                descriptive_sample_summary_complete=complete,
                statistical_validation_complete=False,
                population_inference_permitted=False, automatic_release_permitted=False,
                clinical_release_permitted=False,
                note='Intervals describe observed quota-sample packets only. Population design, weights and clustering are undefined; full response or frozen count limits do not establish population validation or clinical release.')
