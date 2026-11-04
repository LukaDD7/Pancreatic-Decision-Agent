"""CE-GRA-012: replayable provenance for a fresh post-reconstruction graph audit.

The independent auditor receives the prior audit, the separate reconstruction
proposal, and deterministic execution evidence. These are research history, not
clinical facts or an inherited approval.
"""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256


def _object_diff(before, after, collection):
    """Diff frozen discovery-unit events, pair-keyed relations and Episodes."""
    def records(graph):
        if collection == 'events':
            return [event for unit in graph['discovery_units'] for event in unit.get('events', [])]
        return graph[collection]

    def identity(item):
        if collection == 'events':
            return item['event_id']
        if collection == 'episodes':
            return item['episode_id']
        # The reconstruction contract identifies a relation by its ordered pair;
        # it does not require or preserve a synthetic relation_id field.
        return item['source_event_id'] + '->' + item['target_event_id']

    def index(graph):
        items = records(graph)
        result = {identity(item): item for item in items}
        if len(result) != len(items):
            raise ValueError('GRAPH_REVISION_LINEAGE_DUPLICATE_OBJECT_ID')
        return result

    old, new = index(before), index(after)
    result = []
    for identity in sorted(set(old) | set(new)):
        if old.get(identity) != new.get(identity):
            result.append(dict(object_id=identity, before=deepcopy(old.get(identity)),
                               after=deepcopy(new.get(identity))))
    return result


def _metadata_diff(before, after):
    excluded = {'discovery_units', 'relations', 'episodes', 'graph_sha256'}
    return [dict(field=key, before=deepcopy(before.get(key)), after=deepcopy(after.get(key)))
            for key in sorted((set(before) | set(after)) - excluded)
            if before.get(key) != after.get(key)]


def build_graph_revision_lineage(parent_audit_chain, reconstruction_chain, current_graph):
    """Derive history solely from durable, independently replayable chains."""
    from .chain import validate_graph_audit_chain
    from .reconstruction import validate_graph_reconstruction_chain
    parent_replay = validate_graph_audit_chain(parent_audit_chain)
    if parent_replay['stop_reason'] != 'RECONSTRUCTION_REQUIRED' \
            or parent_audit_chain['state'] != 'AUTO_GRAPH_AUDIT_REVISE' \
            or len(parent_audit_chain['rounds']) != 1:
        raise ValueError('GRAPH_REVISION_LINEAGE_PARENT_REVISE_REQUIRED')
    parent_request = parent_audit_chain['initial_request']
    parent_response = parent_audit_chain['rounds'][-1]['response']
    replay = validate_graph_reconstruction_chain(reconstruction_chain,
        expected_request=(parent_request, parent_response))
    if not replay['fresh_independent_graph_audit_required'] \
            or reconstruction_chain.get('graph') != current_graph:
        raise ValueError('GRAPH_REVISION_LINEAGE_CURRENT_GRAPH_MISMATCH')
    old_graph = reconstruction_chain['request']['input_graph']
    if old_graph != parent_request['candidate_graph']:
        raise ValueError('GRAPH_REVISION_LINEAGE_PARENT_GRAPH_MISMATCH')
    result = dict(format='candidate-graph-reconstruction-lineage-0.1.0',
        previous_graph_audit_chain=deepcopy(parent_audit_chain),
        reconstruction_chain=deepcopy(reconstruction_chain),
        previous_review=deepcopy(parent_response),
        reconstruction_response=deepcopy(reconstruction_chain['response']),
        program_execution_receipt=deepcopy(reconstruction_chain['validation_receipt']),
        actual_program_diff=dict(
            before_graph_sha256=old_graph['graph_sha256'],
            after_graph_sha256=current_graph['graph_sha256'],
            graph_changed=True,
            metadata_changes=_metadata_diff(old_graph, current_graph),
            events=_object_diff(old_graph, current_graph, 'events'),
            relations=_object_diff(old_graph, current_graph, 'relations'),
            episodes=_object_diff(old_graph, current_graph, 'episodes')),
        current_graph_sha256=current_graph['graph_sha256'],
        current_state_produced_by='SEPARATE_LLM_RECONSTRUCTION_PROPOSAL_PLUS_DETERMINISTIC_PROGRAM_ASSEMBLY',
        current_graph_requires_fresh_independent_audit=True,
        old_audit_and_material_passes_transferable=False,
        clinical_release_permitted=False)
    result['lineage_sha256'] = canonical_sha256(result)
    return result


def validate_graph_revision_lineage(lineage, current_graph):
    """Recompute the complete history; a tail hash cannot conceal substitution."""
    if not isinstance(lineage, dict) \
            or canonical_sha256({key: value for key, value in lineage.items()
                                 if key != 'lineage_sha256'}) != lineage.get('lineage_sha256'):
        raise ValueError('GRAPH_REVISION_LINEAGE_HASH_MISMATCH')
    expected = build_graph_revision_lineage(lineage.get('previous_graph_audit_chain'),
        lineage.get('reconstruction_chain'), current_graph)
    if lineage != expected:
        raise ValueError('GRAPH_REVISION_LINEAGE_REPLAY_MISMATCH')
    return lineage
