"""Compile and govern a candidate Episode without changing the event graph."""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError, canonical_sha256
from scripts.cohort_construction.p2_checkpoint_material_governance.review import doctor_view
from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import validate_review_chain, validate_candidate_graph_context
from scripts.cohort_construction.p2_checkpoint_material_governance.schema import validate_schema


def validate_candidate_snapshot_identity(snapshot):
    """Validate identity without expanding a retained legacy coverage ledger.

    New graph construction rejects malformed/source-less units. Receipt-only
    review of a preserved historical graph must still expose those units as
    unresolved, not fabricate a replacement graph or erase its coverage.
    """
    if not isinstance(snapshot, dict) or 'graph_sha256' not in snapshot:
        raise ContractError('EPISODE_GRAPH_IDENTITY_INVALID')
    if snapshot['graph_sha256'] != canonical_sha256({key: value
            for key, value in snapshot.items() if key != 'graph_sha256'}):
        raise ContractError('EPISODE_GRAPH_IDENTITY_INVALID')


def _validate_candidate_snapshot(snapshot, candidate_version=None, *, require_reviewable_discovery=True):
    """Replay graph invariants, including legacy read-only graph snapshots."""
    from .assembly import freeze_candidate_graph
    if (not isinstance(snapshot.get('events'), list)
            or not isinstance(snapshot.get('episodes'), list)
            or ('graph_version' in snapshot and not isinstance(snapshot['graph_version'], str))):
        raise ContractError('EPISODE_GRAPH_IDENTITY_INVALID')
    if snapshot.get('graph_sha256') is not None \
            and snapshot['graph_sha256'] != canonical_sha256({key: value
                for key, value in snapshot.items() if key != 'graph_sha256'}):
        raise ContractError('EPISODE_GRAPH_IDENTITY_INVALID')
    # Some historical public references authenticated only the graph subset;
    # their content must still pass every retained structural invariant below.
    self_describing = snapshot.get('format') == 'candidate-reconstruction-graph-snapshot-0.1.0'
    if self_describing and 'graph_sha256' not in snapshot:
        raise ContractError('EPISODE_GRAPH_IDENTITY_INVALID')
    events = deepcopy(snapshot['events'])
    episodes = deepcopy(snapshot['episodes'])
    for episode in episodes:
        alias = episode['event_ids'] if 'event_ids' in episode else episode.get('candidate_ids')
        if 'event_ids' in episode and 'candidate_ids' in episode \
                and episode['event_ids'] != episode['candidate_ids']:
            raise ContractError('CANDIDATE_EPISODE_MEMBERSHIP_ALIAS_MISMATCH')
        episode['event_ids'] = deepcopy(alias)
        episode.pop('candidate_ids', None)
        # Compare legacy membership under the current normalized representation;
        # the saved snapshot itself is never modified.
    relations = snapshot.get('relations', [])
    # Older public Episode boundaries retained only the graph subset. Replay
    # those fields without fabricating a new version or identity in history.
    rebuilt = freeze_candidate_graph(events, relations, episodes,
        graph_version=snapshot.get('graph_version', candidate_version),
        discovery_units=snapshot.get('discovery_units', []) if require_reviewable_discovery else [],
        unit_inventory=snapshot.get('unit_inventory'),
        final_graph_audit=snapshot.get('final_graph_audit'),
        require_reviewable_discovery=require_reviewable_discovery)
    # Replay every retained structural field. A historical subset has no fields
    # for the newer coverage/default projection, so none may be fabricated into
    # its authenticated identity merely for comparison.
    for key, value in rebuilt.items():
        if key == 'discovery_units' and not require_reviewable_discovery:
            continue  # review_coverage retains malformed rows as unresolved
        if key == 'graph_sha256':
            continue  # exact whole-object identity was validated above
        if key == 'episodes':
            original = deepcopy(snapshot[key])
            for episode in original:
                episode['event_ids'] = episode['event_ids'] if 'event_ids' in episode else episode.get('candidate_ids')
                episode.pop('candidate_ids', None)
            if original != value:
                raise ContractError('EPISODE_GRAPH_INVARIANTS_CHANGED')
            continue
        if key in snapshot and snapshot[key] != value:
            raise ContractError('EPISODE_GRAPH_INVARIANTS_CHANGED')


def validate_candidate_snapshot_structure(snapshot, *, require_reviewable_discovery=True):
    """Validate graph invariants without changing a legacy coverage ledger."""
    _validate_candidate_snapshot(snapshot, require_reviewable_discovery=require_reviewable_discovery)

def build_episode_material_set(episode_reference, checkpoint_materials):
    """Bind already-governed Checkpoint versions; order is candidate, not fact."""
    if not isinstance(episode_reference, dict):
        raise ContractError('EPISODE_REFERENCE_OBJECT_REQUIRED')
    for key in ('episode_id', 'candidate_version', 'input_sha256'):
        if not isinstance(episode_reference.get(key), str) or not episode_reference[key]:
            raise ContractError('EPISODE_REFERENCE_BINDING_REQUIRED')
    snapshot = episode_reference.get('graph_snapshot')
    if (not isinstance(snapshot, dict) or canonical_sha256(snapshot) != episode_reference['input_sha256']):
        raise ContractError('EPISODE_VERIFIABLE_GRAPH_SNAPSHOT_REQUIRED')
    _validate_candidate_snapshot(snapshot, episode_reference['candidate_version'])
    episodes = snapshot.get('episodes')
    if not isinstance(episodes, list):
        raise ContractError('EPISODE_GRAPH_MEMBERSHIP_REQUIRED')
    matching = [item for item in episodes if isinstance(item, dict)
                and item.get('episode_id') == episode_reference['episode_id']]
    episode_members = matching[0].get('event_ids', matching[0].get('candidate_ids')) if len(matching) == 1 else None
    if not isinstance(episode_members, list):
        raise ContractError('EPISODE_GRAPH_MEMBERSHIP_REQUIRED')
    checkpoint_ids = matching[0].get('checkpoint_ids')
    if not isinstance(checkpoint_ids, list) or not checkpoint_ids \
            or not set(checkpoint_ids).issubset(episode_members):
        raise ContractError('EPISODE_CHECKPOINT_DECLARATION_REQUIRED')
    if len(checkpoint_ids) != len(set(checkpoint_ids)):
        raise ContractError('EPISODE_DUPLICATE_CHECKPOINT_DECLARATION')
    if len(episode_members) != len(set(episode_members)):
        raise ContractError('EPISODE_DUPLICATE_EVENT_MEMBERSHIP')
    materials, targets = [], set()
    for item in checkpoint_materials:
        target = item['compiled']['target']
        if target['checkpoint_id'] in targets:
            raise ContractError('DUPLICATE_CHECKPOINT_MATERIAL')
        targets.add(target['checkpoint_id'])
        if item['compiled']['compiled_sha256'] != item['doctor_view']['compiled_sha256']:
            raise ContractError('EPISODE_DOCTOR_VIEW_BINDING_MISMATCH')
        if item['doctor_view'] != doctor_view(item['compiled']):
            raise ContractError('EPISODE_DOCTOR_VIEW_PROJECTION_MISMATCH')
        chain = item['auto_review_chain']
        # Distinguish graph replacement, a rebound material target, and a
        # declaration that substitutes a non-Checkpoint member.
        graph = chain.get('task_context', {}).get('candidate_graph_binding')
        event_reference = item.get('event_reference')
        event_id = event_reference.get('event_id') if isinstance(event_reference, dict) else event_reference
        task_episode_id = chain.get('task_context', {}).get('candidate_episode_id')
        if task_episode_id != episode_reference['episode_id']:
            raise ContractError('EPISODE_TASK_CONTEXT_BINDING_MISMATCH')
        if isinstance(event_reference, dict) and (
                set(event_reference) - {'event_id', 'checkpoint_id'}
                or event_reference.get('checkpoint_id') != target['checkpoint_id']):
            raise ContractError('EPISODE_EVENT_REFERENCE_BINDING_MISMATCH')
        if not isinstance(graph, dict) \
                or graph.get('graph_sha256') != episode_reference['input_sha256'] \
                or graph.get('checkpoint_id') != target['checkpoint_id']:
            if event_id == (graph or {}).get('target_event_id') \
                    and event_id not in checkpoint_ids:
                raise ContractError('EPISODE_CHECKPOINT_MEMBERSHIP_MISMATCH')
            raise ContractError('EPISODE_GRAPH_CHANGED_RESTART_MATERIAL_REVIEW')
        if event_id != graph.get('target_event_id'):
            raise ContractError('EPISODE_EVENT_MEMBERSHIP_MISMATCH')
        if event_id not in checkpoint_ids:
            raise ContractError('EPISODE_CHECKPOINT_MEMBERSHIP_MISMATCH')
        validate_review_chain(chain)
        graph = validate_candidate_graph_context(chain['task_context'], target, required=True)
        if (graph['graph_version'] != episode_reference['candidate_version']
                or graph['graph_sha256'] != episode_reference['input_sha256']
                or chain['task_context']['candidate_graph_snapshot'] != snapshot):
            raise ContractError('EPISODE_GRAPH_CHANGED_RESTART_MATERIAL_REVIEW')
        if event_id != graph['target_event_id']:
            raise ContractError('EPISODE_EVENT_MEMBERSHIP_MISMATCH')
        if event_id not in checkpoint_ids:
            raise ContractError('EPISODE_CHECKPOINT_MEMBERSHIP_MISMATCH')
        if item['manifest'] != chain['manifest'] or item['compiled'] != chain['compiled']:
            raise ContractError('EPISODE_CHAIN_MATERIAL_VERSION_MISMATCH')
        if (not isinstance(chain, dict) or chain.get('compiled', {}).get('compiled_sha256') != item['compiled']['compiled_sha256']
                or chain.get('state') not in ('AUTO_REVIEW_PASS', 'AUTO_REVIEW_HOLD')):
            raise ContractError('EPISODE_REVIEW_CHAIN_BINDING_REQUIRED')
        if item['state'] != chain['state']:
            raise ContractError('EPISODE_REVIEW_STATE_MISMATCH')
        expected_ledger = [round_['ledger'] for round_ in chain['rounds']]
        if item['revision_ledger'] != expected_ledger:
            raise ContractError('EPISODE_REVISION_LEDGER_MISMATCH')
        if item['state'] == 'CLINICAL_PACKET_ELIGIBLE':
            raise ContractError('CLINICAL_RELEASE_REQUIRES_EXTERNAL_GOVERNANCE')
        materials.append(dict(
            checkpoint_id=target['checkpoint_id'],
            event_reference=deepcopy(item.get('event_reference')),
            manifest=deepcopy(item['manifest']),
            compiled=deepcopy(item['compiled']),
            doctor_view=deepcopy(item['doctor_view']),
            auto_review_chain=deepcopy(item['auto_review_chain']),
            revision_ledger=deepcopy(item['revision_ledger']),
            state=item['state'], open_issues=deepcopy(item.get('open_issues', [])),
            required_actions=deepcopy(item.get('required_actions', [])),
            human_sample_status=item.get('human_sample_status', 'NOT_ASSESSED'),
            expert_status=item.get('expert_status', 'NOT_ASSESSED'),
        ))
    if {item['event_reference'].get('event_id') if isinstance(item['event_reference'], dict)
            else item['event_reference'] for item in materials} != set(checkpoint_ids):
        raise ContractError('EPISODE_CHECKPOINT_COVERAGE_MISMATCH')
    result = dict(
        format='candidate-episode-material-set-0.2.0',
        episode_reference=deepcopy(episode_reference), checkpoint_materials=materials,
        cross_checkpoint_checks=[
            dict(code='SOURCE_APPEARS_IN_MULTIPLE_CHECKPOINTS',
                 checkpoint_id=current['checkpoint_id'], source_id=source['source_id'],
                 also_used_by_checkpoint_id=other['checkpoint_id'],
                 clinical_visibility_certified=False,
                 review_obligation='Check target-specific restatements and future visibility in original text; shared identity alone does not prove leakage.')
            for current in materials
            for source in current['manifest']['sources']
            for other in materials
            if other is not current
            and any(s['source_id'] == source['source_id'] for s in other['manifest']['sources'])
        ],
        set_version='candidate-episode-materials-0.2.0',
        clinical_release_permitted=False, event_graph_modified=False,
    )
    result['set_sha256'] = canonical_sha256(result)
    validate_schema('CandidateEpisodeMaterialSet', result)
    return result


def graph_revision_handoff(material_set, checkpoint_id, review_id, reason):
    """Return an explicit input for the separate reconstruction layer."""
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise ContractError('CHECKPOINT_ID_REQUIRED')
    validate_episode_material_set(material_set)
    target = next((item for item in material_set['checkpoint_materials']
                   if item['checkpoint_id'] == checkpoint_id), None)
    if target is None:
        raise ContractError('CHECKPOINT_NOT_IN_MATERIAL_SET')
    if not isinstance(reason, str) or not reason.strip():
        raise ContractError('GRAPH_REVISION_REASON_REQUIRED')
    return dict(
        format='candidate-reconstruction-revision-request-0.1.0',
        episode_reference=deepcopy(material_set['episode_reference']),
        checkpoint_id=checkpoint_id,
        event_reference=deepcopy(target['event_reference']),
        originating_material_review_id=review_id, reason=reason,
        source_manifest_sha256=canonical_sha256(target['manifest']),
        previous_compiled_sha256=target['compiled']['compiled_sha256'],
        required_reconstruction_steps=[
            'generate_new_candidate_event_or_relation_version',
            'rerun_final_graph_dependency_audit',
            'prepare_new_manifest_and_partition',
            'restart_material_review_from_initial_version',
        ],
        material_layer_may_change_event_graph=False,
        previous_material_pass_transferable=False,
    )


def validate_episode_material_set(material_set):
    """Rebuild the entire set before accepting a reconstruction handoff."""
    validate_schema('CandidateEpisodeMaterialSet', material_set)
    rebuilt = build_episode_material_set(material_set['episode_reference'], material_set['checkpoint_materials'])
    if rebuilt != material_set:
        raise ContractError('EPISODE_SET_REPLAY_MISMATCH')
    return True


def restart_material_review_after_graph_revision(previous_set, checkpoint_id,
        new_episode_reference, manifest, compiled, ask, *, target_event_id,
        task_context=None, candidate_sources=(), excluded_sources=(), max_rounds=3):
    """Explicit reconstruction-to-material entry; previous PASS is never reused.

    The caller provides the new candidate graph and freshly prepared original
    material. This function binds a fresh request and executes the supplied ask
    adapter; the caller still owns any remote-run authorization. It does not
    decide clinical chronology or mutate the candidate graph.
    """
    from .chain_validation import bind_candidate_graph
    from .review import run_review_chain
    validate_episode_material_set(previous_set)
    previous = next((item for item in previous_set['checkpoint_materials']
                     if item['checkpoint_id'] == checkpoint_id), None)
    if previous is None:
        raise ContractError('CHECKPOINT_NOT_IN_MATERIAL_SET')
    graph = new_episode_reference.get('graph_snapshot')
    if (not isinstance(graph, dict) or new_episode_reference.get('input_sha256') != canonical_sha256(graph)
            or not isinstance(new_episode_reference.get('candidate_version'), str)):
        raise ContractError('EPISODE_VERIFIABLE_GRAPH_SNAPSHOT_REQUIRED')
    if (new_episode_reference['candidate_version'] == previous_set['episode_reference']['candidate_version']
            and new_episode_reference['input_sha256'] == previous_set['episode_reference']['input_sha256']):
        raise ContractError('GRAPH_RESTART_REQUIRES_NEW_CANDIDATE_VERSION')
    matching = [entry for entry in graph.get('episodes', [])
                if entry.get('episode_id') == new_episode_reference.get('episode_id')]
    if len(matching) != 1 or target_event_id not in matching[0].get('checkpoint_ids', []):
        raise ContractError('EPISODE_CHECKPOINT_MEMBERSHIP_MISMATCH')
    context = bind_candidate_graph(graph, graph_version=new_episode_reference['candidate_version'],
        target_event_id=target_event_id, checkpoint_id=compiled['target']['checkpoint_id'], task_context=task_context)
    return run_review_chain(manifest, compiled, ask, candidate_sources=candidate_sources,
        excluded_sources=excluded_sources, max_rounds=max_rounds, task_context=context)
