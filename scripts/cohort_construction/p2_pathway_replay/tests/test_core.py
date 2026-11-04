import copy
import json
import unittest
from scripts.cohort_construction.p2_pathway_replay.core import (
    project_sources,clock,relation_to_cutoff,cite,operative_partition,
    validate_partition,resource_selection,compile_view,validate_assessment_round,
    exact_duplicate_groups)

class SourceReplayRegression(unittest.TestCase):
    def image(self,time='2022-09-28 11:29:10',uid='p',identity='I1',finding='未见明确局限性狭窄。'):
        return dict(patient_uid=uid,index_report_uid=identity,exam_method='冠状动脉CT增强造影',exam_datetime=time,
                    exam_no_raw='N1',original_findings=finding,original_impression='CTA未见明显异常')
    def sources(self,bundle):return project_sources(bundle,'p')[0]
    def lab(self,sample='2022-09-27 08:00:00',report='2022-09-27 10:00:00'):
        return dict(patient_uid='p',source_record_id='L1',检验项目名称='高敏肌钙蛋白I',检验结果值='> 2.224',检验结果单位='μg/L',检验参考值='0-0.04',送检时间=sample,报告时间=report)
    def doc(self,text,create='2022-09-29 18:00:00'):
        return dict(patient_uid='p',source_record_key='D1',document_text=text,create_time=create,admission_time='2022-09-26 10:00:00',文书名称='原文')

    def test_src001_cta_is_not_lost_by_document_only_projection(self):
        ss,inv=project_sources(dict(documents_all=[],imaging_all=[self.image()]),'p')
        self.assertEqual(inv['imaging_all'],1);self.assertEqual(ss[0]['kind'],'imaging')
        self.assertEqual(resource_selection(ss[0],'2022-09-28 18:25:00','2022-09-26')['disposition'],'INCLUDE')

    def test_src001_cta_cannot_leak_into_earlier_request(self):
        s=self.sources(dict(imaging_all=[self.image()]))[0]
        self.assertEqual(resource_selection(s,'2022-09-27 18:18:00','2022-09-26')['reason'],'AFTER_TARGET_PHASE')

    def test_src002_creation_is_not_availability(self):
        s=self.sources(dict(documents_all=[self.doc('2022-09-28 18:25 正文')]))[0]
        self.assertEqual(s['clocks']['body_record_time']['value'],'2022-09-28 18:25:00')
        self.assertEqual(s['clocks']['source_create_time']['value'],'2022-09-29 18:00:00')
        self.assertFalse(s['availability_verified']);self.assertNotIn('recorded_time',s)

    def test_src002_date_only_does_not_become_midnight(self):
        self.assertEqual(relation_to_cutoff(clock('2022-09-28'),'2022-09-28 18:25:00'),'SAME_DAY_UNORDERED')
        self.assertEqual(clock('2022-02-31')['precision'],'invalid')

    def test_src002_original_excel_clock(self):
        self.assertEqual(clock(44832.5)['value'],'2022-09-28 12:00:00')
        self.assertEqual(clock(44832)['precision'],'date')

    def test_src003_two_operative_forms_keep_adverse_evidence(self):
        for tail in ['\n    切除：实施\n    重建：缝合','\n    解剖胰腺上缘，切除']:
            text='实际术式：切除\n手术经过：\n    麻醉成功后进腹\n探查：肠系膜上动脉血管侵犯，术中诊断：占位，遂决定切除'+tail
            s=self.sources(dict(documents_all=[self.doc(text)]))[0];m=operative_partition(s)
            validate_partition(s,m['segments'])
            keep=''.join(x['quote'] for x in m['segments'] if x['disposition']=='KEEP')
            self.assertIn('血管侵犯',keep);self.assertNotIn('遂决定',keep);self.assertNotIn('实际术式',keep)

    def test_src003_no_template_means_review_not_exclusion(self):
        s=self.sources(dict(documents_all=[self.doc('探查后考虑几种方案，决定暂缓。')]))[0]
        self.assertEqual(operative_partition(s)['status'],'SEMANTIC_REVIEW_REQUIRED')

    def test_src004_no_new_evidence_no_extra_round(self):
        verdict=validate_assessment_round('先前条件','当前评估','当前选择',[],[],'2022-09-28 18:25:00')
        self.assertFalse(verdict['eligible'])

    def test_src004_new_test_without_current_judgment_is_not_point(self):
        ss=self.sources(dict(imaging_all=[self.image()]));a=cite(ss[0],ss[0]['original_fields']['original_impression'])
        self.assertFalse(validate_assessment_round('先前条件',None,'', [a],ss,'2022-09-28 18:25:00')['eligible'])

    def test_src004_normal_result_can_support_reviewed_round(self):
        ss=self.sources(dict(imaging_all=[self.image()]));a=cite(ss[0],ss[0]['original_fields']['original_impression'])
        self.assertTrue(validate_assessment_round('待查','当前条件判断','继续推进',[a],ss,'2022-09-28 18:25:00')['eligible'])
        self.assertFalse(validate_assessment_round('待查','当前条件判断','继续推进',[a],ss,'2022-09-27 18:25:00')['eligible'])

    def test_src005_duplicate_current_answer_blocked(self):
        s=self.sources(dict(documents_all=[self.doc('前文。复制的当前答案：决定取消手术。')]))[0]
        a=cite(s,s['text'],0)
        with self.assertRaises(ValueError):compile_view([s],{s['source_id']:[a]},['决定取消手术'])

    def test_src005_real_prior_plan_preserved(self):
        s=self.sources(dict(documents_all=[self.doc('昨日拟手术。今日影像包绕动脉。')]))[0]
        blocks,_=compile_view([s],{s['source_id']:[cite(s,s['text'],0)]},['决定取消手术'])
        self.assertIn('昨日拟手术',blocks[0]['passages'][0])

    def test_src006_mixed_field_evidence_survives(self):
        s=self.sources(dict(documents_all=[self.doc('当前安排：行EUS。手术指征：MRI示静脉接触≤180°。')]))[0]
        a=cite(s,'MRI示静脉接触≤180°。')
        blocks,_=compile_view([s],{s['source_id']:[a]},['行EUS'])
        self.assertIn('≤180°',blocks[0]['passages'][0])

    def test_src007_use_report_time_not_sample_time(self):
        s=self.sources(dict(laboratory_original_L1_all=[self.lab(report='2022-09-29 10:00:00')]))[0]
        self.assertEqual(resource_selection(s,'2022-09-28 18:25:00','2022-09-26')['disposition'],'EXCLUDE')
        self.assertIn('> 2.224',s['text']);self.assertIn('μg/L',s['text'])

    def test_src007_quarantine_and_missing_time_stay_review(self):
        ss=self.sources(dict(laboratory_source_quarantine=[dict(source_record_id='Q',raw_values_json='原始值')],imaging_all=[self.image(time=None)]))
        for s in ss:self.assertEqual(resource_selection(s,'2022-09-28 18:25:00','2022-09-26')['disposition'],'REVIEW')

    def test_src008_no_labels_or_cross_patient(self):
        ss=self.sources(dict(imaging_all=[self.image()],NAT_outcome_summary_analysis_only={'secret':'FUTURE-TRAP'},confirmation_evidence=['FUTURE-TRAP']))
        self.assertNotIn('FUTURE-TRAP',json.dumps(ss))
        with self.assertRaises(AssertionError):self.sources(dict(imaging_all=[self.image(uid='another-patient')]))

    def test_src008_original_versions_do_not_overwrite(self):
        ss=self.sources(dict(imaging_all=[self.image(identity='I1'),self.image(identity='I2'),self.image(identity='I3',finding='无法判断。')]))
        groups=exact_duplicate_groups(ss)
        self.assertEqual(len(groups),1);self.assertEqual(len(groups[0]),2)
        self.assertIn('无法判断',ss[2]['text']);self.assertEqual(len(ss),3)

    def test_src008_ambiguous_quote_requires_explicit_span(self):
        s=self.sources(dict(documents_all=[self.doc('重复。重复。')]))[0]
        with self.assertRaises(ValueError):cite(s,'重复。')
        self.assertEqual(cite(s,'重复。',3)['start'],3)

if __name__=='__main__':unittest.main()
