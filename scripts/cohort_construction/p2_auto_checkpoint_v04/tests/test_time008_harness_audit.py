"""Independent V04-TIME-008 harness audit; entirely synthetic, offline.

These tests assert fail-closed behavior at channel/composition/feedback boundaries.
They do not validate any clinical case, real provider, or production delivery.
"""
from copy import deepcopy
from pathlib import Path
import json
from unittest.mock import Mock, patch
import threading

import pytest
from jsonschema import Draft202012Validator

from scripts.cohort_construction.p2_auto_checkpoint_v04 import engine
from scripts.cohort_construction.p2_auto_checkpoint_v04 import review_feedback as live
from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
from scripts.cohort_construction.p2_auto_checkpoint_v04.prompts import REVIEW_SCHEMA
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_review_feedback import setup as feedback_setup
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_two_channel_semantic_proof import (
    relation_proof, shared_direct_fixture,
)
from scripts.cohort_construction.p2_auto_checkpoint_v05_refine import refine
from scripts.cohort_construction.p2_auto_checkpoint_v05_refine.test_refine import FakeClient
from scripts.cohort_construction.p2_checkpoint_material_governance.review import MATERIAL_REVIEW_SCHEMA


class BoundReceiptClient(FakeClient):
    """Synthetic adapter whose receipt hashes the exact submitted repair payload."""
    def __init__(self, result):
        super().__init__(result)
        self.payload = None

    def ask(self, task, payload, schema):
        self.calls.append(deepcopy(dict(task=task, payload=payload)))
        self.payload = deepcopy(payload)
        receipt = dict(request_sha256=digest(payload), model='synthetic-model', task=task,
                       status='SUCCESS', cached=False)
        self.receipts.append(receipt)
        return deepcopy(self.result), receipt


def _feedback_case():
    event, previous, sources, corrected, payload = feedback_setup()
    event['semantic_review']['event_id'] = event['event_id']
    corrected['event_id'] = event['event_id']
    event['semantic_review']['previous_event_id'] = 'E0'
    corrected['previous_event_id'] = 'E0'
    event['semantic_review']['same_process_previous_id'] = None
    corrected['same_process_previous_id'] = 'E0'
    return event, previous, sources, corrected, payload


def _compile_relation_for_feedback(event, review, sources, previous):
    """Compile a real malformed EO relation, not a hand-authored field error."""
    context = event['evidence_context']
    observations = engine.resolve_model_quotes(review['observation_quotes'], sources, [],
                                               context['allowed_spans'])
    connections = engine.resolve_model_quotes(review['connection_quotes'], sources, [],
        engine._allowed_connection_ranges(event, context, sources))
    engine.compile_semantic_proof(event, review, observations, connections, context,
                                  sources, [previous, event], [])
    return event


def test_audit_initial_legacy_review_composes_without_process_crash():
    event, previous, sources, review, _ = _feedback_case()
    # Archived/minimal reviews may omit the modern comparison. Composition must
    # return a local unresolved result rather than abort the entire review unit.
    review.pop('evidence_comparison')
    observations = engine.resolve_model_quotes(review['observation_quotes'], sources, [],
                                               event['evidence_context']['allowed_spans'])
    connections = dict(valid=[], invalid=[], ambiguous=[])
    comparison, _, reasons, _ = engine.compile_semantic_proof(
        event, review, observations, connections, event['evidence_context'], sources,
        [previous, event], [])
    assert isinstance(comparison['field_errors'], list)
    assert event['semantic_proof_validation']['field_errors'] == []
    assert isinstance(reasons, list)


def test_audit_initial_and_revision_use_same_relation_coverage_outcome():
    old, current, previous, event, review, context, sources = shared_direct_fixture(second_fact=True)
    review['evidence_relations'][0]['direct_quote']['source_id'] = 'R'
    event['semantic_review'] = deepcopy(review)
    observations = engine.resolve_model_quotes(review['observation_quotes'], sources, [],
                                               context['allowed_spans'])
    connections = engine.resolve_model_quotes(review['connection_quotes'], sources, [],
        engine._allowed_connection_ranges(event, context, sources))
    comparison, _, reasons, _ = engine.compile_semantic_proof(
        event, review, observations, connections, context, sources, [previous, event], [])
    assert [row['prechoice_supported']
            for row in comparison['current_fact_coverage']['per_current_fact']] == [True, False]
    event.update(evidence_context=context, review_request_sha256='synthetic-parent', checkpoint_status='UNCERTAIN_CONTRACT_OR_SOURCE',
                 validation_reasons=reasons, provided_prior_event_ids=['E0'],
                 provided_identity_event_ids=['E0', event['event_id']])
    receipt = dict(request_sha256='synthetic-repair', task='document_checkpoint_review_repair',
                   status='SUCCESS', model='synthetic-model')
    revised = refine.revalidate(event, review, sources, [previous, event], [], receipt)
    assert revised['comparison_validation']['current_fact_coverage'] == comparison['current_fact_coverage']
    assert revised['semantic_contract_validation'] == reasons
    assert revised['evidence_relation_ledger'] == event['evidence_relation_ledger']


def test_audit_externally_superseded_ledger_cannot_make_proof_true():
    old, current, previous, event, review, context, sources = shared_direct_fixture()
    observations = engine.resolve_model_quotes(review['observation_quotes'], sources, [],
                                               context['allowed_spans'])
    connections = engine.resolve_model_quotes(review['connection_quotes'], sources, [],
        engine._allowed_connection_ranges(event, context, sources))
    forged = deepcopy(engine.compile_semantic_proof(
        deepcopy(event), review, observations, connections, context, sources,
        [previous, event], [])[3])
    forged['relations'][0]['prechoice_proof_supported'] = False
    comparison, _, reasons, _ = engine.compile_semantic_proof(
        event, review, observations, connections, context, sources, [previous, event], [],
        relation_ledger=forged)
    # Composition must not accept a caller-stale/forged ledger that contradicts
    # the exact source proof it just compiled.
    assert comparison['known_current_information_supported'] is False
    assert event['evidence_relation_ledger'] == forged


def test_audit_real_eo_channel_error_reaches_bounded_feedback():
    event, previous, sources, review, payload = _feedback_case()
    # Supply an exact CT EO, but use the current action as its narrative fact.
    # This is a technical EO-local citation defect, not a semantic no-repair case.
    report = deepcopy(sources[1])
    report['source_id'] = 'CT-SYNTHETIC'
    report['kind'] = 'imaging'
    report['clocks'] = deepcopy(sources[0]['clocks'])
    report['clock'] = deepcopy(sources[0]['clock'])
    report['text'] = '虚构原报告所见：病灶增大。'
    report['source_sha256'] = __import__('hashlib').sha256(report['text'].encode()).hexdigest()
    report['time_provenance'] = dict(report_time=dict(source_field='report_time', status='EXPLICIT_NAMED_FIELD'))
    report['clocks']['report_time'] = dict(value='2022-01-01 09:00:00', precision='minute', raw='2022-01-01 09:00')
    report['clock_basis'] = 'report_time'
    sources = sources + [report]
    old_blocks = event['actual_model_evidence']
    old_context = event['evidence_context']
    context = engine.build_evidence_context(event, sources, [previous, event], preserve_current_context=True)
    event['evidence_context'] = context
    event['actual_model_evidence'] = deepcopy(context['blocks'])
    block = next(b for b in context['blocks'] if b['source_id'] == 'CT-SYNTHETIC')
    assert block.get('publication_time', {}).get('value'), 'synthetic report must retain its publication clock'
    relation = dict(event_id=event['event_id'], evidence_object_ids=[block['evidence_object_id']],
        evidence_use='paraphrased', direct_quote=dict(source_id='NOT-AUTHORITATIVE', quote='虚构原报告所见：病灶增大。'),
        narrative_quote=dict(source_id='C', quote='决定补充检查。'), publication_before_choice='unknown',
        reading_before_choice='unknown', novelty='new', causal_role='trigger', explanation='纯虚构错误引文反例。',
        proof_event_role='evidence_informed_management_choice', temporal_channel_status='unresolved',
        semantic_channel_status='explicit_before_choice', semantic_order='before', proof_channels=['semantic_prechoice'],
        current_action_quote=dict(source_id='C', quote='决定补充检查。'),
        connection_quote=dict(source_id='C', quote=sources[1]['text']), proof_explanation='纯虚构。')
    review['evidence_relations'] = [relation]
    _compile_relation_for_feedback(event, review, sources, previous)
    event['evidence_relation_ledger']['relations'][0]['prechoice_proof_supported'] = False
    event['validation_reasons'] = list(dict.fromkeys(event.get('validation_reasons', []) +
        [row['code'] for row in event['evidence_relation_ledger']['field_errors']]))
    codes = {row['code'] for row in refine.technical_validation_errors(event)}
    assert 'SEMANTIC_PROOF_FACT_NOT_UNIQUELY_VERIFIED' in codes
    assert 'SEMANTIC_PROOF_FACT_NOT_UNIQUELY_VERIFIED' in refine.repair_triggers(event)
    # Keep the complete original request mechanically aligned with the new pool.
    payload['evidence_source_pool'] = [dict(block_id=f"{b['source_id']}:{b['start']}:{b['end']}",
        source_id=b['source_id'], text=b['quote'], start=b['start'], end=b['end'],
        evidence_object_id=b.get('evidence_object_id'), test_execution_anchor_id=b.get('test_execution_anchor_id'),
        execution_time=b.get('execution_time'), publication_time=b.get('publication_time'),
        publication_status=b.get('publication_status')) for b in context['blocks']]
    payload['events'][0]['evidence_context']['block_ids'] = [b['block_id'] for b in payload['evidence_source_pool']]
    client = FakeClient(dict(items=[review]))
    output, audit = live.run_review_feedback([event], payload, [previous, event], sources, [], client,
        {'review_feedback_max_rounds': 1})
    assert audit['attempted'] is True
    assert client.calls[0]['payload']['validation_feedback'][0]['localized_validation_errors']


def test_audit_cross_source_identical_text_without_connection_is_not_semantic_support():
    event, previous, sources, review, payload = _feedback_case()
    # The report and note contain identical words, but the proposed narrative
    # relation has no exact action/connection chain in the note.
    report = deepcopy(sources[1])
    report.update(source_id='CT-SYNTHETIC', kind='imaging', clocks=deepcopy(sources[0]['clocks']),
                  clock=deepcopy(sources[0]['clock']), text='复查提示病灶增大。')
    report['source_sha256'] = __import__('hashlib').sha256(report['text'].encode()).hexdigest()
    report['time_provenance'] = dict(report_time=dict(source_field='report_time', status='EXPLICIT_NAMED_FIELD'))
    report['clocks']['report_time'] = dict(value='2022-01-01 09:00:00', precision='minute', raw='2022-01-01 09:00')
    report['clock_basis'] = 'report_time'
    sources = sources + [report]
    context = engine.build_evidence_context(event, sources, [previous, event], preserve_current_context=True)
    event['evidence_context'] = context
    event['actual_model_evidence'] = deepcopy(context['blocks'])
    block = next(b for b in context['blocks'] if b['source_id'] == 'CT-SYNTHETIC')
    review['evidence_relations'] = [dict(event_id=event['event_id'],
        evidence_object_ids=[block['evidence_object_id']], evidence_use='paraphrased',
        direct_quote=dict(source_id='IGNORED', quote='复查提示病灶增大。'),
        narrative_quote=dict(source_id='C', quote='复查提示病灶增大。'),
        publication_before_choice='unknown', reading_before_choice='unknown', novelty='new', causal_role='trigger',
        explanation='纯虚构跨文书相同文字，不证明叙述关系。', proof_event_role='evidence_informed_management_choice',
        temporal_channel_status='unresolved', semantic_channel_status='explicit_before_choice', semantic_order='before',
        proof_channels=['semantic_prechoice'], current_action_quote=dict(source_id='C', quote='决定补充检查。'),
        connection_quote=None, proof_explanation='纯虚构。')]
    _compile_relation_for_feedback(event, review, sources, previous)
    row = event['evidence_relation_ledger']['relations'][0]
    assert row['prechoice_proof_supported'] is False
    assert 'SEMANTIC_PROOF_CONNECTION_NOT_UNIQUELY_VERIFIED' in row['validation_reasons']
    assert row['clinician_reading_established'] is False


def test_audit_success_receipt_must_hash_actual_repair_payload_live():
    event, previous, sources, corrected, payload = _feedback_case()
    client = BoundReceiptClient(dict(items=[corrected]))
    original = deepcopy(event)
    revised, audit = live.run_review_feedback([event], payload, [previous, event], sources, [], client,
        {'review_feedback_max_rounds': 1})
    assert client.payload is not None
    assert audit['receipt']['request_sha256'] == digest(client.payload)
    assert audit['receipt']['request_sha256'] != original['review_request_sha256']
    assert revised[0]['review_request_sha256'] == digest(client.payload)


def test_audit_success_receipt_must_hash_actual_repair_payload_archived():
    event, previous, sources, corrected, payload = _feedback_case()
    client = BoundReceiptClient(dict(items=[corrected]))
    from unittest.mock import patch
    with patch.object(refine, 'original_payload', return_value=payload):
        revised, audit = refine.repair_group([event], [previous, event], sources, 'unused',
            dict(semantic_model='synthetic-model'), client)
    assert client.payload is not None
    assert audit['receipt']['request_sha256'] == digest(client.payload)
    assert revised[0]['review_request_sha256'] == digest(client.payload)


class WrongReceiptClient(BoundReceiptClient):
    """Return a superficially successful receipt for a different request."""
    def ask(self, task, payload, schema):
        self.calls.append(deepcopy(dict(task=task, payload=payload)))
        self.payload = deepcopy(payload)
        receipt = dict(request_sha256='repair-hash', model='synthetic-model', task=task,
                       status='SUCCESS', cached=False)
        self.receipts.append(receipt)
        return deepcopy(self.result), receipt


@pytest.mark.parametrize('archived', [False, True], ids=['live', 'archived'])
def test_audit_wrong_payload_success_receipt_fails_closed(archived):
    event, previous, sources, corrected, payload = _feedback_case()
    client = WrongReceiptClient(dict(items=[corrected]))
    if archived:
        with patch.object(refine, 'original_payload', return_value=payload):
            revised, audit = refine.repair_group([event], [previous, event], sources, 'unused',
                dict(semantic_model='qwen3.8-max'), client)
    else:
        revised, audit = live.run_review_feedback([event], payload, [previous, event], sources, [], client,
            {'review_feedback_max_rounds': 1})
    assert audit['status'] == 'REPAIR_NOT_APPLIED'
    assert revised[0]['semantic_review'] == event['semantic_review']
    assert revised[0]['review_refinement']['status'] == 'REPAIR_NOT_APPLIED'


@pytest.mark.parametrize('archived', [False, True], ids=['live', 'archived'])
def test_audit_unknown_receipt_origin_fails_closed(archived):
    class UnknownOriginClient(BoundReceiptClient):
        def ask(self, task, payload, schema):
            result, receipt = super().ask(task, payload, schema)
            receipt['receipt_origin'] = 'UNREGISTERED_ORIGIN'
            return result, receipt
    event, previous, sources, corrected, payload = _feedback_case()
    client = UnknownOriginClient(dict(items=[corrected]))
    if archived:
        with patch.object(refine, 'original_payload', return_value=payload):
            revised, audit = refine.repair_group([event], [previous, event], sources, 'unused',
                dict(semantic_model='qwen3.8-max'), client)
    else:
        revised, audit = live.run_review_feedback([event], payload, [previous, event], sources, [], client,
            {'review_feedback_max_rounds': 1})
    assert audit['status'] == 'REPAIR_NOT_APPLIED'
    assert revised[0]['semantic_review'] == event['semantic_review']
    assert revised[0]['review_refinement']['status'] == 'REPAIR_NOT_APPLIED'


def test_audit_partial_multi_target_response_preserves_only_failed_sibling():
    event, previous, sources, corrected, payload = _feedback_case()
    sibling = deepcopy(event)
    sibling['event_id'] = 'SIBLING-SYNTHETIC'
    sibling['semantic_review']['event_id'] = sibling['event_id']
    sibling['validated_observation_citations'] = deepcopy(event['validated_observation_citations'])
    sibling['review_request_sha256'] = event['review_request_sha256']
    # Force a second repair target through a structured local field defect.
    for item in (event, sibling):
        item['semantic_proof_validation'] = dict(field_errors=[dict(code='SEMANTIC_PROOF_FACT_NOT_UNIQUELY_VERIFIED',
            event_id=item['event_id'], evidence_object_id=None, field_path='observation_availability.observation',
            source_id='C', citation_index=0, span=None, rule='V04-TIME-008', repairable=True,
            correction='Synthetic exact citation repair only.')])
    second_input = deepcopy(payload['events'][0])
    second_input['event']['event_id'] = sibling['event_id']
    payload['events'].append(second_input)
    sibling_original = deepcopy(sibling)
    client = BoundReceiptClient(dict(items=[corrected]))
    revised, audit = live.run_review_feedback([event, sibling], payload, [previous, event, sibling], sources,
        [], client, {'review_feedback_max_rounds': 1})
    assert revised[0]['review_refinement']['status'] == 'MODEL_REVISION_VALIDATED'
    assert revised[1]['semantic_review'] == sibling_original['semantic_review']
    assert revised[1]['review_refinement']['status'] == 'TARGET_MISSING_OR_DUPLICATED'


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'wrong_enum'])
def test_audit_modern_review_and_material_schemas_reject_contract_drift(mutation):
    event, previous, sources, review, _ = _feedback_case()
    review['connection_quotes'] = [dict(source_id='C', quote=sources[1]['text'])]
    if mutation == 'missing':
        review['observation_availability'][0].pop('semantic_order')
    elif mutation == 'extra':
        review['unregistered_authority'] = 'synthetic-extra'
    else:
        review['observation_availability'][0]['semantic_order'] = 'before'
        review['evidence_connection'] = 'not-an-enum'
    with pytest.raises(Exception):
        Draft202012Validator(REVIEW_SCHEMA).validate(dict(items=[review]))
    # Closed material schema: a valid minimal response is not required here;
    # direct additionalProperties inspection guards the modern contract itself.
    assert MATERIAL_REVIEW_SCHEMA['additionalProperties'] is False


def test_audit_exact_citation_negative_injections():
    source = dict(source_id='SYNTHETIC', text='重复。重复。决定暂缓。',
                  source_sha256=__import__('hashlib').sha256('重复。重复。决定暂缓。'.encode()).hexdigest())
    cases = [dict(source_id='SYNTHETIC', quote=''),
             dict(source_id='WRONG', quote='决定暂缓。'),
             dict(source_id='SYNTHETIC', quote='重复。'),
             dict(source_id='SYNTHETIC', quote='原文不存在。')]
    result = engine.resolve_model_quotes(cases, [source], [])
    assert not result['valid'] and len(result['invalid']) + len(result['ambiguous']) == 4


def test_audit_clock_conflict_and_after_choice_cannot_be_relabelled():
    event, sources, context, proposed, ledger = relation_proof(publication=dict(
        value='2022-09-01 10:00:00', precision='minute', raw='2022-09-01 10:00'))
    proposed['evidence_relations'][0].update(semantic_order='after', semantic_channel_status='after_choice',
        proof_channels=['semantic_prechoice'])
    proposed['evidence_relations'][0]['temporal_channel_status'] = 'conflict'
    event['actual_model_evidence'] = deepcopy(context['blocks'])
    rows = engine.validate_evidence_relations(event, proposed, context, sources, [])
    row = engine.compile_evidence_relations(event, rows, sources, context=context)['relations'][0]
    assert row['prechoice_proof_supported'] is False
    assert 'SEMANTIC_PROOF_TEMPORAL_CHANNEL_CONFLICT' in row['validation_reasons']
    assert 'SEMANTIC_PROOF_POSTCHOICE_CONTRADICTION' not in row['validation_reasons']



@pytest.mark.parametrize('mutation,expected_error', [
    ('request_body', 'PROVIDER_CACHE_REQUEST_BODY_HASH_MISMATCH'),
    ('receipt_hash', 'PROVIDER_CACHE_RECEIPT_REQUEST_HASH_MISMATCH'),
    ('receipt_status', 'PROVIDER_CACHE_SUCCESS_STATUS_REQUIRED'),
])
def test_audit_client_stale_cache_request_and_receipt_identity(tmp_path, mutation, expected_error):
    from scripts.cohort_construction.model_api import ModelAPISettings
    from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport
    schema = dict(type='object', required=['ok'], properties=dict(ok=dict(type='boolean')))
    settings = ModelAPISettings('credential-sentinel', 'https://example.invalid/v1', 'synthetic-model')
    config = dict(model_temperature=0, max_output_tokens=32, max_http_attempts_per_request=1)
    response = Mock(status_code=200)
    envelope = dict(choices=[dict(finish_reason='stop', message=dict(content='{"ok":true}'))])
    response.text = json.dumps(envelope)
    response.json.return_value = envelope
    session = Mock(); session.post.return_value = response
    with patch.object(transport, '_thread', threading.local()), \
            patch.object(transport.requests, 'Session', return_value=session):
        provider = transport.Client(tmp_path / 'cache', config, settings)
        _, receipt = provider.ask('synthetic-task', dict(synthetic=True), schema)
        path = tmp_path / 'cache' / (receipt['request_sha256'] + '.json')
        original = json.loads(path.read_text())
        # All cryptographic result/evidence hashes remain untouched; only the
        # saved request or receipt identity is stale. Cache replay must reject it.
        saved = deepcopy(original)
        if mutation == 'request_body':
            saved['request']['model'] = 'stale-model'
        elif mutation == 'receipt_hash':
            saved['receipt']['request_sha256'] = '0' * 64
        else:
            saved['receipt']['status'] = 'FAILED'
        path.write_text(json.dumps(saved))
        with pytest.raises(Exception, match=expected_error):
            provider.ask('synthetic-task', dict(synthetic=True), schema)


def test_audit_real_report_time_mismatch_survives_composition():
    event, sources, context, proposed, _ = relation_proof(use='direct', publication=dict(
        value='2022-09-01 10:00:00', precision='minute', raw='2022-09-01 10:00'))
    relation = proposed['evidence_relations'][0]
    # Positive provider evidence binds a valid publication before an exact choice.
    # Inject the opposite model declaration without altering that independently
    # supplied clock. The final row must not report the time channel as supported.
    relation['publication_before_choice'] = 'no'
    event['actual_model_evidence'] = deepcopy(context['blocks'])
    rows = engine.validate_evidence_relations(event, proposed, context, sources, [])
    ledger = engine.compile_evidence_relations(event, rows, sources, context=context)
    row = ledger['relations'][0]
    assert row['publication_before_choice_verified'] is True
    assert row['prechoice_proof_supported'] is False
    assert row['supported_proof_channels'] == []
    assert row['temporal_conflict_present'] is True
    assert 'EVIDENCE_NOT_PUBLISHED_BEFORE_CHOICE' in row['validation_reasons']
    assert 'SEMANTIC_PROOF_TEMPORAL_CHANNEL_CONFLICT' in row['validation_reasons']


def test_audit_real_postchoice_relation_does_not_request_semantic_repair():
    event, previous, sources, review, payload = _feedback_case()
    report = deepcopy(sources[1])
    report.update(source_id='CT-SYNTHETIC', kind='imaging', clocks=deepcopy(sources[0]['clocks']),
                  clock=deepcopy(sources[0]['clock']), text='虚构原报告所见：病灶增大。')
    report['source_sha256'] = __import__('hashlib').sha256(report['text'].encode()).hexdigest()
    report['time_provenance'] = dict(report_time=dict(source_field='report_time', status='EXPLICIT_NAMED_FIELD'))
    report['clocks']['report_time'] = dict(value='2022-01-01 09:00:00', precision='minute', raw='2022-01-01 09:00')
    report['clock_basis'] = 'report_time'
    sources = sources + [report]
    context = engine.build_evidence_context(event, sources, [previous, event], preserve_current_context=True)
    event['evidence_context'] = context
    event['actual_model_evidence'] = deepcopy(context['blocks'])
    block = next(b for b in context['blocks'] if b['source_id'] == 'CT-SYNTHETIC')
    relation = dict(event_id=event['event_id'], evidence_object_ids=[block['evidence_object_id']],
        evidence_use='paraphrased', direct_quote=dict(source_id='CT-SYNTHETIC', quote=report['text']),
        narrative_quote=dict(source_id='C', quote='决定补充检查。'), publication_before_choice='unknown',
        reading_before_choice='unknown', novelty='new', causal_role='trigger', explanation='纯虚构选择后事实反例。',
        proof_event_role='postchoice_record', temporal_channel_status='unresolved',
        semantic_channel_status='explicit_before_choice', semantic_order='before',
        proof_channels=['semantic_prechoice'], current_action_quote=dict(source_id='C', quote='决定补充检查。'),
        connection_quote=dict(source_id='C', quote=sources[1]['text']), proof_explanation='纯虚构。')
    review['evidence_relations'] = [relation]
    _compile_relation_for_feedback(event, review, sources, previous)
    codes = {row['code'] for row in event['evidence_relation_ledger']['field_errors']}
    assert 'SEMANTIC_PROOF_POSTCHOICE_CONTRADICTION' in codes
    assert refine.technical_validation_errors(event) == []
    assert 'SEMANTIC_PROOF_POSTCHOICE_CONTRADICTION' not in refine.repair_triggers(event)


def test_audit_provider_repair_receipt_task_is_not_substitutable():
    from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_time008_provider_repair_receipt import (
        ProviderRepairClient,
    )
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        event, previous, sources, corrected, payload = _feedback_case()
        provider = ProviderRepairClient(Path(directory) / 'cache', corrected)
        try:
            result, receipt = provider.ask('document_checkpoint_review_repair', payload, REVIEW_SCHEMA)
            forged = deepcopy(receipt)
            forged['task'] = 'document_event_discovery'
            assert not refine._valid_success_receipt(forged, payload, result)
        finally:
            provider.close()


@pytest.mark.parametrize('archived', [False, True], ids=['live', 'archived'])
def test_audit_repair_cannot_expand_scope_via_time_quote(archived):
    event, previous, sources, corrected, payload = _feedback_case()
    corrected['time_quotes'] = [dict(source_id='P', quote=sources[0]['text'])]
    corrected['same_event_with'] = ['P-SYNTHETIC-FOR-IMMUTABLE-GUARD']
    client = BoundReceiptClient(dict(items=[corrected]))
    if archived:
        with patch.object(refine, 'original_payload', return_value=payload):
            revised, audit = refine.repair_group([event], [previous, event], sources, 'unused',
                dict(semantic_model='synthetic-model'), client)
    else:
        revised, audit = live.run_review_feedback([event], payload, [previous, event], sources, [], client,
            {'review_feedback_max_rounds': 1})
    assert revised[0]['semantic_review'] == event['semantic_review']
    assert revised[0]['review_refinement']['status'] in ('REPAIR_REVALIDATION_FAILED', 'REPAIR_NOT_APPLIED')


def test_audit_mislocalized_relation_error_cannot_repair_other_event():
    event, previous, sources, corrected, payload = _feedback_case()
    sibling = deepcopy(event)
    sibling['event_id'] = 'SIBLING-SYNTHETIC'
    sibling['semantic_review']['event_id'] = sibling['event_id']
    sibling['validated_observation_citations'] = deepcopy(event['validated_observation_citations'])
    sibling['review_request_sha256'] = event['review_request_sha256']
    defect = dict(code='SEMANTIC_PROOF_FACT_NOT_UNIQUELY_VERIFIED',
        event_id='SIBLING-SYNTHETIC', evidence_object_id='EO-SIBLING-ONLY',
        field_path='evidence_relations.narrative_quote', source_id=None,
        citation_index=None, span=None, rule='V04-TIME-008', repairable=True,
        correction='Synthetic localization counterexample.')
    event['semantic_proof_validation'] = dict(field_errors=[dict(
        code='SEMANTIC_PROOF_FACT_NOT_UNIQUELY_VERIFIED', event_id=event['event_id'],
        evidence_object_id=None, field_path='observation_availability.observation',
        source_id='C', citation_index=0, span=None, rule='V04-TIME-008', repairable=True,
        correction='Synthetic exact citation repair only.')])
    event['evidence_relation_ledger'] = dict(field_errors=[defect])
    event['evidence_relation_validation_errors'] = [defect]
    for item in (event, sibling):
        item['semantic_review']['observation_quotes'] = []
        item['observation_availability'] = []
        item['validation_reasons'] = []
        item['validated_observation_citations'] = dict(valid=[], invalid=[], ambiguous=[])
    sibling_original = deepcopy(sibling)
    second_input = deepcopy(payload['events'][0])
    second_input['event']['event_id'] = sibling['event_id']
    payload['events'].append(second_input)
    feedback = refine.feedback(event)
    assert [row for row in feedback['localized_validation_errors']
            if row['field_path'] == 'evidence_relations.narrative_quote'] == []
    revised, audit = live.run_review_feedback([event, sibling], payload,
        [previous, event, sibling], sources, [], BoundReceiptClient(dict(items=[corrected])),
        {'review_feedback_max_rounds': 1})
    assert revised[0]['review_refinement']['status'] == 'MODEL_REVISION_VALIDATED'
    assert revised[1] == sibling_original
    assert revised[1]['semantic_review'] == sibling_original['semantic_review']
    assert 'review_refinement' not in revised[1]


def test_audit_semantic_channel_tolerates_unresolved_publication_without_clock_conflict():
    _, _, _, _, ledger = relation_proof()
    row = ledger['relations'][0]
    assert row['prechoice_proof_supported'] is True
    assert row['publication_before_choice_verified'] is False
    assert row['temporal_conflict_present'] is False
    assert 'NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED' in row['validation_reasons']
    assert 'NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED' in row['temporal_only_reasons']
    assert 'NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED' in ledger['validation_reasons']
    assert row['clinician_reading_established'] is False
