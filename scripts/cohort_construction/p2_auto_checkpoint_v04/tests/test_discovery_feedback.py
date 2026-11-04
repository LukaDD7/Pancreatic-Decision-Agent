"""Synthetic same-input discovery rechecks; fake clients never access network."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_units
from scripts.cohort_construction.p2_auto_checkpoint_v04.discovery_feedback import (
    run_discovery_feedback, validate_feedback_config,
)
from scripts.cohort_construction.p2_auto_checkpoint_v04.prompts import UNCERTAINTY_KEYS
from scripts.cohort_construction.p2_auto_checkpoint_v04.validation import resolve_citations
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_context import source


TEXT = '观察甲。决定实施甲。实施后记录乙。'


def raw_event(local_id='m1', action_quote='决定实施甲。', boundary_quote='决定实施甲。', sid='D'):
    q = lambda text: dict(source_id=sid, quote=text)
    return dict(local_id=local_id, scope='uncertain', record_function='new_decision',
        management_question='纯虚构管理问题', action='纯虚构提案', action_quotes=[q(action_quote)],
        choice_boundary_quotes=[q(boundary_quote)], prechoice_facts=[], postchoice_facts=[],
        choice_time=None, time_quotes=[], time_basis='unknown', phase='未知',
        uncertainty=['纯虚构开发材料'], rationale='用于接口反例，不是临床Reference',
        uncertainty_dimensions={key: dict(status='uncertain', reason_category='rule_ambiguity',
            explanation='纯虚构未定维度', source_quotes=[]) for key in UNCERTAINTY_KEYS})


def response(events):
    return dict(events=events, rejected_mentions=[])


def payload():
    return dict(instruction='原始发现任务', unit_id='DU-fictional',
        current_unit=dict(unit_id='DU-fictional', source_ids=['D'],
            original_texts=[dict(source_id='D', text=TEXT)]),
        prior_clinical_documents=[dict(source_id='P', text='既往纯虚构原文。')],
        prior_source_catalog=[dict(source_id='CATALOG-ONLY', title='只有元数据，没有正文')])


def compile_response(raw, receipt):
    rows = []
    sources = [dict(source_id='D', text=TEXT), dict(source_id='P', text='既往纯虚构原文。')]
    for event in raw['events']:
        rows.append(dict(local_id=event['local_id'], event_id='DE' + digest(event)[:20],
            unit_id='DU-fictional', action_citations=resolve_citations(event['action_quotes'], sources, {'D': [(0, len(TEXT))]}),
            choice_boundary_citations=resolve_citations(event['choice_boundary_quotes'], sources, {'D': [(0, len(TEXT))]})))
    return dict(unit_id='DU-fictional', events=rows, rejected_mentions=[], receipt=receipt)


class FakeClient:
    def __init__(self, revised=None, error=None, mutate=False):
        self.revised, self.error, self.mutate = revised, error, mutate
        self.calls = []

    def ask(self, task, data, schema):
        self.calls.append(dict(task=task, payload=deepcopy(data), schema=deepcopy(schema)))
        if self.error:
            raise self.error
        if self.mutate:
            data['current_unit']['original_texts'][0]['text'] = '不得替换原始资料'
        return deepcopy(self.revised), dict(request_sha256='feedback-request-fictional', status='SUCCESS')


def run(raw, revised, *, config=None, compiler=compile_response, client=None, data=None):
    initial = compile_response(raw, dict(request_sha256='initial-request-fictional'))
    client = client or FakeClient(revised)
    final, audit = run_discovery_feedback(initial, raw, data or payload(), compiler, client,
        dict(discovery_feedback_max_rounds=1) if config is None else config)
    return initial, final, audit, client


class SupportRoutingTests(unittest.TestCase):
    """CE-AUD-003: support labels may not silently prohibit discovery."""

    def test_CE_AUD_003_default_support_routing_remains_frozen_v05(self):
        s = source('S', '新情况出现，原安排暂缓。', title='护理评估记录')
        audit = []
        self.assertEqual(build_units([s], routing_audit=audit), [])
        self.assertEqual(audit[0]['disposition'], 'SUPPORT_DOCUMENT_CONTEXT_ONLY')

    def test_CE_AUD_003_explicit_support_routing_preserves_complete_source(self):
        s = source('S', '原始完整文字\r\n新情况出现，原安排暂缓。😀', title='Apache II评分')
        audit = []
        units = build_units([s], dict(include_support_documents=True), routing_audit=audit)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]['original_texts'][0]['text'], s['text'])
        self.assertEqual(units[0]['document_profile'], 'support')
        self.assertIn('不是研究相关性', units[0]['type_prior'])
        self.assertIn('零事件', units[0]['type_prior'])
        self.assertEqual(audit[0]['disposition'], 'SUPPORT_DOCUMENT_INCLUDED')
        self.assertEqual(audit[0]['unit_id'], units[0]['unit_id'])

    def test_CE_AUD_003_empty_documents_stay_out_and_have_routing_receipt(self):
        rows = [source('E', '', title='护理评估记录'), source('W', ' \r\n\t', title='查房记录')]
        audit = []
        self.assertEqual(build_units(rows, dict(include_support_documents=True), routing_audit=audit), [])
        self.assertEqual([r['source_id'] for r in audit], ['E', 'W'])
        self.assertTrue(all(r['disposition'] == 'EMPTY_DOCUMENT_NOT_DISCOVERED' for r in audit))

    def test_CE_AUD_003_support_routing_requires_boolean_config(self):
        for bad in (1, 0, 'true', None):
            with self.subTest(config=bad), self.assertRaises(ValueError):
                build_units([], dict(include_support_documents=bad))


class DiscoveryFeedbackTests(unittest.TestCase):
    """CE-AUD-007: bounded revision preserves source/context and uncertainty."""

    def test_CE_AUD_007_zero_event_recheck_can_still_return_zero(self):
        initial, final, audit, client = run(response([]), response([]))
        self.assertEqual(final['events'], [])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(audit['status'], 'REVISED_ACCEPTED')
        self.assertEqual(audit['final_event_count'], 0)
        self.assertTrue(audit['trigger_findings'][0]['not_a_positive_target'])

    def test_CE_AUD_007_zero_event_recheck_can_find_source_bound_record(self):
        _, final, audit, client = run(response([]), response([raw_event()]))
        self.assertEqual(len(final['events']), 1)
        self.assertEqual(audit['final_origin'], 'revised')
        self.assertEqual(len(client.calls), 1)

    def test_CE_AUD_007_bad_action_quote_repaired_with_same_input(self):
        original_payload = payload()
        before = deepcopy(original_payload)
        raw = response([raw_event(action_quote='原文不存在的改写')])
        initial, final, audit, client = run(raw, response([raw_event()]), data=original_payload)
        self.assertTrue(initial['events'][0]['action_citations']['invalid'])
        self.assertTrue(final['events'][0]['action_citations']['valid'])
        self.assertEqual(original_payload, before)
        sent = client.calls[0]['payload']
        self.assertEqual({key: val for key, val in sent.items() if key != 'discovery_feedback'}, original_payload)
        self.assertEqual(audit['original_response'], raw)
        self.assertEqual(audit['original_payload_sha256'], digest(original_payload))
        self.assertEqual(audit['feedback_payload_sha256'], digest(sent))
        self.assertEqual(audit['initial_request_sha256'], 'initial-request-fictional')
        self.assertEqual(audit['feedback_request_sha256'], 'feedback-request-fictional')

    def test_CE_AUD_007_bad_decision_clause_quote_triggers_repair(self):
        _, final, audit, _ = run(response([raw_event(boundary_quote='不存在的决策子句')]), response([raw_event()]))
        self.assertEqual(audit['status'], 'REVISED_ACCEPTED')
        self.assertTrue(final['events'][0]['choice_boundary_citations']['valid'])

    def test_CE_AUD_007_ambiguous_decision_clause_rechecked_without_first_match(self):
        raw = response([raw_event(boundary_quote='。')])
        initial, final, audit, _ = run(raw, response([raw_event()]))
        self.assertTrue(initial['events'][0]['choice_boundary_citations']['ambiguous'])
        self.assertIn('AMBIGUOUS_DECISION_BOUNDARY', [r['code'] for r in audit['trigger_findings']])
        self.assertEqual(audit['status'], 'REVISED_ACCEPTED')
        self.assertEqual(final['events'][0]['choice_boundary_citations']['valid'][0]['quote'], '决定实施甲。')

    def test_CE_AUD_007_disabled_does_not_call_model(self):
        initial, final, audit, client = run(response([]), response([raw_event()]), config={})
        self.assertEqual(initial, final)
        self.assertEqual(client.calls, [])
        self.assertEqual(audit['status'], 'NOT_RUN_DISABLED')

    def test_CE_AUD_007_valid_nonzero_discovery_does_not_request_feedback(self):
        initial, final, audit, client = run(response([raw_event()]), response([]))
        self.assertEqual(initial, final)
        self.assertEqual(client.calls, [])
        self.assertEqual(audit['status'], 'NOT_TRIGGERED')

    def test_CE_AUD_007_empty_decision_clause_keeps_uncertainty_without_forced_repair(self):
        event = raw_event()
        event['choice_boundary_quotes'] = []
        event['uncertainty'].append('目标临床决策子句不能确定，保留待核')
        initial, final, audit, client = run(response([event]), response([raw_event()]))
        self.assertEqual(initial, final)
        self.assertEqual(client.calls, [])
        self.assertEqual(audit['status'], 'NOT_TRIGGERED')
        self.assertEqual(final['events'][0]['choice_boundary_citations'], dict(valid=[], ambiguous=[], invalid=[]))
        self.assertNotIn('blind_replay_ready', final['events'][0])

    def test_CE_AUD_007_initial_other_quote_issues_are_not_new_feedback_clinical_gates(self):
        event = raw_event()
        event['prechoice_facts'] = [dict(source_id='UNKNOWN', quote='不可支持的其他引文')]
        initial, final, audit, client = run(response([event]), response([]))
        self.assertEqual(initial, final)
        self.assertEqual(client.calls, [])
        self.assertEqual(audit['status'], 'NOT_TRIGGERED')

    def test_CE_AUD_007_feedback_limit_is_strict_zero_or_one(self):
        for bad in (False, True, '1', 1.0, -1, 2, None):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                validate_feedback_config(dict(discovery_feedback_max_rounds=bad))
        self.assertEqual(validate_feedback_config({}), 0)
        self.assertEqual(validate_feedback_config(dict(discovery_feedback_max_rounds=1)), 1)

    def test_CE_AUD_007_duplicate_initial_local_id_triggers_recheck(self):
        raw = response([raw_event(), raw_event()])
        _, final, audit, client = run(raw, response([raw_event(local_id='fixed')]))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(final['events']), 1)
        self.assertIn('DUPLICATE_DISCOVERY_LOCAL_ID', [r['code'] for r in audit['trigger_findings']])

    def test_CE_AUD_007_duplicate_revised_local_id_retains_initial(self):
        raw = response([])
        initial, final, audit, _ = run(raw, response([raw_event(), raw_event()]))
        self.assertEqual(initial, final)
        self.assertEqual(audit['rejection_reason'], 'DUPLICATE_REVISED_LOCAL_ID')

    def test_CE_AUD_007_unknown_or_catalog_only_quote_source_retains_initial(self):
        for sid in ('UNKNOWN', 'CATALOG-ONLY'):
            with self.subTest(source_id=sid):
                initial, final, audit, _ = run(response([]), response([raw_event(sid=sid)]))
                self.assertEqual(initial, final)
                self.assertEqual(audit['rejection_reason'], 'REVISED_QUOTE_SOURCE_NOT_IN_ORIGINAL_TEXT_INPUT')

    def test_CE_AUD_007_action_quote_cannot_create_event_in_prior_document(self):
        initial, final, audit, _ = run(response([]), response([raw_event(sid='P')]))
        self.assertEqual(initial, final)
        self.assertEqual(audit['rejection_reason'], 'REVISED_ACTION_OR_BOUNDARY_OUTSIDE_CURRENT_UNIT')

    def test_CE_AUD_007_remaining_mechanical_error_retains_initial_without_third_call(self):
        raw = response([raw_event(action_quote='最初错误')])
        revised = response([raw_event(action_quote='修订仍然错误')])
        initial, final, audit, client = run(raw, revised)
        self.assertEqual(initial, final)
        self.assertEqual(audit['status'], 'REJECTED_UNRESOLVED_MECHANICAL_FINDINGS')
        self.assertEqual(audit['revised_response'], revised)
        self.assertEqual(len(client.calls), 1)

    def test_CE_AUD_007_provider_error_preserves_initial_and_sanitizes_message(self):
        client = FakeClient(error=RuntimeError('do-not-copy-secret-or-clinical-provider-text'))
        initial, final, audit, _ = run(response([]), None, client=client)
        self.assertEqual(initial, final)
        self.assertEqual(audit['error_type'], 'RuntimeError')
        self.assertNotIn('do-not-copy-secret', json.dumps(audit))

    def test_CE_AUD_007_bound_failure_request_hash_is_recorded(self):
        error = RuntimeError('provider-text-is-not-logged')
        error.request_sha256 = 'this-failed-request'
        initial, final, audit, _ = run(response([]), None, client=FakeClient(error=error))
        self.assertEqual(initial, final)
        self.assertEqual(audit['feedback_request_sha256'], 'this-failed-request')

    def test_CE_AUD_007_invalid_schema_or_compile_exception_retains_initial(self):
        initial, final, audit, _ = run(response([]), {'events': [{}]})
        self.assertEqual(initial, final)
        self.assertEqual(audit['error_type'], 'ValidationError')
        def fail_compile(raw, receipt):
            raise ValueError('compile-error')
        initial, final, audit, _ = run(response([]), response([raw_event()]), compiler=fail_compile)
        self.assertEqual(initial, final)
        self.assertEqual(audit['error_type'], 'ValueError')

    def test_CE_AUD_007_client_context_mutation_rejected_and_input_unchanged(self):
        original_payload = payload()
        before = deepcopy(original_payload)
        initial, final, audit, _ = run(response([]), response([raw_event()]), data=original_payload,
                                      client=FakeClient(response([raw_event()]), mutate=True))
        self.assertEqual(initial, final)
        self.assertEqual(original_payload, before)
        self.assertEqual(audit['status'], 'REJECTED_CONTEXT_MUTATION')
        self.assertFalse(audit['context_preserved'])

    def test_CE_AUD_007_changed_compiled_unit_or_duplicate_event_id_rejected(self):
        def changed_unit(raw, receipt):
            compiled = compile_response(raw, receipt)
            compiled['unit_id'] = 'OTHER-UNIT'
            return compiled
        initial, final, audit, _ = run(response([]), response([raw_event()]), compiler=changed_unit)
        self.assertEqual(initial, final)
        self.assertEqual(audit['rejection_reason'], 'COMPILED_UNIT_ID_CHANGED')
        def duplicate_id(raw, receipt):
            compiled = compile_response(raw, receipt)
            compiled['events'].append(deepcopy(compiled['events'][0]))
            return compiled
        initial, final, audit, _ = run(response([]), response([raw_event()]), compiler=duplicate_id)
        self.assertEqual(initial, final)
        self.assertEqual(audit['rejection_reason'], 'DUPLICATE_COMPILED_EVENT_ID')


class DiscoveryIssueMapTests(unittest.TestCase):
    def test_discovery_issue_map_required_tests_exist(self):
        registry = json.loads(Path(__file__).with_name('discovery_feedback_issue_map_v06.json').read_text())
        by_name = {name: getattr(cls, name) for cls in (SupportRoutingTests, DiscoveryFeedbackTests)
                   for name in unittest.defaultTestLoader.getTestCaseNames(cls)}
        registered = {test for issue in registry['issues'] for test in issue['required_tests']}
        self.assertEqual(registered, set(by_name))


class EngineDiscoveryIntegrationTests(unittest.TestCase):
    def test_real_engine_recompiles_revised_anchors_before_event_context(self):
        from scripts.cohort_construction.p2_auto_checkpoint_v04.engine import (
            compile_discovery_response, discover_unit,
        )
        from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_evidence_context
        s = source('D', TEXT)
        unit = build_units([s])[0]
        raw = response([raw_event(action_quote='观察甲。决定实施甲。', boundary_quote='不存在的子句')])
        revised = response([raw_event()])
        class SequentialClient:
            def __init__(self):
                self.calls = []
            def ask(self, task, data, schema):
                self.calls.append(task)
                return deepcopy(raw if len(self.calls) == 1 else revised), dict(request_sha256='request-' + str(len(self.calls)))
        client = SequentialClient()
        old = compile_discovery_response(unit, [s], raw, [], dict(request_sha256='request-1'))
        final = discover_unit(unit, [s], dict(discovery_context_characters=10000, discovery_feedback_max_rounds=1), client, [])
        self.assertEqual(client.calls, ['document_event_discovery', 'document_event_discovery_feedback'])
        self.assertEqual(final['discovery_feedback_audit']['status'], 'REVISED_ACCEPTED')
        self.assertNotEqual(final['events'][0]['event_id'], old['events'][0]['event_id'])
        self.assertEqual(final['events'][0]['action_citations']['valid'][0]['quote'], '决定实施甲。')
        self.assertIn('input_context_summary', final)
        context = build_evidence_context(final['events'][0], [s], final['events'])
        self.assertNotIn('不存在的子句', json.dumps(context, ensure_ascii=False))
        self.assertNotIn(old['events'][0]['event_id'], json.dumps(context, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
