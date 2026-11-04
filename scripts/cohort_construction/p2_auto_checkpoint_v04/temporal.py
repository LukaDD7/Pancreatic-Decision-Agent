"""Verify source-explicit choice times without promoting record/execution clocks.

This module makes only a temporal support check. It does not infer whether a
clinical decision was appropriate, whether a report was used, or a later fact
was visible. Raw citation validation remains mandatory and per-citation.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
import hashlib
import re


_DATE = r'20\d{2}(?:[-/]\d{1,2}[-/]\d{1,2}|年\d{1,2}月\d{1,2}日)'
_CLOCK = r'(?:\d{1,2}[:：]\d{2}(?:[:：]\d{2})?|\d{1,2}时\d{1,2}分(?:\d{1,2}秒)?)'
_MOMENT = rf'(?P<moment>{_DATE}(?:[ T　]*(?:{_CLOCK}))?)'
# The date must be attached to an actual choice/assessment statement, not merely
# somewhere in a long quote that also happens to contain a choice verb.
_LABEL = re.compile(rf'(?P<label>(?:本次|当前)?(?:决定|决策|评估)(?:的)?时间)[：: \t　]*{_MOMENT}')
_NARRATIVE = re.compile(rf'于[ \t　]*{_MOMENT}(?:时)?[ \t　，,]*(?:经[^，。；;\n]{{0,20}}(?:后)?[，,]?)?(?P<verb>决定|作出[^，。；;\n]{{0,8}}决定|评估)')
_DATED_DECISION = re.compile(rf'{_MOMENT}(?:时)?[ \t　，,]*(?P<verb>作出[^，。；;\n]{{0,8}}决定)')
_NOT_ACTUAL_PREFIX = re.compile(r'(?:拟|计划|预计|预约|建议|将|待|尚未|未曾|没有|未|无明确|并非|不是)[^，。；;\n]{0,12}$')
_HEADER_PREFIX = re.compile(r'(?:记录时间|报告时间|检查时间|手术时间|手术开始时间|拟手术时间|拟实施时间)[：: \t　]*$')
_PRECISION = {'date': 0, 'minute': 1, 'second': 2}


def _normalize(value):
    """Canonical local date/minute/second with no midnight/seconds expansion."""
    if not isinstance(value, str):
        return None
    value = value.strip().replace('：', ':').replace('T', ' ')
    match = re.fullmatch(r'(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?(?:[ \t　]*(?:(\d{1,2}):(\d{2})(?::(\d{2}))?|(\d{1,2})时(\d{1,2})分(?:(\d{1,2})秒)?))?', value)
    if not match:
        return None
    year, month, day = map(int, match.group(1, 2, 3))
    hour, minute, second = (match.group(4), match.group(5), match.group(6)) if match.group(4) is not None else (match.group(7), match.group(8), match.group(9))
    try:
        datetime(year, month, day, int(hour or 0), int(minute or 0), int(second or 0))
    except ValueError:
        return None
    normalized = f'{year:04}-{month:02}-{day:02}'
    precision = 'date'
    if hour is not None:
        normalized += f' {int(hour):02}:{int(minute):02}'
        precision = 'minute'
        if second is not None:
            normalized += f':{int(second):02}'
            precision = 'second'
    return dict(value=normalized, precision=precision, raw=value)


def _sources(sources):
    values = sources.items() if isinstance(sources, Mapping) else [(None, s) for s in sources]
    result = {}
    for key, source in values:
        if isinstance(source, str):
            source = dict(source_id=key, text=source)
        if isinstance(source, Mapping):
            sid = source.get('source_id', key)
            if sid:
                result[sid] = source
    return result


def _raw(source):
    return next((source[field] for field in ('raw_text', 'original_text', 'text') if isinstance(source.get(field), str)), None)


def _raw_span(citation, sources):
    source = sources.get(citation.get('source_id'))
    if source is None:
        return None, 'UNKNOWN_SOURCE_ID'
    text = _raw(source)
    if text is None:
        return None, 'NO_RAW_SOURCE_TEXT'
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if source.get('source_sha256') not in (None, digest) or citation.get('source_sha256') not in (None, digest):
        return None, 'SOURCE_HASH_MISMATCH'
    start, end = citation.get('start'), citation.get('end')
    if any(not isinstance(x, int) or isinstance(x, bool) for x in (start, end)) or not 0 <= start < end <= len(text):
        return None, 'INVALID_RESOLVED_SPAN'
    if text[start:end] != citation.get('quote'):
        return None, 'RESOLVED_SPAN_TEXT_MISMATCH'
    return dict(source_id=citation['source_id'], source_sha256=digest, quote=text[start:end], start=start, end=end), None


def _explicit_moments(quote):
    result, seen = [], set()
    for pattern in (_LABEL, _NARRATIVE, _DATED_DECISION):
        for match in pattern.finditer(quote):
            # A future evaluation appointment is not an already made choice.
            prefix = quote[max(0, match.start()-30):match.start()]
            if _NOT_ACTUAL_PREFIX.search(prefix) or _HEADER_PREFIX.search(prefix):
                continue
            normalized = _normalize(match['moment'])
            if normalized is None:
                continue
            key = (match.start('moment'), match.end('moment'), normalized['value'])
            if key in seen:
                continue
            seen.add(key)
            result.append(dict(**normalized, quote_start=match.start('moment'), quote_end=match.end('moment'),
                               explicit_statement=match.group()))
    return result


def _coarse(value, precision):
    return value[:{'date': 10, 'minute': 16, 'second': 19}[precision]]


def _conflicts(a, b):
    precision = min((a['precision'], b['precision']), key=lambda p: _PRECISION[p])
    return _coarse(a['value'], precision) != _coarse(b['value'], precision)


def _fallback_basis(event, primary):
    basis = event.get('time_basis')
    if basis in ('record_time_proxy', 'operative_phase', 'historical_unknown'):
        return basis
    if primary:
        from .source_time import source_record_time
        if source_record_time(primary)['status'] == 'PHYSICIAN_SIGNATURE_RECORD_DAY_PROXY':
            return 'record_time_proxy'
    if basis == 'unknown':
        return basis
    from .context import is_operative_phase
    if event.get('document_profile') == 'operative' or is_operative_phase(event):
        return 'operative_phase'
    if primary and primary.get('kind') == 'document' and isinstance(primary.get('clock'), Mapping) and primary['clock'].get('value'):
        return 'record_time_proxy'
    return 'unknown'


def verify_choice_time(event, sources, time_citations):
    """Return a verified actual choice time or null, keeping every proposal.

    Only resolved valid raw citations in the current event's source(s) can
    establish an actual time. Ambiguous/invalid citations remain in the receipt
    and do not erase independently valid ones. Coarser explicit source precision
    may support a coarser date/minute, never the model's invented finer precision.
    Proxy/operative-phase/performed-report events keep actual ``choice_time``
    null; their retrieval bounds belong to the separate context compiler.
    """
    indexed = _sources(sources)
    primary = indexed.get(event.get('source_id'))
    declared = event.get('choice_time')
    normalized_proposal = _normalize(declared)
    citation_result = time_citations or {}
    valid = citation_result.get('valid', [])
    ambiguous = deepcopy(citation_result.get('ambiguous', []))
    invalid = deepcopy(citation_result.get('invalid', []))
    accepted, rejected, explicit, uncertainty = [], [], [], []
    original = dict(choice_time=deepcopy(declared), time_basis=event.get('time_basis'))
    output = dict(choice_time=None, time_basis=_fallback_basis(event, primary), uncertainty=uncertainty,
                  original_proposal=original, choice_time_precision='unknown', choice_time_verified=False,
                  evidence=dict(accepted_time_quotes=accepted, rejected_time_quotes=rejected,
                                ambiguous_time_quotes=ambiguous, invalid_time_quotes=invalid,
                                explicit_time_statements=explicit, normalized_proposal=normalized_proposal))
    current_ids = set(event.get('source_ids') or []) | {event.get('source_id')}
    current_ids.discard(None)
    for citation in valid:
        span, error = _raw_span(citation, indexed)
        if error:
            rejected.append(dict(citation=deepcopy(citation), reason=error))
            continue
        if current_ids and span['source_id'] not in current_ids:
            rejected.append(dict(**span, reason='NOT_CURRENT_EVENT_SOURCE'))
            continue
        source = indexed[span['source_id']]
        if source.get('kind') in ('imaging', 'lab', 'pathology', 'quarantine'):
            rejected.append(dict(**span, reason='RESULT_SOURCE_NOT_EXPLICIT_CURRENT_CHOICE_TIME'))
            continue
        moments = _explicit_moments(span['quote'])
        if not moments:
            rejected.append(dict(**span, reason='NO_EXPLICIT_CURRENT_CHOICE_TIME_STATEMENT'))
            continue
        for moment in moments:
            explicit.append(dict(**span, moment=moment))
    performed = (event.get('kind') == 'performed_test_report' or event.get('document_profile') == 'performed_test_report'
                 or event.get('selection_basis') == 'performed_test_report' or (primary and primary.get('kind') == 'imaging'))
    if performed:
        output['time_basis'] = 'historical_unknown'
        uncertainty.append('PERFORMED_TEST_TIME_IS_NOT_ACTUAL_CHOICE_TIME')
        return output
    if event.get('time_basis') in ('record_time_proxy', 'operative_phase'):
        uncertainty.append('RECORD_TIME_PROXY_NOT_ACTUAL_CHOICE_TIME' if event['time_basis'] == 'record_time_proxy'
                           else 'OPERATIVE_PHASE_HAS_NO_EXACT_CHOICE_MINUTE')
        return output
    if declared is None:
        uncertainty.append('NO_ACTUAL_CHOICE_TIME_PROPOSED')
        return output
    if normalized_proposal is None:
        uncertainty.append('INVALID_OR_UNSUPPORTED_CHOICE_TIME_FORMAT')
        return output
    if not explicit:
        uncertainty.append('ACTUAL_CHOICE_TIME_UNSUPPORTED_BY_EXPLICIT_SOURCE_STATEMENT')
        if ambiguous:
            uncertainty.append('TIME_QUOTE_SPAN_AMBIGUOUS_NO_PRECISE_BOUNDARY_ASSIGNED')
        return output
    # These citations were all proposed as the current event's time. Conflicting
    # explicit clocks cannot be resolved by choosing the model's preferred one.
    if any(_conflicts(a['moment'], b['moment']) for i, a in enumerate(explicit) for b in explicit[i+1:]):
        uncertainty.append('CONFLICTING_EXPLICIT_CURRENT_CHOICE_TIMES')
        return output
    compatible = []
    for support in explicit:
        moment = support['moment']
        common_precision = min((moment['precision'], normalized_proposal['precision']), key=lambda p: _PRECISION[p])
        if _coarse(moment['value'], common_precision) == _coarse(normalized_proposal['value'], common_precision):
            compatible.append((support, common_precision))
        else:
            rejected.append(dict(**support, reason='PROPOSED_TIME_NOT_IN_EXPLICIT_STATEMENT'))
    if not compatible:
        uncertainty.append('PROPOSED_CHOICE_TIME_MISMATCHES_EXPLICIT_SOURCE_TIME')
        return output
    verified_precision = max((precision for _, precision in compatible), key=lambda p: _PRECISION[p])
    output.update(choice_time=_coarse(normalized_proposal['value'], verified_precision),
                  time_basis='explicit_documented_choice', choice_time_precision=verified_precision,
                  choice_time_verified=True)
    accepted.extend(support for support, _ in compatible)
    if _PRECISION[verified_precision] < _PRECISION[normalized_proposal['precision']]:
        uncertainty.append('MODEL_TIME_PRECISION_REDUCED_TO_EXPLICIT_SOURCE_PRECISION')
    if verified_precision == 'date':
        uncertainty.append('ACTUAL_CHOICE_DATE_ONLY_MINUTE_UNKNOWN')
    if invalid or rejected:
        uncertainty.append('SECONDARY_TIME_CITATIONS_REJECTED_VALID_SUPPORT_PRESERVED')
    return output
