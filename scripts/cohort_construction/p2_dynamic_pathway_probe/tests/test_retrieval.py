"""Synthetic scope regressions, not clinical validation of retrieved decisions."""
from pathlib import Path
import json
import unittest
from scripts.cohort_construction.p2_dynamic_pathway_probe.retrieval import compile_rules, find_hits, review_flags

CONFIG = json.loads((Path(__file__).resolve().parents[1]/'rules.json').read_text())


class RetrievalTests(unittest.TestCase):
    def hits(self, text, category=None):
        rr = find_hits(text, compile_rules(CONFIG))
        return [h for h in rr if not category or h['pattern'] == category]

    def test_biliary_condition_without_word_reassess(self):
        self.assertTrue(self.hits('患者黄疸较重，先行PTCD减黄，待胆红素下降后制定手术方案。', 'D01_BILIARY_CONDITION'))

    def test_adjacent_sentences_keep_original_span(self):
        text = '梗阻性黄疸较重。拟先减黄，改善后评估手术机会。'
        rr = self.hits(text, 'D01_BILIARY_CONDITION')
        self.assertTrue(rr)
        for h in rr:
            self.assertEqual(text[h['start']:h['end']], h['quote'])

    def test_no_cross_paragraph_pairing(self):
        self.assertFalse(self.hits('梗阻性黄疸。\n患者拟手术治疗。', 'D01_BILIARY_CONDITION'))

    def test_ERCP_alone_not_pathway(self):
        self.assertFalse(self.hits('拟行ERCP置管。'))

    def test_pure_chemotherapy_cycle_not_reassessment(self):
        self.assertFalse(self.hits('今日拟行第六周期AG化疗，按计划执行。'))

    def test_optimization_deferred_surgery(self):
        self.assertTrue(self.hits('患者肺部感染尚未控制，暂不宜手术，拟先治疗。', 'D02_MEDICAL_OPTIMIZATION'))

    def test_future_reassessment_is_retrieved_not_completed(self):
        text = '拟先行新辅助化疗，之后重新评估是否可手术。'
        self.assertTrue(self.hits(text, 'D03_TREAT_THEN_REASSESS'))
        self.assertIn('FUTURE_PLAN_CUE', review_flags(text))
        self.assertIn('QUESTION_CUE', review_flags(text))

    def test_current_negative_after_treatment_retained(self):
        self.assertTrue(self.hits('化疗后复查提示进展，目前仍无手术指征。', 'D04_RESPONSE_AND_DISPOSITION'))

    def test_past_negative_does_not_delete_later_positive(self):
        text = '既往评估暂无手术指征，先化疗。治疗后复查肿瘤缩小，拟行胰体尾切除术。'
        rr = self.hits(text)
        self.assertTrue(any('暂无手术指征' in h['quote'] for h in rr))
        self.assertTrue(any('拟行胰体尾切除术' in h['quote'] for h in rr))

    def test_negation_before_anchor_is_not_trimmed(self):
        text = '经评估不考虑直接手术，先化疗后再评估手术机会。'
        self.assertTrue(self.hits(text))
        self.assertTrue(all('不考虑直接手术' in h['quote'] for h in self.hits(text)))

    def test_evidence_comparator_retained(self):
        self.assertTrue(self.hits('肝结节性质待定，需进一步检查后评估手术。', 'D05_EVIDENCE_BEFORE_DISPOSITION'))

    def test_all_term_coordinates_and_no_duplicate_spans(self):
        text = '胰腺肿物伴黄疸，拟先减黄。好转后再评估手术；拟先化疗再评估切除机会。'
        rr = self.hits(text)
        self.assertEqual(len(rr), len({(h['pattern'], h['start'], h['end']) for h in rr}))
        for h in rr:
            for t in h['matched_terms']:
                self.assertEqual(text[t['start']:t['end']], t['text'])


if __name__ == '__main__':
    unittest.main()
