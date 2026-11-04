import copy,json,unittest
from pathlib import Path
from report_parser import parse_report,evaluate_report,relationship,disjoint,angle_value,normalize
from run_quality import verify_source

RULES=json.loads(Path(__file__).with_name('quality_rules.json').read_text())
BASE='''肿块评估：胰头可见肿块，大小约15X18mm。
胰胆管评估：胰管截断伴扩张：有。
动脉评估：1.与肠系膜上动脉无明显接触。2.与腹腔干动脉无明显接触。3.与肝总动脉无明显接触。
静脉评估：1.与门静脉无明显接触。2.与肠系膜上静脉无明显接触。
其它评估：肝脏病变：可能良性病变。腹膜或网膜结节：无。'''

def record(text=BASE,impression='远处转移：未见。'):
    return {'patient_uid':'synthetic','exam_datetime':'2024-01-01','exam_method':'胰腺CT增强','original_findings':text,'original_impression':impression}

def assess(d,verified=True):
    p=parse_report(d,RULES);return p,evaluate_report(d,p,{'verified':verified,'reason':'TEST_UNVERIFIED'},RULES)

class RepresentationTests(unittest.TestCase):
    def test_complete_negative(self):
        d=record();p,s=assess(d);self.assertEqual(s[0],'PASS')
        for v in p['vessels'].values():
            for e in v['evidence']:
                t=normalize(d[e['source_field']])
                self.assertEqual(t[e['normalized_quote_start']:e['normalized_quote_end']],e['quote'])
    def test_positive_contact(self):
        p,s=assess(record(BASE.replace('肝总动脉无明显接触','肝总动脉接触面≤180°')))
        self.assertEqual(s[0],'PASS');self.assertEqual(p['vessels']['CHA']['state'],'CONTACT_OR_INVASION')
    def test_explicit_uncertainty_is_usable(self):
        p,s=assess(record(BASE.replace('门静脉无明显接触','门静脉关系难以判断')))
        self.assertEqual(s[0],'PASS');self.assertEqual(p['vessels']['PV']['state'],'EXPLICIT_INDETERMINATE')
    def test_missing_vessel(self):self.assertIn('VESSEL_SMA_MISSING',assess(record(BASE.replace('1.与肠系膜上动脉无明显接触。','')))[1][1])
    def test_blank_contact_angle(self):self.assertIn('VESSEL_SMA_EMPTY_ANGLE_FIELD',assess(record(BASE.replace('肠系膜上动脉无明显接触','肠系膜上动脉接触面')))[1][1])
    def test_patency_is_not_relationship(self):self.assertIn('VESSEL_SMA_PATENCY_ONLY',assess(record(BASE.replace('肠系膜上动脉无明显接触','肠系膜上动脉通畅')))[1][1])
    def test_conflicting_fields(self):self.assertIn('VESSEL_CONFLICT_SMA',assess(record(impression='与血管接触：肠系膜上动脉>180°。远处转移：未见。'))[1][1])
    def test_conflicting_angles(self):
        p,s=assess(record(BASE.replace('肠系膜上动脉无明显接触','肠系膜上动脉接触≤180°'), '与血管接触：肠系膜上动脉>180°。远处转移：未见。'))
        self.assertEqual(p['vessels']['SMA']['state'],'CONFLICT')
    def test_compatible_angles(self):self.assertFalse(disjoint(angle_value('≤180°'),angle_value('<180°')))
    def test_summary_only_does_not_fill_other_vessels(self):
        p,s=assess(record('肿块评估：胰头肿块15X18mm。','与血管接触：门静脉>180°。远处转移：未见。'))
        self.assertTrue(p['vessels']['PV']['represented']);self.assertFalse(p['vessels']['SMA']['represented'])
    def test_ca19_not_celiac_axis(self):
        p,s=assess(record(BASE.replace('2.与腹腔干动脉无明显接触。','CA19-9明显升高。')))
        self.assertFalse(p['vessels']['CA']['represented'])
    def test_postoperative_context(self):self.assertIn('POSTOPERATIVE_OR_RECURRENCE_CONTEXT',assess(record(BASE+'胰十二指肠切除术后。'))[1][1])
    def test_node_size_not_borrowed(self):
        p,s=assess(record(BASE.replace('大小约15X18mm','可见淋巴结15X18mm')))
        self.assertFalse(p['lesion_size']['represented'])
    def test_explicit_nonmeasurable(self):self.assertEqual(assess(record(BASE.replace('大小约15X18mm','病灶无法测量')))[1][0],'PASS')
    def test_metastasis_conflict(self):self.assertIn('METASTASIS_FIELD_CONFLICT',assess(record(BASE.replace('可能良性病变','转移病变')))[1][1])
    def test_unverified_source_does_not_pass(self):self.assertEqual(assess(record(),False)[1][0],'REVIEW')
    def test_normal_pancreas_size_not_target(self):
        p,s=assess(record(BASE.replace('胰头可见肿块，大小约15X18mm','胰头大小约15X18mm')))
        self.assertFalse(p['lesion_context']);self.assertEqual(s[0],'REVIEW')
    def test_no_angle_explicit_contact(self):
        p,s=assess(record(BASE.replace('肝总动脉无明显接触','肝总动脉受侵')))
        self.assertEqual(s[0],'PASS');self.assertTrue(p['vessels']['CHA']['angle_unquantified'])
    def test_invalid_angle(self):self.assertEqual(relationship('接触面500°')[0],'UNRECOGNIZED_VALUE')
    def test_zero_angle(self):self.assertEqual(relationship('接触面0°')[0],'NO_CONTACT_OR_INVASION')
    def test_contact_field_explicit_none(self):self.assertEqual(relationship('接触面无')[0],'NO_CONTACT_OR_INVASION')
    def test_ct_template_omits_repeated_noun(self):self.assertEqual(assess(record(BASE.replace('胰头可见肿块，大小约15X18mm','增强实质期呈低密度，大小约15X18mm，位于胰头部')))[1][0],'PASS')
    def test_unit_after_each_dimension(self):
        p,s=assess(record(BASE.replace('15X18mm','15mm×1.8cm')))
        self.assertEqual(p['lesion_size']['values_mm'],[15,18]);self.assertEqual(s[0],'PASS')
    def test_two_lesions_need_target_review(self):self.assertIn('MULTIPLE_LESION_MEASUREMENTS_REVIEW',assess(record(BASE.replace('15X18mm','15X18mm，胰尾肿块20X30mm')))[1][1])
    def test_exact_source_column_wrapper(self):
        d=record('原表【报告表现】test finding\n原表【报告结论】test impression','')
        d.update(index_origin=RULES['allowed_origins'][1],source_file_name='source.xlsx',source_sheet='Sheet1',source_row=2)
        tables={('synthetic','source.xlsx','Sheet1','2'):[{'date':'2024-01-01','findings':'testfinding','impression':'testimpression','path':'synthetic','source_record_id':'r2'}]}
        self.assertTrue(verify_source(d,'synthetic',{},tables,RULES)['verified'])
    def test_liver_no_metastasis_not_conflict(self):self.assertNotIn('METASTASIS_FIELD_CONFLICT',assess(record(BASE.replace('可能良性病变','无转移瘤')))[1][1])
    def test_liver_metastasis_not_excluded_is_uncertain(self):
        p,s=assess(record(BASE.replace('可能良性病变','转移瘤待排')))
        self.assertEqual(p['metastatic_domain']['liver']['state'],'EXPLICIT_INDETERMINATE')
    def test_nodal_contact_does_not_fill_primary_relation(self):
        p,s=assess(record(BASE+'可疑淋巴结：肠系膜上动脉周围组（接触>180°）。'))
        self.assertEqual(p['vessels']['SMA']['state'],'NO_CONTACT_OR_INVASION')
    def test_narrative_shared_predicate(self):
        p,s=assess(record(BASE.replace('1.与肠系膜上动脉无明显接触。2.与腹腔干动脉无明显接触。3.与肝总动脉无明显接触。','SMA、腹腔干、CHA均无明显接触。')))
        self.assertEqual(s[0],'PASS')
    def test_source_must_match_original(self):
        d=record();d.update(index_origin=RULES['allowed_origins'][0],source_file_name='A001.csv.record_clusters.jsonl',record_cluster_id=1)
        clusters={('A001.csv','1'):[{'uids':['synthetic'],'date':'2024-01-01','text':'not the report','path':'synthetic','cluster':1}]}
        self.assertFalse(verify_source(d,'synthetic',clusters,{},RULES)['verified'])
    def test_nat_excerpt_never_verified_without_source(self):
        d=record();d['index_origin']=RULES['supplement_origin_requires_review']
        self.assertEqual(verify_source(d,'synthetic',{},{},RULES)['reason'],'SUPPLEMENT_OR_UNKNOWN_ORIGIN')
    def test_ambiguous_raw_identity(self):
        d=record();d.update(index_origin=RULES['allowed_origins'][0],source_file_name='A001.csv.record_clusters.jsonl',record_cluster_id=1)
        clusters={('A001.csv','1'):[{'uids':['synthetic','other'],'date':'2024-01-01','text':BASE,'path':'synthetic','cluster':1}]}
        self.assertFalse(verify_source(d,'synthetic',clusters,{},RULES)['verified'])

if __name__=='__main__':unittest.main(verbosity=2)
