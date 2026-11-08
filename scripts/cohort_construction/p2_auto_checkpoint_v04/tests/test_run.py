"""CE-REAL023: fixed-ten runner authorization, resume and measured-summary tests.

All inputs are synthetic identifiers and minimal FHIR Patient shells. Source
import, unit projection, semantic processing, FHIR build/validation and provider
construction are monkeypatched. No credential, network, archive or patient data
is used. Temporary output roots are physically owner-only (0700).

Issue map (kept in this NEW module because this task owns no shared map file):
- CE-REAL023 dry-run and rejected contracts never construct a provider Client.
- CE-REAL023 exact source/config/schema/manifest/code/model/budget/governance
  bindings are mandatory; the external authorization is read-only.
- CE-REAL023 only hash-verified completed cases resume; partial/tampered output
  fails closed and never becomes a duplicate provider run.
- CE-REAL023 final technical completion requires all ten saved case receipts;
  a selected subset and failure summaries are reported without KeyError.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.cohort_construction.p2_auto_checkpoint_v04 import run


class FixedTenRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='CE-REAL023-')
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.private_root = self.root / 'private'
        self.private_root.mkdir(mode=0o700)
        self.sources = self.root / 'sources'
        self.sources.mkdir()
        for case_id in run.FIXED_CASE_IDS:
            case_dir = self.sources / case_id
            case_dir.mkdir()
            # A synthetic FHIR shell, not clinical content.
            bundle = {'resourceType': 'Bundle', 'entry': [
                {'resource': {'resourceType': 'Patient', 'id': case_id}}]}
            (case_dir / 'source_fhir_r4_bundle.json').write_text(
                json.dumps(bundle), encoding='utf-8')
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({'seed': 20261023, 'selected': [
            {'case_id': case_id} for case_id in run.FIXED_CASE_IDS]}), encoding='utf-8')
        self.schema = self.root / 'schema.json'
        self.schema.write_text('{"synthetic":true}\n', encoding='utf-8')
        self.config_path = self.root / 'config.json'
        self.config = {
            'version': 'synthetic-CE-REAL023',
            'semantic_model': 'synthetic-model',
            'execution_batch_id': 'synthetic-batch',
            'model_temperature': 0,
            'max_output_tokens': 32,
            'discovery_context_characters': 1000,
            'review_context_characters': 1000,
            'max_review_payload_characters': 1000,
            'max_http_attempts_per_request': 2,
            'discovery_feedback_max_rounds': 1,
            'review_feedback_max_rounds': 1,
            'include_support_documents': True,
            'allow_overbudget_current_unit': False,
            'clinical_reference': False,
            'reference_files_permitted_as_pipeline_input': False,
            'doctor_packet_release': False,
            'real_doctor_assignment_creation': False,
            'clinical_release_permitted': False,
        }
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
        self.processed = []
        self.provider = Mock(side_effect=AssertionError(
            'CE-REAL023 provider construction forbidden in this test'))
        self.patches = [
            patch.object(run, 'import_sources', side_effect=self.fake_import),
            patch.object(run, 'build_units', side_effect=self.fake_units),
            patch.object(run, 'context_for_discovery', side_effect=self.fake_context),
            patch.object(run, 'process_sources', side_effect=self.fake_process),
            patch.object(run, 'build', side_effect=self.fake_build),
            patch.object(run, 'validate', return_value={
                'official_r4_json_schema': True, 'local_references_resolved': True}),
            patch('scripts.cohort_construction.p2_auto_checkpoint_v04.engine.Client', self.provider),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    @staticmethod
    def fake_import(bundle):
        case_id = bundle['entry'][0]['resource']['id']
        return ([{'source_id': case_id, 'text': 'synthetic complete document',
                  'kind': 'document'}], {'synthetic_import': True})

    @staticmethod
    def fake_units(sources, config, routing_audit=None):
        return [{'unit_id': sources[0]['source_id'], 'text': sources[0]['text']}]

    @staticmethod
    def fake_context(unit, sources, limit):
        return {'budget': {'current_unit_over_budget': False},
                'complete_document': unit['text']}

    def fake_process(self, case_id, sources, config, out, cache, workers):
        self.processed.append(case_id)
        self.assertEqual(workers, 4)
        out = Path(out)
        (out / 'decision_events.json').write_text('[]\n', encoding='utf-8')
        (out / 'episodes.json').write_text('[]\n', encoding='utf-8')
        (out / 'relations.json').write_text('[]\n', encoding='utf-8')
        (out / 'failures.json').write_text('[]\n', encoding='utf-8')
        (out / 'model_receipts.json').write_text('[]\n', encoding='utf-8')
        summary = {
            'case_id': case_id, 'version': config['version'],
            'discovered_events': 0, 'reviewed_events': 0,
            'candidate_checkpoints': 0, 'candidate_episodes': 0,
            'process_links': 0, 'failures': 0, 'model_calls': 0,
            'cached_calls': 0, 'clinical_accuracy_measured': False,
            'blind_packets_created': False, 'reference_read_by_pipeline': False,
        }
        (out / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')
        return [], [], summary

    @staticmethod
    def fake_build(sources, case_id, annotations, episodes):
        return {'resourceType': 'Bundle', 'synthetic_case_id': case_id,
                'annotations': annotations, 'episodes': episodes}

    def arguments(self, name, *extra, cases=None):
        output = self.private_root / (name + '-output')
        cache = self.private_root / (name + '-cache')
        argv = ['--sources', str(self.sources), '--manifest', str(self.manifest),
                '--schema', str(self.schema), '--config', str(self.config_path),
                '--output', str(output), '--cache', str(cache),
                '--private-root', str(self.private_root)]
        if cases is not None:
            argv.extend(['--cases', *cases])
        argv.extend(extra)
        return argv, output, cache

    def dry_run(self, name='dry', **kwargs):
        argv, output, cache = self.arguments(name, '--dry-run', **kwargs)
        self.assertEqual(run.main(argv), 0)
        contract = run.read(output / 'run_contract.json')
        self.assertFalse(cache.exists())
        self.provider.assert_not_called()
        return contract, output, cache

    def authorization(self, contract):
        authorization = {
            'format': run.AUTHORIZATION_FORMAT,
            'remote_execution_authorized': True,
            'execution_batch_id': contract['execution_batch_id'],
            'semantic_model': contract['semantic_model'],
            'case_ids': contract['case_ids'],
            'max_total_model_requests':
                contract['offline_preflight']['maximum_logical_provider_requests'],
            'max_total_http_attempts':
                contract['offline_preflight']['maximum_http_attempts'],
            'max_http_attempts_per_request':
                contract['config']['max_http_attempts_per_request'],
            'execution_scope': run.EXECUTION_SCOPE,
            'source_fhir_discovery_and_review_context_authorized': True,
            'doctor_packet_release': False,
            'real_doctor_assignment_creation': False,
            'clinical_release_permitted': False,
        }
        authorization.update({key: contract[key] for key in run.AUTH_BINDING_FIELDS})
        return authorization

    def live(self, name, authorization, **kwargs):
        auth_path = self.root / (name + '-authorization.json')
        original = json.dumps(authorization, ensure_ascii=False, indent=2) + '\n'
        auth_path.write_text(original, encoding='utf-8')
        before_hash = run.file_hash(auth_path)
        argv, output, cache = self.arguments(
            name, '--execution-authorization', str(auth_path), **kwargs)
        status = run.main(argv)
        self.assertEqual(run.file_hash(auth_path), before_hash)
        self.assertEqual(auth_path.read_text(encoding='utf-8'), original)
        saved_contract = run.read(output / 'run_contract.json')
        self.assertEqual(saved_contract['format'], run.CONTRACT_FORMAT)
        self.assertTrue(saved_contract['model_execution_requested'])
        self.assertEqual(saved_contract['execution_mode'], 'LIVE')
        self.assertNotIn('actual_model_calls', saved_contract)
        return status, output, cache

    def test_CE_REAL023_dry_run_defaults_full_ten_without_provider_or_cache(self):
        contract, output, cache = self.dry_run()
        self.assertEqual(contract['case_ids'], run.FIXED_CASE_IDS)
        self.assertEqual(contract['seed'], 20261023)
        self.assertEqual(contract['offline_preflight']['maximum_logical_provider_requests'], 40)
        self.assertEqual(contract['offline_preflight']['maximum_http_attempts'], 80)
        self.assertEqual(contract['execution_mode'], 'DRY_RUN')
        self.assertFalse(contract['model_execution_requested'])
        self.assertNotIn('actual_model_calls', contract)
        self.assertTrue((output / 'run_contract.json').is_file())
        self.assertFalse(cache.exists())
        self.assertEqual(output.stat().st_mode & 0o077, 0)
        self.assertEqual((output / 'run_contract.json').stat().st_mode & 0o077, 0)
        with self.assertRaisesRegex(ValueError, 'NEW_OUTPUT_AND_CACHE_REQUIRED'):
            run.main(self.arguments('dry', '--dry-run')[0])

    def test_CE_REAL023_missing_authorization_rejected_before_provider(self):
        argv, output, cache = self.arguments('missing-auth')
        with self.assertRaisesRegex(ValueError, 'EXECUTION_AUTHORIZATION_REQUIRED'):
            run.main(argv)
        self.provider.assert_not_called()
        self.assertEqual(self.processed, [])
        self.assertFalse(output.exists())
        self.assertFalse(cache.exists())

    def test_CE_REAL023_hash_model_budget_and_governance_mismatch_rejected(self):
        contract, _, _ = self.dry_run('binding-preflight')
        mutations = {
            'manifest_sha256': '0' * 64,
            'config_sha256': '0' * 64,
            'schema_sha256': '0' * 64,
            'source_input_sha256': {'SCALE-01': '0' * 64},
            'code_sha256': {},
            'semantic_model': 'different-model',
            'execution_batch_id': 'different-batch',
            'case_ids': ['SCALE-01'],
            'max_total_model_requests': 1,
            'max_total_http_attempts': 1,
            'max_http_attempts_per_request': 1,
            'remote_execution_authorized': False,
            'source_fhir_discovery_and_review_context_authorized': False,
            'doctor_packet_release': True,
            'real_doctor_assignment_creation': True,
            'clinical_release_permitted': True,
        }
        for index, (field, value) in enumerate(mutations.items()):
            with self.subTest(field=field):
                authorization = self.authorization(contract)
                authorization[field] = value
                with self.assertRaisesRegex(ValueError, 'EXECUTION_AUTHORIZATION_'):
                    self.live('rejected-%02d' % index, authorization)
                self.provider.assert_not_called()
        self.assertEqual(self.processed, [])

    def test_CE_REAL023_valid_live_subset_has_no_fixed_ten_completion_claim(self):
        contract, _, _ = self.dry_run('subset-preflight', cases=['SCALE-01'])
        status, output, cache = self.live(
            'subset-live', self.authorization(contract), cases=['SCALE-01'])
        self.assertEqual(status, 0)
        self.assertEqual(self.processed, ['SCALE-01'])
        summary = run.read(output / 'execution_summary.json')
        self.assertEqual(summary['case_count'], 1)
        self.assertTrue(summary['selected_subset_technically_complete'])
        self.assertFalse(summary['all_fixed_ten_selected'])
        self.assertFalse(summary['technically_complete'])
        self.assertEqual(summary['actual_provider_invocations'], 0)
        self.assertFalse(summary['clinical_reference_created'])
        self.assertFalse(summary['clinical_release_permitted'])
        self.provider.assert_not_called()

    def test_CE_REAL023_resume_exact_completed_cases_has_no_duplicate_processing(self):
        contract, _, _ = self.dry_run('resume-preflight')
        status, output, cache = self.live('resume-live', self.authorization(contract))
        self.assertEqual(status, 0)
        self.assertEqual(len(self.processed), 10)
        first = run.read(output / 'execution_summary.json')
        self.assertTrue(first['technically_complete'])
        self.assertEqual(first['case_count'], 10)
        argv, _, _ = self.arguments('resume-live', '--resume')
        self.assertEqual(run.main(argv), 0)
        self.assertEqual(len(self.processed), 10, 'completed cases must not run twice')
        resumed = run.read(output / 'execution_summary.json')
        self.assertTrue(resumed['technically_complete'])
        self.assertEqual(resumed['complete_case_receipt_ids'], run.FIXED_CASE_IDS)
        self.assertTrue(resumed['resumed_execution'])
        self.provider.assert_not_called()

    def test_CE_REAL023_partial_output_resume_rejected_without_overwrite(self):
        contract, _, _ = self.dry_run('partial-preflight')
        status, output, cache = self.live('partial-live', self.authorization(contract))
        self.assertEqual(status, 0)
        target = output / 'SCALE-01' / run.RECEIPT_NAME
        target.unlink()  # Synthetic temp fixture: create an incomplete case directory.
        before = run.file_hash(output / 'run_contract.json')
        argv, _, _ = self.arguments('partial-live', '--resume')
        with self.assertRaisesRegex(ValueError, 'RESUME_INCOMPLETE_CASE_DIRECTORY.*new sibling'):
            run.main(argv)
        self.assertEqual(run.file_hash(output / 'run_contract.json'), before)
        self.assertFalse(target.exists())
        self.assertEqual(len(self.processed), 10)
        self.provider.assert_not_called()

    def test_CE_REAL023_tampered_summary_and_contract_rejected(self):
        for kind in ('summary', 'contract', 'case_contract'):
            with self.subTest(kind=kind):
                contract, _, _ = self.dry_run('tamper-preflight-' + kind)
                status, output, cache = self.live(
                    'tamper-live-' + kind, self.authorization(contract))
                self.assertEqual(status, 0)
                if kind == 'summary':
                    path = output / 'SCALE-01' / 'summary.json'
                    value = run.read(path)
                    value['discovered_events'] = 999
                    path.write_text(json.dumps(value), encoding='utf-8')
                    expected = 'CASE_EVIDENCE_HASH_MISMATCH'
                elif kind == 'contract':
                    path = output / 'run_contract.json'
                    value = run.read(path)
                    value['manifest_sha256'] = '0' * 64
                    path.write_text(json.dumps(value), encoding='utf-8')
                    expected = 'RESUME_FROZEN_CONTRACT_MISMATCH'
                else:
                    path = output / 'SCALE-01' / 'case_contract.json'
                    value = run.read(path)
                    value['semantic_model'] = 'tampered-model'
                    path.write_text(json.dumps(value), encoding='utf-8')
                    expected = 'RESUME_CASE_CONTRACT_BINDING_INVALID'
                argv, _, _ = self.arguments('tamper-live-' + kind, '--resume')
                with self.assertRaisesRegex(ValueError, expected):
                    run.main(argv)
                self.provider.assert_not_called()
        self.assertEqual(len(self.processed), 30)

    def test_CE_REAL023_case_exception_preserves_partial_failure_and_exit_nonzero(self):
        contract, _, _ = self.dry_run('failure-preflight')
        original_process = self.fake_process

        def fail_one(case_id, *args):
            if case_id == 'SCALE-02':
                raise RuntimeError('synthetic case failure')
            return original_process(case_id, *args)

        with patch.object(run, 'process_sources', side_effect=fail_one):
            status, output, cache = self.live('failure-live', self.authorization(contract))
        self.assertEqual(status, 1)
        self.assertTrue((output / 'SCALE-02' / 'failure.json').is_file())
        self.assertFalse((output / 'SCALE-02' / run.RECEIPT_NAME).exists())
        progress = run.read(output / 'summary.json')
        self.assertEqual(len(progress), 10)
        self.assertEqual(next(row for row in progress
            if row['case_id'] == 'SCALE-02')['status'], 'FAILED')
        summary = run.read(output / 'execution_summary.json')
        self.assertFalse(summary['technically_complete'])
        self.assertEqual(summary['failure_count'], 1)
        self.assertEqual(summary['case_count'], 9)
        argv, _, _ = self.arguments('failure-live', '--resume')
        with self.assertRaisesRegex(ValueError, 'RESUME_INCOMPLETE_CASE_DIRECTORY'):
            run.main(argv)
        self.provider.assert_not_called()

    def test_CE_REAL023_execution_summary_handles_incomplete_case_safely(self):
        contract, output, cache = self.dry_run('summary-only-preflight', cases=['SCALE-01'])
        cache.mkdir(mode=0o700)
        summary = run.execution_summary(output, cache)
        self.assertEqual(summary['case_count'], 0)
        self.assertEqual(summary['incomplete_case_ids'], ['SCALE-01'])
        self.assertFalse(summary['technically_complete'])
        self.assertEqual(summary['actual_provider_invocations'], 0)
        self.assertFalse(summary['clinical_release_permitted'])
        self.provider.assert_not_called()

    def test_CE_REAL023_resume_requires_existing_contract_cache_and_same_config(self):
        argv, output, cache = self.arguments('absent-resume', '--resume')
        with self.assertRaisesRegex(ValueError, 'RESUME_EXISTING_CONTRACT_REQUIRED'):
            run.main(argv)
        contract, _, _ = self.dry_run('config-preflight')
        status, output, cache = self.live('config-live', self.authorization(contract))
        self.assertEqual(status, 0)
        changed = dict(self.config, semantic_model='another-model')
        self.config_path.write_text(json.dumps(changed), encoding='utf-8')
        argv, _, _ = self.arguments('config-live', '--resume')
        with self.assertRaisesRegex(ValueError, 'RESUME_FROZEN_CONTRACT_MISMATCH:config_sha256'):
            run.main(argv)
        self.provider.assert_not_called()

    def test_CE_REAL023_resume_rejects_failed_invocation_evidence(self):
        contract, _, _ = self.dry_run('receipt-preflight')
        status, output, cache = self.live('receipt-live', self.authorization(contract))
        self.assertEqual(status, 0)
        invocation_dir = cache / 'invocations'
        invocation_dir.mkdir(mode=0o700)
        (invocation_dir / 'synthetic-failed.json').write_text(json.dumps({
            'status': 'FAILED', 'cached': False, 'attempts': [],
            'provider_invocation_occurred': True}), encoding='utf-8')
        argv, _, _ = self.arguments('receipt-live', '--resume')
        with self.assertRaisesRegex(ValueError, 'RESUME_FAILURE_EVIDENCE'):
            run.main(argv)
        summary = run.execution_summary(output, cache)
        self.assertFalse(summary['technically_complete'])
        self.assertEqual(summary['failure_count'], 1)
        self.provider.assert_not_called()

    def test_CE_REAL023_worker_counts_and_feedback_config_validated_before_execution(self):
        argv, _, _ = self.arguments('bad-workers', '--dry-run', '--case-workers', '0')
        with self.assertRaisesRegex(ValueError, 'WORKER_COUNTS'):
            run.main(argv)
        bad = dict(self.config, review_feedback_max_rounds=2)
        self.config_path.write_text(json.dumps(bad), encoding='utf-8')
        argv, _, _ = self.arguments('bad-feedback', '--dry-run')
        with self.assertRaisesRegex(ValueError, 'REVIEW_FEEDBACK_MAX_ROUNDS'):
            run.main(argv)
        self.provider.assert_not_called()
        self.assertEqual(self.processed, [])


if __name__ == '__main__':
    unittest.main()
