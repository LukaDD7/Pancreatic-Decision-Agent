"""Meaningful time, leakage and raw-field controls for checkpoint drafts."""
from datetime import datetime
import unittest
from scripts.cohort_construction.p2_checkpoint_casebook.snapshot import (
    precise_time,split_at_action,redact_view,select_reported_labs,visibility_status,document_block)


class SnapshotTests(unittest.TestCase):
    def test_date_only_is_not_imputed_to_midnight(self):
        self.assertIsNone(precise_time('2024-12-09'))

    def test_missing_and_future_lab_not_imputed_or_included(self):
        rows=[{'报告时间':'2024-12-09','检验项目名称':'a'},
              {'报告时间':'2024-12-12 08:56:25','检验项目名称':'b'},
              {'报告时间':'2024-12-09 11:39:04','检验项目名称':'c','检验结果值':'404.1已复'}]
        selected,audit=select_reported_labs(rows,datetime(2024,12,10,9,26))
        self.assertEqual([r['item'] for r in selected],['c'])
        self.assertEqual(selected[0]['result_raw'],'404.1已复')
        self.assertEqual([r['included'] for r in audit],[False,False,True])

    def test_same_time_is_not_predecision(self):
        self.assertFalse(select_reported_labs([{'报告时间':'2024-12-10 09:26:00'}],datetime(2024,12,10,9,26))[0])

    def test_current_action_cut_preserves_preceding_observations(self):
        text='胆红素持续升高，今予某项操作，等待改善后再手术。'
        prefix,span=split_at_action(text,'今予某项操作')
        self.assertEqual(prefix,'胆红素持续升高，')
        self.assertEqual(text[span['action_start']:span['action_end']],'今予某项操作，等待改善后再手术。')

    def test_ambiguous_action_marker_fails_closed(self):
        with self.assertRaises(ValueError):split_at_action('拟操作，既往拟操作','拟操作')
        with self.assertRaises(ValueError):split_at_action('未提及操作','今予操作')

    def test_late_document_keeps_time_disagreement(self):
        v=visibility_status('2024-12-07 20:20:00','2024-12-13 18:21:01',datetime(2024,12,10,9,26))
        self.assertTrue(v['body_precedes_cutoff'])
        self.assertFalse(v['create_precedes_cutoff'])
        self.assertFalse(v['availability_certified'])

    def test_redaction_is_view_only(self):
        original='姓名：测试甲\n家住某街道。因腹痛就诊。\n患者联系方式：12345678901\n总胆红素高。'
        displayed=redact_view(original,['测试甲'])
        self.assertNotIn('测试甲',displayed)
        self.assertNotIn('12345678901',displayed)
        self.assertIn('因腹痛就诊',displayed)
        self.assertIn('总胆红素高',displayed)
        self.assertIn('测试甲',original)

    def test_source_block_does_not_copy_outcome_metadata(self):
        n={'record_key':'synthetic','document_type':'查房','body_time':'2024-12-10 09:26:00',
           'system_time':'2024-12-13 18:27:15','future_pathology':'未来答案'}
        block=document_block(n,'既往已引流，当前指标仍高。',[])
        self.assertNotIn('future_pathology',block)
        self.assertIn('既往已引流',block['text'])


if __name__=='__main__':unittest.main()
