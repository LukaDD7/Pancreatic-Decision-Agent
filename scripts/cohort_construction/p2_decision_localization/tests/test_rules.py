"""Regression cases for observed clinically consequential retrieval failures.

All clinical sentences here are synthetic; no source identifiers or raw EHR.
"""
from pathlib import Path
import json
import unittest
from scripts.cohort_construction.p2_decision_localization.labels import Labeler
from scripts.cohort_construction.p2_decision_localization.timeline import build_chains
from scripts.cohort_construction.p2_decision_localization.documents import header_time, explicit_dates, document_family

CONFIG=json.loads((Path(__file__).parents[1]/'configs/rules-v0.3.json').read_text())


def note(text,key='n1',visit='v1',time='2025-01-01 10:00:00'):
    return {'patient_uid':'synthetic','record_key':key,'visit_id':visit,'family':'FIRST_SENIOR_ROUND',
            'body_time':time,'system_time':time,'text':text}


class RegressionTests(unittest.TestCase):
    def setUp(self):self.labeler=Labeler(CONFIG)

    def test_operation_forms_without_radical_word_and_variable_suffix(self):
        for procedure in ['胰十二指肠切除术','胰体尾脾切除术','根治性胰十二指肠切除术','根治性胰腺次全切术','胰体癌根治','胰十二指肠切除手术','胰十二指肠切除','全胰切除术','Whipple手术']:
            with self.subTest(procedure=procedure):
                hits=self.labeler.match(note('治疗计划：拟行'+procedure+'。'))
                self.assertTrue(any(h['label']=='PLAN_RESECTION' and h['candidate_eligible'] for h in hits))

    def test_question_is_not_positive_indication(self):
        hits=self.labeler.match(note('治疗计划：胰腺占位，待检查结果判断是否有手术指征。'))
        self.assertTrue(any(h['label']=='ASSESSMENT_PENDING' and h['candidate_eligible'] for h in hits))
        self.assertFalse(any(h['label']=='INDICATION_YES' and h['candidate_eligible'] for h in hits))

    def test_negative_operability_with_biopsy_keeps_negative(self):
        hits=self.labeler.match(note('病情：胰腺肿瘤伴肝转移，目前无手术指征，拟行EUS-FNA后系统治疗。'))
        self.assertTrue(any(h['label']=='INDICATION_NO' and h['candidate_eligible'] for h in hits))

    def test_diagnostic_consider_does_not_link_to_executed_surgery(self):
        hits=self.labeler.match(note('现病史：考虑恶性肿瘤，为进一步治疗，于某日在全麻下行胰腺根治性大部切除术。'))
        self.assertFalse(any(h['label']=='PLAN_RESECTION' for h in hits))

    def test_history_is_saved_but_not_new_current_decision(self):
        hits=self.labeler.match(note('现病史：当时无手术指征，拟行新辅助治疗。今第十二次化疗住院。'))
        self.assertTrue(any(h['label']=='INDICATION_NO' for h in hits))
        self.assertFalse(any(h['candidate_eligible'] for h in hits))

    def test_pain_procedure_indication_not_pancreas_resection(self):
        hits=self.labeler.match(note('病情：胰腺癌疼痛，手术指征明确。治疗：拟行脊髓鞘内注射术。'))
        self.assertFalse(any(h['candidate_eligible'] for h in hits))

    def test_nat_diagnosis_name_is_not_treatment_intent(self):
        hits=self.labeler.match(note('诊断：胰腺恶性肿瘤新辅助治疗后。现病史：门诊拟“胰腺恶性肿瘤新辅助化疗后”收入院。'))
        self.assertFalse(any(h['label']=='NAT_PLAN' for h in hits))

    def test_fitness_alone_does_not_create_point(self):
        self.assertFalse(any(h['candidate_eligible'] for h in self.labeler.match(note('病情：胰腺占位，目前无手术禁忌症。'))))

    def test_pending_reassessment_after_nonresection_intervention(self):
        hits=self.labeler.match(note('治疗：胰腺肿物，拟行ERCP减黄后，再评估下一步外科手术治疗。'))
        self.assertTrue(any(h['label']=='ASSESSMENT_PENDING' and h['candidate_eligible'] for h in hits))

    def test_chemotherapy_cycle_alone_needs_surgery_assessment_review(self):
        hits=self.labeler.match(note('治疗计划：胰腺肿瘤，拟行新辅助治疗，本次第十二次用药。'))
        self.assertTrue(any(h['label']=='NAT_PLAN' for h in hits))
        self.assertFalse(any(h['candidate_eligible'] for h in hits))

    def test_doctor_conclusion_without_standard_heading_is_retained(self):
        for person,wording in [('病人','有手术指征'),('患者','有外科手术指征')]:
            hits=self.labeler.match(note('辅助检查：胰腺肿瘤，其它诊断：肝囊肿，随访。某医师查看'+person+'后示：'+wording+'，建议转入外科治疗。'))
            self.assertTrue(any(h['label']=='INDICATION_YES' and h['candidate_eligible'] for h in hits))
        history=self.labeler.match(note('现病史：既往在外院治疗。某医师查看患者后示：有手术指征。今再次入院。'))
        self.assertFalse(any(h['candidate_eligible'] for h in history))

    def test_dates_and_document_families_do_not_invent_clinical_times(self):
        self.assertIsNone(header_time('2025-01-01 首次查房记录'))
        dates=explicit_dates('讨论日期：2025年01月01日。手术时间：2025年01月03日')
        self.assertEqual(dates['discussion_date'],'2025-01-01')
        self.assertEqual(dates['scheduled_operation_date'],'2025-01-03')
        self.assertEqual(document_family('某主治医师首次查房记录'),'FIRST_SENIOR_ROUND')
        self.assertEqual(document_family('转入首次病程记录'),'TRANSFER_IN')

    def test_resectability_question_negative_and_preference_are_separate(self):
        cases=[('是否可切除', 'ASSESSMENT_PENDING'),('不可切除','INDICATION_NO'),('暂缓手术','INDICATION_NO'),('患者拒绝手术','INDICATION_NO')]
        for phrase,label in cases:
            with self.subTest(phrase=phrase):
                hits=self.labeler.match(note('病情：胰腺肿瘤，'+phrase+'。'))
                self.assertTrue(any(h['label']==label and h['candidate_eligible'] for h in hits))
                if phrase.startswith(('是否','不可')):
                    self.assertFalse(any(h['label']=='INDICATION_YES' and h['candidate_eligible'] for h in hits))

    def test_same_window_does_not_merge_changed_decision(self):
        rows=[note('病情：胰腺占位，目前无手术指征。','a'),note('治疗计划：拟行胰十二指肠切除术。','b',time='2025-01-01 12:00:00')]
        hits=[h for r in rows for h in self.labeler.match(r)]
        chains,stats=build_chains(rows,hits,['synthetic'],CONFIG,'body_preferred',48)
        self.assertEqual(len(chains),2)
        self.assertEqual(stats['opposing_opinions_or_update_review_chains'],1)

    def test_same_plan_nearby_merges_but_missing_visits_do_not(self):
        rows=[note('治疗计划：拟行胰十二指肠切除术。','a'),note('治疗计划：拟行胰十二指肠切除术。','b',time='2025-01-01 12:00:00')]
        hits=[h for r in rows for h in self.labeler.match(r)]
        self.assertEqual(len(build_chains(rows,hits,['synthetic'],CONFIG,'body_preferred',48)[0]),1)
        for row in rows:row['visit_id']=''
        self.assertEqual(len(build_chains(rows,hits,['synthetic'],CONFIG,'body_preferred',48)[0]),2)


if __name__=='__main__':unittest.main()
