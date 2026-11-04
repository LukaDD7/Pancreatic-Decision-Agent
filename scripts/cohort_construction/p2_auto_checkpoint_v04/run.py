"""Run the unchanged fixed-ten source cohort with v0.4 document semantics."""
import argparse
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from scripts.cohort_construction.p2_auto_checkpoint.extract import read, write, digest
from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources,build,validate
from .engine import process_sources

ROOT=Path(__file__).resolve().parents[3]


def file_hash(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def run_case(case_id,args,config):
    path=args.sources/case_id/'source_fhir_r4_bundle.json'
    source_input=read(path);sources,receipt=import_sources(source_input)
    out=args.output/case_id;out.mkdir(parents=True,exist_ok=True)
    write(out/'source_import_receipt.json',dict(input_path=str(path),sha256=file_hash(path),**receipt))
    # Regenerate a source-only bundle, never carry research Basic labels into
    # semantic input. The original raw source hashes survive unchanged.
    source_bundle=build(sources,case_id,[],[]);validate(source_bundle,args.schema)
    write(out/'source_fhir_r4_bundle.json',source_bundle)
    events,episodes,summary=process_sources(case_id,sources,config,out,args.cache,args.unit_workers)
    annotations=[]
    for e in events:
        small={k:v for k,v in e.items() if k not in ['evidence_context','actual_model_evidence']}
        small['evidence_context_audit_file']=str(out/'decision_events.json')
        small['semantic']=dict(supports=e['action_citations']['valid'])
        small['evolution']=dict(supports=e.get('validated_observation_citations',{}).get('valid',[]),
            connection_supports=e.get('validated_connection_citations',{}).get('valid',[]))
        annotations.append(small)
    result=build(sources,case_id,annotations,episodes)
    summary['fhir_validation']=validate(result,args.schema)
    write(out/'research_fhir_r4_bundle.json',result);write(out/'summary.json',summary)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cache',type=Path,required=True)
    parser.add_argument('--config',type=Path,default=ROOT/'config/p2_auto_checkpoint_v05.json')
    parser.add_argument('--schema',type=Path,default=ROOT/'private/p2_auto_checkpoint_v0.1_20261024/standards/fhir.schema.json')
    parser.add_argument('--cases',nargs='*')
    parser.add_argument('--case-workers',type=int,default=3)
    parser.add_argument('--unit-workers',type=int,default=4)
    args=parser.parse_args();config=read(args.config);manifest=read(args.manifest)
    case_ids=[x['case_id'] for x in manifest['selected'] if not args.cases or x['case_id'] in args.cases]
    assert case_ids
    args.output.mkdir(parents=True,exist_ok=True)
    module_dir=Path(__file__).parent
    write(args.output/'run_contract.json',dict(version=config['version'],config=config,
        case_ids=case_ids,manifest_sha256=file_hash(args.manifest),seed=manifest['seed'],
        source_input_sha256={cid:file_hash(args.sources/cid/'source_fhir_r4_bundle.json') for cid in case_ids},
        code_sha256={str(p.relative_to(ROOT)):file_hash(p) for p in module_dir.glob('*.py')},
        annotation_references_read=False,prior_algorithm_outputs_read=False,
        source_fhir_annotations_ignored=True,clinical_validation=False,actual_model_calls=True))
    summaries=[]
    with ThreadPoolExecutor(max_workers=args.case_workers) as pool:
        futures={pool.submit(run_case,cid,args,config):cid for cid in case_ids}
        for f in as_completed(futures):
            cid=futures[f]
            try:summary=f.result()
            except Exception as exc:
                summary=dict(case_id=cid,status='FAILED',error=type(exc).__name__,detail=str(exc)[:200])
                write(args.output/cid/'failure.json',summary)
            summaries.append(summary)
            print(json.dumps(summary,ensure_ascii=False),flush=True)
            write(args.output/'summary.json',sorted(summaries,key=lambda x:x['case_id']))
    if any(s.get('status')=='FAILED' or s.get('failures',0) for s in summaries):raise SystemExit(1)


if __name__=='__main__':main()
