"""Synthetic one-pass repair controls; no model service or clinical references."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
import tempfile
import json

from scripts.cohort_construction.p2_auto_checkpoint_v04.engine import resolve_model_quotes
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_engine import quotes
from scripts.cohort_construction.p2_auto_checkpoint_v04.prompts import UNCERTAINTY_KEYS
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_v05_semantic_contract import fixture
from . import refine


class FakeClient:
    def __init__(self, result=None, failure=False):
        self.result, self.failure, self.calls, self.receipts = result, failure, [], []
        self.settings = SimpleNamespace(chat_completions_url='https://example.invalid/v1/chat/completions')

    def ask(self, task, payload, schema):
        self.calls.append(deepcopy(dict(task=task, payload=payload)))
        if self.failure:
            raise RuntimeError('synthetic transport failure')
        receipt = dict(request_sha256=refine.digest(payload), model='qwen3.8-max', task=task,
                       cached=False)
        self.receipts.append(receipt)
        return deepcopy(self.result), receipt


def setup():
    old, cur, previous, e, corrected, ctx = fixture()
    corrected['connection_quotes'] = [dict(source_id='C', quote=cur['text'])]
    # CE-REM-002/003: migrate the positive synthetic proposal to the new required
    # schema while retaining the legacy evidence-informed proof obligations.
    corrected['checkpoint_kind'] = 'evidence_informed_choice'
    corrected['evidence_relations'] = []
    corrected['uncertainty_dimensions'] = {key: dict(status='uncertain', reason_category='genuine_missing_information',
        explanation='Synthetic fixture preserves unspecified uncertainty.', source_quotes=[]) for key in UNCERTAINTY_KEYS}
    e.update(evidence_context=ctx, actual_model_evidence=deepcopy(ctx['blocks']),
        choice_boundary_citations=quotes(cur, '决定补充检查。'),
        provided_prior_event_ids=['E0'], provided_identity_event_ids=['E0', 'E1'],
        review_request_sha256='original-hash', validation_reasons=['NO_VALID_PRIOR_OBSERVATION'],
        checkpoint_status='UNCERTAIN_CONTRACT_OR_SOURCE')
    broad = dict(source_id='C', quote=cur['text'])
    initial = deepcopy(corrected)
    initial['observation_quotes'] = [broad]
    e['semantic_review'] = initial
    e['validated_observation_citations'] = resolve_model_quotes([broad], [old, cur], [], ctx['allowed_spans'])
    e['validated_connection_citations'] = dict(valid=[], ambiguous=[], invalid=[])
    pool = [dict(block_id=f"{b['source_id']}:{b['start']}:{b['end']}", source_id=b['source_id'],
                 text=b['text'], start=b['start'], end=b['end']) for b in ctx['blocks']]
    payload = dict(instruction='Original frozen instructions.',
        events=[dict(event=dict(event_id='E1'), evidence_context=dict(block_ids=[b['block_id'] for b in pool],
                     eligible_previous_event_ids=['E0']))],
        evidence_source_pool=pool, earlier_event_ledger=[dict(event_id='E0')],
        current_full_document=[dict(source_id='C', text=cur['text'])])
    return e, previous, [old, cur], corrected, payload


def perform(e, prev, sources, corrected, payload, result=None, failure=False):
    client = FakeClient(result or dict(items=[corrected]), failure)
    with patch.object(refine, 'original_payload', return_value=payload):
        revised, audit = refine.repair_group([e], [prev, e], sources, 'unused',
            dict(semantic_model='qwen3.8-max'), client)
    return revised[0], audit, client


class RefinementTests(unittest.TestCase):
    def test_broad_action_quote_can_be_corrected_by_model_without_clipping(self):
        e, prev, sources, corrected, payload = setup()
        original = deepcopy(e)
        repaired, audit, client = perform(e, prev, sources, corrected, payload)
        self.assertEqual(repaired['checkpoint_status'], 'CANDIDATE_NEW_EVIDENCE')
        self.assertEqual(repaired['review_request_sha256'], refine.digest(client.calls[0]['payload']))
        self.assertEqual(repaired['semantic_review_before_refinement'], original['semantic_review'])
        self.assertEqual(e, original)
        self.assertEqual(repaired['actual_model_evidence'], original['actual_model_evidence'])
        self.assertEqual(len(client.calls), 1)
        sent = client.calls[0]['payload']
        self.assertEqual(sent['evidence_source_pool'], payload['evidence_source_pool'])
        self.assertEqual(sent['earlier_event_ledger'], payload['earlier_event_ledger'])
        self.assertEqual(sent['current_full_document'], payload['current_full_document'])
        self.assertEqual(audit['result']['items'][0], corrected)

    def test_bad_repair_keeps_failed_proof_uncertain(self):
        e, prev, sources, corrected, payload = setup()
        bad = deepcopy(corrected)
        bad['observation_quotes'] = [dict(source_id='C', quote=sources[1]['text'])]
        bad['observation_availability'][0]['observation'] = bad['observation_quotes'][0]
        repaired, audit, client = perform(e, prev, sources, bad, payload)
        self.assertTrue(repaired['checkpoint_status'].startswith('UNCERTAIN'))
        self.assertIn('NO_VALID_PRIOR_OBSERVATION', repaired['validation_reasons'])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(refine.repair_triggers(repaired), [])

    def test_transport_failure_cannot_create_positive(self):
        e, prev, sources, corrected, payload = setup()
        repaired, audit, client = perform(e, prev, sources, corrected, payload, failure=True)
        self.assertEqual(repaired['checkpoint_status'], e['checkpoint_status'])
        self.assertEqual(repaired['semantic_review'], e['semantic_review'])
        self.assertEqual(repaired['review_request_sha256'], 'original-hash')
        self.assertEqual(audit['status'], 'REPAIR_NOT_APPLIED')
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(refine.repair_triggers(repaired), [])

    def test_CEREV001_preprovider_failure_is_not_a_provider_round_or_shared_receipt(self):
        e, prev, sources, corrected, payload = setup()
        payload['evidence_source_pool'][0]['text'] = 'tampered before provider'
        unrelated = dict(request_sha256='another-concurrent-request', task='document_checkpoint_review_repair',
                         status='SUCCESS')
        client = FakeClient(dict(items=[corrected]))
        client.receipts.append(unrelated)
        with patch.object(refine, 'original_payload', return_value=payload):
            revised, audit = refine.repair_group([e], [prev, e], sources, 'unused',
                dict(semantic_model='qwen3.8-max'), client)
        self.assertFalse(client.calls)
        self.assertIsNone(audit['receipt'])
        self.assertFalse(audit['attempted'])
        self.assertEqual(audit['error_code'], 'ORIGINAL_EVIDENCE_TEXT_MISMATCH')
        self.assertEqual(revised[0]['review_refinement']['status'], 'REPAIR_NOT_APPLIED')
        self.assertFalse(revised[0]['review_refinement']['attempted'])
        self.assertEqual(revised[0]['review_refinement']['rounds'], 0)

    def test_duplicate_or_missing_target_preserves_original(self):
        e, prev, sources, corrected, payload = setup()
        for items in [[], [corrected, corrected]]:
            with self.subTest(count=len(items)):
                repaired, audit, client = perform(e, prev, sources, corrected, payload, result=dict(items=items))
                self.assertEqual(repaired['checkpoint_status'], e['checkpoint_status'])
                self.assertEqual(repaired['review_request_sha256'], 'original-hash')
                self.assertEqual(repaired['review_refinement']['status'], 'TARGET_MISSING_OR_DUPLICATED')

    def test_repair_can_remain_semantically_uncertain(self):
        e, prev, sources, corrected, payload = setup()
        corrected['checkpoint'] = 'uncertain'
        repaired, audit, client = perform(e, prev, sources, corrected, payload)
        self.assertEqual(repaired['checkpoint_status'], 'UNCERTAIN_SEMANTIC_JUDGMENT')
        self.assertEqual(repaired['review_request_sha256'], refine.digest(client.calls[0]['payload']))

    def test_pure_semantic_uncertainty_and_valid_proxy_availability_not_targeted(self):
        e, prev, sources, corrected, payload = setup()
        e['validation_reasons'] = []
        e['validated_observation_citations'] = dict(valid=[], invalid=[], ambiguous=[])
        self.assertEqual(refine.repair_triggers(e), [])
        e['validation_reasons'] = ['NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED']
        corrected['observation_availability'][0]['availability'] = 'record_proxy_only'
        e['semantic_review'] = corrected
        e['availability_validation'] = [dict(corrected['observation_availability'][0], is_declared_observation=True,
            eligible_as_known_prechoice=False, validated_observation=dict(invalid=[]), validated_basis=dict(invalid=[]))]
        self.assertEqual(refine.repair_triggers(e), [])

    def test_original_evidence_mismatch_does_not_call_model(self):
        e, prev, sources, corrected, payload = setup()
        payload['events'][0]['evidence_context']['block_ids'] = []
        repaired, audit, client = perform(e, prev, sources, corrected, payload)
        self.assertEqual(client.calls, [])
        self.assertEqual(repaired['checkpoint_status'], e['checkpoint_status'])

    def test_original_evidence_text_coordinates_and_allowed_ranges_verified(self):
        for mutation in ('text', 'coordinates', 'allowed'):
            e, prev, sources, corrected, payload = setup()
            with self.subTest(mutation=mutation):
                if mutation == 'text':
                    payload['evidence_source_pool'][0]['text'] += 'Unprovided fact.'
                elif mutation == 'coordinates':
                    payload['evidence_source_pool'][0]['start'] += 1
                else:
                    sid = next(iter(e['evidence_context']['allowed_spans']))
                    e['evidence_context']['allowed_spans'][sid][0]['start'] += 1
                repaired, audit, client = perform(e, prev, sources, corrected, payload)
                self.assertEqual(client.calls, [])
                self.assertEqual(repaired['checkpoint_status'], e['checkpoint_status'])

    def test_original_cache_key_authenticates_endpoint_and_request_body(self):
        endpoint = 'https://example.invalid/v1/chat/completions'
        request = dict(model='qwen3.8-max', messages=[dict(content='system'), dict(content=json.dumps(
            dict(task='document_checkpoint_review', input={'sentinel': 'original'})))])
        result = dict(items=[])
        key = refine.digest([endpoint, request])
        saved = dict(request=request, result=result, result_sha256=refine.digest(result),
                     receipt=dict(request_sha256=key))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / (key + '.json')
            path.write_text(json.dumps(saved))
            self.assertEqual(refine.original_payload(directory, key, 'qwen3.8-max', endpoint), {'sentinel': 'original'})
            with self.assertRaisesRegex(ValueError, 'ORIGINAL_REQUEST_BODY_HASH_MISMATCH'):
                refine.original_payload(directory, key, 'qwen3.8-max', 'https://other.invalid/v1/chat/completions')
            saved['request']['messages'][1]['content'] += ' '
            path.write_text(json.dumps(saved))
            with self.assertRaisesRegex(ValueError, 'ORIGINAL_REQUEST_BODY_HASH_MISMATCH'):
                refine.original_payload(directory, key, 'qwen3.8-max', endpoint)

    def test_endpoint_with_credentials_or_query_not_recorded(self):
        for url in ['https://user:secret@example.invalid/v1', 'https://example.invalid/v1?key=secret']:
            client = FakeClient()
            client.settings.chat_completions_url = url
            with self.subTest(url_kind='query' if '?' in url else 'userinfo'):
                with self.assertRaisesRegex(ValueError, 'UNSAFE_ENDPOINT'):
                    refine.request_endpoint(client)

    def test_refined_case_preserves_discovery_bytes_and_uses_discovery_events(self):
        e, prev, sources, corrected, payload = setup()
        originals = [prev, e]
        for s in sources:
            s['patient_uid'] = 'SCALE-SYNTHETIC'
        discovery = dict(events=[dict(event_id='DISCOVERY_SENTINEL')])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input'
            source.mkdir()
            content = {'decision_events.json': json.dumps(originals, ensure_ascii=False),
                'source_fhir_r4_bundle.json': '{}', 'model_receipts.json': '[]',
                'discovery.json': json.dumps(discovery, ensure_ascii=False) + '\n',
                'unit_inventory.json': '{"units":[]}\n', 'source_import_receipt.json': '{"hash":"sentinel"}\n'}
            for name, text in content.items():
                (source / name).write_text(text)
            out = Path(directory) / 'output'
            group_result = ([deepcopy(e)], dict(receipt=None))
            with patch.object(refine, 'import_sources', return_value=(sources, {})), \
                 patch.object(refine, 'build', return_value={}), patch.object(refine, 'validate', return_value={}), \
                 patch.object(refine, 'assemble_processes', return_value=([], [])), \
                 patch.object(refine, 'repair_group', return_value=group_result) as group:
                refine.refine_case(source, out, 'unused', {'semantic_model':'qwen3.8-max'}, FakeClient(), 'unused')
            self.assertEqual(group.call_args.args[1], discovery['events'])
            for name in ('discovery.json', 'unit_inventory.json', 'source_import_receipt.json'):
                self.assertEqual((out / name).read_bytes(), (source / name).read_bytes())
            self.assertEqual((out / 'original_decision_events.json').read_bytes(), (source / 'decision_events.json').read_bytes())
            manifest = json.loads((out / 'refinement_manifest.json').read_text())
            self.assertTrue(manifest['revalidation_uses_original_discovery_events'])
            self.assertEqual(manifest['preserved_file_sha256']['discovery.json'], manifest['original_discovery_sha256'])

    def test_valid_candidate_unused_bad_comparison_does_not_trigger_repair(self):
        e, prev, sources, corrected, payload = setup()
        e['checkpoint_status'] = 'CANDIDATE_NEW_EVIDENCE'
        e['validation_reasons'] = []
        e['comparison_validation'] = dict(previous_known=dict(invalid=[{'status':'OUTSIDE_ALLOWED_SPAN'}]))
        self.assertEqual(refine.repair_triggers(e), [])

    def test_repeat_with_irrelevant_invalid_comparison_does_not_trigger_repair(self):
        e, prev, sources, corrected, payload = setup()
        e['checkpoint_status'] = 'REPEATED_OR_EXECUTED_MENTION'
        e['semantic_review']['checkpoint'] = 'repeat'
        e['comparison_validation'] = dict(previous_known=dict(invalid=[{'status':'OUTSIDE_ALLOWED_SPAN'}]))
        self.assertEqual(refine.repair_triggers(e), [])

    def test_blocked_positive_model_self_contradictions_can_be_clarified_once(self):
        for checkpoint, reason in [('new_evidence','MODEL_EVENT_FUNCTION_CHECKPOINT_CONFLICT'),
                                   ('first_anchor','FIRST_ANCHOR_HAS_PRIOR_ROUND')]:
            with self.subTest(checkpoint=checkpoint):
                e, prev, sources, corrected, payload = setup()
                e['semantic_review']['checkpoint'] = checkpoint
                e['validation_reasons'] = [reason]
                self.assertEqual(refine.repair_triggers(e), [reason])

    def test_all_explicit_known_claims_with_failed_comparison_coverage_can_be_repaired(self):
        e, prev, sources, corrected, payload = setup()
        e['validation_reasons'] = ['NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED']
        e['validated_observation_citations'] = dict(valid=[], invalid=[], ambiguous=[])
        e['semantic_review'] = corrected
        e['availability_validation'] = [dict(corrected['observation_availability'][0],
            is_declared_observation=True, eligible_as_known_prechoice=True,
            validated_observation=dict(invalid=[]), validated_basis=dict(invalid=[]))]
        self.assertIn('EXPLICIT_KNOWN_COMPARISON_COVERAGE_MISMATCH', refine.repair_triggers(e))

    def test_invalid_repair_receipt_preserves_original_state_and_receipt(self):
        e, prev, sources, corrected, payload = setup()
        original = deepcopy(e)
        client = FakeClient({'items':[corrected]})
        def invalid_receipt(task, submitted, schema):
            result, receipt = FakeClient.ask(client, task, submitted, schema)
            receipt['status'] = 'FAILED'
            return result, receipt
        client.ask = invalid_receipt
        with patch.object(refine, 'original_payload', return_value=payload):
            revised, audit = refine.repair_group([e], [prev,e], sources, 'unused',
                {'semantic_model':'qwen3.8-max'}, client)
        self.assertEqual(revised[0]['semantic_review'], original['semantic_review'])
        self.assertEqual(revised[0]['review_refinement']['status'], 'REPAIR_NOT_APPLIED')
        self.assertEqual(audit['receipt']['request_sha256'], refine.digest(client.calls[0]['payload']))
        self.assertEqual(audit['receipt']['status'], 'FAILED')
        self.assertEqual(audit['error_code'], 'REVIEW_FEEDBACK_RECEIPT_INVALID')

    def test_cache_failure_preserves_original_state_without_model_call(self):
        e, prev, sources, corrected, payload = setup()
        original = deepcopy(e)
        client = FakeClient({'items': [corrected]})
        with patch.object(refine, 'original_payload', side_effect=OSError('synthetic cache unreadable')):
            revised, audit = refine.repair_group([e], [prev, e], sources, 'unused',
                {'semantic_model': 'qwen3.8-max'}, client)
        self.assertEqual(revised[0]['semantic_review'], original['semantic_review'])
        self.assertEqual(revised[0]['review_request_sha256'], original['review_request_sha256'])
        self.assertEqual(revised[0]['review_refinement']['status'], 'REPAIR_NOT_APPLIED')
        self.assertEqual(revised[0]['review_refinement']['error'], 'OSError')
        self.assertEqual(audit['status'], 'REPAIR_NOT_APPLIED')
        self.assertNotIn('original_review_payload', audit)
        self.assertEqual(client.calls, [])


if __name__ == '__main__':
    unittest.main()
