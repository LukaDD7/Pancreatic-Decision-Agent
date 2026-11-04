"""Build a declared historical-state reconstruction with a separate audit trail.

Clinical source blocks stay separate. Existing actions are history; the action
at the evaluation anchor is withheld. Event time is NOT certified availability.
No pathology/outcome/cohort-derived fields enter the agent contract.
"""
from datetime import datetime
import re


def precise_time(value):
    """Date-only values do not silently become midnight."""
    if not value or not re.search(r'\d{1,2}:\d{2}',str(value)):
        return None
    try:return datetime.fromisoformat(str(value))
    except ValueError:return None


def split_at_action(text, marker):
    if text.count(marker)!=1:
        raise ValueError('Anchor action marker must occur exactly once')
    start=text.index(marker)
    return text[:start], {'action_start':start,'action_end':len(text)}


def redact_view(text, values):
    """Mask identifiers in the presentation copy; retain originals in audit."""
    for value in sorted(set(values),key=len,reverse=True):
        if value:text=text.replace(value,'[身份信息已隐去]')
    text=re.sub(r'家住[^。\n]+。','家住[身份信息已隐去]。',text)
    text=re.sub(r'(?m)^.*(?:现居住地址|患者联系方式|联系方式)[：:].*$','[身份信息已隐去]',text)
    return text


def select_reported_labs(rows, cutoff):
    """Copy raw L1 fields; no result coercion or selective bilirubin-only feed."""
    selected=[];audit=[]
    for i,row in enumerate(rows):
        reported=precise_time(row.get('报告时间'))
        keep=bool(reported and reported<cutoff)
        audit.append({'input_row':i,'source_record_id':row.get('source_record_id'),
                      'report_time':row.get('报告时间'),'included':keep,
                      'reason':'BEFORE_CUTOFF_REPORT_TIME_PROXY' if keep else
                      ('MISSING_OR_DATE_ONLY_REPORT_TIME' if not reported else 'NOT_BEFORE_CUTOFF')})
        if keep:
            selected.append({'item':row.get('检验项目名称'),'result_raw':row.get('检验结果值'),
                             'unit_raw':row.get('检验结果单位'),'reference_raw':row.get('检验参考值'),
                             'abnormal_flag_raw':row.get('结果正常标志'),
                             'specimen_time':row.get('送检时间'),'report_time':row.get('报告时间'),
                             'source_record_id':row.get('source_record_id')})
    return sorted(selected,key=lambda r:(r['report_time'],r['source_record_id'] or '')),audit


def document_block(note, text, values, span_end=None):
    return {'source_id':note['record_key'],'source_kind':'clinical_document',
            'document_type':note['document_type'],'body_time':note['body_time'],
            'record_create_time':note['system_time'],
            'source_span':[0,len(text) if span_end is None else span_end],
            'text':redact_view(text,values),
            'time_basis':'BODY_HEADER_OR_CREATE_TIME_PROXY_NOT_CERTIFIED_AVAILABILITY'}


def visibility_status(body_time,create_time,cutoff):
    body=precise_time(body_time);created=precise_time(create_time)
    return {'body_precedes_cutoff':bool(body and body<cutoff),
            'create_precedes_cutoff':bool(created and created<cutoff),
            'availability_certified':False,
            'caution':'Late creation is not proof of late clinical knowledge; early creation does not certify unchanged final content.'}
