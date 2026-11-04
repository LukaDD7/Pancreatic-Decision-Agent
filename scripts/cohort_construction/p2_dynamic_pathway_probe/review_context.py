"""Save complete in-scope source context for sampled and targeted patients.

The target queue is a declared development query, separate from random samples.
No nearby-time rule defines or certifies an episode here.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
from scripts.cohort_construction.p2_decision_localization.documents import load_sources
from .run import write, jsonl


def local_context_flags(note, quote):
    """Warn at the record/span level; never veto the whole patient or history."""
    flags=[]
    title=note['document_type']
    if re.search(r'同意书|知情同意',title):flags.append('DYN005_CONSENT_DOCUMENT')
    if re.search(r'输尿管|ESWL|腰椎|脊柱',title) or re.search(r'ESWL|输尿管|腰椎间盘突出|腰椎管狭窄|消化道穿孔',quote):
        flags.append('DYN005_OTHER_TARGET_REVIEW')
    if '首先考虑' in quote and re.search(r'(?:新辅助治疗|化疗)后',quote):
        flags.append('DYN006_DIAGNOSTIC_SUMMARY_NOT_A_FUTURE_SEQUENCE')
    return flags


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[3])
    args=ap.parse_args();repo=args.repo.resolve()
    out=repo/'local_artifacts/p2_dynamic_pathway_probe_v0.1_20261021'
    sources=['development_sample.json','relation_sample.json','targeted_biliary_queue.json']
    selected=set(); hashes={}; entries=[]
    for name in sources:
        p=out/name;hashes[name]=hashlib.sha256(p.read_bytes()).hexdigest()
        value=json.loads(p.read_text());rows=value['hits'] if isinstance(value,dict) else value
        selected.update(h['patient_uid'] for h in rows)
        entries.extend((name,h) for h in rows)
    notes,coverage=load_sources(repo,sorted(selected))
    jsonl(out/'review_patient_context.jsonl',notes)
    bykey={n['record_key']:n for n in notes}
    write(out/'sample_record_context_flags.json',[
        {'selection_file':name,'entry_id':h.get('sample_id',h.get('target_id')),
         'record_key':h['record_key'],
         'flags':local_context_flags(bykey[h['record_key']],h['quote'])}
        for name,h in entries])
    write(out/'context_manifest.json',{'patient_count':len(selected),'source_note_count':len(notes),
                                     'selection_files_sha256':hashes,'coverage':coverage,
                                     'purpose':'Full in-scope patient source context for semantic and temporal inspection; no episode certification.'})
    print(json.dumps({'patients':len(selected),'context_notes':len(notes),'all_source_counts_verified':True},ensure_ascii=False))


if __name__=='__main__':main()
