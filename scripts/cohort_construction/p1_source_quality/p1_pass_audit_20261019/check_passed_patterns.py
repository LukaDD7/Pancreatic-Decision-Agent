"""Additional consistency signals; all are descriptive, no membership changes."""
from pathlib import Path
from collections import defaultdict,Counter
import json,random

HERE=Path(__file__).resolve().parent;BASE=HERE.parent/'p1_quality_v0.2/results'
ps={p['patient_uid']:p for p in map(json.loads,(BASE/'patient_quality.jsonl').open()) if p['state']=='PASS_V2'}
rr=[r for r in map(json.loads,(BASE/'report_quality.jsonl').open()) if r['patient_uid'] in ps and r['quality_state']=='PASS']
days=defaultdict(lambda:defaultdict(list));exam=defaultdict(list)
for r in rr:
    days[(r['patient_uid'],r['date'])][r['parsed']['modality']].append(r)
    eno=str(r['exam_no'] or '').strip()
    if eno.endswith('.0'):eno=eno[:-2]
    if len(eno)>=6 and eno.lower() not in {'none','null','nan','未知'}:exam[(r['patient_uid'],r['date'],r['parsed']['modality'],eno)].append(r)
same_day=[];within_exam=[]
for (u,date),md in days.items():
    if not(md.get('CT') and md.get('MR')):continue
    discordant=[]
    for k in ['SMA','CA','CHA','PV','SMV']:
        ct={r['parsed']['vessels'][k]['state'] for r in md['CT']};mr={r['parsed']['vessels'][k]['state'] for r in md['MR']}
        if ('NO_CONTACT_OR_INVASION' in ct and 'CONTACT_OR_INVASION' in mr) or ('CONTACT_OR_INVASION' in ct and 'NO_CONTACT_OR_INVASION' in mr):discordant.append(k)
    if discordant:same_day.append({'alias':ps[u]['alias'],'patient_uid':u,'date':date,'vessels':discordant,'CT_report_ids':[r['quality_report_id'] for r in md['CT']],'MR_report_ids':[r['quality_report_id'] for r in md['MR']]})
for (u,date,modality,eno),rs in exam.items():
    discordant=[]
    for k in ['SMA','CA','CHA','PV','SMV']:
        st={r['parsed']['vessels'][k]['state'] for r in rs}
        if 'NO_CONTACT_OR_INVASION' in st and 'CONTACT_OR_INVASION' in st:discordant.append(k)
    if discordant:within_exam.append({'alias':ps[u]['alias'],'date':date,'modality':modality,'vessels':discordant,'report_ids':[r['quality_report_id'] for r in rs]})
out={'same_day_CT_MR_groups':sum(bool(m.get('CT') and m.get('MR')) for m in days.values()),'same_day_CT_MR_opposite_relation_groups':len(same_day),'patients_with_same_day_CT_MR_opposite_relation':len({x['patient_uid'] for x in same_day}),
     'same_exam_no_opposite_relation_groups':len(within_exam),'same_day_CT_MR_details':same_day,'same_exam_details':within_exam,
     'important':'CT/MR disagreement can be clinical uncertainty; not proof of bad source data. No P1 reclassification.'}
(HERE/'cross_report_consistency.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps({k:v for k,v in out.items() if k not in ['same_day_CT_MR_details','same_exam_details']},ensure_ascii=False))
