"""Run one graph version through gated material and actual delivery audits."""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_delivery_audit.audit import build_delivery_audit_request
from scripts.cohort_construction.p2_checkpoint_delivery_audit.chain import run_delivery_audit_chain, validate_delivery_audit_chain
from scripts.cohort_construction.p2_checkpoint_delivery_audit.ready import build_ready_resource_manifest
from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import material_item, prepare_candidate_materials
from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import validate_review_chain
from scripts.cohort_construction.p2_checkpoint_material_governance.episode import build_episode_material_set
from scripts.cohort_construction.p2_checkpoint_material_governance.review import run_review_chain
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import private_mkdir
from scripts.cohort_construction.p2_checkpoint_material_governance.run import _round_journal
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.audit import build_graph_audit_request
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.chain import run_graph_audit_chain, validate_graph_audit_chain

from .delivery import build_staging_deliveries
from .persistence import (PipelineBudget, RUN_FORMAT, implementation_snapshot,
    json_value, new_output, validate_contract, write, write_text)
from .replay import validate_pipeline_run


def _journal(output):
    return _round_journal(output)


def _checkpoint_dir(output, checkpoint_id):
    if not isinstance(checkpoint_id, str) or not checkpoint_id or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-' for c in checkpoint_id):
        raise ValueError('READY_PIPELINE_CHECKPOINT_OUTPUT_ID_INVALID')
    return output / 'materials' / checkpoint_id


def _targets(graph, episode_id):
    episode = next(item for item in graph['episodes'] if item['episode_id'] == episode_id)
    return list(episode['checkpoint_ids'])


def _stage(stages, stage, state, reason=None):
    item = dict(stage=stage, state=state, reason_code=reason)
    stages.append(item)
    return item


def run_episode_pipeline(*, output, graph, sources, graph_audit_sources=None, episode_id, semantic_audits, questionnaire,
                         graph_audit_context=None, decision_task_id, visibility_policy_version,
                         config, adapters=None, execute=False, max_rounds=3, max_model_calls=20,
                         inherited=None, initial_model_usage=None, continuation=None,
                         persist=True, semantic_audit_factory=None, revision_cycle=None,
                         graph_revision=None, delivery_gate=None, private_root=None):
    """Execute only requested stages; an absent adapter means offline preparation.

    All provider adapters are explicit library inputs. Registration of this
    function does not authorize a remote patient run or a doctor assignment.
    """
    from .persistence import freeze_contract
    contract = freeze_contract(graph=graph, sources=sources, graph_audit_sources=graph_audit_sources,
        episode_id=episode_id,
        semantic_audits=semantic_audits, questionnaire=questionnaire,
        graph_audit_context=graph_audit_context or {}, decision_task_id=decision_task_id,
        visibility_policy_version=visibility_policy_version, max_rounds=max_rounds,
        max_model_calls=max_model_calls, execute=execute, config=config,
        semantic_audit_factory=semantic_audit_factory, delivery_gate=delivery_gate)
    validate_contract(contract)
    adapters = adapters or {}
    if adapters and not execute:
        raise ValueError('READY_PIPELINE_ADAPTER_REQUIRES_EXPLICIT_EXECUTE')
    if set(adapters) - {'graph', 'material', 'delivery'}:
        raise ValueError('READY_PIPELINE_MODEL_STAGE_UNKNOWN')
    if execute and set(adapters) != {'graph', 'material', 'delivery'}:
        raise ValueError('READY_PIPELINE_COMPLETE_ADAPTER_SET_REQUIRED')
    output = new_output(output, private_root=private_root) if persist else None
    budget = PipelineBudget(max_model_calls, adapters, initial_model_usage)
    run = dict(format=RUN_FORMAT, input_contract=contract, episode_id=episode_id,
        expected_checkpoint_ids=_targets(graph, episode_id), stages=[],
        model_usage=budget.snapshot(), delivery_gate=deepcopy(contract['delivery_gate']),
        doctor_assignment_created=False, clinical_release_permitted=False)
    if persist:
        write(output / 'input-contract.json', contract)
        write(output / 'implementation-snapshot.json', implementation_snapshot())
    if graph_revision is not None:
        from .revision import consume_graph_revision, request_graph_revision
        if inherited is not None or revision_cycle is not None:
            raise ValueError('READY_PIPELINE_GRAPH_REVISION_REQUIRES_FRESH_PIPELINE')
        if not isinstance(graph_revision, dict) or set(graph_revision) - {'parent_run', 'origin', 'reconstruction_chain'} != set() \
                or not {'parent_run', 'origin'}.issubset(graph_revision):
            raise ValueError('READY_PIPELINE_GRAPH_REVISION_FIELDS_INVALID')
        parent = graph_revision['parent_run']
        if not isinstance(parent, dict):
            raise ValueError('READY_PIPELINE_GRAPH_REVISION_PARENT_REQUIRED')
        graph_revision_request = request_graph_revision(parent, origin=graph_revision['origin'])
        reconstruction_chain = graph_revision.get('reconstruction_chain')
        receipt = consume_graph_revision(graph_revision_request, graph,
            reconstruction_chain=reconstruction_chain)
        run['graph_revision_cycle'] = receipt
        if reconstruction_chain is not None:
            run['graph_reconstruction_chain'] = deepcopy(reconstruction_chain)
        run['superseded_parent_run'] = deepcopy(parent)
        if persist:
            write(output / 'graph-revision-cycle.json', receipt)
            write(output / 'superseded-parent-run.json', parent)
            if reconstruction_chain is not None:
                write(output / 'graph-reconstruction-chain.json', reconstruction_chain)
    if inherited:
        from .persistence import validate_inheritance
        inherited = validate_inheritance(inherited, contract)
        run['inheritance_receipt'] = inherited.pop('receipt')
    else:
        inherited = {}
    stages = run['stages']

    graph_chain = inherited.get('graph_audit_chain')
    audit_context = deepcopy(contract['graph_audit_context'])
    if graph_revision is not None and reconstruction_chain is not None:
        from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.lineage import build_graph_revision_lineage
        audit_context['reconstruction_lineage'] = build_graph_revision_lineage(
            parent['graph_audit_chain'], reconstruction_chain, graph)
    request = json_value(build_graph_audit_request(graph, contract['graph_audit_sources'],
        **audit_context))
    if persist:
        private_mkdir(output / 'graph', exist_ok=False)
        write(output / 'graph' / 'request.json', request)
    if graph_chain is None and execute:
        graph_chain = run_graph_audit_chain(request,
            lambda task, payload, schema: budget.ask_for('graph', task, payload, schema),
            max_rounds=max_rounds, journal=_journal(output / 'graph') if persist else None,
            compact_transport=config.get('graph_audit_compact_transport', False))
    if graph_chain is not None:
        validate_graph_audit_chain(graph_chain)
        if graph_chain['initial_request'] != request:
            raise ValueError('READY_PIPELINE_INHERITED_GRAPH_REQUEST_CHANGED')
        run['graph_audit_chain'] = graph_chain
        _stage(stages, 'GRAPH_AUDIT', graph_chain['state'], graph_chain['stop_reason'])
        if persist:
            write(output / 'graph' / 'chain.json', graph_chain)
    else:
        _stage(stages, 'GRAPH_AUDIT', 'PREPARED_NOT_EXECUTED')
        return _finish(run, output, budget, continuation, persist)
    if graph_chain['state'] != 'AUTO_GRAPH_AUDIT_PASS':
        return _finish(run, output, budget, continuation, persist)

    tasks, failures = prepare_candidate_materials(graph, sources,
        decision_task_id=decision_task_id, visibility_policy_version=visibility_policy_version)
    selected = [task for task in tasks if task['episode_id'] == episode_id]
    run['assembly_failures'] = [item for item in failures if item['episode_id'] == episode_id]
    if failures or {task['compiled']['target']['checkpoint_id'] for task in selected} != set(run['expected_checkpoint_ids']):
        _stage(stages, 'MATERIAL_PREPARATION', 'HOLD', 'INCOMPLETE_OR_FAILED_EPISODE_TARGETS')
        return _finish(run, output, budget, continuation, persist)
    _stage(stages, 'MATERIAL_PREPARATION', 'PASS')
    chains = dict(inherited.get('material_chains', {}))
    for task in selected:
        checkpoint_id = task['compiled']['target']['checkpoint_id']
        directory = _checkpoint_dir(output, checkpoint_id) if persist else None
        if persist:
            private_mkdir(directory, parents=True)
            write(directory / 'prepared-task.json', task)
        chain = chains.get(checkpoint_id)
        if chain is not None:
            validate_review_chain(chain, expected_graph_context=task['task_context'])
            chains[checkpoint_id] = chain
            if persist:
                write(directory / 'chain.json', chain)
                write(directory / 'replay.json', validate_review_chain(chain, expected_graph_context=task['task_context']))
            _stage(stages, 'MATERIAL:' + checkpoint_id, chain['state'], chain['stop_reason'])
            continue
        if chain is None and execute:
            before = budget.snapshot()
            journal = _journal(directory) if persist else None
            try:
                chain = run_review_chain(task['manifest'], task['compiled'],
                    lambda *args: budget.ask_for('material', *args),
                    candidate_sources=task['candidate_sources'], excluded_sources=task['excluded_sources'],
                    task_context=task['task_context'], max_rounds=max_rounds, journal=journal)
            except Exception as exc:
                _stage(stages, 'MATERIAL:' + checkpoint_id, 'ORCHESTRATION_HOLD', type(exc).__name__)
                if persist:
                    write(directory / 'orchestration-failure.json', dict(
                        reason_code=type(exc).__name__, detail=str(exc),
                        provider_response_not_automatically_validated=True))
                chains[checkpoint_id] = None
                continue
            finally:
                if persist:
                    write(directory / 'model-budget-receipt.json', dict(before=before, after=budget.snapshot()))
        if chain is None:
            _stage(stages, 'MATERIAL:' + checkpoint_id, 'PREPARED_NOT_EXECUTED')
            continue
        validate_review_chain(chain, expected_graph_context=task['task_context'])
        chains[checkpoint_id] = chain
        material_stage = dict(stage='MATERIAL:' + checkpoint_id, state=chain['state'],
            reason_code=chain['stop_reason'], journal_verified=True,
            journal_sha256=canonical_sha256(_persisted_journal(directory / 'rounds', chain)))
        if persist:
            write(directory / 'chain.json', chain)
            write(directory / 'replay.json', validate_review_chain(chain, expected_graph_context=task['task_context']))
            write(directory / 'journal-receipt.json', dict(verified=True,
                journal_sha256=material_stage['journal_sha256'], chain_sha256=canonical_sha256(chain)))
        stages.append(material_stage)
    run['material_chains'] = chains
    if any(chain is None for chain in chains.values()):
        return _finish(run, output, budget, continuation, persist)
    if set(chains) != set(run['expected_checkpoint_ids']):
        _stage(stages, 'EPISODE_MATERIAL_SET', 'HOLD', 'INCOMPLETE_CHECKPOINT_COVERAGE')
        return _finish(run, output, budget, continuation, persist)
    items = [material_item(task, chains[task['compiled']['target']['checkpoint_id']]) for task in selected]
    reference = dict(episode_id=episode_id, candidate_version=graph['graph_version'],
        input_sha256=canonical_sha256(graph), graph_snapshot=graph)
    material_set = build_episode_material_set(reference, items)
    run['episode_material_set'] = material_set
    if persist:
        write(output / 'episode-material-set.json', material_set)
    if any(item['state'] != 'AUTO_REVIEW_PASS' for item in items):
        _stage(stages, 'EPISODE_MATERIAL_SET', 'HOLD', 'CHECKPOINT_MATERIAL_NOT_PASS')
        return _finish(run, output, budget, continuation, persist)
    _stage(stages, 'EPISODE_MATERIAL_SET', 'AUTO_REVIEW_PASS')
    from .delivery_gate import delivery_gate_hold
    if delivery_gate_hold(run, output, persist):
        return _finish(run, output, budget, continuation, persist)

    deliveries = inherited.get('deliveries')
    run['resolved_semantic_audits'] = deepcopy(inherited.get('resolved_semantic_audits'))
    if deliveries is None:
        audits = semantic_audits
        if contract['semantic_audits'].get('mode') == 'AFTER_FINAL_MATERIAL_COMPILATION':
            audits = {item['compiled']['target']['checkpoint_id']: semantic_audit_factory(item)
                      for item in material_set['checkpoint_materials']}
        if inherited.get('deliveries_rebuildable') is False:
            audits = inherited['resolved_semantic_audits']
        run['resolved_semantic_audits'] = deepcopy(audits)
        deliveries = build_staging_deliveries(material_set, audits, questionnaire)
    if deliveries is not None and revision_cycle is None:
        if any(not isinstance(item, dict) for item in deliveries):
            raise ValueError('READY_PIPELINE_INHERITED_DELIVERIES_INVALID')
        audits = run.get('resolved_semantic_audits') or inherited.get('resolved_semantic_audits')
        if audits is None:
            audits = ({item['compiled']['target']['checkpoint_id']: semantic_audit_factory(item)
                       for item in material_set['checkpoint_materials']}
                      if contract['semantic_audits'].get('mode') == 'AFTER_FINAL_MATERIAL_COMPILATION'
                      else semantic_audits)
            run['resolved_semantic_audits'] = deepcopy(audits)
        expected = build_staging_deliveries(material_set, audits, questionnaire)
        if deliveries != expected:
            raise ValueError('READY_PIPELINE_INHERITED_DELIVERY_RECONSTRUCTION_MISMATCH')
    if revision_cycle is not None:
        if inherited.get('delivery_revision_cycle') is not None:
            raise ValueError('READY_PIPELINE_DELIVERY_REVISION_NOT_PENDING')
        deliveries, delivery_cycle = _consume_delivery_revision(
            revision_cycle, run, material_set, deliveries, questionnaire)
        run['delivery_revision_cycle'] = delivery_cycle
        run['superseded_parent_run'] = deepcopy(revision_cycle['parent_run'])
        if persist:
            write(output / 'delivery-revision-cycle.json', delivery_cycle)
            write(output / 'superseded-parent-run.json', revision_cycle['parent_run'])
    run['actual_deliveries'] = deliveries
    if persist:
        for delivery in deliveries:
            directory = output / 'deliveries' / delivery['checkpoint_id']
            private_mkdir(directory, parents=True)
            for name in ('packet', 'bundle'):
                write(directory / (name + '.json'), delivery[name])
            write_text(directory / 'reviewer.html', delivery['html'])
            write(directory / 'delivery.json', delivery)
    request = build_delivery_audit_request(material_set, deliveries)
    if persist:
        private_mkdir(output / 'delivery-audit', exist_ok=False)
        write(output / 'delivery-audit' / 'request.json', request)
    delivery_chain = inherited.get('delivery_audit_chain')
    if revision_cycle is not None:
        delivery_chain = None
    if delivery_chain is not None:
        validate_delivery_audit_chain(delivery_chain)
        if delivery_chain['initial_request'] != request or delivery_chain['state'] != 'AUTO_DELIVERY_AUDIT_PASS':
            raise ValueError('READY_PIPELINE_INHERITED_DELIVERY_REQUEST_CHANGED')
    if delivery_chain is None and execute:
        delivery_chain = run_delivery_audit_chain(request,
            lambda task, payload, schema: budget.ask_for('delivery', task, payload, schema),
            journal=_journal(output / 'delivery-audit') if persist else None)
    if delivery_chain is None:
        _stage(stages, 'DELIVERY_AUDIT', 'PREPARED_NOT_EXECUTED')
        return _finish(run, output, budget, continuation, persist)
    validate_delivery_audit_chain(delivery_chain)
    run['delivery_audit_chain'] = delivery_chain
    _stage(stages, 'DELIVERY_AUDIT', delivery_chain['state'], delivery_chain['stop_reason'])
    if persist:
        write(output / 'delivery-audit' / 'chain.json', delivery_chain)
    if delivery_chain['state'] != 'AUTO_DELIVERY_AUDIT_PASS':
        return _finish(run, output, budget, continuation, persist)
    ready = build_ready_resource_manifest(resource_id='READY-' + episode_id,
        graph_audit=graph_chain, material_set=material_set, delivery_audit=delivery_chain,
        staging_delivery=dict(doctor_assignment_created=False, clinical_release_permitted=False))
    if ready['resource_state'] != 'READY_FOR_HUMAN_SAMPLING':
        raise ValueError('READY_PIPELINE_AUTOMATIC_GATE_MISMATCH')
    run['ready_resource'] = ready
    _stage(stages, 'READY_RESOURCE', ready['resource_state'])
    if persist:
        write(output / 'ready-resource.json', ready)
    return _finish(run, output, budget, continuation, persist)


def _persisted_journal(directory, chain):
    """Reconstruct journal identity; empty execution failures have no round file."""
    from .persistence import read
    records = []
    for index in range(1, len(chain['rounds']) + 2):
        folder = directory / f'{index:04d}'
        if index <= len(chain['rounds']):
            request = read(folder / 'input.json')
            completed = read(folder / 'completed-round.json')
            records.append(dict(index=index, request=request, completed=completed))
        elif (folder / 'failure-hold.json').exists():
            records.append(dict(index=index, failure=read(folder / 'failure-hold.json')))
    return dict(chain_sha256=chain.get('chain_sha256'), records=records)


def _consume_delivery_revision(revision_cycle, run, material_set, deliveries, questionnaire):
    from .revision import consume_delivery_revision
    parent, new_deliveries = revision_cycle['parent_run'], revision_cycle['new_deliveries']
    receipt = consume_delivery_revision(parent, new_deliveries)
    if receipt['previous_material_set_sha256'] != material_set['set_sha256']:
        raise ValueError('READY_PIPELINE_DELIVERY_REVISION_MATERIAL_CHANGED')
    return new_deliveries, receipt


def _finish(run, output, budget, continuation, persist):
    run['model_usage'] = budget.snapshot()
    if continuation is not None:
        run['continuation'] = deepcopy(continuation)
    states = [item['state'] for item in run['stages']]
    if 'ready_resource' in run:
        run['state'] = 'READY_FOR_HUMAN_SAMPLING'
    elif any('REVISE' in state for state in states):
        run['state'] = 'REVISION_REQUIRED'
    elif any('HOLD' in state or 'REVISE' in state for state in states):
        run['state'] = 'HOLD'
    else:
        run['state'] = 'PREPARED_NOT_EXECUTED'
    run['run_sha256'] = canonical_sha256(run)
    if persist:
        from .persistence import write
        write(output / 'run.json', run)
        from .persistence import write as persist_receipt
        persist_receipt(output / 'replay-receipt.json', validate_pipeline_run(run, output=output))
    return run


def resume_episode_pipeline(*, previous_output, output, adapters, execute=True,
                            max_model_calls=None, revision_cycle=None,
                            semantic_audit_factory=None):
    """Replay inherited stages, execute missing stages, and never mutate the parent."""
    from pathlib import Path
    from .persistence import read
    previous_output = Path(previous_output)
    parent = read(previous_output / 'run.json')
    replayed = validate_pipeline_run(parent, output=previous_output)
    contract = parent['input_contract']
    if not replayed['inherited_stages_replayable']:
        raise ValueError('READY_PIPELINE_PARENT_NOT_INHERITABLE')
    if parent.get('ready_resource') is not None:
        raise ValueError('READY_PIPELINE_NO_UNFINISHED_STAGES')
    limit = contract['max_model_invocations'] if max_model_calls is None else max_model_calls
    if limit < parent['model_usage']['actual_uncached_model_invocations']:
        raise ValueError('READY_PIPELINE_RESUME_BUDGET_BELOW_PARENT_USAGE')
    inherited = dict(graph_audit_chain=parent.get('graph_audit_chain'),
        material_chains=parent.get('material_chains', {}),
        resolved_semantic_audits=parent.get('resolved_semantic_audits'),
        deliveries=parent.get('actual_deliveries'), delivery_audit_chain=parent.get('delivery_audit_chain'))
    if revision_cycle is not None:
        if parent.get('delivery_revision_cycle') is not None:
            raise ValueError('READY_PIPELINE_DELIVERY_REVISION_NOT_PENDING')
        revision_cycle = dict(parent_run=parent, **revision_cycle)
    elif parent.get('delivery_audit_chain') is not None \
            and parent['delivery_audit_chain']['state'] != 'AUTO_DELIVERY_AUDIT_PASS':
        raise ValueError('READY_PIPELINE_INHERITED_DELIVERY_REQUEST_CHANGED')
    continuation = dict(parent_run_sha256=parent['run_sha256'],
        inherited_stages_replayed_without_model_calls=True, clinical_or_human_status_inherited=False)
    return run_episode_pipeline(output=output, graph=contract['graph'], sources=contract['sources'],
        graph_audit_sources=contract['graph_audit_sources'], episode_id=contract['episode_id'], semantic_audits=contract['semantic_audits'],
        questionnaire=contract['questionnaire'], graph_audit_context=contract['graph_audit_context'],
        decision_task_id=contract['decision_task_id'], visibility_policy_version=contract['visibility_policy_version'],
        config=contract['config'], adapters=adapters, execute=execute,
        max_rounds=contract['max_rounds'], max_model_calls=limit, inherited=dict(inherited, parent_run=parent),
        initial_model_usage=parent['model_usage'], continuation=continuation,
        revision_cycle=revision_cycle, semantic_audit_factory=semantic_audit_factory,
        delivery_gate=contract.get('delivery_gate'))
