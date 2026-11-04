"""Compare frozen development runs and join N01 to the existing FHIR fixture.

Outputs are private. Original sampled source IDs remain fixed across repairs;
the runner records migration, not automatic clinical review or accuracy.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from .fhir_links import source_crosswalk


def read_nodes(path):
    return {n['id']: n for n in map(json.loads,(path/'nodes.jsonl').read_text().splitlines())}


def compare(repo, baseline, current, output):
    old,new=read_nodes(baseline),read_nodes(current)
    shared=old.keys() & new.keys()
    migrations=[{'id':k,'old_role':old[k]['role'],'new_role':new[k]['role'],
                 'old_status':old[k]['status'],'new_status':new[k]['status'],
                 'old_flags':old[k]['flags'],'new_flags':new[k]['flags']}
                for k in sorted(shared) if any(old[k][f]!=new[k][f] for f in ['role','status','flags'])]
    queue=json.loads((baseline/'review_queue.json').read_text())
    same_sample=[]
    for i,q in enumerate(queue):
        current_nodes=[n for n in new.values() if n['patient_uid']==q['patient_uid'] and n['source_id']==q['source_id']]
        same_sample.append({'sample_index':i,**q,'current_nodes':current_nodes,
                            'same_node_retained':q['node_id'] in new})
    cases=json.loads((repo/'local_artifacts/p2_dynamic_pathway_probe_v0.1_20261021/five_case_review.json').read_text())
    uid=next(c['patient_uid'] for c in cases if c['case_id']=='N01')
    envelopes=repo/'local_artifacts/n01_fhir_replay_v0.1_20261022/researcher_event_envelopes.jsonl'
    events=list(map(json.loads,envelopes.read_text().splitlines()))
    crosswalk=source_crosswalk(new.values(),events,{uid:'N01'})
    summary={'baseline_nodes':len(old),'current_nodes':len(new),'shared_nodes':len(shared),
             'added_nodes':len(new.keys()-old.keys()),'no_longer_emitted_nodes':len(old.keys()-new.keys()),
             'role_migrations':dict(Counter(old[k]['role']+' -> '+new[k]['role'] for k in sorted(shared) if old[k]['role']!=new[k]['role'])),
             'status_migrations':dict(Counter(old[k]['status']+' -> '+new[k]['status'] for k in sorted(shared) if old[k]['status']!=new[k]['status'])),
             'original_sample_rechecked_sources':len(same_sample),'n01_fhir_linked_nodes':len(crosswalk),
             'n01_fhir_linked_documents':len({x['fhir_reference'] for x in crosswalk}),
             'source_or_cohort_membership_changed':False,'confirmed_episode_count':None}
    output.mkdir(parents=True,exist_ok=False)
    artifacts={'summary.json':summary,'node_migrations.json':migrations,'fixed_sample_comparison.json':same_sample,
               'n01_fhir_crosswalk.json':crosswalk,
               'node_presence_changes.json':{'added':sorted(new.keys()-old.keys()),'no_longer_emitted':sorted(old.keys()-new.keys())}}
    for name,value in artifacts.items():
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    inputs=[baseline/'nodes.jsonl',current/'nodes.jsonl',baseline/'review_queue.json',envelopes]
    receipt={'inputs_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
             'outputs_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir()},
             'code_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}}
    (output/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for arg in ['repo','baseline','current','output']:p.add_argument('--'+arg,type=Path,required=True)
    a=p.parse_args();print(json.dumps(compare(a.repo,a.baseline,a.current,a.output),ensure_ascii=False))
