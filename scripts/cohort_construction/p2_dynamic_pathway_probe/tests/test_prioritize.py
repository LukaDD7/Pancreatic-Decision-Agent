"""Prevent known false temporal-operator and template priorities (DYN-001–004)."""
from pathlib import Path
import json
import unittest
from scripts.cohort_construction.p2_dynamic_pathway_probe.prioritize import relation_matches

RULES=json.loads((Path(__file__).resolve().parents[1]/'relation_rules.json').read_text())


class RelationTests(unittest.TestCase):
    def match(self,text,category):
        return relation_matches({'quote':text,'pattern':category,'start':0},RULES)[0]

    def test_DYN001_abdominal_posterior_not_future_surgery(self):
        self.assertFalse(self.match('新辅助化疗4次，已行胰体尾切除术+腹膜后淋巴结清扫术。','D03_TREAT_THEN_REASSESS'))

    def test_DYN001_postop_bilirubin_not_conditional_surgery(self):
        self.assertFalse(self.match('术后第一天，手术切口恢复可，胆红素正常。','D01_BILIARY_CONDITION'))

    def test_DYN002_risk_template_not_current_condition(self):
        text='手术可能存在的风险：术中术后感染、营养情况不佳，恢复欠理想，长期卧床导致坠积性肺炎。'
        self.assertFalse(self.match(text,'D02_MEDICAL_OPTIMIZATION'))

    def test_DYN002_current_infection_contrast(self):
        self.assertTrue(self.match('患者目前肺部感染，暂不宜手术，先对症处理。','D02_MEDICAL_OPTIMIZATION'))

    def test_DYN003_trial_title_not_response(self):
        self.assertFalse(self.match('参加免疫治疗联合化疗在局部进展期不可切除胰腺癌的临床研究。','D04_RESPONSE_AND_DISPOSITION'))

    def test_DYN004_diabetes_and_no_surgery_history(self):
        self.assertFalse(self.match('既往血糖高，控制饮食后恢复正常，否认手术史。','D02_MEDICAL_OPTIMIZATION'))

    def test_conditional_biliary_plan(self):
        self.assertTrue(self.match('黄疸较重，先减黄，待胆红素改善后制定进一步手术方案。','D01_BILIARY_CONDITION'))

    def test_future_after_therapy_relation(self):
        self.assertTrue(self.match('考虑拟先新辅助化疗后再评估是否可切除。','D03_TREAT_THEN_REASSESS'))

    def test_negative_reassessment_retained(self):
        self.assertTrue(self.match('化疗后复查提示疾病进展，目前暂无手术指征。','D04_RESPONSE_AND_DISPOSITION'))

    def test_information_relation_separate_from_optimization(self):
        self.assertTrue(self.match('待病理结果后再评估手术治疗。','D05_EVIDENCE_BEFORE_DISPOSITION'))


if __name__=='__main__':unittest.main()
