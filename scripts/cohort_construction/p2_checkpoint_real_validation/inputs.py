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


def _validate_candidate_structure(case, selected):
    """Reject contradictory identities before writing a scope receipt."""
    episodes = case.get('episodes', [])
    events = case.get('events', [])
    episode_ids = [episode.get('episode_id') for episode in episodes]
    if any(not isinstance(value, str) or not value for value in episode_ids):
        raise ValueError('CANDIDATE_EPISODE_IDENTITY_INVALID')
    if len(episode_ids) != len(set(episode_ids)):
        raise ValueError('DUPLICATE_CANDIDATE_EPISODE_ID')
    event_ids = [event.get('event_id') for event in events]
    if any(not isinstance(value, str) or not value for value in event_ids) \
            or len(event_ids) != len(set(event_ids)):
        raise ValueError('CANDIDATE_EVENT_IDENTITY_INVALID')
    known = set(event_ids)
    checkpoint_locations = {}
    for episode in episodes:
        members = episode['event_ids'] if 'event_ids' in episode else episode.get('candidate_ids')
        checkpoints = episode.get('checkpoint_ids')
        if (not isinstance(members, list) or not isinstance(checkpoints, list)
                or not members or len(members) != len(set(members))
                or any(not isinstance(value, str) for value in members + checkpoints)
                or not set(checkpoints).issubset(members)):
            raise ValueError('CANDIDATE_EPISODE_MEMBERSHIP_INVALID:' + episode['episode_id'])
        if set(members + checkpoints) - known:
            raise ValueError('CANDIDATE_SCOPE_EVENT_MISSING:' + episode['episode_id'])
        for event_id in checkpoints:
            event = events[event_ids.index(event_id)]
            if not str(event.get('checkpoint_status') or '').startswith('CANDIDATE_'):
                raise ValueError('NONCANDIDATE_CANNOT_BE_EPISODE_CHECKPOINT:' + event_id)
            if event_id in checkpoint_locations:
                raise ValueError('CHECKPOINT_IN_MULTIPLE_CANDIDATE_EPISODES:' + event_id)
            checkpoint_locations[event_id] = episode['episode_id']
    if selected is not None:
        matching = [episode for episode in episodes if episode['episode_id'] == selected.get('episode_id')]
        if len(matching) != 1:
            raise ValueError('SELECTED_EPISODE_NOT_UNIQUE_IN_DECLARED_CASE')
        expected = matching[0]
        expected_members = expected.get('event_ids', expected.get('candidate_ids', []))
        if (selected.get('event_ids', selected.get('candidate_ids', [])) != expected_members
                or selected.get('checkpoint_ids', []) != expected.get('checkpoint_ids', [])):
            raise ValueError('SELECTED_EPISODE_MEMBERSHIP_MISMATCH')


def select_episode(episodes):
    """Deterministic development scope, independent of outcome or model labels."""
    if not episodes:
        return None
    return deepcopy(min(episodes, key=lambda item: item['episode_id']))


def candidate_scope_receipt(case, selected):
    """CE-REM-004: account for every saved event before selecting review scope.

    Lexical selection is neither Checkpoint sampling nor clinical ranking. Only
    the selected Episode's checkpoints are *requested* for bounded material
    assembly. Other events remain in the source graph with distinct dispositions;
    this receipt does not assert that assembly or material review has succeeded.
    """
    episodes = case.get('episodes', [])
    events = case.get('events', [])
    _validate_candidate_structure(case, selected)
    selected_id = selected.get('episode_id') if selected else None
    membership = {}
    for episode in episodes:
        for event_id in episode.get('event_ids', episode.get('candidate_ids', [])):
            membership.setdefault(event_id, []).append(episode['episode_id'])
    checkpoint_membership = {
        event_id for episode in episodes for event_id in episode.get('checkpoint_ids', [])}
    selected_checkpoint_ids = set(selected.get('checkpoint_ids', [])) if selected else set()
    rows = []
    for event in events:
        event_id = event['event_id']
        candidate = event_id in checkpoint_membership
        requested = event_id in selected_checkpoint_ids
        if requested:
            disposition = 'SELECTED_FOR_BOUNDED_MATERIAL_ASSEMBLY'
        elif candidate:
            disposition = 'RETAINED_CANDIDATE_OUTSIDE_DEVELOPMENT_REVIEW_SCOPE'
        elif membership.get(event_id):
            disposition = 'RETAINED_NONCHECKPOINT_EPISODE_MEMBER'
        else:
            disposition = 'RETAINED_NONCHECKPOINT_EVENT'
        rows.append(dict(event_id=event_id,
            checkpoint_status=event.get('checkpoint_status'),
            event_preserved=True, candidate_checkpoint=candidate,
            episode_ids=sorted(membership.get(event_id, [])),
            selected_episode_id=selected_id if selected_id in membership.get(event_id, []) else None,
            material_assembly_requested=requested,
            materialization_status='ASSEMBLY_REQUESTED_NOT_OUTCOME_CERTIFIED' if requested
                else 'NOT_REQUESTED_IN_THIS_DEVELOPMENT_SCOPE',
            disposition=disposition, physical_deletion=False))
    known = {event['event_id'] for event in events}
    missing = sorted((set(membership) | checkpoint_membership) - known)
    if missing:
        raise ValueError('CANDIDATE_SCOPE_EVENT_MISSING:' + ','.join(missing))
    selected_members = set(selected.get('event_ids', selected.get('candidate_ids', []))) if selected else set()
    return dict(format='candidate-development-scope-coverage-0.1.0',
        development_review_scope=dict(
            scope_type='ONE_EPISODE_FOR_BOUNDED_DEVELOPMENT_REVIEW',
            selection_method='LEXICOGRAPHICALLY_FIRST_EPISODE_ID',
            selected_episode_id=selected_id,
            selected_episode_member_ids=sorted(selected_members),
            selected_checkpoint_ids=sorted(selected_checkpoint_ids),
            not_checkpoint_sampling=True, not_clinical_importance_ranking=True,
            not_complete_downstream_decision_set=True,
            not_representative_random_sample=True),
        saved_event_count=len(events), candidate_episode_count=len(episodes),
        candidate_checkpoint_count=len(checkpoint_membership),
        selected_checkpoint_count=len(selected_checkpoint_ids),
        retained_candidate_outside_development_scope_count=sum(
            row['disposition'] == 'RETAINED_CANDIDATE_OUTSIDE_DEVELOPMENT_REVIEW_SCOPE' for row in rows),
        event_coverage=rows,
        full_saved_event_graph_preserved=True,
        material_assembly_outcome_certified=False,
        doctor_blind_review_permission_granted=False,
        clinical_release_permitted=False)


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
    budget_omissions = {'omitted_budget', 'omitted_provider_payload_budget'}
    by_event = {}
    omission_ids_by_event = {}
    for event in events:
        selected = set(event.get('source_ids') or [])
        for block in event.get('actual_model_evidence', []):
            sid = block.get('source_id') if isinstance(block, dict) else None
            if not isinstance(sid, str) or not sid:
                raise ValueError('REAL_BATCH_HISTORICAL_EVIDENCE_SOURCE_ID_REQUIRED')
            selected.add(sid)
        context = event.get('evidence_context') or {}
        ledger = context.get('source_ledger', context.get('ledger', []))
        if not isinstance(ledger, list):
            raise ValueError('REAL_BATCH_EVIDENCE_SOURCE_LEDGER_REQUIRED')
        omitted = set()
        for entry in ledger:
            if not isinstance(entry, dict):
                raise ValueError('REAL_BATCH_SOURCE_LEDGER_ROW_MALFORMED')
            disposition = entry.get('disposition')
            if disposition in budget_omissions:
                sid = entry.get('source_id')
                if not isinstance(sid, str) or not sid:
                    raise ValueError('REAL_BATCH_OMITTED_SOURCE_ID_REQUIRED')
                omitted.add(sid)
            elif isinstance(disposition, str) and disposition.startswith('omitted_'):
                raise ValueError('REAL_BATCH_OMISSION_DISPOSITION_UNSUPPORTED')
        by_event[event['event_id']] = sorted(selected)
        omission_ids_by_event[event['event_id']] = sorted(omitted)
        dependencies.update(selected | omitted)
    # Discovery can omit a whole prior document before any event-level evidence
    # context exists. Consume that frozen discovery-unit receipt as an additional
    # dependency; do not infer it from an event or call it clinical absence.
    discovery_omission_ids_by_unit = {}
    discovery_omission_receipts = []
    for unit in units:
        summary = unit.get('input_context_summary') or {}
        ledger = summary.get('source_ledger', summary.get('ledger', []))
        if not isinstance(ledger, list):
            raise ValueError('REAL_BATCH_DISCOVERY_SOURCE_LEDGER_REQUIRED')
        omitted = set()
        for entry in ledger:
            if not isinstance(entry, dict):
                raise ValueError('REAL_BATCH_DISCOVERY_SOURCE_LEDGER_ROW_MALFORMED')
            disposition = entry.get('disposition')
            if disposition not in budget_omissions:
                if isinstance(disposition, str) and disposition.startswith('omitted_'):
                    raise ValueError('REAL_BATCH_DISCOVERY_OMISSION_DISPOSITION_UNSUPPORTED')
                continue
            sid = entry.get('source_id')
            if not isinstance(sid, str) or not sid:
                raise ValueError('REAL_BATCH_DISCOVERY_OMITTED_SOURCE_ID_REQUIRED')
            omitted.add(sid)
            source = indexed.get(sid)
            discovery_omission_receipts.append(dict(
                unit_id=unit['unit_id'], source_id=sid,
                omission_stage='DISCOVERY_INPUT_CONTEXT', disposition=disposition,
                original_not_sent_for_discovery=True,
                source_state='FULL_ORIGINAL_RECOVERED_FROM_FROZEN_PACKAGE' if source
                    else 'FULL_ORIGINAL_NOT_FOUND_IN_DELIVERY',
                source_sha256=source.get('sha256') if source else None,
                recovery_is_not_prechoice_visibility_proof=True,
                doctor_blind_review_permission='NOT_GRANTED_BY_SOURCE_RECONCILIATION'))
        discovery_omission_ids_by_unit[unit['unit_id']] = sorted(omitted)
        dependencies.update(omitted)
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
        elif any(sid in ids for ids in discovery_omission_ids_by_unit.values()):
            role = 'DISCOVERY_BUDGET_OMISSION_ORIGINAL_RECOVERED_FROM_FROZEN_PACKAGE' \
                if sid in indexed else 'DISCOVERY_BUDGET_OMISSION_ORIGINAL_NOT_FOUND_IN_DELIVERY'
        elif any(sid in ids for ids in omission_ids_by_event.values()):
            role = 'BUDGET_OMISSION_ORIGINAL_RECOVERED_FROM_FROZEN_PACKAGE' \
                if sid in indexed else 'BUDGET_OMISSION_ORIGINAL_NOT_FOUND_IN_DELIVERY'
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
        event_budget_omission_source_ids=omission_ids_by_event,
        discovery_unit_budget_omission_source_ids=discovery_omission_ids_by_unit,
        discovery_budget_omission_receipts=discovery_omission_receipts,
        discovery_omission_not_clinical_absence=True,
        discovery_omission_recovery_not_prechoice_visibility_proof=True,
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
    scope_receipt = candidate_scope_receipt(case, episode)
    if episode is None:
        return dict(case=case, graph=None, sources=[], units=[], discoveries=[],
            graph_audit_context=None, tasks=[], failures=[],
            development_scope=scope_receipt,
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
            declared_scope='episode_processing_units_and_recovered_discovery_budget_omissions'),
        final_graph_audit=case['final_graph_audit'])
    context = dict(processing_units=units, unit_inventory=graph['unit_inventory'],
        discovery_proposals=discoveries, review_proposals=events,
        final_graph_audit=case['final_graph_audit'])
    if case['routing_provenance'].get('inventory_unit_membership_verified'):
        route_units = {sid for unit in units for sid in unit['source_ids']}
        context['source_route_receipts'] = [deepcopy(route) for route in case['routing']
            if route['source_id'] in route_units]
    # CE-REAL-012: historical model-evidence dependencies belong in material
    # reconciliation, not graph audit. Add only the explicit discovery omissions
    # to the current-unit graph scope, including their source-coverage binding.
    discovery_omission_ids_by_unit = dependency_receipt[
        'discovery_unit_budget_omission_source_ids']
    graph_audit_ids = ({source['source_id'] for source in current_sources}
        | {sid for ids in discovery_omission_ids_by_unit.values() for sid in ids})
    graph_audit_sources = [deepcopy(source) for source in normalized
        if source['source_id'] in graph_audit_ids]
    audit_units = deepcopy(units)
    for unit in audit_units:
        recovered_ids = discovery_omission_ids_by_unit.get(unit['unit_id'], [])
        unit['source_ids'] = sorted(set(unit['source_ids']) | set(recovered_ids))
    audit_context = dict(context, processing_units=audit_units)
    request = build_graph_audit_request(graph, graph_audit_sources, **audit_context)
    tasks, failures = prepare_candidate_materials(graph, normalized,
        decision_task_id=decision_task_id or DECISION_TASK_PREFIX + case['case_id'],
        visibility_policy_version=visibility_policy_version)
    return dict(case=case, graph=graph, sources=normalized, current_sources=current_sources,
        source_dependency_receipt=dependency_receipt, units=units,
        discoveries=discoveries, graph_audit_context=audit_context, tasks=tasks, failures=failures,
        graph_request=request, graph_audit_sources=graph_audit_sources,
        development_scope=scope_receipt,
        limitations=['EPISODE_PROCESSING_UNIT_SOURCE_SCOPE_NOT_WHOLE_PATIENT_CHART',
            'DEVELOPMENT_EPISODE_NOT_COMPLETE_CANDIDATE_OR_CLINICAL_DECISION_SET',
            'RECOVERED_HISTORICAL_EVIDENCE_NOT_TIME_VISIBILITY_OR_DOCTOR_PERMISSION_CERTIFIED'],
        episode=episode)


def episode_inventory(directory):
    """Enumerate the frozen patient graph without selecting by clinical outcome."""
    case = load_case(directory)
    _validate_candidate_structure(case, None)
    selected = select_episode(case['episodes'])
    return [dict(episode_id=episode['episode_id'],
        checkpoint_ids=deepcopy(episode['checkpoint_ids']),
        selected_for_bounded_review=bool(selected and episode['episode_id'] == selected['episode_id']),
        scope_type='BOUNDED_DEVELOPMENT_REVIEW_NOT_CHECKPOINT_SAMPLING',
        clinical_importance_ranked=False,
        selection_status='SELECTED_LEXICOGRAPHIC_FIRST' if selected and episode['episode_id'] == selected['episode_id']
            else 'RETAINED_CANDIDATE_NOT_SELECTED_FOR_BOUNDED_DEVELOPMENT_REVIEW',
        not_selected_does_not_mean_demoted_or_deleted=True)
        for episode in sorted(case['episodes'], key=lambda item: item['episode_id'])]
