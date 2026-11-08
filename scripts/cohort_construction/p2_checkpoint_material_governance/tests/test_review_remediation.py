"""Synthetic negative/positive controls for V061-AUD001/003/004/005/006."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.review import (
    AUTO_PROTOCOL_VERSION, DIMENSIONS, MaterialReviewError, build_review_request,
    doctor_view, run_review_chain, validate_material_review,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.run import main
from scripts.cohort_construction.p2_checkpoint_packet.compiler import build_source_manifest, canonical_sha256
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_material_governance import (
    TEXT, inputs, operation, review_for,
)


def echo_response(request, *, status='AUTO_REVIEW_HOLD', operations=(), required_actions=None):
    """A fake provider copies hashes visible in input; never computes one."""
    return dict(format='material-review-0.3.0', review_id='SYNTHETIC-ECHO', target=deepcopy(request['target']),
        **deepcopy(request['response_binding']), review_round=deepcopy(request['review_round']), reviewer_type='llm_material_review',
        dimensions=[dict(dimension=d, applicability='APPLICABLE', status='RESOLVED',
                         reason='虚构维度核对') for d in DIMENSIONS], findings=[],
        operations=list(operations), status=status,
        required_actions=['虚构待处理事项'] if required_actions is None else required_actions)


def seed_cli(tmp_path, monkeypatch, *, config=None):
    manifest, compiled = inputs()
    # Preserve noncanonical whitespace/newlines to test exact bytes, not reserialization.
    for name, value in [('manifest', manifest), ('compiled', compiled), ('config', config)]:
        if value is not None:
            (tmp_path / (name + '.json')).write_bytes(
                ('  ' + json.dumps(value, ensure_ascii=False, indent=3) + '\n\n').encode())
    monkeypatch.setattr('scripts.cohort_construction.p2_checkpoint_material_governance.run.require_private_output',
                        lambda path: Path(path).resolve())
    args = ['--manifest', str(tmp_path / 'manifest.json'), '--compiled', str(tmp_path / 'compiled.json'),
            '--output', str(tmp_path / 'out')]
    if config is not None:
        args.extend(['--config', str(tmp_path / 'config.json')])
    return args


class TestRequestBindingRemediation:
    def test_v061_aud003_echo_envelope_suffices_without_model_hashing(self):
        manifest, compiled = inputs()
        request = build_review_request(manifest, compiled)
        assert request['response_binding']['request_sha256'] == canonical_sha256(
            {k: v for k, v in request.items() if k != 'response_binding'})
        assert request['response_binding']['request_sha256'] != canonical_sha256(request)
        result = validate_material_review(manifest, compiled, echo_response(request), expected_request=request)
        assert not result['clinical_release_permitted']

    def test_v061_aud003_old_protocol_cannot_define_expected_request(self):
        manifest, compiled = inputs()
        request = build_review_request(manifest, compiled)
        response = echo_response(request)
        response['protocol_version'] = 'checkpoint-automated-material-review-0.1.0'
        with pytest.raises(MaterialReviewError, match='SCHEMA_INVALID|BINDING'):
            validate_material_review(manifest, compiled, response)
        with pytest.raises(MaterialReviewError, match='UNSUPPORTED'):
            build_review_request(manifest, compiled, protocol_version=response['protocol_version'])

    def test_v061_aud003_old_response_format_rejected(self):
        manifest, compiled = inputs()
        request = build_review_request(manifest, compiled)
        answer = echo_response(request)
        answer['format'] = 'material-review-0.1.0'
        with pytest.raises(MaterialReviewError, match='SCHEMA_INVALID'):
            validate_material_review(manifest, compiled, answer)

    def test_v061_aud003_expected_sent_request_cannot_be_swapped(self):
        manifest, compiled = inputs()
        context = dict(decision_time_evidence='虚构甲', clinical_visibility_certified=False)
        sent = build_review_request(manifest, compiled, task_context=context)
        other = build_review_request(manifest, compiled)
        with pytest.raises(MaterialReviewError, match='EXPECTED_REVIEW_REQUEST_MISMATCH'):
            validate_material_review(manifest, compiled, echo_response(other), expected_request=sent)
        assert validate_material_review(manifest, compiled, echo_response(sent),
                                       task_context=context, expected_request=sent)['review']['status'] == 'AUTO_REVIEW_HOLD'

    def test_v061_aud003_response_hash_or_target_tampering_rejected(self):
        manifest, compiled = inputs()
        request = build_review_request(manifest, compiled)
        for field in ('request_sha256', 'doctor_view_sha256', 'compiled_sha256'):
            answer = echo_response(request)
            answer[field] = '0' * 64
            with pytest.raises(MaterialReviewError, match='INPUT_BINDING'):
                validate_material_review(manifest, compiled, answer, expected_request=request)

    def test_v061_aud003_provider_mutation_does_not_change_frozen_sent_request(self):
        manifest, compiled = inputs()
        def ask(task, payload, schema):
            original = deepcopy(payload)
            payload['task_context']['decision_time_evidence'] = '篡改请求'
            return echo_response(original)
        chain = run_review_chain(manifest, compiled, ask)
        assert chain['rounds'][0]['request']['task_context']['decision_time_evidence'] == 'UNRESOLVED'
        assert chain['stop_reason'] == 'REVIEW_HOLD'


class TestSourceVersionRemediation:
    def test_v061_aud004_same_id_new_text_manifest_old_partition_rejected(self):
        manifest, compiled = inputs()
        replacement = build_source_manifest(manifest['manifest_id'], [dict(source_id='doc1',
            source_version='v2', text='虚构另一份已改正文。')])
        with pytest.raises(MaterialReviewError, match='SOURCE_CONTENT_BINDING'):
            build_review_request(replacement, compiled)
        request = build_review_request(manifest, compiled)
        assert request['researcher_sources'][0]['text'] == ''.join(
            s['text'] for s in compiled['sources'][0]['segments'])

    def test_v061_aud004_same_text_new_version_is_not_interchangeable(self):
        manifest, compiled = inputs()
        replacement = build_source_manifest(manifest['manifest_id'], [dict(source_id='doc1',
            source_version='v2', text=TEXT)])
        with pytest.raises(MaterialReviewError, match='SOURCE_CONTENT_BINDING'):
            build_review_request(replacement, compiled)

    def test_v061_aud004_self_consistent_partition_tamper_still_rejected_by_manifest(self):
        manifest, compiled = inputs()
        tampered = deepcopy(compiled)
        changed_text = TEXT.replace('未见', '可见')
        tampered['sources'][0]['segments'][0]['text'] = changed_text
        tampered['sources'][0]['source_sha256'] = hashlib.sha256(changed_text.encode()).hexdigest()
        tampered['compiled_sha256'] = canonical_sha256({k: v for k, v in tampered.items() if k != 'compiled_sha256'})
        with pytest.raises(MaterialReviewError, match='SOURCE_CONTENT_BINDING'):
            build_review_request(manifest, tampered)

    def test_v061_aud004_overlapping_or_changed_external_source_is_rejected(self):
        manifest, compiled = inputs()
        with pytest.raises(MaterialReviewError, match='SCOPES_OVERLAP'):
            build_review_request(manifest, compiled,
                excluded_sources=[dict(source_id='doc1', source_version='v1', text=TEXT)])
        with pytest.raises(MaterialReviewError, match='SOURCE_HASH_MISMATCH'):
            build_review_request(manifest, compiled,
                candidate_sources=[dict(source_id='candidate', source_version='v1', text='虚构资料', sha256='0' * 64)])


class TestChainTailAndRegistryRemediation:
    def test_v061_aud005_excluded_original_reappears_in_every_later_review(self):
        manifest, compiled = inputs()
        candidate = dict(source_id='doc2', source_version='v1', text='虚构候选文书。')
        seen = []
        def ask(task, request, schema):
            seen.append(deepcopy(request))
            if len(seen) == 1:
                return echo_response(request, status='AUTO_REVIEW_REVISE', required_actions=[], operations=[
                    dict(operation_id='include', operation='INCLUDE_SOURCE', source_id='doc2', reason='虚构核对'),
                    dict(operation_id='exclude', operation='EXCLUDE_SOURCE', source_id='doc1', reason='虚构排除')])
            return echo_response(request)
        result = run_review_chain(manifest, compiled, ask, candidate_sources=[candidate])
        assert len(seen) == 2
        assert [s['source_id'] for s in seen[1]['researcher_sources']] == ['doc2']
        assert [s['source_id'] for s in seen[1]['excluded_sources']] == ['doc1']
        assert seen[1]['excluded_sources'][0]['text'] == TEXT
        assert {s['source_id'] for s in result['source_registry']} == {'doc1', 'doc2'}
        assert result['source_registry_sha256'] == canonical_sha256(result['source_registry'])

    def test_v061_aud005_initial_candidate_and_excluded_sources_remain_available(self):
        manifest, compiled = inputs()
        candidate = dict(source_id='candidate', source_version='v1', text='虚构候选。')
        excluded = dict(source_id='excluded', source_version='v1', text='虚构排除。')
        seen = []
        def ask(task, request, schema):
            seen.append(deepcopy(request))
            if len(seen) == 1:
                return echo_response(request, status='AUTO_REVIEW_REVISE', required_actions=[],
                                     operations=[operation(TEXT, 'KEEP')])
            return echo_response(request, status='AUTO_REVIEW_PASS', required_actions=[])
        result = run_review_chain(manifest, compiled, ask, candidate_sources=[candidate], excluded_sources=[excluded])
        assert result['state'] == 'AUTO_REVIEW_PASS'
        assert all(r['candidate_sources'][0]['source_id'] == 'candidate' for r in seen)
        assert all(r['excluded_sources'][0]['source_id'] == 'excluded' for r in seen)

    def test_v061_aud006_applied_revision_and_hold_have_same_final_version(self):
        manifest, compiled = inputs()
        request = build_review_request(manifest, compiled)
        answer = echo_response(request, operations=[operation(TEXT, 'MASK'),
            dict(operation_id='hold', operation='HOLD_PACKET', source_id='doc1', reason='虚构待核')])
        result = run_review_chain(manifest, compiled, lambda *args: answer)
        assert len(result['rounds']) == 1 and result['state'] == 'AUTO_REVIEW_HOLD'
        assert result['rounds'][0]['changed']
        assert result['manifest'] == result['rounds'][0]['manifest']
        assert result['compiled'] == result['rounds'][0]['compiled']
        assert result['compiled']['compiled_sha256'] != compiled['compiled_sha256']
        assert result['doctor_view'] == doctor_view(result['compiled'])
        assert result['final_binding']['compiled_sha256'] == result['compiled']['compiled_sha256']
        receipt = result['rounds'][0]['application_receipt']
        assert receipt['ledger_sha256'] == canonical_sha256(result['rounds'][0]['ledger'])
        assert not result['clinical_release_permitted']

    def test_v061_aud006_required_actions_hold_does_not_continue_to_auto_pass(self):
        manifest, compiled = inputs()
        calls = []
        def ask(task, request, schema):
            calls.append(request)
            return echo_response(request)
        result = run_review_chain(manifest, compiled, ask)
        assert len(calls) == 1 and result['stop_reason'] == 'REVIEW_HOLD'
        assert result['compiled'] == compiled

    def test_v061_aud006_provider_failure_preserves_completed_revision(self):
        manifest, compiled = inputs()
        seen = []
        def ask(task, request, schema):
            seen.append(request)
            if len(seen) == 1:
                return echo_response(request, status='AUTO_REVIEW_REVISE', required_actions=[],
                                     operations=[operation(TEXT, 'KEEP')]), {'request_sha256': '1' * 64}
            raise RuntimeError('potentially confidential provider detail')
        result = run_review_chain(manifest, compiled, ask)
        assert result['stop_reason'] == 'EXECUTION_FAILED' and len(result['rounds']) == 1
        assert result['compiled'] == result['rounds'][0]['compiled']
        assert result['failure']['round_index'] == 2
        assert result['failure']['reason_code'] == 'RuntimeError'
        assert 'confidential' not in json.dumps(result['failure'])

    def test_v061_aud006_invalid_response_produces_bound_failure_hold(self):
        manifest, compiled = inputs()
        def ask(task, request, schema):
            answer = echo_response(request)
            answer['request_sha256'] = '0' * 64
            return answer, {'request_sha256': '2' * 64, 'status': 'SUCCESS'}
        result = run_review_chain(manifest, compiled, ask)
        assert not result['rounds'] and result['compiled'] == compiled
        assert result['state'] == 'AUTO_REVIEW_HOLD' and result['failure']['stage'] == 'RESPONSE_VALIDATION'
        assert result['failure']['receipt']['request_sha256'] == '2' * 64
        assert result['failure']['request_sha256'] == canonical_sha256(result['failure']['request'])


class TestCliRemediation:
    def test_v061_aud001_default_cli_and_candidates_are_offline(self, tmp_path, monkeypatch):
        args = seed_cli(tmp_path, monkeypatch)
        candidate = [dict(source_id='external', source_version='v1', text='虚构候选来源。')]
        path = tmp_path / 'candidate.json'
        path.write_text(json.dumps(candidate), encoding='utf-8')
        with patch('socket.socket', side_effect=AssertionError('NETWORK_FORBIDDEN')):
            result = main(args + ['--candidate-sources', str(path)])
        assert result['remote_model_calls'] == 0
        request = json.loads((tmp_path / 'out' / 'material-review-request.json').read_text())
        assert request['candidate_sources'][0]['text'] == candidate[0]['text']

    def test_v061_aud001_snapshots_preserve_input_bytes_code_prompt_and_config(self, tmp_path, monkeypatch):
        config = dict(protocol_version=AUTO_PROTOCOL_VERSION, remote_execution_authorized=False)
        args = seed_cli(tmp_path, monkeypatch, config=config)
        main(args)
        for name in ('manifest', 'compiled', 'config'):
            assert (tmp_path / 'out' / 'input-snapshot' / (name + '.json')).read_bytes() == (
                tmp_path / (name + '.json')).read_bytes()
        receipt = json.loads((tmp_path / 'out' / 'input-snapshot-receipt.json').read_text())
        assert len(receipt['code']) >= 20 and len(receipt['prompt_sha256']) == 64
        assert any(p['path'].endswith('/compiler.py') for p in receipt['code'])
        assert any(p['path'].endswith('/client.py') for p in receipt['code'])
        assert any('/schemas/' in p['path'] for p in receipt['code'])
        assert receipt['snapshot_sha256'] == canonical_sha256(
            {k: v for k, v in receipt.items() if k != 'snapshot_sha256'})

    def test_v061_aud001_existing_output_is_never_overwritten(self, tmp_path, monkeypatch):
        args = seed_cli(tmp_path, monkeypatch)
        main(args)
        before = (tmp_path / 'out' / 'material-review-request.json').read_bytes()
        with pytest.raises(FileExistsError):
            main(args)
        assert before == (tmp_path / 'out' / 'material-review-request.json').read_bytes()

    def test_v061_aud001_unapproved_config_gets_failure_hold_without_client(self, tmp_path, monkeypatch):
        args = seed_cli(tmp_path, monkeypatch, config=dict(protocol_version=AUTO_PROTOCOL_VERSION,
                                                        remote_execution_authorized=False))
        with patch('scripts.cohort_construction.p2_checkpoint_material_governance.run.Client',
                   side_effect=AssertionError('CLIENT_MUST_NOT_START')):
            result = main(args + ['--execute'])
        assert result['status'] == 'AUTO_REVIEW_HOLD'
        assert not result['model_receipts']
        assert json.loads((tmp_path / 'out' / 'execution-receipt.json').read_text()) == result

    def test_v061_aud001_round_journal_preserves_success_before_failure(self, tmp_path, monkeypatch):
        args = seed_cli(tmp_path, monkeypatch, config=dict(protocol_version=AUTO_PROTOCOL_VERSION,
            remote_execution_authorized=True, max_material_review_rounds=3))
        class FakeClient:
            def __init__(self, *args):
                self.receipts, self.calls = [], 0
            def ask(self, task, request, schema, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return echo_response(request, status='AUTO_REVIEW_REVISE', required_actions=[],
                                         operations=[operation(TEXT, 'KEEP')]), {'cached': False}
                raise RuntimeError('synthetic provider failure')
        monkeypatch.setattr('scripts.cohort_construction.p2_checkpoint_material_governance.run.Client', FakeClient)
        result = main(args + ['--execute'])
        out = tmp_path / 'out'
        assert result['status'] == 'AUTO_REVIEW_HOLD' and result['rounds'] == 1
        assert (out / 'rounds' / '0001' / 'completed-round.json').exists()
        assert (out / 'rounds' / '0002' / 'input.json').exists()
        assert (out / 'rounds' / '0002' / 'failure-hold.json').exists()
        chain = json.loads((out / 'review-chain.json').read_text())
        assert chain['compiled'] == chain['rounds'][0]['compiled']
        assert not chain['clinical_release_permitted']


class TestRevisionHistoryAndRunSnapshot:
    def test_v061_aud005_candidate_included_then_excluded_is_in_excluded_view(self):
        manifest, compiled = inputs()
        candidate = dict(source_id='candidate', source_version='v1', text='虚构候选。')
        seen = []
        def ask(task, request, schema):
            seen.append(deepcopy(request))
            if len(seen) == 1:
                return echo_response(request, status='AUTO_REVIEW_REVISE', required_actions=[], operations=[
                    operation(TEXT, 'MASK'), dict(operation_id='include', operation='INCLUDE_SOURCE',
                                                  source_id='candidate', reason='虚构纳入')])
            if len(seen) == 2:
                return echo_response(request, status='AUTO_REVIEW_REVISE', required_actions=[], operations=[
                    dict(operation_id='exclude', operation='EXCLUDE_SOURCE', source_id='candidate', reason='虚构排除')])
            return echo_response(request)
        result = run_review_chain(manifest, compiled, ask, candidate_sources=[candidate], max_rounds=4)
        # Third version has the same partition as neither earlier complete input:
        # initial doc1 REVIEW, then doc1 MASK + candidate REVIEW, then doc1 MASK.
        assert len(seen) == 3 and result['stop_reason'] == 'REVIEW_HOLD'
        assert not seen[2]['candidate_sources']
        assert seen[2]['excluded_sources'][0]['source_id'] == 'candidate'
        assert seen[2]['excluded_sources'][0]['text'] == candidate['text']
        assert result['source_registry'][1]['origin_scope'] == 'CANDIDATE'

    def test_v061_aud001_effective_cli_budget_and_sanitized_runtime_bound(self, tmp_path, monkeypatch):
        from scripts.cohort_construction.model_api import ModelAPISettings
        args = seed_cli(tmp_path, monkeypatch, config=dict(protocol_version=AUTO_PROTOCOL_VERSION,
            remote_execution_authorized=True, max_material_review_rounds=3,
            model_temperature=0.1, max_output_tokens=200))
        class FakeClient:
            def __init__(self, *args):
                self.receipts = []
                self.settings = ModelAPISettings('SECRET', 'https://user:password@example.invalid/v1?token=hidden',
                                                 'synthetic-model', thinking='enabled')
            def ask(self, task, request, schema, **kwargs):
                return echo_response(request), {'cached': False}
        monkeypatch.setattr('scripts.cohort_construction.p2_checkpoint_material_governance.run.Client', FakeClient)
        result = main(args + ['--execute', '--max-rounds', '2'])
        out = tmp_path / 'out'
        settings = json.loads((out / 'execution-settings.json').read_text())
        assert settings['model'] == 'synthetic-model' and settings['thinking'] == 'enabled'
        assert settings['max_material_review_rounds'] == 2 and settings['max_tokens'] == 200
        assert settings['endpoint'] == 'https://example.invalid/v1'
        assert settings['endpoint_sensitive_components_omitted']
        assert 'SECRET' not in json.dumps(settings) and 'password' not in json.dumps(settings)
        assert result['execution_settings_sha256'] == canonical_sha256(settings)
        snapshot = json.loads((out / 'input-snapshot-receipt.json').read_text())
        assert snapshot['effective_max_material_review_rounds'] == 2
        assert snapshot['runtime_environment']['python']
        for dependency in snapshot['code']:
            archived = out / dependency['archived_path']
            assert hashlib.sha256(archived.read_bytes()).hexdigest() == dependency['bytes_sha256']

    def test_v061_aud001_credentials_in_config_are_rejected_before_archive(self, tmp_path, monkeypatch):
        args = seed_cli(tmp_path, monkeypatch, config=dict(protocol_version=AUTO_PROTOCOL_VERSION,
                                                         api_key='DO-NOT-COPY'))
        with pytest.raises(ValueError, match='CREDENTIAL_FIELDS'):
            main(args)
        assert not (tmp_path / 'out').exists()


class TestPassRequiredActionsRemediation:
    def test_v061_aud006_pass_cannot_carry_uncompleted_required_actions(self):
        manifest, compiled = inputs()
        revision = validate_material_review(manifest, compiled,
            review_for(manifest, compiled, operations=[operation(TEXT, 'MASK')]))
        manifest, compiled = revision['manifest'], revision['compiled']
        passing = review_for(manifest, compiled, status='AUTO_REVIEW_PASS')
        assert validate_material_review(manifest, compiled, passing)['review']['status'] == 'AUTO_REVIEW_PASS'
        passing['required_actions'] = ['必须补充来源后重新审核']
        with pytest.raises(MaterialReviewError, match='PASS_CANNOT_HAVE_REQUIRED_ACTIONS'):
            validate_material_review(manifest, compiled, passing)
        chain = run_review_chain(manifest, compiled, lambda *args: passing)
        assert chain['state'] == 'AUTO_REVIEW_HOLD' and chain['stop_reason'] == 'EXECUTION_FAILED'
        assert chain['failure']['reason_code'] == 'PASS_CANNOT_HAVE_REQUIRED_ACTIONS'
        assert not chain['clinical_release_permitted']


    def test_v061_aud006_revise_required_actions_retains_revision_and_stops_hold(self):
        manifest, compiled = inputs()
        calls = []
        def ask(task, request, schema):
            calls.append(request)
            return echo_response(request, status='AUTO_REVIEW_REVISE', operations=[operation(TEXT, 'KEEP')],
                                 required_actions=['必须核对来源后再继续'])
        chain = run_review_chain(manifest, compiled, ask)
        assert len(calls) == 1 and len(chain['rounds']) == 1
        assert chain['stop_reason'] == 'REVIEW_HOLD' and chain['state'] == 'AUTO_REVIEW_HOLD'
        assert chain['compiled'] == chain['rounds'][0]['compiled']
        assert chain['compiled']['compiled_sha256'] != compiled['compiled_sha256']


class TestReplayReceiptRemediation:
    @pytest.mark.parametrize('replay_fails', [False, True])
    def test_v061_aud006_cli_replays_durable_chain_and_retains_failures(self, tmp_path, monkeypatch, replay_fails):
        from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError
        args = seed_cli(tmp_path, monkeypatch, config=dict(protocol_version=AUTO_PROTOCOL_VERSION,
            remote_execution_authorized=True, max_material_review_rounds=3))
        calls = []
        class FakeClient:
            def __init__(self, *args):
                self.receipts = []
            def ask(self, task, request, schema, **kwargs):
                calls.append(deepcopy(request))
                return echo_response(request), {'cached': False}
        monkeypatch.setattr('scripts.cohort_construction.p2_checkpoint_material_governance.run.Client', FakeClient)
        if replay_fails:
            def reject(chain):
                assert (tmp_path / 'out' / 'review-chain.json').is_file()
                assert (tmp_path / 'out' / 'rounds' / '0001' / 'completed-round.json').is_file()
                raise ContractError('SYNTHETIC_REPLAY_REJECTION')
            monkeypatch.setattr('scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation.validate_review_chain', reject)
        result = main(args + ['--execute'])
        out = tmp_path / 'out'
        receipt = json.loads((out / 'chain-validation-receipt.json').read_text())
        chain = json.loads((out / 'review-chain.json').read_text())
        assert receipt['status'] == ('REPLAY_FAILED' if replay_fails else 'DETERMINISTIC_REPLAY_VERIFIED')
        if not replay_fails:
            assert receipt['remote_provider_authenticity_certified'] is False
            assert receipt['provider_transport_verified'] is False
        assert receipt['chain_sha256'] == result['review_chain_sha256'] == canonical_sha256(chain)
        assert result['chain_validation_sha256'] == canonical_sha256(receipt)
        assert result['status'] == 'AUTO_REVIEW_HOLD' and not result['clinical_release_permitted']
        assert result['rounds'] == 1 and len(calls) == 1
        assert chain['compiled'] == chain['rounds'][0]['compiled']
