"""Source-bound regressions, no patient data or network calls."""
import hashlib
from copy import deepcopy

from scripts.cohort_construction.p2_auto_checkpoint.extract import clock
from scripts.cohort_construction.p2_auto_checkpoint_v04 import context, engine


def source(sid, text, when, title='查房记录'):
    return dict(source_id=sid,text=text,title=title,kind='document',clock=clock(when),
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),clocks={},provenance={})


def event(s, quote, eid='E', basis='record_time_proxy'):
    start=s['text'].index(quote)
    return dict(event_id=eid,source_id=s['source_id'],source_ids=[s['source_id']],
        unit_id='UNIT_'+s['source_id'],choice_time=None,time_basis=basis,
        source_document_clock=s['clock'],phase='当前评估',scope='yes',
        record_function='new_decision',management_question='切除路径管理',action=quote,
        action_citations=dict(valid=[dict(source_id=s['source_id'],start=start,end=start+len(quote),quote=quote)],
            ambiguous=[],invalid=[]))


def test_LINK001_terminal_punctuation_does_not_erase_connection():
    s=source('D','探查：静脉受侵，遂决定切除。\n切除后记录失血。','2022-01-02 18:00')
    e=event(s,'遂决定切除')
    allowed=engine._allowed_connection_ranges(e,{'allowed_spans':{}},[s])
    cites=engine.resolve_model_quotes([dict(source_id='D',quote='静脉受侵，遂决定切除。')],[s],[],allowed)
    assert engine.supported(cites)


def test_LINK001_subsequent_clinical_result_stays_forbidden():
    s=source('D','静脉受侵，遂决定切除。切除后未见出血。','2022-01-02 18:00')
    allowed=engine._allowed_connection_ranges(event(s,'遂决定切除'),{'allowed_spans':{}},[s])
    cites=engine.resolve_model_quotes([dict(source_id='D',quote=s['text'])],[s],[],allowed)
    assert not engine.supported(cites)
    assert cites['invalid'][0]['status']=='QUOTE_OUTSIDE_ALLOWED_SPANS'


def test_LINK001_whitespace_and_closing_quote_do_not_admit_next_words():
    s=source('D','根据探查“决定切除”。\n  术后转入ICU。','2022-01-02 18:00')
    allowed=engine._allowed_connection_ranges(event(s,'决定切除'),{'allowed_spans':{}},[s])
    good=engine.resolve_model_quotes([dict(source_id='D',quote='探查“决定切除”。')],[s],[],allowed)
    bad=engine.resolve_model_quotes([dict(source_id='D',quote='探查“决定切除”。\n  术后转入ICU。')],[s],[],allowed)
    assert engine.supported(good)
    assert not engine.supported(bad)


def test_LINK002_explicit_postchoice_fact_before_narrative_choice_is_not_connection_proof():
    s=source('D','术后病理提示腺癌。回述术中：探查未见转移，遂决定切除。','2022-01-02 18:00')
    e=event(s,'遂决定切除')
    quote='术后病理提示腺癌。'
    e['postchoice_citations']=dict(valid=[dict(source_id='D',start=0,end=len(quote),quote=quote)])
    allowed=engine._allowed_connection_ranges(e,{'allowed_spans':{}},[s])
    bad=engine.resolve_model_quotes([dict(source_id='D',quote=s['text'])],[s],[],allowed)
    good=engine.resolve_model_quotes([dict(source_id='D',quote='探查未见转移，遂决定切除。')],[s],[],allowed)
    assert not engine.supported(bad)
    assert engine.supported(good)


def operative_fixture(interval=True):
    header='手术记录\n'
    if interval:header+='手术时间：2022-01-02 12:00 - 2022-01-02 15:00\n'
    op=source('OP',header+'探查：未见转移。遂决定切除。','2022-01-02 18:13','手术记录')
    before=source('B','此前拟行探查，评估是否切除。','2022-01-02 09:00')
    during=source('I','监测检查结果已回报。','2022-01-02 13:00')
    after=source('A','术后转入ICU，继续监护。','2022-01-02 15:32','重症医学科转入记录')
    older=source('Y','入院后完善检查。','2022-01-01 10:00','入院记录')
    sources=[older,before,op,during,after]
    u=next(u for u in context.build_units(sources) if u['source_ids']==['OP'])
    return sources,u


def test_TIMELEDGER001_late_written_op_discovery_uses_source_operation_interval():
    sources,u=operative_fixture()
    ctx=context.context_for_discovery(u,sources)
    actual={s['source_id'] for s in ctx['prior_clinical_documents']+ctx['background_clinical_documents']}
    assert actual=={'Y','B'}
    assert ctx['cutoff']['value'].startswith('2022-01-02 12:00')
    assert not ctx['uncertainty']['prechoice_state_certified']
    assert u['clock']['value'].startswith('2022-01-02 18:13')


def test_TIMELEDGER001_late_written_op_review_never_offers_postop_or_intraop_comparator():
    sources,u=operative_fixture();byid={s['source_id']:s for s in sources}
    events=[event(s,s['text'],s['source_id']) for s in sources if s['source_id']!='OP']
    ledger=engine.earlier_ledger(u,events,byid)
    assert {e['event_id'] for e in ledger}=={'Y','B'}


def test_TIMELEDGER002_missing_operation_interval_does_not_invent_same_day_order():
    sources,u=operative_fixture(False)
    ctx=context.context_for_discovery(u,sources)
    assert {s['source_id'] for s in ctx['prior_clinical_documents']+ctx['background_clinical_documents']}=={'Y'}
    assert ctx['cutoff']['precision']=='date'
    assert {s['source_id'] for s in ctx['uncertain_time_source_catalog']} >= {'B','I','A'}


def test_TIMELEDGER002_nonoperative_context_and_ledger_keep_original_clock():
    sources,u=operative_fixture();u=deepcopy(u);u['document_profile']='round'
    ctx=context.context_for_discovery(u,sources)
    assert ctx['cutoff']==u['clock']
    assert 'comparison_boundary_basis' not in ctx
    assert {'I','A'} <= {s['source_id'] for s in ctx['prior_clinical_documents']+ctx['background_clinical_documents']}


def test_TIMELEDGER002_conflicting_operation_intervals_keep_order_unknown():
    _,u=operative_fixture()
    u['hints'].append(dict(kind='operation_interval_not_choice_time',start='2022-01-02 09:00',end='2022-01-02 11:00'))
    boundary,basis=context.unit_comparison_boundary(u)
    assert boundary['precision']=='date'
    assert basis=='conflicting_operation_intervals_day_only'
