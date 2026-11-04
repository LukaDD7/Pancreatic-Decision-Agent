from copy import deepcopy
import pytest

from scripts.cohort_construction.p2_auto_checkpoint.extract import write
from scripts.cohort_construction.p2_auto_checkpoint_v06.experiments.summarize import (
    without_graph_and_grouping, transport_statistics, release,technically_complete,
)


def test_replay_restores_graph_input_and_preserves_model_proposal():
    events=[dict(event_id='synthetic',checkpoint_status='GROUPED_SAME_EVENT_MENTION',
        status_before_semantic_identity_grouping='UNCERTAIN_FINAL_GRAPH',
        semantic_review={'checkpoint':'new_evidence'},validation_reasons=['original','added'],
        episode_id='derived',canonical_event_id='derived',
        final_graph_audit={'before':{'checkpoint_status':'CANDIDATE_NEW_EVIDENCE','validation_reasons':['original']}})]
    original=deepcopy(events)
    restored=without_graph_and_grouping(events)
    assert restored[0]['checkpoint_status']=='CANDIDATE_NEW_EVIDENCE'
    assert restored[0]['validation_reasons']==['original']
    assert restored[0]['semantic_review']==events[0]['semantic_review']
    assert not {'episode_id','canonical_event_id','final_graph_audit'}&restored[0].keys()
    assert events==original


def test_transport_stats_excludes_frozen_cached_invocations(tmp_path):
    folder=tmp_path/'model_cache/invocations';folder.mkdir(parents=True)
    write(tmp_path/'protected_old_hashes.json',{'/frozen/model_cache/invocations/old.json':'irrelevant'})
    write(folder/'old.json',dict(invocation_id='old',status='SUCCESS',task='task',attempts=[{'status':'SUCCESS'}]))
    write(folder/'new.json',dict(invocation_id='new',status='FAILED',task='task',attempts=[{'status':'FAILED','error':'SSLError'}]))
    stats=transport_statistics(tmp_path)
    assert stats['new_logical_invocations']==1
    assert stats['transport_attempts']==1
    assert stats['failure_categories']=={'SSLError':1}


def test_incomplete_ten_cannot_be_released(tmp_path):
    (tmp_path/'model_cache/invocations').mkdir(parents=True)
    write(tmp_path/'protected_old_hashes.json',{})
    with pytest.raises(ValueError,match='CANNOT_RELEASE_TECHNICALLY_INCOMPLETE_TEN_CASES'):
        release(tmp_path)
    assert not (tmp_path/'fixed_ten_development_release').exists()


def test_intermediate_engine_summary_is_not_a_completed_fhir_case(tmp_path):
    path=tmp_path/'summary.json';write(path,{'failures':0})
    assert not technically_complete(path)
    write(path,{'failures':0,'fhir_validation':{'official_r4_json_schema':True}})
    assert technically_complete(path)
    write(tmp_path/'failure.json',{'error':'synthetic'})
    assert not technically_complete(path)
