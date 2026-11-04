"""Deterministic development probe; signal hits are not adjudicated episodes."""
from pathlib import Path
from collections import Counter, defaultdict
import argparse, datetime as dt, hashlib, json, random, re
import pyarrow.parquet as pq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--runs-root', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = args.runs_root
    audit = json.loads((root/'p1_pass_audit_20261019/patient_audit.json').read_text())
    index = json.loads((root/'p1_quality_v0.2/results/input_manifest.json').read_text())
    all_sources = [Path(p) for p in index if '/raw/document_l1/' in p or '/raw/legacy_xml_document_l1_v1/' in p]
    sources = [p for p in all_sources if not p.stat().st_flags & (1 << 30)]
    covered = set()
    readable_source_rows = 0
    for path in sources:
        table = pq.read_table(path, columns=['patient_uid'])
        covered.update(table.column('patient_uid').to_pylist())
        readable_source_rows += table.num_rows
    available_audit = [p for p in audit if p['patient_uid'] in covered]
    rng = random.Random(20261020)
    selected, strata = {}, {}
    for name, pool in [
        ('one_exam_group', [p for p in available_audit if p['exam_group_proxy_count'] == 1]),
        ('multiple_exam_groups', [p for p in available_audit if p['exam_group_proxy_count'] >= 2]),
    ]:
        for p in rng.sample(sorted(pool, key=lambda p:p['alias']), 10):
            selected[p['patient_uid']] = p
            strata[p['patient_uid']] = name
    targeted = ['Q2241','Q0124','Q0692','Q1533','Q1500','Q2526','Q2590','Q2655']
    risk = [p for p in available_audit if p['alias'] in targeted and p['patient_uid'] not in selected]
    remaining = [p for p in available_audit if p['patient_uid'] not in selected and p not in risk and (p['report_alerts'] or p['nearest_image_note_gap_days']>30)]
    risk += rng.sample(sorted(remaining,key=lambda p:p['alias']), 10-len(risk))
    for p in risk:
        selected[p['patient_uid']] = p
        strata[p['patient_uid']] = 'targeted_quality_or_time_boundary'
    assert len(selected)==30
    patterns = {
        'surgery_assessment': r'手术(?:指征|机会|可能|条件|评估|治疗)|(?:可|能|拟|考虑|建议|暂不|不宜|不适合|无法|不能|不具备)[^。；;\n]{0,12}(?:手术|切除)|可切除|切除性|根治性?切除',
        'negative_surgery_broad': r'(?:不宜|不适合|无法|不能|无|不具备|未达|不考虑|暂不)[^。；;\n]{0,10}(?:手术|切除)|失去[^。；;\n]{0,8}手术',
        'negative_surgery_scoped': r'(?:不宜|不适合|无法|不能|不考虑|暂不|难以)(?:行|进行|接受|实施)?[^。；;\n]{0,8}(?:手术|切除)|(?:无|不具备|失去|丧失)(?:根治性?)?(?:手术|切除)(?:指征|机会|条件)|(?:不具备|未达)(?:根治性?)?手术(?:条件|指征)',
        'additional_evidence': r'(?:完善|进一步|补充|建议|拟行|待)[^。；;\n]{0,18}(?:MRI|MR|CT|活检|穿刺|腹腔镜|PET|分期|病理)',
        'neoadjuvant': r'新辅助|\bNAT\b',
        'reassessment': r'(?:新辅助|化疗|治疗)后[^。；;\n]{0,25}(?:评估|手术|切除)|(?:再|重新|复)[^。；;\n]{0,4}(?:评估|分期)',
        'redirect_therapy': r'(?:建议|拟|行|予以|继续|转)[^。；;\n]{0,12}(?:新辅助|化疗|系统治疗|姑息治疗)|姑息性',
        'mdt': r'\bMDT\b|多学科|术前讨论',
    }
    compiled={k:re.compile(v,re.I) for k,v in patterns.items()}
    notes=defaultdict(list); totals=Counter(); errors=[]
    cols=['patient_uid','文书名称','文书内容','create_time','admission_time','source_record_id','content_sha256','source_file','source_row','VISIT_ID']
    for path in sources:
        try:
            table=pq.read_table(path, columns=cols, filters=[('patient_uid','in',list(selected))])
            totals['parquet_files_read']+=1
            for x in table.to_pylist():
                raw=x['文书内容']
                if isinstance(raw,bytes):
                    try: text=raw.decode('utf-8')
                    except UnicodeDecodeError:text=raw.decode('gb18030')
                else:text=str(raw or '')
                text=re.sub(r'<[^>]+>','\n',text)
                typ=x['文书名称'] or ''
                if re.search(r'护理|同意书|交接|核查|物品|告知|申请单|报告卡',typ):continue
                if not re.search(r'胰|PDAC|pancrea',text,re.I):continue
                date=x['create_time'] or x['admission_time']
                day=date.date().isoformat() if isinstance(date,dt.datetime) else str(date or '')[:10]
                hits={k:[{'start':m.start(),'end':m.end(),'quote':text[max(0,m.start()-35):min(len(text),m.end()+70)]} for m in list(p.finditer(text))[:3]] for k,p in compiled.items()}
                hits={k:v for k,v in hits.items() if v}
                body_clock=bool(re.search(r'20\d{2}[-年/.]\d{1,2}[-月/.]\d{1,2}[日\sT]*\d{1,2}[:：]\d{2}',text))
                notes[x['patient_uid']].append({'day':day,'recorded_datetime':str(x['create_time'] or ''),'document_type':typ,'source_record_id':x['source_record_id'],'source_parquet':str(path),'source_file':x['source_file'],'source_row':x['source_row'],'visit_id':x['VISIT_ID'],'body_sha256':hashlib.sha256(text.encode()).hexdigest(),'body_clock_present':body_clock,'signals':hits,'text':text})
        except Exception as e:errors.append({'path':str(path),'error':repr(e)})
    assert not errors, errors
    patient_summary=[]; signal_patients=Counter(); body_time_patients=0
    for uid,p in selected.items():
        ns=notes[uid]; unique={n['body_sha256']:n for n in ns}
        sig={k:sum(k in n['signals'] for n in unique.values()) for k in patterns}
        for k,v in sig.items():signal_patients[k]+=bool(v)
        body_time_patients+=any(n['body_clock_present'] for n in ns)
        signal_days=sorted({n['day'] for n in unique.values() if n['signals'].get('surgery_assessment') and n['day']})
        repeated=defaultdict(set)
        for n in unique.values():
            for h in n['signals'].get('surgery_assessment',[]):
                repeated[h['quote']].add(n['day'])
        patient_summary.append({'alias':p['alias'],'patient_uid':uid,'stratum':strata[uid],'pancreas_note_rows':len(ns),'unique_full_text_notes':len(unique),'qualified_exam_group_proxies':p['exam_group_proxy_count'],'signal_document_counts':sig,'surgical_signal_days':signal_days,'repeated_surgical_context_across_days':any(len(days)>=2 for days in repeated.values()),'body_clock_present':any(n['body_clock_present'] for n in ns)})
    reports=[]
    with (root/'p1_quality_v0.2/results/report_quality.jsonl').open() as f:
        for line in f:
            r=json.loads(line)
            if r['patient_uid'] in selected and r['quality_state']=='PASS':reports.append(r)
    raw_images={}; image_keys=Counter(); image_time_keys=Counter()
    image_paths = {ref['patient_file'] for r in reports for ref in r['source_refs']}
    skipped_images = []
    for path in image_paths:
        if Path(path).stat().st_flags & (1 << 30):
            skipped_images.append(path)
            continue
        with Path(path).open() as f:
            for line in f:
                x=json.loads(line)
                raw_images[(x['patient_uid'],x.get('index_report_uid'))]=x
    image_report_records_loaded = 0
    for r in reports:
        xs=[raw_images[(r['patient_uid'],ref['index_report_uid'])] for ref in r['source_refs'] if (r['patient_uid'],ref['index_report_uid']) in raw_images]
        if not xs:continue
        x=xs[0]  # Field inventory only, not report-content adjudication.
        image_report_records_loaded += 1
        image_keys.update(x.keys())
        image_time_keys.update(k for k,v in x.items() if v and not k.endswith('_status') and re.search(r'(report|sign|release|available|报告|签发).*(date|time|日期|时间)|(date|time).*(report|sign|release)',k,re.I))
    result={
        'date':'2026-10-20','seed':20261020,'sample_patients':30,'stratum_counts':dict(Counter(strata.values())),
        'clinical_parquet_files_in_frozen_manifest':len(all_sources),
        'clinical_parquet_files_dataless_skipped':len(all_sources)-len(sources),
        'readable_clinical_source_rows':readable_source_rows,
        'passed_patients_in_readable_file_pool':len(available_audit),
        'parquet_files_read':totals['parquet_files_read'],'pancreas_note_rows':sum(len(v) for v in notes.values()),
        'unique_full_text_notes_within_patient':sum(p['unique_full_text_notes'] for p in patient_summary),
        'patients_with_signal':dict(signal_patients),'patients_with_multiple_surgical_signal_days':sum(len(p['surgical_signal_days'])>=2 for p in patient_summary),
        'patients_with_identical_surgical_context_across_recorded_days':sum(p['repeated_surgical_context_across_days'] for p in patient_summary),
        'patients_with_body_clock':body_time_patients,'qualified_report_versions':len(reports),
        'qualified_image_report_versions_with_original_record_loaded':image_report_records_loaded,
        'image_patient_files_dataless_skipped':len(skipped_images),
        'image_field_names':sorted(image_keys),'nonempty_explicit_image_report_time_fields':dict(image_time_keys),
        'source_read_errors':errors,'meaning':'Convenience sample from currently hydrated files, stratified and enriched for quality/time boundaries; not whole-cohort exploration, episode validation or population prevalence. Unreadable cloud placeholders are not missing clinical data. Recorded dates and body clocks do not prove information availability.',
        'signal_patterns':patterns,
    }
    (args.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (args.output/'patients.json').write_text(json.dumps(patient_summary,ensure_ascii=False,indent=2)+'\n')
    with (args.output/'clinical_notes.jsonl').open('w') as f:
        for uid,ns in notes.items():
            for n in ns:f.write(json.dumps({'patient_uid':uid,**n},ensure_ascii=False)+'\n')
    manifest={'input_manifest_sha256':hashlib.sha256((root/'p1_quality_v0.2/results/input_manifest.json').read_bytes()).hexdigest(),'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'patient_selection':patient_summary,'input_paths_and_recorded_hashes':{str(p):index[str(p)] for p in sources}}
    (args.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
