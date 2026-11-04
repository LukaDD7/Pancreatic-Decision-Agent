"""Declared development query for explicit wait/improvement-to-surgery wording.

Run after the two fixed samples exposed generic temporal-word/target noise.
This is purposeful sampling, with a reproducible order; not a new validation set.
"""
from pathlib import Path
import argparse
import json
import re
from .run import write


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[3])
    args=ap.parse_args();repo=args.repo.resolve()
    out=repo/'local_artifacts/p2_dynamic_pathway_probe_v0.1_20261021'
    hits=[json.loads(s) for s in (out/'hits.jsonl').read_text().splitlines()]
    notes={n['record_key']:n for n in map(json.loads,(out/'matched_notes.jsonl').read_text().splitlines())}
    candidates=[h for h in hits if h['pattern']=='D01_BILIARY_CONDITION'
                and re.search(r'(?:待|减黄后|改善后|好转后|下降后|恢复后)[^。；;\n]{0,55}(?:手术|切除|外科)',h['quote'])
                and re.search('胰|肿瘤',notes[h['record_key']]['text'])]
    selected=[];seen=set()
    for h in sorted(candidates,key=lambda h:(h['family'] not in ['CONSULT','FIRST_SENIOR_ROUND','WARD_ROUND'],len(h['quote']),h['record_key'])):
        if h['patient_uid'] in seen:continue
        seen.add(h['patient_uid']);selected.append(h)
        if len(selected)==12:break
    write(out/'targeted_biliary_queue.json',{
        'selection':'D01 broad hits with explicit waiting/improvement-to-surgery phrase; pancreas/tumor mention somewhere in source note; sorted physician context, shortest quote; first12 distinct patients. These are not clinical acceptance criteria.',
        'hits':[{**h,'target_id':f'T{i:02d}'} for i,h in enumerate(selected,1)]})
    print(json.dumps({'candidates':len(candidates),'selected':len(selected)},ensure_ascii=False))


if __name__=='__main__':main()
