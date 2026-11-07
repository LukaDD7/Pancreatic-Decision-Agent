"""Authoritative replay of immutable material review chains (no model calls).

Checksums prove internal identity, not authorship or clinical visibility. The
request/revision implementation is loaded only inside validation so review.py
never imports this module and no import cycle is introduced.
"""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError, canonical_sha256

LEGACY_CHAIN_FORMATS = ('automated-material-review-chain-0.2.0',
                        'automated-material-review-chain-0.3.0')


def _same(actual, expected, code):
    if actual != expected:
        raise ContractError(code)


def validate_candidate_graph_context(task_context, target, *, required=False):
    """Bind a program candidate graph; never certify clinical timing/order."""
    context = task_context or {}
    snapshot = context.get('candidate_graph_snapshot')
    binding = context.get('candidate_graph_binding')
    if snapshot is None and binding is None and not required:
        return None
    if not isinstance(snapshot, dict) or not isinstance(binding, dict):
        raise ContractError('CHAIN_CANDIDATE_GRAPH_BINDING_REQUIRED')
    if set(binding) != {'graph_version', 'graph_sha256', 'target_event_id', 'checkpoint_id'}:
        raise ContractError('CHAIN_CANDIDATE_GRAPH_FIELDS_MISMATCH')
    if (not isinstance(binding['graph_version'], str) or not binding['graph_version']
            or canonical_sha256(snapshot) != binding['graph_sha256']
            or binding['checkpoint_id'] != target['checkpoint_id']):
        raise ContractError('CHAIN_CANDIDATE_GRAPH_IDENTITY_MISMATCH')
    events = snapshot.get('events')
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise ContractError('CHAIN_CANDIDATE_GRAPH_EVENTS_REQUIRED')
    ids = [event.get('event_id') for event in events]
    if (any(not isinstance(value, str) or not value for value in ids)
            or len(ids) != len(set(ids)) or binding['target_event_id'] not in ids):
        raise ContractError('CHAIN_TARGET_EVENT_NOT_IN_CANDIDATE_GRAPH')
    return deepcopy(binding)


def bind_candidate_graph(snapshot, *, graph_version, target_event_id, checkpoint_id,
                         task_context=None):
    """Create context for a fresh material review after reconstruction."""
    result = deepcopy(task_context or {})
    result.update(candidate_graph_snapshot=deepcopy(snapshot), candidate_graph_binding=dict(
        graph_version=graph_version, graph_sha256=canonical_sha256(snapshot),
        target_event_id=target_event_id, checkpoint_id=checkpoint_id))
    validate_candidate_graph_context(result, {'checkpoint_id': checkpoint_id}, required=True)
    return result


def validate_review_chain(chain, *, expected_graph_context=None):
    """Rebuild every request and replay every operation, failing closed.

    A legitimate execution failure can retain zero completed rounds only as
    HOLD/EXECUTION_FAILED. It cannot supply a PASS or clinical permission.
    """
    from .review import (AUTO_PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSIONS, CHAIN_FORMAT,
                         MaterialReviewError, build_review_request,
                         build_source_registry, registry_review_sources,
                         validate_material_review, doctor_view, _content_signature, _diff,
                         _new_source_resolution_pending, _state_is_new_source_pending)
    if not isinstance(chain, dict) or (chain.get('format') != CHAIN_FORMAT
            and chain.get('format') not in LEGACY_CHAIN_FORMATS):
        raise ContractError('CHAIN_VERSION_REQUIRES_REPLAYABLE_V02_V03_OR_V04')
    protocol_version = chain.get('protocol_version')
    current = chain['format'] == CHAIN_FORMAT
    if (current and protocol_version != AUTO_PROTOCOL_VERSION) \
            or (not current and protocol_version not in LEGACY_PROTOCOL_VERSIONS):
        raise ContractError('CHAIN_PROTOCOL_MISMATCH')
    previous_v03 = chain['format'] == 'automated-material-review-chain-0.3.0'
    if current:
        from .schema import validate_schema
        validate_schema('AutomatedMaterialReviewChain', chain)
    if (chain.get('clinical_release_permitted') is not False
            or chain.get('event_graph_modified') is not False
            or chain.get('clinical_reference_created') is not False
            or chain.get('human_sample_status') != 'NOT_ASSESSED'):
        raise ContractError('CHAIN_GOVERNANCE_FLAGS_MISMATCH')
    maximum, rounds = chain.get('max_rounds'), chain.get('rounds')
    if type(maximum) is not int or not 1 <= maximum <= 20 or not isinstance(rounds, list) or len(rounds) > maximum:
        raise ContractError('CHAIN_ROUND_BUDGET_INVALID')
    registry = chain.get('source_registry')
    if not isinstance(registry, list):
        raise ContractError('CHAIN_SOURCE_REGISTRY_REQUIRED')
    _same(chain.get('source_registry_sha256'), canonical_sha256(registry), 'CHAIN_SOURCE_REGISTRY_HASH_MISMATCH')
    initial_manifest, initial_compiled = chain.get('initial_manifest'), chain.get('initial_compiled')
    if not isinstance(initial_manifest, dict) or not isinstance(initial_compiled, dict):
        raise ContractError('CHAIN_INITIAL_VERSION_REQUIRED')
    # Rebuild the registry from its declared immutable initial scopes. This also
    # validates exact text/version/hash and rejects duplicate/overlapping IDs.
    candidates = [source for source in registry if source.get('origin_scope') == 'CANDIDATE']
    excluded = [source for source in registry if source.get('origin_scope') == 'EXCLUDED']
    _same(registry, build_source_registry(initial_manifest, candidate_sources=candidates,
          excluded_sources=excluded), 'CHAIN_SOURCE_REGISTRY_INITIAL_SCOPE_MISMATCH')
    context = chain.get('task_context')
    if not isinstance(context, dict):
        raise ContractError('CHAIN_TASK_CONTEXT_REQUIRED')
    graph_binding = validate_candidate_graph_context(context, initial_compiled['target'])
    if expected_graph_context is not None:
        expected = validate_candidate_graph_context(expected_graph_context, initial_compiled['target'], required=True)
        _same(graph_binding, expected, 'CHAIN_CANDIDATE_GRAPH_CHANGED_RESTART_REQUIRED')
        _same(context.get('candidate_graph_snapshot'), expected_graph_context['candidate_graph_snapshot'],
              'CHAIN_CANDIDATE_GRAPH_CHANGED_RESTART_REQUIRED')
    manifest, compiled = initial_manifest, initial_compiled
    seen = {_content_signature(compiled): 0}
    previously_included_ids = {s['source_id'] for s in manifest['sources']}
    expected_stop = None
    for index, round_ in enumerate(rounds, 1):
        if expected_stop is not None:
            raise ContractError('CHAIN_ROUND_AFTER_TERMINAL_STATE')
        if not isinstance(round_, dict):
            raise ContractError('CHAIN_ROUND_OBJECT_REQUIRED')
        if current:
            from .review import _transport_binding
            from scripts.cohort_construction.p2_auto_checkpoint_v04.provider_diagnostics import (
                validate_provider_success_binding,
            )
            receipt = round_.get('receipt')
            if not isinstance(receipt, dict):
                raise ContractError('CHAIN_ROUND_RECEIPT_REQUIRED')
            declared = _transport_binding(round_.get('request'),
                projection_format=receipt.get('transport_projection_format'))
            _same(round_.get('transport_projection_format'),
                  declared['transport']['transport_projection_format'], 'CHAIN_TRANSPORT_PROJECTION_MISMATCH')
            _same(round_.get('declared_provider_payload_sha256'),
                  declared['transport']['declared_provider_payload_sha256'], 'CHAIN_TRANSPORT_PAYLOAD_HASH_MISMATCH')
            if receipt.get('receipt_origin') == 'SYNTHETIC_ADAPTER':
                if (receipt.get('provider_invocation_occurred') is not False
                        or receipt.get('provider_transport_verified') is not False
                        or receipt.get('declared_provider_payload_sha256')
                        != declared['transport']['declared_provider_payload_sha256']
                        or 'provider_attempt_evidence' in round_):
                    raise ContractError('CHAIN_SYNTHETIC_RECEIPT_PROVENANCE_MISMATCH')
            elif receipt.get('receipt_origin') in ('PROVIDER_CLIENT', 'PROVIDER_CLIENT_CACHE'):
                cached = receipt.get('receipt_origin') == 'PROVIDER_CLIENT_CACHE'
                evidence = round_.get('provider_attempt_evidence')
                if cached:
                    # A cache hit has no fresh HTTP bytes to certify. Require
                    # each retained retry to embed its exact archived evidence.
                    from scripts.cohort_construction.p2_auto_checkpoint_v04.provider_diagnostics import (
                        private_chain_evidence,
                    )
                    for attempt in receipt.get('attempts', [])[:-1]:
                        archived = attempt.get('archived_evidence')
                        pointer = attempt.get('evidence', {})
                        if not isinstance(archived, dict) \
                                or private_chain_evidence(archived) != archived \
                                or pointer.get('sha256') != archived['evidence_content_sha256']:
                            raise ContractError('CHAIN_CACHE_RETRY_EVIDENCE_REQUIRED')
                        archived.pop('evidence_content_sha256', None)
                        private_chain_evidence(archived)
                validate_provider_success_binding(evidence,
                    dict(round_, stage='SUCCESS', cached=cached),
                    round_['request'], declared['provider_payload'], cached=cached)
            else:
                raise ContractError('CHAIN_ROUND_RECEIPT_ORIGIN_INVALID')
        _same(round_.get('input_manifest'), manifest, 'CHAIN_INPUT_MANIFEST_CONTINUITY_MISMATCH')
        _same(round_.get('input_compiled'), compiled, 'CHAIN_INPUT_COMPILED_CONTINUITY_MISMATCH')
        candidates, excluded = registry_review_sources(registry, manifest, previously_included_ids=previously_included_ids)
        request = build_review_request(manifest, compiled, candidate_sources=candidates,
            excluded_sources=excluded, task_context=context, protocol_version=protocol_version,
            round_index=index, max_rounds=maximum, previous_round=rounds[index-2] if (current or previous_v03) and index > 1 else None)
        _same(round_.get('request'), request, 'CHAIN_REQUEST_RECONSTRUCTION_MISMATCH')
        _same(round_.get('request_sha256'), canonical_sha256(request), 'CHAIN_REQUEST_HASH_MISMATCH')
        _same(round_.get('request_binding_sha256'), request['response_binding']['request_sha256'],
              'CHAIN_REQUEST_BINDING_HASH_MISMATCH')
        _same(round_.get('source_registry_sha256'), chain['source_registry_sha256'], 'CHAIN_ROUND_REGISTRY_MISMATCH')
        plan = validate_material_review(manifest, compiled, round_.get('review'),
            candidate_sources=candidates, excluded_sources=excluded, task_context=context,
            expected_request=request, protocol_version=protocol_version, round_index=index,
            max_rounds=maximum, previous_round=rounds[index-2] if (current or previous_v03) and index > 1 else None)
        for key in ('manifest', 'compiled', 'ledger', 'review', 'changed', 'input_compiled_sha256',
                    'input_manifest_sha256', 'compiled_changed_requires_new_review',
                    'human_review_created', 'clinical_release_permitted', 'event_graph_modified',
                    'application_receipt'):
            _same(round_.get(key), plan.get(key), 'CHAIN_REPLAY_' + key.upper() + '_MISMATCH')
        _same(round_.get('diff'), _diff(compiled, plan['compiled']), 'CHAIN_DIFF_REPLAY_MISMATCH')
        if plan['review']['status'] == 'AUTO_REVIEW_REVISE' and not _diff(compiled, plan['compiled'])['changed'] \
                and not _new_source_resolution_pending(compiled, rounds[index-2] if (current or previous_v03) and index > 1 else None):
            raise ContractError('CHAIN_REVISE_REQUIRES_ACTUAL_NEW_CONTENT')
        if (current or previous_v03) and plan['review']['status'] == 'AUTO_REVIEW_REVISE' \
                and not (_diff(compiled, plan['compiled'])['doctor_visible_material_changed']
                         or _diff(compiled, plan['compiled'])['source_scope_changed']) \
                and not _new_source_resolution_pending(compiled, rounds[index-2] if (current or previous_v03) and index > 1 else None):
            raise ContractError('CHAIN_REVISE_REQUIRES_DOCTOR_VISIBLE_OR_SOURCE_SCOPE_CHANGE')
        manifest, compiled = plan['manifest'], plan['compiled']
        previously_included_ids.update(s['source_id'] for s in manifest['sources'])
        ledger = plan['ledger']
        status = plan['review']['status']
        if (ledger['unresolved_sources'] or ledger['graph_issues'] or ledger['human_requests']
                or ledger['holds'] or plan['review']['required_actions'] or status == 'AUTO_REVIEW_HOLD'):
            expected_stop = 'REVIEW_HOLD'
        elif status == 'AUTO_REVIEW_PASS':
            expected_stop = 'AUTO_PASS'
        else:
            digest = _content_signature(compiled)
            if digest in seen and not _state_is_new_source_pending(compiled,
                    rounds[index-2] if (current or previous_v03) and index > 1 else None):
                expected_stop = 'REVISION_CYCLE'
            seen[digest] = index
            if expected_stop is None and index == maximum:
                expected_stop = 'BUDGET_EXHAUSTED'
    failure = chain.get('failure')
    if failure is not None:
        if expected_stop is not None or len(rounds) >= maximum or not isinstance(failure, dict):
            raise ContractError('CHAIN_FAILURE_AFTER_TERMINAL_STATE')
        if failure.get('round_index') != len(rounds) + 1:
            raise ContractError('CHAIN_FAILURE_ROUND_MISMATCH')
        candidates, excluded = registry_review_sources(registry, manifest, previously_included_ids=previously_included_ids)
        request = build_review_request(manifest, compiled, candidate_sources=candidates,
            excluded_sources=excluded, task_context=context, protocol_version=protocol_version,
            round_index=len(rounds)+1, max_rounds=maximum,
            previous_round=rounds[-1] if (current or previous_v03) and rounds else None)
        _same(failure.get('request'), request, 'CHAIN_FAILURE_REQUEST_MISMATCH')
        _same(failure.get('request_sha256'), canonical_sha256(request), 'CHAIN_FAILURE_REQUEST_HASH_MISMATCH')
        if current:
            _same(failure.get('request_binding_sha256'), request['response_binding']['request_sha256'],
                  'CHAIN_FAILURE_BINDING_HASH_MISMATCH')
        if current:
            from .review import _transport_binding
            declared = _transport_binding(request,
                projection_format=failure.get('transport_projection_format'))
            _same(failure.get('declared_provider_payload_sha256'),
                declared['transport']['declared_provider_payload_sha256'], 'CHAIN_FAILURE_PAYLOAD_HASH_MISMATCH')
        else:
            declared = None
        if not all(isinstance(failure.get(key), str) and failure[key] for key in ('stage', 'error_type', 'reason_code')):
            raise ContractError('CHAIN_FAILURE_REASON_REQUIRED')
        if failure.get('program_diagnostics') is not None:
            from scripts.cohort_construction.p2_auto_checkpoint_v04.provider_diagnostics import diagnostics_for
            try:
                validate_material_review(manifest, compiled, failure.get('response'),
                    candidate_sources=candidates, excluded_sources=excluded, task_context=context,
                    expected_request=request, protocol_version=protocol_version,
                    round_index=len(rounds)+1, max_rounds=maximum,
                    previous_round=rounds[-1] if (current or previous_v03) and rounds else None)
            except Exception as exc:
                _same(failure['program_diagnostics'], diagnostics_for(exc),
                    'CHAIN_FAILURE_DIAGNOSTIC_REPLAY_MISMATCH')
            else:
                raise ContractError('CHAIN_FAILURE_DIAGNOSTIC_RESPONSE_DID_NOT_FAIL')
        local_budget = (failure['stage'] == 'LOCAL_BUDGET_CONTROL'
            and failure['error_type'] == 'LocalModelBudgetHold'
            and failure['reason_code'] == 'ASSEMBLY_MODEL_CALL_BUDGET_REACHED'
            and failure.get('receipt') == dict(cached=False,
                status='LOCAL_BUDGET_DENIED', provider_invocation_occurred=False,
                receipt_origin='LOCAL_CONTROL', provider_transport_verified=False))
        if local_budget:
            if not current or 'provider_attempt_evidence' in failure \
                    or 'response' in failure or 'program_diagnostics' in failure:
                raise ContractError('CHAIN_LOCAL_BUDGET_CONTROL_MISMATCH')
        elif failure['stage'] == 'LOCAL_BUDGET_CONTROL':
            raise ContractError('CHAIN_LOCAL_BUDGET_CONTROL_MISMATCH')
        elif failure['stage'] != 'RESPONSE_VALIDATION':
            if current and failure.get('provider_attempt_evidence') is not None:
                from scripts.cohort_construction.p2_auto_checkpoint_v04.provider_diagnostics import (
                    replay_transport_evidence, validate_private_chain_evidence,
                    validate_provider_attempt_binding,
                )
                from .review import MATERIAL_REVIEW_SCHEMA
                evidence = failure.get('provider_attempt_evidence')
                validate_private_chain_evidence(evidence, evidence)
                if failure['receipt'].get('receipt_origin') != 'PROVIDER_CLIENT':
                    raise ContractError('CHAIN_PROVIDER_RECEIPT_ORIGIN_INVALID')
                validate_provider_attempt_binding(evidence, failure)
                replay_transport_evidence(evidence, MATERIAL_REVIEW_SCHEMA)
                if failure['receipt'].get('declared_provider_payload_sha256') \
                        != declared['transport']['declared_provider_payload_sha256']:
                    raise ContractError('CHAIN_FAILURE_RECEIPT_PAYLOAD_MISMATCH')
            elif 'provider_attempt_evidence' in failure:
                raise ContractError('CHAIN_LEGACY_PROVIDER_EVIDENCE_REQUIRES_REVIEW')
            elif current and failure.get('receipt', {}).get('receipt_origin') == 'SYNTHETIC_ADAPTER':
                # Explicitly unverified offline adapter outcome. Full request,
                # declared payload and local HOLD remain replayable; no provider
                # invocation or transport authenticity is asserted.
                receipt = failure['receipt']
                if (receipt.get('provider_invocation_occurred') is not False
                        or receipt.get('provider_transport_verified') is not False
                        or receipt.get('declared_provider_payload_sha256')
                        != declared['transport']['declared_provider_payload_sha256']):
                    raise ContractError('CHAIN_SYNTHETIC_FAILURE_PROVENANCE_MISMATCH')
            elif current:
                raise ContractError('CHAIN_PROVIDER_ATTEMPT_EVIDENCE_REQUIRED')
        elif failure.get('provider_attempt_evidence') is not None:
            raise ContractError('CHAIN_RESPONSE_FAILURE_PROVIDER_EVIDENCE_REQUIRES_REVIEW')
        if failure['stage'] == 'RESPONSE_VALIDATION':
            if 'response' not in failure:
                if evidence is None or not isinstance(evidence.get('response'), dict):
                    raise ContractError('CHAIN_FAILURE_RESPONSE_REQUIRED')
                failure = deepcopy(failure)
                failure['response'] = deepcopy(evidence['response'])
            try:
                failed_plan = validate_material_review(manifest, compiled, failure['response'],
                    candidate_sources=candidates, excluded_sources=excluded, task_context=context,
                    expected_request=request, protocol_version=protocol_version,
                    round_index=len(rounds)+1, max_rounds=maximum,
                    previous_round=rounds[-1] if (current or previous_v03) and rounds else None)
                if (failed_plan['review']['status'] == 'AUTO_REVIEW_REVISE'
                        and not _diff(compiled, failed_plan['compiled'])['changed']
                        and not _new_source_resolution_pending(compiled, rounds[-1] if (current or previous_v03) and rounds else None)):
                    raise MaterialReviewError('REVISE_REQUIRES_ACTUAL_NEW_CONTENT')
                if (current or previous_v03) and failed_plan['review']['status'] == 'AUTO_REVIEW_REVISE' \
                        and not (_diff(compiled, failed_plan['compiled'])['doctor_visible_material_changed']
                                 or _diff(compiled, failed_plan['compiled'])['source_scope_changed']) \
                        and not _new_source_resolution_pending(compiled, rounds[-1] if rounds else None):
                    raise MaterialReviewError('REVISE_REQUIRES_DOCTOR_VISIBLE_OR_SOURCE_SCOPE_CHANGE')
            except Exception as exc:
                code = str(exc) if isinstance(exc, MaterialReviewError) else type(exc).__name__
                _same(failure['error_type'], type(exc).__name__, 'CHAIN_FAILURE_ERROR_TYPE_MISMATCH')
                _same(failure['reason_code'], code, 'CHAIN_FAILURE_REASON_REPLAY_MISMATCH')
            else:
                raise ContractError('CHAIN_FAILURE_RESPONSE_DID_NOT_FAIL_REPLAY')
        elif not local_budget and (failure['stage'] not in ('PROVIDER_REQUEST', 'PROVIDER_HTTP_RESPONSE',
                'PROVIDER_ENVELOPE_PARSE', 'PROVIDER_CONTENT_PARSE', 'RESPONSE_SCHEMA_VALIDATION')
                or ('response' in failure and failure['stage'] != 'RESPONSE_SCHEMA_VALIDATION')
                or (not current and 'provider_attempt_evidence' not in failure)):
            raise ContractError('CHAIN_FAILURE_STAGE_MISMATCH')
        expected_stop = 'EXECUTION_FAILED'
    if expected_stop is None:
        raise ContractError('CHAIN_NONTERMINAL_OR_EMPTY_REVIEW_NOT_ACCEPTED')
    _same(chain.get('stop_reason'), expected_stop, 'CHAIN_TERMINAL_REASON_MISMATCH')
    _same(chain.get('state'), 'AUTO_REVIEW_PASS' if expected_stop == 'AUTO_PASS' else 'AUTO_REVIEW_HOLD',
          'CHAIN_TERMINAL_STATE_MISMATCH')
    _same(chain.get('manifest'), manifest, 'CHAIN_FINAL_MANIFEST_MISMATCH')
    _same(chain.get('compiled'), compiled, 'CHAIN_FINAL_COMPILED_MISMATCH')
    view = doctor_view(compiled)
    _same(chain.get('doctor_view'), view, 'CHAIN_FINAL_DOCTOR_VIEW_MISMATCH')
    binding = dict(manifest_sha256=canonical_sha256(manifest), compiled_sha256=compiled['compiled_sha256'],
        doctor_view_sha256=view['view_sha256'], source_registry_sha256=chain['source_registry_sha256'], round_count=len(rounds))
    _same(chain.get('final_binding'), binding, 'CHAIN_FINAL_BINDING_MISMATCH')
    return dict(format='material-review-chain-validation-0.2.0', chain_sha256=canonical_sha256(chain),
        round_count=len(rounds), state=chain['state'], final_binding=binding,
        candidate_graph_binding=graph_binding, deterministic_replay_verified=True,
        clinical_visibility_certified=False, clinical_release_permitted=False)
