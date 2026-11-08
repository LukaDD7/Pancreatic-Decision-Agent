"""CE-REM-ADV-005/006/007: final source audit counterexamples, synthetic only."""
import base64
from copy import deepcopy
import hashlib

from scripts.cohort_construction.p2_auto_checkpoint import fhir_io
from scripts.cohort_construction.p2_auto_checkpoint.extract import window
from scripts.cohort_construction.p2_auto_checkpoint_v04.context import (
    build_evidence_context, context_for_discovery,
)
from scripts.cohort_construction.p2_auto_checkpoint_v04.evidence import report_objects
from scripts.cohort_construction.p2_auto_checkpoint_v04.source_time import source_record_time
from scripts.cohort_construction.p2_pathway_replay.core import (
    available_source_clock, clock, relation_to_cutoff, resource_selection,
)


def document(text, sid, when=None):
    return dict(source_id=sid, kind='document', title='note', text=text,
        source_sha256=hashlib.sha256(text.encode()).hexdigest(), clocks={},
        clock=clock(when), clock_basis='body_record_time')


def test_CEREM_ADV005_offset_seconds_cannot_admit_same_instant_as_prior():
    raw = '2022-01-01T10:00:00+05:30:15'
    exact = clock(raw)
    # Reference arithmetic: 10:00 +05:30:15 == 12:29:45 +08:00.
    assert exact == dict(value='2022-01-01 12:29:45', precision='second', raw=raw)
    assert relation_to_cutoff(exact, '2022-01-01 12:29:45') == 'SAME_TIME_UNORDERED'
    assert relation_to_cutoff(exact, '2022-01-01 12:29:45.5') == 'BEFORE'
    source = dict(source_id='I', kind='imaging', title='CT', text='synthetic',
        source_sha256='x', provenance={}, time_provenance={},
        clocks=dict(exam_time=clock('2022-01-01 08:00'), report_time=exact))
    assert resource_selection(source, '2022-01-01 12:29:45', '2021-01-01')['reason'] == 'SAME_TIME_UNORDERED'
    current = document('current', 'A', '2022-01-01 12:29:45')
    event = dict(event_id='E', source_id='A', source_ids=['A'], choice_time=current['clock']['value'],
        time_basis='explicit_documented_choice', action_citations=[], prechoice_citations=[], postchoice_citations=[])
    context = build_evidence_context(event, [current, source])
    assert not any(block['source_id'] == 'I' for block in context['blocks'])


def test_CEREM_ADV006_legacy_window_uses_structured_release_not_stale_sample_clock():
    source = dict(source_id='L', kind='lab', title='analyte', text='synthetic',
        source_sha256='l', provenance={}, time_provenance={},
        clocks=dict(sample_time=clock('2022-01-01 08:00'), report_time=clock('2022-01-01 09:00')),
        clock=clock('2022-01-01 08:00'), clock_basis='legacy_report_time')
    candidate = dict(source_id='X', same_test_source_ids=[], decision_clock=clock('2022-01-01 08:30'),
        decision_time_upper_bound=None, historical_hint=False, anchor=dict(start=0), choice_start=0,
        current_note_prefix=dict(start=0))
    assert window([source], candidate)['source_ledger'][0]['disposition'] == 'EXCLUDE_FUTURE'
    candidate['decision_clock'] = clock('2022-01-01 10:00')
    assert window([source], candidate)['blocks'][0]['clock']['value'] == '2022-01-01 09:00:00'
    source['clocks']['report_time'] = clock('bad')
    ledger = window([source], candidate)['source_ledger'][0]
    assert ledger['disposition'] == 'REVIEW'
    assert ledger['reason'] == 'RELEASE_TIME_UNRESOLVED_EXAM_PROXY_FORBIDDEN'
    assert 'INVALID_STRUCTURED_RELEASE_CLOCK:report_time' in ledger['release_time_provenance_errors']


def test_CEREM_ADV007_document_adapter_preserves_minute_and_rejects_bad_clocks():
    current = document('current', 'A', '2022-01-01 10:00:30')
    minute = dict(document('minute raw', 'B'),
        clock=dict(value='2022-01-01 10:00:00', raw='2022-01-01 10:00'))
    event = dict(event_id='E', source_id='A', source_ids=['A'], choice_time=None,
        time_basis='record_time_proxy', action_citations=[], prechoice_citations=[], postchoice_citations=[])
    context = build_evidence_context(event, [current, minute])
    row = next(item for item in context['source_ledger'] if item['source_id'] == 'B')
    assert row['disposition'] == 'REVIEW' and row['reason'] == 'SAME_MINUTE_UNORDERED'
    assert context_for_discovery(dict(unit_id='U', source_ids=['A'], kind='document',
        document_profile='clinical_other', clock=current['clock'], original_texts=[]),
        [current, minute])['uncertain_time_source_catalog'][0]['clock']['precision'] == 'minute'
    for mutation in (
            dict(value='2022-01-01 10:00:00', raw='2022-01-01 10:00', precision='second'),
            dict(value='0000-invalid', raw='bad', precision='second')):
        bad = dict(minute, clock=mutation)
        record = source_record_time(bad)
        assert record['clock']['value'] is None
        assert record['status'] == 'SOURCE_CLOCK_INVALID_REVIEW_REQUIRED'
        assert build_evidence_context(event, [current, bad])['blocks'] == []


def test_CEREM_ADV007_structured_document_creation_beats_signature_and_stale_proxy():
    text = '医师甲\n2022年9月3日\n'
    source = dict(document(text, 'D'), title='消化内镜检查申请单',
        clocks=dict(source_create_time=clock('2022-09-01T09:00:00+08:00')))
    # Even an old stale signature proxy cannot override a valid native clock.
    source['clock'] = dict(value='2022-09-03', precision='date', raw='2022年9月3日')
    source['clock_basis'] = 'request_physician_signature_record_day_proxy'
    assert source_record_time(source)['clock']['value'] == '2022-09-01 09:00:00'
    resource = dict(resourceType='DocumentReference', id='D', subject={'reference': 'Patient/P'},
        type={'text': '消化内镜检查申请单'}, date='2022-09-01T09:00:00+08:00',
        content=[{'attachment': {'data': base64.b64encode(text.encode()).decode()}}])
    bundle = dict(resourceType='Bundle', type='collection', entry=[
        dict(resource=dict(resourceType='Patient', id='P')), dict(resource=resource)])
    restored, _ = fhir_io.import_sources(bundle)
    assert restored[0]['clock']['value'] == '2022-09-01 09:00:00'
    assert restored[0]['clock_basis'] == 'source_create_time'


def test_CEREM_ADV007_signature_prose_is_not_a_physician_name():
    text = '医师：2022年8月1日修订\n2022年9月3日\n'
    source = dict(document(text, 'D'), title='消化内镜检查申请单', clock=clock(None))
    result = source_record_time(source)
    assert result['clock']['value'] is None
    assert result['status'] == 'REQUEST_SIGNATURE_DAY_NOT_FOUND'
    assert 'PHYSICIAN_SIGNATURE_NAME_CONTAINS_DATE_OR_PROSE' in result['uncertainty']
    # Same layout with a non-date signatory remains a bounded record-day proxy.
    positive = dict(source, text='医师：甲\n2022年9月3日\n')
    positive['source_sha256'] = hashlib.sha256(positive['text'].encode()).hexdigest()
    assert source_record_time(positive)['clock']['value'] == '2022-09-03'


def test_CEREM_ADV006_generic_fhir_observation_issued_is_retained():
    resource = dict(resourceType='Observation', id='O', subject={'reference': 'Patient/P'},
        code={'text': 'analyte'}, effectiveDateTime='2022-01-01T08:00:00+08:00',
        issued='2022-01-01T09:00:00+08:00')
    bundle = dict(resourceType='Bundle', type='collection', entry=[
        dict(resource=dict(resourceType='Patient', id='P')), dict(resource=resource)])
    restored, _ = fhir_io.import_sources(bundle)
    assert restored[0]['clock']['value'] == '2022-01-01 09:00:00'
    assert restored[0]['clock_basis'] == 'report_time_proxy'
    candidate = dict(source_id='X', same_test_source_ids=[], decision_clock=clock('2022-01-01 10:00'),
        decision_time_upper_bound=None, historical_hint=False, anchor=dict(start=0), choice_start=0,
        current_note_prefix=dict(start=0))
    assert window(restored, candidate)['source_ledger'][0]['disposition'] == 'INCLUDE_PROXY_PRIOR'


def test_CEREM_ADV006_evidence_object_uses_resolver_precision():
    source = dict(source_id='L', kind='lab', title='analyte', text='synthetic',
        source_sha256='l', provenance={}, time_provenance={},
        clocks=dict(sample_time=clock('2022-01-01 08:00'),
            report_time=dict(value='2022-01-01 09:00:00', raw='2022-01-01 09:00')))
    assert available_source_clock(source)[1]['precision'] == 'minute'
    anchor, evidence = report_objects(source)
    assert evidence['publication_time']['precision'] == 'minute'
    assert anchor['execution_time']['precision'] == 'minute'


def test_CEREM_ADV005_submicroseconds_remain_distinct_and_zero_fraction_keeps_precision():
    before = clock('2022-01-01T10:00:00.123456788+08:00')
    after = clock('2022-01-01T10:00:00.123456789+08:00')
    assert after['value'] == '2022-01-01 10:00:00.123456789'
    assert after['precision'] == 'fractional_second'
    assert relation_to_cutoff(after, before) == 'AFTER'
    assert relation_to_cutoff(before, after) == 'BEFORE'
    tiny = clock('2022-01-01 10:00:00.0000001')
    assert tiny['value'].endswith('.0000001') and tiny['precision'] == 'fractional_second'
    zero = clock('2022-01-01 10:00:00.000000000')
    assert zero['precision'] == 'fractional_second'
    # Extra precision beyond the supported representation fails closed, not truncates.
    assert clock('2022-01-01 10:00:00.1234567891')['precision'] == 'invalid'


def test_CEREM_ADV005_minute_offset_arithmetic_is_not_a_lexical_guess():
    early = clock('2022-01-01T10:59+10:00')  # 08:59, minute precision at UTC+8
    late = clock('2022-01-01T11:00+09:00')   # 10:00, minute precision at UTC+8
    assert relation_to_cutoff(early, late) == 'BEFORE'
    assert relation_to_cutoff(late, early) == 'AFTER'
    assert relation_to_cutoff(early, '2022-01-01 08:59:30') == 'SAME_MINUTE_UNORDERED'
