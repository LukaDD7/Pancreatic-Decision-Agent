"""Describe literal labels x document types x nearby records, not decision times."""
from pathlib import Path
from collections import Counter, defaultdict
import argparse, datetime as dt, hashlib, json, re, statistics


PATTERNS = {
    'PLAN_CURATIVE_PANCREAS': r'(?:拟|建议|考虑|计划)[^。；;\n]{0,50}(?:胰[^。；;\n]{0,16}根治(?:术|性切除|性大部切除)|胰[^。；;\n]{0,10}根治性[^。；;\n]{0,10}切除)',
    'INDICATION_YES': r'有手术指征|存在手术指征|手术指征明确',
    'INDICATION_NO': r'无手术指征|(?:不宜|不适合|无法|不能)(?:行|进行)?[^。；;\n]{0,8}(?:手术治疗|外科手术切除)',
    'PAST_INDICATION_NO': r'当时无手术指征',
    'SURGERY_INTENT_GENERIC': r'拟行手术治疗|建议[：:]?手术治疗|拟限期[^。；;\n]{0,12}手术|择期手术|限期手术',
    'NAT_INTENT': r'(?:拟|建议|考虑|先行)[^。；;\n]{0,12}(?:术前新辅助|新辅助治疗|新辅助化疗)',
    'FITNESS_NO_CONTRAINDICATION': r'(?:无|未见|排除)[^。；;\n]{0,5}手术禁忌',
}


def family(title):
    if '术前小结' in title:return '术前小结'
    if '术前讨论' in title:return '术前讨论'
    if '会诊' in title:return '会诊记录'
    if '首次病程' in title and '术后' not in title:return '首次病程'
    if '首次查房' in title or '主诊首次查房' in title or '主治首次查房' in title:return '首次上级查房'
    if '入院记录' in title:return '入院记录'
    if '出院' in title:return '出院文书'
    if '术后' in title:return '术后病程'
    if '查房' in title:return '其他查房'
    return '其他文书'


def body_time(text):
    m=re.match(r'\s*(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})日?\s+(\d{1,2})[:：](\d{2})(?:[:：](\d{2}))?',text)
    if not m:return None
    try:return dt.datetime(*[int(x or 0) for x in m.groups()])
    except ValueError:return None


def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--input',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    raw=[json.loads(s) for s in args.input.read_text().splitlines() if s.strip()]
    compiled={k:re.compile(p) for k,p in PATTERNS.items()};rows=[];hits=[];cross=defaultdict(Counter)
    for i,r in enumerate(raw):
        r={**r,'row_id':i,'family':family(r['document_type']),'labels':[],'body_time':body_time(r['text'])}
        try:r['system_time']=dt.datetime.fromisoformat(r['recorded_datetime'])
        except ValueError:r['system_time']=None
        for label,p in compiled.items():
            found=list(p.finditer(r['text']))
            if not found:continue
            r['labels'].append(label);cross[label][r['family']]+=1
            for m in found:
                hits.append({'row_id':i,'patient_uid':r['patient_uid'],'visit_id':r['visit_id'],
                    'label':label,'document_type':r['document_type'],'family':r['family'],
                    'body_time':str(r['body_time'] or ''),'system_time':str(r['system_time'] or ''),
                    'source_record_id':r['source_record_id'],'body_sha256':r['body_sha256'],
                    'start':m.start(),'end':m.end(),'match':m.group(0),
                    'context':r['text'][max(0,m.start()-45):min(len(r['text']),m.end()+90)]})
        rows.append(r)
    summaries={}
    for label in PATTERNS:
        rr=[r for r in rows if label in r['labels']]
        summaries[label]={'notes':len(rr),'patients':len({r['patient_uid'] for r in rr}),
            'patient_visit_groups':len({(r['patient_uid'],r['visit_id']) for r in rr if r['visit_id']}),
            'document_type_families':dict(cross[label])}
    groups=defaultdict(list)
    for r in rows:
        if r['visit_id']:groups[(r['patient_uid'],r['visit_id'])].append(r)
    comparisons={};cards=[]
    for clock in ['body_time','system_time']:
        anchors=[];deltas=defaultdict(list);covered=Counter()
        for (uid,visit),rr in groups.items():
            candidates=[r for r in rr if 'PLAN_CURATIVE_PANCREAS' in r['labels'] and r['family'] in ['术前小结','术前讨论'] and r[clock]]
            if not candidates:continue
            anchor=min(candidates,key=lambda r:(r[clock],r['row_id']));anchors.append(anchor)
            for name,families in [('首次病程',['首次病程']),('首次上级查房',['首次上级查房']),('会诊记录',['会诊记录']),('更早同类根治计划',['首次病程','首次上级查房','会诊记录','入院记录','其他查房'])]:
                pool=[r for r in rr if r['row_id']!=anchor['row_id'] and r['family'] in families and r[clock] and r[clock]<=anchor[clock]]
                if name=='更早同类根治计划':pool=[r for r in pool if 'PLAN_CURATIVE_PANCREAS' in r['labels']]
                if not pool:continue
                previous=max(pool,key=lambda r:(r[clock],r['row_id']))
                gap=(anchor[clock]-previous[clock]).total_seconds()/3600
                deltas[name].append(gap);covered[name]+=1
                cards.append({'clock':clock,'patient_uid':uid,'visit_id':visit,'anchor_row_id':anchor['row_id'],'preceding_kind':name,'preceding_row_id':previous['row_id'],'gap_hours':round(gap,3),'preceding_labels':previous['labels'],'preceding_header':str(previous[clock]),'anchor_header':str(anchor[clock])})
        comparisons[clock]={'preoperative_first_label_anchors':len(anchors),'patients':len({r['patient_uid'] for r in anchors}),
            'preceding_records':{name:{'paired_groups':len(v),'median_gap_hours':round(statistics.median(v),2),
                'within_8h':sum(x<=8 for x in v),'within_24h':sum(x<=24 for x in v),'within_48h':sum(x<=48 for x in v),
                'over_48h':sum(x>48 for x in v),'max_gap_hours':round(max(v),2)} for name,v in deltas.items()}}
    summary={'date':'2026-10-20','patients':len({r['patient_uid'] for r in rows}),'note_rows':len(rows),
        'labels':summaries,'time_pairing':comparisons,'patterns':PATTERNS,
        'input_sha256':digest(args.input),'script_sha256':digest(Path(__file__)),
        'meaning':'Literal multi-label exploration on the same convenience sample. VISIT_ID is a search aid, not an episode definition. Body headers and system times are alternative observation clocks, not verified decision times. Within-window records are candidates, not confirmed decision points. Prior source coverage limits apply.'}
    (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (args.output/'private_label_hits.json').write_text(json.dumps(hits,ensure_ascii=False,indent=2)+'\n')
    (args.output/'private_temporal_pairs.json').write_text(json.dumps(cards,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
