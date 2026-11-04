"""V04-PHASE001: pre/postoperative are not intraoperative (no API/data)."""
import hashlib

import pytest

from scripts.cohort_construction.p2_auto_checkpoint.extract import clock
from scripts.cohort_construction.p2_auto_checkpoint_v04 import context,temporal
from scripts.cohort_construction.p2_auto_checkpoint_v04.validation import resolve_citations


def source(sid,text,when,title='查房记录'):
    return dict(source_id=sid,text=text,clock=clock(when),kind='document',title=title,
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),clocks={},provenance={})


@pytest.mark.parametrize('phase',[
    'preoperative_assessment','postoperative_management','perioperative_management',
    'pre-operative surgical readiness','post-operative recovery',
    'preoperative_exploration_plan','术前计划探查','术后第1天低血糖处置','围手术期管理',
])
def test_PHASE001_pre_post_perioperative_labels_do_not_mean_intraoperative(phase):
    assert not context.is_operative_phase(dict(time_basis='record_time_proxy',phase=phase))


@pytest.mark.parametrize('phase',['intraoperative_decision','operative phase','after_exploration','术中探查后'])
def test_PHASE001_actual_exploration_phase_remains_recognized(phase):
    assert context.is_operative_phase(dict(time_basis='historical_unknown',phase=phase))


def test_PHASE001_explicit_model_operative_basis_is_not_vetoed_by_phase_wording():
    assert context.is_operative_phase(dict(time_basis='operative_phase',phase='postoperative note recounts exploration'))


@pytest.mark.parametrize('phase',['preoperative_assessment','postoperative_management'])
def test_PHASE002_full_ordinary_context_and_earlier_originals_not_erased_by_phase_substring(phase):
    earlier=source('P','此前检查提示需要进一步评估。','2022-01-01 10:00')
    current=source('C','当前状态：指标异常。拟完善评估后处理。','2022-01-02 09:00')
    e=dict(event_id='E',source_id='C',source_ids=['C'],time_basis='record_time_proxy',
        phase=phase,choice_time=None,action_citations=resolve_citations([
            dict(source_id='C',quote='拟完善评估后处理。')],[current]),
        prechoice_citations=dict(valid=[],ambiguous=[],invalid=[]),postchoice_citations=dict(valid=[],ambiguous=[],invalid=[]))
    ctx=context.build_evidence_context(e,[earlier,current],preserve_current_context=True)
    assert ctx['cutoff']==current['clock']
    assert not ctx['uncertainty']['operative_choice_minute_unknown']
    blocks=ctx['blocks']
    assert any(b['source_id']=='P' and b['quote']==earlier['text'] for b in blocks)
    assert any(b['source_id']=='C' and '当前状态：指标异常。' in b['quote'] for b in blocks)
    assert not any(r['disposition']=='REVIEW_NO_STRUCTURAL_EXPLORATION_BOUNDARY' for r in ctx['ledger'])


@pytest.mark.parametrize('phase',['preoperative_assessment','postoperative_management'])
def test_PHASE003_rejected_explicit_time_falls_back_to_record_proxy_not_operative(phase):
    s=source('C','记录时间：2022-01-02 09:00。拟完善评估。','2022-01-02 09:00')
    e=dict(source_id='C',source_ids=['C'],document_profile='round',phase=phase,
        time_basis='explicit_documented_choice',choice_time='2022-01-02 09:00')
    checked=temporal.verify_choice_time(e,[s],dict(valid=[],ambiguous=[],invalid=[]))
    assert checked['choice_time'] is None
    assert checked['time_basis']=='record_time_proxy'
