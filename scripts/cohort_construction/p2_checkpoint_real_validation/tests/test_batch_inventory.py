"""CE-REAL-013: independent case inventory, partial replay and recovery controls."""
from copy import deepcopy

import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import _pass_only, fictional_graph
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import read
from scripts.cohort_construction.p2_checkpoint_real_validation.batch import (
    execute_batch, execution_report, replay_batch,
)
from scripts.cohort_construction.p2_checkpoint_real_validation.budget import _journal_write
from scripts.cohort_construction.p2_checkpoint_real_validation.tests.test_real_validation import CONFIG, _receipt, _write_synthetic_batch_case
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response


def synthetic_batch(root, *, extra_cases=False, initial_budget=None):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    graph, sources = fictional_graph()
    preflight, authorization = _write_synthetic_batch_case(root, graph, sources, sources)
    if extra_cases:
        value = read(preflight / 'batch-preflight.json')
        value['cases'].extend(dict(case_id=sid, status='PREFLIGHT_HOLD', reason_codes=['SYNTHETIC'])
            for sid in ('SCALE-02', 'SCALE-03'))
        _journal_write(preflight / 'batch-preflight.json', value)

    class Client:
        def ask(self, task, payload, schema, system_instructions):
            response = passing_graph_response(payload) if task.endswith('independent_audit') else _pass_only(payload)
            return response, _receipt('SYNTHETIC-' + task)

    output = root / 'execution'
    result = execute_batch(preflight_output=preflight, authorization=authorization,
        base_config=CONFIG, model_client_factory=lambda *args: Client(), output=output,
        initial_budget_state=initial_budget, private_root=root)
    return output, result


def test_CE_REAL022_report_requires_independently_verified_replay(tmp_path):
    output, baseline = synthetic_batch(tmp_path / 'report')
    receipt_path = output / 'batch-replay-receipt.json'
    receipt_path.unlink()
    report = execution_report(output, replay=dict(baseline,
        implementation_snapshot_verified=False))
    assert report['model_usage'] == baseline['model_usage']
    assert report['all_saved_episode_chains_replay_verified'] is True
    assert report['saved_episode_chain_count'] == 1
    assert not report['clinical_release_permitted']
    for changed in (dict(baseline, batch_id='OTHER'), dict(baseline, cases=[]),
            dict(baseline, fixed_case_count=99),
            dict(baseline, replay_verified=False, model_usage={'requests': 999}),
            dict(baseline, cases=[dict(baseline['cases'][0], replay_verified=False)])):
        with pytest.raises(ValueError, match='REPORT_REPLAY'):
            execution_report(output, replay=changed)
    _journal_write(receipt_path, baseline)
    with pytest.raises(ValueError, match='REPORT_REPLAY_RECEIPT_MISMATCH'):
        execution_report(output, replay=dict(baseline, model_usage={}))


@pytest.fixture
def completed(tmp_path):
    return synthetic_batch(tmp_path / 'batch')


def test_CE_REAL013_first_replay_rejects_missing_duplicate_or_unattempted_called_cases(completed):
    output, baseline = completed
    assert baseline['replay_verified'] and baseline['verified_case_count'] == 1
    path = output / 'batch-progress.json'
    progress = read(path)
    # Prove the inventory check itself works without an old receipt as a guard.
    (output / 'batch-replay-receipt.json').unlink()
    rows = progress['cases']
    for changed_rows, error in (([], 'CASE_INVENTORY_MISMATCH'),
            ([dict(case_id='SCALE-02', state='NOT_ATTEMPTED_OUTSIDE_AUTHORIZED_SUBSET')], 'CASE_INVENTORY_MISMATCH'),
            (rows + rows, 'CASE_IDENTITY_INVALID'),
            ([dict(rows[0], state='NOT_ATTEMPTED_BATCH_BUDGET_EXHAUSTED')], 'CALLED_CASE_MARKED_UNATTEMPTED'),
            ([dict(rows[0], run_sha256=None)], 'CALLED_CASE_RUN_EVIDENCE_REQUIRED')):
        _journal_write(path, dict(progress, cases=changed_rows))
        with pytest.raises(ValueError, match=error):
            replay_batch(output)
    _journal_write(path, progress)
    assert replay_batch(output) == baseline


def test_CE_REAL013_subset_preserves_final_skipped_rows_and_clear_denominators(tmp_path):
    output, result = synthetic_batch(tmp_path / 'subset', extra_cases=True)
    assert result['fixed_case_count'] == 3 and result['verified_case_count'] == 1
    assert result['case_inventory']['frozen_case_count'] == 3
    assert result['case_inventory']['authorized_case_count'] == 1
    assert result['case_inventory']['attempted_case_count'] == 1
    assert result['case_inventory']['outside_authorized_subset_count'] == 2
    assert len(read(output / 'batch-progress.json')['cases']) == 3
    assert len(list((output / 'batch-progress-events').glob('*.json'))) == 3
    progress = read(output / 'batch-progress.json')
    _journal_write(output / 'batch-progress.json', dict(progress, cases=progress['cases'][:1]))
    with pytest.raises(ValueError, match='CASE_INVENTORY_MISMATCH'):
        replay_batch(output, persist=False)


def test_CE_REAL013_unsettled_calls_and_missing_inventory_cannot_bypass_replay(completed):
    output, _ = completed
    path = output / 'model-call-ledger.json'
    original = read(path)
    changed = deepcopy(original)
    changed['records'].pop()  # Keep the last pre-I/O plan; drop its settlement.
    _journal_write(path, changed)
    with pytest.raises(ValueError, match='LEDGER_UNSETTLED_CALLS'):
        replay_batch(output, persist=False)
    _journal_write(path, original)
    inventory = output / 'batch-execution-inventory.json'
    inventory.rename(output / 'preserved-inventory.json')
    with pytest.raises(ValueError, match='EXECUTION_INVENTORY_REQUIRED'):
        replay_batch(output, persist=False)
    (output / 'preserved-inventory.json').rename(inventory)
    assert replay_batch(output)['replay_verified']


def test_CE_REAL013_versioned_settled_budget_recovery_and_legacy_coverage(tmp_path):
    parent, _ = synthetic_batch(tmp_path / 'parent')
    inherited = read(parent / 'model-call-ledger.json')
    child, result = synthetic_batch(tmp_path / 'child', initial_budget=inherited)
    assert result['replay_verified']
    assert result['model_usage']['requests'] > inherited['requests']
    frozen = read(child / 'batch-execution-inventory.json')
    assert frozen['inherited_record_count'] == len(inherited['records'])
    assert read(parent / 'model-call-ledger.json') == inherited
    partial = deepcopy(inherited)
    partial['records'].pop()
    with pytest.raises(ValueError, match='LEDGER_UNSETTLED_CALLS'):
        synthetic_batch(tmp_path / 'unsettled-child', initial_budget=partial)
    # A legacy receipt cannot prove a frozen denominator, but must still cover
    # every authorized and called case; it does not skip orphan calls.
    (child / 'batch-execution-inventory.json').rename(child / 'preserved-inventory.json')
    authorization = read(child / 'effective-batch-authorization.json')
    authorization.pop('execution_inventory_version')
    _journal_write(child / 'effective-batch-authorization.json', authorization)
    legacy = replay_batch(child, persist=False)
    assert legacy['case_inventory']['frozen_case_count'] is None
    assert legacy['replay_verified']
    progress = read(child / 'batch-progress.json')
    _journal_write(child / 'batch-progress.json', dict(progress, cases=[]))
    with pytest.raises(ValueError, match='AUTHORIZED_CASE_MISSING'):
        replay_batch(child, persist=False)


def test_CE_REAL013_existing_receipt_and_authorization_binding_remain_protected(completed):
    output, baseline = completed
    assert replay_batch(output) == baseline
    auth_path = output / 'effective-batch-authorization.json'
    auth = read(auth_path)
    _journal_write(auth_path, dict(auth, execution_subset=['SCALE-02']))
    with pytest.raises(ValueError, match='INVENTORY_BINDING_INVALID'):
        replay_batch(output, persist=False)
    _journal_write(auth_path, auth)
    receipt_path = output / 'batch-replay-receipt.json'
    _journal_write(receipt_path, dict(baseline, verified_case_count=999))
    with pytest.raises(ValueError, match='RECEIPT_CHANGED_REQUIRES_NEW_OUTPUT'):
        replay_batch(output)


def test_CE_GRA011_production_client_batch_reconstruction_reaches_material_review(tmp_path, monkeypatch):
    """Use actual transport Schema and batch orchestration; mock HTTP only."""
    import json
    from unittest.mock import Mock
    from scripts.cohort_construction.model_api import ModelAPISettings
    from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import GRAPH_RECONSTRUCTION_SCHEMA
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_graph_reconstruction import proposed
    root = tmp_path / 'production-client'
    root.mkdir(mode=0o700)
    graph, sources = fictional_graph()
    preflight, authorization = _write_synthetic_batch_case(root, graph, sources, sources)
    graph_calls, material_calls = [], []

    def post(*args, **kwargs):
        payload = json.loads(kwargs['json']['messages'][1]['content'])
        request = payload['input']
        if payload['task'] == 'checkpoint_candidate_reconstruction_independent_audit':
            graph_calls.append(request)
            response = passing_graph_response(request)
            if len(graph_calls) == 1:
                response.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
                    request_id='SYNTHETIC-CONTRACT', request_kind='EVENT_RECONSTRUCTION',
                    reason='虚构事件接口修订', affected_event_ids=['EVENT-01'],
                    affected_episode_ids=['EPISODE-01'], required_reconstruction=True,
                    evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')])])
        elif payload['task'] == 'checkpoint_candidate_graph_bounded_reconstruction':
            response = proposed(request)
            required = GRAPH_RECONSTRUCTION_SCHEMA['properties']['graph']['properties']['events']['items']['required']
            for event in response['graph']['events']:
                for key in list(event):
                    if key not in required:
                        del event[key]
                event.update(choice_time=None, time_basis='unknown', time_validation='UNRESOLVED')
        else:
            assert payload['task'] == 'checkpoint_material_full_source_and_doctor_view_review'
            material_calls.append(request)
            response = _pass_only(request)
        http = Mock()
        http.json.return_value = dict(choices=[dict(finish_reason='stop',
            message=dict(content=json.dumps(response, ensure_ascii=False)))])
        return http

    session = Mock()
    session.post.side_effect = post
    monkeypatch.setattr(transport._thread, 'session', session, raising=False)
    monkeypatch.setattr(transport.time, 'sleep', lambda *args: None)
    monkeypatch.setattr('requests.sessions.Session.send', lambda *args, **kwargs: pytest.fail('REAL_HTTP_FORBIDDEN'))
    settings = ModelAPISettings('synthetic', 'https://example.invalid/v1', 'synthetic')
    output = root / 'execution'
    result = execute_batch(preflight_output=preflight, authorization=authorization,
        base_config=dict(CONFIG, model_temperature=0, max_output_tokens=16000),
        model_client_factory=lambda cache, config: transport.Client(cache, config, settings),
        output=output, private_root=root)
    assert len(graph_calls) == 2 and len(material_calls) == 2
    fresh = read(output / 'automatic-revisions/SCALE-01/generation-0001/run.json')
    assert not fresh['assembly_failures']
    assert fresh['graph_audit_chain']['state'] == 'AUTO_GRAPH_AUDIT_PASS'
    assert set(fresh['material_chains']) == {'EVENT-01', 'EVENT-02'}
    assert all(event['choice_time'] is None for event in fresh['input_contract']['graph']['events'])
    assert result['replay_verified'] and result['verified_case_count'] == 1
    assert not result['clinical_release_permitted']
