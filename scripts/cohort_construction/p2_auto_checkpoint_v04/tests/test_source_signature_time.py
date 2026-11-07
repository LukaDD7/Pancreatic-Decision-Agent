"""V04-TIMEINDEX001 counterexamples: record-day retrieval is not choice time."""
from copy import deepcopy
import hashlib
import unittest

from scripts.cohort_construction.p2_auto_checkpoint_v04.context import (
    build_evidence_context, build_units, context_for_discovery, source_clock, time_order,
)
from scripts.cohort_construction.p2_auto_checkpoint_v04.source_time import source_record_time
from scripts.cohort_construction.p2_auto_checkpoint_v04.temporal import verify_choice_time
from scripts.cohort_construction.p2_auto_checkpoint_v04.validation import resolve_citations
from scripts.cohort_construction.p2_pathway_replay.core import clock


def source(text, title='消化内镜检查申请单', when=None, sid='REQUEST', kind='document'):
    return dict(source_id=sid, title=title, text=text, kind=kind,
                source_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(),
                clock=clock(when), clock_basis='body_record_time')


class SourceSignatureTimeTests(unittest.TestCase):
    def test_SIG001_signature_day_indexed_schedule_not_substituted(self):
        raw = source('申请目的 诊断性：超声内镜 医师甲\n   2022年09月03日\n备注：2022-9-5做，早上7点送至内镜室。\n')
        original = deepcopy(raw)
        result = source_record_time(raw)
        self.assertEqual(result['clock'], dict(value='2022-09-03', precision='date', raw='2022年09月03日'))
        self.assertEqual(result['clock_basis'], 'request_physician_signature_record_day_proxy')
        self.assertFalse(result['actual_choice_time_established'])
        self.assertFalse(result['availability_certified'])
        self.assertEqual(result['raw_clock'], clock(None))
        self.assertEqual(raw, original)
        q = result['supports'][0]
        self.assertEqual(raw['text'][q['start']:q['end']], q['quote'])
        self.assertEqual(q['source_sha256'], raw['source_sha256'])
        context = q['signature_context']
        self.assertEqual(raw['text'][context['start']:context['end']], context['quote'])
        self.assertEqual(context['quote'], '医师甲\n   2022年09月03日')

    def test_SIG002_separate_signature_blank_lines_and_non_padded_date(self):
        result = source_record_time(source('申请目的：穿刺活检\n医师：乙\n  \n  2023年11月8日\n'))
        self.assertEqual(result['clock']['value'], '2023-11-08')
        self.assertEqual(result['clock']['precision'], 'date')

    def test_SIG003_existing_structured_clock_preserved(self):
        raw = source('医师甲\n2022年9月3日\n', when='2022-09-02 10:49')
        result = source_record_time(raw)
        self.assertEqual(result['clock'], raw['clock'])
        self.assertEqual(result['status'], 'STRUCTURED_CLOCK_PRESERVED')
        self.assertEqual(result['supports'], [])

    def test_SIG004_future_schedule_without_signature_day_not_record_clock(self):
        for text in ['医师甲\n备注：2022年9月5日做检查。\n', '申请目的：拟于2022年9月5日超声内镜。\n',
                     '医师甲\n2022年9月5日拟行超声内镜。\n']:
            with self.subTest(text=text):
                result = source_record_time(source(text))
                self.assertIsNone(result['clock']['value'])
                self.assertEqual(result['status'], 'REQUEST_SIGNATURE_DAY_NOT_FOUND')

    def test_SIG005_history_date_not_signature_date(self):
        result = source_record_time(source('2022年8月31日CT提示需增强。\n医师甲\n'))
        self.assertIsNone(result['clock']['value'])

    def test_SIG006_other_document_roles_not_inferred_from_same_layout(self):
        for title in ['术前知情同意书', '出院证', 'MRI报告', '其他申请单']:
            with self.subTest(title=title):
                result = source_record_time(source('医师甲\n2022年9月3日\n', title=title))
                self.assertIsNone(result['clock']['value'])
                self.assertEqual(result['status'], 'NOT_APPLICABLE')
        self.assertIsNone(source_record_time(source('医师甲\n2022年9月3日\n', kind='imaging'))['clock']['value'])

    def test_SIG007_multiple_conflicting_signature_days_remain_unknown(self):
        result = source_record_time(source('医师甲\n2022年9月3日\n医师乙\n2022年9月4日\n'))
        self.assertIsNone(result['clock']['value'])
        self.assertEqual(result['status'], 'REQUEST_SIGNATURE_DAYS_CONFLICT')
        self.assertEqual(len(result['supports']), 2)

    def test_SIG008_repeated_same_signature_day_not_false_conflict(self):
        result = source_record_time(source('医师甲\n2022年9月3日\n医师乙\n2022年09月03日\n'))
        self.assertEqual(result['clock']['value'], '2022-09-03')
        self.assertEqual(len(result['supports']), 2)

    def test_SIG009_invalid_date_not_filled(self):
        result = source_record_time(source('医师甲\n2022年2月30日\n'))
        self.assertIsNone(result['clock']['value'])
        self.assertIn('INVALID_PHYSICIAN_SIGNATURE_DATE', result['uncertainty'])

    def test_SIG010_source_hash_mismatch_no_proxy(self):
        raw = source('医师甲\n2022年9月3日\n')
        raw['source_sha256'] = '0' * 64
        result = source_record_time(raw)
        self.assertIsNone(result['clock']['value'])
        self.assertEqual(result['status'], 'REQUEST_SIGNATURE_DAY_SOURCE_HASH_MISMATCH')

    def test_SIG011_source_clock_and_discovery_carry_proxy_provenance(self):
        request = source('申请目的：超声内镜\n医师甲\n2022年9月3日\n')
        earlier = source('CT建议进一步检查', title='CT', when='2022-08-31 16:03', sid='CT', kind='imaging')
        same_day = source('同期文书', title='查房记录', when='2022-09-03 09:00', sid='SAME')
        unit = next(u for u in build_units([request, earlier, same_day]) if u['source_ids'] == ['REQUEST'])
        self.assertEqual(source_clock(request)['value'], '2022-09-03')
        current = unit['original_texts'][0]
        self.assertEqual(current['record_time_provenance']['raw_clock'], clock(None))
        data = context_for_discovery(unit, [request, earlier, same_day])
        self.assertEqual({x['source_id'] for x in data['prior_source_catalog']}, {'CT'})
        self.assertEqual(next(x for x in data['uncertain_time_source_catalog'] if x['source_id'] == 'SAME')['relation_to_cutoff'],
                         'SAME_DAY_UNORDERED')
        self.assertEqual(time_order(source_clock(request), '2022-09-03 15:00'), 'SAME_DAY_UNORDERED')

    def test_SIG012_signature_cannot_validate_model_actual_choice_time(self):
        request = source('申请目的：超声内镜\n医师甲\n2022年9月3日\n')
        citations = resolve_citations([dict(source_id='REQUEST', quote='2022年9月3日')], [request])
        event = dict(source_id='REQUEST', choice_time='2022-09-03 10:49', time_basis='explicit_documented_choice')
        result = verify_choice_time(event, [request], citations)
        self.assertIsNone(result['choice_time'])
        self.assertFalse(result['choice_time_verified'])
        self.assertEqual(result['time_basis'], 'record_time_proxy')

    def test_SIG013_missing_actual_choice_keeps_record_proxy_available_for_context(self):
        request = source('申请目的：超声内镜\n医师甲\n2022年9月3日\n')
        earlier = source('CT建议进一步检查', title='CT', when='2022-08-31 16:03', sid='CT', kind='imaging')
        event = dict(source_id='REQUEST', source_ids=['REQUEST'], choice_time=None, time_basis='unknown',
                     action_citations={'valid': []}, prechoice_citations={'valid': []}, postchoice_citations={'valid': []})
        checked = verify_choice_time(event, [request, earlier], {})
        event['time_basis'] = checked['time_basis']
        self.assertEqual(event['time_basis'], 'record_time_proxy')
        context = build_evidence_context(event, [request, earlier])
        self.assertEqual(context['cutoff']['value'], '2022-09-03')
        self.assertTrue(any(x['source_id'] == 'CT' for x in context['blocks']))
        self.assertFalse(context['blind_replay_ready'])

    def test_SIG014_historical_event_not_forced_into_current_record_day(self):
        request = source('申请目的：超声内镜\n医师甲\n2022年9月3日\n')
        checked = verify_choice_time(dict(source_id='REQUEST', choice_time=None, time_basis='historical_unknown'), [request], {})
        self.assertEqual(checked['time_basis'], 'historical_unknown')
        self.assertIsNone(checked['choice_time'])

    def test_SIG015_blank_signature_field_not_accepted(self):
        for text in ['医师：   \n2022年9月3日\n', '医师签名：   \n2022年9月3日\n']:
            with self.subTest(text=text):
                checked = source_record_time(source(text))
                self.assertIsNone(checked['clock']['value'])
                self.assertIn('PHYSICIAN_SIGNATURE_NAME_FIELD_EMPTY', checked['uncertainty'])

    def test_SIG016_unsigned_pending_and_placeholder_never_supply_clock(self):
        placeholders = ['待签名', '未签', '未签名', '待签字', '尚未签署',
                        '___', 'N/A', 'PENDING', 'TBD', '空白', '待补签']
        for placeholder in placeholders:
            text = f'申请目的：超声内镜\n医师：{placeholder}\n2022年9月3日\n'
            with self.subTest(placeholder=placeholder):
                result = source_record_time(source(text))
                self.assertIsNone(result['clock']['value'])
                self.assertEqual(result['status'], 'REQUEST_SIGNATURE_NOT_EXECUTED_OR_PLACEHOLDER')
                self.assertIn('PHYSICIAN_SIGNATURE_UNSIGNED_PENDING_OR_PLACEHOLDER', result['uncertainty'])
                self.assertEqual(result['supports'], [])
                self.assertIn('V04-TIMEINDEX002', result['rule_ids'])

    def test_SIG017_placeholder_signature_cannot_cutoff_evidence_context(self):
        for placeholder in ['待签名', '未签', '___']:
            with self.subTest(placeholder=placeholder):
                request = source(f'申请目的：超声内镜\n医师：{placeholder}\n2022年9月3日\n')
                earlier = source('CT建议进一步检查', title='CT', when='2022-08-31 16:03', sid='CT', kind='imaging')
                later = source('后续报告', title='CT', when='2022-09-02 16:03', sid='LATE', kind='imaging')
                event = dict(source_id='REQUEST', source_ids=['REQUEST'], choice_time=None,
                             time_basis='record_time_proxy', action_citations={'valid': []},
                             prechoice_citations={'valid': []}, postchoice_citations={'valid': []})
                context = build_evidence_context(event, [request, earlier, later])
                self.assertIsNone(context['cutoff']['value'])
                self.assertEqual(context['blocks'], [])
                self.assertFalse(context['blind_replay_ready'])
                self.assertEqual(
                    {x['source_id'] for x in context['ledger'] if x['disposition'] == 'REVIEW'},
                    {'CT', 'LATE'})

    def test_SIG018_valid_signature_still_establishes_day_only_cutoff(self):
        request = source('申请目的：超声内镜\n医师：甲\n2022年9月3日\n')
        earlier = source('CT建议进一步检查', title='CT', when='2022-08-31 16:03', sid='CT', kind='imaging')
        later = source('后续报告', title='CT', when='2022-09-02 16:03', sid='LATE', kind='imaging')
        result = source_record_time(request)
        self.assertEqual(result['status'], 'PHYSICIAN_SIGNATURE_RECORD_DAY_PROXY')
        event = dict(source_id='REQUEST', source_ids=['REQUEST'], choice_time=None,
                     time_basis='record_time_proxy', action_citations={'valid': []},
                     prechoice_citations={'valid': []}, postchoice_citations={'valid': []})
        context = build_evidence_context(event, [request, earlier, later])
        self.assertEqual(context['cutoff']['value'], '2022-09-03')
        self.assertEqual(context['cutoff']['precision'], 'date')
        self.assertIn('CT', {x['source_id'] for x in context['blocks']})
        self.assertIn('LATE', {x['source_id'] for x in context['blocks']})


if __name__ == '__main__':
    unittest.main()
