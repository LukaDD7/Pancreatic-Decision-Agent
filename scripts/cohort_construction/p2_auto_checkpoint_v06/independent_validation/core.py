"""Small immutable-file and exact-source binding primitives; no model client."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

VERSION = 'independent-validation-0.1.0'
ORIGINS = {'independent_human', 'ai_preparation', 'synthetic_test'}
DIMENSIONS = (
    'event_detection', 'event_granularity', 'previous_comparison_object',
    'evidence_evolution', 'source_availability', 'decision_time_boundary',
)
ASSESSMENTS = {'SUPPORTED', 'REVISE', 'UNRESOLVED', 'OUT_OF_SCOPE'}


class ValidationError(ValueError):
    """Stable codes deliberately omit clinical content and submitted answers."""


def require(condition, code):
    if not condition:
        raise ValidationError(code)


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha_text(text):
    return sha_bytes(text.encode('utf-8'))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return sha_bytes(canonical(value))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def read(path):
    try:
        return json.loads(Path(path).read_bytes(), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValidationError('NONFINITE_JSON_NUMBER')))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError('INVALID_JSON_FILE') from error


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')
    with path.open('xb') as stream:
        stream.write(data)
    path.chmod(0o600)
    return sha_bytes(data)


def snapshot(path, target):
    data = Path(path).read_bytes()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with target.open('xb') as stream:
        stream.write(data)
    target.chmod(0o600)
    return {'sha256': sha_bytes(data), 'bytes': len(data)}


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', value)
            and value not in {'.', '..'}, 'INVALID_PSEUDONYMOUS_IDENTIFIER')
    return value


def string(value, code='NONEMPTY_TEXT_REQUIRED'):
    require(isinstance(value, str) and bool(value.strip()), code)
    return value


def fields(value, allowed, required=()):
    require(isinstance(value, dict), 'OBJECT_REQUIRED')
    require(not set(value) - set(allowed), 'UNKNOWN_FIELD')
    require(not set(required) - set(value), 'REQUIRED_FIELD_MISSING')


def bound(value, name):
    return {**value, name: digest(value)}


def verify_bound(value, name):
    require(isinstance(value, dict) and name in value, 'HASH_BINDING_MISSING')
    require(value[name] == digest({k: v for k, v in value.items() if k != name}), 'ARTIFACT_HASH_MISMATCH')
    return value


def validate_sources(bundle):
    verify_bound(bundle, 'source_bundle_sha256')
    indexed = {}
    for source in bundle['documents']:
        sid = source['source_id']
        require(sid not in indexed, 'DUPLICATE_SOURCE_ID')
        require(isinstance(source['text'], str) and sha_text(source['text']) == source['source_sha256'],
                'SOURCE_TEXT_HASH_MISMATCH')
        indexed[sid] = source
    require(indexed, 'EMPTY_SOURCE_SCOPE')
    return indexed


def validate_citations(citations, sources):
    require(isinstance(citations, list), 'CITATION_ARRAY_REQUIRED')
    for citation in citations:
        fields(citation, {'source_id', 'source_sha256', 'start', 'end', 'exact_quote'},
               {'source_id', 'source_sha256', 'start', 'end', 'exact_quote'})
        source = sources.get(citation['source_id'])
        require(source is not None, 'CITATION_OUTSIDE_DECLARED_SCOPE')
        start, end = citation['start'], citation['end']
        require(type(start) is int and type(end) is int and 0 <= start < end <= len(source['text']),
                'INVALID_SOURCE_CODEPOINT_SPAN')
        require(source['source_sha256'] == citation['source_sha256'] and
                source['text'][start:end] == citation['exact_quote'], 'VERBATIM_SOURCE_BINDING_MISMATCH')


def origin_status(value):
    require(value.get('origin') in ORIGINS, 'INVALID_REVIEW_ORIGIN')
    require(value.get('status') in {'PENDING', 'INDEPENDENT_COMPLETE'}, 'INVALID_REVIEW_STATUS')
    require(type(value.get('independence_attested')) is bool, 'INDEPENDENCE_BOOLEAN_REQUIRED')
    if value['origin'] != 'independent_human':
        require(value['status'] == 'PENDING' and not value['independence_attested'],
                'AI_OR_SYNTHETIC_CANNOT_COMPLETE_INDEPENDENT_REVIEW')
    if value['status'] == 'INDEPENDENT_COMPLETE':
        require(value['origin'] == 'independent_human' and value['independence_attested'],
                'INDEPENDENT_HUMAN_ATTESTATION_REQUIRED')
    identifier(value.get('reviewer_id'))


def now():
    return datetime.now(timezone.utc).isoformat()


def private_output(path):
    # macOS /private/var is a system temporary root, not the project's private
    # research folder. A later explicit private/ component must be present.
    return 'private' in Path(path).resolve().parts[2:]
