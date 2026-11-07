"""Adversarial source-clock and EvidenceObject counterexamples; synthetic only.

Function-name markers are navigation labels. Stable issue IDs live in the
external remediation regression_issue_map.json; duplicate labels are not issue IDs.
"""
from copy import deepcopy
from unittest.mock import patch

from scripts.cohort_construction.p2_auto_checkpoint_v04 import engine
from scripts.cohort_construction.p2_auto_checkpoint import fhir_io
from scripts.cohort_construction.p2_auto_checkpoint_v04.context import context_for_discovery
from scripts.cohort_construction.p2_auto_checkpoint_v04.evidence import report_objects, lineage_metrics
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_engine import (
    FakeClient, event, source, integration_fixture, review, select, empty_quotes,
)
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_evidence_chain_remediation import (
    projected, relation_fixture, proposal,
)
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_v05_semantic_contract import fixture
from scripts.cohort_construction.p2_auto_checkpoint_v05_refine import refine
from scripts.cohort_construction.p2_pathway_replay.core import (
    clock, project_sources, resource_selection, validate_assessment_round, exact_duplicate_groups,
)


def test_CEREM_ADV001_offset_clock_is_normalized_before_temporal_gating():
    current, _ = projected(exam_datetime='2022-01-01T01:00Z', report_time='2022-01-01T02:30Z')
    assert current['clocks']['report_time']['value'] == '2022-01-01 10:30:00'
    assert resource_selection(current, '2022-01-01 10:00:00', '2021-12-01')['disposition'] == 'EXCLUDE'
    earlier, _ = projected(exam_datetime='2022-01-01T01:00Z', report_time='2022-01-01T01:30Z')
    assert earlier['clocks']['report_time']['value'] == '2022-01-01 09:30:00'
    assert resource_selection(earlier, '2022-01-01 10:00:00', '2021-12-01')['disposition'] == 'INCLUDE'
    e, _, ctx = relation_fixture()
    e['choice_time'] = '2022-01-01 10:00:00'
    e['time_basis'] = 'explicit_documented_choice'
    e['source_document_clock'] = clock('2022-01-01 10:00')
    ctx = engine.build_evidence_context(e, [current, next(s for s in relation_fixture()[1] if s['source_id'] == 'C')], [e])
    assert not any(b['source_id'] == current['source_id'] for b in ctx['blocks'])


def test_CEREM_ADV002_fractional_seconds_are_not_truncated_to_same_time():
    first, second = clock('2022-01-01 10:00:00.100'), clock('2022-01-01 10:00:00.900')
    assert first['precision'] == second['precision'] == 'fractional_second'
    assert engine.time_order(first, second) == 'BEFORE'
    assert engine.time_order(second, first) == 'AFTER'


def test_CEREM_ADV002_excel_serial_subseconds_survive_native_fhir_and_time_gates():
    serial = 44805.500005787037
    current = clock(serial)
    assert current['value'] == '2022-09-01 12:00:00.499999824'
    assert current['precision'] == 'fractional_second'
    assert current['raw'] == serial
    assert engine.time_order(current, '2022-09-01 12:00:00.25') == 'AFTER'
    assert clock(44832.5)['value'] == '2022-09-28 12:00:00'
    assert clock(44832.5)['precision'] == 'second'
    assert clock(44832) == dict(value='2022-09-28', precision='date', raw=44832)
    source, _ = projected(exam_datetime='2022-09-01 08:00', report_time=serial)
    assert source['clocks']['report_time'] == current
    restored, _ = fhir_io.import_sources(fhir_io.build([source], 'SYNTHETIC', [], []))
    assert restored[0]['clocks']['report_time'] == current
    assert resource_selection(source, '2022-09-01 12:00:00.25', '2022-01-01')['disposition'] == 'EXCLUDE'
    assert resource_selection(source, '2022-09-01 12:00:00.75', '2022-01-01')['disposition'] == 'INCLUDE'
    quote = dict(source_id=source['source_id'], source_sha256=source['source_sha256'],
                 quote=source['text'], start=0, end=len(source['text']))
    assert not validate_assessment_round('old', 'new', 'choice', [quote], [source],
        '2022-09-01 12:00:00.25')['eligible']
    assert validate_assessment_round('old', 'new', 'choice', [quote], [source],
        '2022-09-01 12:00:00.75')['eligible']


def test_CEREM_ADV030_zero_fractional_release_survives_shared_clock_and_fhir():
    from scripts.cohort_construction.p2_pathway_replay.core import _structured_clock, _compare_clocks
    for suffix in ('0', '000', '000000', '000000000'):
        for zone in ('', 'Z', '+08:00'):
            raw = '2022-01-01T10:00:00.' + suffix + zone
            parsed = clock(raw)
            local = '2022-01-01 18:00:00' if zone == 'Z' else '2022-01-01 10:00:00'
            before = '2022-01-01 17:59:59' if zone == 'Z' else '2022-01-01 09:59:59'
            after = '2022-01-01 18:00:01' if zone == 'Z' else '2022-01-01 10:00:01'
            assert parsed['precision'] == 'fractional_second'
            assert _structured_clock(parsed) == parsed
            assert _compare_clocks(parsed, local) == 'SAME_TIME_UNORDERED'
            assert _compare_clocks(parsed, before) == 'AFTER'
            assert _compare_clocks(parsed, after) == 'BEFORE'
    current, _ = projected(report_time='2022-01-01T10:00:00.000+08:00')
    _, evidence = report_objects(current)
    assert evidence['publication_time'] == current['clocks']['report_time']
    assert resource_selection(current, '2022-01-01 10:00:01', '2022-01-01')['disposition'] == 'INCLUDE'
    restored, _ = fhir_io.import_sources(fhir_io.build([current], 'SYNTHETIC', [], []))
    assert restored[0]['clocks'] == current['clocks']
    assert restored[0]['clock'] == current['clock']


def test_CEREM_ADV002_excel_serial_cannot_silently_round_or_upgrade_precision():
    assert clock(1.9999999999)['value'] == '1899-12-31 23:59:59.99999136'
    assert clock(1.00000000000001)['precision'] == 'invalid'
    assert clock(1.00000000001)['value'] == '1899-12-31 00:00:00.000000864'
    assert clock(float('nan'))['precision'] == 'invalid'


def test_CEREM_ADV003_invalid_alias_does_not_fall_back_to_exam_clock():
    current, _ = projected(report_time='not-a-clock', report_datetime='2022-01-02 10:00')
    assert current['clocks']['report_time']['value'] == '2022-01-02 10:00:00'
    assert current['time_provenance']['report_time']['invalid_source_fields'] == ['report_time']
    assert resource_selection(current, '2022-01-01 12:00:00', '2021-12-01')['disposition'] == 'EXCLUDE'


def test_CEREM_ADV004_conflicting_release_aliases_fail_closed_with_receipt():
    current, _ = projected(exam_datetime='2022-01-01 08:00',
        report_time='2022-01-02 10:00', report_datetime='2022-01-03 10:00')
    assert current['clocks']['report_time']['value'] is None
    assert current['clocks']['exam_time']['value'] == '2022-01-01 08:00:00'
    assert current['clock']['value'] is None
    provenance = current['time_provenance']['report_time']
    assert provenance['status'] == 'CONFLICTING_NAMED_EXPORT_FIELDS_REVIEW_REQUIRED'
    assert len(provenance['conflict_values']) == 2
    selected = resource_selection(current, '2022-01-01 12:00:00', '2021-12-01')
    assert selected['disposition'] == 'REVIEW'
    assert selected['reason'] == 'RELEASE_TIME_UNRESOLVED_EXAM_PROXY_FORBIDDEN'
    # An actually absent release field can retain a weak exam-time proxy; a
    # populated conflicting field may not be silently converted into absence.
    exam_only, _ = projected(exam_datetime='2022-01-01 08:00')
    assert resource_selection(exam_only, '2022-01-01 12:00:00', '2021-12-01')['disposition'] == 'INCLUDE'
    invalid, _ = projected(exam_datetime='2022-01-01 08:00', report_time='not-a-clock')
    assert resource_selection(invalid, '2022-01-01 12:00:00', '2021-12-01')['disposition'] == 'REVIEW'
    from scripts.cohort_construction.p2_auto_checkpoint_v04.evidence import report_objects
    _, evidence = report_objects(current)
    assert evidence['publication_time']['value'] is None
    assert evidence['publication_status'] == 'PUBLICATION_TIME_UNRESOLVED_REVIEW_REQUIRED'
    assert evidence['original_text'] == current['text']


def test_CEREM_ADV004_cross_role_release_conflict_does_not_pick_a_preferred_clock():
    current, _ = projected(exam_datetime='2022-01-01 08:00',
        report_time='2022-01-02 10:00', issued='2022-01-03 10:00')
    assert current['clock']['value'] is None
    assert resource_selection(current, '2022-01-01 12:00:00', '2021-12-01')['disposition'] == 'REVIEW'
    current['time_provenance']['issued']['status'] = 'CONFLICTING_NAMED_EXPORT_FIELDS_REVIEW_REQUIRED'
    current['clocks']['issued'] = clock(None)
    selected = resource_selection(current, '2022-01-01 12:00:00', '2021-12-01')
    assert selected['disposition'] == 'REVIEW'
    assert selected['clock_value'] is None


def test_CEREM_ADV005_assessment_round_uses_same_release_clock_as_retrieval():
    current, _ = projected(exam_datetime='2022-01-01 08:00', report_time='2022-01-02 10:00')
    quote = dict(source_id=current['source_id'], source_sha256=current['source_sha256'],
                 quote=current['text'], start=0, end=len(current['text']))
    assert resource_selection(current, '2022-01-01 09:00:00', '2021-12-01')['disposition'] == 'EXCLUDE'
    assert validate_assessment_round('old', 'new', 'choice', [quote], [current], '2022-01-01 09:00:00') == {
        'eligible': False, 'reason': 'NEW_EVIDENCE_NOT_ORDERED_BEFORE_CURRENT_PHASE'}


def test_CEREM_CLOCK010_minute_precision_survives_shared_resolver_and_legacy_source_clock():
    current, _ = projected(exam_datetime='2022-01-01 08:00', report_time='2022-01-02 10:00')
    assert current['clocks']['report_time']['precision'] == 'minute'
    assert current['clock'] == current['clocks']['report_time']
    assert resource_selection(current, '2022-01-02 10:00:30', '2021-12-01')['disposition'] == 'REVIEW'
    current['clocks']['report_time']['precision'] = 'second'  # inconsistent legacy metadata: fail closed
    assert resource_selection(current, '2022-01-02 10:00:30', '2021-12-01')['disposition'] == 'REVIEW'
    legacy = dict(source_id='OLD-CT', kind='imaging', clocks=dict(
        report_time={'value': '2022-01-02 10:00:00', 'raw': '2022-01-02 10:00'}),
        clock=dict(value='2022-01-02 10:00:00'), clock_basis='report_time')
    assert resource_selection(legacy, '2022-01-02 10:00:30', '2021-12-01')['disposition'] == 'REVIEW'
    legacy['clocks']['report_time']['precision'] = 'minute'
    assert resource_selection(legacy, '2022-01-02 10:00:30', '2021-12-01')['disposition'] == 'REVIEW'
    legacy['clocks']['report_time'] = clock('2022-01-02 10:00:00')
    assert resource_selection(legacy, '2022-01-02 10:00:30', '2021-12-01')['disposition'] == 'INCLUDE'
    exact_second = deepcopy(current)
    exact_second['clocks']['report_time'] = clock('2022-01-02 10:00:00')
    assert resource_selection(exact_second, '2022-01-02 10:00:30', '2021-12-01')['disposition'] == 'INCLUDE'
    quote = dict(source_id=current['source_id'], source_sha256=current['source_sha256'],
                 quote=current['text'], start=0, end=len(current['text']))
    assert not validate_assessment_round('old', 'new', 'choice', [quote], [current],
        '2022-01-02 10:00:30')['eligible']
    legacy = dict(source_id='OLD-LAB', kind='lab', text='legacy report',
        clocks={}, clock=clock('2022-01-01 09:00'), clock_basis='report_time_proxy')
    assert resource_selection(legacy, '2022-01-02 10:00:00', '2021-12-01')['disposition'] == 'INCLUDE'
    # A projected source with explicit execution/sample fields but no release
    # cannot claim that its legacy display clock is a publication timestamp.
    projected_lab = dict(legacy, clocks={'sample_time': clock('2022-01-01 08:00')})
    assert resource_selection(projected_lab, '2022-01-02 10:00', '2021-12-01')['reason'] == 'UNKNOWN_TIME'


def test_CEREM_ADV011_native_report_date_conflicts_are_not_dropped():
    current, _ = projected(exam_datetime='2022-01-01 08:00',
        report_date='2022-01-02', issued='2022-01-03 10:00')
    assert current['clocks']['report_date']['value'] == '2022-01-02'
    assert current['clock']['value'] is None
    selected = resource_selection(current, '2022-01-02 12:00', '2021-12-01')
    assert selected['disposition'] == 'REVIEW'
    assert any(reason.startswith('CONFLICTING_RELEASE_ROLES')
               for reason in selected['release_time_provenance_errors'])
    _, evidence = report_objects(current)
    assert evidence['publication_status'] == 'PUBLICATION_TIME_UNRESOLVED_REVIEW_REQUIRED'
    assert evidence['original_text'] == current['text']
    bundle = fhir_io.build([current], 'SYNTHETIC', [], [])
    restored, _ = fhir_io.import_sources(bundle)
    assert restored[0]['clocks']['report_date'] == current['clocks']['report_date']
    assert restored[0]['clock']['value'] is None
    different = deepcopy(current)
    different['clocks']['issued'] = clock(None)
    different['time_provenance']['issued'] = dict(status='NOT_MAPPED_NO_NAMED_FIELD_IN_PROVIDED_ROW')
    assert exact_duplicate_groups([current, different]) == []


def test_CEREM_ADV012_generic_fhir_document_retains_narrow_signature_day_adapter():
    text = '申请目的：超声内镜\n医师甲\n2022年9月3日\n'
    import base64
    resource = dict(resourceType='DocumentReference', id='SYNTHETIC-REQUEST', status='current',
        subject={'reference': 'Patient/SYNTHETIC'}, type={'text': '消化内镜检查申请单'},
        content=[{'attachment': {'contentType': 'text/plain',
            'data': base64.b64encode(text.encode()).decode()}}])
    bundle = dict(resourceType='Bundle', type='collection', entry=[
        dict(resource=dict(resourceType='Patient', id='SYNTHETIC')), dict(resource=resource)])
    restored, _ = fhir_io.import_sources(bundle)
    assert restored[0]['clock']['value'] == '2022-09-03'
    assert restored[0]['clock']['precision'] == 'date'
    assert restored[0]['clock_basis'] == 'request_physician_signature_record_day_proxy'
    # No reading or exact choice-time certification follows from the signature.
    assert restored[0]['clock']['value'] != '2022-09-03 00:00:00'


def test_CEREM_ADV013_release_versions_are_not_exact_duplicates():
    first, _ = projected(report_time='2022-01-01 10:00')
    second, _ = projected(index_report_uid='SYNTHETIC-REPORT-2', report_time='2022-01-03 10:00')
    assert exact_duplicate_groups([first, second]) == []
    same, _ = projected(index_report_uid='SYNTHETIC-REPORT-3', report_time='2022-01-01 10:00')
    assert exact_duplicate_groups([first, same]) == [[first['source_id'], same['source_id']]]


def test_CEREM_ADV017_narrative_must_not_predate_the_report_it_paraphrases():
    e, sources, ctx = relation_fixture()
    actual = next(b for b in ctx['blocks'] if b['source_id'] == 'CT')
    proposed = proposal('paraphrased', dict(source_id='C', quote='CT提示静脉受侵。'))
    proposed['evidence_relations'][0]['evidence_object_ids'] = [actual['evidence_object_id']]
    proposed['evidence_relations'][0]['publication_before_choice'] = 'unknown'
    proposed['evidence_relations'][0]['temporal_channel_status'] = 'unresolved'
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    for value, expected in [
        ('2022-08-01 09:00', 'NARRATIVE_RECORD_TIME_BEFORE_EVIDENCE_PUBLICATION'),
        ('2022-09-01 09:00', 'NARRATIVE_RECORD_TIME_BEFORE_EVIDENCE_PUBLICATION'),
        (None, 'NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED'),
        ('invalid-clock', 'NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED'),
    ]:
        current = next(source for source in sources if source['source_id'] == 'C')
        current['clock'] = clock(value)
        e['source_document_clock'] = current['clock']
        for block in ctx['blocks']:
            if block['source_id'] == 'C':
                block['clock'] = deepcopy(current['clock'])
        e['actual_model_evidence'] = deepcopy(ctx['blocks'])
        rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
        ledger = engine.compile_evidence_relations(e, rows, sources, context=ctx)
        row = ledger['relations'][0]
        # V04-TIME-008: only a real narrative-before-publication conflict makes
        # the relation unresolved. Unknown clocks retain temporal-only uncertainty
        # and cannot veto an independently source-bound semantic proposal.
        assert row['relation_status'] == (
            'UNRESOLVED' if expected == 'NARRATIVE_RECORD_TIME_BEFORE_EVIDENCE_PUBLICATION'
            else 'SOURCE_BOUND_PROPOSAL')
        assert expected in row['validation_reasons']
        assert row['narrative_record_time_order_not_reading_proof'] is True
        if expected == 'NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED':
            assert row['supported_proof_channels'] == ['semantic_prechoice']
            assert row['prechoice_proof_supported'] is True
            assert row['publication_before_choice_established'] is False
        else:
            assert row['prechoice_proof_supported'] is False
        assert row['clinician_reading_established'] is False
        assert row['clinical_causality_certified'] is False


def test_CEREM_ADV017_same_minute_and_later_narrative_remain_only_source_bound():
    e, sources, ctx = relation_fixture()
    actual = next(b for b in ctx['blocks'] if b['source_id'] == 'CT')
    proposed = proposal('paraphrased', dict(source_id='C', quote='CT提示静脉受侵。'))
    proposed['evidence_relations'][0]['evidence_object_ids'] = [actual['evidence_object_id']]
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    for value in ('2022-09-01 10:00', '2022-09-01 10:30', '2022-09-02 09:00'):
        current = next(source for source in sources if source['source_id'] == 'C')
        current['clock'] = clock(value)
        e['source_document_clock'] = current['clock']
        for block in ctx['blocks']:
            if block['source_id'] == 'C':
                block['clock'] = deepcopy(current['clock'])
        e['actual_model_evidence'] = deepcopy(ctx['blocks'])
        rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
        ledger = engine.compile_evidence_relations(e, rows, sources, context=ctx)
        row = ledger['relations'][0]
        assert row['relation_status'] == 'SOURCE_BOUND_PROPOSAL'
        assert row['narrative_publication_time_compatible'] is True
        assert row['reading_before_choice'] == 'unknown'
        assert not row['reading_before_choice_established']


def test_CEREM_ADV017_unresolved_publication_cannot_certify_narrative_time_order():
    e, sources, ctx = relation_fixture()
    actual = next(b for b in ctx['blocks'] if b['source_id'] == 'CT')
    proposed = proposal('paraphrased', dict(source_id='C', quote='CT提示静脉受侵。'))
    proposed['evidence_relations'][0]['evidence_object_ids'] = [actual['evidence_object_id']]
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    block = next(block for block in ctx['blocks'] if block['source_id'] == 'CT')
    block['publication_time'] = clock(None)
    proposed['evidence_relations'][0]['publication_before_choice'] = 'unknown'
    proposed['evidence_relations'][0]['temporal_channel_status'] = 'unresolved'
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    ledger = engine.compile_evidence_relations(e, rows, sources, context=ctx)
    assert 'NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED' in ledger['relations'][0]['validation_reasons']


def rebind_changed_relation_source(e, sources, proposed):
    """Recompute immutable source-bound discovery/proof quotes after text edits."""
    current = next(s for s in sources if s['source_id'] == 'C')
    current['source_sha256'] = __import__('hashlib').sha256(current['text'].encode()).hexdigest()
    action = e['action']
    narrative = 'CT提示静脉受侵。'
    e['action_citations'] = engine.resolve_model_quotes(
        [dict(source_id='C', quote=action)], [current], [])
    e['choice_boundary_citations'] = deepcopy(e['action_citations'])
    e['prechoice_citations'] = engine.resolve_model_quotes(
        [dict(source_id='C', quote=narrative)], [current], [])
    ctx = engine.build_evidence_context(e, sources, [e], preserve_current_context=True)
    relation = proposed['evidence_relations'][0]
    relation['current_action_quote'] = dict(source_id='C', quote=action)
    relation['connection_quote'] = dict(source_id='C', quote=narrative + action)
    return ctx


def test_CEREM_ADV031_paraphrase_clock_binds_only_the_cited_ranges():
    e, sources, ctx = relation_fixture()
    current = next(s for s in sources if s['source_id'] == 'C')
    current['text'] = '既往稳定。CT提示静脉受侵。决定补充病理取材。'
    proposed = proposal('paraphrased', dict(source_id='C', quote='CT提示静脉受侵。'))
    ctx = rebind_changed_relation_source(e, sources, proposed)
    imaging_block = next(b for b in ctx['blocks'] if b['source_id'] == 'CT')
    narrative_block = dict(source_id='C', start=0, end=len(current['text']),
        quote=current['text'], text=current['text'], clock=current['clock'])
    ctx['blocks'] = [
        dict(narrative_block, start=0, end=5, quote='既往稳定。',
             clock=clock('2022-08-01 09:00')),
        dict(narrative_block, start=5, end=14, quote='CT提示静脉受侵。',
             clock=current['clock']),
        imaging_block,
    ]
    ctx['allowed_spans']['C'] = [dict(start=0, end=14)]
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    proposed['evidence_relations'][0]['evidence_object_ids'] = [imaging_block['evidence_object_id']]
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    ledger = engine.compile_evidence_relations(e, rows, sources, context=ctx)
    row = ledger['relations'][0]
    assert row['relation_status'] == 'SOURCE_BOUND_PROPOSAL'
    assert row['narrative_record_to_publication_order'] == 'AFTER'
    assert row['narrative_record_clock'] == current['clock']
    assert row['reading_before_choice'] == 'unknown'


def test_CEREM_ADV033_narrative_substring_binds_containing_document_not_sibling_clock():
    e, sources, ctx = relation_fixture()
    current = next(s for s in sources if s['source_id'] == 'C')
    current['text'] = '既往稳定。CT提示静脉受侵。决定补充病理取材。'
    proposed = proposal('paraphrased', dict(source_id='C', quote='CT提示静脉受侵。'))
    ctx = rebind_changed_relation_source(e, sources, proposed)
    imaging = next(b for b in ctx['blocks'] if b['source_id'] == 'CT')
    whole = dict(source_id='C', start=0, end=len(current['text']),
        quote=current['text'], text=current['text'], clock=current['clock'])
    ctx['blocks'] = [whole, imaging]
    ctx['allowed_spans']['C'] = [dict(start=0, end=len(current['text']))]
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    proposed['evidence_relations'][0]['evidence_object_ids'] = [imaging['evidence_object_id']]
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    assert rows[0]['validated_object_citations'][0]['narrative_quote_validation']['valid']
    row = engine.compile_evidence_relations(e, rows, sources, context=ctx)['relations'][0]
    assert row['relation_status'] == 'SOURCE_BOUND_PROPOSAL'
    assert row['narrative_record_clock'] == current['clock']
    assert row['narrative_clock_binding']['status'] == 'EXACT_OR_CONTAINED_SUPPLIED_BLOCK'
    # The same source text in a second supplied block makes the narrative block
    # unresolved; the validator may not select a convenient same-source clock.
    ctx['blocks'].insert(0, dict(whole, start=100, end=100+len(current['text']),
        clock=clock('2022-08-01 09:00')))
    # Both ranges are supplied but the second remains outside the model's
    # citation allowlist; only the submitted evidence pool, not arbitrary source
    # text, can create a same-source ambiguity.
    ctx['allowed_spans']['C'] = [dict(start=0, end=len(current['text']))]
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    row = engine.compile_evidence_relations(e, rows, sources, context=ctx)['relations'][0]
    # Exact source/object support and the semantic prechoice channel survive an
    # ambiguous narrative clock; only that clock remains unresolved.
    assert row['relation_status'] == 'SOURCE_BOUND_PROPOSAL'
    assert row['source_binding_status'] == 'SOURCE_BOUND'
    assert row['supported_proof_channels'] == ['semantic_prechoice']
    assert 'NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED' in row['validation_reasons']
    assert row['temporal_only_reasons'] == ['NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED']
    assert row['narrative_record_clock']['value'] is None


def test_CEREM_ADV033_two_evidence_rows_do_not_share_or_pollute_narrative_clock():
    from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_context import source as report_source
    e, sources, ctx = relation_fixture()
    first = next(b for b in ctx['blocks'] if b['source_id'] == 'CT')
    # Use the same finding so this regression isolates per-EO narrative clocks,
    # rather than intentionally failing CT2's independent direct-quote proof.
    second_source = report_source('CT2', '静脉受侵。', when='2022-08-02 08:00', title='CT', kind='imaging')
    second_source['clocks']['report_time'] = dict(value='2022-08-02 10:00:00', precision='minute', raw='2022-08-02 10:00')
    sources.append(second_source)
    second_context = engine.build_evidence_context(e, sources, [e], preserve_current_context=True)
    second = next(b for b in second_context['blocks'] if b['source_id'] == 'CT2')
    ctx = second_context
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    proposed = proposal('paraphrased', dict(source_id='C', quote='CT提示静脉受侵。'))
    proposed['evidence_relations'][0]['evidence_object_ids'] = [first['evidence_object_id'], second['evidence_object_id']]
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    ledger = engine.compile_evidence_relations(e, rows, sources, context=ctx)
    assert all(row['relation_status'] == 'SOURCE_BOUND_PROPOSAL' for row in ledger['relations'])
    assert all(row['narrative_record_clock'] == e['source_document_clock'] for row in ledger['relations'])
    assert all(row['narrative_clock_binding']['status'] == 'EXACT_OR_CONTAINED_SUPPLIED_BLOCK'
               for row in ledger['relations'])
    # A later publication invalidates only that EO row, not a resolved sibling.
    for block in ctx['blocks']:
        if block['source_id'] == 'CT2':
            block['publication_time'] = clock('2022-09-03 10:00')
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    ledger = engine.compile_evidence_relations(e, rows, sources, context=ctx)
    statuses = {row['source_id']: row['relation_status'] for row in ledger['relations']}
    assert statuses == {'CT': 'SOURCE_BOUND_PROPOSAL', 'CT2': 'UNRESOLVED'}
    assert len([row for row in ledger['relations']
                if 'NARRATIVE_RECORD_TIME_BEFORE_EVIDENCE_PUBLICATION' in row['validation_reasons']]) == 1


def test_CEREM_ADV031_legacy_single_narrative_pool_keeps_event_clock_without_context():
    e, sources, ctx = relation_fixture()
    actual = next(b for b in ctx['blocks'] if b['source_id'] == 'CT')
    proposed = proposal('paraphrased', dict(source_id='C', quote='CT提示静脉受侵。'))
    proposed['evidence_relations'][0]['evidence_object_ids'] = [actual['evidence_object_id']]
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    with_context = engine.compile_evidence_relations(e, rows, sources, context=ctx)
    without_context = engine.compile_evidence_relations(e, rows, sources)
    assert with_context['relations'][0]['relation_status'] == 'SOURCE_BOUND_PROPOSAL'
    assert without_context['relations'][0]['relation_status'] == 'SOURCE_BOUND_PROPOSAL'
    assert without_context['relations'][0]['narrative_record_clock'] == e['source_document_clock']
    # A valid event document clock is not transferred to an unrelated narrative source.
    citation = rows[0]['validated_object_citations'][0]
    citation['narrative_quote_validation']['valid'][0]['source_id'] = 'OTHER'
    ledger = engine.compile_evidence_relations(e, rows, sources)
    assert 'NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED' in ledger['validation_reasons']


def test_CEREM_ADV018_repair_rebinds_exact_evidence_object_metadata_not_only_coordinates():
    old, cur, prev, e, corrected, ctx = fixture()
    for proposal in (corrected,):
        proposal.update(checkpoint_kind='new_management_choice', previous_event_id=None,
            same_process_previous_id=None, process_link='uncertain', evidence_connection='uncertain',
            evidence_evolution='none', observation_quotes=[], connection_quotes=[], observation_availability=[])
        proposal['evidence_comparison'] = dict(previous_known_quotes=[], current_information_quotes=[],
            change_kind='none', reinterpretation_quotes=[], explanation='Synthetic management repair.')
    e.update(evidence_context=ctx, actual_model_evidence=deepcopy(ctx['blocks']),
        review_request_sha256='original-hash', semantic_review=deepcopy(corrected))
    # Bind a real report EO before emulating the saved request. The legacy
    # document-only fixture cannot exercise EO metadata by adding forged fields.
    report_e, _, report_ctx = relation_fixture()
    ctx = report_ctx
    e['source_id'] = report_e['source_id']
    e['evidence_context'] = ctx
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    payload = dict(instruction='Synthetic frozen review.',
        events=[dict(event=dict(event_id=e['event_id']), evidence_context=dict(
            block_ids=[f"{block['source_id']}:{block['start']}:{block['end']}" for block in ctx['blocks']]))],
        evidence_source_pool=[dict(block, block_id=f"{block['source_id']}:{block['start']}:{block['end']}",
            text=block.get('quote', block.get('text'))) for block in ctx['blocks']], earlier_event_ledger=[])
    for field in ('evidence_object_id', 'publication_time', 'execution_time'):
        block = next(block for block in payload['evidence_source_pool']
                     if block.get('evidence_object_id'))
        original_block = deepcopy(block)
        block[field] = 'EO-TAMPERED' if field == 'evidence_object_id' else clock('2021-01-01 00:00')
        try:
            refine.prepare_payload(payload, [e], [])
        except ValueError as error:
            assert str(error) == 'ORIGINAL_EVIDENCE_OBJECT_BINDING_MISMATCH'
        else:
            raise AssertionError('Tampered repair metadata reached preparation: ' + field)
        assert block[field] != original_block[field]
        block[field] = original_block[field]
    prepared = refine.prepare_payload(payload, [e], [])
    assert prepared['repair_contract']['original_evidence_pool_sha256'] == refine.digest(
        payload['evidence_source_pool'])


def test_CEREM_ADV018_evidence_relation_gate_depends_on_checkpoint_proof_kind():
    from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_engine import empty_quotes, select
    e = event('SYNTHETIC-RELATION', source('C', '决定补充检查。', '2022-09-02 10:00'),
        action='决定补充检查。', phase='术前', time_basis='record_time_proxy')
    e['evidence_relation_ledger'] = dict(validation_reasons=['UNKNOWN_OR_UNPROVIDED_EVIDENCE_OBJECT'],
        relations=[dict(evidence_object_id='EO-UNKNOWN', relation_status='UNRESOLVED',
            source_blocking_reasons=['UNKNOWN_OR_UNPROVIDED_EVIDENCE_OBJECT'])])
    for kind, expected, required in [
        ('new_management_choice', 'CANDIDATE_NEW_MANAGEMENT_CHOICE', False),
        ('evidence_informed_choice', 'UNCERTAIN_CONTRACT_OR_SOURCE', True),
    ]:
        r = review(e['event_id'], checkpoint_kind=kind, previous_event_id=None,
            same_process_previous_id=None, process_link='uncertain', evidence_connection='uncertain',
            evidence_evolution='none' if kind == 'new_management_choice' else 'new_relevant',
            evidence_relations=[dict(evidence_object_ids=['EO-UNKNOWN'], evidence_use='direct')])
        status, reasons = select(e=e, r=r, observations=empty_quotes(), connections=empty_quotes(), earlier_ids=set())
        assert status == expected
        assert ('EVIDENCE_RELATION_UNRESOLVED' in reasons) is required


def test_CEREM_ADV032_noncandidate_proof_kind_and_unresolved_reasons_are_preserved():
    from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_engine import empty_quotes, select
    e = event('SYNTHETIC-KIND', source('C', '决定补充检查。', '2022-09-02 10:00'),
        action='决定补充检查。', phase='术前', time_basis='record_time_proxy')
    e['evidence_relation_ledger'] = dict(validation_reasons=[],
        relations=[dict(evidence_object_id='EO-RESOLVED', relation_status='SOURCE_BOUND_PROPOSAL')])
    for checkpoint, expected in [
        ('first_anchor', 'CANDIDATE_FIRST_ANCHOR'),
        ('repeat', 'REPEATED_OR_EXECUTED_MENTION'),
        ('not_related', 'NOT_RELATED'),
        ('uncertain', 'UNCERTAIN_SEMANTIC_JUDGMENT'),
    ]:
        r = review(e['event_id'], checkpoint=checkpoint, checkpoint_kind='not_applicable',
            previous_event_id=None, same_process_previous_id=None,
            record_function='repeat' if checkpoint == 'repeat' else 'new_decision')
        status, reasons = select(e=e, r=r, observations=empty_quotes(),
            connections=empty_quotes(), earlier_ids=set())
        assert status == expected
        assert 'CHECKPOINT_PROOF_KIND_NOT_APPLICABLE_REQUIRED' not in reasons
        assert 'EVIDENCE_RELATION_UNRESOLVED' not in reasons
        r['checkpoint_kind'] = 'new_management_choice'
        status, reasons = select(e=e, r=r, observations=empty_quotes(),
            connections=empty_quotes(), earlier_ids=set())
        assert status == expected
        assert 'CHECKPOINT_PROOF_KIND_NOT_APPLICABLE_REQUIRED' in reasons
    e['evidence_relation_ledger']['relations'] = [dict(evidence_object_id='EO-UNKNOWN',
        relation_status='UNRESOLVED', source_blocking_reasons=['UNKNOWN_OR_UNPROVIDED_EVIDENCE_OBJECT'])]
    e['evidence_relation_ledger']['validation_reasons'] = ['UNKNOWN_OR_UNPROVIDED_EVIDENCE_OBJECT']
    r = review(e['event_id'], checkpoint='repeat', checkpoint_kind='not_applicable',
        previous_event_id=None, same_process_previous_id=None, record_function='repeat')
    status, reasons = select(e=e, r=r, observations=empty_quotes(),
        connections=empty_quotes(), earlier_ids=set())
    assert status == 'REPEATED_OR_EXECUTED_MENTION'
    assert 'EVIDENCE_RELATION_UNRESOLVED' in reasons
    e['kind'] = 'performed_test_report'
    r = review(e['event_id'], checkpoint='first_anchor', checkpoint_kind='not_applicable',
        previous_event_id=None, same_process_previous_id=None)
    status, reasons = select(e=e, r=r, observations=empty_quotes(),
        connections=empty_quotes(), earlier_ids=set())
    assert status == 'CANDIDATE_TEST_ANCHOR_TIME_UNCERTAIN'


def test_CEREM_ADV007_execution_anchor_does_not_substitute_publication_time():
    source = relation_fixture()[1][0]
    source.update(kind='lab', clocks=dict(sample_time=clock('2022-01-01 08:00'),
                                          report_time=clock('2022-01-02 10:00')))
    anchor, evidence = report_objects(source)
    assert anchor['execution_time']['value'] == '2022-01-01 08:00:00'
    assert evidence['publication_time']['value'] == '2022-01-02 10:00:00'
    del source['clocks']['sample_time']
    anchor, _ = report_objects(source)
    assert anchor['execution_time']['value'] is None
    assert anchor['execution_time_basis'] == 'UNKNOWN_NOT_PUBLICATION_TIME'


def test_CEREM_ADV008_wrong_same_source_object_or_range_is_not_supplied():
    e, sources, ctx = relation_fixture()
    actual = next(b for b in ctx['blocks'] if b['source_id'] == 'CT')
    oid = actual['evidence_object_id']
    proposed = proposal('direct', None)
    proposed['evidence_relations'][0]['evidence_object_ids'] = [oid]
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    for mutation in (dict(start=999, end=1000, evidence_object_id='EO-WRONG'),
                     dict(start=None, end=None, evidence_object_id='EO-WRONG')):
        wrong = deepcopy(ctx)
        block = next(b for b in wrong['blocks'] if b['source_id'] == 'CT')
        block.update(mutation)
        ledger = engine.compile_evidence_relations(e, rows, sources, context=wrong)
        assert ledger['relations'][0]['relation_status'] == 'UNRESOLVED'
        assert 'EVIDENCE_OBJECT_NOT_IN_SUBMITTED_CONTEXT' in ledger['validation_reasons']


def test_CEREM_ADV009_unresolved_or_none_cannot_be_source_bound_or_established():
    e, sources, ctx = relation_fixture()
    actual = next(b for b in ctx['blocks'] if b['source_id'] == 'CT')
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    for use in ('unresolved', 'none'):
        proposed = proposal(use, None)
        proposed['evidence_relations'][0]['evidence_object_ids'] = [actual['evidence_object_id']]
        proposed['evidence_relations'][0]['reading_before_choice'] = 'yes'
        rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
        ledger = engine.compile_evidence_relations(e, rows, sources, context=ctx)
        assert all(row['relation_status'] == 'UNRESOLVED' for row in ledger['relations'])
        metrics = lineage_metrics([dict(e, evidence_relation_ledger=ledger)])
        for field in ('paraphrase_linked', 'publication_before_choice_established',
                      'reading_before_choice_established', 'evidence_predecessor_selected',
                      'publication_before_choice_proposed', 'reading_before_choice_proposed',
                      'evidence_predecessor_proposed'):
            assert metrics[field] == 0


def test_CEREM_ADV010_bad_direct_quote_cannot_create_established_metrics():
    e, sources, ctx = relation_fixture()
    actual = next(b for b in ctx['blocks'] if b['source_id'] == 'CT')
    proposed = proposal('direct', None)
    proposed['evidence_relations'][0]['evidence_object_ids'] = [actual['evidence_object_id']]
    proposed['evidence_relations'][0]['direct_quote']['quote'] = '原文不存在的所见'
    proposed['evidence_relations'][0]['reading_before_choice'] = 'yes'
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    ledger = engine.compile_evidence_relations(e, rows, sources, context=ctx)
    assert ledger['relations'][0]['relation_status'] == 'UNRESOLVED'
    metrics = lineage_metrics([dict(e, evidence_relation_ledger=ledger)])
    assert metrics['publication_before_choice_established'] == 0
    assert metrics['reading_before_choice_established'] == 0
    assert metrics['evidence_predecessor_selected'] == 0


def test_CEREM_ADV011_relation_errors_are_local_and_unknown_object_is_not_erased():
    e, sources, ctx = relation_fixture()
    actual = next(b for b in ctx['blocks'] if b['source_id'] == 'CT')
    proposed = proposal('direct', None)
    proposed['evidence_relations'][0]['evidence_object_ids'] = ['EO-UNKNOWN', actual['evidence_object_id']]
    e['actual_model_evidence'] = deepcopy(ctx['blocks'])
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    ledger = engine.compile_evidence_relations(e, rows, sources, context=ctx)
    assert len(ledger['relations']) == 2
    assert all(row['relation_status'] == 'UNRESOLVED' for row in ledger['relations'])
    assert all('RELATION_CONTAINS_UNKNOWN_OR_UNPROVIDED_OBJECT' in row['validation_reasons']
               for row in ledger['relations'])
    rows[0]['validated_object_citations'].append(dict(rows[0]['validated_object_citations'][0],
        evidence_object_id='EO-UNKNOWN', direct_quote_valid=False))
    ledger = engine.compile_evidence_relations(e, rows, sources, context=ctx)
    valid_row = next(row for row in ledger['relations'] if row.get('source_id') == 'CT')
    assert 'DIRECT_EVIDENCE_USE_QUOTE_NOT_VERIFIED' not in valid_row['validation_reasons']
    proposed['evidence_relations'][0]['evidence_object_ids'] = [actual['evidence_object_id']] * 2
    rows = engine.validate_evidence_relations(e, proposed, ctx, sources, [])
    ledger = engine.compile_evidence_relations(e, rows, sources, context=ctx)
    assert 'DUPLICATE_EVIDENCE_OBJECT_ID_IN_RELATION' in ledger['validation_reasons']
    assert len({row['relation_id'] for row in ledger['relations']}) == len(ledger['relations'])


def test_CEREM_ADV012_current_unit_over_budget_is_never_complete_coverage():
    unit = dict(unit_id='U', source_ids=['D'], original_texts=[dict(source_id='D', text='1234')],
                document_profile='clinical_other', clock=clock('2022-01-01 10:00'))
    result = context_for_discovery(unit, [], budget=1)
    assert result['budget']['current_unit_over_budget'] is True
    assert result['budget']['current_unit_budget_waiver_required'] is True
    assert result['uncertainty']['context_coverage_complete'] is False
    assert any(row['disposition'] == 'CURRENT_UNIT_OVER_BUDGET_RETAINED_NOT_TRUNCATED'
               for row in result['source_ledger'])


def test_CEREM_ADV013_pool_exposes_execution_and_publication_roles_separately():
    e, sources, ctx = relation_fixture()
    block = next(b for b in ctx['blocks'] if b['source_id'] == 'CT')
    assert block['execution_time']['value'] == '2022-09-01 08:00:00'
    assert block['publication_time']['value'] == '2022-09-01 10:00:00'
    assert block['clock']['value'] == block['available_clock_proxy']['value'] == '2022-09-01 10:00:00'
    unit = dict(unit_id='U', source_ids=['C'], document_profile='preoperative_summary',
                type_prior='synthetic', original_texts=[dict(source_id='C', text=sources[1]['text'])])
    client = FakeClient({'items': []})
    with patch.object(engine, 'build_evidence_context', return_value=ctx):
        engine.review_unit(unit, [e], [e], sources, {'review_context_characters': 10000},
                           client, [])
    pool = next(b for b in client.requests[0]['payload']['evidence_source_pool'] if b['source_id'] == 'CT')
    assert pool['execution_time']['value'] == '2022-09-01 08:00:00'
    assert pool['publication_time']['value'] == '2022-09-01 10:00:00'


def test_CEREM_ADV014_ambiguous_action_occurrence_is_not_a_valid_prior_fact():
    old, cur, prev, e, corrected, ctx = management_repair_fixture()
    cur['text'] = '决定补充检查。决定补充检查。'
    cur['source_sha256'] = __import__('hashlib').sha256(cur['text'].encode()).hexdigest()
    e['choice_boundary_citations'] = engine.resolve_model_quotes(
        [dict(source_id='C', quote='决定补充检查。')], [cur], [])
    assert e['choice_boundary_citations']['ambiguous']
    corrected['observation_quotes'] = [dict(source_id='C', quote='决定补充检查。')]
    e['semantic_review'] = deepcopy(corrected)
    revised = refine.revalidate(e, corrected, [old, cur], [prev, e], [], {'request_sha256': 'repair-hash'})
    assert revised['checkpoint_status'] == 'UNCERTAIN_CONTRACT_OR_SOURCE'
    assert 'CURRENT_ACTION_USED_AS_NEW_PATIENT_FACT' in revised['validation_reasons']


def test_CEREM_ADV014_broad_action_quote_with_independent_prior_fact_is_not_field_error():
    old, cur, prev, e, corrected, ctx = fixture()
    # No exact choice-boundary citation: broad action support alone cannot prove
    # that an independent patient fact is the current choice.
    corrected['observation_quotes'] = [dict(source_id='C', quote='复查提示病灶增大。')]
    corrected['observation_availability'][0]['observation'] = deepcopy(corrected['observation_quotes'][0])
    corrected['observation_availability'][0]['availability'] = 'unresolved'
    corrected['observation_availability'][0]['proof_channels'] = []
    corrected['observation_availability'][0]['semantic_order'] = 'unknown'
    corrected['observation_availability'][0]['semantic_channel_status'] = 'unresolved'
    corrected['connection_quotes'] = [dict(source_id='C', quote=cur['text'])]
    e['choice_boundary_citations'] = {'valid': [], 'invalid': [], 'ambiguous': []}
    # Exact action support remains independently valid even though this
    # counterexample deliberately has no usable semantic choice-clause region.
    assert e['action_citations']['valid']
    e.update(evidence_context=ctx, actual_model_evidence=deepcopy(ctx['blocks']),
        provided_prior_event_ids=['E0'], provided_identity_event_ids=['E0', 'E1'],
        validated_connection_citations=engine.resolve_model_quotes(corrected['connection_quotes'], [old, cur], [],
            engine._allowed_connection_ranges(e, ctx, [old, cur])),
        review_request_sha256='original-hash', checkpoint_status='UNCERTAIN_CONTRACT_OR_SOURCE',
        validation_reasons=['CURRENT_ACTION_USED_AS_NEW_PATIENT_FACT'], semantic_review=deepcopy(corrected))
    revised = refine.revalidate(e, corrected, [old, cur], [prev, e], [], {'request_sha256': 'repair-hash'})
    assert revised['checkpoint_status'] == 'UNCERTAIN_CONTRACT_OR_SOURCE'
    assert 'NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED' in revised['validation_reasons']
    assert not any(error['code'] == 'CURRENT_ACTION_USED_AS_NEW_PATIENT_FACT'
                   for error in revised['field_validation_errors'])


def test_CEREM_ADV014_initial_pipeline_structurally_records_omitted_id():
    # An action-only current-information citation must also veto through the
    # live validator -> selector pipeline, without manually injecting a reason.
    r = review(observation_quotes=[], connection_quotes=[])
    _, e, sources, old, context, unit, _ = integration_fixture(r)
    with patch.object(engine, 'build_evidence_context', return_value=context):
        result = engine.review_unit(unit, [e], [old, e], sources,
                                    {'review_context_characters': 10000}, FakeClient({'items': []}), [])
    out = result['events'][0]
    assert out['initial_response_id_errors'] == ['MODEL_OMITTED_EVENT_ID']
    assert result['initial_review_id_audit']['missing_event_ids'] == [e['event_id']]


def test_CEREM_ADV014b_ambiguous_action_current_information_cannot_bypass_field_gate():
    from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_evidence_chain_remediation import choice_fixture
    e, r, sources, ctx = choice_fixture()
    sources[0]['text'] = '拟行超声内镜引导下细针穿刺。拟行超声内镜引导下细针穿刺。'
    sources[0]['source_sha256'] = __import__('hashlib').sha256(sources[0]['text'].encode()).hexdigest()
    action = '拟行超声内镜引导下细针穿刺。'
    e['choice_boundary_citations'] = engine.resolve_model_quotes(
        [dict(source_id='EUS', quote=action)], sources, [])
    r['evidence_comparison']['current_information_quotes'] = [dict(source_id='EUS', quote=action)]
    comparison, _, contract = engine.validate_semantic_contract(e, r, ctx, sources, [e], [])
    e['semantic_contract_validation'] = contract
    assert comparison['current_information']['invalid'][0]['occurrences']
    assert comparison['field_errors'][0]['field'] == 'evidence_comparison.current_information_quotes'
    status, reasons = select(e=e, r=r, observations=empty_quotes(), connections=empty_quotes(), earlier_ids=set())
    assert status == 'UNCERTAIN_CONTRACT_OR_SOURCE'
    assert 'CURRENT_ACTION_USED_AS_NEW_PATIENT_FACT' in reasons


def test_CEREM_ADV014c_performed_test_anchor_preserves_proof_reasons_without_new_gate():
    e = event('SYNTHETIC-TEST', source('C', 'CT检查已完成。', '2022-09-02 10:00'),
        kind='performed_test_report', record_function='execution', time_basis='historical_unknown')
    r = review(e['event_id'], checkpoint='first_anchor', record_function='execution',
        previous_event_id=None, same_process_previous_id=None, evidence_evolution='uncertain',
        process_link='uncertain')
    status, reasons = select(e=e, r=r, observations=empty_quotes(), connections=empty_quotes())
    assert status == 'CANDIDATE_TEST_ANCHOR_TIME_UNCERTAIN' and reasons == []
    e['evidence_relation_ledger'] = dict(validation_reasons=['NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED'],
        temporal_validation_reasons=['NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED'],
        relations=[dict(relation_status='SOURCE_BOUND_PROPOSAL',
            temporal_only_reasons=['NARRATIVE_OR_PUBLICATION_TIME_UNRESOLVED'], source_blocking_reasons=[])])
    r['checkpoint_kind'] = 'not_applicable'
    status, reasons = select(e=e, r=r, observations=empty_quotes(), connections=empty_quotes())
    assert status == 'CANDIDATE_TEST_ANCHOR_TIME_UNCERTAIN'
    assert reasons == []
    e['evidence_relation_ledger']['relations'][0]['source_blocking_reasons'] = [
        'UNKNOWN_OR_UNPROVIDED_EVIDENCE_OBJECT']
    status, reasons = select(e=e, r=r, observations=empty_quotes(), connections=empty_quotes())
    assert status == 'CANDIDATE_TEST_ANCHOR_TIME_UNCERTAIN'
    assert reasons == ['EVIDENCE_RELATION_UNRESOLVED']
    e['evidence_relation_ledger']['relations'][0].update(
        source_blocking_reasons=[], temporal_conflict_present=True)
    r['checkpoint'] = 'new_evidence'
    r['checkpoint_kind'] = 'evidence_informed_choice'
    status, reasons = select(e=e, r=r, observations=empty_quotes(), connections=empty_quotes())
    assert status == 'UNCERTAIN_CONTRACT_OR_SOURCE'
    assert 'EVIDENCE_RELATION_UNRESOLVED' in reasons


def test_CEREM_ADV014b_initial_management_action_fact_uses_real_validator_selector_chain():
    # An action-only current-information citation must also veto through the
    # live validator -> selector pipeline, without manually injecting a reason.
    r = review(checkpoint_kind='new_management_choice', previous_event_id=None,
        same_process_previous_id=None, process_link='uncertain', evidence_connection='uncertain',
        evidence_evolution='none', observation_quotes=[], connection_quotes=[], observation_availability=[])
    r['evidence_comparison'] = dict(previous_known_quotes=[], current_information_quotes=[
        dict(source_id='D', quote='遂决定切除重建。')], change_kind='none', reinterpretation_quotes=[],
        explanation='Synthetic current choice cannot predate itself.')
    _, e, sources, old, context, unit, client = integration_fixture(r)
    e['choice_boundary_citations'] = engine.resolve_model_quotes(
        [dict(source_id='D', quote='遂决定切除重建。')], [sources[1]], [])
    with patch.object(engine, 'build_evidence_context', return_value=context):
        result = engine.review_unit(unit, [e], [old, e], sources, {'review_context_characters': 10000}, client, [])
    out = result['events'][0]
    assert out['checkpoint_status'] == 'UNCERTAIN_CONTRACT_OR_SOURCE'
    assert 'CURRENT_ACTION_USED_AS_NEW_PATIENT_FACT' in out['validation_reasons']
    assert out['field_validation_errors'][0]['field'] == 'evidence_comparison.current_information_quotes'
    assert out['event_id'] == e['event_id']


def management_repair_fixture():
    old, cur, prev, e, corrected, ctx = fixture()
    e['choice_boundary_citations'] = engine.resolve_model_quotes(
        [dict(source_id='C', quote='决定补充检查。')], [cur], [])
    for proposal in (corrected,):
        proposal.update(checkpoint_kind='new_management_choice', proof_event_role='new_management_choice', previous_event_id=None,
            same_process_previous_id=None, process_link='uncertain', evidence_connection='uncertain',
            evidence_evolution='none', observation_quotes=[], connection_quotes=[], observation_availability=[])
        proposal['evidence_comparison'] = dict(previous_known_quotes=[], current_information_quotes=[],
            change_kind='none', reinterpretation_quotes=[], explanation='Synthetic current management choice.')
    e.update(evidence_context=ctx, actual_model_evidence=deepcopy(ctx['blocks']),
        provided_prior_event_ids=['E0'], provided_identity_event_ids=['E0', 'E1'],
        validated_connection_citations=engine.resolve_model_quotes(corrected['connection_quotes'], [old, cur], [],
            engine._allowed_connection_ranges(e, ctx, [old, cur])),
        review_request_sha256='original-hash', checkpoint_status='UNCERTAIN_CONTRACT_OR_SOURCE',
        validation_reasons=['CURRENT_ACTION_USED_AS_NEW_PATIENT_FACT'], semantic_review=deepcopy(corrected))
    return old, cur, prev, e, corrected, ctx


def test_CEREM_ADV015_repair_cannot_keep_action_fact_and_only_change_record_function():
    old, cur, prev, e, corrected, ctx = management_repair_fixture()
    corrected['evidence_comparison']['current_information_quotes'] = [dict(source_id='C', quote='决定补充检查。')]
    e['semantic_review'] = deepcopy(corrected)
    e['semantic_review']['record_function'] = 'execution'
    revised = refine.revalidate(e, corrected, [old, cur], [prev, e], [], {'request_sha256': 'repair-hash'})
    assert revised['checkpoint_status'] == 'UNCERTAIN_CONTRACT_OR_SOURCE'
    assert 'CURRENT_ACTION_USED_AS_NEW_PATIENT_FACT' in revised['validation_reasons']
    assert revised['field_validation_errors'][0]['field'] == 'evidence_comparison.current_information_quotes'
    assert refine.repair_triggers(revised) == []  # exactly one repair, not an unbounded loop


def test_CEREM_ADV016_repair_cannot_promote_action_only_observation():
    old, cur, prev, e, corrected, ctx = management_repair_fixture()
    corrected['observation_quotes'] = [dict(source_id='C', quote='决定补充检查。')]
    e['semantic_review'] = deepcopy(corrected)
    revised = refine.revalidate(e, corrected, [old, cur], [prev, e], [], {'request_sha256': 'repair-hash'})
    assert revised['checkpoint_status'] == 'UNCERTAIN_CONTRACT_OR_SOURCE'
    assert 'CURRENT_ACTION_USED_AS_NEW_PATIENT_FACT' in revised['validation_reasons']
    field_errors = [error for error in revised['field_validation_errors']
                    if (error.get('field_path') or error.get('field')) == 'observation_quotes']
    assert any(error['code'] == 'CURRENT_ACTION_USED_AS_NEW_PATIENT_FACT' for error in field_errors)
    corrected['observation_quotes'] = []
    positive = refine.revalidate(e, corrected, [old, cur], [prev, e], [], {'request_sha256': 'repair-hash'})
    assert positive['checkpoint_status'] == 'CANDIDATE_NEW_MANAGEMENT_CHOICE'
