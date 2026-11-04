"""Summarize transport/completeness and isolate final-graph changes offline."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import shutil

from scripts.cohort_construction.p2_auto_checkpoint.extract import read, write
from scripts.cohort_construction.p2_auto_checkpoint_v06.offline import compare_case
from .fixed_model import DEFAULT, OLD, CASES, sha, verify


def transport_statistics(destination):
    destination=Path(destination)
    protected=read(destination/'protected_old_hashes.json')
    old_invocations={Path(p).name for p in protected if '/model_cache/invocations/' in p}
    calls=[read(p) for p in (destination/'model_cache/invocations').glob('*.json')
           if p.name not in old_invocations]
    return dict(new_logical_invocations=len(calls),
        invocation_statuses=dict(Counter(c['status'] for c in calls)),
        invocations_by_task=dict(Counter(c['task'] for c in calls)),
        transport_attempts=sum(len(c.get('attempts',[])) for c in calls),
        transport_attempt_statuses=dict(Counter(a['status'] for c in calls for a in c.get('attempts',[]))),
        failure_categories=dict(Counter(a['error'] for c in calls for a in c.get('attempts',[]) if a['status']=='FAILED')),
        receipt_files=[str(destination/'model_cache/invocations'/(c['invocation_id']+'.json')) for c in calls])


def baseline_cases():
    completed={}
    for path in sorted(OLD.glob('attempts/*/SCALE-*/summary.json')):
        summary=read(path)
        if summary.get('failures')==0:completed[summary['case_id']]=path.parent
    return completed


def technically_complete(path):
    path=Path(path);summary=read(path)
    # engine writes an intermediate summary before official FHIR validation;
    # that intermediate state must never be treated as a completed case.
    return (summary.get('failures')==0 and not (path.parent/'failure.json').exists()
            and summary.get('fhir_validation',{}).get('official_r4_json_schema') is True)


def without_graph_and_grouping(events):
    result=deepcopy(events)
    for event in result:
        if event.get('checkpoint_status')=='GROUPED_SAME_EVENT_MENTION':
            event['checkpoint_status']=event['status_before_semantic_identity_grouping']
        audit=event.pop('final_graph_audit',None)
        if audit:
            event.update(deepcopy(audit['before']))
        for field in ('canonical_event_id','episode_id','status_before_semantic_identity_grouping'):
            event.pop(field,None)
    return result


def derive_case_summary(previous,events,episodes,relations,*,discovery=None,model_receipts=None,failures=None,graph_audit=None):
    """CE-EXP-003: all event/assembly-derived metadata comes from final objects.

    A prior summary is a provenance source for settings and frozen input data;
    its pre-audit distributions must never describe audited event objects.
    """
    summary=deepcopy(previous)
    summary.update(checkpoint_statuses=dict(sorted(Counter(e['checkpoint_status'] for e in events).items())),
        reviewed_events=len(events),candidate_checkpoints=sum(e['checkpoint_status'].startswith('CANDIDATE_') for e in events),
        candidate_episodes=len(episodes),process_links=len(relations))
    if discovery is not None:summary['discovered_events']=len(discovery['events'])
    if model_receipts is not None:
        summary.update(model_calls=len(model_receipts),cached_calls=sum(r.get('cached') is True for r in model_receipts))
    if failures is not None:summary['failures']=len(failures)
    if graph_audit is not None:
        if graph_audit['event_count']!=len(events):raise ValueError('FINAL_GRAPH_AUDIT_EVENT_COUNT_MISMATCH')
        summary['final_graph_audit']={k:graph_audit[k] for k in ('version','event_count','demoted_checkpoint_count','blocked_relation_count')}
    return summary


def case_summary_from_directory(directory,previous=None):
    directory=Path(directory)
    previous=read(directory/'summary.json') if previous is None else previous
    values={name:read(directory/(name+'.json')) for name in ('decision_events','episodes','relations','discovery','model_receipts','failures')}
    graph=read(directory/'final_graph_audit.json') if (directory/'final_graph_audit.json').exists() else None
    return derive_case_summary(previous,values['decision_events'],values['episodes'],values['relations'],
        discovery=values['discovery'],model_receipts=values['model_receipts'],failures=values['failures'],graph_audit=graph)


def assert_summary_consistent(summary,events,episodes,relations):
    expected=derive_case_summary({},events,episodes,relations)
    fields=('checkpoint_statuses','reviewed_events','candidate_checkpoints','candidate_episodes','process_links')
    mismatches=[field for field in fields if summary.get(field)!=expected[field]]
    if mismatches:raise ValueError('FINAL_SUMMARY_OBJECT_MISMATCH:'+','.join(mismatches))
    return dict(passed=True,event_count=len(events),checkpoint_status_total=sum(summary['checkpoint_statuses'].values()),
        candidate_checkpoints=summary['candidate_checkpoints'],candidate_episodes=summary['candidate_episodes'],process_links=summary['process_links'])


def summarize(destination=DEFAULT):
    destination=Path(destination)
    summaries=[];case_paths={};completed_ids=[]
    runs=[destination/'technical_completion_run']+sorted(destination.glob('technical_completion_retry_*'))
    for cid in CASES:
        candidates=[run/cid/'summary.json' for run in runs if (run/cid/'summary.json').exists()]
        if candidates:
            complete=[p for p in candidates if technically_complete(p)]
            path=(complete or candidates)[-1]
            summaries.append(read(path));case_paths[cid]=str(path.parent)
            if complete:completed_ids.append(cid)
    status=dict(new_model='deepseek-v4.1-flash',old_model='deepseek-v4.1-flash',
        same_temperature=True,source_expansion_enabled=False,semantic_feedback_enabled_in_completion=False,
        requested_completion_cases=list(CASES),completed_cases=completed_ids,
        remaining_cases=[cid for cid in CASES if cid not in completed_ids],
        technical_summaries=summaries,case_output_paths=case_paths,transport=transport_statistics(destination),
        clinical_reference_read=False,clinical_accuracy_measured=False,improvement_proven=False)
    write(destination/'model_validation_status.json',status)
    return status


def release(destination=DEFAULT):
    destination=Path(destination);status=summarize(destination)
    if status['remaining_cases']:
        raise ValueError('CANNOT_RELEASE_TECHNICALLY_INCOMPLETE_TEN_CASES')
    out=destination/'fixed_ten_development_release';out.mkdir(exist_ok=False)
    completed=baseline_cases();source_paths={}
    rows=[]
    for cid,case_dir in sorted(completed.items()):
        case_out=out/cid;case_out.mkdir()
        original=read(case_dir/'decision_events.json')
        comparison=compare_case(original,read(case_dir/'episodes.json'),read(case_dir/'relations.json'))
        for name,value in [('decision_events',comparison['strict_events']),
                           ('episodes',comparison['strict_episodes']),('relations',comparison['strict_relations']),
                           ('final_graph_audit',comparison['graph_audit']),
                           ('offline_graph_comparison',comparison['summary'])]:write(case_out/(name+'.json'),value)
        for name in ('source_fhir_r4_bundle.json','model_receipts.json','discovery.json','failures.json'):
            shutil.copyfile(case_dir/name,case_out/name)
        prior=read(case_dir/'summary.json');summary=dict(prior)
        summary.update(version='p2-auto-checkpoint-0.6.0-fixed-model-offline-graph-migration',
            original_input_directory=str(case_dir),new_remote_calls=0,
            candidate_checkpoints=comparison['summary']['strict_counts']['candidate_checkpoint_count'],
            candidate_episodes=comparison['summary']['strict_counts']['candidate_episode_count'],
            clinical_accuracy_measured=False,doctor_packet_release=False)
        summary=derive_case_summary(summary,comparison['strict_events'],comparison['strict_episodes'],comparison['strict_relations'],
            discovery=read(case_dir/'discovery.json'),model_receipts=read(case_dir/'model_receipts.json'),
            failures=read(case_dir/'failures.json'),graph_audit=comparison['graph_audit'])
        write(case_out/'summary.json',summary)
        source_paths[cid]=str(case_dir);rows.append(dict(case_id=cid,path_kind='old_model_proposals_offline_final_graph_migration',
            old_checkpoints=prior['candidate_checkpoints'],new_checkpoints=summary['candidate_checkpoints'],
            old_episodes=prior['candidate_episodes'],new_episodes=summary['candidate_episodes'],
            final_graph_demotions=comparison['summary']['checkpoint_demotion_count']))
    for cid in CASES:
        case_dir=Path(status['case_output_paths'][cid])
        shutil.copytree(case_dir,out/cid)
        prior=read(OLD/'attempts'/('04_'+cid)/cid/'summary.json')
        summary=read(case_dir/'summary.json');source_paths[cid]=str(case_dir)
        rows.append(dict(case_id=cid,path_kind='fixed_model_technical_completion_with_final_graph_validation',
            old_checkpoints=prior['candidate_checkpoints'],new_checkpoints=summary['candidate_checkpoints'],
            old_episodes=prior['candidate_episodes'],new_episodes=summary['candidate_episodes'],
            old_failures=prior['failures'],new_failures=summary['failures'],
            final_graph_demotions=summary['final_graph_audit']['demoted_checkpoint_count']))
    release_receipt=dict(version='fixed-ten-development-release-1',model='deepseek-v4.1-flash',
        case_count=len(rows),cases=sorted(rows,key=lambda r:r['case_id']),case_source_directories=source_paths,
        candidate_checkpoints=sum(r['new_checkpoints'] for r in rows),candidate_episodes=sum(r['new_episodes'] for r in rows),
        technical_complete=True,clinical_reference=False,doctor_blind_review_packets=False,
        clinical_accuracy_measured=False,improvement_proven=False,
        interpretation='Seven unchanged model-proposal cases migrated offline; three technically completed cases also changed source coverage, so count differences are not accuracy gains.')
    if len(rows)!=10:raise ValueError('EXPECTED_EXACT_FIXED_TEN_CASES')
    write(out/'release_receipt.json',release_receipt)
    verify(destination)
    return release_receipt


def repair_summary_release(destination=DEFAULT):
    """Create v2 metadata from immutable v1 objects, preserving every v1 byte."""
    destination=Path(destination);old=destination/'fixed_ten_development_release'
    new=destination/'fixed_ten_development_release_v2';new.mkdir(exist_ok=False)
    protected={str(p.resolve()):sha(p) for p in old.rglob('*') if p.is_file()}
    write(new/'summary_metadata_fix_freeze.json',dict(issue_id='CE-EXP-003',original_release_directory=str(old.resolve()),
        original_file_sha256=protected,model_api_calls=0,semantic_objects_changed=False))
    rows=[]
    for case_dir in sorted(old.glob('SCALE-*')):
        out=new/case_dir.name;out.mkdir()
        for p in case_dir.iterdir():
            if p.name!='summary.json':(out/p.name).symlink_to(p.resolve(),target_is_directory=p.is_dir())
        before=read(case_dir/'summary.json');summary=case_summary_from_directory(case_dir,before)
        summary.update(summary_metadata_revision='CE-EXP-003-v2',original_summary_sha256=sha(case_dir/'summary.json'),
            original_summary_path=str((case_dir/'summary.json').resolve()),new_remote_calls=0)
        write(out/'summary.json',summary)
        events=read(out/'decision_events.json');episodes=read(out/'episodes.json');relations=read(out/'relations.json')
        check=assert_summary_consistent(summary,events,episodes,relations)
        all_expected=case_summary_from_directory(out,summary)
        derived_fields=('checkpoint_statuses','discovered_events','reviewed_events','candidate_checkpoints','candidate_episodes','process_links','model_calls','cached_calls','failures','final_graph_audit')
        assert all(summary.get(k)==all_expected.get(k) for k in derived_fields)
        rows.append(dict(case_id=case_dir.name,consistency=check,
            changed_summary_fields=[k for k in derived_fields if before.get(k)!=summary.get(k)],
            before_checkpoint_statuses=before.get('checkpoint_statuses'),after_checkpoint_statuses=summary['checkpoint_statuses'],
            source_event_file_sha256=sha(out/'decision_events.json'),new_summary_sha256=sha(out/'summary.json')))
    old_release=read(old/'release_receipt.json')
    counts=dict(candidate_checkpoints=sum(r['consistency']['candidate_checkpoints'] for r in rows),
        candidate_episodes=sum(r['consistency']['candidate_episodes'] for r in rows))
    if len(rows)!=10 or any(counts[k]!=old_release[k] for k in counts):raise ValueError('SUMMARY_ONLY_REVISION_CHANGED_RELEASE_COUNTS')
    if any(sha(Path(p))!=h for p,h in protected.items()):raise ValueError('FROZEN_V1_RELEASE_CHANGED')
    code_files=sorted(Path(__file__).parent.rglob('*.py'))
    for p in code_files:
        target=new/'code_snapshot'/p.relative_to(Path(__file__).parent);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,target)
    receipt=dict(version='fixed-ten-development-release-2',issue_id='CE-EXP-003',change_type='derived_summary_metadata_only',
        supersedes_summary_metadata_of=str(old.resolve()),original_release_receipt_sha256=sha(old/'release_receipt.json'),
        case_count=len(rows),**counts,technical_complete=True,all_case_summary_consistency_passed=True,
        changed_summary_cases=[r['case_id'] for r in rows if r['changed_summary_fields']],
        semantic_objects_changed=False,source_files_changed=False,v1_files_changed=False,
        model_api_calls=0,socket_creation_calls=0,clinical_reference=False,clinical_accuracy_measured=False,
        doctor_blind_review_packets=False,code_sha256={str(p):sha(p) for p in code_files})
    write(new/'summary_consistency.json',rows);write(new/'release_receipt.json',receipt)
    return receipt


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['summarize','release','repair_summary_release'])
    parser.add_argument('--destination',type=Path,default=DEFAULT)
    args=parser.parse_args()
    result=globals()[args.action](args.destination)
    print(json.dumps({k:v for k,v in result.items() if k not in ('technical_summaries','transport','cases','case_source_directories')},ensure_ascii=False))
