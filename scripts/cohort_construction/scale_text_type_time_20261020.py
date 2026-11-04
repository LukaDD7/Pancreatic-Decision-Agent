"""Nested n=100/300 label, repetition and retrieval exploration.

Counts are operational candidate document points, not validated clinical decisions.
The original seven literal patterns are imported unchanged from the pilot.
"""
from pathlib import Path
from collections import Counter, defaultdict
import argparse, datetime as dt, hashlib, json, random, re, statistics
import pyarrow.parquet as pq
from probe_text_type_time_20261020 import PATTERNS, family, body_time

START=dt.datetime(2021,1,1)
END=dt.datetime(2026,10,18,23,59,59)
CORE={'PLAN_CURATIVE_PANCREAS','INDICATION_YES','INDICATION_NO'}
SUPPORT=CORE|{'SURGERY_INTENT_GENERIC'}
FAMILIES={'首次病程','首次上级查房','会诊记录','术前小结','术前讨论','入院记录','其他查房','门诊记录'}
NONTARGET_TITLE=re.compile(r'颈椎|颈后路|腰椎|脊柱|PICC|CVC|ERCP|超声胃镜|穿刺|置管|碎石')
HISTORY_MARKER=re.compile(r'当时|曾(?:经)?(?:考虑|建议|拟|行)|既往(?:考虑|建议|拟|无)|此前(?:考虑|建议|无)')


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path,obj):path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str)+'\n')


def dist(values):
    values=sorted(values)
    if not values:return {'n':0,'median':None,'q1':None,'q3':None}
    q=statistics.quantiles(values,n=4,method='inclusive') if len(values)>1 else [values[0]]*3
    return {'n':len(values),'median':round(statistics.median(values),3),'q1':round(q[0],3),'q3':round(q[2],3),'min':values[0],'max':values[-1]}


def parse_dt(x):
    if isinstance(x,dt.datetime):return x.replace(tzinfo=None)
    if not x:return None
    try:return dt.datetime.fromisoformat(str(x).replace('Z','+00:00')).replace(tzinfo=None)
    except ValueError:return None


def clock(row,which):
    return row['body_time'] or row['system_time'] if which=='body_preferred' else row['system_time']


def visitkey(row):return row['visit_id'] or 'unknown:'+row['record_key']


def gap_summary(pairs,total):
    vals=[p['gap_hours'] for p in pairs]
    return {'anchors':total,'paired':len(vals),'within_24h':sum(v<=24 for v in vals),'within_48h':sum(v<=48 for v in vals),'within_7d':sum(v<=168 for v in vals),'gap_hours':dist(vals)}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--repo',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();repo=args.repo;out=args.output;out.mkdir(parents=True,exist_ok=True)
    selection=json.loads((out/'selection.json').read_text());ordered=[p['patient_uid'] for p in selection['selected']];uids=set(ordered)
    assert len(uids)==300
    clinical_manifest=json.loads((repo/'local_artifacts/source_cache_20261020/clinical_cache_manifest.json').read_text())
    image_manifest=json.loads((repo/'local_artifacts/source_cache_20261020/image_cache_manifest.json').read_text())
    assert len(clinical_manifest['files'])==265 and len(image_manifest['files'])==300
    frozen=[json.loads(s) for s in (repo/'local_artifacts/p1_quality_v0.2/results/patient_quality.jsonl').read_text().splitlines()]
    population=sorted(p['patient_uid'] for p in frozen if p['state']=='PASS_V2')
    random.Random(20261020).shuffle(population)
    assert len(population)==2004 and ordered==population[:300], 'Selection must replay from the frozen P1 population'
    frozen={p['patient_uid']:p for p in frozen};rules=json.loads((repo/'local_artifacts/p1_quality_v0.2/quality_rules.json').read_text())
    inc=re.compile(rules['clinical_document_include_pattern'],re.I);exc=re.compile(rules['clinical_document_exclude_pattern'])
    compiled={k:re.compile(p,re.I) for k,p in PATTERNS.items()}
    raw_counts=Counter();notes=[];hits=[];coverage=Counter();source_hashes={}
    cols=['patient_uid','VISIT_ID','文书名称','文书内容','create_time','admission_time','discharge_time','source_record_id','source_file','source_row']
    for original,meta in clinical_manifest['files'].items():
        path=Path(meta['cached_path']);assert sha(path)==meta['sha256'];source_hashes[original]=meta['sha256']
        for x in pq.read_table(path,columns=cols,filters=[('patient_uid','in',ordered)]).to_pylist():
            uid=x['patient_uid'];raw_counts[uid]+=1;raw=x['文书内容']
            if isinstance(raw,bytes):
                try:text=raw.decode('utf-8')
                except UnicodeDecodeError:text=raw.decode('gb18030')
            else:text=str(raw or '')
            if not text.strip() or not re.search(r'胰|PDAC|pancrea',text,re.I):continue
            typ=x['文书名称'] or ''
            if re.search(r'护理|同意书|交接|核查|物品|告知|申请单|报告卡',typ):continue
            text=re.sub(r'<[^>]+>','\n',text)
            sys_time=parse_dt(x['create_time']) or parse_dt(x['admission_time']);bt=body_time(text)
            text_sha=hashlib.sha256(text.encode()).hexdigest()
            key=hashlib.sha256((uid+'|'+str(x['source_record_id'])+'|'+text_sha).encode()).hexdigest()[:24]
            fam='门诊记录' if re.search(r'门诊|就诊记录',typ) else family(typ)
            r={'patient_uid':uid,'visit_id':str(x['VISIT_ID'] or ''),'record_key':key,'source_record_id':x['source_record_id'],'document_type':typ,'family':fam,'body_time':bt,'system_time':sys_time,'admission_time':parse_dt(x['admission_time']),'discharge_time':parse_dt(x['discharge_time']),'body_sha256':text_sha,'text':text,'source_parquet':original,'source_file':x['source_file'],'source_row':x['source_row'],'labels':[],'usable_labels':[],'clinical_type':bool((inc.search(typ) and not exc.search(typ)) or fam=='门诊记录')}
            for label,p in compiled.items():
                matches=list(p.finditer(text))
                if not matches:continue
                r['labels'].append(label);usable=False
                for m in matches:
                    before=text[max(0,m.start()-30):m.start()]
                    past=bool(HISTORY_MARKER.search(before)) or (label=='INDICATION_NO' and '当时' in text[max(0,m.start()-4):m.start()])
                    hits.append({'patient_uid':uid,'record_key':key,'label':label,'start':m.start(),'end':m.end(),'match':m.group(0),'past_marker':past,'context':text[max(0,m.start()-60):min(len(text),m.end()+110)]})
                    usable|=not past
                if usable:r['usable_labels'].append(label)
            notes.append(r);coverage[uid]+=1
    assert all(raw_counts[u]==frozen[u]['clinical_source_rows'] for u in uids),'Raw clinical coverage differs from frozen P1'
    note_index={r['record_key']:r for r in notes}
    assert len(note_index)==len(notes),'Duplicate stable note keys require explicit reconciliation'
    print(json.dumps({'stage':'clinical_loaded','patients':300,'raw_rows':sum(raw_counts.values()),'pancreas_notes':len(notes)}),flush=True)

    images=defaultdict(list)
    for original,meta in image_manifest['files'].items():
        path=Path(meta['cached_path']);assert sha(path)==meta['sha256'];source_hashes[original]=meta['sha256']
        for line in path.read_text().splitlines():
            if not line.strip():continue
            x=json.loads(line);uid=x['patient_uid'];t=parse_dt(x.get('exam_datetime'));method=str(x.get('exam_method') or '')
            if uid not in uids or not t:continue
            if not re.search(r'CT|MR|磁共振',method,re.I):continue
            if not (x.get('original_findings') or x.get('original_impression')):continue
            modality='MR' if re.search(r'MR|磁共振',method,re.I) else 'CT'
            key=(str(x.get('exam_no_raw') or ''),str(t),modality)
            images[uid].append({'exam_key':key,'exam_time':t,'modality':modality,'method':method,'index_report_uid':x.get('index_report_uid')})
    images={u:list({tuple(x['exam_key']):x for x in xs}.values()) for u,xs in images.items()}

    # Keep both clocks; body_preferred is an observation proxy, not availability.
    all_stats={};all_candidate_rows=[];all_pairs=[]
    for n in [100,300]:
        subset=set(ordered[:n]);rr=[r for r in notes if r['patient_uid'] in subset];hh=[h for h in hits if h['patient_uid'] in subset]
        counters=Counter();labelstats={}
        for label in PATTERNS:
            lr=[r for r in rr if label in r['labels']];lh=[h for h in hh if h['label']==label]
            groups=Counter((r['patient_uid'],visitkey(r)) for r in lr)
            pair_count=len(lr);units=len(groups)
            labelstats[label]={'matched_notes':pair_count,'literal_occurrences':len(lh),'patients':len({r['patient_uid'] for r in lr}),'patient_visit_label_groups':units,'groups_with_repeat':sum(v>1 for v in groups.values()),'excess_label_note_fraction':round((pair_count-units)/pair_count,4) if pair_count else None,'document_families':dict(Counter(r['family'] for r in lr))}
        label_rows=[r for r in rr if r['labels']]
        per_patient_raw=Counter(r['patient_uid'] for r in label_rows)
        exact_unique={(r['patient_uid'],r['body_sha256']) for r in label_rows}
        time_results={};count_results={}
        for which in ['body_preferred','system']:
            in_window=[r for r in rr if clock(r,which) and START<=clock(r,which)<=END]
            by_visit=defaultdict(list)
            for r in in_window:by_visit[(r['patient_uid'],visitkey(r))].append(r)
            core_rows=[r for r in in_window if r['clinical_type'] and r['family'] in FAMILIES and not NONTARGET_TITLE.search(r['document_type']) and CORE.intersection(r['usable_labels'])]
            support_rows=[r for r in in_window if r['clinical_type'] and r['family'] in FAMILIES and not NONTARGET_TITLE.search(r['document_type']) and SUPPORT.intersection(r['usable_labels'])]
            support_by_visit=defaultdict(list)
            for r in support_rows:support_by_visit[(r['patient_uid'],visitkey(r))].append(r)
            all_labels=[r for r in in_window if r['clinical_type'] and r['labels']]
            firstanchors={}
            for r in all_labels:
                for label in r['labels']:
                    key=(r['patient_uid'],visitkey(r),label,r['family'])
                    if key not in firstanchors or clock(r,which)<clock(firstanchors[key],which):firstanchors[key]=r
            per_label=defaultdict(lambda:defaultdict(list));anchor_totals=Counter()
            for (uid,visit,label,fam),anchor in firstanchors.items():
                at=clock(anchor,which);anchor_totals[(label,fam)]+=1;group=by_visit[(uid,visit)]
                for predecessor,desired,require_label in [('earlier_first_course',{'首次病程'},False),('earlier_first_senior',{'首次上级查房'},False),('earlier_consult',{'会诊记录'},False),('earlier_preop_discussion',{'术前讨论'},False),('earlier_judgment_document',FAMILIES,True)]:
                    pool=[p for p in group if p['record_key']!=anchor['record_key'] and p['family'] in desired and clock(p,which)<=at and (not require_label or SUPPORT.intersection(p['usable_labels']))]
                    if not pool:continue
                    prev=max(pool,key=lambda p:clock(p,which));gap=(at-clock(prev,which)).total_seconds()/3600
                    pair={'sample_n':n,'clock':which,'patient_uid':uid,'label':label,'anchor_family':fam,'anchor_record':anchor['record_key'],'preceding_kind':predecessor,'preceding_record':prev['record_key'],'preceding_family':prev['family'],'gap_hours':round(gap,3)}
                    per_label[(label,fam)][predecessor].append(pair);all_pairs.append(pair)
                ims=[x for x in images.get(uid,[]) if x['exam_time']<=at]
                if ims:
                    im=max(ims,key=lambda x:x['exam_time']);gap=(at-im['exam_time']).total_seconds()/3600
                    pair={'sample_n':n,'clock':which,'patient_uid':uid,'label':label,'anchor_family':fam,'anchor_record':anchor['record_key'],'preceding_kind':'earlier_CT_MR_exam','exam_key':im['exam_key'],'modality':im['modality'],'gap_hours':round(gap,3)}
                    per_label[(label,fam)]['earlier_CT_MR_exam'].append(pair);all_pairs.append(pair)
            time_results[which]={label+'|'+fam:{k:gap_summary(v,anchor_totals[(label,fam)]) for k,v in dd.items()} for (label,fam),dd in per_label.items()}
            # Candidate sources start from first same-label witness per visit.
            # This is deliberately a conservative retrieval unit, not an episode.
            unit_anchors={}
            for r in core_rows:
                for label in sorted(CORE.intersection(r['usable_labels'])):
                    key=(r['patient_uid'],visitkey(r),label)
                    if key not in unit_anchors or clock(r,which)<clock(unit_anchors[key],which):unit_anchors[key]=r
            variants={}
            for hours in [24,48,168]:
                selected={}
                for (uid,visit,label),anchor in unit_anchors.items():
                    at=clock(anchor,which)
                    options=[p for p in support_by_visit[(uid,visit)] if 0<=(at-clock(p,which)).total_seconds()/3600<=hours]
                    chosen=min(options,key=lambda p:(clock(p,which),p['record_key'])) if options else anchor
                    selected[chosen['record_key']]=chosen
                counts=Counter(r['patient_uid'] for r in selected.values())
                variants[str(hours)+'h']={'total_candidate_points':len(selected),'per_patient':dist([counts[u] for u in ordered[:n]]),'patients_zero':sum(counts[u]==0 for u in subset),'patients_ge2':sum(counts[u]>=2 for u in subset),'covered_patients':sum(counts[u]>0 for u in subset)}
                if n==300:
                    for r in selected.values():all_candidate_rows.append({'clock':which,'lookback_hours':hours,'patient_uid':r['patient_uid'],'record_key':r['record_key'],'document_type':r['document_type'],'family':r['family'],'labels':r['usable_labels'],'time':str(clock(r,which)),'status':'UNVALIDATED_CANDIDATE_DOCUMENT_POINT'})
            core_by_patient=Counter(r['patient_uid'] for r in core_rows)
            core_visits=Counter(u for u,v in {(r['patient_uid'],visitkey(r)) for r in core_rows})
            count_results[which]={'core_labeled_records':len(core_rows),'core_records_per_patient':dist([core_by_patient[u] for u in ordered[:n]]),'core_patient_visit_units':sum(core_visits.values()),'core_visits_per_patient':dist([core_visits[u] for u in ordered[:n]]),'first_same_label_per_visit_anchors':len(unit_anchors),'backtrack_variants':variants,'body_header_available_notes':sum(r['body_time'] is not None for r in in_window),'system_proxy_fallback_notes':sum(r['body_time'] is None for r in in_window)}
        all_stats[str(n)]={'patients':n,'raw_clinical_rows':sum(raw_counts[u] for u in subset),'pancreas_notes':len(rr),'labeled_notes':len(label_rows),'exact_unique_labeled_full_texts':len(exact_unique),'exact_full_text_duplicate_fraction':round(1-len(exact_unique)/len(label_rows),4) if label_rows else None,'labeled_notes_per_patient':dist([per_patient_raw[u] for u in ordered[:n]]),'labels':labelstats,'time_patterns':time_results,'candidate_point_counts':count_results}
    summary={'date':'2026-10-20','selection':{k:v for k,v in selection.items() if k!='selected'},'clinical_files_read':265,'image_patient_files_read':300,'clinical_source_counts_match_frozen_P1_for_all_selected':True,'study_window':['2021-01-01','2026-10-18'],'samples':all_stats,'candidate_rule':'First usable CORE label per patient/VISIT_ID/label in physician record families; explicit nearby past markers and named non-target procedure titles excluded from candidate counts, retained in label statistics. Backtrack to earliest judgment-bearing record within same visit and 24/48/168h; deduplicate identical selected source record keys. This is a conservative candidate retrieval count, not a validated decision count. It can miss repeated reassessments with unchanged labels and within-visit events. Generic predecessor targets and historical carry-forward still require review.','imaging_rule':'Nearest preceding recorded CT/MR exam across supplied history, grouped by exam number/time/modality. Exam time is not report availability or proof of relevance; version conflicts are not resolved by pairing.','script_sha256':sha(Path(__file__)),'pilot_pattern_module_sha256':sha(Path(__file__).with_name('probe_text_type_time_20261020.py'))}
    write(out/'summary.json',summary);write(out/'candidate_points.json',all_candidate_rows);write(out/'temporal_pairs.json',all_pairs);write(out/'label_hits.json',hits)
    with (out/'clinical_notes.jsonl').open('w') as f:
        for r in notes:f.write(json.dumps(r,ensure_ascii=False,default=str)+'\n')
    write(out/'input_hashes.json',source_hashes)
    print(json.dumps({'samples':{n:{'patients':d['patients'],'pancreas_notes':d['pancreas_notes'],'labeled_notes':d['labeled_notes'],'candidate_48h':d['candidate_point_counts']['body_preferred']['backtrack_variants']['48h']} for n,d in all_stats.items()}},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
