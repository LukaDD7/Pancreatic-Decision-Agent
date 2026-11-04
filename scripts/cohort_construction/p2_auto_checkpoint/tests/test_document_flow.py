"""Observed document-pattern failures plus controls; no clinical gold assertions."""
import copy
import json
import unittest

from test_contract import ROOT,doc,image
from test_diagnostic_decisions import semantics,evolved
from scripts.cohort_construction.p2_auto_checkpoint.extract import extract,quote,window
from scripts.cohort_construction.p2_auto_checkpoint.document_flow import profile,generic_support,prior_occurrences,evidence_guard
from scripts.cohort_construction.p2_auto_checkpoint.semantic import analyze_evolution,normalize
from scripts.cohort_construction.p2_auto_checkpoint.run import choose_comparator,select_status

CFG=json.loads((ROOT/'config/p2_auto_checkpoint_v03.json').read_text())
PLAN='经与家属谈话并签字同意定于2022年01月04日在全麻下行胰十二指肠切除术治疗。'
GENERIC='术中根据探查情况决定具体手术方式，注意仔细操作解剖，严密止血，避免副损伤。'

def candidates(ss):return extract(ss,CFG)[0]

class DocumentFlowTests(unittest.TestCase):
    def test_body_header_recognizes_nonstandard_admission_title(self):
        s=doc('入 院 记 录\n记录时间：2022-01-01 12:00\n拟完善MR。',title='胰腺癌')
        self.assertEqual(profile(s),'admission')

    def test_cta_choice_in_diagnostic_basis_survives(self):
        s=doc('2022-01-02 12:00\n诊断依据：此次入院肌钙蛋白2.224μg/L↑，拟完善冠脉CTA检查。',title='主治医师查房记录')
        c=candidates([s])[0]
        self.assertEqual(c['paragraph_role'],'diagnostic_assessment')
        self.assertEqual(c['flow_route'],'primary');self.assertEqual(c['management_track'],'cardiac_safety')

    def test_same_document_fields_bundle_keeps_every_span(self):
        s=doc('2022-01-02 12:00\n拟施手术名称和手术方式：胰十二指肠切除术。\n'+PLAN,title='术前小结')
        cc=candidates([s]);primary=[c for c in cc if c['flow_route']=='primary']
        self.assertEqual((len(cc),len(primary)),(2,1))
        self.assertEqual(len(primary[0]['member_mentions']),2)
        self.assertEqual(primary[0]['choice_start'],min(c['choice_start'] for c in cc))
        self.assertEqual({c['anchor']['quote'] for c in cc},{m['anchor']['quote'] for m in primary[0]['member_mentions']})

    def test_changed_plan_and_different_dates_not_bundled(self):
        s=doc('2022-01-02 12:00\n'+PLAN+'\n拟于2022年01月05日行胰十二指肠切除术。\n因发热暂缓胰十二指肠切除术。',title='术前小结')
        self.assertEqual(sum(c['flow_route']=='primary' for c in candidates([s])),3)

    def test_same_action_explicit_reassessment_stays_separate(self):
        s=doc('2022-01-02 12:00\n'+PLAN+'\n再次评估肝功能改善，仍拟行胰十二指肠切除术。',title='术前讨论')
        self.assertEqual(sum(c['flow_route']=='primary' for c in candidates([s])),2)

    def test_generic_future_phrase_is_support_only(self):
        s=doc('2022-01-02 12:00\n'+GENERIC,title='术前讨论')
        c=candidates([s])[0]
        self.assertEqual(c['flow_route'],'support_only')
        self.assertEqual(c['flow_reason'],'generic_future_exploration_statement')

    def test_individualized_branch_and_added_finding_survive(self):
        for text in ['若术中发现静脉受侵则考虑重建。',GENERIC+'新发胸痛，暂缓手术。',
                     '术中根据探查情况决定具体手术方式，现发现静脉受侵，拟重建。']:
            self.assertIsNone(generic_support(text,'preoperative_summary','generic_precautions'))

    def test_generic_readiness_not_dropped_by_document_type(self):
        s=doc('2022-01-02 12:00\n术前主刀医师查看患者及病情评估：患者目前一般情况尚可，营养状况尚可，手术风险较大，术前术后应注意及时予对症治疗。',title='术前小结')
        self.assertTrue(all(c['flow_route']=='primary' for c in candidates([s])))

    def test_summary_as_only_visible_plan_survives(self):
        c=candidates([doc('2022-01-02 12:00\n'+PLAN,title='术前小结')])[0]
        self.assertEqual(c['flow_route'],'primary');self.assertFalse(c['prior_plan_mentions'])

    def test_prior_plan_context_is_earlier_not_future(self):
        ss=[doc('2022-01-01 12:00\n'+PLAN,identity='old'),
            doc('2022-01-02 12:00\n'+PLAN,title='术前小结',identity='now'),
            doc('2022-01-03 12:00\n'+PLAN,identity='future')]
        c=next(c for c in candidates(ss) if c['source_id']==ss[1]['source_id'])
        self.assertEqual([p['anchor']['source_id'] for p in c['prior_plan_mentions']],[ss[0]['source_id']])
        self.assertEqual(c['flow_route'],'primary')

    def test_old_score_retrieved_as_same_wording_not_same_measurement(self):
        ss=[doc('2022-01-01 12:00\n评估分数：4。级别：中危。措施：基本预防。',identity='old'),
            doc('2022-01-02 12:00\n拟行手术。',identity='prev'),
            doc('2022-01-03 12:00\n评估分数：4。级别：中危。拟行手术。',title='术前小结',identity='now')]
        previous=semantics(candidates([ss[1]])[0],role='decision')
        hits=prior_occurrences(quote(ss[2],0,len(ss[2]['text'])),previous,ss)
        self.assertTrue(any(h['anchor']['quote']=='评估分数：4。级别：中危' for h in hits))
        self.assertTrue(all('not_proof_same_measurement' in h['interpretation'] for h in hits))

    def test_baseline_raw_clinical_note_reaches_model(self):
        ss=[doc('2022-01-01 12:00\n评估分数：4。级别：中危。',identity='old'),
            doc('2022-01-02 12:00\n拟行手术。',identity='prev'),
            doc('2022-01-03 12:00\n拟行手术。',identity='now')]
        prev=semantics(candidates([ss[1]])[0],role='decision');c=semantics(candidates([ss[2]])[0],role='decision')
        class Fake:
            def ask(self,task,payload,schema):
                self.payload=payload
                return dict(decision_interval_status='ordered',relation_to_previous='same_round',evidence_evolution='repeat_only',
                    management_process_link='context_only',same_task='yes',current_judgment_supported=True,
                    supports=[],connection_supports=[],missing_information=[],rationale='重复',evidence_character='repeated_documentation_only')
        f=Fake();analyze_evolution(c,prev,window(ss,c,prev),ss,f,CFG)
        self.assertTrue(any(b['interval']=='baseline_before_previous' and '级别：中危' in b['source_text'] for b in f.payload['blocks']))

    def test_same_value_explicit_new_assessment_is_not_auto_repeat(self):
        ss=[doc('2022-01-01 12:00\n评估分数：4。级别：中危。',identity='old'),
            doc('2022-01-02 12:00\n拟行手术。',identity='prev'),
            doc('2022-01-03 12:00\n再次评估分数：4。级别：中危。拟行手术。',title='术前小结',identity='now')]
        prev=semantics(candidates([ss[1]])[0],role='decision');c=candidates([ss[2]])[0]
        a=quote(ss[2],ss[2]['text'].index('再次'),ss[2]['text'].index('拟'))
        self.assertIsNone(evidence_guard(c,prev,evolved([a],evidence_character='explicit_reassessment'),ss))

    def test_old_document_alone_cannot_prove_interval_evolution(self):
        ss=[doc('2022-01-01 12:00\nCT提示静脉受侵。',identity='old'),
            doc('2022-01-02 12:00\n拟行手术。',identity='prev'),
            doc('2022-01-03 12:00\n拟行手术。',identity='now')]
        prev=candidates([ss[1]])[0];c=candidates([ss[2]])[0]
        self.assertIsNotNone(evidence_guard(c,prev,evolved([quote(ss[0],0,len(ss[0]['text']))]),ss))
        # Mixing an old fact with the current choice must not evade either guard.
        self.assertIsNotNone(evidence_guard(c,prev,evolved([quote(ss[0],0,len(ss[0]['text'])),c['anchor']]),ss))

    def test_time_disagreement_retained_and_future_mr_excluded(self):
        s=doc('2022-01-02 12:00\n'+PLAN,title='术前小结');im=image(at='2022-01-02 12:30:00')
        c=candidates([s])[0];self.assertEqual(c['documentation_time_status'],'body_and_creation_differ')
        self.assertEqual(s['clocks']['source_create_time']['value'],'2022-01-09 12:00:00')
        self.assertEqual(window([s,im],c)['source_ledger'][1]['disposition'],'EXCLUDE_FUTURE')

    def test_comparator_stays_on_same_track_without_forced_relation(self):
        c=candidates([doc('2022-01-03 12:00\n拟行胰十二指肠切除术。')])[0]
        op=candidates([doc('2022-01-01 12:00\n拟行胰十二指肠切除术。',identity='op')])[0]
        cardiac=candidates([doc('2022-01-02 12:00\n拟完善冠脉CTA。',identity='cardiac')])[0]
        p=choose_comparator(c,[],cardiac,[op,cardiac]);self.assertEqual(p['candidate_id'],op['candidate_id'])
        self.assertIsNone(choose_comparator(c,[],cardiac,[cardiac]))

    def test_evidence_guard_cannot_upgrade_or_force_uncertainty_into_cp(self):
        c=semantics(candidates([doc('2022-01-02 12:00\n拟行手术。')])[0],role='decision');c['historical_hint']=False
        c['semantic']['temporal_role']='current'
        e=evolved([c['anchor']]);e['evidence_guard']='REPEATED_WORDING_IDENTITY_UNCERTAIN'
        self.assertEqual(select_status(c,e,None),'UNCERTAIN_REPEATED_WORDING_IDENTITY')

    def test_first_visible_real_plan_not_required_to_have_interval_change(self):
        s=doc('2022-01-02 12:00\n拟完善术前检验检查，排除手术禁忌症。')
        c=semantics(candidates([s])[0],role='decision');c['historical_hint']=False;c['semantic']['temporal_role']='current'
        e=evolved([],evidence_character='plan_or_form_only');e.update(evidence_evolution='first_visible',relation_to_previous='first_visible')
        e['evidence_guard']=evidence_guard(c,None,e,[s])
        self.assertEqual(select_status(c,e,None),'CANDIDATE_FIRST_VISIBLE')
        # The same form-only characterization cannot establish a later change.
        e['evidence_evolution']='new_relevant';e['supports']=[c['anchor']]
        self.assertEqual(evidence_guard(c,c,e,[s]),'NO_NEW_CLINICAL_EVIDENCE')

    def test_existing_plan_classification_requires_earlier_quote(self):
        ss=[doc('2022-01-01 12:00\n'+PLAN,identity='old'),doc('2022-01-02 12:00\n'+PLAN,title='术前小结',identity='now')]
        c=next(c for c in candidates(ss) if c['source_id']==ss[1]['source_id'])
        class Fake:
            def ask(self,task,payload,schema):
                self.payload=payload;v=payload['candidates'][0]
                return {'items':[dict(candidate_id=v['candidate_id'],role='decision',scope='yes',management_question='手术安排',action='手术',
                    temporal_role='current',selection_reason_known=False,supports=[dict(alias='S000',quote=PLAN)],
                    missing_information=[],rationale='重述',record_function='existing_plan_documentation',prior_plan_supports=[])]}
        f=Fake();rr,ff=normalize([c],ss,f,CFG)
        self.assertFalse(ff);self.assertEqual(rr[0]['semantic']['record_function'],'uncertain')
        self.assertTrue(f.payload['candidates'][0]['prior_plan_context'])

    def test_bad_prior_quote_does_not_discard_valid_current_choice(self):
        ss=[doc('2022-01-01 12:00\n'+PLAN,identity='old'),doc('2022-01-02 12:00\n'+PLAN,title='术前小结',identity='now')]
        c=next(c for c in candidates(ss) if c['source_id']==ss[1]['source_id'])
        class Fake:
            def ask(self,task,payload,schema):
                v=payload['candidates'][0]
                return {'items':[dict(candidate_id=v['candidate_id'],role='decision',scope='yes',management_question='手术安排',action='手术',
                    temporal_role='current',selection_reason_known=False,supports=[dict(alias='S000',quote=PLAN)],missing_information=[],
                    rationale='重述',record_function='existing_plan_documentation',prior_plan_supports=[dict(alias='S000',quote=PLAN)])]}
        rr,ff=normalize([c],ss,Fake(),CFG)
        self.assertFalse(ff);self.assertEqual(rr[0]['semantic']['record_function'],'uncertain')
        self.assertEqual(rr[0]['semantic']['prior_plan_evidence_status'],'UNCERTAIN_INVALID_PRIOR_QUOTE')
        self.assertFalse(rr[0]['semantic']['prior_plan_supports'])
        self.assertEqual(rr[0]['semantic']['supports'][0]['source_id'],ss[1]['source_id'])

    def test_exact_postop_copy_supports_operative_phase_not_new_choice(self):
        choice='肠系膜上静脉血管侵犯，遂决定行开腹根治性胰十二指肠切除术+肠系膜上静脉楔形切除重建术。'
        op=doc('手术记录\n记录时间：2022-01-02 15:00\n手术时间：2022-01-02 12:00-2022-01-02 14:00\n探查：'+choice,title='手术记录',identity='op')
        post=doc('2022-01-02 16:00\n探查：'+choice,title='术后首次病程记录',identity='post')
        cc=candidates([op,post]);p=next(c for c in cc if c['source_id']==post['source_id'])
        self.assertEqual(p['flow_route'],'support_only')
        self.assertEqual(p['flow_reason'],'exact_secondary_operative_restatement')
        c=next(c for c in cc if c['source_id']==op['source_id']);self.assertEqual(c['flow_route'],'primary')
        text=''.join(b['anchor']['quote'] for b in window([op,post],c)['blocks'])
        self.assertIn('血管侵犯',text);self.assertNotIn('遂决定',text)

if __name__=='__main__':unittest.main()
