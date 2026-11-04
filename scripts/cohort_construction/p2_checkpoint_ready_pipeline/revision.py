"""Versioned revision requests; auditors do not mutate graphs or deliveries."""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256


def _hash(value):
    return canonical_sha256(value)


def _validated_revision(run, revision_kind):
    from .persistence import RUN_FORMAT, self_hash
    if not isinstance(run, dict) or run.get('format') != RUN_FORMAT \
            or not self_hash(run, 'run_sha256'):
        raise ValueError('READY_PIPELINE_REVISION_PARENT_INVALID')
    result = dict(format='checkpoint-ready-pipeline-revision-request-0.1.0',
        revision_kind=revision_kind, parent_run_sha256=run['run_sha256'],
        clinical_release_permitted=False)
    return run, result


def request_graph_revision(run, *, origin):
    _, base = _validated_revision(run, 'INDEPENDENT_GRAPH_RECONSTRUCTION_REQUIRED')
    if origin in ('graph_audit', 'graph_review_hold'):
        chain = run.get('graph_audit_chain', {})
        if origin == 'graph_audit':
            if chain.get('state') != 'AUTO_GRAPH_AUDIT_REVISE':
                raise ValueError('READY_PIPELINE_GRAPH_REVISION_NOT_REQUESTED')
            handoff = deepcopy(chain['handoff'])
        else:
            if (chain.get('state') != 'AUTO_GRAPH_AUDIT_HOLD'
                    or chain.get('stop_reason') != 'REVIEW_HOLD' or not chain.get('rounds')):
                raise ValueError('READY_PIPELINE_GRAPH_REVIEW_HOLD_REQUIRED')
            handoff = deepcopy(chain['rounds'][-1]['handoff'])
            if handoff.get('audit_status') not in ('AUTO_GRAPH_AUDIT_HOLD', 'AUTO_GRAPH_AUDIT_REVISE') \
                    or not handoff.get('required_actions'):
                raise ValueError('READY_PIPELINE_GRAPH_REVIEW_HOLD_HANDOFF_INVALID')
    elif origin == 'material_review':
        from scripts.cohort_construction.p2_checkpoint_material_governance.episode import graph_revision_handoff
        handoffs = []
        for item in run['episode_material_set']['checkpoint_materials']:
            for round_ in item['auto_review_chain']['rounds']:
                if round_['ledger'].get('graph_issues'):
                    handoffs.append(graph_revision_handoff(run['episode_material_set'],
                        item['compiled']['target']['checkpoint_id'],
                        round_['review']['review_id'],
                        'Material review flagged an event graph issue'))
        if not handoffs:
            raise ValueError('READY_PIPELINE_MATERIAL_GRAPH_REVISION_NOT_REQUESTED')
        handoff = handoffs
    else:
        raise ValueError('READY_PIPELINE_REVISION_ORIGIN_INVALID')
    result = dict(base, origin=origin,
        parent_run_sha256=run['run_sha256'], input_graph_sha256=run['input_contract']['graph']['graph_sha256'],
        handoff=handoff, input_graph_version=run['input_contract']['graph']['graph_version'],
        new_graph_required=True, new_graph_hash_must_change=True,
        all_material_and_delivery_passes_nontransferable=True, new_run_required=True,
        hold_preserved=origin == 'graph_review_hold',
        human_or_independent_reconstruction_required=origin == 'graph_review_hold',
        automatic_reconstruction_performed=False, clinical_release_permitted=False)
    result['revision_request_sha256'] = canonical_sha256(result)
    return result


def request_delivery_revision(run):
    _, base = _validated_revision(run, 'NEW_STAGING_DELIVERY_VERSION_REQUIRED')
    chain = run.get('delivery_audit_chain', {})
    if chain.get('state') != 'AUTO_DELIVERY_AUDIT_REVISE':
        raise ValueError('READY_PIPELINE_DELIVERY_REVISION_NOT_REQUESTED')
    result = dict(base, origin='delivery_audit',
        parent_run_sha256=run['run_sha256'], material_set_sha256=run['episode_material_set']['set_sha256'],
        delivery_input_sha256=chain['delivery_input_sha256'],
        response=deepcopy(chain['rounds'][-1]['response']),
        new_delivery_required=True, audit_may_modify_delivery=False,
        material_or_graph_revision_requires_new_run=True,
        automatic_delivery_repair_performed=False, clinical_release_permitted=False)
    result['revision_request_sha256'] = canonical_sha256(result)
    return result


def recover_graph_revision_request(run, expected_sha256):
    """Recover a historical request from bound parent evidence, not receipt schema."""
    candidates = []
    for origin in ('graph_audit', 'graph_review_hold', 'material_review'):
        try:
            request = request_graph_revision(run, origin=origin)
        except (ValueError, KeyError):
            continue
        if request['revision_request_sha256'] == expected_sha256:
            candidates.append(request)
    if len(candidates) != 1:
        raise ValueError('READY_PIPELINE_GRAPH_REVISION_REQUEST_REQUIRED')
    return candidates[0]


def consume_graph_revision(request, new_graph, *, reconstruction_chain=None):
    """Bind an independently rebuilt graph; no previous audit result is inherited.

    A supplied graph alone is not evidence that automatic reconstruction ran.
    The automatic path must provide its complete, separately replayable chain.
    """
    if not isinstance(request, dict) or request.get('format') != 'checkpoint-ready-pipeline-revision-request-0.1.0' \
            or request.get('revision_kind') != 'INDEPENDENT_GRAPH_RECONSTRUCTION_REQUIRED' \
            or request.get('revision_request_sha256') != _hash({k: v for k, v in request.items()
                                                              if k != 'revision_request_sha256'}):
        raise ValueError('READY_PIPELINE_GRAPH_REVISION_REQUEST_INVALID')
    if not isinstance(new_graph, dict) or new_graph.get('graph_version') is None \
            or new_graph.get('graph_sha256') != _hash({k: v for k, v in new_graph.items()
                                                      if k != 'graph_sha256'}) \
            or new_graph['graph_sha256'] == request['input_graph_sha256'] \
            or new_graph['graph_version'] == request.get('input_graph_version'):
        raise ValueError('READY_PIPELINE_GRAPH_REVISION_REQUIRES_NEW_VERSION_AND_HASH')
    if reconstruction_chain is None:
        automatic = False
        replay_sha256 = None
    else:
        from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
            validate_graph_reconstruction_chain,
        )
        replay = validate_graph_reconstruction_chain(reconstruction_chain)
        if (replay['input_graph_sha256'] != request['input_graph_sha256']
                or replay['output_graph_sha256'] != new_graph['graph_sha256']
                or reconstruction_chain['request']['input_graph']['graph_sha256'] != request['input_graph_sha256']
                or reconstruction_chain['request']['revision_requests'] != request['handoff']['revision_requests']
                or reconstruction_chain['request']['required_actions'] != request['handoff']['required_actions']):
            raise ValueError('READY_PIPELINE_GRAPH_RECONSTRUCTION_BINDING_MISMATCH')
        automatic = True
        replay_sha256 = replay['chain_sha256']
    result = dict(format='checkpoint-ready-pipeline-graph-revision-receipt-0.2.0',
        revision_request_sha256=request['revision_request_sha256'],
        parent_run_sha256=request['parent_run_sha256'], input_graph_sha256=request['input_graph_sha256'],
        new_graph_sha256=new_graph['graph_sha256'], new_graph_version=new_graph['graph_version'],
        independent_reconstruction_supplied=True, final_graph_dependency_audit_required=True,
        old_material_and_delivery_passes_inherited=False, new_pipeline_run_required=True,
        automatic_reconstruction_performed=automatic,
        reconstruction_chain_sha256=replay_sha256,
        independent_reconstruction_receipt_required=automatic, clinical_release_permitted=False)
    result['revision_receipt_sha256'] = _hash(result)
    return result


def consume_delivery_revision(parent_run, new_deliveries):
    """Require fresh actual artifacts from downstream, then demand a fresh audit."""
    from scripts.cohort_construction.p2_checkpoint_delivery_audit.audit import build_delivery_audit_request
    from .persistence import json_value
    parent_run, request = _validated_revision(parent_run, 'NEW_STAGING_DELIVERY_VERSION_REQUIRED')
    chain = parent_run['delivery_audit_chain']
    if chain.get('state') != 'AUTO_DELIVERY_AUDIT_REVISE':
        raise ValueError('READY_PIPELINE_DELIVERY_REVISION_NOT_REQUESTED')
    material_set = parent_run['episode_material_set']
    audit_request = build_delivery_audit_request(material_set, json_value(new_deliveries))
    previous_ids = {item['checkpoint_id'] for item in chain['initial_request']['actual_deliveries']}
    if {item['checkpoint_id'] for item in audit_request['actual_deliveries']} != previous_ids:
        raise ValueError('READY_PIPELINE_DELIVERY_REVISION_TARGET_SET_CHANGED')
    if audit_request['response_binding']['delivery_input_sha256'] == chain['delivery_input_sha256']:
        raise ValueError('READY_PIPELINE_DELIVERY_REVISION_HASH_CHANGED_REQUIRED')
    result = dict(format='checkpoint-ready-pipeline-delivery-revision-receipt-0.1.0',
        parent_run_sha256=parent_run['run_sha256'],
        previous_delivery_audit_chain_sha256=chain['chain_sha256'],
        previous_delivery_input_sha256=chain['delivery_input_sha256'],
        previous_material_set_sha256=material_set['set_sha256'],
        new_delivery_input_sha256=audit_request['response_binding']['delivery_input_sha256'],
        new_delivery_version_supplied_by_downstream=True,
        audit_modified_delivery=False, previous_delivery_pass_inherited=False,
        new_delivery_audit_required=True, clinical_release_permitted=False)
    result['revision_receipt_sha256'] = _hash(result)
    return result
