"""V04-TIME-008 production-shaped repair receipt integration; offline/synthetic."""
from copy import deepcopy
import json
import threading
from unittest.mock import Mock, patch

import pytest

from scripts.cohort_construction.model_api import ModelAPISettings
from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport
from scripts.cohort_construction.p2_auto_checkpoint_v04 import review_feedback as live
from scripts.cohort_construction.p2_auto_checkpoint_v04.prompts import REVIEW_SCHEMA
from scripts.cohort_construction.p2_auto_checkpoint_v05_refine import refine
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_time008_harness_audit import _feedback_case


class ProviderRepairClient:
    """Real Client request/receipt construction; the HTTP session is a local double."""
    def __init__(self, cache, corrected):
        self.corrected = deepcopy(corrected)
        self.calls = []
        self.settings = ModelAPISettings(
            'synthetic-credential', 'https://example.invalid/v1', 'synthetic-model')
        envelope = dict(choices=[dict(finish_reason='stop', message=dict(
            content=json.dumps(dict(items=[self.corrected]), ensure_ascii=False)))])
        response = Mock(status_code=200)
        response.text = json.dumps(envelope, ensure_ascii=False)
        response.json.return_value = envelope
        session = Mock()
        session.post.return_value = response
        self._patches = (
            patch.object(transport, '_thread', threading.local()),
            patch.object(transport.requests, 'Session', return_value=session),
        )
        for control in self._patches:
            control.start()
        self.client = transport.Client(cache, dict(
            model_temperature=0, max_output_tokens=8192,
            max_http_attempts_per_request=1), self.settings)

    def ask(self, task, payload, schema):
        self.calls.append(deepcopy(dict(task=task, payload=payload)))
        return self.client.ask(task, payload, schema)

    def close(self):
        for control in reversed(self._patches):
            control.stop()


@pytest.mark.parametrize('mode', ['fresh-live', 'fresh-archived', 'cached-live', 'cached-archived'])
def test_TIME008_production_transport_binds_repair_without_cross_domain_hash(mode, tmp_path):
    event, previous, sources, corrected, payload = _feedback_case()
    provider = ProviderRepairClient(tmp_path / 'cache', corrected)
    archived = mode.endswith('archived')
    try:
        def run_once():
            if archived:
                with patch.object(refine, 'original_payload', return_value=payload):
                    return refine.repair_group([event], [previous, event], sources, 'unused',
                        dict(semantic_model='synthetic-model'), provider)
            return live.run_review_feedback([event], payload, [previous, event], sources, [],
                provider, {'review_feedback_max_rounds': 1})

        revised, audit = run_once()
        assert audit['status'] == 'COMPLETE', audit
        assert revised[0]['checkpoint_status'] == 'CANDIDATE_NEW_EVIDENCE'
        receipt = audit['receipt']
        assert receipt['receipt_origin'] == 'PROVIDER_CLIENT'
        assert receipt['request_sha256'] != digest(provider.calls[0]['payload'])
        assert receipt['declared_provider_payload_sha256'] == canonical_sha256(
            provider.calls[0]['payload'])
        provenance = audit['provenance']['repair']
        assert provenance['request_hash_domain'] == 'transport_endpoint_and_http_body'
        assert provenance['repair_payload_sha256'] == digest(provider.calls[0]['payload'])
        assert provenance['cache_identity']['key_algorithm'] == 'digest([endpoint, request])'

        if mode.startswith('cached'):
            # Keep a byte-identical prepared payload so the real cache key is stable.
            with patch.object(refine, 'prepare_payload', side_effect=lambda original, targets, names: deepcopy(payload)) as prepare:
                prepare.side_effect = lambda original, targets, names: deepcopy(provider.calls[0]['payload'])
                cached_revised, cached_audit = run_once()
            receipt = cached_audit['receipt']
            assert receipt['receipt_origin'] == 'PROVIDER_CLIENT_CACHE'
            assert receipt['cached'] is True and receipt['provider_invocation_occurred'] is False
            assert receipt['declared_provider_payload_sha256'] == canonical_sha256(
                provider.calls[-1]['payload'])
            assert cached_audit['status'] == 'COMPLETE'
            assert cached_revised[0]['checkpoint_status'] == 'CANDIDATE_NEW_EVIDENCE'
            assert len(provider.client.receipts) == 2
    finally:
        provider.close()


def test_TIME008_production_receipt_rejects_cross_domain_and_result_substitution(tmp_path):
    event, previous, sources, corrected, payload = _feedback_case()
    provider = ProviderRepairClient(tmp_path / 'cache', corrected)
    try:
        result, receipt = provider.ask('document_checkpoint_review_repair', payload, REVIEW_SCHEMA)
        assert refine._valid_success_receipt(receipt, payload, result)
        wrong = deepcopy(payload)
        wrong['synthetic_substitution'] = True
        assert not refine._valid_success_receipt(receipt, wrong, result)
        for field in ('status', 'task'):
            forged = deepcopy(receipt)
            forged[field] = 'FAILED' if field == 'status' else 'document_event_discovery'
            assert not refine._valid_success_receipt(forged, payload, result)
        altered_result = deepcopy(result)
        altered_result['items'] = []
        assert not refine._valid_success_receipt(receipt, payload, altered_result)
        # The transport key is intentionally not the canonical payload digest.
        forged = deepcopy(receipt)
        forged.pop('terminal_attempt_evidence')
        forged['request_sha256'] = digest(payload)
        assert not refine._valid_success_receipt(forged, payload, result)
        mismatched_evidence = deepcopy(receipt)
        mismatched_evidence['terminal_attempt_evidence']['declared_provider_payload_sha256'] = digest(payload)
        assert not refine._valid_success_receipt(mismatched_evidence, payload, result)
    finally:
        provider.close()
