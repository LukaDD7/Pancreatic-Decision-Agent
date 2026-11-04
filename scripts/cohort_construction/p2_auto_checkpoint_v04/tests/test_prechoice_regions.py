"""V04.1: whole prechoice context, independent of model-chosen highlights."""
import hashlib
import unittest
from scripts.cohort_construction.p2_auto_checkpoint.extract import clock
from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_evidence_context


def fixture(text,action,facts=(),post=(),phase=False):
    s=dict(source_id='D',kind='document',text=text,title='手术记录' if phase else '查房记录',
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),clock=clock('2022-01-01 15:00'),clocks={},clock_basis='body_record_time')
    def q(t):
        start=text.index(t);return dict(source_id='D',source_sha256=s['source_sha256'],start=start,end=start+len(t),quote=t)
    e=dict(source_id='D',source_ids=['D'],choice_time=None,time_basis='operative_phase' if phase else 'record_time_proxy',
        prechoice_citations={'valid':[q(t) for t in facts]},action_citations={'valid':[q(action)]},
        postchoice_citations={'valid':[q(t) for t in post]})
    return s,e


class RegionTests(unittest.TestCase):
    def test_REG001_complete_history_kept_even_if_not_in_selected_highlights(self):
        text='记录时间：2022-01-01 15:00\n既往经治疗病灶较前缩小。当前一般情况可。拟进一步检查排除禁忌。后来结果回报。'
        s,e=fixture(text,'拟进一步检查排除禁忌。',['当前一般情况可。'])
        c=build_evidence_context(e,[s],preserve_current_context=True)
        strings=[b['quote'] for b in c['blocks']]
        self.assertTrue(any('既往经治疗病灶较前缩小。当前一般情况可。' in t for t in strings))
        self.assertFalse(any('拟进一步检查' in t or '后来结果' in t for t in strings))

    def test_REG002_whole_exploration_keeps_negative_and_unselected_findings(self):
        text='手术记录\n实施手术名称：切除重建术\n手术时间：2022-01-01 12:00-2022-01-01 14:00\n探查：无腹水，肝表面无结节，门静脉受侵，术中诊断：恶性肿瘤，遂决定切除重建。切除：随后执行。'
        s,e=fixture(text,'遂决定切除重建。',['门静脉受侵，'],phase=True)
        c=build_evidence_context(e,[s],preserve_current_context=True)
        texts=[b['quote'] for b in c['blocks']]
        self.assertIn('探查：无腹水，肝表面无结节，门静脉受侵，',texts)
        self.assertFalse(any('实施手术名称' in t or '术中诊断' in t or '遂决定' in t or '随后执行' in t for t in texts))

    def test_REG003_semantic_fact_inside_broad_node_proof_is_not_action(self):
        text='患者血型特殊，需充分备血后手术。'
        s,e=fixture(text,text,['患者血型特殊'])
        c=build_evidence_context(e,[s],preserve_current_context=True)
        self.assertEqual([b['quote'] for b in c['blocks']],['患者血型特殊'])
        self.assertFalse(any('需充分备血' in b['quote'] for b in c['blocks']))

    def test_REG004_full_action_cannot_be_relabelled_as_fact(self):
        text='决定行手术。'
        s,e=fixture(text,text,[text])
        c=build_evidence_context(e,[s],preserve_current_context=True)
        self.assertEqual(c['blocks'],[])

    def test_REG005_explicit_postchoice_fact_removed_from_nonchronological_prefix(self):
        text='记录时间：2022-01-01 15:00\n术后疗效已回报。选择前患者腹痛。决定手术。'
        s,e=fixture(text,'决定手术。',['选择前患者腹痛。'],['术后疗效已回报。'])
        c=build_evidence_context(e,[s],preserve_current_context=True)
        self.assertFalse(any('术后疗效已回报。' in b['quote'] for b in c['blocks']))
        self.assertTrue(any('选择前患者腹痛。' in b['quote'] for b in c['blocks']))

    def test_REG006_report_selection_never_uses_own_result_region(self):
        s,e=fixture('报告：血管侵犯。','报告：血管侵犯。',['血管侵犯。'])
        s['kind']='imaging';s['clocks']['exam_time']=clock('2022-01-01 14:00')
        e.update(kind='performed_test_report',time_basis='historical_unknown',same_test_source_ids=['D'])
        c=build_evidence_context(e,[s],preserve_current_context=True)
        self.assertEqual(c['blocks'],[])


if __name__=='__main__':unittest.main()
