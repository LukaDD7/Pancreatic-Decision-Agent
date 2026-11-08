"""Bounded, separate LLM graph reconstruction after an independent audit.

The auditor supplies findings, not edits. A separate request proposes a complete
candidate graph; deterministic checks bind every revision to the audited graph,
validate all current action citations and rebuild material evidence contexts.
The same adapter is deliberately not treated as an independent approval.
"""
from copy import deepcopy

from scripts.cohort_construction.p2_auto_checkpoint_v04.provider_diagnostics import (
    CHAIN_STAGES, DiagnosticValueError, diagnostics_for, private_chain_evidence,
    reason_for, replay_transport_evidence, schema_diagnostic,
    validate_private_chain_evidence, validate_provider_attempt_binding,
)
from scripts.cohort_construction.p2_auto_checkpoint_v04.validation import resolve_citations
from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import (
    freeze_candidate_graph, rebuild_reconstruction_context,
)
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from .audit import DIMENSIONS, build_graph_audit_request, validate_graph_audit
from .audit import PROTOCOL_VERSION as AUDIT_PROTOCOL_VERSION
from .audit import LEGACY_PROTOCOL_VERSIONS as LEGACY_AUDIT_PROTOCOL_VERSIONS

REQUEST_FORMAT = 'candidate-graph-reconstruction-request-0.2.0'
LEGACY_REQUEST_FORMAT = 'candidate-graph-reconstruction-request-0.1.0'
EVENT_CONTRACT = 'candidate-reconstruction-material-event-0.1.0'

FORMAT = 'candidate-graph-reconstruction-response-0.2.0'
CHAIN_FORMAT = 'candidate-graph-reconstruction-chain-0.3.0'
LEGACY_CHAIN_FORMATS = ('candidate-graph-reconstruction-chain-0.2.0',)
TASK = 'checkpoint_candidate_graph_bounded_reconstruction'
REQUEST_KINDS = ('EVENT_RECONSTRUCTION', 'RELATION_RECONSTRUCTION',
                 'EPISODE_RECONSTRUCTION', 'PROGRAM_REPAIR')

_CITATION = {'type': 'object', 'required': ['source_id', 'quote'],
             'properties': {'source_id': {'type': 'string', 'minLength': 1},
                            'quote': {'type': 'string', 'minLength': 1}}}
_CITATION_GROUPS = {'type': 'object', 'required': ['valid', 'invalid', 'ambiguous'],
                    'properties': {key: {'type': 'array', 'items': _CITATION}
                                   for key in ('valid', 'invalid', 'ambiguous')}}
GRAPH_RECONSTRUCTION_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'type': 'object', 'additionalProperties': False,
    'required': ['format', 'reconstruction_id', 'protocol_version', 'graph_sha256',
                 'request_sha256', 'status', 'graph', 'rationale'],
    'properties': {
        'format': {'const': FORMAT},
        'reconstruction_id': {'type': 'string', 'minLength': 1},
        'protocol_version': {'type': 'string', 'minLength': 1},
        'graph_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'request_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'status': {'enum': ['RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT',
                            'RECONSTRUCTION_HOLD']},
        'graph': {'type': ['object', 'null'], 'additionalProperties': False,
            'required': ['events', 'relations', 'episodes', 'graph_version'],
            'properties': {
                'events': {'type': 'array', 'items': {'type': 'object',
                    'required': ['event_id', 'source_id', 'checkpoint_status',
                                 'action_citations'],
                    'properties': {
                        'event_id': {'type': 'string', 'minLength': 1},
                        'source_id': {'type': 'string', 'minLength': 1},
                        'checkpoint_status': {'type': 'string', 'minLength': 1},
                        'action_citations': _CITATION_GROUPS,
                    }}},
                'relations': {'type': 'array', 'items': {'type': 'object',
                    'required': ['source_event_id', 'target_event_id'],
                    'properties': {
                        'source_event_id': {'type': 'string', 'minLength': 1},
                        'target_event_id': {'type': 'string', 'minLength': 1},
                    }}},
                'episodes': {'type': 'array', 'items': {'type': 'object',
                    'required': ['episode_id', 'event_ids', 'checkpoint_ids'],
                    'properties': {
                        'episode_id': {'type': 'string', 'minLength': 1},
                        'event_ids': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
                        'checkpoint_ids': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
                    }}},
                'graph_version': {'type': 'string', 'minLength': 1},
            }},
        'rationale': {'type': 'string', 'minLength': 1},
    },
    'allOf': [
        {'if': {'properties': {'status': {'const': 'RECONSTRUCTION_HOLD'}}},
         'then': {'properties': {'graph': {'type': 'null'}}}},
        {'if': {'properties': {'status': {'const': 'RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT'}}},
         'then': {'properties': {'graph': {'type': 'object'}}}},
    ],
}

# Historical requests retain their original validation semantics. New requests
# bind the downstream event contract explicitly; no field is inferred from an
# old event merely because its event_id was reused.
LEGACY_GRAPH_RECONSTRUCTION_SCHEMA = deepcopy(GRAPH_RECONSTRUCTION_SCHEMA)
_EVENT_PROPERTIES = {
    'source_ids': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                   'items': {'type': 'string', 'minLength': 1}},
    'choice_time': {'type': ['string', 'object', 'null']},
    'time_basis': {'type': 'string', 'minLength': 1},
    'time_validation': {'type': ['object', 'string']},
    'kind': {'type': 'string', 'minLength': 1},
    'phase': {'type': 'string', 'minLength': 1},
    'record_function': {'type': 'string', 'minLength': 1},
    'choice_boundary_citations': _CITATION_GROUPS,
    'prechoice_citations': _CITATION_GROUPS,
    'postchoice_citations': _CITATION_GROUPS,
    'validated_observation_citations': _CITATION_GROUPS,
    'availability_validation': {'type': 'array', 'items': {'type': 'object'}},
}
_EVENT_SCHEMA = GRAPH_RECONSTRUCTION_SCHEMA['properties']['graph']['properties']['events']['items']
_EVENT_SCHEMA['required'].extend(_EVENT_PROPERTIES)
_EVENT_SCHEMA['properties'].update(deepcopy(_EVENT_PROPERTIES))

INSTRUCTIONS = '''你是与候选图审核请求分离的候选图重建者，不是医生Reference评审者。
输入保存旧候选图、同一声明范围的完整原文、发现/复核阶段LLM提案和独立图审核者的revision_requests。审核意见是需要核对的修订请求，不是ground truth。
请独立阅读完整原文，只针对本次范围输出一个完整新候选图。不得用后续结局改写当时信息，不得把缺失推断为阴性，不得把自动审核PASS称为临床确认。
events保留现有事件结构及不确定性；action_citations.valid只能放当前原文中逐字、唯一且正确定位的引文。不得把action、uncertainty、reason或上游invalid/ambiguous引文复制成证据。无有效支持时valid=[]，并在invalid或uncertainty保留程序可核对的待核状态，不能伪造引文。
relations及episodes只引用新图实际存在的event_id；checkpoint只能是CANDIDATE_状态的事件。每个候选Episode须保留event_ids和checkpoint_ids。处理单位、零发现和被拒单位及unit_inventory仍须保留，不得借重建缩小声明范围。
输出新的graph_version，不填写graph_sha256；程序重新组装图、重新定位引文、重建证据上下文并计算hash。所有revision request仍须独立新图审核，不得自行宣称已解决或PASS。
逐字复制response_binding的protocol_version、graph_sha256、request_sha256。完整新图使用status=RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT；graph只含events、relations、episodes、graph_version，事件必须含event_id、source_id、checkpoint_status和action_citations.valid/invalid/ambiguous，关系必须含source_event_id、target_event_id，Episode必须含episode_id、event_ids、checkpoint_ids。
无法在现有原文上形成可核验新图时使用status=RECONSTRUCTION_HOLD、graph=null，并在rationale说明具体阻塞；不得输出占位对象，也不得把HOLD写成重建完成。仅按schema输出JSON。'''

INSTRUCTIONS += '''\n新版event_contract要求每个事件显式保留source_ids（完整声明来源数组，必须包含source_id）、choice_time、time_basis、time_validation、kind、phase、record_function、choice_boundary_citations、prechoice_citations、postchoice_citations、validated_observation_citations和availability_validation。所有引文集合均须有valid/invalid/ambiguous数组；只有可逐字唯一定位的原文可放valid，未解决引文保留invalid/ambiguous，不能晋升为支持。时间未知时choice_time=null、time_basis=unknown，time_validation显式说明待核；未找到引文时返回空集合，不得复制旧事件的时间或遮蔽判断来填空。availability_validation是提案，不能自称程序或临床认证。程序重建evidence_context和actual_model_evidence，不得把模型生成的证据上下文作为原文。无法提供可核验的完整事件契约时返回RECONSTRUCTION_HOLD。'''


def graph_reconstruction_client(client):
    def ask(task, payload, schema):
        return client.ask(task, payload, schema, system_instructions=INSTRUCTIONS)
    return ask


def _request_binding(request):
    return dict(protocol_version=request['response_binding']['protocol_version'],
        graph_sha256=request['response_binding']['graph_sha256'],
        request_sha256=request['response_binding']['request_sha256'])


def build_graph_reconstruction_request(audit_request, audit_response, *, legacy=False):
    """Bind a separate revision task to the complete original audit scope."""
    audit_response = validate_graph_audit(audit_request, deepcopy(audit_response))
    if audit_response['status'] != 'AUTO_GRAPH_AUDIT_REVISE':
        raise ValueError('GRAPH_RECONSTRUCTION_REQUIRES_VALID_REVISE_RESPONSE')
    revisions = audit_response['revision_requests']
    if (not revisions or any(item['request_kind'] not in REQUEST_KINDS
            or item['required_reconstruction'] is not True for item in revisions)):
        raise ValueError('GRAPH_RECONSTRUCTION_INPUT_UNRESOLVED')
    request = dict(format=LEGACY_REQUEST_FORMAT if legacy else REQUEST_FORMAT,
        task=TASK, input_graph=deepcopy(audit_request['candidate_graph']),
        complete_originals_supplied=deepcopy(audit_request['audit_scope']['complete_originals_supplied']),
        processing_units=deepcopy(audit_request['processing_units']),
        zero_discovery_and_rejected_units=deepcopy(audit_request['zero_discovery_and_rejected_units']),
        unit_inventory=deepcopy(audit_request['unit_inventory']),
        discovery_llm_proposals=deepcopy(audit_request['discovery_llm_proposals']),
        review_llm_proposals=deepcopy(audit_request['review_llm_proposals']),
        feedback_history=deepcopy(audit_request['feedback_history']),
        source_route_receipts=deepcopy(audit_request['source_route_receipts']),
        final_graph_audit=deepcopy(audit_request['final_graph_audit']),
        additional_excluded_units=deepcopy(audit_request['additional_excluded_units']),
        revision_requests=deepcopy(revisions),
        required_actions=deepcopy(audit_response['required_actions']),
        previous_review_id=audit_response['review_id'],
        auditor_may_not_approve_own_reconstruction=True,
        original_clinical_text_may_not_be_rewritten=True,
        clinical_release_permitted=False)
    if audit_request.get('reconstruction_lineage') is not None:
        # A later generation audits a reconstructed graph. Its complete lineage
        # participates in the original audit request hash and must therefore be
        # retained by the separate reconstruction request as well.
        request['reconstruction_lineage'] = deepcopy(audit_request['reconstruction_lineage'])
    request['response_binding'] = _request_binding(audit_request)
    if not legacy:
        request['event_contract_version'] = EVENT_CONTRACT
    return request


def _validate_response(request, response):
    from jsonschema import Draft202012Validator, ValidationError
    try:
        schema = (LEGACY_GRAPH_RECONSTRUCTION_SCHEMA
            if request['format'] == LEGACY_REQUEST_FORMAT else GRAPH_RECONSTRUCTION_SCHEMA)
        Draft202012Validator(schema).validate(response)
    except ValidationError as exc:
        raise DiagnosticValueError('GRAPH_RECONSTRUCTION_RESPONSE_SCHEMA_INVALID',
            [schema_diagnostic(exc, schema, response)]) from exc
    if any(response.get(key) != request['response_binding'][key]
            for key in ('protocol_version', 'graph_sha256', 'request_sha256')):
        raise ValueError('GRAPH_RECONSTRUCTION_RESPONSE_BINDING_MISMATCH')
    return response


def _valid_actions(event, sources):
    sid = event.get('source_id')
    if sid not in sources:
        raise ValueError('GRAPH_RECONSTRUCTION_EVENT_SOURCE_UNKNOWN')
    citations = event.get('action_citations', {}).get('valid', [])
    # Invalid/ambiguous upstream citations remain provenance, never current support.
    resolved = resolve_citations(citations, [sources[sid]], {sid: [(0, len(sources[sid]['text']))]})
    if resolved.get('invalid') or resolved.get('ambiguous'):
        raise ValueError('GRAPH_RECONSTRUCTION_ACTION_CITATION_NOT_VERIFIABLE')
    if not resolved.get('valid') and event.get('checkpoint_status', '').startswith('CANDIDATE_'):
        raise ValueError('GRAPH_RECONSTRUCTION_CANDIDATE_ACTION_SUPPORT_REQUIRED')
    event['action_citations'] = resolved


def _validate_request(request):
    from .chain import _reconstruction_input
    if not isinstance(request, dict) or request.get('task') != TASK:
        raise ValueError('GRAPH_RECONSTRUCTION_REQUEST_INVALID')
    if (request.get('format') not in (REQUEST_FORMAT, LEGACY_REQUEST_FORMAT)
            or (request['format'] == REQUEST_FORMAT
                and request.get('event_contract_version') != EVENT_CONTRACT)
            or (request['format'] == LEGACY_REQUEST_FORMAT and 'event_contract_version' in request)):
        raise ValueError('GRAPH_RECONSTRUCTION_EVENT_CONTRACT_VERSION_INVALID')
    audit_request = build_graph_audit_request(request['input_graph'],
        request['complete_originals_supplied'], **_reconstruction_input(request))
    if audit_request['response_binding'] != request['response_binding']:
        raise ValueError('GRAPH_RECONSTRUCTION_REQUEST_BINDING_INVALID')
    response = dict(format='candidate-reconstruction-audit-0.2.0'
            if audit_request['protocol_version'] == AUDIT_PROTOCOL_VERSION
            else 'candidate-reconstruction-audit-0.1.0',
        review_id=request['previous_review_id'],
        protocol_version=request['response_binding']['protocol_version'],
        graph_sha256=request['response_binding']['graph_sha256'],
        request_sha256=request['response_binding']['request_sha256'],
        target={'graph_sha256': request['response_binding']['graph_sha256']},
        reviewer_type='llm_candidate_reconstruction_audit',
        dimensions=[dict(dimension=dimension, applicability='NOT_APPLICABLE',
            status='NOT_APPLICABLE', reason='Reconstruction request preserves the prior REVISE binding; dimensions are not a new audit.')
            for dimension in DIMENSIONS],
        findings=[], revision_requests=deepcopy(request['revision_requests']),
        status='AUTO_GRAPH_AUDIT_REVISE', required_actions=deepcopy(request['required_actions']))
    validate_graph_audit(audit_request, response)
    if audit_request != build_graph_audit_request(request['input_graph'],
            request['complete_originals_supplied'], **_reconstruction_input(request)):
        raise ValueError('GRAPH_RECONSTRUCTION_REQUEST_CONTENT_INVALID')
    return request


def _citation_diagnostics(resolved, *, event_id, path):
    diagnostics = []
    for category in ('invalid', 'ambiguous'):
        for item in resolved.get(category, []):
            citation = item.get('citation', {})
            diagnostics.append(dict(kind='RECONSTRUCTION_CITATION',
                code='GRAPH_RECONSTRUCTION_MATERIAL_CITATION_NOT_VERIFIABLE',
                event_id=event_id, instance_path=[*path, 'valid', item['citation_index'], 'quote'],
                citation_index=item['citation_index'], resolution_status=item['status'],
                source_id=item.get('source_id', citation.get('source_id')),
                source_sha256=item.get('source_sha256'), quote=citation.get('quote'),
                occurrences=deepcopy(item.get('occurrences', [])),
                auto_repair_permitted=False))
    return diagnostics


def _validate_material_event(event, sources):
    declared = event['source_ids']
    if event['source_id'] not in declared:
        raise ValueError('GRAPH_RECONSTRUCTION_PRIMARY_SOURCE_NOT_DECLARED')
    if any(sid not in sources for sid in declared):
        raise ValueError('GRAPH_RECONSTRUCTION_DECLARED_SOURCE_UNKNOWN')
    originals = [sources[sid] for sid in declared]
    allowed = {sid: [(0, len(sources[sid]['text']))] for sid in declared}

    def verify(collection, path):
        resolved = resolve_citations(collection['valid'], originals, allowed)
        diagnostics = _citation_diagnostics(resolved,
            event_id=event['event_id'], path=path)
        if diagnostics:
            raise DiagnosticValueError('GRAPH_RECONSTRUCTION_MATERIAL_CITATION_NOT_VERIFIABLE', diagnostics)
        # Invalid and ambiguous proposals remain provenance, never evidence.
        return dict(valid=resolved['valid'], invalid=deepcopy(collection['invalid']),
                    ambiguous=deepcopy(collection['ambiguous']))

    for name in ('choice_boundary_citations', 'prechoice_citations',
                 'postchoice_citations', 'validated_observation_citations'):
        event[name] = verify(event[name], ['graph', 'events', event['event_id'], name])
    for item in event['availability_validation']:
        for name in ('validated_observation', 'validated_basis'):
            if name in item:
                from jsonschema import Draft202012Validator, ValidationError
                try:
                    Draft202012Validator(_CITATION_GROUPS).validate(item[name])
                except ValidationError as exc:
                    raise ValueError('GRAPH_RECONSTRUCTION_AVAILABILITY_CITATIONS_INVALID') from exc
                item[name] = verify(item[name],
                    ['graph', 'events', event['event_id'], 'availability_validation', name])


def validate_graph_reconstruction(request, response):
    """Return a complete new graph, an explicit HOLD, or a stable error."""
    _validate_request(request)
    response = _validate_response(request, deepcopy(response))
    if response['status'] == 'RECONSTRUCTION_HOLD':
        receipt = dict(format=FORMAT, reconstruction_id=response['reconstruction_id'],
            input_graph_sha256=request['input_graph']['graph_sha256'],
            output_graph_sha256=None, state='RECONSTRUCTION_HOLD',
            reason=response['rationale'],
            revision_request_ids=[item['request_id'] for item in request['revision_requests']],
            invalid_upstream_citations_promoted=False, evidence_context_rebuilt=False,
            graph_reconstructed=False, clinical_release_permitted=False)
        receipt['receipt_sha256'] = canonical_sha256(receipt)
        return None, receipt
    if response['status'] != 'RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT':
        raise ValueError('GRAPH_RECONSTRUCTION_STATUS_INVALID')
    candidate = response['graph']
    required = {'events', 'relations', 'episodes', 'graph_version'}
    if not isinstance(candidate, dict) or set(candidate) != required:
        raise ValueError('GRAPH_RECONSTRUCTION_COMPLETE_GRAPH_FIELDS_REQUIRED')
    if candidate['graph_version'] == request['input_graph']['graph_version']:
        raise ValueError('GRAPH_RECONSTRUCTION_REQUIRES_NEW_GRAPH_VERSION')
    old = request['input_graph']
    try:
        graph = freeze_candidate_graph(candidate['events'], candidate['relations'],
            candidate['episodes'], graph_version=candidate['graph_version'],
            discovery_units=old['discovery_units'], unit_inventory=old['unit_inventory'],
            final_graph_audit=old['final_graph_audit'])
        sources = {source['source_id']: source for source in request['complete_originals_supplied']}
        for event in graph['events']:
            if request['format'] == REQUEST_FORMAT:
                _validate_material_event(event, sources)
            unresolved_actions = {key: deepcopy(event.get('action_citations', {}).get(key, []))
                for key in ('invalid', 'ambiguous')}
            _valid_actions(event, sources)
            if request['format'] == REQUEST_FORMAT:
                event['action_citations'].update(unresolved_actions)
            event['evidence_context'] = rebuild_reconstruction_context(event, list(sources.values()))
            event['actual_model_evidence'] = deepcopy(event['evidence_context']['blocks'])
        graph = freeze_candidate_graph(graph['events'], graph['relations'], graph['episodes'],
            graph_version=graph['graph_version'], discovery_units=old['discovery_units'],
            unit_inventory=old['unit_inventory'], final_graph_audit=old['final_graph_audit'])
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('GRAPH_RECONSTRUCTION_GRAPH_ASSEMBLY_INVALID') from exc
    if graph['graph_sha256'] == old['graph_sha256']:
        raise ValueError('GRAPH_RECONSTRUCTION_REQUIRES_NEW_GRAPH_HASH')
    if request['format'] == REQUEST_FORMAT:
        from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import normalize_sources, prepare_candidate_materials
        _, failures = prepare_candidate_materials(graph, normalize_sources(list(sources.values())),
            decision_task_id='RECONSTRUCTION-CONTRACT-CHECK',
            visibility_policy_version='UNREVIEWED-NO-CLINICAL-PERMISSION')
        if failures:
            raise ValueError('GRAPH_RECONSTRUCTION_DOWNSTREAM_MATERIAL_CONTRACT_INVALID')
    receipt = dict(format=FORMAT, reconstruction_id=response['reconstruction_id'],
        input_graph_sha256=old['graph_sha256'], output_graph_sha256=graph['graph_sha256'],
        state='RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT',
        revision_request_ids=[item['request_id'] for item in request['revision_requests']],
        invalid_upstream_citations_promoted=False, evidence_context_rebuilt=True,
        graph_reconstructed=True,
        fresh_independent_graph_audit_required=True, clinical_release_permitted=False)
    receipt['receipt_sha256'] = canonical_sha256(receipt)
    return graph, receipt


def _provider_binding(response, receipt):
    """Bind private provider evidence without copying credentials into receipts."""
    if not isinstance(receipt, dict):
        raise ValueError('GRAPH_RECONSTRUCTION_PROVIDER_RECEIPT_INVALID')
    invocation_id = receipt.get('invocation_id')
    response_sha256 = receipt.get('response_sha256')
    if (not isinstance(invocation_id, str) or not invocation_id
            or not isinstance(response_sha256, str) or len(response_sha256) != 64
            or any(c not in '0123456789abcdef' for c in response_sha256)):
        raise ValueError('GRAPH_RECONSTRUCTION_PROVIDER_RECEIPT_BINDING_REQUIRED')
    return dict(invocation_id=invocation_id, response_sha256=response_sha256,
        response_content_sha256=canonical_sha256(response))


def run_graph_reconstruction(audit_request, audit_response, ask):
    """One bounded separate reconstruction call; no audit result is inherited."""
    request = build_graph_reconstruction_request(audit_request, audit_response)
    response = None
    receipt = {}
    provider_binding = None
    graph = None
    validation = None
    try:
        answer = ask(TASK, deepcopy(request), deepcopy(GRAPH_RECONSTRUCTION_SCHEMA))
        response, receipt = answer if isinstance(answer, tuple) else (answer, {})
        if not isinstance(response, dict):
            raise ValueError('GRAPH_RECONSTRUCTION_RESPONSE_SCHEMA_INVALID')
        if isinstance(receipt, dict) and receipt.get('invocation_id'):
            provider_binding = _provider_binding(response, receipt)
        else:
            # A returned object without provider identity is not replayable
            # evidence. Real clients always provide this binding.
            raise ValueError('GRAPH_RECONSTRUCTION_PROVIDER_RECEIPT_BINDING_REQUIRED')
        graph, validation = validate_graph_reconstruction(request, response)
    except Exception as exc:
        exception_receipt = getattr(exc, 'receipt', None)
        if isinstance(exception_receipt, dict):
            receipt = exception_receipt
        evidence = getattr(exc, 'response_evidence', None)
        if response is None and isinstance(evidence, dict) \
                and isinstance(evidence.get('response'), dict) \
                and isinstance(evidence.get('provider_envelope'), dict) \
                and isinstance(receipt.get('response_sha256'), str):
            response = evidence['response']
        if response is not None and provider_binding is None:
            if receipt.get('invocation_id') and receipt.get('response_sha256'):
                provider_binding = _provider_binding(response, receipt)
            else:
                response = None
                receipt = {}
        if provider_binding is not None and response is not None \
                and receipt.get('status') == 'FAILED' \
                and any(isinstance(item, dict) and 'schema_path' in item
                        for item in receipt.get('attempts', [])):
            provider_binding['transport_response_schema_rejected'] = True
        stage = 'RESPONSE_VALIDATION' if response is not None else 'PROVIDER_REQUEST'
        if isinstance(getattr(exc, 'failure_evidence', None), dict):
            stage = exc.failure_evidence['terminal_stage']
        failure = dict(stage=stage,
            error_type=type(exc).__name__, reason_code=reason_for(exc))
        if isinstance(receipt, dict):
            for key in ('invocation_id', 'request_sha256'):
                if isinstance(receipt.get(key), str) and receipt[key]:
                    failure['provider_' + key if key == 'invocation_id' else key] = receipt[key]
        if type(exc).__name__ == 'ModelRequestFailed' \
                and isinstance(getattr(exc, 'failure_evidence', None), dict):
            failure['reason_code'] = exc.failure_evidence.get('reason_code', failure['reason_code'])
        if provider_binding is not None:
            failure['provider_invocation_id'] = receipt['invocation_id']
        evidence = getattr(exc, 'failure_evidence', None)
        if isinstance(evidence, dict):
            failure['provider_attempt_evidence'] = private_chain_evidence(evidence)
        diagnostics = diagnostics_for(exc)
        if diagnostics is not None:
            failure['program_diagnostics'] = diagnostics
        chain = dict(format=CHAIN_FORMAT,
            state='RECONSTRUCTION_HOLD', request=request, failure=failure,
            response_evidence_preserved=response is not None,
            graph_modified_by_auditor=False, clinical_release_permitted=False)
        if response is not None:
            chain.update(response=deepcopy(response), provider_receipt=deepcopy(receipt),
                provider_response_binding=provider_binding)
        chain['chain_sha256'] = canonical_sha256(chain)
        return chain
    chain = dict(format=CHAIN_FORMAT,
        state='RECONSTRUCTION_HOLD' if graph is None else 'RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT',
        request=request, response=deepcopy(response), provider_receipt=deepcopy(receipt),
        provider_response_binding=provider_binding, graph=graph,
        validation_receipt=validation, graph_modified_by_auditor=False,
        response_evidence_preserved=True, clinical_release_permitted=False)
    chain['chain_sha256'] = canonical_sha256(chain)
    return chain


def validate_graph_reconstruction_chain(chain, *, expected_request=None,
                                        expected_provider=None):
    """Replay success, explicit model HOLD and response/provider failure offline.

    A chain hash authenticates its own bytes, not its clinical parent. When an
    expected request or ledger binding is supplied, both are independently
    checked; absence of either is an error rather than a fallback to self-hash.
    """
    if not isinstance(chain, dict) or (chain.get('format') != CHAIN_FORMAT
            and chain.get('format') not in LEGACY_CHAIN_FORMATS) \
            or canonical_sha256({key: value for key, value in chain.items() if key != 'chain_sha256'}) != chain.get('chain_sha256'):
        raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_FORMAT_OR_HASH_INVALID')
    if (chain.get('graph_modified_by_auditor') is not False
            or chain.get('clinical_release_permitted') is not False):
        raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_GOVERNANCE_INVALID')
    state = chain.get('state')
    if state == 'RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT':
        if 'failure' in chain or chain.get('response_evidence_preserved') is not True:
            raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_SUCCESS_FIELDS_INVALID')
    elif state == 'RECONSTRUCTION_HOLD':
        failure = chain.get('failure')
        if failure is not None:
            if (not isinstance(failure, dict)
                    or failure.get('stage') not in CHAIN_STAGES
                    or not isinstance(failure.get('error_type'), str) or not failure['error_type']
                    or not isinstance(failure.get('reason_code'), str) or not failure['reason_code']):
                raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_FAILURE_INVALID')
            if failure['stage'] in ('PROVIDER_REQUEST', 'PROVIDER_HTTP_RESPONSE',
                    'PROVIDER_ENVELOPE_PARSE', 'PROVIDER_CONTENT_PARSE'):
                if any(key in chain for key in ('response', 'provider_receipt', 'provider_response_binding')) \
                        or chain.get('response_evidence_preserved') is not False:
                    raise ValueError('GRAPH_RECONSTRUCTION_PROVIDER_FAILURE_EVIDENCE_INVALID')
                invocation_id = failure.get('provider_invocation_id')
                if invocation_id is not None and (not isinstance(invocation_id, str) or not invocation_id):
                    raise ValueError('GRAPH_RECONSTRUCTION_PROVIDER_FAILURE_EVIDENCE_INVALID')
            elif failure['stage'] == 'RESPONSE_SCHEMA_VALIDATION':
                if not chain.get('provider_receipt') or not chain.get('provider_response_binding') \
                        or 'response' not in chain or chain.get('response_evidence_preserved') is not True:
                    raise ValueError('GRAPH_RECONSTRUCTION_PROVIDER_FAILURE_EVIDENCE_INVALID')
            elif chain.get('response_evidence_preserved') is not True:
                raise ValueError('GRAPH_RECONSTRUCTION_RESPONSE_FAILURE_EVIDENCE_REQUIRED')
        elif chain.get('response_evidence_preserved') is not True:
            raise ValueError('GRAPH_RECONSTRUCTION_HOLD_EVIDENCE_REQUIRED')
    else:
        raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_STATE_INVALID')
    failure = chain.get('failure')
    # Validate the parent and independently supplied expected input before any
    # failure-response diagnostics. Otherwise a changed source can be reported as
    # a model-diagnostic mismatch instead of a tampered request.
    try:
        _validate_request(chain['request'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_REQUEST_INVALID') from exc
    if expected_request is not None:
        expected = build_graph_reconstruction_request(expected_request[0], expected_request[1],
            legacy=chain['request']['format'] == LEGACY_REQUEST_FORMAT)
        if chain['request'] != expected:
            raise ValueError('GRAPH_RECONSTRUCTION_PARENT_REQUEST_BINDING_MISMATCH')
    if expected_provider is not None:
        if expected_provider.get('payload_sha256') is not None \
                and canonical_sha256(chain['request']) != expected_provider['payload_sha256']:
            raise ValueError('GRAPH_RECONSTRUCTION_LEDGER_REQUEST_BINDING_MISMATCH')
    if failure is not None:
        evidence = failure.get('provider_attempt_evidence')
        if failure['stage'] != 'RESPONSE_VALIDATION':
            current = chain['format'] == CHAIN_FORMAT
            synthetic = current and evidence is None \
                and failure.get('error_type') == 'RuntimeError'
            if chain['format'] in LEGACY_CHAIN_FORMATS and evidence is None:
                # A frozen old shell remains inspectable, but cannot acquire a
                # verified local replay merely by adding a transport-stage label.
                if expected_request is None:
                    return dict(format='candidate-graph-reconstruction-chain-replay-0.2.0',
                        chain_sha256=chain['chain_sha256'], state=state,
                        input_graph_sha256=chain['request']['input_graph']['graph_sha256'],
                        output_graph_sha256=None, failure_replayed=False, replay_verified=False,
                        model_call_performed=False, historical_failure_shell_only=True,
                        fresh_independent_graph_audit_required=False,
                        clinical_release_permitted=False)
                if expected_provider is None or failure.get('error_type') != 'RuntimeError' \
                        or failure.get('provider_invocation_id') != expected_provider.get('invocation_id'):
                    raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_LEGACY_FAILURE_BINDING_REQUIRED')
            if not synthetic:
                validate_private_chain_evidence(evidence, evidence)
                validate_provider_attempt_binding(evidence, failure)
                replay_transport_evidence(evidence, LEGACY_GRAPH_RECONSTRUCTION_SCHEMA
                    if chain['request']['format'] == LEGACY_REQUEST_FORMAT else GRAPH_RECONSTRUCTION_SCHEMA)
                if failure.get('error_type') != 'ModelRequestFailed':
                    raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_PROVIDER_REASON_MISMATCH')
        elif evidence is not None:
            validate_private_chain_evidence(evidence, evidence)
            validate_provider_attempt_binding(evidence, failure, receipt={})
            if failure.get('error_type') != 'ModelRequestFailed':
                raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_PROVIDER_REASON_MISMATCH')
        if failure.get('program_diagnostics') is not None:
            try:
                validate_graph_reconstruction(chain['request'], chain['response'])
            except ValueError as exc:
                if diagnostics_for(exc) != failure['program_diagnostics']:
                    raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_DIAGNOSTIC_REPLAY_MISMATCH') from exc
    if chain.get('failure') is None or chain['failure']['stage'] not in ('PROVIDER_REQUEST',
            'PROVIDER_HTTP_RESPONSE', 'PROVIDER_ENVELOPE_PARSE', 'PROVIDER_CONTENT_PARSE'):
        expected_binding = _provider_binding(chain.get('response'), chain.get('provider_receipt'))
        if chain['provider_receipt'].get('status') == 'FAILED' \
                and any(isinstance(item, dict) and 'schema_path' in item
                        for item in chain['provider_receipt'].get('attempts', [])):
            expected_binding['transport_response_schema_rejected'] = True
        if chain.get('provider_response_binding') != expected_binding:
            raise ValueError('GRAPH_RECONSTRUCTION_PROVIDER_BINDING_MISMATCH')
    if expected_provider is not None:
        binding = chain.get('provider_response_binding') or chain['failure']
        if binding.get('invocation_id', binding.get('provider_invocation_id')) \
                != expected_provider.get('invocation_id'):
            raise ValueError('GRAPH_RECONSTRUCTION_LEDGER_INVOCATION_BINDING_MISMATCH')
        # The batch ledger hashes the structured payload. The transport Client
        # separately hashes [endpoint, HTTP body]; never compare those domains.
        failure = chain.get('failure', {})
        if expected_provider.get('transport_request_sha256') is not None \
                and failure.get('request_sha256') is not None \
                and failure['request_sha256'] != expected_provider['transport_request_sha256']:
            raise ValueError('GRAPH_RECONSTRUCTION_LEDGER_REQUEST_BINDING_MISMATCH')
        if expected_provider.get('payload_sha256') is not None \
                and canonical_sha256(chain['request']) != expected_provider['payload_sha256']:
            raise ValueError('GRAPH_RECONSTRUCTION_LEDGER_REQUEST_BINDING_MISMATCH')
        provider_receipt = chain.get('provider_receipt', {})
        if expected_provider.get('response_sha256') is not None \
                and provider_receipt.get('response_sha256') != expected_provider['response_sha256']:
            raise ValueError('GRAPH_RECONSTRUCTION_LEDGER_RESPONSE_BINDING_MISMATCH')
    if chain.get('failure') is not None:
        if failure['stage'] in ('PROVIDER_REQUEST', 'PROVIDER_HTTP_RESPONSE',
                'PROVIDER_ENVELOPE_PARSE', 'PROVIDER_CONTENT_PARSE'):
            if current and evidence is None and not synthetic:
                raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_PROVIDER_EVIDENCE_REQUIRED')
            if not any(key in chain['failure'] for key in ('provider_invocation_id', 'request_sha256')):
                raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_PROVIDER_RECEIPT_REQUIRED')
            return dict(format='candidate-graph-reconstruction-chain-replay-0.2.0',
                chain_sha256=chain['chain_sha256'], state=state,
                input_graph_sha256=chain['request']['input_graph']['graph_sha256'],
                output_graph_sha256=None, failure_replayed=True,
                replay_verified=evidence is not None,
                failure_category_binding_verified=evidence is None,
                local_failure_recomputed=False,
                synthetic_adapter_failure_only=synthetic,
                historical_failure_shell_only=synthetic,
                model_call_performed=False, fresh_independent_graph_audit_required=False,
                clinical_release_permitted=False)
        # Transport HTTP/JSON/Schema failures are already replayed from the
        # terminal provider evidence. Do not reclassify their parsed response as
        # a business RESPONSE_VALIDATION exception (which may use another Schema).
        if failure['stage'] in ('PROVIDER_HTTP_RESPONSE', 'PROVIDER_ENVELOPE_PARSE',
                'PROVIDER_CONTENT_PARSE', 'RESPONSE_SCHEMA_VALIDATION'):
            return dict(format='candidate-graph-reconstruction-chain-replay-0.2.0',
                chain_sha256=chain['chain_sha256'], state=state,
                input_graph_sha256=chain['request']['input_graph']['graph_sha256'],
                output_graph_sha256=None, failure_replayed=True,
                replay_verified=evidence is not None,
                failure_category_binding_verified=evidence is None,
                model_call_performed=False, fresh_independent_graph_audit_required=False,
                clinical_release_permitted=False)
        try:
            validate_graph_reconstruction(chain['request'], chain['response'])
        except ValueError as exc:
            if (str(exc) != chain['failure']['reason_code'] or chain['failure']['error_type'] != 'ValueError') \
                    and not (chain['failure']['error_type'] == 'DiagnosticValueError'
                    and str(exc) == chain['failure']['reason_code']) \
                    and not (chain['format'] in LEGACY_CHAIN_FORMATS
                    and chain['failure']['error_type'] == 'ModelRequestFailed'
                    and chain['failure']['reason_code'] in (
                        'ModelRequestFailed', 'RESPONSE_SCHEMA_VALIDATION_FAILED')
                    and str(exc) == 'GRAPH_RECONSTRUCTION_RESPONSE_SCHEMA_INVALID'
                    and chain['provider_response_binding'].get('transport_response_schema_rejected') is True):
                raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_FAILURE_REPLAY_MISMATCH') from exc
        else:
            raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_FAILURE_REPLAY_MISMATCH')
        return dict(format='candidate-graph-reconstruction-chain-replay-0.2.0',
            chain_sha256=chain['chain_sha256'], state=state,
            input_graph_sha256=chain['request']['input_graph']['graph_sha256'],
            output_graph_sha256=None, failure_replayed=True, replay_verified=True,
            model_call_performed=False, fresh_independent_graph_audit_required=False,
            clinical_release_permitted=False)
    graph, receipt = validate_graph_reconstruction(chain['request'], chain['response'])
    if graph != chain.get('graph') or receipt != chain.get('validation_receipt') \
            or receipt['state'] != state:
        raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_REPLAY_MISMATCH')
    if state == 'RECONSTRUCTION_HOLD':
        if graph is not None or receipt['state'] != 'RECONSTRUCTION_HOLD':
            raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_HOLD_REPLAY_MISMATCH')
    elif graph is None:
        raise ValueError('GRAPH_RECONSTRUCTION_CHAIN_GRAPH_MISSING')
    return dict(format='candidate-graph-reconstruction-chain-replay-0.2.0',
        chain_sha256=chain['chain_sha256'], state=state,
        input_graph_sha256=receipt['input_graph_sha256'],
        output_graph_sha256=receipt['output_graph_sha256'], replay_verified=True,
        model_call_performed=False,
        fresh_independent_graph_audit_required=graph is not None,
        clinical_release_permitted=False)
