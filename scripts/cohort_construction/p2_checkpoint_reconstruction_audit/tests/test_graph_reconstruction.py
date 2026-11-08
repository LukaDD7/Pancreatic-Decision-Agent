"""Synthetic counterexamples for separate, bounded graph reconstruction."""
from copy import deepcopy

import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import fictional_graph
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.audit import build_graph_audit_request
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
    build_graph_reconstruction_request, run_graph_reconstruction, validate_graph_reconstruction,
    validate_graph_reconstruction_chain,
)


def fixtures():
    graph, sources = fictional_graph()
    request = build_graph_audit_request(graph, sources,
        processing_units=[dict(unit_id='ALL', source_ids=[s['source_id'] for s in sources])])
    answer = passing_graph_response(request)
    answer.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
        request_id='REV-01', request_kind='EVENT_RECONSTRUCTION', reason='虚构重建请求',
        affected_event_ids=['EVENT-01'], affected_episode_ids=['EPISODE-01'],
        evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
        required_reconstruction=True)])
    return graph, request, answer


def proposed(request):
    graph = deepcopy(request['input_graph'])
    graph.pop('graph_sha256')
    graph['graph_version'] = 'SYNTHETIC-GRAPH-V2'
    return dict(format='candidate-graph-reconstruction-response-0.2.0',
        reconstruction_id='SYNTHETIC-RECONSTRUCTION', **request['response_binding'],
        status='RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT',
        graph=dict(events=graph['events'], relations=graph['relations'], episodes=graph['episodes'],
            graph_version=graph['graph_version']), rationale='虚构独立重建')


def provider_receipt():
    return dict(invocation_id='SYNTHETIC-INVOCATION', response_sha256='a' * 64,
        cached=False, status='SUCCESS', attempts=[dict(attempt=1)])


def test_CE_GRA007_separate_reconstruction_revalidates_support_and_requires_new_audit():
    old, audit_request, audit_response = fixtures()
    request = build_graph_reconstruction_request(audit_request, audit_response)
    answer = proposed(request)
    # Simulate a model copying an invalid upstream quotation as current support.
    event = answer['graph']['events'][0]
    event['action_citations']['valid'] = [dict(source_id='DOC-01', quote='非原文动作描述')]
    with pytest.raises(ValueError, match='ACTION_CITATION_NOT_VERIFIABLE'):
        validate_graph_reconstruction(request, answer)
    answer = proposed(request)
    chain = run_graph_reconstruction(audit_request, audit_response,
        lambda *args: (answer, provider_receipt()))
    assert chain['state'] == 'RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT'
    graph = chain['graph']
    assert graph['graph_sha256'] != old['graph_sha256']
    assert graph['events'][0]['action_citations']['invalid'] == []
    assert chain['validation_receipt']['fresh_independent_graph_audit_required']
    assert not chain['validation_receipt']['clinical_release_permitted']
    replay = validate_graph_reconstruction_chain(chain)
    assert replay['replay_verified'] and replay['model_call_performed'] is False
    tampered = deepcopy(chain)
    tampered['response']['rationale'] = '篡改重建响应'
    with pytest.raises(ValueError, match='CHAIN_FORMAT_OR_HASH_INVALID'):
        validate_graph_reconstruction_chain(tampered)
    unchanged = proposed(request)
    unchanged['graph']['graph_version'] = old['graph_version']
    with pytest.raises(ValueError, match='NEW_GRAPH_VERSION'):
        validate_graph_reconstruction(request, unchanged)


def test_CE_GRA009_placeholder_graphs_are_rejected_by_transport_schema():
    from jsonschema import Draft202012Validator, ValidationError
    _, audit_request, audit_response = fixtures()
    request = build_graph_reconstruction_request(audit_request, audit_response)
    for placeholder in ({'、': ['、']}, {'、': None}, {'graph_version': 'V2'},
                        dict(events=[], relations=[], episodes=[], graph_version='V2', extra=True)):
        response = proposed(request)
        response['graph'] = placeholder
        with pytest.raises(ValidationError):
            Draft202012Validator(__import__(
                'scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction',
                fromlist=['GRAPH_RECONSTRUCTION_SCHEMA']).GRAPH_RECONSTRUCTION_SCHEMA).validate(response)
        with pytest.raises(ValueError, match='RESPONSE_SCHEMA_INVALID'):
            validate_graph_reconstruction(request, response)


def test_CE_GRA009_explicit_model_hold_does_not_fabricate_a_graph():
    _, audit_request, audit_response = fixtures()
    request = build_graph_reconstruction_request(audit_request, audit_response)
    response = proposed(request)
    response.update(status='RECONSTRUCTION_HOLD', graph=None,
        rationale='虚构：现有原文不足以形成可核验完整新图')
    graph, receipt = validate_graph_reconstruction(request, response)
    assert graph is None and receipt['graph_reconstructed'] is False
    chain = run_graph_reconstruction(audit_request, audit_response,
        lambda *args: (response, provider_receipt()))
    assert chain['state'] == 'RECONSTRUCTION_HOLD' and chain['graph'] is None
    replay = validate_graph_reconstruction_chain(chain)
    assert replay['state'] == 'RECONSTRUCTION_HOLD' and replay['replay_verified']


def test_CE_GRA010_failed_response_is_preserved_replayed_and_tamper_rejected():
    _, audit_request, audit_response = fixtures()
    response = dict(format='candidate-graph-reconstruction-response-0.2.0',
        reconstruction_id='SYNTHETIC-FAILED', **build_graph_reconstruction_request(
            audit_request, audit_response)['response_binding'],
        status='RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT', graph={'、': ['、']},
        rationale='虚构：占位图必须拒绝')
    chain = run_graph_reconstruction(audit_request, audit_response,
        lambda *args: (response, provider_receipt()))
    assert chain['failure']['stage'] == 'RESPONSE_VALIDATION'
    assert chain['response'] == response and chain['response_evidence_preserved']
    replay = validate_graph_reconstruction_chain(chain)
    assert replay['failure_replayed'] if 'failure_replayed' in replay else replay['replay_verified']
    tampered = deepcopy(chain)
    tampered['response']['graph'] = {'changed': True}
    with pytest.raises(ValueError, match='CHAIN_FORMAT_OR_HASH_INVALID'):
        validate_graph_reconstruction_chain(tampered)


def test_CE_GRA010_provider_failure_has_no_fabricated_response_and_success_still_replays():
    _, audit_request, audit_response = fixtures()
    def fail(*args):
        error = RuntimeError('synthetic provider failure')
        error.receipt = dict(invocation_id='SYNTHETIC-PROVIDER-FAIL', response_sha256='b' * 64)
        raise error
    chain = run_graph_reconstruction(audit_request, audit_response, fail)
    assert chain['failure']['stage'] == 'PROVIDER_REQUEST'
    assert 'response' not in chain and 'provider_receipt' not in chain
    assert validate_graph_reconstruction_chain(chain)['failure_replayed']
    request = build_graph_reconstruction_request(audit_request, audit_response)
    success = run_graph_reconstruction(audit_request, audit_response,
        lambda *args: (proposed(request), provider_receipt()))
    assert validate_graph_reconstruction_chain(success)['output_graph_sha256']



def test_CE_GRA010_real_client_schema_exception_preserves_private_response_and_binding(tmp_path):
    """Use production Client with mocked HTTP, not a mock returning an invalid graph."""
    import json
    from unittest.mock import Mock, patch
    from scripts.cohort_construction.model_api import ModelAPISettings
    from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import GRAPH_RECONSTRUCTION_SCHEMA
    _, audit_request, audit_response = fixtures()
    request = build_graph_reconstruction_request(audit_request, audit_response)
    response = dict(format='candidate-graph-reconstruction-response-0.2.0',
        reconstruction_id='SYNTHETIC-TRANSPORT-FAIL', **request['response_binding'],
        status='RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT', graph={'、': ['、']},
        rationale='虚构：真实Client transport Schema拒绝')
    envelope = dict(choices=[dict(finish_reason='stop',
        message=dict(content=json.dumps(response, ensure_ascii=False)))])
    http = Mock(); http.json.return_value = envelope
    session = Mock(); session.post.return_value = http
    settings = ModelAPISettings('credential-sentinel', 'https://example.invalid/v1', 'synthetic-model')
    config = dict(model_temperature=0, max_output_tokens=64, max_http_attempts_per_request=1)
    with patch.object(transport.requests, 'Session', return_value=session):
        # Isolate this production-Client test from sessions mocked by earlier tests.
        transport._thread.session = session
        provider = transport.Client(tmp_path / 'cache', config, settings)
        chain = run_graph_reconstruction(audit_request, audit_response,
            lambda task, payload, schema: provider.ask(task, payload, schema))
    assert chain['failure']['stage'] == 'RESPONSE_SCHEMA_VALIDATION'
    assert chain['response'] == response
    assert chain['provider_receipt']['invocation_id']
    assert chain['provider_receipt']['response_sha256'] == __import__(
        'scripts.cohort_construction.p2_auto_checkpoint.extract', fromlist=['digest']
    ).digest(envelope)
    assert chain['provider_response_binding']['transport_response_schema_rejected']
    assert chain['response_evidence_preserved']
    replay = validate_graph_reconstruction_chain(chain)
    assert replay['failure_replayed']
    saved = next((tmp_path / 'cache/invocations').glob('*.json')).read_text()
    assert 'credential-sentinel' not in saved and 'TRANSPORT-FAIL' not in saved
    tampered = deepcopy(chain)
    tampered['request']['input_graph']['graph_version'] = 'TAMPERED-GRAPH'
    tampered['chain_sha256'] = canonical_sha256({key: value for key, value in tampered.items()
        if key != 'chain_sha256'})
    with pytest.raises(ValueError, match='CHAIN_REQUEST_INVALID|PARENT_REQUEST_BINDING_MISMATCH'):
        validate_graph_reconstruction_chain(tampered,
            expected_request=(audit_request, audit_response),
            expected_provider=dict(invocation_id=chain['provider_receipt']['invocation_id'],
                payload_sha256=canonical_sha256(chain['request']),
                transport_request_sha256=chain['failure']['request_sha256'],
                response_sha256=chain['provider_receipt']['response_sha256']))
    assert chain['failure']['reason_code'] == 'RESPONSE_SCHEMA_VALIDATION_FAILED'
    forged = deepcopy(chain)
    forged['failure']['reason_code'] = 'GRAPH_RECONSTRUCTION_RESPONSE_SCHEMA_INVALID'
    forged['chain_sha256'] = canonical_sha256({key: value for key, value in forged.items()
        if key != 'chain_sha256'})
    with pytest.raises(ValueError, match='PROVIDER_CHAIN_EVIDENCE_STAGE_OR_REASON_MISMATCH'):
        validate_graph_reconstruction_chain(forged)
    if hasattr(transport._thread, 'session'):
        del transport._thread.session


def test_CE_GRA010_provider_failure_revalidates_request_before_replay():
    _, audit_request, audit_response = fixtures()
    def fail(*args):
        error = RuntimeError('synthetic provider failure')
        error.receipt = dict(invocation_id='SYNTHETIC-PROVIDER-FAIL')
        raise error
    chain = run_graph_reconstruction(audit_request, audit_response, fail)
    expected = dict(invocation_id='SYNTHETIC-PROVIDER-FAIL',
        payload_sha256=canonical_sha256(chain['request']))
    assert validate_graph_reconstruction_chain(chain,
        expected_request=(audit_request, audit_response), expected_provider=expected)['failure_replayed']
    tampered = deepcopy(chain)
    tampered['request']['input_graph']['graph_version'] = 'TAMPERED-GRAPH'
    tampered['chain_sha256'] = canonical_sha256({key: value for key, value in tampered.items()
        if key != 'chain_sha256'})
    with pytest.raises(ValueError, match='CHAIN_REQUEST_INVALID|PARENT_REQUEST_BINDING_MISMATCH'):
        validate_graph_reconstruction_chain(tampered,
            expected_request=(audit_request, audit_response), expected_provider=expected)

def test_CE_GRA010_success_chain_cannot_be_turned_into_failure_by_rehash():
    _, audit_request, audit_response = fixtures()
    request = build_graph_reconstruction_request(audit_request, audit_response)
    chain = run_graph_reconstruction(audit_request, audit_response,
        lambda *args: (proposed(request), provider_receipt()))
    changed = deepcopy(chain)
    changed['state'] = 'RECONSTRUCTION_HOLD'
    changed['failure'] = dict(stage='RESPONSE_VALIDATION', error_type='ValueError',
        reason_code='GRAPH_RECONSTRUCTION_RESPONSE_SCHEMA_INVALID')
    changed['chain_sha256'] = canonical_sha256({key: value for key, value in changed.items()
        if key != 'chain_sha256'})
    with pytest.raises(ValueError, match='REPLAY_MISMATCH'):
        validate_graph_reconstruction_chain(changed)
    failed = deepcopy(chain)
    failed['state'] = 'RECONSTRUCTION_HOLD'
    failed['failure'] = dict(stage='RESPONSE_VALIDATION', error_type='ValueError',
        reason_code='GRAPH_RECONSTRUCTION_RESPONSE_SCHEMA_INVALID')
    failed['graph'] = None
    failed['validation_receipt'] = None
    failed['chain_sha256'] = canonical_sha256({key: value for key, value in failed.items()
        if key != 'chain_sha256'})
    with pytest.raises(ValueError, match='FAILURE_REPLAY_MISMATCH'):
        validate_graph_reconstruction_chain(failed)
