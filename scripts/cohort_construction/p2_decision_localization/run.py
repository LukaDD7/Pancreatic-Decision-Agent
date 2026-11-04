"""Run v0.3 on the unchanged nested random sample; save only aggregates publicly.

Usage: python -m scripts.cohort_construction.p2_decision_localization.run --repo .
Private outputs contain patient records. Do not commit or upload that directory.
"""
from pathlib import Path
from collections import Counter, defaultdict
import argparse
import hashlib
import json
import random
import re
from .documents import load_sources, observed_time, parse_time, visit_key
from .labels import Labeler
from .timeline import build_chains, cross_visit_repeats, distribution


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(); repo = args.repo.resolve()
    config_path = Path(__file__).parent/'configs/rules-v0.3.json'
    config = json.loads(config_path.read_text())
    output = args.output or repo/'local_artifacts/p2_localization_v0.3_20261020'
    output.mkdir(parents=True, exist_ok=True)
    selection = json.loads((repo/'local_artifacts/p2_scale_20261020/selection.json').read_text())
    selected = [r['patient_uid'] for r in selection['selected']]
    frozen = list(map(json.loads, (repo/'local_artifacts/p1_quality_v0.2/results/patient_quality.jsonl').read_text().splitlines()))
    replay = sorted(p['patient_uid'] for p in frozen if p['state']=='PASS_V2')
    random.Random(config['seed']).shuffle(replay)
    assert len(replay)==2004 and selected==replay[:300]
    notes, coverage = load_sources(repo, selected)
    print(json.dumps({'stage':'loaded', 'notes':len(notes), 'source_rows':coverage['raw_rows']}), flush=True)
    labeler = Labeler(config); hits = [h for n in notes for h in labeler.match(n)]
    ni = {n['record_key']:n for n in notes}; hi = defaultdict(list)
    for h in hits:
        assert ni[h['record_key']]['text'][h['start']:h['end']]==h['match']
        hi[h['record_key']].append(h)
    repeats = cross_visit_repeats(notes,hits)
    all_chains, samples = [], {}
    for size in config['sample_sizes']:
        subset = selected[:size]; ss = set(subset)
        rr = [n for n in notes if n['patient_uid'] in ss]
        hh = [h for h in hits if h['patient_uid'] in ss]
        labels = {}
        for label in config['label_definitions']:
            lh = [h for h in hh if h['label']==label]
            labels[label] = {'hits':len(lh), 'notes':len({h['record_key'] for h in lh}),
                             'patients':len({h['patient_uid'] for h in lh}),
                             'roles':dict(Counter(h['role'] for h in lh)),
                             'eligible_notes':len({h['record_key'] for h in lh if h['candidate_eligible']}),
                             'other_procedure_notes':len({h['record_key'] for h in lh if h['target']=='OTHER_PROCEDURE'})}
        clock_results = {}
        for clock in ['body_preferred','system']:
            clock_results[clock] = {}
            for hours in config['chain_windows_hours']:
                chains, stats = build_chains(rr,hh,subset,config,clock,hours)
                clock_results[clock][str(hours)+'h'] = stats
                if size==300: all_chains.extend(chains)
        samples[str(size)] = {'patients':size, 'context_notes':len(rr), 'labeled_notes':len({h['record_key'] for h in hh}),
                              'labels':labels, 'candidate_chains':clock_results}
    # All provided CT/MR records are cached locally; pairing is a temporal
    # association, not proof of report availability or decision relevance.
    ims = defaultdict(dict)
    im_manifest = json.loads((repo/'local_artifacts/source_cache_20261020/image_cache_manifest.json').read_text())
    for original,meta in im_manifest['files'].items():
        path=Path(meta['cached_path']); digest=hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest==meta['sha256']; coverage['source_hashes'][original]=digest
        for x in map(json.loads, path.read_text().splitlines()):
            method=str(x.get('exam_method') or '');time=parse_time(x.get('exam_datetime'))
            if not time or not re.search(r'CT|MR|磁共振',method,re.I) or not re.search(r'胰|上腹|腹部|肝|胆|脾|肾|腹盆',method):continue
            if not (x.get('original_findings') or x.get('original_impression')):continue
            key=(str(x.get('exam_no_raw') or ''),str(time),method)
            ims[x['patient_uid']][key]={'time':time,'method':method,'report_uid':x.get('index_report_uid')}
    pairs, pair_summary = [], {}
    for clock in ['body_preferred','system']:
        groups=defaultdict(list); anchors={}
        for n in notes:
            time=observed_time(n,clock)
            if not time or not parse_time(config['study_start'])<=time<=parse_time(config['study_end']):continue
            key=(n['patient_uid'],visit_key(n));groups[key].append(n)
            if n['family'] in {'PREOP_BRIEF','PREOP_DISCUSSION'} and any(h['label']=='PLAN_RESECTION' and h['candidate_eligible'] for h in hi[n['record_key']]):
                if key not in anchors or time<observed_time(anchors[key],clock):anchors[key]=n
        results=defaultdict(list)
        for key,a in anchors.items():
            uid,visit=key;at=observed_time(a,clock)
            for kind in ['first_senior_round','current_same_surgery_path','abdominal_CT_MR_exam']:
                if kind=='abdominal_CT_MR_exam':
                    pool=[im for im in ims[uid].values() if im['time']<=at]
                    if not pool:continue
                    chosen=max(pool,key=lambda im:im['time']);t=chosen['time'];extra={'report_uid':chosen['report_uid'],'method':chosen['method']}
                else:
                    pool=[n for n in groups[key] if n['record_key']!=a['record_key'] and observed_time(n,clock)<=at and (n['family']=='FIRST_SENIOR_ROUND' if kind=='first_senior_round' else n['family'] not in {'PREOP_BRIEF','PREOP_DISCUSSION'} and any(h['candidate_eligible'] and h['label'] in {'PLAN_RESECTION','INDICATION_YES'} for h in hi[n['record_key']]))]
                    if not pool:continue
                    chosen=max(pool,key=lambda n:observed_time(n,clock));t=observed_time(chosen,clock);extra={'record_key':chosen['record_key'],'family':chosen['family']}
                gap=round((at-t).total_seconds()/3600,4)
                pairs.append({'clock':clock,'patient_uid':uid,'anchor_record':a['record_key'],'kind':kind,'gap_hours':gap,**extra});results[kind].append(gap)
        pair_summary[clock]={'preop_plan_anchors':len(anchors),'pairs':{k:{'paired':len(v),'within_24h':sum(x<=24 for x in v),'within_48h':sum(x<=48 for x in v),'within_7d':sum(x<=168 for x in v),'hours':distribution(v)} for k,v in results.items()}}
    oldnotes={x['record_key'] for x in map(json.loads,(repo/'local_artifacts/p2_scale_20261020/clinical_notes.jsonl').read_text().splitlines())}
    summary={'version':config['version'],'config_sha256':hashlib.sha256(config_path.read_bytes()).hexdigest(),
             'source_code_sha256':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(Path(__file__).parent.glob('*.py'))},
             'population':2004,'nested_sample':[100,300],'seed':config['seed'],'study_window':[config['study_start'],config['study_end']],
             'clinical_source_coverage':{k:v for k,v in coverage.items() if k!='source_hashes'},'imaging_patient_files':len(im_manifest['files']),
             'broadened_context_records':sum(n['record_key'] not in oldnotes for n in notes),'samples':samples,
             'preop_backtrack':pair_summary,
             'cross_visit_statement_repeat':{'records':len({x['record_key'] for x in repeats}),'patients':len({x['patient_uid'] for x in repeats}),'eligible_records':len({x['record_key'] for x in repeats if x['candidate_eligible']}),'hit_roles':dict(Counter(x['role'] for x in repeats))},
             'meaning':'Unadjudicated source-witness chains; not independent clinical decisions or episodes. v0.2 and v0.3 differ in dictionary, source context coverage and counting method; totals are not directly interchangeable.'}
    with (output/'notes.jsonl').open('w') as stream:
        for n in notes:stream.write(json.dumps(n,ensure_ascii=False)+'\n')
    write(output/'hits.json',hits);write(output/'chains.json',all_chains);write(output/'pairs.json',pairs)
    write(output/'review_queue.json',[h for h in hits if h['label'] in config['candidate_labels'] and not h['candidate_eligible']])
    write(output/'cross_visit_repeats.json',repeats);write(output/'input_hashes.json',coverage['source_hashes'])
    write(output/'selection.json',selection);write(output/'aggregate.json',summary)
    assert not any(u in json.dumps(summary) for u in selected)
    print(json.dumps({'stage':'done','samples':{n:d['candidate_chains']['body_preferred']['48h'] for n,d in samples.items()},'backtrack':pair_summary['body_preferred']},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
