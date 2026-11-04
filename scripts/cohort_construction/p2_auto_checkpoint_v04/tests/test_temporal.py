"""Synthetic temporal counterexamples; no case ids, labels, or model fixtures."""
from copy import deepcopy
import hashlib
import unittest

from scripts.cohort_construction.p2_auto_checkpoint_v04.temporal import verify_choice_time
from scripts.cohort_construction.p2_auto_checkpoint_v04.validation import resolve_citations


def source(text, sid='D', kind='document'):
    return dict(source_id=sid,text=text,kind=kind,source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                clock={'value':'2022-03-04 17:56:00','precision':'minute'})


def check(text, proposed='2022-03-04 10:30', time_basis='explicit_documented_choice', quote=None, **event_fields):
    s=source(text)
    citations=resolve_citations([{'source_id':'D','quote':quote or text}], [s])
    event=dict(source_id='D',choice_time=proposed,time_basis=time_basis)
    event.update(event_fields)
    return verify_choice_time(event,[s],citations)


class TemporalTests(unittest.TestCase):
    def test_TMP001_explicit_decision_time_iso_minute(self):
        result=check('决定时间：2022-03-04 10:30。决定暂缓。')
        self.assertEqual(result['choice_time'],'2022-03-04 10:30')
        self.assertEqual(result['choice_time_precision'],'minute')
        self.assertTrue(result['choice_time_verified'])

    def test_TMP002_chinese_date_and_time_normalized_without_extra_seconds(self):
        result=check('决策时间：2022年3月4日10时30分。')
        self.assertEqual(result['choice_time'],'2022-03-04 10:30')
        self.assertFalse(result['choice_time'].endswith(':00'))

    def test_TMP003_explicit_assessment_date_stays_date(self):
        result=check('评估时间：2022年3月4日。', proposed='2022-03-04')
        self.assertEqual(result['choice_time'],'2022-03-04')
        self.assertEqual(result['choice_time_precision'],'date')
        self.assertIn('ACTUAL_CHOICE_DATE_ONLY_MINUTE_UNKNOWN',result['uncertainty'])

    def test_TMP004_narrative_at_time_decided(self):
        result=check('患者于2022年3月4日10:30经讨论后决定暂缓。')
        self.assertEqual(result['choice_time'],'2022-03-04 10:30')

    def test_TMP005_narrative_date_made_decision_without_at(self):
        result=check('2022-03-04 10:30作出暂缓决定。')
        self.assertTrue(result['choice_time_verified'])

    def test_TMP006_record_header_plus_action_not_actual_time(self):
        result=check('记录时间：2022-03-04 10:30\n患者状态稳定。决定继续。')
        self.assertIsNone(result['choice_time'])
        self.assertEqual(result['time_basis'],'record_time_proxy')
        self.assertFalse(result['choice_time_verified'])

    def test_TMP007_surgery_start_not_choice_time(self):
        result=check('手术时间：2022-03-04 10:30-2022-03-04 14:00\n探查后遂决定切除。', document_profile='operative')
        self.assertIsNone(result['choice_time'])
        self.assertEqual(result['time_basis'],'operative_phase')

    def test_TMP008_planned_execution_date_not_decision_date(self):
        result=check('经讨论决定于2022年3月4日10:30行手术。')
        self.assertIsNone(result['choice_time'])

    def test_TMP009_future_scheduled_assessment_time_not_actual(self):
        result=check('拟评估时间：2022-03-04 10:30。')
        self.assertIsNone(result['choice_time'])
        self.assertTrue(any(x['reason']=='NO_EXPLICIT_CURRENT_CHOICE_TIME_STATEMENT' for x in result['evidence']['rejected_time_quotes']))

    def test_TMP010_header_date_cannot_attach_distant_decision_verb(self):
        result=check('记录时间：2022-03-04 10:30，神志清楚，拟予观察，决定暂缓。')
        self.assertIsNone(result['choice_time'])

    def test_TMP011_performed_report_keeps_null_even_when_model_proposes_exam_time(self):
        s=source('检查时间：2022-03-04 10:30。','IMG','imaging')
        c=resolve_citations([{'source_id':'IMG','quote':s['text']}],[s])
        e=dict(source_id='IMG',choice_time='2022-03-04 10:30',time_basis='explicit_documented_choice',kind='performed_test_report')
        result=verify_choice_time(e,[s],c)
        self.assertIsNone(result['choice_time'])
        self.assertEqual(result['time_basis'],'historical_unknown')

    def test_TMP012_record_proxy_always_null_preserves_original_proposal(self):
        result=check('决定时间：2022-03-04 10:30。',time_basis='record_time_proxy')
        self.assertIsNone(result['choice_time'])
        self.assertEqual(result['original_proposal']['choice_time'],'2022-03-04 10:30')
        self.assertEqual(result['time_basis'],'record_time_proxy')

    def test_TMP013_operative_phase_keeps_null(self):
        result=check('决定时间：2022-03-04 10:30。',time_basis='operative_phase')
        self.assertIsNone(result['choice_time'])
        self.assertIn('OPERATIVE_PHASE_HAS_NO_EXACT_CHOICE_MINUTE',result['uncertainty'])

    def test_TMP014_exact_actual_operative_time_if_explicitly_documented(self):
        result=check('于2022-03-04 10:30决定进一步探查。',document_profile='operative',phase='intraoperative')
        self.assertEqual(result['choice_time'],'2022-03-04 10:30')
        self.assertEqual(result['time_basis'],'explicit_documented_choice')

    def test_TMP015_cannot_pick_source_latest_or_record_time(self):
        result=check('2022-03-04 10:30 当前记载：拟复查。',proposed='2022-03-04 17:56')
        self.assertIsNone(result['choice_time'])

    def test_TMP016_explicit_different_date_rejects_proposal(self):
        result=check('决定时间：2022-03-03 10:30。')
        self.assertIsNone(result['choice_time'])
        self.assertIn('PROPOSED_CHOICE_TIME_MISMATCHES_EXPLICIT_SOURCE_TIME',result['uncertainty'])

    def test_TMP017_explicit_different_minute_rejects_proposal(self):
        result=check('决定时间：2022-03-04 10:31。')
        self.assertIsNone(result['choice_time'])

    def test_TMP018_model_seconds_precision_reduced_to_raw_minute(self):
        result=check('决定时间：2022-03-04 10:30。',proposed='2022-03-04 10:30:00')
        self.assertEqual(result['choice_time'],'2022-03-04 10:30')
        self.assertIn('MODEL_TIME_PRECISION_REDUCED_TO_EXPLICIT_SOURCE_PRECISION',result['uncertainty'])

    def test_TMP019_model_minute_precision_reduced_to_explicit_date(self):
        result=check('决定时间：2022年3月4日。',proposed='2022-03-04 10:30')
        self.assertEqual(result['choice_time'],'2022-03-04')
        self.assertEqual(result['choice_time_precision'],'date')

    def test_TMP020_source_explicit_second_precision_is_retained(self):
        result=check('决策时间：2022年3月4日10时30分15秒。',proposed='2022-03-04T10:30:15')
        self.assertEqual(result['choice_time'],'2022-03-04 10:30:15')
        self.assertEqual(result['choice_time_precision'],'second')

    def test_TMP021_invalid_calendar_date_not_time_support(self):
        result=check('决定时间：2022-02-30 10:30。',proposed='2022-02-30 10:30')
        self.assertIsNone(result['choice_time'])
        self.assertIn('INVALID_OR_UNSUPPORTED_CHOICE_TIME_FORMAT',result['uncertainty'])

    def test_TMP022_ambiguous_quote_not_forced_to_first_occurrence(self):
        s=source('决定时间：2022-03-04 10:30。其他段。决定时间：2022-03-04 10:30。')
        c=resolve_citations([{'source_id':'D','quote':'决定时间：2022-03-04 10:30。'}],[s])
        result=verify_choice_time(dict(source_id='D',choice_time='2022-03-04 10:30',time_basis='explicit_documented_choice'),[s],c)
        self.assertIsNone(result['choice_time'])
        self.assertEqual(len(result['evidence']['ambiguous_time_quotes']),1)

    def test_TMP023_invalid_secondary_time_quote_preserves_valid_support(self):
        s=source('决定时间：2022-03-04 10:30。')
        c=resolve_citations([{'source_id':'D','quote':s['text']},{'source_id':'D','quote':'不存在。'}],[s])
        result=verify_choice_time(dict(source_id='D',choice_time='2022-03-04 10:30',time_basis='explicit_documented_choice'),[s],c)
        self.assertTrue(result['choice_time_verified'])
        self.assertEqual(len(result['evidence']['invalid_time_quotes']),1)

    def test_TMP024_wrong_raw_offsets_not_accepted_as_parsed_valid(self):
        s=source('决定时间：2022-03-04 10:30。')
        c=resolve_citations([{'source_id':'D','quote':s['text']}],[s]);c['valid'][0]['start']=1
        result=verify_choice_time(dict(source_id='D',choice_time='2022-03-04 10:30',time_basis='explicit_documented_choice'),[s],c)
        self.assertIsNone(result['choice_time'])
        self.assertEqual(result['evidence']['rejected_time_quotes'][0]['reason'],'RESOLVED_SPAN_TEXT_MISMATCH')

    def test_TMP025_conflicting_explicit_times_not_chosen_by_model_preference(self):
        result=check('决定时间：2022-03-04 10:30。决策时间：2022-03-05 10:30。')
        self.assertIsNone(result['choice_time'])
        self.assertIn('CONFLICTING_EXPLICIT_CURRENT_CHOICE_TIMES',result['uncertainty'])

    def test_TMP026_other_document_time_not_assumed_same_event(self):
        prior=source('决定时间：2022-03-04 10:30。','P')
        current=source('当前仅重复记录，时间不详。','D')
        c=resolve_citations([{'source_id':'P','quote':prior['text']}],[prior,current])
        result=verify_choice_time(dict(source_id='D',choice_time='2022-03-04 10:30',time_basis='explicit_documented_choice'),[prior,current],c)
        self.assertIsNone(result['choice_time'])
        self.assertEqual(result['evidence']['rejected_time_quotes'][0]['reason'],'NOT_CURRENT_EVENT_SOURCE')

    def test_TMP027_no_proposal_does_not_invent_time_from_good_quote(self):
        result=check('决定时间：2022-03-04 10:30。',proposed=None)
        self.assertIsNone(result['choice_time'])
        self.assertIn('NO_ACTUAL_CHOICE_TIME_PROPOSED',result['uncertainty'])

    def test_TMP028_header_and_actual_date_use_actual_marker_not_first_date(self):
        result=check('记录时间：2022-03-05 12:00。决定时间：2022-03-04 10:30。')
        self.assertEqual(result['choice_time'],'2022-03-04 10:30')

    def test_TMP029_long_quote_unrelated_date_not_promoted_by_choice_elsewhere(self):
        result=check('检查于2022-03-04 10:30完成。现计划继续观察，决定待复查。')
        self.assertIsNone(result['choice_time'])

    def test_TMP030_chinese_fullwidth_colon_and_unpadded_date(self):
        result=check('评估时间：2022年3月4日 10：30。',proposed='2022-3-4 10:30')
        self.assertEqual(result['choice_time'],'2022-03-04 10:30')

    def test_TMP031_both_inputs_remain_unmodified(self):
        s=source('决定时间：2022-03-04 10:30。')
        e=dict(source_id='D',choice_time='2022-03-04 10:30',time_basis='explicit_documented_choice')
        c=resolve_citations([{'source_id':'D','quote':s['text']}],[s]); before=deepcopy((e,[s],c))
        verify_choice_time(e,[s],c)
        self.assertEqual((e,[s],c),before)

    def test_TMP032_negated_time_statement_not_actual(self):
        for text in ['尚未于2022年3月4日10:30决定手术。','无明确决定时间：2022-03-04 10:30。','并非于2022-03-04 10:30作出决定。']:
            self.assertIsNone(check(text)['choice_time'],text)

    def test_TMP033_future_decision_time_not_already_made_choice(self):
        for text in ['将于2022-03-04 10:30决定手术。','拟于2022-03-04 10:30评估。','计划评估时间：2022-03-04 10:30。']:
            self.assertIsNone(check(text)['choice_time'],text)


if __name__=='__main__':
    unittest.main()
