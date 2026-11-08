"""Append-only orchestration of separate graph reconstruction and fresh audits."""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_material_governance.run import _json_exclusive
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import private_mkdir
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
    run_graph_reconstruction,
)
from .orchestration import run_episode_pipeline
from .persistence import PipelineBudget


def _reconstruction_hold_receipt(reconstruction, *, automatic_retry_performed=False,
                                 persistence_failure=None, failure_scope='RECONSTRUCTION'):
    """Preserve the exact chain and classify unresolved provider/local writes."""
    provider_unresolved = reconstruction.get('failure', {}).get('stage') == 'PROVIDER_REQUEST'
    states = {
        ('RECONSTRUCTION', True, False): 'IN_FLIGHT_ACCOUNTING_UNRESOLVED',
        ('RECONSTRUCTION', False, False): 'RECONSTRUCTION_NOT_ACCEPTED',
        ('RECONSTRUCTION', False, True): 'RECONSTRUCTION_PERSISTENCE_UNRESOLVED',
        ('FRESH_PIPELINE', False, True): 'FRESH_PIPELINE_PERSISTENCE_UNRESOLVED',
    }
    if failure_scope not in ('RECONSTRUCTION', 'FRESH_PIPELINE') \
            or failure_scope == 'FRESH_PIPELINE' and (
                provider_unresolved or not isinstance(persistence_failure, str)):
        raise ValueError('GRAPH_REVISION_HOLD_FAILURE_SCOPE_INVALID')
    result = dict(format='checkpoint-reconstruction-accounting-hold-0.1.0',
        state=states[(failure_scope, provider_unresolved, persistence_failure is not None)],
        chain_sha256=reconstruction['chain_sha256'],
        automatic_retry_performed=automatic_retry_performed,
        persistence_failure=persistence_failure,
        failure_scope=failure_scope,
        reconstruction_chain=deepcopy(reconstruction),
        clinical_release_permitted=False)
    from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
    result['hold_sha256'] = canonical_sha256(result)
    return result


def _persist_revision_summary(output, result):
    """Retain completed generation identities and the unresolved terminal hold."""
    from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
    receipt = dict(format='bounded-graph-revision-summary-0.2.0',
        state=result['state'], stop_reason=result['stop_reason'],
        generations=[item['generation'] for item in result['generations']],
        reconstruction_chain_ids=[dict(generation=item['generation'],
            chain_sha256=item['reconstruction']['chain_sha256']) for item in result['generations']],
        reconstruction_accounting_hold=deepcopy(result.get('reconstruction_accounting_hold')),
        clinical_release_permitted=False)
    receipt['summary_sha256'] = canonical_sha256(receipt)
    _json_exclusive(output / 'automatic-revision-summary.json', receipt)


def _validate_revision_summary(summary):
    """Validate summary and hold identity without authenticating provider I/O."""
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
        validate_graph_reconstruction_chain,
    )
    from .persistence import self_hash
    states = {'HOLD', 'RECONSTRUCTION_HOLD', 'HUMAN_REVIEW_REQUIRED',
        'READY_FOR_HUMAN_FINAL_REVIEW', 'RESEARCH_FEEDBACK_DRAFT_READY',
        'DELIVERY_GATE_HOLD', 'AUTO_GRAPH_AUDIT_HOLD', 'CHECKPOINT_MATERIAL_HOLD'}
    if not isinstance(summary, dict) or summary.get('state') not in states \
            or not isinstance(summary.get('stop_reason'), str) \
            or not isinstance(summary.get('generations'), list) \
            or any(type(value) is not int or value < 1 for value in summary['generations']) \
            or summary['generations'] != list(range(1, len(summary['generations']) + 1)) \
            or summary.get('clinical_release_permitted') is not False:
        raise ValueError('REAL_BATCH_REVISION_SUMMARY_INVALID')
    current = summary.get('format') == 'bounded-graph-revision-summary-0.2.0'
    if current:
        if not self_hash(summary, 'summary_sha256'):
            raise ValueError('REAL_BATCH_REVISION_SUMMARY_HASH_INVALID')
        ids = summary.get('reconstruction_chain_ids')
        if not isinstance(ids, list) or len(ids) != len(summary['generations']):
            raise ValueError('REAL_BATCH_REVISION_SUMMARY_INVALID')
        for generation, item in zip(summary['generations'], ids):
            if not isinstance(item, dict) or item.get('generation') != generation \
                    or not isinstance(item.get('chain_sha256'), str) \
                    or len(item['chain_sha256']) != 64:
                raise ValueError('REAL_BATCH_REVISION_SUMMARY_INVALID')
    elif summary.get('format') is not None or set(summary) - {
            'state', 'stop_reason', 'generations', 'clinical_release_permitted'}:
        raise ValueError('REAL_BATCH_LEGACY_REVISION_SUMMARY_INVALID')
    if summary['generations'] and summary['state'] == 'RECONSTRUCTION_HOLD':
        raise ValueError('REAL_BATCH_REVISION_SUMMARY_INVALID')
    reasons = {
        'RECONSTRUCTION_PROVIDER_USAGE_UNRESOLVED': (
            'IN_FLIGHT_ACCOUNTING_UNRESOLVED', 'RECONSTRUCTION', False),
        'RECONSTRUCTION_PERSISTENCE_FAILED': (
            'RECONSTRUCTION_PERSISTENCE_UNRESOLVED', 'RECONSTRUCTION', True),
        'FRESH_RECONSTRUCTED_PIPELINE_PERSISTENCE_FAILED': (
            'FRESH_PIPELINE_PERSISTENCE_UNRESOLVED', 'FRESH_PIPELINE', True),
        'AUTOMATIC_RECONSTRUCTION_FAILED': (
            'RECONSTRUCTION_NOT_ACCEPTED', 'RECONSTRUCTION', None),
    }
    if current and summary['stop_reason'] in reasons:
        expected_state, scope, persistence_failed = reasons[summary['stop_reason']]
        hold = summary.get('reconstruction_accounting_hold')
        if not isinstance(hold, dict) \
                or hold.get('format') != 'checkpoint-reconstruction-accounting-hold-0.1.0' \
                or hold.get('state') != expected_state \
                or hold.get('failure_scope') != scope \
                or hold.get('automatic_retry_performed') is not False \
                or (persistence_failed is False and hold.get('persistence_failure') is not None) \
                or (persistence_failed is True and (not isinstance(hold.get('persistence_failure'), str)
                    or not hold['persistence_failure'])) \
                or hold.get('clinical_release_permitted') is not False \
                or not self_hash(hold, 'hold_sha256') \
                or (summary['stop_reason'] != 'AUTOMATIC_RECONSTRUCTION_FAILED'
                    and summary['state'] != 'HOLD'):
            raise ValueError('REVISION_ACCOUNTING_HOLD_INVALID')
        chain = hold.get('reconstruction_chain')
        validate_graph_reconstruction_chain(chain)
        if chain['chain_sha256'] != hold.get('chain_sha256') \
                or (persistence_failed is False and
                    chain.get('failure', {}).get('stage') != 'PROVIDER_REQUEST') \
                or (scope == 'FRESH_PIPELINE' and
                    chain.get('state') != 'RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT'):
            raise ValueError('REVISION_ACCOUNTING_HOLD_INVALID')
    elif current and summary.get('reconstruction_accounting_hold') is not None:
        raise ValueError('REVISION_ACCOUNTING_HOLD_INVALID')
    return summary


def _read_revision_summary(path):
    """Read a current self-hashed summary or a bounded legacy one."""
    from .persistence import read
    return _validate_revision_summary(read(path))


def run_bounded_graph_revision_pipeline(*, parent_run, output, max_graph_revisions=1, **kwargs):
    """Consume a graph REVISE handoff through a separate reconstruction interface.

    Each generation is a complete fresh pipeline run with its own graph/material/
    delivery audits. The immutable parent and every intermediate run are retained.
    A budget or unresolved handoff stops without human or clinical approval.
    """
    if type(max_graph_revisions) is not int or not 1 <= max_graph_revisions <= 5:
        raise ValueError('GRAPH_REVISION_LOOP_BUDGET_INVALID')
    current = deepcopy(parent_run)
    private_mkdir(output, parents=True)
    _json_exclusive(output / 'parent-run.json', current)
    parent_output = output
    children = []
    for generation in range(1, max_graph_revisions + 1):
        if (current.get('state') != 'REVISION_REQUIRED'
                or current.get('graph_audit_chain', {}).get('stop_reason') != 'RECONSTRUCTION_REQUIRED'):
            break
        adapters = kwargs.get('adapters', {})
        adapter = adapters.get('reconstruction')
        if not callable(adapter):
            raise ValueError('GRAPH_REVISION_SEPARATE_RECONSTRUCTION_ADAPTER_REQUIRED')
        if adapters.get('graph') is adapter:
            raise ValueError('GRAPH_REVISION_RECONSTRUCTION_ADAPTER_MUST_BE_SEPARATE')
        max_model_calls = kwargs.get('max_model_calls', 20)
        if type(max_model_calls) is not int or not 1 <= max_model_calls <= 1000:
            raise ValueError('GRAPH_REVISION_MODEL_BUDGET_INVALID')
        budget = PipelineBudget(max_model_calls, dict(reconstruction=adapter))
        reconstruct_ask = lambda task, payload, schema: budget.ask_for(
            'reconstruction', task, payload, schema)
        chain = current['graph_audit_chain']
        reconstruction = run_graph_reconstruction(chain['initial_request'],
            chain['rounds'][-1]['response'], reconstruct_ask)
        generation_output = output / f'generation-{generation:04d}'
        if reconstruction['state'] != 'RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT':
            unresolved = reconstruction.get('failure', {}).get('stage') == 'PROVIDER_REQUEST'
            persistence_failure = None
            try:
                _json_exclusive(output / f'reconstruction-{generation:04d}.json', reconstruction)
                private_mkdir(generation_output, parents=True)
                receipt = _reconstruction_hold_receipt(reconstruction)
                _json_exclusive(generation_output / 'reconstruction-accounting-hold.json', receipt)
            except Exception as exc:
                persistence_failure = type(exc).__name__
                # The adapter may already have returned; retain the in-memory
                # chain and never retry it to work around an archive failure.
                receipt = _reconstruction_hold_receipt(reconstruction,
                    persistence_failure=persistence_failure)
            if persistence_failure is not None or unresolved:
                result = dict(format='bounded-graph-revision-orchestration-0.1.0',
                    state='HOLD', stop_reason=(
                        'RECONSTRUCTION_PROVIDER_USAGE_UNRESOLVED'
                        if unresolved else 'RECONSTRUCTION_PERSISTENCE_FAILED'),
                    final_run=current, reconstruction=reconstruction,
                    reconstruction_accounting_hold=receipt,
                    generations=children, parent_output=parent_output,
                    automatic_retry_performed=False, clinical_release_permitted=False)
                _persist_revision_summary(output, result)
                return result
            result = dict(format='bounded-graph-revision-orchestration-0.1.0',
                state='HUMAN_REVIEW_REQUIRED' if generation >= max_graph_revisions else 'RECONSTRUCTION_HOLD',
                stop_reason='AUTOMATIC_RECONSTRUCTION_FAILED',
                final_run=current, reconstruction=reconstruction,
                reconstruction_accounting_hold=receipt, generations=children,
                parent_output=parent_output, clinical_release_permitted=False)
            _persist_revision_summary(output, result)
            return result
        try:
            _json_exclusive(output / f'reconstruction-{generation:04d}.json', reconstruction)
        except Exception as exc:
            hold = _reconstruction_hold_receipt(reconstruction,
                persistence_failure=type(exc).__name__)
            result = dict(format='bounded-graph-revision-orchestration-0.1.0',
                state='HOLD', stop_reason='RECONSTRUCTION_PERSISTENCE_FAILED',
                final_run=current, reconstruction=reconstruction,
                reconstruction_accounting_hold=hold, generations=children,
                parent_output=parent_output, automatic_retry_performed=False,
                clinical_release_permitted=False)
            _persist_revision_summary(output, result)
            return result
        kwargs['graph'] = reconstruction['graph']
        kwargs['graph_revision'] = dict(parent_run=current, origin='graph_audit',
            reconstruction_chain=reconstruction)
        kwargs['output'] = generation_output
        # One bounded budget includes both reconstruction and all fresh audits.
        kwargs['max_model_calls'] = max_model_calls
        kwargs['initial_model_usage'] = budget.snapshot()
        fresh_adapters = dict(kwargs['adapters'])
        fresh_adapters.pop('reconstruction', None)
        current_kwargs = dict(kwargs, adapters=fresh_adapters)
        try:
            current = run_episode_pipeline(**current_kwargs)
        except Exception as exc:
            # The settled reconstruction chain is archived. Preserve the last
            # replayable parent and do not retry either settled provider call.
            hold = _reconstruction_hold_receipt(reconstruction,
                persistence_failure=type(exc).__name__, failure_scope='FRESH_PIPELINE')
            result = dict(format='bounded-graph-revision-orchestration-0.1.0',
                state='HOLD', stop_reason='FRESH_RECONSTRUCTED_PIPELINE_PERSISTENCE_FAILED',
                final_run=current,
                reconstruction=reconstruction, reconstruction_accounting_hold=hold,
                generations=children, parent_output=parent_output,
                automatic_retry_performed=False, clinical_release_permitted=False)
            _persist_revision_summary(output, result)
            return result
        children.append(dict(generation=generation, output=generation_output,
            run_sha256=current['run_sha256'], reconstruction=reconstruction))
        if current['state'] in ('READY_FOR_HUMAN_SAMPLING', 'RESEARCH_FEEDBACK_DRAFT_READY'):
            research_draft = current['state'] == 'RESEARCH_FEEDBACK_DRAFT_READY'
            result = dict(format='bounded-graph-revision-orchestration-0.1.0',
                state='RESEARCH_FEEDBACK_DRAFT_READY' if research_draft else 'READY_FOR_HUMAN_FINAL_REVIEW',
                stop_reason='RESEARCH_FEEDBACK_DRAFT_CREATED' if research_draft else 'AUTOMATIC_AUDITS_COMPLETE',
                final_run=current, generations=children, parent_output=parent_output,
                human_final_review_required=True, clinical_reference_created=False,
                clinical_release_permitted=False)
            _persist_revision_summary(output, result)
            return result
    state = 'HUMAN_REVIEW_REQUIRED' if current.get('state') == 'REVISION_REQUIRED' else 'HOLD'
    stop_reason = 'GRAPH_REVISION_BUDGET_EXHAUSTED' if state == 'HUMAN_REVIEW_REQUIRED' else 'PIPELINE_NOT_AUTOMATICALLY_COMPLETE'
    if current.get('state') == 'HOLD':
        # Preserve a delivery gate or terminal material failure rather than
        # replacing its deterministic stage reason with the wrapper status.
        from scripts.cohort_construction.p2_checkpoint_real_validation.batch import _summary_state
        projected_state, projected_reason = _summary_state(current)
        if projected_state == 'DELIVERY_GATE_HOLD':
            state, stop_reason = projected_state, projected_reason
        elif stop_reason == 'PIPELINE_NOT_AUTOMATICALLY_COMPLETE':
            material_reason = next((item.get('reason_code') for item in reversed(
                current.get('stages', [])) if item.get('reason_code')), None)
            if material_reason is not None:
                stop_reason = material_reason
    result = dict(format='bounded-graph-revision-orchestration-0.1.0', state=state,
        stop_reason=stop_reason,
        final_run=current, generations=children, parent_output=parent_output,
        human_final_review_required=True, clinical_reference_created=False,
        clinical_release_permitted=False)
    _persist_revision_summary(output, result)
    return result
