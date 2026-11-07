"""Versioned logical model-attempt accounting; no provider or clinical semantics.

An attempt enters ``PipelineBudget.ask_for``. Only an uncached adapter call is an
uncached invocation; cache hits and local budget denials remain separate. A
response that later fails validation is still an uncached adapter success.
"""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256


ACCOUNTING_VERSION = 'checkpoint-ready-pipeline-model-accounting-0.2.0'
OUTCOMES = ('uncached_succeeded', 'uncached_failed', 'cache_hits', 'denied')
STAGE_OUTCOMES = ('ask_for_attempts', *OUTCOMES)
ALLOWED_STAGES = {'graph', 'material', 'delivery', 'reconstruction'}


def zero_usage(limit):
    return dict(uncached_succeeded=0, uncached_failed=0, cache_hits=0, denied=0,
        actual_uncached_model_invocations=0, max_model_invocations=limit,
        stage_call_counts={}, stage_usage={}, accounting_version=ACCOUNTING_VERSION,
        accounting_scope='all_declared_pipeline_stages_attempts_and_adapter_outcomes')


def _nonnegative_ints(record, keys):
    return isinstance(record, dict) and all(type(record.get(key)) is int and record[key] >= 0
                                           for key in keys)


def validate_usage(usage, limit, *, allowed_stages=ALLOWED_STAGES):
    """Validate the whole snapshot rather than only its uncached total."""
    if not _nonnegative_ints(usage, OUTCOMES) \
            or usage.get('actual_uncached_model_invocations') != usage['uncached_succeeded'] + usage['uncached_failed'] \
            or usage.get('max_model_invocations') != limit \
            or usage['actual_uncached_model_invocations'] > limit \
            or usage.get('accounting_version') != ACCOUNTING_VERSION \
            or usage.get('accounting_scope') != 'all_declared_pipeline_stages_attempts_and_adapter_outcomes' \
            or not isinstance(usage.get('stage_call_counts'), dict) \
            or not isinstance(usage.get('stage_usage'), dict) \
            or set(usage['stage_call_counts']) != set(usage['stage_usage']):
        raise ValueError('READY_PIPELINE_MODEL_USAGE_INVALID')
    totals = {key: 0 for key in STAGE_OUTCOMES}
    for stage, counts in usage['stage_usage'].items():
        if stage not in allowed_stages or not _nonnegative_ints(counts, STAGE_OUTCOMES) \
                or set(counts) != set(STAGE_OUTCOMES) \
                or usage['stage_call_counts'].get(stage) != counts['ask_for_attempts'] \
                or counts['ask_for_attempts'] != sum(counts[key] for key in OUTCOMES):
            raise ValueError('READY_PIPELINE_MODEL_STAGE_USAGE_INVALID')
        for key in STAGE_OUTCOMES:
            totals[key] += counts[key]
    if any(usage[key] != totals[key] for key in OUTCOMES):
        raise ValueError('READY_PIPELINE_MODEL_AGGREGATE_USAGE_MISMATCH')
    return deepcopy(usage)


def usage_delta(base, after, limit):
    """Return after-base, preserving exact per-stage outcome distinctions."""
    validate_usage(base, limit)
    validate_usage(after, limit)
    result = zero_usage(limit)
    for key in OUTCOMES:
        result[key] = after[key] - base[key]
    result['actual_uncached_model_invocations'] = result['uncached_succeeded'] + result['uncached_failed']
    for stage in sorted(set(base['stage_usage']) | set(after['stage_usage'])):
        old = base['stage_usage'].get(stage, {})
        new = after['stage_usage'].get(stage, {})
        counts = {key: new.get(key, 0) - old.get(key, 0) for key in STAGE_OUTCOMES}
        if any(counts.values()):
            result['stage_usage'][stage] = counts
            result['stage_call_counts'][stage] = counts['ask_for_attempts']
    return validate_usage(result, limit)


def usage_sum(base, delta, limit):
    """Reconcile cumulative usage from independently validated base and delta."""
    validate_usage(base, limit)
    validate_usage(delta, limit)
    result = zero_usage(limit)
    for key in OUTCOMES:
        result[key] = base[key] + delta[key]
    result['actual_uncached_model_invocations'] = result['uncached_succeeded'] + result['uncached_failed']
    for stage in sorted(set(base['stage_usage']) | set(delta['stage_usage'])):
        old, new = base['stage_usage'].get(stage, {}), delta['stage_usage'].get(stage, {})
        counts = {key: old.get(key, 0) + new.get(key, 0) for key in STAGE_OUTCOMES}
        if counts['ask_for_attempts']:
            result['stage_usage'][stage] = counts
            result['stage_call_counts'][stage] = counts['ask_for_attempts']
    return validate_usage(result, limit)


def add_chain_attempt(usage, stage, record, *, failure=False):
    """Classify a replayed chain attempt from its receipt and failure stage."""
    if stage not in ALLOWED_STAGES or not isinstance(record, dict) \
            or not isinstance(record.get('receipt'), dict):
        raise ValueError('READY_PIPELINE_CHAIN_ATTEMPT_RECEIPT_INVALID')
    if failure and record.get('stage') not in ('PROVIDER_REQUEST', 'RESPONSE_VALIDATION', 'LOCAL_BUDGET_CONTROL'):
        raise ValueError('READY_PIPELINE_CHAIN_ATTEMPT_STAGE_INVALID')
    receipt = record['receipt']
    local_denial = receipt.get('status') in (
        'LOCAL_BUDGET_DENIED', 'LOCAL_BATCH_BUDGET_DENIED')
    cached = receipt.get('cached') is True
    synthetic_adapter = receipt.get('receipt_origin') == 'SYNTHETIC_ADAPTER' \
        and receipt.get('provider_transport_verified') is False
    if local_denial and (cached or receipt.get('provider_invocation_occurred') is not False \
                         or not failure or record['stage'] not in ('PROVIDER_REQUEST', 'LOCAL_BUDGET_CONTROL')):
        raise ValueError('READY_PIPELINE_CHAIN_ATTEMPT_OUTCOME_CONFLICT')
    if receipt.get('cached') is True and receipt.get('provider_invocation_occurred') is True \
            or receipt.get('provider_invocation_occurred') is False and not (
                local_denial or cached or synthetic_adapter) \
            or receipt.get('cached') is False and receipt.get('provider_invocation_occurred') is False \
            and not (local_denial or synthetic_adapter):
        raise ValueError('READY_PIPELINE_CHAIN_ATTEMPT_OUTCOME_CONFLICT')
    outcome = 'denied' if local_denial else 'cache_hits' if cached else (
        'uncached_failed' if failure and record['stage'] == 'PROVIDER_REQUEST' else 'uncached_succeeded')
    counts = usage['stage_usage'].setdefault(stage, {key: 0 for key in STAGE_OUTCOMES})
    counts[outcome] += 1
    counts['ask_for_attempts'] += 1
    usage['stage_call_counts'][stage] = counts['ask_for_attempts']
    usage[outcome] += 1
    usage['actual_uncached_model_invocations'] = usage['uncached_succeeded'] + usage['uncached_failed']


def chain_usage(chain, stage, usage):
    """Rebuild attempts from terminal chain records, not from round counts."""
    if not isinstance(chain, dict) or not isinstance(chain.get('rounds'), list):
        raise ValueError('READY_PIPELINE_CHAIN_ATTEMPTS_INVALID')
    for record in chain['rounds']:
        add_chain_attempt(usage, stage, record)
    if 'failure' in chain:
        add_chain_attempt(usage, stage, chain['failure'], failure=True)
    return usage


def validate_graph_reconstruction_usage(chain, usage, limit, *, provider_receipt=None):
    """Reconcile the separate reconstruction chain's actual adapter outcome.

    A receipt archived separately from the chain takes precedence over the
    chain's editable snapshot. Neither proves that the remote provider sent it.
    """
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
        validate_graph_reconstruction_chain,
    )
    validate_graph_reconstruction_chain(chain)
    if chain.get('failure', {}).get('stage') == 'PROVIDER_REQUEST':
        raise ValueError('READY_PIPELINE_RECONSTRUCTION_IN_FLIGHT_USAGE_UNRESOLVED')
    if provider_receipt is not None and chain.get('provider_receipt') != provider_receipt:
        raise ValueError('READY_PIPELINE_RECONSTRUCTION_PROVIDER_RECEIPT_MISMATCH')
    add_chain_attempt(usage, 'reconstruction', dict(
        receipt=deepcopy(chain['provider_receipt'] if provider_receipt is None else provider_receipt)),
        failure=chain.get('failure', {}).get('stage') == 'RESPONSE_VALIDATION')
    return usage


def budget_receipt(*, stage, target_id, before, after, chain, execution_origin, parent_run_sha256=None):
    """Bind before/after snapshots to a validated chain and its execution origin."""
    delta = usage_delta(before, after, after['max_model_invocations'])
    result = dict(format='checkpoint-ready-pipeline-model-budget-receipt-0.2.0',
        accounting_version=ACCOUNTING_VERSION, stage=stage, target_id=target_id,
        execution_origin=execution_origin, parent_run_sha256=parent_run_sha256,
        chain_sha256=canonical_sha256(chain), before=deepcopy(before), after=deepcopy(after), delta=delta,
        provider_token_usage_is_not_invocation_accounting=True)
    result['receipt_sha256'] = canonical_sha256(result)
    return result


def validate_budget_receipt(receipt, *, stage, target_id, chain, execution_origin,
                            parent_run_sha256, limit):
    if not isinstance(receipt, dict) \
            or receipt.get('format') != 'checkpoint-ready-pipeline-model-budget-receipt-0.2.0' \
            or receipt.get('receipt_sha256') != canonical_sha256({key: value for key, value in receipt.items()
                                                               if key != 'receipt_sha256'}):
        raise ValueError('READY_PIPELINE_MODEL_BUDGET_RECEIPT_INVALID')
    expected = budget_receipt(stage=stage, target_id=target_id, before=receipt.get('before'),
        after=receipt.get('after'), chain=chain, execution_origin=execution_origin,
        parent_run_sha256=parent_run_sha256)
    if receipt != expected:
        raise ValueError('READY_PIPELINE_MODEL_BUDGET_RECEIPT_MISMATCH')
    expected_usage = zero_usage(limit)
    if execution_origin == 'EXECUTED':
        chain_usage(chain, stage, expected_usage)
    elif execution_origin == 'INHERITED':
        if receipt['after'] != receipt['before']:
            raise ValueError('READY_PIPELINE_INHERITED_MODEL_BUDGET_CHANGED')
    else:
        raise ValueError('READY_PIPELINE_MODEL_BUDGET_RECEIPT_ORIGIN_INVALID')
    expected_usage = validate_usage(expected_usage, limit)
    if receipt['delta'] != expected_usage:
        raise ValueError('READY_PIPELINE_MODEL_BUDGET_RECEIPT_CHAIN_USAGE_MISMATCH')
    return receipt
