"""Restore a declared Episode audit scope from frozen real development inputs."""
from copy import deepcopy
import json

from scripts.cohort_construction.p2_auto_checkpoint_v06.independent_validation.adapter import load_case
from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import (
    freeze_candidate_graph, normalize_sources, prepare_candidate_materials,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.audit import build_graph_audit_request


GRAPH_VERSION = 'fixed-ten-v0.6.0-episode-audit-v1'
DECISION_TASK_PREFIX = 'development-v063-real-episode-20261030-'
VISIBILITY_POLICY = 'development-independent-review-required-20261030'


def select_episode(episodes):
    """Deterministic development scope, independent of outcome or model labels."""
    if not episodes:
        return None
    return deepcopy(min(episodes, key=lambda item: item['episode_id']))


def _original(source):
    # normalize_sources preserves the complete original string and binds its hash.
    digest = source.get('source_sha256', source.get('sha256'))
    from scripts.cohort_construction.p2_checkpoint_packet.compiler import text_sha256
    digest = digest or text_sha256(source['text'])
    return dict(source_id=source['source_id'], source_version=source.get('source_version')
        or 'content-sha256:' + digest, text=source['text'],
        sha256=digest, source_sha256=digest)


def resolve_episode_source_dependencies(case, events, units):
    """Separate current-unit originals from historical LLM evidence dependencies.

    Recovering a frozen original is provenance reconciliation only. It neither
    establishes target-time visibility nor grants doctor blind-review access.
    Missing dependencies stay explicit; they are never removed from the event.
    """
    current_ids = {sid for unit in units for sid in unit['source_ids']}
    available = normalize_sources([_original(source) for source in case['sources']])
    indexed = {source['source_id']: source for source in available}
    dependencies = set(current_ids)
    by_event = {}
    for event in events:
        selected = set(event.get('source_ids') or [])
        for block in event.get('actual_model_evidence', []):
            sid = block.get('source_id') if isinstance(block, dict) else None
            if not isinstance(sid, str) or not sid:
                raise ValueError('REAL_BATCH_HISTORICAL_EVIDENCE_SOURCE_ID_REQUIRED')
            selected.add(sid)
        by_event[event['event_id']] = sorted(selected)
        dependencies.update(selected)
    historical_ids = dependencies - current_ids
    recovered_ids = historical_ids & indexed.keys()
    missing_ids = dependencies - indexed.keys()
    sources = normalize_sources([indexed[sid] for sid in sorted(dependencies & indexed.keys())])
    current_sources = normalize_sources([indexed[sid] for sid in sorted(current_ids)])
    provenance = []
    for sid in sorted(dependencies):
        source = next((item for item in case['sources'] if item['source_id'] == sid), {})
        if sid in current_ids:
            role = 'CURRENT_PROCESSING_UNIT_ORIGINAL'
        else:
            role = 'HISTORICAL_MODEL_EVIDENCE_DEPENDENCY_RECOVERED_FROM_FROZEN_PACKAGE' \
                if sid in recovered_ids else 'HISTORICAL_MODEL_EVIDENCE_DEPENDENCY_NOT_FOUND_IN_DELIVERY'
        provenance.append(dict(source_id=sid, dependency_role=role,
            source_sha256=source.get('source_sha256'),
            frozen_package_hash_verified=sid in indexed,
            source_clock=deepcopy(source.get('clock')),
            clock_basis=source.get('clock_basis', 'UNRESOLVED'),
            target_time_visibility_status='NOT_CERTIFIED_BY_SOURCE_RECONCILIATION',
            doctor_blind_review_permission='NOT_GRANTED_BY_SOURCE_RECONCILIATION'))
    receipt = dict(format='episode-source-dependency-reconciliation-0.1.0',
        current_processing_unit_source_ids=sorted(current_ids),
        event_historical_evidence_source_ids=by_event,
        required_source_dependency_ids=sorted(dependencies),
        recovered_frozen_package_source_ids=sorted(recovered_ids),
        not_found_in_delivery_source_ids=sorted(missing_ids),
        dependency_closure_complete=not missing_ids,
        source_provenance=provenance,
        source_provenance_sha256=None,
        whole_patient_chart_certified=False,
        target_time_visibility_certified=False,
        doctor_blind_review_permission_granted=False)
    from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
    receipt['source_provenance_sha256'] = canonical_sha256(receipt['source_provenance'])
    return current_sources, sources, receipt


def load_episode_case(directory, *, decision_task_id=None, visibility_policy_version=VISIBILITY_POLICY):
    """Use full current-unit text, not an LLM summary or a stitched fact.

    The declared source scope is the complete source(s) of processing units that
    contain this Episode's candidate events.  Other patient sources are not
    claimed absent or clinically irrelevant; they are outside this explicit
    audit request and recorded as an uncovered source coverage limitation.
    """
    case = load_case(directory)
    episode = select_episode(case['episodes'])
    if episode is None:
        return dict(case=case, graph=None, sources=[], units=[], discoveries=[],
            graph_audit_context=None, tasks=[], failures=[],
            limitations=['EMPTY_CANDIDATE_EPISODES'])
    ids = set(episode.get('event_ids', episode.get('candidate_ids', [])))
    events = [deepcopy(event) for event in case['events'] if event['event_id'] in ids]
    if {event['event_id'] for event in events} != ids:
        raise ValueError('REAL_BATCH_EPISODE_EVENT_MISSING')
    unit_ids = {event['unit_id'] for event in events}
    units = [deepcopy(unit) for unit in case['units'] if unit['unit_id'] in unit_ids]
    if {unit['unit_id'] for unit in units} != unit_ids:
        raise ValueError('REAL_BATCH_EPISODE_UNIT_MISSING')
    current_sources, normalized, dependency_receipt = resolve_episode_source_dependencies(
        case, events, units)
    discoveries = [deepcopy(case['discoveries'][uid]) for uid in sorted(unit_ids)]
    episode_only = deepcopy(episode)
    episode_only['event_ids'] = sorted(ids)
    graph = freeze_candidate_graph(events,
        [deepcopy(relation) for relation in case['relations']
         if relation['source_event_id'] in ids and relation['target_event_id'] in ids],
        [episode_only], graph_version=GRAPH_VERSION + ':' + case['case_id'] + ':' + episode['episode_id'],
        discovery_units=units,
        unit_inventory=dict(units=units, routing_provenance=deepcopy(case['routing_provenance']),
            declared_scope='complete_originals_of_episode_processing_units'),
        final_graph_audit=case['final_graph_audit'])
    context = dict(processing_units=units, unit_inventory=graph['unit_inventory'],
        discovery_proposals=discoveries, review_proposals=events,
        final_graph_audit=case['final_graph_audit'])
    if case['routing_provenance'].get('inventory_unit_membership_verified'):
        route_units = {sid for unit in units for sid in unit['source_ids']}
        context['source_route_receipts'] = [deepcopy(route) for route in case['routing']
            if route['source_id'] in route_units]
    request = build_graph_audit_request(graph, current_sources, **context)
    tasks, failures = prepare_candidate_materials(graph, normalized,
        decision_task_id=decision_task_id or DECISION_TASK_PREFIX + case['case_id'],
        visibility_policy_version=visibility_policy_version)
    return dict(case=case, graph=graph, sources=normalized, current_sources=current_sources,
        source_dependency_receipt=dependency_receipt, units=units,
        discoveries=discoveries, graph_audit_context=context, tasks=tasks, failures=failures,
        graph_request=request,
        limitations=['EPISODE_PROCESSING_UNIT_SOURCE_SCOPE_NOT_WHOLE_PATIENT_CHART',
            'RECOVERED_HISTORICAL_EVIDENCE_NOT_TIME_VISIBILITY_OR_DOCTOR_PERMISSION_CERTIFIED'],
        episode=episode)


def episode_inventory(directory):
    """Enumerate the frozen patient graph without selecting by clinical outcome."""
    case = load_case(directory)
    selected = select_episode(case['episodes'])
    return [dict(episode_id=episode['episode_id'],
        checkpoint_ids=deepcopy(episode['checkpoint_ids']),
        selected_for_bounded_review=bool(selected and episode['episode_id'] == selected['episode_id']),
        selection_status='SELECTED_LEXICOGRAPHIC_FIRST' if selected and episode['episode_id'] == selected['episode_id']
            else 'NOT_SELECTED_FOR_BOUNDED_DEVELOPMENT_REVIEW')
        for episode in sorted(case['episodes'], key=lambda item: item['episode_id'])]
