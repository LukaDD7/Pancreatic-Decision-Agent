"""V04-VISIBILITY001/002: audit time is visible time; the archive merge it needs.

Fictional text only — no clinical claim, no real patient, no provider call.

V04-VISIBILITY001 (user decision 2026-11-09)
    审核时间 (the audit/verification instant) is when an imaging report became
    visible. Before this, the archive's verified clock was parsed into every
    imaging source and read by no resolver: availability fell back to the 报告时间
    writing clock, or, when that was absent, to the exam proxy. Every negative
    control below is the dangerous direction — a report admitted earlier than it
    could be seen, or a bad audit clock silently replaced by the weaker one.

V04-VISIBILITY002
    The frozen imaging bundles carried no release or verification clock and, in
    44 of 102 rows, an empty impression whose text was buried in
    ``original_findings`` behind 原表【…】 markers. The merge corrects the data
    rather than annotating it, and must refuse every row it cannot reproduce from
    the archive verbatim.
"""
import hashlib

import pytest

from scripts.cohort_construction.p2_auto_checkpoint import fhir_io
from scripts.cohort_construction.p2_auto_checkpoint_v04 import evidence, evidence_objects
from scripts.cohort_construction.p2_imaging_report_merge import merge
from scripts.cohort_construction.p2_imaging_report_merge.archive import (
    ArchiveError, CellConvention, recover_cells, split_labeled,
)
from scripts.cohort_construction.p2_pathway_replay.core import (
    available_source_clock, clock, exact_duplicate_groups, project_sources,
    release_time_provenance, resource_selection,
)

PATIENT = 'SYNTHETIC-PATIENT'


def imaging(clocks, **fields):
    """A projected imaging source with only the clocks the test supplies."""
    row = dict(index_report_uid='SYNTHETIC-REPORT-1', exam_method='CT',
               original_findings='虚构所见。', original_impression='虚构结论。',
               exam_no_raw='EXAM-1', raw_record_path='synthetic/report.jsonl')
    row.update(fields)
    row.update(clocks)
    sources, _ = project_sources(dict(imaging_all=[row]), PATIENT)
    return sources[0]


# --- V04-VISIBILITY001: which clock makes a report visible ---------------------

def test_VIS001_audit_time_governs_visibility_and_the_writing_clock_is_kept():
    source = imaging(dict(exam_datetime='2022-09-01 08:00',
                          report_time='2022-09-01 10:00',
                          verified_time='2022-09-01 10:30'))
    role, chosen, basis, unresolved = available_source_clock(source)
    assert (role, chosen['value'], basis, unresolved) == \
        ('verified_time', '2022-09-01 10:30:00', 'verified_time_proxy', [])
    assert source['clock_basis'] == 'verified_time_proxy'
    assert source['clocks']['report_time']['value'] == '2022-09-01 10:00:00'


def test_VIS001_without_an_audit_clock_the_writing_clock_remains_the_proxy():
    source = imaging(dict(exam_datetime='2022-09-01 08:00', report_time='2022-09-01 10:00'))
    role, chosen, basis, unresolved = available_source_clock(source)
    assert (role, chosen['value'], basis, unresolved) == \
        ('report_time', '2022-09-01 10:00:00', 'report_time_proxy', [])


def test_VIS001_an_audit_clock_before_the_report_is_unresolved_not_earlier_visibility():
    # The archive answers 审核 before 报告: one of the two clocks is mislabeled.
    # Admitting the earlier one would date visibility before the report existed.
    source = imaging(dict(exam_datetime='2022-09-01 08:00',
                          report_time='2022-09-01 10:00',
                          verified_time='2022-09-01 09:00'))
    assert 'VERIFIED_BEFORE_REPORT_REVIEW_REQUIRED:report_time>verified_time' \
        in release_time_provenance(source)
    role, chosen, basis, unresolved = available_source_clock(source)
    assert (role, chosen['value'], basis) == \
        (None, None, 'release_time_unresolved_exam_fallback_forbidden')
    assert unresolved


def test_VIS001_an_unreadable_audit_clock_withholds_availability_without_fallback():
    source = imaging(dict(exam_datetime='2022-09-01 08:00', report_time='2022-09-01 10:00'))
    source['clocks']['verified_time'] = dict(value=None, precision='invalid', raw='2022-09-01 25:00')
    assert 'INVALID_STRUCTURED_VISIBILITY_CLOCK:verified_time' in release_time_provenance(source)
    assert available_source_clock(source)[0] is None
    # The whole source is withheld, not just the audit clock: a supplied-but-
    # unreadable 审核时间 leaves the report's time story contradictory, so
    # neither the audit nor the writing instant is published as settled.
    anchor, obj = evidence.report_objects(source)
    assert obj['publication_time']['value'] is None
    assert obj['publication_status'] == 'PUBLICATION_TIME_UNRESOLVED_REVIEW_REQUIRED'
    assert obj['availability_time']['value'] is None
    assert obj['availability_status'] == 'AVAILABILITY_TIME_UNRESOLVED_REVIEW_REQUIRED'
    # The raw clock is still preserved, so the review has the original value.
    assert source['clocks']['verified_time']['raw'] == '2022-09-01 25:00'


def test_VIS001_both_evidence_builders_name_the_same_visibility_clock():
    source = imaging(dict(exam_datetime='2022-09-01 08:00',
                          report_time='2022-09-01 10:00',
                          verified_time='2022-09-01 10:30'))
    _, first = evidence.report_objects(source)
    _, second = evidence_objects.report_objects(source)
    for built in (first, second):
        assert built['publication_time']['value'] == '2022-09-01 10:00:00'
        assert built['availability_time']['value'] == '2022-09-01 10:30:00'
        assert built['availability_role'] == 'verified_time'
        assert built['availability_clock_basis'] == 'VERIFIED_AUDIT_TIME_IS_VISIBILITY_CLOCK'
        assert built['availability_status'] == 'AVAILABILITY_TIME_PRESENT_NOT_READING_CERTIFIED'
        assert built['availability_establishes_reading'] is False
        assert built['availability_establishes_choice_time'] is False
        assert built['reading_status'] == 'UNKNOWN'


def test_VIS001_fhir_issued_is_the_audit_clock_and_the_writing_clock_survives_roundtrip():
    source = imaging(dict(exam_datetime='2022-09-01 08:00',
                          report_time='2022-09-01 10:00',
                          verified_time='2022-09-01 10:30'))
    bundle = fhir_io.build([source], PATIENT, [], [])
    report = next(e['resource'] for e in bundle['entry']
                  if e['resource']['resourceType'] == 'DiagnosticReport')
    assert report['issued'] == '2022-09-01T10:30:00+08:00'
    assert report['effectiveDateTime'] == '2022-09-01T08:00:00+08:00'
    restored, _ = fhir_io.import_sources(bundle)
    assert restored[0]['clocks'] == source['clocks']
    assert restored[0]['clock_basis'] == 'verified_time_proxy'


def test_VIS001_duplicate_reports_with_different_audit_clocks_are_not_one_source():
    first = imaging(dict(exam_datetime='2022-09-01 08:00', verified_time='2022-09-01 10:30'))
    second = imaging(dict(exam_datetime='2022-09-01 08:00', verified_time='2022-09-02 09:00'))
    assert first['source_sha256'] == second['source_sha256']
    assert exact_duplicate_groups([first, second]) == []
    third = imaging(dict(exam_datetime='2022-09-01 08:00', verified_time='2022-09-01 10:30'))
    assert sorted(exact_duplicate_groups([first, third])) == [sorted(
        [first['source_id'], third['source_id']])]


def test_VIS001_selection_names_the_visibility_clock_and_still_excludes_late_audit():
    source = imaging(dict(exam_datetime='2022-09-01 08:00', verified_time='2022-09-05 10:30'))
    selected = resource_selection(source, '2022-09-03 09:00', '2021-01-01')
    assert selected['disposition'] == 'EXCLUDE'
    assert selected['reason'] == 'AFTER_TARGET_PHASE'
    assert selected['clock_basis'] == 'verified_time'
    source = imaging(dict(exam_datetime='2022-09-01 08:00', verified_time='2022-09-02 10:30'))
    selected = resource_selection(source, '2022-09-03 09:00', '2021-01-01')
    assert selected['disposition'] == 'INCLUDE'
    assert selected['reason'] == \
        'VERIFIED_VISIBILITY_CLOCK_BEFORE_TARGET_NOT_READING_CERTIFICATION'


# --- V04-VISIBILITY002: the archive merge -------------------------------------

FINDINGS = '虚构：局部低密度灶，边界欠清，增强后轻度强化。'
IMPRESSION = '虚构：占位，建议复查。'


def archive_row(**changes):
    row = dict(检查号='EXAM-1', 检查日期='2022-09-01', 检查时间='08:00:00',
               报告日期='2022-09-01', 报告时间='10:00:00',
               审核日期='2022-09-02', 审核时间='10:30:00',
               报告表现=IMPRESSION, 报告结论=FINDINGS,
               _archive_file='虚构导出/影像.csv')
    row.update(changes)
    return row


def frozen_source(*, labeled=False, impression=IMPRESSION, findings=FINDINGS):
    if labeled:
        findings = '原表【报告表现】\n' + impression + '\n原表【报告结论】\n' + findings
        impression = ''
    return imaging(dict(exam_datetime='2022-09-01 08:00'),
                   original_findings=findings, original_impression=impression)


def merge_one(source, rows):
    return merge.merge_source(source, {'EXAM-1': rows}, patient_uid=PATIENT)


def test_VIS002_labelled_cells_are_separated_and_the_impression_is_restored():
    source = frozen_source(labeled=True)
    assert source['original_fields']['original_impression'] == ''
    merged, record = merge_one(source, [archive_row()])
    assert record['status'] == merge.MERGED
    assert record['matched_on'] == 'labeled_markers_split_verbatim'
    assert merged['original_fields']['original_impression'] == IMPRESSION
    assert merged['original_fields']['original_findings'] == FINDINGS
    assert record['text_changed'] is True and record['identity_preserved'] is False


def test_VIS002_plain_cells_are_verified_against_the_frozen_text_not_assumed():
    source = frozen_source()
    merged, record = merge_one(source, [archive_row()])
    assert record['matched_on'] == 'plain_cells_verbatim'
    assert record['text_changed'] is False
    # Same text -> the identity recipe must reproduce the frozen id exactly.
    assert record['identity_preserved'] is True
    assert merged['source_id'] == source['source_id']
    assert merged['source_sha256'] == source['source_sha256']


def test_VIS002_swapped_columns_would_put_findings_in_the_impression_field():
    # Negative control for the column convention: trusting the export's names
    # reverses the two cells, and the merge then refuses the row.
    swapped = archive_row(报告表现=FINDINGS, 报告结论=IMPRESSION)
    _, record = merge_one(frozen_source(), [swapped])
    assert record['status'] == merge.TEXT_DIFFERS
    assert record['detail'] == 'CELL_CONTENT_NOT_REPRODUCED'
    _, record = merge_one(frozen_source(labeled=True), [swapped])
    assert record['status'] == merge.TEXT_DIFFERS


def test_VIS002_a_kept_frozen_source_is_byte_identical_and_never_half_merged():
    source = frozen_source()
    kept, record = merge_one(source, [archive_row(报告结论='虚构：别的报告。')])
    assert record['status'] == merge.TEXT_DIFFERS
    assert kept == source


def test_VIS002_archive_rows_that_disagree_on_content_are_refused():
    source = frozen_source()
    rows = [archive_row(), archive_row(报告结论='虚构：另一份内容。',
                                       _archive_file='虚构导出/影像2.csv')]
    _, record = merge_one(source, rows)
    assert record['status'] == merge.CONFLICTING


def test_VIS002_a_row_with_another_exam_clock_is_not_this_report():
    source = frozen_source()
    _, record = merge_one(source, [archive_row(检查时间='09:15:00')])
    assert record['status'] == merge.UNMAPPED


def test_VIS002_exam_not_in_the_archive_stays_frozen_and_is_reported():
    source = frozen_source()
    kept, record = merge_one(source, [])
    assert record['status'] == merge.UNMAPPED
    assert kept == source


def test_VIS002_only_the_three_named_clocks_are_used_and_none_is_defaulted():
    merged, record = merge_one(frozen_source(), [archive_row()])
    assert record['clocks'] == {'exam_time': '2022-09-01 08:00:00',
                                'report_time': '2022-09-01 10:00:00',
                                'verified_time': '2022-09-02 10:30:00'}
    assert merged['clock_basis'] == 'verified_time_proxy'
    assert merged['clock']['value'] == '2022-09-02 10:30:00'
    provenance = merged['time_provenance']
    for role in ('exam_time', 'report_time', 'verified_time'):
        assert provenance[role]['status'] == 'MAPPED_FROM_NAMED_EXPORT_FIELD'
        assert provenance[role]['derived_from']['rule'] == \
            'SAME_ROW_DATE_FIELD_PLUS_TIME_FIELD_NO_DEFAULTING'
        assert provenance[role]['clinician_reading_certified'] is False


@pytest.mark.parametrize('column, value', [
    ('审核时间', ''), ('审核日期', ''), ('报告时间', ''), ('报告日期', '2022-9-1'),
    ('审核时间', '25:00:00'), ('报告日期', '2022-13-01')])
def test_VIS002_a_missing_or_bad_time_part_refuses_the_row(column, value):
    with pytest.raises(merge.MergeError):
        merge_one(frozen_source(), [archive_row(**{column: value})])


@pytest.mark.parametrize('value', ['25:00:00', '', '早上'])
def test_VIS002_a_row_whose_exam_clock_is_unusable_is_not_matched_at_all(value):
    # The exam clock is the join key. An unusable one cannot confirm that this
    # row is the frozen exam's report, so the row is left alone — not merged
    # with a guessed identity, and not treated as a confirmed absence.
    _, record = merge_one(frozen_source(), [archive_row(检查时间=value)])
    assert record['status'] == merge.UNMAPPED


def test_VIS002_merged_bundle_round_trips_with_the_merged_identity():
    source = frozen_source(labeled=True)
    merged, record = merge_one(source, [archive_row()])
    bundle = fhir_io.build([merged], PATIENT, [], [])
    restored, _ = fhir_io.import_sources(bundle)
    assert restored[0]['source_id'] == merged['source_id']
    assert restored[0]['text'] == merged['text']
    assert restored[0]['original_fields'] == merged['original_fields']
    assert restored[0]['clocks'] == merged['clocks']
    assert restored[0]['time_provenance'] == merged['time_provenance']


def test_VIS002_column_convention_is_proved_from_content_and_stops_on_disagreement():
    convention = CellConvention()
    convention.add_exam('EXAM-1', [archive_row()])
    receipt = convention.check()
    assert receipt['finding'] == '报告结论_holds_findings，报告表现_holds_impression'
    assert receipt['basis']['descriptive_votes_per_row']['conclusion'] == 1

    labeled = archive_row(报告表现='', 报告结论='原表【报告表现】\n' + IMPRESSION
                          + '\n原表【报告结论】\n' + FINDINGS)
    convention = CellConvention()
    convention.add_exam('EXAM-1', [archive_row(), labeled])
    assert convention.check()['basis']['both_layout_cell_agreements'] == 1

    disagreeing = dict(labeled, 报告结论='原表【报告表现】\n' + FINDINGS
                       + '\n原表【报告结论】\n' + IMPRESSION)
    convention = CellConvention()
    convention.add_exam('EXAM-1', [archive_row(), disagreeing])
    with pytest.raises(ArchiveError, match='LABELED_AND_NAMED_LAYOUT_DISAGREE'):
        convention.check()


def test_VIS002_convention_is_rejected_when_content_does_not_support_it():
    convention = CellConvention()
    # Findings and impression swapped relative to the export's column names:
    # the descriptive vote turns negative and the merge must not proceed.
    convention.add_exam('EXAM-1', [archive_row(报告表现=FINDINGS, 报告结论=IMPRESSION)])
    with pytest.raises(ArchiveError, match='COLUMN_CONVENTION_NOT_SUPPORTED_BY_CONTENT'):
        convention.check()


def test_VIS002_labeled_cell_missing_a_section_is_refused_not_partially_read():
    with pytest.raises(ArchiveError, match='LABELED_CELL_INCOMPLETE'):
        recover_cells(archive_row(报告表现='', 报告结论='原表【报告表现】\n' + IMPRESSION))
    assert split_labeled('前有散文\n原表【报告表现】\n甲') is None


def test_VIS002_issue_map_registers_these_tests():
    import json
    from pathlib import Path
    registry = json.loads((Path(__file__).with_name('visibility_issue_map_v04.json'))
                          .read_text())
    assert registry['clinical_gold'] is False
    names = {name for name in globals() if name.startswith('test_')}
    registered = {name for issue in registry['issues']
                  for name in issue['required_tests']}
    # Every local test has a registered issue. Tests implemented in the owning
    # context/organization modules use their fully qualified names instead.
    local_registered = {name for name in registered if '::' not in name}
    assert local_registered <= names, local_registered - names
    assert names - {'test_VIS002_issue_map_registers_these_tests'} <= local_registered
    assert [issue['id'] for issue in registry['issues']] == \
        ['V04-VISIBILITY001', 'V04-VISIBILITY002', 'V04-VISIBILITY003']