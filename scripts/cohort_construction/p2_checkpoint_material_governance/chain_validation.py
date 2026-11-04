"""Authoritative replay of immutable material review chains (no model calls).

Checksums prove internal identity, not authorship or clinical visibility. The
request/revision implementation is loaded only inside validation so review.py
never imports this module and no import cycle is introduced.
"""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError, canonical_sha256

CHAIN_FORMAT = 'automated-material-review-chain-0.2.0'


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
    from .review import (AUTO_PROTOCOL_VERSION, MaterialReviewError, build_review_request,
                         build_source_registry, registry_review_sources,
                         validate_material_review, doctor_view, _content_signature, _diff)
    if not isinstance(chain, dict) or chain.get('format') != CHAIN_FORMAT:
        raise ContractError('CHAIN_VERSION_REQUIRES_REPLAYABLE_V02')
    from .schema import validate_schema
    validate_schema('AutomatedMaterialReviewChain', chain)
    _same(chain.get('protocol_version'), AUTO_PROTOCOL_VERSION, 'CHAIN_PROTOCOL_MISMATCH')
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
        _same(round_.get('input_manifest'), manifest, 'CHAIN_INPUT_MANIFEST_CONTINUITY_MISMATCH')
        _same(round_.get('input_compiled'), compiled, 'CHAIN_INPUT_COMPILED_CONTINUITY_MISMATCH')
        candidates, excluded = registry_review_sources(registry, manifest, previously_included_ids=previously_included_ids)
        request = build_review_request(manifest, compiled, candidate_sources=candidates,
            excluded_sources=excluded, task_context=context)
        _same(round_.get('request'), request, 'CHAIN_REQUEST_RECONSTRUCTION_MISMATCH')
        _same(round_.get('request_sha256'), canonical_sha256(request), 'CHAIN_REQUEST_HASH_MISMATCH')
        _same(round_.get('request_binding_sha256'), request['response_binding']['request_sha256'],
              'CHAIN_REQUEST_BINDING_HASH_MISMATCH')
        _same(round_.get('source_registry_sha256'), chain['source_registry_sha256'], 'CHAIN_ROUND_REGISTRY_MISMATCH')
        plan = validate_material_review(manifest, compiled, round_.get('review'),
            candidate_sources=candidates, excluded_sources=excluded, task_context=context,
            expected_request=request)
        for key in ('manifest', 'compiled', 'ledger', 'review', 'changed', 'input_compiled_sha256',
                    'input_manifest_sha256', 'compiled_changed_requires_new_review',
                    'human_review_created', 'clinical_release_permitted', 'event_graph_modified',
                    'application_receipt'):
            _same(round_.get(key), plan.get(key), 'CHAIN_REPLAY_' + key.upper() + '_MISMATCH')
        _same(round_.get('diff'), _diff(compiled, plan['compiled']), 'CHAIN_DIFF_REPLAY_MISMATCH')
        if plan['review']['status'] == 'AUTO_REVIEW_REVISE' and not _diff(compiled, plan['compiled'])['changed']:
            raise ContractError('CHAIN_REVISE_REQUIRES_ACTUAL_NEW_CONTENT')
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
            if digest in seen:
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
            excluded_sources=excluded, task_context=context)
        _same(failure.get('request'), request, 'CHAIN_FAILURE_REQUEST_MISMATCH')
        _same(failure.get('request_sha256'), canonical_sha256(request), 'CHAIN_FAILURE_REQUEST_HASH_MISMATCH')
        if not all(isinstance(failure.get(key), str) and failure[key] for key in ('stage', 'error_type', 'reason_code')):
            raise ContractError('CHAIN_FAILURE_REASON_REQUIRED')
        if failure['stage'] == 'RESPONSE_VALIDATION':
            if 'response' not in failure:
                raise ContractError('CHAIN_FAILURE_RESPONSE_REQUIRED')
            try:
                failed_plan = validate_material_review(manifest, compiled, failure['response'],
                    candidate_sources=candidates, excluded_sources=excluded, task_context=context,
                    expected_request=request)
                if (failed_plan['review']['status'] == 'AUTO_REVIEW_REVISE'
                        and not _diff(compiled, failed_plan['compiled'])['changed']):
                    raise MaterialReviewError('REVISE_REQUIRES_ACTUAL_NEW_CONTENT')
            except Exception as exc:
                code = str(exc) if isinstance(exc, MaterialReviewError) else type(exc).__name__
                _same(failure['error_type'], type(exc).__name__, 'CHAIN_FAILURE_ERROR_TYPE_MISMATCH')
                _same(failure['reason_code'], code, 'CHAIN_FAILURE_REASON_REPLAY_MISMATCH')
            else:
                raise ContractError('CHAIN_FAILURE_RESPONSE_DID_NOT_FAIL_REPLAY')
        elif failure['stage'] != 'PROVIDER_REQUEST' or 'response' in failure:
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
