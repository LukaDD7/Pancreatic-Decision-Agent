"""Offline audit probes; assertions reproduce defects, not repaired behavior.

Real artifacts are read only. Mutations are confined to newly created fictional
temporary batches. No provider request or clinical acceptance is performed.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator

from receipts.p2_fix_review2_20261102_probes import raw_response, revise
from scripts.cohort_construction.model_api import ModelAPISettings
from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport
from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import prepare_candidate_materials
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import fictional_graph
from scripts.cohort_construction.p2_checkpoint_material_review_app import build
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.replay import validate_pipeline_run
from scripts.cohort_construction.p2_checkpoint_real_validation.batch import (
    _reconstruction_ledger, _reconstruction_parent, execute_batch, replay_batch,
)
from scripts.cohort_construction.p2_checkpoint_real_validation.tests.test_real_validation import CONFIG, _write_synthetic_batch_case
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
    GRAPH_RECONSTRUCTION_SCHEMA, build_graph_reconstruction_request,
    validate_graph_reconstruction, validate_graph_reconstruction_chain,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_graph_reconstruction import fixtures, proposed


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def source_contract_probe():
    _, sources = fictional_graph()
    _, audit_request, audit_response = fixtures()
    request = build_graph_reconstruction_request(audit_request, audit_response)
    results = {}
    for scenario in ('complete_event_control', 'missing_source_ids', 'schema_minimum_events'):
        response = proposed(request)
        for event in response['graph']['events']:
            if scenario == 'missing_source_ids':
                event.pop('source_ids', None)
            elif scenario == 'schema_minimum_events':
                keep = ('event_id', 'source_id', 'checkpoint_status', 'action_citations')
                for key in list(event):
                    if key not in keep:
                        del event[key]
        Draft202012Validator(GRAPH_RECONSTRUCTION_SCHEMA).validate(response)
        graph, receipt = validate_graph_reconstruction(request, response)
        tasks, failures = prepare_candidate_materials(graph, sources,
            decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
        results[scenario] = dict(schema_accepted=True, reconstruction_state=receipt['state'],
            prepared_tasks=len(tasks), assembly_failure_codes=[f['reason_code'] for f in failures])
        if scenario == 'complete_event_control':
            assert len(tasks) == 2 and not failures
        else:
            assert not tasks and len(failures) == 2
            assert {f['reason_code'] for f in failures} == {'EVENT_SOURCE_IDS_REQUIRED'}
    return results


def progress_inventory_probe(root):
    """Exercise production Client and batch replay using fictional mock HTTP."""
    graph, sources = fictional_graph()
    preflight, authorization = _write_synthetic_batch_case(root, graph, sources, sources)
    session = Mock()

    def post(*args, **kwargs):
        payload = json.loads(kwargs['json']['messages'][1]['content'])
        if payload['task'] == 'checkpoint_candidate_reconstruction_independent_audit':
            return raw_response(revise(payload['input']))
        response = proposed(payload['input'])
        response['graph'] = {'、': ['、']}
        return raw_response(response)

    session.post.side_effect = post
    settings = ModelAPISettings('synthetic', 'https://example.invalid/v1', 'synthetic')
    with patch.object(build, 'PRIVATE_ROOT', root), \
            patch.object(transport._thread, 'session', session, create=True), \
            patch.object(transport.time, 'sleep', return_value=None):
        baseline = execute_batch(preflight_output=preflight, authorization=authorization,
            base_config=dict(CONFIG, model_temperature=0, max_output_tokens=16000),
            model_client_factory=lambda cache, config: transport.Client(cache, config, settings),
            output=root / 'execution', private_root=root)
        assert baseline['replay_verified'] and baseline['fixed_case_count'] == 1
        output = root / 'execution'
        progress_path = output / 'batch-progress.json'
        original = progress_path.read_bytes()
        progress = json.loads(original)
        ledger = read(output / 'model-call-ledger.json')
        assert any(row.get('stage') == 'reconstruction' for row in ledger['records'])
        results = {}
        receipt_path = output / 'batch-replay-receipt.json'
        changed = deepcopy(progress)
        changed['cases'] = []
        progress_path.write_text(json.dumps(changed), encoding='utf-8')
        try:
            replay_batch(output)
        except ValueError as exc:
            assert str(exc) == 'REAL_BATCH_RECEIPT_CHANGED_REQUIRES_NEW_OUTPUT'
            prior_receipt_guard = str(exc)
        else:
            raise AssertionError('Existing immutable receipt must reject changed aggregation')
        finally:
            progress_path.write_bytes(original)
        preserved_receipt = root / 'baseline-replay-receipt.preserved.json'
        receipt_path.rename(preserved_receipt)
        try:
            for scenario, rows in (
                    ('empty_progress', []),
                    ('omitted_called_case_with_other_row', [dict(case_id='SCALE-02',
                        state='NOT_ATTEMPTED_OUTSIDE_AUTHORIZED_SUBSET',
                        reason_code='EXPLICIT_MODEL_SWITCH_SUBSET_ONLY')])):
                changed = deepcopy(progress)
                changed['cases'] = rows
                progress_path.write_text(json.dumps(changed), encoding='utf-8')
                replay = replay_batch(output)
                # This acceptance is the defect. It is not a success criterion.
                assert replay['replay_verified']
                results[scenario] = dict(incorrectly_accepted=True,
                    replay_verified=replay['replay_verified'],
                    reported_case_count=replay['fixed_case_count'],
                    retained_request_count=replay['model_usage']['requests'])
                receipt_path.rename(root / (scenario + '-replay.json'))
        finally:
            progress_path.write_bytes(original)
            if not receipt_path.exists():
                preserved_receipt.rename(receipt_path)
        restored = replay_batch(output)
        assert restored['replay_verified'] and restored['fixed_case_count'] == 1
    return dict(baseline_replay_verified=True, restored_replay_verified=True,
        existing_immutable_receipt_guard=prior_receipt_guard,
        defect_precondition='NO_PRIOR_BATCH_REPLAY_RECEIPT_AVAILABLE',
        mocked_http_attempts=session.post.call_count, defects=results)


def real_artifact_audit():
    base = Path('private/p2_checkpoint_next_20261028')
    names = [
        'ready_pipeline_real_validation_all10_auto_closure_postaudit_r1_20261102',
        'ready_pipeline_real_validation_retry_provider5_auto_closure_r1_20261103',
        'ready_pipeline_real_validation_retry_provider5_auto_closure_r2_20261103',
    ]
    results = []
    for name in names:
        output = base / name / 'execution'
        files = [p for p in output.rglob('*') if p.is_file()]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        progress = read(output / 'batch-progress.json')
        ledger = read(output / 'model-call-ledger.json')
        runs, reconstructions = [], []
        for path in sorted(output.rglob('run.json')):
            try:
                run = read(path)
                replay = validate_pipeline_run(run, output=path.parent, verify_implementation=True)
            except (ValueError, KeyError) as exc:
                runs.append(dict(path=str(path.relative_to(output)), replay_verified=False,
                    error_type=type(exc).__name__,
                    reason_code='UNREADABLE_INTERRUPTED_JSON' if isinstance(exc, json.JSONDecodeError) else str(exc)))
                continue
            assert replay['replay_verified']
            runs.append(dict(path=str(path.relative_to(output)), state=run['state'],
                graph_audit_state=run.get('graph_audit_chain', {}).get('state'),
                event_count=len(run['input_contract']['graph']['events']),
                events_missing_source_ids=sum('source_ids' not in event
                    for event in run['input_contract']['graph']['events']),
                assembly_failure_counts=dict(Counter(f['reason_code'] for f in run.get('assembly_failures', []))),
                replay_verified=True))
        for path in sorted((output / 'automatic-revisions').glob('*/reconstruction-*.json')):
            chain = read(path)
            case_id = path.parent.name
            generation = int(path.stem.split('-')[-1])
            row = next(c for c in progress['cases'] if c['case_id'] == case_id)
            initial_directory = output / 'episodes' / case_id / row['episode_id']
            parent, _ = _reconstruction_parent(output, case_id, initial_directory, generation)
            audit = parent['graph_audit_chain']
            replay = validate_graph_reconstruction_chain(chain,
                expected_request=(audit['initial_request'], audit['rounds'][-1]['response']),
                expected_provider=_reconstruction_ledger(ledger, case_id, audit['graph_sha256']))
            reconstructions.append(dict(path=str(path.relative_to(output)),
                state=replay['state'], replay_verified=replay['replay_verified']))
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        assert before == after
        case_ids = [c['case_id'] for c in progress['cases']]
        called_ids = sorted({r['context']['case_id'] for r in ledger['records']
            if r.get('provider_request_performed')})
        receipt = output / 'batch-execution-receipt.json'
        saved = read(receipt) if receipt.exists() else None
        results.append(dict(batch=name, progress_case_ids=case_ids,
            ledger_called_case_ids=called_ids,
            called_case_ids_missing_from_progress=sorted(set(called_ids) - set(case_ids)),
            saved_batch_receipt_present=saved is not None,
            saved_state_counts=saved['state_counts'] if saved else None,
            model_usage={k: v for k, v in ledger.items() if k != 'records'},
            runs=runs, reconstructions=reconstructions,
            input_artifact_hashes_unchanged=True, verified_artifact_count=len(files)))
    return results


def main():
    root = Path(mkdtemp(prefix='p2-latest-real-audit-', dir='/private/tmp'))
    previous_umask = os.umask(0o077)
    try:
        with patch('requests.sessions.Session.send', side_effect=AssertionError('NO_REAL_HTTP')):
            result = dict(actual_provider_requests=0, clinical_reference_created=False,
                implementation_modified=False, synthetic_output_root=str(root),
                CE_GRA_011=source_contract_probe(),
                CE_REAL_013=progress_inventory_probe(root / 'synthetic'),
                real_batches=real_artifact_audit())
        (root / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        os.umask(previous_umask)


if __name__ == '__main__':
    main()
