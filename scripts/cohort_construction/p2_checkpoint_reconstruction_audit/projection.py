"""Lossless compact transport for graph-audit requests.

The frozen request and graph remain authoritative. Only repeated JSON subtrees
are replaced by references for provider transport. The original complete request
is reconstructed before chain persistence, evidence validation and no-model
replay. No source text, event or clinical assertion is omitted.
"""
from copy import deepcopy
from hashlib import sha256
import json


COMPACT_FORMAT = 'candidate-reconstruction-audit-compact-json-0.2.0'
PROJECTION_SCOPE = 'DUPLICATE_JSON_SUBTREES_ONLY_NO_SOURCE_EVENT_OR_CLINICAL_FIELD_OMISSION'
_MIN_CONTAINER_BYTES = 256
_MIN_STRING_BYTES = 128


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _reference(key):
    return {'$compact_json_ref': key}


def compact_graph_audit_request(request):
    """Recursively reference duplicate values; preserve exact JSON semantics.

    Blocks themselves are projected so shared evidence and source-route objects
    are stored once even when they occur inside different events. References are
    allowed in multiple places, but cyclic block references fail closed.
    """
    counts, originals = {}, {}

    def digest(value):
        data = _canonical(value)
        key = 'sha256:' + sha256(data).hexdigest()
        return key, len(data)

    def collect(value):
        if isinstance(value, (dict, list)):
            key, size = digest(value)
            if size > _MIN_CONTAINER_BYTES:
                counts[key] = counts.get(key, 0) + 1
                if key in originals and originals[key] != value:
                    raise ValueError('GRAPH_AUDIT_COMPACT_DIGEST_COLLISION')
                originals.setdefault(key, value)
            children = value.values() if isinstance(value, dict) else value
            for child in children:
                collect(child)
        elif isinstance(value, str) and len(value.encode('utf-8')) > _MIN_STRING_BYTES:
            key, _ = digest(value)
            counts[key] = counts.get(key, 0) + 1
            originals.setdefault(key, value)

    collect(request)

    def substitute(value):
        if isinstance(value, (dict, list)):
            key, size = digest(value)
            if size > _MIN_CONTAINER_BYTES and counts[key] > 1:
                return _reference(key)
            if isinstance(value, dict):
                return {key: substitute(child) for key, child in value.items()}
            return [substitute(child) for child in value]
        if isinstance(value, str) and len(value.encode('utf-8')) > _MIN_STRING_BYTES:
            key, _ = digest(value)
            if counts[key] > 1:
                return _reference(key)
        return deepcopy(value)

    payload = substitute(request)
    # All duplicates form a finite acyclic set because a value is strictly
    # larger than each descendant. Materialize each once, retaining the block's
    # own container while replacing only its children.
    blocks = {}
    for key, value in originals.items():
        if counts[key] <= 1:
            continue
        if isinstance(value, dict):
            blocks[key] = {child_key: substitute(child) for child_key, child in value.items()}
        elif isinstance(value, list):
            blocks[key] = [substitute(child) for child in value]
        else:
            blocks[key] = deepcopy(value)
    envelope = dict(format=COMPACT_FORMAT,
        original_request_sha256=sha256(_canonical(request)).hexdigest(),
        projection_scope=PROJECTION_SCOPE, blocks=blocks, payload=payload)
    if len(_canonical(envelope)) >= len(_canonical(request)):
        return deepcopy(request)
    restored = expand_graph_audit_request(envelope)
    if restored != request:
        raise ValueError('GRAPH_AUDIT_COMPACT_ROUNDTRIP_MISMATCH')
    return envelope


def expand_graph_audit_request(envelope):
    """Deterministically restore references; reject malformed/cyclic/tampered data."""
    if not isinstance(envelope, dict) or envelope.get('format') != COMPACT_FORMAT:
        raise ValueError('GRAPH_AUDIT_COMPACT_FORMAT_INVALID')
    if set(envelope) != {'format', 'original_request_sha256', 'projection_scope', 'blocks', 'payload'}:
        raise ValueError('GRAPH_AUDIT_COMPACT_FIELDS_INVALID')
    if envelope.get('projection_scope') != PROJECTION_SCOPE:
        raise ValueError('GRAPH_AUDIT_COMPACT_SCOPE_INVALID')
    if not isinstance(envelope.get('original_request_sha256'), str) \
            or len(envelope['original_request_sha256']) != 64:
        raise ValueError('GRAPH_AUDIT_COMPACT_ORIGINAL_HASH_INVALID')
    blocks = envelope['blocks']
    if not isinstance(blocks, dict):
        raise ValueError('GRAPH_AUDIT_COMPACT_BLOCKS_INVALID')
    if not isinstance(envelope['payload'], (dict, list, str)):
        raise ValueError('GRAPH_AUDIT_COMPACT_PAYLOAD_INVALID')
    for key in blocks:
        if not isinstance(key, str) or not key.startswith('sha256:') or len(key) != 71:
            raise ValueError('GRAPH_AUDIT_COMPACT_BLOCK_KEY_INVALID')
    memo = {}

    def expand(value, stack=()):
        if isinstance(value, dict) and set(value) == {'$compact_json_ref'}:
            key = value['$compact_json_ref']
            if not isinstance(key, str) or key not in blocks or key in stack:
                raise ValueError('GRAPH_AUDIT_COMPACT_REFERENCE_INVALID')
            if key not in memo:
                memo[key] = expand(blocks[key], stack + (key,))
                restored = memo[key]
                if 'sha256:' + sha256(_canonical(restored)).hexdigest() != key:
                    raise ValueError('GRAPH_AUDIT_COMPACT_BLOCK_HASH_MISMATCH')
            return deepcopy(memo[key])
        if isinstance(value, dict):
            return {key: expand(child, stack) for key, child in value.items()}
        if isinstance(value, list):
            return [expand(child, stack) for child in value]
        return deepcopy(value)

    result = expand(envelope['payload'])
    if sha256(_canonical(result)).hexdigest() != envelope['original_request_sha256']:
        raise ValueError('GRAPH_AUDIT_COMPACT_ORIGINAL_HASH_MISMATCH')
    return result
