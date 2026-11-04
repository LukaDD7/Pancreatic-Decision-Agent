"""Fictional full-document/visibility counterexamples, never clinical labels."""
from copy import deepcopy
import hashlib
import unittest

from scripts.cohort_construction.p2_auto_checkpoint_v04.context import (
    build_units, context_for_discovery, build_evidence_context, time_order,
)
from scripts.cohort_construction.p2_pathway_replay.core import clock


def source(sid, text, when='2022-01-01 09:00', title='查房记录', kind='document', exam=None):
    return dict(source_id=sid, text=text, source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                title=title, kind=kind, clock=clock(when), clock_basis='body_record_time' if kind == 'document' else 'exam_time_proxy_release_unknown',
                clocks={'exam_time': clock(when) if kind == 'imaging' else clock(None), 'report_time': clock(None)},
                provenance={'exam_no_raw': exam})


def citation(s, quote):
    start=s['text'].index(quote)
    return dict(source_id=s['source_id'], source_sha256=s['source_sha256'], start=start, end=start+len(quote), quote=quote)


def event(s, fact=None, action=None, **extra):
    e=dict(source_id=s['source_id'], choice_time='2022-01-02 10:00', time_basis='explicit_documented_choice',
           prechoice_citations={'valid':[citation(s, fact)] if fact else [],'ambiguous':[]},
           action_citations={'valid':[citation(s, action)] if action else [],'ambiguous':[]})
    e.update(extra);return e


class ContextTests(unittest.TestCase):
    def test_CTX001_complete_document_not_sentence_candidates(self):
        current=source('D','一般情况可。拟行手术。风险较大。拟施术式：切除。',title='术前小结')
        units=build_units([current], {'topics':{}})
        self.assertEqual(len(units),1)
        self.assertEqual(units[0]['original_texts'][0]['text'],current['text'])
        self.assertEqual(units[0]['document_profile'],'preoperative_summary')
        self.assertEqual(units[0]['source_ids'],['D'])

    def test_CTX002_keep_specialty_documents_without_tumor_keywords(self):
        rows=[source(str(n),text,title=title) for n,(title,text) in enumerate([
            ('气管导管拔除记录','意识恢复，决定拔除。'),('输血前评估','Hb低，申请红细胞。'),
            ('会诊记录','甲状腺激素升高，先控制。'),('消化内镜检查申请单','申请目的：诊断性超声胃镜。'),
            ('不识别的临床标题','目前难以判断，需进一步评估。')])]
        self.assertEqual(len(build_units(rows)),5)

    def test_CTX003_support_forms_remain_sources_not_primary(self):
        forms=[source(str(n),'原始表单内容',title=title) for n,title in enumerate([
            '护理评估记录','术前知情同意书','伤病员使用物品清单','健康教育记录','Apache II评分'])]
        current=source('D','当前选择',when='2022-01-02 10:00')
        units=build_units(forms+[current]);self.assertEqual(len(units),1)
        context=context_for_discovery(units[0],forms+[current])
        self.assertEqual({s['source_id'] for s in context['prior_source_catalog']},{s['source_id'] for s in forms})
        self.assertEqual(context['prior_clinical_documents'],[])

    def test_CTX004_known_exam_versions_group_unknown_exam_not_merge(self):
        ss=[source('A','版本A',kind='imaging',title='MR',exam='1'),source('B','版本B',kind='imaging',title='MR',exam='1'),
            source('C','未知号版本C',kind='imaging',title='MR'),source('D','未知号版本D',kind='imaging',title='MR')]
        units=build_units(ss);self.assertEqual(len(units),3)
        grouped=next(x for x in units if len(x['source_ids'])==2)
        self.assertEqual(set(grouped['source_ids']),{'A','B'})
        self.assertIsNone(grouped['choice_time'])
        self.assertEqual(len(grouped['original_texts']),2)

    def test_CTX005_same_exam_different_clock_or_modality_not_merge(self):
        ss=[source('A','MR',kind='imaging',title='MR',exam='1'),source('B','CT',kind='imaging',title='CT',exam='1'),
            source('C','第二次MR',when='2022-01-02 09:00',kind='imaging',title='MR',exam='1')]
        self.assertEqual(len(build_units(ss)),3)

    def test_CTX006_discovery_uses_last_four_whole_prior_notes_no_future_body(self):
        ss=[source(str(n),f'完整原文{n}。甲。乙。',when=f'2022-01-0{n} 09:00') for n in range(1,7)]
        current=source('C','当前完整原文。',when='2022-01-06 10:00',title='术前小结')
        future=source('F','不得看到的以后结果',when='2022-01-07 09:00')
        unit=next(u for u in build_units(ss+[current,future]) if u['source_ids']==['C'])
        data=context_for_discovery(unit,ss+[current,future])
        self.assertEqual([s['source_id'] for s in data['prior_clinical_documents']],['6','5','4','3'])
        self.assertEqual(data['prior_clinical_documents'][0]['text'],ss[-1]['text'])
        self.assertNotIn('F',{s['source_id'] for s in data['prior_source_catalog']})
        self.assertNotIn('不得看到的以后结果',str(data))

    def test_CTX007_broader_surgical_background_from_earlier_full_note(self):
        old=source('OP','手 术 记 录\n手术时间：2022-01-01 09:00-2022-01-01 12:00\n探查后选择',title='特殊手术名')
        later=[source(str(n),'恢复状态',when=f'2022-01-0{n} 09:00') for n in range(2,7)]
        now=source('C','拟优化营养',when='2022-01-07 09:00')
        unit=next(u for u in build_units([old]+later+[now]) if u['source_ids']==['C'])
        data=context_for_discovery(unit,[old]+later+[now])
        self.assertEqual(data['background_clinical_documents'][0]['text'],old['text'])

    def test_CTX008_discovery_budget_omits_whole_source_records_receipt(self):
        prior=source('P','超长原始文书'*100)
        now=source('C','当前全文',when='2022-01-02 09:00')
        unit=next(u for u in build_units([prior,now]) if u['source_ids']==['C'])
        data=context_for_discovery(unit,[prior,now],budget=20)
        self.assertEqual(data['current_unit']['original_texts'][0]['text'],'当前全文')
        self.assertEqual(data['prior_clinical_documents'],[])
        self.assertEqual(data['source_ledger'][0]['disposition'],'omitted_budget')
        self.assertFalse(data['uncertainty']['context_coverage_complete'])

    def test_CTX009_precision_same_day_minute_not_ordered(self):
        self.assertEqual(time_order('2022-01-02','2022-01-02 10:00'),'SAME_DAY_UNORDERED')
        self.assertEqual(time_order('2022-01-02 10:00:30','2022-01-02 10:00'),'SAME_MINUTE_UNORDERED')
        self.assertEqual(time_order('2022-01-01','2022-01-02'),'BEFORE')

    def test_CTX010_only_current_valid_facts_no_action_or_result(self):
        current=source('C','神志清楚。决定拔管。拔管后平稳。',when='2022-01-02 10:00')
        result=build_evidence_context(event(current,'神志清楚。','决定拔管。'),[current])
        self.assertEqual([b['text'] for b in result['blocks']],['神志清楚。'])
        self.assertNotIn('决定拔管',str(result['blocks']))
        self.assertNotIn('拔管后平稳',str(result['blocks']))
        self.assertFalse(result['blind_replay_ready'])

    def test_CTX011_bad_secondary_fact_does_not_drop_valid_fact(self):
        current=source('C','神志清楚。决定拔管。')
        e=event(current,'神志清楚。','决定拔管。')
        e['prechoice_citations']['valid'].append(dict(source_id='C',start=999,end=1000,quote='错'))
        result=build_evidence_context(e,[current])
        self.assertEqual(result['blocks'][0]['text'],'神志清楚。')
        self.assertTrue(any(x['disposition']=='REVIEW_INVALID_PRECHOICE_SPAN' for x in result['ledger']))

    def test_CTX012_mixed_action_sentence_not_whole_evidence(self):
        current=source('C','胃液深红，给予处理。')
        e=event(current,current['text'],'给予处理。')
        result=build_evidence_context(e,[current])
        self.assertEqual(result['blocks'],[])
        self.assertTrue(any(x['disposition']=='REVIEW_PRECHOICE_OVERLAPS_CURRENT_ACTION' for x in result['ledger']))

    def test_CTX013_earlier_labs_imaging_and_notes_all_retained(self):
        rows=[source('OLD','旧基线',when='2021-12-01 09:00'),source('IMG','原始影像',kind='imaging'),source('LAB','原始检验',kind='lab')]
        current=source('C','新状态。决定处置。',when='2022-01-02 10:00')
        result=build_evidence_context(event(current,'新状态。','决定处置。'),rows+[current])
        self.assertEqual({b['source_id'] for b in result['blocks']},{'OLD','IMG','LAB','C'})
        self.assertEqual(next(b for b in result['blocks'] if b['source_id']=='OLD')['text'],'旧基线')

    def test_CTX014_future_same_day_unknown_quarantine_review(self):
        rows=[source('F','未来',when='2022-01-03 09:00'),source('S','同日但无法排序',when='2022-01-02'),
              source('Q','隔离检验',kind='quarantine'),source('P','病理时点未知',when=None,kind='pathology')]
        current=source('C','新状态。决定。',when='2022-01-02 10:00')
        result=build_evidence_context(event(current,'新状态。','决定。'),rows+[current])
        self.assertEqual({b['source_id'] for b in result['blocks']},{'C'})
        dispositions={x['source_id']:x['disposition'] for x in result['ledger']}
        self.assertEqual(dispositions['F'],'EXCLUDE_FUTURE')
        self.assertEqual([dispositions[x] for x in ['S','Q','P']],['REVIEW']*3)

    def test_CTX015_unknown_test_choice_excludes_all_variants_marks_potential_state(self):
        rows=[source('A','本次结果A',when='2022-01-02 11:00',kind='imaging',title='MRI',exam='E'),
              source('B','本次结果B',when='2022-01-02 11:00',kind='imaging',title='MRI',exam='E'),
              source('P','此前记载',when='2022-01-02 09:00')]
        e=event(rows[0],choice_time=None,time_basis='historical_unknown',kind='performed_test_report',same_test_source_ids=['A'])
        result=build_evidence_context(e,rows)
        self.assertEqual(result['same_test_source_ids'],['A','B'])
        self.assertEqual({b['source_id'] for b in result['blocks']},{'P'})
        self.assertEqual(result['blocks'][0]['interval'],'before_test_upper_bound_not_certified_prechoice')
        self.assertTrue(result['uncertainty']['performed_test_prechoice_state_uncertified'])
        self.assertFalse(result['cutoff_is_exact_choice_time'])

    def test_CTX016_operating_start_not_choice_minute_within_operation_review(self):
        current=source('C','手 术 记 录\n手术时间：2022-01-02 09:00-2022-01-02 12:00\n探查：静脉受侵。遂决定切除。执行切除。',when='2022-01-02 15:00',title='手术记录')
        rows=[source('BEFORE','术前资料',when='2022-01-02 08:00'),source('DURING','术中化验未明可见',when='2022-01-02 10:00'),source('AFTER','术后资料',when='2022-01-02 14:00')]
        e=event(current,'静脉受侵。','遂决定切除。',choice_time=None,time_basis='operative_phase',phase='探查后切除前')
        result=build_evidence_context(e,rows+[current])
        self.assertEqual({b['source_id'] for b in result['blocks']},{'C','BEFORE'})
        self.assertFalse(result['cutoff_is_exact_choice_time'])
        self.assertTrue(result['uncertainty']['operative_choice_minute_unknown'])
        self.assertIsNone(result['choice_time']['value'])
        row=next(x for x in result['ledger'] if x['source_id']=='DURING')
        self.assertEqual(row['reason'],'WITHIN_OPERATION_EXACT_CHOICE_TIME_UNKNOWN')

    def test_CTX017_explicit_upper_bound_beats_late_record_proxy(self):
        current=source('C','为加强监测而入ICU。',when='2022-01-02 17:56',title='转入首程')
        between=source('L','入室后的化验',when='2022-01-02 16:00',kind='lab')
        early=source('P','入室前状态',when='2022-01-02 14:00')
        e=event(current,choice_time=None,time_basis='record_time_proxy',time_upper_bound='2022-01-02 15:32')
        result=build_evidence_context(e,[current,between,early])
        self.assertEqual({b['source_id'] for b in result['blocks']},{'P'})
        self.assertEqual(result['cutoff']['value'],'2022-01-02 15:32:00')

    def test_CTX018_report_release_time_controls_not_exam_time(self):
        imaging=source('I','检查已完成但晚发报告',when='2022-01-02 08:00',kind='imaging')
        imaging['clocks']['report_time']=clock('2022-01-02 11:00')
        current=source('C','新状态。决定。',when='2022-01-02 10:00')
        result=build_evidence_context(event(current,'新状态。','决定。'),[imaging,current])
        self.assertNotIn('I',{b['source_id'] for b in result['blocks']})
        self.assertTrue(any(x['source_id']=='I' and x['disposition']=='EXCLUDE_FUTURE' for x in result['ledger']))

    def test_CTX019_budget_keeps_facts_then_recent_clinical_never_truncates(self):
        current=source('C','现状。决定。',when='2022-01-02 10:00')
        rows=[source('OLD','旧'*100,when='2021-12-01 09:00'),source('LAB','检验'*100,kind='lab'),source('P','最近临床',when='2022-01-02 09:00')]
        result=build_evidence_context(event(current,'现状。','决定。'),rows+[current],budget=12)
        self.assertEqual({b['source_id'] for b in result['blocks']},{'C','P'})
        self.assertEqual(result['budget']['omitted_source_count'],2)
        self.assertTrue(all(x['full_source_not_truncated'] for x in result['ledger'] if x['disposition']=='omitted_budget'))
        self.assertFalse(result['uncertainty']['source_coverage_complete'])

    def test_CTX020_ambiguous_current_fact_not_arbitrarily_first_span(self):
        current=source('C','稳定。稳定。决定。')
        e=event(current,action='决定。');e['prechoice_citations']['ambiguous']=[{'source_id':'C','quote':'稳定。','occurrences':[{'start':0,'end':3},{'start':3,'end':6}]}]
        result=build_evidence_context(e,[current]);self.assertEqual(result['blocks'],[])
        self.assertEqual(result['uncertainty']['prechoice_ambiguous_count'],1)

    def test_CTX021_conflicting_discussion_date_is_hint_not_repaired(self):
        s=source('C','2022-01-01 09:00\n讨论日期：2022年01月05日\n拟1月2日手术。',title='术前讨论')
        unit=build_units([s])[0]
        hint=unit['hints'][0];self.assertEqual(hint['body_discussion_date'],'2022-01-05')
        self.assertFalse(hint['resolved'])
        self.assertEqual(unit['original_texts'][0]['text'],s['text'])

    def test_CTX022_no_known_choice_time_no_false_certified_prior(self):
        current=source('C','很久前已检查，时点不明。',when='2022-01-02 10:00')
        old=source('P','记录早但未证明早于历史选择')
        result=build_evidence_context(event(current,choice_time=None,time_basis='historical_unknown'),[old,current])
        self.assertEqual(result['blocks'],[])
        self.assertIsNone(result['cutoff']['value'])
        self.assertFalse(result['uncertainty']['prechoice_state_certified'])

    def test_CTX023_lab_catalog_group_count_without_results_and_index_pointer(self):
        lab1=source('L1','检验值1234',kind='lab',title='某项检验')
        lab2=source('L2','检验值5678',kind='lab',title='某项检验')
        current=source('C','当前选择',when='2022-01-02 10:00')
        unit=next(u for u in build_units([lab1,lab2,current]) if u['source_ids']==['C'])
        unit['source_id_index_path']='test-case/source_fhir_r4_bundle.json#/entry'
        data=context_for_discovery(unit,[lab1,lab2,current])
        group=data['prior_source_catalog'][0]
        self.assertEqual(group['kind'],'lab_catalog_group')
        self.assertEqual(group['source_count'],2)
        self.assertFalse(group['results_included'])
        self.assertEqual(group['source_id_index_path'],unit['source_id_index_path'])
        self.assertNotIn('检验值1234',str(data))
        self.assertEqual(data['catalog_compression']['prior']['original_lab_source_count'],2)

    def test_CTX024_top_level_span_equals_source_anchor(self):
        current=source('C','新状态。决定。')
        result=build_evidence_context(event(current,'新状态。','决定。'),[current])
        block=result['blocks'][0]
        for key in ['source_id','start','end','quote']:
            self.assertEqual(block[key],block['anchor'][key])

    def test_CTX025_operating_upper_end_does_not_certify_intraop_report_before_choice(self):
        current=source('C','手 术 记 录\n手术时间：2022-01-02 09:00-2022-01-02 12:00\n探查：静脉受侵。遂决定切除。',when='2022-01-02 15:00',title='手术记录')
        during=source('D','术中检验',kind='lab',when='2022-01-02 10:00')
        e=event(current,'静脉受侵。','遂决定切除。',choice_time='2022-01-02 09:00',time_basis='operative_phase',time_upper_bound='2022-01-02 12:00')
        result=build_evidence_context(e,[current,during])
        self.assertNotIn('D',{b['source_id'] for b in result['blocks']})
        self.assertIsNone(result['choice_time']['value'])
        self.assertTrue(result['uncertainty']['supplied_operative_proxy_ignored'])
        self.assertTrue(any(x.get('reason')=='WITHIN_OPERATION_EXACT_CHOICE_TIME_UNKNOWN' for x in result['ledger']))


if __name__ == '__main__':
    unittest.main()
