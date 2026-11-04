"""Replay full frozen P0; keep relevance, usability, and QC flags independent.

No old PASS-only filter. All output carrying patients/source text stays in the
ignored runtime folder. No API calls; original data is read, never rewritten.
"""
from collections import Counter, defaultdict
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import pyarrow
import pyarrow.parquet as pq
from .labels import coverage, digest, normalize, parse_image
from .references import citations, dateof, exam_number, link, modality


def private_mkdir(path, *, parents=False, exist_ok=False):
    """Create owner-only output directories without changing existing roots."""
    path = Path(path)
    if parents and not path.exists():
        private_mkdir(path.parent, parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=exist_ok)
    if path.stat().st_mode & 0o077:
        raise PermissionError('P1_PRIVATE_DIRECTORY_PERMISSIONS_TOO_BROAD: ' + str(path))


def private_open(path, mode):
    """Create a private artifact atomically; never overwrite a prior release."""
    if 'x' not in mode:
        raise ValueError('P1_PRIVATE_ARTIFACT_REQUIRES_EXCLUSIVE_CREATE')
    descriptor = os.open(Path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, mode, encoding=None if 'b' in mode else 'utf-8',
                     newline=None if 'b' in mode else '')


def private_root(repo):
    """The physical private/ root must also be the Git-ignored project path."""
    root = (repo / 'private').resolve()
    if not root.is_dir():
        raise FileNotFoundError('P1_PRIVATE_ROOT_REQUIRED: ' + str(root))
    if (Path(repo) / '.gitignore').is_file():
        if '/private/' not in (Path(repo) / '.gitignore').read_text(encoding='utf-8').splitlines():
            raise ValueError('P1_PRIVATE_ROOT_MUST_BE_GIT_IGNORED')
    return root


def private_path(repo, *parts):
    path = private_root(repo).joinpath(*parts).resolve()
    if not path.is_relative_to(private_root(repo)):
        raise ValueError('P1_PRIVATE_OUTPUT_PATH_REQUIRED')
    return path


def legacy_path(path):
    """Resolve an already migrated frozen input without using old storage layout."""
    value = str(path)
    marker = '/local_artifacts/'
    if marker in value:
        prefix, suffix = value.split(marker, 1)
        candidate = Path(prefix) / 'private' / suffix
        if candidate.is_file():
            return candidate.resolve()
    return Path(path).resolve()


def clinical_inputs(manifest, prior_run=None):
    """Read frozen clinical originals without the stale cache manifest.

    The prior release's input_hashes records the old cache-manifest insertion
    order. Retaining that order makes a storage-only replay comparable, while
    each original still requires the independent frozen v0.2 byte hash.
    """
    originals = [original for original in manifest if original.endswith('.parquet')]
    if prior_run is not None:
        previous = json.loads((prior_run/'run_manifest.json').read_text())
        ordered = [original for original in previous.get('input_hashes', {}) if original.endswith('.parquet')]
        if len(ordered) != len(set(ordered)) or set(ordered) != set(originals):
            raise ValueError('P1_PRIOR_CLINICAL_INPUT_ORDER_MISMATCH')
        for original in ordered:
            if previous['input_hashes'][original] != manifest[original]['sha256']:
                raise ValueError('P1_PRIOR_CLINICAL_INPUT_HASH_MISMATCH')
        return ordered
    return originals



def read_jsonl(path):
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def write_json(path, value):
    with private_open(path, 'x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def write_jsonl(path, values):
    with private_open(path, 'x') as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False)+'\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance_key(uid, content_hash, ref):
    return (uid, content_hash, str(ref.get('index_report_uid')), str(ref.get('index_origin')),
            str(ref.get('source_file_name')), str(ref.get('source_sheet')),
            str(ref.get('source_row')), str(ref.get('record_cluster_id')))


def classify(identity_ok, usable_related_count, potential_count, coverage_complete):
    # QC flags cannot negate another independently usable, relevant record.
    if identity_ok and usable_related_count:
        return 'INCLUDE_V3'
    # Zero algorithm hits cannot prove clinical irrelevance. Even complete
    # file coverage is not expert confirmation of absence: keep reviewable.
    return 'REVIEW_V3'


def ensure_images(repo, frozen_manifest):
    """Copy exact frozen image inputs out of iCloud before analysis.

    Freeze-hash mismatches fail the run. A cached copy is reused only after its
    hash matches the frozen input manifest, never from file presence alone.
    """
    # Immutable byte-identical source cache is shared; derived outputs are not.
    output = private_path(repo, 'p1_quality_v0.3', 'source_cache')
    paths = [p for p in frozen_manifest if p.endswith('/imaging_reports_all_sources.jsonl')]
    def copy_one(original):
        src = legacy_path(original)
        expected = frozen_manifest[original]['sha256']
        # Include the frozen content hash in the cache identity. A later
        # legitimate source revision must not collide with earlier bytes.
        target = output/src.parent.parent.parent.name/src.parent.name/(expected + '-' + src.name)
        if target.exists() and sha(target) == expected:
            return original, target, expected
        raw = src.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected, 'Frozen imaging input changed'
        private_mkdir(target.parent, parents=True, exist_ok=True)
        try:
            with private_open(target, 'xb') as stream:
                stream.write(raw)
        except FileExistsError:
            # A concurrent identical cache fill is valid only for exact bytes;
            # a mismatched existing cache must never be silently reused.
            if sha(target) != expected:
                raise ValueError('P1_SOURCE_CACHE_HASH_MISMATCH: ' + str(target))
        return original, target, expected
    records = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, row in enumerate(pool.map(copy_one, paths), 1):
            records.append(row)
            if i % 300 == 0:
                print(json.dumps({'stage': 'image_cache', 'files': i, 'total': len(paths)}), flush=True)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument('--run-id', default='results',
        help='New output directory under this version; existing outputs are never overwritten')
    parser.add_argument('--no-public-release', action='store_true',
        help='Synthetic/repeat development run without publishing the aggregate summary')
    parser.add_argument('--prior-run', type=Path,
        help='Prior release directory used only to preserve frozen clinical input order')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', args.run_id) or '..' in args.run_id:
        raise ValueError('P1_RUN_ID_INVALID')
    repo = args.repo.resolve()
    here = Path(__file__).resolve().parent
    code_hashes_at_start = {p.name: sha(p) for p in sorted(here.glob('*.py'))}
    rules_hash_at_start = sha(here/'rules.json')
    rules = json.loads((here/'rules.json').read_text())
    output = private_path(repo, rules['artifact_directory'], args.run_id)
    public_summary = repo/'docs'/(rules['public_release_stem']+'.json')
    if not args.no_public_release and public_summary.exists():
        raise FileExistsError('P1_PUBLIC_SUMMARY_REQUIRES_NEW_VERSION: '+str(public_summary))
    private_mkdir(output, parents=True)
    start = time.monotonic()
    oldbase = private_path(repo, 'p1_quality_v0.2', 'results')
    old = {p['patient_uid']: p for p in read_jsonl(oldbase/'patient_quality.jsonl')}
    manifest = json.loads((oldbase/'input_manifest.json').read_text())
    assert len(old) == rules['P0_n']
    sources = ensure_images(repo, manifest)
    assert len(sources) == len(old), 'Must inspect imaging inputs for full P0'
    hashes = {str(original): digest_bytes for original, _, digest_bytes in sources}
    # Reuse ORIGINAL-source reconciliation only for byte-identical frozen
    # indexed input. This is inherited proof, not a fresh raw-cluster re-audit.
    proof = {}
    for r in read_jsonl(oldbase/'report_quality.jsonl'):
        for ref in r['source_refs']:
            key = provenance_key(r['patient_uid'], r['parsed']['normalized_text_sha256'], ref)
            value = ref['source_verification']
            if key in proof:
                assert proof[key] == value
            proof[key] = value
    patients = {uid: {'patient_uid': uid, 'alias': p['alias'], 'previous_state': p['state'],
                       'identity_index_ok': p['identity_index_ok'], 'raw_clinical_rows': 0,
                       'image_rows': 0, 'usable_related_records': 0, 'potential_records': 0,
                       'ABC_record_counts': Counter(), 'flags': Counter()}
                for uid, p in old.items()}
    images, image_by_patient = {}, defaultdict(list)
    raw_image_rows = 0
    for original, cached, _ in sources:
        folder_uid = Path(original).parent.name
        for row in read_jsonl(cached):
            raw_image_rows += 1
            uid = row['patient_uid']
            assert uid == folder_uid and uid in patients
            p = patients[uid]
            p['image_rows'] += 1
            fields = [normalize(row.get(k)) for k in ['original_findings', 'original_impression']]
            content_hash = digest('\n'.join(fields))
            inherited = proof[provenance_key(uid, content_hash, row)]
            mod, date = modality(row.get('exam_method')), dateof(row.get('exam_datetime'))
            # Content and source metadata are separate. Metadata union is
            # deterministic and must not depend on first input row.
            key = digest('|'.join([uid, date, mod, content_hash]))[:24]
            if key not in images:
                parsed = parse_image(row, rules)
                images[key] = {'record_key': key, 'patient_uid': uid, 'date': date, 'modality': mod,
                               'exam_nos': [], 'methods': [], 'source_refs': [], 'has_verified_source': False,
                               'parsed': parsed, 'coverage': parsed['coverage'],
                               'original_findings': row.get('original_findings'), 'original_impression': row.get('original_impression')}
                image_by_patient[uid].append(images[key])
            r = images[key]
            r['has_verified_source'] = r['has_verified_source'] or inherited['verified']
            r['source_refs'].append({'original_input_path': original, 'index_report_uid': row.get('index_report_uid'),
                                     'origin': row.get('index_origin'), 'inherited_source_proof': inherited,
                                     'proof_mode': 'frozen_v0.2_proof_on_identical_index_input'})
            r['methods'] = sorted(set(r['methods']) | {str(row.get('exam_method') or '')})
            eno = exam_number(row.get('exam_no_raw'))
            r['exam_nos'] = sorted(set(r['exam_nos']) | ({eno} if eno else set()))
    assert raw_image_rows == json.loads((oldbase/'summary.json').read_text())['imaging_input_rows']
    uid_versions = defaultdict(set)
    for r in images.values():
        r['source_refs'].sort(key=lambda ref: json.dumps(ref, sort_keys=True, ensure_ascii=False))
        for ref in r['source_refs']:
            if ref['index_report_uid']:
                uid_versions[(r['patient_uid'], ref['index_report_uid'])].add(r['record_key'])
    for qids in uid_versions.values():
        if len(qids) > 1:
            for qid in qids:
                images[qid]['same_uid_other_content_keys'] = sorted(qids-{qid})
                patients[images[qid]['patient_uid']]['flags']['SAME_REPORT_UID_DIFFERENT_CONTENT_REVIEW'] += 1
    for r in images.values():
        p = patients[r['patient_uid']]
        cv = r['coverage']
        # A traceable image can support relevance without a separate note.
        # Supplement-only excerpts remain available with source-review flags.
        usable = r['has_verified_source'] and r['parsed']['has_text']
        if usable and cv['substantive_research_related']:
            p['usable_related_records'] += 1
        elif cv['target_context'] or cv['substantive_research_related']:
            p['potential_records'] += 1
        for name in ['A', 'B', 'C', 'ASSESSMENT_OR_ACQUISITION_CONTEXT']:
            if cv['target_context'] and cv[name]:
                p['ABC_record_counts'][name] += 1
        if not r['has_verified_source']:
            p['flags']['IMAGE_ORIGINAL_NOT_INDEPENDENTLY_RECONCILED'] += 1
        if r['parsed']['postoperative_clue']:
            p['flags']['POSTOPERATIVE_CONTEXT_CLUE_ONLY'] += 1
        if r['parsed']['difference_flags']:
            p['flags']['DIFFERING_LOCAL_DESCRIPTIONS_REVIEW'] += 1
        if cv['target_context'] and len(set(r['parsed']['represented_vessels']) & {'SMA','CA','CHA','PV','SMV'}) < 5:
            p['flags']['TARGET_IMAGE_WITH_PARTIAL_OR_UNRECOGNIZED_VESSEL_COVERAGE'] += 1
    print(json.dumps({'stage': 'images_parsed', 'input_rows': raw_image_rows, 'content_versions': len(images)}), flush=True)
    clinical_paths = clinical_inputs(manifest, args.prior_run)
    notes, refs = [], []
    decode_errors = []
    title_include = re.compile(rules['clinical_document_pattern'], re.I)
    title_exclude = re.compile(rules['nonphysician_title_pattern'])
    for original in clinical_paths:
        path = legacy_path(original)
        actual = sha(path)
        assert actual == manifest[original]['sha256']
        hashes[original] = actual
        for source_row_number, row in enumerate(pq.read_table(path).to_pylist()):
            uid = row['patient_uid']
            if uid not in patients:
                continue
            p = patients[uid]
            p['raw_clinical_rows'] += 1
            title = str(row['文书名称'] or '')
            # Scan every row for coverage, including excluded document types.
            # Excluded titles are not silently labelled "no clinical record".
            raw = row['文书内容']
            try:
                if isinstance(raw, bytes):
                    try:
                        raw_text = raw.decode('utf-8')
                    except UnicodeDecodeError:
                        raw_text = raw.decode('gb18030')
                else:
                    raw_text = str(raw or '')
            except UnicodeDecodeError:
                decode_errors.append({'patient_uid': uid, 'source': original, 'row': source_row_number})
                p['flags']['CLINICAL_DECODE_ERROR'] += 1
                continue
            # Plain text is an indexing view. Raw bytes/hash/source row remain
            # the authoritative material; no rewritten narrative is generated.
            text = re.sub(r'<[^>]+>', '\n', raw_text)
            cv = coverage(text, rules)
            if not cv['target_context']:
                continue
            known_type = bool(title_include.search(title)) and not title_exclude.search(title)
            locator = {'input_path': original, 'parquet_row_zero_based': source_row_number,
                       'source_record_id': str(row['source_record_id'] or ''),
                       'source_file': row['source_file'], 'source_row': row['source_row']}
            key = digest(uid+'|'+original+'|'+str(source_row_number)+'|'+digest(raw_text))[:24]
            usable = bool(text.strip()) and known_type
            if usable and cv['substantive_research_related']:
                p['usable_related_records'] += 1
            else:
                p['potential_records'] += 1
            if not known_type:
                flag = ('SUPPLEMENTAL_NONPHYSICIAN_OR_CONSENT_RECORD' if title_exclude.search(title)
                        else 'TARGET_NOTE_TYPE_REQUIRES_REVIEW')
                p['flags'][flag] += 1
            if not row['source_record_id']:
                p['flags']['NOTE_NO_LOGICAL_ID_PHYSICAL_ROW_TRACEABLE'] += 1
            if not dateof(row['create_time']):
                p['flags']['NOTE_CREATE_DATE_MISSING'] += 1
            for name in ['A', 'B', 'C', 'ASSESSMENT_OR_ACQUISITION_CONTEXT']:
                if cv[name]:
                    p['ABC_record_counts'][name] += 1
            note = {'record_key': key, 'patient_uid': uid, 'title': title, 'coverage': cv, 'source': locator,
                    'record_type_supported': known_type, 'raw_text_sha256': digest(raw_text),
                    'create_time': str(row['create_time'] or ''), 'admission_time': str(row['admission_time'] or ''),
                    'text_index_view': text}
            notes.append(note)
            if known_type:
                for citation in citations(text):
                    annotation = link(citation, image_by_patient[uid])
                    refs.append({'patient_uid': uid, 'note_key': key, **citation, **annotation})
                    p['flags']['CITATION_'+annotation['match_status']] += 1
    coverage_errors = [uid for uid, p in patients.items() if p['raw_clinical_rows'] != old[uid]['clinical_source_rows']]
    assert not coverage_errors, 'Full-P0 source row coverage must match frozen run'
    for p in patients.values():
        if not any(r['has_verified_source'] and r['coverage']['substantive_research_related'] for r in image_by_patient[p['patient_uid']]):
            p['flags']['NO_VERIFIED_TARGET_RELATED_IMAGE_DETECTED'] += 1
        complete = not any(e['patient_uid'] == p['patient_uid'] for e in decode_errors)
        p['source_coverage_complete'] = complete
        p['state'] = classify(p['identity_index_ok'], p['usable_related_records'], p['potential_records'], complete)
        p['p2_access'] = 'NOT_DETERMINED_BY_P1_STATE'
        p['not_detected_coverage_dimensions'] = [name for name in ['A','B','C'] if not p['ABC_record_counts'][name]]
        p['classification_basis'] = ('TRACEABLE_SUBSTANTIVE_RESEARCH_RECORD_EXISTS' if p['state'] == 'INCLUDE_V3'
                                     else 'IDENTITY_SOURCE_OR_RELEVANCE_UNRESOLVED' if not p['identity_index_ok'] or p['potential_records'] or not complete
                                     else 'NO_SUBSTANTIVE_RELEVANCE_DETECTED_REQUIRES_REVIEW')
    transitions = Counter((p['previous_state'], p['state']) for p in patients.values())
    summary = {'version': rules['version'], 'P0_n': len(patients),
               'patient_states': Counter(p['state'] for p in patients.values()),
               'transitions': [{'before': a, 'after': b, 'n': n} for (a,b),n in sorted(transitions.items())],
               'clinical_rows': sum(p['raw_clinical_rows'] for p in patients.values()),
               'image_source_rows': raw_image_rows, 'image_content_versions': len(images),
               'target_context_notes': len(notes), 'citation_statuses': Counter(r['match_status'] for r in refs),
               'patient_flag_counts': Counter(flag for p in patients.values() for flag in p['flags']),
               'coverage_patients_all_history_not_same_time': {name: sum(bool(p['ABC_record_counts'][name]) for p in patients.values()) for name in ['A','B','C','ASSESSMENT_OR_ACQUISITION_CONTEXT']},
               'clinical_decode_errors': len(decode_errors), 'source_row_coverage_matches_frozen': True,
               'p2_gate': False, 'clinical_validation_completed': False,
               'proof_policy': 'Inherited v0.2 original-source proof on SHA-identical indexed inputs; raw clusters not newly reconciled.',
               'elapsed_seconds': round(time.monotonic()-start, 2)}
    assert sum(summary['patient_states'].values()) == rules['P0_n']
    assert code_hashes_at_start == {p.name:sha(p) for p in sorted(here.glob('*.py'))}, 'Code changed during run; rerun before release'
    assert rules_hash_at_start == sha(here/'rules.json'), 'Rules changed during run'
    ordered = sorted(patients.values(), key=lambda p: p['alias'])
    write_jsonl(output/'patient_quality.jsonl', ordered)
    write_jsonl(output/'image_annotations.jsonl', sorted(images.values(), key=lambda r:r['record_key']))
    write_jsonl(output/'clinical_annotations.jsonl', notes)
    write_jsonl(output/'reference_links.jsonl', refs)
    write_json(output/'decode_errors.json', decode_errors)
    # Deterministic development sample spans restored, unresolved and retained.
    rng = random.Random(20261021)
    sampling = []
    for transition in sorted(transitions):
        pool = [p for p in ordered if (p['previous_state'],p['state']) == transition]
        for p in rng.sample(pool, min(8,len(pool))):
            sampling.append({'patient_uid': p['patient_uid'], 'alias': p['alias'], 'stratum': list(transition)})
    write_json(output/'development_sample.json', sampling)
    write_json(output/'summary.json', summary)
    write_json(output/'run_manifest.json', {'version': rules['version'], 'python': sys.version, 'pyarrow': pyarrow.__version__,
               'input_hashes': hashes, 'frozen_patient_sha256': sha(oldbase/'patient_quality.jsonl'),
               'prior_run_order_reference': str(args.prior_run) if args.prior_run else None,
               'frozen_report_sha256': sha(oldbase/'report_quality.jsonl'),
               'script_hashes': code_hashes_at_start, 'rules_sha256': rules_hash_at_start,
               'original_data_modified': False, 'labels_are_GT': False, 'sample_purpose': 'development, not independent validation'})
    public_summary = repo/'docs'/(rules['public_release_stem']+'.json')
    if not args.no_public_release:
        if public_summary.exists():
            raise FileExistsError('P1_PUBLIC_SUMMARY_REQUIRES_NEW_VERSION: '+str(public_summary))
        write_json(public_summary, summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
