import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.cohort_construction.p2_auto_checkpoint.extract import (
    body_clock, clock, extract, load_sources, quote, time_order, window)
from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import build, validate, import_sources, fhir_clock
from scripts.cohort_construction.p2_auto_checkpoint.semantic import exact_supports, mask_text, normalize, analyze_evolution
from scripts.cohort_construction.p2_auto_checkpoint.run import select_status
from scripts.cohort_construction.p2_pathway_replay.core import project_sources

ROOT = Path(__file__).resolve().parents[4]
CONFIG = json.loads((ROOT/'config/p2_auto_checkpoint_v01.json').read_text())


def doc(text, title='病程记录', identity='a'):
    s = project_sources({'documents_all': [dict(patient_uid='synthetic', document_text=text,
        文书名称=title, source_record_key=identity, create_time='2022-01-09 12:00:00')]}, 'synthetic')[0][0]
    s['clock'] = body_clock(text); s['clock_basis'] = 'body_record_time'
    s['clocks']['body_record_time'] = s['clock']
    return s


def image(text='未见明确转移。', exam='胰腺MR平扫+增强', at='2022-01-02 10:00:00', number='1', uid='im1'):
    s = project_sources({'imaging_all': [dict(patient_uid='synthetic', index_report_uid=uid,
        exam_method=exam, exam_datetime=at, exam_no_raw=number, original_findings=text,
        original_impression=text)]}, 'synthetic')[0][0]
    s['clock'] = s['clocks']['exam_time']; s['clock_basis'] = 'exam_time_proxy_release_unknown'
    return s


def candidate(s):
    cc = extract([s], CONFIG)[0]
    return next(c for c in cc if '拟' in c['anchor']['quote'] or '决定' in c['anchor']['quote'])


class ContractTests(unittest.TestCase):
    def test_record_time_after120_is_found(self):
        t='一般资料\n'+'甲'*140+'\n记录时间：2022-01-02 10:00\n病史'
        self.assertEqual(body_clock(t)['value'], '2022-01-02 10:00:00')

    def test_multiple_record_times_not_chosen(self):
        self.assertIsNone(body_clock('记录时间：2022-01-01 10:00\n记录时间：2022-01-02 10:00')['value'])

    def test_history_date_not_document_clock(self):
        self.assertIsNone(body_clock('现病史：2022-01-01 10:00行检查。')['value'])

    def test_leading_narrative_time(self):
        self.assertEqual(body_clock('2022-01-03 10:00　查房')['value'], '2022-01-03 10:00:00')

    def test_report_only_is_not_decision(self):
        cc, rr, _ = extract([image()], CONFIG)
        self.assertEqual(len(cc), 0); self.assertEqual(len(rr), 1)

    def test_two_report_versions_one_exam_group(self):
        cc, rr, _ = extract([image(uid='im1'), image('不同表述', uid='im2')], CONFIG)
        self.assertEqual(len(cc), 0); self.assertEqual(rr[0]['exam_group'], rr[1]['exam_group'])

    def test_report_without_exam_number_not_forced_merge(self):
        _, rr, _ = extract([image(number=None, uid='im1'), image('不同表述', number=None, uid='im2')], CONFIG)
        self.assertNotEqual(rr[0]['exam_group'], rr[1]['exam_group'])

    def test_narrative_mri_result_is_not_order(self):
        cc, _, _ = extract([doc('2022-01-02 12:00\nMRI提示胰腺占位。')], CONFIG)
        self.assertEqual(cc, [])

    def test_explicit_mri_plan_detected(self):
        c = candidate(doc('2022-01-02 12:00\n拟完善胰腺MRI检查。'))
        self.assertIn('MRI', c['topics']); self.assertFalse(c['historical_hint'])

    def test_chemical_treatment_synonym_not_lost(self):
        cc,_,_=extract([doc('2022-01-02 12:00\n根据病情发展分析，建议行化学治疗。')],CONFIG)
        self.assertEqual(len(cc),1);self.assertIn('SYSTEMIC',cc[0]['topics'])

    def test_resectability_assessment_without_action_plan_detected(self):
        cc,_,_=extract([doc('2022-01-02 12:00\n根据复查影像评估肿瘤不可切除。')],CONFIG)
        self.assertEqual(len(cc),1);self.assertIn('SURGERY',cc[0]['topics'])

    def test_conditional_surgery_plan_without_xing_verb_detected(self):
        cc,_,_=extract([doc('2022-01-02 12:00\n治疗计划：排除手术禁忌症后限期手术治疗。')],CONFIG)
        self.assertEqual(len(cc),1);self.assertEqual(cc[0]['choice_boundary_basis'],'explicit_action_marker')

    def test_radical_operation_synonym_without_qiechu_detected(self):
        cc,_,_=extract([doc('2022-01-02 12:00\n经谈话同意定于明日在全麻下行胰体癌根治术治疗。')],CONFIG)
        self.assertEqual(len(cc),1);self.assertIn('SURGERY',cc[0]['topics'])

    def test_historical_pet_preserved_with_earlier_date(self):
        cc, _, _ = extract([doc('2022-01-03 12:00\n于2022-01-01在外院就诊，予以PET-MRI检查提示胰腺占位。')], CONFIG)
        c=cc[0]; self.assertTrue(c['historical_hint']); self.assertEqual(c['decision_clock']['value'], '2022-01-01')
        self.assertEqual(c['topics'], ['PET_MRI'])

    def test_unknown_title_has_action_fallback(self):
        cc, _, ledger=extract([doc('2022-01-02 12:00\n拟完善MRCP。', title='非标准新表单')], CONFIG)
        self.assertTrue(cc); self.assertEqual(ledger[0]['document_type'], 'other')

    def test_support_form_stays_in_coverage(self):
        cc, _, ledger=extract([doc('拟行手术，已健康教育。',title='护理评估记录')], CONFIG)
        self.assertFalse(cc); self.assertTrue(ledger)

    def test_future_plan_date_is_not_decision_date(self):
        c=candidate(doc('2022-01-02 12:00\n拟于2022-01-04行手术治疗。'))
        self.assertEqual(c['decision_clock']['value'], '2022-01-02 12:00:00')

    def test_date_only_same_day_unordered(self):
        self.assertEqual(time_order(clock('2022-01-02'), clock('2022-01-02 12:00:00')), 'SAME_DAY_UNORDERED')

    def test_minute_source_cannot_be_ordered_before_same_minute_second(self):
        self.assertEqual(time_order(clock('2022-01-02 12:00'),clock('2022-01-02 12:00:30')),'SAME_MINUTE_UNORDERED')
        self.assertEqual(time_order(clock('2022-01-02 11:59'),clock('2022-01-02 12:00:30')),'BEFORE')

    def test_late_mri_not_in_prior_state(self):
        s=doc('2022-01-02 12:00\n拟行手术。'); c=candidate(s)
        late=image(at='2022-01-02 13:00:00')
        w=window([s, late],c)
        self.assertNotIn(late['source_id'], [b['alias'] for b in w['blocks']])
        self.assertEqual(w['source_ledger'][1]['disposition'],'EXCLUDE_FUTURE')

    def test_prior_cta_added_without_current_plan(self):
        s=doc('2022-01-02 12:00\n肌钙蛋白升高。拟行手术。'); c=candidate(s)
        early=image(exam='冠状动脉CTA',at='2022-01-02 10:00:00')
        w=window([s, early],c)
        self.assertIn(early['source_id'],[b['alias'] for b in w['blocks']])
        prefix=next(b for b in w['blocks'] if b['alias']==s['source_id'])['anchor']['quote']
        self.assertNotIn('拟行手术',prefix); self.assertIn('肌钙蛋白升高',prefix)

    def test_noncp_prior_history_is_included(self):
        history=doc('入院\n'+'甲'*140+'\n记录时间：2022-01-01 12:00\n既往室速、室早。',identity='history')
        now=doc('2022-01-02 12:00\n拟完善冠脉CTA。'); c=candidate(now)
        texts=''.join(b['anchor']['quote'] for b in window([history,now],c)['blocks'])
        self.assertIn('室速、室早',texts)

    def test_operative_downstream_not_in_prefix(self):
        s=doc('2022-01-02 12:00\n探查：静脉侵犯。遂决定行切除重建术。切除后出血100ml。',title='手术记录')
        c=next(c for c in extract([s],CONFIG)[0] if '遂决定' in c['anchor']['quote'])
        t=''.join(b['anchor']['quote'] for b in window([s],c)['blocks'])
        self.assertIn('静脉侵犯',t); self.assertNotIn('出血100',t); self.assertNotIn('遂决定',t)

    def test_operative_record_time_not_used_as_choice_time(self):
        s=doc('手术记录\n记录时间：2022-01-02 15:00\n手术时间：2022-01-02 10:00-2022-01-02 12:00\n实施手术名称：根治切除\n探查：静脉侵犯。遂决定行切除重建术。切除后出血100ml。',title='手术记录')
        c=next(c for c in extract([s],CONFIG)[0] if '遂决定' in c['anchor']['quote'])
        during=image(at='2022-01-02 11:00:00')
        w=window([s,during],c)
        self.assertEqual(c['decision_clock']['value'],'2022-01-02 10:00:00')
        self.assertEqual(w['source_ledger'][1]['disposition'],'REVIEW')
        prefix=''.join(b['anchor']['quote'] for b in w['blocks'])
        self.assertIn('静脉侵犯',prefix);self.assertNotIn('实施手术名称',prefix)

    def test_one_sentence_exploration_and_choice_preserve_findings(self):
        s=doc('手术记录\n记录时间：2022-01-02 15:00\n手术时间：2022-01-02 10:00-2022-01-02 12:00\n实施手术名称：根治切除\n探查：腹腔未见转移，静脉侵犯，术中诊断：胰腺肿瘤，遂决定行切除重建术\n切除：完成切除。',title='手术记录')
        c=next(c for c in extract([s],CONFIG)[0] if '遂决定' in c['anchor']['quote'])
        text=''.join(b['anchor']['quote'] for b in window([s],c)['blocks'])
        self.assertIn('静脉侵犯',text);self.assertNotIn('遂决定',text);self.assertNotIn('术中诊断',text)

    def test_indented_operative_paragraph_still_has_stage_clock(self):
        s=doc('手术记录\n记录时间：2022-01-02 15:00\n手术时间：2022-01-02 10:00-2022-01-02 12:00\n    探查：无腹水，静脉侵犯，术中诊断：胰腺肿瘤，遂决定行切除术\n    切除：实施切除。',title='手术记录')
        c=next(c for c in extract([s],CONFIG)[0] if '遂决定' in c['anchor']['quote'])
        self.assertEqual(c['decision_clock']['value'],'2022-01-02 10:00:00')
        self.assertIn('静脉侵犯',''.join(b['anchor']['quote'] for b in window([s],c)['blocks']))

    def test_postop_operative_restatement_is_historical_not_new_point(self):
        s=doc('2022-01-02 15:00\n术中探查静脉侵犯，遂决定行切除术。今日予营养支持。',title='术后首次病程记录')
        cc=extract([s],CONFIG)[0]
        old=next(c for c in cc if '遂决定' in c['anchor']['quote']);current=next(c for c in cc if '营养支持' in c['anchor']['quote'])
        self.assertTrue(old['historical_hint']);self.assertEqual(old['decision_clock']['precision'],'date')
        self.assertFalse(current['historical_hint']);self.assertEqual(current['decision_clock']['value'],'2022-01-02 15:00:00')

    def test_one_sentence_troponin_and_cta_keep_trigger(self):
        s=doc('2022-01-02 12:00\n此次入院肌钙蛋白2.224升高，拟完善冠脉CTA检查。')
        c=candidate(s);text=''.join(b['anchor']['quote'] for b in window([s],c)['blocks'])
        self.assertIn('肌钙蛋白2.224升高',text);self.assertNotIn('拟完善',text)

    def test_report_time_not_sample_time(self):
        bundle={'laboratory_original_L1_all':[dict(patient_uid='synthetic', source_record_id='l1',
            检验项目名称='肌钙蛋白',检验结果值='>2',送检时间='2022-01-02 10:00:00',报告时间='2022-01-02 13:00:00')]}
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'source.json';p.write_text(json.dumps(bundle,ensure_ascii=False));ss,_=load_sources(p,'synthetic')
        now=doc('2022-01-02 12:00\n拟完善CTA。');c=candidate(now)
        self.assertEqual(window([now]+ss,c)['source_ledger'][1]['disposition'],'EXCLUDE_FUTURE')

    def test_current_choice_cannot_be_new_evidence(self):
        s=doc('2022-01-02 12:00\n拟行手术。');c=candidate(s)
        c['semantic']=dict(scope='yes',role='decision',temporal_role='current')
        evo=dict(status='VALIDATED_QUOTES',current_judgment_supported=True,relation_to_previous='new_round',
                 evidence_evolution='new_relevant',supports=[c['anchor']],management_process_link='context_only',connection_supports=[])
        self.assertEqual(select_status(c,evo,c),'UNCERTAIN_NO_PRECHOICE_EVIDENCE_SUPPORT')

    def test_missing_connection_evidence_not_supported(self):
        s=doc('2022-01-02 12:00\n新MRI提示血管接触。拟行手术。');c=candidate(s)
        c['semantic']=dict(scope='yes',role='decision',temporal_role='current')
        prior=quote(s,0,c['anchor']['start'])
        evo=dict(status='VALIDATED_QUOTES',current_judgment_supported=True,relation_to_previous='new_round',
                 evidence_evolution='new_relevant',supports=[prior],management_process_link='supported',connection_supports=[])
        self.assertEqual(select_status(c,evo,c),'UNCERTAIN_CONNECTION_SUPPORT')

    def test_previous_choice_quote_is_not_new_evidence(self):
        old=doc('2022-01-01 12:00\n拟行手术。',identity='old'); pc=candidate(old)
        now=doc('2022-01-02 12:00\n拟继续手术。',identity='now'); cc=candidate(now)
        pc['semantic']=dict(supports=[pc['anchor']])
        cc['semantic']=dict(scope='yes',role='decision',temporal_role='current')
        evo=dict(status='VALIDATED_QUOTES',current_judgment_supported=True,relation_to_previous='new_round',
                 evidence_evolution='new_relevant',supports=[pc['anchor']],management_process_link='context_only',connection_supports=[])
        self.assertEqual(select_status(cc,evo,pc),'UNCERTAIN_NO_PRECHOICE_EVIDENCE_SUPPORT')

    def test_redaction_preserves_offsets_and_clinical_values(self):
        t='患者张三，男，电话13812345678。CA199 123.03U/ml，肌钙蛋白2.224。'
        m=mask_text(t,['张三']);self.assertEqual(len(m),len(t));self.assertNotIn('13812345678',m)
        self.assertIn('2.224',m);self.assertIn('123.03',m)

    def test_unprovided_or_invented_quote_rejected(self):
        s=doc('2022-01-02 12:00\n拟行手术。'); b={'X':{'anchor':quote(s,0,len(s['text']))}}
        with self.assertRaises(ValueError):exact_supports([dict(alias='Y',quote='拟行手术。')],b,{s['source_id']:s},[])
        with self.assertRaises(ValueError):exact_supports([dict(alias='X',quote='无禁忌症')],b,{s['source_id']:s},[])

    def test_model_normalization_request_is_one_time_context(self):
        sources=[doc('2022-01-01 12:00\n拟完善MRI。',identity='a'),doc('2022-01-02 12:00\n拟行手术。',identity='b')]
        cc=extract(sources,CONFIG)[0]
        class Fake:
            def __init__(self):self.seen=[]
            def ask(self,task,payload,schema):
                self.seen.append(payload);c=payload['candidates'][0]
                return {'items':[dict(candidate_id=c['candidate_id'],role='decision',scope='yes',management_question='评估',
                  action='检查',temporal_role='current',selection_reason_known=False,
                  supports=[dict(alias=c['alias'],quote=c['anchor'])],missing_information=[],rationale='原文计划')]}
        fake=Fake(); rr,ff=normalize(cc,sources,fake,CONFIG)
        self.assertEqual(len(rr),2);self.assertFalse(ff)
        self.assertTrue(all(len(p['candidates'])==1 for p in fake.seen))

    def test_evolution_uses_interval_and_cited_prior_state(self):
        ancient=doc('2020-01-01 12:00\n既往颈椎退变。',identity='old')
        prior=doc('2022-01-01 12:00\n拟完善MRI。',identity='previous')
        current=doc('2022-01-02 12:00\nMRI提示血管接触。拟行手术。',identity='current')
        pc=candidate(prior);cc=candidate(current)
        for c in [pc,cc]:c['semantic']=dict(management_question='根治路径评估',role='decision',supports=[c['anchor']])
        class Fake:
            def ask(self,task,payload,schema):
                self.payload=payload
                return dict(relation_to_previous='new_round',evidence_evolution='new_but_link_unclear',
                    management_process_link='context_only',same_task='yes',current_judgment_supported=True,
                    supports=[],connection_supports=[],missing_information=[],rationale='待核')
        fake=Fake();ss=[ancient,prior,current];analyze_evolution(cc,pc,window(ss,cc,pc),ss,fake,CONFIG)
        text=json.dumps(fake.payload,ensure_ascii=False)
        self.assertNotIn('颈椎退变',text);self.assertIn('拟完善MRI',text);self.assertIn('血管接触',text)

    def test_unordered_previous_not_fake_first_visible(self):
        c=candidate(doc('2022-01-02 12:00\n拟行手术。'))
        c.update(semantic=dict(scope='yes',role='decision',temporal_role='current'),previous_order_uncertain=True)
        self.assertEqual(select_status(c,{},None),'UNCERTAIN_PREVIOUS_ORDER')

    def test_large_interval_pages_keep_verbatim_evidence(self):
        old=doc('2022-01-01 12:00\n拟完善MRI。',identity='old')
        now=doc('2022-01-03 12:00\n拟行手术。',identity='now')
        pc=candidate(old);cc=candidate(now)
        for c in [pc,cc]:c['semantic']=dict(management_question='根治路径评估',role='decision',supports=[c['anchor']])
        ims=[image(text='未见明确转移。'+('有待核对的血管描述。'*30),at='2022-01-02 10:00:00',uid='im'+str(i),number=str(i)) for i in range(4)]
        class Fake:
            def ask(self,task,payload,schema):
                if task=='evidence_page_relevance':
                    return dict(selected_passages=[dict(alias=b['alias'],quote='未见明确转移。') for b in payload['sources'] if b['kind']=='imaging'],missing_information=[])
                b=next(b for b in payload['blocks'] if b['alias'].startswith('PAGE'))
                return dict(relation_to_previous='new_round',evidence_evolution='new_relevant',
                    management_process_link='context_only',same_task='yes',current_judgment_supported=True,
                    supports=[dict(alias=b['alias'],quote=b['source_text'])],connection_supports=[],missing_information=[],rationale='新增影像原句')
        # Image text has two equal fields; make the test quote unique per block.
        for s in ims:
            s['text']=s['text'].replace('[original_impression]\n未见明确转移。','[original_impression]\n结论待核。')
            import hashlib
            s['source_sha256']=hashlib.sha256(s['text'].encode()).hexdigest()
        # The explicit two-event/time descriptions add fixed prompt overhead.
        cfg=dict(CONFIG,max_window_characters=6000,evidence_page_characters=1500)
        ss=[old,now]+ims
        result,_=analyze_evolution(cc,pc,window(ss,cc,pc),ss,Fake(),cfg)
        self.assertEqual(result['status'],'VALIDATED_QUOTES');self.assertGreater(result['paging']['page_count'],1)
        self.assertEqual(result['supports'][0]['quote'],'未见明确转移。')

    def test_fhir_report_is_not_order_and_annotations_are_research(self):
        sources=[doc('2022-01-01 12:00\nMRI提示占位。'),image()]
        b=build(sources,'synthetic',[],[])
        types=[e['resource']['resourceType'] for e in b['entry']]
        self.assertIn('DocumentReference',types);self.assertIn('DiagnosticReport',types)
        self.assertNotIn('ServiceRequest',types)
        schema=ROOT/'private/p2_auto_checkpoint_v0.1_20261024/standards/fhir.schema.json'
        if schema.exists():self.assertTrue(validate(b,schema)['official_r4_json_schema'])

    def test_fhir_roundtrip_preserves_source_text_and_ignores_annotations(self):
        ss=[doc('2022-01-01 12:00\n拟完善MRI。'),image()]
        original=extract(ss,CONFIG)[0]
        b=build(ss,'synthetic',[dict(original[0],semantic={'supports':[original[0]['anchor']]})],[])
        restored,receipt=import_sources(b)
        self.assertEqual([(s['source_id'],s['text'],s['clock']) for s in ss],[(s['source_id'],s['text'],s['clock']) for s in restored])
        self.assertEqual(len(extract(restored,CONFIG)[0]),len(original))
        self.assertFalse(receipt['research_annotations_used_as_input'])

    def test_fhir_observation_roundtrip_preserves_raw_cells(self):
        raw={'laboratory_original_L1_all':[dict(patient_uid='synthetic',source_record_id='l1',检验项目名称='CA199',
                  检验结果值='>1200',检验结果单位='U/ml',送检时间='2022-01-01 10:00:00',报告时间='2022-01-01 11:00:00')]}
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'source.json';p.write_text(json.dumps(raw,ensure_ascii=False));ss,_=load_sources(p,'synthetic')
        b=build(ss,'synthetic',[],[]); restored,_=import_sources(b)
        self.assertEqual(ss[0]['text'],restored[0]['text']);self.assertEqual(ss[0]['clock'],restored[0]['clock'])
        self.assertEqual(next(e['resource'] for e in b['entry'] if e['resource']['resourceType']=='Observation')['valueString'],'>1200')

    def test_native_fhir_timezone_conversion_preserves_order(self):
        self.assertEqual(fhir_clock('2022-01-01T02:00:00Z')['value'],'2022-01-01 10:00:00')

    def test_fhir_annotation_preserves_fullwidth_whitespace_losslessly(self):
        s=doc('2022-01-01 12:00\n患者　　拟完善MRI。'); c=extract([s],CONFIG)[0][0]
        b=build([s],'synthetic',[dict(c,semantic={'supports':[c['anchor']]})],[])
        basic=next(e['resource'] for e in b['entry'] if e['resource']['resourceType']=='Basic')
        restored=json.loads(basic['extension'][0]['valueString'])
        self.assertEqual(restored['anchor']['quote'],c['anchor']['quote'])
        schema=ROOT/'private/p2_auto_checkpoint_v0.1_20261024/standards/fhir.schema.json'
        if schema.exists():self.assertTrue(validate(b,schema)['official_r4_json_schema'])

    def test_fhir_title_display_normalization_preserves_raw_title(self):
        s=doc('2022-01-01 12:00\n拟行手术。',title='　术前讨论')
        b=build([s],'synthetic',[],[]);r=next(e['resource'] for e in b['entry'] if e['resource']['resourceType']=='DocumentReference')
        self.assertEqual(r['type']['text'],' 术前讨论')
        restored,_=import_sources(b);self.assertEqual(restored[0]['title'],'　术前讨论')
        schema=ROOT/'private/p2_auto_checkpoint_v0.1_20261024/standards/fhir.schema.json'
        if schema.exists():self.assertTrue(validate(b,schema)['official_r4_json_schema'])


if __name__=='__main__':unittest.main()
