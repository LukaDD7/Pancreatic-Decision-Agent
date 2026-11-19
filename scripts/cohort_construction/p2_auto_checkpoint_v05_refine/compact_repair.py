"""Versioned, source-coordinate review repair with bounded display material.

The original parent request is the immutable authority. Compact requests render
selected exact source windows, while returned quotations are resolved against
original saved allowlists by the ordinary full revalidator. Text omitted from
this presentation is not a negative finding or proof of clinical absence.
"""
from copy import deepcopy
import hashlib

from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
from scripts.cohort_construction.p2_auto_checkpoint.semantic import mask_text
from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_evidence_context
from scripts.cohort_construction.p2_auto_checkpoint_v04.engine import (
    _allowed_connection_ranges, redact, resolve_model_quotes,
)


REPAIR_CONTRACT_VERSION = 'review-compact-repair-0.2.0'
EVIDENCE_AVAILABILITY = 'SELECTED_ORIGINAL_WINDOWS_WITH_ORIGINAL_AUTHORITY_REVALIDATION'
COMPACT_INSTRUCTIONS = """
这是原始review的一次新的、版本化的受限修订实现，不是同一provider请求的第二轮。
原始完整来源权限已离线保存并用哈希绑定；本请求仅呈现选定原文窗口，不是完整证据覆盖。
source_coordinate_index以原始block_id索引全部允许证据的哈希、EO与坐标；evidence_source_pool仅呈现带绝对坐标的精确切片。
允许保留或修订已有原文引文；引文可以引用未显示的原允许范围，但不得猜测未读内容，且必须逐字通过原文复核。
不得新增来源、EvidenceObject、事件、前轮或时间字段；前轮仅取目标输入的eligible_previous_event_ids。
可以保留uncertain，不要求阳性结果。current_full_document仍含实际选择及选择后文字，仅用于动作与边界，不得冒充选择前事实。
只为events中的每个event_id返回一条完整review items；validation_feedback为精简定位错误，不是新临床事实。
"""


def _target(payload, event_id):
    rows = [row for row in payload['events'] if row['event']['event_id'] == event_id]
    if len(rows) != 1:
        raise ValueError('TARGET_NOT_IN_ORIGINAL_REQUEST')
    return rows[0]


def unmask_submitted_id(value, saved):
    """Recover only a unique saved ID matching this legacy masked ID pattern."""
    if not isinstance(value, str):
        return None
    matches = [candidate for candidate in saved
               if len(candidate) == len(value) and all(
                   submitted == '*' or submitted == original
                   for submitted, original in zip(value, candidate))]
    return matches[0] if len(matches) == 1 else None


def _submitted_authority(payload, targets):
    result = {}
    for event in targets:
        target = _target(payload, event['event_id'])
        submitted = sorted(target['evidence_context'].get('eligible_previous_event_ids', []))
        saved = sorted(event.get('provided_prior_event_ids', []))
        from scripts.cohort_construction.p2_auto_checkpoint_v04.engine import redact
        # Existing parent payloads redacted free-form ledger values, while the

        # saved event retained the exact IDs. Treat only a one-to-one masking
        # equivalent as the same submitted authority; any new, missing or
        # ambiguous identity fails closed.
        submitted_unmasked = [unmask_submitted_id(value, saved) for value in submitted]
        if None in submitted_unmasked or sorted(submitted_unmasked) != saved:
            raise ValueError('COMPACT_REPAIR_SUBMITTED_PRIOR_AUTHORITY_MISMATCH')
        submitted = sorted(submitted_unmasked)
        identity = sorted(set(saved) | set(event.get('provided_identity_event_ids', []))
                          | {event['event_id']})
        result[event['event_id']] = dict(
            submitted_eligible_previous_event_ids=submitted,
            original_submitted_eligible_previous_event_ids=sorted(
                target['evidence_context'].get('eligible_previous_event_ids', [])),
            saved_provided_prior_event_ids=saved, identity_event_ids=identity)
    return result


def _coordinate(block):
    # Original IDs and clocks retain full EO/anchor bindings. Repeated source
    # coordinates and clock/provenance receipts are hash-bound offline, not
    # retransmitted as though every row were independent clinical narrative.
    return dict(block_id=block['block_id'],
        evidence_object_id=block.get('evidence_object_id'),
        test_execution_anchor_id=block.get('test_execution_anchor_id'),
        coordinate_digest=digest({key: block.get(key) for key in (
            'block_id', 'source_id', 'start', 'end', 'source_title', 'clock',
            'execution_time', 'publication_time', 'publication_status',
            'evidence_object_id', 'test_execution_anchor_id')}),
        masked_text_sha256=hashlib.sha256(block['text'].encode()).hexdigest())


def _feedback(event, submitted_ids):
    from .refine import feedback, technical_validation_errors

    complete = feedback(event)
    return dict(event_id=event['event_id'], triggers=complete['triggers'],
        checkpoint_status=event.get('checkpoint_status'),
        validation_reasons=deepcopy(event.get('validation_reasons', [])),
        localized_validation_errors=[{key: deepcopy(error[key]) for key in (
            'code', 'event_id', 'evidence_object_id', 'field_path', 'source_id',
            'citation_index', 'span', 'rule', 'repairable', 'correction') if key in error}
            for error in technical_validation_errors(event)],
        allowed_revision_scope=dict(complete['allowed_revision_scope'],
            eligible_previous_event_ids=deepcopy(submitted_ids),
            evidence_object_ids=sorted({block.get('evidence_object_id')
                for block in event['actual_model_evidence'] if block.get('evidence_object_id')}),
            revision_policy='WHOLE_ITEM_SEMANTIC_REVISION_WITH_ORIGINAL_SOURCE_AND_IDENTITY_BOUNDARIES'),
        allowed_observation_spans=deepcopy(event['evidence_context']['allowed_spans']))


def _valid_span(source_id, start, end, sources):
    return (isinstance(source_id, str) and source_id in sources and type(start) is int
            and type(end) is int and 0 <= start < end <= len(sources[source_id]['text']))


def _citation_spans(citation):
    if not isinstance(citation, dict):
        return
    candidates = [citation] + list(citation.get('occurrences', [])) + list(
        citation.get('raw_occurrences', []))
    for row in candidates:
        if isinstance(row, dict):
            yield row.get('source_id', citation.get('source_id')), row.get('start'), row.get('end')


def _compact_ledger_row(row):
    """Return a hashed identity row, never a rediscovered prior event.

    Exact prior clocks remain available in source_coordinate_index. A row not
    rendered here cannot become new prior authority; the submitted event input's
    eligible_previous_event_ids is the sole prior allowlist.
    """
    return dict(event_id=row['event_id'], original_row_sha256=digest(row))


def _group_citations(event):
    for name in ('action_citations', 'choice_boundary_citations',
                 'prechoice_citations', 'postchoice_citations'):
        for status in ('valid', 'ambiguous'):
            for row in (event.get(name) or {}).get(status, []):
                yield row


def _proposal_spans(value):
    if isinstance(value, dict):
        if isinstance(value.get('source_id'), str) and isinstance(value.get('quote'), str):
            yield value
        for child in value.values():
            yield from _proposal_spans(child)
    elif isinstance(value, list):
        for child in value:
            yield from _proposal_spans(child)


def _ranges(context):
    return [dict(span, source_id=sid) if isinstance(span, dict) else dict(
        source_id=sid, start=span[0], end=span[1])
        for sid, spans in context.items() for span in spans]


def _window(span, sources, names, original_pool):
    sid, start, end = span
    if not _valid_span(sid, start, end, sources):
        raise ValueError('COMPACT_REPAIR_SELECTION_SPAN_INVALID')
    containing = any(block['source_id'] == sid and block['start'] <= start and end <= block['end']
                     for block in original_pool.values())
    return dict(block_id=f'{sid}:{start}:{end}', source_id=sid, start=start, end=end,
        text=mask_text(sources[sid]['text'][start:end], names),
        authority_basis=('ORIGINAL_PARENT_EVIDENCE_POOL' if containing else
            'PARENT_CURRENT_FULL_DOCUMENT_ACTION_OR_CONNECTION_BOUNDARY'),
        presentation_role='EXACT_ORIGINAL_SOURCE_WINDOW_NOT_NEW_SOURCE_AUTHORITY')


def _merge_windows(windows):
    result = []
    for span in sorted(set(windows)):
        if (result and span[0] == result[-1][0]
                and span[1] <= result[-1][2] + 16):
            # Join only genuinely adjacent source spans. Sixteen separators
            # preserve enough local syntax without blanketing unrelated text.
            result[-1] = (span[0], result[-1][1], max(result[-1][2], span[2]))
        else:
            result.append(span)
    return result


def _referenced_eos(event):
    from .refine import technical_validation_errors

    result = set()

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('evidence_object_id', 'test_execution_anchor_id') and isinstance(child, str):
                    result.add(child)
                elif key == 'evidence_object_ids' and isinstance(child, list):
                    result.update(item for item in child if isinstance(item, str))
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(event['semantic_review'])
    walk(technical_validation_errors(event))
    return result


def prepare_compact_payload(payload, targets, names, sources, all_events):
    """Compile exact original windows; never truncate text to meet a budget."""
    from .refine import feedback, prepare_payload, technical_validation_errors

    # Reuse all original target/pool/EO/text checks before creating a new shape.
    parent = prepare_payload(payload, targets, names)
    authority = _submitted_authority(payload, targets)
    original_pool = {block['block_id']: block for block in payload['evidence_source_pool']}
    if len(original_pool) != len(payload['evidence_source_pool']):
        raise ValueError('COMPACT_REPAIR_DUPLICATE_ORIGINAL_BLOCK_ID')
    source_index = {source['source_id']: source for source in sources}
    all_by_id = {event['event_id']: event for event in all_events}
    selected, roles = set(), {}
    unresolved_feedback = []

    def select(span, role, event_id):
        sid, start, end = span
        if not _valid_span(sid, start, end, source_index):
            unresolved_feedback.append(dict(target_event_id=event_id, role=role,
                source_id=sid if isinstance(sid, str) else None,
                start=start if type(start) is int else None, end=end if type(end) is int else None,
                status='SPAN_NOT_AUTHENTICATED_AS_RENDERABLE_SOURCE_WINDOW'))
            return
        key = (sid, start, end)
        selected.add(key)
        roles.setdefault(key, set()).add(role)

    for event in targets:
        eid = event['event_id']
        event_source_ids = set(event.get('source_ids') or [event.get('source_id')])
        original_allowed = {(sid, span['start'], span['end'])
            for sid, spans in event['evidence_context']['allowed_spans'].items() for span in spans}
        actual = {(b['source_id'], b['start'], b['end']) for b in event['actual_model_evidence']}
        if original_allowed != actual or not actual <= set(
                (b['source_id'], b['start'], b['end']) for b in original_pool.values()):
            raise ValueError('COMPACT_REPAIR_ORIGINAL_AUTHORITY_NOT_IN_PARENT_POOL')
        connection = _ranges(_allowed_connection_ranges(event, event['evidence_context'], sources))
        previous = all_by_id.get(event['semantic_review'].get('previous_event_id'))
        if previous is not None:
            rebuilt = build_evidence_context(previous, sources, all_events,
                budget=150000, preserve_current_context=True)
            previous_ranges = _ranges(_allowed_connection_ranges(previous, rebuilt, sources))
            for row in _group_citations(previous):
                for span in _citation_spans(row):
                    if any(type(span[1]) is int and type(span[2]) is int
                           and candidate['source_id'] == span[0]
                           and candidate['start'] <= span[1] and span[2] <= candidate['end']
                           for candidate in previous_ranges):
                        select(span, 'SELECTED_PREVIOUS_EVENT_ACTION_AND_BOUNDARY', eid)
        for row in _group_citations(event):
            if row.get('source_id') not in event_source_ids:
                continue
            for span in _citation_spans(row):
                select(span, 'DISCOVERY_ACTION_PRECHOICE_POSTCHOICE_OR_CONNECTION_BOUNDARY', eid)
        referenced_eos = _referenced_eos(event)
        for block in event['actual_model_evidence']:
            if (block.get('evidence_object_id') in referenced_eos
                    or block.get('test_execution_anchor_id') in referenced_eos):
                select((block['source_id'], block['start'], block['end']),
                    'DECLARED_EVIDENCE_OBJECT_DIRECT_QUOTE_AND_CLOCK_BINDING', eid)
        for row in _proposal_spans(event['semantic_review']):
            resolved = resolve_model_quotes([row], sources, names)
            for status in ('valid', 'ambiguous'):
                for citation in resolved[status]:
                    for span in _citation_spans(citation):
                        select(span, 'ORIGINAL_PROPOSAL_OBSERVATION_CONNECTION_COMPARISON_OR_ACTION', eid)
        for error in technical_validation_errors(event):
            span = error.get('span')
            if isinstance(span, dict):
                select((error.get('source_id'), span.get('start'), span.get('end')),
                    'LOCALIZED_TECHNICAL_ERROR_SOURCE_SPAN', eid)
    result = {key: deepcopy(parent[key]) for key in
              ('unit_id', 'document_profile', 'type_prior', 'events', 'current_full_document')
              if key in parent}
    rendered = [_window(span, source_index, names, original_pool)
                for span in _merge_windows(selected)]
    result.update(instruction=payload['instruction'] + '\n' + COMPACT_INSTRUCTIONS,
        reconstruction_only_actual_choices_disclosed=True,
        evidence_source_pool=rendered,
        source_coordinate_index=[_coordinate(original_pool[key]) for key in sorted(original_pool)],
        earlier_event_ledger=_compact_ledger(payload['earlier_event_ledger'], authority),
        validation_feedback=redact([_feedback(event, authority[event['event_id']]
            ['submitted_eligible_previous_event_ids']) for event in targets], names),
        original_proposals=parent['original_proposals'])
    omitted_fields = ['observation_validation', 'connection_validation', 'comparison_validation',
        'availability_validation', 'semantic_proof_validation', 'field_validation_errors',
        'evidence_relation_ledger']
    result['repair_contract'] = dict(
        version=REPAIR_CONTRACT_VERSION, maximum_semantic_repair_rounds=1,
        repair_attempt_semantics='NEW_IMPLEMENTATION_VERSION_NOT_PROVIDER_SECOND_ROUND',
        original_evidence_pool_sha256=digest(payload['evidence_source_pool']),
        original_full_document_sha256=digest(payload['current_full_document']),
        original_earlier_event_ledger_sha256=digest(payload['earlier_event_ledger']),
        original_event_inputs_sha256=digest({e['event_id']: _target(payload, e['event_id'])
            for e in targets}),
        original_allowed_spans_sha256=digest({e['event_id']: e['evidence_context']['allowed_spans']
            for e in targets}),
        submitted_evidence_windows_sha256=digest(rendered),
        submitted_event_authority=authority,
        source_context_added=False, source_authority_expanded=False,
        authority_narrowed=False, repair_evidence_availability=EVIDENCE_AVAILABILITY,
        source_coverage_complete=False, positive_result_required=False,
        omitted_derived_receipt_fields=omitted_fields,
        omitted_derived_receipts_sha256={
            field: digest({e['event_id']: feedback(e).get(field) for e in targets})
            for field in omitted_fields},
        display_roles=dict(
            current_full_document='ACTION_AND_POSTCHOICE_BOUNDARY_ONLY_NOT_PRECHOICE_AUTHORITY',
            unrendered_original_blocks='NOT_DISPLAYED_NOT_NEGATIVE_NOT_CLINICALLY_ABSENT',
            original_coordinate_index='HASH_BOUND_AUTHORITY_NOT_A_CLAIM_THAT_MODEL_READ_THE_TEXT'),
        selection_receipt=[dict(source_id=sid, start=start, end=end, roles=sorted(roles[(sid, start, end)]))
            for sid, start, end in sorted(selected)],
        unresolved_feedback_spans=unresolved_feedback,
        unrendered_original_block_ids=sorted(set(original_pool) - {
            block_id for sid, start, end in selected
            for block_id, block in original_pool.items()
            if block['source_id'] == sid and block['start'] < end and start < block['end']}))
    if any(not block['text'] for block in rendered):
        raise ValueError('COMPACT_REPAIR_EMPTY_OR_TRUNCATED_WINDOW')
    return result


def _compact_ledger(ledger, authority):
    ids = {eid for row in authority.values()
           for key in ('submitted_eligible_previous_event_ids', 'identity_event_ids')
           for eid in row[key]}
    return [_compact_ledger_row(row) for row in ledger if row.get('event_id') in ids]
