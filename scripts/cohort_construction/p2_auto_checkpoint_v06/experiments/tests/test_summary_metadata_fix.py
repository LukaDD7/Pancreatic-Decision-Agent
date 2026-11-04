from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import pytest

from scripts.cohort_construction.p2_auto_checkpoint.extract import read,write
from scripts.cohort_construction.p2_auto_checkpoint_v06.experiments.summarize import (
    assert_summary_consistent,derive_case_summary,repair_summary_release,
)


def objects():
    events=[dict(event_id='e'+str(i),checkpoint_status='CANDIDATE_NEW_EVIDENCE') for i in range(5)]
    events += [dict(event_id='a'+str(i),checkpoint_status='CANDIDATE_FIRST_ANCHOR') for i in range(6)]
    events += [dict(event_id='u',checkpoint_status='UNCERTAIN_FINAL_GRAPH')]
    return events,[{'episode_id':'episode'}],[{'relation':'synthetic'}]


def test_stale_distribution_reproduces_scale09_summary_bug():
    events,episodes,relations=objects()
    stale=dict(checkpoint_statuses={'CANDIDATE_NEW_EVIDENCE':6,'CANDIDATE_FIRST_ANCHOR':6},
        candidate_checkpoints=11,candidate_episodes=1,reviewed_events=12,process_links=1)
    with pytest.raises(ValueError,match='FINAL_SUMMARY_OBJECT_MISMATCH:checkpoint_statuses'):
        assert_summary_consistent(stale,events,episodes,relations)
    revised=derive_case_summary(stale,events,episodes,relations)
    assert revised['checkpoint_statuses']=={'CANDIDATE_NEW_EVIDENCE':5,'CANDIDATE_FIRST_ANCHOR':6,'UNCERTAIN_FINAL_GRAPH':1}
    assert revised['candidate_checkpoints']==11
    assert assert_summary_consistent(revised,events,episodes,relations)['passed']


def test_all_derived_counts_recomputed_with_correct_summary_control():
    events,episodes,relations=objects();original=deepcopy(events)
    summary=derive_case_summary({'case_id':'synthetic','discovered_events':999,'model_calls':999,'cached_calls':999,'failures':999},
        events,episodes,relations,discovery={'events':events},model_receipts=[{'cached':True},{'cached':False}],failures=[])
    assert summary['case_id']=='synthetic' and summary['discovered_events']==12
    assert summary['reviewed_events']==12 and summary['candidate_episodes']==1 and summary['process_links']==1
    assert summary['model_calls']==2 and summary['cached_calls']==1 and summary['failures']==0
    assert derive_case_summary(summary,events,episodes,relations)==summary
    assert events==original


def test_v2_summary_repair_preserves_v1_and_links_unchanged_objects(tmp_path):
    old=tmp_path/'fixed_ten_development_release';old.mkdir()
    for i in range(1,11):
        case=old/f'SCALE-{i:02d}';case.mkdir()
        events=[dict(event_id='candidate',checkpoint_status='CANDIDATE_FIRST_ANCHOR'),dict(event_id='u',checkpoint_status='UNCERTAIN_FINAL_GRAPH')]
        for name,value in [('decision_events',events),('episodes',[{'episode_id':'e'}]),('relations',[]),
                           ('discovery',{'events':events}),('model_receipts',[]),('failures',[])]:write(case/(name+'.json'),value)
        write(case/'summary.json',dict(checkpoint_statuses={'CANDIDATE_FIRST_ANCHOR':2},candidate_checkpoints=1,candidate_episodes=1))
    write(old/'release_receipt.json',dict(candidate_checkpoints=10,candidate_episodes=10))
    before={str(p):p.read_bytes() for p in old.rglob('*') if p.is_file()}
    with patch('socket.socket',side_effect=AssertionError('NO_NETWORK')) as sockets:
        receipt=repair_summary_release(tmp_path)
    assert sockets.call_count==0 and receipt['model_api_calls']==0
    assert receipt['candidate_checkpoints']==10 and receipt['candidate_episodes']==10
    assert all(Path(p).read_bytes()==v for p,v in before.items())
    new=tmp_path/'fixed_ten_development_release_v2'
    assert (new/'SCALE-09/decision_events.json').is_symlink()
    assert not (new/'SCALE-09/summary.json').is_symlink()
    assert read(new/'SCALE-09/summary.json')['checkpoint_statuses']=={'CANDIDATE_FIRST_ANCHOR':1,'UNCERTAIN_FINAL_GRAPH':1}
    with pytest.raises(FileExistsError):repair_summary_release(tmp_path)
