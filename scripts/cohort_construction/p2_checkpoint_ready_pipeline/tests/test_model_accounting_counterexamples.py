"""CE-REM-ADV-021/022/023: synthetic model-accounting failure controls.

No provider or real clinical record is used. These tests distinguish logical
attempt accounting from remote-provider authentication and clinical outcomes.
"""
from copy import deepcopy
import json

import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.orchestrate import SharedModelBudget
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
    run_graph_reconstruction, validate_graph_reconstruction_chain,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_graph_reconstruction import (
    fixtures, proposed, provider_receipt,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import _passing_answer
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.model_accounting import (
    chain_usage, validate_graph_reconstruction_usage, zero_usage,
)

from scripts.cohort_construction.p2_checkpoint_ready_pipeline.tests.test_ready_pipeline import (
    CONFIG, _adapters, _run, _semantic, fixtures as pipeline_fixtures, private_tmp,
)
from scripts.cohort_construction.p2_checkpoint_packet.tests.test_compiler import frozen_questionnaire


def test_CEREM_ADV021_contradictory_cached_invocation_never_spends_zero_budget():
    budget = SharedModelBudget(1)
    # Bind a minimal synthetic adapter exactly as PipelineBudget does.
    budget.adapter = lambda *args: ({'synthetic': True}, dict(
        cached=True, provider_invocation_occurred=True))
    with pytest.raises(ValueError, match='RECEIPT_INVOCATION_OUTCOME_CONTRADICTORY'):
        budget.ask('synthetic', {}, {})
    assert budget.snapshot()['actual_uncached_model_invocations'] == 0
    assert budget.cache_hits == 0
    usage = zero_usage(2)
    for receipt in (dict(cached=True, provider_invocation_occurred=True),
                    dict(cached=False, provider_invocation_occurred=False)):
        with pytest.raises(ValueError, match='CHAIN_ATTEMPT_OUTCOME_CONFLICT'):
            chain_usage(dict(rounds=[dict(receipt=receipt)]), 'graph', usage)


def test_CEREM_ADV021_local_denial_cache_hit_and_cached_failure_are_not_uncached():
    budget = SharedModelBudget(1)
    budget.adapter = lambda *args: ({'synthetic': True}, dict(
        cached=True, provider_invocation_occurred=False))
    assert budget.ask('synthetic', {}, {})[0]['synthetic']
    budget.adapter = lambda *args: ({'synthetic': True}, dict(cached=False))
    assert budget.ask('synthetic', {}, {})[0]['synthetic']
    with pytest.raises(RuntimeError, match='BUDGET_REACHED') as denied:
        budget.ask('synthetic', {}, {})
    assert denied.value.receipt['provider_invocation_occurred'] is False
    assert budget.snapshot() == dict(uncached_succeeded=1, uncached_failed=0,
        cache_hits=1, denied=1, actual_uncached_model_invocations=1,
        max_model_invocations=1)
    usage = zero_usage(3)
    chain_usage(dict(rounds=[dict(receipt=dict(cached=True,
        provider_invocation_occurred=False))], failure=dict(
            stage='PROVIDER_REQUEST', receipt=denied.value.receipt)), 'material', usage)
    assert usage['stage_usage']['material'] == dict(ask_for_attempts=2,
        uncached_succeeded=0, uncached_failed=0, cache_hits=1, denied=1)


def test_CEREM_ADV024_batch_local_denial_stays_denied_and_does_not_call_adapter():
    class BatchBudgetExhausted(RuntimeError):
        def __init__(self):
            self.receipt = dict(cached=False, status='LOCAL_BATCH_BUDGET_DENIED',
                provider_invocation_occurred=False)
            super().__init__('synthetic batch ceiling reached')

    calls = []
    def adapter(*args):
        calls.append(args)
        raise BatchBudgetExhausted()
    budget = SharedModelBudget(2)
    budget.adapter = adapter
    with pytest.raises(BatchBudgetExhausted) as denied:
        budget.ask('synthetic', {}, {})
    assert len(calls) == 1  # Only the synthetic local gate, never a provider.
    assert budget.snapshot() == dict(uncached_succeeded=0, uncached_failed=0,
        cache_hits=0, denied=1, actual_uncached_model_invocations=0,
        max_model_invocations=2)
    usage = zero_usage(2)
    chain_usage(dict(rounds=[], failure=dict(stage='PROVIDER_REQUEST',
        receipt=denied.value.receipt)), 'material', usage)
    assert usage['denied'] == 1 and usage['uncached_failed'] == 0
    assert usage['stage_usage']['material'] == dict(ask_for_attempts=1,
        uncached_succeeded=0, uncached_failed=0, cache_hits=0, denied=1)
    contradictory = dict(denied.value.receipt, cached=True)
    with pytest.raises(ValueError, match='CHAIN_ATTEMPT_OUTCOME_CONFLICT'):
        chain_usage(dict(rounds=[], failure=dict(stage='PROVIDER_REQUEST',
            receipt=contradictory)), 'material', zero_usage(2))


def test_CEREM_ADV024_pre_adapter_material_failure_has_zero_delta_and_replays(private_tmp, monkeypatch):
    graph, sources, _ = pipeline_fixtures()
    calls = []
    def material(task, request, schema):
        from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import prepare_candidate_materials
        tasks, _ = prepare_candidate_materials(graph, sources,
            decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
        current = next(item for item in tasks if item['compiled']['target']['checkpoint_id']
                       == request['target']['checkpoint_id'])
        response = _passing_answer(current, current['manifest'], current['compiled'],
            current['candidate_sources'], current['excluded_sources'])
        response.update(request['response_binding'], status='AUTO_REVIEW_HOLD',
            required_actions=['虚构材料待人工复核'])
        calls.append('material')
        return response
    adapters = _adapters(graph, sources, [], graph_answer=passing_graph_response)
    adapters['material'] = material
    from scripts.cohort_construction.p2_checkpoint_material_governance import review
    def fail_before_adapter(*args, **kwargs):
        raise review.MaterialReviewError('SYNTHETIC_PRE_ADAPTER_MATERIAL_FAILURE')
    monkeypatch.setattr(review, 'build_review_request', fail_before_adapter)
    run = _run(private_tmp / 'pre-adapter-hold', graph, sources, adapters=adapters)
    stage = next(item for item in run['stages'] if item['stage'] == 'MATERIAL:EVENT-01')
    assert stage['state'] == 'ORCHESTRATION_HOLD'
    assert stage['model_usage_delta']['actual_uncached_model_invocations'] == 0
    assert stage['model_usage_delta']['denied'] == 0
    assert not stage['model_usage_delta']['stage_usage']
    assert 'material' not in run['model_usage']['stage_usage']
    assert run['model_usage']['denied'] == 0
    assert calls == []  # The failure precedes every material adapter invocation.
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.replay import validate_pipeline_run
    assert validate_pipeline_run(run, output=private_tmp / 'pre-adapter-hold')['replay_verified']


def test_CEREM_ADV022_reconstruction_provider_failure_does_not_retry_or_settle_usage(private_tmp):
    graph, sources, _ = pipeline_fixtures()
    def revise(request):
        from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response
        answer = passing_graph_response(request)
        answer.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
            request_id='REV-UNRESOLVED', request_kind='EVENT_RECONSTRUCTION', reason='虚构未决调用',
            affected_event_ids=['EVENT-01'], affected_episode_ids=['EPISODE-01'],
            evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
            required_reconstruction=True)])
        return answer
    calls = []
    parent = _run(private_tmp / 'parent', graph, sources,
        adapters=_adapters(graph, sources, calls, graph_answer=revise))
    adapters = _adapters(graph, sources, calls)
    def fail(*args):
        calls.append('reconstruction')
        error = RuntimeError('synthetic transport state unknown')
        error.receipt = dict(invocation_id='SYNTHETIC-UNRESOLVED', cached=False, status='FAILED')
        raise error
    adapters['reconstruction'] = fail
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.automatic_revision import (
        run_bounded_graph_revision_pipeline,
    )
    result = run_bounded_graph_revision_pipeline(parent_run=parent, output=private_tmp / 'auto',
        graph=graph, sources=sources, episode_id='EPISODE-01', semantic_audits='LAZY',
        questionnaire=frozen_questionnaire(), graph_audit_context=parent['input_contract']['graph_audit_context'],
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        config=CONFIG, adapters=adapters, execute=True, semantic_audit_factory=_semantic)
    assert result['state'] == 'HOLD'
    assert result['stop_reason'] == 'RECONSTRUCTION_PROVIDER_USAGE_UNRESOLVED'
    assert result['automatic_retry_performed'] is False
    assert calls.count('reconstruction') == 1 and result['generations'] == []
    hold = result['reconstruction_accounting_hold']
    assert hold['state'] == 'IN_FLIGHT_ACCOUNTING_UNRESOLVED'
    assert hold['reconstruction_chain'] == result['reconstruction']
    assert not (private_tmp / 'auto/generation-0001/run.json').exists()
    with pytest.raises(ValueError, match='RECONSTRUCTION_IN_FLIGHT_USAGE_UNRESOLVED'):
        validate_graph_reconstruction_usage(result['reconstruction'], zero_usage(20), 20)


def test_CEREM_ADV022_reconstruction_write_failure_returns_chain_without_second_call(private_tmp, monkeypatch):
    graph, sources, _ = pipeline_fixtures()
    def revise(request):
        from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response
        answer = passing_graph_response(request)
        answer.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
            request_id='REV-PERSIST', request_kind='EVENT_RECONSTRUCTION', reason='虚构归档失败',
            affected_event_ids=['EVENT-01'], affected_episode_ids=['EPISODE-01'],
            evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
            required_reconstruction=True)])
        return answer
    calls = []
    parent = _run(private_tmp / 'parent', graph, sources,
        adapters=_adapters(graph, sources, calls, graph_answer=revise))
    def reconstruction_adapter(task, request, schema):
        from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
        calls.append('reconstruction')
        response = proposed(request)
        return response, dict(provider_receipt(),
            response_sha256=digest(dict(synthetic_provider_response=response)))
    adapters = _adapters(graph, sources, calls)
    adapters['reconstruction'] = reconstruction_adapter
    import scripts.cohort_construction.p2_checkpoint_ready_pipeline.automatic_revision as revision
    def fail_write(path, value):
        if str(path).endswith('reconstruction-0001.json'):
            raise OSError('synthetic post-response disk failure')
        from scripts.cohort_construction.p2_checkpoint_material_governance.run import _json_exclusive
        _json_exclusive(path, value)
    monkeypatch.setattr(revision, '_json_exclusive', fail_write)
    result = revision.run_bounded_graph_revision_pipeline(parent_run=parent,
        output=private_tmp / 'auto', graph=graph, sources=sources, episode_id='EPISODE-01',
        semantic_audits='LAZY', questionnaire=frozen_questionnaire(),
        graph_audit_context=parent['input_contract']['graph_audit_context'],
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        config=CONFIG, adapters=adapters, execute=True, semantic_audit_factory=_semantic)
    assert result['state'] == 'HOLD' and result['stop_reason'] == 'RECONSTRUCTION_PERSISTENCE_FAILED'
    assert result['reconstruction']['state'] == 'RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT'
    assert result['reconstruction_accounting_hold']['persistence_failure'] == 'OSError'
    assert result['reconstruction_accounting_hold']['reconstruction_chain'] == result['reconstruction']
    assert result['automatic_retry_performed'] is False
    assert calls.count('reconstruction') == 1 and calls.count('graph') == 1
    assert not (private_tmp / 'auto/generation-0001/run.json').exists()


def test_CEREM_ADV023_chain_hash_does_not_authenticate_editable_provider_receipt():
    _, audit_request, audit_response = fixtures()
    request = deepcopy(audit_request)
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import build_graph_reconstruction_request
    request = build_graph_reconstruction_request(audit_request, audit_response)
    chain = run_graph_reconstruction(audit_request, audit_response,
        lambda *args: (proposed(request), provider_receipt()))
    forged = deepcopy(chain)
    forged['provider_receipt']['cached'] = True
    forged['chain_sha256'] = canonical_sha256({key: value for key, value in forged.items()
        if key != 'chain_sha256'})
    assert validate_graph_reconstruction_chain(forged,
        expected_request=(audit_request, audit_response),
        expected_provider=dict(invocation_id='SYNTHETIC-INVOCATION',
            payload_sha256=canonical_sha256(chain['request']), response_sha256='a' * 64))['replay_verified']
    # This expected failure demonstrates that a chain hash alone is not remote
    # evidence. The separately archived receipt mismatch must be rejected.
    with pytest.raises(ValueError, match='RECONSTRUCTION_PROVIDER_RECEIPT_MISMATCH'):
        validate_graph_reconstruction_usage(forged, zero_usage(20), 20,
            provider_receipt=chain['provider_receipt'])
