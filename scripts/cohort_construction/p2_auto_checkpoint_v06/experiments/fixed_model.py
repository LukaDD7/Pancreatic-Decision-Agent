"""Prepare isolated, hash-protected DeepSeek continuation and cache preflight.

No evaluation Reference is read. Frozen requests are reused only under their
actual endpoint/body hash. The old cache and result folders remain read-only.
"""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from jsonschema import Draft202012Validator
from scripts.cohort_construction.p2_auto_checkpoint.extract import digest, read, write
from scripts.cohort_construction.p2_auto_checkpoint_v04 import engine, run
from scripts.cohort_construction.p2_auto_checkpoint_model_rerun.run import (
    ShapeRetryClient, REMINDER, body_for,
)


ROOT = Path(__file__).resolve().parents[4]
OLD = ROOT/'private/p2_auto_checkpoint_v0.5_deepseek_v4.1_flash_20261027'
SOURCES = ROOT/'private/p2_auto_checkpoint_v0.3_20261025/release_fixed10'
MANIFEST = ROOT/'private/p2_pathway_checkpoint_scale10_v0.1_20261023/sampling_manifest.json'
DEFAULT = ROOT/'private/p2_checkpoint_next_20261028/model_validation'
CASES = ('SCALE-03', 'SCALE-04', 'SCALE-10')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def frozen_files():
    # Include both old successful requests and all old run outputs. Avoid
    # symlink duplicates; the resolved target hash is still protected.
    return sorted({p.resolve() for p in OLD.rglob('*') if p.is_file()})


def prepare(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    protected = {str(p): sha(p) for p in frozen_files()}
    write(destination/'protected_old_hashes.json', protected)
    shutil.copytree(OLD/'model_cache', destination/'model_cache')
    source_hashes = {}
    for cid in CASES:
        source = SOURCES/cid/'source_fhir_r4_bundle.json'
        target = destination/'exact_source_inputs'/cid/source.name
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
        source_hashes[cid] = dict(original_path=str(source), snapshot_path=str(target), sha256=sha(target))
    shutil.copyfile(MANIFEST, destination/'sampling_manifest.json')
    config = read(ROOT/'config/p2_auto_checkpoint_v06_deepseek.json')
    config.update(include_support_documents=False, discovery_feedback_max_rounds=0,
        review_feedback_max_rounds=0, final_graph_validation=True,
        input_unit='complete_document_or_candidate_same_examination_report_group',
        experiment='fixed_model_technical_completion_with_final_graph_validation',
        model_change_and_code_change_separately_attributable=True)
    write(destination/'technical_completion_config.json', config)
    paths = []
    for package in ('p2_auto_checkpoint_v04', 'p2_auto_checkpoint_v06',
                    'p2_auto_checkpoint_v05_refine', 'p2_auto_checkpoint_model_rerun',
                    'p2_auto_checkpoint_shape_retry', 'p2_auto_checkpoint'):
        paths.extend((ROOT/'scripts/cohort_construction'/package).glob('*.py'))
    paths.extend(Path(__file__).parent.glob('*.py'))
    for path in paths:
        target = destination/'code_snapshot'/path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    receipt = dict(version='fixed-model-validation-1', cases=list(CASES),
        original_model='deepseek-v4.1-flash', new_model='deepseek-v4.1-flash',
        temperature=config['model_temperature'], max_output_tokens=config['max_output_tokens'],
        source_files=source_hashes, manifest_sha256=sha(destination/'sampling_manifest.json'),
        config_sha256=sha(destination/'technical_completion_config.json'),
        code_sha256={str(p.relative_to(ROOT)):sha(p) for p in paths},
        protected_old_files=len(protected), copied_successful_cache_files=len(list((destination/'model_cache').glob('*.json'))),
        source_context_expanded=False, semantic_feedback_enabled=False,
        final_graph_validation=True, reference_read=False, clinical_accuracy_measured=False)
    write(destination/'experiment_freeze.json',receipt)
    return receipt


class CacheOnlyClient(ShapeRetryClient):
    """Expose cache misses without transport calls or synthetic cache entries."""
    missing = []

    def ask(self, task, payload, schema):
        body = body_for(self.settings, self.config, task, payload, schema)
        original_key = digest([self.settings.chat_completions_url, body])
        key = original_key
        if not (self.cache/(key+'.json')).exists() and original_key in self.failed_schema:
            revised = deepcopy(payload)
            revised['instruction'] += '\n'+REMINDER
            revised['format_recovery_contract'] = dict(original_request_sha256=original_key,
                maximum_format_feedback_rounds=1,source_context_added=False,semantic_positive_required=False)
            body = body_for(self.settings,self.config,task,revised,schema)
            key = digest([self.settings.chat_completions_url,body])
        path = self.cache/(key+'.json')
        if not path.exists():
            self.missing.append(dict(task=task,original_request_sha256=original_key,actual_request_sha256=key))
            raise RuntimeError('CACHE_ONLY_MISS:'+key)
        saved = read(path)
        if digest([self.settings.chat_completions_url,saved['request']])!=key or saved['receipt']['request_sha256']!=key:
            raise ValueError('CACHE_REQUEST_HASH_MISMATCH')
        Draft202012Validator(schema).validate(saved['result'])
        if digest(saved['result']) != saved['result_sha256']:
            raise ValueError('CACHE_RESPONSE_HASH_MISMATCH')
        receipt = dict(saved['receipt'],cached=True)
        self._record_receipt(receipt)
        return saved['result'],receipt


def probe(destination):
    destination = Path(destination)
    out = destination/'cache_only_preflight'
    out.mkdir(exist_ok=False)
    args = SimpleNamespace(sources=destination/'exact_source_inputs',output=out,
        cache=destination/'model_cache',unit_workers=4,
        schema=ROOT/'private/p2_auto_checkpoint_v0.1_20261024/standards/fhir.schema.json')
    config = read(destination/'technical_completion_config.json')
    old_client = engine.Client
    CacheOnlyClient.missing = []
    engine.Client = CacheOnlyClient
    summaries = []
    try:
        for cid in CASES:
            summaries.append(run.run_case(cid,args,config))
    finally:
        engine.Client = old_client
    receipt = dict(remote_calls=0,summaries=summaries,
        missing_requests=CacheOnlyClient.missing,
        missing_by_task=dict(Counter(r['task'] for r in CacheOnlyClient.missing)),
        downstream_review_count_is_lower_bound=any(r['task']=='document_event_discovery' for r in CacheOnlyClient.missing),
        reference_read=False)
    write(destination/'cache_only_preflight_receipt.json',receipt)
    return {k:v for k,v in receipt.items() if k not in ('summaries','missing_requests')}


def verify(destination):
    destination=Path(destination)
    protected=read(destination/'protected_old_hashes.json')
    changed=[p for p,h in protected.items() if not Path(p).exists() or sha(p)!=h]
    receipt=dict(protected_old_files=len(protected),changed_old_files=changed,passed=not changed)
    write(destination/'old_artifact_protection_receipt.json',receipt)
    if changed:raise ValueError('OLD_ARTIFACT_CHANGED')
    return receipt


def feedback(destination):
    """Pre-register two mechanically blocked request groups and revise once.

    Selection uses only validation errors from the fixed model's completed
    development cases. It reads no annotation Reference or clinical score.
    """
    from collections import defaultdict
    from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources
    from scripts.cohort_construction.p2_auto_checkpoint.semantic import patient_names
    from scripts.cohort_construction.p2_auto_checkpoint_v04.review_feedback import (
        run_review_feedback, review_repair_triggers,
    )
    from scripts.cohort_construction.p2_auto_checkpoint_v05_refine.refine import (
        original_payload, request_endpoint, prepare_payload,
    )
    destination=Path(destination)
    out=destination/'feedback_comparison'
    out.mkdir(exist_ok=False)
    config=read(destination/'technical_completion_config.json')
    config['review_feedback_max_rounds']=1
    write(out/'feedback_config.json',config)
    client=ShapeRetryClient(destination/'model_cache',config)
    completed={}
    for summary_path in sorted(OLD.glob('attempts/*/SCALE-*/summary.json')):
        summary=read(summary_path)
        if summary.get('failures')==0:completed[summary['case_id']]=summary_path.parent
    selected=[];preflight_errors=[]
    for cid,case_dir in sorted(completed.items()):
        events=read(case_dir/'decision_events.json')
        sources,_=import_sources(read(case_dir/'source_fhir_r4_bundle.json'))
        names=patient_names(sources)
        groups=defaultdict(list)
        for event in events:
            if review_repair_triggers(event):groups[event['review_request_sha256']].append(event)
        for request_hash,targets in sorted(groups.items()):
            try:
                original=original_payload(destination/'model_cache',request_hash,config['semantic_model'],request_endpoint(client))
                proposed=prepare_payload(original,targets,names)
                if len(json.dumps(proposed,ensure_ascii=False))>config['max_review_payload_characters']:
                    raise ValueError('FEEDBACK_PAYLOAD_EXCEEDS_FROZEN_BUDGET')
            except Exception as exc:
                preflight_errors.append(dict(case_id=cid,request_sha256=request_hash,error=type(exc).__name__))
                continue
            selected.append(dict(case_id=cid,input_directory=str(case_dir),original_review_request_sha256=request_hash,
                target_event_ids=[e['event_id'] for e in targets],
                trigger_codes={e['event_id']:review_repair_triggers(e) for e in targets},
                original_payload_sha256=digest(original),repair_payload_sha256=digest(proposed),
                input_file_sha256={n:sha(case_dir/n) for n in (
                    'decision_events.json','source_fhir_r4_bundle.json','discovery.json')}))
            break  # one request group per case; deterministic case/hash order
        if len(selected)==2:break
    write(out/'selection_freeze.json',dict(selection='first_valid_group_per_case_sorted_by_case_then_request_hash',
        maximum_groups=2,groups=selected,preflight_errors=preflight_errors,
        clinical_reference_read=False,positive_result_required=False,
        same_model=config['semantic_model'],same_temperature=config['model_temperature'],
        source_context_added=False,maximum_semantic_feedback_rounds=1,
        experiment_code_sha256=sha(__file__),config_sha256=sha(out/'feedback_config.json'),
        code_sha256={str(p.relative_to(ROOT)):sha(p) for package in (
            'p2_auto_checkpoint_v04','p2_auto_checkpoint_v05_refine','p2_auto_checkpoint_model_rerun')
            for p in (ROOT/'scripts/cohort_construction'/package).glob('*.py')}))
    shutil.copyfile(__file__,out/'fixed_model_execution_snapshot.py')
    summaries=[]
    for selected_group in selected:
        cid=selected_group['case_id'];case_dir=Path(selected_group['input_directory'])
        group_out=out/cid;group_out.mkdir()
        for n in ('decision_events.json','source_fhir_r4_bundle.json','discovery.json'):
            shutil.copyfile(case_dir/n,group_out/('original_'+n))
        sources,_=import_sources(read(group_out/'original_source_fhir_r4_bundle.json'))
        discovery_events=read(group_out/'original_discovery.json')['events']
        events=read(group_out/'original_decision_events.json')
        ids=set(selected_group['target_event_ids']);targets=[e for e in events if e['event_id'] in ids]
        original=original_payload(destination/'model_cache',selected_group['original_review_request_sha256'],
            config['semantic_model'],request_endpoint(client))
        revised,audit=run_review_feedback(targets,original,discovery_events,sources,patient_names(sources),client,config)
        write(group_out/'feedback_audit.json',audit)
        write(group_out/'revised_target_events.json',revised)
        before={e['event_id']:e for e in targets}
        comparison=[]
        for event in revised:
            prior=before[event['event_id']]
            invalid=lambda e:sum(len(e.get(field,{}).get('invalid',[])) for field in (
                'validated_observation_citations','validated_connection_citations'))+sum(
                len(value.get('invalid',[])) for value in e.get('comparison_validation',{}).values() if isinstance(value,dict))
            comparison.append(dict(event_id=event['event_id'],
                before_status=prior['checkpoint_status'],after_status=event['checkpoint_status'],
                before_validation_reasons=prior.get('validation_reasons',[]),
                after_validation_reasons=event.get('validation_reasons',[]),
                removed_validation_reasons=sorted(set(prior.get('validation_reasons',[]))-set(event.get('validation_reasons',[]))),
                added_validation_reasons=sorted(set(event.get('validation_reasons',[]))-set(prior.get('validation_reasons',[]))),
                before_invalid_citations=invalid(prior),after_invalid_citations=invalid(event),
                semantic_review_changed=prior['semantic_review']!=event['semantic_review'],
                evidence_pool_unchanged=prior.get('actual_model_evidence')==event.get('actual_model_evidence'),
                refinement=event.get('review_refinement')))
        write(group_out/'event_comparison.json',comparison)
        summaries.append(dict(case_id=cid,target_events=len(targets),status=audit['status'],
            request_attempted=audit['attempted'],cached=audit.get('receipt',{}).get('cached'),
            before_statuses=dict(Counter(e['checkpoint_status'] for e in targets)),
            after_statuses=dict(Counter(e['checkpoint_status'] for e in revised)),
            before_invalid_citations=sum(r['before_invalid_citations'] for r in comparison),
            after_invalid_citations=sum(r['after_invalid_citations'] for r in comparison),
            removed_reason_counts=dict(Counter(reason for r in comparison for reason in r['removed_validation_reasons'])),
            added_reason_counts=dict(Counter(reason for r in comparison for reason in r['added_validation_reasons'])),
            changed_semantic_proposals=sum(r['semantic_review_changed'] for r in comparison),
            all_evidence_pools_unchanged=all(r['evidence_pool_unchanged'] for r in comparison)))
        write(out/'progress.json',summaries)
    write(out/'model_receipts.json',client.receipts)
    receipt=dict(groups=summaries,model_receipt_count=len(client.receipts),
        cached_requests=sum(r.get('cached',False) for r in client.receipts),
        transport_attempts=sum(len(r.get('attempts',[])) for r in client.receipts if not r.get('cached')),
        clinical_accuracy_measured=False,improvement_proven=False,
        semantic_adjudication='requires independent review; mechanical error resolution alone does not establish correctness')
    write(out/'summary.json',receipt)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','probe','verify','feedback'])
    parser.add_argument('--destination',type=Path,default=DEFAULT)
    args=parser.parse_args()
    result=globals()[args.action](args.destination)
    if args.action=='prepare':result={k:v for k,v in result.items() if k not in ('code_sha256','source_files')}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
