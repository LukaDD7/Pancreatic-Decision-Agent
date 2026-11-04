"""Synthetic full-material, revision and Episode governance regression tests."""
from copy import deepcopy
import unittest

from scripts.cohort_construction.p2_checkpoint_packet.compiler import (
    PLACEHOLDER, build_source_manifest, compile_partition,
)
from scripts.cohort_construction.p2_checkpoint_packet.semantic_audit import validate_semantic_audit
from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import bind_candidate_graph
from scripts.cohort_construction.p2_checkpoint_material_governance.episode import (
    build_episode_material_set, graph_revision_handoff,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.review import (
    AUTO_PROTOCOL_VERSION, DIMENSIONS, MaterialReviewError, build_review_request,
    doctor_view, run_review_chain, validate_material_review,
)


TEXT = '虚构既往检查未见远处转移。若未累及重要血管，可评估根治性切除。故拟行根治性切除术。'


def inputs(text=TEXT):
    manifest = build_source_manifest('SM-synthetic', [dict(source_id='doc1', source_version='v1', text=text)])
    target = dict(checkpoint_id='cp1', decision_task_id='task1', visibility_policy_version='policy1')
    proposal = dict(schema_version='0.1.0', proposal_id='SPL-initial', proposal_version='0.1.0',
                    manifest_id=manifest['manifest_id'], target=target,
                    source_bindings=[dict(source_id='doc1', source_version='v1', sha256=manifest['sources'][0]['sha256'])],
                    segments=[dict(segment_id='old', source_id='doc1', disposition='REVIEW',
                                   resolved_span=dict(start=0, end=len(text), exact_quote=text))])
    return manifest, compile_partition(manifest, proposal)


def review_for(manifest, compiled, *, candidates=(), excluded=(), operations=(), status='AUTO_REVIEW_REVISE',
               blocking=False, task_context=None):
    request = build_review_request(manifest, compiled, candidate_sources=candidates, excluded_sources=excluded, task_context=task_context)
    return dict(format='material-review-0.3.0', review_id='MR-synthetic-1', target=compiled['target'],
        review_round=deepcopy(request['review_round']),
        protocol_version=AUTO_PROTOCOL_VERSION, compiled_sha256=compiled['compiled_sha256'],
        doctor_view_sha256=request['doctor_view']['view_sha256'], request_sha256=request['response_binding']['request_sha256'],
        reviewer_type='llm_material_review',
        dimensions=[dict(dimension=d, applicability='APPLICABLE', status='OPEN' if blocking else 'RESOLVED', reason='虚构审核维度') for d in DIMENSIONS],
        findings=[dict(finding_id='F1', dimension='boundaries', severity='BLOCKING' if blocking else 'NON_BLOCKING', status='OPEN' if blocking else 'RESOLVED', reason='虚构反例')],
        operations=list(operations), status=status,
        required_actions=['等待人工或重建处理'] if blocking else [])


def operation(quote, disposition='KEEP', source='doc1'):
    return dict(operation_id='OP-' + quote[:3], operation='SET_DISPOSITION', source_id=source,
                reason='保留虚构的当前决策前证据或遮蔽目标选择', disposition=disposition, exact_quote=quote)


class FullViewTests(unittest.TestCase):
    def test_preview_masks_review_and_does_not_disclose_original(self):
        manifest, compiled = inputs()
        view = doctor_view(compiled)
        self.assertEqual(view['materials'][0]['text'], PLACEHOLDER)
        self.assertNotIn(TEXT, view['materials'][0]['text'])
        request = build_review_request(manifest, compiled)
        self.assertEqual(request['researcher_sources'][0]['text'], TEXT)
        self.assertNotIn('doctor_view_sha256', request['doctor_view'])
        self.assertFalse(request['graph_is_candidate_not_clinical_reference'] is False)

    def test_all_dimensions_and_binding_required(self):
        manifest, compiled = inputs()
        answer = review_for(manifest, compiled, operations=[operation(TEXT)], status='AUTO_REVIEW_PASS')
        with self.assertRaisesRegex(MaterialReviewError, 'PASS_CANNOT'):
            validate_material_review(manifest, compiled, answer)
        answer['compiled_sha256'] = '0' * 64
        with self.assertRaisesRegex(MaterialReviewError, 'INPUT_BINDING'):
            validate_material_review(manifest, compiled, answer)

    def test_source_scope_is_explicit_and_no_inferred_clinical_visibility(self):
        manifest, compiled = inputs()
        candidate = dict(source_id='later', source_version='v1', text='虚构后续复查资料。')
        request = build_review_request(manifest, compiled, candidate_sources=[candidate])
        self.assertEqual(request['candidate_sources'][0]['scope_status'], 'CANDIDATE_NOT_INCLUDED')
        self.assertFalse(request['task_context']['clinical_visibility_certified'])


class RevisionTests(unittest.TestCase):
    def test_split_preserves_negation_condition_and_masks_target(self):
        manifest, compiled = inputs()
        answer = review_for(manifest, compiled, operations=[
            operation('虚构既往检查未见远处转移。'), operation('若未累及重要血管，可评估根治性切除。'),
            operation('故拟行根治性切除术。', 'MASK')])
        plan = validate_material_review(manifest, compiled, answer)
        self.assertTrue(plan['changed'])
        self.assertNotEqual(plan['input_compiled_sha256'], plan['compiled']['compiled_sha256'])
        self.assertEqual([s['disposition'] for s in plan['compiled']['sources'][0]['segments']], ['KEEP', 'KEEP', 'MASK'])
        self.assertEqual(''.join(s['text'] for s in plan['compiled']['sources'][0]['segments']), TEXT)
        self.assertEqual(plan['compiled']['sources'][0]['segments'][-1]['text'], '故拟行根治性切除术。')
        self.assertFalse(plan['event_graph_modified'])
        self.assertFalse(plan['clinical_release_permitted'])

    def test_ambiguous_quote_is_rejected_not_first_match(self):
        manifest, compiled = inputs('虚构重复。后续。虚构重复。')
        answer = review_for(manifest, compiled, operations=[operation('虚构重复。')])
        with self.assertRaisesRegex(MaterialReviewError, 'NOT_UNIQUE'):
            validate_material_review(manifest, compiled, answer)

    def test_CE_AMG025_disposition_quote_expands_context_not_first_match(self):
        manifest, compiled = inputs('虚构重复。后续。虚构重复。')
        answer = review_for(manifest, compiled, operations=[
            operation('虚构重复。后续。虚构重复。', 'MASK')])
        plan = validate_material_review(manifest, compiled, answer)
        self.assertEqual(plan['compiled']['sources'][0]['segments'][0]['text'],
                         '虚构重复。后续。虚构重复。')
        self.assertEqual(plan['compiled']['sources'][0]['segments'][0]['disposition'], 'MASK')

    def test_CE_AMG026_local_material_revisions_continue_until_fresh_pass(self):
        manifest, compiled = inputs()
        answers = [review_for(manifest, compiled, operations=[operation(TEXT)]),
                   review_for(manifest, compiled, operations=[operation(TEXT)], status='AUTO_REVIEW_PASS')]
        calls = []
        def ask(task, request, schema):
            calls.append(request['response_binding']['compiled_sha256'])
            answer = deepcopy(answers[len(calls) - 1])
            answer.update(request['response_binding'])
            answer['review_round'] = deepcopy(request['review_round'])
            answer['doctor_view_sha256'] = request['doctor_view']['view_sha256']
            return answer
        chain = run_review_chain(manifest, compiled, ask, max_rounds=3)
        self.assertEqual(chain['state'], 'AUTO_REVIEW_PASS')
        self.assertEqual(chain['stop_reason'], 'AUTO_PASS')
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        self.assertFalse(chain['clinical_release_permitted'])

    def test_CE_AMG026_unresolved_human_or_graph_issue_still_stops(self):
        manifest, compiled = inputs()
        answer = review_for(manifest, compiled, status='AUTO_REVIEW_HOLD', blocking=True,
            operations=[dict(operation_id='H1', operation='REQUEST_HUMAN_REVIEW',
                source_id='doc1', reason='虚构重大未决问题')])
        chain = run_review_chain(manifest, compiled, lambda *args: answer, max_rounds=3)
        self.assertEqual(chain['stop_reason'], 'REVIEW_HOLD')
        self.assertEqual(len(chain['rounds']), 1)
        self.assertTrue(chain['rounds'][0]['ledger']['human_requests'])

    def test_unauthorized_source_and_all_excluded_are_rejected(self):
        manifest, compiled = inputs()
        answer = review_for(manifest, compiled, operations=[dict(operation_id='O1', operation='INCLUDE_SOURCE',
            source_id='not-supplied', reason='越权来源')])
        with self.assertRaisesRegex(MaterialReviewError, 'UNAUTHORIZED'):
            validate_material_review(manifest, compiled, answer)
        answer = review_for(manifest, compiled, operations=[dict(operation_id='O1', operation='EXCLUDE_SOURCE',
            source_id='doc1', reason='排除唯一来源')])
        with self.assertRaisesRegex(MaterialReviewError, 'ALL_MATERIAL'):
            validate_material_review(manifest, compiled, answer)

    def test_source_change_creates_new_manifest_review_not_permission(self):
        manifest, compiled = inputs()
        candidate = dict(source_id='earlier', source_version='v1', text='虚构既往检查已报告。')
        answer = review_for(manifest, compiled, candidates=[candidate], operations=[dict(operation_id='O1', operation='INCLUDE_SOURCE',
            source_id='earlier', reason='需要核对既往资料')])
        plan = validate_material_review(manifest, compiled, answer, candidate_sources=[candidate])
        self.assertNotEqual(plan['manifest']['manifest_id'], manifest['manifest_id'])
        self.assertEqual(plan['compiled']['sources'][-1]['segments'][0]['disposition'], 'REVIEW')
        self.assertEqual(plan['compiled']['semantic_audit_status'], 'NOT_ASSESSED')
        with self.assertRaisesRegex(Exception, 'BINDING'):
            validate_material_review(plan['manifest'], plan['compiled'], answer)

    def test_pass_cannot_hide_revision_unresolved_or_open_finding(self):
        manifest, compiled = inputs()
        # First resolve the entire source, then pass only on that new hash.
        revision = review_for(manifest, compiled, operations=[operation(TEXT, 'MASK')])
        plan = validate_material_review(manifest, compiled, revision)
        passing = review_for(plan['manifest'], plan['compiled'], status='AUTO_REVIEW_PASS')
        passed = validate_material_review(plan['manifest'], plan['compiled'], passing)
        self.assertFalse(passed['changed'])
        passing['operations'] = [operation('虚构既往检查未见远处转移。', 'MASK')]
        with self.assertRaisesRegex(MaterialReviewError, 'PASS_CANNOT'):
            validate_material_review(plan['manifest'], plan['compiled'], passing)
        passing = review_for(plan['manifest'], plan['compiled'], status='AUTO_REVIEW_PASS', blocking=True)
        passing['required_actions'] = []  # Isolate the original unresolved finding/dimension guard.
        with self.assertRaisesRegex(MaterialReviewError, 'PASS_WITH'):
            validate_material_review(plan['manifest'], plan['compiled'], passing)

    def test_noop_revise_and_human_review_are_not_acceptance(self):
        manifest, compiled = inputs()
        answer = review_for(manifest, compiled, operations=[])
        with self.assertRaisesRegex(MaterialReviewError, 'ACTUAL_NEW_PARTITION'):
            validate_material_review(manifest, compiled, answer)
        answer = review_for(manifest, compiled, status='AUTO_REVIEW_HOLD', operations=[dict(
            operation_id='O1', operation='REQUEST_HUMAN_REVIEW', source_id='doc1', reason='不能确定')])
        plan = validate_material_review(manifest, compiled, answer)
        self.assertFalse(plan['human_review_created'])
        self.assertFalse(plan['clinical_release_permitted'])

    def test_automatic_review_is_never_human_semantic_pass(self):
        manifest, compiled = inputs()
        answer = review_for(manifest, compiled, status='AUTO_REVIEW_HOLD', blocking=True)
        self.assertEqual(answer['reviewer_type'], 'llm_material_review')
        self.assertNotIn('independence_attested', answer)
        with self.assertRaisesRegex(Exception, 'SemanticAudit'):
            validate_semantic_audit(compiled, answer)


class ChainTests(unittest.TestCase):
    def test_two_round_revision_then_pass(self):
        manifest, compiled = inputs()
        first = review_for(manifest, compiled, operations=[operation(TEXT, 'KEEP')])
        plan = validate_material_review(manifest, compiled, first)
        second = review_for(plan['manifest'], plan['compiled'], status='AUTO_REVIEW_PASS')
        second['review_round'] = dict(round_index=2, max_rounds=3, is_first_review=False,
            current_state_produced_by_previous_review=True)
        answers = iter([(first, {'cached': False}), (second, {'cached': True})])
        def respond(task, request, schema):
            answer = next(answers)[0]
            answer.update(request['response_binding'])
            answer['review_round'] = deepcopy(request['review_round'])
            return answer
        result = run_review_chain(manifest, compiled, respond, max_rounds=3)
        self.assertEqual(result['state'], 'AUTO_REVIEW_PASS')
        self.assertEqual(len(result['rounds']), 2)
        self.assertTrue(result['rounds'][0]['compiled_changed_requires_new_review'])
        self.assertFalse(result['clinical_release_permitted'])

    def test_revision_cycle_and_budget_hold(self):
        manifest, compiled = inputs()
        candidate = dict(source_id='later', source_version='v1', text='虚构后续资料。')
        candidate_answer = review_for(manifest, compiled, candidates=[candidate],
            operations=[dict(operation_id='cycle', operation='INCLUDE_SOURCE', source_id='later', reason='虚构纳入')])
        def cycle(task, request, schema):
            answer = deepcopy(candidate_answer)
            answer.update(request['response_binding'])
            answer['review_round'] = deepcopy(request['review_round'])
            return answer
        result = run_review_chain(manifest, compiled, cycle, candidate_sources=[candidate], max_rounds=3)
        self.assertEqual(result['stop_reason'], 'REVISION_CYCLE')
        self.assertEqual(len(result['rounds']), 2)
        self.assertTrue(result['rounds'][0]['diff']['source_scope_changed'])
        answers = iter([review_for(manifest, compiled, operations=[operation(TEXT, 'KEEP')])])
        first_request = build_review_request(manifest, compiled, max_rounds=1)
        first_answer = deepcopy(next(answers))
        first_answer.update(first_request['response_binding'])
        first_answer['review_round'] = deepcopy(first_request['review_round'])
        result = run_review_chain(manifest, compiled, lambda *args: deepcopy(first_answer), max_rounds=1)
        self.assertEqual(result['stop_reason'], 'BUDGET_EXHAUSTED')


class EpisodeTests(unittest.TestCase):
    def test_episode_versions_and_graph_handoff(self):
        manifest, compiled = inputs()
        graph = dict(events=[dict(event_id='event1')], episodes=[dict(episode_id='ep1', event_ids=['event1'])])
        context = bind_candidate_graph(graph, graph_version='v1', target_event_id='event1', checkpoint_id='cp1')
        answer = review_for(manifest, compiled, status='AUTO_REVIEW_HOLD', blocking=True, task_context=context)
        chain = run_review_chain(manifest, compiled, lambda *args: answer, task_context=context)
        item = dict(manifest=manifest, compiled=compiled, doctor_view=doctor_view(compiled),
                    auto_review_chain=chain, revision_ledger=[r['ledger'] for r in chain['rounds']],
                    state=chain['state'], event_reference='event1')
        result = build_episode_material_set(dict(episode_id='ep1', candidate_version='v1',
            input_sha256=__import__('scripts.cohort_construction.p2_checkpoint_packet.compiler',
                fromlist=['canonical_sha256']).canonical_sha256(graph), graph_snapshot=graph), [item])
        self.assertFalse(result['clinical_release_permitted'])
        self.assertFalse(result['event_graph_modified'])
        handoff = graph_revision_handoff(result, 'cp1', 'MR1', '虚构误拆事件需重建')
        self.assertFalse(handoff['material_layer_may_change_event_graph'])
        self.assertFalse(handoff['previous_material_pass_transferable'])
        self.assertIn('rerun_final_graph_dependency_audit', handoff['required_reconstruction_steps'])


if __name__ == '__main__':
    unittest.main()
