"""Pool wire-encoding regressions: lossless, fail-closed, no authority change.

The encoder changes only how repeated attributes are represented.  These tests
pin the properties that make that safe: exact round trip, refusal to invent a
value when an attribute is not actually constant, and refusal to drop a field
the codec does not yet classify.
"""
import json
import unittest

from scripts.cohort_construction.p2_auto_checkpoint_v04 import payload_codec as codec


def block(block_id='A:0:4', source_id='A', start=0, end=4, text='静脉受侵', **extra):
    row = dict(block_id=block_id, source_id=source_id, start=start, end=end, text=text,
               interval='current_prechoice_fact',
               evidence_object_id='EO1', test_execution_anchor_id='TX1',
               kind='document', source_title='手术记录',
               clock={'value': '2022-01-02T10:00:00+08:00'},
               available_clock_proxy={'value': '2022-01-02T10:00:00+08:00'},
               execution_time=None, publication_time=None,
               publication_status='UNKNOWN', availability_status='PRIOR_SOURCE_PROXY_UNCERTIFIED',
               priority=1, admissibility_role='source_context_requires_semantic_time_review',
               availability_certified=False, reading_before_choice_certified=False,
               publication_before_choice_certified=False, time_provenance={})
    row.update(extra)
    return row


class PayloadCodecTests(unittest.TestCase):
    def test_ENC001_round_trip_is_exact_for_a_repeated_pool(self):
        pool = [block('A:0:4'), block('A:4:8', start=4, end=8, text='遂决定切除'),
                block('B:0:3', source_id='B', start=0, end=3, text='云雾状')]
        encoded = codec.encode_pool(pool)
        self.assertEqual(codec.decode_pool(encoded), pool)
        # Re-encoding is refused rather than silently applied twice; a decoded
        # pool is once again a valid encode input.
        self.assertEqual(codec.decode_pool(codec.encode_pool(codec.decode_pool(encoded))), pool)

    def test_ENC002_encoding_shrinks_repetition_without_touching_text(self):
        pool = [block('A:%d:%d' % (i, i + 4), start=i, end=i + 4, text='原文%d' % i)
                for i in range(20)]
        before = len(json.dumps(pool, ensure_ascii=False))
        after = len(json.dumps(codec.encode_pool(pool), ensure_ascii=False))
        self.assertLess(after, before / 2)
        self.assertEqual([b['text'] for b in codec.decode_pool(codec.encode_pool(pool))],
                         [b['text'] for b in pool])

    def test_ENC003_block_level_interval_may_differ_within_one_source(self):
        # One source legitimately carries two interval kinds; this must encode.
        pool = [block('A:0:4'), block('A:4:8', start=4, end=8,
                                      interval='current_prechoice_complete_region')]
        self.assertEqual(codec.decode_pool(codec.encode_pool(pool)), pool)

    def test_ENC004_non_constant_source_field_is_refused_not_guessed(self):
        pool = [block('A:0:4'), block('A:4:8', start=4, end=8,
                                      clock={'value': '2023-05-06T09:00:00+08:00'})]
        with self.assertRaises(ValueError) as raised:
            codec.encode_pool(pool)
        self.assertIn('POOL_SOURCE_FIELD_VARIES', str(raised.exception))

    def test_ENC005_non_constant_pool_field_is_refused_not_guessed(self):
        pool = [block('A:0:4'), block('A:4:8', start=4, end=8,
                                      admissibility_role='different_role')]
        with self.assertRaises(ValueError) as raised:
            codec.encode_pool(pool)
        self.assertIn('POOL_CONSTANT_FIELD_VARIES', str(raised.exception))

    def test_ENC006_unclassified_field_fails_closed(self):
        pool = [block('A:0:4', new_authority_bit=True)]
        with self.assertRaises(ValueError) as raised:
            codec.encode_pool(pool)
        self.assertIn('POOL_BLOCK_FIELD_UNCLASSIFIED', str(raised.exception))

    def test_ENC007_duplicate_or_missing_block_identity_fails_closed(self):
        with self.assertRaises(ValueError):
            codec.encode_pool([block('A:0:4'), block('A:0:4')])
        with self.assertRaises(ValueError):
            codec.encode_pool([block('')])

    def test_ENC008_empty_pool_stays_a_plain_list(self):
        self.assertEqual(codec.encode_pool([]), [])
        self.assertEqual(codec.decode_pool([]), [])

    def test_ENC009_truncated_encoding_is_refused(self):
        encoded = codec.encode_pool([block('A:0:4')])
        broken = dict(encoded, blocks=[['A:0:4', 'A', 0]])
        with self.assertRaises(ValueError):
            codec.decode_pool(broken)

    def test_ENC010_payload_wrappers_do_not_mutate_the_input(self):
        pool = [block('A:0:4')]
        payload = {'instruction': 'x', 'evidence_source_pool': pool}
        encoded = codec.encode_payload(payload)
        self.assertIsInstance(encoded['evidence_source_pool'], dict)
        self.assertIsInstance(payload['evidence_source_pool'], list)
        self.assertEqual(codec.decode_payload(encoded), payload)
        self.assertEqual(payload['evidence_source_pool'], pool)

    def test_ENC011_authority_fields_survive_round_trip_unchanged(self):
        pool = [block('A:0:4')]
        decoded = codec.decode_pool(codec.encode_pool(pool))[0]
        for field in ('availability_certified', 'reading_before_choice_certified',
                      'publication_before_choice_certified', 'admissibility_role',
                      'availability_status', 'time_provenance'):
            self.assertEqual(decoded[field], pool[0][field])

    def test_ENC012_columns_are_part_of_the_encoding_identity(self):
        encoded = codec.encode_pool([block('A:0:4')])
        broken = dict(encoded, block_columns=list(reversed(encoded['block_columns'])))
        with self.assertRaises(ValueError) as raised:
            codec.decode_pool(broken)
        self.assertIn('POOL_ENCODING_COLUMNS_MISMATCH', str(raised.exception))

    def test_ENC013_already_encoded_input_is_not_re_encoded(self):
        encoded = codec.encode_pool([block('A:0:4')])
        with self.assertRaises(ValueError):
            codec.encode_pool(encoded)

    def test_ENC014_transport_projection_names_the_format_it_applied(self):
        pool = [block('A:%d:%d' % (i, i + 4), start=i, end=i + 4, text='原文%d' % i)
                for i in range(10)]
        payload = {'instruction': 'x', 'evidence_source_pool': pool}
        wire, fmt = codec.transport_projection(payload, 'source_table')
        self.assertEqual(fmt, codec.POOL_ENCODING)
        self.assertEqual(codec.decode_payload(wire), payload)
        self.assertEqual(payload['evidence_source_pool'], pool)
        plain, fmt = codec.transport_projection(payload, 'expanded')
        self.assertEqual(fmt, codec.EXPANDED_POOL_FORMAT)
        self.assertEqual(plain, payload)

    def test_ENC014b_a_pool_too_small_to_benefit_is_sent_expanded(self):
        # The encoding header can outweigh one block. Capacity must never
        # shrink, so the smaller representation is sent and named.
        payload = {'instruction': 'x', 'evidence_source_pool': [block('A:0:4')]}
        wire, fmt = codec.transport_projection(payload, 'source_table')
        self.assertEqual(fmt, codec.EXPANDED_POOL_FORMAT)
        self.assertEqual(wire, payload)

    def test_ENC015_transport_projection_never_claims_an_encoding_it_did_not_apply(self):
        # A compact-repair window row has a different registered shape. It must
        # travel unchanged and be reported as unencoded, never approximated.
        window = {'block_id': 'A:0:4', 'source_id': 'A', 'start': 0, 'end': 4,
                  'text': '静脉受侵', 'authority_basis': 'ORIGINAL_PARENT_EVIDENCE_POOL',
                  'presentation_role': 'EXACT_ORIGINAL_SOURCE_WINDOW_NOT_NEW_SOURCE_AUTHORITY'}
        payload = {'instruction': 'x', 'evidence_source_pool': [window]}
        wire, fmt = codec.transport_projection(payload, 'source_table')
        self.assertEqual(fmt, codec.UNENCODED_POOL_FORMAT)
        self.assertEqual(wire['evidence_source_pool'], [window])
        self.assertEqual(json.dumps(wire, ensure_ascii=False),
                         json.dumps(payload, ensure_ascii=False))

    def test_ENC016_transport_projection_refuses_to_invent_a_missing_field(self):
        # Missing a classified field: encoding would decode it back as null, so
        # the exact bytes are preserved instead of a silently altered block.
        partial = {'block_id': 'A:0:4', 'source_id': 'A', 'start': 0, 'end': 4, 'text': '原文'}
        payload = {'instruction': 'x', 'evidence_source_pool': [partial]}
        wire, fmt = codec.transport_projection(payload, 'source_table')
        self.assertEqual(fmt, codec.UNENCODED_POOL_FORMAT)
        self.assertEqual(wire, payload)

    def test_ENC017_unknown_encoding_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            codec.transport_projection({'evidence_source_pool': []}, 'gzip')
        self.assertEqual(str(raised.exception), 'REVIEW_PAYLOAD_POOL_ENCODING_INVALID')

    def test_ENC018_a_payload_without_a_pool_is_not_given_one(self):
        # Adding an empty pool would change the hash of a differently shaped
        # request. Both directions must leave the key set untouched.
        payload = {'instruction': 'x'}
        self.assertEqual(codec.decode_payload(payload), payload)
        self.assertEqual(codec.encode_payload(payload), payload)


if __name__ == '__main__':
    unittest.main()