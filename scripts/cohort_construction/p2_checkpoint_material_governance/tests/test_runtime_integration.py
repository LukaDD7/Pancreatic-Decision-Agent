"""Synthetic provider, Episode, workbench and doctor projection integration."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.cohort_construction.model_api import ModelAPISettings
from scripts.cohort_construction.p2_auto_checkpoint_v04.client import Client
from scripts.cohort_construction.p2_auto_checkpoint_v04.prompts import SYSTEM_INSTRUCTIONS
from scripts.cohort_construction.p2_checkpoint_material_governance.episode import build_episode_material_set
from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import bind_candidate_graph
from scripts.cohort_construction.p2_checkpoint_material_governance.prompts import MATERIAL_REVIEW_INSTRUCTIONS, material_review_client
from scripts.cohort_construction.p2_checkpoint_material_governance.review import doctor_view, run_review_chain
from scripts.cohort_construction.p2_checkpoint_material_governance.run import main
from scripts.cohort_construction.p2_checkpoint_material_governance.workbench import build_auto_review_assignment, render_auto_opinion, write_workbench_package
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import build_researcher_html
from scripts.cohort_construction.p2_checkpoint_material_review_app.synthetic_demo import synthetic_inputs
from scripts.cohort_construction.p2_checkpoint_materials.review import create_review_draft
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256, project_doctor_materials
from scripts.cohort_construction.p2_checkpoint_packet.tests.test_compiler import packet, safe_compiled
from scripts.cohort_construction.p2_checkpoint_fhir import doctor_bundle_from_packet, packet_from_doctor_bundle
from scripts.cohort_construction.p2_checkpoint_fhir.tests.test_fhir_adapter import submission
from scripts.cohort_construction.p2_checkpoint_fhir import response_bundle_from_submission, submission_from_response_bundle

from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_material_governance import inputs, review_for


class TestMaterialRuntime:
    def test_cli_actual_client_echoes_visible_binding_without_network(self, tmp_path, monkeypatch):
        """Use the real Client/parser/cache, replacing only transport and secrets."""
        import threading
        import scripts.cohort_construction.p2_auto_checkpoint_v04.client as client_module
        from scripts.cohort_construction.p2_checkpoint_material_governance.review import AUTO_PROTOCOL_VERSION, DIMENSIONS
        from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import validate_review_chain

        manifest, compiled = inputs()
        config = dict(protocol_version=AUTO_PROTOCOL_VERSION, remote_execution_authorized=True,
            semantic_model='fictional-echo-model', model_temperature=0, max_output_tokens=300,
            max_material_review_rounds=2)
        for name, value in [('manifest', manifest), ('compiled', compiled), ('config', config)]:
            (tmp_path / (name + '.json')).write_text(json.dumps(value), encoding='utf-8')
        sent = []
        class Session:
            def post(self, url, **kwargs):
                body = deepcopy(kwargs['json'])
                sent.append(body)
                request = json.loads(body['messages'][1]['content'])['input']
                # No helper computes a digest for the provider: all binding
                # values come verbatim from its actual serialized input.
                answer = dict(format='material-review-0.3.0', review_id='FICTIONAL-ECHO',
                    target=request['target'], **request['response_binding'], review_round=request['review_round'], reviewer_type='llm_material_review',
                    dimensions=[dict(dimension=d, applicability='APPLICABLE', status='RESOLVED',
                                     reason='虚构传输测试') for d in DIMENSIONS], findings=[], operations=[],
                    status='AUTO_REVIEW_HOLD', required_actions=['虚构待核，未发生实际人工审核'])
                class Response:
                    status_code = 200
                    def __init__(self):
                        envelope = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}]}
                        self.text = json.dumps(envelope)
                        self._envelope = envelope
                    def raise_for_status(self): pass
                    def json(self):
                        return self._envelope
                return Response()

        settings = ModelAPISettings('FICTIONAL-NOT-A-SECRET', 'https://example.invalid/v1', 'fictional-base-model')
        monkeypatch.setattr(client_module, '_thread', threading.local())
        monkeypatch.setattr(client_module.requests, 'Session', Session)
        monkeypatch.setattr('scripts.cohort_construction.p2_checkpoint_material_governance.run.Client',
                            lambda cache, supplied: Client(cache, supplied, settings))
        monkeypatch.setattr('scripts.cohort_construction.p2_checkpoint_material_governance.run.require_private_output',
                            lambda path: Path(path).resolve())
        output = tmp_path / 'run'
        with patch('socket.socket', side_effect=AssertionError('NETWORK_FORBIDDEN')):
            result = main(['--manifest', str(tmp_path / 'manifest.json'), '--compiled', str(tmp_path / 'compiled.json'),
                '--config', str(tmp_path / 'config.json'), '--output', str(output), '--execute'])
        chain = json.loads((output / 'review-chain.json').read_text())
        assert len(sent) == 1 and sent[0]['model'] == 'fictional-echo-model'
        assert sent[0]['messages'][0]['content'] == MATERIAL_REVIEW_INSTRUCTIONS
        assert json.loads(sent[0]['messages'][1]['content'])['input'] == chain['rounds'][0]['request']
        assert chain['rounds'][0]['receipt']['request_sha256'] != chain['rounds'][0]['request_binding_sha256']
        assert validate_review_chain(chain)['state'] == result['status'] == 'AUTO_REVIEW_HOLD'
        assert result['chain_validation_sha256'] and not result['clinical_release_permitted']

    def test_material_system_message_is_separate_and_cli_default_is_offline(self, tmp_path, monkeypatch):
        captured = {}
        class Session:
            def post(self, url, **kwargs):
                captured.update(kwargs)
                class Response:
                    status_code = 200
                    text = '{"choices":[{"finish_reason":"stop","message":{"content":"{\\"ok\\":true}"}}]}'
                    def raise_for_status(self): pass
                    def json(self):
                        return json.loads(self.text)
                return Response()
        settings = ModelAPISettings('synthetic-secret', 'https://example.invalid/v1', 'synthetic-model')
        client = Client(tmp_path / 'cache', {'model_temperature': 0, 'max_output_tokens': 20}, settings)
        import threading
        with patch('scripts.cohort_construction.p2_auto_checkpoint_v04.client._thread', threading.local()), \
                patch('scripts.cohort_construction.p2_auto_checkpoint_v04.client.requests.Session', return_value=Session()):
            result, _ = material_review_client(client)('synthetic', {'no_patient_data': True},
                {'type': 'object', 'required': ['ok'], 'properties': {'ok': {'type': 'boolean'}}})
        messages = captured['json']['messages']
        assert result['ok'] and messages[0]['content'] == MATERIAL_REVIEW_INSTRUCTIONS
        assert messages[0]['content'] != SYSTEM_INSTRUCTIONS
        assert '沿原文向前或向后扩展' in messages[0]['content']
        assert '只出现一次' in messages[0]['content']
        assert 'material_review_instructions' not in captured['json']['messages'][1]['content']

        manifest, compiled = inputs()
        for name, value in [('manifest.json', manifest), ('compiled.json', compiled)]:
            (tmp_path / name).write_text(json.dumps(value), encoding='utf-8')
        private = tmp_path / 'private'
        monkeypatch.setattr('scripts.cohort_construction.p2_checkpoint_material_governance.run.require_private_output',
                            lambda path: Path(path).resolve())
        with patch('socket.socket', side_effect=AssertionError('NETWORK_FORBIDDEN')):
            result = main(['--manifest', str(tmp_path / 'manifest.json'), '--compiled', str(tmp_path / 'compiled.json'),
                           '--output', str(private / 'request')])
        assert result['remote_model_calls'] == 0 and not result['doctor_packet_created']

    def test_task_context_is_rebuilt_and_bound_in_each_round(self):
        manifest, compiled = inputs()
        context = {'decision_time_evidence': 'fictional-source-time-clue', 'clinical_visibility_certified': False}
        seen = []
        def ask(task, request, schema):
            seen.append(request)
            return review_for(manifest, compiled, status='AUTO_REVIEW_HOLD', blocking=True, task_context=context)
        result = run_review_chain(manifest, compiled, ask, task_context=context)
        assert seen[0]['task_context'] == context and result['state'] == 'AUTO_REVIEW_HOLD'
        with pytest.raises(Exception, match='TASK_CONTEXT'):
            run_review_chain(manifest, compiled, ask, task_context='not-an-object')

    def test_same_text_in_multiple_checkpoints_cannot_hide_overlap(self):
        manifest, first = inputs()
        second = deepcopy(first)
        second['target']['checkpoint_id'] = 'cp2'
        second['compiled_sha256'] = canonical_sha256(
            {k: v for k, v in second.items() if k != 'compiled_sha256'})
        graph = dict(format='candidate-reconstruction-graph-snapshot-0.1.0', graph_version='v1',
                     events=[dict(event_id='e1', checkpoint_status='CANDIDATE_SYNTHETIC'),
                             dict(event_id='e2', checkpoint_status='CANDIDATE_SYNTHETIC')],
                     episodes=[dict(episode_id='ep1', event_ids=['e1', 'e2'], checkpoint_ids=['e1', 'e2'])],
                     relations=[], discovery_units=[], unit_inventory=None, final_graph_audit=None,
                     graph_is_candidate_not_clinical_reference=True)
        graph['graph_sha256'] = canonical_sha256(graph)
        def item(compiled, event_id):
            context = bind_candidate_graph(graph, graph_version='v1', target_event_id=event_id,
                                           checkpoint_id=compiled['target']['checkpoint_id'],
                                           task_context=dict(candidate_episode_id='ep1'))
            answer = review_for(manifest, compiled, status='AUTO_REVIEW_HOLD', blocking=True, task_context=context)
            chain = run_review_chain(manifest, compiled, lambda *args: answer, task_context=context)
            return dict(manifest=manifest, compiled=compiled, doctor_view=doctor_view(compiled),
                        auto_review_chain=chain, revision_ledger=[r['ledger'] for r in chain['rounds']],
                        state=chain['state'], event_reference=event_id)
        result = build_episode_material_set(dict(episode_id='ep1', candidate_version='v1',
            input_sha256=canonical_sha256(graph), graph_snapshot=graph), [item(first, 'e1'), item(second, 'e2')])
        assert len(result['cross_checkpoint_checks']) == 2
        assert {c['checkpoint_id'] for c in result['cross_checkpoint_checks']} == {'cp1', 'cp2'}
        assert all(not c['clinical_visibility_certified'] for c in result['cross_checkpoint_checks'])

    def test_workbench_shows_version_history_without_human_permission(self, tmp_path, monkeypatch):
        documents, sources, _ = synthetic_inputs()
        manifest, compiled = documents['manifest'], documents['compiled']
        first = review_for(manifest, compiled, status='AUTO_REVIEW_HOLD', blocking=True,
                           operations=[dict(operation_id='O1', operation='REQUEST_HUMAN_REVIEW',
                                            source_id='S-fictional-included', reason='虚构人工升级')])
        chain = run_review_chain(manifest, compiled, lambda *args: first)
        draft = create_review_draft(compiled, manifest, documents['preparation_audit'], reviewer_id='AI-DRAFT-NOT-HUMAN')
        package = build_auto_review_assignment(chain, draft, documents['preparation_audit'],
            assignment_id='SYNTHETIC-AUTO-OPINION', coverage_sources=sources)
        html = render_auto_opinion(build_researcher_html(package['assignment']), package['assignment'], package['auto_review'])
        assert '自动材料审核与修订历史' in html and '新旧版本对照' in html
        monkeypatch.setattr('scripts.cohort_construction.p2_checkpoint_material_governance.workbench.require_private_output', lambda path: Path(path).resolve())
        write_workbench_package(package, tmp_path / 'private' / 'workbench')
        forged = deepcopy(package['auto_review'])
        forged['independence_attested'] = True
        with pytest.raises(ValueError, match='GOVERNANCE'):
            render_auto_opinion(html, package['assignment'], forged)

    def test_human_fhir_projection_uses_same_doctor_view(self):
        original = packet()
        # The FHIR path still requires a separately supplied human PASS, not an auto chain.
        assert original['materials'] == project_doctor_materials(safe_compiled())
        bundle = doctor_bundle_from_packet(original)
        assert packet_from_doctor_bundle(bundle, expected_packet_sha256=original['packet_sha256']) == original
        response_bundle = response_bundle_from_submission(submission(original), original)
        assert submission_from_response_bundle(response_bundle, original,
            expected_reviewer_assignment_id='fictional-assignment-1') == submission(original)
