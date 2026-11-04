from copy import deepcopy
import pytest

from scripts.cohort_construction.p2_checkpoint_packet.compiler import (
    ContractError,freeze_checkpoint_packet,text_sha256,validate_contract,
)
from scripts.cohort_construction.p2_checkpoint_materials import (
    MaterialPreparationError,prepare_material_review,
)


def source(sid='s1',text='评估甲。随后决定切除。术后稳定。'):
    return dict(source_id=sid,text=text,source_sha256=text_sha256(text))


def citation(s,text):
    start=s['text'].index(text)
    return dict(source_id=s['source_id'],source_sha256=s['source_sha256'],quote=text,
        start=start,end=start+len(text),status='VALID',raw_quote_verified=True,
        span_verified=True,eligible_as_source_support=True)


def event(s):
    q=citation(s,'随后决定切除。')
    return dict(event_id='candidate-e1',source_id=s['source_id'],source_ids=[s['source_id']],
        actual_model_evidence=[dict(source_id=s['source_id'],start=0,end=4,quote=s['text'][:4])],
        choice_boundary_citations={'valid':[q],'invalid':[],'ambiguous':[]},
        postchoice_citations={'valid':[],'invalid':[],'ambiguous':[]},
        validated_observation_citations={'valid':[]},availability_validation=[])


def prepare(e,s):
    return prepare_material_review(e,s,decision_task_id='task-explicit',visibility_policy_version='draft-visibility-1')


def known(e,s,quote='评估甲。',**updates):
    q=citation(s,quote);e['validated_observation_citations']['valid']=[q]
    e['availability_validation']=[dict(availability='explicitly_known_before',is_declared_observation=True,
        eligible_as_known_prechoice=True,validated_observation={'valid':[q]},validated_basis={'valid':[q]},**updates)]


def test_exact_whole_source_manifest_and_explicit_disjoint_partition():
    s=source();e=event(s);original=deepcopy((e,s))
    manifest,proposal,compiled,audit=prepare(e,[s,source('outside','未提供给当前事件的资料')])
    for name,value in [('SourceManifest',manifest),('SplitProposal',proposal),('CompiledPartition',compiled)]:validate_contract(name,value)
    assert manifest['sources'][0]['text']==s['text']
    assert ''.join(p['text'] for p in compiled['sources'][0]['segments'])==s['text']
    assert [p['disposition'] for p in compiled['sources'][0]['segments']]==['REVIEW','MASK','REVIEW']
    assert audit['excluded_source_ids']==['outside']
    assert audit['model_api_calls']==0 and audit['material_release_permitted'] is False
    assert compiled['semantic_audit_status']=='NOT_ASSESSED'
    assert (e,s)==original


def test_explicit_known_observation_is_hint_only_never_keep():
    s=source();e=event(s);known(e,s)
    _,_,compiled,audit=prepare(e,[s])
    assert audit['eligible_keep_hint_count']==1
    assert not any(p['disposition']=='KEEP' for p in compiled['sources'][0]['segments'])
    assert audit['unresolved_review_segment_count']>0


@pytest.mark.parametrize('availability',[None,'unknown','record_proxy_only','explicitly_known_after'])
def test_unknown_or_nonbefore_availability_never_eligible(availability):
    s=source();e=event(s);known(e,s);e['availability_validation'][0]['availability']=availability
    _,_,compiled,audit=prepare(e,[s])
    assert audit['eligible_keep_hint_count']==0
    assert audit['disposition_segment_counts'].get('KEEP',0)==0


def test_keep_mask_conflict_becomes_review_without_clipping_decision():
    s=source();e=event(s);known(e,s,'决定切除')
    _,_,compiled,audit=prepare(e,[s])
    conflicts=[d for d in audit['segment_decisions'] if d['reason']=='KEEP_MASK_HINT_CONFLICT']
    assert conflicts and all(d['disposition']=='REVIEW' for d in conflicts)
    assert compiled['mechanical_audit']['mutually_exclusive'] is True


@pytest.mark.parametrize('change',[{'source_sha256':'0'*64},{'start':-1},{'end':9999},{'start':True},{'quote':'伪造'}, {'span_verified':None}])
def test_invalid_mask_hint_quarantines_source_for_review(change):
    s=source();e=event(s);e['choice_boundary_citations']['valid'][0].update(change)
    _,_,compiled,audit=prepare(e,[s])
    assert audit['quarantined_source_ids']==['s1']
    assert all(p['disposition']=='REVIEW' for p in compiled['sources'][0]['segments'])
    assert audit['issues']


def test_missing_boundaries_leave_entire_source_review():
    s=source();e=event(s);e['choice_boundary_citations']=None
    _,_,compiled,audit=prepare(e,[s])
    assert [p['disposition'] for p in compiled['sources'][0]['segments']]==['REVIEW']
    assert audit['located_mask_hint_count']==0


def test_citation_cannot_expand_source_scope():
    s=source();other=source('other');e=event(s)
    e['choice_boundary_citations']['valid']=[citation(other,'随后决定切除。')]
    manifest,_,compiled,audit=prepare(e,[s,other])
    assert [x['source_id'] for x in manifest['sources']]==['s1']
    assert audit['located_mask_hint_count']==0
    assert any(h['rejection_code']=='CITATION_SOURCE_OUTSIDE_DECLARED_SCOPE' for h in audit['citation_hints'])


def test_evidence_source_scope_uses_full_text_not_only_model_block():
    s=source();other=source('evidence','先验观察。其他未审核字符。');e=event(s)
    e['actual_model_evidence'].append(dict(source_id='evidence',start=0,end=5,quote='先验观察。'))
    manifest,_,compiled,audit=prepare(e,[s,other])
    assert len(manifest['sources'])==2
    assert next(m for m in manifest['sources'] if m['source_id']=='evidence')['text']==other['text']
    assert next(m for m in compiled['sources'] if m['source_id']=='evidence')['segments'][0]['disposition']=='REVIEW'


@pytest.mark.parametrize('mode',['missing_source','missing_hash','wrong_hash','empty_text'])
def test_missing_or_unverifiable_original_rejected_as_processing_issue(mode):
    s=source();e=event(s);sources=[s]
    if mode=='missing_source':sources=[]
    elif mode=='missing_hash':s.pop('source_sha256')
    elif mode=='wrong_hash':s['source_sha256']='0'*64
    else:s['text']='';s['source_sha256']=text_sha256('')
    with pytest.raises(MaterialPreparationError) as error:prepare(e,sources)
    assert error.value.audit['preparation_status']=='REJECTED'
    assert all(q['attribution']=='processing_or_proposal_issue_not_patient_quality' for q in error.value.audit['issues'])


def test_forged_known_basis_not_counted_as_eligible():
    s=source();e=event(s);known(e,s)
    e['availability_validation'][0]['validated_basis']['valid'][0]=dict(e['availability_validation'][0]['validated_basis']['valid'][0],source_sha256='0'*64)
    _,_,_,audit=prepare(e,[s])
    assert audit['eligible_keep_hint_count']==0


def test_unicode_codepoints_and_crlf_preserved():
    s=source(text='🙂评估甲。\r\n随后决定切除。\r\n术后稳定。');e=event(s)
    manifest,_,compiled,_=prepare(e,[s])
    mask=next(p for p in compiled['sources'][0]['segments'] if p['disposition']=='MASK')
    assert mask['start']==s['text'].index('随后')
    assert manifest['sources'][0]['text']==s['text']
    assert compiled['sources'][0]['source_sha256']==text_sha256(s['text'])


def test_researcher_bridge_cannot_freeze_unreviewed_material():
    s=source();_,_,compiled,_=prepare(event(s),[s])
    with pytest.raises(ContractError,match='UNRESOLVED_REVIEW_SEGMENT'):
        freeze_checkpoint_packet(compiled,{}, {},packet_id='never',packet_version='0')
