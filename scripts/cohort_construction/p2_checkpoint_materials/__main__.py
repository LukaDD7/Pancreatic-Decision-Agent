"""Prepare a researcher-only material review package from existing results."""
import argparse
import hashlib
import json
from pathlib import Path

from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources
from scripts.cohort_construction.p2_checkpoint_packet import compiler
from .prepare import MaterialPreparationError,prepare_material_review


def file_hash(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def prepare_files(reviewed_events,source_fhir,*,event_id,decision_task_id,visibility_policy_version,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    event_path=Path(reviewed_events);source_path=Path(source_fhir)
    receipt=dict(event_input_path=str(event_path.resolve()),source_fhir_input_path=str(source_path.resolve()),
        event_id=event_id,model_api_calls=0,clinical_reference_read=False,doctor_packet_created=False,
        source_fhir_annotations_ignored=True,full_patient_directory_copied=False,
        semantic_audit_status='NOT_ASSESSED',code_sha256={str(p):file_hash(p) for p in sorted(Path(__file__).parent.glob('*.py'))},
        packet_compiler_sha256=file_hash(Path(compiler.__file__)))
    write(output/'input_receipt.json',receipt)
    try:
        receipt.update(event_input_sha256=file_hash(event_path),source_fhir_input_sha256=file_hash(source_path))
        write(output/'input_receipt.json',receipt)
        events=json.loads(event_path.read_text())
        if isinstance(events,dict):events=[events]
        selected=[e for e in events if e.get('event_id')==event_id]
        if len(selected)!=1:raise ValueError('EVENT_ID_NOT_UNIQUE_IN_INPUT')
        sources,import_receipt=import_sources(json.loads(source_path.read_text()))
        manifest,proposal,compiled,audit=prepare_material_review(selected[0],sources,
            decision_task_id=decision_task_id,visibility_policy_version=visibility_policy_version)
        for name,value in [('selected_reviewed_event',selected[0]),('source_manifest',manifest),
                           ('split_proposal',proposal),('compiled_partition',compiled),('researcher_audit',audit)]:
            write(output/(name+'.json'),value)
        receipt.update(selected_event_sha256=compiler.canonical_sha256(selected[0]),source_import_receipt=import_receipt,
            manifest_sha256=compiler.canonical_sha256(manifest),proposal_sha256=compiler.canonical_sha256(proposal),
            compiled_sha256=compiled['compiled_sha256'],audit_sha256=audit['audit_sha256'],
            status='PREPARED_RESEARCHER_REVIEW_REQUIRED')
        if file_hash(event_path)!=receipt['event_input_sha256'] or file_hash(source_path)!=receipt['source_fhir_input_sha256']:
            raise ValueError('INPUT_CHANGED_DURING_PREPARATION')
    except Exception as exc:
        receipt.update(status='REJECTED',error=type(exc).__name__)
        if isinstance(exc,MaterialPreparationError):
            receipt['error_code']=exc.code;write(output/'preparation_failure_audit.json',exc.audit)
        write(output/'input_receipt.json',receipt)
        raise
    write(output/'input_receipt.json',receipt)
    return dict(status=receipt['status'],event_id=event_id,source_count=len(manifest['sources']),
        review_segments=audit['unresolved_review_segment_count'],segment_counts=audit['disposition_segment_counts'],
        character_counts=audit['disposition_character_counts'],semantic_audit_status='NOT_ASSESSED',
        doctor_packet_created=False,model_api_calls=0)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reviewed-events',type=Path,required=True)
    parser.add_argument('--event-id',required=True)
    parser.add_argument('--source-fhir',type=Path,required=True)
    parser.add_argument('--decision-task-id',required=True)
    parser.add_argument('--visibility-policy-version',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=prepare_files(args.reviewed_events,args.source_fhir,event_id=args.event_id,
        decision_task_id=args.decision_task_id,visibility_policy_version=args.visibility_policy_version,output=args.output)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
