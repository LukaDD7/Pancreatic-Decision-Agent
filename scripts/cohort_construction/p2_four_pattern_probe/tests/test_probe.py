"""Synthetic controls for retrieval scope; no original EHR in tests."""
from pathlib import Path
import json
import unittest
from scripts.cohort_construction.p2_four_pattern_probe.probe import find_patterns,annotate
from scripts.cohort_construction.p2_decision_localization.labels import Labeler

HERE=Path(__file__).resolve().parents[1]
RULES=json.loads((HERE/'rules.json').read_text())
CONFIG=json.loads((HERE.parent/'p2_decision_localization/configs/rules-v0.3.json').read_text())


class PatternTests(unittest.TestCase):
    def note(self,text,family='WARD_ROUND'):
        return {'record_key':'synthetic-note','patient_uid':'synthetic-patient','family':family,'text':text}

    def matches(self,text):
        return list(find_patterns(text,RULES))

    def test_four_current_patterns(self):
        cases=[('CONDITION_DEFERRAL','病情：胰腺肿物，肺炎尚未控制，不宜手术，先治疗感染。'),
               ('DECOMPRESSION_REASSESSMENT','治疗计划：胰腺肿物，ERCP减黄后再评估外科手术。'),
               ('SURGICAL_REFERRAL','病情：胰腺肿物，有外科手术指征，建议转入外科手术治疗。'),
               ('DISEASE_NO_SURGERY','病情：胰腺肿物伴肝转移，无手术指征，拟系统治疗。')]
        for category,text in cases:
            with self.subTest(category=category):
                found=[m for m in self.matches(text) if m['pattern']==category]
                self.assertTrue(found)
                note=self.note(text)
                self.assertTrue(annotate(note,found[0],Labeler(CONFIG))['current_opinion_hint'])

    def test_ERCP_alone_not_surgical_decision(self):
        self.assertFalse(self.matches('治疗计划：胰腺肿物伴黄疸，今日拟行ERCP置管。'))

    def test_chemotherapy_cycle_alone_not_reassessment(self):
        self.assertFalse(self.matches('诊断：胰腺癌。治疗计划：继续第六次新辅助化疗。'))

    def test_history_saved_without_current_certification(self):
        text='现病史：既往因肝转移明确，无手术指征，予化疗。'
        found=self.matches(text)
        self.assertTrue(found)
        self.assertFalse(annotate(self.note(text,'FIRST_COURSE'),found[0],Labeler(CONFIG))['current_opinion_hint'])

    def test_question_not_positive_referral_decision(self):
        text='治疗计划：胰腺肿物，是否有手术指征，建议转入外科评估。'
        found=self.matches(text)
        self.assertTrue(found)
        self.assertFalse(annotate(self.note(text),found[0],Labeler(CONFIG))['current_opinion_hint'])

    def test_no_cross_sentence_pairing(self):
        self.assertFalse(self.matches('肺炎已控制。胰腺癌伴血管受累，不宜手术。'))

    def test_quote_coordinates(self):
        text='病情：胰腺肿物伴肺炎，暂缓手术；予以治疗。'
        for m in self.matches(text):
            self.assertEqual(text[m['start']:m['end']],m['quote'])


if __name__=='__main__':unittest.main()
