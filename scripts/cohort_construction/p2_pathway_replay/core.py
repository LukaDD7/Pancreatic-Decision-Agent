"""Preserve sources, expose coverage, and compile reviewed clinical phases.

No outcome labels, numeric truth aggregation, P1 gate, or clinical correctness
judgments. All source families have an inventory; not retrieved != not present.
"""
import collections
from datetime import datetime, timedelta
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

def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def clock(raw):
    """Raw original Excel/string clock with explicit precision; no midnight fill."""
    if raw is None or isinstance(raw, bool) or raw == '':
        return {'value': None, 'precision': 'unknown', 'raw': raw}
    if isinstance(raw, (int, float)):
        if not math.isfinite(raw) or not 1 <= raw <= 100000:
            return {'value': None, 'precision': 'invalid', 'raw': raw}
        value=datetime(1899,12,30)+timedelta(seconds=round(raw*86400))
        precision='date' if abs(raw-round(raw))<1e-10 else 'second'
        return {'value':value.strftime('%Y-%m-%d' if precision=='date' else '%Y-%m-%d %H:%M:%S'), 'precision':precision,'raw':raw}
    s=str(raw).strip().replace('T',' ')
    if re.fullmatch(r'\d{4}[-/]\d\d[-/]\d\d',s):
        try:datetime.strptime(s.replace('/','-'),'%Y-%m-%d')
        except ValueError:return {'value':None,'precision':'invalid','raw':raw}
        return {'value':s.replace('/','-'),'precision':'date','raw':raw}
    try:
        value=datetime.fromisoformat(s)
        if re.search(r'\d\d:\d\d',s):
            return {'value':value.strftime('%Y-%m-%d %H:%M:%S'),'precision':'second' if re.search(r':\d\d:\d\d',s) else 'minute','raw':raw}
    except ValueError:pass
    return {'value':None,'precision':'invalid','raw':raw}

def relation_to_cutoff(c,cutoff):
    if not c['value']:return 'UNKNOWN_TIME'
    if c['precision']=='date':
        return 'BEFORE' if c['value']<cutoff[:10] else ('AFTER' if c['value']>cutoff[:10] else 'SAME_DAY_UNORDERED')
    return 'BEFORE' if c['value']<cutoff else ('AFTER' if c['value']>cutoff else 'SAME_TIME_UNORDERED')

def fields_text(fields):
    """Display original cells with field labels; do not summarize clinical facts."""
    text='';spans={}
    for key,value in fields.items():
        text+=f'[{key}]\n';start=len(text)
        cell='' if value is None else str(value)
        text+=cell;spans[key]={'start':start,'end':len(text),'quote':cell};text+='\n'
    return text,spans

def project_sources(bundle, patient_uid):
    sources=[];inventory={k:len(bundle.get(k,[])) for k in SOURCE_FAMILIES}
    def emit(kind,family,identity,title,fields,clocks,provenance,flags=()):
        text,field_spans=fields_text(fields)
        sid=sha(patient_uid+'|'+family+'|'+str(identity)+'|'+sha(text))[:24]
        sources.append(dict(source_id=sid,patient_uid=patient_uid,kind=kind,family=family,title=title,
                            text=text,source_sha256=sha(text),original_fields=fields,field_spans=field_spans,
                            clocks=clocks,provenance=provenance,flags=list(flags),availability_verified=False))
    for row in bundle.get('documents_all',[]):
        assert row.get('patient_uid',patient_uid)==patient_uid
        text=row.get('document_text') or '';key=str(row.get('source_record_key') or row.get('source_record_id'))
        sid=sha(patient_uid+'|'+key+'|'+sha(text))[:24]
        m=re.search(r'(20\d\d)[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?[\s　]*(\d{1,2}):(\d{2})',text[:120])
        body=f'{int(m[1]):04}-{int(m[2]):02}-{int(m[3]):02} {int(m[4]):02}:{int(m[5]):02}:00' if m else None
        sources.append(dict(source_id=sid,patient_uid=patient_uid,kind='document',family='documents_all',title=row.get('文书名称'),
                            text=text,source_sha256=sha(text),clocks=dict(body_record_time=clock(body),source_create_time=clock(row.get('create_time') or row.get('CREATE_DATE_TIME')),encounter_admission_time=clock(row.get('admission_time') or row.get('ADMISSION_DATE_TIME'))),
                            provenance={k:row.get(k) for k in ['source_record_id','source_record_key','content_sha256']},flags=[],availability_verified=False))
    for row in bundle.get('imaging_all',[]):
        assert row.get('patient_uid',patient_uid)==patient_uid
        emit('imaging','imaging_all',row.get('index_report_uid'),row.get('exam_method'),
             {k:row.get(k) for k in ['original_findings','original_impression']},
             dict(exam_time=clock(row.get('exam_datetime')),report_time=clock(None)),
             {k:row.get(k) for k in ['index_report_uid','exam_no_raw','source_file_name','record_cluster_id','raw_record_path']})
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

def resource_selection(source,cutoff,interval_start,document_decision=None):
    """Every source receives a reason; a family cannot silently vanish."""
    base=dict(source_id=source['source_id'],kind=source['kind'],disposition='REVIEW',reason='',clock_basis=None)
    kind=source['kind'];cs=source['clocks']
    if kind=='document':
        if document_decision:return dict(base,**document_decision)
        return dict(base,disposition='REVIEW',reason='DOCUMENT_SEMANTIC_SCOPE_NOT_YET_REVIEWED')
    if kind=='quarantine':return dict(base,reason='ORIGINAL_SOURCE_QUARANTINE_REVIEW:'+str(source['provenance'].get('quarantine_reason')))
    basis='report_time' if kind in ['lab','pathology'] else 'exam_time'
    c=cs[basis];order=relation_to_cutoff(c,cutoff)
    base.update(clock_basis=basis,clock_value=c['value'],clock_precision=c['precision'])
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
            key=(s['kind'],s['source_sha256'],s['clocks']['exam_time']['value'],s['title'])
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
        c=s['clocks']['exam_time' if s['kind']=='imaging' else 'report_time']
        if relation_to_cutoff(c,cutoff)!='BEFORE':
            return dict(eligible=False,reason='NEW_EVIDENCE_NOT_ORDERED_BEFORE_CURRENT_PHASE')
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
