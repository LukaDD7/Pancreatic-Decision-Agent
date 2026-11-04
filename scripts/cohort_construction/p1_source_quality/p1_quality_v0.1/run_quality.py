"""CLI source audit and patient-level P1 quality screening. No LLM or cloud API."""
from pathlib import Path
from collections import Counter,defaultdict
import argparse,csv,datetime as dt,hashlib,json,platform,random,re,sys,time
import pyarrow
import pyarrow.parquet as pq
from report_parser import basename,compact,dateof,normalize,parse_report,evaluate_report

def jsonwrite(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def filekey(v):return re.sub('/+','/',str(v or '').replace('\\','/'))
def rows(path):
    with path.open(encoding='utf-8') as f:
        for line in f:
            if line.strip():yield json.loads(line)

class InputAudit:
    def __init__(self):self.items={}
    def add(self,p):
        p=Path(p)
        if str(p) in self.items:return
        h=hashlib.sha256()
        with p.open('rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
        self.items[str(p)]={'bytes':p.stat().st_size,'sha256':h.hexdigest()}

def load_sources(batch,audit):
    clusters=defaultdict(list);tables=defaultdict(list)
    raw=batch/'raw/imaging_original_clusters.jsonl'
    audit.add(raw)
    for r in rows(raw):
        values=[]
        for f in r.get('fragments',[]):
            values.extend(str(x) for x in f.get('raw_token_array',[]) if x is not None)
            values.append(f.get('raw_fragment') or '')
        # Raw tokens/CSV fragments are checked; repaired output is not used as independent proof.
        rawtext='\n'.join(values)
        tokens=r.get('fragments',[{}])[0].get('raw_token_array',[]) if r.get('fragments') else []
        rd=dateof(tokens[5]) if len(tokens)>=6 else ''
        clusters[(basename(r.get('source_file')),str(r.get('record_cluster_id')))].append(
            {'uids':r.get('linked_patient_uids') or [],'text':compact(rawtext),'date':rd,'path':str(raw),'cluster':r.get('record_cluster_id')})
    paths=list(batch.glob('patients/*/CT_total_source_rows.jsonl'))+[batch/'raw/legacy_disease_table_rows.jsonl']
    for path in paths:
        if not path.is_file():continue
        audit.add(path)
        for r in rows(path):
            v=r.get('raw_values') or {}
            key=(r.get('patient_uid'),filekey(r.get('source_file')),str(r.get('source_sheet')),str(r.get('source_row')))
            tables[key].append({'findings':compact(v.get('报告表现')),'impression':compact(v.get('报告结论')),
                                'date':dateof(v.get('检查日期')),'path':str(path),'source_record_id':r.get('source_record_id')})
    return clusters,tables

def verify_source(d,uid,clusters,tables,rules):
    if d.get('patient_uid')!=uid:return {'verified':False,'reason':'FOLDER_RECORD_IDENTITY_MISMATCH'}
    origin=d.get('index_origin') or ''
    if origin not in rules['allowed_origins']:return {'verified':False,'reason':'SUPPLEMENT_OR_UNKNOWN_ORIGIN'}
    a,b=compact(d.get('original_findings')),compact(d.get('original_impression'));date=dateof(d.get('exam_datetime'))
    if not(a or b):return {'verified':False,'reason':'NO_TEXT_TO_VERIFY'}
    if origin==rules['allowed_origins'][0]:
        candidates=clusters.get((basename(d.get('source_file_name')),str(d.get('record_cluster_id'))),[])
        for c in candidates:
            if uid not in c['uids'] or len(set(c['uids']))!=1:continue
            if c['date']!=date or not date:continue
            if (not a or a in c['text']) and (not b or b in c['text']):
                return {'verified':True,'reason':'RAW_CLUSTER_TEXT_ID_DATE_MATCH','path':c['path'],'source_key':str(c['cluster'])}
        return {'verified':False,'reason':'RAW_CLUSTER_NOT_RECONCILED'}
    key=(uid,filekey(d.get('source_file_name')),str(d.get('source_sheet')),str(d.get('source_row')))
    for c in tables.get(key,[]):
        exact_separate=(a==c['findings'] and b==c['impression'])
        # Upstream preserves both original columns with explicit labels in original_findings.
        exact_wrapped=(not b and a==compact('原表【报告表现】'+c['findings']+'原表【报告结论】'+c['impression']))
        if c['date']==date and date and (exact_separate or exact_wrapped):
            return {'verified':True,'reason':'RAW_TABLE_ROW_TEXT_ID_DATE_MATCH','path':c['path'],'source_key':c['source_record_id'] or str(d.get('source_row')),'layout':'labeled_original_columns' if exact_wrapped else 'separate_columns'}
    return {'verified':False,'reason':'RAW_TABLE_NOT_RECONCILED'}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--data-root',type=Path,required=True);ap.add_argument('--index',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True);ap.add_argument('--rules',type=Path,default=Path(__file__).with_name('quality_rules.json'))
    args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    rules=json.loads(args.rules.read_text());audit=InputAudit();audit.add(args.index);audit.add(args.rules)
    with args.index.open(encoding='utf-8-sig',newline='') as f:index={r['patient_uid']:r for r in csv.DictReader(f)}
    assert len(index)==2707,'This frozen first version expects delivered P0=2707.'
    patients={u:{'patient_uid':u,'alias':f'Q{i:04d}','identity_index_ok':r['身份状态'] in rules['identity_index_allowed'],
                 'clinical_source_rows':0,'qualified_clinical_notes':0,'clinical_source_errors':[],'image_ids':[], 'image_read_errors':[]} for i,(u,r) in enumerate(sorted(index.items()),1)}
    incl=re.compile(rules['clinical_document_include_pattern'],re.I);excl=re.compile(rules['clinical_document_exclude_pattern'])
    stats=Counter();decoding=Counter();all_reports={};originstats=Counter();errorlog=[];start=time.monotonic()
    # Source/context availability is technical and pancreas-related, not a surgical-decision classifier.
    for path in sorted(args.data_root.glob('batch_*/raw/document_l1/**/*.parquet'))+sorted(args.data_root.glob('batch_*/raw/legacy_xml_document_l1_v1/**/*.parquet')):
        try:
            audit.add(path)
            table=pq.read_table(path,columns=['patient_uid','文书名称','文书内容','create_time','admission_time','source_record_id'])
            stats['clinical_source_rows']+=table.num_rows
            for r in table.to_pylist():
                u=r['patient_uid']
                if u not in patients:continue
                p=patients[u];p['clinical_source_rows']+=1;typ=r['文书名称'] or ''
                if not incl.search(typ) or excl.search(typ):continue
                raw=r['文书内容']
                if isinstance(raw,bytes):
                    try:text=raw.decode('utf-8');enc='utf-8'
                    except UnicodeDecodeError:text=raw.decode('gb18030');enc='gb18030'
                else:text=str(raw or '');enc='string'
                decoding[enc]+=1
                if text.strip() and re.search(r'胰|PDAC|pancrea',text,re.I) and (dateof(r['create_time']) or dateof(r['admission_time'])) and r['source_record_id']:
                    p['qualified_clinical_notes']+=1
        except Exception as e:errorlog.append({'path':str(path),'stage':'clinical','error':repr(e)})
    print(json.dumps({'stage':'clinical_done','rows':stats['clinical_source_rows'],'patients_with_qualified_note':sum(p['qualified_clinical_notes']>0 for p in patients.values())}),flush=True)
    batch_index_uids=[]
    for batch in sorted(p for p in args.data_root.glob('batch_*') if p.is_dir()):
        idx=batch/'患者索引.csv';audit.add(idx)
        with idx.open(encoding='utf-8-sig',newline='') as f:batch_index_uids.extend(r['patient_uid'] for r in csv.DictReader(f))
        clusters,tables=load_sources(batch,audit)
        for path in sorted(batch.glob('patients/*/imaging_reports_all_sources.jsonl')):
            uid=path.parent.name
            if uid not in patients:raise ValueError('Patient directory outside frozen P0')
            try:
                audit.add(path)
                for d in rows(path):
                    stats['imaging_input_rows']+=1;originstats[d.get('index_origin','')]+=1
                    parsed=parse_report(d,rules);prov=verify_source(d,uid,clusters,tables,rules)
                    state,reasons=evaluate_report(d,parsed,prov,rules)
                    date=dateof(d.get('exam_datetime'))
                    key='|'.join([uid,date,parsed['modality'],parsed['normalized_text_sha256']])
                    qid=hashlib.sha256(key.encode()).hexdigest()[:24]
                    source_ref={'index_report_uid':d.get('index_report_uid'),'index_origin':d.get('index_origin'),'source_file_name':d.get('source_file_name'),
                                'source_sheet':d.get('source_sheet'),'source_row':d.get('source_row'),'record_cluster_id':d.get('record_cluster_id'),
                                'patient_file':str(path),'source_verification':prov}
                    if qid not in all_reports:
                        all_reports[qid]={'quality_report_id':qid,'patient_uid':uid,'date':date,'method':d.get('exam_method'),
                            'exam_no':str(d.get('exam_no_raw') or ''),'quality_state':state,'reason_codes':reasons,'parsed':parsed,
                            'source_refs':[source_ref],'raw_content_rows':1,'has_verified_source':prov['verified']}
                        patients[uid]['image_ids'].append(qid)
                    else:
                        old=all_reports[qid];old['source_refs'].append(source_ref);old['raw_content_rows']+=1
                        if prov['verified'] and not old['has_verified_source']:
                            old['has_verified_source']=True;old['quality_state']=state;old['reason_codes']=reasons
            except Exception as e:
                patients[uid]['image_read_errors'].append(repr(e));errorlog.append({'path':str(path),'stage':'imaging','error':repr(e)})
        print(json.dumps({'stage':batch.name,'image_rows':stats['imaging_input_rows'],'unique_content_reports':len(all_reports),'elapsed':round(time.monotonic()-start,1)}),flush=True)
    assert len(batch_index_uids)==2707 and set(batch_index_uids)==set(index),'Batch/P0 membership mismatch'
    # A stable report identifier with divergent versions is flagged, never silently overwritten.
    uid_versions=defaultdict(set)
    for qid,r in all_reports.items():
        for ref in r['source_refs']:
            if ref['index_report_uid']:uid_versions[(r['patient_uid'],ref['index_report_uid'])].add(qid)
    for qids in uid_versions.values():
        if len(qids)>1:
            for qid in qids:
                r=all_reports[qid];r['reason_codes'].append('SAME_REPORT_UID_DIFFERENT_CONTENT');r['quality_state']='REVIEW'
    status=Counter();reasons=Counter();vessel_states={k:Counter() for k in rules['required_vessels']};report_state=Counter();template_state=Counter();raw_proof=Counter()
    for r in all_reports.values():
        report_state[r['quality_state']]+=1
        if r['parsed']['template_detected']:template_state[r['quality_state']]+=1
        for k,v in r['parsed']['vessels'].items():vessel_states[k][v['state']]+=1
        reasons.update(r['reason_codes'])
        for ref in r['source_refs']:raw_proof[ref['source_verification']['reason']]+=1
    for p in patients.values():
        rr=[all_reports[q] for q in p['image_ids']];passed=[r for r in rr if r['quality_state']=='PASS'];review=[r for r in rr if r['quality_state']=='REVIEW']
        pc=[]
        if not p['identity_index_ok']:pc.append('INDEX_IDENTITY_NOT_CLEARED')
        if not p['qualified_clinical_notes']:pc.append('NO_ANCHORED_PANCREAS_CLINICAL_NOTE')
        if not passed:pc.append('NO_QUALIFIED_REPORT')
        if p['image_read_errors']:pc.append('IMAGING_READ_ERROR')
        p['qualifying_report_ids']=[r['quality_report_id'] for r in passed]
        p['qualifying_report_dates']=sorted(set(r['date'] for r in passed));p['qualifying_reports_in_window']=sum(rules['study_window'][0]<=r['date']<=rules['study_window'][1] for r in passed)
        p['review_report_count']=len(review);p['report_content_count']=len(rr);p['reason_codes']=pc
        p['state']='PASS_V1' if not pc else 'REVIEW_V1' if review or not p['identity_index_ok'] or p['image_read_errors'] else 'NOT_MET_V1'
        status[p['state']]+=1
        if not p['qualifying_reports_in_window']:p['scope_flag']='NO_QUALIFIED_REPORT_IN_WINDOW'
    assert sum(status.values())==2707
    assert stats['imaging_input_rows']==sum(r['raw_content_rows'] for r in all_reports.values())
    assert not errorlog,'Input errors: inspect error log before treating screening output as complete.'
    summary={'version':rules['version'],'P0_n':2707,'patient_states':status,'imaging_input_rows':stats['imaging_input_rows'],'unique_content_report_versions':len(all_reports),
        'report_states':report_state,'structured_template_report_states':template_state,'origin_rows':originstats,'source_proof_rows':raw_proof,
        'reason_code_reports':reasons,'vessel_state_reports':vessel_states,'clinical_source_rows':stats['clinical_source_rows'],
        'patients_with_anchored_pancreas_clinical_note':sum(p['qualified_clinical_notes']>0 for p in patients.values()),
        'patients_with_any_report_pass':sum(bool(p['qualifying_report_ids']) for p in patients.values()),
        'patients_with_any_report_pass_in_window':sum(p['qualifying_reports_in_window']>0 for p in patients.values()),
        'source_read_errors':errorlog,'clinical_decoding':decoding,'elapsed_seconds':round(time.monotonic()-start,1),
        'important':'PASS is operational source-representation eligibility v0.1, not clinical correctness or P3 eligibility. Content versions are not independent exams.'}
    jsonwrite(args.output/'summary.json',summary);jsonwrite(args.output/'input_manifest.json',audit.items)
    with (args.output/'report_quality.jsonl').open('w',encoding='utf-8') as f:
        for r in all_reports.values():f.write(json.dumps(r,ensure_ascii=False)+'\n')
    with (args.output/'patient_quality.jsonl').open('w',encoding='utf-8') as f:
        for p in patients.values():f.write(json.dumps(p,ensure_ascii=False)+'\n')
    with (args.output/'patient_quality.csv').open('w',newline='',encoding='utf-8-sig') as f:
        cols=['alias','patient_uid','state','qualified_clinical_notes','report_content_count','qualifying_report_count','qualifying_reports_in_window','review_report_count','reason_codes']
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
        for p in patients.values():w.writerow({**{k:p[k] for k in cols if k in p},'qualifying_report_count':len(p['qualifying_report_ids']),'reason_codes':'|'.join(p['reason_codes'])})
    rng=random.Random(20261018);samples=[]
    strata={key:[r for r in all_reports.values() if r['quality_state']==key] for key in ['PASS','REVIEW','NOT_APPLICABLE']}
    for reason in ['VESSEL_SMA_EMPTY_ANGLE_FIELD','VESSEL_CONFLICT_SMA','METASTASIS_FIELD_CONFLICT','RAW_CLUSTER_NOT_RECONCILED','POSTOPERATIVE_OR_RECURRENCE_CONTEXT']:
        strata[reason]=[r for r in all_reports.values() if reason in r['reason_codes']]
    for key,pool in strata.items():
        for r in rng.sample(sorted(pool,key=lambda x:x['quality_report_id']),min(5,len(pool))):samples.append({'sampling_stratum':key,**r})
    jsonwrite(args.output/'development_review_samples.json',samples)
    jsonwrite(args.output/'run_manifest.json',{'version':rules['version'],'python':sys.version,'pyarrow':pyarrow.__version__,'platform':platform.platform(),
        'arguments':{k:str(v) for k,v in vars(args).items()},'rules_sha256':hashlib.sha256(args.rules.read_bytes()).hexdigest(),
        'script_hashes':{name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in ['run_quality.py','report_parser.py']},
        'input_files':len(audit.items),'clinical_validation_completed':False,'P2_and_P3_implemented':False})
    print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
