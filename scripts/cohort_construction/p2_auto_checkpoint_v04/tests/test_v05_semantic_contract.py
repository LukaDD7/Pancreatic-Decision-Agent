"""Versioned source-proof controls; synthetic text only, no external calls."""
import copy
import tempfile
from pathlib import Path
from unittest.mock import Mock,patch

from scripts.cohort_construction.model_api import ModelAPISettings
from scripts.cohort_construction.p2_auto_checkpoint_v04 import engine,client
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_engine import source,event,review,quotes,select,empty_quotes


def fixture(kind='changed_state',availability='explicitly_known_before'):
    old=source('P','此前病灶稳定。拟继续观察。','2022-01-01 09:00')
    cur=source('C','复查提示病灶增大。因病灶增大，决定补充检查。','2022-01-02 09:00')
    def q(s,t):return {'source_id':s['source_id'],'quote':t}
    prev=event('E0',old,action='拟继续观察。',phase='术前',time_basis='record_time_proxy',prechoice_citations=quotes(old,'此前病灶稳定。'))
    e=event('E1',cur,action='决定补充检查。',phase='术前',time_basis='record_time_proxy',prechoice_citations=quotes(cur,'复查提示病灶增大。'),
        choice_boundary_citations=engine.resolve_model_quotes([q(cur,'决定补充检查。')],[cur],[]))
    obs=q(cur,'复查提示病灶增大。')
    r=review(observation_quotes=[obs],evidence_comparison={'previous_known_quotes':[q(old,'此前病灶稳定。')],
        'current_information_quotes':[obs],'change_kind':kind,'reinterpretation_quotes':[],'explanation':'前后比较'},
        observation_availability=[{'observation':obs,'availability':availability,
            'basis_quotes':[q(cur,'复查提示病灶增大。因病灶增大，决定补充检查。')],
            'explanation':'同文已知观察连接当前安排',
            'proof_channels':['semantic_prechoice'] if availability=='explicitly_known_before' else [],
            'semantic_order':'before' if availability=='explicitly_known_before' else ('after' if availability=='after_choice' else 'unknown'),
            'action_quote':q(cur,'决定补充检查。'),
            'connection_quote':q(cur,'复查提示病灶增大。因病灶增大，决定补充检查。'),
            'proof_explanation':'纯虚构：分开证据子句、动作子句和连接原文。'}],evidence_connection='supported',
        checkpoint_kind='evidence_informed_choice',evidence_relations=[],
        proof_event_role='evidence_informed_management_choice',
        temporal_channel_status='unresolved',
        semantic_channel_status='explicit_before_choice' if availability=='explicitly_known_before' else 'unresolved',
        key_management_predecessor_rationale='纯虚构相关前轮。')
    ctx=engine.build_evidence_context(e,[old,cur],[prev,e],preserve_current_context=True)
    return old,cur,prev,e,r,ctx


def validate(f):
    old,cur,prev,e,r,ctx=f
    return engine.validate_semantic_contract(e,r,ctx,[old,cur],[prev,e],[])


def test_SEMNEW001_old_fact_repeated_not_new_because_new_rationale():
    f=fixture(kind='repeated');v,a,reasons=validate(f)
    e,r=f[3:5];e['semantic_contract_validation']=reasons
    status,reasons=select(e=e,r=r)
    assert status.startswith('UNCERTAIN')
    assert 'COMPARISON_DOES_NOT_ESTABLISH_NEW_INFORMATION' in reasons


def test_SEMNEW001_unchanged_action_does_not_veto_new_measurement():
    f=fixture(kind='new_measurement');v,a,reasons=validate(f)
    e,r=f[3:5];e['action']='继续原方案';e['semantic_contract_validation']=reasons
    status,reasons=select(e=e,r=r)
    assert status=='CANDIDATE_NEW_EVIDENCE' and reasons==[]


def test_SEMNEW001_reinterpretation_requires_explicit_source():
    f=fixture(kind='explicit_reinterpretation');v,a,reasons=validate(f)
    assert 'EXPLICIT_REINTERPRETATION_HAS_NO_SOURCE_PROOF' in reasons


def test_SEMNEW001_current_fact_cannot_be_previous_known():
    f=fixture();f[4]['evidence_comparison']['previous_known_quotes']=[{'source_id':'C','quote':'复查提示病灶增大。'}]
    v,a,reasons=validate(f)
    assert 'PREVIOUS_KNOWLEDGE_PROOF_MISSING_OR_OUTSIDE_PRIOR_ROUND' in reasons


def test_AVAIL001_earlier_report_clock_not_proof_of_reading():
    v,a,reasons=validate(fixture(availability='record_proxy_only'))
    assert 'NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED' in reasons
    assert not v['known_current_information_supported']


def test_AVAIL001_other_old_observation_does_not_certify_new_fact():
    f=fixture();r=f[4]
    r['observation_availability'][0]['observation']={'source_id':'P','quote':'此前病灶稳定。'}
    v,a,reasons=validate(f)
    assert 'NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED' in reasons


def test_AVAIL001_invented_basis_does_not_certify_known():
    f=fixture();f[4]['observation_availability'][0]['basis_quotes']=[{'source_id':'C','quote':'医生明确阅读报告（原文无此句）'}]
    v,a,reasons=validate(f)
    assert not v['known_current_information_supported']


def test_MODEL001_configured_model_changes_real_body_and_cache_key():
    cfg={'model_temperature':0,'max_output_tokens':32,'semantic_model':'qwen3.8-max'}
    settings=ModelAPISettings('secret','https://example.invalid/v1','old-model')
    response=Mock();response.json.return_value={'model':'qwen3.8-max','choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}
    session=Mock();session.post.return_value=response
    with tempfile.TemporaryDirectory() as directory,patch.object(client.requests,'Session',return_value=session):
        if hasattr(client._thread,'session'):del client._thread.session
        c=client.Client(Path(directory),cfg,settings)
        _,receipt=c.ask('synthetic',{}, {'type':'object'})
        assert session.post.call_args.kwargs['json']['model']=='qwen3.8-max'
        assert not session.post.call_args.kwargs['allow_redirects']
        assert receipt['model']=='qwen3.8-max'
        assert settings.model=='old-model'
        del client._thread.session


def test_AVAIL002_old_known_fact_cannot_certify_unknown_new_fact():
    f=fixture();old,cur,prev,e,r,ctx=f
    old_q={'source_id':'P','quote':'此前病灶稳定。'}
    new_q={'source_id':'C','quote':'复查提示病灶增大。'}
    r['evidence_comparison']['current_information_quotes']=[old_q,new_q]
    r['observation_quotes']=[old_q,new_q]
    r['observation_availability']=[
        {'observation':old_q,'availability':'explicitly_known_before','basis_quotes':[old_q],'explanation':'旧状态确实已知',
         'proof_channels':['semantic_prechoice'],'semantic_order':'before','action_quote':{'source_id':'C','quote':'决定补充检查。'},
         'connection_quote':{'source_id':'C','quote':'复查提示病灶增大。因病灶增大，决定补充检查。'},'proof_explanation':'纯虚构旧事实'},
        {'observation':new_q,'availability':'uncertain','basis_quotes':[],'explanation':'新结果何时获得不明',
         'proof_channels':[],'semantic_order':'unknown','action_quote':{'source_id':'C','quote':'决定补充检查。'},
         'connection_quote':None,'proof_explanation':'纯虚构新事实未决'}]
    v,a,reasons=validate(f)
    assert not v['known_current_information_supported']
    assert 'NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED' in reasons


def test_AVAIL003_partial_overlap_does_not_certify_entire_new_fact():
    f=fixture();r=f[4]
    short={'source_id':'C','quote':'病灶增大'}
    r['observation_quotes']=[short]
    r['observation_availability'][0]['observation']=short
    v,a,reasons=validate(f)
    assert not v['known_current_information_supported']


def test_UNCERT001_program_does_not_relabel_model_proof_gap_as_missing_source():
    f=fixture();e=f[3];r=f[4]
    from scripts.cohort_construction.p2_auto_checkpoint_v04.prompts import UNCERTAINTY_KEYS
    r['uncertainty_dimensions']={key:dict(status='uncertain',reason_category='processing_gap',
        explanation='原文有线索但输入覆盖不足',source_quotes=[]) for key in UNCERTAINTY_KEYS}
    raw,final,c=engine.dimension_audit(e,r,[f[0],f[1]],[],['NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED'])
    assert final['choice_time']['reason_category']=='processing_gap'
    assert final['source_availability']['reason_category']=='processing_gap'
    assert raw==final
