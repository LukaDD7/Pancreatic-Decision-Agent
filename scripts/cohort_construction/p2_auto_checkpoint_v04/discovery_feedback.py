"""One bounded, same-input discovery recheck; no clinical reference inputs.

The engine supplies its response compiler as a callback, so this module does
not import the engine. Recompilation precedes all event evidence contexts and
relations; this module never patches an existing event graph in place.
"""

from copy import deepcopy

from jsonschema import Draft202012Validator

from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
from .prompts import DISCOVERY_SCHEMA


FEEDBACK_INSTRUCTION = """程序请求一次同源发现复核。原始完整文书、来源ID、上下文和输出schema保持不变。
请核对原始发现提案和下列机械报错，重新输出完整events/rejected_mentions；可以修正逐字引文、目标临床决策子句、管理记录数量、文书表述功能分类及不确定性。
若原始结果为零事件，只重新检查是否遗漏针对当前患者的管理判断；零事件仍是合法答案，不要求发现阳性或达到任何数量。
action_quotes和choice_boundary_quotes只能引用当前处理单位原文；其他引文也只能来自本次已提供的完整原文，不能从来源目录推测正文，不能新增来源。
逐字复制引文；不能自行改写原文或填写字符偏移。未知或确实无法定位时允许保留不确定，不能为了通过程序而编造。
本次不是医生盲评，不提供、不推断专家Reference，也不评价治疗正确性。local_id须在本次输出内唯一。"""


def validate_feedback_config(config):
    """Reject booleans, strings and values outside the explicit 0/1 limit."""
    limit = config.get('discovery_feedback_max_rounds', 0)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit not in (0, 1):
        raise ValueError('discovery_feedback_max_rounds must be integer 0 or 1')
    return limit


def _citation_findings(compiled):
    findings = []
    for event in compiled.get('events', []):
        for field in ('action_citations', 'choice_boundary_citations'):
            citations = event.get(field) or {}
            invalid = citations.get('invalid', [])
            if invalid:
                findings.append(dict(code='INVALID_DISCOVERY_CITATION', event_id=event.get('event_id'),
                    local_id=event.get('local_id'), citation_field=field,
                    citation_statuses=[item.get('status', 'UNKNOWN') for item in invalid]))
            # Ambiguous action quotes can retain source-level support, but an
            # ambiguous decision clause cannot establish one exact interval.
            ambiguous = citations.get('ambiguous', [])
            if field == 'choice_boundary_citations' and ambiguous:
                findings.append(dict(code='AMBIGUOUS_DECISION_BOUNDARY', event_id=event.get('event_id'),
                    local_id=event.get('local_id'), citation_field=field))
        actions = event.get('action_citations') or {}
        supported = bool(actions.get('valid') or any(q.get('eligible_as_source_support')
                                                    for q in actions.get('ambiguous', [])))
        if not supported:
            findings.append(dict(code='ACTION_SOURCE_UNVERIFIED', event_id=event.get('event_id'),
                                 local_id=event.get('local_id'), citation_field='action_citations'))
    return findings


def _full_text_source_ids(payload):
    ids = set()
    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get('source_id'), str) and isinstance(value.get('text'), str):
                ids.add(value['source_id'])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(payload)
    return ids


def _submitted_quotes(response):
    def visit(value):
        if isinstance(value, dict):
            if 'quote' in value and 'source_id' in value:
                yield value
            for child in value.values():
                yield from visit(child)
        elif isinstance(value, list):
            for child in value:
                yield from visit(child)
    yield from visit(response)


def _revision_violation(response, payload):
    local_ids = [event['local_id'] for event in response['events']]
    if len(local_ids) != len(set(local_ids)):
        return 'DUPLICATE_REVISED_LOCAL_ID'
    provided_ids = _full_text_source_ids(payload)
    if any(quote['source_id'] not in provided_ids for quote in _submitted_quotes(response)):
        return 'REVISED_QUOTE_SOURCE_NOT_IN_ORIGINAL_TEXT_INPUT'
    current_ids = set((payload.get('current_unit') or {}).get('source_ids', []))
    for event in response['events']:
        for field in ('action_quotes', 'choice_boundary_quotes'):
            if any(quote['source_id'] not in current_ids for quote in event.get(field, [])):
                return 'REVISED_ACTION_OR_BOUNDARY_OUTSIDE_CURRENT_UNIT'
    return None


def run_discovery_feedback(initial_compiled, initial_response, original_payload,
                           compile_response, client, config):
    """Return ``(final_compiled, audit)`` after at most one feedback invocation.

    ``compile_response(response, receipt)`` must call the engine's exact same
    compiler with the original unit/sources/name map. ``original_payload`` is
    the actually sent, already name-masked discovery input. Provider receipts
    provide request hashes; payload hashes separately attest unchanged context.
    Callback/provider errors, duplicate local IDs, new source IDs and unresolved
    mechanical quotation errors retain the initial compiled result.
    """
    limit = validate_feedback_config(config)
    original = deepcopy(initial_compiled)
    response = deepcopy(initial_response)
    payload = deepcopy(original_payload)
    findings = _citation_findings(original)
    local_ids = [event.get('local_id') for event in response.get('events', [])]
    duplicate_local_ids = sorted({item for item in local_ids if item is not None and local_ids.count(item) > 1})
    if duplicate_local_ids:
        findings.insert(0, dict(code='DUPLICATE_DISCOVERY_LOCAL_ID', local_ids=duplicate_local_ids))
    if not original.get('events'):
        findings.insert(0, dict(code='ZERO_DISCOVERED_EVENTS_RECHECK', not_a_positive_target=True))
    audit = dict(schema_version='0.1.0', max_rounds=limit, rounds_attempted=0,
                 status='NOT_RUN_DISABLED' if not limit else 'NOT_TRIGGERED',
                 trigger_findings=findings, original_response=response, revised_response=None,
                 initial_request_sha256=(original.get('receipt') or {}).get('request_sha256'),
                 original_payload_sha256=digest(payload), feedback_payload_sha256=None,
                 feedback_request_sha256=None, revised_response_sha256=None,
                 context_preserved=True, final_origin='initial', semantic_correctness='NOT_INDEPENDENTLY_VALIDATED')
    if not limit or not findings:
        return original, audit
    feedback_payload = deepcopy(payload)
    if 'discovery_feedback' in feedback_payload:
        raise ValueError('original discovery payload must not contain discovery_feedback')
    feedback_payload['discovery_feedback'] = dict(instruction=FEEDBACK_INSTRUCTION,
        original_response=deepcopy(response), mechanical_findings=deepcopy(findings),
        maximum_additional_discovery_invocations=1, zero_events_remain_valid=True)
    audit.update(rounds_attempted=1, feedback_payload_sha256=digest(feedback_payload))
    try:
        revised, receipt = client.ask('document_event_discovery_feedback', feedback_payload, DISCOVERY_SCHEMA)
        audit['feedback_request_sha256'] = receipt.get('request_sha256')
        audit['feedback_receipt'] = deepcopy(receipt)
        audit['revised_response'] = deepcopy(revised)
        audit['revised_response_sha256'] = digest(revised)
        unchanged_payload = {key: value for key, value in feedback_payload.items() if key != 'discovery_feedback'}
        if digest(unchanged_payload) != audit['original_payload_sha256']:
            audit.update(status='REJECTED_CONTEXT_MUTATION', context_preserved=False)
            return original, audit
        Draft202012Validator(DISCOVERY_SCHEMA).validate(revised)
        violation = _revision_violation(revised, payload)
        if violation:
            audit.update(status='REJECTED_INVALID_REVISION', rejection_reason=violation)
            return original, audit
        compiled = compile_response(deepcopy(revised), deepcopy(receipt))
        if compiled.get('unit_id') != original.get('unit_id'):
            audit.update(status='REJECTED_INVALID_REVISION', rejection_reason='COMPILED_UNIT_ID_CHANGED')
            return original, audit
        ids = [event['event_id'] for event in compiled.get('events', [])]
        if len(ids) != len(set(ids)):
            audit.update(status='REJECTED_INVALID_REVISION', rejection_reason='DUPLICATE_COMPILED_EVENT_ID')
            return original, audit
        remaining = _citation_findings(compiled)
        audit['revised_mechanical_findings'] = remaining
        if remaining:
            audit.update(status='REJECTED_UNRESOLVED_MECHANICAL_FINDINGS')
            return original, audit
        audit.update(status='REVISED_ACCEPTED', final_origin='revised',
                     initial_event_count=len(original.get('events', [])), final_event_count=len(compiled.get('events', [])))
        return compiled, audit
    except Exception as exc:
        # Never pull the shared client's last receipt and never copy provider
        # exception messages (which may contain clinical or credential data).
        audit.update(status='FAILED_INITIAL_PRESERVED', error_type=type(exc).__name__)
        request_hash = getattr(exc, 'request_sha256', None)
        if isinstance(request_hash, str):
            audit['feedback_request_sha256'] = request_hash
        return original, audit
