"""Fictional controls for independent graph and actual-delivery audits."""
from copy import deepcopy
import unittest

from scripts.cohort_construction.p2_checkpoint_fhir.doctor import doctor_bundle_from_packet
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import fictional_graph
from scripts.cohort_construction.p2_checkpoint_packet.tests.test_compiler import packet
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit import (
    DIMENSIONS, GRAPH_AUDIT_SCHEMA, build_graph_audit_request, reconstruction_handoff,
    validate_graph_audit, run_graph_audit_chain, validate_graph_audit_chain,
)
from scripts.cohort_construction.p2_checkpoint_review_app.build import build_reviewer_html


def passing_graph_response(request):
    return dict(format='candidate-reconstruction-audit-0.2.0' if request['protocol_version'].endswith('0.2.0')
        else 'candidate-reconstruction-audit-0.1.0', review_id='SYNTHETIC-GRAPH-AUDIT',
        target=dict(graph_sha256=request['response_binding']['graph_sha256']), **request['response_binding'],
        reviewer_type='llm_candidate_reconstruction_audit',
        dimensions=[dict(dimension=dimension, applicability='APPLICABLE', status='RESOLVED', reason='虚构独立审计')
                    for dimension in DIMENSIONS],
        findings=[], revision_requests=[], status='AUTO_GRAPH_AUDIT_PASS', required_actions=[])


class ReconstructionAuditTests(unittest.TestCase):
    def test_full_scope_and_upstream_proposals_are_bound(self):
        graph, sources = fictional_graph()
        units = [dict(unit_id='ALL', source_ids=[source['source_id'] for source in sources])]
        request = build_graph_audit_request(graph, sources, processing_units=units,
            discovery_proposals=[dict(unit_id='ALL', synthetic=True)],
            review_proposals=[dict(proposal_id='SYNTHETIC-REVIEW')],
            feedback_history=[dict(round=1, synthetic=True)])
        self.assertTrue(request['audit_scope']['source_coverage_complete'])
        self.assertFalse(request['audit_scope']['whole_patient_chart_certified'])
        self.assertEqual(request['zero_discovery_and_rejected_units'], graph['discovery_units'])
        response = passing_graph_response(request)
        self.assertEqual(validate_graph_audit(request, response)['status'], 'AUTO_GRAPH_AUDIT_PASS')
        handoff = reconstruction_handoff(request, response)
        self.assertFalse(handoff['fresh_graph_material_and_delivery_reaudit_required'])

    def test_missed_radical_pathway_needs_evidence_and_new_graph_restarts(self):
        graph, sources = fictional_graph()
        request = build_graph_audit_request(graph, sources,
            processing_units=[dict(unit_id='ALL', source_ids=[s['source_id'] for s in sources])])
        response = passing_graph_response(request)
        response.update(status='AUTO_GRAPH_AUDIT_REVISE', findings=[dict(finding_id='MISSED-EVENT',
            dimension='radical_pathway_relevance', severity='BLOCKING', status='OPEN',
            reason='虚构反例：程序未纳入暂缓切除并补充证据的复评',
            evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
            affected_event_ids=['EVENT-02'], affected_episode_ids=['EPISODE-01'])],
            revision_requests=[dict(request_id='RECONSTRUCT-01', request_kind='EVENT_RECONSTRUCTION',
                reason='回到独立重建层核对事件粒度', affected_event_ids=['EVENT-02'],
                affected_episode_ids=['EPISODE-01'],
                evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
                required_reconstruction=True)])
        validated = validate_graph_audit(request, response)
        self.assertEqual(validated['findings'][0]['evidence'][0]['exact_quote'], '决定：暂缓切除并补充病理证据。')
        handoff = reconstruction_handoff(request, validated, output_graph=dict(graph_version='SYNTHETIC-GRAPH-V2'))
        self.assertTrue(handoff['fresh_graph_material_and_delivery_reaudit_required'])
        self.assertFalse(handoff['old_material_pass_transferable'])

    def test_unverifiable_quote_and_empty_pass_are_rejected(self):
        graph, sources = fictional_graph()
        request = build_graph_audit_request(graph, sources,
            processing_units=[dict(unit_id='ALL', source_ids=[s['source_id'] for s in sources])])
        response = passing_graph_response(request)
        response['findings'] = [dict(finding_id='BAD-QUOTE', dimension='event_identity', severity='NON_BLOCKING',
            status='RESOLVED', reason='不存在于原文的判断', affected_event_ids=[], affected_episode_ids=[],
            evidence=[dict(source_id='DOC-01', exact_quote='原文没有这句')])]
        with self.assertRaisesRegex(ValueError, 'QUOTE_NOT_UNIQUE'):
            validate_graph_audit(request, response)
        incomplete = build_graph_audit_request(graph, sources, processing_units=[])
        with self.assertRaisesRegex(ValueError, 'UNRESOLVED_PASS'):
            validate_graph_audit(incomplete, passing_graph_response(incomplete))
        repeated = passing_graph_response(request)
        repeated['findings'] = [dict(finding_id='REPEATED-QUOTE', dimension='event_identity',
            severity='NON_BLOCKING', status='OPEN', reason='虚构重复短句不能任意选定位置',
            affected_event_ids=[], affected_episode_ids=[], evidence=[dict(
                source_id='DOC-01', exact_quote='决定')])]
        sources_with_repeat = deepcopy(sources)
        sources_with_repeat[0]['text'] += '\n决定'
        sources_with_repeat[0].pop('sha256', None)
        sources_with_repeat[0].pop('source_sha256', None)
        repeated_request = build_graph_audit_request(graph, sources_with_repeat,
            processing_units=[dict(unit_id='ALL', source_ids=[s['source_id'] for s in sources_with_repeat])])
        repeated.update(repeated_request['response_binding'], target=dict(
            graph_sha256=repeated_request['response_binding']['graph_sha256']))
        with self.assertRaisesRegex(ValueError, 'QUOTE_NOT_UNIQUE'):
            validate_graph_audit(repeated_request, repeated)
        # Extending the same verbatim quote with adjacent context, rather than
        # silently choosing first/latest, is the valid disambiguation contract.
        source = sources_with_repeat[0]
        prefix = source['text'][:source['text'].index('决定')]
        repeated['findings'][0]['evidence'][0]['exact_quote'] = prefix + '决定'
        self.assertEqual(validate_graph_audit(repeated_request, repeated)['findings'][0]['finding_id'], 'REPEATED-QUOTE')


    def test_bounded_graph_chain_replay_and_reconstruction_restart(self):
        graph, sources = fictional_graph()
        units = [dict(unit_id='ALL', source_ids=[s['source_id'] for s in sources])]
        request = build_graph_audit_request(graph, sources, processing_units=units)
        revise = passing_graph_response(request)
        revise.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(request_id='EPISODE-REPAIR',
            request_kind='EPISODE_RECONSTRUCTION', reason='虚构程序错误合并Episode的反例',
            affected_event_ids=['EVENT-01', 'EVENT-02'], affected_episode_ids=['EPISODE-01'],
            evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')], required_reconstruction=True)],
            required_actions=['独立重建事件与Episode，随后对新图重新审计'])
        journal = []
        chain = run_graph_audit_chain(request, lambda task, payload, schema: revise,
            journal=lambda stage, index, value: journal.append((stage, index)))
        self.assertEqual(chain['state'], 'AUTO_GRAPH_AUDIT_REVISE')
        self.assertEqual(chain['stop_reason'], 'RECONSTRUCTION_REQUIRED')
        self.assertIsNotNone(chain['handoff'])
        self.assertEqual(chain['handoff']['required_actions'], revise['required_actions'])
        self.assertFalse(chain['handoff']['old_material_pass_transferable'])
        self.assertFalse(chain['handoff']['clinical_release_permitted'])
        self.assertFalse(chain['handoff']['old_material_pass_transferable'])
        self.assertFalse(chain['graph_modified_by_audit'])
        self.assertTrue(chain['fresh_graph_material_and_delivery_reaudit_required'])
        self.assertEqual(journal, [('REQUEST', 1), ('ROUND', 1)])
        replay = validate_graph_audit_chain(chain)
        self.assertEqual(replay['status'], 'PASS')
        self.assertFalse(replay['model_call_performed'])
        # Audit history cannot be redirected to a new graph by relabelling its tail.
        changed = deepcopy(chain)
        changed['graph_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'CHAIN_HASH_MISMATCH|INITIAL_INPUT_INVALID'):
            validate_graph_audit_chain(changed)
        # A real reconstruction handoff creates a fresh request rather than an
        # unbounded conversation on the superseded graph.
        rebuilt = deepcopy(graph)
        rebuilt['graph_version'] = 'SYNTHETIC-GRAPH-V2'
        rebuilt['graph_sha256'] = __import__('scripts.cohort_construction.p2_checkpoint_packet.compiler', fromlist=['canonical_sha256']).canonical_sha256(
            {key: value for key, value in rebuilt.items() if key != 'graph_sha256'})
        next_request = build_graph_audit_request(rebuilt, sources, processing_units=units)
        self.assertNotEqual(next_request['response_binding']['request_sha256'], request['response_binding']['request_sha256'])
        with self.assertRaisesRegex(ValueError, 'RESPONSE_BINDING_MISMATCH'):
            validate_graph_audit(next_request, passing_graph_response(request))
        passed = run_graph_audit_chain(next_request, lambda task, payload, schema: passing_graph_response(payload))
        self.assertEqual(validate_graph_audit_chain(passed)['state'], 'AUTO_GRAPH_AUDIT_PASS')

    def test_failed_graph_review_cannot_become_an_empty_pass(self):
        graph, sources = fictional_graph()
        request = build_graph_audit_request(graph, sources,
            processing_units=[dict(unit_id='ALL', source_ids=[s['source_id'] for s in sources])])
        journal = []
        def fail(task, payload, schema):
            from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.chain import FORMAT
            error = RuntimeError('synthetic provider failure')
            error.receipt = dict(invocation_id='SYNTHETIC-GRAPH-AUDIT',
                legacy_chain_format=FORMAT)
            raise error
        chain = run_graph_audit_chain(request, fail,
            journal=lambda stage, index, value: journal.append(stage))
        self.assertEqual(chain['state'], 'AUTO_GRAPH_AUDIT_HOLD')
        self.assertFalse(chain['rounds'])
        self.assertEqual(journal, ['REQUEST', 'FAILURE'])
        self.assertEqual(validate_graph_audit_chain(chain)['stop_reason'], 'EXECUTION_FAILED')

    def test_failure_evidence_preserves_json_schema_http_and_whitespace_quote_details(self):
        import json
        from unittest.mock import Mock, patch
        import requests
        from scripts.cohort_construction.model_api import ModelAPISettings
        from scripts.cohort_construction.p2_auto_checkpoint_v04.client import Client, _thread
        graph, sources = fictional_graph()
        request = build_graph_audit_request(graph, sources,
            processing_units=[dict(unit_id='ALL', source_ids=[s['source_id'] for s in sources])])
        config = dict(model_temperature=0, max_output_tokens=64,
            max_http_attempts_per_request=1, retry_backoff_base_seconds=1)
        settings = ModelAPISettings('credential-sentinel', 'https://example.invalid/v1', 'synthetic-model')

        def execute(response):
            session = Mock(); session.post.return_value = response
            with patch.object(_thread, 'session', session, create=True):
                provider = Client(self.enterContext(__import__('tempfile').TemporaryDirectory()),
                    config, settings)
                return run_graph_audit_chain(request,
                    lambda task, payload, schema: provider.ask(task, payload, schema))

        malformed = Mock(); malformed.text = '{"status":\n  broken'
        malformed.json.side_effect = lambda: json.loads(malformed.text)
        chain = execute(malformed)
        failure = chain['failure']
        self.assertEqual(failure['stage'], 'PROVIDER_ENVELOPE_PARSE')
        diagnostic = failure['provider_attempt_evidence']['diagnostic']
        self.assertEqual((diagnostic['line'], diagnostic['column'], diagnostic['position']),
            (failure['receipt']['attempts'][0]['line'], failure['receipt']['attempts'][0]['column'],
             failure['receipt']['attempts'][0]['position']))
        self.assertTrue(validate_graph_audit_chain(chain)['local_failure_recomputed'])
        tampered = deepcopy(chain)
        tampered['failure']['provider_attempt_evidence']['provider_response_text'] += 'changed'
        tampered['chain_sha256'] = __import__(
            'scripts.cohort_construction.p2_checkpoint_packet.compiler', fromlist=['canonical_sha256']
        ).canonical_sha256({key: value for key, value in tampered.items() if key != 'chain_sha256'})
        with self.assertRaisesRegex(ValueError, 'CHAIN_EVIDENCE_HASH_MISMATCH'):
            validate_graph_audit_chain(tampered)

        invalid_enum = passing_graph_response(request)
        invalid_enum['dimensions'][0]['dimension'] = 'NOT_A_DIMENSION'
        response = Mock()
        response.text = json.dumps(dict(choices=[dict(finish_reason='stop',
            message=dict(content=json.dumps(invalid_enum)))]))
        response.json.return_value = json.loads(response.text)
        failed = execute(response)
        detail = failed['failure']['provider_attempt_evidence']['diagnostic']
        self.assertEqual(detail['instance_path'], ['dimensions', 0, 'dimension'])
        self.assertEqual(detail['validator'], 'enum')
        self.assertIn('source_coverage', detail['allowed_value'])
        self.assertTrue(validate_graph_audit_chain(failed)['local_failure_recomputed'])

        for bad, path, validator in ((dict(extra_key='blocked'), [], 'additionalProperties'),
                ({key: value for key, value in passing_graph_response(request).items() if key != 'review_id'},
                 [], 'required')):
            response = Mock()
            response.text = json.dumps(dict(choices=[dict(finish_reason='stop',
                message=dict(content=json.dumps(bad)))]))
            response.json.return_value = json.loads(response.text)
            failed = execute(response)
            self.assertEqual(failed['failure']['stage'], 'RESPONSE_SCHEMA_VALIDATION')
            detail = failed['failure']['provider_attempt_evidence']['diagnostic']
            self.assertEqual((detail['instance_path'], detail['validator']), (path, validator))
            if validator == 'additionalProperties':
                self.assertEqual(detail['unexpected_keys'], ['extra_key'])
            else:
                self.assertEqual(detail['missing_keys'], ['review_id'])
            self.assertTrue(validate_graph_audit_chain(failed)['local_failure_recomputed'])

        http = Mock(); http.status_code = 400; http.text = '{"error":"synthetic server detail"}'
        http.raise_for_status.side_effect = requests.exceptions.HTTPError(response=http)
        failed = execute(http)
        self.assertEqual(failed['failure']['reason_code'], 'PROVIDER_HTTP_400')
        self.assertNotIn('synthetic server detail', json.dumps(failed['failure']['receipt']))
        self.assertEqual(failed['failure']['provider_attempt_evidence']['provider_response_text'], http.text)
        replay = validate_graph_audit_chain(failed)
        self.assertTrue(replay['provider_evidence_binding_verified'])
        self.assertFalse(replay['local_failure_recomputed'])

        response = passing_graph_response(request)
        response['findings'] = [dict(finding_id='WHITESPACE-CONTROL', dimension='event_identity',
            severity='NON_BLOCKING', status='OPEN', reason='虚构空白差异，不自动修复',
            affected_event_ids=[], affected_episode_ids=[], evidence=[dict(
                source_id='DOC-01', exact_quote='当前 决定')])]
        source_text = sources[0]['text'].replace('当前决定', '当前 决定')
        self.assertNotEqual(source_text, sources[0]['text'])
        sources = deepcopy(sources); sources[0]['text'] = source_text
        for key in ('sha256', 'source_sha256'): sources[0].pop(key, None)
        spaced = build_graph_audit_request(graph, sources,
            processing_units=[dict(unit_id='ALL', source_ids=[s['source_id'] for s in sources])])
        response.update(spaced['response_binding'], target=dict(
            graph_sha256=spaced['response_binding']['graph_sha256']))
        response['findings'][0]['evidence'][0]['exact_quote'] = '当前决定'
        chain = run_graph_audit_chain(spaced, lambda *args: response)
        self.assertEqual(chain['failure']['reason_code'], 'GRAPH_AUDIT_EVIDENCE_QUOTE_NOT_UNIQUE')
        detail = chain['failure']['program_diagnostics'][0]
        self.assertEqual(detail['instance_path'], ['findings', 0, 'evidence', 0, 'exact_quote'])
        self.assertEqual(detail['exact_match_count'], 0)
        self.assertEqual(detail['normalized_match_count'], 1)
        self.assertTrue(detail['whitespace_only_candidate_match'])
        self.assertFalse(detail['auto_repair_permitted'])
        self.assertTrue(validate_graph_audit_chain(chain)['status'] == 'PASS')
        response['findings'][0]['evidence'][0]['exact_quote'] = '当前 决定'
        passing = run_graph_audit_chain(spaced, lambda *args: response)
        self.assertTrue(validate_graph_audit_chain(passing)['completed_rounds'], 1)
        forged = deepcopy(chain)
        forged.update(state='AUTO_GRAPH_AUDIT_PASS', stop_reason='AUTO_PASS')
        with self.assertRaises(ValueError):
            validate_graph_audit_chain(forged)


if __name__ == '__main__':
    unittest.main()
