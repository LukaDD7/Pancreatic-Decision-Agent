"""Retrieve condition/choice co-occurrences from all P0 clinical-context rows.

Existing P2 role labels are annotation/prioritization only. Neither phrases nor
their deduplication are independently confirmed decisions, episodes or modes.
Raw quotes and identities stay in ignored local artifacts; only aggregate output
is exported to docs. No P1 state filter, no outcome-based certification.
"""
from collections import Counter,defaultdict
from pathlib import Path
import argparse
import hashlib
import json
import random
import re
import pyarrow.parquet as pq
from scripts.cohort_construction.p2_decision_localization.labels import Labeler
from scripts.cohort_construction.p2_decision_localization.documents import document_family,header_time


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def find_patterns(text,rules):
    """Pair terms within a sentence; emit the source span, never a clinical GT."""
    for unit in re.finditer(r'[^。；;\n]+',text):
        for name,rule in rules['patterns'].items():
            if all(re.search(pattern,unit.group(),re.I) for pattern in rule['all_of']):
                yield {'pattern':name,'start':unit.start(),'end':unit.end(),'quote':unit.group(),
                       'supporting_label':rule['supporting_label']}


def annotate(note,match,labeler):
    hits=[h for h in labeler.match(note) if h['label']==match['supporting_label'] and
          h['start']<match['end'] and h['end']>match['start']]
    return {'current_opinion_hint':any(h['candidate_eligible'] for h in hits),
            'roles':sorted({h['role'] for h in hits}),
            'sections':sorted({h['section'] for h in hits}),
            'no_role_match':not bool(hits)}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[3])
    args=ap.parse_args();repo=args.repo.resolve();here=Path(__file__).resolve().parent
    rules=json.loads((here/'rules.json').read_text())
    config_path=repo/'scripts/cohort_construction/p2_decision_localization/configs/rules-v0.3.json'
    labeler=Labeler(json.loads(config_path.read_text()))
    frozen={p['patient_uid']:p for p in map(json.loads,(repo/'local_artifacts/p1_quality_v0.2/results/patient_quality.jsonl').read_text().splitlines())}
    manifest=json.loads((repo/'local_artifacts/source_cache_20261020/clinical_cache_manifest.json').read_text())
    counts=Counter();retained=Counter();results=[];hashes={};notes={}
    for original,meta in manifest['files'].items():
        p=Path(meta['cached_path']);h=hashlib.sha256(p.read_bytes()).hexdigest();assert h==meta['sha256'];hashes[original]=h
        for row_number,row in enumerate(pq.read_table(p).to_pylist()):
            uid=row['patient_uid']
            if uid not in frozen:continue
            counts[uid]+=1
            title=str(row['文书名称'] or '')
            if re.search(r'护理|同意书|交接|核查|物品|告知|申请单|报告卡',title):continue
            raw=row['文书内容']
            if isinstance(raw,bytes):
                try:text=raw.decode('utf-8')
                except UnicodeDecodeError:text=raw.decode('gb18030')
            else:text=str(raw or '')
            text=re.sub(r'<[^>]+>','\n',text)
            if not text.strip():continue
            retained[uid]+=1
            matches=list(find_patterns(text,rules))
            if not matches:continue
            key=digest(uid+'|'+str(row['source_record_id'])+'|'+digest(text))[:24]
            note={'record_key':key,'patient_uid':uid,'family':document_family(title),'document_type':title,
                  'text':text,'visit_id':str(row['VISIT_ID'] or ''),'body_time':str(header_time(text) or ''),
                  'system_time':str(row['create_time'] or ''),'source_file':original,'source_parquet_row':row_number,
                  'source_record_id':str(row['source_record_id'] or '')}
            notes[key]=note
            for match in matches:
                assert text[match['start']:match['end']]==match['quote']
                results.append({'record_key':key,'patient_uid':uid,'document_type':title,'family':note['family'],
                                'visit_id':note['visit_id'],**match,**annotate(note,match,labeler)})
    assert all(counts[u]==p['clinical_source_rows'] for u,p in frozen.items())
    out=repo/'local_artifacts/p2_four_pattern_probe_20261021';out.mkdir(parents=True,exist_ok=True)
    with (out/'hits.jsonl').open('w') as f:
        for r in results:f.write(json.dumps(r,ensure_ascii=False)+'\n')
    with (out/'matched_notes.jsonl').open('w') as f:
        for n in notes.values():f.write(json.dumps(n,ensure_ascii=False)+'\n')
    summary={}
    for name in rules['patterns']:
        rr=[r for r in results if r['pattern']==name];current=[r for r in rr if r['current_opinion_hint']]
        signatures=Counter((r['patient_uid'],re.sub(r'\s+','',r['quote'])) for r in current)
        summary[name]={'name':rules['patterns'][name]['name'],'raw_sentence_hits':len(rr),
                       'raw_hit_patients':len({r['patient_uid'] for r in rr}),
                       'current_opinion_hint_hits':len(current),'current_opinion_hint_patients':len({r['patient_uid'] for r in current}),
                       'within_patient_distinct_sentence_signatures':len(signatures),
                       'repeat_hint_hits':sum(n-1 for n in signatures.values()),
                       'document_families_current_hints':dict(Counter(r['family'] for r in current)),
                       'role_counts_all_hits':dict(Counter(role for r in rr for role in r['roles']))}
    rng=random.Random(20261021);sample=[]
    for name in rules['patterns']:
        for flag in [True,False]:
            pool=sorted([r for r in results if r['pattern']==name and r['current_opinion_hint']==flag],key=lambda r:(r['record_key'],r['start']))
            sample.extend(rng.sample(pool,min(6,len(pool))))
    write(out/'development_sample.json',sample)
    # Verify that the original four examples are retrieved, without using them
    # as an independent validation set or exporting their IDs/text to Git.
    originals=json.loads((repo/'local_artifacts/p2_localization_v0.3_20261020/semantic_annotations.json').read_text())
    seed_map={'B02':'CONDITION_DEFERRAL','B03':'DECOMPRESSION_REASSESSMENT','B06':'SURGICAL_REFERRAL','B01':'DISEASE_NO_SURGERY'}
    seeds=[]
    for a in originals:
        if a['case_id'] not in seed_map:continue
        found=[r for r in results if r['record_key']==a['record_key'] and r['pattern']==seed_map[a['case_id']]]
        seeds.append({'case_id':a['case_id'],'pattern':seed_map[a['case_id']],'retrieved':bool(found),'current_opinion_hint':any(r['current_opinion_hint'] for r in found)})
    public={'version':rules['version'],'P0_n':len(frozen),'clinical_source_rows':sum(counts.values()),
            'retained_context_rows':sum(retained.values()),'source_files':len(hashes),'P1_filter_used':False,
            'raw_hit_patients_any_category':len({r['patient_uid'] for r in results}),
            'study_period_filter_used':False,'development_sample_size':len(sample),
            'categories':summary,'original_four_seed_retrieval':seeds,'clinical_validation_completed':False,
            'units':'Sentence hits/patients, not decisions or episodes; categories overlap; wording signatures do not prove distinct decisions.',
            'limitations':'Co-occurrence does not establish causality, currentness or surgical target. Current-opinion hints only prioritize review; false hints remain in the output.'}
    write(out/'summary.json',public);write(repo/'docs/p2-four-pattern-probe-v0.1-20261021.json',public)
    write(out/'run_manifest.json',{'source_sha256':hashes,'rules_sha256':hashlib.sha256((here/'rules.json').read_bytes()).hexdigest(),
                                 'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                 'dependency_config_sha256':hashlib.sha256(config_path.read_bytes()).hexdigest(),
                                 'dependency_code_sha256':{name:hashlib.sha256((config_path.parents[1]/name).read_bytes()).hexdigest() for name in ['labels.py','documents.py']},
                                 'seed':20261021,'no_P1_gate':True,'clinical_validation_completed':False})
    print(json.dumps(public,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
