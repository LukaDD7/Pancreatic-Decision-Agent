"""Independent automated review of exact original material and its doctor view.

The LLM supplies observations, bounded operations and uncertainty. It never
writes offsets or changes the event graph. This module resolves exact quotes,
recompiles new versions and requires a new review for every changed hash.
"""
from copy import deepcopy

from jsonschema import Draft202012Validator

from scripts.cohort_construction.p2_checkpoint_material_governance.schema import validate_schema

from scripts.cohort_construction.p2_checkpoint_packet.compiler import (
    CONTRACT_VERSION, ContractError, PLACEHOLDER, _verified_sources,
    build_source_manifest, canonical_sha256, compile_partition, locate_exact_quote,
    project_doctor_materials, text_sha256,
)
from scripts.cohort_construction.p2_checkpoint_packet.semantic_audit import _verified_compiled
from scripts.cohort_construction.p2_auto_checkpoint_v04.provider_diagnostics import (
    diagnostics_for, private_chain_evidence, reason_for,
)

AUTO_PROTOCOL_VERSION = 'checkpoint-automated-material-review-0.3.0'
LEGACY_PROTOCOL_VERSIONS = ('checkpoint-automated-material-review-0.2.0',)
REVIEW_RESPONSE_FORMAT = 'material-review-0.3.0'
CHAIN_FORMAT = 'automated-material-review-chain-0.4.0'
TASK = 'checkpoint_material_full_source_and_doctor_view_review'
DIMENSIONS = ('scope', 'target', 'leakage', 'evidence', 'boundaries', 'time', 'evaluability')
APPLICABLE = ('APPLICABLE', 'NOT_APPLICABLE', 'UNKNOWN')
OPERATIONS = ('SET_DISPOSITION', 'INCLUDE_SOURCE', 'EXCLUDE_SOURCE',
              'MARK_SOURCE_UNRESOLVED', 'FLAG_EVENT_GRAPH_ISSUE',
              'REQUEST_HUMAN_REVIEW', 'HOLD_PACKET')
DISPOSITIONS = ('KEEP', 'MASK', 'REVIEW')


class MaterialReviewError(ContractError):
    pass


def _transport_binding(request, *, projection_format='FULL_REQUEST', projection=None):
    """Declare the exact provider payload separately from the persisted request.

    ``FULL_REQUEST`` preserves the historical default. Compact transport is an
    explicit registered projection and never silently replaces the full request.
    """
    from .projection import FORMAT as COMPACT_FORMAT, compact_material_review_request
    # Callers without a precomputed projection use the default full request.
    # Compact callers must supply the exact registered projection and are
    # checked below; the legacy initial/default validation cannot omit it.
    if projection_format == 'FULL_REQUEST':
        if projection is not None:
            raise MaterialReviewError('TRANSPORT_FULL_REQUEST_PROJECTION_UNEXPECTED')
        payload = deepcopy(request)
    elif projection_format == COMPACT_FORMAT:
        payload = compact_material_review_request(request)
        if projection is not None and projection != payload:
            raise MaterialReviewError('TRANSPORT_DECLARED_PROJECTION_MISMATCH')
    else:
        raise MaterialReviewError('TRANSPORT_PROJECTION_FORMAT_UNSUPPORTED')
    return dict(provider_payload=deepcopy(payload), transport=dict(
        transport_projection_format=projection_format,
        declared_provider_payload_sha256=canonical_sha256(payload)))


def _synthetic_receipt(receipt, transport):
    """Make offline adapter provenance explicit; do not invent provider proof."""
    receipt = deepcopy(receipt) if isinstance(receipt, dict) else {}
    origin = receipt.get('receipt_origin', 'SYNTHETIC_ADAPTER')
    if origin != 'SYNTHETIC_ADAPTER':
        raise MaterialReviewError('TRANSPORT_RECEIPT_ORIGIN_INVALID')
    receipt.update(receipt_origin='SYNTHETIC_ADAPTER',
        provider_invocation_occurred=False, provider_transport_verified=False,
        transport_projection_format=transport['transport_projection_format'],
        declared_provider_payload_sha256=transport['declared_provider_payload_sha256'])
    return receipt


def _client_receipt(receipt, transport, *, success):
    """Bind real Client receipt domains to the payload actually declared here."""
    cached = isinstance(receipt, dict) and receipt.get('receipt_origin') == 'PROVIDER_CLIENT_CACHE'
    if not isinstance(receipt, dict) \
            or receipt.get('receipt_origin') not in ('PROVIDER_CLIENT', 'PROVIDER_CLIENT_CACHE') \
            or receipt.get('declared_provider_payload_sha256') \
            != transport['declared_provider_payload_sha256'] \
            or receipt.get('provider_invocation_occurred') is not (not cached) \
            or receipt.get('cached') is not cached:
        raise MaterialReviewError('TRANSPORT_PROVIDER_RECEIPT_PAYLOAD_MISMATCH')
    if success and (receipt.get('status') != 'SUCCESS'
                    or receipt.get('provider_transport_verified') is not (not cached)):
        raise MaterialReviewError('TRANSPORT_PROVIDER_SUCCESS_RECEIPT_INVALID')
    if not success and (receipt.get('status') != 'FAILED'
                        or cached or receipt.get('provider_transport_verified') is not False):
        raise MaterialReviewError('TRANSPORT_PROVIDER_FAILURE_RECEIPT_INVALID')
    result = deepcopy(receipt)
    result['transport_projection_format'] = transport['transport_projection_format']
    return result


def _round_transport(request, receipt, *, success, failure=False):
    """Return one replayable transport record, including local non-invocation."""
    transport = _transport_binding(request,
        projection_format=receipt.get('transport_projection_format', 'FULL_REQUEST'))
    if receipt.get('receipt_origin') == 'SYNTHETIC_ADAPTER':
        receipt = _synthetic_receipt(receipt, transport)
    elif receipt.get('receipt_origin') in ('PROVIDER_CLIENT', 'PROVIDER_CLIENT_CACHE'):
        receipt = _client_receipt(receipt, transport, success=success)
    elif receipt.get('status') == 'LOCAL_BUDGET_DENIED' \
            and receipt.get('provider_invocation_occurred') is False \
            and receipt.get('cached') is False:
        receipt = deepcopy(receipt)
        receipt.update(receipt_origin='LOCAL_CONTROL',
            provider_transport_verified=False, **transport['transport'])
    else:
        raise MaterialReviewError('TRANSPORT_RECEIPT_ORIGIN_REQUIRED')
    evidence = receipt.pop('terminal_attempt_evidence', None)
    result = dict(request=request, request_sha256=canonical_sha256(request),
        request_binding_sha256=request['response_binding']['request_sha256'],
        receipt=receipt, **transport['transport'])
    if evidence is not None:
        result['provider_attempt_evidence'] = evidence
    return result


def doctor_view(compiled):
    """The one source projection shared with packet construction and preview."""
    materials = [dict(source_id=m['source_id'], source_version=m['source_version'],
                      text=m['text'], sha256=m['sha256'])
                 for m in project_doctor_materials(compiled)]
    result = dict(format='checkpoint-doctor-view-preview-0.1.0',
                  target=deepcopy(compiled['target']),
                  compiled_sha256=compiled['compiled_sha256'],
                  materials=materials, researcher_original_included=False)
    result['view_sha256'] = canonical_sha256(result)
    return result


def _validate_schema(value, schema):
    try:
        Draft202012Validator(schema).validate(value)
    except Exception as exc:
        raise MaterialReviewError('MATERIAL_REVIEW_SCHEMA_INVALID') from exc


def _catalog(compiled):
    catalog = []
    for source in compiled['sources']:
        for span in source['segments']:
            catalog.append(dict(source_id=source['source_id'], start=span['start'],
                                end=span['end'], disposition=span['disposition'],
                                exact_quote=span['text']))
    return catalog


def build_review_request(manifest, compiled, *, candidate_sources=(),
                         excluded_sources=(), protocol_version=AUTO_PROTOCOL_VERSION,
                         task_context=None, round_index=1, max_rounds=3,
                         previous_round=None):
    """Provide full researcher text and the actual projected doctor view.

    Source metadata and model evidence are researcher context, never clinical
    assertions. Only explicitly supplied candidate source text can be included.
    """
    if protocol_version != AUTO_PROTOCOL_VERSION and protocol_version not in LEGACY_PROTOCOL_VERSIONS:
        raise MaterialReviewError('UNSUPPORTED_MATERIAL_REVIEW_PROTOCOL')
    if type(round_index) is not int or type(max_rounds) is not int \
            or not 1 <= round_index <= max_rounds <= 20:
        raise MaterialReviewError('REVIEW_ROUND_CONTEXT_INVALID')
    if previous_round is not None and (protocol_version in LEGACY_PROTOCOL_VERSIONS or round_index == 1):
        raise MaterialReviewError('LEGACY_OR_INITIAL_REVIEW_CANNOT_HAVE_PREVIOUS_ROUND')
    source_map = _verified_sources(manifest)
    _verified_compiled(compiled)
    if manifest['manifest_id'] != compiled['manifest_id']:
        raise MaterialReviewError('REVIEW_INPUT_MANIFEST_MISMATCH')
    if {s['source_id'] for s in compiled['sources']} != set(source_map):
        raise MaterialReviewError('REVIEW_INPUT_SOURCE_SCOPE_MISMATCH')
    for source in compiled['sources']:
        original = source_map[source['source_id']]
        if (source['source_version'] != original['source_version']
                or source['source_sha256'] != original['sha256']
                or source['length_codepoints'] != original['length_codepoints']
                or ''.join(span['text'] for span in source['segments']) != original['text']):
            raise MaterialReviewError('REVIEW_INPUT_SOURCE_CONTENT_BINDING_MISMATCH')
    candidates, excluded = {}, {}
    for collection, name in ((candidate_sources, 'candidate'), (excluded_sources, 'excluded')):
        destination = candidates if name == 'candidate' else excluded
        for source in collection:
            sid = source.get('source_id')
            text = source.get('text')
            version = source.get('source_version')
            if not all(isinstance(v, str) and v for v in (sid, text, version)):
                raise MaterialReviewError('REVIEW_SOURCE_ID_VERSION_TEXT_REQUIRED')
            if sid in destination:
                raise MaterialReviewError('DUPLICATE_REVIEW_SOURCE')
            digest = text_sha256(text)
            if source.get('sha256', source.get('source_sha256', digest)) != digest:
                raise MaterialReviewError('REVIEW_SOURCE_HASH_MISMATCH')
            destination[sid] = dict(source_id=sid, source_version=version,
                                    text=text, sha256=digest,
                                    scope_status='CANDIDATE_NOT_INCLUDED',
                                    supplied_by_research_scope=True)
    if set(candidates) & set(excluded) or set(candidates) & set(source_map) or set(excluded) & set(source_map):
        raise MaterialReviewError('REVIEW_SOURCE_SCOPES_OVERLAP')
    for collection in (candidates, excluded):
        for source in collection.values():
            source['scope_status'] = 'EXCLUDED_REVIEW_SOURCE' if collection is excluded else 'CANDIDATE_NOT_INCLUDED'
    if task_context is not None and not isinstance(task_context, dict):
        raise MaterialReviewError('REVIEW_TASK_CONTEXT_MUST_BE_OBJECT')
    history = _previous_round_history(previous_round, round_index) if previous_round is not None else None
    requirements = ['Do not infer absent text or negative findings.',
                    'Do not rewrite original clinical text or supply character offsets.',
                    'Use exact quotes for interval changes; SPLIT_SPAN is executed by SET_DISPOSITION.',
                    'Any changed partition requires a new review; automatic PASS is not human or clinical approval.']
    if history is not None:
        requirements += [
            'This is a continuation, not a first review. previous_round contains your own previous review, the operations proposed by it, and the deterministic program execution receipts.',
            'The current dispositions and doctor view are the result of your previous recommendations plus program execution. The current doctor_view_sha256 and compiled_sha256 are new bindings; use them, not the previous hashes.',
            'Address every previous OPEN finding explicitly in findings using the same finding_id, with RESOLVED/OPEN/NOT_APPLICABLE and a source-grounded reason. Do not reopen a closed issue or reverse a previous disposition without new original-text evidence or a specific execution defect.',
            'MASK is correctly absent from doctor_view. REVIEW is not doctor-visible by default. A MASK to REVIEW change may alter compiled_sha256 without changing doctor-visible text; do not call normal projection a partition/view inconsistency.',
            'A REVISE response must change doctor-visible material or the included source scope. INCLUDE_SOURCE/EXCLUDE_SOURCE may legitimately change only researcher-only material in this round, including a newly included source that starts as REVIEW. Do not spend a round changing only internal REVIEW/MASK state within the same source scope; use HOLD for an unresolved research-only issue, or PASS only if all current requirements are resolved.',
            'If your new review restores a previous doctor view after changing it, do not oscillate. Explain the source-grounded reason; the program detects repeated states and stops rather than silently PASSing.']
    request = dict(task=TASK, protocol_version=protocol_version,
                instructions=dict(
                    task='Read all included original text and all supplied candidate/excluded sources; compare with the doctor view for this target Checkpoint.',
                    requirements=requirements),
                target=deepcopy(compiled['target']),
                review_round=dict(round_index=round_index, max_rounds=max_rounds,
                    is_first_review=history is None,
                    current_state_produced_by_previous_review=history is not None),
                task_context=deepcopy(task_context) if task_context is not None else dict(
                    decision_time_evidence='UNRESOLVED', clinical_visibility_certified=False),
                researcher_sources=[dict(source_id=s['source_id'], source_version=s['source_version'],
                                         sha256=s['sha256'], text=s['text']) for s in manifest['sources']],
                candidate_sources=list(candidates.values()), excluded_sources=list(excluded.values()),
                current_dispositions=_catalog(compiled), doctor_view=doctor_view(compiled),
                graph_is_candidate_not_clinical_reference=True)
    if history is not None:
        request['previous_round'] = history
    if protocol_version in LEGACY_PROTOCOL_VERSIONS:
        request.pop('review_round')
        request['instructions']['requirements'] = request['instructions']['requirements'][:4]
    # This hash covers material payload only, excluding its own echo envelope.
    # Provider transport hashes (including model/system/schema) live in receipts.
    request['response_binding'] = dict(protocol_version=protocol_version,
        compiled_sha256=compiled['compiled_sha256'],
        doctor_view_sha256=request['doctor_view']['view_sha256'],
        request_sha256=canonical_sha256(request))
    return request


def build_source_registry(manifest, *, candidate_sources=(), excluded_sources=()):
    """Freeze every supplied source, including sources subsequently excluded."""
    # Reuse exact source validation including scope exclusivity without a model.
    registry, seen = [], set()
    for scope, records in (('INCLUDED', _verified_sources(manifest).values()),
                           ('CANDIDATE', candidate_sources), ('EXCLUDED', excluded_sources)):
        for source in records:
            sid, version, text = source.get('source_id'), source.get('source_version'), source.get('text')
            if not all(isinstance(v, str) and v for v in (sid, version, text)):
                raise MaterialReviewError('REVIEW_SOURCE_ID_VERSION_TEXT_REQUIRED')
            if sid in seen:
                raise MaterialReviewError('REVIEW_SOURCE_SCOPES_OVERLAP')
            seen.add(sid)
            digest = text_sha256(text)
            if source.get('sha256', source.get('source_sha256', digest)) != digest:
                raise MaterialReviewError('REVIEW_SOURCE_HASH_MISMATCH')
            registry.append(dict(source_id=sid, source_version=version, text=text,
                                 sha256=digest, origin_scope=scope))
    return registry


def registry_review_sources(registry, manifest, *, previously_included_ids=()):
    included = set(_verified_sources(manifest))
    previously_included = set(previously_included_ids)
    candidates = [deepcopy(s) for s in registry
                  if s['source_id'] not in included and s['origin_scope'] == 'CANDIDATE'
                  and s['source_id'] not in previously_included]
    excluded = [deepcopy(s) for s in registry
                if s['source_id'] not in included and (s['origin_scope'] != 'CANDIDATE'
                    or s['source_id'] in previously_included)]
    return candidates, excluded


def _material_review(review, compiled, request):
    expected_fields = {'format', 'review_id', 'target', 'protocol_version', 'compiled_sha256',
        'doctor_view_sha256', 'request_sha256', 'reviewer_type', 'dimensions',
        'findings', 'operations', 'status', 'required_actions'}
    if request['protocol_version'] == AUTO_PROTOCOL_VERSION:
        expected_fields.add('review_round')
    if not isinstance(review, dict) or set(review) != expected_fields:
        raise MaterialReviewError('MATERIAL_REVIEW_FIELDS_MISMATCH')
    if request['protocol_version'] == AUTO_PROTOCOL_VERSION \
            and review.get('review_round') != request['review_round']:
        raise MaterialReviewError('MATERIAL_REVIEW_ROUND_BINDING_MISMATCH')
    _validate_schema(review, _review_schema(request['protocol_version']))
    if (review['target'] != compiled['target']
            or review['compiled_sha256'] != compiled['compiled_sha256']
            or review['doctor_view_sha256'] != request['doctor_view']['view_sha256']
            or review['request_sha256'] != request['response_binding']['request_sha256']
            or review['protocol_version'] != request['protocol_version']):
        raise MaterialReviewError('MATERIAL_REVIEW_INPUT_BINDING_MISMATCH')
    if review['reviewer_type'] != 'llm_material_review':
        raise MaterialReviewError('AUTOMATED_REVIEWER_TYPE_REQUIRED')
    dimensions = [item['dimension'] for item in review['dimensions']]
    if len(dimensions) != len(set(dimensions)) or set(dimensions) != set(DIMENSIONS):
        raise MaterialReviewError('MATERIAL_REVIEW_DIMENSIONS_MISMATCH')
    findings, operations = set(), set()
    for item in review['findings']:
        if item['finding_id'] in findings:
            raise MaterialReviewError('DUPLICATE_MATERIAL_FINDING_ID')
        findings.add(item['finding_id'])
    for item in review['operations']:
        if item['operation_id'] in operations:
            raise MaterialReviewError('DUPLICATE_MATERIAL_OPERATION_ID')
        if (item['operation'] == 'SET_DISPOSITION'
                and ('exact_quote' not in item or 'disposition' not in item)):
            raise MaterialReviewError('DISPOSITION_REQUIRES_EXACT_QUOTE_AND_DISPOSITION')
        operations.add(item['operation_id'])
    return review


def _source_record(source):
    return dict(source_id=source['source_id'], source_version=source['source_version'], text=source['text'])


def _revise(manifest, compiled, review, candidate_sources, excluded_sources):
    """Deterministically apply permitted operations; never mutate old objects."""
    source_map = _verified_sources(manifest)
    sources = {sid: _source_record(source) for sid, source in source_map.items()}
    candidates = {s['source_id']: s for s in list(candidate_sources) + list(excluded_sources)}
    for source in candidates.values():
        if text_sha256(source['text']) != source.get('sha256', source.get('source_sha256', text_sha256(source['text']))):
            raise MaterialReviewError('REVISION_CANDIDATE_HASH_MISMATCH')
    segments = [dict(segment_id='MR-' + canonical_sha256([review['review_id'], source['source_id'],
                       span['start'], span['end'], span['disposition']])[:24],
                     source_id=source['source_id'], disposition=span['disposition'],
                     resolved_span=dict(start=span['start'], end=span['end'], exact_quote=span['text']))
                for source in compiled['sources'] for span in source['segments']]
    operations, reasons, flags, human, holds = [], [], [], [], []
    # Resolve every interval against the same input version before changing any
    # partition. A quote may cross existing REVIEW/MASK boundaries. Operations
    # in one response form one atomic plan, not a sequence of new model states.
    pending_intervals = []
    for operation in review['operations']:
        if operation['operation'] != 'SET_DISPOSITION':
            continue
        sid = operation['source_id']
        if sid not in source_map or operation['disposition'] not in DISPOSITIONS:
            raise MaterialReviewError('INVALID_DISPOSITION_OPERATION')
        match = locate_exact_quote(manifest, sid, operation['exact_quote'],
                                   operation.get('prefix'), operation.get('suffix'))
        if match['status'] != 'UNIQUE':
            raise MaterialReviewError('DISPOSITION_QUOTE_NOT_UNIQUE')
        anchor = match['anchors'][0]
        pending_intervals.append(dict(operation=operation, anchor=anchor,
            start=anchor['start'], end=anchor['end'], status='PENDING_ATOMIC_PLAN'))
    for index, left in enumerate(pending_intervals):
        for right in pending_intervals[index + 1:]:
            if (left['operation']['source_id'] == right['operation']['source_id']
                    and left['start'] < right['end'] and right['start'] < left['end']
                    and left['operation']['disposition'] != right['operation']['disposition']):
                raise MaterialReviewError('DISPOSITION_ATOMIC_PLAN_CONFLICT')
    # Later source-scope operations control inclusion. Excluding a source makes
    # its proposed interval a recorded no-op, never a cross-version relocation.
    for operation in review['operations']:
        if operation['operation'] == 'EXCLUDE_SOURCE' and operation['source_id'] in source_map:
            for interval in pending_intervals:
                if interval['operation']['source_id'] == operation['source_id']:
                    interval['status'] = 'SOURCE_EXCLUDED_IN_ATOMIC_PLAN'
    for operation in review['operations']:
        kind, sid = operation['operation'], operation['source_id']
        record = dict(operation=deepcopy(operation), status='REJECTED')
        if kind == 'SET_DISPOSITION':
            record['execution_scope'] = 'ATOMIC_INPUT_VERSION_PLAN'
        operations.append(record)
        if kind in ('INCLUDE_SOURCE', 'EXCLUDE_SOURCE', 'MARK_SOURCE_UNRESOLVED'):
            if sid not in sources and sid not in candidates:
                record['reason'] = 'SOURCE_NOT_SUPPLIED_TO_REVIEW'
                raise MaterialReviewError('UNAUTHORIZED_MATERIAL_SOURCE_OPERATION')
            if kind == 'INCLUDE_SOURCE':
                if sid in sources:
                    record['reason'] = 'SOURCE_ALREADY_INCLUDED'
                    continue
                sources[sid] = _source_record(candidates[sid])
                segments.extend(_whole_source_segments(candidates[sid], review['review_id']))
            elif kind == 'EXCLUDE_SOURCE':
                if sid not in sources:
                    record['reason'] = 'SOURCE_NOT_CURRENTLY_INCLUDED'
                    continue
                del sources[sid]
                segments = [s for s in segments if s['source_id'] != sid]
            else:
                reasons.append(dict(source_id=sid, reason=operation['reason'],
                                    scope='specific_source_not_patient_or_hospital'))
            record['status'] = 'APPLIED'
        elif kind == 'SET_DISPOSITION':
            interval = next(item for item in pending_intervals
                            if item['operation']['operation_id'] == operation['operation_id'])
            if interval['status'] != 'PENDING_ATOMIC_PLAN':
                record.update(status='NO_CHANGE', reason=interval['status'])
                continue
            anchor = interval['anchor']
            start, end = interval['start'], interval['end']
            target = [s for s in segments if s['source_id'] == sid
                      and s['resolved_span']['start'] <= start and end <= s['resolved_span']['end']]
            # A no-op exact interval needs no split. A requested interval may
            # cover several current segments only if their disposition is same;
            # the final deterministic union below handles those boundaries.
            same_disposition = all(s['disposition'] == operation['disposition'] for s in target)
            if same_disposition and len(target) == 1 \
                    and target[0]['resolved_span']['start'] == start \
                    and target[0]['resolved_span']['end'] == end:
                record.update(status='NO_CHANGE', reason='SAME_DISPOSITION_AND_SPAN')
                continue
            covered = [s for s in segments if s['source_id'] == sid
                       and start < s['resolved_span']['end']
                       and s['resolved_span']['start'] < end]
            if not covered:
                raise MaterialReviewError('DISPOSITION_ATOMIC_INTERVAL_OUTSIDE_SOURCE')
            replacement = []
            for old in covered:
                base = old['resolved_span']
                for a, b, disposition in (
                    (base['start'], min(start, base['end']), old['disposition']),
                    (max(start, base['start']), min(end, base['end']), operation['disposition']),
                    (max(end, base['start']), base['end'], old['disposition']),
                ):
                    if a >= b:
                        continue
                    replacement.append(dict(
                        segment_id='MR-' + canonical_sha256([review['review_id'], sid, a, b, disposition])[:24],
                        source_id=sid, disposition=disposition,
                        resolved_span=dict(start=a, end=b, exact_quote=source_map[sid]['text'][a:b])))
            first = segments.index(covered[0])
            segments[first:first + len(covered)] = replacement
            record.update(status='APPLIED', resolved_anchor=anchor,
                execution_scope='ATOMIC_INPUT_VERSION_PLAN')
        else:
            if kind == 'FLAG_EVENT_GRAPH_ISSUE':
                flags.append(deepcopy(operation))
            elif kind == 'REQUEST_HUMAN_REVIEW':
                human.append(deepcopy(operation))
            else:
                holds.append(deepcopy(operation))
            record.update(status='RECORDED_REQUIRES_EXTERNAL_ACTION', no_graph_or_permission_change=True)
    if not sources:
        raise MaterialReviewError('CANNOT_EXCLUDE_ALL_MATERIAL_SOURCES')
    new_manifest = build_source_manifest('SM-' + canonical_sha256([
        manifest['manifest_id'], review['review_id'], sorted(sources)])[:24], list(sources.values()))
    proposal = dict(schema_version=CONTRACT_VERSION,
                    proposal_id='SPL-' + canonical_sha256([review['review_id'], new_manifest])[:24],
                    proposal_version=AUTO_PROTOCOL_VERSION, manifest_id=new_manifest['manifest_id'],
                    target=deepcopy(compiled['target']),
                    source_bindings=[dict(source_id=s['source_id'], source_version=s['source_version'],
                                          sha256=s['sha256']) for s in new_manifest['sources']],
                    segments=segments)
    if not any(op['status'] == 'APPLIED' for op in operations):
        segments = [dict(segment_id=s['segment_id'], source_id=s['source_id'], disposition=s['disposition'],
                         resolved_span=deepcopy(s['resolved_span'])) for s in segments]
        new_manifest = deepcopy(manifest)
        proposal = deepcopy(proposal)
        proposal.update(proposal_id=compiled['proposal_id'], proposal_version=compiled['proposal_version'])
        new_compiled = deepcopy(compiled)
    else:
        new_compiled = compile_partition(new_manifest, proposal)
    return new_manifest, new_compiled, dict(operations=operations,
        unresolved_sources=reasons, graph_issues=flags, human_requests=human, holds=holds)


def _whole_source_segments(source, review_id):
    # A newly included source is REVIEW, never implicitly KEEP.
    if not source['text']:
        return []
    return [dict(segment_id='MR-' + canonical_sha256([review_id, source['source_id'], 'include'])[:24],
                 source_id=source['source_id'], disposition='REVIEW',
                 resolved_span=dict(start=0, end=len(source['text']), exact_quote=source['text']))]


def validate_material_review(manifest, compiled, review, *, candidate_sources=(), excluded_sources=(),
                             task_context=None, expected_request=None, protocol_version=AUTO_PROTOCOL_VERSION,
                             round_index=1, max_rounds=3, previous_round=None):
    """Validate review and return an immutable recompilation plan, if needed."""
    request = build_review_request(manifest, compiled, candidate_sources=candidate_sources,
                                   excluded_sources=excluded_sources, protocol_version=protocol_version,
                                   task_context=task_context, round_index=round_index,
                                   max_rounds=max_rounds, previous_round=previous_round)
    if expected_request is not None and request != expected_request:
        raise MaterialReviewError('EXPECTED_REVIEW_REQUEST_MISMATCH')
    review = _material_review(review, compiled, request)
    new_manifest, new_compiled, ledger = _revise(manifest, compiled, review, candidate_sources, excluded_sources)
    changed = (new_compiled['compiled_sha256'] != compiled['compiled_sha256']
               and (new_manifest['manifest_id'] != manifest['manifest_id']
                    or new_compiled['sources'] != compiled['sources']))
    status = review['status']
    if status == 'AUTO_REVIEW_PASS':
        if review['required_actions']:
            raise MaterialReviewError('PASS_CANNOT_HAVE_REQUIRED_ACTIONS')
        if changed or any(op['status'] == 'APPLIED' for op in ledger['operations']):
            raise MaterialReviewError('PASS_CANNOT_REQUIRE_OR_EXECUTE_REVISION')
        if ledger['unresolved_sources'] or ledger['graph_issues'] or ledger['human_requests'] or ledger['holds']:
            raise MaterialReviewError('PASS_CANNOT_HAVE_OPEN_BLOCKING_ACTIONS')
        if any(f['status'] == 'OPEN' and f['severity'] == 'BLOCKING' for f in review['findings']):
            raise MaterialReviewError('PASS_WITH_OPEN_BLOCKING_FINDING')
        if any(d['status'] not in ('RESOLVED', 'NOT_APPLICABLE') for d in review['dimensions']):
            raise MaterialReviewError('PASS_WITH_UNRESOLVED_DIMENSION')
        if any(d['applicability'] == 'UNKNOWN' for d in review['dimensions']):
            raise MaterialReviewError('PASS_WITH_UNKNOWN_DIMENSION')
        if any(s['disposition'] == 'REVIEW' for s in new_compiled['sources'] for s in s['segments']):
            raise MaterialReviewError('REVIEW_SEGMENT_CANNOT_AUTO_PASS')
    elif status == 'AUTO_REVIEW_REVISE' and not changed \
            and not _new_source_resolution_pending(compiled, previous_round):
        raise MaterialReviewError('REVISE_REQUIRES_ACTUAL_NEW_PARTITION')
    elif status == 'AUTO_REVIEW_HOLD' and not (ledger['unresolved_sources'] or ledger['graph_issues']
            or ledger['human_requests'] or ledger['holds'] or review['required_actions']
            or changed
            or any(f['status'] == 'OPEN' and f['severity'] == 'BLOCKING' for f in review['findings'])):
        raise MaterialReviewError('HOLD_REQUIRES_RECORDED_REASON')
    result = dict(manifest=new_manifest, compiled=new_compiled, ledger=ledger,
                  review=deepcopy(review), changed=changed,
                  input_compiled_sha256=compiled['compiled_sha256'],
                  input_manifest_sha256=canonical_sha256(manifest),
                  compiled_changed_requires_new_review=changed,
                  human_review_created=False, clinical_release_permitted=False,
                  event_graph_modified=False)
    application = dict(input_manifest_sha256=canonical_sha256(manifest),
        input_compiled_sha256=compiled['compiled_sha256'],
        output_manifest_sha256=canonical_sha256(new_manifest),
        output_compiled_sha256=new_compiled['compiled_sha256'],
        ledger_sha256=canonical_sha256(ledger), changed=changed)
    application['application_sha256'] = canonical_sha256(application)
    result['application_receipt'] = application
    validate_schema('MaterialRevisionPlan', dict(format='material-revision-plan-0.1.0',
        review_id=review['review_id'], status=status,
        input_compiled_sha256=result['input_compiled_sha256'],
        output_compiled_sha256=new_compiled['compiled_sha256'],
        changed=changed, operation_count=len(review['operations']),
        event_graph_modified=False, clinical_release_permitted=False))
    return result


def _new_source_resolution_pending(compiled, previous_round):
    """Permit one explicit continuation to resolve newly included REVIEW sources.

    This is not a second no-op edit within an unchanged source scope. The prior
    persisted execution must have added a source whose every segment is REVIEW.
    """
    if not isinstance(previous_round, dict) \
            or not previous_round.get('diff', {}).get('source_scope_changed'):
        return False
    old_ids = {s['source_id'] for s in previous_round['input_compiled']['sources']}
    added = [s for s in compiled['sources'] if s['source_id'] not in old_ids]
    return bool(added) and all(p['disposition'] == 'REVIEW'
        for source in added for p in source['segments'])


def _state_is_new_source_pending(current, prior_round):
    return prior_round is not None and _new_source_resolution_pending(current, prior_round)


def _content_signature(compiled):
    return canonical_sha256([dict(source_id=s['source_id'], source_version=s['source_version'],
        source_sha256=s['source_sha256'], segments=[dict(start=p['start'], end=p['end'],
        disposition=p['disposition'], text=p['text']) for p in s['segments']])
        for s in compiled['sources']])


def _diff(before, after):
    def spans(compiled):
        return {(s['source_id'], p['start'], p['end']): p['disposition']
                for s in compiled['sources'] for p in s['segments']}
    a, b = spans(before), spans(after)
    before_view, after_view = doctor_view(before), doctor_view(after)
    return dict(before_compiled_sha256=before['compiled_sha256'],
                after_compiled_sha256=after['compiled_sha256'],
                before_doctor_view_sha256=before_view['view_sha256'],
                after_doctor_view_sha256=after_view['view_sha256'],
                doctor_visible_material_changed=_doctor_visibility_signature(before_view) != _doctor_visibility_signature(after_view),
                source_scope_changed={s['source_id'] for s in before['sources']} != {s['source_id'] for s in after['sources']},
                changed=[dict(source_id=k[0], start=k[1], end=k[2], before=a.get(k), after=b.get(k))
                         for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)])


def _doctor_visibility_signature(view):
    """Hash actual visible text, not extra all-masked source placeholders."""
    return canonical_sha256([m for m in view['materials'] if m['text'] != PLACEHOLDER])


def _previous_round_history(round_, round_index):
    if not isinstance(round_, dict) or not isinstance(round_.get('review'), dict) \
            or not isinstance(round_.get('ledger'), dict) or not isinstance(round_.get('diff'), dict):
        raise MaterialReviewError('REVIEW_HISTORY_ROUND_REQUIRED')
    result = dict(round_index=round_index - 1,
        previous_review=deepcopy(round_['review']),
        program_execution_receipt=deepcopy(round_['application_receipt']),
        operation_receipts=deepcopy(round_['ledger']['operations']),
        actual_program_diff=deepcopy(round_['diff']),
        current_manifest_sha256=canonical_sha256(round_['manifest']),
        current_compiled_sha256=round_['compiled']['compiled_sha256'],
        current_doctor_view_sha256=doctor_view(round_['compiled'])['view_sha256'])
    if round_['input_compiled']['compiled_sha256'] != result['previous_review']['compiled_sha256']:
        raise MaterialReviewError('REVIEW_HISTORY_HASH_BINDING_MISMATCH')
    return result


def run_review_chain(manifest, compiled, ask, *, candidate_sources=(), excluded_sources=(),
                     max_rounds=3, protocol_version=AUTO_PROTOCOL_VERSION, task_context=None,
                     journal=None, transport_projection_format='FULL_REQUEST'):
    """Run bounded reviews; record exact inputs and retain completed rounds on failure.

    ``journal`` is a local persistence hook, never a model authorization. A failed
    provider or response validation returns HOLD, bound to the last applied output.
    """
    if type(max_rounds) is not int or not 1 <= max_rounds <= 20:
        raise MaterialReviewError('MATERIAL_REVIEW_BUDGET_OUT_OF_RANGE')
    # Reject malformed initial inputs before any provider execution.
    initial_request = build_review_request(manifest, compiled, candidate_sources=candidate_sources,
        excluded_sources=excluded_sources, protocol_version=protocol_version, task_context=task_context,
        round_index=1, max_rounds=max_rounds)
    task_context = deepcopy(initial_request['task_context'])
    registry = build_source_registry(manifest, candidate_sources=candidate_sources,
                                     excluded_sources=excluded_sources)
    registry_sha = canonical_sha256(registry)
    rounds, current_manifest, current = [], deepcopy(manifest), deepcopy(compiled)
    transport_projection = None
    if transport_projection_format != 'FULL_REQUEST':
        from .projection import FORMAT as COMPACT_FORMAT, compact_material_review_request
        if transport_projection_format != COMPACT_FORMAT:
            raise MaterialReviewError('TRANSPORT_PROJECTION_FORMAT_UNSUPPORTED')
        transport_projection = compact_material_review_request
    # Validate the explicit default/projection before any provider invocation.
    _transport_binding(initial_request, projection_format=transport_projection_format)
    seen = {_content_signature(current): 0}
    seen_included = {s['source_id'] for s in current_manifest['sources']}
    stop, failure = None, None
    for index in range(1, max_rounds + 1):
        active_candidates, active_excluded = registry_review_sources(registry, current_manifest, previously_included_ids=seen_included)
        request = build_review_request(current_manifest, current, candidate_sources=active_candidates,
            excluded_sources=active_excluded, protocol_version=protocol_version, task_context=task_context,
            round_index=index, max_rounds=max_rounds, previous_round=rounds[-1] if rounds else None)
        declared = _transport_binding(request, projection_format=transport_projection_format)
        round_input = dict(input_manifest=deepcopy(current_manifest), input_compiled=deepcopy(current),
            request=deepcopy(request), request_sha256=canonical_sha256(request),
            request_binding_sha256=request['response_binding']['request_sha256'],
            source_registry_sha256=registry_sha, **declared['transport'])
        if journal:
            journal('REQUEST', index, deepcopy(round_input))
        response, receipt, stage = None, dict(receipt_origin='SYNTHETIC_ADAPTER',
            transport_projection_format=transport_projection_format,
            declared_provider_payload_sha256=declared['transport']['declared_provider_payload_sha256'],
            provider_transport_verified=False), 'PROVIDER_REQUEST'
        try:
            import inspect
            parameters = inspect.signature(ask).parameters
            accepts_projection = any(p.kind == inspect.Parameter.VAR_KEYWORD
                for p in parameters.values()) or 'transport_projection' in parameters
            if accepts_projection:
                answer = ask(TASK, deepcopy(request), deepcopy(MATERIAL_REVIEW_SCHEMA),
                             transport_projection=transport_projection)
            else:
                # Existing offline test/library adapters keep their old call
                # signature; a compact declaration may not silently claim that
                # such an adapter transmitted the projected payload.
                if transport_projection is not None:
                    raise MaterialReviewError('TRANSPORT_PROJECTION_ADAPTER_UNSUPPORTED')
                answer = ask(TASK, deepcopy(request), deepcopy(MATERIAL_REVIEW_SCHEMA))
            response, receipt = answer if isinstance(answer, tuple) else (answer, receipt)
            if isinstance(receipt, dict) and 'receipt_origin' not in receipt:
                receipt = dict(receipt, receipt_origin='SYNTHETIC_ADAPTER',
                    transport_projection_format=transport_projection_format,
                    declared_provider_payload_sha256=declared['transport']['declared_provider_payload_sha256'],
                    provider_transport_verified=False)
            stage = 'RESPONSE_VALIDATION'
            plan = validate_material_review(current_manifest, current, response,
                candidate_sources=active_candidates, excluded_sources=active_excluded,
                task_context=task_context, expected_request=request,
                protocol_version=protocol_version, round_index=index, max_rounds=max_rounds,
                previous_round=rounds[-1] if rounds else None)
            plan['diff'] = _diff(current, plan['compiled'])
            if response['status'] == 'AUTO_REVIEW_REVISE' and not plan['diff']['changed'] \
                    and not _new_source_resolution_pending(current, rounds[-1] if rounds else None):
                raise MaterialReviewError('REVISE_REQUIRES_ACTUAL_NEW_CONTENT')
            if response['status'] == 'AUTO_REVIEW_REVISE' \
                    and not (plan['diff']['doctor_visible_material_changed'] or plan['diff']['source_scope_changed']) \
                    and not _new_source_resolution_pending(current, rounds[-1] if rounds else None):
                raise MaterialReviewError('REVISE_REQUIRES_DOCTOR_VISIBLE_OR_SOURCE_SCOPE_CHANGE')
        except Exception as exc:
            # Keep sanitized failure categories; original error strings can contain
            # clinical text or provider credentials. Specific contract codes are safe.
            exception_receipt = getattr(exc, 'receipt', receipt)
            if isinstance(exception_receipt, dict):
                receipt = exception_receipt
            evidence = getattr(exc, 'failure_evidence', None)
            recovered = getattr(exc, 'response_evidence', None)
            if response is None and isinstance(recovered, dict) \
                    and isinstance(recovered.get('response'), dict):
                response = recovered['response']
            local_budget = (type(exc).__name__ == 'LocalModelBudgetHold'
                    and str(exc) == 'ASSEMBLY_MODEL_CALL_BUDGET_REACHED'
                    and receipt.get('provider_invocation_occurred') is False
                    and receipt.get('status') == 'LOCAL_BUDGET_DENIED'
                    and receipt.get('cached') is False and response is None
                    and not isinstance(evidence, dict))
            if local_budget:
                # Local control flow stopped before transport. It must not be
                # mistaken for a provider request lacking terminal evidence.
                stage = 'LOCAL_BUDGET_CONTROL'
                code = 'ASSEMBLY_MODEL_CALL_BUDGET_REACHED'
            elif isinstance(evidence, dict):
                stage = evidence['terminal_stage']
            elif response is not None:
                stage = 'RESPONSE_VALIDATION'
            code = reason_for(exc)
            if type(exc).__name__ == 'LocalModelBudgetHold' \
                    and str(exc) == 'ASSEMBLY_MODEL_CALL_BUDGET_REACHED' \
                    and receipt.get('provider_invocation_occurred') is False \
                    and receipt.get('status') == 'LOCAL_BUDGET_DENIED' \
                    and receipt.get('cached') is False and response is None \
                    and not isinstance(evidence, dict):
                code = 'ASSEMBLY_MODEL_CALL_BUDGET_REACHED'
            if type(exc).__name__ == 'ModelRequestFailed' \
                    and isinstance(evidence, dict) and isinstance(evidence.get('reason_code'), str):
                # Preserve the terminal transport reason even when its private
                # evidence also contains a parsed response rejected by Schema.
                code = evidence['reason_code']
            failure = dict(stage=stage, error_type=type(exc).__name__, reason_code=code,
                round_index=index, receipt=deepcopy(getattr(exc, 'receipt', receipt)),
                request=deepcopy(request), request_sha256=canonical_sha256(request),
                request_binding_sha256=request['response_binding']['request_sha256'],
                **declared['transport'])
            if response is not None:
                failure['response'] = deepcopy(response)
            if isinstance(evidence, dict):
                failure['provider_attempt_evidence'] = private_chain_evidence(evidence)
            failure_receipt = failure['receipt']
            if isinstance(failure_receipt, dict) and failure_receipt.get('receipt_origin') in \
                    ('PROVIDER_CLIENT', 'PROVIDER_CLIENT_CACHE'):
                failure['receipt'] = _client_receipt(failure_receipt, declared['transport'], success=False)
            elif local_budget:
                failure['receipt'] = dict(failure_receipt, receipt_origin='LOCAL_CONTROL',
                    provider_transport_verified=False)
            elif isinstance(failure_receipt, dict):
                failure['receipt'] = _synthetic_receipt(failure_receipt, declared['transport'])
            diagnostics = diagnostics_for(exc)
            if diagnostics is not None:
                failure['program_diagnostics'] = diagnostics
            if journal:
                journal('FAILURE', index, deepcopy(failure))
            stop = 'EXECUTION_FAILED'
            break
        plan.update(round_input)
        bound_receipt = _client_receipt(receipt, declared['transport'], success=True) \
            if isinstance(receipt, dict) and receipt.get('receipt_origin') in \
                    ('PROVIDER_CLIENT', 'PROVIDER_CLIENT_CACHE') \
            else _synthetic_receipt(receipt, declared['transport'])
        terminal_evidence = bound_receipt.pop('terminal_attempt_evidence', None)
        plan['receipt'] = bound_receipt
        if terminal_evidence is not None:
            plan['provider_attempt_evidence'] = terminal_evidence
        application = dict(input_manifest_sha256=canonical_sha256(current_manifest),
            input_compiled_sha256=current['compiled_sha256'],
            output_manifest_sha256=canonical_sha256(plan['manifest']),
            output_compiled_sha256=plan['compiled']['compiled_sha256'],
            ledger_sha256=canonical_sha256(plan['ledger']), changed=plan['changed'])
        application['application_sha256'] = canonical_sha256(application)
        plan['application_receipt'] = application
        rounds.append(plan)
        # Applied operations and chain tail always advance together, even on HOLD.
        current_manifest, current = plan['manifest'], plan['compiled']
        seen_included.update(s['source_id'] for s in current_manifest['sources'])
        if journal:
            journal('ROUND', index, deepcopy(plan))
        if response['status'] == 'AUTO_REVIEW_HOLD' or response['required_actions'] or any(plan['ledger'][key] for key in
                ('unresolved_sources', 'graph_issues', 'human_requests', 'holds')):
            stop = 'REVIEW_HOLD'
            break
        if response['status'] == 'AUTO_REVIEW_PASS':
            stop = 'AUTO_PASS'
            break
        digest = _content_signature(current)
        if digest in seen and not _state_is_new_source_pending(current, rounds[-1] if rounds else None):
            stop = 'REVISION_CYCLE'
            break
        seen[digest] = index
        if index == max_rounds:
            stop = 'BUDGET_EXHAUSTED'
    view = doctor_view(current)
    chain = dict(format=CHAIN_FORMAT,
        state='AUTO_REVIEW_PASS' if stop == 'AUTO_PASS' else 'AUTO_REVIEW_HOLD',
        protocol_version=protocol_version, stop_reason=stop, max_rounds=max_rounds,
        initial_manifest=deepcopy(manifest), initial_compiled=deepcopy(compiled),
        task_context=deepcopy(task_context), source_registry=registry, source_registry_sha256=registry_sha,
        rounds=rounds, manifest=current_manifest, compiled=current, doctor_view=view,
        final_binding=dict(manifest_sha256=canonical_sha256(current_manifest),
            compiled_sha256=current['compiled_sha256'], doctor_view_sha256=view['view_sha256'],
            source_registry_sha256=registry_sha, round_count=len(rounds)),
        human_sample_status='NOT_ASSESSED', clinical_release_permitted=False,
        event_graph_modified=False, clinical_reference_created=False)
    if failure:
        chain['failure'] = failure
    return chain


MATERIAL_REVIEW_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'type': 'object', 'additionalProperties': False,
    'required': ['format', 'review_id', 'target', 'protocol_version', 'compiled_sha256',
                 'doctor_view_sha256', 'request_sha256', 'reviewer_type', 'dimensions',
                 'findings', 'operations', 'status', 'required_actions'],
    'properties': {
        'format': {'const': REVIEW_RESPONSE_FORMAT},
        'review_id': {'type': 'string', 'minLength': 1},
        'target': {'type': 'object', 'additionalProperties': False,
                   'required': ['checkpoint_id', 'decision_task_id', 'visibility_policy_version'],
                   'properties': {k: {'type': 'string', 'minLength': 1} for k in
                                  ('checkpoint_id', 'decision_task_id', 'visibility_policy_version')}},
        'protocol_version': {'enum': [AUTO_PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS]},
        'review_round': {'type': 'object', 'additionalProperties': False,
            'required': ['round_index', 'max_rounds', 'is_first_review',
                         'current_state_produced_by_previous_review'],
            'properties': {
                'round_index': {'type': 'integer', 'minimum': 1, 'maximum': 20},
                'max_rounds': {'type': 'integer', 'minimum': 1, 'maximum': 20},
                'is_first_review': {'type': 'boolean'},
                'current_state_produced_by_previous_review': {'type': 'boolean'}}},
        'compiled_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'doctor_view_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'request_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'reviewer_type': {'const': 'llm_material_review'},
        'dimensions': {'type': 'array', 'minItems': 7, 'items': {'type': 'object',
            'additionalProperties': False, 'required': ['dimension', 'applicability', 'status', 'reason'],
            'properties': {'dimension': {'enum': list(DIMENSIONS)},
                           'applicability': {'enum': list(APPLICABLE)},
                           'status': {'enum': ['RESOLVED', 'OPEN', 'NOT_APPLICABLE']},
                           'reason': {'type': 'string', 'minLength': 1}}}},
        'findings': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'required': ['finding_id', 'dimension', 'severity', 'status', 'reason'],
            'properties': {'finding_id': {'type': 'string', 'minLength': 1},
                           'dimension': {'enum': list(DIMENSIONS)},
                           'severity': {'enum': ['BLOCKING', 'NON_BLOCKING']},
                           'status': {'enum': ['OPEN', 'RESOLVED', 'NOT_APPLICABLE']},
                           'reason': {'type': 'string', 'minLength': 1},
                           'source_ids': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}}}}},
        'operations': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'required': ['operation_id', 'operation', 'source_id', 'reason'],
            'properties': {'operation_id': {'type': 'string', 'minLength': 1},
                           'operation': {'enum': list(OPERATIONS)},
                           'source_id': {'type': 'string', 'minLength': 1},
                           'reason': {'type': 'string', 'minLength': 1},
                           'disposition': {'enum': list(DISPOSITIONS)},
                           'exact_quote': {'type': 'string', 'minLength': 1},
                           'prefix': {'type': 'string'}, 'suffix': {'type': 'string'}}}},
        'status': {'enum': ['AUTO_REVIEW_PASS', 'AUTO_REVIEW_REVISE', 'AUTO_REVIEW_HOLD']},
        'required_actions': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
    },
    'allOf': [{'if': {'properties': {'protocol_version': {'const': AUTO_PROTOCOL_VERSION}}},
               'then': {'required': ['review_round']}}],
}


def _review_schema(protocol_version):
    if protocol_version == AUTO_PROTOCOL_VERSION:
        return MATERIAL_REVIEW_SCHEMA
    if protocol_version in LEGACY_PROTOCOL_VERSIONS:
        schema = deepcopy(MATERIAL_REVIEW_SCHEMA)
        schema.pop('allOf')
        schema['properties'].pop('review_round')
        schema['properties']['format']['const'] = 'material-review-0.2.0'
        schema['properties']['protocol_version'] = {'const': protocol_version}
        return schema
    raise MaterialReviewError('UNSUPPORTED_MATERIAL_REVIEW_PROTOCOL')
