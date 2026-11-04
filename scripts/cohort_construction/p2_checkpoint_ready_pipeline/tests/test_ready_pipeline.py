"""Fictional end-to-end and failure-path checks for the persistent pipeline."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.cohort_construction.p2_checkpoint_delivery_audit.audit import DIMENSIONS as DELIVERY_DIMENSIONS
from scripts.cohort_construction.p2_checkpoint_delivery_audit.tests.test_delivery_audit import passing_delivery
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_material_governance import (
    inputs as material_inputs, operation as material_operation, review_for as material_review,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import (
    _pass_only, _passing_answer, fictional_graph,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import prepare_candidate_materials
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_packet.tests.test_compiler import frozen_questionnaire, semantic
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response
from scripts.cohort_construction.p2_checkpoint_ready_pipeline import validate_pipeline_run
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.orchestration import run_episode_pipeline, resume_episode_pipeline
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import read, json_value
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.revision import (
    consume_graph_revision, request_delivery_revision, request_graph_revision,
)


CONFIG = dict(protocol_version='checkpoint-automated-material-review-0.2.0',
    remote_execution_authorized=True, synthetic_only=True,
    doctor_packet_release=False, real_doctor_assignment_creation=False,
    clinical_release_permitted=False)


@pytest.fixture
def private_tmp(tmp_path):
    from scripts.cohort_construction.p2_checkpoint_material_review_app import build
    from scripts.cohort_construction.p2_checkpoint_material_review_app.build import private_mkdir
    root = build.PRIVATE_ROOT.resolve() / 'p2_checkpoint_ready_pipeline_tests_20261101'
    private_mkdir(root, parents=True, exist_ok=True)
    destination = root / (tmp_path.name + '-' + str(len(list(root.glob(tmp_path.name + '*')))))
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    yield destination


def fixtures():
    graph, sources = fictional_graph()
    audits = {event['event_id']: None for event in graph['events']}
    return graph, sources, audits


def run_pass(private_tmp, *, execute=True, max_calls=20, graph_answer=passing_graph_response,
             delivery_answer=passing_delivery):
    graph, sources, _ = fixtures()
    calls = []
    def graph_adapter(task, request, schema):
        calls.append('graph')
        return graph_answer(request), dict(cached=False)
    material_calls = {}
    def material_adapter(task, request, schema):
        calls.append('material')
        target = request['target']['checkpoint_id']
        material_calls[target] = material_calls.get(target, 0) + 1
        if material_calls[target] > 1:
            return _pass_only(request), dict(cached=False)
        from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import prepare_candidate_materials
        tasks, failures = prepare_candidate_materials(graph, sources,
            decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
        current = next(t for t in tasks if t['compiled']['target']['checkpoint_id'] == target)
        answer = _passing_answer(current, current['manifest'], current['compiled'],
            current['candidate_sources'], current['excluded_sources'])
        answer.update(request['response_binding'])
        return answer, dict(cached=False)
    def delivery_adapter(task, request, schema):
        calls.append('delivery')
        return delivery_answer(request), dict(cached=False)
    run = run_episode_pipeline(output=private_tmp / 'run', graph=graph, sources=sources,
        episode_id='EPISODE-01', semantic_audits='LAZY', questionnaire=frozen_questionnaire(),
        graph_audit_context=dict(processing_units=[dict(unit_id='ALL', source_ids=[s['source_id'] for s in sources])]),
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        config=CONFIG, adapters=dict(graph=graph_adapter, material=material_adapter, delivery=delivery_adapter),
        execute=execute, max_model_calls=max_calls,
        semantic_audit_factory=lambda item: dict(semantic(item['compiled']),
            target=deepcopy(item['compiled']['target'])))
    # SemanticAudit is bound to the final compiled version, not an automatic PASS.
    if run.get('episode_material_set') is not None and not run.get('actual_deliveries'):
        pass
    return run, calls


def passing_run(private_tmp):
    graph, sources, _ = fixtures()
    calls = []
    material_tasks = {}
    def material(task, request, schema):
        calls.append('material')
        target = request['target']['checkpoint_id']
        material_tasks[target] = material_tasks.get(target, 0) + 1
        if material_tasks[target] > 1:
            return _pass_only(request)
        from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import prepare_candidate_materials
        tasks, _ = prepare_candidate_materials(graph, sources, decision_task_id='SYNTHETIC-TASK',
            visibility_policy_version='SYNTHETIC-POLICY')
        current = next(t for t in tasks if t['compiled']['target']['checkpoint_id'] == target)
        answer = _passing_answer(current, current['manifest'], current['compiled'],
            current['candidate_sources'], current['excluded_sources'])
        answer.update(request['response_binding'])
        return answer
    # The independent semantic fixture is constructed only after the material
    # partition exists. A delivery factory supplies it to the pipeline compiler.
    run = run_episode_pipeline(output=private_tmp / 'run', graph=graph, sources=sources,
        episode_id='EPISODE-01', semantic_audits='LAZY', questionnaire=frozen_questionnaire(),
        graph_audit_context=dict(processing_units=[dict(unit_id='ALL', source_ids=[s['source_id'] for s in sources])]),
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        config=CONFIG, adapters=dict(graph=lambda t, r, s: (calls.append('graph') or passing_graph_response(r)),
            material=material, delivery=lambda t, r, s: (calls.append('delivery') or passing_delivery(r))),
        execute=True, semantic_audit_factory=lambda item: dict(semantic(item['compiled']),
            target=deepcopy(item['compiled']['target'])))
    return run, calls


def test_actual_staging_pipeline_persists_replays_and_never_releases(private_tmp):
    run, calls = passing_run(private_tmp)
    assert not run['doctor_assignment_created'] and not run['clinical_release_permitted']
    assert run['state'] == 'READY_FOR_HUMAN_SAMPLING'
    assert run['ready_resource']['resource_state'] == 'READY_FOR_HUMAN_SAMPLING'
    assert calls == ['graph', 'material', 'material', 'material', 'material', 'delivery']
    assert run['model_usage']['actual_uncached_model_invocations'] == 6
    receipt = validate_pipeline_run(run, output=private_tmp / 'run')
    assert receipt['replay_verified'] and not receipt['model_call_performed']
    assert json.loads((private_tmp / 'run/replay-receipt.json').read_text()) == receipt
    tampered = deepcopy(run); tampered['ready_resource']['resource_state'] = 'CLINICAL_PACKET_ELIGIBLE'
    with pytest.raises(ValueError, match='HASH_INVALID'):
        validate_pipeline_run(tampered)


def test_graph_hold_and_revision_never_create_material_or_delivery(private_tmp):
    graph, sources, _ = fixtures()
    def revise(request):
        answer = passing_graph_response(request)
        answer['status'] = 'AUTO_GRAPH_AUDIT_REVISE'
        answer['revision_requests'] = [dict(request_id='REV-1', request_kind='EVENT_RECONSTRUCTION',
            reason='虚构新图请求', evidence=[dict(source_id='DOC-02',
                exact_quote='决定：暂缓切除并补充病理证据。')],
            affected_event_ids=['EVENT-01'], affected_episode_ids=['EPISODE-01'],
            required_reconstruction=True)]
        return answer
    run, calls = run_pass(private_tmp, graph_answer=revise)
    assert run['state'] == 'REVISION_REQUIRED'
    assert 'episode_material_set' not in run and 'actual_deliveries' not in run
    handoff = request_graph_revision(run, origin='graph_audit')
    assert handoff['new_graph_required'] and handoff['all_material_and_delivery_passes_nontransferable']
    assert not handoff['automatic_reconstruction_performed']
    assert validate_pipeline_run(run, output=private_tmp / 'run')['state'] == 'REVISION_REQUIRED'


def test_shared_budget_denies_later_stage_and_resume_replays_parent(private_tmp):
    run, calls = run_pass(private_tmp / 'first', max_calls=2)
    assert calls == ['graph', 'material']
    assert run['state'] == 'HOLD'
    assert 'actual_deliveries' not in run
    assert 'delivery_audit_chain' not in run and 'ready_resource' not in run
    assert run['episode_material_set']['checkpoint_materials'][1]['state'] == 'AUTO_REVIEW_HOLD'
    before = (private_tmp / 'first/run/run.json').read_bytes()
    resumed, calls2 = run_pass(private_tmp / 'second')
    # Parent is unchanged; resume API tested separately with a partially executed run below.
    assert (private_tmp / 'first/run/run.json').read_bytes() == before


def _adapters(graph, sources, calls, *, graph_answer=passing_graph_response,
              delivery_answer=passing_delivery):
    material_calls = {}
    def material(task, request, schema):
        calls.append('material')
        target = request['target']['checkpoint_id']
        material_calls[target] = material_calls.get(target, 0) + 1
        if material_calls[target] > 1:
            return _pass_only(request)
            return _pass_only(request)
        tasks, _ = prepare_candidate_materials(graph, sources,
            decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
        current = next(t for t in tasks if t['compiled']['target']['checkpoint_id'] == request['target']['checkpoint_id'])
        answer = _passing_answer(current, current['manifest'], current['compiled'],
            current['candidate_sources'], current['excluded_sources'])
        answer.update(request['response_binding'])
        return answer
    return dict(graph=lambda task, request, schema: (calls.append('graph') or graph_answer(request)),
        material=material,
        delivery=lambda task, request, schema: (calls.append('delivery') or delivery_answer(request)))


def _semantic(item):
    return dict(semantic(item['compiled']), target=deepcopy(item['compiled']['target']))


def _run(output, graph, sources, *, adapters, execute=True, max_calls=20,
         graph_revision=None):
    return run_episode_pipeline(output=output, graph=graph, sources=sources,
        episode_id='EPISODE-01', semantic_audits='LAZY', questionnaire=frozen_questionnaire(),
        graph_audit_context=dict(processing_units=[dict(unit_id='ALL', source_ids=[s['source_id'] for s in sources])]),
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        config=CONFIG, adapters=adapters, execute=execute, max_model_calls=max_calls,
        semantic_audit_factory=_semantic, graph_revision=graph_revision)


def _delivery_revise(request):
    answer = passing_delivery(request)
    answer.update(status='AUTO_DELIVERY_AUDIT_REVISE', revision_requests=[dict(
        request_id='SYNTHETIC-HTML-REVISION', request_kind='HTML_RENDERER_REPAIR',
        reason='虚构：要求下游重新生成实际交付版本', checkpoint_ids=['EVENT-01', 'EVENT-02'],
        required_new_version=True)])
    return answer


def _new_staging_version(parent):
    deliveries = deepcopy(parent['actual_deliveries'])
    for delivery in deliveries:
        delivery['assignment_id'] += '-V2'
        from scripts.cohort_construction.p2_checkpoint_review_app.build import build_reviewer_html
        delivery['html'] = build_reviewer_html(delivery['bundle'], assignment_id=delivery['assignment_id'],
            expected_packet_sha256=delivery['packet']['packet_sha256'])
    return deliveries


def _crash_window_complete_run(private_tmp):
    graph, sources, _ = fixtures()
    calls = []
    parent = _run(private_tmp / 'complete', graph, sources,
        adapters=_adapters(graph, sources, calls))
    parent['material_chains'] = {'EVENT-01': parent['episode_material_set']['checkpoint_materials'][0]['auto_review_chain']}
    del parent['episode_material_set']
    del parent['actual_deliveries']
    del parent['delivery_audit_chain']
    del parent['ready_resource']
    parent['stages'] = [dict(stage='GRAPH_AUDIT', state='AUTO_GRAPH_AUDIT_PASS', reason_code='AUTO_PASS'),
        dict(stage='MATERIAL_PREPARATION', state='PASS', reason_code=None),
        dict(stage='MATERIAL:EVENT-01', state='AUTO_REVIEW_PASS', reason_code='AUTO_PASS'),
        dict(stage='MATERIAL:EVENT-02', state='PREPARED_NOT_EXECUTED', reason_code=None)]
    parent['state'] = 'PREPARED_NOT_EXECUTED'
    parent = json_value(parent)
    parent['run_sha256'] = canonical_sha256({key: value for key, value in parent.items()
        if key != 'run_sha256'})
    return graph, sources, parent, calls


def _persist_crash_window(private_tmp, parent):
    partial = private_tmp / 'parent'
    (partial / 'materials/EVENT-01').mkdir(parents=True)
    (partial / 'materials/EVENT-02').mkdir(parents=True)
    (partial / 'graph').mkdir()
    for name, value in [('run', parent), ('input-contract', parent['input_contract']),
                        ('graph/request', parent['graph_audit_chain']['initial_request']),
                        ('graph/chain', parent['graph_audit_chain']),
                        ('materials/EVENT-01/prepared-task',
                            read(private_tmp / 'complete/materials/EVENT-01/prepared-task.json')),
                        ('materials/EVENT-01/chain', parent['material_chains']['EVENT-01'])]:
        (partial / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return partial


def test_partial_material_chains_resume_without_recalling_completed_stages(private_tmp):
    graph, sources, parent, calls = _crash_window_complete_run(private_tmp)
    parent = _persist_crash_window(private_tmp, parent)
    partial = parent
    parent = read(parent / 'run.json')
    # Synthetic crash-window fixture: the first chain is durable, but the
    # Episode material set and second chain were not reached before interruption.
    complete = private_tmp / 'complete'
    for relative in ('implementation-snapshot.json', 'materials/EVENT-01/replay.json',
                     'materials/EVENT-01/journal-receipt.json'):
        (partial / relative).write_bytes((complete / relative).read_bytes())
    for source in (complete / 'materials/EVENT-01/rounds').rglob('*.json'):
        destination = partial / source.relative_to(complete)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    calls = ['graph', 'material', 'material', 'material']
    assert parent['state'] == 'PREPARED_NOT_EXECUTED'
    assert 'episode_material_set' not in parent
    assert parent['material_chains']['EVENT-01']['state'] == 'AUTO_REVIEW_PASS'
    assert not (private_tmp / 'parent/materials/EVENT-02/chain.json').exists()
    assert calls == ['graph', 'material', 'material', 'material']
    before = (private_tmp / 'parent/run.json').read_bytes()
    calls2 = []
    resumed = resume_episode_pipeline(previous_output=private_tmp / 'parent', output=private_tmp / 'child',
        adapters=_adapters(graph, sources, calls2), max_model_calls=20,
        semantic_audit_factory=_semantic)
    assert resumed['state'] == 'READY_FOR_HUMAN_SAMPLING'
    assert calls2 == ['material', 'material', 'delivery']
    assert resumed['material_chains']['EVENT-01'] == parent['material_chains']['EVENT-01']
    assert resumed['model_usage']['actual_uncached_model_invocations'] == 9
    assert resumed['continuation']['inherited_stages_replayed_without_model_calls']
    assert (private_tmp / 'parent/run.json').read_bytes() == before
    assert validate_pipeline_run(resumed, output=private_tmp / 'child')['replay_verified']
    tampered = deepcopy(parent)
    tampered['input_contract']['visibility_policy_version'] = 'CHANGED-POLICY'
    with pytest.raises(ValueError, match='HASH_INVALID'):
        validate_pipeline_run(tampered)


def test_graph_revision_consumes_new_graph_and_restarts_every_audit(private_tmp):
    graph, sources, _ = fixtures()
    def revise(request):
        answer = passing_graph_response(request)
        answer.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
            request_id='RECONSTRUCT-01', request_kind='EVENT_RECONSTRUCTION',
            reason='虚构：独立重建层需重新定位候选事件',
            affected_event_ids=['EVENT-01'], affected_episode_ids=['EPISODE-01'],
            evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
            required_reconstruction=True)])
        return answer
    calls = []
    parent = _run(private_tmp / 'superseded', graph, sources,
        adapters=_adapters(graph, sources, calls, graph_answer=revise))
    request = request_graph_revision(parent, origin='graph_audit')
    unchanged = deepcopy(graph)
    with pytest.raises(ValueError, match='NEW_VERSION_AND_HASH'):
        consume_graph_revision(request, unchanged)
    rebuilt = deepcopy(graph)
    rebuilt['graph_version'] = 'SYNTHETIC-GRAPH-V2'
    rebuilt['graph_sha256'] = canonical_sha256({key: value for key, value in rebuilt.items() if key != 'graph_sha256'})
    receipt = consume_graph_revision(request, rebuilt)
    assert receipt['automatic_reconstruction_performed'] is False
    assert receipt['independent_reconstruction_receipt_required'] is False
    assert not receipt['old_material_and_delivery_passes_inherited']
    calls2 = []
    fresh = _run(private_tmp / 'rebuilt', rebuilt, sources, adapters=_adapters(rebuilt, sources, calls2),
        graph_revision=dict(parent_run=parent, origin='graph_audit'))
    assert fresh['state'] == 'READY_FOR_HUMAN_SAMPLING'
    assert fresh['graph_audit_chain']['graph_sha256'] != parent['graph_audit_chain']['graph_sha256']
    assert fresh['episode_material_set']['set_sha256'] != parent.get('episode_material_set', {}).get('set_sha256')
    assert calls2 == ['graph', 'material', 'material', 'material', 'material', 'delivery']
    assert fresh['material_chains']['EVENT-01'] != parent.get('material_chains', {}).get('EVENT-01')
    assert fresh['graph_revision_cycle']['parent_run_sha256'] == parent['run_sha256']
    assert fresh['superseded_parent_run']['run_sha256'] == parent['run_sha256']
    assert validate_pipeline_run(fresh, output=private_tmp / 'rebuilt')['replay_verified']


def test_material_graph_issue_consumes_new_graph_without_mutating_material(private_tmp):
    graph, sources, _ = fixtures()
    manifest, compiled = material_inputs()
    from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import bind_candidate_graph
    from scripts.cohort_construction.p2_checkpoint_material_governance.review import run_review_chain
    from scripts.cohort_construction.p2_checkpoint_material_governance.episode import build_episode_material_set
    context = bind_candidate_graph(graph, graph_version=graph['graph_version'],
        target_event_id='EVENT-01', checkpoint_id='cp1')
    answer = material_review(manifest, compiled, status='AUTO_REVIEW_HOLD', blocking=True,
        task_context=context, operations=[dict(operation_id='GRAPH-01',
            operation='FLAG_EVENT_GRAPH_ISSUE', source_id='doc1', reason='虚构材料层发现候选事件问题'),
            material_operation(manifest['sources'][0]['text'])])
    chain = run_review_chain(manifest, compiled, lambda *args: answer, task_context=context)
    item = dict(manifest=chain['manifest'], compiled=chain['compiled'],
        doctor_view=chain['doctor_view'], auto_review_chain=chain,
        revision_ledger=[r['ledger'] for r in chain['rounds']], state=chain['state'], event_reference='EVENT-01')
    material_set = build_episode_material_set(dict(episode_id='EPISODE-01',
        candidate_version=graph['graph_version'], input_sha256=canonical_sha256(graph),
        graph_snapshot=graph), [item])
    parent = dict(format='checkpoint-ready-pipeline-run-0.1.0',
        input_contract=dict(graph=graph), episode_material_set=material_set)
    parent['run_sha256'] = canonical_sha256(parent)
    request = request_graph_revision(parent, origin='material_review')
    assert request['handoff'][0]['material_layer_may_change_event_graph'] is False
    rebuilt = deepcopy(graph)
    rebuilt['graph_version'] = 'SYNTHETIC-GRAPH-V2'
    rebuilt['graph_sha256'] = canonical_sha256({key: value for key, value in rebuilt.items()
        if key != 'graph_sha256'})
    receipt = consume_graph_revision(request, rebuilt)
    assert receipt['old_material_and_delivery_passes_inherited'] is False
    with pytest.raises(ValueError, match='GRAPH_REVISION_NOT_REQUESTED'):
        request_graph_revision(parent, origin='graph_audit')


def test_CE_PIPE007_graph_revision_auto_loop_uses_separate_reconstruction_and_fresh_audits(private_tmp):
    graph, sources, _ = fixtures()
    def revise(request):
        answer = passing_graph_response(request)
        answer.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
            request_id='REV-1', request_kind='EVENT_RECONSTRUCTION', reason='虚构修订',
            affected_event_ids=['EVENT-01'], affected_episode_ids=['EPISODE-01'],
            evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
            required_reconstruction=True)])
        return answer
    calls = []
    parent = _run(private_tmp / 'parent', graph, sources,
        adapters=_adapters(graph, sources, calls, graph_answer=revise))
    def reconstruct(request):
        if 'candidate_graph' in request:
            return passing_graph_response(request)
        calls.append('reconstruct')
        candidate = deepcopy(request['input_graph'])
        return dict(format='candidate-graph-reconstruction-response-0.2.0',
            reconstruction_id='SYNTHETIC-R1', **request['response_binding'],
            status='RECONSTRUCTED_PENDING_INDEPENDENT_AUDIT',
            graph=dict(events=candidate['events'], relations=candidate['relations'],
                episodes=candidate['episodes'], graph_version='SYNTHETIC-GRAPH-V2'),
            rationale='虚构独立重建')
    def reconstruction_adapter(task, request, schema):
        from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
        response = reconstruct(request)
        return response, dict(invocation_id='SYNTHETIC-RECONSTRUCTION-INVOKE',
            response_sha256=digest(dict(synthetic_provider_response=response)), cached=False)
    adapters = _adapters(graph, sources, calls, graph_answer=reconstruct)
    adapters['reconstruction'] = reconstruction_adapter
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.automatic_revision import run_bounded_graph_revision_pipeline
    rebuilt = deepcopy(graph)
    rebuilt['graph_version'] = 'placeholder'
    result = run_bounded_graph_revision_pipeline(parent_run=parent, output=private_tmp / 'auto',
        graph=rebuilt, sources=sources,
        graph_audit_sources=[source for source in sources if source['source_id'] != 'DOC-BUDGET'],
        episode_id='EPISODE-01', semantic_audits='LAZY',
        questionnaire=frozen_questionnaire(), graph_audit_context=dict(processing_units=[
            dict(unit_id='ALL', source_ids=[s['source_id'] for s in sources if s['source_id'] != 'DOC-BUDGET'])]),
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        config=CONFIG, adapters=adapters, execute=True,
        max_rounds=3, semantic_audit_factory=_semantic)
    assert result['state'] == 'READY_FOR_HUMAN_FINAL_REVIEW'
    assert calls.count('reconstruct') == 1 and calls.count('graph') == 2
    assert result['final_run']['graph_revision_cycle']['automatic_reconstruction_performed']
    assert result['final_run']['state'] == 'READY_FOR_HUMAN_SAMPLING'
    assert result['final_run']['model_usage']['actual_uncached_model_invocations'] == 7
    assert result['final_run']['model_usage']['max_model_invocations'] == 20
    assert not result['clinical_release_permitted'] and not result['clinical_reference_created']
    assert validate_pipeline_run(result['final_run'], output=private_tmp / 'auto/generation-0001')['replay_verified']
    final = result['final_run']
    assert len(final['input_contract']['graph_audit_sources']) == 3
    assert len(final['input_contract']['sources']) == 4
    assert final['graph_audit_chain']['initial_request']['audit_scope']['source_coverage_complete']
    assert not final['graph_audit_chain']['initial_request']['audit_scope']['uncovered_supplied_source_ids']


def test_CE_PIPE007_unresolved_graph_input_never_calls_reconstructor(private_tmp):
    graph, sources, _ = fixtures()
    def unresolved(request):
        answer = passing_graph_response(request)
        answer.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
            request_id='HUMAN-1', request_kind='HUMAN_REVIEW', reason='虚构重大未决',
            affected_event_ids=[], affected_episode_ids=[], evidence=[], required_reconstruction=True)])
        return answer
    parent = _run(private_tmp / 'unresolved', graph, sources,
        adapters=_adapters(graph, sources, [], graph_answer=unresolved))
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.automatic_revision import run_bounded_graph_revision_pipeline
    result = run_bounded_graph_revision_pipeline(parent_run=parent, output=private_tmp / 'unresolved-auto',
        graph=graph, sources=sources, episode_id='EPISODE-01', semantic_audits='LAZY',
        questionnaire=frozen_questionnaire(), graph_audit_context=parent['input_contract']['graph_audit_context'],
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY', config=CONFIG,
        adapters=_adapters(graph, sources, []), execute=True, semantic_audit_factory=_semantic)
    assert result['state'] == 'HOLD'
    assert result['generations'] == []


def test_delivery_revision_binds_new_actual_artifacts_and_reaudits(private_tmp):
    graph, sources, _ = fixtures()
    calls = []
    parent = _run(private_tmp / 'superseded', graph, sources,
        adapters=_adapters(graph, sources, calls, delivery_answer=_delivery_revise))
    assert parent['state'] == 'REVISION_REQUIRED' and 'ready_resource' not in parent
    request = request_delivery_revision(parent)
    assert request['audit_may_modify_delivery'] is False
    with pytest.raises(ValueError, match='HASH_CHANGED_REQUIRED'):
        resume_episode_pipeline(previous_output=private_tmp / 'superseded', output=private_tmp / 'unchanged',
            adapters=_adapters(graph, sources, []), revision_cycle=dict(new_deliveries=parent['actual_deliveries']))
    calls2 = []
    child = resume_episode_pipeline(previous_output=private_tmp / 'superseded', output=private_tmp / 'revised',
        adapters=_adapters(graph, sources, calls2),
        revision_cycle=dict(new_deliveries=_new_staging_version(parent)))
    assert child['state'] == 'READY_FOR_HUMAN_SAMPLING'
    assert calls2 == ['delivery']
    assert child['delivery_revision_cycle']['new_delivery_input_sha256'] != parent['delivery_audit_chain']['delivery_input_sha256']
    assert child['delivery_audit_chain']['initial_request'] != parent['delivery_audit_chain']['initial_request']
    assert not child['delivery_revision_cycle']['audit_modified_delivery']
    assert validate_pipeline_run(child, output=private_tmp / 'revised')['replay_verified']


def test_resume_requires_explicit_revision_cycle_and_rejects_changed_parent(private_tmp):
    graph, sources, _ = fixtures()
    parent = _run(private_tmp / 'superseded', graph, sources,
        adapters=_adapters(graph, sources, [], delivery_answer=_delivery_revise))
    with pytest.raises(ValueError, match='INHERITED_DELIVERY_REQUEST_CHANGED'):
        resume_episode_pipeline(previous_output=private_tmp / 'superseded', output=private_tmp / 'without-revision',
            adapters=_adapters(graph, sources, []))
    parent['input_contract']['graph_audit_context']['processing_units'] = []
    (private_tmp / 'superseded/changed-run.json').write_text(json.dumps(parent), encoding='utf-8')
    with pytest.raises(ValueError, match='HASH_INVALID'):
        validate_pipeline_run(parent)


def test_pipeline_persists_graph_and_delivery_journals_without_rewriting_parent(private_tmp):
    run, _ = passing_run(private_tmp)
    for path in ('graph/rounds/0001/input.json', 'graph/rounds/0001/completed-round.json',
                 'delivery-audit/rounds/0001/input.json', 'delivery-audit/rounds/0001/completed-round.json'):
        assert (private_tmp / 'run' / path).is_file()
    for checkpoint_id in ('EVENT-01', 'EVENT-02'):
        receipt = read(private_tmp / 'run/materials' / checkpoint_id / 'journal-receipt.json')
        assert receipt['verified']
    assert not run['doctor_assignment_created'] and not run['clinical_release_permitted']

    graph, sources, _ = fixtures()
    run = run_episode_pipeline(output=private_tmp / 'offline', graph=graph, sources=sources,
        episode_id='EPISODE-01', semantic_audits={}, questionnaire=frozen_questionnaire(),
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY', config=CONFIG)
    assert run['state'] == 'PREPARED_NOT_EXECUTED'
    assert not run['model_usage']['actual_uncached_model_invocations']
    assert not (private_tmp / 'offline/episode-material-set.json').exists()
    assert validate_pipeline_run(run, output=private_tmp / 'offline')['replay_verified']
