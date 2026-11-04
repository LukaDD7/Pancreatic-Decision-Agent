"""New IR counterexamples, including migrated relevant DYN/localization cases."""
import copy
import json
from pathlib import Path
import unittest
from scripts.cohort_construction.p2_decision_ir.extract import Extractor, relate
from scripts.cohort_construction.p2_decision_ir.fhir_links import source_crosswalk
from scripts.cohort_construction.p2_fhir_replay.core import pack_event

CONFIG = json.loads((Path(__file__).parents[2]/'p2_decision_localization/configs/rules-v0.3.json').read_text())


def note(text, key='a', patient='demo', time='2025-01-01 10:00:00', title='医师查房记录', family='WARD_ROUND'):
    return dict(text=text, record_key=key, patient_uid=patient, visit_id='v1', body_time=time,
                system_time=time, document_type=title, family=family)


class IRTests(unittest.TestCase):
    def setUp(self): self.e = Extractor(CONFIG)
    def current(self, text, **kwargs):
        return [n for n in self.e.extract(note(text, **kwargs)) if n['status']=='CURRENT_STATEMENT_CANDIDATE']

    def test_IR001_negative_judgment_is_a_decision_not_a_dropped_negation(self):
        self.assertTrue(self.current('病情：胰腺占位，目前无手术指征，拟行EUS活检。'))

    def test_IR002_vascular_observation_not_surgical_judgment(self):
        self.assertFalse(self.current('未见肠系膜上动脉受侵；与门静脉关系无法判断。'))

    def test_IR003_question_not_affirmative_but_planned_assessment_kept(self):
        self.assertFalse(self.current('胰腺肿瘤是否有手术指征？'))
        nodes=self.current('治疗计划：胰腺肿瘤，待MRI后评估是否有手术指征。')
        self.assertTrue(nodes)
        self.assertTrue(all(s.get('suppressed_as_affirmative') for n in nodes for s in n['signals'] if s['label']=='INDICATION_YES'))

    def test_IR004_risk_template_and_real_risk_are_different(self):
        self.assertFalse(self.current('手术可能存在的风险：术后感染，营养情况不佳，恢复欠理想。'))
        self.assertTrue(self.current('患者胆红素高，手术风险高，今予PTCD，待改善后行手术治疗。'))

    def test_IR005_history_and_current_withdrawal_not_confused(self):
        self.assertFalse(self.current('现病史：当时无手术指征，拟行新辅助治疗。',family='ADMISSION'))
        self.assertTrue(self.current('目前心功能不全，暂不适合全麻手术治疗。'))

    def test_IR006_trial_title_not_current_inoperability(self):
        self.assertFalse(self.current('参加局部进展期不可切除胰腺癌的临床研究。'))

    def test_IR007_other_operation_and_consent_review_not_deleted(self):
        nodes=self.e.extract(note('待感染控制后手术。',title='输尿管软镜知情同意'))
        self.assertTrue(nodes)
        self.assertTrue(all(n['status']=='CONTEXT_REVIEW' for n in nodes))

    def test_IR008_narrow_radical_word_not_required(self):
        for operation in ['胰十二指肠切除术','胰体尾脾切除术','根治性胰十二指肠切除术','Whipple手术']:
            with self.subTest(operation=operation): self.assertTrue(self.current('治疗计划：拟行'+operation+'。'))

    def test_IR009_executed_operation_not_new_plan(self):
        nodes=self.e.extract(note('在全麻下行胰十二指肠切除术，操作顺利。',title='手术记录',family='OTHER'))
        self.assertTrue(any(n['role']=='EXECUTION_MENTION' for n in nodes))
        self.assertFalse(any(n['status']=='CURRENT_STATEMENT_CANDIDATE' for n in nodes))

    def test_IR010_diagnostic_nat_not_new_treatment_plan(self):
        nodes=self.e.extract(note('目前首先考虑：胰头恶性肿瘤新辅助治疗后，需手术治疗。'))
        self.assertFalse(any(s['label']=='NAT_PLAN' for n in nodes for s in n['signals']))

    def test_IR011_conditional_wait_has_multiple_actions_not_many_points(self):
        nodes=self.current('今予PTCD胆汁引流，待胆红素下降后行手术治疗。')
        self.assertEqual(len(nodes),1)
        self.assertIn('PTCD',{a['concept'] for a in nodes[0]['action_mentions']})
        self.assertIn('SURGERY_UNSPECIFIED',{a['concept'] for a in nodes[0]['action_mentions']})

    def test_IR012_repeated_ptcd_after_failure_not_merged(self):
        a=self.e.extract(note('今予PTCD胆汁引流。'))
        b=self.e.extract(note('管道脱出，择期再行PTCD手术治疗。',key='b',time='2025-01-02 10:00:00'))
        nodes=a+b;links=relate(nodes)
        self.assertEqual(len(nodes),2)
        self.assertTrue(links)
        self.assertTrue(all(x['episode_id'] is None for x in links))

    def test_IR013_same_words_different_sources_retained(self):
        a=self.e.extract(note('今予PTCD胆汁引流。'))
        b=self.e.extract(note('今予PTCD胆汁引流。',key='b',time='2025-01-02 10:00:00'))
        self.assertEqual(len(a+b),2)
        self.assertTrue(any(l['hint']=='IDENTICAL_WORDING_NOT_MERGED' for l in relate(a+b)))

    def test_IR014_cross_patient_never_linked(self):
        nodes=self.e.extract(note('今予PTCD。'))+self.e.extract(note('今予ERCP。',key='b',patient='other',time='2025-01-02 10:00:00'))
        self.assertFalse(relate(nodes))

    def test_IR015_same_instant_different_timezone_not_ordered(self):
        nodes=self.e.extract(note('今予PTCD。'))+self.e.extract(note('今予ERCP。',key='b',time='2025-01-01T02:00:00Z'))
        self.assertFalse(relate(nodes))

    def test_IR016_source_spans_exact_and_unmutated(self):
        n=note('总胆红素404.1μmol/L。今予PTCD，待指标下降后手术。');old=copy.deepcopy(n)
        nodes=self.e.extract(n)
        for x in nodes:
            self.assertEqual(n['text'][slice(*x['span'])],x['quote'])
            for a in x['action_mentions']:self.assertEqual(n['text'][a['start']:a['end']],a['text'])
        self.assertEqual(n,old)

    def test_IR017_no_history_result_or_reference_gate(self):
        n=note('治疗计划：拟行MRI，待结果后再评估是否手术。')
        a=self.e.extract(n);n['future_mri_available']=False;n['expert_reference']='unknown';b=self.e.extract(n)
        self.assertEqual(a,b)
        self.assertTrue(any(x['role']=='DECISION_CANDIDATE' for x in b))

    def test_IR018_imaging_negative_not_positive_resectability(self):
        nodes=self.current('病情：胰腺肿瘤不可切除。')
        self.assertTrue(nodes)
        self.assertFalse(any(s['label']=='INDICATION_YES' and not s.get('suppressed_as_affirmative') for n in nodes for s in n['signals']))

    def test_IR019_postop_posterior_not_future_intent(self):
        self.assertFalse(self.current('术后第一天，手术切口恢复可，胆红素正常。'))
        self.assertFalse(self.current('已行胰体尾切除术及腹膜后淋巴结清扫。'))

    def test_IR020_date_only_not_padded_to_midnight(self):
        n=note('今予PTCD。',time='2025-01-01');a=self.e.extract(n)
        self.assertIsNone(a[0]['ordering_time'])

    def test_IR021_postop_type_does_not_block_a_new_plan(self):
        self.assertTrue(self.current('引流管脱出，择期再行PTCD手术治疗。',family='POSTOP'))
        nodes=self.current('今PTCD引流不畅，仍需胆汁引流，今予ERCP治疗。',family='POSTOP')
        self.assertTrue(nodes)
        self.assertEqual(nodes[0]['surgical_pathway_relevance'],'UNADJUDICATED')
        self.assertFalse(self.current('术后第一天，切口愈合可。',family='POSTOP'))

    def test_IR022_scheduled_anaesthesia_not_performed(self):
        for text in ['定于2025年11月10日在全麻下行胰十二指肠切除术治疗。',
                     '故拟全麻下行胰体癌根治术治疗。',
                     '拟于2025-11-10行胰十二指肠切除术。']:
            nodes=self.current(text,family='PREOP_DISCUSSION')
            self.assertTrue(nodes,text)
            self.assertFalse(any(n['execution_cues'] for n in nodes),text)
        nodes=self.e.extract(note('已行PTCD，拟于2025-11-10行胰十二指肠切除术。'))
        self.assertTrue(any(n['execution_cues'] for n in nodes))

    def test_IR023_discharge_threshold_not_observed_feedback(self):
        nodes=self.e.extract(note('每天记引流量，如连续3天引流量＜5ml，可拔管。',family='DISCHARGE'))
        self.assertEqual(nodes[0]['role'],'INSTRUCTION_MENTION')
        nodes=self.e.extract(note('术后第一天，今日引流量250ml。',family='POSTOP'))
        self.assertEqual(nodes[0]['role'],'FEEDBACK_MENTION')

    def test_IR024_template_intent_not_current_judgment(self):
        nodes=self.e.extract(note('可能出现的意外及防范措施：如发生出血，需行手术治疗。',family='PREOP_DISCUSSION'))
        self.assertFalse(any(n['status']=='CURRENT_STATEMENT_CANDIDATE' for n in nodes))

    def test_IR025_supportive_plan_not_confirmed_resection_decision(self):
        nodes=self.current('继续予以营养支持、对症补液支持等治疗。',family='POSTOP')
        self.assertIn('SUPPORTIVE_CARE_PATHWAY_LINK_REQUIRED',nodes[0]['flags'])
        nodes=self.current('先行营养支持，待营养改善后行手术治疗。')
        self.assertTrue(nodes)
        self.assertNotIn('SUPPORTIVE_CARE_PATHWAY_LINK_REQUIRED',nodes[0]['flags'])

    def test_IR026_postdischarge_observation_not_instruction(self):
        nodes=self.e.extract(note('患者出院后引流量100ml，今日来院复查。',family='OUTPATIENT'))
        self.assertEqual(nodes[0]['role'],'FEEDBACK_MENTION')

    def test_IR027_fhir_link_requires_explicit_patient_alias(self):
        text='今予PTCD。';nodes=self.e.extract(note(text))
        event=pack_event('N01','document','a','查房',text)
        self.assertFalse(source_crosswalk(nodes,[event],{}))
        self.assertEqual(len(source_crosswalk(nodes,[event],{'demo':'N01'})),1)

    def test_IR028_fhir_link_rejects_changed_source_version(self):
        nodes=self.e.extract(note('今予PTCD。'))
        event=pack_event('N01','document','a','查房','今予ERCP。')
        with self.assertRaises(ValueError):source_crosswalk(nodes,[event],{'demo':'N01'})

    def test_IR029_fhir_link_rejects_ambiguous_source(self):
        text='今予PTCD。';nodes=self.e.extract(note(text))
        event=pack_event('N01','document','a','查房',text)
        with self.assertRaises(ValueError):source_crosswalk(nodes,[event,event],{'demo':'N01'})

    def test_IR030_historical_evidence_does_not_erase_current_conclusion(self):
        nodes=self.current('结合既往肿瘤指标及影像表现，患者目前治疗效果确切，目前已具备手术条件。')
        self.assertTrue(nodes)
        self.assertIn('CURRENT_JUDGMENT_WITH_HISTORICAL_CONTEXT',nodes[0]['flags'])
        self.assertFalse(self.current('当时无手术指征，拟行新辅助治疗。'))
        self.assertFalse(self.current('目前回顾当时无手术指征。'))

    def test_IR031_scheduled_drainage_biopsy_not_lost_when_execution_removed(self):
        for operation in ['ERCP治疗','PTCD术','EUS+FNA治疗']:
            nodes=self.current('定于2025年11月10日在局麻下行'+operation+'。',family='PREOP_DISCUSSION')
            self.assertTrue(nodes)
            self.assertFalse(nodes[0]['execution_cues'])
        nodes=self.e.extract(note('定于2025年11月10日在全麻下行输尿管镜检术。',family='PREOP_DISCUSSION'))
        self.assertTrue(nodes)
        self.assertTrue(all(n['status']=='CONTEXT_REVIEW' for n in nodes))

    def test_IR032_link_order_is_stable_across_topic_sets(self):
        a=self.e.extract(note('今予PTCD，并予以营养支持。'))
        b=self.e.extract(note('今予ERCP，并予以营养支持。',key='b',time='2025-01-02 10:00:00'))
        links=relate(a+b)
        self.assertEqual([l['topic'] for l in links],sorted(l['topic'] for l in links))
        self.assertEqual(relate(b+a),links)


if __name__=='__main__':unittest.main()
