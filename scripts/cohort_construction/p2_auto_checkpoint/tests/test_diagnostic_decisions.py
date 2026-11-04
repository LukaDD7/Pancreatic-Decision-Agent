"""Counterexamples for reports as evidence AND earlier diagnostic choices."""
import json
from pathlib import Path
import unittest

from test_contract import CONFIG, ROOT, candidate, doc, image
from scripts.cohort_construction.p2_auto_checkpoint.extract import comparison_clock, extract, window
from scripts.cohort_construction.p2_auto_checkpoint.semantic import (
    EVOLUTION_INSTRUCTION, NORMAL_INSTRUCTION, SYSTEM, analyze_evolution, normalize)
from scripts.cohort_construction.p2_auto_checkpoint.run import choose_comparator, screen, select_status
from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import build, import_sources, validate

CFG=json.loads((ROOT/'config/p2_auto_checkpoint_v02.json').read_text())

def semantics(c, role='historical_performed_test', scope='yes'):
    c['semantic']=dict(role=role,scope=scope,temporal_role='historical',
                       management_question='胰腺占位进一步分期',action='选择MR检查',supports=[c['anchor']])
    return c

def evolved(supports, **kwargs):
    return dict(status='VALIDATED_QUOTES',decision_interval_status='upper_bound_only',
                relation_to_previous='new_round',evidence_evolution='new_relevant',
                current_judgment_supported=True,management_process_link='context_only',
                connection_supports=[],supports=supports,**kwargs)

class DiagnosticDecisionTests(unittest.TestCase):
    def test_report_produces_evidence_and_earlier_choice_candidate(self):
        cc,rr,_=extract([image()],CFG)
        self.assertEqual((len(cc),len(rr)),(1,1))
        self.assertEqual(rr[0]['role'],'evidence_only_not_a_decision')
        self.assertIsNone(cc[0]['decision_clock']['value'])
        self.assertEqual(comparison_clock(cc[0])['value'],'2022-01-02 10:00:00')
        self.assertEqual(cc[0]['selection_basis'],'performed_test_report')

    def test_same_exam_versions_one_choice_all_results_excluded_from_prechoice(self):
        ss=[image(uid='a'),image('另一来源的同次结果',uid='b')]
        cc,rr,_=extract(ss,CFG)
        self.assertEqual((len(cc),len(rr)),(1,2))
        w=window(ss,cc[0])
        self.assertEqual(w['blocks'],[])
        self.assertEqual({l['disposition'] for l in w['source_ledger']},{'EXCLUDE_CURRENT_TEST_RESULT_FROM_PRECHOICE'})

    def test_missing_exam_number_preserves_ambiguous_examinations(self):
        cc,_,_=extract([image(number=None,uid='a'),image(number=None,uid='b')],CFG)
        self.assertEqual(len(cc),2)

    def test_hist_pet_keeps_prior_ultrasound_mr_but_not_own_result(self):
        s=doc('2022-01-03 12:00\n现病史：腹痛，B超提示占位，MR提示分期未明。于2022-01-01在外院就诊，予以PET-MRI检查提示远处转移。')
        c=next(x for x in extract([s],CFG)[0] if 'PET_MRI' in x['topics'])
        self.assertIsNone(c['decision_clock']['value'])
        self.assertEqual(c['execution_clock']['value'],'2022-01-01')
        w=window([s],c);t=''.join(b['anchor']['quote'] for b in w['blocks'])
        self.assertIn('B超提示占位',t);self.assertIn('MR提示分期未明',t)
        self.assertNotIn('检查提示远处转移',t)
        self.assertTrue(w['cutoff_is_choice_time_upper_bound'])
        self.assertEqual(w['blocks'][0]['interval'],'retrospective_pretest_narrative_unverified')

    def test_future_state_is_not_inferred_pretest_state(self):
        im=image();later=doc('2022-01-03 12:00\n发现转移，取消手术。')
        c=extract([im],CFG)[0][0];w=window([im,later],c)
        self.assertFalse(w['blocks']);self.assertEqual(w['source_ledger'][1]['disposition'],'EXCLUDE_FUTURE')

    def test_reason_unknown_does_not_block_evolving_test_candidate(self):
        old=doc('2022-01-01 12:00\nCT分期仍不明确，拟进一步检查。')
        c=semantics(extract([image()],CFG)[0][0]);c['semantic']['selection_reason_known']=False
        a=dict(source_id=old['source_id'],start=0,end=len(old['text']),quote=old['text'])
        self.assertEqual(select_status(c,evolved([a]),None),'CANDIDATE_TEST_NEW_EVIDENCE_TIME_UNCERTAIN')

    def test_report_itself_cannot_satisfy_new_evidence_for_own_choice(self):
        c=semantics(extract([image()],CFG)[0][0])
        self.assertEqual(select_status(c,evolved([c['anchor']]),None),'UNCERTAIN_NO_PRECHOICE_EVIDENCE_SUPPORT')

    def test_first_visible_report_without_prestate_is_uncertain(self):
        c=semantics(extract([image()],CFG)[0][0]);e=evolved([])
        e.update(relation_to_previous='first_visible',evidence_evolution='first_visible')
        self.assertEqual(select_status(c,e,None),'UNCERTAIN_PRECHOICE_STATE')

    def test_historical_role_is_not_discarded_by_screen(self):
        c=semantics(extract([image()],CFG)[0][0]);eligible,unselected=screen([c])
        self.assertEqual(len(eligible),1);self.assertFalse(unselected)
        c['semantic']['scope']='no';eligible,unselected=screen([c]);self.assertFalse(eligible)

    def test_normalization_has_prior_context_and_unknown_choice_time(self):
        prior=doc('2022-01-01 12:00\n胰腺占位，分期未明，目前尚未决定手术。')
        ss=[prior,image()];cc=extract([ss[1]],CFG)[0]
        class Fake:
            def ask(self,task,payload,schema):
                self.payload=payload;c=payload['candidates'][0]
                return {'items':[dict(candidate_id=c['candidate_id'],role='historical_performed_test',scope='yes',
                    management_question='诊断分期',action='MR检查',temporal_role='historical',selection_reason_known=False,
                    supports=[dict(alias=c['alias'],quote='未见明确转移。\n[original_impression]')],missing_information=[],rationale='检查实施线索')]}
        f=Fake();rr,ff=normalize(cc,ss,f,CFG)
        self.assertFalse(ff);self.assertEqual(len(rr),1)
        inp=f.payload['candidates'][0]
        self.assertIsNone(inp['selection_time']['value'])
        self.assertIn('尚未决定手术',inp['prior_context'][0]['source_text'])

    def test_evolution_explicitly_has_both_events_times_and_question(self):
        old=doc('2022-01-01 12:00\nCT不能明确分期，拟进一步评估。',identity='old')
        pc=semantics(candidate(old),role='decision');im=image();c=semantics(extract([im],CFG)[0][0])
        class Fake:
            def ask(self,task,payload,schema):
                self.payload=payload
                return dict(decision_interval_status='upper_bound_only',relation_to_previous='new_round',
                    evidence_evolution='insufficient',management_process_link='uncertain',same_task='uncertain',
                    current_judgment_supported=True,supports=[],connection_supports=[],missing_information=['选择时间未知'],rationale='保留')
        f=Fake();analyze_evolution(c,pc,window([old,im],c,pc),[old,im],f,CFG)
        p=f.payload;self.assertEqual(p['previous_decision']['candidate_id'],pc['candidate_id'])
        self.assertEqual(p['previous_decision']['selection_time']['value'],'2022-01-01 12:00:00')
        self.assertIsNone(p['current_decision']['selection_time']['value'])
        self.assertEqual(p['current_decision']['execution_time']['value'],'2022-01-02 10:00:00')
        self.assertIn('两个候选决策事件之间',p['instruction'])
        self.assertTrue(p['comparison_window']['upper_bound_is_not_exact_selection_time'])

    def test_scope_prompts_include_no_commitment_and_no_surgery(self):
        self.assertIn('尚未决定手术',SYSTEM);self.assertIn('最终不推进手术',EVOLUTION_INSTRUCTION)
        self.assertIn('false不等于无决策',NORMAL_INSTRUCTION)

    def test_historical_sequence_can_compare_without_inventing_times(self):
        s=doc('2022-01-03 12:00\n现病史：于外院行MR检查提示分期不明。于2022-01-01在外院予以PET-MRI检查提示占位。')
        cc=extract([s],CFG)[0]
        for c in cc:semantics(c)
        current=next(c for c in cc if 'PET_MRI' in c['topics'])
        prev=choose_comparator(current,cc,None)
        self.assertIsNotNone(prev)
        self.assertLessEqual(prev['anchor']['end'],current['anchor']['start'])
        self.assertIsNone(window([s],current,prev)['lower']['value'])
        self.assertEqual(prev['comparison_basis'],'same_source_narrative_order_only_not_exact_timed_interval')

    def test_previous_test_result_is_new_after_its_choice_not_automatically_baseline(self):
        s=doc('2022-01-03 12:00\n现病史：在外院行MR检查提示分期不明。于2022-01-01在外院予以PET-MRI检查提示占位。')
        cc=extract([s],CFG)[0]
        for c in cc:semantics(c)
        current=next(c for c in cc if 'PET_MRI' in c['topics']);previous=choose_comparator(current,cc,None)
        class Fake:
            def ask(self,task,payload,schema):
                self.payload=payload
                return dict(decision_interval_status='narrative_order_only',relation_to_previous='new_round',
                            evidence_evolution='insufficient',management_process_link='uncertain',same_task='yes',
                            current_judgment_supported=True,supports=[],connection_supports=[],missing_information=[],rationale='保留')
        f=Fake();analyze_evolution(current,previous,window([s],current,previous),[s],f,CFG)
        self.assertFalse(any(b['interval']=='baseline_carried_quote' for b in f.payload['blocks']))
        e=evolved([previous['anchor']])
        self.assertEqual(select_status(current,e,previous),'CANDIDATE_TEST_NEW_EVIDENCE_TIME_UNCERTAIN')

    def test_oldest_record_is_representative_not_source_id_sort(self):
        text='于2022-01-01在外院就诊，予以PET-MRI检查提示占位。'
        a=doc('2022-01-03 12:00\n'+text,identity='a');b=doc('2022-01-04 12:00\n'+text,identity='b')
        cc=extract([b,a],CFG)[0]
        self.assertEqual(cc[0]['source_id'],a['source_id'])

    def test_inferred_choice_not_fabricated_fhir_order(self):
        ss=[image()];c=semantics(extract(ss,CFG)[0][0]);b=build(ss,'synthetic',[c],[])
        self.assertNotIn('ServiceRequest',[e['resource']['resourceType'] for e in b['entry']])
        self.assertTrue(validate(b,ROOT/'private/p2_auto_checkpoint_v0.1_20261024/standards/fhir.schema.json')['official_r4_json_schema'])
        restored,_=import_sources(b);self.assertEqual(restored[0]['text'],ss[0]['text'])

if __name__=='__main__':unittest.main()
