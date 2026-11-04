"""Known adversarial regressions and meaningful P1/reference invariants."""
import json
from pathlib import Path
import unittest
from scripts.cohort_construction.p1_source_quality.p1_quality_v0_3.labels import (
    coverage, measurement_statements, metastatic_state, normalize, parse_image, vessel_statements)
from scripts.cohort_construction.p1_source_quality.p1_quality_v0_3.references import citations, exam_number, link
from scripts.cohort_construction.p1_source_quality.p1_quality_v0_3.run import classify

RULES = json.loads((Path(__file__).resolve().parents[1]/'rules.json').read_text())


def statements(text, vessel='SMA'):
    return [s for s in vessel_statements(normalize(text), RULES, 'synthetic') if s['vessel'] == vessel]


class ScopeRegressionTests(unittest.TestCase):
    def test_front_negation(self):
        self.assertEqual(statements('未见肠系膜上动脉受侵。')[0]['state'], 'NO_CONTACT_OR_INVASION')

    def test_positive_control(self):
        self.assertEqual(statements('肠系膜上动脉受侵。')[0]['state'], 'CONTACT_OR_INVASION')

    def test_qualified_front_negation(self):
        self.assertEqual(statements('未见明显肠系膜上动脉受侵。')[0]['state'], 'NO_CONTACT_OR_INVASION')

    def test_short_uninvaded(self):
        self.assertEqual(statements('肠系膜上动脉未受侵。')[0]['state'], 'NO_CONTACT_OR_INVASION')

    def test_preceding_uncertainty(self):
        self.assertEqual(statements('不能明确是否与肠系膜上动脉接触。')[0]['state'], 'EXPLICIT_INDETERMINATE')

    def test_segment_local_positive_not_erased(self):
        data = statements('肠系膜上动脉近段无接触，远段局部包绕。')
        self.assertEqual({s['state'] for s in data}, {'NO_CONTACT_OR_INVASION','CONTACT_OR_INVASION'})

    def test_same_unit_mixed_preserved(self):
        self.assertEqual(statements('肠系膜上动脉近段无接触但远段包绕。')[0]['state'], 'MULTIPLE_DESCRIPTIONS')

    def test_other_organ_uncertainty(self):
        self.assertEqual(statements('肠系膜上动脉无接触，肝内结节可疑转移。')[0]['state'], 'NO_CONTACT_OR_INVASION')

    def test_other_organ_without_comma(self):
        self.assertEqual(statements('肠系膜上动脉无接触肝内结节可疑转移。')[0]['state'], 'NO_CONTACT_OR_INVASION')

    def test_splenic_angle_is_scoped(self):
        text = '肠系膜上动脉无接触，脾动脉包绕270°。'
        self.assertEqual(statements(text)[0]['angles'], [])
        self.assertEqual(statements(text, 'SA')[0]['angles'], ['270°'])

    def test_history_current_both_kept(self):
        data = statements('既往肠系膜上动脉接触180°，本次肠系膜上动脉无接触。')
        self.assertEqual([s['time_role'] for s in data], ['HISTORY_EXPLICIT','CURRENT_EXPLICIT'])
        self.assertEqual(data[1]['state'], 'NO_CONTACT_OR_INVASION')

    def test_shared_negation(self):
        data = vessel_statements('未见肠系膜上动脉、门静脉受侵。', RULES, 'synthetic')
        self.assertEqual([s['state'] for s in data], ['NO_CONTACT_OR_INVASION']*2)

    def test_shared_after_predicate(self):
        data = vessel_statements('肠系膜上动脉、门静脉均未见接触。', RULES, 'synthetic')
        self.assertEqual([s['state'] for s in data], ['NO_CONTACT_OR_INVASION']*2)

    def test_parent_not_filled_by_branch(self):
        data = statements('肠系膜上静脉无累及第一空肠支。', 'SMV')
        self.assertEqual(data[0]['state'], 'MISSING')

    def test_blank_does_not_erase_other_statement(self):
        data = statements('肠系膜上动脉无接触；肠系膜上动脉接触面。')
        self.assertEqual([s['state'] for s in data], ['NO_CONTACT_OR_INVASION','EMPTY_ANGLE_FIELD'])

    def test_explicit_uncertainty_kept(self):
        self.assertEqual(statements('门静脉关系难以判断。', 'PV')[0]['state'], 'EXPLICIT_INDETERMINATE')

    def test_duct_measurement_not_tumor(self):
        data = measurement_statements('胰头肿块，胆总管直径12mm。', 'synthetic')
        self.assertEqual(data[0]['object'], 'OTHER_OBJECT')

    def test_comparison_measurements_keep_roles(self):
        data = measurement_statements('本次胰头肿块20×18mm，较前25×22mm缩小。', 'synthetic')
        self.assertEqual([s['time_role'] for s in data], ['CURRENT_EXPLICIT','HISTORY_EXPLICIT'])

    def test_pathology_is_separate(self):
        data = measurement_statements('胰头肿块20×18mm。术后病理标本15×12mm。', 'synthetic')
        self.assertEqual([s['object'] for s in data], ['PANCREATIC_LESION_CANDIDATE','PATHOLOGY_OR_SPECIMEN'])

    def test_stability_not_negative_metastasis(self):
        self.assertEqual(metastatic_state('肝脏病变：转移灶未见明显变化'), 'METASTATIC_STATEMENT')
        self.assertEqual(metastatic_state('肝脏及腹膜未见转移'), 'NEGATIVE')

    def test_quote_coordinates(self):
        raw = '既往肠系膜上动脉接触180°，本次肠系膜上动脉无接触。'
        for s in statements(raw):
            self.assertEqual(normalize(raw)[s['start']:s['end']], s['quote'])


class PatientAndReferenceTests(unittest.TestCase):
    def test_partial_anatomy_relevant(self):
        self.assertTrue(coverage('胰头肿块，门静脉受压。',RULES)['substantive_research_related'])

    def test_acquisition_pending_relevant(self):
        self.assertTrue(coverage('胰腺癌，拟完善CT后评估手术指征。',RULES)['substantive_research_related'])

    def test_name_only_not_automatic(self):
        self.assertFalse(coverage('诊断：PDAC。',RULES)['substantive_research_related'])

    def test_blank_headings_not_substantive(self):
        self.assertFalse(coverage('诊断：PDAC。\n手术指征：\n远处转移：\n',RULES)['substantive_research_related'])

    def test_postop_and_difference_not_veto(self):
        self.assertEqual(classify(True,1,5,True), 'INCLUDE_V3')
        self.assertEqual(classify(True,1,0,False), 'INCLUDE_V3')

    def test_unknown_source_or_relevance_review(self):
        self.assertEqual(classify(True,0,1,True),'REVIEW_V3')
        self.assertEqual(classify(True,0,0,False),'REVIEW_V3')
        self.assertEqual(classify(False,1,0,True),'REVIEW_V3')

    def test_zero_hits_not_automatic_exclusion(self):
        self.assertEqual(classify(True,0,0,True),'REVIEW_V3')

    def test_postop_no_parse_shortcut(self):
        p = parse_image({'original_findings':'胰头肿块。胰十二指肠切除术后。门静脉受侵。'},RULES)
        self.assertTrue(p['postoperative_clue'])
        self.assertTrue(p['vessel_statements'])

    def test_future_plan_not_missing(self):
        for text in ['拟完善腹部增强CT。','建议行MRI。','预约胰腺CT。']:
            cite = list(citations(text))[0]
            self.assertEqual(link(cite,[])['match_status'],'PLANNED_NOT_MISSING')

    def test_no_nearest_exam_guess(self):
        cite = list(citations('CT提示胰头占位。'))[0]
        self.assertEqual(link(cite,[])['match_status'],'UNRESOLVED_REFERENCE')

    def test_date_only_candidate(self):
        cite = list(citations('2024-01-01CT提示胰头占位。'))[0]
        report = {'record_key':'r1','modality':'CT','date':'2024-01-01','exam_nos':['123456'],'has_verified_source':True}
        self.assertEqual(link(cite,[report])['match_status'],'FOUND_CANDIDATE')
        self.assertEqual(link(cite,[])['match_status'],'NOT_FOUND_IN_DELIVERED')

    def test_unverified_not_missing(self):
        cite = list(citations('2024-01-01CT提示胰头占位。'))[0]
        report = {'record_key':'r1','modality':'CT','date':'2024-01-01','exam_nos':['123456'],'has_verified_source':False}
        self.assertEqual(link(cite,[report])['match_status'],'SOURCE_UNVERIFIED_CANDIDATE')

    def test_same_day_unknown_identity_ambiguous(self):
        cite = list(citations('2024-01-01CT提示胰头占位。'))[0]
        reports = [{'record_key':r,'modality':'CT','date':'2024-01-01','exam_nos':[],'has_verified_source':True} for r in ['r1','r2']]
        self.assertEqual(link(cite,reports)['match_status'],'AMBIGUOUS')

    def test_exam_number_numeric_artifact(self):
        self.assertEqual(exam_number('123456.0'),'123456')
        self.assertEqual(exam_number('00123456'),'00123456')
        self.assertEqual(exam_number('0.0'),'')


if __name__ == '__main__':
    unittest.main()
