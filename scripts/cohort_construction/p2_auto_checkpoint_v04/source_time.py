"""Source-bound request signature dates, separate from actual choice time.

V04-TIMEINDEX001: an endoscopy request can have an empty exported record clock
while its physician signature gives a record day. Only that specific document
role and signature layout are supported. This helper never changes the raw
source or promotes a signature, appointment, creation, or admission clock into
an actual clinical decision timestamp.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import re

from scripts.cohort_construction.p2_pathway_replay.core import clock as parse_clock


RULE_ID = 'V04-TIMEINDEX001'
_REQUEST_TITLE = re.compile(r'^(?:汇总[:：])?消化内镜检查申请单$')
# A date must occupy the next nonblank line after the physician signature.
# Dates in the history, a scheduling remark, or a free-form future appointment
# cannot match. Match only the footer role, not a particular doctor's identity.
_SIGNATURE_DAY = re.compile(
    r'(?P<signature>(?:申请)?医[师生](?:签名)?[ \t　]*[：:]?[^\r\n]{1,60})'
    r'\r?\n(?:[ \t　]*\r?\n)*[ \t　]*'
    r'(?P<date>(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日)'
    r'[ \t　]*(?=\r?\n|$)'
)


def _clock(value):
    if isinstance(value, dict):
        if value.get('value') is None:
            return dict(value=None, precision=value.get('precision', 'unknown'), raw=value.get('raw'))
        if value.get('precision'):
            return deepcopy(value)
        return parse_clock(value['value'])
    return parse_clock(value)


def source_record_time(source):
    """Return an effective retrieval clock with raw clock and exact provenance.

    A returned date has precision ``date`` and supports only before/after-day
    retrieval. Its actual clinical choice time remains unknown; same-day order
    must remain unresolved. Multiple distinct signature days or a source hash
    mismatch preserve an unknown clock instead of choosing a preferred date.
    Existing structured clocks always retain precedence and are not corrected
    or replaced by this narrow missing-clock adapter.
    """
    raw_clock = deepcopy(source.get('clock'))
    clock = _clock(raw_clock)
    result = dict(rule_id=RULE_ID, clock=clock,
                  clock_basis=source.get('clock_basis', 'source_time_proxy'),
                  raw_clock=raw_clock, raw_clock_basis=source.get('clock_basis'),
                  status='STRUCTURED_CLOCK_PRESERVED' if clock['value'] else 'NOT_APPLICABLE',
                  actual_choice_time_established=False, availability_certified=False,
                  supports=[], uncertainty=[])
    if clock['value']:
        return result
    title = re.sub(r'\s+', '', source.get('title') or '')
    if source.get('kind') != 'document' or not _REQUEST_TITLE.fullmatch(title):
        return result
    result['status'] = 'REQUEST_SIGNATURE_DAY_NOT_FOUND'
    text = source.get('text')
    if not isinstance(text, str):
        result['uncertainty'].append('REQUEST_RAW_TEXT_UNAVAILABLE')
        return result
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if source.get('source_sha256') not in (None, digest):
        result['status'] = 'REQUEST_SIGNATURE_DAY_SOURCE_HASH_MISMATCH'
        result['uncertainty'].append('SOURCE_HASH_MISMATCH_NO_SIGNATURE_PROXY_ASSIGNED')
        return result
    dates = set()
    for match in _SIGNATURE_DAY.finditer(text):
        signatory = re.sub(r'^(?:申请)?医[师生](?:签名)?[ \t　：:]*', '', match['signature']).strip()
        if not signatory:
            result['uncertainty'].append('PHYSICIAN_SIGNATURE_NAME_FIELD_EMPTY')
            continue
        try:
            day = datetime(int(match['year']), int(match['month']), int(match['day'])).strftime('%Y-%m-%d')
        except ValueError:
            result['uncertainty'].append('INVALID_PHYSICIAN_SIGNATURE_DATE')
            continue
        dates.add(day)
        result['supports'].append(dict(
            source_id=source.get('source_id'), source_sha256=digest,
            start=match.start('date'), end=match.end('date'), quote=match['date'],
            normalized_day=day,
            signature_context=dict(start=match.start('signature'), end=match.end('date'),
                                   quote=text[match.start('signature'):match.end('date')]),
        ))
    if len(dates) > 1:
        result['status'] = 'REQUEST_SIGNATURE_DAYS_CONFLICT'
        result['uncertainty'].append('CONFLICTING_PHYSICIAN_SIGNATURE_DAYS_NO_PROXY_ASSIGNED')
        return result
    if not dates:
        result['uncertainty'].append('NO_UNAMBIGUOUS_PHYSICIAN_SIGNATURE_RECORD_DAY')
        return result
    result.update(status='PHYSICIAN_SIGNATURE_RECORD_DAY_PROXY',
                  clock=dict(value=next(iter(dates)), precision='date', raw=result['supports'][0]['quote']),
                  clock_basis='request_physician_signature_record_day_proxy')
    result['uncertainty'].extend([
        'SIGNATURE_DATE_IS_RECORD_DAY_PROXY_NOT_ACTUAL_CHOICE_TIME',
        'SAME_DAY_CLINICAL_ORDER_UNRESOLVED',
        'SOURCE_AVAILABILITY_NOT_CERTIFIED_BY_SIGNATURE_DATE',
    ])
    return result
