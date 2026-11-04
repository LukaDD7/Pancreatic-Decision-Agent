"""End-to-end boundary counterexamples found before the first clinical run."""
import hashlib
import unittest
from copy import deepcopy

from scripts.cohort_construction.p2_auto_checkpoint.extract import clock
from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_units
from scripts.cohort_construction.p2_auto_checkpoint_v04.engine import discover_unit,assemble_processes


def source(text):
    return dict(source_id='D',text=text,title='临床决策记录',kind='document',
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),clock=clock('2021-11-30 15:32'),
        clocks={},clock_basis='body_record_time',provenance={})


class Fake:
    def __init__(self,result):self.result=result
    def ask(self,*args):return deepcopy(self.result),{'request_sha256':'test'}


def proposal(basis,time,text):
    return dict(events=[dict(local_id='1',scope='yes',record_function='new_decision',
        management_question='根治切除路径',action='决定行切除',
        action_quotes=[{'source_id':'D','quote':'决定行切除。'}],prechoice_facts=[],postchoice_facts=[],
        choice_time=time,time_basis=basis,time_quotes=[{'source_id':'D','quote':text}],
        phase='术中探查后' if basis=='operative_phase' else '当前评估',uncertainty=[],rationale='source')],rejected_mentions=[])


class ReleaseCounterexamples(unittest.TestCase):
    def test_REL001_adapter_preserves_operative_model_time_proposal_but_not_actual_minute(self):
        text='记录时间：2021-11-30 15:32\n探查未见转移，决定行切除。'
        sources=[source(text)];unit=build_units(sources)[0]
        event=discover_unit(unit,sources,{'discovery_context_characters':10000},
            Fake(proposal('operative_phase','2021-11-30 12:05',text)),[])['events'][0]
        self.assertIsNone(event['choice_time'])
        self.assertEqual(event['model_time_proposal']['choice_time'],'2021-11-30 12:05')
        self.assertEqual(event['time_validation']['original_proposal']['choice_time'],'2021-11-30 12:05')

    def test_REL002_chinese_explicit_choice_time_reaches_temporal_validator(self):
        text='于2021年11月30日12时05分决定行切除。'
        sources=[source(text)];unit=build_units(sources)[0]
        event=discover_unit(unit,sources,{'discovery_context_characters':10000},
            Fake(proposal('explicit_documented_choice','2021年11月30日12时05分',text)),[])['events'][0]
        self.assertEqual(event['choice_time'],'2021-11-30 12:05')
        self.assertTrue(event['time_validation']['choice_time_verified'])

    def test_REL003_unverified_action_endpoint_cannot_support_identity_edge(self):
        citation={'valid':[{'source_id':'D','start':0,'end':1,'quote':'同'}],'ambiguous':[],'invalid':[]}
        def event(eid,supported):
            return dict(event_id=eid,scope='yes',record_function='new_decision',
                management_question='手术管理',checkpoint_status='CANDIDATE_FIRST_ANCHOR',
                discovery_source_supported=supported,validated_connection_citations=deepcopy(citation),
                semantic_review={'same_event_with':[],'same_process_previous_id':None,'process_link':'supported'})
        a,b=event('A',True),event('B',False)
        b['semantic_review']['same_event_with']=['A']
        b['validation_reasons']=['CURRENT_ACTION_SOURCE_UNVERIFIED']
        _,links=assemble_processes([a,b])
        self.assertEqual(links[0]['status'],'uncertain')


if __name__=='__main__':unittest.main()
