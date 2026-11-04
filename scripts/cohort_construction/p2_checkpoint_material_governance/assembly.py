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
                           discovery_units=(), unit_inventory=None,
                           final_graph_audit=None):
    """Freeze candidate structure and negative-discovery coverage for later audit."""
    if not isinstance(graph_version, str) or not graph_version.strip():
        raise ContractError('CANDIDATE_GRAPH_VERSION_REQUIRED')
    if not all(isinstance(value, list) for value in (events, relations, episodes, discovery_units)):
        raise ContractError('CANDIDATE_GRAPH_COLLECTIONS_REQUIRED')
    ids = [event.get('event_id') for event in events if isinstance(event, dict)]
    if len(ids) != len(events) or any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        raise ContractError('CANDIDATE_EVENT_IDENTITY_INVALID')
    known = set(ids)
    targets = []
    normalized_episodes = []
    for episode in episodes:
        if not isinstance(episode, dict) or not isinstance(episode.get('episode_id'), str) or not episode['episode_id']:
            raise ContractError('CANDIDATE_EPISODE_IDENTITY_INVALID')
        members = episode.get('event_ids', episode.get('candidate_ids'))
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


def review_coverage(graph, sources):
    """Retain zero-discovery and rejected units, not just selected Checkpoints."""
    indexed = {source['source_id']: source for source in sources}
    records = []
    for unit in graph.get('discovery_units', []):
        events = unit.get('events', []) if isinstance(unit, dict) else []
        records.append(dict(unit_id=unit.get('unit_id') if isinstance(unit, dict) else None,
            source_ids=deepcopy(unit.get('source_ids', [])) if isinstance(unit, dict) else [],
            proposed_event_count=len(events) if isinstance(events, list) else None,
            selected_checkpoint_count=sum(event.get('event_id') in {
                cp for episode in graph['episodes'] for cp in episode['checkpoint_ids']}
                for event in events if isinstance(event, dict)),
            zero_discovery=isinstance(events, list) and not events,
            failures=deepcopy(unit.get('failures', [])) if isinstance(unit, dict) else [],
            full_original_supplied=all(sid in indexed for sid in unit.get('source_ids', [])) if isinstance(unit, dict) else False,
            coverage_status='REVIEWABLE' if isinstance(unit, dict) and all(sid in indexed for sid in unit.get('source_ids', [])) else 'SOURCE_OR_UNIT_UNRESOLVED',
            zero_or_rejected_not_clinical_exclusion=True))
    return records


def rebuild_reconstruction_context(event, sources):
    """Recheck the existing evidence-pool derivation without creating a new event."""
    return build_evidence_context(event, sources, preserve_current_context=True)


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
    omitted = {entry.get('source_id') for entry in ledger
               if isinstance(entry, dict) and entry.get('disposition') == 'omitted_budget'}
    omitted = {sid for sid in omitted if sid in indexed and sid not in selected}
    candidates = [deepcopy(indexed[sid]) for sid in sorted(omitted)]
    excluded = [deepcopy(source) for source in sources
                if source['source_id'] not in selected and source['source_id'] not in omitted]
    manifest, proposal, compiled, preparation = prepare_material_review(
        event, sources, decision_task_id=decision_task_id,
        visibility_policy_version=visibility_policy_version)
    preparation['assembly_version'] = ASSEMBLY_VERSION
    preparation['candidate_source_ids_from_evidence_budget_omissions'] = sorted(omitted)
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
        failures=deepcopy(failures), discovery_coverage=review_coverage(graph, sources),
        graph_clinical_semantics_certified=False, material_quality_measured=False,
        budget_omissions_remain_review_candidates=True,
        human_sample_status='NOT_ASSESSED', doctor_packet_created=False,
        clinical_reference_created=False, clinical_release_permitted=False)
    result['receipt_sha256'] = canonical_sha256(result)
    validate_schema('CandidateMaterialAssemblyReceipt', result)
    return result
