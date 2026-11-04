"""Prioritize explicit relation wording; retain broad hits and all original spans.

Developed after reviewing the frozen broad sample, so these results are development
outputs, not a held-out accuracy test. No clinical mode/event is assigned.
"""
from collections import Counter, defaultdict
from pathlib import Path
import argparse
import hashlib
import json
import random
import re

from .run import write


def relation_matches(hit, rules):
    quote = hit['quote']
    if re.search(r'手术可能存在的风险|手术风险[：:]', quote):
        return [], ['LOCAL_RISK_TEMPLATE']
    matches = []
    for pattern in rules['patterns'][hit['pattern']]:
        for m in re.finditer(pattern, quote, re.I):
            matches.append({'text': m.group(), 'start': hit['start']+m.start(), 'end': hit['start']+m.end()})
    return matches, []


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[3])
    args = ap.parse_args(); repo = args.repo.resolve(); here = Path(__file__).resolve().parent
    out = repo/'local_artifacts/p2_dynamic_pathway_probe_v0.1_20261021'
    rules = json.loads((here/'relation_rules.json').read_text())
    hits = [json.loads(s) for s in (out/'hits.jsonl').read_text().splitlines()]
    notes = {n['record_key']: n for n in map(json.loads, (out/'matched_notes.jsonl').read_text().splitlines())}
    annotations = json.loads((repo/'local_artifacts/p2_localization_v0.3_20261020/semantic_annotations.json').read_text())
    keys = {a['record_key'] for a in annotations if a['case_id'] in ['B01','B02','B03','B06']}
    seed_patients = {n['patient_uid'] for n in notes.values() if n['record_key'] in keys}
    for h in hits:
        h['relation_matches'], h['priority_flags'] = relation_matches(h, rules)
        h['explicit_relation_hint'] = bool(h['relation_matches'])
        assert all(notes[h['record_key']]['text'][m['start']:m['end']] == m['text'] for m in h['relation_matches'])
    with (out/'prioritized_hits.jsonl').open('w') as f:
        for h in hits: f.write(json.dumps(h, ensure_ascii=False)+'\n')
    # Distinct-patient sampling within each category. This second-stage sample
    # intentionally targets relation wording; retain its different selection rule.
    rng = random.Random(20261022); sample = []
    for category in rules['patterns']:
        pool = defaultdict(list)
        for h in hits:
            if h['pattern']==category and h['explicit_relation_hint'] and h['patient_uid'] not in seed_patients:
                pool[h['patient_uid']].append(h)
        for i, uid in enumerate(rng.sample(sorted(pool), min(4,len(pool))), 1):
            h=rng.choice(sorted(pool[uid], key=lambda h:(h['record_key'],h['start'],h['end'])))
            sample.append({**h,'sample_id':f'R{category[1:3]}-{i:02d}','sampling_stage':'RELATION_HINT_DEVELOPMENT'})
    write(out/'relation_sample.json', sample)
    stats={'version':rules['version'], 'broad_hits_retained':len(hits), 'filtering_patients':False,
           'categories':{k:{'relation_hint_hits':len(rr:=[h for h in hits if h['pattern']==k and h['explicit_relation_hint']]),
                            'patients':len({h['patient_uid'] for h in rr}),
                            'families':dict(Counter(h['family'] for h in rr))} for k in rules['patterns']},
           'sample':{'seed':20261022,'hits':len(sample),'patients':len({h['patient_uid'] for h in sample})},
           'held_out_validation':False,'independent_decision_count':None,
           'rule_sha256':hashlib.sha256((here/'relation_rules.json').read_bytes()).hexdigest()}
    write(out/'relation_aggregate.json',stats)
    write(repo/'docs/p2-dynamic-pathway-relations-v0.1-20261021.json',stats)
    print(json.dumps(stats,ensure_ascii=False,indent=2))


if __name__ == '__main__': main()
