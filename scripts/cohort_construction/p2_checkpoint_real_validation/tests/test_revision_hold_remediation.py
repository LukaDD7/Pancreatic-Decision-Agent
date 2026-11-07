"""CE-REM-ADV-024: synthetic provider/persistence hold delivery controls.

All provider I/O is fictional and local. These tests bind a settled call to the
initial run and the exact unresolved revision hold, not to clinical outcomes.
"""
from copy import deepcopy

import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import fictional_graph
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import read
from scripts.cohort_construction.p2_checkpoint_real_validation.batch import execute_batch, replay_batch
from scripts.cohort_construction.p2_checkpoint_real_validation.tests.test_real_validation import (
    CONFIG, _write_synthetic_batch_case, private_tmp,
)
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.tests.test_ready_pipeline import (
    CONFIG as PIPELINE_CONFIG, _adapters, _run, _semantic,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_graph_reconstruction import (
    proposed, provider_receipt,
)
from scripts.cohort_construction.p2_checkpoint_packet.tests.test_compiler import frozen_questionnaire
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response


def _hold_batch(private_tmp, monkeypatch, *, provider_failure):
    graph, sources = fictional_graph()
    preflight, authorization = _write_synthetic_batch_case(private_tmp, graph, sources, sources)
    calls = []

    class Client:
        def ask(self, task, payload, schema, system_instructions):
            calls.append(task)
            if task == 'checkpoint_candidate_reconstruction_independent_audit':
                response = passing_graph_response(payload)
                response.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
                    request_id='REV-HOLD', request_kind='EVENT_RECONSTRUCTION', reason='虚构未决重建',
                    affected_event_ids=['EVENT-01'], affected_episode_ids=['EPISODE-01'],
                    evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
                    required_reconstruction=True)])
                return response, dict(invocation_id='SYNTHETIC-GRAPH', response_sha256='a' * 64,
                    cached=False, status='SUCCESS', attempts=[dict(attempt=1)])
            if provider_failure:
                error = RuntimeError('synthetic provider state unknown')
                error.receipt = dict(invocation_id='SYNTHETIC-RECONSTRUCTION', cached=False, status='FAILED')
                raise error
            return dict(format='candidate-graph-reconstruction-response-0.2.0',
                reconstruction_id='SYNTHETIC-HOLD', **payload['response_binding'],
                status='RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT',
                graph=dict(events=deepcopy(payload['input_graph']['events']),
                    relations=deepcopy(payload['input_graph']['relations']),
                    episodes=deepcopy(payload['input_graph']['episodes']),
                    graph_version='SYNTHETIC-RECONSTRUCTED-V2'), rationale='虚构重建后落盘失败'), dict(
                    invocation_id='SYNTHETIC-RECONSTRUCTION', response_sha256='a' * 64,
                    cached=False, status='SUCCESS', attempts=[dict(attempt=1)])

    if not provider_failure:
        import scripts.cohort_construction.p2_checkpoint_ready_pipeline.automatic_revision as revision

        def fail_chain_archive(path, value):
            if str(path).endswith('reconstruction-0001.json'):
                raise OSError('synthetic post-response archive failure')
            from scripts.cohort_construction.p2_checkpoint_material_governance.run import _json_exclusive
            _json_exclusive(path, value)
        monkeypatch.setattr(revision, '_json_exclusive', fail_chain_archive)
    output = private_tmp / 'execution'
    result = execute_batch(preflight_output=preflight, authorization=authorization,
        base_config=CONFIG, model_client_factory=lambda cache, config: Client(),
        output=output, private_root=private_tmp)
    return output, result, calls


@pytest.mark.parametrize(('provider_failure', 'reason'), [
    (True, 'RECONSTRUCTION_PROVIDER_USAGE_UNRESOLVED'),
    (False, 'RECONSTRUCTION_PERSISTENCE_FAILED'),
])
def test_CEREM_ADV024_settled_reconstruction_hold_is_replayable_not_unattempted(
        private_tmp, monkeypatch, provider_failure, reason):
    output, result, calls = _hold_batch(private_tmp, monkeypatch, provider_failure=provider_failure)
    assert calls == ['checkpoint_candidate_reconstruction_independent_audit',
        'checkpoint_candidate_graph_bounded_reconstruction']
    case = result['cases'][0]
    assert case['state'] == 'HOLD'
    assert case['reason_code'] == reason
    assert case['automatic_revision_stop_reason'] == reason
    assert case['run_sha256'] == read(output / 'episodes/SCALE-01/EPISODE-01/run.json')['run_sha256']
    assert case['replay_verified'] is True
    summary = read(output / 'automatic-revisions/SCALE-01/automatic-revision-summary.json')
    assert summary['stop_reason'] == reason and summary['generations'] == []
    assert summary['summary_sha256'] == canonical_sha256({key: value for key, value in summary.items()
        if key != 'summary_sha256'})
    hold = summary['reconstruction_accounting_hold']
    assert hold['reconstruction_chain']['chain_sha256'] == hold['chain_sha256']
    if provider_failure:
        chain = read(output / 'automatic-revisions/SCALE-01/reconstruction-0001.json')
        assert chain == hold['reconstruction_chain']
        assert chain['failure']['stage'] == 'PROVIDER_REQUEST'
        assert hold['state'] == 'IN_FLIGHT_ACCOUNTING_UNRESOLVED'
    else:
        assert not (output / 'automatic-revisions/SCALE-01/reconstruction-0001.json').exists()
        assert hold['persistence_failure'] == 'OSError'
        assert hold['state'] == 'RECONSTRUCTION_PERSISTENCE_UNRESOLVED'
        assert hold['reconstruction_chain']['state'] == 'RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT'
    assert replay_batch(output, persist=False)['replay_verified']
    # An edited hold can be self-rehashed, but cannot become independent
    # provider evidence. Persisted replay keeps its chain unavailable.
    from scripts.cohort_construction.p2_checkpoint_real_validation.batch import _journal_write
    summary['reconstruction_accounting_hold']['automatic_retry_performed'] = True
    summary['summary_sha256'] = canonical_sha256({key: value for key, value in summary.items()
        if key != 'summary_sha256'})
    _journal_write(output / 'automatic-revisions/SCALE-01/automatic-revision-summary.json', summary)
    with pytest.raises(ValueError, match='REVISION_ACCOUNTING_HOLD_INVALID'):
        replay_batch(output, persist=False)


def _revising_response(payload):
    response = passing_graph_response(payload)
    response.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
        request_id='REV-MULTI', request_kind='EVENT_RECONSTRUCTION', reason='虚构多代重建',
        affected_event_ids=['EVENT-01'], affected_episode_ids=['EPISODE-01'],
        evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
        required_reconstruction=True)])
    return response


def _successful_reconstruction(task, payload, schema):
    from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
    response = dict(format='candidate-graph-reconstruction-response-0.2.0',
        reconstruction_id='SYNTHETIC-MULTI', **payload['response_binding'],
        status='RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT',
        graph={**{key: deepcopy(payload['input_graph'][key]) for key in
            ('events', 'relations', 'episodes')},
            'graph_version': payload['input_graph']['graph_version'] + '-RECONSTRUCTED'},
        rationale='虚构多代重建')
    return response, dict(invocation_id='SYNTHETIC-MULTI', cached=False, status='SUCCESS',
        response_sha256=digest(dict(synthetic_provider_response=response)), attempts=[dict(attempt=1)])


def test_CEREM_ADV024_second_generation_retains_separate_adapter_and_binds_hold(
        private_tmp):
    graph, sources = fictional_graph()
    parent = _run(private_tmp / 'parent', graph, sources, adapters=_adapters(
        graph, sources, [], graph_answer=_revising_response))
    graph_answers = iter([_revising_response, _revising_response])
    calls = []
    adapters = _adapters(graph, sources, calls, graph_answer=lambda payload: next(graph_answers)(payload))
    adapters['reconstruction'] = _successful_reconstruction
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.automatic_revision import (
        _read_revision_summary, run_bounded_graph_revision_pipeline,
    )
    result = run_bounded_graph_revision_pipeline(parent_run=parent,
        output=private_tmp / 'multi', graph=graph, sources=sources, episode_id='EPISODE-01',
        semantic_audits='LAZY', questionnaire=frozen_questionnaire(),
        graph_audit_context=dict(processing_units=[dict(unit_id='ALL',
            source_ids=[source['source_id'] for source in sources])]),
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        config=PIPELINE_CONFIG, adapters=adapters, execute=True, max_graph_revisions=2,
        semantic_audit_factory=_semantic)
    assert result['stop_reason'] == 'GRAPH_REVISION_BUDGET_EXHAUSTED'
    assert [item['generation'] for item in result['generations']] == [1, 2]
    summary = _read_revision_summary(private_tmp / 'multi/automatic-revision-summary.json')
    assert [row['generation'] for row in summary['reconstruction_chain_ids']] == [1, 2]


def test_CEREM_ADV024_second_generation_hold_preserves_completed_generation(
        private_tmp):
    graph, sources = fictional_graph()
    parent = _run(private_tmp / 'parent', graph, sources, adapters=_adapters(
        graph, sources, [], graph_answer=_revising_response))
    graph_answers = iter([_revising_response, _revising_response])
    reconstructions = iter([_successful_reconstruction, RuntimeError('synthetic provider unknown')])
    def reconstruction(task, payload, schema):
        outcome = next(reconstructions)
        if isinstance(outcome, Exception):
            outcome.receipt = dict(invocation_id='SYNTHETIC-SECOND-HOLD',
                cached=False, status='FAILED')
            raise outcome
        return outcome(task, payload, schema)
    calls = []
    adapters = _adapters(graph, sources, calls,
        graph_answer=lambda payload: next(graph_answers)(payload))
    adapters['reconstruction'] = reconstruction
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.automatic_revision import (
        _read_revision_summary, run_bounded_graph_revision_pipeline,
    )
    result = run_bounded_graph_revision_pipeline(parent_run=parent,
        output=private_tmp / 'multi-hold', graph=graph, sources=sources, episode_id='EPISODE-01',
        semantic_audits='LAZY', questionnaire=frozen_questionnaire(),
        graph_audit_context=dict(processing_units=[dict(unit_id='ALL',
            source_ids=[source['source_id'] for source in sources])]),
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        config=PIPELINE_CONFIG, adapters=adapters,
        execute=True, max_graph_revisions=2, semantic_audit_factory=_semantic)
    assert result['stop_reason'] == 'RECONSTRUCTION_PROVIDER_USAGE_UNRESOLVED'
    summary = _read_revision_summary(private_tmp / 'multi-hold/automatic-revision-summary.json')
    assert summary['generations'] == [1]
    assert len(summary['reconstruction_chain_ids']) == 1
    hold = summary['reconstruction_accounting_hold']
    assert hold['state'] == 'IN_FLIGHT_ACCOUNTING_UNRESOLVED'
    assert hold['reconstruction_chain']['failure']['stage'] == 'PROVIDER_REQUEST'


def test_CEREM_ADV024_fresh_pipeline_failure_retains_parent_and_archived_chain(
        private_tmp, monkeypatch):
    graph, sources = fictional_graph()
    parent = _run(private_tmp / 'parent', graph, sources, adapters=_adapters(
        graph, sources, [], graph_answer=_revising_response))
    import scripts.cohort_construction.p2_checkpoint_ready_pipeline.automatic_revision as revision
    monkeypatch.setattr(revision, 'run_episode_pipeline',
        lambda **kwargs: (_ for _ in ()).throw(OSError('synthetic fresh archive failure')))
    result = revision.run_bounded_graph_revision_pipeline(parent_run=parent,
        output=private_tmp / 'fresh-hold', graph=graph, sources=sources, episode_id='EPISODE-01',
        semantic_audits='LAZY', questionnaire=frozen_questionnaire(),
        graph_audit_context=dict(processing_units=[dict(unit_id='ALL',
            source_ids=[source['source_id'] for source in sources])]),
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        config=PIPELINE_CONFIG, adapters=dict(graph=lambda *args: pytest.fail(
            'FAILED_FRESH_PIPELINE_MUST_NOT_RETRY'), reconstruction=_successful_reconstruction),
        execute=True, semantic_audit_factory=_semantic)
    assert result['final_run']['run_sha256'] == parent['run_sha256']
    assert result['stop_reason'] == 'FRESH_RECONSTRUCTED_PIPELINE_PERSISTENCE_FAILED'
    summary = revision._read_revision_summary(
        private_tmp / 'fresh-hold/automatic-revision-summary.json')
    assert summary['reconstruction_accounting_hold']['failure_scope'] == 'FRESH_PIPELINE'
    assert (private_tmp / 'fresh-hold/reconstruction-0001.json').is_file()


def _multigeneration_batch(private_tmp, monkeypatch, *, second_outcome):
    """Exercise the real batch adapter/replay, with only fictional local I/O."""
    graph, sources = fictional_graph()
    preflight, authorization = _write_synthetic_batch_case(private_tmp, graph, sources, sources)
    authorization_value = read(authorization)
    authorization_value['max_graph_revisions'] = 2
    authorization_value['max_rounds'] = 3
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import write_text
    import json
    authorization.unlink()
    write_text(authorization, json.dumps(authorization_value, ensure_ascii=False))
    import scripts.cohort_construction.p2_checkpoint_real_validation.batch as batch
    monkeypatch.setattr(batch, 'GRAPH_RECONSTRUCTION_GENERATIONS', 2)
    graph_calls, reconstruction_calls = [], []
    class Client:
        def ask(self, task, payload, schema, system_instructions):
            if task == 'checkpoint_candidate_reconstruction_independent_audit':
                graph_calls.append(payload)
                response = _revising_response(payload)
                return response, dict(provider_receipt(),
                    invocation_id='SYNTHETIC-BATCH-GRAPH-' + str(len(graph_calls)))
            if task == 'checkpoint_candidate_graph_bounded_reconstruction':
                reconstruction_calls.append(payload)
                if len(reconstruction_calls) == 1 or second_outcome == 'exhausted':
                    response = proposed(payload)
                    response['graph']['graph_version'] = \
                        payload['input_graph']['graph_version'] + '-RECONSTRUCTED'
                elif second_outcome == 'provider':
                    error = RuntimeError('synthetic second transport unknown')
                    error.receipt = dict(invocation_id='SYNTHETIC-BATCH-RECONSTRUCTION-2',
                        cached=False, status='FAILED', attempts=[dict(attempt=1)])
                    raise error
                elif second_outcome == 'response':
                    response = proposed(payload)
                    response['graph'] = None
                    response['status'] = 'RECONSTRUCTION_HOLD'
                return response, dict(provider_receipt(),
                    invocation_id='SYNTHETIC-BATCH-RECONSTRUCTION-' + str(len(reconstruction_calls)))
            raise AssertionError('A revising fresh graph cannot reach material/delivery: ' + task)
    if second_outcome == 'fresh_failure':
        import scripts.cohort_construction.p2_checkpoint_ready_pipeline.automatic_revision as revision
        monkeypatch.setattr(revision, 'run_episode_pipeline',
            lambda **kwargs: (_ for _ in ()).throw(OSError('synthetic fresh archive failure')))
    output = private_tmp / 'execution-multi'
    result = execute_batch(preflight_output=preflight, authorization=authorization,
        base_config=CONFIG, model_client_factory=lambda cache, config: Client(),
        output=output, private_root=private_tmp)
    return output, result, graph_calls, reconstruction_calls


@pytest.mark.parametrize(('second_outcome', 'reason', 'hold_state', 'state'), [
    ('provider', 'RECONSTRUCTION_PROVIDER_USAGE_UNRESOLVED',
     'IN_FLIGHT_ACCOUNTING_UNRESOLVED', 'HOLD'),
    ('response', 'AUTOMATIC_RECONSTRUCTION_FAILED', 'RECONSTRUCTION_NOT_ACCEPTED',
     'HUMAN_REVIEW_REQUIRED'),
])
def test_CEREM_ADV024_second_generation_batch_hold_replays_parent_and_chain(
        private_tmp, monkeypatch, second_outcome, reason, hold_state, state):
    output, result, graph_calls, reconstruction_calls = _multigeneration_batch(
        private_tmp, monkeypatch, second_outcome=second_outcome)
    assert len(graph_calls) == 2 and len(reconstruction_calls) == 2
    assert 'reconstruction_lineage' not in graph_calls[0]
    assert reconstruction_calls[1]['reconstruction_lineage'] == \
        graph_calls[1]['reconstruction_lineage']
    case = result['cases'][0]
    assert case['state'] == state and case['reason_code'] == reason
    generation = output / 'automatic-revisions/SCALE-01/generation-0001'
    assert case['run_sha256'] == read(generation / 'run.json')['run_sha256']
    replay = replay_batch(output, persist=False)
    assert replay['replay_verified']
    chain_rows = replay['cases'][0]['reconstruction_chain_replays']
    assert [(row['generation'], row['independently_archived']) for row in chain_rows] == [
        (1, True), (2, True)]
    assert replay['cases'][0]['reconstruction_accounting_hold_state'] == hold_state


def test_CEREM_ADV024_multigeneration_budget_exhaustion_replays_both_generations(
        private_tmp, monkeypatch):
    output, result, graph_calls, reconstruction_calls = _multigeneration_batch(
        private_tmp, monkeypatch, second_outcome='exhausted')
    assert len(graph_calls) == 3 and len(reconstruction_calls) == 2
    case = result['cases'][0]
    assert case['state'] == 'HUMAN_REVIEW_REQUIRED'
    assert case['reason_code'] == 'GRAPH_REVISION_BUDGET_EXHAUSTED'
    assert case['run_sha256'] == read(
        output / 'automatic-revisions/SCALE-01/generation-0002/run.json')['run_sha256']
    replay = replay_batch(output, persist=False)
    assert replay['replay_verified']
    assert [row['generation'] for row in
        replay['cases'][0]['reconstruction_chain_replays']] == [1, 2]
    assert len(read(output / 'model-call-ledger.json')['records']) == 10


def test_CEREM_ADV024_fresh_pipeline_batch_failure_binds_latest_replayable_parent(
        private_tmp, monkeypatch):
    output, result, graph_calls, reconstruction_calls = _multigeneration_batch(
        private_tmp, monkeypatch, second_outcome='fresh_failure')
    assert len(graph_calls) == 1 and len(reconstruction_calls) == 1
    case = result['cases'][0]
    assert case['state'] == 'HOLD'
    assert case['reason_code'] == 'FRESH_RECONSTRUCTED_PIPELINE_PERSISTENCE_FAILED'
    assert case['run_sha256'] == read(output / 'episodes/SCALE-01/EPISODE-01/run.json')['run_sha256']
    replay = replay_batch(output, persist=False)
    assert replay['replay_verified']
    row = replay['cases'][0]['reconstruction_chain_replays'][0]
    assert row['independently_archived']
    assert replay['cases'][0]['reconstruction_accounting_hold_state'] == \
        'FRESH_PIPELINE_PERSISTENCE_UNRESOLVED'
