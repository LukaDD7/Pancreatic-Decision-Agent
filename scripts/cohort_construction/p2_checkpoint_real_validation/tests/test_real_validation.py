"""Synthetic regressions for the real-batch adapter and delivery hold gate."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import (
    _pass_only, _passing_answer, fictional_graph,
)
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.orchestration import run_episode_pipeline
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.replay import validate_pipeline_run
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import read
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response
from scripts.cohort_construction.p2_checkpoint_real_validation.budget import BatchModelBudget
from scripts.cohort_construction.p2_checkpoint_real_validation.inputs import select_episode
from scripts.cohort_construction.p2_checkpoint_real_validation.batch import _authorized_config, _summary_state


CONFIG = dict(remote_execution_authorized=True, doctor_packet_release=False,
    real_doctor_assignment_creation=False, clinical_release_permitted=False)


def _synthetic_material_failure():
    from scripts.cohort_construction.p2_checkpoint_material_governance.review import CHAIN_FORMAT
    error = RuntimeError('SYNTHETIC_MATERIAL_HOLD')
    error.receipt = dict(invocation_id='SYNTHETIC-MATERIAL', cached=False,
        status='FAILED', attempts=[dict(attempt=1)], legacy_chain_format=CHAIN_FORMAT)
    return error


def test_CE_REAL026_partial_release_preserves_missing_cases_without_cohort_substitution(
        private_tmp, monkeypatch):
    """Only an actually completed case may become an offline downstream graph."""
    from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_run import FixedTenRunnerTests
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import preflight_batch
    fixture = FixedTenRunnerTests(methodName='test_CE_REAL023_dry_run_defaults_full_ten_without_provider_or_cache')
    fixture.setUp()
    try:
        from unittest.mock import patch
        with patch.object(FixedTenRunnerTests, 'fake_process', wraps=fixture.fake_process):
            contract, output, cache = fixture.dry_run()
        complete = private_tmp / 'complete-output'
        complete.mkdir()
        case_dir = complete / 'SCALE-01'
        case_dir.mkdir()
        from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import write
        write(case_dir / 'source_fhir_r4_bundle.json', {'resourceType': 'Bundle', 'entry': []})
        calls = []
        def fake_inventory(directory):
            calls.append(directory.name)
            return [dict(episode_id='EPISODE-01', checkpoint_ids=[])]
        monkeypatch.setattr(
            'scripts.cohort_construction.p2_checkpoint_real_validation.batch.episode_inventory', fake_inventory)
        monkeypatch.setattr('scripts.cohort_construction.p2_checkpoint_real_validation.batch.load_episode_case',
            lambda directory: dict(graph=None, episode=None))
        result = preflight_batch(manifest=fixture.manifest, derived_release=complete,
            output=private_tmp / 'partial-preflight', private_root=private_tmp)
        assert result['fixed_case_count'] == 10
        assert result['available_case_input_count'] == 1
        assert result['missing_case_input_count'] == 9
        assert result['hold_case_count'] == 10
        assert result['graph_eligible_case_count'] == 0
        assert sum(row['reason_codes'] == ['EMPTY_CANDIDATE_EPISODES']
            for row in result['cases']) == 1
        assert calls == ['SCALE-01']
        unavailable = [row for row in result['cases'] if row['case_id'] != 'SCALE-01']
        assert all(row['reason_codes'] == ['CASE_INPUT_NOT_AVAILABLE_IN_THIS_DERIVED_RELEASE']
            for row in unavailable)
        assert all(row['source_bundle_sha256'] is None for row in unavailable)
        assert all(row['input_availability'] ==
            'SOURCE_BUNDLE_NOT_PRESENT_NOT_PATIENT_EXCLUSION' for row in unavailable)
        assert not result['model_call_performed']
    finally:
        fixture.tearDown()


@pytest.fixture
def private_tmp(tmp_path):
    from scripts.cohort_construction.p2_checkpoint_material_review_app import build
    from scripts.cohort_construction.p2_checkpoint_material_review_app.build import private_mkdir
    root = build.PRIVATE_ROOT.resolve() / 'p2_checkpoint_real_validation_tests_20261101'
    private_mkdir(root, parents=True, exist_ok=True)
    destination = root / (tmp_path.name + '-' + str(len(list(root.glob(tmp_path.name + '*')))))
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    return destination


def test_CE_REAL001_selection_is_lexical_and_never_replaces_empty_candidates():
    assert select_episode([dict(episode_id='EP-Z'), dict(episode_id='EP-A')])['episode_id'] == 'EP-A'
    assert select_episode([]) is None


def test_CE_REAL007_relocated_default_output_stays_inside_migrated_private_parent(tmp_path, monkeypatch):
    """Bind one explicit physical parent; a path name alone never grants trust."""
    from scripts.cohort_construction.p2_checkpoint_material_review_app.build import (
        PRIVATE_ROOT, private_mkdir, require_private_output,
    )
    from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError
    from scripts.cohort_construction.p2_checkpoint_real_validation import __main__ as cli
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import new_output
    from unittest.mock import patch
    physical = tmp_path / 'external-volume' / 'project' / 'private' / 'p2_checkpoint_next_20261028'
    private_mkdir(physical, parents=True)
    monkeypatch.setattr(cli, 'DEFAULT_STORAGE_ROOT', physical)
    monkeypatch.setattr(cli, 'DEFAULT_OUTPUT', physical / 'batch')
    parsed = cli.argparse.Namespace(command='preflight', output=None, output_root=cli.DEFAULT_OUTPUT,
        private_root=None, manifest=cli.DEFAULT_BASE / 'sampling_manifest.json',
        derived_release=cli.DEFAULT_BASE / 'fixed_ten_development_release')
    monkeypatch.setattr(cli.argparse.ArgumentParser, 'parse_args', lambda self, argv=None: parsed)
    with patch('scripts.cohort_construction.p2_checkpoint_real_validation.__main__.preflight_batch', return_value={}) as preflight:
        cli.main()
    assert preflight.call_args.kwargs['private_root'] == cli.DEFAULT_STORAGE_ROOT
    assert preflight.call_args.kwargs['output'] == physical / 'batch' / 'preflight'
    output = new_output(cli.DEFAULT_OUTPUT / 'preflight', private_root=cli.DEFAULT_STORAGE_ROOT)
    assert output.is_relative_to(physical.resolve())
    assert output.stat().st_mode & 0o077 == 0
    for unrelated in (tmp_path / 'elsewhere', Path('/private/var')):
        with pytest.raises(ContractError, match='MRA_REAL_ASSIGNMENT_OUTPUT_MUST_BE_PRIVATE'):
            require_private_output(unrelated / 'output')
    with pytest.raises(ContractError, match='MRA_REAL_ASSIGNMENT_OUTPUT_MUST_BE_PRIVATE'):
        require_private_output(tmp_path / 'public', private_root=PRIVATE_ROOT)


def test_CE_REAL022_provider_called_first_stage_hold_without_run_replays(private_tmp):
    """A settled first-stage call and a pre-run failure remain an explicit HOLD."""
    from unittest.mock import patch
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import (
        execute_batch, execution_report, replay_batch,
    )
    from scripts.cohort_construction.p2_checkpoint_real_validation.inventory import (
        validate_inventory,
    )
    graph, sources = fictional_graph()
    preflight, authorization = _write_synthetic_batch_case(private_tmp, graph, sources, sources)
    class Client:
        def ask(self, task, payload, schema, system_instructions):
            assert task == 'checkpoint_candidate_reconstruction_independent_audit'
            response = passing_graph_response(payload)
            response.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
                request_id='REV-PRE-RUN-HOLD', request_kind='EVENT_RECONSTRUCTION', reason='虚构重建',
                affected_event_ids=['EVENT-01'], affected_episode_ids=['EPISODE-01'],
                evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
                required_reconstruction=True)])
            return response, _receipt('SYNTHETIC-FIRST-STAGE')
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import execute_batch
    execution = private_tmp / 'execution'
    def fail_before_finish(run, output, budget, continuation, persist):
        assert not (output / 'run.json').exists()
        raise RuntimeError('SYNTHETIC_FAILURE_BEFORE_RUN')
    with patch(
            'scripts.cohort_construction.p2_checkpoint_ready_pipeline.orchestration._finish',
            side_effect=fail_before_finish):
        execute_batch(preflight_output=preflight, authorization=authorization,
            base_config=CONFIG, model_client_factory=lambda cache, config: Client(),
            output=execution, private_root=private_tmp)
    assert not (execution / 'episodes/SCALE-01/EPISODE-01/run.json').exists()
    progress = read(execution / 'batch-progress.json')
    case = progress['cases'][0]
    assert case['case_id'] == 'SCALE-01'
    assert case['episode_id'] == 'EPISODE-01'
    assert case['state'] == 'ORCHESTRATION_HOLD'
    assert case['reason_code'] == 'RuntimeError'
    assert 'run_sha256' not in case
    ledger = read(execution / 'model-call-ledger.json')
    assert [row['outcome'] for row in ledger['records']] == [
        'PROVIDER_IO_STARTED_OR_UNSETTLED', 'RESPONSE_RETURNED_PENDING_CHAIN_VALIDATION']
    inventory = validate_inventory(execution, progress, ledger)
    assert inventory['recorded_calls_settled'] and inventory['attempted_case_count'] == 1
    result = replay_batch(execution)
    report = execution_report(execution, replay=result)
    assert report['all_saved_episode_chains_replay_verified'] is None
    assert report['saved_episode_chain_count'] == 0
    assert report['graph_audit_eligible_case_count'] == 0
    replayed = result['cases'][0]
    assert replayed['state'] == 'ORCHESTRATION_HOLD'
    assert replayed['episode_id'] == 'EPISODE-01'
    assert replayed['reason_code'] == 'RuntimeError'
    assert replayed['case_id'] == 'SCALE-01'
    assert 'run_sha256' not in replayed and 'replay_verified' not in replayed
    assert result['case_inventory'] == inventory
    assert read(execution / 'batch-replay-receipt.json') == result


def test_CE_REAL022_called_hold_cannot_bypass_run_evidence_or_claim_success(private_tmp):
    """Forged hashes and successful no-run states still fail closed."""
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import replay_batch
    from scripts.cohort_construction.p2_checkpoint_real_validation.inventory import validate_inventory
    graph, sources = fictional_graph()
    preflight, _ = _write_synthetic_batch_case(private_tmp, graph, sources, sources)
    execution = private_tmp / 'execution'
    execution.mkdir()
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import write
    contract = dict(batch_id='SYNTHETIC-BATCH', manifest_sha256='a' * 64,
        semantic_model='kimi-k3', max_requests=60, max_http_attempts=600,
        remote_execution_authorized=True, doctor_packet_release=False,
        real_doctor_assignment_creation=False, clinical_release_permitted=False,
        execution_subset=['SCALE-01'], model_switch_is_explicit=True, no_case_replacement=True,
        execution_inventory_version='checkpoint-real-execution-inventory-0.1.0')
    from scripts.cohort_construction.p2_checkpoint_real_validation.inventory import freeze_inventory
    preflight_value = read(preflight / 'batch-preflight.json')
    batch_contract = read(preflight / 'batch-contract.json')
    ledger = BatchModelBudget(execution, max_requests=60, max_http_attempts=600)
    ledger.adapter('graph', lambda task, payload, schema: (dict(synthetic='settled'),
        _receipt('SYNTHETIC-FIRST-STAGE')),
        dict(case_id='SCALE-01', episode_id='EPISODE-01'))('task', {'synthetic': True}, {})
    write(execution / 'effective-batch-authorization.json', contract)
    write(execution / 'batch-execution-inventory.json',
        freeze_inventory(preflight_value, batch_contract, contract))
    for state, run_sha256, expected in [
            ('ORCHESTRATION_HOLD', 'f' * 64, 'RUN_EVIDENCE|RUN_BINDING'),
            ('DELIVERY_GATE_HOLD', None, 'RUN_EVIDENCE_REQUIRED')]:
        case = dict(case_id='SCALE-01', episode_id='EPISODE-01', state=state,
            reason_code='SYNTHETIC_EVIDENCE_BYPASS')
        if run_sha256 is not None:
            case['run_sha256'] = run_sha256
        progress = dict(batch_id='SYNTHETIC-BATCH', cases=[case], model_usage=ledger.snapshot())
        from scripts.cohort_construction.p2_checkpoint_real_validation.budget import _journal_write
        _journal_write(execution / 'batch-progress.json', progress)
        with pytest.raises(ValueError, match=expected):
            validate_inventory(execution, progress, ledger.snapshot())
        with pytest.raises(ValueError, match=expected):
            replay_batch(execution)


def test_CE_REAL022_called_hold_requires_episode_and_reason(private_tmp):
    """A bare HOLD label is not an episode-level pre-run failure receipt."""
    from scripts.cohort_construction.p2_checkpoint_real_validation.inventory import (
        freeze_inventory, validate_inventory,
    )
    graph, sources = fictional_graph()
    preflight, _ = _write_synthetic_batch_case(private_tmp, graph, sources, sources)
    execution = private_tmp / 'execution'
    execution.mkdir()
    contract = dict(batch_id='SYNTHETIC-BATCH', manifest_sha256='a' * 64,
        execution_subset=['SCALE-01'], execution_inventory_version='checkpoint-real-execution-inventory-0.1.0')
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import write
    write(execution / 'effective-batch-authorization.json', contract)
    write(execution / 'batch-execution-inventory.json', freeze_inventory(
        read(preflight / 'batch-preflight.json'), read(preflight / 'batch-contract.json'), contract))
    ledger = BatchModelBudget(execution, max_requests=60, max_http_attempts=600)
    ledger.adapter('graph', lambda task, payload, schema: (dict(synthetic='settled'),
        _receipt('SYNTHETIC-FIRST-STAGE')),
        dict(case_id='SCALE-01', episode_id='EPISODE-01'))('task', {'synthetic': True}, {})
    for field in ('episode_id', 'reason_code'):
        case = dict(case_id='SCALE-01', episode_id='EPISODE-01', state='ORCHESTRATION_HOLD',
            reason_code='RuntimeError')
        case.pop(field)
        progress = dict(batch_id='SYNTHETIC-BATCH', cases=[case])
        with pytest.raises(ValueError, match='CALLED_CASE_HOLD_EVIDENCE_REQUIRED'):
            validate_inventory(execution, progress, ledger.snapshot())


def test_CE_REAL022_unexpected_run_artifact_requires_validation(private_tmp):
    """A saved run is evidence to validate, never part of the no-run exemption."""
    from scripts.cohort_construction.p2_checkpoint_real_validation.inventory import validate_inventory
    graph, sources = fictional_graph()
    preflight, _ = _write_synthetic_batch_case(private_tmp, graph, sources, sources)
    execution = private_tmp / 'execution'
    execution.mkdir()
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import write
    contract = dict(batch_id='SYNTHETIC-BATCH', manifest_sha256='a' * 64,
        execution_subset=['SCALE-01'])
    write(execution / 'effective-batch-authorization.json', contract)
    ledger = BatchModelBudget(execution, max_requests=60, max_http_attempts=600)
    ledger.adapter('graph', lambda task, payload, schema: (dict(synthetic='settled'),
        _receipt('SYNTHETIC-FIRST-STAGE')),
        dict(case_id='SCALE-01', episode_id='EPISODE-01'))('task', {'synthetic': True}, {})
    progress = dict(batch_id='SYNTHETIC-BATCH', cases=[dict(case_id='SCALE-01',
        episode_id='EPISODE-01', state='ORCHESTRATION_HOLD', reason_code='RuntimeError')])
    run_path = execution / 'episodes/SCALE-01/EPISODE-01/run.json'
    run_path.parent.mkdir(parents=True)
    write(run_path, dict(run_sha256='not-a-replayable-run'))
    with pytest.raises(ValueError, match='CALLED_CASE_RUN_EVIDENCE_REQUIRED'):
        validate_inventory(execution, progress, ledger.snapshot())
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import replay_batch
    from scripts.cohort_construction.p2_checkpoint_real_validation.budget import _journal_write
    _journal_write(execution / 'batch-progress.json', progress)
    with pytest.raises(ValueError):
        replay_batch(execution)


def test_CE_REAL018_explicit_private_root_is_forwarded_without_weakening_boundary(tmp_path, monkeypatch):
    """A new private root is explicit; a missing/outside root never grants trust."""
    from scripts.cohort_construction.p2_checkpoint_material_review_app.build import (
        private_mkdir, require_private_output,
    )
    from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError
    from scripts.cohort_construction.p2_checkpoint_real_validation import __main__ as cli
    from unittest.mock import patch
    root = tmp_path / 'research-private'
    private_mkdir(root, parents=True)
    output = root / 'fixed-ten-batch'
    with patch(f'{cli.__name__}.preflight_batch', return_value={}) as preflight:
        cli.main(['--output-root', str(output), '--private-root', str(root), 'preflight'])
    assert preflight.call_args.kwargs['private_root'] == root.resolve()
    assert preflight.call_args.kwargs['output'] == output / 'preflight'
    assert not output.exists()  # The adapter mock must not create a real batch.
    with pytest.raises(SystemExit):
        cli.main(['--output-root', str(root / 'another'),
            '--private-root', str(root / 'not-created'), 'preflight'])
    with pytest.raises(ContractError, match='MRA_REAL_ASSIGNMENT_OUTPUT_MUST_BE_PRIVATE'):
        require_private_output(tmp_path / 'outside' / 'execution', private_root=root)

    with patch('scripts.cohort_construction.model_api.load_boyu_settings'), \
            patch('scripts.cohort_construction.p2_auto_checkpoint_v04.client.Client'), \
            patch(f'{cli.__name__}.execute_batch', return_value={}) as execute, \
            patch(f'{cli.__name__}.read', side_effect=[{}, dict(model_temperature=0, max_output_tokens=16000)]):
        cli.main(['--output-root', str(output), '--private-root', str(root), 'execute'])
    assert execute.call_args.kwargs['private_root'] == root.resolve()
    assert execute.call_args.kwargs['output'] == output / 'execution'


def test_CE_REAL019_frozen_graph_retains_actual_discovery_proposals_not_empty_shadow_rows():
    """Discovery metadata rows must not masquerade as zero-event discovery receipts."""
    from scripts.cohort_construction.p2_checkpoint_real_validation.inputs import load_episode_case
    graph, sources = fictional_graph()
    event = graph['events'][0]
    current = next(source for source in sources if source['source_id'] == 'DOC-01')
    event['actual_model_evidence'] = []
    event['evidence_context']['ledger'] = []
    event['evidence_context']['source_ledger'] = []
    routing_unit = dict(unit_id=event['unit_id'], source_ids=['DOC-01'],
        original_texts=[current], document_profile='clinical_note', kind='clinical_note',
        clock=current.get('clock'))
    proposal = dict(unit_id=event['unit_id'], events=[deepcopy(event)],
        rejected_mentions=[], input_context_summary=dict(source_ledger=[]))
    case = dict(case_id='SYNTHETIC', sources=[current], units=[routing_unit],
        discoveries={event['unit_id']: proposal}, events=[event],
        episodes=[dict(episode_id='EPISODE-01', event_ids=['EVENT-01'],
            checkpoint_ids=['EVENT-01'])], relations=[], routing=[],
        routing_provenance=dict(inventory_unit_membership_verified=False),
        final_graph_audit=None)
    with __import__('unittest.mock', fromlist=['patch']).patch(
            'scripts.cohort_construction.p2_checkpoint_real_validation.inputs.load_case', return_value=case):
        loaded = load_episode_case('SYNTHETIC')
    assert loaded['graph']['discovery_units'][0]['events'][0]['event_id'] == 'EVENT-01'
    assert loaded['graph']['discovery_units'][0]['source_ids'] == ['DOC-01']
    assert loaded['graph']['discovery_units'][0] is not routing_unit
    assert loaded['graph_request']['zero_discovery_and_rejected_units'][0]['events']
    coverage = loaded['tasks'][0]['manifest']['sources']
    assert [source['source_id'] for source in coverage] == ['DOC-01']


def test_CE_REAL024_projected_material_receipt_and_batch_ledger_match(private_tmp):
    """Regression for r6 FULL_REQUEST receipt versus compact wire payload."""
    from scripts.cohort_construction.p2_checkpoint_material_governance.projection import FORMAT
    from scripts.cohort_construction.p2_checkpoint_material_governance.review import (
        run_review_chain,
    )
    from scripts.cohort_construction.p2_checkpoint_packet.compiler import (
        canonical_sha256 as payload_sha256,
    )
    from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_material_governance import inputs
    manifest, compiled = inputs('虚构材料原文。')
    context = dict(decision_time_evidence='UNRESOLVED', clinical_visibility_certified=False)
    seen = []
    def ask(task, payload, schema):
        seen.append(payload)
        return dict(format='material-review-0.3.0'), dict(cached=False,
            receipt_origin='PROVIDER_CLIENT', status='SUCCESS',
            provider_invocation_occurred=True, provider_transport_verified=True,
            declared_provider_payload_sha256=payload_sha256(payload))
    adapter = BatchModelBudget(private_tmp, max_requests=2, max_http_attempts=20).adapter(
        'material', ask, dict(case_id='SYNTHETIC'))
    def bound(task, payload, schema, *, transport_projection=None):
        return adapter(task, payload, schema, transport_projection=transport_projection)
    chain = run_review_chain(manifest, compiled, bound, task_context=context,
        transport_projection_format=FORMAT)
    assert chain['stop_reason'] == 'EXECUTION_FAILED'
    assert chain['failure']['reason_code'] == 'MATERIAL_REVIEW_FIELDS_MISMATCH'
    assert chain['failure']['receipt']['status'] == 'SUCCESS'
    # A request without the optional whole-graph snapshot legitimately projects
    # byte-for-byte to itself; this does not manufacture a projection envelope.
    assert seen and 'material_transport_projection' not in seen[0]
    assert chain['failure']['transport_projection_format'] == FORMAT
    ledger = read(private_tmp / 'model-call-ledger.json')
    assert ledger['records'][-1]['provider_payload_binding_verified'] is True
    assert ledger['records'][-1]['receipt']['declared_provider_payload_sha256'] \
        == ledger['records'][-1]['request_sha256']


def test_CE_REAL024_batch_freezes_implementation_once_for_all_episode_runs(private_tmp):
    """A changed working tree during execution cannot relabel earlier runs."""
    from unittest.mock import patch
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import execute_batch
    graph, sources = fictional_graph()
    preflight, authorization = _write_synthetic_batch_case(private_tmp, graph, sources, sources)
    class Client:
        def ask(self, task, payload, schema, system_instructions):
            return passing_graph_response(payload), _receipt('SYNTHETIC-FROZEN')
    frozen = dict(format='checkpoint-ready-pipeline-implementation-snapshot-0.1.0',
        implementation_sha256='FROZEN-ONE')
    with patch('scripts.cohort_construction.p2_checkpoint_real_validation.batch.implementation_snapshot',
            return_value=frozen) as outer, patch(
            'scripts.cohort_construction.p2_checkpoint_ready_pipeline.orchestration.implementation_snapshot',
            side_effect=AssertionError('LIVE_WORKING_TREE_REHASH_FORBIDDEN')), patch(
            'scripts.cohort_construction.p2_checkpoint_ready_pipeline.automatic_revision.run_bounded_graph_revision_pipeline',
            side_effect=AssertionError('NOT_THE_FROZEN_SNAPSHOT_TEST')), patch(
            'scripts.cohort_construction.p2_checkpoint_real_validation.batch.validate_pipeline_run',
            side_effect=AssertionError('CURRENT_CODE_CANNOT_REPLAY_FROZEN_SYNTHETIC_HASH')), patch(
            'scripts.cohort_construction.p2_checkpoint_real_validation.batch.replay_batch',
            return_value=dict(replay_verified=True, cases=[], test_scope='FROZEN_IMPLEMENTATION_IDENTITY_ONLY')):
        execute_batch(preflight_output=preflight, authorization=authorization,
            base_config=CONFIG, model_client_factory=lambda cache, config: Client(),
            output=private_tmp / 'execution', private_root=private_tmp)
    assert outer.call_count == 1
    assert read(private_tmp / 'execution/episodes/SCALE-01/EPISODE-01/implementation-snapshot.json') == frozen


def test_CE_REAL002_batch_budget_counts_failures_cache_and_preserves_call_gain(private_tmp):
    ledger = BatchModelBudget(private_tmp, max_requests=2, max_http_attempts=6)
    def cached(task, payload, schema):
        return dict(ok=True), dict(cached=True, usage=dict(total_tokens=3))
    response, _ = ledger.adapter('graph', cached, dict(case_id='SYNTHETIC'))('task', {'x': 1}, {})
    assert response == {'ok': True} and ledger.requests == 0 and ledger.cache_hits == 1
    def fail(task, payload, schema):
        error = RuntimeError('synthetic failure')
        error.receipt = dict(cached=False, status='FAILED', attempts=[dict(attempt=1), dict(attempt=2)])
        raise error
    with pytest.raises(RuntimeError):
        ledger.adapter('material', fail, dict(case_id='SYNTHETIC'))('task', {'x': 2}, {})
    assert ledger.requests == 1 and ledger.http_attempts == 2
    assert all(row['expected_gain'] for row in ledger.records)
    assert ledger.snapshot()['cost_amount'] is None


def test_CE_REAL003_real_delivery_gate_stops_before_packet_or_human_factory(private_tmp):
    graph, sources = fictional_graph()
    calls = []
    def material(task, request, schema):
        calls.append('material')
        target = request['target']['checkpoint_id']
        # Keep one initial review per Checkpoint; subsequent identical requests
        # are intentionally answered with the same deterministic PASS fixture.
        count = sum(1 for row in getattr(material, 'targets', []) if row == target)
        material.targets = getattr(material, 'targets', []) + [target]
        if count:
            return _pass_only(request)
        from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import prepare_candidate_materials
        tasks, _ = prepare_candidate_materials(graph, sources, decision_task_id='SYNTHETIC',
            visibility_policy_version='SYNTHETIC')
        task = next(item for item in tasks if item['compiled']['target']['checkpoint_id'] == target)
        response = _passing_answer(task, task['manifest'], task['compiled'],
            task['candidate_sources'], task['excluded_sources'])
        response.update(request['response_binding'])
        return response
    run = run_episode_pipeline(output=private_tmp / 'run', graph=graph, sources=sources,
        episode_id='EPISODE-01', semantic_audits={}, questionnaire=None,
        graph_audit_context=dict(processing_units=[dict(unit_id='ALL',
            source_ids=[source['source_id'] for source in sources])]),
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC', config=CONFIG,
        adapters=dict(graph=lambda t, r, s: passing_graph_response(r), material=material,
            delivery=lambda t, r, s: calls.append('delivery')), execute=True,
        delivery_gate=dict(reason_codes=['QUESTIONNAIRE_NOT_FROZEN',
            'PENDING_INDEPENDENT_SEMANTIC_AUDIT']))
    assert run['state'] == 'HOLD' and 'actual_deliveries' not in run
    assert 'delivery_audit_chain' not in run and 'ready_resource' not in run
    assert 'delivery' not in calls and run.get('resolved_semantic_audits') is None
    assert _summary_state(run)[0] == 'DELIVERY_GATE_HOLD'
    assert validate_pipeline_run(run, output=private_tmp / 'run')['replay_verified']
    tampered = deepcopy(run)
    tampered['delivery_gate']['reason_codes'] = []
    with pytest.raises(ValueError):
        validate_pipeline_run(tampered)


def test_CE_REAL004_batch_authorization_is_specific_and_never_releases():
    base = dict(remote_execution_authorized=False, clinical_release_permitted=False)
    contract = dict(batch_id='B', manifest_sha256='a' * 64,
        semantic_model='deepseek-v4.1-flash', max_requests=60, max_http_attempts=600,
        max_http_attempts_per_request=10,
        remote_execution_authorized=True, doctor_packet_release=False,
        real_doctor_assignment_creation=False, clinical_release_permitted=False)
    result = _authorized_config(base, contract)
    assert result['remote_execution_authorized'] and base['remote_execution_authorized'] is False
    assert result['max_http_attempts_per_request'] == 10 and contract['max_http_attempts'] == 600
    assert not result['doctor_packet_release'] and not result['clinical_release_permitted']
    with pytest.raises(ValueError, match='AUTHORIZATION_MISMATCH'):
        _authorized_config(base, dict(contract, semantic_model='unapproved-model'))
    with pytest.raises(ValueError, match='AUTHORIZATION_MISMATCH'):
        _authorized_config(base, dict(contract, semantic_model='kimi-k3'))
    subset = dict(contract, semantic_model='kimi-k3', execution_subset=['SCALE-06'],
        model_switch_is_explicit=True, no_case_replacement=True)
    assert _authorized_config(base, subset)['semantic_model'] == 'kimi-k3'
    with pytest.raises(ValueError, match='AUTHORIZATION_MISMATCH'):
        _authorized_config(base, dict(subset, semantic_model='unapproved-model'))
    paced = dict(subset, execution_subset=['SCALE-01','SCALE-05','SCALE-08'],
        min_request_interval_seconds=10, retry_backoff_base_seconds=30,
        retry_backoff_max_seconds=120, rate_control_is_explicit=True,
        http_400_rate_cause_unproven=True)
    paced_config = _authorized_config(base, paced)
    assert paced_config['min_request_interval_seconds'] == 10
    assert paced_config['retry_backoff_base_seconds'] == 30
    assert paced_config['retry_backoff_max_seconds'] == 120
    for invalid in (dict(paced, min_request_interval_seconds=0),
                    dict(paced, retry_backoff_base_seconds=2),
                    dict(paced, http_400_rate_cause_unproven=False)):
        with pytest.raises(ValueError, match='AUTHORIZATION_MISMATCH'):
            _authorized_config(base, invalid)
    compact = dict(paced, graph_audit_compact_transport=True,
        lossless_request_projection_is_explicit=True)
    assert _authorized_config(base, compact)['graph_audit_compact_transport'] is True
    for invalid in (dict(compact, graph_audit_compact_transport=False),
                    dict(compact, lossless_request_projection_is_explicit=False),
                    dict(paced, graph_audit_compact_transport=True)):
        with pytest.raises(ValueError, match='AUTHORIZATION_MISMATCH'):
            _authorized_config(base, invalid)
    closure = dict(compact, max_rounds=3, automatic_graph_reconstruction_authorized=True,
        max_graph_revisions=1, graph_revision_human_final_review_required=True)
    closure_config = _authorized_config(base, closure)
    assert closure_config['material_review_max_rounds'] == 3
    assert closure_config['automatic_graph_reconstruction'] is True
    for invalid in (dict(closure, max_rounds=1),
                    dict(closure, automatic_graph_reconstruction_authorized=False),
                    dict(closure, max_graph_revisions=0),
                    dict(compact, max_rounds=3)):
        with pytest.raises(ValueError, match='AUTHORIZATION_MISMATCH'):
            _authorized_config(base, invalid)


def test_CE_REAL010_pacing_receipt_is_audited_without_provider_content(private_tmp):
    ledger = BatchModelBudget(private_tmp, max_requests=1, max_http_attempts=10,
        max_http_attempts_per_request=10)
    def ask(task, payload, schema):
        return dict(ok=True), dict(cached=False, status='SUCCESS', attempts=[dict(attempt=1)],
            min_request_interval_seconds=10, retry_backoff_base_seconds=30,
            retry_backoff_max_seconds=120, pacing_seconds_total=0,
            exception_body='must-not-be-copied')
    ledger.adapter('graph', ask, dict(case_id='SYNTHETIC'))('task', {'x':1}, {})
    receipt = ledger.records[-1]['receipt']
    assert receipt['min_request_interval_seconds'] == 10
    assert receipt['retry_backoff_max_seconds'] == 120
    assert 'exception_body' not in receipt


def test_CE_REAL005_progress_snapshots_preserve_every_case_event(private_tmp):
    path = private_tmp / 'batch-progress.json'
    events = private_tmp / 'batch-progress-events'
    for index in (1, 2):
        progress = dict(cases=list(range(index)))
        from scripts.cohort_construction.p2_checkpoint_real_validation.budget import _journal_write
        from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import write
        _journal_write(path, progress)
        events.mkdir(exist_ok=True)
        write(events / ('%06d.json' % index), progress)
        assert read(path) == progress
    assert [read(path) for path in sorted(events.glob('*.json'))] == [dict(cases=[0]), dict(cases=[0, 1])]
    assert sorted(private_tmp.glob('batch-progress.json*')) == [path]


def test_CE_REAL006_historical_replay_keeps_chain_validation_and_provenance(private_tmp):
    graph, sources = fictional_graph()
    run = run_episode_pipeline(output=private_tmp / 'run', graph=graph, sources=sources,
        episode_id='EPISODE-01', semantic_audits={}, questionnaire=None,
        graph_audit_context=dict(processing_units=[dict(unit_id='ALL',
            source_ids=[source['source_id'] for source in sources])]),
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC', config=CONFIG,
        adapters=dict(graph=lambda t, r, s: passing_graph_response(r),
            material=lambda t, r, s: (_ for _ in ()).throw(_synthetic_material_failure()),
            delivery=lambda t, r, s: pytest.fail('delivery should not run')), execute=True,
        delivery_gate=dict(reason_codes=['QUESTIONNAIRE_NOT_FROZEN']))
    from unittest.mock import patch
    with patch('scripts.cohort_construction.p2_checkpoint_ready_pipeline.replay.implementation_snapshot',
               return_value=dict(implementation_sha256='current-source-changed')):
        with pytest.raises(ValueError, match='SAVED_ARTIFACT_CHANGED'):
            validate_pipeline_run(run, output=private_tmp / 'run')
        receipt = validate_pipeline_run(run, output=private_tmp / 'run', verify_implementation=False)
    assert receipt['replay_verified'] and receipt['state'] == 'HOLD'
    tampered = deepcopy(run)
    tampered['graph_audit_chain']['chain_sha256'] = '0' * 64
    with pytest.raises(ValueError):
        validate_pipeline_run(tampered, verify_implementation=False)


def test_CE_REAL014_execution_report_uses_safe_specific_transport_failure_class():
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import _graph_failure_class
    for stage, reason in [('PROVIDER_REQUEST', 'ConnectionError'),
            ('PROVIDER_HTTP_RESPONSE', 'PROVIDER_HTTP_503'),
            ('PROVIDER_ENVELOPE_PARSE', 'PROVIDER_ENVELOPE_INVALID'),
            ('PROVIDER_CONTENT_PARSE', 'PROVIDER_CONTENT_MISSING'),
            ('RESPONSE_SCHEMA_VALIDATION', 'RESPONSE_SCHEMA_VALIDATION_FAILED')]:
        graph = dict(failure=dict(stage=stage, reason_code=reason,
            raw_provider_body='synthetic private sentinel'))
        expected = reason if stage == 'PROVIDER_REQUEST' else stage + ':' + reason
        assert _graph_failure_class(graph) == expected
        assert 'synthetic private sentinel' not in _graph_failure_class(graph)
    assert _graph_failure_class(dict(failure=dict(
        stage='RESPONSE_SCHEMA_VALIDATION', error_type='ValidationError')
    )) == 'RESPONSE_SCHEMA_VALIDATION:UNSPECIFIED_PROVIDER_FAILURE'
    assert _graph_failure_class({}) is None
def test_CE_REAL012_historical_source_closure_recovers_only_frozen_verified_originals():
    from scripts.cohort_construction.p2_checkpoint_real_validation.inputs import (
        resolve_episode_source_dependencies,
    )
    graph, sources = fictional_graph()
    extra = dict(source_id='DOC-HISTORY', source_version='1', text='虚构既往来源。')
    case = dict(sources=sources + [extra])
    units = [dict(unit_id='CURRENT', source_ids=['DOC-01', 'DOC-02', 'DOC-ZERO'])]
    event = deepcopy(graph['events'][0])
    event['actual_model_evidence'] = [dict(source_id='DOC-BUDGET'), dict(source_id='DOC-HISTORY')]
    current, recovered, receipt = resolve_episode_source_dependencies(case, [event], units)
    assert len(current) == 3 and len(recovered) == 5
    assert receipt['recovered_frozen_package_source_ids'] == ['DOC-BUDGET', 'DOC-HISTORY']
    assert receipt['not_found_in_delivery_source_ids'] == []
    assert event['actual_model_evidence'] == [dict(source_id='DOC-BUDGET'), dict(source_id='DOC-HISTORY')]
    assert all(not row['doctor_blind_review_permission_granted'] if 'doctor_blind_review_permission_granted' in row
        else row['doctor_blind_review_permission'] == 'NOT_GRANTED_BY_SOURCE_RECONCILIATION'
        for row in receipt['source_provenance'])
    assert all(row['target_time_visibility_status'] == 'NOT_CERTIFIED_BY_SOURCE_RECONCILIATION'
        for row in receipt['source_provenance'])
    # A missing historical source remains a precise dependency gap, not an empty fact.
    event_missing = deepcopy(event)
    event_missing['actual_model_evidence'].append(dict(source_id='DOC-NOT-DELIVERED'))
    _, sources_missing, missing = resolve_episode_source_dependencies(case, [event_missing], units)
    assert len(sources_missing) == 5 and not missing['dependency_closure_complete']
    assert missing['not_found_in_delivery_source_ids'] == ['DOC-NOT-DELIVERED']
    event_zero = deepcopy(event)
    event_zero['source_ids'] = ['DOC-01']
    event_zero['actual_model_evidence'] = [dict(source_id='DOC-01')]
    event_zero['evidence_context']['ledger'] = []
    event_zero['evidence_context']['source_ledger'] = []
    _, sources_zero, zero = resolve_episode_source_dependencies(case, [event_zero], units)
    assert len(sources_zero) == 3 and zero['dependency_closure_complete']
    assert zero['recovered_frozen_package_source_ids'] == []


def test_CE_REAL013_omission_only_closure_reaches_material_candidates_or_local_hold():
    from scripts.cohort_construction.p2_checkpoint_real_validation.inputs import resolve_episode_source_dependencies
    from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import prepare_checkpoint_task
    graph, all_sources = fictional_graph()
    units = [dict(unit_id='UNIT-EVENT-01', source_ids=['DOC-01'])]
    for disposition in ('omitted_budget', 'omitted_provider_payload_budget'):
        event = deepcopy(graph['events'][0])
        event['actual_model_evidence'] = []
        event['evidence_context']['ledger'] = [dict(source_id='DOC-BUDGET', disposition=disposition)]
        event['evidence_context']['source_ledger'] = deepcopy(event['evidence_context']['ledger'])
        _, recovered, receipt = resolve_episode_source_dependencies(dict(sources=all_sources), [event], units)
        assert receipt['dependency_closure_complete']
        assert receipt['event_budget_omission_source_ids'] == {'EVENT-01': ['DOC-BUDGET']}
        local_graph = deepcopy(graph)
        local_graph['events'][0] = event
        task = prepare_checkpoint_task(local_graph, recovered, target_event_id='EVENT-01',
            decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC')
        assert task['candidate_sources'][0]['source_id'] == 'DOC-BUDGET'
        assert task['candidate_sources'][0]['text'] == '虚构预算遗漏的既往完整报告。'
        _, _, missing = resolve_episode_source_dependencies(
            dict(sources=[s for s in all_sources if s['source_id'] != 'DOC-BUDGET']), [event], units)
        assert not missing['dependency_closure_complete']
        assert missing['not_found_in_delivery_source_ids'] == ['DOC-BUDGET']
        from scripts.cohort_construction.p2_checkpoint_materials.prepare import MaterialPreparationError
        with pytest.raises(MaterialPreparationError) as hold:
            prepare_checkpoint_task(local_graph, [s for s in recovered if s['source_id'] != 'DOC-BUDGET'],
                target_event_id='EVENT-01', decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC')
        assert str(hold.value) == 'ASSEMBLY_OMITTED_SOURCE_MISSING'
        malformed = deepcopy(event)
        malformed['evidence_context']['source_ledger'] = ['not-a-ledger-row']
        with pytest.raises(ValueError, match='LEDGER_ROW_MALFORMED'):
            resolve_episode_source_dependencies(dict(sources=all_sources), [malformed], units)
        unsupported = deepcopy(event)
        unsupported['evidence_context']['source_ledger'] = [dict(source_id='DOC-BUDGET', disposition='omitted_unknown')]
        with pytest.raises(ValueError, match='OMISSION_DISPOSITION_UNSUPPORTED'):
            resolve_episode_source_dependencies(dict(sources=all_sources), [unsupported], units)


def test_CE_REAL015_discovery_only_omission_reaches_graph_and_material_exactly():
    """A prior whole document omitted before review must remain a real dependency."""
    from scripts.cohort_construction.p2_auto_checkpoint_v04.context import context_for_discovery
    from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256, text_sha256
    from scripts.cohort_construction.p2_checkpoint_real_validation.inputs import (
        load_episode_case, resolve_episode_source_dependencies,
    )
    from scripts.cohort_construction.p2_checkpoint_material_governance.review import build_review_request
    graph, all_sources = fictional_graph()
    event = deepcopy(graph['events'][0])
    historical = deepcopy(next(source for source in all_sources if source['source_id'] == 'DOC-02'))
    historical['source_id'] = 'DOC-HISTORICAL-MATERIAL-ONLY'
    event['source_ids'] = []
    event['actual_model_evidence'] = [dict(source_id=historical['source_id'])]
    event['evidence_context']['ledger'] = []
    event['evidence_context']['source_ledger'] = []
    current = next(source for source in all_sources if source['source_id'] == 'DOC-01')
    prior = next(source for source in all_sources if source['source_id'] == 'DOC-BUDGET')
    prior['kind'] = 'document'
    prior['clock'] = dict(value='2019-12-31T09:00:00+08:00')
    current['clock'] = dict(value='2020-01-02T09:00:00+08:00')
    unit = dict(unit_id=event['unit_id'], source_ids=['DOC-01'], original_texts=[current],
        document_profile='clinical_note', kind='clinical_note', clock=current['clock'])
    discovery = context_for_discovery(unit, [current, prior], budget=len(current['text']))
    assert discovery['source_ledger'] == [dict(source_id='DOC-BUDGET',
        disposition='omitted_budget', category='recent_clinical',
        source_characters=len(prior['text']), full_source_not_truncated=True)]
    unit['input_context_summary'] = dict(source_ledger=deepcopy(discovery['source_ledger']))
    # Discovery-only receipt: no event ledger mentions it; the separate historical
    # evidence dependency protects the CE-REAL-012 material-only graph boundary.
    units = [dict(unit, events=[event])]
    current_sources, recovered, receipt = resolve_episode_source_dependencies(
        dict(sources=[current, prior, historical]), [event], units)
    assert [source['source_id'] for source in current_sources] == ['DOC-01']
    assert [source['source_id'] for source in recovered] == [
        'DOC-01', 'DOC-BUDGET', 'DOC-HISTORICAL-MATERIAL-ONLY']
    assert receipt['dependency_closure_complete']
    assert receipt['discovery_unit_budget_omission_source_ids'] == {unit['unit_id']: ['DOC-BUDGET']}
    discovery_receipt = receipt['discovery_budget_omission_receipts'][0]
    assert discovery_receipt['source_state'] == 'FULL_ORIGINAL_RECOVERED_FROM_FROZEN_PACKAGE'
    assert discovery_receipt['source_sha256'] == text_sha256(prior['text'])
    assert receipt['discovery_omission_not_clinical_absence']
    assert not receipt['target_time_visibility_certified']
    assert not receipt['doctor_blind_review_permission_granted']

    local_graph = deepcopy(graph)
    local_graph['events'][0] = event
    local_graph['discovery_units'] = [unit]
    local_graph['graph_sha256'] = canonical_sha256(
        {key: value for key, value in local_graph.items() if key != 'graph_sha256'})
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.audit import build_graph_audit_request
    request = build_graph_audit_request(local_graph, recovered,
        processing_units=units, discovery_proposals=[dict(unit_id=unit['unit_id'], events=[])])
    graph_original = next(source for source in request['audit_scope']['complete_originals_supplied']
        if source['source_id'] == 'DOC-BUDGET')
    assert graph_original['text'] == prior['text']
    assert graph_original['sha256'] == text_sha256(prior['text'])
    from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import prepare_checkpoint_task
    task = prepare_checkpoint_task(local_graph, recovered, target_event_id='EVENT-01',
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC')
    candidate = next(source for source in task['candidate_sources'] if source['source_id'] == 'DOC-BUDGET')
    assert candidate['text'] == prior['text']
    assert candidate['sha256'] == text_sha256(prior['text'])
    assert candidate['text'] == graph_original['text']
    assert task['preparation_audit']['candidate_source_ids_from_discovery_budget_omissions'] == ['DOC-BUDGET']
    assert task['preparation_audit']['discovery_budget_omission_receipts'][0][
        'recovery_is_not_prechoice_visibility_proof']
    material_request = build_review_request(task['manifest'], task['compiled'],
        candidate_sources=task['candidate_sources'], excluded_sources=task['excluded_sources'],
        task_context=task['task_context'])
    assert next(source for source in material_request['candidate_sources']
        if source['source_id'] == 'DOC-BUDGET')['text'] == prior['text']
    # Recovered originals enter the researcher review registry, never the initial doctor projection.
    assert all(source['source_id'] != 'DOC-BUDGET' for source in task['manifest']['sources'])

    case = dict(case_id='SYNTHETIC', sources=[current, prior, historical], units=units,
        discoveries={unit['unit_id']: dict(unit_id=unit['unit_id'], events=[])},
        events=[event], episodes=[dict(episode_id='EPISODE-01', event_ids=['EVENT-01'],
            checkpoint_ids=['EVENT-01'])], relations=[],
        routing=[], routing_provenance=dict(inventory_unit_membership_verified=False),
        final_graph_audit=None)
    with __import__('unittest.mock', fromlist=['patch']).patch(
            'scripts.cohort_construction.p2_checkpoint_real_validation.inputs.load_case', return_value=case):
        loaded = load_episode_case('SYNTHETIC')
    loaded_original = next(source for source in loaded['graph_request']['audit_scope'][
        'complete_originals_supplied'] if source['source_id'] == 'DOC-BUDGET')
    assert loaded_original['text'] == prior['text'] and loaded_original['sha256'] == text_sha256(prior['text'])
    assert loaded['graph_audit_sources'] == [recovered[0], recovered[1]]
    assert [source['source_id'] for source in loaded['graph_request']['audit_scope'][
        'complete_originals_supplied']] == ['DOC-01', 'DOC-BUDGET']
    assert loaded['graph_request']['audit_scope']['source_coverage_complete']
    assert loaded['graph_request']['audit_scope']['uncovered_supplied_source_ids'] == []
    assert [source['source_id'] for source in loaded['sources']] == [
        'DOC-01', 'DOC-BUDGET', 'DOC-HISTORICAL-MATERIAL-ONLY']
    historical_material = next(source for source in loaded['tasks'][0]['manifest']['sources']
        if source['source_id'] == 'DOC-HISTORICAL-MATERIAL-ONLY')
    assert historical_material['text'] == historical['text']
    assert all(source['source_id'] != 'DOC-HISTORICAL-MATERIAL-ONLY'
        for source in loaded['graph_audit_sources'])
    assert next(source for source in loaded['tasks'][0]['candidate_sources']
        if source['source_id'] == 'DOC-BUDGET')['text'] == prior['text']

    # A separate unit's Checkpoint does not inherit this discovery-only receipt.
    second = deepcopy(graph['events'][1])
    second['actual_model_evidence'] = []
    second['evidence_context']['ledger'] = []
    second['evidence_context']['source_ledger'] = []
    shared_case = deepcopy(case)
    shared_case['sources'].append(deepcopy(next(source for source in all_sources
        if source['source_id'] == 'DOC-02')))
    shared_case['events'].append(second)
    shared_case['episodes'][0]['event_ids'].append('EVENT-02')
    shared_case['episodes'][0]['checkpoint_ids'].append('EVENT-02')
    shared_case['units'].append(dict(unit_id=second['unit_id'], source_ids=['DOC-02'], events=[second]))
    shared_case['discoveries'][second['unit_id']] = dict(unit_id=second['unit_id'], events=[])
    with __import__('unittest.mock', fromlist=['patch']).patch(
            'scripts.cohort_construction.p2_checkpoint_real_validation.inputs.load_case', return_value=shared_case):
        shared_loaded = load_episode_case('SYNTHETIC')
    first_task = next(task for task in shared_loaded['tasks'] if task['event']['event_id'] == 'EVENT-01')
    second_task = next(task for task in shared_loaded['tasks'] if task['event']['event_id'] == 'EVENT-02')
    assert [source['source_id'] for source in first_task['candidate_sources']] == ['DOC-BUDGET']
    assert second_task['candidate_sources'] == []

    # Negative controls: no discovery receipt, no candidate; malformed receipts fail closed.
    no_omission = deepcopy(unit)
    no_omission['input_context_summary']['source_ledger'] = []
    _, controlled, controlled_receipt = resolve_episode_source_dependencies(
        dict(sources=[current, prior, historical]), [event], [no_omission])
    assert [source['source_id'] for source in controlled] == [
        'DOC-01', 'DOC-HISTORICAL-MATERIAL-ONLY']
    assert controlled_receipt['discovery_budget_omission_receipts'] == []
    controlled_graph = deepcopy(local_graph)
    controlled_graph['discovery_units'] = [no_omission]
    controlled_task = prepare_checkpoint_task(controlled_graph, controlled,
        target_event_id='EVENT-01', decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC')
    assert controlled_task['candidate_sources'] == []
    missing_case = dict(sources=[current, historical])
    _, missing_sources, missing = resolve_episode_source_dependencies(missing_case, [event], units)
    assert not missing['dependency_closure_complete']
    assert missing['not_found_in_delivery_source_ids'] == ['DOC-BUDGET']
    assert missing['discovery_budget_omission_receipts'][0]['source_state'] == 'FULL_ORIGINAL_NOT_FOUND_IN_DELIVERY'
    tasks, failures = __import__(
        'scripts.cohort_construction.p2_checkpoint_material_governance.assembly', fromlist=['prepare_candidate_materials']
    ).prepare_candidate_materials(local_graph, missing_sources,
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC')
    assert not tasks and len(failures) == 2
    first_failure = next(failure for failure in failures if failure['event_id'] == 'EVENT-01')
    assert first_failure['status'] == 'ASSEMBLY_HOLD'
    assert first_failure['reason_code'] == 'ASSEMBLY_DISCOVERY_OMITTED_SOURCE_MISSING'
    assert first_failure['clinical_release_permitted'] is False
    for bad, code in [(['bad-row'], 'ROW_MALFORMED'),
            ([dict(source_id='DOC-BUDGET', disposition='omitted_unknown')], 'UNSUPPORTED'),
            ([dict(disposition='omitted_budget')], 'SOURCE_ID_REQUIRED')]:
        invalid = deepcopy(unit)
        invalid['input_context_summary']['source_ledger'] = bad
        with pytest.raises(ValueError, match=code):
            resolve_episode_source_dependencies(dict(sources=[current, prior]), [event], [invalid])


def test_CE_REAL015_batch_execution_uses_recovered_graph_sources_and_materials(private_tmp):
    """Recovered sources reach both pipeline stages; preparation may locally HOLD."""
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import execute_batch
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import write
    graph, all_sources = fictional_graph()
    current = next(source for source in all_sources if source['source_id'] == 'DOC-01')
    prior = next(source for source in all_sources if source['source_id'] == 'DOC-BUDGET')
    event = graph['events'][0]
    event['actual_model_evidence'] = []
    event['evidence_context']['ledger'] = []
    event['evidence_context']['source_ledger'] = []
    unit = dict(unit_id=event['unit_id'], source_ids=['DOC-01'], events=[event],
        input_context_summary=dict(source_ledger=[dict(
            source_id='DOC-BUDGET', disposition='omitted_budget', full_source_not_truncated=True)]))
    graph['discovery_units'] = [unit]
    graph['episodes'][0]['event_ids'] = ['EVENT-01']
    graph['episodes'][0]['candidate_ids'] = ['EVENT-01']
    graph['episodes'][0]['checkpoint_ids'] = ['EVENT-01']
    graph['graph_sha256'] = __import__(
        'scripts.cohort_construction.p2_checkpoint_packet.compiler', fromlist=['canonical_sha256']
    ).canonical_sha256({key: value for key, value in graph.items() if key != 'graph_sha256'})
    recovered = [current, prior]
    preflight, authorization = _write_synthetic_batch_case(private_tmp, graph, recovered, [current])
    write(preflight / 'preflight/SCALE-01/graph-audit-sources.json', recovered)
    request_path = preflight / 'preflight/SCALE-01/execution-request.json'
    execution_request = read(request_path)
    execution_request['graph_audit_context']['processing_units'] = [dict(unit,
        source_ids=['DOC-01', 'DOC-BUDGET'])]
    request_path.unlink()
    write(request_path, execution_request)
    sent = []
    class Client:
        def ask(self, task, payload, schema, system_instructions):
            sent.append(dict(task=task, payload=payload))
            if task == 'checkpoint_candidate_reconstruction_independent_audit':
                original = next(source for source in payload['audit_scope']['complete_originals_supplied']
                    if source['source_id'] == 'DOC-BUDGET')
                assert original['text'] == prior['text']
                assert original['sha256'] == __import__('hashlib').sha256(
                    prior['text'].encode('utf-8')).hexdigest()
                return passing_graph_response(payload), _receipt('SYNTHETIC-GRAPH')
            if payload['target']['checkpoint_id'] == 'EVENT-01':
                candidate = next(source for source in payload['candidate_sources']
                    if source['source_id'] == 'DOC-BUDGET')
                assert candidate['text'] == prior['text']
                assert candidate['sha256'] == __import__('hashlib').sha256(
                    prior['text'].encode('utf-8')).hexdigest()
            return _pass_only(payload), _receipt('SYNTHETIC-MATERIAL-HOLD')
    result = execute_batch(preflight_output=preflight, authorization=authorization,
        base_config=CONFIG, model_client_factory=lambda cache, config: Client(),
        output=private_tmp / 'execution', private_root=private_tmp)
    run = read(private_tmp / 'execution/episodes/SCALE-01/EPISODE-01/run.json')
    assert run['input_contract']['graph_audit_sources'] == recovered
    assert not run.get('assembly_failures')
    graph_call = next(row for row in sent
        if row['task'] == 'checkpoint_candidate_reconstruction_independent_audit')
    assert graph_call['payload']['audit_scope']['source_coverage_complete']
    assert graph_call['payload']['audit_scope']['uncovered_supplied_source_ids'] == []
    assert len([row for row in sent if row['task'] == 'checkpoint_candidate_reconstruction_independent_audit']) == 1
    assert result['cases'][0]['state'] == 'HOLD'


def _write_synthetic_batch_case(private_tmp, graph, sources, current_sources):
    """The batch fixtures contain no patient data and never construct a remote Client."""
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import write
    preflight = private_tmp / 'preflight'
    case_dir = preflight / 'preflight' / 'SCALE-01'
    case_dir.mkdir(parents=True)
    contract = dict(batch_id='SYNTHETIC-BATCH', manifest_sha256='a' * 64)
    write(preflight / 'batch-contract.json', contract)
    write(preflight / 'batch-preflight.json', dict(cases=[dict(case_id='SCALE-01',
        status='READY_FOR_AUTHORIZED_DEVELOPMENT_EXECUTION')]))
    for name, value in [('graph.json', graph), ('sources.json', sources),
            ('current-unit-sources.json', current_sources)]:
        write(case_dir / name, value)
    write(case_dir / 'execution-request.json', dict(episode_id='EPISODE-01',
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC',
        graph_audit_context=dict(processing_units=[dict(unit_id='ALL',
            source_ids=[source['source_id'] for source in current_sources])])))
    authorization = private_tmp / 'authorization.json'
    write(authorization, dict(contract, semantic_model='kimi-k3', max_requests=60,
        max_http_attempts=600, remote_execution_authorized=True, doctor_packet_release=False,
        real_doctor_assignment_creation=False, clinical_release_permitted=False,
        execution_subset=['SCALE-01'], model_switch_is_explicit=True, no_case_replacement=True,
        max_rounds=3, automatic_graph_reconstruction_authorized=True, max_graph_revisions=1,
        graph_revision_human_final_review_required=True))
    return preflight, authorization


def _receipt(invocation_id):
    return dict(invocation_id=invocation_id, response_sha256='a' * 64,
        cached=False, status='SUCCESS', attempts=[dict(attempt=1)])


def test_CE_REAL012_first_batch_graph_pass_material_receives_historical_dependency(private_tmp):
    """Exercise the actual first-pass material adapter, not just preflight closure."""
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import execute_batch
    from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import prepare_candidate_materials
    graph, all_sources = fictional_graph()
    current_sources = [source for source in all_sources if source['source_id'] != 'DOC-BUDGET']
    graph['events'][0]['actual_model_evidence'].append(dict(source_id='DOC-BUDGET'))
    graph['graph_sha256'] = __import__(
        'scripts.cohort_construction.p2_checkpoint_packet.compiler', fromlist=['canonical_sha256']
    ).canonical_sha256({key: value for key, value in graph.items() if key != 'graph_sha256'})
    preflight, authorization = _write_synthetic_batch_case(
        private_tmp, graph, all_sources, current_sources)
    sent = []
    class Client:
        def ask(self, task, payload, schema, system_instructions):
            sent.append(dict(task=task, payload=payload))
            if task == 'checkpoint_candidate_reconstruction_independent_audit':
                return passing_graph_response(payload), _receipt('SYNTHETIC-GRAPH-PASS')
            target = payload['target']['checkpoint_id']
            calls = sum(1 for row in sent
                if row['task'] == 'checkpoint_material_full_source_and_doctor_view_review'
                and row['payload']['target']['checkpoint_id'] == target)
            if calls > 1:
                return _pass_only(payload), _receipt('SYNTHETIC-MATERIAL-PASS-' + target)
            tasks, failures = prepare_candidate_materials(graph, all_sources,
                decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC')
            assert not failures
            prepared = next(item for item in tasks
                if item['compiled']['target']['checkpoint_id'] == target)
            response = _passing_answer(prepared, prepared['manifest'], prepared['compiled'],
                prepared['candidate_sources'], prepared['excluded_sources'])
            response.update(payload['response_binding'])
            return response, _receipt('SYNTHETIC-MATERIAL-REVISE-' + target)
    result = execute_batch(preflight_output=preflight, authorization=authorization,
        base_config=CONFIG, model_client_factory=lambda cache, config: Client(),
        output=private_tmp / 'execution', private_root=private_tmp)
    material = [row['payload'] for row in sent if row['task'] != 'checkpoint_candidate_reconstruction_independent_audit']
    assert len(material) == 4 and all(row['task'] != 'checkpoint_candidate_graph_bounded_reconstruction'
        for row in sent)
    for request in material:
        supplied = {source['source_id'] for key in
            ('researcher_sources', 'candidate_sources', 'excluded_sources')
            for source in request[key]}
        assert supplied == {source['source_id'] for source in all_sources}
        assert 'DOC-BUDGET' in supplied
    run = read(private_tmp / 'execution/episodes/SCALE-01/EPISODE-01/run.json')
    assert run['input_contract']['graph_audit_sources'] == current_sources
    assert run['input_contract']['sources'] == all_sources
    assert run['assembly_failures'] == []
    assert next(stage for stage in run['stages']
        if stage['stage'] == 'MATERIAL_PREPARATION')['state'] == 'PASS'
    assert result['cases'][0]['state'] == 'DELIVERY_GATE_HOLD'


def test_CE_GRA010_successful_batch_reconstruction_binds_initial_parent_and_fresh_sources(private_tmp):
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import execute_batch, replay_batch
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import GRAPH_RECONSTRUCTION_SCHEMA
    from jsonschema import Draft202012Validator
    graph, all_sources = fictional_graph()
    current_sources = [source for source in all_sources if source['source_id'] != 'DOC-BUDGET']
    graph['events'][0]['actual_model_evidence'].append(dict(source_id='DOC-BUDGET'))
    graph['graph_sha256'] = __import__(
        'scripts.cohort_construction.p2_checkpoint_packet.compiler', fromlist=['canonical_sha256']
    ).canonical_sha256({key: value for key, value in graph.items() if key != 'graph_sha256'})
    preflight, authorization = _write_synthetic_batch_case(
        private_tmp, graph, all_sources, current_sources)
    graph_calls = 0
    class Client:
        def ask(self, task, payload, schema, system_instructions):
            nonlocal graph_calls
            if task == 'checkpoint_candidate_reconstruction_independent_audit':
                graph_calls += 1
                response = passing_graph_response(payload)
                if graph_calls == 1:
                    response.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
                        request_id='REV-SUCCESS', request_kind='EVENT_RECONSTRUCTION', reason='虚构重建',
                        affected_event_ids=['EVENT-01'], affected_episode_ids=['EPISODE-01'],
                        evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
                        required_reconstruction=True)])
                return response, _receipt('SYNTHETIC-GRAPH-' + str(graph_calls))
            if task == 'checkpoint_candidate_graph_bounded_reconstruction':
                new_graph = deepcopy(payload['input_graph'])
                new_graph['graph_version'] = 'SYNTHETIC-GRAPH-V2'
                response = dict(format='candidate-graph-reconstruction-response-0.2.0',
                    reconstruction_id='SYNTHETIC-SUCCESS', **payload['response_binding'],
                    status='RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT',
                    graph={key: deepcopy(new_graph[key]) for key in
                        ('events', 'relations', 'episodes', 'graph_version')}, rationale='虚构新图')
                Draft202012Validator(GRAPH_RECONSTRUCTION_SCHEMA).validate(response)
                return response, _receipt('SYNTHETIC-RECONSTRUCTION-SUCCESS')
            return _pass_only(payload), _receipt('SYNTHETIC-MATERIAL')
    result = execute_batch(preflight_output=preflight, authorization=authorization,
        base_config=CONFIG, model_client_factory=lambda cache, config: Client(),
        output=private_tmp / 'execution', private_root=private_tmp)
    assert graph_calls == 2
    case = result['cases'][0]
    assert case['reconstruction_chain_replays'][0]['state'] == 'RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT'
    # The default REVIEW spans cannot be bypassed by an immediate PASS; keep the
    # successful-generation assertion focused on its fresh graph audit and source scopes.
    assert case['state'] == 'HOLD' and case['reason_code'] == 'CHECKPOINT_MATERIAL_NOT_PASS'
    initial = read(private_tmp / 'execution/episodes/SCALE-01/EPISODE-01/run.json')
    fresh = read(private_tmp / 'execution/automatic-revisions/SCALE-01/generation-0001/run.json')
    assert fresh['graph_revision_cycle']['parent_run_sha256'] == initial['run_sha256']
    assert fresh['input_contract']['graph_audit_sources'] == current_sources
    assert fresh['input_contract']['sources'] == all_sources
    assert fresh['assembly_failures'] == []
    assert fresh['graph_audit_chain']['state'] == 'AUTO_GRAPH_AUDIT_PASS'
    assert replay_batch(private_tmp / 'execution')['replay_verified']


def test_CE_GRA010_provider_failure_chain_requires_parent_and_ledger_binding(private_tmp):
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import execute_batch, replay_batch
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import write
    graph, sources = fictional_graph()
    preflight = private_tmp / 'preflight'
    case_dir = preflight / 'preflight' / 'SCALE-01'
    case_dir.mkdir(parents=True)
    contract = dict(batch_id='SYNTHETIC-BATCH', manifest_sha256='a' * 64)
    write(preflight / 'batch-contract.json', contract)
    write(preflight / 'batch-preflight.json', dict(cases=[dict(case_id='SCALE-01',
        status='READY_FOR_AUTHORIZED_DEVELOPMENT_EXECUTION')]))
    for name, value in [('graph.json', graph), ('sources.json', sources),
            ('current-unit-sources.json', sources)]:
        write(case_dir / name, value)
    write(case_dir / 'execution-request.json', dict(episode_id='EPISODE-01',
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC',
        graph_audit_context=dict(processing_units=[dict(unit_id='ALL',
            source_ids=[source['source_id'] for source in sources])])))
    class Client:
        def ask(self, task, payload, schema, system_instructions):
            if task == 'checkpoint_candidate_reconstruction_independent_audit':
                response = passing_graph_response(payload)
                response.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
                    request_id='REV-PROVIDER', request_kind='EVENT_RECONSTRUCTION', reason='虚构重建',
                    affected_event_ids=['EVENT-01'], affected_episode_ids=['EPISODE-01'],
                    evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
                    required_reconstruction=True)])
                return response, dict(invocation_id='SYNTHETIC-GRAPH', response_sha256='a' * 64,
                    cached=False, status='SUCCESS', attempts=[dict(attempt=1)])
            from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import CHAIN_FORMAT
            error = RuntimeError('synthetic provider failure')
            error.receipt = dict(invocation_id='SYNTHETIC-PROVIDER', cached=False, status='FAILED',
                attempts=[dict(attempt=1, error='ConnectionError')], legacy_chain_format=CHAIN_FORMAT)
            raise error
    write(private_tmp / 'authorization.json', dict(contract, semantic_model='kimi-k3',
        max_requests=60, max_http_attempts=600, remote_execution_authorized=True,
        doctor_packet_release=False, real_doctor_assignment_creation=False,
        clinical_release_permitted=False, execution_subset=['SCALE-01'],
        model_switch_is_explicit=True, no_case_replacement=True, max_rounds=3,
        automatic_graph_reconstruction_authorized=True, max_graph_revisions=1,
        graph_revision_human_final_review_required=True))
    result = execute_batch(preflight_output=preflight,
        authorization=private_tmp / 'authorization.json', base_config=CONFIG,
        model_client_factory=lambda cache, config: Client(), output=private_tmp / 'execution',
        private_root=private_tmp)
    assert result['replay_verified']
    path = private_tmp / 'execution/automatic-revisions/SCALE-01/reconstruction-0001.json'
    chain = read(path)
    assert chain['failure']['stage'] == 'PROVIDER_REQUEST'
    assert chain['failure']['provider_invocation_id'] == 'SYNTHETIC-PROVIDER'
    assert 'response' not in chain and 'provider_receipt' not in chain
    from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
    tampered = deepcopy(chain)
    tampered['request']['input_graph']['graph_version'] = 'WRONG-PARENT-GRAPH'
    tampered['chain_sha256'] = canonical_sha256({key: value for key, value in tampered.items()
        if key != 'chain_sha256'})
    path.write_text(json.dumps(tampered, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='REQUEST_INVALID|PARENT_REQUEST_BINDING_MISMATCH'):
        replay_batch(private_tmp / 'execution')
    path.write_text(json.dumps(chain, ensure_ascii=False), encoding='utf-8')
    assert replay_batch(private_tmp / 'execution')['replay_verified']
    # The request field itself is not an authority to bypass external parent/ledger binding.
    summary_path = private_tmp / 'execution/automatic-revisions/SCALE-01/automatic-revision-summary.json'
    summary = read(summary_path)
    summary_path.unlink()
    (private_tmp / 'execution/automatic-revisions/SCALE-01/reconstruction-0001.json').unlink()
    summary['generations'] = [1]
    # Preserve the missing-chain counterexample under the current self-hashed
    # summary contract instead of accidentally testing its stale hash first.
    summary['reconstruction_chain_ids'] = [dict(generation=1,
        chain_sha256=chain['chain_sha256'])]
    summary['summary_sha256'] = canonical_sha256({key: value for key, value in summary.items()
        if key != 'summary_sha256'})
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import write_text
    write_text(summary_path, json.dumps(summary, ensure_ascii=False))
    with pytest.raises(ValueError, match='RECONSTRUCTION_(REQUIRED_CHAIN_MISSING|LEDGER_CHAIN_INVENTORY_MISMATCH)'):
        replay_batch(private_tmp / 'execution')
    # A real failed batch retains generations=[]: the ledger still requires its chain.
    original_summary = deepcopy(summary)
    summary.update(generations=[], reconstruction_chain_ids=[])
    summary['summary_sha256'] = canonical_sha256({key: value for key, value in summary.items()
        if key != 'summary_sha256'})
    summary_path.unlink()
    write_text(summary_path, json.dumps(summary, ensure_ascii=False))
    with pytest.raises(ValueError, match='RECONSTRUCTION_LEDGER_CHAIN_INVENTORY_MISMATCH'):
        replay_batch(private_tmp / 'execution')
    path.write_text(json.dumps(chain, ensure_ascii=False), encoding='utf-8')
    summary_path.unlink()
    write_text(summary_path, json.dumps(summary, ensure_ascii=False))
    assert replay_batch(private_tmp / 'execution')['replay_verified']
    # Ledger evidence requires the entire revision delivery, not only individual chains.
    import shutil
    shutil.rmtree(path.parent)
    with pytest.raises(ValueError, match='RECONSTRUCTION_LEDGER_REVISION_DELIVERY_MISSING'):
        replay_batch(private_tmp / 'execution')

def test_CE_GRA010_real_client_and_batch_ledger_bind_same_hash_domains(private_tmp):
    """Positive and negative controls through production Client and real BatchModelBudget."""
    import requests
    from unittest.mock import Mock, patch
    from scripts.cohort_construction.model_api import ModelAPISettings
    from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import _reconstruction_ledger
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
        build_graph_reconstruction_request, graph_reconstruction_client, run_graph_reconstruction,
        validate_graph_reconstruction_chain,
    )
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_graph_reconstruction import (
        fixtures, proposed,
    )
    for scenario in ('SCHEMA_REJECTION', 'CONNECTION_FAILURE'):
        _, audit_request, audit_response = fixtures()
        request = build_graph_reconstruction_request(audit_request, audit_response)
        response = proposed(request)
        response['graph'] = {'、': ['、']}
        session = Mock()
        if scenario == 'SCHEMA_REJECTION':
            envelope = dict(choices=[dict(finish_reason='stop', message=dict(
                content=json.dumps(response, ensure_ascii=False)))])
            session.post.return_value = Mock(**{'json.return_value': envelope})
        else:
            session.post.side_effect = requests.exceptions.ConnectionError('synthetic')
        settings = ModelAPISettings('synthetic', 'https://example.invalid/v1', 'synthetic')
        provider = transport.Client(private_tmp / scenario, dict(model_temperature=0,
            max_output_tokens=64, max_http_attempts_per_request=1), settings)
        ledger = BatchModelBudget(None, max_requests=1, max_http_attempts=1,
            max_http_attempts_per_request=1)
        context = dict(case_id='SYNTHETIC',
            graph_sha256=request['response_binding']['graph_sha256'])
        adapter = ledger.adapter('reconstruction', graph_reconstruction_client(provider), context)
        with patch.object(transport._thread, 'session', session, create=True):
            chain = run_graph_reconstruction(audit_request, audit_response, adapter)
        expected = _reconstruction_ledger(ledger.snapshot(), 'SYNTHETIC',
            request['response_binding']['graph_sha256'])
        assert chain['failure']['request_sha256'] == expected['transport_request_sha256']
        assert chain['failure']['request_sha256'] != expected['payload_sha256']
        assert expected['payload_sha256'] == __import__(
            'scripts.cohort_construction.p2_checkpoint_packet.compiler', fromlist=['canonical_sha256']
        ).canonical_sha256(chain['request'])
        assert validate_graph_reconstruction_chain(chain,
            expected_request=(audit_request, audit_response),
            expected_provider=expected)['replay_verified']
        for changed in (dict(expected, transport_request_sha256='0' * 64),
                        dict(expected, payload_sha256='0' * 64),
                        dict(expected, invocation_id='SYNTHETIC-OTHER-CALL')):
            with pytest.raises(ValueError, match='LEDGER_.*BINDING_MISMATCH'):
                validate_graph_reconstruction_chain(chain,
                    expected_request=(audit_request, audit_response), expected_provider=changed)
        if hasattr(transport._thread, 'session'):
            del transport._thread.session


def test_CE_GRA008_010_real_batch_uses_separate_reconstruction_and_replays_failed_chain(private_tmp):
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import execute_batch, replay_batch
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.prompts import GRAPH_AUDIT_INSTRUCTIONS
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import INSTRUCTIONS
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import write
    graph, all_sources = fictional_graph()
    current_sources = [source for source in all_sources if source['source_id'] != 'DOC-BUDGET']
    graph['events'][0]['actual_model_evidence'].append(dict(source_id='DOC-BUDGET'))
    graph['graph_sha256'] = __import__(
        'scripts.cohort_construction.p2_checkpoint_packet.compiler', fromlist=['canonical_sha256']
    ).canonical_sha256({key: value for key, value in graph.items() if key != 'graph_sha256'})
    preflight = private_tmp / 'preflight'
    case_dir = preflight / 'preflight' / 'SCALE-01'
    case_dir.mkdir(parents=True)
    contract = dict(batch_id='SYNTHETIC-BATCH', manifest_sha256='a' * 64)
    write(preflight / 'batch-contract.json', contract)
    write(preflight / 'batch-preflight.json', dict(cases=[dict(case_id='SCALE-01',
        status='READY_FOR_AUTHORIZED_DEVELOPMENT_EXECUTION')]))
    write(case_dir / 'graph.json', graph)
    write(case_dir / 'sources.json', all_sources)
    write(case_dir / 'current-unit-sources.json', current_sources)
    write(case_dir / 'execution-request.json', dict(episode_id='EPISODE-01',
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC',
        graph_audit_context=dict(processing_units=[dict(unit_id='ALL',
            source_ids=[source['source_id'] for source in current_sources])])))
    sent = []
    class Client:
        def ask(self, task, payload, schema, system_instructions):
            sent.append(dict(task=task, instructions=system_instructions, payload=payload))
            if task == 'checkpoint_candidate_reconstruction_independent_audit':
                response = passing_graph_response(payload)
                response.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
                    request_id='REV-REAL', request_kind='EVENT_RECONSTRUCTION', reason='虚构重建',
                    affected_event_ids=['EVENT-01'], affected_episode_ids=['EPISODE-01'],
                    evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
                    required_reconstruction=True)])
            else:
                response = dict(format='candidate-graph-reconstruction-response-0.2.0',
                    reconstruction_id='SYNTHETIC-FAILED', **payload['response_binding'],
                    status='RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT', graph={'、': ['、']},
                    rationale='虚构占位响应')
            return response, dict(invocation_id='SYNTHETIC-' + task,
                response_sha256='b' * 64, cached=False, status='SUCCESS',
                attempts=[dict(attempt=1)])
    authorization = private_tmp / 'authorization.json'
    write(authorization, dict(contract, semantic_model='kimi-k3', max_requests=60,
        max_http_attempts=600, remote_execution_authorized=True, doctor_packet_release=False,
        real_doctor_assignment_creation=False, clinical_release_permitted=False,
        execution_subset=['SCALE-01'], model_switch_is_explicit=True, no_case_replacement=True,
        max_rounds=3, automatic_graph_reconstruction_authorized=True, max_graph_revisions=1,
        graph_revision_human_final_review_required=True))
    result = execute_batch(preflight_output=preflight, authorization=authorization,
        base_config=CONFIG, model_client_factory=lambda cache, config: Client(),
        output=private_tmp / 'execution', private_root=private_tmp)
    assert [row['task'] for row in sent] == ['checkpoint_candidate_reconstruction_independent_audit',
        'checkpoint_candidate_graph_bounded_reconstruction']
    assert sent[0]['instructions'] == GRAPH_AUDIT_INSTRUCTIONS
    assert sent[1]['instructions'] == INSTRUCTIONS
    assert sent[0]['instructions'] != sent[1]['instructions']
    ledger = read(private_tmp / 'execution/model-call-ledger.json')
    reconstruction = [row for row in ledger['records'] if row.get('stage') == 'reconstruction']
    assert reconstruction and all(row['task'] == 'checkpoint_candidate_graph_bounded_reconstruction'
        and row['expected_gain'] == 'SEPARATE_RECONSTRUCTION_PROPOSAL_FOR_THIS_AUDITED_GRAPH'
        for row in reconstruction)
    case = result['cases'][0]
    assert case['reconstruction_chain_replays'][0]['state'] == 'RECONSTRUCTION_HOLD'
    assert case['reconstruction_chain_replays'][0]['replay_verified']
    chain = read(private_tmp / 'execution/automatic-revisions/SCALE-01/reconstruction-0001.json')
    assert chain['response']['graph'] == {'、': ['、']}
    assert chain['failure']['stage'] == 'RESPONSE_VALIDATION'
    assert replay_batch(private_tmp / 'execution')['replay_verified']
    run = read(private_tmp / 'execution/episodes/SCALE-01/EPISODE-01/run.json')
    assert run['input_contract']['graph_audit_sources'] == current_sources
    assert run['input_contract']['sources'] == all_sources
    assert len(sent[0]['payload']['audit_scope']['complete_originals_supplied']) == 3
    assert len(sent[1]['payload']['complete_originals_supplied']) == 3
    tampered = deepcopy(chain)
    tampered['request']['input_graph']['graph_version'] = 'TAMPERED-GRAPH'
    tampered['chain_sha256'] = __import__(
        'scripts.cohort_construction.p2_checkpoint_packet.compiler', fromlist=['canonical_sha256']
    ).canonical_sha256({key: value for key, value in tampered.items() if key != 'chain_sha256'})
    chain_path = private_tmp / 'execution/automatic-revisions/SCALE-01/reconstruction-0001.json'
    chain_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='REQUEST_INVALID|PARENT_REQUEST_BINDING_MISMATCH'):
        replay_batch(private_tmp / 'execution')
    chain_path.write_text(json.dumps(chain, ensure_ascii=False), encoding='utf-8')
    replay_batch(private_tmp / 'execution')
