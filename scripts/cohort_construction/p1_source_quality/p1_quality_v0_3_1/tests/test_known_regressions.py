"""Known-defect contracts, including former v0.2 fixes and v0.3 gaps.

Inputs are synthetic. Assertions cover expected semantics plus positive/negative
controls, rather than merely asserting the current implementation's output.
"""
import json
from pathlib import Path
import re
import unittest
from scripts.cohort_construction.p1_source_quality.p1_quality_v0_3_1.labels import (
    coverage, measurement_statements, normalize, parse_image, relationship, vessel_statements)

RULES = json.loads((Path(__file__).resolve().parents[1]/'rules.json').read_text())


def parse(text, impression=''):
    return parse_image({'original_findings':text,'original_impression':impression},RULES)


class KnownDefectContracts(unittest.TestCase):
    def test_W01_partial_ABC_and_vessel_coverage_is_substantive(self):
        for text in ['胰头肿块，门静脉受侵。','PDAC。CA19-9：120 U/ml。','PDAC。ECOG 1。']:
            with self.subTest(text=text):
                self.assertTrue(coverage(text,RULES)['substantive_research_related'])
        self.assertFalse(coverage('诊断：PDAC。',RULES)['substantive_research_related'])

    def test_W07_normal_size_comparison_remains_available(self):
        p = parse('肿块评估：本次胰头肿块20×18mm，较前25×22mm缩小。')
        self.assertEqual([m['quote'] for m in p['measurements']],['20×18mm','25×22mm'])
        self.assertEqual([m['time_role'] for m in p['measurements']],['CURRENT_EXPLICIT','HISTORY_EXPLICIT'])
        self.assertTrue(p['coverage']['substantive_research_related'])

    def test_W10_narrative_metastatic_domain_is_represented(self):
        p = parse('PDAC。肝脏及腹膜未见转移。')
        self.assertEqual(p['metastatic_statements'][0]['state'],'NEGATIVE')
        self.assertTrue(p['coverage']['B'])
        self.assertTrue(p['coverage']['substantive_research_related'])

    def test_R01_blank_fields_never_replace_existing_values(self):
        for value, expected in [('无接触','NO_CONTACT_OR_INVASION'),('受侵','CONTACT_OR_INVASION'),('关系难以判断','EXPLICIT_INDETERMINATE')]:
            with self.subTest(value=value):
                p=parse('胰头肿块。肠系膜上动脉'+value+'；肠系膜上动脉接触面。')
                statements=[s for s in p['vessel_statements'] if s['vessel']=='SMA']
                self.assertEqual([s['state'] for s in statements],[expected,'EMPTY_ANGLE_FIELD'])
                self.assertIn('SMA',p['represented_vessels'])

    def test_R01_blank_never_erases_opposing_descriptions(self):
        p=parse('胰头肿块。肠系膜上动脉无接触；肠系膜上动脉受侵；肠系膜上动脉接触面。')
        self.assertEqual({s['state'] for s in p['vessel_statements']},{'NO_CONTACT_OR_INVASION','CONTACT_OR_INVASION','EMPTY_ANGLE_FIELD'})
        self.assertTrue(p['difference_flags'])

    def test_R02_parent_state_survives_jejunal_branch_negation(self):
        for value,expected in [('接触>180°','CONTACT_OR_INVASION'),('无接触','NO_CONTACT_OR_INVASION'),('关系难以判断','EXPLICIT_INDETERMINATE')]:
            with self.subTest(value=value):
                p=parse('胰头肿块。肠系膜上静脉'+value+'，无累及第一空肠引流支。')
                smv=[s for s in p['vessel_statements'] if s['vessel']=='SMV']
                self.assertEqual(smv[0]['state'],expected)

    def test_R02_branch_only_in_contact_summary_never_fills_parent(self):
        for text in ['肠系膜上静脉无累及空肠引流支。','与血管接触：肠系膜上静脉无累及空肠引流支。']:
            with self.subTest(text=text):
                p=parse(text)
                self.assertNotIn('SMV',p['represented_vessels'])
                self.assertEqual(p['vessel_statements'][0]['state'],'MISSING')

    def test_R03_template_bare_no_and_patency_are_distinct(self):
        self.assertEqual(parse('动脉评估：肝总动脉无。')['vessel_statements'][0]['state'],'NO_CONTACT_OR_INVASION')
        self.assertEqual(relationship('无'),'UNRECOGNIZED_VALUE')
        self.assertEqual(relationship('无狭窄',template=True),'PATENCY_ONLY')

    def test_R04_angle_forms_and_contact_without_angle(self):
        for text,expected in [('接触面>180度','CONTACT_OR_INVASION'),('接触面≤180°','CONTACT_OR_INVASION'),('受侵','CONTACT_OR_INVASION'),('接触面0°','NO_CONTACT_OR_INVASION'),('接触面无','NO_CONTACT_OR_INVASION'),('接触面500°','UNRECOGNIZED_VALUE')]:
            with self.subTest(text=text):
                self.assertEqual(relationship(text),expected)

    def test_R05_CA19_is_not_celiac_axis(self):
        p=parse('胰头肿块。CA19-9：500 U/ml。')
        self.assertTrue(p['coverage']['B'])
        self.assertFalse(p['vessel_statements'])

    def test_R06_nodal_contact_does_not_fill_primary_vessel(self):
        p=parse('胰头肿块。SMA无接触。可疑淋巴结：SMA周围组接触>180°。')
        self.assertEqual([s['state'] for s in p['vessel_statements']],['NO_CONTACT_OR_INVASION'])

    def test_R07_other_object_measurements_never_become_tumor(self):
        for obj in ['胆总管','胰管','淋巴结','肝脏','肾脏','脾脏']:
            with self.subTest(obj=obj):
                m=measurement_statements('胰头肿块，'+obj+'直径12mm。','synthetic')
                self.assertEqual(m[0]['object'],'OTHER_OBJECT')

    def test_R08_structured_lesion_can_omit_repeated_noun(self):
        p=parse('肿块评估：增强实质期呈低密度，大小约15X18mm，位于胰头部。')
        self.assertTrue(p['coverage']['target_context'])
        self.assertTrue(p['coverage']['substantive_research_related'])
        # The fallback must not borrow anatomy from another section.
        q=parse('肿块评估：肝内低密度结节。\n动脉评估：胰头周围血管通畅。')
        self.assertFalse(q['coverage']['target_context'])

    def test_R09_measurement_units_and_nonmeasurable_are_kept(self):
        p=parse('胰头肿块15mm×1.8cm。')
        self.assertEqual(p['measurements'][0]['quote'],'15mm×1.8cm')
        q=parse('胰头肿瘤无法测量。')
        self.assertTrue(q['coverage']['substantive_research_related'])
        self.assertEqual(q['measurements'],[])

    def test_R10_splenic_contact_is_not_a_required_vessel_gate(self):
        p=parse('胰尾肿块。与血管接触：脾动脉、脾静脉>180°。')
        self.assertTrue(p['coverage']['substantive_research_related'])
        self.assertEqual(set(p['represented_vessels']),{'SA','SV'})
        self.assertEqual(RULES['required_vessels'],[])

    def test_R11_config_regex_roundtrip_uses_whitespace_and_digits(self):
        roundtrip=json.loads(json.dumps(RULES,ensure_ascii=False))
        for text,dim in [('PDAC。CA 19-9 : 120.5 U/ml。','B'),('PDAC。ECOG 2。','C')]:
            with self.subTest(text=text):
                self.assertTrue(coverage(text,roundtrip)[dim])
        self.assertIsNone(re.search(roundtrip['target_pattern'],'胰头\n肿块'))


if __name__=='__main__':
    unittest.main()
