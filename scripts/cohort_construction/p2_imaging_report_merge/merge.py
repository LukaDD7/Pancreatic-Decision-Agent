"""Merge the hospital imaging-report archive into frozen P2 source bundles.

Read-only on the frozen side. Every output goes to a new private directory.

What this fixes, and why it is a correction rather than an annotation
---------------------------------------------------------------------
Three defects in the frozen imaging sources are corrected in the data itself, so
that a later agent with no conversation context reads the right thing:

1. **44 labeled rows lost their impression.** Where both cells had been
   concatenated into ``original_findings`` behind ``原表【报告表现】`` /
   ``原表【报告结论】`` markers, ``original_impression`` was empty. The archive
   supplies the two cells separately, so each takes its own field.
2. **No release or verification clock existed.** The frozen projection carried
   only ``exam_time``; ``report_time`` was null on all 102 rows. The archive has
   检查/报告/审核 date+time, so ``report_time`` (written) and ``verified_time``
   (audited) are now populated as separate roles.
3. **审核时间 is the visible time** (user decision 2026-11-09). The shared
   resolver in ``p2_pathway_replay.core`` now returns the verified clock as the
   availability clock, so a later agent does not have to know this rule.

Identity is rebuilt rather than patched. ``source_id`` hashes
``patient_uid|family|index_report_uid|sha(text)``; correcting text necessarily
changes ``source_sha256``, ``source_id`` and the FHIR resource id. The receipt
therefore carries an old -> new map. For a row whose text did not change the
recomputed id must equal the frozen id; that equality is the independent check
that the join and the identity recipe are both right.

Best effort, and never silent
-----------------------------
A source that cannot be merged without guessing is kept frozen and recorded
under its own status — the batch still produces output for the other sources,
but the runner exits non-zero, so partial coverage cannot be mistaken for
success.
"""
from __future__ import annotations

import hashlib
import json
import re

from scripts.cohort_construction.p2_auto_checkpoint import fhir_io
from scripts.cohort_construction.p2_pathway_replay.core import (
    available_source_clock, availability_clock_role, clock, fields_text,
    imaging_projection, sha,
)

from .archive import recover_cells, split_labeled

FORMAT = 'p2-imaging-report-merge-0.1.0'
RELEASE_TIME_IS_AUDIT_TIME = True
MERGE_FAMILY = 'imaging_all'
DATE_TIME = re.compile(r'^\d{4}-\d{2}-\d{2}$')
CLOCK_TIME = re.compile(r'^\d{2}:\d{2}(?::\d{2})?$')
CELL_FIELDS = ('original_findings', 'original_impression')
# Every merged source needs all three named clocks present in its row.
CLOCK_COLUMNS = (('exam_time', 'exam_datetime', '检查日期', '检查时间'),
                 ('report_time', '报告时间', '报告日期', '报告时间'),
                 ('verified_time', 'verified_time', '审核日期', '审核时间'))

MERGED = 'MERGED'
UNMAPPED = 'UNMAPPED_KEPT_FROZEN'
CONFLICTING = 'CONFLICTING_ARCHIVE_ROWS_KEPT_FROZEN'
TEXT_DIFFERS = 'ARCHIVE_TEXT_DIFFERS_FROM_FROZEN_KEPT_FROZEN'
# Statuses that leave a source exactly as frozen.
FROZEN_KEPT = (UNMAPPED, CONFLICTING, TEXT_DIFFERS)


class MergeError(RuntimeError):
    """This code's own invariant failed; the run stops rather than write."""


def _combined(date_value, time_value, *, label):
    """Compose ``YYYY-MM-DD HH:MM[:SS]`` from two named export columns.

    Both parts are required. A date alone is not promoted to midnight and a time
    alone is not attached to the exam day: silence about the clock is not zero.
    """
    date_value = (date_value or '').strip()
    time_value = (time_value or '').strip()
    if not DATE_TIME.match(date_value):
        raise MergeError(label + '_DATE_NOT_EXACT_DAY:' + date_value[:24])
    if not CLOCK_TIME.match(time_value):
        raise MergeError(label + '_TIME_NOT_EXACT_CLOCK:' + time_value[:24])
    if len(time_value) == 5:
        time_value += ':00'
    composed = date_value + ' ' + time_value
    # The pattern alone accepts 25:00; the shared clock grammar decides.
    if not clock(composed)['value']:
        raise MergeError(label + '_CLOCK_NOT_VALID_INSTANT:' + composed[:24])
    return composed


def _try_combined(date_value, time_value, *, label):
    """``_combined`` for a candidate row: a malformed value simply does not match."""
    try:
        return _combined(date_value, time_value, label=label)
    except MergeError:
        return None


def _same_instant(left, right):
    """Compare to the coarser supplied precision, never upgrade either side."""
    left = (left or '').strip()
    right = (right or '').strip()
    if not left or not right:
        return False
    if len(left) == 16 and len(right) == 19:
        return right.startswith(left)
    if len(right) == 16 and len(left) == 19:
        return left.startswith(right)
    return left == right


def _row_cells(row):
    impression, findings, _ = recover_cells(row)
    return impression.strip(), findings.strip()


def select_row(rows, frozen_exam):
    """Return ``(row, archive_files, status)`` for one exam's candidate rows."""
    if not rows:
        return None, [], UNMAPPED
    agreeing = [row for row in rows
                if _same_instant(_try_combined(row.get('检查日期'), row.get('检查时间'),
                                               label='检查'), frozen_exam)]
    if not agreeing:
        # The exam number exists but no row has the frozen execution clock: the
        # archive row is not this exam's report.
        return None, sorted({row.get('_archive_file') for row in rows}), UNMAPPED
    contents = {_row_cells(row) for row in agreeing}
    files = sorted({row.get('_archive_file') for row in agreeing})
    if len(contents) != 1:
        return None, files, CONFLICTING
    return agreeing[0], files, None


def _frozen_split(frozen_findings):
    sections = split_labeled(frozen_findings or '')
    if sections is None:
        return None
    return ((sections.get('报告表现') or '').strip(),
            (sections.get('报告结论') or '').strip())


def _verify_against_frozen(frozen_fields, impression, findings):
    """Return the match basis, or ``None`` when the archive says something else.

    Comparison is on stripped text because a labeled split ends at the next
    marker, not at the cell's trailing newline; the merged fields keep the
    archive cell verbatim, so that whitespace is preserved, not normalized away.
    """
    frozen_impression = (frozen_fields.get('original_impression') or '').strip()
    frozen_findings = (frozen_fields.get('original_findings') or '').strip()
    archive_cells = (impression.strip(), findings.strip())
    split = _frozen_split(frozen_findings)
    if split is None:
        return ('plain_cells_verbatim'
                if (frozen_impression, frozen_findings) == archive_cells else None)
    return 'labeled_markers_split_verbatim' if split == archive_cells else None


def _status_entry(source, status, *, exam_no, detail=None, archive_files=()):
    return dict(
        status=status, frozen_source_id=source['source_id'],
        merged_source_id=source['source_id'], exam_no=exam_no,
        identity_preserved=True, text_changed=False,
        archive_files=list(archive_files), detail=detail)


def merge_source(source, archive_rows, *, patient_uid):
    """Return ``(source, evidence)`` for one frozen imaging source.

    The returned source is either fully merged or byte-identical to the frozen
    one; ``evidence['status']`` says which, and why in the second case.
    """
    provenance = source.get('provenance') or {}
    exam_no = str(provenance.get('exam_no_raw') or '').strip()
    if not exam_no:
        return source, _status_entry(source, UNMAPPED, exam_no='',
                                     detail='FROZEN_SOURCE_HAS_NO_EXAM_NUMBER')
    frozen_exam = (source.get('clocks', {}).get('exam_time') or {}).get('value') or ''
    row, archive_files, problem = select_row(archive_rows.get(exam_no) or [], frozen_exam)
    if problem:
        return source, _status_entry(source, problem, exam_no=exam_no,
                                     archive_files=archive_files)

    impression, findings, layout = recover_cells(row)
    if not impression.strip() or not findings.strip():
        return source, _status_entry(source, TEXT_DIFFERS, exam_no=exam_no,
                                     archive_files=archive_files,
                                     detail='ARCHIVE_CELL_EMPTY')
    frozen_fields = source.get('original_fields') or {}
    basis = _verify_against_frozen(frozen_fields, impression, findings)
    if basis is None:
        # The archive row is not the same report text; merging would replace
        # clinical text on a similarity judgement. Keep the frozen text.
        return source, _status_entry(source, TEXT_DIFFERS, exam_no=exam_no,
                                     archive_files=archive_files,
                                     detail='CELL_CONTENT_NOT_REPRODUCED')

    clocks_row = {}
    for role, key, date_field, time_field in CLOCK_COLUMNS:
        clocks_row[key] = _combined(row.get(date_field), row.get(time_field),
                                    label=date_field[:-2])
    projection_row = dict(provenance, **{
        'original_findings': findings, 'original_impression': impression}, **clocks_row)
    projected_fields, clocks, time_provenance = imaging_projection(projection_row)
    for key in CELL_FIELDS:
        if projected_fields.get(key) != \
                (findings if key == 'original_findings' else impression):
            raise MergeError('PROJECTION_CHANGED_FIELDS:' + exam_no + ':' + key)
    for role, key, date_field, time_field in CLOCK_COLUMNS:
        entry = time_provenance[role]
        entry['derived_from'] = dict(
            fields=[date_field, time_field],
            rule='SAME_ROW_DATE_FIELD_PLUS_TIME_FIELD_NO_DEFAULTING',
            values=[row.get(date_field), row.get(time_field)],
            archive_file=row.get('_archive_file'))
        entry['clinician_reading_certified'] = False

    # Frozen field order is preserved; only the two cell values are replaced.
    fields = {key: frozen_fields[key] for key in frozen_fields}
    fields['original_findings'] = findings
    fields['original_impression'] = impression
    text, field_spans = fields_text(fields)
    source_sha256 = sha(text)
    source_id = sha(patient_uid + '|' + MERGE_FAMILY + '|'
                    + str(provenance.get('index_report_uid')) + '|'
                    + sha(text))[:24]
    updated = dict(source)
    updated.update(text=text, source_sha256=source_sha256, source_id=source_id,
                   original_fields=fields, field_spans=field_spans,
                   clocks=clocks, time_provenance=time_provenance)
    updated['provenance'] = dict(
        provenance,
        merged_from_archive=dict(
            archive_file=row.get('_archive_file'),
            archive_files_with_same_content=archive_files,
            exam_no=exam_no, matched_on=basis, cell_layout=layout,
            clocks={role: clocks_row[key] for role, key, _, _ in CLOCK_COLUMNS}),
        column_semantics='报告结论=findings（长描述）; 报告表现=impression（短结论）')
    _, chosen, clock_basis, unresolved = available_source_clock(updated)
    if unresolved:
        raise MergeError('MERGED_SOURCE_TIME_UNRESOLVED:' + exam_no + ':'
                         + ','.join(unresolved))
    updated.update(clock=chosen, clock_basis=clock_basis)

    evidence = dict(
        status=MERGED, frozen_source_id=source['source_id'],
        merged_source_id=source_id, exam_no=exam_no,
        identity_preserved=source_id == source['source_id'],
        text_changed=text != source.get('text'),
        matched_on=basis, cell_layout=layout, archive_files=archive_files,
        exam_time_matched=frozen_exam,
        exam_time_archive=clocks_row['exam_datetime'],
        clock_basis=clock_basis,
        availability_role=availability_clock_role(clocks),
        clocks={role: (clocks.get(role) or {}).get('value')
                for role in ('exam_time', 'report_time', 'verified_time')})
    return updated, evidence


def merge_bundle(bundle, *, patient_uid, patient_alias, archive_rows,
                 convention_receipt):
    """Return ``(new_bundle, receipt)`` for one frozen case bundle."""
    sources, inventory = fhir_io.import_sources(bundle, deidentify=False)
    imaging = [source for source in sources if source.get('kind') == 'imaging']
    if not imaging:
        raise MergeError('CASE_HAS_NO_IMAGING_SOURCES:' + patient_uid)
    updates, evidence = {}, []
    for source in imaging:
        updated, row_evidence = merge_source(source, archive_rows,
                                             patient_uid=patient_uid)
        updates[source['source_id']] = updated
        evidence.append(row_evidence)
    merged_sources = [updates.get(source['source_id'], source) for source in sources]
    new_bundle = fhir_io.build(merged_sources, patient_alias, [], [])
    merged = [row for row in evidence if row['status'] == MERGED]
    receipt = dict(
        format=FORMAT, case=patient_alias, patient_uid=patient_uid,
        frozen_source_count=len(sources), imaging_source_count=len(imaging),
        merged_count=len(merged),
        frozen_kept_count=len(evidence) - len(merged),
        frozen_kept=[dict(status=row['status'], exam_no=row['exam_no'],
                          detail=row.get('detail'),
                          archive_files=row.get('archive_files', []))
                     for row in evidence if row['status'] != MERGED],
        identity_preserved_count=sum(row['identity_preserved'] for row in merged),
        identity_changed_count=sum(not row['identity_preserved'] for row in merged),
        text_changed_count=sum(row['text_changed'] for row in merged),
        verified_time_is_availability_count=sum(
            row.get('availability_role') == 'verified_time' for row in merged),
        source_import=inventory, rows=evidence,
        source_id_map=[dict(old=row['frozen_source_id'], new=row['merged_source_id'],
                            exam_no=row['exam_no'], status=row['status'],
                            text_changed=row['text_changed']) for row in evidence],
        column_semantics=convention_receipt,
        release_time_is_audit_time=RELEASE_TIME_IS_AUDIT_TIME,
        complete=len(merged) == len(evidence),
        clinical_reference_created=False, clinical_accuracy_measured=False)
    return new_bundle, receipt


def batch_receipt(cases, *, archive_path, archive_sha256, scan, convention,
                  output_dir, frozen_root):
    """One receipt for the whole merge: inputs, coverage, and every change."""
    merged = sum(case['merged_count'] for case in cases)
    total = sum(case['imaging_source_count'] for case in cases)
    return dict(
        format=FORMAT, archive_path=archive_path, archive_sha256=archive_sha256,
        frozen_root=frozen_root, output_dir=output_dir,
        archive_scan=dict(scanned_rows=scan['scanned_rows'],
                          csv_files=scan['csv_files'],
                          columns=len(scan['columns'])),
        exam_coverage=dict(frozen_imaging_sources=total, merged=merged,
                           frozen_kept=total - merged,
                           complete=merged == total),
        cases=cases, column_semantics=convention,
        release_time_is_audit_time=RELEASE_TIME_IS_AUDIT_TIME,
        frozen_inputs_modified=False, files_written_outside_output_dir=False,
        clinical_reference_created=False, clinical_accuracy_measured=False)


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + '\n'


def file_sha256(path):
    with open(path, 'rb') as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()