"""Verify source pointers/quote spans, not clinical correctness or reference GT."""
from collections import Counter, defaultdict
from pathlib import Path
import argparse
import json
from .labels import normalize
from .run import read_jsonl, sha, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[4])
    args = parser.parse_args()
    repo = args.repo.resolve()
    here = Path(__file__).resolve().parent
    rules = json.loads((here/'rules.json').read_text())
    base = repo/'local_artifacts'/rules['artifact_directory']/'results'
    manifest = json.loads((base/'run_manifest.json').read_text())
    # Only files imported by the runner at its release time are hashed there.
    for name, expected in manifest['script_hashes'].items():
        assert sha(here/name) == expected, 'Runtime/code mismatch: '+name
    assert sha(here/'rules.json') == manifest['rules_sha256']
    old = repo/'local_artifacts/p1_quality_v0.2/results'
    assert sha(old/'patient_quality.jsonl') == manifest['frozen_patient_sha256']
    assert sha(old/'report_quality.jsonl') == manifest['frozen_report_sha256']
    images = list(read_jsonl(base/'image_annotations.jsonl'))
    notes = {n['record_key']:n for n in read_jsonl(base/'clinical_annotations.jsonl')}
    patients = list(read_jsonl(base/'patient_quality.jsonl'))
    references = list(read_jsonl(base/'reference_links.jsonl'))
    quote_count = 0
    for image in images:
        fields = {k: normalize(image.get(k)) for k in ['original_findings','original_impression']}
        for kind in ['vessel_statements','measurements','metastatic_statements']:
            for item in image['parsed'][kind]:
                assert fields[item['source_field']][item['start']:item['end']] == item['quote']
                quote_count += 1
    for reference in references:
        text = notes[reference['note_key']]['text_index_view']
        assert text[reference['start']:reference['end']] == reference['quote']
        assert reference['mention_start'] >= reference['start']
        assert reference['mention_end'] <= reference['end']
        if reference['role'] == 'PLANNED':
            assert reference['match_status'] == 'PLANNED_NOT_MISSING'
        quote_count += 1
    summary = json.loads((base/'summary.json').read_text())
    assert summary['source_row_coverage_matches_frozen']
    assert dict(Counter(p['state'] for p in patients)) == summary['patient_states']
    assert len(patients) == 2707
    # Private cards preserve source identity and excerpts. These are development
    # review material; selection is not an independent accuracy sample.
    sample = json.loads((base/'development_sample.json').read_text())
    selected = {s['patient_uid'] for s in sample}
    image_groups, note_groups = defaultdict(list), defaultdict(list)
    for image in images:
        if image['patient_uid'] in selected:
            image_groups[image['patient_uid']].append(image)
    for note in notes.values():
        if note['patient_uid'] in selected:
            note_groups[note['patient_uid']].append(note)
    by_patient = {p['patient_uid']:p for p in patients}
    cards = []
    for selected_item in sample:
        uid = selected_item['patient_uid']
        image = sorted(image_groups[uid], key=lambda r:(not (r['has_verified_source'] and r['coverage']['substantive_research_related']),r['record_key']))[:1]
        note = sorted(note_groups[uid], key=lambda n:(not (n['record_type_supported'] and n['coverage']['substantive_research_related']),n['record_key']))[:2]
        cards.append({'selection':selected_item,'patient':by_patient[uid],'images':image,'notes':note})
    write_json(base/'development_source_cards.json',cards)
    receipt = {'version':summary['version'],'patient_count':len(patients),'image_content_versions':len(images),
               'quote_coordinates_verified':quote_count,'quote_coordinate_failures':0,
               'clinical_source_rows':summary['clinical_rows'],'source_row_coverage_matches_frozen':True,
               'development_sample_patients':len(cards),'clinical_validation_completed':False,
               'output_hashes':{n:sha(base/n) for n in ['patient_quality.jsonl','image_annotations.jsonl','clinical_annotations.jsonl','reference_links.jsonl','summary.json','run_manifest.json','development_sample.json','development_source_cards.json']}}
    write_json(base/'verification_receipt.json',receipt)
    public = {**receipt,'output_hashes':receipt['output_hashes']}
    write_json(repo/'receipts'/(rules['receipt_stem']+'.json'),public)
    print(json.dumps({k:v for k,v in receipt.items() if k!='output_hashes'},ensure_ascii=False))


if __name__ == '__main__':
    main()
