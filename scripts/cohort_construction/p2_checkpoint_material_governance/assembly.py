"""Assemble existing candidate graphs into complete, replayable material tasks.

The graph is frozen verbatim in the review context. Program assembly does not
certify its clinical semantics, target time, Episode membership or visibility.
Every supplied original remains in the per-Checkpoint source registry; budget
omissions are separately visible to the material reviewer.
"""
from copy import deepcopy

from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_evidence_context
from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import bind_candidate_graph
from scripts.cohort_construction.p2_checkpoint_material_governance.review import build_review_request, doctor_view
from scripts.cohort_construction.p2_checkpoint_material_governance.schema import validate_schema
from scripts.cohort_construction.p2_checkpoint_materials.prepare import MaterialPreparationError, prepare_material_review
from scripts.cohort_construction.p2_checkpoint_materials.review import create_review_draft
from scripts.cohort_construction.p2_checkpoint_material_governance.workbench import build_auto_review_assignment
from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError, canonical_sha256, text_sha256

ASSEMBLY_VERSION = 'checkpoint-candidate-material-assembly-0.1.0'


def freeze_candidate_graph(events, relations, episodes, *, graph_version,
                           discovery_units=None, unit_inventory=None,
                           final_graph_audit=None, require_reviewable_discovery=True):
    """Freeze candidate structure and negative-discovery coverage for later audit."""
    if not isinstance(graph_version, str) or not graph_version.strip():
        raise ContractError('CANDIDATE_GRAPH_VERSION_REQUIRED')
    if not all(isinstance(value, list) for value in (events, relations, episodes)):
        raise ContractError('CANDIDATE_GRAPH_COLLECTIONS_REQUIRED')
    if discovery_units is None:
        discovery_units = []
    if not isinstance(discovery_units, list):
        raise ContractError('CANDIDATE_GRAPH_COLLECTIONS_REQUIRED')
    for unit in discovery_units:
        if (not isinstance(unit, dict) or not isinstance(unit.get('events'), list)
                or any(not isinstance(event, dict) for event in unit['events'])
                or not isinstance(unit.get('source_ids'), list)
                or (require_reviewable_discovery and not unit['source_ids'])
                or any(not isinstance(sid, str) or not sid for sid in unit['source_ids'])
                or len(unit['source_ids']) != len(set(unit['source_ids']))):
            raise ContractError('CANDIDATE_DISCOVERY_UNIT_COVERAGE_INVALID')
    ids = [event.get('event_id') for event in events if isinstance(event, dict)]
    if len(ids) != len(events) or any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        raise ContractError('CANDIDATE_EVENT_IDENTITY_INVALID')
    for event in events:
        status = event.get('checkpoint_status')
        if not isinstance(status, str) or not status.startswith(('CANDIDATE_', 'UNCERTAIN_', 'EXCLUDED_', 'NOT_')):
            raise ContractError('CANDIDATE_EVENT_STATUS_INVALID')
    known = set(ids)
    targets = []
    normalized_episodes = []
    for episode in episodes:
        if not isinstance(episode, dict) or not isinstance(episode.get('episode_id'), str) or not episode['episode_id']:
            raise ContractError('CANDIDATE_EPISODE_IDENTITY_INVALID')
        members = episode.get('event_ids', episode.get('candidate_ids'))
        if 'event_ids' in episode and 'candidate_ids' in episode \
                and episode['event_ids'] != episode['candidate_ids']:
            raise ContractError('CANDIDATE_EPISODE_MEMBERSHIP_ALIAS_MISMATCH')
        checkpoints = episode.get('checkpoint_ids')
        if (not isinstance(members, list) or not isinstance(checkpoints, list)
                or any(not isinstance(value, str) or value not in known for value in members + checkpoints)
                or len(members) != len(set(members)) or len(checkpoints) != len(set(checkpoints))
                or not set(checkpoints).issubset(members)):
            raise ContractError('CANDIDATE_EPISODE_MEMBERSHIP_INVALID')
        for event_id in checkpoints:
            event = events[ids.index(event_id)]
            if not event.get('checkpoint_status', '').startswith('CANDIDATE_'):
                raise ContractError('NONCANDIDATE_CANNOT_BE_EPISODE_CHECKPOINT')
        item = deepcopy(episode)
        item['event_ids'] = deepcopy(members)
        normalized_episodes.append(item)
        targets.extend(checkpoints)
    if len(targets) != len(set(targets)):
        raise ContractError('CHECKPOINT_IN_MULTIPLE_CANDIDATE_EPISODES')
    episode_ids = [episode['episode_id'] for episode in episodes]
    if len(episode_ids) != len(set(episode_ids)):
        raise ContractError('DUPLICATE_CANDIDATE_EPISODE_ID')
    for relation in relations:
        if not isinstance(relation, dict) or any(relation.get(key) not in known for key in ('source_event_id', 'target_event_id')):
            raise ContractError('CANDIDATE_RELATION_EVENT_UNKNOWN')
    result = dict(format='candidate-reconstruction-graph-snapshot-0.1.0', graph_version=graph_version,
                  events=deepcopy(events), relations=deepcopy(relations), episodes=normalized_episodes,
                  discovery_units=deepcopy(discovery_units), unit_inventory=deepcopy(unit_inventory),
                  final_graph_audit=deepcopy(final_graph_audit),
                  graph_is_candidate_not_clinical_reference=True)
    result['graph_sha256'] = canonical_sha256(result)
    return result


def normalize_sources(sources):
    """Bind exact full original strings; absent text is never an empty original."""
    if not isinstance(sources, list):
        raise ContractError('ASSEMBLY_SOURCE_ARRAY_REQUIRED')
    result, seen = [], set()
    for source in sources:
        if not isinstance(source, dict):
            raise ContractError('ASSEMBLY_SOURCE_OBJECT_REQUIRED')
        sid, text = source.get('source_id'), source.get('text')
        if not isinstance(sid, str) or not sid or not isinstance(text, str) or not text:
            raise ContractError('ASSEMBLY_FULL_SOURCE_TEXT_REQUIRED')
        if sid in seen:
            raise ContractError('ASSEMBLY_DUPLICATE_SOURCE_ID')
        seen.add(sid)
        digest = text_sha256(text)
        if source.get('sha256', source.get('source_sha256', digest)) != digest:
            raise ContractError('ASSEMBLY_SOURCE_HASH_MISMATCH')
        version = source.get('source_version') or 'content-sha256:' + digest
        if not isinstance(version, str) or not version:
            raise ContractError('ASSEMBLY_SOURCE_VERSION_REQUIRED')
        result.append(dict(source_id=sid, source_version=version, text=text, sha256=digest, source_sha256=digest))
    return result


def review_coverage(graph, sources, tasks=None, failures=None):
    """Retain every event without equating graph membership with a task request."""
    indexed = {source['source_id']: source for source in sources}
    checkpoint_ids = {cp for episode in graph['episodes'] for cp in episode['checkpoint_ids']}
    requested = None if tasks is None and failures is None else {
        task['event']['event_id'] for task in tasks or []} | {
        failure.get('event_id') for failure in failures or []}
    outcomes = {task['event']['event_id']: ('PREPARED_NOT_REVIEWED', task['episode_id'])
                for task in tasks or []}
    for failure in failures or []:
        event_id = failure.get('event_id')
        if event_id in outcomes:
            raise ContractError('ASSEMBLY_EVENT_OUTCOME_NOT_UNIQUE')
        outcomes[event_id] = ('ASSEMBLY_HOLD', failure.get('episode_id'))
    if requested is not None:
        task_ids = [task['event']['event_id'] for task in tasks or []]
        failure_ids = [failure.get('event_id') for failure in failures or []]
        if len(task_ids) != len(set(task_ids)) or len(failure_ids) != len(set(failure_ids)) \
                or set(task_ids) & set(failure_ids):
            raise ContractError('ASSEMBLY_EVENT_OUTCOME_NOT_UNIQUE')
        if requested - checkpoint_ids:
            raise ContractError('ASSEMBLY_REQUEST_NOT_DECLARED_CHECKPOINT')
        if any(failure.get('event_id') not in checkpoint_ids
               or failure.get('status') != 'ASSEMBLY_HOLD' for failure in failures or []):
            raise ContractError('ASSEMBLY_FAILURE_RECEIPT_INVALID')
        by_id = {event['event_id']: event for event in graph['events']}
        compiled_ids = []
        for task in tasks or []:
            if not isinstance(task, dict) or not isinstance(task.get('event'), dict) \
                    or not isinstance(task.get('compiled'), dict):
                raise ContractError('ASSEMBLY_TASK_BINDING_INVALID')
            event_id = task['event'].get('event_id')
            target = task['compiled'].get('target') or {}
            checkpoint_id = target.get('checkpoint_id')
            if checkpoint_id != event_id:
                raise ContractError('ASSEMBLY_COMPILED_TARGET_EVENT_MISMATCH')
            if task.get('task_context', {}).get('candidate_graph_binding', {}).get('target_event_id') != event_id \
                    or task['task_context']['candidate_graph_binding'].get('checkpoint_id') != checkpoint_id:
                raise ContractError('ASSEMBLY_TASK_GRAPH_BINDING_MISMATCH')
            compiled_ids.append(checkpoint_id)
        if len(compiled_ids) != len(set(compiled_ids)):
            raise ContractError('ASSEMBLY_COMPILED_TARGET_NOT_UNIQUE')
        for failure in failures or []:
            if not isinstance(failure, dict) or not isinstance(failure.get('reason_code'), str) \
                    or not failure['reason_code'] or failure.get('clinical_release_permitted') is not False:
                raise ContractError('ASSEMBLY_FAILURE_RECEIPT_INVALID')
        for event_id, (_, episode_id) in outcomes.items():
            if episode_id not in {episode['episode_id'] for episode in graph['episodes']
                                  if event_id in episode['checkpoint_ids']}:
                raise ContractError('ASSEMBLY_EVENT_EPISODE_BINDING_MISMATCH')
            if event_id not in by_id:
                raise ContractError('ASSEMBLY_TARGET_EVENT_NOT_UNIQUE')
    records = []
    for unit in graph.get('discovery_units', []):
        valid = isinstance(unit, dict) and isinstance(unit.get('events'), list) \
            and all(isinstance(event, dict) for event in unit.get('events', []))
        source_ids = unit.get('source_ids') if isinstance(unit, dict) else None
        valid = valid and isinstance(source_ids, list) and bool(source_ids) \
            and all(isinstance(sid, str) and sid for sid in source_ids) \
            and len(source_ids) == len(set(source_ids))
        events = unit.get('events', []) if valid else []
        source_ids = source_ids if valid else []
        supplied = valid and all(sid in indexed for sid in source_ids)
        records.append(dict(unit_id=unit.get('unit_id') if isinstance(unit, dict) else None,
            source_ids=deepcopy(source_ids),
            proposed_event_count=len(events) if valid else None,
            selected_checkpoint_count=sum(event.get('event_id') in checkpoint_ids
                for event in events),
            zero_discovery=valid and not events,
            failures=deepcopy(unit.get('failures', [])) if isinstance(unit, dict) else [],
            full_original_supplied=supplied,
            coverage_status='REVIEWABLE' if supplied else 'SOURCE_OR_UNIT_UNRESOLVED',
            zero_or_rejected_not_clinical_exclusion=True))
    event_coverage = []
    for event in graph['events']:
        event_id = event['event_id']
        candidate = event_id in checkpoint_ids
        is_requested = candidate if requested is None else event_id in requested
        if event_id in outcomes:
            disposition = outcomes[event_id][0]
        elif is_requested:
            disposition = 'ASSEMBLY_REQUESTED_OUTCOME_NOT_CERTIFIED'
        elif candidate:
            disposition = 'RETAINED_CANDIDATE_NOT_REQUESTED_IN_THIS_ASSEMBLY'
        else:
            disposition = 'RETAINED_NOT_REQUESTED_NONCHECKPOINT_EVENT'
        event_coverage.append(dict(event_id=event_id,
            candidate_checkpoint=candidate, material_assembly_requested=is_requested,
            materialization_status=disposition, disposition=disposition,
            event_preserved=True, physical_deletion=False,
            material_review_passed=False, doctor_blind_review_permission_granted=False))
    return dict(discovery_units=records, event_coverage=event_coverage,
        candidate_checkpoint_count=len(checkpoint_ids),
        assembly_requested_count=len(checkpoint_ids) if requested is None else len(requested),
        assembly_request_accounting='GRAPH_ELIGIBILITY_ONLY_NOT_TASK_RECEIPT'
            if requested is None else 'BOUND_TO_TASK_AND_FAILURE_RECEIPTS',
        full_graph_not_clinical_reference=True)


def rebuild_reconstruction_context(event, sources):
    """Recheck the existing evidence-pool derivation without creating a new event."""
    return build_evidence_context(event, sources, preserve_current_context=True)


def _discovery_budget_omissions(graph, event, indexed):
    """Read the frozen discovery-unit omission receipt for this event only.

    A unit-level receipt applies only to events explicitly bound to that unit;
    events in other units do not inherit it. This is provenance recovery, not a
    target-time visibility assertion.
    """
    omitted, receipts = set(), []
    for unit in graph.get('discovery_units', []):
        if unit.get('unit_id') != event.get('unit_id'):
            continue
        summary = unit.get('input_context_summary') or {}
        ledger = summary.get('source_ledger', summary.get('ledger', []))
        if not isinstance(ledger, list):
            raise MaterialPreparationError('ASSEMBLY_DISCOVERY_SOURCE_LEDGER_REQUIRED', dict(
                preparation_status='REJECTED', issues=[dict(
                    code='ASSEMBLY_DISCOVERY_SOURCE_LEDGER_REQUIRED', unit_id=unit.get('unit_id'))]))
        for entry in ledger:
            if not isinstance(entry, dict):
                raise MaterialPreparationError('ASSEMBLY_DISCOVERY_SOURCE_LEDGER_ROW_MALFORMED', dict(
                    preparation_status='REJECTED', issues=[dict(
                        code='ASSEMBLY_DISCOVERY_SOURCE_LEDGER_ROW_MALFORMED', unit_id=unit.get('unit_id'))]))
            disposition = entry.get('disposition')
            if disposition not in {'omitted_budget', 'omitted_provider_payload_budget'}:
                if isinstance(disposition, str) and disposition.startswith('omitted_'):
                    raise MaterialPreparationError('ASSEMBLY_DISCOVERY_OMISSION_DISPOSITION_UNSUPPORTED', dict(
                        preparation_status='REJECTED', issues=[dict(
                            code='ASSEMBLY_DISCOVERY_OMISSION_DISPOSITION_UNSUPPORTED', disposition=disposition)]))
                continue
            sid = entry.get('source_id')
            if not isinstance(sid, str) or not sid:
                raise MaterialPreparationError('ASSEMBLY_DISCOVERY_OMITTED_SOURCE_ID_REQUIRED', dict(
                    preparation_status='REJECTED', issues=[dict(
                        code='ASSEMBLY_DISCOVERY_OMITTED_SOURCE_ID_REQUIRED')]))
            if sid not in indexed:
                raise MaterialPreparationError('ASSEMBLY_DISCOVERY_OMITTED_SOURCE_MISSING', dict(
                    preparation_status='REJECTED', issues=[dict(
                        code='ASSEMBLY_DISCOVERY_OMITTED_SOURCE_MISSING', source_ids=[sid],
                        unit_id=unit.get('unit_id'))]))
            omitted.add(sid)
            receipts.append(dict(unit_id=unit['unit_id'], source_id=sid,
                omission_stage='DISCOVERY_INPUT_CONTEXT', disposition=disposition,
                source_state='FULL_ORIGINAL_RECOVERED_PENDING_MATERIAL_REVIEW',
                source_sha256=indexed[sid]['sha256'],
                discovery_omission_is_not_clinical_absence=True,
                recovery_is_not_prechoice_visibility_proof=True,
                doctor_blind_review_permission='NOT_GRANTED_BY_SOURCE_RECONCILIATION'))
    return omitted, receipts


def prepare_checkpoint_task(graph, sources, target_event_id, *, decision_task_id,
                            visibility_policy_version):
    """Prepare one Checkpoint, preserving omitted and excluded complete originals."""
    indexed = {source['source_id']: source for source in sources}
    matches = [event for event in graph['events'] if event['event_id'] == target_event_id]
    if len(matches) != 1:
        raise ContractError('ASSEMBLY_TARGET_EVENT_NOT_UNIQUE')
    event = matches[0]
    memberships = [episode for episode in graph['episodes'] if target_event_id in episode['checkpoint_ids']]
    if len(memberships) != 1:
        raise ContractError('ASSEMBLY_TARGET_EPISODE_NOT_UNIQUE')
    selected = set(event.get('source_ids') or [])
    selected.update(block.get('source_id') for block in event.get('actual_model_evidence', []))
    missing = sorted(sid for sid in selected if sid not in indexed)
    if missing:
        raise MaterialPreparationError('ASSEMBLY_SELECTED_SOURCE_MISSING', dict(
            preparation_status='REJECTED', issues=[dict(code='ASSEMBLY_SELECTED_SOURCE_MISSING', source_ids=missing)],
            selected_source_ids=sorted(selected)))
    context = event.get('evidence_context')
    if not isinstance(context, dict) or not isinstance(context.get('source_ledger', context.get('ledger')), list):
        raise MaterialPreparationError('ASSEMBLY_EVIDENCE_LEDGER_REQUIRED', dict(
            preparation_status='REJECTED', issues=[dict(code='ASSEMBLY_EVIDENCE_LEDGER_REQUIRED')]))
    ledger = context.get('source_ledger', context.get('ledger'))
    # Only actual complete originals may become candidate sources. Budget omissions
    # are candidate for review; every other supplied source is explicit exclusion,
    # not a claim that it is clinically irrelevant or invisible at the target time.
    budget_omissions = {'omitted_budget', 'omitted_provider_payload_budget'}
    omitted = set()
    for entry in ledger:
        if not isinstance(entry, dict):
            raise MaterialPreparationError('ASSEMBLY_SOURCE_LEDGER_ROW_MALFORMED', dict(
                preparation_status='REJECTED', issues=[dict(
                    code='ASSEMBLY_SOURCE_LEDGER_ROW_MALFORMED')]))
        disposition = entry.get('disposition')
        if disposition in budget_omissions:
            sid = entry.get('source_id')
            if not isinstance(sid, str) or not sid:
                raise MaterialPreparationError('ASSEMBLY_OMITTED_SOURCE_ID_REQUIRED', dict(
                    preparation_status='REJECTED', issues=[dict(
                        code='ASSEMBLY_OMITTED_SOURCE_ID_REQUIRED')]))
            if sid not in indexed:
                raise MaterialPreparationError('ASSEMBLY_OMITTED_SOURCE_MISSING', dict(
                    preparation_status='REJECTED', issues=[dict(
                        code='ASSEMBLY_OMITTED_SOURCE_MISSING', source_ids=[sid])]))
            if sid not in selected:
                omitted.add(sid)
        elif isinstance(disposition, str) and disposition.startswith('omitted_'):
            raise MaterialPreparationError('ASSEMBLY_OMISSION_DISPOSITION_UNSUPPORTED', dict(
                preparation_status='REJECTED', issues=[dict(
                    code='ASSEMBLY_OMISSION_DISPOSITION_UNSUPPORTED', disposition=disposition)]))
    discovery_omitted, discovery_receipts = _discovery_budget_omissions(graph, event, indexed)
    omitted.update(discovery_omitted - selected)
    candidates = [deepcopy(indexed[sid]) for sid in sorted(omitted)]
    excluded = [deepcopy(source) for source in sources
                if source['source_id'] not in selected and source['source_id'] not in omitted]
    manifest, proposal, compiled, preparation = prepare_material_review(
        event, sources, decision_task_id=decision_task_id,
        visibility_policy_version=visibility_policy_version)
    preparation['assembly_version'] = ASSEMBLY_VERSION
    preparation['candidate_source_ids_from_evidence_budget_omissions'] = sorted(omitted)
    preparation['candidate_source_ids_from_discovery_budget_omissions'] = sorted(discovery_omitted - selected)
    preparation['discovery_budget_omission_receipts'] = discovery_receipts
    preparation['assembly_source_scope'] = 'all supplied complete originals retained in candidate/excluded registry; no inferred clinical irrelevance'
    preparation['reconstruction_context_rebuild_required_for_changed_graph'] = True
    preparation.pop('audit_sha256', None)
    preparation['audit_sha256'] = canonical_sha256(preparation)
    task_context = bind_candidate_graph(graph, graph_version=graph['graph_version'],
        target_event_id=target_event_id, checkpoint_id=compiled['target']['checkpoint_id'],
        task_context=dict(decision_time_evidence=deepcopy(event.get('time_validation', 'UNRESOLVED')),
            clinical_visibility_certified=False,
            candidate_episode_id=memberships[0]['episode_id'],
            reconstruction_context_is_not_blind_packet=True,
            material_release_permitted=False))
    return dict(format=ASSEMBLY_VERSION, event=deepcopy(event), manifest=manifest,
        split_proposal=proposal, compiled=compiled, preparation_audit=preparation,
        task_context=task_context, candidate_sources=candidates, excluded_sources=excluded,
        sources=deepcopy(sources), episode_id=memberships[0]['episode_id'])


def prepare_candidate_materials(graph, sources, *, decision_task_id, visibility_policy_version):
    """Assemble all targets while retaining local source/preparation failures."""
    tasks, failures = [], []
    for episode in graph['episodes']:
        for event_id in episode['checkpoint_ids']:
            try:
                tasks.append(prepare_checkpoint_task(graph, sources, event_id,
                    decision_task_id=decision_task_id, visibility_policy_version=visibility_policy_version))
            except Exception as exc:
                code = exc.code if isinstance(exc, MaterialPreparationError) else str(exc) if isinstance(exc, ContractError) else type(exc).__name__
                failures.append(dict(event_id=event_id, episode_id=episode['episode_id'],
                    status='ASSEMBLY_HOLD', reason_code=code,
                    audit=deepcopy(exc.audit) if isinstance(exc, MaterialPreparationError) else None,
                    clinical_release_permitted=False))
    return tasks, failures


def material_item(task, chain):
    """Bind the chain tail without promoting its automatic state."""
    from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import validate_review_chain
    replay = validate_review_chain(chain, expected_graph_context=task['task_context'])
    if replay['state'] != chain['state']:
        raise ContractError('ASSEMBLY_CHAIN_STATE_MISMATCH')
    return dict(manifest=deepcopy(chain['manifest']), compiled=deepcopy(chain['compiled']),
        doctor_view=doctor_view(chain['compiled']), auto_review_chain=deepcopy(chain),
        revision_ledger=[deepcopy(round_['ledger']) for round_ in chain['rounds']],
        state=chain['state'], event_reference=task['event']['event_id'],
        open_issues=[], required_actions=[], human_sample_status='NOT_ASSESSED', expert_status='NOT_ASSESSED')


def pending_workbench(task, chain, *, assignment_id):
    """Create an unattested human draft for either automatic PASS or HOLD."""
    draft = create_review_draft(chain['compiled'], chain['manifest'], task['preparation_audit'],
        reviewer_id='ASSEMBLY-PENDING-ACTUAL-HUMAN-NOT-AN-IDENTITY')
    return build_auto_review_assignment(chain, draft, task['preparation_audit'],
        assignment_id=assignment_id, selected_event=task['event'], coverage_sources=task['sources'])


def assembly_receipt(graph, sources, tasks, failures):
    """Summarize the assembly, never claim automatic or clinical quality acceptance."""
    from .episode import validate_candidate_snapshot_structure
    # A historical malformed/source-less discovery unit remains visible as an
    # unresolved coverage row. It never bypasses event/Episode/relation checks.
    validate_candidate_snapshot_structure(graph, require_reviewable_discovery=False)
    coverage = review_coverage(graph, sources, tasks, failures)
    result = dict(format='candidate-material-assembly-receipt-0.1.0',
        assembly_version=ASSEMBLY_VERSION, graph_version=graph['graph_version'],
        graph_sha256=canonical_sha256(graph), supplied_source_count=len(sources),
        supplied_sources_sha256=canonical_sha256(sources),
        candidate_episode_count=len(graph['episodes']),
        candidate_checkpoint_count=sum(len(episode['checkpoint_ids']) for episode in graph['episodes']),
        prepared_checkpoint_count=len(tasks), assembly_hold_count=len(failures),
        tasks=[dict(event_id=task['event']['event_id'], episode_id=task['episode_id'],
            checkpoint_id=task['compiled']['target']['checkpoint_id'],
            initial_compiled_sha256=task['compiled']['compiled_sha256'],
            included_source_ids=[source['source_id'] for source in task['manifest']['sources']],
            candidate_source_ids=[source['source_id'] for source in task['candidate_sources']],
            excluded_source_ids=[source['source_id'] for source in task['excluded_sources']],
            preparation_sha256=canonical_sha256(task['preparation_audit'])) for task in tasks],
        failures=deepcopy(failures), discovery_coverage=coverage['discovery_units'],
        event_material_coverage=coverage['event_coverage'],
        graph_clinical_semantics_certified=False, material_quality_measured=False,
        budget_omissions_remain_review_candidates=True,
        human_sample_status='NOT_ASSESSED', doctor_packet_created=False,
        clinical_reference_created=False, clinical_release_permitted=False)
    result['receipt_sha256'] = canonical_sha256(result)
    validate_schema('CandidateMaterialAssemblyReceipt', result)
    return result
