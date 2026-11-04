"""Compare frozen patient/report versions and preserve all membership transitions."""
from pathlib import Path
from collections import Counter,defaultdict
import csv,hashlib,json

HERE=Path(__file__).resolve().parent;OLD=HERE.parent/'p1_quality_v0.1';NEW=HERE/'results'
def read(p):return [json.loads(x) for x in p.open() if x.strip()]
def write(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf-8')
def cls(p):return p['state'].rsplit('_V',1)[0]
op={p['patient_uid']:p for p in read(OLD/'results/patient_quality.jsonl')};np={p['patient_uid']:p for p in read(NEW/'patient_quality.jsonl')}
orr={r['quality_report_id']:r for r in read(OLD/'results/report_quality.jsonl')};nr={r['quality_report_id']:r for r in read(NEW/'report_quality.jsonl')}
assert set(op)==set(np) and len(np)==2707
assert set(orr)==set(nr),'Content versions changed despite fixed inputs'
om=json.loads((OLD/'results/input_manifest.json').read_text());nm=json.loads((NEW/'input_manifest.json').read_text())
common=set(om)&set(nm)
assert all(om[p]==nm[p] for p in common),'A shared source file changed'
assert hashlib.sha256((OLD/'inputs/P0_index_snapshot.csv').read_bytes()).hexdigest()==hashlib.sha256((HERE/'inputs/P0_index_snapshot.csv').read_bytes()).hexdigest()
transitions=Counter();deltas=[];by_patient=defaultdict(list);report_transitions=Counter();report_deltas=[]
for q,r in nr.items():
    before=orr[q];report_transitions[before['quality_state']+' -> '+r['quality_state']]+=1
    by_patient[r['patient_uid']].append(r)
    if before['quality_state']!=r['quality_state']:
        report_deltas.append({'report_id':q,'patient_uid':r['patient_uid'],'before':before['quality_state'],'after':r['quality_state'],
           'before_reasons':before['reason_codes'],'after_reasons':r['reason_codes'],
           'new_quality_flags':sorted(set(f for v in r['parsed']['vessels'].values() for f in v.get('quality_flags',[])))})
groups=defaultdict(list);best_combinations=Counter()
for u,p in np.items():
    old=op[u];a,b=cls(old),cls(p);transitions[a+' -> '+b]+=1
    if a!=b:
        gained=[q for q in p['qualifying_report_ids'] if q not in old['qualifying_report_ids']]
        old_blocks=sorted(set(c for q in gained for c in orr[q]['reason_codes']))
        flags=sorted(set(f for q in gained for v in nr[q]['parsed']['vessels'].values() for f in v.get('quality_flags',[])))
        deltas.append({'alias':p['alias'],'patient_uid':u,'before':a,'after':b,'clinical_notes':p['qualified_clinical_notes'],
                       'gained_qualifying_report_ids':gained,'old_blocking_codes_on_gained_reports':old_blocks,'new_nonblocking_flags':flags,
                       'current_patient_reasons':p['reason_codes']})
    if b!='REVIEW':continue
    rr=by_patient[u];passed=[r for r in rr if r['quality_state']=='PASS'];rv=[r for r in rr if r['quality_state']=='REVIEW'];nonpost=[r for r in rv if 'POSTOPERATIVE_OR_RECURRENCE_CONTEXT' not in r['reason_codes']]
    def codes(r):return sorted(set(c for c in r['reason_codes'] if not c.startswith('VESSEL_CONFLICT_')))
    best=min(passed or nonpost or rv,key=lambda r:(len(codes(r)),not r['has_verified_source'],not r['parsed']['template_detected'],r['quality_report_id']))
    bc=codes(best);union={k for r in nonpost for k,v in r['parsed']['vessels'].items() if v['represented']}
    if passed:g='A_影像通过但患者级上下文待补'
    elif not nonpost:g='B_仅术后语境候选报告'
    elif bc==['VESSEL_SMA_EMPTY_ANGLE_FIELD']:g='C_仅SMA空模板栏阻断'
    elif bc==['RAW_CLUSTER_NOT_RECONCILED']:g='D_仅来源核对阻断'
    elif len(bc)<=2:g='E_其余一至两个阻断项'
    elif not union:g='F_五血管均未抽到有效关系'
    else:g='G_有部分血管信息但多项未满足'
    groups[g].append({'alias':p['alias'],'patient_uid':u,'best_report_id':best['quality_report_id'],'best_reasons':bc})
    best_combinations['|'.join(bc)]+=1
assert not any(cls(op[u])=='PASS' and cls(p)!='PASS' for u,p in np.items()),'A previous PASS was lost; inspect before release'
assert sum(transitions.values())==2707
summary={'version':'v0.1 -> v0.2','patient_transition_counts':transitions,'report_state_transition_counts':report_transitions,
         'changed_patients':len(deltas),'changed_report_states':len(report_deltas),'remaining_review_groups':{k:len(v) for k,v in sorted(groups.items())},
         'remaining_review_best_reason_combinations':best_combinations,'shared_source_files_hash_identical':len(common),'P0_membership_identical':True,'report_content_versions_identical':True,
         'input_output_hashes':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [OLD/'results/patient_quality.jsonl',OLD/'results/report_quality.jsonl',NEW/'patient_quality.jsonl',NEW/'report_quality.jsonl']}}
write(NEW/'comparison_summary.json',summary);write(NEW/'patient_delta.json',deltas);write(NEW/'report_state_delta.json',report_deltas);write(NEW/'remaining_review_groups.json',groups)
with (NEW/'patient_delta.csv').open('w',encoding='utf-8-sig',newline='') as f:
    cols=['alias','patient_uid','before','after','clinical_notes','gained_qualifying_report_ids','old_blocking_codes_on_gained_reports','new_nonblocking_flags','current_patient_reasons'];w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
    for d in deltas:w.writerow({k:'|'.join(d[k]) if isinstance(d[k],list) else d[k] for k in cols})
print(json.dumps(summary,ensure_ascii=False))
