"""Model-free tests for the distinct Episode/decision-point feasibility viewer."""
from copy import deepcopy
import json

import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import (
    material_item, prepare_candidate_materials,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.episode import (
    build_episode_material_set,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.feasibility import (
    FORMAT, build_episode_feasibility_review, render_episode_feasibility_html,
    replay_episode_feasibility_review, validate_episode_feasibility_review,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.review import run_review_chain
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import (
    fictional_graph,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_review_remediation import echo_response
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.audit import build_graph_audit_request
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.chain import run_graph_audit_chain
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response


def fixture():
    graph, sources = fictional_graph()
    tasks, failures = prepare_candidate_materials(graph, sources,
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC')
    assert not failures
    items = []
    for task in tasks:
        chain = run_review_chain(task['manifest'], task['compiled'],
            lambda task_name, request, schema, **kwargs: echo_response(request),
            candidate_sources=task['candidate_sources'], excluded_sources=task['excluded_sources'],
            task_context=task['task_context'])
        items.append(material_item(task, chain))
    material_set = build_episode_material_set(dict(episode_id='EPISODE-01',
        candidate_version=graph['graph_version'], input_sha256=canonical_sha256(graph),
        graph_snapshot=graph), items)
    proposals = []
    for task in tasks:
        proposal = dict(event_id=task['event']['event_id'],
            management_question='这个虚构病例应否进入根治路径评估？',
            action='虚构记录中的候选管理动作', rationale='原文有管理选择与条件变化，故作为候选而非临床认证。',
            uncertainty_dimensions=dict(choice_time=dict(status='uncertain',
                reason_category='genuine_missing_information', explanation='虚构时点未证实，不补造选择时间。')),
            source_document_clock=dict(value='2020-01-02 09:00:00', precision='minute'))
        proposals.append(dict(unit_id=task['event']['unit_id'], events=[proposal]))
    context = dict(processing_units=[dict(unit_id='ALL',
        source_ids=[source['source_id'] for source in sources])],
        discovery_proposals=proposals)
    request = build_graph_audit_request(graph, sources, **context)
    audit = run_graph_audit_chain(request, lambda *args: passing_graph_response(request))
    return material_set, sources, audit


def test_CE_AMG031_feasibility_shows_full_originals_rationale_and_structure_questions():
    material_set, sources, audit = fixture()
    review = build_episode_feasibility_review(material_set, sources, audit, case_id='SYNTHETIC')
    assert validate_episode_feasibility_review(review)
    assert all(event['selection_rationale'] for event in review['events'])
    assert all(event['rationale_provenance'] == 'UPSTREAM_DISCOVERY_LLM_PROPOSAL_NOT_CLINICAL_CERTIFICATION'
               for event in review['events'])
    assert any(event['citations'] for event in review['events'])
    assert all(event['ordering']['kind'] == 'RECORD_TIME_PROXY' for event in review['events'])
    assert all('记录时间代理：' in event['ordering']['display'] for event in review['events'])
    assert review['clinical_content_is_private'] and not review['formal_blind_packet_created']
    assert not review['acceptable_mode_set_created'] and not review['doctor_answer_created']
    assert all(event['automatic_state_is_not_clinical_acceptance'] for event in review['events'])
    page = render_episode_feasibility_html(review)
    assert '为什么被还原为候选决策点' in page and '本点资料与原始文件' in page
    assert '这个决策点前，已经保存了哪些资料' in page and '之前可见投影' in page
    assert all(event['checkpoint_context'] is not None for event in review['events'])
    assert all(event['structured_llm_judgments'] for event in review['events'])
    assert all(event['material_review_judgment']['available'] for event in review['events'])
    first = review['events'][0]['checkpoint_context']
    assert first['prechoice_coverage_note'].endswith('缺失不等于没有资料。')
    assert first['projection_status'] == 'SAVED_ALGORITHMIC_PROJECTION_NOT_CLINICIAN_APPROVED'
    assert '决定：暂缓切除并补充病理证据。' not in review['events'][1]['checkpoint_context']['projected_visible_materials'][0]['text']
    assert '不是全部既往病历' in first['prechoice_coverage_note']
    assert '完整原文：可能包含当前选择及选择后信息' in page
    assert 'Episode 的起止范围' in page and 'SHOULD_MERGE' in page
    assert sources[0]['text'] in page
    assert '这不是正式盲评' in page and '不生成治疗答案' in page
    assert 'readability' not in page
    # A real private viewer initially failed direct replay only because tuple
    # constants became arrays after JSON loading. This compares the actual
    # persistence representation, not merely two fresh in-memory objects.
    restored = json.loads(json.dumps(review, ensure_ascii=False))
    assert build_episode_feasibility_review(material_set, sources, audit,
        case_id='SYNTHETIC') == restored
    assert validate_episode_feasibility_review(restored)
    assert render_episode_feasibility_html(restored) == page
    receipt = replay_episode_feasibility_review(review, restored, page)
    assert receipt['replay_verified'] and receipt['model_call_performed'] is False
    assert receipt['formal_blind_packet_created'] is False
    with pytest.raises(ValueError, match='REPLAY_REVIEW_MISMATCH'):
        replay_episode_feasibility_review(review, dict(restored, case_id='OTHER'), page)
    with pytest.raises(ValueError, match='REPLAY_HTML_MISMATCH'):
        replay_episode_feasibility_review(review, restored, page + 'tampered')


def test_CE_AMG032_checkpoint_context_projection_and_llm_provenance_fail_closed():
    material_set, sources, audit = fixture()
    review = build_episode_feasibility_review(material_set, sources, audit)
    for event in review['events']:
        context = event['checkpoint_context']
        assert context['event_id'] == event['event_id']
        assert context['projection_status'] == 'SAVED_ALGORITHMIC_PROJECTION_NOT_CLINICIAN_APPROVED'
        assert context['material_summary']['clinical_release_permitted'] is False
        for source in context['projected_visible_materials']:
            assert source['text']
        assert event['material_review_judgment']['rounds'][0]['ledger']
    tampered = deepcopy(review)
    tampered['events'][0]['checkpoint_context']['prechoice_coverage_note'] = '全部既往病历已齐全'
    tampered['review_sha256'] = canonical_sha256({key: value for key, value in tampered.items()
                                                  if key != 'review_sha256'})
    with pytest.raises(ValueError, match='CHECKPOINT_CONTEXT_INVALID'):
        validate_episode_feasibility_review(tampered)
    page = render_episode_feasibility_html(review)
    assert '完整原文：可能包含当前选择及选择后信息' in page
    assert '模型提出操作' in page and '程序：' in page or '本轮没有材料操作' in page
    assert '正式盲评包' in page and 'doctor_answer_created:false' in page

def test_CE_AMG031_order_never_fabricates_missing_choice_time():
    graph, sources = fictional_graph()
    event = next(event for event in graph['events'] if event['event_id'] == 'EVENT-01')
    event.update(choice_time=None, phase='术中探查后', time_basis='operative_phase',
        action='虚构术中探查后的候选管理动作',
        time_validation=dict(choice_time=None, choice_time_verified=False,
            original_proposal=dict(choice_time='2020-01-02T09:00:00+08:00')))
    for event in graph['events']:
        if event['event_id'] in graph['episodes'][0]['checkpoint_ids']:
            event['action'] = '虚构记录中的候选管理动作'
    graph['graph_sha256'] = canonical_sha256({key: value for key, value in graph.items()
        if key != 'graph_sha256'})
    tasks, failures = prepare_candidate_materials(graph, sources,
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC')
    assert not failures
    items = []
    for task in tasks:
        chain = run_review_chain(task['manifest'], task['compiled'],
            lambda task_name, request, schema, **kwargs: echo_response(request),
            candidate_sources=task['candidate_sources'], excluded_sources=task['excluded_sources'],
            task_context=task['task_context'])
        items.append(material_item(task, chain))
    material_set = build_episode_material_set(dict(episode_id='EPISODE-01',
        candidate_version=graph['graph_version'], input_sha256=canonical_sha256(graph),
        graph_snapshot=graph), items)
    review = build_episode_feasibility_review(material_set, sources)
    changed = next(event for event in review['events'] if event['event_id'] == 'EVENT-01')
    assert changed['ordering']['kind'] == 'PHASE_PROXY'
    assert '阶段代理' in changed['ordering']['display'] and changed['choice_time'] is None
    assert '不是已证实的临床时间顺序' in review['ordering_note']
    assert changed['original_time_proposal']['choice_time'] == '2020-01-02T09:00:00+08:00'
    assert '2020-01-02T09:00:00+08:00' != changed['ordering'].get('value')


def test_CE_AMG031_source_and_proposal_bindings_fail_closed():
    material_set, sources, audit = fixture()
    broken_sources = deepcopy(sources)
    broken_sources[0]['text'] = '被替换的虚构原文'
    with pytest.raises(ValueError, match='ORIGINAL_HASH_MISMATCH'):
        build_episode_feasibility_review(material_set, broken_sources, audit)
    wrong_audit = deepcopy(audit)
    wrong_audit['initial_request']['discovery_llm_proposals'] = []
    with pytest.raises(ValueError):
        build_episode_feasibility_review(material_set, sources, wrong_audit)


def test_CE_AMG031_hash_governance_and_browser_bindings_cannot_be_silently_changed():
    review = build_episode_feasibility_review(*fixture())
    tampered = deepcopy(review)
    tampered['source_documents'][0]['text'] = 'tampered'
    tampered['review_sha256'] = canonical_sha256({key: value for key, value in tampered.items()
                                                 if key != 'review_sha256'})
    with pytest.raises(ValueError, match='SOURCE_HASH_CHANGED'):
        validate_episode_feasibility_review(tampered)
    governance = deepcopy(review)
    governance['formal_blind_packet_created'] = True
    governance['review_sha256'] = canonical_sha256({key: value for key, value in governance.items()
                                                  if key != 'review_sha256'})
    with pytest.raises(ValueError, match='GOVERNANCE_CHANGED'):
        validate_episode_feasibility_review(governance)
    page = render_episode_feasibility_html(review)
    assert "doctor_answer_created:false" in page
    assert 'clinical_reference_created:false' in page
    assert '尚未上传或提交' in page
    assert 'fetch(' not in page and 'XMLHttpRequest' not in page
    assert all(event['event_id'] in page for event in review['events'])
