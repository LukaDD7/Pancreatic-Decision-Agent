"""Independent automated review of exact original material and its doctor view.

The LLM supplies observations, bounded operations and uncertainty. It never
writes offsets or changes the event graph. This module resolves exact quotes,
recompiles new versions and requires a new review for every changed hash.
"""
from copy import deepcopy

from jsonschema import Draft202012Validator

from scripts.cohort_construction.p2_checkpoint_material_governance.schema import validate_schema

from scripts.cohort_construction.p2_checkpoint_packet.compiler import (
    CONTRACT_VERSION, ContractError, _verified_sources,
    build_source_manifest, canonical_sha256, compile_partition, locate_exact_quote,
    project_doctor_materials, text_sha256,
)
from scripts.cohort_construction.p2_checkpoint_packet.semantic_audit import _verified_compiled

AUTO_PROTOCOL_VERSION = 'checkpoint-automated-material-review-0.2.0'
TASK = 'checkpoint_material_full_source_and_doctor_view_review'
DIMENSIONS = ('scope', 'target', 'leakage', 'evidence', 'boundaries', 'time', 'evaluability')
APPLICABLE = ('APPLICABLE', 'NOT_APPLICABLE', 'UNKNOWN')
OPERATIONS = ('SET_DISPOSITION', 'INCLUDE_SOURCE', 'EXCLUDE_SOURCE',
              'MARK_SOURCE_UNRESOLVED', 'FLAG_EVENT_GRAPH_ISSUE',
              'REQUEST_HUMAN_REVIEW', 'HOLD_PACKET')
DISPOSITIONS = ('KEEP', 'MASK', 'REVIEW')


class MaterialReviewError(ContractError):
    pass


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
                         task_context=None):
    """Provide full researcher text and the actual projected doctor view.

    Source metadata and model evidence are researcher context, never clinical
    assertions. Only explicitly supplied candidate source text can be included.
    """
    if protocol_version != AUTO_PROTOCOL_VERSION:
        raise MaterialReviewError('UNSUPPORTED_MATERIAL_REVIEW_PROTOCOL')
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
    request = dict(task=TASK, protocol_version=protocol_version,
                instructions=dict(
                    task='Read all included original text and all supplied candidate/excluded sources; compare with the doctor view for this target Checkpoint.',
                    requirements=['Do not infer absent text or negative findings.',
                                  'Do not rewrite original clinical text or supply character offsets.',
                                  'Use exact quotes for interval changes; SPLIT_SPAN is executed by SET_DISPOSITION.',
                                  'Any changed partition requires a new review; automatic PASS is not human or clinical approval.']),
                target=deepcopy(compiled['target']),
                task_context=deepcopy(task_context) if task_context is not None else dict(
                    decision_time_evidence='UNRESOLVED', clinical_visibility_certified=False),
                researcher_sources=[dict(source_id=s['source_id'], source_version=s['source_version'],
                                         sha256=s['sha256'], text=s['text']) for s in manifest['sources']],
                candidate_sources=list(candidates.values()), excluded_sources=list(excluded.values()),
                current_dispositions=_catalog(compiled), doctor_view=doctor_view(compiled),
                graph_is_candidate_not_clinical_reference=True)
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
    if not isinstance(review, dict) or set(review) != {
        'format', 'review_id', 'target', 'protocol_version', 'compiled_sha256',
        'doctor_view_sha256', 'request_sha256', 'reviewer_type', 'dimensions',
        'findings', 'operations', 'status', 'required_actions',
    }:
        raise MaterialReviewError('MATERIAL_REVIEW_FIELDS_MISMATCH')
    _validate_schema(review, MATERIAL_REVIEW_SCHEMA)
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
    for operation in review['operations']:
        kind, sid = operation['operation'], operation['source_id']
        record = dict(operation=deepcopy(operation), status='REJECTED')
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
            if sid not in source_map or operation['disposition'] not in DISPOSITIONS:
                raise MaterialReviewError('INVALID_DISPOSITION_OPERATION')
            match = locate_exact_quote(manifest, sid, operation['exact_quote'],
                                       operation.get('prefix'), operation.get('suffix'))
            if match['status'] != 'UNIQUE':
                raise MaterialReviewError('DISPOSITION_QUOTE_NOT_UNIQUE')
            anchor = match['anchors'][0]
            start, end = anchor['start'], anchor['end']
            target = [s for s in segments if s['source_id'] == sid
                      and s['resolved_span']['start'] <= start and end <= s['resolved_span']['end']]
            if len(target) != 1:
                raise MaterialReviewError('DISPOSITION_MUST_RESOLVE_WITHIN_ONE_SEGMENT')
            target = target[0]
            if (target['disposition'] == operation['disposition']
                    and target['resolved_span']['start'] == start and target['resolved_span']['end'] == end):
                record.update(status='NO_CHANGE', reason='SAME_DISPOSITION_AND_SPAN')
                continue
            base = target['resolved_span']
            replacement = []
            for a, b in ((base['start'], start), (start, end), (end, base['end'])):
                if a == b:
                    continue
                replacement.append(dict(
                    segment_id='MR-' + canonical_sha256([review['review_id'], sid, a, b,
                        operation['disposition'] if (a, b) == (start, end) else target['disposition']])[:24],
                    source_id=sid,
                    disposition=operation['disposition'] if (a, b) == (start, end) else target['disposition'],
                    resolved_span=dict(start=a, end=b, exact_quote=source_map[sid]['text'][a:b])))
            segments[segments.index(target):segments.index(target) + 1] = replacement
            record.update(status='APPLIED', resolved_anchor=anchor)
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
                             task_context=None, expected_request=None):
    """Validate review and return an immutable recompilation plan, if needed."""
    request = build_review_request(manifest, compiled, candidate_sources=candidate_sources,
                                   excluded_sources=excluded_sources,
                                   protocol_version=AUTO_PROTOCOL_VERSION,
                                   task_context=task_context)
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
    elif status == 'AUTO_REVIEW_REVISE' and not changed:
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
    return dict(before_compiled_sha256=before['compiled_sha256'],
                after_compiled_sha256=after['compiled_sha256'],
                changed=[dict(source_id=k[0], start=k[1], end=k[2], before=a.get(k), after=b.get(k))
                         for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)])


def run_review_chain(manifest, compiled, ask, *, candidate_sources=(), excluded_sources=(),
                     max_rounds=3, protocol_version=AUTO_PROTOCOL_VERSION, task_context=None,
                     journal=None):
    """Run bounded reviews; record exact inputs and retain completed rounds on failure.

    ``journal`` is a local persistence hook, never a model authorization. A failed
    provider or response validation returns HOLD, bound to the last applied output.
    """
    if type(max_rounds) is not int or not 1 <= max_rounds <= 20:
        raise MaterialReviewError('MATERIAL_REVIEW_BUDGET_OUT_OF_RANGE')
    # Reject malformed initial inputs before any provider execution.
    initial_request = build_review_request(manifest, compiled, candidate_sources=candidate_sources,
        excluded_sources=excluded_sources, protocol_version=protocol_version, task_context=task_context)
    task_context = deepcopy(initial_request['task_context'])
    registry = build_source_registry(manifest, candidate_sources=candidate_sources,
                                     excluded_sources=excluded_sources)
    registry_sha = canonical_sha256(registry)
    rounds, current_manifest, current = [], deepcopy(manifest), deepcopy(compiled)
    seen = {_content_signature(current): 0}
    seen_included = {s['source_id'] for s in current_manifest['sources']}
    stop, failure = None, None
    for index in range(1, max_rounds + 1):
        active_candidates, active_excluded = registry_review_sources(registry, current_manifest, previously_included_ids=seen_included)
        request = build_review_request(current_manifest, current, candidate_sources=active_candidates,
            excluded_sources=active_excluded, protocol_version=protocol_version, task_context=task_context)
        round_input = dict(input_manifest=deepcopy(current_manifest), input_compiled=deepcopy(current),
            request=deepcopy(request), request_sha256=canonical_sha256(request),
            request_binding_sha256=request['response_binding']['request_sha256'],
            source_registry_sha256=registry_sha)
        if journal:
            journal('REQUEST', index, deepcopy(round_input))
        response, receipt, stage = None, {}, 'PROVIDER_REQUEST'
        try:
            answer = ask(TASK, deepcopy(request), deepcopy(MATERIAL_REVIEW_SCHEMA))
            response, receipt = answer if isinstance(answer, tuple) else (answer, {})
            stage = 'RESPONSE_VALIDATION'
            plan = validate_material_review(current_manifest, current, response,
                candidate_sources=active_candidates, excluded_sources=active_excluded,
                task_context=task_context, expected_request=request)
            plan['diff'] = _diff(current, plan['compiled'])
            if response['status'] == 'AUTO_REVIEW_REVISE' and not plan['diff']['changed']:
                raise MaterialReviewError('REVISE_REQUIRES_ACTUAL_NEW_CONTENT')
        except Exception as exc:
            # Keep sanitized failure categories; original error strings can contain
            # clinical text or provider credentials. Specific contract codes are safe.
            code = str(exc) if isinstance(exc, MaterialReviewError) else type(exc).__name__
            failure = dict(stage=stage, error_type=type(exc).__name__, reason_code=code,
                round_index=index, receipt=deepcopy(getattr(exc, 'receipt', receipt)),
                request=deepcopy(request), request_sha256=canonical_sha256(request))
            if response is not None:
                failure['response'] = deepcopy(response)
            if journal:
                journal('FAILURE', index, deepcopy(failure))
            stop = 'EXECUTION_FAILED'
            break
        plan.update(round_input)
        plan['receipt'] = deepcopy(receipt)
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
        if digest in seen:
            stop = 'REVISION_CYCLE'
            break
        seen[digest] = index
        if index == max_rounds:
            stop = 'BUDGET_EXHAUSTED'
    view = doctor_view(current)
    chain = dict(format='automated-material-review-chain-0.2.0',
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
        'format': {'const': 'material-review-0.2.0'},
        'review_id': {'type': 'string', 'minLength': 1},
        'target': {'type': 'object', 'additionalProperties': False,
                   'required': ['checkpoint_id', 'decision_task_id', 'visibility_policy_version'],
                   'properties': {k: {'type': 'string', 'minLength': 1} for k in
                                  ('checkpoint_id', 'decision_task_id', 'visibility_policy_version')}},
        'protocol_version': {'const': AUTO_PROTOCOL_VERSION},
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
}
