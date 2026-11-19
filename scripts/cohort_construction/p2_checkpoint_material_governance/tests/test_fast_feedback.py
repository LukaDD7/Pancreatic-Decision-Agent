"""CE-AMG-028/029/030: compact task, atomic execution and research feedback."""
from copy import deepcopy
import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.feedback import (
    build_research_feedback_draft, render_research_feedback_html,
    validate_research_feedback_draft,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.projection import (
    compact_material_review_request, expand_material_review_request,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.review import (
    MaterialReviewError, build_review_request, validate_material_review,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_material_governance import (
    inputs, operation, review_for,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import (
    fictional_graph, _passing_answer,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import (
    prepare_candidate_materials,
)
from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError, canonical_sha256
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.tests.test_ready_pipeline import private_tmp


def test_CE_AMG028_projection_retains_full_sources_and_binds_target_without_whole_graph():
    graph, sources = fictional_graph()
    tasks, failures = prepare_candidate_materials(graph, sources,
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC')
    assert not failures
    task = tasks[0]
    request = build_review_request(task['manifest'], task['compiled'],
        candidate_sources=task['candidate_sources'], excluded_sources=task['excluded_sources'],
        task_context=task['task_context'])
    payload = compact_material_review_request(request)
    assert 'candidate_graph_snapshot' not in payload['task_context']
    assert payload['task_context']['candidate_graph_binding'] == request['task_context']['candidate_graph_binding']
    assert payload['task_context']['target_event_research_context']['event_id'] == task['event']['event_id']
    assert payload['researcher_sources'] == request['researcher_sources']
    assert payload['candidate_sources'] == request['candidate_sources']
    assert payload['excluded_sources'] == request['excluded_sources']
    assert payload['response_binding'] == request['response_binding']
    assert expand_material_review_request(payload, request) == request
    tampered = deepcopy(payload)
    tampered['task_context']['target_event_research_context']['event_id'] = 'OTHER'
    with pytest.raises(ContractError, match='PROJECTION_REPLAY_MISMATCH'):
        expand_material_review_request(tampered, request)


def test_CE_AMG029_cross_partition_operations_are_atomic_and_order_independent():
    text = '甲。乙。丙。丁。'
    manifest, compiled = inputs(text)
    first = review_for(manifest, compiled, operations=[operation('乙。', 'KEEP')])
    plan = validate_material_review(manifest, compiled, first)
    # 甲。乙。丙。 crosses the newly split REVIEW/KEEP/REVIEW partition.
    # Sequential execution of the other two operations made it fail in SCALE-07;
    # all three have one compatible MASK disposition here.
    operations = [operation('甲。', 'MASK'), operation('丙。', 'MASK'), operation('甲。乙。丙。', 'MASK')]
    for index, item in enumerate(operations):
        item['operation_id'] = 'OP-ATOMIC-' + str(index)
    second = review_for(plan['manifest'], plan['compiled'], operations=operations)
    forward = validate_material_review(plan['manifest'], plan['compiled'], second)
    reverse_answer = deepcopy(second)
    reverse_answer['operations'].reverse()
    reverse = validate_material_review(plan['manifest'], plan['compiled'], reverse_answer)
    assert forward['compiled']['sources'] == reverse['compiled']['sources']
    assert [(p['start'], p['end'], p['disposition']) for p in forward['compiled']['sources'][0]['segments']] == [
        (0, 2, 'MASK'), (2, 4, 'MASK'), (4, 6, 'MASK'), (6, len(text), 'REVIEW')]
    assert all(row['execution_scope'] == 'ATOMIC_INPUT_VERSION_PLAN'
        for row in forward['ledger']['operations'])
    conflict = review_for(plan['manifest'], plan['compiled'], operations=[
        operation('乙。丙。', 'MASK'), operation('丙。', 'KEEP')])
    with pytest.raises(MaterialReviewError, match='ATOMIC_PLAN_CONFLICT'):
        validate_material_review(plan['manifest'], plan['compiled'], conflict)
    repeated_manifest, repeated_compiled = inputs('甲。乙。丙。丁。丙。')
    with pytest.raises(MaterialReviewError, match='NOT_UNIQUE'):
        validate_material_review(repeated_manifest, repeated_compiled,
            review_for(repeated_manifest, repeated_compiled, operations=[operation('丙。', 'MASK')]))


def test_CE_AMG030_feedback_contains_only_visible_text_and_no_model_or_formal_packet():
    graph, sources = fictional_graph()
    tasks, failures = prepare_candidate_materials(graph, sources,
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC')
    assert not failures
    from scripts.cohort_construction.p2_checkpoint_material_governance.episode import build_episode_material_set
    from scripts.cohort_construction.p2_checkpoint_material_governance.review import run_review_chain
    from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import material_item
    from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_review_remediation import echo_response
    items = []
    for task in tasks:
        chain = run_review_chain(task['manifest'], task['compiled'],
            lambda task_name, request, schema, **kwargs: echo_response(request,
                status='AUTO_REVIEW_HOLD', required_actions=['虚构待人工确认']),
            candidate_sources=task['candidate_sources'], excluded_sources=task['excluded_sources'],
            task_context=task['task_context'])
        items.append(material_item(task, chain))
    material_set = build_episode_material_set(dict(episode_id='EPISODE-01',
        candidate_version=graph['graph_version'], input_sha256=canonical_sha256(graph),
        graph_snapshot=graph), items)
    draft = build_research_feedback_draft(material_set)
    assert validate_research_feedback_draft(draft)
    assert all(cp['no_visible_material'] for cp in draft['checkpoints'])
    serialized = str(draft) + render_research_feedback_html(draft)
    for forbidden in ('auto_review_chain', 'candidate_graph_snapshot', 'actual_model_evidence',
                      'previous_review', '虚构既往检查未见远处转移'):
        assert forbidden not in serialized
    assert not draft['formal_blind_packet_created'] and not draft['clinical_release_permitted']
    tampered = deepcopy(draft)
    tampered['formal_blind_packet_created'] = True
    with pytest.raises(ValueError):
        validate_research_feedback_draft(tampered)


def _feedback_pipeline(private_tmp, *, all_pass, gate=False):
    """Exercise the real ready-pipeline interfaces with fictional adapters."""
    from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import _pass_only
    from scripts.cohort_construction.p2_checkpoint_packet.tests.test_compiler import frozen_questionnaire, semantic
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.orchestration import run_episode_pipeline
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response
    graph, sources = fictional_graph()
    config = dict(protocol_version='checkpoint-automated-material-review-0.3.0',
        remote_execution_authorized=True, synthetic_only=True,
        doctor_packet_release=False, real_doctor_assignment_creation=False,
        clinical_release_permitted=False, research_feedback_enabled=True,
        research_feedback_limitations=['虚构来源覆盖限制，仅供研究性试阅。'])
    def material(task_name, request, schema):
        if all_pass and request['review_round']['round_index'] > 1:
            return _pass_only(request)
        current = next(t for t in tasks if t['compiled']['target']['checkpoint_id'] == request['target']['checkpoint_id'])
        answer = _passing_answer(current, current['manifest'], current['compiled'],
            current['candidate_sources'], current['excluded_sources'])
        answer.update(request['response_binding'], review_round=deepcopy(request['review_round']))
        answer['doctor_view_sha256'] = request['doctor_view']['view_sha256']
        if not all_pass:
            answer.update(status='AUTO_REVIEW_HOLD', required_actions=['虚构材料待修订'])
        return answer
    tasks, failures = prepare_candidate_materials(graph, sources,
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
    assert not failures
    return run_episode_pipeline(output=private_tmp / 'run', graph=graph, sources=sources,
        episode_id='EPISODE-01', semantic_audits='LAZY', questionnaire=frozen_questionnaire(),
        graph_audit_context=dict(processing_units=[dict(unit_id='ALL',
            source_ids=[source['source_id'] for source in sources])]),
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        config=config, adapters=dict(graph=lambda task_name, request, schema: passing_graph_response(request),
            material=material, delivery=lambda *args: pytest.fail('不得进入正式交付审核')),
        execute=True, max_model_calls=20, semantic_audit_factory=lambda item: dict(
            semantic(item['compiled']), target=deepcopy(item['compiled']['target'])),
        delivery_gate=dict(reason_codes=['PENDING_INDEPENDENT_SEMANTIC_AUDIT',
            'QUESTIONNAIRE_NOT_FROZEN']) if gate else None)


def test_CE_AMG030_material_hold_reaches_feedback_but_never_formal_delivery(private_tmp):
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.replay import validate_pipeline_run
    run = _feedback_pipeline(private_tmp, all_pass=False)
    assert run['state'] == 'RESEARCH_FEEDBACK_DRAFT_READY'
    assert all(item['state'] == 'AUTO_REVIEW_HOLD'
        for item in run['episode_material_set']['checkpoint_materials'])
    assert all(material['text'] for cp in run['research_feedback_draft']['checkpoints']
        for material in cp['visible_materials'])
    assert run.get('actual_deliveries') is None and run.get('ready_resource') is None
    assert run.get('delivery_audit_chain') is None and run.get('resolved_semantic_audits') is None
    assert (private_tmp / 'run/research-feedback.html').is_file()
    receipt = validate_pipeline_run(run, output=private_tmp / 'run')
    assert receipt['state'] == 'RESEARCH_FEEDBACK_DRAFT_READY' and receipt['replay_verified']
    forged = deepcopy(run)
    forged['actual_deliveries'] = []
    forged['run_sha256'] = canonical_sha256({key: value for key, value in forged.items() if key != 'run_sha256'})
    with pytest.raises(ValueError, match='MATERIAL_HOLD_CANNOT_DELIVER'):
        validate_pipeline_run(forged)


def test_CE_AMG030_pass_and_delivery_gate_keep_feedback_separate_from_formal_ready(private_tmp):
    from scripts.cohort_construction.p2_checkpoint_ready_pipeline.replay import validate_pipeline_run
    run = _feedback_pipeline(private_tmp, all_pass=True, gate=True)
    assert run['state'] == 'RESEARCH_FEEDBACK_DRAFT_READY'
    assert run['delivery_gate']['reason_codes'] == ['PENDING_INDEPENDENT_SEMANTIC_AUDIT',
        'QUESTIONNAIRE_NOT_FROZEN']
    assert run.get('actual_deliveries') is None and run.get('ready_resource') is None
    assert run.get('delivery_audit_chain') is None and run.get('resolved_semantic_audits') is None
    assert validate_pipeline_run(run, output=private_tmp / 'run')['replay_verified']
