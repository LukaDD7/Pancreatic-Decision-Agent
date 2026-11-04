"""Characterize documented decision anchors in the frozen 30-patient probe.

Patterns locate language for review. Counts are neither episode counts nor
validated clinical assessment labels. Patient excerpts are private outputs only.
"""
from pathlib import Path
from collections import Counter
import argparse, datetime as dt, hashlib, json, re


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(s) for s in args.input.read_text().splitlines() if s.strip()]
    patterns = {
        'standard_resectability_terms': r'交界可切除|潜在可切除|不可切除|可切除|切除性|局部进展',
        'positive_indication_statement': r'有手术指征|存在手术指征|手术指征明确',
        'historical_negative_exact_phrase': r'考虑当时无手术指征',
        'curative_pancreas_plan': r'(?:拟|建议|考虑|计划)[^。；;\n]{0,50}(?:胰[^。；;\n]{0,16}根治(?:术|性切除|性大部切除)|胰[^。；;\n]{0,10}根治性[^。；;\n]{0,10}切除)',
        'fitness_negative_contraindication': r'(?:无|未见|排除)[^。；;\n]{0,5}手术禁忌',
        'current_clinical_assessment_marker': r'目前[^。；;\n]{0,65}(?:手术|切除)|(?:会诊意见|治疗计划|诊疗计划|医师总结)[^。；;\n]{0,80}(?:手术|切除)',
        'future_general_chemotherapy_reassessment': r'每[^。；;\n]{0,12}周期化疗后[^。；;\n]{0,40}评估疗效',
        'targeted_resectability_workup': r'(?:评估|明确|判断)[^。；;\n]{0,12}(?:可切除性|能否[^。；;\n]{0,5}(?:手术|切除))|(?:MRI|CT|MR)[^。；;\n]{0,15}(?:可切除性|能否切除)',
    }
    compiled = {k: re.compile(v) for k, v in patterns.items()}
    docs = Counter(); pats = {k:set() for k in patterns}; examples = []
    header_count = header_mismatch = 0
    mismatch_patients = set()
    unrelated_title = []
    for row in rows:
        text = row['text']
        for key, regex in compiled.items():
            matches = list(regex.finditer(text))
            if not matches: continue
            docs[key] += 1; pats[key].add(row['patient_uid'])
            if sum(e['category']==key for e in examples) < 8:
                for m in matches[:1]:
                    examples.append({'category':key,'patient_uid':row['patient_uid'],
                        'source_record_id':row['source_record_id'],'document_type':row['document_type'],
                        'recorded_datetime':row['recorded_datetime'],'start':m.start(),'end':m.end(),
                        'matched':m.group(0),'context':text[max(0,m.start()-80):min(len(text),m.end()+140)],
                        'body_sha256':row['body_sha256']})
        if re.search(r'颈椎|颈后路', row['document_type']):
            unrelated_title.append({'source_record_id':row['source_record_id'],'patient_uid':row['patient_uid'],'document_type':row['document_type']})
        # Only a leading header date; other dates anywhere in a note are not
        # accepted as current assessment times (they may be historical events).
        m = re.match(r'\s*(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})日?',text)
        if m:
            try: day = dt.date(*map(int,m.groups())).isoformat()
            except ValueError: continue
            header_count += 1
            if row['day'] and day != row['day']:
                header_mismatch += 1; mismatch_patients.add(row['patient_uid'])
    result = {
        'date':'2026-10-20','patients':len({r['patient_uid'] for r in rows}),'note_rows':len(rows),
        'language_patterns':{key:{'notes':docs[key],'patients':len(pats[key]),'pattern':pattern} for key,pattern in patterns.items()},
        'notes_with_leading_body_header_date':header_count,
        'leading_header_date_differs_from_record_date_notes':header_mismatch,
        'leading_header_date_differs_from_record_date_patients':len(mismatch_patients),
        'unrelated_cervical_surgery_title_notes':len(unrelated_title),
        'unrelated_cervical_surgery_title_patients':len({x['patient_uid'] for x in unrelated_title}),
        'input_sha256':hashlib.sha256(args.input.read_bytes()).hexdigest(),
        'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'limitations':'Same convenience sample as the earlier probe, not new patients or a validation set. Pattern hits are not current resectability decisions. Header date differences do not by themselves establish which time is correct. No original imaging signature times were newly verified.',
    }
    (args.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (args.output/'private_examples.json').write_text(json.dumps(examples,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
