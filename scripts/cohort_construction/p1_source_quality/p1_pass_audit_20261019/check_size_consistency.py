"""Narrow same-report size comparison; never silently correct a measurement."""
from pathlib import Path
from collections import Counter
import hashlib,json,re,sys

HERE=Path(__file__).resolve().parent;BASE=HERE.parent/'p1_quality_v0.2'
sys.path.insert(0,str(BASE));from report_parser import normalize
ps={p['patient_uid']:p for p in map(json.loads,(BASE/'results/patient_quality.jsonl').open()) if p['state']=='PASS_V2'}
dim=re.compile(r'(\d+(?:\.\d+)?)\s*(mm|cm|毫米|厘米)?\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(mm|cm|毫米|厘米)?(?:\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(mm|cm|毫米|厘米)?)?',re.I)
cache={};checked=0;compared=[];mismatch=[]
for r in map(json.loads,(BASE/'results/report_quality.jsonl').open()):
    if r['patient_uid'] not in ps or r['quality_state']!='PASS':continue
    d=None
    for ref in r['source_refs']:
        path=Path(ref['patient_file'])
        if str(path) not in cache:cache[str(path)]=[json.loads(x) for x in path.open()]
        for x in cache[str(path)]:
            t=normalize(x.get('original_findings'))+'\n'+normalize(x.get('original_impression'))
            if x.get('index_report_uid')==ref['index_report_uid'] and hashlib.sha256(t.encode()).hexdigest()==r['parsed']['normalized_text_sha256']:d=x;break
        if d:break
    assert d
    text=normalize(d.get('original_findings'))+'\n'+normalize(d.get('original_impression'))
    candidates=[]
    for line in text.split('\n'):
        if '胰腺癌' not in line:continue
        for m in re.finditer(r'[（(]([^（）()\n]*)[）)]',line):
            block=m[1]
            if not re.search(r'胰|钩突',block):continue
            dims=[]
            for hit in dim.finditer(block):
                units=[hit[i] for i in [2,4,6] if hit[i]]
                if not units:continue
                vals=[float(hit[i])*(10 if (hit[i+1] or units[-1]).lower() in ['cm','厘米'] else 1) for i in [1,3,5] if hit[i]]
                if all(v>0 for v in vals):dims.append({'values_mm':vals,'quote':hit.group()})
            if len(dims)==1:candidates.append({'line':line,'measurement':dims[0]})
    base=r['parsed']['lesion_size'].get('values_mm',[])
    if not candidates or len(base)!=2:continue
    candidates=[x for x in candidates if len(x['measurement']['values_mm'])==2]
    if not candidates:continue
    checked+=1;bad=[]
    for x in candidates:
        a=sorted(base);b=sorted(x['measurement']['values_mm']);absolute=[abs(x-y) for x,y in zip(a,b)];relative=[abs(x-y)/max(x,y) for x,y in zip(a,b)]
        if any(z>0.01 for z in absolute):bad.append({**x,'absolute_axis_difference_mm':absolute,'relative_axis_difference':relative,'large_difference':any(z>=3 and v>0.2 for z,v in zip(absolute,relative))})
    if bad:mismatch.append({'alias':ps[r['patient_uid']]['alias'],'patient_uid':r['patient_uid'],'report_id':r['quality_report_id'],'date':r['date'],'method':r['method'],'selected_lesion_measurement':r['parsed']['lesion_size'],'diagnostic_discrepancies':bad})
summary={'report_versions_with_comparable_pancreatic_cancer_diagnostic_parenthesis':checked,'report_versions_with_different_size':len(mismatch),'patients_with_different_size':len({x['patient_uid'] for x in mismatch}),
 'report_versions_with_large_difference':sum(any(x['large_difference'] for x in m['diagnostic_discrepancies']) for m in mismatch),
 'patients_with_large_difference':len({m['patient_uid'] for m in mismatch if any(x['large_difference'] for x in m['diagnostic_discrepancies'])}),
 'threshold_note':'Axis order ignored; two-dimensional measurements only. Any >0.01 mm is a difference; large requires >=3 mm AND >20% relative difference in at least one axis. This is an audit triage threshold, not a clinical change criterion.',
 'limitation':'Only diagnostic parentheses with a pancreatic site on a line containing 胰腺癌 are compared. Misses other wording and cannot certify same-lesion identity. No automatic value replacement or patient exclusion.'}
(HERE/'size_consistency_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));(HERE/'size_consistency_cases.json').write_text(json.dumps(mismatch,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False))
