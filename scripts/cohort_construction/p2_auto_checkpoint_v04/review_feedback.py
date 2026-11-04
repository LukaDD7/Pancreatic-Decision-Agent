"""One conditional semantic revision in the live review pipeline (CE-AUD-007).

The submitted source pool, event IDs and allowlists remain fixed.  Changing
discovery anchors is a separate earlier stage that rebuilds all review contexts.
No clinical Reference, evaluation metric or additional patient source is read.
"""
from collections import Counter
from copy import deepcopy
import json

from jsonschema import Draft202012Validator

from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
from .client import ModelRequestFailed
from .prompts import REVIEW_SCHEMA


_TASK = 'document_checkpoint_review_repair'
_INITIAL_ID_ERRORS = {'MODEL_OMITTED_EVENT_ID', 'MODEL_DUPLICATED_EVENT_ID'}
_SAFE_ERROR_CODES = {
    'TARGET_NOT_IN_ORIGINAL_REQUEST', 'ORIGINAL_EVIDENCE_CONTEXT_MISMATCH',
    'ORIGINAL_ALLOWED_SPANS_MISMATCH', 'ORIGINAL_EVIDENCE_COORDINATES_MISMATCH',
    'ORIGINAL_EVIDENCE_TEXT_MISMATCH', 'REVIEW_FEEDBACK_PAYLOAD_EXCEEDS_BUDGET',
    'REVIEW_FEEDBACK_CLIENT_MUTATED_PAYLOAD', 'REVIEW_FEEDBACK_RECEIPT_INVALID',
}


def _bound_failure_receipt(exc):
    """Use only the failed invocation's own transport exception snapshot.

    A shared receipt list is deliberately not a fallback: review requests for
    different source units run concurrently, including requests with this task.
    """
    if not isinstance(exc, ModelRequestFailed):
        return None
    receipt = exc.receipt
    if (not isinstance(receipt, dict) or receipt.get('task') != _TASK
            or receipt.get('status') != 'FAILED'
            or receipt.get('request_sha256') != exc.request_sha256):
        return None
    return deepcopy(receipt)


def _valid_success_receipt(receipt):
    return (isinstance(receipt, dict)
            and isinstance(receipt.get('request_sha256'), str)
            and bool(receipt['request_sha256'])
            and receipt.get('status') in (None, 'SUCCESS')
            and receipt.get('task', _TASK) == _TASK)


def review_repair_triggers(event):
    """Include initial response ID failures, without promoting semantic doubt.

    The engine generates these markers only when the initial response lacks a
    unique item for an event. Such a mechanical failure can be revised once even
    though the fallback classification itself is semantically uncertain.
    """
    from scripts.cohort_construction.p2_auto_checkpoint_v05_refine.refine import repair_triggers
    triggers = repair_triggers(event)
    if event.get('review_refinement', {}).get('attempted') or not event.get('review_request_sha256'):
        return triggers
    review = event.get('semantic_review') or {}
    rationale = review.get('rationale', '')
    uncertainty = review.get('uncertainty', [])
    markers = {marker for marker in _INITIAL_ID_ERRORS
               if (isinstance(rationale, str) and marker in rationale)
               or (isinstance(uncertainty, list) and marker in uncertainty)}
    return sorted(set(triggers) | markers)


def validate_feedback_config(config):
    for field in ('review_feedback_max_rounds', 'discovery_feedback_max_rounds'):
        value = config.get(field, 0)
        if type(value) is not int or value not in (0, 1):
            raise ValueError(field.upper() + '_MUST_BE_ZERO_OR_ONE')
    if type(config.get('final_graph_validation', False)) is not bool:
        raise ValueError('FINAL_GRAPH_VALIDATION_MUST_BE_BOOLEAN')
    if type(config.get('include_support_documents', False)) is not bool:
        raise ValueError('INCLUDE_SUPPORT_DOCUMENTS_MUST_BE_BOOLEAN')


def run_review_feedback(events, original_payload, discovery_events, sources,
                        names, client, config):
    """Return revised copies and a request-bound audit; failure retains originals."""
    validate_feedback_config(config)
    from scripts.cohort_construction.p2_auto_checkpoint_v05_refine.refine import (
        prepare_payload, revalidate,
    )
    output = deepcopy(events)
    targets = [e for e in events if review_repair_triggers(e)]
    audit = dict(issue_id='CE-AUD-007', attempted=False, maximum_rounds=1,
                 target_event_ids=[e['event_id'] for e in targets],
                 original_payload_sha256=digest(original_payload),
                 source_context_added=False, positive_result_required=False,
                 original_proposals=[deepcopy(e['semantic_review']) for e in targets],
                 status='DISABLED' if not config.get('review_feedback_max_rounds') else 'NO_REPAIRABLE_ERROR')
    if not config.get('review_feedback_max_rounds') or not targets:
        return output, audit
    original_hashes = {e['review_request_sha256'] for e in targets}
    if len(original_hashes) != 1:
        raise ValueError('FEEDBACK_REQUIRES_ONE_ORIGINAL_REVIEW_REQUEST')
    audit['original_review_request_sha256'] = next(iter(original_hashes))
    receipt = None
    try:
        payload = prepare_payload(original_payload, targets, names)
        # prepare_payload implements the older proof-error triggers; enrich its
        # feedback with live initial-response ID errors without changing any
        # submitted document, source block, ID allowlist or original proposal.
        triggers_by_id = {e['event_id']: review_repair_triggers(e) for e in targets}
        for item in payload['validation_feedback']:
            item['triggers'] = triggers_by_id[item['event_id']]
        audit['repair_payload_sha256'] = digest(payload)
        if len(json.dumps(payload, ensure_ascii=False)) > config.get('max_review_payload_characters', 240000):
            raise ValueError('REVIEW_FEEDBACK_PAYLOAD_EXCEEDS_BUDGET')
        audit['attempted'] = True
        result, receipt = client.ask(_TASK, payload, REVIEW_SCHEMA)
        if digest(payload) != audit['repair_payload_sha256']:
            raise ValueError('REVIEW_FEEDBACK_CLIENT_MUTATED_PAYLOAD')
        if not _valid_success_receipt(receipt):
            raise ValueError('REVIEW_FEEDBACK_RECEIPT_INVALID')
        audit.update(result=deepcopy(result), receipt=deepcopy(receipt))
        Draft202012Validator(REVIEW_SCHEMA).validate(result)
    except Exception as exc:
        # Never pick a shared last receipt: other unit requests run concurrently.
        bound = _bound_failure_receipt(exc)
        if bound:
            audit['receipt'] = bound
        code = str(exc) if type(exc) is ValueError and str(exc) in _SAFE_ERROR_CODES else None
        audit.update(status='REPAIR_NOT_APPLIED', error=type(exc).__name__,
                     error_code=code)
        for e in output:
            if e['event_id'] in audit['target_event_ids']:
                e['review_refinement'] = dict(attempted=audit['attempted'], rounds=int(audit['attempted']),
                    status='REPAIR_NOT_APPLIED', original_review_request_sha256=e['review_request_sha256'],
                    error=type(exc).__name__, triggers=review_repair_triggers(e))
        return output, audit
    counts = Counter(r['event_id'] for r in result['items'])
    proposals = {r['event_id']: r for r in result['items'] if counts[r['event_id']] == 1}
    ids = set(audit['target_event_ids'])
    audit['unexpected_event_ids'] = sorted(set(counts) - ids)
    revised = {}
    for event in targets:
        eid = event['event_id']
        if eid not in proposals:
            e = deepcopy(event)
            e['review_refinement'] = dict(attempted=True, rounds=1, status='TARGET_MISSING_OR_DUPLICATED',
                original_review_request_sha256=event['review_request_sha256'],
                repair_request_sha256=receipt['request_sha256'], triggers=review_repair_triggers(event))
        else:
            try:
                e = revalidate(event, proposals[eid], sources, discovery_events, names, receipt)
                e['review_refinement']['triggers'] = review_repair_triggers(event)
            except Exception as exc:
                e = deepcopy(event)
                e['review_refinement'] = dict(attempted=True, rounds=1, status='REPAIR_REVALIDATION_FAILED',
                    original_review_request_sha256=event['review_request_sha256'],
                    repair_request_sha256=receipt['request_sha256'], error=type(exc).__name__,
                    triggers=review_repair_triggers(event))
        revised[eid] = e
    audit['status'] = 'COMPLETE'
    audit['event_results'] = {eid: e['review_refinement']['status'] for eid, e in revised.items()}
    return [revised.get(e['event_id'], e) for e in output], audit
