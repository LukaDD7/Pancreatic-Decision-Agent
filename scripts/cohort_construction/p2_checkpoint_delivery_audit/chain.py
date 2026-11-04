"""Independent delivery rounds; repair requests never mutate frozen artifacts."""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from .audit import DELIVERY_AUDIT_SCHEMA, TASK, build_delivery_audit_request, validate_delivery_audit

FORMAT = 'episode-delivery-audit-chain-0.1.0'


def _stop(response):
    if response['status'] == 'AUTO_DELIVERY_AUDIT_PASS':
        return 'AUTO_PASS'
    if response['status'] == 'AUTO_DELIVERY_AUDIT_HOLD' or response['required_actions']:
        return 'REVIEW_HOLD'
    return 'RENDERER_OR_MATERIAL_REVISION_REQUIRED'


def _state(stop):
    return {'AUTO_PASS': 'AUTO_DELIVERY_AUDIT_PASS',
            'RENDERER_OR_MATERIAL_REVISION_REQUIRED': 'AUTO_DELIVERY_AUDIT_REVISE'}.get(
                stop, 'AUTO_DELIVERY_AUDIT_HOLD')


def _replay_request(request):
    expected = build_delivery_audit_request(request['episode_material_set'],
        [dict(checkpoint_id=item['checkpoint_id'], assignment_id=item['assignment_id'],
              packet=item['packet'], bundle=item['bundle'], html=item['html'],
              **item['delivery_gate']) for item in request['actual_deliveries']],
        researcher_originals=request['researcher_originals'])
    if expected != request:
        raise ValueError('DELIVERY_AUDIT_CHAIN_INPUT_RECONSTRUCTION_MISMATCH')


def _journal(journal, stage, index, value):
    if journal is not None:
        journal(stage, index, deepcopy(value))


def run_delivery_audit_chain(request, ask, *, journal=None):
    """One frozen Episode/delivery version; a repair creates a new delivery request."""
    initial = deepcopy(request)
    _replay_request(initial)
    rounds, stop, failure = [], None, None
    index = 1
    journal_input = dict(request=initial, request_sha256=canonical_sha256(initial),
        episode_material_set_sha256=initial['response_binding']['episode_material_set_sha256'],
        delivery_input_sha256=initial['response_binding']['delivery_input_sha256'])
    _journal(journal, 'REQUEST', index, journal_input)
    response, receipt, stage = None, {}, 'PROVIDER_REQUEST'
    try:
        answer = ask(TASK, deepcopy(initial), deepcopy(DELIVERY_AUDIT_SCHEMA))
        response, receipt = answer if isinstance(answer, tuple) else (answer, {})
        stage = 'RESPONSE_VALIDATION'
        validated = validate_delivery_audit(initial, response)
        stop = _stop(validated)
    except Exception as exc:
        failure = dict(stage=stage, error_type=type(exc).__name__, reason_code=type(exc).__name__,
            round_index=index, request=initial, request_sha256=canonical_sha256(initial),
            receipt=deepcopy(getattr(exc, 'receipt', receipt)))
        if response is not None:
            failure['response'] = deepcopy(response)
        _journal(journal, 'FAILURE', index, failure)
        stop = 'EXECUTION_FAILED'
    if response is not None and failure is None:
        record = dict(round_index=index, **journal_input, response=deepcopy(validated),
                      receipt=deepcopy(receipt), stop_reason=stop)
        rounds.append(record)
        _journal(journal, 'ROUND', index, record)
    result = dict(format=FORMAT, protocol_version=initial['protocol_version'],
        initial_request=initial, episode_material_set_sha256=journal_input['episode_material_set_sha256'],
        delivery_input_sha256=journal_input['delivery_input_sha256'], state=_state(stop),
        stop_reason=stop, rounds=rounds, delivery_artifacts_modified_by_audit=False,
        new_delivery_version_required=stop == 'RENDERER_OR_MATERIAL_REVISION_REQUIRED',
        old_delivery_pass_transferable=False, human_sample_status='NOT_ASSESSED',
        doctor_assignment_created=False, clinical_release_permitted=False)
    if failure:
        result['failure'] = failure
    result['chain_sha256'] = canonical_sha256(result)
    return result


def validate_delivery_audit_chain(chain):
    """Model-free replay of actual FHIR/HTML inputs, response and stopping rule."""
    if not isinstance(chain, dict) or chain.get('format') != FORMAT:
        raise ValueError('DELIVERY_AUDIT_CHAIN_FORMAT_INVALID')
    if canonical_sha256({key: value for key, value in chain.items() if key != 'chain_sha256'}) != chain.get('chain_sha256'):
        raise ValueError('DELIVERY_AUDIT_CHAIN_HASH_MISMATCH')
    initial = chain.get('initial_request')
    if not isinstance(initial, dict) or not isinstance(chain.get('rounds'), list):
        raise ValueError('DELIVERY_AUDIT_CHAIN_INITIAL_INPUT_INVALID')
    binding = initial.get('response_binding', {})
    if any(chain.get(key) != binding.get(key) for key in ('episode_material_set_sha256', 'delivery_input_sha256')):
        raise ValueError('DELIVERY_AUDIT_CHAIN_BINDING_MISMATCH')
    failure = chain.get('failure')
    if not chain['rounds']:
        if (chain.get('stop_reason') != 'EXECUTION_FAILED' or chain.get('state') != 'AUTO_DELIVERY_AUDIT_HOLD'
                or not isinstance(failure, dict) or failure.get('request') != initial
                or failure.get('request_sha256') != canonical_sha256(initial) or failure.get('round_index') != 1):
            raise ValueError('DELIVERY_AUDIT_CHAIN_EMPTY_OR_FAILURE_INVALID')
    else:
        if len(chain['rounds']) != 1 or failure is not None:
            raise ValueError('DELIVERY_AUDIT_CHAIN_ROUND_COUNT_INVALID')
        record = chain['rounds'][0]
        if (record.get('round_index') != 1 or record.get('request') != initial
                or record.get('request_sha256') != canonical_sha256(initial)
                or any(record.get(key) != binding.get(key) for key in ('episode_material_set_sha256', 'delivery_input_sha256'))
                or not isinstance(record.get('receipt'), dict)):
            raise ValueError('DELIVERY_AUDIT_CHAIN_ROUND_BINDING_MISMATCH')
        _replay_request(initial)
        response = validate_delivery_audit(initial, record.get('response'))
        stop = _stop(response)
        if record.get('stop_reason') != stop or chain.get('stop_reason') != stop or chain.get('state') != _state(stop):
            raise ValueError('DELIVERY_AUDIT_CHAIN_STOP_MISMATCH')
    if (chain.get('new_delivery_version_required') is not (chain['stop_reason'] == 'RENDERER_OR_MATERIAL_REVISION_REQUIRED')
            or chain.get('delivery_artifacts_modified_by_audit') is not False
            or chain.get('old_delivery_pass_transferable') is not False
            or chain.get('human_sample_status') != 'NOT_ASSESSED'
            or chain.get('doctor_assignment_created') is not False
            or chain.get('clinical_release_permitted') is not False):
        raise ValueError('DELIVERY_AUDIT_CHAIN_FINAL_STATE_MISMATCH')
    return dict(format='episode-delivery-audit-chain-replay-0.1.0', status='PASS',
        chain_sha256=chain['chain_sha256'], state=chain['state'], stop_reason=chain['stop_reason'],
        completed_rounds=len(chain['rounds']), actual_fhir_html_verified=True,
        model_call_performed=False, clinical_semantics_certified=False, clinical_release_permitted=False)
