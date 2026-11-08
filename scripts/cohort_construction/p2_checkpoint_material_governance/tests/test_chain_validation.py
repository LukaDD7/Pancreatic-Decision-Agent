"""V061-AUD-007/GAP-02: replay history, targets and candidate graph identity."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile

import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import (
    bind_candidate_graph, validate_review_chain,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.episode import (
    build_episode_material_set, graph_revision_handoff, validate_episode_material_set,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.review import DIMENSIONS, run_review_chain
from scripts.cohort_construction.p2_checkpoint_material_review_app.synthetic_demo import synthetic_inputs
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256


def response_for(request, *, status='AUTO_REVIEW_HOLD', operations=(), review_id='MR-fictional'):
    return dict(format='material-review-0.3.0', review_id=review_id, target=deepcopy(request['target']),
        **deepcopy(request['response_binding']), review_round=deepcopy(request['review_round']), reviewer_type='llm_material_review',
        dimensions=[dict(dimension=d, applicability='APPLICABLE', status='RESOLVED',
                         reason='虚构工程测试，未形成临床意见') for d in DIMENSIONS],
        findings=[], operations=list(operations), status=status,
        required_actions=['虚构待人工核验'] if status=='AUTO_REVIEW_HOLD' else [])


def fictional_chain(*, include_source=False, graph=False):
    documents, sources, _ = synthetic_inputs()
    outside = dict(source_id=sources[1]['source_id'], source_version='v1', text=sources[1]['text'],
                   sha256=sources[1]['source_sha256'])
    context = None
    documents['selected_event']['checkpoint_status'] = 'CANDIDATE_SYNTHETIC'
    # Keep this fixture's selected-event binding aligned with its promoted graph
    # event. Production validation still rejects any real preparation mismatch.
    snapshot = dict(format='candidate-reconstruction-graph-snapshot-0.1.0',
                    graph_version='graph-v1', relations=[],
                    events=[deepcopy(documents['selected_event'])],
                    episodes=[dict(episode_id='ep1', event_ids=[documents['selected_event']['event_id']],
                        checkpoint_ids=[documents['selected_event']['event_id']])],
                    discovery_units=[], unit_inventory=None, final_graph_audit=None,
                    graph_is_candidate_not_clinical_reference=True)
    snapshot['graph_sha256'] = canonical_sha256(snapshot)
    documents['preparation_audit']['event_sha256'] = canonical_sha256(documents['selected_event'])
    documents['preparation_audit'].pop('audit_sha256', None)
    documents['preparation_audit']['audit_sha256'] = canonical_sha256(documents['preparation_audit'])
    if graph:
        context = bind_candidate_graph(snapshot, graph_version='graph-v1',
            target_event_id=documents['selected_event']['event_id'],
            checkpoint_id=documents['compiled']['target']['checkpoint_id'],
            task_context=dict(decision_time_evidence='UNRESOLVED', clinical_visibility_certified=False,
                              candidate_episode_id='ep1'))
    count = 0
    def ask(task, request, schema):
        nonlocal count
        count += 1
        operations = []
        if count == 1:
            for index, segment in enumerate(request['current_dispositions']):
                if segment['disposition'] == 'REVIEW':
                    operations.append(dict(operation_id='OP-'+str(index), operation='SET_DISPOSITION',
                        source_id=segment['source_id'], exact_quote=segment['exact_quote'], disposition='KEEP',
                        reason='虚构程序修订，不认证当时可见性'))
            if include_source:
                operations.append(dict(operation_id='OP-include', operation='INCLUDE_SOURCE',
                    source_id=outside['source_id'], reason='虚构来源范围修订，不改候选图'))
        elif include_source and count == 2:
            segment = next(s for s in request['current_dispositions']
                if s['source_id'] == outside['source_id'])
            operations.append(dict(operation_id='OP-outside', operation='SET_DISPOSITION',
                source_id=outside['source_id'], exact_quote=segment['exact_quote'], disposition='MASK',
                reason='虚构后续来源遮蔽'))
        return response_for(request, status='AUTO_REVIEW_REVISE' if operations else 'AUTO_REVIEW_PASS',
                            operations=operations, review_id='MR-fictional-'+str(count))
    chain = run_review_chain(documents['manifest'], documents['compiled'], ask,
        candidate_sources=[outside] if include_source else [],
        excluded_sources=[] if include_source else [outside], max_rounds=4, task_context=context)
    if include_source:
        assert chain['stop_reason'] == 'AUTO_PASS'
        assert len(chain['rounds']) == 3
        assert chain['rounds'][0]['diff']['source_scope_changed'] is True
        assert chain['rounds'][0]['diff']['doctor_visible_material_changed'] is True
    return documents, sources, chain, snapshot


def material_item(chain):
    graph = chain['task_context']['candidate_graph_binding']
    return dict(manifest=chain['manifest'], compiled=chain['compiled'], doctor_view=chain['doctor_view'],
                auto_review_chain=chain, revision_ledger=[r['ledger'] for r in chain['rounds']],
                state=chain['state'], event_reference=graph['target_event_id'])


def _episode_graph(graph):
    graph['graph_sha256'] = canonical_sha256({k: v for k, v in graph.items() if k != 'graph_sha256'})
    return graph


def episode_reference(snapshot):
    return dict(episode_id='ep1', candidate_version='graph-v1', input_sha256=canonical_sha256(snapshot),
                graph_snapshot=snapshot)


def test_replay_valid_multiround_with_new_source():
    _, _, chain, _ = fictional_chain(include_source=True)
    receipt = validate_review_chain(chain)
    assert receipt['deterministic_replay_verified'] and receipt['round_count'] == 3
    assert receipt['state'] == 'AUTO_REVIEW_PASS'
    assert chain['stop_reason'] == 'AUTO_PASS'
    assert chain['rounds'][0]['diff']['doctor_visible_material_changed']
    assert chain['rounds'][0]['diff']['source_scope_changed']
    assert receipt['clinical_visibility_certified'] is False


@pytest.mark.parametrize('mutation', ['input_hash','target','ledger','output','request','receipt','tail','terminal'])
def test_replay_rejects_historical_tampering(mutation):
    _, _, chain, _ = fictional_chain(include_source=True)
    broken = deepcopy(chain)
    first = broken['rounds'][0]
    if mutation == 'input_hash': first['input_compiled_sha256'] = '0'*64
    elif mutation == 'target': first['review']['target']['checkpoint_id'] = 'other-checkpoint'
    elif mutation == 'ledger': first['ledger']['operations'] = []
    elif mutation == 'output': first['compiled'] = deepcopy(broken['initial_compiled'])
    elif mutation == 'request': first['request']['researcher_sources'][0]['text'] = '伪造正文'
    elif mutation == 'receipt': first['application_receipt']['changed'] = False
    elif mutation == 'tail': broken['compiled'] = deepcopy(broken['initial_compiled'])
    else: broken['stop_reason'] = 'REVIEW_HOLD'
    with pytest.raises(Exception): validate_review_chain(broken)


def test_empty_pass_does_not_become_verified_history():
    _, _, chain, _ = fictional_chain()
    chain['rounds'] = []
    with pytest.raises(Exception, match='NONTERMINAL_OR_EMPTY'): validate_review_chain(chain)


def test_provider_failure_retains_zero_completed_rounds_only_as_hold():
    documents, _, _ = synthetic_inputs()
    def ask(*args):
        from scripts.cohort_construction.p2_checkpoint_material_governance.review import CHAIN_FORMAT
        error = RuntimeError('fictional-provider-failure')
        error.receipt = dict(invocation_id='SYNTHETIC-MATERIAL-FAILURE',
            legacy_chain_format=CHAIN_FORMAT)
        raise error
    chain = run_review_chain(documents['manifest'], documents['compiled'], ask)
    assert validate_review_chain(chain)['state'] == 'AUTO_REVIEW_HOLD'
    chain['state'] = 'AUTO_REVIEW_PASS'
    with pytest.raises(Exception, match='TERMINAL_STATE'): validate_review_chain(chain)


def test_CE_AMG033_real_client_material_schema_failure_preserves_private_evidence_and_replays(tmp_path):
    """Production Client -> material chain, with only HTTP transport mocked."""
    import json
    from unittest.mock import Mock, patch
    from scripts.cohort_construction.model_api import ModelAPISettings
    from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport

    documents, _, _ = synthetic_inputs()
    def execute(http):
        session = Mock(); session.post.return_value = http
        settings = ModelAPISettings('credential-sentinel', 'https://example.invalid/v1', 'synthetic-model')
        config = dict(model_temperature=0, max_output_tokens=64, max_http_attempts_per_request=1)
        import threading
        try:
            # Replace the thread-local container itself. Adding a temporary
            # attribute to the old container would leak its next mock Session.
            with patch.object(transport, '_thread', threading.local()), \
                    patch.object(transport.requests, 'Session', return_value=session):
                provider = transport.Client(tmp_path / 'cache', config, settings)
                return run_review_chain(documents['manifest'], documents['compiled'],
                    lambda task, request, schema: provider.ask(task, request, schema))
        finally:
            if hasattr(transport._thread, 'session'):
                del transport._thread.session

    bad = response_for({
        'target': documents['compiled']['target'],
        'response_binding': dict(protocol_version='checkpoint-automated-material-review-0.3.0',
            compiled_sha256=documents['compiled']['compiled_sha256'],
            doctor_view_sha256='0' * 64, request_sha256='0' * 64),
        'review_round': dict(round_index=1, max_rounds=3, is_first_review=True,
            current_state_produced_by_previous_review=False),
    })
    bad.pop('review_id')
    envelope = dict(choices=[dict(finish_reason='stop', message=dict(content=json.dumps(bad)))])
    http = Mock(); http.status_code = 200
    http.text = json.dumps(envelope); http.json.return_value = envelope
    chain = execute(http)
    failure = chain['failure']
    assert failure['stage'] == 'RESPONSE_SCHEMA_VALIDATION'
    assert failure['reason_code'] == 'RESPONSE_SCHEMA_VALIDATION_FAILED'
    assert failure['error_type'] == 'ModelRequestFailed'
    diagnostic = failure['provider_attempt_evidence']['diagnostic']
    assert diagnostic['validator'] == 'required' and diagnostic['missing_keys'] == ['review_id']
    assert failure['provider_attempt_evidence']['provider_response_text'] == http.text
    assert validate_review_chain(chain)['deterministic_replay_verified']
    # A rehashed diagnostic cannot replace the failed field or its definition path.
    tampered = deepcopy(chain)
    tampered['failure']['provider_attempt_evidence']['diagnostic']['instance_path'] = ['operations']
    with pytest.raises(Exception, match='CHAIN_EVIDENCE_HASH_MISMATCH'):
        validate_review_chain(tampered)

    http = Mock(); http.status_code = 503; http.text = '{"error":"synthetic private server body"}'
    http.raise_for_status.side_effect = __import__('requests').exceptions.HTTPError(response=http)
    chain = execute(http)
    assert chain['failure']['stage'] == 'PROVIDER_HTTP_RESPONSE'
    assert chain['failure']['reason_code'] == 'PROVIDER_HTTP_503'
    assert chain['failure']['provider_attempt_evidence']['provider_response_text'] == http.text
    assert 'synthetic private server body' not in json.dumps(chain['failure']['receipt'])
    assert validate_review_chain(chain)['deterministic_replay_verified']
    # Raw response bytes are saved before status parsing, in their own attempt file.
    invocation = chain['failure']['receipt']['invocation_id']
    snapshot = tmp_path / 'cache/invocations' / invocation / 'attempts/0001.provider-response.json'
    assert json.loads(snapshot.read_text())['provider_response_text'] == http.text


def test_CEREM_ADV034_full_and_compact_provider_transport_have_distinct_hash_domains(tmp_path):
    """Mock only transport; no external provider or patient data is used."""
    import hashlib
    import json
    import threading
    from unittest.mock import patch
    from scripts.cohort_construction.model_api import ModelAPISettings
    from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport
    from scripts.cohort_construction.p2_checkpoint_material_governance.prompts import material_review_client
    from scripts.cohort_construction.p2_checkpoint_material_governance.projection import (
        FORMAT as COMPACT_FORMAT, compact_material_review_request,
    )

    documents, _, _ = synthetic_inputs()
    graph = dict(format='candidate-reconstruction-graph-snapshot-0.1.0', graph_version='graph-v1',
        relations=[], events=[dict(event_id=documents['selected_event']['event_id'],
            checkpoint_status='CANDIDATE_SYNTHETIC')],
        episodes=[dict(episode_id='ep1', event_ids=[documents['selected_event']['event_id']],
            checkpoint_ids=[documents['selected_event']['event_id']])],
        discovery_units=[], unit_inventory=None, final_graph_audit=None,
        graph_is_candidate_not_clinical_reference=True)
    graph['graph_sha256'] = canonical_sha256(graph)
    context = bind_candidate_graph(graph, graph_version='graph-v1',
        target_event_id=documents['selected_event']['event_id'],
        checkpoint_id=documents['compiled']['target']['checkpoint_id'],
        task_context=dict(candidate_episode_id='ep1'))
    config = dict(model_temperature=0, max_output_tokens=4000, max_http_attempts_per_request=1)
    settings = ModelAPISettings('credential-sentinel', 'https://example.invalid/v1', 'synthetic-model')
    import tempfile as tempfile_module
    for compact in (False, True):
        with tempfile_module.TemporaryDirectory() as directory:
            bodies = []
            class Session:
                def prepare_request(self, request):
                    return request.prepare()
                def send(self, prepared, **kwargs):
                    bodies.append(prepared.body)
                    request = json.loads(json.loads(prepared.body)['messages'][1]['content'])['input']
                    answer = response_for(request)
                    envelope = dict(choices=[dict(finish_reason='stop',
                        message=dict(content=json.dumps(answer)))])
                    class Response:
                        status_code = 200
                        text = json.dumps(envelope)
                        def raise_for_status(self): pass
                        def json(self): return envelope
                    return Response()
            with patch.object(transport, '_thread', threading.local()), \
                    patch.object(transport.requests, 'Session', Session):
                provider = transport.Client(Path(directory), config, settings)
                kwargs = dict(transport_projection_format=COMPACT_FORMAT) if compact else {}
                chain = run_review_chain(documents['manifest'], documents['compiled'],
                    material_review_client(provider), task_context=context, **kwargs)
            assert chain['rounds'], (compact, chain.get('failure'))
            round_ = chain['rounds'][0]
            receipt = round_['receipt']
            assert round_['transport_projection_format'] == (COMPACT_FORMAT if compact else 'FULL_REQUEST')
            assert round_['declared_provider_payload_sha256'] == canonical_sha256(
                compact_material_review_request(round_['request']) if compact else round_['request'])
            assert receipt['declared_provider_payload_sha256'] == round_['declared_provider_payload_sha256']
            assert receipt['actual_provider_http_body_sha256'] == hashlib.sha256(bodies[0]).hexdigest()
            hashes = {round_['request_sha256'], round_['request_binding_sha256'],
                receipt['request_sha256'], receipt['declared_provider_payload_sha256'],
                receipt['actual_provider_http_body_sha256']}
            assert len(hashes) == (5 if compact else 4)
            assert (round_['request_sha256'] != receipt['declared_provider_payload_sha256']) is compact
            assert validate_review_chain(chain)['deterministic_replay_verified']
            mutations = [
                ('receipt.declared_provider_payload_sha256', '0' * 64),
                ('receipt.actual_provider_http_body_sha256', '0' * 64),
                ('receipt.request_sha256', '0' * 64),
                ('provider_attempt_evidence.actual_provider_http_body_sha256', '0' * 64),
                ('provider_attempt_evidence.provider_response_text', 'forged synthetic response'),
                ('receipt.attempts.-1.evidence.sha256', '0' * 64),
            ]
            for location, value in mutations:
                broken = deepcopy(chain)
                target = broken['rounds'][0]
                parts = location.split('.')
                for key in parts[:-1]:
                    target = target[int(key)] if key.lstrip('-').isdigit() else target[key]
                key = parts[-1]
                if key == 'evidence_content_sha256':
                    target[key] = value
                else:
                    target[key] = value
                with pytest.raises(Exception):
                    validate_review_chain(broken)


def test_CEREM_ADV034_cached_success_reuses_original_attempt_without_new_invocation(tmp_path):
    import json
    import threading
    from unittest.mock import patch
    from scripts.cohort_construction.model_api import ModelAPISettings
    from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport
    from scripts.cohort_construction.p2_checkpoint_material_governance.prompts import material_review_client

    documents, _, _ = synthetic_inputs()
    class Session:
        calls = 0
        def prepare_request(self, request): return request.prepare()
        def send(self, prepared, **kwargs):
            type(self).calls += 1
            request = json.loads(json.loads(prepared.body)['messages'][1]['content'])['input']
            answer = response_for(request)
            envelope = dict(choices=[dict(finish_reason='stop', message=dict(content=json.dumps(answer)))])
            class Response:
                status_code = 200
                text = json.dumps(envelope)
                def raise_for_status(self): pass
                def json(self): return envelope
            return Response()
    settings = ModelAPISettings('credential-sentinel', 'https://example.invalid/v1', 'synthetic-model')
    with patch.object(transport, '_thread', threading.local()), \
            patch.object(transport.requests, 'Session', Session):
        provider = transport.Client(tmp_path / 'cache', dict(model_temperature=0, max_output_tokens=64), settings)
        first = run_review_chain(documents['manifest'], documents['compiled'], material_review_client(provider))
        second = run_review_chain(documents['manifest'], documents['compiled'], material_review_client(provider))
    assert Session.calls == 1
    cached = second['rounds'][0]['receipt']
    assert cached['receipt_origin'] == 'PROVIDER_CLIENT_CACHE' and cached['cached'] is True
    assert cached['provider_invocation_occurred'] is False
    assert cached['provider_transport_verified'] is False
    assert second['rounds'][0]['provider_attempt_evidence'] == first['rounds'][0]['provider_attempt_evidence']
    assert validate_review_chain(second)['deterministic_replay_verified']
    mutations = [
        ('provider_attempt_evidence', dict(provider_response_text='forged')),
        ('provider_attempt_evidence', dict(response_content_text='{}')),
        ('provider_attempt_evidence', dict(response=dict(format='forged'))),
        ('receipt', dict(declared_provider_payload_sha256='0' * 64)),
        ('receipt', dict(response_sha256='0' * 64)),
        ('receipt', dict(provider_transport_verified=True)),
        ('receipt', dict(provider_invocation_occurred=True)),
        ('provider_attempt_evidence', dict(actual_provider_http_body_sha256=None)),
        ('provider_attempt_evidence', dict(attempt=2)),
        ('receipt', dict(attempts=[dict(second['rounds'][0]['receipt']['attempts'][-1],
            evidence=dict(second['rounds'][0]['receipt']['attempts'][-1]['evidence'], sha256='0' * 64))])),
    ]
    for parent, changes in mutations:
        broken = deepcopy(second)
        broken['rounds'][0][parent].update(changes)
        with pytest.raises(Exception):
            validate_review_chain(broken)
    for origin in ('PROVIDER_CLIENT', 'SYNTHETIC_ADAPTER'):
        broken = deepcopy(second)
        broken['rounds'][0]['receipt']['receipt_origin'] = origin
        with pytest.raises(Exception):
            validate_review_chain(broken)


def test_CEREM_ADV034_retry_failure_then_success_preserves_distinct_terminal_attempt(tmp_path):
    """One synthetic invocation fails, then succeeds with a different body."""
    import threading
    from unittest.mock import patch
    import requests
    from scripts.cohort_construction.model_api import ModelAPISettings
    from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport
    from scripts.cohort_construction.p2_checkpoint_material_governance.prompts import material_review_client

    documents, _, _ = synthetic_inputs()
    class Session:
        calls = 0
        def prepare_request(self, request): return request.prepare()
        def send(self, prepared, **kwargs):
            type(self).calls += 1
            if type(self).calls == 1:
                class Failure:
                    status_code = 503
                    text = '{"error":"fictional retry"}'
                    def raise_for_status(self):
                        raise requests.exceptions.HTTPError(response=self)
                return Failure()
            request = json.loads(json.loads(prepared.body)['messages'][1]['content'])['input']
            envelope = dict(choices=[dict(finish_reason='stop', message=dict(
                content=json.dumps(response_for(request))))])
            class Success:
                status_code = 200
                text = json.dumps(envelope)
                def raise_for_status(self): pass
                def json(self): return envelope
            return Success()
    settings = ModelAPISettings('credential-sentinel', 'https://example.invalid/v1', 'synthetic-model')
    with patch.object(transport, '_thread', threading.local()), \
            patch.object(transport.requests, 'Session', Session), \
            patch.object(transport.time, 'sleep'):
        provider = transport.Client(tmp_path / 'cache', dict(model_temperature=0,
            max_output_tokens=64, max_http_attempts_per_request=2), settings)
        chain = run_review_chain(documents['manifest'], documents['compiled'],
            material_review_client(provider))
    assert Session.calls == 2
    receipt = chain['rounds'][0]['receipt']
    assert [row['status'] for row in receipt['attempts']] == ['FAILED', 'SUCCESS']
    assert chain['rounds'][0]['provider_attempt_evidence']['attempt'] == 2
    assert receipt['attempts'][-1]['evidence']['attempt'] == 2
    assert validate_review_chain(chain)['deterministic_replay_verified']
    broken = deepcopy(chain)
    broken['rounds'][0]['receipt']['attempts'][0]['evidence']['sha256'] = '0' * 64
    with pytest.raises(Exception, match='PROVIDER_FAILED_ATTEMPT_(POINTER|INTEGRITY_INDEX)_MISMATCH'):
        validate_review_chain(broken)
    broken = deepcopy(chain)
    broken['rounds'][0]['receipt']['attempts'][1]['evidence']['sha256'] = '0' * 64
    with pytest.raises(Exception, match='PROVIDER_SUCCESS_EVIDENCE_POINTER_MISMATCH'):
        validate_review_chain(broken)


def test_CEREM_ADV034_empty_rounds_cannot_vacuously_verify_transport():
    from scripts.cohort_construction.p2_checkpoint_material_governance.run import provider_transport_verified
    assert provider_transport_verified([]) is False
    assert provider_transport_verified([dict(receipt=dict(provider_transport_verified=False))]) is False
    assert provider_transport_verified([dict(receipt=dict(provider_transport_verified=True))]) is True


def test_CEREM_ADV034_failure_receipt_http_status_and_response_bytes_cannot_be_rebound(tmp_path):
    import json
    from unittest.mock import Mock, patch
    import requests
    from scripts.cohort_construction.model_api import ModelAPISettings
    from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport
    documents, _, _ = synthetic_inputs()
    http = Mock(); http.status_code = 503; http.text = '{"error":"fictional transport failure"}'
    http.raise_for_status.side_effect = requests.exceptions.HTTPError(response=http)
    session = Mock(); session.post.return_value = http
    with patch.object(transport, '_thread', __import__('threading').local()), \
            patch.object(transport.requests, 'Session', return_value=session):
        provider = transport.Client(tmp_path / 'cache', dict(model_temperature=0, max_output_tokens=64,
            max_http_attempts_per_request=1), ModelAPISettings(
                'credential-sentinel', 'https://example.invalid/v1', 'synthetic-model'))
        chain = run_review_chain(documents['manifest'], documents['compiled'],
            lambda task, request, schema: provider.ask(task, request, schema))
    assert validate_review_chain(chain)['deterministic_replay_verified']
    for pointer_field, value in [('sha256', '0' * 64), ('attempt', 2),
                                 ('relative_path', 'invocations/forged/attempts/0001.json')]:
        broken = deepcopy(chain)
        broken['failure']['receipt']['attempts'][-1]['evidence'][pointer_field] = value
        with pytest.raises(Exception, match='PROVIDER_FAILURE_EVIDENCE_POINTER_MISMATCH'):
            validate_review_chain(broken)
    for field, value in [('http_status', 500), ('provider_response_text', 'forged'),
                         ('provider_response_sha256', '0' * 64)]:
        broken = deepcopy(chain)
        broken['failure']['provider_attempt_evidence'][field] = value
        with pytest.raises(Exception):
            validate_review_chain(broken)
    broken = deepcopy(chain)
    broken['failure']['receipt']['attempts'][-1]['http_status'] = 500
    with pytest.raises(Exception):
        validate_review_chain(broken)
    broken = deepcopy(chain)
    broken['failure']['receipt']['attempts'].append(dict(
        broken['failure']['receipt']['attempts'][-1], status='SUCCESS'))
    with pytest.raises(Exception):
        validate_review_chain(broken)



    _, _, chain, snapshot = fictional_chain(graph=True)
    target_event_id = chain['task_context']['candidate_graph_binding']['target_event_id']
    changed = deepcopy(snapshot)
    changed['episodes'][0].update(event_ids=[target_event_id, 'OTHER-EVENT'],
        checkpoint_ids=['OTHER-EVENT'])
    context = bind_candidate_graph(changed, graph_version='graph-v1',
        target_event_id=target_event_id, checkpoint_id=chain['compiled']['target']['checkpoint_id'])
    rebound = material_item(deepcopy(chain))
    rebound['auto_review_chain']['task_context'] = context
    with pytest.raises(Exception, match='CANDIDATE_EPISODE_MEMBERSHIP_INVALID'):
        build_episode_material_set(episode_reference(_episode_graph(changed)), [rebound])
    snapshot['episodes'][0]['checkpoint_ids'] = []
    with pytest.raises(Exception, match='EPISODE_CHECKPOINT_DECLARATION_REQUIRED'):
        build_episode_material_set(episode_reference(_episode_graph(snapshot)), [])


def test_CEREM_ADV018_episode_rejects_noncandidate_and_contradictory_event_reference():
    _, _, chain, snapshot = fictional_chain(graph=True)
    item = material_item(chain)
    item['event_reference'] = dict(event_id=item['event_reference'], checkpoint_id='WRONG-CHECKPOINT')
    with pytest.raises(Exception, match='EPISODE_EVENT_REFERENCE_BINDING_MISMATCH'):
        build_episode_material_set(episode_reference(snapshot), [item])
    item['event_reference']['checkpoint_id'] = chain['compiled']['target']['checkpoint_id']
    result = build_episode_material_set(episode_reference(snapshot), [item])
    assert validate_episode_material_set(result)
    noncandidate = deepcopy(snapshot)
    noncandidate['events'][0]['checkpoint_status'] = 'UNCERTAIN_REVIEW'
    with pytest.raises(Exception, match='NONCANDIDATE_CANNOT_BE_EPISODE_CHECKPOINT'):
        build_episode_material_set(episode_reference(_episode_graph(noncandidate)), [material_item(chain)])



def test_episode_requires_whole_replay_and_graph_membership():
    _, _, chain, snapshot = fictional_chain(graph=True)
    result = build_episode_material_set(episode_reference(snapshot), [material_item(chain)])
    assert validate_episode_material_set(result)
    handoff = graph_revision_handoff(result, chain['compiled']['target']['checkpoint_id'], 'MR1', '虚构重建')
    assert handoff['previous_material_pass_transferable'] is False
    broken = material_item(deepcopy(chain));broken['auto_review_chain']['rounds'][0]['ledger']['operations'] = []
    with pytest.raises(Exception): build_episode_material_set(episode_reference(snapshot), [broken])


def test_graph_change_requires_new_material_review_not_relabeling():
    _, _, chain, snapshot = fictional_chain(graph=True)
    changed = deepcopy(snapshot);changed['events'][0]['source_ids'] = ['different-source']
    with pytest.raises(Exception, match='GRAPH_CHANGED'):
        build_episode_material_set(episode_reference(_episode_graph(changed)), [material_item(chain)])
    context = bind_candidate_graph(changed, graph_version='graph-v2',
        target_event_id=chain['task_context']['candidate_graph_binding']['target_event_id'],
        checkpoint_id=chain['compiled']['target']['checkpoint_id'])
    with pytest.raises(Exception, match='GRAPH_CHANGED'):
        validate_review_chain(chain, expected_graph_context=context)


def test_event_reference_cannot_be_rebound_and_graph_requires_target():
    _, _, chain, snapshot = fictional_chain(graph=True)
    item = material_item(chain);item['event_reference'] = 'unrelated-event'
    with pytest.raises(Exception, match='EVENT_MEMBERSHIP'):
        build_episode_material_set(episode_reference(snapshot), [item])
    snapshot['events'] = []
    with pytest.raises(Exception, match='TARGET_EVENT_NOT_IN'):
        bind_candidate_graph(snapshot, graph_version='v2', target_event_id='missing', checkpoint_id='cp1')


def test_reconstruction_entry_runs_fresh_review_and_rejects_previous_pass_response():
    from scripts.cohort_construction.p2_checkpoint_material_governance.episode import restart_material_review_after_graph_revision
    _, _, chain, snapshot = fictional_chain(graph=True)
    old = build_episode_material_set(episode_reference(snapshot), [material_item(chain)])
    changed = deepcopy(snapshot);changed['events'][0]['revision_note'] = '虚构图版本修订'
    reference = episode_reference(_episode_graph(changed));reference['candidate_version'] = 'graph-v2'
    old_answer = chain['rounds'][-1]['review']
    rejected = restart_material_review_after_graph_revision(old, chain['compiled']['target']['checkpoint_id'],
        reference, chain['manifest'], chain['compiled'], lambda *args:old_answer,
        target_event_id=chain['task_context']['candidate_graph_binding']['target_event_id'])
    assert rejected['stop_reason'] == 'EXECUTION_FAILED'
    assert rejected['state'] == 'AUTO_REVIEW_HOLD'
    assert validate_review_chain(rejected)['round_count'] == 0
    fresh = restart_material_review_after_graph_revision(old, chain['compiled']['target']['checkpoint_id'],
        reference, chain['manifest'], chain['compiled'],
        lambda task,request,schema:response_for(request,status='AUTO_REVIEW_PASS'),
        target_event_id=chain['task_context']['candidate_graph_binding']['target_event_id'])
    assert validate_review_chain(fresh)['state'] == 'AUTO_REVIEW_PASS'
    assert fresh['rounds'][0]['request_sha256'] != chain['rounds'][-1]['request_sha256']
    assert fresh['clinical_release_permitted'] is False


def test_required_action_revision_adopts_tail_then_holds_and_cannot_be_forged_pass():
    documents,_,_=synthetic_inputs()
    def ask(task,request,schema):
        segment=next(s for s in request['current_dispositions'] if s['disposition']=='REVIEW')
        operation=dict(operation_id='OP-required',operation='SET_DISPOSITION',source_id=segment['source_id'],
            exact_quote=segment['exact_quote'],disposition='KEEP',reason='虚构修订与未决待办')
        answer=response_for(request,status='AUTO_REVIEW_REVISE',operations=[operation])
        answer['required_actions']=['由实际人员核对虚构待办']
        return answer
    chain=run_review_chain(documents['manifest'],documents['compiled'],ask)
    assert validate_review_chain(chain)['state']=='AUTO_REVIEW_HOLD'
    assert chain['stop_reason']=='REVIEW_HOLD'
    assert chain['compiled']==chain['rounds'][-1]['compiled']
    assert chain['compiled']!=documents['compiled']
    chain['state']='AUTO_REVIEW_PASS';chain['stop_reason']='AUTO_PASS'
    with pytest.raises(Exception,match='TERMINAL_REASON'):
        validate_review_chain(chain)
