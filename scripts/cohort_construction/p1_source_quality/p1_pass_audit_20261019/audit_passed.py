"""Audit the frozen 2,004 PASS_V2 patients, without changing membership."""
from pathlib import Path
from collections import Counter,defaultdict
from bisect import bisect_left
import csv,datetime as dt,hashlib,json,random,re,sys
import pyarrow.parquet as pq

HERE=Path(__file__).resolve().parent;BASE=HERE.parent/'p1_quality_v0.2'
sys.path.insert(0,str(BASE))
from report_parser import normalize,dateof

def read(p):return [json.loads(s) for s in p.open(encoding='utf-8') if s.strip()]
def write(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf-8')
def median(v):
    v=sorted(v);n=len(v)
    return None if not n else v[n//2] if n%2 else (v[n//2-1]+v[n//2])/2
def ordinal(s):return dt.date.fromisoformat(s).toordinal()
def valid_exam_no(s):
    s=str(s or '').strip()
    if s.endswith('.0'):s=s[:-2]
    return s if s.lower() not in {'','none','nan','null','0','未知','无'} and len(s)>=6 else ''

patients={p['patient_uid']:p for p in read(BASE/'results/patient_quality.jsonl') if p['state']=='PASS_V2'}
assert len(patients)==2004
all_reports=read(BASE/'results/report_quality.jsonl');rr={r['quality_report_id']:r for r in all_reports if r['patient_uid'] in patients and r['quality_state']=='PASS'}
by_patient=defaultdict(list)
for r in rr.values():by_patient[r['patient_uid']].append(r)
rules=json.loads((BASE/'quality_rules.json').read_text());cache={};originals={};alerts=defaultdict(list);raw_hashes={}
exact_quote_errors=[];not_matched=[];source_stats=Counter();report_flags=Counter();exam_groups=defaultdict(list);cross_exam=defaultdict(set);cross_text=defaultdict(set)
for q,r in rr.items():
    p=r['parsed'];u=r['patient_uid'];d=None
    assert r['has_verified_source'] and all(v['represented'] for v in p['vessels'].values()) and not p['conflicts'] and not p['postoperative_context']
    # Re-link to the original patient record by both ID and normalized full text.
    for ref in sorted(r['source_refs'],key=lambda x:not x['source_verification']['verified']):
        path=Path(ref['patient_file'])
        if str(path) not in cache:
            cache[str(path)]=read(path);raw_hashes[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
        for x in cache[str(path)]:
            if x.get('index_report_uid')!=ref['index_report_uid']:continue
            text=normalize(x.get('original_findings'))+'\n'+normalize(x.get('original_impression'))
            if hashlib.sha256(text.encode()).hexdigest()==p['normalized_text_sha256']:d=x;break
        if d is not None:break
    if d is None:not_matched.append(q);continue
    originals[q]=d
    texts={field:normalize(d.get(field)) for field in ['original_findings','original_impression']};text='\n'.join(texts.values())
    for k,v in p['vessels'].items():
        for e in v['evidence']:
            f=texts[e['source_field']]
            if f[e['normalized_quote_start']:e['normalized_quote_end']]!=e['quote']:exact_quote_errors.append({'report':q,'vessel':k})
            segment=f[e['normalized_start']:e['normalized_end']]
            if e.get('angle') and re.search(r'脾动脉|脾静脉|脾动[、和及]?静脉',segment):
                alerts[q].append('ANGLE_SEGMENT_CONTAINS_SPLENIC_VESSEL')
    if not p['template_detected']:alerts[q].append('NOT_ALL_THREE_TEMPLATE_HEADERS')
    sizes=p['lesion_size'].get('values_mm',[])
    if sizes and (min(sizes)<5 or max(sizes)>100):alerts[q].append('SIZE_EXTREME_REVIEW')
    # Broader than the frozen postoperative rule: a review signal, not a new label.
    if re.search(r'(?:胰十二指肠|全胰|胰(?:腺)?(?:体尾|头|体|尾)|Whipple)[^。；;\n]{0,35}术后',text,re.I):alerts[q].append('BROADER_PANCREAS_POSTOP_WORDING')
    if re.search(r'壶腹|神经内分泌|IPMN|ITPN',text,re.I):alerts[q].append('TARGET_OR_DIAGNOSTIC_BOUNDARY_WORDING')
    if any(v['state']=='EXPLICIT_INDETERMINATE' for v in p['vessels'].values()):alerts[q].append('EXPLICIT_VASCULAR_UNCERTAINTY')
    flags={f for v in p['vessels'].values() for f in v.get('quality_flags',[])};report_flags.update(flags)
    verified_origins={ref['index_origin'] for ref in r['source_refs'] if ref['source_verification']['verified']}
    source_stats.update(verified_origins)
    eno=valid_exam_no(r['exam_no']);key=(u,r['date'],p['modality'],'exam:'+eno if eno else 'method:'+normalize(r['method']))
    exam_groups[key].append(q)
    if eno:cross_exam[(r['date'],p['modality'],eno)].add(u)
    cross_text[(r['date'],p['modality'],p['normalized_text_sha256'])].add(u)

assert not not_matched and not exact_quote_errors
data=Path(next(iter(rr.values()))['source_refs'][0]['patient_file']).parents[3]
incl=re.compile(rules['clinical_document_include_pattern'],re.I);excl=re.compile(rules['clinical_document_exclude_pattern'])
note_dates=defaultdict(set);note_types=defaultdict(Counter);note_count=Counter();clinical_errors=[];clinical_hashes={}
for path in sorted(data.glob('batch_*/raw/document_l1/**/*.parquet'))+sorted(data.glob('batch_*/raw/legacy_xml_document_l1_v1/**/*.parquet')):
    try:
        clinical_hashes[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
        for x in pq.read_table(path,columns=['patient_uid','文书名称','文书内容','create_time','admission_time','source_record_id']).to_pylist():
            u=x['patient_uid']
            if u not in patients:continue
            typ=x['文书名称'] or ''
            if not incl.search(typ) or excl.search(typ):continue
            raw=x['文书内容']
            if isinstance(raw,bytes):
                try:text=raw.decode('utf-8')
                except UnicodeDecodeError:text=raw.decode('gb18030')
            else:text=str(raw or '')
            date=dateof(x['create_time']) or dateof(x['admission_time'])
            if text.strip() and re.search(r'胰|PDAC|pancrea',text,re.I) and date and x['source_record_id']:
                note_dates[u].add(date);note_types[u][typ]+=1;note_count[u]+=1
    except Exception as e:clinical_errors.append({'path':str(path),'error':repr(e)})
assert not clinical_errors
assert all(note_count[u]==p['qualified_clinical_notes'] for u,p in patients.items()),'Clinical context count diverged from v0.2'

exam_per_patient=Counter(k[0] for k in exam_groups);new_pass={x['patient_uid'] for x in json.loads((BASE/'results/patient_delta.json').read_text()) if x['after']=='PASS'}
patient_rows=[];risk_pools=defaultdict(list);stats=Counter();nearest_all=[]
for u,p in patients.items():
    pr=by_patient[u];dates=sorted({r['date'] for r in pr});qual_flags={flag for r in pr for v in r['parsed']['vessels'].values() for flag in v.get('quality_flags',[])}
    origins={ref['index_origin'] for r in pr for ref in r['source_refs'] if ref['source_verification']['verified']}
    has_raw=rules['allowed_origins'][0] in origins
    n_ord=sorted(ordinal(d) for d in note_dates[u]);near=[]
    for date in dates:
        x=ordinal(date);i=bisect_left(n_ord,x);near.append(min(abs(x-n_ord[j]) for j in [i-1,i] if 0<=j<len(n_ord)))
    nearest=min(near);nearest_all.append(nearest)
    ar={'alias':p['alias'],'patient_uid':u,'qualified_content_versions':len(pr),'exam_group_proxy_count':exam_per_patient[u],
        'qualifying_report_dates':dates,'first_qualifying_date':dates[0],'last_qualifying_date':dates[-1],
        'in_window_report_versions':p['qualifying_reports_in_window'],'clinical_note_count':note_count[u],
        'first_clinical_note_date':min(note_dates[u]),'last_clinical_note_date':max(note_dates[u]),'nearest_image_note_gap_days':nearest,
        'has_original_cluster_proof':has_raw,'verified_origins':sorted(origins),'quality_flags':sorted(qual_flags),
        'all_qualified_reports_flagged':all(any(v.get('quality_flags') for v in r['parsed']['vessels'].values()) for r in pr),
        'newly_restored_in_v02':u in new_pass,'report_alerts':sorted({a for r in pr for a in alerts[r['quality_report_id']]})}
    patient_rows.append(ar)
    conditions={'random':True,'newly_restored':u in new_pass,'one_exam_group':exam_per_patient[u]==1,'table_proof_only':not has_raw,
       'all_reports_flagged':ar['all_qualified_reports_flagged'],'no_clinical_note_within_30_days':nearest>30,
       'historical_only':not p['qualifying_reports_in_window']}
    for k,v in conditions.items():
        if v:risk_pools[k].append(ar);stats[k]+=1
    for a in ar['report_alerts']:risk_pools[a].append(ar);stats['patient_alert_'+a]+=1
    for f in qual_flags:stats['patient_flag_'+f]+=1

rng=random.Random(20261019);selected={};sampling=[]
for key,pool in sorted(risk_pools.items()):
    n=10 if key=='random' else 6
    if key in ['EXPLICIT_VASCULAR_UNCERTAINTY','NOT_ALL_THREE_TEMPLATE_HEADERS','BROADER_PANCREAS_POSTOP_WORDING','historical_only']:n=10000
    for ar in rng.sample(sorted(pool,key=lambda x:x['alias']),min(n,len(pool))):selected[ar['patient_uid']]=ar;sampling.append({'alias':ar['alias'],'stratum':key})

packets=[]
for u,ar in sorted(selected.items(),key=lambda x:x[1]['alias']):
    pr=by_patient[u]
    # Include alert-rich report, earliest report and one new/flagged example, without duplication.
    chosen={r['quality_report_id']:r for r in [max(pr,key=lambda r:(len(alerts[r['quality_report_id']]),r['quality_report_id'])),min(pr,key=lambda r:(r['date'],r['quality_report_id']))]}
    for r in pr:
        if r['quality_report_id'] not in chosen and any(v.get('quality_flags') for v in r['parsed']['vessels'].values()):chosen[r['quality_report_id']]=r;break
    packets.append({'patient_audit':ar,'clinical_note_type_counts':dict(note_types[u]),'selected_reports':[{'saved':r,'original':{k:originals[q].get(k) for k in ['exam_method','exam_datetime','original_findings','original_impression','index_report_uid','index_origin','source_file_name']},'audit_alerts':alerts[q]} for q,r in chosen.items()]})

summary={'passed_patients':len(patients),'qualified_content_versions_in_passed_patients':len(rr),'exam_group_proxies':len(exam_groups),
   'report_modality_counts':Counter(r['parsed']['modality'] for r in rr.values()),'report_quality_flag_counts':report_flags,'patient_metrics':stats,
   'qualified_content_versions_per_patient':{'min':min(len(by_patient[u]) for u in patients),'median':median([len(by_patient[u]) for u in patients]),'max':max(len(by_patient[u]) for u in patients),
      'exactly_one':sum(len(by_patient[u])==1 for u in patients)},
   'exam_group_proxy_per_patient':{'min':min(exam_per_patient.values()),'median':median(list(exam_per_patient.values())),'max':max(exam_per_patient.values())},
   'image_note_nearest_gap_days':{'median':median(nearest_all),'max':max(nearest_all),'over_7':sum(x>7 for x in nearest_all),'over_30':sum(x>30 for x in nearest_all),'over_90':sum(x>90 for x in nearest_all)},
   'cross_patient_same_exam_no_date_modality_groups':sum(len(v)>1 for v in cross_exam.values()),'cross_patient_same_full_text_date_modality_groups':sum(len(v)>1 for v in cross_text.values()),
   'report_alert_counts':Counter(a for q in rr for a in set(alerts[q])),'original_relink_failures':not_matched,'quote_coordinate_failures':exact_quote_errors,'clinical_read_errors':clinical_errors,
   'sample_patients':len(packets),'sample_report_versions':sum(len(p['selected_reports']) for p in packets),
   'important':'Signals are audit priorities, not new exclusion rules. Exam groups are proxies; clinical note dates are recorded dates, not proof of report availability or Episode linkage.'}
write(HERE/'summary.json',summary);write(HERE/'patient_audit.json',patient_rows);write(HERE/'sample_packets.json',packets);write(HERE/'report_alerts.json',{q:sorted(set(a)) for q,a in alerts.items() if a})
write(HERE/'sampling_manifest.json',{'seed':20261019,'selection':sampling,'unique_patients':len(packets),'purpose':'development source review, not accuracy validation'})
write(HERE/'input_hashes.json',{'cohort_outputs':{n:hashlib.sha256((BASE/'results'/n).read_bytes()).hexdigest() for n in ['patient_quality.jsonl','report_quality.jsonl','patient_delta.json']},'patient_imaging_sources':raw_hashes,'clinical_sources':clinical_hashes})
with (HERE/'patient_audit.csv').open('w',encoding='utf-8-sig',newline='') as f:
    cols=['alias','patient_uid','qualified_content_versions','exam_group_proxy_count','first_qualifying_date','last_qualifying_date','in_window_report_versions','clinical_note_count','nearest_image_note_gap_days','has_original_cluster_proof','all_qualified_reports_flagged','newly_restored_in_v02','quality_flags','report_alerts'];w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
    for ar in patient_rows:w.writerow({k:'|'.join(ar[k]) if isinstance(ar[k],list) else ar[k] for k in cols})
print(json.dumps(summary,ensure_ascii=False))
