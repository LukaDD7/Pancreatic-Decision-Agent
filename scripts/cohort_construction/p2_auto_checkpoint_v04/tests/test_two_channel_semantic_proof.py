"""V04-TIME-008 two-channel counterexamples; synthetic source text only."""
from copy import deepcopy

from scripts.cohort_construction.p2_auto_checkpoint_v04 import engine
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_context import source as report_source
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_engine import event, quotes, select
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_evidence_chain_remediation import (
    proposal, relation_fixture,
)
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_v05_semantic_contract import fixture


def relation_proof(use='paraphrased', publication=None):
    e, sources, ctx = relation_fixture()
    imaging = next(source for source in sources if source['source_id'] == 'CT')
    imaging['clocks'].pop('report_time', None)
    if publication:
        imaging['clocks']['report_time'] = publication
        imaging['clock'] = publication
        imaging['clock_basis'] = 'report_time'
    ctx = engine.build_evidence_context(e, sources, [e], preserve_current_context=True)
    block = next(block for block in ctx['blocks'] if block['source_id'] == 'CT')
    if not publication:
        # Preserve the execution clock but remove only the independently named
        # publication field in this supplied EvidenceObject.
        block['publication_time'] = dict(value=None, precision='unknown', raw=None)
    proposed = proposal(use, dict(source_id='C', quote='CT提示静脉受侵。')
                        if use == 'paraphrased' else None)
    if not publication:
        proposed['evidence_relations'][0]['publication_before_choice'] = 'unknown'
    proposed['evidence_relations'][0]['evidence_object_ids'] = [block['evidence_object_id']]
    proposed['evidence_relations'][0]['proof_channels'] = (
        ['semantic_prechoice'] if use == 'paraphrased' else ['report_time'])
    proposed['evidence_relations'][0]['temporal_channel_status'] = (
        'temporally_possibly_visible' if publication and use == 'direct' else 'unresolved')
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    return e, sources, ctx, proposed, engine.compile_evidence_relations(e, rows, sources, context=ctx)


def test_TIME008_semantic_channel_does_not_require_or_invent_publication_time():
    _, _, _, _, ledger = relation_proof()
    row = ledger['relations'][0]
    assert row['prechoice_proof_supported'] is True
    assert row['supported_proof_channels'] == ['semantic_prechoice']
    assert row['publication_before_choice_verified'] is False
    assert row['publication_before_choice_established'] is False
    assert row['clinician_reading_established'] is False
    assert 'NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED' in row['validation_reasons']
    assert not row['source_blocking_reasons']


def test_TIME008_real_narrative_publication_conflict_cannot_be_relabelled_semantic():
    e, sources, ctx, proposed, _ = relation_proof(publication=dict(
        value='2022-09-01 10:00:00', precision='minute', raw='2022-09-01 10:00'))
    for block in ctx['blocks']:
        if block['source_id'] == 'C':
            clock = dict(value='2022-08-01 09:00:00', precision='minute', raw='2022-08-01 09:00')
            block['clock'] = clock
            next(source for source in sources if source['source_id'] == 'C')['clock'] = clock
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    row = engine.compile_evidence_relations(e, rows, sources, context=ctx)['relations'][0]
    assert row['prechoice_proof_supported'] is False
    assert row['temporal_conflict_present'] is True
    assert 'NARRATIVE_RECORD_TIME_BEFORE_EVIDENCE_PUBLICATION' in row['validation_reasons']
    assert 'SEMANTIC_PROOF_TEMPORAL_CHANNEL_CONFLICT' in row['validation_reasons']


def test_TIME008_report_time_requires_exact_choice_order_not_exam_or_narrative_proxy():
    _, _, _, _, ledger = relation_proof(use='direct', publication=dict(
        value='2022-09-01 10:00:00', precision='minute', raw='2022-09-01 10:00'))
    row = ledger['relations'][0]
    assert row['supported_proof_channels'] == ['report_time']
    assert row['prechoice_proof_supported'] is True
    assert row['publication_before_choice_established'] is True
    assert row['clinician_reading_established'] is False
    _, _, _, _, unresolved = relation_proof(use='direct')
    assert unresolved['relations'][0]['prechoice_proof_supported'] is False
    assert 'SEMANTIC_PROOF_REPORT_TIME_NOT_INDEPENDENTLY_VERIFIED' in \
        unresolved['relations'][0]['validation_reasons']


def test_TIME008_missing_exact_choice_boundary_is_not_a_proof_channel_bypass():
    for use in ('direct', 'paraphrased'):
        e, sources, ctx, proposed, _ = relation_proof(use=use, publication=dict(
            value='2022-09-01 10:00:00', precision='minute', raw='2022-09-01 10:00'))
        e['choice_boundary_citations'] = None
        e['actual_model_evidence'] = deepcopy(ctx['blocks'])
        rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
        row = engine.compile_evidence_relations(e, rows, sources, context=ctx)['relations'][0]
        assert row['prechoice_proof_supported'] is False
        assert row['supported_proof_channels'] == []
        assert 'SEMANTIC_PROOF_ACTION_NOT_EXACT_CHOICE_BOUNDARY' in row['validation_reasons']


def test_TIME008_declaration_requires_same_observation_object_not_longer_parent_quote():
    old, cur, prev, e, review, ctx = fixture()
    review['observation_quotes'] = [dict(source_id='C', quote='复查提示病灶增大。因病灶增大，')]
    validated, availability, reasons = engine.validate_semantic_contract(
        e, review, ctx, [old, cur], [prev, e], [])
    assert availability[0]['validated_observation']['valid']
    assert availability[0]['is_declared_observation'] is False
    assert availability[0]['eligible_as_known_prechoice'] is False
    assert validated['known_current_information_supported'] is False
    assert 'SEMANTIC_PROOF_OBSERVATION_NOT_DECLARED' in reasons
    review['observation_quotes'] = deepcopy(review['evidence_comparison']['current_information_quotes'])
    validated, availability, _ = engine.validate_semantic_contract(
        e, review, ctx, [old, cur], [prev, e], [])
    assert availability[0]['eligible_as_known_prechoice'] is True
    assert validated['known_current_information_supported'] is True


def test_TIME008_invalid_current_action_span_cannot_borrow_valid_connection_allowlist():
    old, cur, prev, e, review, ctx = fixture()
    whole = dict(source_id='C', quote='复查提示病灶增大。因病灶增大，决定补充检查。')
    review['observation_quotes'] = [whole]
    review['observation_availability'][0]['observation'] = whole
    review['evidence_comparison']['current_information_quotes'] = [whole]
    validated, availability, reasons = engine.validate_semantic_contract(
        e, review, ctx, [old, cur], [prev, e], [])
    assert availability[0]['validated_observation']['invalid']
    assert availability[0]['validated_connection']['valid']
    assert availability[0]['eligible_as_known_prechoice'] is False
    assert 'CURRENT_ACTION_USED_AS_NEW_PATIENT_FACT' in reasons


def shared_direct_fixture(second_fact=False):
    old, cur, prev, e, review, _ = fixture()
    review['evidence_comparison']['current_information_quotes'] = [
        dict(source_id='C', quote='当前复述病灶增大。')]
    report = report_source('R', '复查提示病灶增大。', when='2022-01-01 08:00', title='复查', kind='imaging')
    report['clocks']['report_time'] = dict(value='2022-01-01 10:00:00', precision='minute', raw='2022-01-01 10:00')
    report['text'] = '虚构报告所见：病灶增大。'
    report['source_sha256'] = __import__('hashlib').sha256(report['text'].encode()).hexdigest()
    cur['text'] = '当前复述病灶增大。' + cur['text']
    cur['source_sha256'] = __import__('hashlib').sha256(cur['text'].encode()).hexdigest()
    e = event('E1', cur, action='决定补充检查。', phase='术前',
        time_basis='explicit_documented_choice', choice_time='2022-01-02 09:00:00',
        prechoice_citations=quotes(cur, '当前复述病灶增大。'),
        choice_boundary_citations=engine.resolve_model_quotes([
            dict(source_id='C', quote='决定补充检查。')], [cur], []))
    sources = [old, cur, report]
    ctx = engine.build_evidence_context(e, sources, [prev, e], preserve_current_context=True)
    block = next(block for block in ctx['blocks'] if block['source_id'] == 'R')
    relation = proposal('paraphrased', dict(source_id='C', quote='当前复述病灶增大。'))['evidence_relations'][0]
    relation.update(event_id=e['event_id'], evidence_object_ids=[block['evidence_object_id']],
        direct_quote=dict(source_id='MODEL-CANNOT-REASSIGN-EO', quote='虚构报告所见：病灶增大。'),
        current_action_quote=dict(source_id='C', quote='决定补充检查。'),
        connection_quote=dict(source_id='C', quote='当前复述病灶增大。复查提示病灶增大。因病灶增大，决定补充检查。'),
        temporal_channel_status='temporally_possibly_visible',
        proof_channels=['report_time', 'semantic_prechoice'])
    review['evidence_relations'] = [relation]
    review['observation_availability'] = []
    if second_fact:
        review['evidence_comparison']['current_information_quotes'].append(
            dict(source_id='C', quote='因病灶增大，'))
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    return old, cur, prev, e, review, ctx, sources


def test_TIME008_direct_evidence_object_covers_current_fact_through_shared_compiler():
    old, cur, prev, e, review, ctx, sources = shared_direct_fixture()
    comparison, availability, reasons, ledger = engine.compile_semantic_proof(
        e, review, quotes(cur, '当前复述病灶增大。'), quotes(cur, '复查提示病灶增大。因病灶增大，决定补充检查。'),
        ctx, sources, [prev, e], [])
    assert comparison['known_current_information_supported'] is True
    coverage = comparison['current_fact_coverage']['per_current_fact'][0]
    assert coverage['observation_channel_supported'] is False
    assert coverage['evidence_object_relation_ids']
    assert coverage['prechoice_supported'] is True
    assert ledger['relations'][0]['prechoice_proof_supported'] is True
    assert e['semantic_proof_validation']['field_errors'] == []
    assert e['semantic_proof_validation']['complete_source_retrieval_unaffected_by_key_predecessors'] is True


def test_TIME008_semantic_support_cannot_hide_missing_connection_until_composition():
    old, cur, prev, e, review, ctx = fixture()
    review['observation_availability'][0]['connection_quote'] = None
    comparison, availability, reasons = engine.validate_semantic_contract(
        e, review, ctx, [old, cur], [prev, e], [])
    proof = availability[0]['compiled_proof_channels']
    assert proof['supported_channels'] == []
    assert availability[0]['eligible_as_known_prechoice'] is False
    assert comparison['known_current_information_supported'] is False
    assert 'SEMANTIC_PROOF_CONNECTION_NOT_UNIQUELY_VERIFIED' in reasons
    assert 'NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED' in reasons


def test_TIME008_one_relation_cannot_certify_an_uncovered_second_current_fact():
    old, cur, prev, e, review, ctx, sources = shared_direct_fixture(second_fact=True)
    comparison, _, reasons, _ = engine.compile_semantic_proof(
        e, review, quotes(cur, '当前复述病灶增大。'), quotes(cur, '复查提示病灶增大。因病灶增大，决定补充检查。'),
        ctx, sources, [prev, e], [])
    rows = comparison['current_fact_coverage']['per_current_fact']
    assert [row['prechoice_supported'] for row in rows] == [True, False]
    assert comparison['known_current_information_supported'] is False
    assert 'SEMANTIC_PROOF_CURRENT_FACT_NOT_COVERED' in reasons
    assert 'NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED' in reasons
    assert any(item['field_path'] == 'evidence_comparison.current_information_quotes'
               for item in e['semantic_proof_validation']['field_errors'])
