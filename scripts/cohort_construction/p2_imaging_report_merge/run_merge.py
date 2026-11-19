"""Run the imaging-report archive merge into brand-new private output.

This writes a new directory and nothing else. The frozen bundles under
``--frozen-root`` are opened read-only, and the archive zip is parsed in memory
without extraction, because it is untrusted external data.

Usage
-----
    python3 -m scripts.cohort_construction.p2_imaging_report_merge \\
        --archive /Volumes/LQ1000/影像报告.zip \
        --frozen-root /private/tmp/.../output \
        --manifest private/p2_pathway_checkpoint_scale10_v0.1_20261023/sampling_manifest.json \
        --output private/p2_imaging_report_merge_<date>

Exit status is 0 only when every frozen imaging source merged. A partially
merged batch still writes its full output and receipt, then exits non-zero, so
an incomplete merge cannot be read later as a complete one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .archive import CellConvention, read_rows
from .merge import (
    FROZEN_KEPT, MergeError, batch_receipt, dump, file_sha256, merge_bundle,
)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCHEMA = ROOT / 'private/p2_auto_checkpoint_v0.1_20261024/standards/fhir.schema.json'


def _read(path):
    return json.loads(Path(path).read_text('utf-8'))


def _mk_private(path):
    """Create a new owner-only directory; refuse to reuse an existing one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        raise SystemExit('OUTPUT_DIRECTORY_ALREADY_EXISTS:' + str(path))
    if path.stat().st_mode & 0o077:
        raise SystemExit('PRIVATE_DIRECTORY_PERMISSIONS_TOO_BROAD:' + str(path))
    return path


def _write_new(path, value):
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(dump(value))
        stream.flush()
        os.fsync(stream.fileno())


def freeze_source_ids(frozen_root):
    """Every frozen imaging source's identity, by exam number, per case."""
    from scripts.cohort_construction.p2_auto_checkpoint import fhir_io
    wanted, cases = set(), {}
    for case_dir in sorted(Path(frozen_root).glob('SCALE-*')):
        bundle = _read(case_dir / 'source_fhir_r4_bundle.json')
        sources, _ = fhir_io.import_sources(bundle, deidentify=False)
        imaging = [source for source in sources if source.get('kind') == 'imaging']
        if not imaging:
            continue
        cases[case_dir.name] = case_dir / 'source_fhir_r4_bundle.json'
        for source in imaging:
            exam = str((source.get('provenance') or {}).get('exam_no_raw') or '')
            if exam:
                wanted.add(exam)
    return wanted, cases


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--archive', required=True)
    parser.add_argument('--frozen-root', required=True)
    parser.add_argument('--manifest', required=True,
                        help='sampling manifest holding each case patient_uid')
    parser.add_argument('--output', required=True)
    parser.add_argument('--schema', default=str(DEFAULT_SCHEMA))
    parser.add_argument('--no-validate', action='store_true',
                        help='skip the FHIR schema check (never for a real merge)')
    args = parser.parse_args(argv)

    manifest = _read(args.manifest)
    aliases = {entry['patient_uid']: entry['case_id'] for entry in manifest['selected']}
    wanted, bundles = freeze_source_ids(args.frozen_root)
    if not bundles:
        raise SystemExit('FROZEN_ROOT_HAS_NO_CASE_BUNDLES:' + args.frozen_root)
    # Cases named by the manifest but absent from the frozen root: recorded, not
    # silently dropped, and never counted as merged.
    unassigned = sorted(uid for uid, case_id in aliases.items()
                        if case_id not in bundles)
    if unassigned:
        print('CASES_WITHOUT_FROZEN_BUNDLE:' + ','.join(unassigned), file=sys.stderr)

    scan = read_rows(args.archive, wanted)
    convention = CellConvention()
    for exam, rows in scan['rows'].items():
        convention.add_exam(exam, rows)
    convention_receipt = convention.check()

    output = _mk_private(args.output)
    from scripts.cohort_construction.p2_auto_checkpoint import fhir_io
    cases, failures = [], []
    for case_id, bundle_path in sorted(bundles.items()):
        patient_uid = next((uid for uid, alias in aliases.items() if alias == case_id), None)
        if patient_uid is None:
            raise SystemExit('CASE_NOT_IN_SAMPLING_MANIFEST:' + case_id)
        bundle = _read(bundle_path)
        merged, receipt = merge_bundle(bundle, patient_uid=patient_uid,
                                       patient_alias=case_id, archive_rows=scan['rows'],
                                       convention_receipt=convention_receipt)
        receipt['frozen_bundle_path'] = str(bundle_path)
        receipt['frozen_bundle_sha256'] = file_sha256(bundle_path)
        if not args.no_validate:
            receipt['fhir_validation'] = fhir_io.validate(merged, args.schema)
        case_dir = _mk_private(output / case_id)
        _write_new(case_dir / 'source_fhir_r4_bundle.json', merged)
        _write_new(case_dir / 'merge_receipt.json', receipt)
        cases.append(receipt)
        for row in receipt['frozen_kept']:
            failures.append(dict(case=case_id, **row))

    receipt = batch_receipt(
        cases, archive_path=str(Path(args.archive).resolve()),
        archive_sha256=file_sha256(args.archive), scan=scan, convention=convention_receipt,
        output_dir=str(output.resolve()),
        frozen_root=str(Path(args.frozen_root).resolve()))
    receipt['cases_without_frozen_bundle'] = unassigned
    receipt['frozen_kept_details'] = failures
    receipt['outputs_verified_new'] = True
    _write_new(output / 'merge_batch_receipt.json', receipt)

    coverage = receipt['exam_coverage']
    print('merged %d/%d frozen imaging sources across %d cases'
          % (coverage['merged'], coverage['frozen_imaging_sources'], len(cases)))
    for row in failures:
        print('KEPT FROZEN %s %s %s' % (row['case'], row['status'], row['exam_no']),
              file=sys.stderr)
    return 0 if coverage['complete'] else 1


if __name__ == '__main__':
    sys.exit(main())