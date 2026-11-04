"""Run on frozen 49-patient development context, not a new cohort sample.

Saves all source indices including zero-hit notes, statement nodes, tentative
links, old/new retrieval differences and a reproducible balanced review queue.
This runner never assigns expert Reference or final episodes.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import statistics
from jsonschema import Draft202012Validator
from .extract import Extractor, relate


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def write(path, value):path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
def jsonl(path, rows):path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))


def run(repo, output):
    prior=repo/'local_artifacts/p2_dynamic_pathway_probe_v0.1_20261021'
    source=prior/'review_patient_context.jsonl'
    release=json.loads((repo/'scripts/cohort_construction/p2_dynamic_pathway_probe/release.json').read_text())
    assert sha(source)==release['private_outputs_sha256'][source.name], 'Frozen context changed'
    config_path=repo/'scripts/cohort_construction/p2_decision_localization/configs/rules-v0.3.json'
    extractor=Extractor(json.loads(config_path.read_text()))
    notes=[json.loads(s) for s in source.read_text().splitlines()]
    assert len(notes)==5199 and len({n['patient_uid'] for n in notes})==49
    bykey={(n['patient_uid'],n['record_key']):n for n in notes}
    assert len(bykey)==len(notes), 'Source identities are not unique'
    nodes=[];index=[];old_keys=set();new_keys=set()
    for n in notes:
        found=extractor.extract(n);nodes.extend(found)
        old=extractor.labeler.match(n)
        key=(n['patient_uid'],n['record_key'])
        if any(x['candidate_eligible'] for x in old):old_keys.add(key)
        if any(x['status']=='CURRENT_STATEMENT_CANDIDATE' for x in found):new_keys.add(key)
        index.append({k:n.get(k) for k in ['patient_uid','record_key','document_type','family','body_time','system_time','source_file','source_row','source_record_id']} |
                     {'body_sha256':hashlib.sha256(n['text'].encode()).hexdigest(), 'node_ids':[x['id'] for x in found],
                      'legacy_eligible':key in old_keys, 'new_current_candidate':key in new_keys})
    validator=Draft202012Validator(json.loads((Path(__file__).parent/'node.schema.json').read_text()))
    for x in nodes:
        validator.validate(x)
        n=bykey[x['patient_uid'],x['source_id']]
        assert n['text'][slice(*x['span'])]==x['quote']
        assert hashlib.sha256(n['text'].encode()).hexdigest()==x['source_sha256']
        for a in x['action_mentions']:assert n['text'][a['start']:a['end']]==a['text']
        for s in x['signals']:assert n['text'][slice(*s['span'])]==s['quote']
    assert len({n['id'] for n in nodes})==len(nodes)
    links=relate(nodes);byid={n['id']:n for n in nodes}
    for edge in links:
        a,b=byid[edge['from']],byid[edge['to']]
        assert a['patient_uid']==b['patient_uid']
        assert a['ordering_time']<b['ordering_time']
        assert edge['episode_id'] is None
    cases=json.loads((prior/'five_case_review.json').read_text())
    checks=[]
    for c in cases:
        matched=[n for n in nodes if n['source_id']==c['record_key'] and n['patient_uid']==c['patient_uid']
                 and n['span'][0]<c['entry_end'] and c['entry_start']<n['span'][1]]
        checks.append({'case_id':c['case_id'],'node_ids':[n['id'] for n in matched],
                       'statuses':[n['status'] for n in matched],
                       'interpretation':'overlap with prior analyst-selected source; NOT independent validation'})
    # Independent of the original FHIR mapper: only use the reviewed anchor list
    # to inspect extraction coverage AFTER the new rules have run on every note.
    from scripts.cohort_construction.p2_fhir_replay.build_n01 import ANCHORS
    n01=next(c['patient_uid'] for c in cases if c['case_id']=='N01')
    anchor_checks=[]
    for point,key,marker,role in ANCHORS:
        note=bykey[n01,key];pos=note['text'].index(marker)
        matches=[n for n in nodes if n['patient_uid']==n01 and n['source_id']==key and n['span'][0]<=pos<n['span'][1]]
        anchor_checks.append({'anchor':point,'manual_role':role,'node_ids':[n['id'] for n in matches],
                              'statuses':[n['status'] for n in matches]})
    # Reproducible review queue. Five strata, up to four distinct patients each.
    # It is development sampling within an already purposefully selected pool.
    rng=random.Random(20261022);queue=[]
    groups={
        'current_candidate':[n for n in nodes if n['status']=='CURRENT_STATEMENT_CANDIDATE'],
        'context_review':[n for n in nodes if n['role']=='DECISION_CANDIDATE' and n['status']=='CONTEXT_REVIEW'],
        'execution':[n for n in nodes if n['role']=='EXECUTION_MENTION'],
        'feedback':[n for n in nodes if n['role']=='FEEDBACK_MENTION'],
        'zero_hit_source':[n for n in index if not n['node_ids']],
    }
    for stratum,rows in groups.items():
        patients=sorted({r['patient_uid'] for r in rows});rng.shuffle(patients)
        for patient in patients[:4]:
            options=sorted([r for r in rows if r['patient_uid']==patient],key=lambda r:r.get('id',r.get('record_key')))
            row=rng.choice(options);key=row.get('source_id',row.get('record_key'))
            queue.append({'stratum':stratum,'patient_uid':patient,'source_id':key,'node_id':row.get('id'),
                          'source_note':bykey[patient,key],'review_status':'PENDING'})
    counts=Counter(n['patient_uid'] for n in nodes if n['status']=='CURRENT_STATEMENT_CANDIDATE')
    patients=sorted({n['patient_uid'] for n in notes})
    aggregate={'version':'0.1','pool':'49 previously selected development patients; NOT representative P0 sample',
               'source_patients':len(patients),'source_documents':len(notes),'statement_nodes':len(nodes),
               'roles':dict(Counter(n['role'] for n in nodes)),'statuses':dict(Counter(n['status'] for n in nodes)),
               'supportive_care_pathway_link_required':sum('SUPPORTIVE_CARE_PATHWAY_LINK_REQUIRED' in n['flags'] for n in nodes),
               'zero_node_documents':sum(not n['node_ids'] for n in index),'review_links':len(links),
               'current_candidate_patients':len(counts),'current_candidate_statement_median_per_patient':statistics.median(counts[p] for p in patients),
               'legacy_comparison_same_inputs':{'legacy_eligible_documents':len(old_keys),'new_current_candidate_documents':len(new_keys),
                                               'retained':len(old_keys&new_keys),'newly_prioritized':len(new_keys-old_keys),
                                               'no_longer_prioritized':len(old_keys-new_keys)},
               'manual_anchor_count':4,'n01_anchor_hits':sum(bool(x['node_ids']) for x in anchor_checks),
               'new_review_queue_size':len(queue),'clinical_decision_count':None,'episode_count':None,
               'expert_reference_generated':False,'clinical_validation':False}
    output.mkdir(parents=True,exist_ok=False)
    jsonl(output/'source_index.jsonl',index);jsonl(output/'nodes.jsonl',nodes);jsonl(output/'links.jsonl',links)
    write(output/'review_queue.json',queue);write(output/'known_case_checks.json',checks)
    write(output/'n01_anchor_checks.json',anchor_checks);write(output/'aggregate.json',aggregate)
    write(output/'legacy_comparison.json',{'newly_prioritized':[list(x) for x in sorted(new_keys-old_keys)],
                                          'no_longer_prioritized':[list(x) for x in sorted(old_keys-new_keys)]})
    # Full context stays researcher-only. Each card has neighbours but makes no
    # claim that neighbour ordering proves clinical causation or availability.
    lines=['# 五例 Decision-IR 开发复核卡\n','原文用于定位复核，含当次选择与后续，不是Agent或专家盲评包。\n']
    for c in cases:
        lines.append(f'\n## {c["case_id"]}\n')
        patient_nodes=[n for n in nodes if n['patient_uid']==c['patient_uid']]
        # N01 shows all current-statement candidates and all PTCD/ERCP sources;
        # other cases show their known anchor source plus adjacent source notes.
        pn=sorted([n for n in notes if n['patient_uid']==c['patient_uid']],key=lambda n:((n.get('body_time') or n.get('system_time') or ''),n['record_key']))
        i=next(i for i,n in enumerate(pn) if n['record_key']==c['record_key'])
        keys={n['record_key'] for n in pn[max(0,i-1):i+2]}
        if c['case_id']=='N01':keys|={n['source_id'] for n in patient_nodes if n['status']=='CURRENT_STATEMENT_CANDIDATE' or any(a['concept'] in {'PTCD','ERCP'} for a in n['action_mentions'])}
        for note in pn:
            if note['record_key'] not in keys:continue
            found=[n for n in patient_nodes if n['source_id']==note['record_key']]
            lines += [f'### {note["document_type"]} | 正文 {note.get("body_time")} | 创建 {note.get("system_time")}\n',
                      f'来源 `{note["record_key"]}`；IR单元数 {len(found)}\n', note['text']+'\n']
            for n in found:
                lines.append(f'- `{n["id"]}` {n["role"]} / {n["status"]}；偏移{n["span"]}；标记{n["flags"]}\n')
    (output/'五例-判断执行反馈复核卡.md').write_text('\n'.join(lines))
    write(output/'run_manifest.json',{'input_sha256':{str(source):sha(source),str(config_path):sha(config_path),str(prior/'five_case_review.json'):sha(prior/'five_case_review.json')},
                                    'seed':20261022,'schema_validated':True,'source_and_action_spans_checked':True,
                                    'legacy_chain_builder_used':False,'fhir_dependency_for_extraction':False,
                                    'code_sha256':{str(p.relative_to(repo)):sha(p) for p in (Path(__file__).parent).rglob('*') if p.is_file() and '__pycache__' not in p.parts},
                                    'outputs_sha256':{p.name:sha(p) for p in output.iterdir() if p.is_file()}})
    return aggregate


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--repo',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(json.dumps(run(a.repo,a.output),ensure_ascii=False))
