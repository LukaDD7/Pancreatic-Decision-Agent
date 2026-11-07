"""Authoritative, model-free reconstruction of one persisted pipeline run."""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_delivery_audit.audit import build_delivery_audit_request
from scripts.cohort_construction.p2_checkpoint_delivery_audit.chain import validate_delivery_audit_chain
from scripts.cohort_construction.p2_checkpoint_delivery_audit.ready import build_ready_resource_manifest
from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import material_item, prepare_candidate_materials
from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import validate_review_chain
from scripts.cohort_construction.p2_checkpoint_material_governance.episode import build_episode_material_set
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.audit import build_graph_audit_request
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.chain import validate_graph_audit_chain

from .delivery import build_staging_deliveries
from .delivery_gate import delivery_gate_hold, freeze_delivery_gate
from .persistence import RUN_FORMAT, implementation_snapshot, json_value, read, self_hash, validate_contract


def _stage_receipt(run, name, state):
    matches = [item for item in run['stages'] if item['stage'] == name]
    if len(matches) != 1 or matches[0]['state'] != state:
        raise ValueError('READY_PIPELINE_STAGE_RECEIPT_MISMATCH:' + name)
    return matches[0]


def validate_pipeline_run(run, *, output=None, verify_implementation=True):
    """Recreate completed stages; optionally bind to the current source snapshot.

    Historical replay keeps the saved implementation hash as provenance, while
    verifying clinical chains and artifact bytes without relabeling old source
    as current. Same-version execution replay retains the strict default.
    """
    if not isinstance(run, dict) or run.get('format') != RUN_FORMAT or not self_hash(run, 'run_sha256'):
        raise ValueError('READY_PIPELINE_RUN_FORMAT_OR_HASH_INVALID')
    contract = validate_contract(run.get('input_contract'))
    if run.get('continuation') is not None:
        if not isinstance(run.get('inheritance_receipt'), dict) \
                or not self_hash(run['inheritance_receipt'], 'replay_sha256') \
                or run['continuation'].get('parent_run_sha256') != run['inheritance_receipt'].get('run_sha256') \
                or run['inheritance_receipt'].get('replay_verified') is not True:
            raise ValueError('READY_PIPELINE_CONTINUATION_BINDING_INVALID')
    elif run.get('inheritance_receipt') is not None:
        raise ValueError('READY_PIPELINE_UNEXPECTED_INHERITANCE_RECEIPT')
    if run.get('delivery_revision_cycle') is not None and run.get('continuation') is None:
        raise ValueError('READY_PIPELINE_DELIVERY_REVISION_CONTINUATION_REQUIRED')
    if run.get('graph_revision_cycle') is not None and run.get('continuation') is not None:
        raise ValueError('READY_PIPELINE_GRAPH_REVISION_REQUIRES_FRESH_PIPELINE')
    if run.get('episode_id') != contract['episode_id'] or run.get('doctor_assignment_created') is not False \
            or run.get('clinical_release_permitted') is not False:
        raise ValueError('READY_PIPELINE_RUN_GOVERNANCE_MISMATCH')
    if run.get('delivery_gate') != freeze_delivery_gate(contract.get('delivery_gate')):
        raise ValueError('READY_PIPELINE_DELIVERY_GATE_BINDING_MISMATCH')
    graph, sources = contract['graph'], contract['sources']
    graph_audit_sources = contract.get('graph_audit_sources', sources)
    if 'graph_audit_sources' not in contract and verify_implementation:
        raise ValueError('READY_PIPELINE_HISTORICAL_SINGLE_SCOPE_REQUIRES_EXPLICIT_REPLAY')
    episode = next(item for item in graph['episodes'] if item['episode_id'] == contract['episode_id'])
    if run.get('expected_checkpoint_ids') != episode['checkpoint_ids']:
        raise ValueError('READY_PIPELINE_EXPECTED_TARGETS_CHANGED')
    _usage(run.get('model_usage'), contract)
    stages, stage_records = _stages(run)
    episode_tasks, _ = prepare_candidate_materials(graph, sources,
        decision_task_id=contract['decision_task_id'],
        visibility_policy_version=contract['visibility_policy_version'])
    episode_tasks = [task for task in episode_tasks if task['episode_id'] == contract['episode_id']]
    _model_accounting(run, stages, stage_records, episode_tasks)
    if output is not None:
        if read(output / 'input-contract.json') != contract:
            raise ValueError('READY_PIPELINE_SAVED_CONTRACT_CHANGED')
        if verify_implementation:
            _saved(output, 'implementation-snapshot.json', implementation_snapshot())
        elif not isinstance(read(output / 'implementation-snapshot.json').get('implementation_sha256'), str):
            raise ValueError('READY_PIPELINE_SAVED_IMPLEMENTATION_SNAPSHOT_INVALID')
        if (output / 'replay-receipt.json').is_file():
            saved_receipt = read(output / 'replay-receipt.json')
            if not self_hash(saved_receipt, 'replay_sha256'):
                raise ValueError('READY_PIPELINE_SAVED_REPLAY_RECEIPT_CHANGED')

    if run.get('graph_revision_cycle') is not None:
        from .revision import consume_graph_revision
        saved = run['graph_revision_cycle']
        parent = run.get('superseded_parent_run')
        if not isinstance(parent, dict) or parent.get('run_sha256') != saved['parent_run_sha256']:
            raise ValueError('READY_PIPELINE_GRAPH_REVISION_PARENT_REQUIRED')
        from .revision import recover_graph_revision_request
        request = recover_graph_revision_request(parent, saved['revision_request_sha256'])
        reconstruction_chain = run.get('graph_reconstruction_chain')
        if consume_graph_revision(request, graph,
                reconstruction_chain=reconstruction_chain) != saved:
            raise ValueError('READY_PIPELINE_GRAPH_REVISION_REPLAY_MISMATCH')
        if output is not None:
            _saved(output, 'graph-revision-cycle.json', saved)
            _saved(output, 'superseded-parent-run.json', parent)
            if reconstruction_chain is not None:
                _saved(output, 'graph-reconstruction-chain.json', reconstruction_chain)
    audit_context = deepcopy(contract['graph_audit_context'])
    if run.get('graph_revision_cycle') is not None and reconstruction_chain is not None:
        from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.lineage import build_graph_revision_lineage
        audit_context['reconstruction_lineage'] = build_graph_revision_lineage(
            parent['graph_audit_chain'], reconstruction_chain, graph)
    chain = run.get('graph_audit_chain')
    if chain is not None:
        audit_context['protocol_version'] = chain['initial_request']['protocol_version']
        if chain['initial_request']['protocol_version'] != 'checkpoint-candidate-reconstruction-audit-0.2.0':
            # Historical runs have no lineage field; replay cannot upgrade them.
            audit_context.pop('reconstruction_lineage', None)
    request = json_value(build_graph_audit_request(graph, graph_audit_sources,
        **audit_context))
    if chain is None:
        _expect_terminal(run, stages, [('GRAPH_AUDIT', 'PREPARED_NOT_EXECUTED')], 'PREPARED_NOT_EXECUTED')
        return _receipt(run, [])
    replay = validate_graph_audit_chain(chain)
    if chain['initial_request'] != request or stages.get('GRAPH_AUDIT') != chain['state']:
        raise ValueError('READY_PIPELINE_GRAPH_STAGE_MISMATCH')
    if output is not None:
        _saved(output, 'graph/chain.json', chain)
        _saved(output, 'graph/request.json', request)
    else:
        _stage_receipt(run, 'GRAPH_AUDIT', chain['state'])
    if chain['state'] != 'AUTO_GRAPH_AUDIT_PASS':
        _expect_terminal(run, stages, [('GRAPH_AUDIT', chain['state'])], _stop(chain['state']))
        return _receipt(run, ['GRAPH_AUDIT'])
    inherited = ['GRAPH_AUDIT']

    tasks, failures = prepare_candidate_materials(graph, sources,
        decision_task_id=contract['decision_task_id'], visibility_policy_version=contract['visibility_policy_version'])
    tasks = [task for task in tasks if task['episode_id'] == contract['episode_id']]
    relevant_failures = [item for item in failures if item['episode_id'] == contract['episode_id']]
    if run.get('assembly_failures') != relevant_failures:
        raise ValueError('READY_PIPELINE_ASSEMBLY_FAILURES_CHANGED')
    if failures or {task['compiled']['target']['checkpoint_id'] for task in tasks} != set(episode['checkpoint_ids']):
        _expect_terminal(run, stages, [('GRAPH_AUDIT', chain['state']),
            ('MATERIAL_PREPARATION', 'HOLD')], 'HOLD')
        return _receipt(run, inherited)
    inherited.append('MATERIAL_PREPARATION')
    chains = {}
    material_set = run.get('episode_material_set')
    for task in tasks:
        checkpoint_id = task['compiled']['target']['checkpoint_id']
        stage = 'MATERIAL:' + checkpoint_id
        if stage not in stages:
            raise ValueError('READY_PIPELINE_MATERIAL_STAGE_MISSING')
        material_chain = run.get('material_chains', {}).get(checkpoint_id)
        if material_chain is None and material_set is not None:
            matches = [item for item in material_set['checkpoint_materials'] if item['checkpoint_id'] == checkpoint_id]
            if len(matches) == 1:
                material_chain = matches[0]['auto_review_chain']
        if material_chain is None:
            if stages[stage] != 'PREPARED_NOT_EXECUTED':
                # A failed provider has no completed chain. Persisted execution
                # failure is recorded by the journal, not fabricated as a review.
                if stages[stage] not in ('ORCHESTRATION_HOLD',):
                    raise ValueError('READY_PIPELINE_UNBACKED_MATERIAL_STATE')
            continue
        validate_review_chain(material_chain, expected_graph_context=task['task_context'])
        chains[checkpoint_id] = material_chain
        if stages[stage] != material_chain['state']:
            raise ValueError('READY_PIPELINE_MATERIAL_STAGE_STATE_MISMATCH')
        if output is not None:
            _saved(output, 'materials/' + checkpoint_id + '/chain.json', material_chain)
            _saved(output, 'materials/' + checkpoint_id + '/prepared-task.json', task)
        else:
            _stage_receipt(run, stage, material_chain['state'])
    if material_set is not None:
        material_ids = {item['checkpoint_id'] for item in material_set['checkpoint_materials']}
        if set(chains) != material_ids:
            raise ValueError('READY_PIPELINE_MATERIAL_CHAIN_IDENTITY_MISMATCH')
    if material_set is None and stages.get('EPISODE_MATERIAL_SET') == 'HOLD' \
            and len(chains) == len(tasks):
        if run.get('state') != 'HOLD' or run.get('actual_deliveries') is not None:
            raise ValueError('READY_PIPELINE_INCOMPLETE_COVERAGE_GATE_MISMATCH')
        return _receipt(run, inherited)
    if material_set is None and any(state == 'ORCHESTRATION_HOLD' for key, state in stages.items()
                                    if key.startswith('MATERIAL:')):
        if run.get('state') != 'HOLD':
            raise ValueError('READY_PIPELINE_EXECUTION_FAILURE_NOT_HOLD')
        return _receipt(run, inherited)
    if len(chains) != len(tasks):
        if material_set is not None or run.get('actual_deliveries') is not None \
                or run.get('delivery_audit_chain') is not None or run.get('ready_resource') is not None:
            raise ValueError('READY_PIPELINE_INCOMPLETE_MATERIALS_CANNOT_DELIVER')
        return _receipt(run, inherited + list(chains))

    if material_set is None:
        raise ValueError('READY_PIPELINE_INCOMPLETE_COVERAGE_GATE_MISMATCH')
    reference = dict(episode_id=contract['episode_id'], candidate_version=graph['graph_version'],
        input_sha256=canonical_sha256(graph), graph_snapshot=graph)
    expected_set = build_episode_material_set(reference,
        [material_item(task, chains[task['compiled']['target']['checkpoint_id']]) for task in tasks])
    if material_set != expected_set:
        raise ValueError('READY_PIPELINE_MATERIAL_SET_RECONSTRUCTION_MISMATCH')
    if output is not None:
        _saved(output, 'episode-material-set.json', material_set)
    else:
        if len({item['state'] for item in material_set['checkpoint_materials']}) != 1 \
                or material_set['checkpoint_materials'][0]['state'] != 'AUTO_REVIEW_PASS':
            _stage_receipt(run, 'EPISODE_MATERIAL_SET', 'HOLD')
        else:
            _stage_receipt(run, 'EPISODE_MATERIAL_SET', 'AUTO_REVIEW_PASS')
    inherited.append('EPISODE_MATERIAL_SET')
    if run.get('research_feedback_draft') is not None:
        from scripts.cohort_construction.p2_checkpoint_material_governance.feedback import (
            build_research_feedback_draft, render_research_feedback_html,
            validate_research_feedback_draft,
        )
        if contract['config'].get('research_feedback_enabled') is not True:
            raise ValueError('READY_PIPELINE_RESEARCH_FEEDBACK_NOT_AUTHORIZED')
        expected_draft = build_research_feedback_draft(material_set,
            source_limitations=contract['config'].get('research_feedback_limitations', []))
        if run['research_feedback_draft'] != expected_draft:
            raise ValueError('READY_PIPELINE_RESEARCH_FEEDBACK_RECONSTRUCTION_MISMATCH')
        validate_research_feedback_draft(expected_draft)
        if run.get('research_feedback_html') != render_research_feedback_html(expected_draft):
            raise ValueError('READY_PIPELINE_RESEARCH_FEEDBACK_HTML_RECONSTRUCTION_MISMATCH')
        _stage_receipt(run, 'RESEARCH_FEEDBACK_DRAFT', 'RESEARCH_FEEDBACK_DRAFT_READY')
        if output is not None:
            _saved(output, 'research-feedback-draft.json', expected_draft)
            if (output / 'research-feedback.html').read_text(encoding='utf-8') != run['research_feedback_html']:
                raise ValueError('READY_PIPELINE_RESEARCH_FEEDBACK_HTML_CHANGED')
    elif contract['config'].get('research_feedback_enabled') is True:
        raise ValueError('READY_PIPELINE_RESEARCH_FEEDBACK_MISSING')
    elif 'RESEARCH_FEEDBACK_DRAFT' in stages or run.get('research_feedback_html') is not None:
        raise ValueError('READY_PIPELINE_UNEXPECTED_RESEARCH_FEEDBACK')
    if any(item['state'] != 'AUTO_REVIEW_PASS' for item in material_set['checkpoint_materials']):
        if run.get('actual_deliveries') is not None or run.get('ready_resource') is not None:
            raise ValueError('READY_PIPELINE_MATERIAL_HOLD_CANNOT_DELIVER')
        _expect_feedback_or_formal_hold(run, stages, 'HOLD')
        return _receipt(run, inherited, material_set_rebuildable=True)
    if stages.get('EPISODE_MATERIAL_SET') != 'AUTO_REVIEW_PASS':
        raise ValueError('READY_PIPELINE_MATERIAL_SET_GATE_MISMATCH')
    if run.get('delivery_gate') is not None:
        gate = _stage_receipt(run, 'DELIVERY_GATE', 'HOLD')
        if gate.get('reason_code') != ';'.join(run['delivery_gate']['reason_codes']):
            raise ValueError('READY_PIPELINE_DELIVERY_GATE_REASON_MISMATCH')
        if output is not None:
            _saved(output, 'delivery-gate.json', run['delivery_gate'])
        if (run.get('actual_deliveries') is not None or run.get('delivery_audit_chain') is not None
                or run.get('ready_resource') is not None or run.get('resolved_semantic_audits') is not None
                or run.get('state') not in ('HOLD', 'RESEARCH_FEEDBACK_DRAFT_READY')):
            raise ValueError('READY_PIPELINE_DELIVERY_GATE_BYPASSED')
        _expect_feedback_or_formal_hold(run, stages, 'HOLD')
        return _receipt(run, inherited, material_set_rebuildable=True)

    deliveries = run.get('actual_deliveries')
    if deliveries is None:
        raise ValueError('READY_PIPELINE_PASSED_MATERIALS_REQUIRE_STAGING_DELIVERIES')
    cycle = run.get('delivery_revision_cycle')
    if cycle is not None:
        from .revision import consume_delivery_revision
        if consume_delivery_revision(run.get('superseded_parent_run'), deliveries) != cycle:
            raise ValueError('READY_PIPELINE_DELIVERY_REVISION_REPLAY_MISMATCH')
        if output is not None:
            _saved(output, 'delivery-revision-cycle.json', cycle)
            _saved(output, 'superseded-parent-run.json', run['superseded_parent_run'])
    expected_deliveries = None
    if cycle is None:
        if contract['semantic_audits'].get('mode') == 'AFTER_FINAL_MATERIAL_COMPILATION':
            if run.get('resolved_semantic_audits') is None:
                raise ValueError('READY_PIPELINE_RESOLVED_SEMANTIC_AUDITS_REQUIRED')
            expected_deliveries = build_staging_deliveries(material_set,
                run['resolved_semantic_audits'], contract['questionnaire'])
        else:
            if run.get('resolved_semantic_audits') is not None:
                raise ValueError('READY_PIPELINE_UNEXPECTED_RESOLVED_AUDITS')
            expected_deliveries = build_staging_deliveries(material_set,
                contract['semantic_audits'], contract['questionnaire'])
        if deliveries != expected_deliveries:
            raise ValueError('READY_PIPELINE_STAGING_DELIVERY_RECONSTRUCTION_MISMATCH')
    request = build_delivery_audit_request(material_set, deliveries)
    if output is not None:
        for delivery in deliveries:
            directory = 'deliveries/' + delivery['checkpoint_id'] + '/'
            _saved(output, directory + 'packet.json', delivery['packet'])
            _saved(output, directory + 'bundle.json', delivery['bundle'])
            _saved(output, directory + 'delivery.json', delivery)
            if (output / (directory + 'reviewer.html')).read_text(encoding='utf-8') != delivery['html']:
                raise ValueError('READY_PIPELINE_SAVED_HTML_CHANGED')
        _saved(output, 'delivery-audit/request.json', request)
    delivery_chain = run.get('delivery_audit_chain')
    if delivery_chain is None:
        if stages.get('DELIVERY_AUDIT') != 'PREPARED_NOT_EXECUTED' or run.get('ready_resource') is not None \
                or run.get('state') != 'PREPARED_NOT_EXECUTED':
            raise ValueError('READY_PIPELINE_UNEXECUTED_DELIVERY_STATE_INVALID')
        return _receipt(run, inherited, deliveries_rebuildable=True)
    validate_delivery_audit_chain(delivery_chain)
    if delivery_chain['initial_request'] != request or stages.get('DELIVERY_AUDIT') != delivery_chain['state']:
        raise ValueError('READY_PIPELINE_DELIVERY_STAGE_MISMATCH')
    if output is not None:
        _saved(output, 'delivery-audit/chain.json', delivery_chain)
    else:
        _stage_receipt(run, 'DELIVERY_AUDIT', delivery_chain['state'])
    inherited.append('DELIVERY_AUDIT')
    if delivery_chain['state'] != 'AUTO_DELIVERY_AUDIT_PASS':
        if run.get('ready_resource') is not None:
            raise ValueError('READY_PIPELINE_FAILED_DELIVERY_CANNOT_READY')
        return _receipt(run, inherited)
    expected_ready = build_ready_resource_manifest(resource_id='READY-' + contract['episode_id'],
        graph_audit=chain, material_set=material_set, delivery_audit=delivery_chain,
        staging_delivery=dict(doctor_assignment_created=False, clinical_release_permitted=False))
    if run.get('ready_resource') != expected_ready or run.get('state') != 'READY_FOR_HUMAN_SAMPLING':
        raise ValueError('READY_PIPELINE_READY_RESOURCE_RECONSTRUCTION_MISMATCH')
    if stages.get('READY_RESOURCE') != 'READY_FOR_HUMAN_SAMPLING':
        raise ValueError('READY_PIPELINE_READY_STAGE_MISMATCH')
    if output is not None:
        _saved(output, 'ready-resource.json', expected_ready)
    else:
        _stage_receipt(run, 'READY_RESOURCE', 'READY_FOR_HUMAN_SAMPLING')
    return _receipt(run, inherited + ['READY_RESOURCE'], deliveries_rebuildable=True)


def _saved(output, path, value):
    if read(output / path) != value:
        raise ValueError('READY_PIPELINE_SAVED_ARTIFACT_CHANGED:' + path)


def _stages(run):
    if not isinstance(run.get('stages'), list):
        raise ValueError('READY_PIPELINE_STAGES_REQUIRED')
    states, records = {}, {}
    for item in run['stages']:
        if not isinstance(item, dict) or not isinstance(item.get('stage'), str) or item['stage'] in states:
            raise ValueError('READY_PIPELINE_STAGE_IDENTITY_INVALID')
        states[item['stage']] = item.get('state')
        records[item['stage']] = item
    return states, records


def _stop(state):
    if 'REVISE' in state:
        return 'REVISION_REQUIRED'
    return 'HOLD'


def _expect_terminal(run, stages, prefix, state):
    if list(stages) != [item[0] for item in prefix] or any(stages[key] != value for key, value in prefix) \
            or run.get('state') != state:
        raise ValueError('READY_PIPELINE_TERMINAL_STAGE_MISMATCH')


def _expect_feedback_or_formal_hold(run, stages, terminal_state):
    if run.get('research_feedback_draft') is not None:
        if run.get('state') != 'RESEARCH_FEEDBACK_DRAFT_READY':
            raise ValueError('READY_PIPELINE_RESEARCH_FEEDBACK_TERMINAL_STATE_INVALID')
        return
    if run.get('state') != terminal_state:
        raise ValueError('READY_PIPELINE_FORMAL_HOLD_STATE_INVALID')


def _model_accounting(run, stages, stage_records, tasks=()):
    """Reconcile recorded usage to the exact chains and chainless stage deltas."""
    from .model_accounting import chain_usage, usage_sum, validate_usage, zero_usage
    for stage, item in stage_records.items():
        if not isinstance(item.get('reason_code'), (str, type(None))) \
                or item.get('execution_origin') not in ('EXECUTED', 'INHERITED') \
                or (item['execution_origin'] == 'EXECUTED') != item.get('model_accounted', False) \
                or item.get('parent_run_sha256') != (run.get('continuation') or {}).get('parent_run_sha256'):
            raise ValueError('READY_PIPELINE_STAGE_RECEIPT_INVALID')
    expected = {'GRAPH_AUDIT', 'RESEARCH_FEEDBACK_DRAFT', 'READY_RESOURCE'}
    if stages.get('GRAPH_AUDIT') == 'AUTO_GRAPH_AUDIT_PASS':
        expected.add('MATERIAL_PREPARATION')
        if stages.get('MATERIAL_PREPARATION') == 'PASS':
            expected |= {'MATERIAL:' + task['compiled']['target']['checkpoint_id'] for task in tasks}
            if all(stage_records.get('MATERIAL:' + task['compiled']['target']['checkpoint_id'], {}).get('state')
                   != 'ORCHESTRATION_HOLD' for task in tasks):
                expected.add('EPISODE_MATERIAL_SET')
                if stages.get('EPISODE_MATERIAL_SET') == 'AUTO_REVIEW_PASS':
                    expected |= {'DELIVERY_GATE', 'DELIVERY_AUDIT'}
    if set(stages) - expected:
        raise ValueError('READY_PIPELINE_STAGE_IDENTITY_UNKNOWN')
    usage = zero_usage(run['model_usage']['max_model_invocations'])
    for stage, item in stage_records.items():
        if item['execution_origin'] == 'INHERITED':
            if item.get('model_usage_base') != run.get('model_usage_base'):
                raise ValueError('READY_PIPELINE_INHERITED_STAGE_BASE_MISMATCH')
            continue
        if stage == 'GRAPH_AUDIT' and run.get('graph_audit_chain') is not None:
            chain_usage(run['graph_audit_chain'], 'graph', usage)
        elif stage.startswith('MATERIAL:'):
            checkpoint_id = stage[len('MATERIAL:'):]
            chain = (run.get('material_chains') or {}).get(checkpoint_id)
            if chain is None and run.get('episode_material_set') is not None:
                matches = [item['auto_review_chain'] for item in run['episode_material_set']['checkpoint_materials']
                    if item['checkpoint_id'] == checkpoint_id]
                chain = matches[0] if len(matches) == 1 else None
            if chain is not None:
                chain_usage(chain, 'material', usage)
            elif item.get('state') == 'ORCHESTRATION_HOLD':
                # A chainless handled failure must carry its exact before/after
                # delta. Inferring zero or one from its name would be unsound.
                delta = item.get('model_usage_delta')
                if not isinstance(delta, dict):
                    raise ValueError('READY_PIPELINE_ORCHESTRATION_ATTEMPT_EVIDENCE_REQUIRED')
                usage = usage_sum(usage, validate_usage(delta, usage['max_model_invocations']),
                    usage['max_model_invocations'])
        elif stage == 'DELIVERY_AUDIT' and run.get('delivery_audit_chain') is not None:
            chain_usage(run['delivery_audit_chain'], 'delivery', usage)
        elif item.get('model_accounted'):
            raise ValueError('READY_PIPELINE_UNBACKED_MODEL_ATTEMPT')
    if any(item.get('execution_origin') == 'EXECUTED' and item.get('state') == 'PREPARED_NOT_EXECUTED'
           for item in stage_records.values()):
        raise ValueError('READY_PIPELINE_PREPARED_STAGE_EXECUTION_CONFLICT')
    if run.get('graph_reconstruction_chain') is not None:
        if run.get('model_usage_base', {}).get('binding', {}).get('kind') != 'GRAPH_RECONSTRUCTION':
            raise ValueError('READY_PIPELINE_RECONSTRUCTION_BASE_REQUIRED')
        # Reconstruction precedes the fresh graph; do not count it again as a
        # fresh-graph/material/delivery call.
        from .model_accounting import validate_graph_reconstruction_usage
        reconstruction = zero_usage(run['model_usage']['max_model_invocations'])
        validate_graph_reconstruction_usage(run['graph_reconstruction_chain'], reconstruction,
            run['model_usage']['max_model_invocations'])
        if reconstruction != run['model_usage_base']['usage']:
            raise ValueError('READY_PIPELINE_RECONSTRUCTION_BASE_USAGE_MISMATCH')
    base = run.get('model_usage_base', {}).get('usage')
    if base is None:
        base = zero_usage(run['model_usage']['max_model_invocations'])
    expected_usage = usage_sum(validate_usage(base, run['model_usage']['max_model_invocations']),
        validate_usage(usage, run['model_usage']['max_model_invocations']),
        run['model_usage']['max_model_invocations'])
    if expected_usage != run['model_usage']:
        raise ValueError('READY_PIPELINE_STAGE_MODEL_ACCOUNTING_MISMATCH')


def _usage(usage, contract):
    if not isinstance(usage, dict):
        raise ValueError('READY_PIPELINE_MODEL_USAGE_REQUIRED')
    for key in ('uncached_succeeded', 'uncached_failed', 'cache_hits', 'denied'):
        if type(usage.get(key)) is not int or usage[key] < 0:
            raise ValueError('READY_PIPELINE_MODEL_USAGE_INVALID')
    total = usage['uncached_succeeded'] + usage['uncached_failed']
    if (usage.get('actual_uncached_model_invocations') != total
            or usage.get('max_model_invocations') != contract['max_model_invocations']
            or total > contract['max_model_invocations']
            or not isinstance(usage.get('stage_call_counts'), dict)):
        raise ValueError('READY_PIPELINE_MODEL_BUDGET_MISMATCH')
    if 'stage_usage' in usage:
        from .model_accounting import validate_usage
        validate_usage(usage, contract['max_model_invocations'])


def _receipt(run, inherited, *, material_set_rebuildable=False, deliveries_rebuildable=False):
    result = dict(format='checkpoint-ready-pipeline-replay-0.1.0',
        run_sha256=run['run_sha256'], state=run['state'], replay_verified=True,
        model_call_performed=False, inherited_stages_replayable=bool(inherited),
        replayed_stages=inherited, material_set_rebuildable=material_set_rebuildable,
        deliveries_rebuildable=deliveries_rebuildable,
        human_sampling_completed=False, doctor_assignment_created=False,
        clinical_release_permitted=False)
    result['replay_sha256'] = canonical_sha256(result)
    return result
