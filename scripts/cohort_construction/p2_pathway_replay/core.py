"""Preserve sources, expose coverage, and compile reviewed clinical phases.

No outcome labels, numeric truth aggregation, P1 gate, or clinical correctness
judgments. All source families have an inventory; not retrieved != not present.
"""
import collections
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re

SOURCE_FAMILIES = ('documents_all', 'imaging_all', 'laboratory_original_L1_all',
                   'laboratory_source_quarantine', 'pathology_source_rows_all')
LAB_FIELDS = ('检验项目名称', '检验结果值', '检验结果单位', '结果正常标志',
              '送检时间', '报告时间', '检验参考值', '检验单号', '标本')
PATH_FIELDS = ('病理号', '标本类型', '标本名称', '临床诊断', '收到日期', '取材日期',
               '报告日期', '肉眼所见', '镜下所见', '病理诊断', '补充意见1', '补充意见2')

# CE-REM-001: map only explicitly named exported fields. Never parse an arbitrary
# first line as a release time, and never infer clinician reading from a clock.
IMAGING_TIME_FIELDS = {
    'exam_time': ('exam_datetime',),
    'report_time': ('report_time', 'report_datetime', '报告时间', '报告日期'),
    'report_date': ('report_date',),
    'issued': ('issued', 'issued_datetime', '签发时间'),
    'verified_time': ('verified_time', 'verified_datetime', '审核时间'),
}
IMAGING_HEADER_FIELDS = ('original_report_header', 'report_header')

def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def _wall_day_nanoseconds(value):
    """Return exact day and wall nanoseconds without timezone conversion."""
    day=(value.date()-datetime(1,1,1).date()).days
    return day,(value.hour*3600+value.minute*60+value.second)*1_000_000_000


def _parse_instant(text):
    """Return exact ISO fields; RFC3339 allows offset seconds and nanoseconds."""
    match=re.fullmatch(
        r'(?P<year>\d{4})-(?P<month>\d\d)-(?P<day>\d\d)[ T]'
        r'(?P<hour>\d\d):(?P<minute>\d\d)(?::(?P<second>\d\d))?'
        r'(?P<fraction>\.\d+)?(?P<offset>Z|[+-]\d\d:\d\d(?::\d\d)?)?',text)
    if match is None:
        return None
    numbers={key:int(match[key]) for key in ('year','month','day','hour','minute')}
    second=int(match['second'] or 0)
    if second==60:
        return None  # Leap seconds are not silently rolled into the next minute.
    if second>59:
        return None
    fraction=match['fraction'] or ''
    nanosecond=int((fraction[1:]+'000000000')[:9]) if fraction else 0
    if fraction and len(fraction)-1>9 and set(fraction[10:]) - {'0'}:
        return None  # Do not silently truncate precision beyond the stored format.
    offset=match['offset']
    if offset:
        offset_parts=[0 if offset=='Z' else int(offset[1:3])*60+int(offset[4:6]),
                      0 if offset=='Z' or len(offset)==6 else int(offset[7:9])]
        if offset_parts[0]>23*60+59 or offset_parts[1]>59:
            return None
        offset_seconds=(1 if offset=='Z' or offset[0]=='+' else -1)*(
            offset_parts[0]*60+offset_parts[1])
    else:
        offset_seconds=None
    return dict(numbers, second=second, nanosecond=nanosecond,
                offset_seconds=offset_seconds,
                precision='fractional_second' if fraction else
                    'second' if match['second'] is not None else 'minute')


def _compare_clocks(left,right):
    """Compare exact values without upgrading minute/date or precision lies."""
    a,b=_structured_clock(left),_structured_clock(right)
    if not a['value'] or not b['value']:
        return 'UNKNOWN_TIME'
    imprecise = 'date' in (a['precision'], b['precision']) or 'minute' in (a['precision'], b['precision'])
    if not imprecise:
        exact_a,exact_b=_exact_wall_time(a),_exact_wall_time(b)
        return 'BEFORE' if exact_a<exact_b else 'AFTER' if exact_a>exact_b else 'SAME_TIME_UNORDERED'
    # An imprecise timestamp denotes an interval, not its normalized lower bound.
    # Use the normalized UTC+8 start (including offset seconds) for both values;
    # raw re-parsing here would reintroduce timezone and precision divergence.
    exact_a,exact_b=_exact_wall_time(a),_exact_wall_time(b)
    interval_ns={'date':86_400_000_000_000,'minute':60_000_000_000}
    end_a=exact_a[0],exact_a[1]+interval_ns.get(a['precision'],0)
    end_b=exact_b[0],exact_b[1]+interval_ns.get(b['precision'],0)
    if end_a<=exact_b:
        return 'BEFORE'
    if end_b<=exact_a:
        return 'AFTER'
    return 'SAME_DAY_UNORDERED' if 'date' in (a['precision'],b['precision']) else 'SAME_MINUTE_UNORDERED'


def _exact_wall_time(value):
    """Exact wall day/nanoseconds for a validated normalized clock."""
    instant=_parse_instant(value['value'] + 'T00:00' if value['precision']=='date' else value['value'])
    if instant is None or instant['offset_seconds'] is not None:
        raise ValueError('Normalized clock must have no timezone offset')
    base=datetime(instant['year'],instant['month'],instant['day'],
                  instant['hour'],instant['minute'])
    day=(base.date()-datetime(1,1,1).date()).days
    return day,(instant['hour']*3600+instant['minute']*60+instant['second'])*1_000_000_000+instant['nanosecond']


def clock(raw):
    """Preserve source precision and normalize explicit offsets to local UTC+8.

    Naive values retain their supplied wall time. Offset-bearing values must not
    be silently stripped: doing so can admit future evidence or hide prior text.
    Exact ISO parsing preserves offset seconds and submicrosecond fractions;
    datetime is used only for calendar arithmetic, never precision truncation.
    """
    if raw is None or isinstance(raw, bool) or raw == '':
        return {'value': None, 'precision': 'unknown', 'raw': raw}
    if isinstance(raw, (int, float)):
        if not math.isfinite(raw) or not 1 <= raw <= 100000:
            return {'value': None, 'precision': 'invalid', 'raw': raw}
        # Excel's fractional day is a decimal source value, not an exact second.
        # Decimal(str(float)) preserves the supplied short decimal representation
        # instead of exposing binary multiplication noise. Do not round a near
        # integer into a date, or a subsecond into a whole-second timestamp.
        try:
            seconds = Decimal(str(raw)) * 86400
        except InvalidOperation:
            return {'value': None, 'precision': 'invalid', 'raw': raw}
        whole, fraction = divmod(seconds, 1)
        nanoseconds = fraction * 1_000_000_000
        # The shared clock grammar stores at most nanoseconds. Reject rather
        # than silently truncate a still finer nonzero fraction.
        if nanoseconds != nanoseconds.to_integral_value():
            return {'value': None, 'precision': 'invalid', 'raw': raw}
        day, remainder = divmod(int(whole), 86400)
        hour, remainder = divmod(remainder, 3600)
        minute, second = divmod(remainder, 60)
        try:
            date = datetime(1899, 12, 30) + timedelta(days=day)
        except (ValueError, OverflowError):
            return {'value': None, 'precision': 'invalid', 'raw': raw}
        if type(raw) is int:
            return {'value': f'{date:%Y-%m-%d}', 'precision': 'date', 'raw': raw}
        value = f'{date:%Y-%m-%d} {hour:02}:{minute:02}:{second:02}'
        if nanoseconds:
            value += '.' + f'{int(nanoseconds):09}'.rstrip('0')
        return {'value': value,
                'precision': 'fractional_second' if nanoseconds else 'second', 'raw': raw}
    text=str(raw).strip()
    s=text.replace('/','-').replace('T',' ')
    date_match=re.fullmatch(r'(\d{4})年(\d{1,2})月(\d{1,2})日',text)
    if date_match:
        s='-'.join((date_match[1],date_match[2].zfill(2),date_match[3].zfill(2)))
    else:
        date_match=re.fullmatch(r'(\d{4})[-/](\d\d)[-/](\d\d)',text)
    if date_match:
        try:datetime(*(int(part) for part in date_match.groups()))
        except ValueError:return {'value':None,'precision':'invalid','raw':raw}
        return {'value':'-'.join((date_match[1],date_match[2].zfill(2),date_match[3].zfill(2))),
                'precision':'date','raw':raw}
    parsed=_parse_instant(s)
    if parsed is None:
        return {'value':None,'precision':'invalid','raw':raw}
    try:
        value=datetime(parsed['year'],parsed['month'],parsed['day'],
                       parsed['hour'],parsed['minute'],parsed['second'])
        day,wall_ns=_wall_day_nanoseconds(value)
        wall_ns+=parsed['nanosecond']
        if parsed['offset_seconds'] is not None:
            # Subtract the exact offset in integer nanoseconds; offset seconds
            # and fractional nanoseconds must not disappear in UTC conversion.
            day_delta,wall_ns=divmod(wall_ns+(8*3600-parsed['offset_seconds'])*1_000_000_000,
                                    86_400_000_000_000)
            day+=day_delta
        date=datetime(1,1,1)+timedelta(days=day)
        hours,remainder=divmod(wall_ns,3_600_000_000_000)
        minutes,remainder=divmod(remainder,60_000_000_000)
        seconds,nanoseconds=divmod(remainder,1_000_000_000)
        base=f'{date:%Y-%m-%d} {hours:02}:{minutes:02}'
        if parsed['precision']=='minute':
            return {'value':base+':00','precision':'minute','raw':raw}
        base+=f':{seconds:02}'
        if nanoseconds:
            base+='.'+f'{nanoseconds:09}'.rstrip('0')
        return {'value':base,'precision':parsed['precision'],'raw':raw}
    except (ValueError,OverflowError):
        return {'value':None,'precision':'invalid','raw':raw}


def relation_to_cutoff(c,cutoff):
    """Compare with shared precision; never turn a minute into exact seconds."""
    return _compare_clocks(c,cutoff)

def body_record_clock(text):
    """Parse only an explicit record-time label or a document-anchored clock.

    CE-REM-002: a timestamp in history or another narrative field is not a
    document record clock. Conflicting labels remain unknown rather than being
    resolved by position or by a preferred date.
    """
    labeled = re.findall(
        r'记录时间[：:]\s*(20\d\d-\d\d-\d\d \d\d:\d\d(?::\d\d)?)', text)
    if len(set(labeled)) == 1:
        return clock(labeled[0])
    if len(set(labeled)) > 1:
        return clock(None)
    leading = re.match(r'\s*(20\d\d-\d\d-\d\d \d\d:\d\d(?::\d\d)?)', text)
    return clock(leading[1] if leading else None)


def fields_text(fields):
    """Display original cells with field labels; do not summarize clinical facts."""
    text='';spans={}
    for key,value in fields.items():
        text+=f'[{key}]\n';start=len(text)
        cell='' if value is None else str(value)
        text+=cell;spans[key]={'start':start,'end':len(text),'quote':cell};text+='\n'
    return text,spans


def imaging_projection(row):
    """Return explicit time/header fields and missingness without inventing data.

    Frozen exports without a named report-time field retain their original
    findings/impression text and source identity. Newly available report headers
    are separate original fields, not spliced into a clinical observation.
    """
    fields = {key: row.get(key) for key in ['original_findings', 'original_impression']}
    fields.update({key: row.get(key) for key in IMAGING_HEADER_FIELDS if key in row})
    clocks, provenance = {}, {}
    for role, aliases in IMAGING_TIME_FIELDS.items():
        present = [key for key in aliases if key in row]
        candidates = [(key, clock(row[key])) for key in present if row.get(key) not in (None, '')]
        values = {(key, parsed['value']) for key, parsed in candidates}
        valid = [(key, parsed) for key, parsed in candidates if parsed['value']]
        selected = valid[0][0] if valid else None
        clocks[role] = dict(valid[0][1] if valid else clock(None))
        distinct_values = {parsed['value'] for _, parsed in valid}
        if len(distinct_values) > 1:
            # Two populated named fields disagree: no silent alias precedence.
            clocks[role] = clock(None)
            status = 'CONFLICTING_NAMED_EXPORT_FIELDS_REVIEW_REQUIRED'
            selected = None
        else:
            status = 'MAPPED_FROM_NAMED_EXPORT_FIELD' if selected else \
                'NAMED_EXPORT_FIELD_INVALID' if candidates else \
                'FIELD_PRESENT_BUT_EMPTY' if present else 'NOT_MAPPED_NO_NAMED_FIELD_IN_PROVIDED_ROW'
        provenance[role] = dict(
            status=status, source_field=selected, candidate_fields=list(aliases),
            invalid_source_fields=[key for key, parsed in candidates if not parsed['value']],
            mapped_source_values=[dict(source_field=key, value=parsed['value'], raw=parsed['raw'])
                                  for key, parsed in candidates],
            conflict_values=sorted(value for value in distinct_values if value) if len(distinct_values) > 1 else [],
            missing_reason=None if selected else 'UNAVAILABLE_NOT_PROVEN_ABSENT',
            clinician_reading_certified=False)
    return fields, clocks, provenance


def project_sources(bundle, patient_uid):
    sources=[];inventory={k:len(bundle.get(k,[])) for k in SOURCE_FAMILIES}
    def emit(kind,family,identity,title,fields,clocks,provenance,flags=(),time_provenance=None):
        text,field_spans=fields_text(fields)
        sid=sha(patient_uid+'|'+family+'|'+str(identity)+'|'+sha(text))[:24]
        source = dict(source_id=sid,patient_uid=patient_uid,kind=kind,family=family,title=title,
                      text=text,source_sha256=sha(text),original_fields=fields,field_spans=field_spans,
                      clocks=clocks,provenance=provenance,flags=list(flags),availability_verified=False,
                      availability_certified=False,time_provenance=time_provenance or {},
                      clinician_reading_established=False)
        _, selected_clock, selected_basis, _ = available_source_clock(source)
        source.update(clock=selected_clock,
            clock_basis=selected_basis if kind == 'imaging' else
                'report_time_proxy' if kind in ('lab', 'pathology') else 'source_time_proxy')
        sources.append(source)
    for row in bundle.get('documents_all',[]):
        assert row.get('patient_uid',patient_uid)==patient_uid
        text=row.get('document_text') or '';key=str(row.get('source_record_key') or row.get('source_record_id'))
        sid=sha(patient_uid+'|'+key+'|'+sha(text))[:24]
        sources.append(dict(source_id=sid,patient_uid=patient_uid,kind='document',family='documents_all',title=row.get('文书名称'),
                            text=text,source_sha256=sha(text),clocks=dict(body_record_time=body_record_clock(text),source_create_time=clock(row.get('create_time') or row.get('CREATE_DATE_TIME')),encounter_admission_time=clock(row.get('admission_time') or row.get('ADMISSION_DATE_TIME'))),
                            provenance={k:row.get(k) for k in ['source_record_id','source_record_key','content_sha256']},flags=[],availability_verified=False))
    for row in bundle.get('imaging_all',[]):
        assert row.get('patient_uid',patient_uid)==patient_uid
        fields, clocks, time_provenance = imaging_projection(row)
        emit('imaging','imaging_all',row.get('index_report_uid'),row.get('exam_method'),
             fields, clocks,
             {k:row.get(k) for k in ['index_report_uid','exam_no_raw','source_file_name','record_cluster_id','raw_record_path']},
             time_provenance=time_provenance)
    for row in bundle.get('laboratory_original_L1_all',[]):
        assert row.get('patient_uid',patient_uid)==patient_uid
        emit('lab','laboratory_original_L1_all',row.get('source_record_id'),row.get('检验项目名称'),
             {k:row.get(k) for k in LAB_FIELDS},
             dict(sample_time=clock(row.get('送检时间')),report_time=clock(row.get('报告时间'))),
             {k:row.get(k) for k in ['source_record_id','source_workbook','source_sheet','source_row','row_hash']})
    for row in bundle.get('laboratory_source_quarantine',[]):
        # Keep provenance for coverage, never promote quarantine to clean fact.
        emit('quarantine','laboratory_source_quarantine',row.get('source_record_id'),'原始检验隔离记录',
             {'raw_values_json':row.get('raw_values_json')},dict(report_time=clock(None)),
             {k:row.get(k) for k in ['source_record_id','source_workbook','source_sheet','source_row','quarantine_reason']},['ORIGINAL_SOURCE_QUARANTINE_REVIEW_REQUIRED'])
    for row in bundle.get('pathology_source_rows_all',[]):
        # Gate the clinical text later by original report date. No disease_label.
        emit('pathology','pathology_source_rows_all',row.get('source_record_key'),'原始病理报告',
             {k:row.get(k) for k in PATH_FIELDS},
             dict(report_time=clock(row.get('报告日期')),specimen_time=clock(row.get('取材日期'))),
             {k:row.get(k) for k in ['source_record_key','source_file','source_sheet','source_row','row_hash']})
    if len({s['source_id'] for s in sources})!=len(sources):
        raise ValueError('Duplicate source identities: preserve/resolve row provenance explicitly')
    return sources,inventory

def cite(source,quote,start=None):
    t=source['text']
    if start is None:
        hits=[m.start() for m in re.finditer(re.escape(quote),t)]
        if len(hits)!=1:raise ValueError(('ambiguous quote',source['source_id'],quote[:60],hits))
        start=hits[0]
    end=start+len(quote)
    if not quote or t[start:end]!=quote:raise ValueError('Quote mismatch')
    return dict(source_id=source['source_id'],source_sha256=source['source_sha256'],start=start,end=end,quote=quote)

def operative_partition(source):
    """Find a documented stage pattern; no marker => semantic review, not drop."""
    t=source['text'];exp=t.find('探查：');choice=t.find('遂决定',exp)
    if exp<0 or choice<0 or t.count('遂决定')!=1:
        return dict(status='SEMANTIC_REVIEW_REQUIRED',reason='No unique regular stage pattern; source retained.',source_id=source['source_id'])
    begin=t.find('    麻醉成功后')
    diagnosis=t.rfind('术中诊断：',exp,choice)
    after=[t.find(x,choice) for x in ['\n    切除：','\n    解剖胰腺上缘']]
    after=[i for i in after if i>=0]
    if begin<0 or diagnosis<0 or not after or not begin<exp<diagnosis<choice<min(after):
        return dict(status='SEMANTIC_REVIEW_REQUIRED',reason='Stage order unresolved; source retained.',source_id=source['source_id'])
    execution=min(after)
    segments=[]
    for start,end,disposition,reason in [(0,begin,'MASK','Current procedure metadata/header'),(begin,diagnosis,'KEEP','Prior access/exploration, including adverse findings'),(diagnosis,execution,'MASK','Current assessment/actual choice'),(execution,len(t),'MASK','Downstream execution/outcome for this cutoff; still available for later-point review')]:
        segments.append(dict(start=start,end=end,quote=t[start:end],disposition=disposition,reason=reason))
    return dict(status='STAGE_PATTERN_PROPOSAL',source_id=source['source_id'],segments=segments,clinical_phase='after_exploration_before_current_choice',source_timing='retrospective_stage_reconstruction',late_content_review_span=cite(source,t[execution:],execution))

def validate_partition(source,segments):
    p=0
    for s in segments:
        assert s['start']==p and s['end']>p and source['text'][p:s['end']]==s['quote']
        assert s['disposition'] in ['KEEP','MASK','REVIEW'];p=s['end']
    assert p==len(source['text'])

def _available_clock(source):
    """Compatibility wrapper; all temporal consumers share the same resolver."""
    role, value, _, _ = available_source_clock(source)
    return role, value


RELEASE_ROLES = ('report_time', 'report_date', 'issued')
RELEASE_UNRESOLVED_STATUSES = {
    'NAMED_EXPORT_FIELD_INVALID', 'CONFLICTING_NAMED_EXPORT_FIELDS_REVIEW_REQUIRED'}


def release_time_provenance(source):
    """Keep populated-but-invalid release fields distinct from absent aliases.

    An exam clock may be a weak proxy only when no supplied release value was
    invalidated or contradicted. Otherwise time gating must remain unresolved;
    the full report and its execution anchor are still preserved.
    """
    clocks = source.get('clocks') or {}
    provenance = source.get('time_provenance') or {}
    unresolved = []
    release_values = {}
    for role in RELEASE_ROLES:
        supplied = clocks.get(role)
        c = _structured_clock(supplied)
        if (supplied or {}).get('precision') == 'invalid' \
                or ((supplied or {}).get('value') is not None and c['value'] is None):
            unresolved.append('INVALID_STRUCTURED_RELEASE_CLOCK:' + role)
        elif c['value'] is not None:
            release_values[role] = c['value']
        status = (provenance.get(role) or {}).get('status')
        if status in RELEASE_UNRESOLVED_STATUSES:
            unresolved.append(status + ':' + role)
    if len(set(release_values.values())) > 1:
        unresolved.append('CONFLICTING_RELEASE_ROLES_REVIEW_REQUIRED:' +
                          ','.join(sorted(release_values)))
    return unresolved


def _structured_clock(value):
    """Validate raw/normalized agreement before trusting declared precision."""
    if not isinstance(value, dict):
        return clock(value)
    invalid = dict(value=None, precision='invalid',
                   raw=value.get('raw', value.get('value')))
    if value.get('value') is None:
        return clock(None) if value.get('precision') not in ('invalid', 'unknown') else dict(value)
    normalized = clock(value['value'])
    if not normalized['value']:
        return invalid
    raw = value.get('raw')
    if isinstance(raw, (dict, list)):
        return invalid
    if raw is not None:
        parsed_raw = clock(raw)
        if not parsed_raw['value']:
            return invalid
        if parsed_raw['value'] != normalized['value']:
            return invalid
        # A minute raw deliberately normalizes to :00 while retaining minute
        # precision. It cannot be reclassified as an exact second value.
        if parsed_raw['precision'] == 'minute' and normalized['precision'] == 'second':
            normalized['precision'] = 'minute'
        elif parsed_raw['precision'] != normalized['precision']:
            # A supplied fractional part can be exactly zero. Its normalized
            # value has no suffix, but its source precision remains fractional;
            # reparsing the normalized value must not invalidate that fact.
            if not (parsed_raw['precision'] == 'fractional_second'
                    and normalized['precision'] == 'second'):
                return invalid
            normalized['precision'] = 'fractional_second'
    if value.get('precision') == normalized['precision']:
        return dict(value)
    if 'precision' not in value:
        normalized['raw'] = raw if raw is not None else value['value']
        return normalized
    return invalid


def available_source_clock(source, *, document_record_time=None):
    """Resolve one availability proxy without upgrading its stored precision.

    ``document_record_time`` is the v04 narrow signature-day adapter. The core
    replay layer does not import that higher-level module; callers which already
    own it must inject it rather than dropping its record-clock fallback.
    """
    clocks = source.get('clocks') or {}
    if source.get('kind') in ('lab', 'pathology') and not clocks:
        # Legacy interfaces supplied only source.clock. Keep that explicitly
        # weak report-time proxy; projected/FHIR sources have execution clocks
        # and may not silently reinterpret them as publication time.
        record = source.get('clock')
        if isinstance(record, dict) and record.get('value') is not None:
            return 'legacy_report_time', _structured_clock(record), source.get(
                'clock_basis', 'source_time_proxy'), []
    if source.get('kind') in ('imaging', 'lab', 'pathology'):
        unresolved = release_time_provenance(source)
        if unresolved:
            return None, clock(None), 'release_time_unresolved_exam_fallback_forbidden', unresolved
        for role in RELEASE_ROLES:
            if (clocks.get(role) or {}).get('value'):
                return role, _structured_clock(clocks[role]), role + '_proxy', []
        if source.get('kind') == 'imaging':
            exam = _structured_clock(clocks.get('exam_time') or clock(None))
            if exam['value']:
                return 'exam_time', exam, 'exam_time_proxy_release_unknown', []
            # Compatibility only for legacy v04 sources which never received
            # separate execution clocks. Native projected/FHIR report objects
            # always have clocks and must not substitute execution for release.
            if 'exam_time' not in clocks and not source.get('time_provenance'):
                record = source.get('clock')
                if isinstance(record, dict) and record.get('value') is not None:
                    return 'legacy_exam_time', _structured_clock(record), \
                        'exam_time_proxy_release_unknown', []
            return 'exam_time', exam, 'exam_time_proxy_release_unknown', []
        return 'report_time', clock(None), 'unknown', []
    if source.get('kind') == 'document':
        # Let the shared adapter normalize a legacy raw/value clock and decide
        # whether a signature-day fallback is applicable. It cannot overwrite a
        # valid body/create clock.
        if document_record_time is not None:
            record = document_record_time(source)
            status = record.get('status')
            if status not in ('NOT_APPLICABLE', 'STRUCTURED_CLOCK_PRESERVED', 'PHYSICIAN_SIGNATURE_RECORD_DAY_PROXY'):
                return 'source_record_time', clock(None), \
                    'source_record_time_unresolved:' + str(status), \
                    ['SOURCE_RECORD_TIME_UNRESOLVED:' + str(status)]
            return 'source_record_time', _structured_clock(record['clock']), record['clock_basis'], []
        for role in ('body_record_time', 'source_create_time'):
            if (clocks.get(role) or {}).get('value'):
                return role, _structured_clock(clocks[role]), role, []
        record = source.get('clock')
        if isinstance(record, dict) and record.get('value') is not None:
            return 'source_record_time', _structured_clock(record), source.get(
                'clock_basis', 'source_time_proxy'), []
    # Old source interfaces may expose only source.clock. Preserve that legacy
    # report-time proxy only when no structured execution/publication clocks
    # exist; native projected/FHIR sources cannot substitute execution time.
    if source.get('kind') not in ('document', 'imaging') and not clocks:
        record = source.get('clock')
        if isinstance(record, dict) and record.get('value') is not None:
            return 'legacy_report_time', _structured_clock(record), source.get(
                'clock_basis', 'source_time_proxy'), []
    return 'report_time', clock(None), 'unknown', []


def resource_selection(source,cutoff,interval_start,document_decision=None):
    """Every source receives a reason; a family cannot silently vanish."""
    base=dict(source_id=source['source_id'],kind=source['kind'],disposition='REVIEW',reason='',clock_basis=None)
    kind=source['kind'];cs=source['clocks']
    if kind=='document':
        if document_decision:return dict(base,**document_decision)
        return dict(base,disposition='REVIEW',reason='DOCUMENT_SEMANTIC_SCOPE_NOT_YET_REVIEWED')
    if kind=='quarantine':return dict(base,reason='ORIGINAL_SOURCE_QUARANTINE_REVIEW:'+str(source['provenance'].get('quarantine_reason')))
    basis, c, clock_basis, unresolved = available_source_clock(source)
    if unresolved:
        return dict(base,disposition='REVIEW',clock_basis=clock_basis,
            clock_value=None,clock_precision='unknown',
            reason='RELEASE_TIME_UNRESOLVED_EXAM_PROXY_FORBIDDEN',
            release_time_provenance_errors=unresolved,source_text_preserved=True)
    order=relation_to_cutoff(c,cutoff)
    base.update(clock_basis=basis,clock_value=c['value'],clock_precision=c['precision'],
        clock_basis_detail=clock_basis)
    if order=='AFTER':return dict(base,disposition='EXCLUDE',reason='AFTER_TARGET_PHASE')
    if order!='BEFORE':return dict(base,reason=order)
    if c['value'][:10]<interval_start[:10]:
        return dict(base,disposition='REVIEW',reason='EARLIER_CONTEXT_NOT_SILENTLY_DISCARDED')
    if kind=='imaging':
        return dict(base,disposition='INCLUDE',reason='EXAM_BEFORE_TARGET_RECONSTRUCTION_PROXY_NOT_RELEASE_CERTIFICATION')
    return dict(base,disposition='INCLUDE',reason='ORIGINAL_REPORT_CLOCK_BEFORE_TARGET_NOT_ACCESS_CERTIFICATION')

def exact_duplicate_groups(sources):
    """Group exact same-content report copies; retain every original provenance."""
    groups=collections.defaultdict(list)
    for s in sources:
        if s['kind']=='imaging':
            key=(s['kind'],s['source_sha256'],
                 tuple((role, s['clocks'].get(role, {}).get('value'))
                       for role in ('exam_time',) + RELEASE_ROLES),s['title'])
        else:key=(s['source_id'],)
        groups[key].append(s['source_id'])
    return [v for v in groups.values() if len(v)>1]

def validate_assessment_round(previous_condition, current_assessment, choice, new_evidence, sources, cutoff):
    """Validate a semantic proposal; neither new labs nor a date alone create a point."""
    if not previous_condition or not current_assessment or not choice or not new_evidence:
        return dict(eligible=False,reason='NO_SUPPORTED_NEW_ASSESSMENT_ROUND')
    byid={s['source_id']:s for s in sources}
    for a in new_evidence:
        s=byid[a['source_id']]
        assert a['quote']==s['text'][a['start']:a['end']]
        if s['kind'] not in ['imaging','lab','pathology']:
            return dict(eligible=False,reason='REQUIRES_EXPLICIT_SEMANTIC_EVIDENCE_REVIEW')
        basis, c, _, unresolved = available_source_clock(s)
        if unresolved or relation_to_cutoff(c,cutoff)!='BEFORE':
            result = dict(eligible=False,reason='NEW_EVIDENCE_NOT_ORDERED_BEFORE_CURRENT_PHASE')
            if unresolved:
                result['release_time_provenance_errors'] = unresolved
            return result
    return dict(eligible=True,reason='SEMANTICALLY_REVIEWED_NEW_EVIDENCE_AND_CURRENT_CONDITION_ASSESSMENT',
                historical_subjective_causality_proven=False)

def compile_view(sources,source_spans,forbidden_current_quotes=()):
    byid={s['source_id']:s for s in sources};blocks=[];audit=[]
    for sid,spans in source_spans.items():
        s=byid[sid];passages=[]
        for a in spans:
            assert a['source_sha256']==s['source_sha256']
            assert a['quote']==s['text'][a['start']:a['end']]
            if any(q and q in a['quote'] for q in forbidden_current_quotes):
                raise ValueError(('current answer remains in selected passage',sid))
            passages.append(a['quote'])
        if not passages:continue
        alias=f'S{len(blocks)+1:03}'
        blocks.append(dict(source=alias,source_kind=s['kind'],passages=passages))
        audit.append(dict(source_alias=alias,source_id=sid,source_sha256=s['source_sha256'],spans=spans,clocks=s['clocks']))
    return blocks,audit
