"""Run all-P0 retrieval, preserve source scope, draw a reproducible development sample."""
from collections import Counter, defaultdict
from pathlib import Path
import argparse
import hashlib
import json
import random

from scripts.cohort_construction.p2_decision_localization.documents import load_sources
from .retrieval import compile_rules, find_hits, review_flags


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def jsonl(path, rows):
    with path.open('w') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[3])
    args = ap.parse_args()
    repo = args.repo.resolve()
    here = Path(__file__).resolve().parent
    config = json.loads((here/'rules.json').read_text())
    out = repo/'local_artifacts/p2_dynamic_pathway_probe_v0.1_20261021'
    out.mkdir(parents=True, exist_ok=True)
    frozen = [json.loads(s) for s in (repo/'local_artifacts/p1_quality_v0.2/results/patient_quality.jsonl').read_text().splitlines()]
    patients = [p['patient_uid'] for p in frozen]
    notes, coverage = load_sources(repo, patients)
    compiled = compile_rules(config)
    hits = []
    for note in notes:
        for hit in find_hits(note['text'], compiled):
            assert note['text'][hit['start']:hit['end']] == hit['quote']
            assert all(note['text'][t['start']:t['end']] == t['text'] for t in hit['matched_terms'])
            hits.append({'record_key': note['record_key'], 'patient_uid': note['patient_uid'],
                         'document_type': note['document_type'], 'family': note['family'],
                         'flags': review_flags(hit['quote']), **hit})
    jsonl(out/'hits.jsonl', hits)
    matched_keys = {h['record_key'] for h in hits}
    jsonl(out/'matched_notes.jsonl', [n for n in notes if n['record_key'] in matched_keys])

    # Exclude the original four patients from NEW development sampling only.
    # They remain in the search population and serve as development anchors.
    annotations = json.loads((repo/'local_artifacts/p2_localization_v0.3_20261020/semantic_annotations.json').read_text())
    seed_keys = {a['record_key'] for a in annotations if a['case_id'] in ['B01', 'B02', 'B03', 'B06']}
    seed_patients = {n['patient_uid'] for n in notes if n['record_key'] in seed_keys}
    rng = random.Random(config['seed'])
    sample = []
    for category in config['patterns']:
        used_patients = set()
        candidates = [h for h in hits if h['pattern'] == category and h['patient_uid'] not in seed_patients]
        # Two samples with explicit past-language clues, two without; fill a
        # deficient stratum from remaining patients. Both strata may be wrong
        # about clinical currentness, which is precisely what review checks.
        for past in [False, True]:
            pool = defaultdict(list)
            for h in candidates:
                if ('PAST_CUE' in h['flags']) == past and h['patient_uid'] not in used_patients:
                    pool[h['patient_uid']].append(h)
            chosen = rng.sample(sorted(pool), min(2, len(pool)))
            for uid in chosen:
                alternatives = sorted(pool[uid], key=lambda h: (h['record_key'], h['start']))
                sample.append({**rng.choice(alternatives), 'sample_id': f'{category}-{len(used_patients)+1:02d}',
                               'sampling_stratum': 'PAST_CUE' if past else 'NO_EXPLICIT_PAST_CUE'})
                used_patients.add(uid)
        if len(used_patients) < 4:
            pool = defaultdict(list)
            for h in candidates:
                if h['patient_uid'] not in used_patients:
                    pool[h['patient_uid']].append(h)
            for uid in rng.sample(sorted(pool), min(4-len(used_patients), len(pool))):
                sample.append({**rng.choice(sorted(pool[uid], key=lambda h: (h['record_key'], h['start']))),
                               'sample_id': f'{category}-{len(used_patients)+1:02d}', 'sampling_stratum': 'FILL'})
                used_patients.add(uid)
    write(out/'development_sample.json', sample)
    selected_patients = {h['patient_uid'] for h in sample}
    jsonl(out/'sample_patient_context.jsonl', [n for n in notes if n['patient_uid'] in selected_patients])
    summary = {
        'version': config['version'], 'P0_n': len(patients), 'P1_filter_used': False,
        'study_period_filter_used': False,
        'clinical_source_rows': coverage['raw_rows'], 'retained_context_notes': len(notes),
        'source_files_verified': coverage['clinical_files'], 'total_category_hits': len(hits),
        'unique_hit_notes': len(matched_keys), 'unique_hit_patients': len({h['patient_uid'] for h in hits}),
        'categories': {k: {
            'name': config['patterns'][k]['name'], 'hits': len(rr := [h for h in hits if h['pattern'] == k]),
            'patients': len({h['patient_uid'] for h in rr}), 'notes': len({h['record_key'] for h in rr}),
            'sentence_count': dict(Counter(h['sentence_count'] for h in rr)),
            'families': dict(Counter(h['family'] for h in rr)),
            'review_flag_counts': dict(Counter(f for h in rr for f in h['flags']))
        } for k in config['patterns']},
        'sample': {'seed': config['seed'], 'hits': len(sample), 'patients': len(selected_patients),
                   'original_seed_patients_excluded_only_from_new_sample': len(seed_patients)},
        'clinical_reference_validated': False,
        'units': 'Overlapping retrieval spans and patients, not independent decisions/episodes or accepted modes.'
    }
    write(out/'aggregate.json', summary)
    write(repo/'docs/p2-dynamic-pathway-probe-v0.1-20261021.json', summary)
    guide = json.loads((repo/'docs/nccn-pancreatic-v4.2026-source.json').read_text())
    assert sha(Path(guide['local_path'])) == guide['sha256']
    write(out/'run_manifest.json', {
        'version': config['version'], 'coverage': coverage, 'guideline_sha256': guide['sha256'],
        'rules_sha256': sha(here/'rules.json'), 'code_sha256': {n: sha(here/n) for n in ['run.py', 'retrieval.py']},
        'reader_dependency_sha256': sha(repo/'scripts/cohort_construction/p2_decision_localization/documents.py'),
        'P0_registry_sha256': sha(repo/'local_artifacts/p1_quality_v0.2/results/patient_quality.jsonl'),
        'sample_sha256': sha(out/'development_sample.json'), 'seed': config['seed']
    })
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
