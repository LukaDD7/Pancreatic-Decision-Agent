"""Build the local, read-only v0.4.3 progress casebook; never call a model."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources  # noqa: E402

RELEASE = ROOT / 'private/p2_auto_checkpoint_v0.4_20261025/release_fixed10_v043'
OUTPUT = ROOT / 'outputs/v043-progress-casebook'
MASK = '[身份信息已隐去]'
IDENTITY_KEYS = ('姓名', '地址', '电话', '联系方式', '病案号', '住院号', '门诊号', '身份证号', '证件号')
IDENTITY_LABEL = r'(?:患者姓名|姓名|现居住地址|家庭住址|住址|地址|联系电话|联系方式|病案号|住院号|门诊号|身份证号|证件号)'
STAFF_LABEL = r'(?:医师|医生|术者|助手|护士|签名)'


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def identity_values(sources):
    values = set()
    for source in sources:
        for key, value in (source.get('original_fields') or {}).items():
            if any(word in str(key) for word in IDENTITY_KEYS) and isinstance(value, (str, int)):
                if str(value).strip():
                    values.add(str(value).strip())
    return values


def masked_display(text, values):
    """Return a presentation copy and raw-to-display spans; never alter sources."""
    replacements = []
    for value in sorted(values, key=len, reverse=True):
        if value:
            replacements.extend((match.start(), match.end()) for match in re.finditer(re.escape(value), text))
    patterns = (
        rf'({IDENTITY_LABEL}\s*[：:]\s*)[^\r\n]{{0,100}}',
        rf'({STAFF_LABEL}\s*[：:]\s*)[^\s，,；;。]{{1,20}}',
        r'家住[^。\n]+。',
        r'(?<!\d)1[3-9]\d{9}(?!\d)',
        r'(?<!\d)\d{17}[\dXx](?!\d)',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            replacements.append((match.start(1) if match.lastindex else match.start(), match.end()))
    chunks, offset_map, previous, display_length = [], [], 0, 0
    for start, end in sorted(replacements):
        if start < previous or start == end:
            continue
        chunks.append(text[previous:start])
        display_length += start - previous
        offset_map.append((previous, start, display_length))
        chunks.append(MASK)
        display_length += len(MASK)
        previous = end
    chunks.append(text[previous:])
    offset_map.append((previous, len(text), display_length))
    display = ''.join(chunks)

    return display, offset_map


def citation_rows(event, role, values):
    rows = []
    for item in (event.get(role + '_citations') or {}).get('valid', []):
        rows.append({
            'source_id': item['source_id'], 'source_sha256': item['source_sha256'],
            'start': item['start'], 'end': item['end'], 'role': role,
            'quote': masked_display(item['quote'], values)[0],
            'span_verified': item['span_verified'], 'raw_quote_verified': item['raw_quote_verified'],
        })
    return rows


def compact_context(event):
    context = event.get('evidence_context') or {}
    keys = ('uncertainty', 'cutoff', 'cutoff_basis', 'choice_time', 'cutoff_is_exact_choice_time',
            'cutoff_is_choice_time_upper_bound', 'operation_interval', 'same_test_source_ids',
            'budget', 'availability_certified', 'blind_replay_ready', 'actual_payload_characters',
            'provider_budget_omitted_blocks')
    result = {key: context.get(key) for key in keys}
    result['ledger_dispositions'] = dict(Counter(item.get('disposition') for item in context.get('ledger', [])))
    result['current_source_allowed_spans'] = (context.get('allowed_spans') or {}).get(event['source_id'], [])
    return result


def compact_blocks(blocks):
    # De-duplicate actual review blocks, not repeated selected evidence ranges.
    unique = {}
    for block in blocks or []:
        key = (block.get('source_id'), block.get('start'), block.get('end'),
               hashlib.sha256((block.get('text') or '').encode()).hexdigest())
        unique.setdefault(key, {k: v for k, v in block.items()
                              if k in ('source_id', 'title', 'kind', 'clock', 'interval', 'start', 'end')})
    return list(unique.values())


def build_case(case_dir, values):
    bundle = load(case_dir / 'source_fhir_r4_bundle.json')
    sources, receipt = import_sources(bundle)
    source_index = {source['source_id']: source for source in sources}
    if receipt['research_annotations_used_as_input']:
        raise ValueError('Research annotations must not be source inputs')
    source_rows = []
    for source in sources:
        text, offset_map = masked_display(source['text'], values)
        source_rows.append({
            'id': source['source_id'], 'kind': source['kind'], 'family': source['family'],
            'title': masked_display(source['title'] or source['kind'], values)[0],
            'clock': source.get('clock'), 'clock_basis': source.get('clock_basis'),
            'clocks': source.get('clocks'), 'text': text,
            'source_sha256': source['source_sha256'], 'raw_characters': len(source['text']),
            'display_masked': text != source['text'], 'offset_map': offset_map,
        })
    events = load(case_dir / 'decision_events.json')
    event_rows = []
    for event in events:
        for source_id in event.get('source_ids', [event.get('source_id')]):
            if source_id not in source_index:
                raise ValueError(f'Unknown event source: {source_id}')
        event_rows.append({
            key: event.get(key) for key in (
                'event_id', 'local_id', 'source_id', 'source_ids', 'unit_id', 'kind',
                'document_profile', 'management_question', 'action', 'scope',
                'record_function', 'phase', 'choice_time', 'time_basis',
                'source_document_clock', 'decision_clock', 'checkpoint_status',
                'previous_event_id', 'same_process_previous_id', 'episode_id',
                'rationale', 'uncertainty', 'uncertainty_dimensions', 'blind_replay_ready',
                'expert_reference', 'discovery_request_sha256', 'review_request_sha256',
                'current_prechoice_complete_region',
            )
        } | {
            'semantic_review': event.get('semantic_review'),
            'citations': sum((citation_rows(event, role, values) for role in ('action', 'prechoice', 'postchoice')), []),
            'evidence_context': compact_context(event),
            'evidence_blocks': compact_blocks(event.get('actual_model_evidence')),
        })
    return {
        'case_id': case_dir.name, 'sources': source_rows, 'events': event_rows,
        'units': load(case_dir / 'unit_inventory.json'),
        'summary': load(case_dir / 'summary.json'),
        'failures': load(case_dir / 'failures.json'),
        'model_receipts': load(case_dir / 'model_receipts.json'),
    }


def audit(data):
    """Check counts, raw citation spans, source hashes and release completeness."""
    assert len(data['cases']) == 10
    assert data['release']['status'] == 'COMPLETE_DEVELOPMENT_RERUN_V043'
    summaries = [case['summary'] for case in data['cases']]
    assert sum(s['discovered_events'] for s in summaries) == 414
    assert sum(s['candidate_checkpoints'] for s in summaries) == 146
    assert sum(s['whole_document_or_test_units'] for s in summaries) == 347
    assert all(s['failures'] == 0 and not case['failures'] for s, case in zip(summaries, data['cases']))
    statuses = Counter()
    citation_count = 0
    for case in data['cases']:
        assert all(source['source_sha256'] not in ('', None) for source in case['sources'])
        event_ids = {event['event_id'] for event in case['events']}
        assert len(event_ids) == len(case['events']) == case['summary']['discovered_events']
        for event in case['events']:
            statuses[event['checkpoint_status']] += 1
            assert event['blind_replay_ready'] is False
            for citation in event['citations']:
                assert citation['span_verified'] and citation['raw_quote_verified']
                citation_count += 1
            for key in ('previous_event_id', 'same_process_previous_id'):
                if event.get(key):
                    assert event[key] in event_ids
    assert statuses['CANDIDATE_NEW_EVIDENCE'] == 112
    assert statuses['CANDIDATE_FIRST_ANCHOR'] == 34
    return dict(cases=10, source_resources=sum(len(c['sources']) for c in data['cases']),
                input_units=347, semantic_events=414, candidate_checkpoints=146,
                valid_event_citations=citation_count, raw_citation_spans='preserved_in_release_and_checked_by_pipeline',
                display_masking='presentation_only; original SHA-256 retained; display offsets can change',
                failures=0, model_calls_during_build=0, statuses=dict(statuses))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, default=RELEASE)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    case_dirs = sorted(args.release.glob('SCALE-[0-9][0-9]'))
    if [p.name for p in case_dirs] != [f'SCALE-{i:02d}' for i in range(1, 11)]:
        raise ValueError('Expected the fixed ten final release cases')
    all_sources = [import_sources(load(p / 'source_fhir_r4_bundle.json'))[0] for p in case_dirs]
    data = {
        'schema': 'pdac-v043-progress-casebook/v1',
        'release': load(args.release.parent / 'CURRENT_RUN.json'),
        'manifest': load(args.release.parent / 'change_manifest_v043.json'),
        'provenance': load(args.release / 'case_provenance.json'),
        'verification': load(args.release.parent / 'verification_receipt_v043.json'),
        'cases': [build_case(path, values) for path, values in zip(case_dirs, map(identity_values, all_sources))],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    (args.output / 'casebook-data.json').write_text(payload, encoding='utf-8')
    template = (Path(__file__).parent / 'template.html').read_text(encoding='utf-8')
    if template.count('__DATA__') != 1:
        raise ValueError('Template must contain exactly one data slot')
    html = template.replace('__DATA__', payload)
    (args.output / 'index.html').write_text(html, encoding='utf-8')
    portable = html.replace('<body>', '<body data-portable="true">', 1)
    portable_name = 'PDAC-v0.4.3-十例进度对照-分享版.html'
    (args.output / portable_name).write_text(portable, encoding='utf-8')
    readme = '''PDAC v0.4.3 固定十例进度对照\n\n打开 HTML 文件即可离线阅读，无需服务器或联网。\n分享版已内嵌十例展示数据；不含完整模型缓存、原始运行 JSON / FHIR。\n候选决策时点不是医生确认的临床 Reference，也不是正式盲评材料。\n\n隐私提醒：页面含临床内容。虽对常见身份字段作额外掩蔽，\n但未完成人工逐字隐私审查，也不保证匿名化；请仅发送给获授权的研究人员。\n'''
    (args.output / '分享说明.txt').write_text(readme, encoding='utf-8')
    archive = args.output / 'PDAC-v0.4.3-十例进度对照-分享版.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        bundle.writestr('README.txt', readme)
        bundle.write(args.output / portable_name, portable_name)
    (args.output / 'build-receipt.json').write_text(json.dumps(audit(data), ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'cases': len(data['cases']), 'events': 414, 'checkpoints': 146}, ensure_ascii=False))


if __name__ == '__main__':
    main()
