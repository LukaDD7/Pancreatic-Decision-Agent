"""Append-only orchestration of separate graph reconstruction and fresh audits."""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_material_governance.run import _json_exclusive
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import private_mkdir
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
    run_graph_reconstruction,
)
from .orchestration import run_episode_pipeline
from .persistence import PipelineBudget


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
        _json_exclusive(output / f'reconstruction-{generation:04d}.json', reconstruction)
        generation_output = output / f'generation-{generation:04d}'
        if reconstruction['state'] != 'RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT':
            return dict(format='bounded-graph-revision-orchestration-0.1.0',
                state='HUMAN_REVIEW_REQUIRED' if generation >= max_graph_revisions else 'RECONSTRUCTION_HOLD',
                stop_reason='AUTOMATIC_RECONSTRUCTION_FAILED',
                final_run=current, reconstruction=reconstruction, generations=children,
                parent_output=parent_output, clinical_release_permitted=False)
        kwargs['graph'] = reconstruction['graph']
        kwargs['graph_revision'] = dict(parent_run=current, origin='graph_audit',
            reconstruction_chain=reconstruction)
        kwargs['output'] = generation_output
        # One bounded budget includes both reconstruction and all fresh audits.
        kwargs['max_model_calls'] = max_model_calls
        kwargs['initial_model_usage'] = budget.snapshot()
        fresh_adapters = dict(kwargs['adapters'])
        fresh_adapters.pop('reconstruction', None)
        kwargs['adapters'] = fresh_adapters
        current = run_episode_pipeline(**kwargs)
        children.append(dict(generation=generation, output=generation_output,
            run_sha256=current['run_sha256'], reconstruction=reconstruction))
        if current['state'] == 'READY_FOR_HUMAN_SAMPLING':
            return dict(format='bounded-graph-revision-orchestration-0.1.0',
                state='READY_FOR_HUMAN_FINAL_REVIEW', stop_reason='AUTOMATIC_AUDITS_COMPLETE',
                final_run=current, generations=children, parent_output=parent_output,
                human_final_review_required=True, clinical_reference_created=False,
                clinical_release_permitted=False)
    state = 'HUMAN_REVIEW_REQUIRED' if current.get('state') == 'REVISION_REQUIRED' else 'HOLD'
    return dict(format='bounded-graph-revision-orchestration-0.1.0', state=state,
        stop_reason='GRAPH_REVISION_BUDGET_EXHAUSTED' if state == 'HUMAN_REVIEW_REQUIRED' else 'PIPELINE_NOT_AUTOMATICALLY_COMPLETE',
        final_run=current, generations=children, parent_output=parent_output,
        human_final_review_required=True, clinical_reference_created=False,
        clinical_release_permitted=False)
