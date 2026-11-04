"""Candidate-graph assembly with fictional, structurally realistic v0.6 outputs."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

import scripts.cohort_construction.p2_checkpoint_material_governance.orchestrate as orchestration
import scripts.cohort_construction.p2_checkpoint_material_governance.workbench as workbench
from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_evidence_context
from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import (
    freeze_candidate_graph, material_item, normalize_sources, prepare_candidate_materials,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.orchestrate import (
    assemble_candidate_case, resume_candidate_case,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.review import build_review_request
from scripts.cohort_construction.p2_checkpoint_material_governance.workbench import receive_auto_review_submission
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_material_governance import review_for
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256


@pytest.fixture(autouse=True)
def private_test_output(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestration, 'require_private_output', lambda path: Path(path).resolve())
    monkeypatch.setattr(workbench, 'require_private_output', lambda path: Path(path).resolve())


def _event(event_id, source, decision, *, status='CANDIDATE_NEW_DECISION'):
    start = source['text'].index(decision)
    citation = dict(source_id=source['source_id'], source_sha256=source['sha256'],
        quote=decision, start=start, end=start + len(decision), status='VALID',
        raw_quote_verified=True, span_verified=True, eligible_as_source_support=True)
    event = dict(event_id=event_id, source_id=source['source_id'], source_ids=[source['source_id']],
        unit_id='UNIT-' + event_id, checkpoint_status=status, scope='yes', record_function='choice',
        management_question='虚构根治路径管理任务', choice_time='2020-01-02T09:00:00+08:00',
        time_basis='explicit_documented_choice', time_validation=dict(status='PROGRAM_CANDIDATE_NOT_CERTIFIED'),
        kind='clinical_note', phase='preoperative', action_citations=dict(valid=[citation], invalid=[], ambiguous=[]),
        choice_boundary_citations=dict(valid=[citation], invalid=[], ambiguous=[]),
        prechoice_citations=dict(valid=[], invalid=[], ambiguous=[]),
        postchoice_citations=dict(valid=[], invalid=[], ambiguous=[]),
        validated_observation_citations=dict(valid=[], invalid=[], ambiguous=[]), availability_validation=[])
    event['evidence_context'] = build_evidence_context(event, [], preserve_current_context=True)
    event['actual_model_evidence'] = deepcopy(event['evidence_context']['blocks'])
    return event


def fictional_graph():
    first = dict(source_id='DOC-01', source_version='1', text='虚构条件：未见远处转移。若血管可重建，可评估切除。当前决定：先行血管重建评估。')
    second = dict(source_id='DOC-02', source_version='1', text='虚构复评：已完成补充影像。决定：暂缓切除并补充病理证据。')
    omitted = dict(source_id='DOC-BUDGET', source_version='1', text='虚构预算遗漏的既往完整报告。')
    zero = dict(source_id='DOC-ZERO', source_version='1', text='虚构无候选管理判断单位。')
    sources = normalize_sources([first, second, omitted, zero])
    first_event = _event('EVENT-01', sources[0], '当前决定：先行血管重建评估。')
    second_event = _event('EVENT-02', sources[1], '决定：暂缓切除并补充病理证据。')
    # The same upstream event/evidence proposal remains intact; this is a ledger
    # omission to be reviewed, not a fabricated negative or an automatic inclusion.
    first_event['evidence_context']['ledger'].append(dict(source_id='DOC-BUDGET', disposition='omitted_budget'))
    first_event['evidence_context']['source_ledger'] = first_event['evidence_context']['ledger']
    rejected = _event('EVENT-REJECTED', sources[0], '当前决定：先行血管重建评估。', status='UNCERTAIN_REVIEW')
    episode = dict(episode_id='EPISODE-01', candidate_ids=['EVENT-01', 'EVENT-02'],
        checkpoint_ids=['EVENT-01', 'EVENT-02'], management_questions=['虚构根治路径管理任务'])
    graph = freeze_candidate_graph([first_event, second_event, rejected], [], [episode], graph_version='SYNTHETIC-GRAPH-V1',
        discovery_units=[dict(unit_id='UNIT-ZERO', source_ids=['DOC-ZERO'], events=[]),
            dict(unit_id='UNIT-REJECTED', source_ids=['DOC-01'], events=[rejected])])
    return graph, sources


def test_candidate_graph_prepares_all_targets_and_retains_omitted_sources():
    graph, sources = fictional_graph()
    tasks, failures = prepare_candidate_materials(graph, sources,
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
    assert not failures and len(tasks) == 2
    first = tasks[0]
    assert first['event']['event_id'] == 'EVENT-01'
    assert [source['source_id'] for source in first['candidate_sources']] == ['DOC-BUDGET']
    assert {source['source_id'] for source in first['excluded_sources']} == {'DOC-02', 'DOC-ZERO'}
    request = build_review_request(first['manifest'], first['compiled'],
        candidate_sources=first['candidate_sources'], excluded_sources=first['excluded_sources'], task_context=first['task_context'])
    assert len(request['researcher_sources']) + len(request['candidate_sources']) + len(request['excluded_sources']) == len(sources)
    assert request['task_context']['candidate_graph_snapshot']['events'] == graph['events']
    # Rebuilding the same task is byte-for-byte deterministic.
    repeated, _ = prepare_candidate_materials(graph, sources,
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
    assert repeated == tasks


def test_missing_source_is_local_hold_and_zero_discovery_is_retained(tmp_path):
    graph, sources = fictional_graph()
    tasks, failures = prepare_candidate_materials(graph, sources[1:],
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
    assert len(tasks) == 1 and len(failures) == 1
    assert failures[0]['event_id'] == 'EVENT-01' and failures[0]['reason_code'] == 'ASSEMBLY_SELECTED_SOURCE_MISSING'
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    for name, value in [('decision_events.json', graph['events']), ('relations.json', []),
                        ('episodes.json', graph['episodes']), ('discovery.json', dict(units=graph['discovery_units']))]:
        (candidate / name).write_text(json.dumps(value), encoding='utf-8')
    summary = assemble_candidate_case(candidate_directory=candidate, sources=sources[1:], output=tmp_path / 'private' / 'case',
        graph_version=graph['graph_version'], decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
    receipt = json.loads((tmp_path / 'private' / 'case' / 'assembly-receipt.json').read_text())
    coverage = {item['unit_id']: item for item in receipt['discovery_coverage']}
    assert coverage['UNIT-ZERO']['zero_discovery']
    assert coverage['UNIT-REJECTED']['selected_checkpoint_count'] == 0
    assert summary['executed_checkpoints'] == 0 and summary['prepared_checkpoints'] == 1 and len(summary['assembly_failures']) == 1
    assert summary['episodes'][0]['status'] == 'INCOMPLETE_OR_UNEXECUTED_CHECKPOINTS'
    assert not summary['doctor_packet_created'] and not summary['clinical_release_permitted']


def _passing_answer(task, manifest, compiled, candidates, excluded):
    segments = [dict(operation_id='KEEP-' + source['source_id'] + '-' + str(span['start']),
        operation='SET_DISPOSITION', source_id=source['source_id'],
        disposition='MASK' if '决定：' in span['text'] else 'KEEP',
        exact_quote=span['text'], reason='虚构目标选择隔离或既往依据保留')
        for source in compiled['sources'] for span in source['segments']]
    return review_for(manifest, compiled, candidates=candidates, excluded=excluded,
        operations=segments, status='AUTO_REVIEW_REVISE', task_context=task['task_context'])


def test_orchestration_multiround_episode_workbench_and_local_failure(tmp_path):
    graph, sources = fictional_graph()
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    for name, value in [('decision_events.json', graph['events']), ('relations.json', []),
                        ('episodes.json', graph['episodes']), ('discovery.json', dict(units=graph['discovery_units']))]:
        (candidate / name).write_text(json.dumps(value), encoding='utf-8')
    answers = {}
    prepared, failures = prepare_candidate_materials(graph, sources,
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
    tasks = {task['compiled']['target']['checkpoint_id']: task for task in prepared}
    current_manifest, current_compiled = {}, {}
    def controlled_ask(task_name, request, schema):
        target = request['target']['checkpoint_id']
        if target == 'EVENT-02':
            return dict(invalid='fictional local failure')
        task = tasks[target]
        if target not in answers:
            answers[target] = 'REVISED'
            return _passing_answer(task, task['manifest'], task['compiled'], task['candidate_sources'], task['excluded_sources'])
        # The independent mock reads this round's actual program binding rather
        # than guessing how source/version metadata changes affect a hash.
        return dict(format='material-review-0.3.0', review_id='SYNTHETIC-PASS-' + target,
            target=request['target'], **request['response_binding'], review_round=deepcopy(request['review_round']), reviewer_type='llm_material_review',
            dimensions=[dict(dimension=dimension, applicability='APPLICABLE', status='RESOLVED', reason='虚构新版复审')
                for dimension in ('scope', 'target', 'leakage', 'evidence', 'boundaries', 'time', 'evaluability')],
            findings=[], operations=[], status='AUTO_REVIEW_PASS', required_actions=[])
    summary = assemble_candidate_case(candidate_directory=candidate, sources=sources, output=tmp_path / 'private' / 'case',
        graph_version=graph['graph_version'], decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        ask=controlled_ask, config=dict(protocol_version='checkpoint-automated-material-review-0.3.0', remote_execution_authorized=True),
        execute=True, max_rounds=3)
    assert summary['automatic_pass_checkpoints'] == 1 and summary['hold_checkpoints'] == 1, json.dumps(summary)
    assert summary['episodes'][0]['material_set_sha256']
    passed = json.loads((tmp_path / 'private' / 'case' / 'checkpoints' / 'EVENT-01' / 'review-chain.json').read_text())
    assert passed['state'] == 'AUTO_REVIEW_PASS' and len(passed['rounds']) == 2
    failed = json.loads((tmp_path / 'private' / 'case' / 'checkpoints' / 'EVENT-02' / 'review-chain.json').read_text())
    assert failed['state'] == 'AUTO_REVIEW_HOLD' and failed['failure']['stage'] == 'RESPONSE_VALIDATION'
    assignment_path = tmp_path / 'private' / 'case' / 'checkpoints' / 'EVENT-01' / 'researcher-workbench' / 'researcher-assignment.json'
    assignment = json.loads(assignment_path.read_text())
    draft = json.loads(assignment['documents']['draft'])
    assert draft['status'] == 'PENDING' and not draft['independence_attested']
    package = dict(assignment=assignment,
        auto_review=json.loads((assignment_path.parent / 'automated-material-review.json').read_text()))
    audit = deepcopy(draft)
    audit.update(status='PASS', assessment_id='SYNTHETIC-ACTUAL-HUMAN-REVIEW', reviewer_id='SYNTHETIC-HUMAN-DECLARATION',
        reviewer_type='human', independence_attested=True, assessed_at=datetime.now(timezone.utc).isoformat(),
        checks={name: True for name in draft['checks']})
    audit['review_scope']['scope_status'] = 'COMPLETE'
    for segment in audit['segment_assessments']:
        segment['assessment'] = 'ACCEPT'
    for coverage in audit['source_coverage_assessments']:
        coverage['disposition'] = 'REVIEWED_INCLUDED' if coverage['source_id'] in audit['review_scope']['included_source_ids'] else 'REVIEWED_EXCLUDED'
        coverage['availability_basis'] = 'SYNTHETIC_HUMAN_DECLARATION_NOT_CLINICAL_CERTIFICATION'
    audit['required_actions'] = []
    response = dict(format='checkpoint-researcher-material-response-0.1.0', assignment_id=assignment['assignment_id'],
        assignment_sha256=assignment['assignment_sha256'], input_bindings=assignment['bindings'],
        audit=audit, audit_sha256=canonical_sha256(audit))
    assert receive_auto_review_submission(package, response)['status'] == 'PASS'
    assert not summary['clinical_release_permitted']


def test_orchestration_offline_and_exclusive_output(tmp_path):
    graph, sources = fictional_graph()
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    for name, value in [('decision_events.json', graph['events']), ('relations.json', []), ('episodes.json', graph['episodes'])]:
        (candidate / name).write_text(json.dumps(value), encoding='utf-8')
    output = tmp_path / 'private' / 'offline'
    def forbidden(*args):
        raise AssertionError('OFFLINE_MODEL_ADAPTER_FORBIDDEN')
    summary = assemble_candidate_case(candidate_directory=candidate, sources=sources, output=output,
        graph_version=graph['graph_version'], decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
    assert summary['executed_checkpoints'] == 0 and summary['prepared_checkpoints'] == 2
    assert (output / 'checkpoints' / 'EVENT-01' / 'preparation-receipt.json').is_file()
    with pytest.raises(ValueError, match='NEW_CASE_OUTPUT'):
        assemble_candidate_case(candidate_directory=candidate, sources=sources, output=output,
            graph_version=graph['graph_version'], decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
    with pytest.raises(ValueError, match='EXPLICIT_EXECUTE'):
        assemble_candidate_case(candidate_directory=candidate, sources=sources, output=tmp_path / 'private' / 'no-explicit-execution',
            graph_version=graph['graph_version'], decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY', ask=forbidden)


def _pass_only(request):
    return dict(format='material-review-0.3.0', review_id='SYNTHETIC-PASS-' + request['target']['checkpoint_id'],
        target=request['target'], **request['response_binding'], review_round=deepcopy(request['review_round']), reviewer_type='llm_material_review',
        dimensions=[dict(dimension=dimension, applicability='APPLICABLE', status='RESOLVED', reason='虚构独立审核通过')
            for dimension in ('scope', 'target', 'leakage', 'evidence', 'boundaries', 'time', 'evaluability')],
        findings=[], operations=[], status='AUTO_REVIEW_PASS', required_actions=[])


def _budget_case(tmp_path, maximum=1):
    graph, sources = fictional_graph()
    # Remove initial REVIEW spans so the controlled response is a valid PASS.
    for event in graph['events']:
        if event['event_id'] == 'EVENT-01':
            event['prechoice_citations']['valid'] = [deepcopy(event['choice_boundary_citations']['valid'][0])]
            event['prechoice_citations']['valid'][0].update(
                quote='虚构条件：未见远处转移。若血管可重建，可评估切除。', start=0, end=25)
    tasks, failures = prepare_candidate_materials(graph, sources,
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
    assert not failures
    # The default preparation starts with REVIEW spans. Use one actual revision
    # then PASS for budget tests; the two calls expose an intra-chain overrun.
    calls = []
    def ask(task, request, schema):
        calls.append(request['target']['checkpoint_id'])
        if len(calls) == 1:
            segments = [dict(operation_id='REVISE-' + source['source_id'] + '-' + str(span['start']),
                operation='SET_DISPOSITION', source_id=source['source_id'],
                disposition='MASK' if '决定：' in span['text'] else 'KEEP',
                exact_quote=span['text'], reason='虚构保留依据或遮蔽目标选择')
                for source in tasks[0]['compiled']['sources'] for span in source['segments']]
            answer = _passing_answer(tasks[0], tasks[0]['manifest'], tasks[0]['compiled'],
                tasks[0]['candidate_sources'], tasks[0]['excluded_sources'])
            answer['operations'] = segments
            answer.update(request['response_binding'])
            return answer
        return _pass_only(request)
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    for name, value in [('decision_events.json', graph['events']), ('relations.json', []),
                        ('episodes.json', graph['episodes']), ('discovery.json', dict(units=graph['discovery_units']))]:
        (candidate / name).write_text(json.dumps(value), encoding='utf-8')
    config = dict(protocol_version='checkpoint-automated-material-review-0.3.0', remote_execution_authorized=True)
    return graph, sources, candidate, calls, ask, config


def test_shared_model_budget_stops_inside_chain_and_preserves_local_hold(tmp_path):
    graph, sources, candidate, calls, ask, config = _budget_case(tmp_path)
    output = tmp_path / 'private' / 'budget'
    summary = assemble_candidate_case(candidate_directory=candidate, sources=sources, output=output,
        graph_version=graph['graph_version'], decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        ask=ask, config=config, execute=True, max_rounds=3, max_model_calls=1)
    assert calls == ['EVENT-01']
    assert summary['actual_uncached_model_calls'] == 1
    assert summary['model_usage']['uncached_succeeded'] == 1
    assert summary['model_usage']['denied'] == 1
    assert summary['executed_checkpoints'] == 1
    first = json.loads((output / 'checkpoints' / 'EVENT-01' / 'review-chain.json').read_text())
    assert first['state'] == 'AUTO_REVIEW_HOLD' and first['stop_reason'] == 'EXECUTION_FAILED'
    assert first['failure']['stage'] == 'PROVIDER_REQUEST'
    assert first['failure']['receipt']['provider_invocation_occurred'] is False
    second = json.loads((output / 'checkpoints' / 'EVENT-02' / 'execution-receipt.json').read_text())
    assert second['status'] == 'MODEL_BUDGET_HOLD'
    assert not (output / 'checkpoints' / 'EVENT-02' / 'review-chain.json').exists()


def test_resume_only_unfinished_targets_and_rebinds_parent_budget(tmp_path):
    graph, sources, candidate, calls, ask, config = _budget_case(tmp_path, maximum=1)
    parent_root = tmp_path / 'private' / 'parent'
    parent = assemble_candidate_case(candidate_directory=candidate, sources=sources, output=parent_root / 'case',
        graph_version=graph['graph_version'], decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        ask=ask, config=config, execute=True, max_rounds=3, max_model_calls=2)
    assert parent['executed_checkpoints'] == 1 and calls == ['EVENT-01', 'EVENT-01']
    original_summary = (parent_root / 'case' / 'summary.json').read_bytes()
    original_chain = (parent_root / 'case' / 'checkpoints' / 'EVENT-01' / 'review-chain.json').read_bytes()
    continuation = resume_candidate_case(previous_output=parent_root / 'case', output=parent_root.parent / 'parent-resume-test',
        candidate_directory=candidate, sources=sources, graph_version=graph['graph_version'],
        decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        ask=ask, config=config, execute=True, max_rounds=3, max_model_calls=2)
    assert calls == ['EVENT-01', 'EVENT-01'], calls
    assert continuation['executed_checkpoints'] == 1
    assert continuation['automatic_pass_checkpoints'] == 1
    assert continuation['unexecuted_checkpoints'] == 1
    assert continuation['continuation']['inherited_completed_checkpoint_ids'] == ['EVENT-01']
    assert continuation['model_usage']['actual_uncached_model_invocations'] == 2
    assert continuation['model_usage']['denied'] == 0
    resumed = json.loads((parent_root.parent / 'parent-resume-test' / 'case' / 'checkpoints' / 'EVENT-02' / 'execution-receipt.json').read_text())
    assert resumed['status'] == 'MODEL_BUDGET_HOLD'
    assert (parent_root / 'case' / 'summary.json').read_bytes() == original_summary
    assert (parent_root / 'case' / 'checkpoints' / 'EVENT-01' / 'review-chain.json').read_bytes() == original_chain


def test_resume_rejects_changed_graph_and_does_not_create_new_output(tmp_path):
    graph, sources, candidate, calls, ask, config = _budget_case(tmp_path, maximum=1)
    parent_root = tmp_path / 'private' / 'parent'
    assemble_candidate_case(candidate_directory=candidate, sources=sources, output=parent_root / 'case',
        graph_version=graph['graph_version'], decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
        ask=ask, config=config, execute=True, max_rounds=3, max_model_calls=2)
    changed = json.loads((candidate / 'decision_events.json').read_text())
    changed[0]['scope'] = 'changed_after_parent_freeze'
    (candidate / 'decision_events.json').write_text(json.dumps(changed), encoding='utf-8')
    output = parent_root.parent / 'parent-resume-changed'
    with pytest.raises(ValueError, match='RESUME_FROZEN_INPUT_CHANGED|RESUME_CONTRACT_CHANGED'):
        resume_candidate_case(previous_output=parent_root / 'case', output=output,
            candidate_directory=candidate, sources=sources, graph_version=graph['graph_version'],
            decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY',
            ask=ask, config=config, execute=True, max_rounds=3, max_model_calls=2)
    assert not output.exists()
