"""Prepare the same fixed ten patients with all original source families."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
from .core import project_sources, operative_partition, resource_selection

ROOT=Path(__file__).resolve().parents[3]
BASE=ROOT/'private/p2_pathway_checkpoint_scale10_v0.1_20261023'
OUT=ROOT/'private/p2_pathway_checkpoint_scale10_v0.2_20261024'

def prepare_one(chosen):
    cid=chosen['case_id'];old=json.loads((BASE/cid/'sources.json').read_text())
    payload=Path(old['source_container']).read_bytes()
    assert hashlib.sha256(payload).hexdigest()==old['source_container_sha256']
    ss,inv=project_sources(json.loads(payload),chosen['patient_uid'])
    byid={s['source_id']:s for s in ss}
    for i,d in enumerate(old['documents'],1):
        assert d['source_id'] in byid
        assert byid[d['source_id']]['text']==d['text']
        byid[d['source_id']]['source_document_index']=f'D{i:03}'
    r=json.loads((BASE/cid/'trial_result.json').read_text())
    preop=next(m for m in r['mixed_document_masks'] if m['mask_id'].startswith('PREOP'))
    op=next(m for m in r['mixed_document_masks'] if m['mask_id'].startswith('INTRAOP'))
    optext=byid[op['source_id']]['text']
    start=re.search(r'手术时间：(\d{4}-\d\d-\d\d \d\d:\d\d)',optext).group(1)+':00'
    pretime=byid[preop['source_id']]['clocks']['body_record_time']['value']
    # Encounter start scopes lab retrieval, never defines episode boundaries.
    interval=byid[preop['source_id']]['clocks']['encounter_admission_time']['value']
    if not interval:raise ValueError(('No declared source admission boundary',cid))
    current_reports=[];old_reports=[];lab_days={};trajectories=[]
    names=re.compile('白细胞计数|血小板计数|总胆红素|直接胆红素|肌钙蛋白|CA199|CA19-9|淀粉酶|脂肪酶|C反应蛋白|凝血酶原时间')
    for s in ss:
        if s['kind'] not in ['imaging','lab','pathology']:continue
        a=resource_selection(s,start,interval)
        if s['kind']=='imaging':
            entry={k:s[k] for k in ['source_id','title','clocks','provenance']};entry['selection']=a
            if a['disposition']=='INCLUDE':
                entry['original_fields']=s['original_fields'];current_reports.append(entry)
            elif a['reason']=='EARLIER_CONTEXT_NOT_SILENTLY_DISCARDED':old_reports.append(entry)
        if s['kind']=='lab' and a['disposition']=='INCLUDE':
            day=s['clocks']['report_time']['value'][:10];lab_days[day]=lab_days.get(day,0)+1
            if names.search(str(s['title'])):
                trajectories.append(dict(source_id=s['source_id'],title=s['title'],report_time=s['clocks']['report_time']['value'],raw_result=s['original_fields']['检验结果值'],raw_unit=s['original_fields']['检验结果单位']))
    base=OUT/cid;base.mkdir(parents=True,exist_ok=True)
    (base/'sources.json').write_text(json.dumps(dict(case_id=cid,patient_uid=chosen['patient_uid'],source_container=old['source_container'],source_container_sha256=old['source_container_sha256'],sources=ss,source_family_inventory=inv),ensure_ascii=False,indent=2)+'\n')
    review=dict(case_id=cid,main_current_admission_start=interval,preop_body_time=pretime,operation_start=start,
                source_family_inventory=inv,current_preop_imaging=current_reports,earlier_imaging_metadata=old_reports,
                laboratory_rows_by_report_day=lab_days,key_laboratory_trajectories=trajectories,
                operative_phase_proposal=operative_partition(byid[op['source_id']]))
    (base/'review_materials.json').write_text(json.dumps(review,ensure_ascii=False,indent=2)+'\n')
    return dict(case_id=cid,inventory=inv,projected_sources=len(ss),current_preop_imaging=len(current_reports),preop_lab_rows=sum(lab_days.values()))

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((BASE/'sampling_manifest.json').read_text())
    (OUT/'sampling_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    protocol=dict(version='two-principle-source-replay-v0.2',date='2026-10-24',baseline=str(BASE),same_seed_and_patients=True,
                  unchanged_core_rule_hashes=manifest['rules_sha256'],source_scope='Original documents, imaging text, original lab cells, raw pathology dates/text, quarantine inventory. No outcome summaries/Clinical Reference/generated graph.',
                  changes=['Multisource coverage and per-source selection reasons','Original clock meanings and explicit time precision','Retrospective clinical phase reconstruction','Explicit semantic review of assessment rounds, not one event per document','Whole-packet current-answer checking and prior-plan preservation'],
                  limits='Development rerun; semantic decisions by same Codex assistant, not independent gold or clinical accuracy evaluation.')
    (OUT/'run_protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2)+'\n')
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:results=list(ex.map(prepare_one,manifest['selected']))
    (OUT/'source_preparation_summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    for r in results:print(json.dumps(r,ensure_ascii=False))

if __name__=='__main__':main()
