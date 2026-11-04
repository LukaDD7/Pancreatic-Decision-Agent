"""Index imaging citations without inserting reports into clinical notes."""
import datetime as dt
import re


def exam_number(value):
    value = str(value or '').strip()
    if re.fullmatch(r'\d+\.0', value):
        value = value[:-2]
    return '' if value.lower() in {'', 'none', 'nan', 'null', '0', '未知', '无'} else value.upper()


def modality(value):
    value = str(value or '')
    if re.search(r'MRI?|磁共振', value, re.I):
        return 'MR'
    if re.search(r'CT|计算机断层', value, re.I):
        return 'CT'
    return 'OTHER'


def dateof(value):
    match = re.search(r'(19\d{2}|20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})', str(value or ''))
    if not match:
        return ''
    try:
        return dt.date(*map(int, match.groups())).isoformat()
    except ValueError:
        return ''


def citations(text):
    for unit in re.finditer(r'[^。；;\n]+', text):
        for match in re.finditer(r'(?<![A-Za-z])(?:CT|MRI|MR)(?![A-Za-z])|磁共振', unit.group(), re.I):
            # PET/CT is not equated to a diagnostic pancreas CT.
            if re.search(r'PET\s*/\s*$', unit.group()[:match.start()], re.I):
                continue
            left = unit.group()[max(0, match.start()-28):match.start()]
            right = unit.group()[match.end():match.end()+70]
            local_left = re.split(r'[，,]', left)[-1]
            qualifier = r'(?:腹部|上腹部|全腹部|胰腺|增强|平扫|及|和|\+|\s){0,6}'
            planned = bool(re.search(r'(?:拟(?:行|查|完善)?|建议(?:行|查|完善)?|预约|待(?:行|查)|计划(?:行|查)?|完善)'+qualifier+r'$', local_left))
            if re.search(r'已(?:行|查|完善)\s*(?:腹部|胰腺|增强)?\s*$', local_left):
                planned = False
            query = unit.group()[max(0, match.start()-35):match.end()+50]
            date = dateof(query)
            number = re.search(r'(?:检查号|影像号|报告号)\s*[:：]\s*([A-Za-z0-9-]{6,})', query)
            has_result = bool(re.search(r'提示|所见|报告|显示|检查|复查|考虑|见|示|结果|增强|平扫', right)) or bool(date or number)
            yield {'quote': unit.group(), 'start': unit.start(), 'end': unit.end(),
                   'mention_start': unit.start()+match.start(), 'mention_end': unit.start()+match.end(),
                   'modality': modality(match.group()), 'referenced_date': date,
                   'exam_no': exam_number(number[1]) if number else '',
                   'role': 'PLANNED' if planned else 'EXISTING_CITATION' if has_result else 'UNRESOLVED_MENTION',
                   'external_clue': bool(re.search(r'外院|外地|当地', local_left))}


def link(citation, reports):
    """No nearest-date guessing. Date-only matches stay candidates."""
    if citation['role'] == 'PLANNED':
        return {'match_status': 'PLANNED_NOT_MISSING', 'candidate_report_ids': [], 'match_basis': 'future_plan'}
    if citation['role'] != 'EXISTING_CITATION':
        return {'match_status': 'UNRESOLVED_REFERENCE', 'candidate_report_ids': [], 'match_basis': 'insufficient_citation'}
    eligible = [r for r in reports if r['modality'] == citation['modality']]
    date, number = citation['referenced_date'], citation['exam_no']
    if number:
        candidates = [r for r in eligible if number in r['exam_nos'] and (not date or date == r['date'])]
        basis = 'patient_modality_exam_number_optional_date'
    elif date:
        candidates = [r for r in eligible if date == r['date']]
        basis = 'patient_modality_date_only'
    else:
        return {'match_status': 'UNRESOLVED_REFERENCE', 'candidate_report_ids': [], 'match_basis': 'no_specific_exam_key'}
    verified = [r for r in candidates if r['has_verified_source']]
    if candidates and not verified:
        return {'match_status': 'SOURCE_UNVERIFIED_CANDIDATE', 'candidate_report_ids': sorted(r['record_key'] for r in candidates), 'match_basis': basis}
    candidates = verified
    groups = {(r['date'], tuple(r['exam_nos']) if r['exam_nos'] else r['record_key']) for r in candidates}
    status = ('NOT_FOUND_IN_DELIVERED' if not candidates else 'AMBIGUOUS' if len(groups) > 1
              else 'FOUND_EXACT' if number else 'FOUND_CANDIDATE')
    return {'match_status': status, 'candidate_report_ids': sorted(r['record_key'] for r in candidates),
            'match_basis': basis, 'not_found_meaning': 'No independently verified original in supplied snapshot; not proof exam was not performed.'}
