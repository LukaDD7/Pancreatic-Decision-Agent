"""Bounded graph-audit rounds and deterministic, model-free replay.

The graph auditor proposes revision requests. It cannot edit the graph or turn a
revision request into an audit PASS. Reconstruction is an explicit downstream
operation whose new graph starts a new request, not another round of the old one.
"""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from .audit import GRAPH_AUDIT_SCHEMA, TASK, build_graph_audit_request, reconstruction_handoff, validate_graph_audit
from .projection import COMPACT_FORMAT, compact_graph_audit_request, expand_graph_audit_request

FORMAT = 'candidate-reconstruction-audit-chain-0.1.0'
_HANDOFF_KEYS = ('revision_requests', 'required_actions', 'fresh_graph_material_and_delivery_reaudit_required',
                 'old_material_pass_transferable', 'clinical_release_permitted')


def _reconstruction_input(request):
    """Freeze the complete declared input scope without the response envelope."""
    return dict(discovery_proposals=deepcopy(request.get('discovery_llm_proposals', [])),
        review_proposals=deepcopy(request.get('review_llm_proposals', [])),
        feedback_history=deepcopy(request.get('feedback_history', [])),
        processing_units=deepcopy(request.get('processing_units')),
        unit_inventory=deepcopy(request.get('unit_inventory')),
        source_route_receipts=deepcopy(request.get('source_route_receipts', [])),
        final_graph_audit=deepcopy(request.get('final_graph_audit')),
        additional_excluded_units=deepcopy(request.get('additional_excluded_units', [])))


def _handoff(handoff):
    return dict(audit_status=handoff['audit_status'], input_graph_sha256=handoff['input_graph_sha256'],
                **{key: deepcopy(handoff[key]) for key in _HANDOFF_KEYS})


def _canonical_replay(request, response):
    expected = build_graph_audit_request(request['candidate_graph'],
        request['audit_scope']['complete_originals_supplied'], **_reconstruction_input(request))
    if expected != request:
        raise ValueError('GRAPH_AUDIT_CHAIN_INPUT_RECONSTRUCTION_MISMATCH')
    validated = validate_graph_audit(expected, response)
    return _handoff(reconstruction_handoff(expected, validated))


def _journal(journal, stage, index, value):
    if journal is not None:
        journal(stage, index, deepcopy(value))


def run_graph_audit_chain(request, ask, *, max_rounds=3, journal=None, compact_transport=False):
    """Run independent reviews against one frozen graph; retain bounded history.

    Compact transport is a reversible JSON projection, not a reduced clinical
    source scope. Persisted requests always contain the original full graph.
    """
    if type(max_rounds) is not int or not 1 <= max_rounds <= 20:
        raise ValueError('GRAPH_AUDIT_MAX_ROUNDS_OUT_OF_RANGE')
    if type(compact_transport) is not bool:
        raise ValueError('GRAPH_AUDIT_COMPACT_TRANSPORT_FLAG_INVALID')
    initial = deepcopy(request)
    if build_graph_audit_request(request['candidate_graph'],
            request['audit_scope']['complete_originals_supplied'], **_reconstruction_input(request)) != request:
        raise ValueError('GRAPH_AUDIT_CHAIN_INPUT_RECONSTRUCTION_MISMATCH')
    rounds, stop, failure, handoff = [], None, None, None
    for index in range(1, max_rounds + 1):
        current = deepcopy(initial)
        journal_input = dict(request=current, request_sha256=canonical_sha256(current),
                             graph_sha256=current['response_binding']['graph_sha256'])
        _journal(journal, 'REQUEST', index, journal_input)
        response, receipt, stage = None, {}, 'PROVIDER_REQUEST'
        try:
            provider_payload = compact_graph_audit_request(current) if compact_transport else deepcopy(current)
            answer = ask(TASK, provider_payload, deepcopy(GRAPH_AUDIT_SCHEMA))
            response, receipt = answer if isinstance(answer, tuple) else (answer, {})
            stage = 'RESPONSE_VALIDATION'
            expected_handoff = _canonical_replay(current, response)
        except Exception as exc:
            failure = dict(stage=stage, error_type=type(exc).__name__, reason_code=type(exc).__name__,
                round_index=index, request=current, request_sha256=canonical_sha256(current),
                receipt=deepcopy(getattr(exc, 'receipt', receipt)))
            if response is not None:
                failure['response'] = deepcopy(response)
            _journal(journal, 'FAILURE', index, failure)
            stop = 'EXECUTION_FAILED'
            break
        record = dict(round_index=index, **journal_input, response=deepcopy(response), receipt=deepcopy(receipt),
                      handoff=expected_handoff)
        rounds.append(record)
        _journal(journal, 'ROUND', index, record)
        status = response['status']
        if status == 'AUTO_GRAPH_AUDIT_PASS':
            stop = 'AUTO_PASS'
            break
        if status == 'AUTO_GRAPH_AUDIT_HOLD':
            stop = 'REVIEW_HOLD'
            break
        if any(item.get('request_kind') in ('HUMAN_REVIEW', 'SOURCE_COVERAGE') for item in response['revision_requests']):
            stop = 'RECONSTRUCTION_INPUT_UNRESOLVED'
            break
        handoff = expected_handoff
        stop = 'RECONSTRUCTION_REQUIRED'
        break
    result = dict(format=FORMAT, protocol_version=initial['protocol_version'],
        graph_sha256=initial['response_binding']['graph_sha256'], initial_request=initial,
        state='AUTO_GRAPH_AUDIT_PASS' if stop == 'AUTO_PASS' else (
            'AUTO_GRAPH_AUDIT_REVISE' if stop == 'RECONSTRUCTION_REQUIRED' else 'AUTO_GRAPH_AUDIT_HOLD'),
        stop_reason=stop, max_rounds=max_rounds, rounds=rounds,
        handoff=handoff, graph_modified_by_audit=False,
        fresh_graph_material_and_delivery_reaudit_required=stop != 'AUTO_PASS',
        old_material_pass_transferable=False, human_sample_status='NOT_ASSESSED',
        clinical_release_permitted=False)
    if failure:
        result['failure'] = failure
    result['chain_sha256'] = canonical_sha256(result)
    return result


def validate_graph_audit_chain(chain):
    """Authoritative no-model replay; a tail hash cannot hide a changed history."""
    if not isinstance(chain, dict) or chain.get('format') != FORMAT:
        raise ValueError('GRAPH_AUDIT_CHAIN_FORMAT_INVALID')
    if canonical_sha256({key: value for key, value in chain.items() if key != 'chain_sha256'}) != chain.get('chain_sha256'):
        raise ValueError('GRAPH_AUDIT_CHAIN_HASH_MISMATCH')
    initial = chain.get('initial_request')
    if not isinstance(initial, dict) or chain.get('graph_sha256') != initial.get('response_binding', {}).get('graph_sha256'):
        raise ValueError('GRAPH_AUDIT_CHAIN_INITIAL_INPUT_INVALID')
    if not isinstance(chain.get('rounds'), list):
        raise ValueError('GRAPH_AUDIT_CHAIN_ROUNDS_REQUIRED')
    if not chain['rounds']:
        failure = chain.get('failure')
        if (chain.get('state') != 'AUTO_GRAPH_AUDIT_HOLD' or chain.get('stop_reason') != 'EXECUTION_FAILED'
                or not isinstance(failure, dict) or failure.get('stage') not in ('PROVIDER_REQUEST', 'RESPONSE_VALIDATION')):
            raise ValueError('GRAPH_AUDIT_CHAIN_EMPTY_ROUNDS_NOT_PASS')
        failed_request = failure.get('request')
        if (failed_request != initial or canonical_sha256(failed_request) != failure.get('request_sha256')
                or failure.get('round_index') != 1 or chain.get('handoff') is not None
                or chain.get('graph_modified_by_audit') is not False
                or chain.get('clinical_release_permitted') is not False):
            raise ValueError('GRAPH_AUDIT_CHAIN_FAILURE_BINDING_MISMATCH')
        return dict(format='candidate-reconstruction-audit-chain-replay-0.1.0', status='PASS',
            chain_sha256=chain['chain_sha256'], state=chain['state'], stop_reason=chain['stop_reason'],
            completed_rounds=0, model_call_performed=False,
            clinical_semantics_certified=False, clinical_release_permitted=False)
    expected_stop = None
    for position, record in enumerate(chain['rounds'], 1):
        if record.get('round_index') != position:
            raise ValueError('GRAPH_AUDIT_CHAIN_ROUND_INDEX_MISMATCH')
        request = record.get('request')
        if not isinstance(request, dict):
            raise ValueError('GRAPH_AUDIT_CHAIN_ROUND_REQUEST_REQUIRED')
        if request != initial or canonical_sha256(request) != record.get('request_sha256'):
            raise ValueError('GRAPH_AUDIT_CHAIN_REQUEST_MISMATCH')
        if record.get('graph_sha256') != chain['graph_sha256']:
            raise ValueError('GRAPH_AUDIT_CHAIN_GRAPH_MISMATCH')
        if not isinstance(record.get('receipt'), dict):
            raise ValueError('GRAPH_AUDIT_CHAIN_RECEIPT_REQUIRED')
        handoff = _canonical_replay(request, record.get('response'))
        if handoff != record.get('handoff'):
            raise ValueError('GRAPH_AUDIT_CHAIN_HANDOFF_MISMATCH')
        response = record['response']
        if response['status'] == 'AUTO_GRAPH_AUDIT_PASS':
            expected_stop = 'AUTO_PASS'
        elif response['status'] == 'AUTO_GRAPH_AUDIT_HOLD':
            expected_stop = 'REVIEW_HOLD'
        elif any(item.get('request_kind') in ('HUMAN_REVIEW', 'SOURCE_COVERAGE') for item in response['revision_requests']):
            expected_stop = 'RECONSTRUCTION_INPUT_UNRESOLVED'
        else:
            expected_stop = 'RECONSTRUCTION_REQUIRED'
        break
    if len(chain['rounds']) != position or expected_stop != chain.get('stop_reason'):
        raise ValueError('GRAPH_AUDIT_CHAIN_STOP_MISMATCH')
    state = 'AUTO_GRAPH_AUDIT_PASS' if expected_stop == 'AUTO_PASS' else (
        'AUTO_GRAPH_AUDIT_REVISE' if expected_stop == 'RECONSTRUCTION_REQUIRED' else 'AUTO_GRAPH_AUDIT_HOLD')
    expected_handoff = _handoff(reconstruction_handoff(initial, chain['rounds'][-1]['response'])) \
        if expected_stop == 'RECONSTRUCTION_REQUIRED' else None
    if (chain.get('state') != state or chain.get('handoff') != expected_handoff
            or chain.get('fresh_graph_material_and_delivery_reaudit_required') is not (state != 'AUTO_GRAPH_AUDIT_PASS')
            or chain.get('old_material_pass_transferable') is not False
            or chain.get('graph_modified_by_audit') is not False
            or chain.get('human_sample_status') != 'NOT_ASSESSED'
            or chain.get('clinical_release_permitted') is not False):
        raise ValueError('GRAPH_AUDIT_CHAIN_FINAL_STATE_MISMATCH')
    failure = chain.get('failure')
    if expected_stop == 'EXECUTION_FAILED':
        if not isinstance(failure, dict) or failure.get('stage') not in ('PROVIDER_REQUEST', 'RESPONSE_VALIDATION'):
            raise ValueError('GRAPH_AUDIT_CHAIN_FAILURE_REQUIRED')
        failed_request = failure.get('request')
        if failed_request != initial or canonical_sha256(failed_request) != failure.get('request_sha256'):
            raise ValueError('GRAPH_AUDIT_CHAIN_FAILURE_BINDING_MISMATCH')
        if failure.get('round_index') != len(chain['rounds']) + 1:
            raise ValueError('GRAPH_AUDIT_CHAIN_FAILURE_ROUND_MISMATCH')
    elif failure is not None:
        raise ValueError('GRAPH_AUDIT_CHAIN_UNEXPECTED_FAILURE')
    return dict(format='candidate-reconstruction-audit-chain-replay-0.1.0', status='PASS',
        chain_sha256=chain['chain_sha256'], state=state, stop_reason=expected_stop,
        completed_rounds=len(chain['rounds']), model_call_performed=False,
        clinical_semantics_certified=False, clinical_release_permitted=False)
