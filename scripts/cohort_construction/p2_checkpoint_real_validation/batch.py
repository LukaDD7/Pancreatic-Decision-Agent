"""Offline preflight, bounded execution, and no-model replay of the fixed ten."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from scripts.cohort_construction.p2_checkpoint_material_review_app.build import private_mkdir
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.orchestration import run_episode_pipeline
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import (
    implementation_snapshot, new_output, read, write,
)
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.replay import validate_pipeline_run
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
    validate_graph_reconstruction_chain,
)

from .budget import BatchModelBudget, _journal_write
from .inventory import FORMAT as INVENTORY_FORMAT, freeze_inventory, validate_inventory


def _persist_receipt(output, name, value):
    """Return a receipt without overwriting an earlier replay result."""
    path = output / name
    if path.exists():
        if read(path) != value:
            raise ValueError('REAL_BATCH_RECEIPT_CHANGED_REQUIRES_NEW_OUTPUT')
        return
    write(path, value)
from .inputs import DECISION_TASK_PREFIX, VISIBILITY_POLICY, episode_inventory, load_episode_case


BATCH_FORMAT = 'checkpoint-real-development-batch-0.1.0'
HTTP_ATTEMPTS_PER_REQUEST = 10
RETRY_BACKOFF_BASE_SECONDS = 30
RETRY_BACKOFF_MAX_SECONDS = 120
MIN_REQUEST_INTERVAL_SECONDS = 10
MATERIAL_REVIEW_ROUNDS = 3
GRAPH_RECONSTRUCTION_GENERATIONS = 1
EXECUTION_BUDGET_KEYS = ('max_rounds', 'automatic_graph_reconstruction_authorized',
    'max_graph_revisions', 'graph_revision_human_final_review_required')
RATE_CONTROL_KEYS = ('min_request_interval_seconds', 'retry_backoff_base_seconds',
    'retry_backoff_max_seconds', 'rate_control_is_explicit',
    'http_400_rate_cause_unproven')
COMPACT_TRANSPORT_KEYS = ('graph_audit_compact_transport',
    'lossless_request_projection_is_explicit')
_GRAPH_ELIGIBLE = frozenset({'READY_FOR_AUTHORIZED_DEVELOPMENT_EXECUTION',
    'READY_GRAPH_AUDIT_WITH_MATERIAL_PREPARATION_HOLD'})


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _summary_state(run):
    chain = run.get('graph_audit_chain')
    if chain is not None and chain['state'] != 'AUTO_GRAPH_AUDIT_PASS':
        return chain['state'], chain.get('stop_reason')
    if run.get('delivery_gate') is not None and run.get('episode_material_set') is not None \
            and all(item['state'] == 'AUTO_REVIEW_PASS'
                    for item in run['episode_material_set']['checkpoint_materials']):
        return 'DELIVERY_GATE_HOLD', ';'.join(run['delivery_gate']['reason_codes'])
    return run['state'], next((stage.get('reason_code') for stage in reversed(run['stages'])
        if stage.get('reason_code')), None)


def preflight_batch(*, manifest, derived_release, output, batch_id='REAL-V063-FIXED-TEN-20261030',
                    private_root=None):
    """Fully inspect all ten before any provider Client is constructed."""
    output = new_output(output, private_root=private_root)
    manifest = Path(manifest).resolve()
    derived_release = Path(derived_release).resolve()
    contract = dict(format=BATCH_FORMAT, batch_id=batch_id,
        manifest_path=str(manifest), manifest_sha256=file_sha256(manifest),
        derived_release=str(derived_release), selection='one_lexicographically_first_episode_per_case',
        selection_uses=['episode_id only'], replacements_for_missing_or_failed_cases=False,
        declared_source_scope='complete_originals_of_selected_episode_processing_units',
        whole_patient_chart_certified=False, graph_version='fixed-ten-v0.6.0-episode-audit-v1',
        remote_execution_authorized_by_preflight=False,
        semantic_audit_status='PENDING_INDEPENDENT_SEMANTIC_AUDIT',
        questionnaire_status='QUESTIONNAIRE_NOT_FROZEN',
        doctor_assignment_created=False, clinical_release_permitted=False)
    write(output / 'batch-contract.json', contract)
    manifest_value = read(manifest)
    if manifest_value.get('seed') != 20261023 or len(manifest_value.get('selected', [])) != 10:
        raise ValueError('REAL_BATCH_FIXED_TEN_MANIFEST_REQUIRED')
    ids = [item['case_id'] for item in manifest_value['selected']]
    if len(set(ids)) != 10 or sorted(ids) != ['SCALE-%02d' % number for number in range(1, 11)]:
        raise ValueError('REAL_BATCH_FIXED_TEN_IDS_CHANGED')
    records = []
    for item in sorted(manifest_value['selected'], key=lambda value: value['case_id']):
        case_id = item['case_id']
        directory = derived_release / case_id
        case_output = output / 'preflight' / case_id
        private_mkdir(case_output, parents=True)
        record = dict(case_id=case_id, source_directory=str(directory),
            source_bundle_sha256=file_sha256(directory / 'source_fhir_r4_bundle.json'))
        try:
            inventory = episode_inventory(directory)
            write(case_output / 'episode-inventory.json', inventory)
            loaded = load_episode_case(directory)
            if loaded['graph'] is None:
                record.update(status='PREFLIGHT_HOLD', reason_codes=['EMPTY_CANDIDATE_EPISODES'],
                    candidate_episode_count=0, candidate_checkpoint_count=0)
            else:
                graph = loaded['graph']
                write(case_output / 'graph.json', graph)
                write(case_output / 'sources.json', loaded['sources'])
                write(case_output / 'current-unit-sources.json', loaded['current_sources'])
                write(case_output / 'source-dependency-reconciliation.json',
                    loaded['source_dependency_receipt'])
                write(case_output / 'graph-audit-request.json', loaded['graph_request'])
                write(case_output / 'assembly-failures.json', loaded['failures'])
                write(case_output / 'execution-request.json', dict(
                    episode_id=loaded['episode']['episode_id'],
                    decision_task_id=DECISION_TASK_PREFIX + case_id,
                    visibility_policy_version=VISIBILITY_POLICY,
                    graph_audit_context=loaded['graph_audit_context'],
                    graph_sha256=graph['graph_sha256'],
                    sources_sha256=canonical_sha256(loaded['sources'])))
                record.update(status='READY_FOR_AUTHORIZED_DEVELOPMENT_EXECUTION'
                    if not loaded['failures'] else 'READY_GRAPH_AUDIT_WITH_MATERIAL_PREPARATION_HOLD',
                    reason_codes=[] if not loaded['failures'] else ['ASSEMBLY_HOLD'],
                    graph_sha256=graph['graph_sha256'],
                    selected_episode_id=loaded['episode']['episode_id'],
                    selected_checkpoint_ids=loaded['episode']['checkpoint_ids'],
                    candidate_episode_count=len(inventory),
                    candidate_checkpoint_count=sum(len(row['checkpoint_ids']) for row in inventory),
                    source_count=len(loaded['sources']),
                    current_unit_source_count=len(loaded['current_sources']),
                    recovered_historical_dependency_count=len(
                        loaded['source_dependency_receipt']['recovered_frozen_package_source_ids']),
                    missing_historical_dependency_count=len(
                        loaded['source_dependency_receipt']['not_found_in_delivery_source_ids']),
                    source_text_characters=sum(len(source['text']) for source in loaded['sources']),
                    limitations=loaded['limitations'])
        except Exception as exc:
            record.update(status='PREFLIGHT_HOLD', reason_codes=[type(exc).__name__],
                detail=str(exc)[:500], remote_model_call_performed=False)
        write(case_output / 'case-preflight.json', record)
        records.append(record)
    receipt = dict(format=BATCH_FORMAT + '-preflight', batch_id=batch_id,
        fixed_case_count=len(records),
        graph_eligible_case_count=sum(row['status'] in _GRAPH_ELIGIBLE for row in records),
        ready_case_count=sum(row['status'] == 'READY_FOR_AUTHORIZED_DEVELOPMENT_EXECUTION' for row in records),
        material_preparation_hold_case_count=sum(row['status'] == 'READY_GRAPH_AUDIT_WITH_MATERIAL_PREPARATION_HOLD' for row in records),
        hold_case_count=sum(row['status'] == 'PREFLIGHT_HOLD' for row in records),
        cases=records, model_call_performed=False, clinical_accuracy_measured=False,
        clinical_reference_created=False, doctor_assignment_created=False,
        clinical_release_permitted=False)
    write(output / 'batch-preflight.json', receipt)
    return receipt


def _authorized_config(base_config, contract):
    config = deepcopy(base_config)
    expected = dict(batch_id=contract['batch_id'], manifest_sha256=contract['manifest_sha256'],
        max_requests=60, max_http_attempts=60 * HTTP_ATTEMPTS_PER_REQUEST,
        remote_execution_authorized=True, doctor_packet_release=False,
        real_doctor_assignment_creation=False, clinical_release_permitted=False)
    for key, value in expected.items():
        if contract.get(key) != value:
            raise ValueError('REAL_BATCH_AUTHORIZATION_MISMATCH:' + key)
    allowed_models = {'deepseek-v4.1-flash'}
    if contract.get('execution_subset') is not None:
        allowed_models.add('kimi-k3')
    if contract.get('semantic_model') not in allowed_models:
        raise ValueError('REAL_BATCH_AUTHORIZATION_MISMATCH:semantic_model')
    config.update(remote_execution_authorized=True, semantic_model=contract['semantic_model'],
        max_http_attempts_per_request=HTTP_ATTEMPTS_PER_REQUEST,
        execution_scope='fixed_ten_real_development_batch:' + contract['batch_id'],
        doctor_packet_release=False, real_doctor_assignment_creation=False,
        clinical_release_permitted=False)
    if any(key in contract for key in RATE_CONTROL_KEYS):
        expected_rate = dict(min_request_interval_seconds=MIN_REQUEST_INTERVAL_SECONDS,
            retry_backoff_base_seconds=RETRY_BACKOFF_BASE_SECONDS,
            retry_backoff_max_seconds=RETRY_BACKOFF_MAX_SECONDS,
            rate_control_is_explicit=True, http_400_rate_cause_unproven=True)
        for key, value in expected_rate.items():
            if contract.get(key) != value:
                raise ValueError('REAL_BATCH_AUTHORIZATION_MISMATCH:' + key)
        config.update({key: contract[key] for key in RATE_CONTROL_KEYS[:3]})
    if any(key in contract for key in COMPACT_TRANSPORT_KEYS):
        for key in COMPACT_TRANSPORT_KEYS:
            if contract.get(key) is not True:
                raise ValueError('REAL_BATCH_AUTHORIZATION_MISMATCH:' + key)
        config['graph_audit_compact_transport'] = True
    if any(key in contract for key in EXECUTION_BUDGET_KEYS):
        expected_execution = dict(max_rounds=MATERIAL_REVIEW_ROUNDS,
            automatic_graph_reconstruction_authorized=True,
            max_graph_revisions=GRAPH_RECONSTRUCTION_GENERATIONS,
            graph_revision_human_final_review_required=True)
        for key, value in expected_execution.items():
            if contract.get(key) != value:
                raise ValueError('REAL_BATCH_AUTHORIZATION_MISMATCH:' + key)
        config.update(material_review_max_rounds=MATERIAL_REVIEW_ROUNDS,
            graph_reconstruction_max_generations=GRAPH_RECONSTRUCTION_GENERATIONS,
            automatic_graph_reconstruction=True,
            automatic_pass_is_clinical_reference=False)
    return config


def execute_batch(*, preflight_output, authorization, base_config, model_client_factory,
                  output, initial_budget_state=None, private_root=None):
    """Run only the separately authorized batch; never called by preflight."""
    preflight_output = Path(preflight_output)
    output = new_output(output, private_root=private_root)
    contract = read(authorization)
    batch_contract = read(preflight_output / 'batch-contract.json')
    for key in ('batch_id', 'manifest_sha256'):
        if contract.get(key) != batch_contract.get(key):
            raise ValueError('REAL_BATCH_AUTHORIZATION_BATCH_MISMATCH')
    config = _authorized_config(base_config, contract)
    preflight = read(preflight_output / 'batch-preflight.json')
    effective_contract = dict(contract, execution_inventory_version=INVENTORY_FORMAT)
    inventory = freeze_inventory(preflight, batch_contract, effective_contract, initial_budget_state)
    write(output / 'effective-batch-authorization.json', effective_contract)
    write(output / 'batch-execution-inventory.json', inventory)
    write(output / 'implementation-snapshot.json', implementation_snapshot())
    ledger = BatchModelBudget(output, max_requests=contract['max_requests'],
        max_http_attempts=contract['max_http_attempts'],
        max_http_attempts_per_request=HTTP_ATTEMPTS_PER_REQUEST, state=initial_budget_state)
    ledger.persist()
    client = model_client_factory(output / 'model-cache', config)
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.prompts import graph_audit_client
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import graph_reconstruction_client
    from scripts.cohort_construction.p2_checkpoint_material_governance.prompts import material_review_client
    from scripts.cohort_construction.p2_checkpoint_delivery_audit.prompts import delivery_audit_client
    prompts = dict(graph=graph_audit_client(client),
        reconstruction=graph_reconstruction_client(client),
        material=material_review_client(client), delivery=delivery_audit_client(client))
    preflight = read(preflight_output / 'batch-preflight.json')
    subset = contract.get('execution_subset')
    if subset is not None:
        eligible_ids = [row['case_id'] for row in preflight['cases'] if row['status'] in _GRAPH_ELIGIBLE]
        if (type(subset) is not list or not subset or len(subset) != len(set(subset))
                or any(type(case_id) is not str or case_id not in eligible_ids for case_id in subset)
                or contract.get('semantic_model') != 'kimi-k3'
                or contract.get('model_switch_is_explicit') is not True
                or contract.get('no_case_replacement') is not True):
            raise ValueError('REAL_BATCH_SUBSET_AUTHORIZATION_INVALID')
        selected_ids = set(subset)
    records = []
    _journal_write(output / 'batch-progress.json', dict(batch_id=contract['batch_id'],
        cases=[], model_usage=ledger.snapshot()))

    def save_progress():
        progress = dict(batch_id=contract['batch_id'], cases=records, model_usage=ledger.snapshot())
        _journal_write(output / 'batch-progress.json', progress)
        events = output / 'batch-progress-events'
        private_mkdir(events, exist_ok=True)
        write(events / ('%06d.json' % len(records)), progress)

    for case in preflight['cases']:
        case_id = case['case_id']
        if subset is not None and case_id not in selected_ids:
            records.append(dict(case_id=case_id, state='NOT_ATTEMPTED_OUTSIDE_AUTHORIZED_SUBSET',
                reason_code='EXPLICIT_MODEL_SWITCH_SUBSET_ONLY'))
            save_progress()
            continue
        if case['status'] not in _GRAPH_ELIGIBLE:
            records.append(dict(case_id=case_id, state='PREFLIGHT_HOLD', reason_codes=case['reason_codes']))
            save_progress()
            continue
        if ledger.requests >= ledger.max_requests or ledger.http_attempts >= ledger.max_http_attempts:
            records.append(dict(case_id=case_id, state='NOT_ATTEMPTED_BATCH_BUDGET_EXHAUSTED'))
            save_progress()
            continue
        request = read(preflight_output / 'preflight' / case_id / 'execution-request.json')
        graph = read(preflight_output / 'preflight' / case_id / 'graph.json')
        sources = read(preflight_output / 'preflight' / case_id / 'sources.json')
        current_sources = read(preflight_output / 'preflight' / case_id / 'current-unit-sources.json')
        context = dict(batch_id=contract['batch_id'], case_id=case_id,
            episode_id=request['episode_id'], graph_sha256=graph['graph_sha256'])
        adapters = {stage: ledger.adapter(stage, prompts[stage], context) for stage in prompts}
        pipeline_adapters = {stage: adapters[stage] for stage in ('graph', 'material', 'delivery')}
        try:
            run = run_episode_pipeline(output=output / 'episodes' / case_id / request['episode_id'],
                graph=graph, sources=sources, graph_audit_sources=current_sources,
                episode_id=request['episode_id'],
                semantic_audits={}, questionnaire=None,
                graph_audit_context=request['graph_audit_context'],
                decision_task_id=request['decision_task_id'],
                visibility_policy_version=request['visibility_policy_version'],
                config=config, adapters=pipeline_adapters, execute=True,
                max_rounds=config.get('material_review_max_rounds', 1),
                max_model_calls=min(20, ledger.max_requests),
                delivery_gate=dict(reason_codes=['PENDING_INDEPENDENT_SEMANTIC_AUDIT',
                    'QUESTIONNAIRE_NOT_FROZEN']), private_root=private_root)
            context = dict(batch_id=contract['batch_id'], case_id=case_id,
                episode_id=request['episode_id'], graph_sha256=graph['graph_sha256'])
            if config.get('automatic_graph_reconstruction'):
                from scripts.cohort_construction.p2_checkpoint_ready_pipeline.automatic_revision import (
                    run_bounded_graph_revision_pipeline,
                )
                revision = run_bounded_graph_revision_pipeline(parent_run=run,
                    output=output / 'automatic-revisions' / case_id,
                    graph=graph, sources=sources, graph_audit_sources=current_sources,
                    episode_id=request['episode_id'],
                    semantic_audits={}, questionnaire=None,
                    graph_audit_context=request['graph_audit_context'],
                    decision_task_id=request['decision_task_id'],
                    visibility_policy_version=request['visibility_policy_version'],
                    config=config, adapters=adapters, execute=True,
                    max_rounds=config['material_review_max_rounds'],
                    max_graph_revisions=config['graph_reconstruction_max_generations'],
                    max_model_calls=min(20, ledger.max_requests),
                    delivery_gate=dict(reason_codes=['PENDING_INDEPENDENT_SEMANTIC_AUDIT',
                        'QUESTIONNAIRE_NOT_FROZEN']), private_root=private_root)
                run = revision['final_run']
                write(output / 'automatic-revisions' / case_id / 'automatic-revision-summary.json', dict(
                    state=revision['state'], stop_reason=revision['stop_reason'],
                    generations=[item['generation'] for item in revision['generations']],
                    clinical_release_permitted=False))
            state, reason = _summary_state(run)
            records.append(dict(case_id=case_id, episode_id=request['episode_id'], state=state,
                reason_code=reason, run_sha256=run['run_sha256'],
                model_usage=run['model_usage'],
                replay_verified=validate_pipeline_run(run)['replay_verified']))
        except Exception as exc:
            # Unexpected orchestration errors are preserved, not relabeled PASS.
            records.append(dict(case_id=case_id, episode_id=request['episode_id'],
                state='ORCHESTRATION_HOLD', reason_code=type(exc).__name__, detail=str(exc)[:500]))
        ledger.persist()
        save_progress()
    result = replay_batch(output)
    _persist_receipt(output, 'batch-execution-receipt.json', result)
    return result


def _reconstruction_parent(output, case_id, initial_directory, generation):
    if generation == 1:
        directory = initial_directory
    else:
        directory = output / 'automatic-revisions' / case_id / ('generation-%04d' % (generation - 1))
    return read(directory / 'run.json'), directory


def _reconstruction_ledger(ledger, case_id, graph_sha256):
    matches = [record for record in ledger['records']
        if record.get('context', {}).get('case_id') == case_id
        and record.get('stage') == 'reconstruction'
        and record.get('provider_request_performed') is True
        and record.get('precondition', {}).get('graph_sha256') == graph_sha256]
    if len(matches) != 2:
        raise ValueError('REAL_BATCH_RECONSTRUCTION_LEDGER_BINDING_REQUIRED')
    before, after = matches
    if (before.get('outcome') != 'PROVIDER_IO_STARTED_OR_UNSETTLED'
            or after.get('outcome', '').startswith('PROVIDER_IO_STARTED')):
        raise ValueError('REAL_BATCH_RECONSTRUCTION_LEDGER_SETTLEMENT_INVALID')
    if any(before.get(key) != after.get(key) for key in
            ('task', 'request_sha256', 'expected_gain', 'context')):
        raise ValueError('REAL_BATCH_RECONSTRUCTION_LEDGER_REQUEST_CHANGED')
    return dict(payload_sha256=after['request_sha256'],
        transport_request_sha256=after.get('receipt', {}).get('request_sha256'),
        invocation_id=after.get('receipt', {}).get('invocation_id'),
        response_sha256=after.get('receipt', {}).get('response_sha256'))


def replay_batch(output, *, verify_implementation=True, persist=True):
    """Recompute completed chains; replay never constructs a provider Client."""
    output = Path(output)
    progress = read(output / 'batch-progress.json')
    ledger = read(output / 'model-call-ledger.json')
    inventory_receipt = validate_inventory(output, progress, ledger)
    replayed = []
    for case in progress['cases']:
        item = deepcopy(case)
        directory = output / 'episodes' / case['case_id'] / case['episode_id'] \
            if case.get('episode_id') else None
        revision_path = output / 'automatic-revisions' / case['case_id'] / 'automatic-revision-summary.json'
        reconstruction_calls = [record for record in ledger['records']
            if record.get('context', {}).get('case_id') == case['case_id']
            and record.get('stage') == 'reconstruction'
            and record.get('provider_request_performed') is True]
        # The ledger is the external evidence that reconstruction was attempted.
        # Never infer "no reconstruction" from a missing summary or directory.
        if reconstruction_calls and not revision_path.is_file():
            raise ValueError('REAL_BATCH_RECONSTRUCTION_LEDGER_REVISION_DELIVERY_MISSING')
        if revision_path.is_file():
            revision = read(revision_path)
            generations = revision.get('generations', [])
            initial_directory = directory
            if generations:
                directory = output / 'automatic-revisions' / case['case_id'] / (
                    'generation-%04d' % generations[-1])
            reconstruction_replays = []
            chain_paths = sorted(revision_path.parent.glob('reconstruction-*.json'))
            expected_chain_count = len(reconstruction_calls) // 2
            if len(reconstruction_calls) % 2 or len(chain_paths) != expected_chain_count:
                raise ValueError('REAL_BATCH_RECONSTRUCTION_LEDGER_CHAIN_INVENTORY_MISMATCH')
            missing_generation_chains = [generation for generation in generations
                if not (revision_path.parent / (
                    'reconstruction-%04d.json' % generation)).is_file()]
            if missing_generation_chains:
                raise ValueError('REAL_BATCH_RECONSTRUCTION_REQUIRED_CHAIN_MISSING')
            if [path.name for path in chain_paths] != [
                    'reconstruction-%04d.json' % number
                    for number in range(1, len(chain_paths) + 1)]:
                raise ValueError('REAL_BATCH_RECONSTRUCTION_GENERATION_SEQUENCE_INVALID')
            for chain_path in chain_paths:
                generation = int(chain_path.stem.split('-')[-1])
                parent, parent_directory = _reconstruction_parent(
                    output, case['case_id'], initial_directory, generation)
                parent_replay = validate_pipeline_run(parent, output=parent_directory,
                    verify_implementation=verify_implementation)
                if not parent_replay['replay_verified'] \
                        or parent.get('graph_audit_chain', {}).get('stop_reason') != 'RECONSTRUCTION_REQUIRED':
                    raise ValueError('REAL_BATCH_RECONSTRUCTION_PARENT_REVISE_REQUIRED')
                parent_audit = parent['graph_audit_chain']
                expected_provider = _reconstruction_ledger(ledger, case['case_id'],
                    parent_audit['graph_sha256'])
                chain_replay = validate_graph_reconstruction_chain(read(chain_path),
                    expected_request=(parent_audit['initial_request'],
                        parent_audit['rounds'][-1]['response']),
                    expected_provider=expected_provider)
                if chain_replay['state'] == 'RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT':
                    if not generations:
                        raise ValueError('REAL_BATCH_RECONSTRUCTION_SUCCESS_WITHOUT_GENERATION')
                    generated = read(revision_path.parent / (
                        'generation-%04d' % int(chain_path.stem.split('-')[-1])) /
                        'graph-reconstruction-chain.json')
                    if generated['chain_sha256'] != chain_replay['chain_sha256']:
                        raise ValueError('REAL_BATCH_RECONSTRUCTION_GENERATION_BINDING_MISMATCH')
                elif chain_replay['state'] != 'RECONSTRUCTION_HOLD':
                    raise ValueError('REAL_BATCH_RECONSTRUCTION_REPLAY_STATE_INVALID')
                reconstruction_replays.append(dict(path=chain_path.name,
                    chain_sha256=chain_replay['chain_sha256'], state=chain_replay['state'],
                    replay_verified=chain_replay['replay_verified']))
            item['automatic_revision_state'] = revision['state']
            item['automatic_revision_stop_reason'] = revision['stop_reason']
            item['reconstruction_chain_replays'] = reconstruction_replays
        elif case.get('state') not in ('NOT_ATTEMPTED_OUTSIDE_AUTHORIZED_SUBSET',
                'NOT_ATTEMPTED_BATCH_BUDGET_EXHAUSTED'):
            if (output / 'automatic-revisions' / case['case_id']).exists():
                raise ValueError('REAL_BATCH_ORCHESTRATION_FAILURE_NOT_REPLAYABLE')
        run_path = directory / 'run.json' if directory is not None else None
        if case.get('run_sha256') or (run_path is not None and run_path.is_file()):
            run = read(run_path)
            receipt = validate_pipeline_run(run, output=directory,
                verify_implementation=verify_implementation)
            if run['run_sha256'] != case.get('run_sha256', run['run_sha256']):
                raise ValueError('REAL_BATCH_RUN_BINDING_CHANGED')
            item['replay_verified'] = receipt['replay_verified']
            item['replay_sha256'] = receipt['replay_sha256']
        replayed.append(item)
    ledger = read(output / 'model-call-ledger.json')
    for record in ledger['records']:
        if record.get('provider_request_performed') and not record.get('expected_gain'):
            raise ValueError('REAL_BATCH_CALL_WITHOUT_EXPECTED_GAIN')
    aggregate = dict(format=BATCH_FORMAT + '-receipt', batch_id=progress['batch_id'],
        fixed_case_count=len(replayed), state_counts=dict(sorted(Counter(row['state'] for row in replayed).items())),
        cases=replayed, replay_verified=all(row.get('replay_verified') is not False for row in replayed),
        implementation_snapshot_verified=verify_implementation,
        model_usage={key: value for key, value in ledger.items() if key != 'records'},
        clinical_accuracy_measured=False, representative_performance_estimated=False,
        clinical_reference_created=False, formal_human_sampling_completed=False,
        statistical_qualification_assessed=False, doctor_assignment_created=False,
        clinical_release_permitted=False)
    aggregate['case_inventory'] = inventory_receipt
    aggregate['verified_case_count'] = sum(row.get('replay_verified') is True for row in replayed)
    if persist:
        _persist_receipt(output, 'batch-replay-receipt.json', aggregate)
    return aggregate


def execution_report(output):
    """Aggregate persisted facts without reconstructing provider responses."""
    output = Path(output)
    progress = read(output / 'batch-progress.json')
    replay = read(output / 'batch-replay-receipt.json')
    cases = []
    for row in progress['cases']:
        case = dict(case_id=row['case_id'], state=row['state'], reason_code=row.get('reason_code'))
        path = output / 'episodes' / row['case_id'] / row['episode_id'] / 'run.json' \
            if row.get('episode_id') else None
        if path is not None and path.is_file():
            run = read(path)
            revision_path = output / 'automatic-revisions' / row['case_id'] / 'automatic-revision-summary.json'
            if revision_path.is_file():
                revision = read(revision_path)
                generations = revision.get('generations', [])
                if generations:
                    generation = output / 'automatic-revisions' / row['case_id'] / (
                        'generation-%04d' % generations[-1])
                    path = generation / 'run.json'
                    run = read(path)
                case.update(automatic_revision_state=revision['state'],
                    automatic_revision_stop_reason=revision['stop_reason'],
                    graph_reconstruction_generations=len(generations))
            graph = run.get('graph_audit_chain', {})
            failure = graph.get('failure', {})
            failure_class = None
            if failure:
                if failure.get('stage') == 'PROVIDER_REQUEST':
                    failure_class = 'PROVIDER_REQUEST_FAILED'
                elif failure.get('stage') == 'RESPONSE_VALIDATION':
                    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.audit import validate_graph_audit
                    try:
                        validate_graph_audit(graph['initial_request'], failure.get('response'))
                    except Exception as exc:
                        failure_class = 'RESPONSE_VALIDATION:' + str(exc)
                    else:
                        failure_class = 'RESPONSE_VALIDATION:' + failure.get('error_type', 'UNSPECIFIED')
            case.update(graph_state=graph.get('state'), graph_stop_reason=graph.get('stop_reason'),
                graph_failure_stage=failure.get('stage'), graph_failure_type=failure.get('error_type'),
                graph_failure_class=failure_class,
                replay_verified=validate_pipeline_run(run, output=path.parent,
                    verify_implementation=False)['replay_verified'],
                model_usage=run['model_usage'])
            if run.get('episode_material_set') is not None:
                materials = run['episode_material_set']['checkpoint_materials']
                case.update(checkpoint_count=len(materials),
                    material_states=dict(sorted(Counter(item['state'] for item in materials).items())),
                    delivery_gate_present=run.get('delivery_gate') is not None,
                    delivery_gate_reached=any(item['stage'] == 'DELIVERY_GATE' for item in run['stages']),
                    actual_delivery_count=len(run.get('actual_deliveries', [])),
                    ready_resource_created=run.get('ready_resource') is not None)
        cases.append(case)
    return dict(format=BATCH_FORMAT + '-technical-closure-report', batch_id=progress['batch_id'],
        fixed_case_count=len(cases), terminal_state_counts=dict(sorted(Counter(row['state'] for row in cases).items())),
        graph_audit_eligible_case_count=len(cases),
        model_usage=replay['model_usage'], all_saved_episode_chains_replay_verified=all(
            row.get('replay_verified') is not False for row in cases),
        cases=cases, limitations=[
            'FIXED_TEN_DEVELOPMENT_SET_NOT_CLINICAL_PERFORMANCE_STUDY',
            'EPISODE_PROCESSING_UNIT_SOURCE_SCOPE_NOT_WHOLE_PATIENT_CHART',
            'HISTORICAL_IMPLEMENTATION_PROVENANCE_RETAINED_CURRENT_SOURCE_SNAPSHOT_NOT_CERTIFIED',
            'PENDING_INDEPENDENT_SEMANTIC_AUDIT', 'QUESTIONNAIRE_NOT_FROZEN',
        ], clinical_accuracy_measured=False, clinical_reference_created=False,
        formal_human_sampling_completed=False, doctor_assignment_created=False,
        clinical_release_permitted=False)
