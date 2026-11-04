"""Read hashed clinical sources and expose raw type, normalized family and clocks.

VISIT_ID is a source encounter identifier. Neither this module nor downstream
code equates an encounter with a surgical-candidacy episode.
"""
from pathlib import Path
from collections import Counter
import datetime as dt
import hashlib
import json
import re
import pyarrow.parquet as pq


def parse_time(value):
    """Retain source precision: no date-only body header is imputed to midnight."""
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        return None


def header_time(text):
    match = re.match(r'\s*(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})日?\s+(\d{1,2})[:：](\d{2})(?::(\d{2}))?', text)
    if not match:
        return None
    try:
        return dt.datetime(*[int(x or 0) for x in match.groups()])
    except ValueError:
        return None


def document_family(title):
    """Map original titles to transparent groups, in precedence order."""
    mapping = [
        ('MDT', r'MDT|多学科'), ('PREOP_DISCUSSION', r'术前讨论'),
        ('PREOP_BRIEF', r'术前小结'), ('POSTOP', r'术后'),
        ('TRANSFER_IN', r'转入'), ('TRANSFER_OUT', r'转出'),
        ('DISCHARGE', r'出院'), ('CONSULT', r'会诊'),
        ('OUTPATIENT', r'门诊|就诊记录'), ('FIRST_COURSE', r'首次病程'),
        ('FIRST_SENIOR_ROUND', r'首次查房|首次.{0,12}查房'),
        ('WARD_ROUND', r'查房'), ('ADMISSION', r'入院'),
        ('OPERATION', r'手术记录|操作记录'),
    ]
    return next((family for family, pattern in mapping if re.search(pattern, title, re.I)), 'OTHER')


def explicit_dates(text):
    """Named date fields are additional observations, never clock replacements.

    A date-only discussion field has no invented time-of-day. A scheduled
    operation date is a plan date, not a decision date or proof of execution.
    """
    output = {}
    for key, pattern in [('discussion_date', r'讨论日期[：:]\s*(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})'),
                         ('scheduled_operation_date', r'(?:手术时间[：:]|定于)\s*(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})')]:
        match = re.search(pattern,text)
        if match:
            try:output[key] = dt.date(*map(int,match.groups())).isoformat()
            except ValueError:output[key] = None
    return output


def visit_key(note):
    """Unknown encounters remain separate; never merge all missing VISIT_IDs."""
    return note['visit_id'] or 'unknown:' + note['record_key']


def observed_time(note, clock='body_preferred'):
    """Both are observation proxies, not proved clinical/report availability."""
    if clock == 'system':
        return parse_time(note['system_time'])
    return parse_time(note['body_time']) or parse_time(note['system_time'])


def load_sources(repo, selected):
    """Read all physician/context records for selected patients, even without 胰.

    Counts of ALL source rows are checked before excluding nursing/consent
    documents. This broadens v0.2's pancreas-word filter; it is separately
    reported and never changes frozen P1 membership.
    """
    manifest = json.loads((repo/'local_artifacts/source_cache_20261020/clinical_cache_manifest.json').read_text())
    frozen = {x['patient_uid']: x for x in map(json.loads, (repo/'local_artifacts/p1_quality_v0.2/results/patient_quality.jsonl').read_text().splitlines())}
    counts, notes, hashes = Counter(), [], {}
    columns = ['patient_uid', 'VISIT_ID', '文书名称', '文书内容', 'create_time', 'admission_time', 'discharge_time', 'source_record_id', 'source_file', 'source_row']
    for original, meta in manifest['files'].items():
        path = Path(meta['cached_path'])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == meta['sha256']
        hashes[original] = digest
        for row in pq.read_table(path, columns=columns, filters=[('patient_uid', 'in', selected)]).to_pylist():
            uid = row['patient_uid']; counts[uid] += 1
            title = row['文书名称'] or ''
            if re.search(r'护理|同意书|交接|核查|物品|告知|申请单|报告卡', title):
                continue
            raw = row['文书内容']
            if isinstance(raw, bytes):
                try:
                    text = raw.decode('utf-8')
                except UnicodeDecodeError:
                    text = raw.decode('gb18030')
            else:
                text = str(raw or '')
            text = re.sub(r'<[^>]+>', '\n', text)
            if not text.strip():
                continue
            body_hash = hashlib.sha256(text.encode()).hexdigest()
            key = hashlib.sha256((uid+'|'+str(row['source_record_id'])+'|'+body_hash).encode()).hexdigest()[:24]
            system = parse_time(row['create_time'])
            fallback = parse_time(row['admission_time'])
            notes.append({'patient_uid': uid, 'visit_id': str(row['VISIT_ID'] or ''), 'record_key': key,
                          'document_type': title, 'family': document_family(title), 'text': text,
                          'body_time': str(header_time(text)) if header_time(text) else None,
                          'system_time': str(system or fallback) if system or fallback else None,
                          'system_time_origin': 'create_time' if system else 'admission_fallback' if fallback else 'missing',
                          'admission_time': str(fallback) if fallback else None,
                          'discharge_time': str(parse_time(row['discharge_time'])) if row['discharge_time'] else None,
                          'source_parquet': original, 'source_file': row['source_file'], 'source_row': row['source_row'],
                          'source_record_id': row['source_record_id'], 'body_sha256': body_hash})
            notes[-1]['explicit_dates'] = explicit_dates(text)
            dates = notes[-1]['explicit_dates']; body = notes[-1]['body_time']; sys = notes[-1]['system_time']
            flags = []
            if body and sys and body[:10] != sys[:10]: flags.append('BODY_SYSTEM_DATE_DISAGREE')
            if body and dates.get('discussion_date') and body[:10] != dates['discussion_date']: flags.append('DISCUSSION_BODY_DATE_DISAGREE')
            notes[-1]['time_quality_flags'] = flags
    assert all(counts[u] == frozen[u]['clinical_source_rows'] for u in selected)
    assert len({n['record_key'] for n in notes}) == len(notes)
    return notes, {'clinical_files': len(hashes), 'raw_rows': sum(counts.values()), 'retained_context_notes': len(notes),
                   'source_counts_match_frozen_P1': True, 'source_hashes': hashes}
