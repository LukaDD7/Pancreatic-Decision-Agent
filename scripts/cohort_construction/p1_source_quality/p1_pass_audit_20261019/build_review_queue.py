"""Join audit signals into a review table; retain every frozen PASS patient."""
from pathlib import Path
import csv,json

HERE=Path(__file__).resolve().parent
patients=json.loads((HERE/'patient_audit.json').read_text());sizes=json.loads((HERE/'size_consistency_cases.json').read_text());cross=json.loads((HERE/'cross_report_consistency.json').read_text())
size_any={x['patient_uid'] for x in sizes};size_large={x['patient_uid'] for x in sizes if any(y['large_difference'] for y in x['diagnostic_discrepancies'])}
source_aliases={x['alias'] for x in cross['same_exam_details']};multi={x['patient_uid'] for x in cross['same_day_CT_MR_details']}
out=[]
for p in patients:
    u=p['patient_uid'];flags=[]
    if p['alias'] in source_aliases:flags.append('SOURCE_VERSION_OPPOSITE_VALUE')
    if u in size_large:flags.append('SIZE_LARGE_DIFFERENCE_CANDIDATE')
    elif u in size_any:flags.append('SIZE_DIFFERENCE_CANDIDATE')
    if u in multi:flags.append('SAME_DAY_CT_MR_DISAGREEMENT')
    if p['nearest_image_note_gap_days']>30:flags.append('IMAGE_NOTE_RECORDED_DATE_GAP_GT30D')
    if not p['in_window_report_versions']:flags.append('ONLY_PREWINDOW_QUALIFIED_IMAGE')
    out.append({**p,'frozen_cohort_status':'PASS_V2','audit_review_flags':flags})
assert len(out)==2004
(HERE/'patient_quality_with_audit.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
cols=['alias','patient_uid','frozen_cohort_status','qualified_content_versions','exam_group_proxy_count','first_qualifying_date','last_qualifying_date','clinical_note_count','nearest_image_note_gap_days','has_original_cluster_proof','all_qualified_reports_flagged','audit_review_flags']
with (HERE/'patient_quality_with_audit.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
    for x in out:w.writerow({k:'|'.join(x[k]) if isinstance(x[k],list) else x[k] for k in cols})
print(json.dumps({'patients':len(out),'patients_with_any_new_review_flag':sum(bool(x['audit_review_flags']) for x in out),'cohort_changed':False},ensure_ascii=False))
