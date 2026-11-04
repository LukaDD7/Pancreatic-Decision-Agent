"""Build a local FHIR R4 source collection and four reviewed N01 replay fixtures.

This adapter is a feasibility fixture, NOT a cohort-wide decision extractor.
All full-text outputs are private. No network, model, server or EHR writes.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .core import pack_event, replay, source_bundle


ANCHORS = [
    ('D01', '4ae5a785ff15ff090c2bdcef', '今予PTCD术胆汁引流', 'initial_drainage_plan'),
    ('D02', '396323346aab110ac28bf70f', '予拔除引流管', 'tube_failure_management_and_repeat_plan'),
    ('D03', '4d095889eed5a7546132418e', '今予PTCD术胆汁引流', 'reassessment_of_repeat_plan'),
    ('D04', '1e6a2b321477ddbb96b21bfc', '仍需胆汁引流', 'reassessment_and_ercp_plan'),
]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(repo, project, output, schema_path):
    previous = repo / 'local_artifacts/p2_dynamic_pathway_probe_v0.1_20261021'
    cases_path = previous / 'five_case_review.json'
    context_path = previous / 'review_patient_context.jsonl'
    case = next(c for c in json.loads(cases_path.read_text()) if c['case_id'] == 'N01')
    patient_dir = project / 'data/PDAC/batches/batch_008/patients' / case['patient_uid']
    bundle_path = patient_dir / 'patient_source_bundle.json'
    image_path = patient_dir / 'imaging_reports_all_sources.jsonl'
    source = json.loads(bundle_path.read_text())
    notes = [n for n in map(json.loads, context_path.read_text().splitlines())
             if n['patient_uid'] == case['patient_uid']]
    images = [r for r in map(json.loads, image_path.read_text().splitlines())
              if r['index_report_uid'].startswith('all-original:')]
    events = []
    for n in notes:
        events.append(pack_event('N01', 'document', n['record_key'], n['document_type'], n['text'],
                                 event_time=n['body_time'], recorded_time=n['system_time'],
                                 time_basis='body_time_or_source_create_proxy'))
    for r in images:
        # The two original text fields remain separate inside a verbatim JSON payload.
        text = json.dumps({k: r.get(k) for k in ['original_findings', 'original_impression']},
                          ensure_ascii=False)
        events.append(pack_event('N01', 'imaging', r['index_report_uid'], r['exam_method'], text,
                                 event_time=r['exam_datetime'], time_basis='exam_only_release_unknown'))
    lab_keys = ['检验项目名称', '检验结果值', '检验结果单位', '检验参考值', '报告时间', '送检时间']
    for r in source['laboratory_original_L1_all']:
        text = json.dumps({k: r.get(k) for k in lab_keys}, ensure_ascii=False)
        events.append(pack_event('N01', 'lab', r['source_record_id'], r['检验项目名称'], text,
                                 release_time=r.get('报告时间'), time_basis='source_report_time_proxy',
                                 result=r.get('检验结果值'), unit=r.get('检验结果单位'),
                                 reference=r.get('检验参考值')))
    collection = source_bundle(events, datetime.now(timezone.utc).isoformat())
    # Official JSON schema checks structure only, not all FHIRPath invariants,
    # terminology, profile compliance, clinical semantics or actual availability.
    from jsonschema import Draft6Validator
    schema = json.loads(schema_path.read_text())
    errors = list(Draft6Validator(schema).iter_errors(collection))
    if errors:
        raise ValueError([(list(e.path), e.message) for e in errors[:5]])
    resources = {e['resource']['resourceType'] + '/' + e['resource']['id'] for e in collection['entry']}
    for entry in collection['entry']:
        r = entry['resource']
        for ref in [r.get('subject', {})] + r.get('target', []) + [a.get('who', {}) for a in r.get('agent', [])]:
            if ref.get('reference') and ref['reference'] not in resources:
                raise ValueError('Unresolved resource reference')
    output.mkdir(parents=True, exist_ok=False)
    write(output / 'researcher_full_fhir_r4_bundle.json', collection)
    (output / 'researcher_event_envelopes.jsonl').write_text(''.join(
        json.dumps(e, ensure_ascii=False) + '\n' for e in events))
    records = {n['record_key']: n for n in notes}
    summary = []
    for point_id, key, marker, role in ANCHORS:
        for mode in ['reconstruction', 'certified']:
            packet, audit = replay(events, 'N01', records[key]['body_time'], key, marker, mode)
            write(output / f'{point_id}_{mode}_state.json', packet)
            write(output / f'{point_id}_{mode}_reviewer_audit.json', audit)
            if mode == 'reconstruction':
                summary.append({'point_id': point_id, 'manual_role': role,
                                'counts': dict(Counter(b['kind'] for b in packet['source_blocks'])),
                                'late_created_selected': sum(x['selected'] and x['late_creation'] for x in audit['source_selection'])})
    # Compare with the earlier curated draft: broad source replay also retains
    # three admission/education forms, chest X-ray and first procedure report.
    # Their presence is not evidence of a surgical decision or treatment stage.
    d3 = json.loads((output / 'D03_reconstruction_state.json').read_text())
    assert Counter(b['kind'] for b in d3['source_blocks']) == {'document': 10, 'imaging': 4, 'lab': 138}
    assert all('今予PTCD术胆汁引流' not in b['text'] for b in d3['source_blocks'] if b['source_id'] == ANCHORS[2][1])
    assert any('择期再行PTCD手术治疗' in b['text'] for b in d3['source_blocks'])
    assert not json.loads((output / 'D03_certified_state.json').read_text())['source_blocks']
    receipt = {'version': '0.1', 'status': 'LOCAL_FEASIBILITY_FIXTURE',
               'fhir_version': '4.0.1', 'schema_validation': 'official JSON schema structure only; passed',
               'schema_sha256': sha(schema_path), 'source_counts': dict(Counter(e['kind'] for e in events)),
               'bundle_resource_count': len(collection['entry']), 'checkpoints': summary,
               'source_coverage': {'context_notes': len(notes), 'all_delivered_documents': len(source['documents_all']),
                                   'note_selection': 'previous clinical-context scope, not all documents',
                                   'images': 'all-original copies only; other copies preserved in source, not imported twice',
                                   'labs': 'original L1 only; stage7/analysis containers excluded'},
               'automatic_decision_detection': False, 'formal_blind_evaluation_ready': False,
               'certified_mode': 'no source version availability verified; empty subset is NOT an empty clinical state',
               'input_sha256': {str(p): sha(p) for p in [cases_path, context_path, bundle_path, image_path]},
               'code_sha256': {p.name: sha(p) for p in Path(__file__).parent.glob('*.py')},
               'output_sha256': {p.name: sha(p) for p in output.iterdir() if p.is_file()}}
    write(output / 'build_receipt.json', receipt)
    return {k: receipt[k] for k in ['source_counts', 'bundle_resource_count', 'checkpoints', 'schema_validation']}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--project', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True, help='New output directory; existing runs are never overwritten')
    ap.add_argument('--schema', type=Path, required=True)
    a = ap.parse_args()
    print(json.dumps(build(a.repo, a.project, a.output, a.schema), ensure_ascii=False))


if __name__ == '__main__':
    main()
