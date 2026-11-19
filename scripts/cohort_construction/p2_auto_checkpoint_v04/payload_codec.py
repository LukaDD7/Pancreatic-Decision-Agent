"""Lossless wire compaction of the review evidence pool.

The review payload repeats, for every evidence block, attributes that are
constant for the whole request (admissibility, certification flags, time
provenance) or constant per source (clock, execution/publication time, kind,
priority).  On this corpus that repetition is most of the request: the median
review request was 237,044 characters while its block text was only 32,050.

This module rewrites only the *representation* of ``evidence_source_pool``.
No block, source, character range or quotation is added, removed, reordered or
altered, and ``decode`` reproduces the exact original list.  It is therefore a
transport encoding, never a change of evidence authority: whatever the
original pool allowed, the encoded pool still allows, and nothing else.

Encoding never silently drops a field.  If a source-level attribute is not in
fact constant within its source, or a pool-level attribute varies across the
pool, ``encode_pool`` raises instead of guessing which value to keep.
"""
import json
from copy import deepcopy

POOL_ENCODING = 'source-table-0.1.0'
EXPANDED_POOL_FORMAT = 'expanded-0.1.0'
# A pool this codec does not own (compact-repair windows, partial fixtures).
UNENCODED_POOL_FORMAT = 'unencoded-0.1.0'

# Per-block identity and text.  ``interval`` is block-level, not source-level:
# one source legitimately carries both ``current_prechoice_fact`` and
# ``current_prechoice_complete_region`` regions.
BLOCK_FIELDS = (
    'block_id', 'source_id', 'start', 'end', 'text', 'interval',
    'evidence_object_id', 'test_execution_anchor_id',
)
# Attributes that must be identical for every block of the same source.
SOURCE_FIELDS = (
    'kind', 'source_title', 'clock', 'available_clock_proxy', 'execution_time',
    'publication_time', 'publication_status', 'availability_status', 'priority',
)
# Attributes that must be identical for every block in the pool.
POOL_CONSTANT_FIELDS = (
    'admissibility_role', 'availability_certified',
    'reading_before_choice_certified', 'publication_before_choice_certified',
    'time_provenance',
)


def _canonical(value):
    """A comparable, order-insensitive form of one attribute value."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def is_encoded(pool):
    return isinstance(pool, dict) and pool.get('encoding') == POOL_ENCODING


def encode_pool(blocks):
    """Return the compact representation of an evidence block list."""
    if is_encoded(blocks):
        raise ValueError('POOL_ALREADY_ENCODED')
    if not isinstance(blocks, list):
        raise ValueError('POOL_BLOCKS_MUST_BE_LIST')
    if not blocks:
        # An empty pool carries no repetition to remove. Keep the plain list so
        # every consumer keeps one shape for the empty case.
        return []
    seen = set()
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError('POOL_BLOCK_MUST_BE_OBJECT')
        unknown = set(block) - set(BLOCK_FIELDS) - set(SOURCE_FIELDS) \
            - set(POOL_CONSTANT_FIELDS)
        if unknown:
            # A new field must be classified explicitly, not silently dropped.
            raise ValueError('POOL_BLOCK_FIELD_UNCLASSIFIED:' + ','.join(sorted(unknown)))
        key = block.get('block_id')
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError('POOL_BLOCK_ID_INVALID_OR_DUPLICATED')
        seen.add(key)

    constants = {}
    for field in POOL_CONSTANT_FIELDS:
        values = {_canonical(block.get(field)) for block in blocks}
        if len(values) != 1:
            raise ValueError('POOL_CONSTANT_FIELD_VARIES:' + field)
        constants[field] = deepcopy(blocks[0].get(field))

    sources = {}
    for block in blocks:
        source_id = block.get('source_id')
        if not isinstance(source_id, str) or not source_id:
            raise ValueError('POOL_SOURCE_ID_INVALID')
        row = [deepcopy(block.get(field)) for field in SOURCE_FIELDS]
        if source_id not in sources:
            sources[source_id] = row
        elif [_canonical(value) for value in sources[source_id]] != \
                [_canonical(value) for value in row]:
            raise ValueError('POOL_SOURCE_FIELD_VARIES:' + source_id)

    return {
        'encoding': POOL_ENCODING,
        'block_columns': list(BLOCK_FIELDS),
        'blocks': [[deepcopy(block.get(field)) for field in BLOCK_FIELDS]
                   for block in blocks],
        'source_columns': list(SOURCE_FIELDS),
        'sources': sources,
        'constant_fields': constants,
    }


def decode_pool(pool):
    """Return the original block list from either representation."""
    if not is_encoded(pool):
        return pool
    if pool.get('block_columns') != list(BLOCK_FIELDS) \
            or pool.get('source_columns') != list(SOURCE_FIELDS):
        raise ValueError('POOL_ENCODING_COLUMNS_MISMATCH')
    constants = pool.get('constant_fields')
    sources = pool.get('sources')
    rows = pool.get('blocks')
    if not isinstance(constants, dict) or not isinstance(sources, dict) \
            or not isinstance(rows, list):
        raise ValueError('POOL_ENCODING_STRUCTURE_INVALID')
    if set(constants) != set(POOL_CONSTANT_FIELDS):
        raise ValueError('POOL_ENCODING_CONSTANT_FIELDS_MISMATCH')
    decoded = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(BLOCK_FIELDS):
            raise ValueError('POOL_ENCODING_BLOCK_ROW_INVALID')
        block = dict(zip(BLOCK_FIELDS, deepcopy(row)))
        source_id = block.get('source_id')
        if source_id not in sources:
            raise ValueError('POOL_ENCODING_SOURCE_ROW_MISSING')
        values = sources[source_id]
        if not isinstance(values, list) or len(values) != len(SOURCE_FIELDS):
            raise ValueError('POOL_ENCODING_SOURCE_ROW_INVALID')
        block.update(dict(zip(SOURCE_FIELDS, deepcopy(values))))
        block.update(deepcopy(constants))
        decoded.append(block)
    if len({block['block_id'] for block in decoded}) != len(decoded):
        raise ValueError('POOL_ENCODING_BLOCK_ID_DUPLICATED')
    return decoded


def encode_payload(payload):
    """Return a copy of ``payload`` whose pool uses the compact representation."""
    if not isinstance(payload, dict):
        raise ValueError('PAYLOAD_MUST_BE_OBJECT')
    result = dict(payload)
    if 'evidence_source_pool' in payload:
        result['evidence_source_pool'] = encode_pool(payload['evidence_source_pool'])
    return result


def decode_payload(payload):
    """Return a copy of ``payload`` whose pool is the original block list."""
    if not isinstance(payload, dict):
        raise ValueError('PAYLOAD_MUST_BE_OBJECT')
    result = dict(payload)
    if 'evidence_source_pool' in payload:
        # Never invent the key: a payload without a pool is a different
        # registered shape, and adding an empty one would change its hash.
        result['evidence_source_pool'] = decode_pool(payload['evidence_source_pool'])
    return result


def transport_projection(payload, encoding='source_table'):
    """Return ``(wire_payload, format)`` for one outgoing provider request.

    Only the pool this codec actually owns is rewritten. A pool with a
    different registered shape -- most importantly the compact-repair window
    list -- is returned unchanged and named as such, so a caller never records
    an encoding it did not apply. The returned dict is always a copy.

    A pool that cannot be represented exactly (an unclassified field, a
    genuinely non-constant source attribute, a duplicated block id, or a block
    object that lacks a classified field and would decode with that field
    invented as null) is sent in its original expanded form. This is the
    conservative direction: the request is then byte-identical in shape to what
    the previous version sent, and the caller records the format so a silent
    loss never goes unnoticed.
    """
    if encoding == 'expanded':
        return decode_payload(payload), EXPANDED_POOL_FORMAT
    if encoding != 'source_table':
        raise ValueError('REVIEW_PAYLOAD_POOL_ENCODING_INVALID')
    plain = decode_payload(payload)
    try:
        encoded = encode_payload(payload)
    except ValueError:
        return plain, UNENCODED_POOL_FORMAT
    if decode_pool(encoded['evidence_source_pool']) != payload.get('evidence_source_pool'):
        return plain, UNENCODED_POOL_FORMAT
    # A tiny or single-block pool can be outweighed by the encoding header. Send
    # the smaller of the two so the budget can never shrink capacity, only grow
    # it, and so the recorded format always names the bytes actually sent.
    if len(json.dumps(encoded, ensure_ascii=False)) >= len(json.dumps(plain, ensure_ascii=False)):
        return plain, EXPANDED_POOL_FORMAT
    return encoded, POOL_ENCODING


def round_trip_ok(blocks):
    """True when encoding then decoding reproduces the input exactly."""
    try:
        encoded = encode_pool(blocks)
    except ValueError:
        return False
    return decode_pool(encoded) == blocks