"""Private, deterministic model-attempt diagnostics; no clinical inference.

Provider bodies and failed model values belong in the private invocation bundle.
Operational receipts retain only paths, hashes, structural coordinates and safe
schema constants. These helpers never repair a quotation or accept a response.
"""
from copy import deepcopy
import hashlib
import json

from jsonschema import Draft202012Validator, ValidationError
from jsonschema.exceptions import best_match

from scripts.cohort_construction.p2_auto_checkpoint.extract import digest

FORMAT = 'provider-attempt-evidence-0.1.0'
TRANSPORT_STAGES = (
    'PROVIDER_REQUEST', 'PROVIDER_HTTP_RESPONSE', 'PROVIDER_ENVELOPE_PARSE',
    'PROVIDER_CONTENT_PARSE', 'RESPONSE_SCHEMA_VALIDATION',
)
CHAIN_STAGES = TRANSPORT_STAGES + ('RESPONSE_VALIDATION',)


class DiagnosticValueError(ValueError):
    """Stable program rejection plus private, replayable structured details."""
    def __init__(self, reason_code, diagnostics):
        self.reason_code = reason_code
        self.diagnostics = deepcopy(diagnostics)
        super().__init__(reason_code)


def reason_for(exc, default=None):
    if isinstance(exc, DiagnosticValueError):
        return exc.reason_code
    if isinstance(exc, ValueError):
        return str(exc)
    return default or type(exc).__name__


def diagnostics_for(exc):
    value = getattr(exc, 'diagnostics', None)
    return deepcopy(value) if isinstance(value, list) else None


def json_diagnostic(exc, text):
    result = dict(kind='JSON_PARSE', content_sha256=hashlib.sha256(
        text.encode('utf-8')).hexdigest(), content_length=len(text))
    if isinstance(exc, json.JSONDecodeError):
        result.update(line=exc.lineno, column=exc.colno, position=exc.pos,
            reason=exc.msg)
    else:
        result.update(error_type=type(exc).__name__)
    return result


def _schema_at(schema, path):
    current = schema
    for part in path:
        current = current[part]
    return current


def schema_diagnostic(exc, schema, instance=None):
    """Preserve both instance and definition paths without exposing values."""
    if not isinstance(exc, ValidationError):
        raise TypeError('ValidationError required')
    if instance is not None:
        errors = list(Draft202012Validator(schema).iter_errors(instance))
        if not errors:
            raise ValueError('SCHEMA_DIAGNOSTIC_ERROR_NOT_REPRODUCED')
        exc = best_match(errors)
    instance_path = list(exc.absolute_path)
    schema_path = list(exc.absolute_schema_path)
    local_schema = _schema_at(schema, schema_path[:-1]) if schema_path \
        and schema_path[-1] in ('required', 'additionalProperties') else _schema_at(schema, schema_path)
    result = dict(kind='SCHEMA_VALIDATION', instance_path=instance_path,
        schema_path=schema_path, validator=exc.validator,
        schema_sha256=digest(schema),
        failing_value_sha256=digest(exc.instance))
    if exc.validator in ('enum', 'const'):
        result['allowed_value'] = deepcopy(exc.validator_value)
    if not isinstance(local_schema, dict):
        local_schema = {}
    if exc.validator == 'required' and isinstance(exc.instance, dict):
        required = local_schema.get('required', [])
        if not required and len(schema_path) >= 2 and schema_path[-1] == 'required':
            parent = schema
            for part in schema_path[:-1]:
                parent = parent[part]
            required = parent.get('required', []) if isinstance(parent, dict) else []
        result['missing_keys'] = sorted(key for key in required if key not in exc.instance)
    if exc.validator == 'additionalProperties' and isinstance(exc.instance, dict):
        properties = local_schema.get('properties', {})
        result['unexpected_keys'] = sorted(set(exc.instance) - set(properties))
    return result


def provider_reason(stage, exc):
    if stage == 'PROVIDER_HTTP_RESPONSE' and isinstance(exc, __import__('requests').exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        return 'PROVIDER_HTTP_' + str(status) if status is not None else 'PROVIDER_HTTP_ERROR'
    return {
        'PROVIDER_REQUEST': 'PROVIDER_TRANSPORT_FAILED',
        'PROVIDER_HTTP_RESPONSE': 'PROVIDER_HTTP_ERROR',
        'PROVIDER_ENVELOPE_PARSE': 'PROVIDER_ENVELOPE_INVALID',
        'PROVIDER_CONTENT_PARSE': 'OUTPUT_TRUNCATED' if str(exc) == 'OUTPUT_TRUNCATED'
            else 'PROVIDER_CONTENT_JSON_INVALID' if isinstance(exc, json.JSONDecodeError)
            else str(exc) if str(exc) in ('PROVIDER_CONTENT_NOT_TEXT', 'PROVIDER_CONTENT_MISSING')
            else 'PROVIDER_CONTENT_INVALID',
        'RESPONSE_SCHEMA_VALIDATION': 'RESPONSE_SCHEMA_VALIDATION_FAILED',
    }.get(stage, type(exc).__name__)


def receipt_diagnostic(diagnostic):
    """Safe receipt projection: no raw response, quote or failed clinical value."""
    if not isinstance(diagnostic, dict):
        return None
    allowed = ('kind', 'instance_path', 'schema_path', 'validator', 'schema_sha256',
        'failing_value_sha256', 'allowed_value', 'missing_keys', 'unexpected_keys',
        'content_sha256', 'content_length', 'line', 'column', 'position', 'reason',
        'error_type', 'reason_code')
    return {key: deepcopy(diagnostic[key]) for key in allowed if key in diagnostic}


def _envelope_diagnostic(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('choices'), list) \
            or not raw['choices'] or not isinstance(raw['choices'][0], dict):
        return dict(kind='PROVIDER_ENVELOPE_STRUCTURE',
            error_type='ValueError', reason_code='PROVIDER_ENVELOPE_STRUCTURE_INVALID')
    return None


def replay_transport_evidence(evidence, schema):
    """Recompute saved local parse/Schema facts; never repeat a remote request.

    HTTP status and network categories are evidence preservation checks, not a
    claim that offline replay rediscovered the provider's root cause.
    """
    if not isinstance(evidence, dict) or evidence.get('format') != FORMAT:
        raise ValueError('PROVIDER_ATTEMPT_EVIDENCE_INVALID')
    text = evidence.get('provider_response_text')
    if text is not None:
        if not isinstance(text, str) or hashlib.sha256(text.encode('utf-8')).hexdigest() \
                != evidence.get('provider_response_sha256'):
            raise ValueError('PROVIDER_RESPONSE_BYTES_BINDING_MISMATCH')
        if evidence.get('terminal_stage') == 'SUCCESS':
            snapshot = evidence.get('raw_response_snapshot')
            if snapshot is None:
                raise ValueError('PROVIDER_RAW_RESPONSE_SNAPSHOT_REQUIRED')
            expected_path = 'invocations/%s/attempts/%04d.provider-response.json' % (
                evidence.get('invocation_id'), evidence.get('attempt'))
            if snapshot.get('sha256') != evidence['provider_response_sha256'] \
                    or snapshot.get('relative_path') != expected_path:
                raise ValueError('PROVIDER_RAW_RESPONSE_SNAPSHOT_BINDING_MISMATCH')
    envelope = evidence.get('provider_envelope')
    if evidence.get('terminal_stage') == 'PROVIDER_ENVELOPE_PARSE':
        if evidence.get('diagnostic', {}).get('kind') == 'PROVIDER_ENVELOPE_STRUCTURE':
            if not isinstance(text, str):
                raise ValueError('PROVIDER_ENVELOPE_TEXT_REQUIRED')
            try:
                transmitted = json.loads(text)
            except (TypeError, ValueError) as exc:
                raise ValueError('PROVIDER_ENVELOPE_BYTES_REPLAY_MISMATCH') from exc
            if transmitted != envelope:
                raise ValueError('PROVIDER_ENVELOPE_BYTES_REPLAY_MISMATCH')
    elif envelope is not None:
        if not isinstance(envelope, dict) \
                or evidence.get('provider_envelope_sha256') != digest(envelope) \
                or (_envelope_diagnostic(envelope) is not None
                    and evidence.get('terminal_stage') != 'PROVIDER_ENVELOPE_PARSE'):
            raise ValueError('PROVIDER_ENVELOPE_HASH_BINDING_MISMATCH')
        content = envelope['choices'][0].get('message', {}).get('content')
        saved_content = evidence.get('response_content_text')
        if saved_content is not None and (content != saved_content
                or hashlib.sha256(saved_content.encode('utf-8')).hexdigest()
                != evidence.get('response_content_sha256')):
            raise ValueError('PROVIDER_CONTENT_HASH_BINDING_MISMATCH')
        try:
            transmitted = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise ValueError('PROVIDER_ENVELOPE_BYTES_REPLAY_MISMATCH') from exc
        if transmitted != envelope:
            raise ValueError('PROVIDER_ENVELOPE_BYTES_REPLAY_MISMATCH')
    if evidence.get('response') is not None:
        if not isinstance(envelope, dict) or not isinstance(evidence.get('response_content_text'), str) \
                or evidence.get('response_content_sha256') != hashlib.sha256(
                    evidence['response_content_text'].encode('utf-8')).hexdigest():
            raise ValueError('PROVIDER_SUCCESS_CONTENT_BINDING_REQUIRED')
        if evidence.get('response_sha256') != digest(envelope):
            raise ValueError('PROVIDER_RESPONSE_HASH_BINDING_MISMATCH')
        try:
            parsed = json.loads(evidence.get('response_content_text'))
        except (TypeError, ValueError) as exc:
            raise ValueError('PROVIDER_RESPONSE_CONTENT_REPLAY_MISMATCH') from exc
        if parsed != evidence['response']:
            raise ValueError('PROVIDER_RESPONSE_CONTENT_REPLAY_MISMATCH')
    if evidence.get('terminal_stage') not in TRANSPORT_STAGES + ('SUCCESS',):
        raise ValueError('PROVIDER_ATTEMPT_STAGE_INVALID')
    if evidence.get('terminal_stage') in ('PROVIDER_HTTP_RESPONSE',
            'PROVIDER_ENVELOPE_PARSE', 'PROVIDER_CONTENT_PARSE', 'RESPONSE_SCHEMA_VALIDATION',
            'SUCCESS') and not isinstance(text, str):
        raise ValueError('PROVIDER_RESPONSE_BYTES_REQUIRED')
    if evidence.get('terminal_stage') == 'PROVIDER_HTTP_RESPONSE':
        expected = 'PROVIDER_HTTP_' + str(evidence.get('http_status')) \
            if type(evidence.get('http_status')) is int else 'PROVIDER_HTTP_ERROR'
        if evidence.get('reason_code') != expected:
            raise ValueError('PROVIDER_HTTP_STATUS_BINDING_MISMATCH')
    if evidence.get('terminal_stage') == 'SUCCESS':
        if evidence.get('terminal_status') not in (None, 'SUCCESS'):
            raise ValueError('PROVIDER_CHAIN_EVIDENCE_STATUS_MISMATCH')
        if evidence.get('terminal_status') == 'SUCCESS':
            evidence = dict(evidence)
            evidence.pop('terminal_status', None)
        response = evidence.get('response')
        envelope = evidence.get('provider_envelope')
        if not isinstance(response, dict) or not isinstance(envelope, dict) \
                or evidence.get('response_sha256') != digest(envelope) \
                or evidence.get('provider_envelope_sha256') != digest(envelope):
            raise ValueError('PROVIDER_SUCCESS_RESPONSE_BINDING_MISMATCH')
        try:
            Draft202012Validator(schema).validate(response)
        except ValidationError as exc:
            raise ValueError('PROVIDER_SUCCESS_SCHEMA_REPLAY_MISMATCH') from exc
        return dict(local_failure_recomputed=True, evidence_binding_verified=True,
            success_schema_replayed=True)
    stage = evidence['terminal_stage']
    if stage not in TRANSPORT_STAGES:
        raise ValueError('PROVIDER_ATTEMPT_STAGE_INVALID')
    diagnostic = evidence.get('diagnostic')
    if stage == 'PROVIDER_ENVELOPE_PARSE':
        text = evidence.get('provider_response_text')
        if not isinstance(text, str):
            raise ValueError('PROVIDER_ENVELOPE_TEXT_REQUIRED')
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            if json_diagnostic(exc, text) != diagnostic:
                raise ValueError('PROVIDER_ENVELOPE_DIAGNOSTIC_REPLAY_MISMATCH') from exc
        else:
            if digest(raw) != evidence.get('provider_envelope_sha256') \
                    or raw != evidence.get('provider_envelope') \
                    or _envelope_diagnostic(raw) != diagnostic \
                    or evidence.get('reason_code') != 'PROVIDER_ENVELOPE_INVALID':
                raise ValueError('PROVIDER_ENVELOPE_DIAGNOSTIC_REPLAY_MISMATCH')
    elif stage == 'PROVIDER_CONTENT_PARSE':
        text = evidence.get('response_content_text')
        if text is not None and not isinstance(text, str):
            raise ValueError('PROVIDER_CONTENT_TEXT_REQUIRED')
        envelope = evidence.get('provider_envelope')
        if _envelope_diagnostic(envelope) is not None \
                or digest(envelope) != evidence.get('provider_envelope_sha256'):
            raise ValueError('PROVIDER_CONTENT_ENVELOPE_BINDING_MISMATCH')
        choice = envelope['choices'][0]
        reason = evidence.get('reason_code')
        if reason == 'OUTPUT_TRUNCATED':
            if choice.get('finish_reason') != 'length' or diagnostic is not None:
                raise ValueError('PROVIDER_CONTENT_TRUNCATION_BINDING_MISMATCH')
            return dict(local_failure_recomputed=False, evidence_binding_verified=True,
                failure_category='OUTPUT_TRUNCATED')
        if reason == 'PROVIDER_CONTENT_NOT_TEXT':
            message = choice.get('message')
            if not isinstance(message, dict) or isinstance(message.get('content'), str) \
                    or 'content' not in message or diagnostic is not None:
                raise ValueError('PROVIDER_CONTENT_TYPE_BINDING_MISMATCH')
            return dict(local_failure_recomputed=True, evidence_binding_verified=True,
                failure_category=reason)
        if reason == 'PROVIDER_CONTENT_MISSING':
            message = choice.get('message')
            if not isinstance(message, dict) or 'content' in message or diagnostic is not None:
                raise ValueError('PROVIDER_CONTENT_TYPE_BINDING_MISMATCH')
            return dict(local_failure_recomputed=True, evidence_binding_verified=True,
                failure_category=reason)
        if not isinstance(text, str) or text != choice.get('message', {}).get('content') \
                or hashlib.sha256(text.encode('utf-8')).hexdigest() \
                != evidence.get('response_content_sha256'):
            raise ValueError('PROVIDER_CONTENT_TEXT_REQUIRED')
        try:
            json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            if json_diagnostic(exc, text) != diagnostic:
                raise ValueError('PROVIDER_CONTENT_DIAGNOSTIC_REPLAY_MISMATCH') from exc
        else:
            raise ValueError('PROVIDER_CONTENT_FAILURE_NOT_REPRODUCED')
    elif stage == 'RESPONSE_SCHEMA_VALIDATION':
        if 'response' not in evidence or evidence.get('reason_code') \
                != 'RESPONSE_SCHEMA_VALIDATION_FAILED':
            raise ValueError('PROVIDER_SCHEMA_RESPONSE_REQUIRED')
        try:
            Draft202012Validator(schema).validate(evidence['response'])
        except ValidationError as exc:
            if schema_diagnostic(exc, schema, evidence['response']) != diagnostic:
                raise ValueError('PROVIDER_SCHEMA_DIAGNOSTIC_REPLAY_MISMATCH') from exc
        else:
            raise ValueError('PROVIDER_SCHEMA_FAILURE_NOT_REPRODUCED')
    else:
        return dict(local_failure_recomputed=False, evidence_binding_verified=True,
            failure_category=evidence.get('reason_code'))
    return dict(local_failure_recomputed=True, evidence_binding_verified=True,
        failure_category=evidence.get('reason_code'))


def private_chain_evidence(evidence):
    """Embed one terminal attempt in a private chain, excluding duplicate request."""
    if not isinstance(evidence, dict):
        return None
    original = {key: value for key, value in evidence.items()
                if key != 'evidence_content_sha256'}
    result = {key: deepcopy(value) for key, value in original.items() if key != 'request'}
    result['evidence_content_sha256'] = digest(result)
    return result


def _provider_receipt_binding(evidence, outcome, *, cached=False):
    """Bind a real Client receipt to its terminal attempt without a remote call."""
    receipt = outcome.get('receipt')
    cached = cached or outcome.get('cached', False)
    expected_origin = 'PROVIDER_CLIENT_CACHE' if cached else 'PROVIDER_CLIENT'
    if not isinstance(receipt, dict) or receipt.get('receipt_origin') != expected_origin \
            or receipt.get('invocation_id') != evidence.get('invocation_id') \
            or receipt.get('request_sha256') != evidence.get('transport_request_sha256') \
            or receipt.get('cached') is not cached \
            or receipt.get('provider_invocation_occurred') is cached \
            or not isinstance(receipt.get('attempts'), list) or not receipt['attempts']:
        raise ValueError('PROVIDER_CHAIN_EVIDENCE_RECEIPT_BINDING_MISMATCH')
    usage = receipt.get('usage')
    if usage is not None and (not isinstance(usage, dict)
            or any(key not in {'input_tokens', 'output_tokens', 'total_tokens',
                'cache_read_input_tokens', 'cache_creation_input_tokens',
                'prompt_tokens', 'completion_tokens'}
                or type(value) is not int or not 0 <= value <= 10**12
                for key, value in usage.items())):
        raise ValueError('PROVIDER_RECEIPT_METADATA_UNSAFE')
    model = receipt.get('returned_model')
    if model is not None and (not isinstance(model, str) or not model or len(model) > 128
            or any(ord(char) < 32 or char.isspace() for char in model)):
        raise ValueError('PROVIDER_RECEIPT_METADATA_UNSAFE')
    for key in ('declared_provider_payload_sha256', 'actual_provider_http_body_sha256'):
        declared = evidence.get(key)
        # Offline post-only doubles intentionally leave wire-byte identity
        # unknown. A missing value must never be promoted into a 64-hex claim.
        if declared is None:
            if receipt.get(key) is not None:
                raise ValueError('PROVIDER_CHAIN_TRANSPORT_DOMAIN_MISMATCH')
        elif not isinstance(declared, str) or len(declared) != 64 \
                or receipt.get(key) != declared:
            raise ValueError('PROVIDER_CHAIN_TRANSPORT_DOMAIN_MISMATCH')
    attempt = receipt['attempts'][-1]
    if not isinstance(attempt, dict) or attempt.get('attempt') != evidence.get('attempt') \
            or attempt.get('status') != evidence.get('terminal_status') \
            or attempt.get('status') not in ('SUCCESS', 'FAILED'):
        raise ValueError('PROVIDER_CHAIN_EVIDENCE_ATTEMPT_BINDING_MISMATCH')
    expected_status = 'SUCCESS' if evidence.get('terminal_stage') == 'SUCCESS' else 'FAILED'
    if receipt.get('status') != expected_status or attempt.get('status') != expected_status:
        raise ValueError('PROVIDER_CHAIN_EVIDENCE_STATUS_MISMATCH')
    if receipt.get('provider_transport_verified') is not (expected_status == 'SUCCESS' and not cached):
        raise ValueError('PROVIDER_CHAIN_TRANSPORT_VERIFICATION_FLAG_MISMATCH')
    if expected_status == 'SUCCESS':
        if evidence.get('reason_code') is not None \
                or attempt.get('stage') != 'RESPONSE_SCHEMA_VALIDATION' \
                or outcome.get('stage') != 'SUCCESS' \
                or receipt.get('response_sha256') != evidence.get('response_sha256'):
            raise ValueError('PROVIDER_CHAIN_SUCCESS_BINDING_MISMATCH')
        return receipt, attempt
    if attempt.get('stage') != evidence.get('terminal_stage') \
            or attempt.get('reason_code') != evidence.get('reason_code'):
        raise ValueError('PROVIDER_CHAIN_EVIDENCE_ATTEMPT_BINDING_MISMATCH')
    if 'http_status' in attempt and attempt['http_status'] != evidence.get('http_status'):
        raise ValueError('PROVIDER_CHAIN_HTTP_STATUS_BINDING_MISMATCH')
    if 'http_status' in evidence and evidence.get('terminal_stage') == 'PROVIDER_HTTP_RESPONSE' \
            and attempt.get('http_status') != evidence['http_status']:
        raise ValueError('PROVIDER_CHAIN_HTTP_STATUS_BINDING_MISMATCH')
    expected_safe = receipt_diagnostic(evidence.get('diagnostic')) or {}
    for key, value in expected_safe.items():
        if key != 'kind' and attempt.get(key) != value:
            raise ValueError('PROVIDER_CHAIN_EVIDENCE_RECEIPT_DIAGNOSTIC_MISMATCH')
    if 'provider_response_sha256' in attempt \
            and attempt['provider_response_sha256'] != evidence.get('provider_response_sha256'):
        raise ValueError('PROVIDER_CHAIN_EVIDENCE_RESPONSE_BINDING_MISMATCH')
    return receipt, attempt


def _terminal_status(evidence):
    status = 'SUCCESS' if evidence.get('terminal_stage') == 'SUCCESS' else 'FAILED'
    return dict(evidence, terminal_status=status)


def _validate_attempt_pointer(attempt, evidence, *, code):
    """Bind both fresh and cached outcomes to their archived terminal attempt.

    The full attempt file retains the duplicated private request; its recorded
    identity is the self-hash of the same request-excluding embedded evidence.
    """
    expected_pointer = private_chain_evidence(evidence)['evidence_content_sha256']
    reference = attempt.get('evidence')
    if not isinstance(reference, dict) or reference.get('format') != FORMAT \
            or reference.get('attempt') != evidence.get('attempt') \
            or reference.get('sha256') != expected_pointer \
            or reference.get('relative_path') != 'invocations/%s/attempts/%04d.json' % (
                evidence.get('invocation_id'), evidence.get('attempt')):
        raise ValueError(code)


def validate_provider_attempt_binding(evidence, failure, receipt=None):
    """Bind embedded terminal evidence to the exact invocation, request and stage.

    A self-consistent evidence hash alone must not allow a forged failure to be
    attached to a different provider call. The full request remains private in
    the per-attempt bundle; the chain carries only its transport hash.
    """
    if not isinstance(evidence, dict) or not isinstance(failure, dict) \
            or evidence.get('format') != FORMAT:
        raise ValueError('PROVIDER_CHAIN_EVIDENCE_REQUIRED')
    if evidence.get('terminal_stage') != failure.get('stage') \
            or evidence.get('reason_code') != failure.get('reason_code') \
            or type(evidence.get('attempt')) is not int or evidence['attempt'] < 1:
        raise ValueError('PROVIDER_CHAIN_EVIDENCE_STAGE_OR_REASON_MISMATCH')
    for key, field in (('invocation_id', 'provider_invocation_id'),
                       ('transport_request_sha256', 'transport_request_sha256')):
        if not isinstance(evidence.get(key), str) or not evidence[key] \
                or (field in failure and failure[field] != evidence[key]):
            raise ValueError('PROVIDER_CHAIN_EVIDENCE_REQUEST_BINDING_MISMATCH')
    # Some chain failures use request_sha256 for their canonical structured
    # payload, not for the transport hash of [endpoint, HTTP body]. Validate
    # that distinct domain at its call site; never equate the two hashes here.
    if receipt is None:
        receipt = failure.get('receipt')
    if receipt is not None and receipt.get('receipt_origin') == 'PROVIDER_CLIENT':
        if not isinstance(receipt, dict) \
                or receipt.get('invocation_id') != evidence['invocation_id'] \
                or receipt.get('request_sha256') != evidence['transport_request_sha256'] \
                or receipt.get('status') != 'FAILED':
            raise ValueError('PROVIDER_CHAIN_EVIDENCE_RECEIPT_BINDING_MISMATCH')
        _provider_receipt_binding(_terminal_status(evidence), failure)
        _validate_attempt_pointer(failure['receipt']['attempts'][-1], evidence,
            code='PROVIDER_FAILURE_EVIDENCE_POINTER_MISMATCH')
    elif receipt is not None:
        if not isinstance(receipt, dict) or not isinstance(receipt.get('attempts'), list) \
                or not receipt['attempts']:
            raise ValueError('PROVIDER_CHAIN_EVIDENCE_RECEIPT_BINDING_MISMATCH')
        attempt = receipt['attempts'][-1]
        if not isinstance(attempt, dict) or attempt.get('attempt') != evidence['attempt'] \
                or attempt.get('status') != 'FAILED' \
                or attempt.get('stage') != evidence['terminal_stage'] \
                or attempt.get('reason_code') != evidence['reason_code']:
            raise ValueError('PROVIDER_CHAIN_EVIDENCE_ATTEMPT_BINDING_MISMATCH')
        if 'http_status' in attempt and attempt['http_status'] != evidence.get('http_status'):
            raise ValueError('PROVIDER_CHAIN_HTTP_STATUS_BINDING_MISMATCH')
        if 'http_status' in evidence and evidence.get('terminal_stage') == 'PROVIDER_HTTP_RESPONSE' \
                and attempt.get('http_status') != evidence['http_status']:
            raise ValueError('PROVIDER_CHAIN_HTTP_STATUS_BINDING_MISMATCH')
        expected_safe = receipt_diagnostic(evidence.get('diagnostic')) or {}
        for key, value in expected_safe.items():
            if key != 'kind' and attempt.get(key) != value:
                raise ValueError('PROVIDER_CHAIN_EVIDENCE_RECEIPT_DIAGNOSTIC_MISMATCH')
        if 'provider_response_sha256' in attempt \
                and attempt['provider_response_sha256'] != evidence.get('provider_response_sha256'):
            raise ValueError('PROVIDER_CHAIN_EVIDENCE_RESPONSE_BINDING_MISMATCH')
    return True


def _validate_archived_failed_attempt(prior, evidence, invocation_id, index, schema):
    """Replay every retained failed attempt, not just its pointer shape."""
    expected_path = 'invocations/%s/attempts/%04d.json' % (invocation_id, index)
    reference = prior.get('evidence') if isinstance(prior, dict) else None
    if not isinstance(reference, dict) or reference.get('format') != FORMAT \
            or reference.get('attempt') != index \
            or reference.get('relative_path') != expected_path:
        raise ValueError('PROVIDER_FAILED_ATTEMPT_POINTER_MISMATCH')
    archived = prior.get('archived_evidence')
    if not isinstance(archived, dict):
        raise ValueError('PROVIDER_FAILED_ATTEMPT_EVIDENCE_REQUIRED')
    validate_private_chain_evidence(archived, archived)
    if (archived.get('invocation_id') != invocation_id
            or archived.get('attempt') != index
            or archived.get('terminal_stage') == 'SUCCESS'
            or archived.get('terminal_status') not in (None, 'FAILED')
            or archived.get('terminal_stage') != prior.get('stage')
            or archived.get('reason_code') != prior.get('reason_code')
            or archived.get('provider_response_sha256') != prior.get('provider_response_sha256')):
        raise ValueError('PROVIDER_FAILED_ATTEMPT_BINDING_MISMATCH')
    _validate_attempt_pointer(prior, archived, code='PROVIDER_FAILED_ATTEMPT_POINTER_MISMATCH')
    from scripts.cohort_construction.p2_checkpoint_material_governance.review import MATERIAL_REVIEW_SCHEMA
    replay_transport_evidence(archived, schema)
    return archived


def validate_provider_success_binding(evidence, round_, request, declared_payload, *, cached=False,
        schema=None):
    """Bind a successful round to its exact declared payload and terminal attempt.

    Generic clients and graph-audit callers must replay against the schema that
    accepted their response, not against the material-review schema by default.
    """
    if schema is None:
        from scripts.cohort_construction.p2_checkpoint_material_governance.review import MATERIAL_REVIEW_SCHEMA
        schema = MATERIAL_REVIEW_SCHEMA
    if not isinstance(round_, dict) or not isinstance(request, dict) \
            or round_.get('stage') != 'SUCCESS':
        raise ValueError('PROVIDER_SUCCESS_ROUND_REQUIRED')
    validate_private_chain_evidence(evidence, evidence)
    replay_transport_evidence(evidence, schema)
    from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
    if canonical_sha256(declared_payload) != evidence.get('declared_provider_payload_sha256'):
        raise ValueError('PROVIDER_DECLARED_PAYLOAD_HASH_MISMATCH')
    if round_.get('review') != evidence.get('response'):
        raise ValueError('PROVIDER_CHAIN_REVIEW_RESPONSE_MISMATCH')
    receipt, attempt = _provider_receipt_binding(_terminal_status(evidence), round_, cached=cached)
    if not cached and any('archived_evidence' in prior for prior in receipt['attempts'][:-1]):
        raise ValueError('PROVIDER_FRESH_RETRY_EMBED_REQUIRES_CACHE_READER')
    terminal_number = evidence.get('attempt')
    if attempt.get('attempt') != terminal_number \
            or [prior.get('attempt') for prior in receipt['attempts'][:-1]] != list(range(1, terminal_number)):
        raise ValueError('PROVIDER_RETRY_HISTORY_BINDING_MISMATCH')
    if cached:
        if not isinstance(receipt.get('attempts'), list) or len(receipt['attempts']) < 1:
            raise ValueError('PROVIDER_CACHE_ATTEMPTS_REQUIRED')
        for prior in receipt['attempts'][:-1]:
            if not isinstance(prior.get('archived_evidence'), dict):
                raise ValueError('PROVIDER_CACHE_RETRY_EVIDENCE_REQUIRED')
    for index, prior in enumerate(receipt['attempts'][:-1], 1):
        # A terminal SUCCESS can follow a retry. Preserve the failed attempt's
        # exact archived content and replay its local parse/schema failure too.
        if prior.get('status') != 'FAILED':
            raise ValueError('PROVIDER_RETRY_HISTORY_BINDING_MISMATCH')
        archive_index = receipt.get('attempt_evidence_integrity')
        if not isinstance(archive_index, list) or len(archive_index) != len(receipt['attempts']):
            raise ValueError('PROVIDER_ATTEMPT_INTEGRITY_INDEX_MISMATCH')
        if cached:
            if not isinstance(prior.get('archived_evidence'), dict):
                raise ValueError('PROVIDER_CACHE_RETRY_EVIDENCE_REQUIRED')
            _validate_archived_failed_attempt(prior, evidence, evidence['invocation_id'], index, schema)
        reference = prior['evidence']
        if not isinstance(archive_index, list) or len(archive_index) != len(receipt['attempts']):
            raise ValueError('PROVIDER_ATTEMPT_INTEGRITY_INDEX_MISMATCH')
        entry = archive_index[index - 1]
        if entry != dict(attempt=index, status='FAILED',
                evidence_content_sha256=reference['sha256'],
                provider_response_sha256=prior.get('provider_response_sha256'),
                reason_code=prior.get('reason_code'), stage=prior.get('stage')):
            raise ValueError('PROVIDER_FAILED_ATTEMPT_INTEGRITY_INDEX_MISMATCH')
    _validate_attempt_pointer(attempt, evidence, code='PROVIDER_SUCCESS_EVIDENCE_POINTER_MISMATCH')
    archive_index = receipt.get('attempt_evidence_integrity')
    if not isinstance(archive_index, list) or len(archive_index) != len(receipt['attempts']):
        raise ValueError('PROVIDER_ATTEMPT_INTEGRITY_INDEX_MISMATCH')
    terminal_entry = dict(attempt=attempt['attempt'], status='SUCCESS',
        evidence_content_sha256=attempt['evidence']['sha256'],
        provider_response_sha256=attempt.get('provider_response_sha256'),
        reason_code=attempt.get('reason_code'), stage=attempt.get('stage'))
    if archive_index[-1] != terminal_entry:
        raise ValueError('PROVIDER_SUCCESS_ATTEMPT_INTEGRITY_INDEX_MISMATCH')
    return dict(provider_transport_verified=not cached,
        provider_invocation_occurred=not cached, cached=cached, declared_provider_payload_sha256=evidence['declared_provider_payload_sha256'],
        actual_provider_http_body_sha256=evidence['actual_provider_http_body_sha256'])


def validate_private_chain_evidence(evidence, original):
    if not isinstance(original, dict):
        raise ValueError('PROVIDER_CHAIN_EVIDENCE_HASH_MISMATCH')
    expected = private_chain_evidence(original)
    if not isinstance(evidence, dict) or evidence != expected:
        raise ValueError('PROVIDER_CHAIN_EVIDENCE_HASH_MISMATCH')
