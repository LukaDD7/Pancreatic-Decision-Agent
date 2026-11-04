"""Build a researcher-only offline assignment, never a doctor blind-review file."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from scripts.cohort_construction.p2_checkpoint_packet.compiler import (
    ContractError, _verified_sources, canonical_sha256, text_sha256,
)
from scripts.cohort_construction.p2_checkpoint_packet.semantic_audit import validate_semantic_audit

FORMAT = 'checkpoint-researcher-material-assignment-0.1.0'
ASSETS = Path(__file__).with_name('assets')
PRIVATE_ROOT = (Path(__file__).resolve().parents[3] / 'private').resolve()
REQUIRED_DOCUMENTS = {'compiled', 'manifest', 'draft', 'preparation_audit'}


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def parse_json_unique(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ContractError('MRA_DUPLICATE_JSON_KEY')
            value[key] = item
        return value
    def invalid(value):
        raise ContractError('MRA_NONFINITE_JSON')
    parsed = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    # Reject lone surrogates and nonfinite overflow before accepting any object.
    text_sha256(canonical_json(parsed))
    return parsed


def _nonempty(value, code):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(code)


def build_assignment(compiled, manifest, draft, preparation_audit, *, assignment_id,
                     selected_event=None, coverage_sources=(), source_fhir_sha256=None):
    """Bind exact input objects; no judgement or existing file is modified.

    Each document is embedded as its canonical JSON *string*. Hashing that
    string in a browser preserves Python numeric lexemes in legacy LLM outputs.
    It does not ask JavaScript to reserialize clinical/model input numbers.
    """
    _nonempty(assignment_id, 'MRA_ASSIGNMENT_ID_REQUIRED')
    documents = dict(compiled=compiled, manifest=manifest, draft=draft,
                     preparation_audit=preparation_audit)
    if selected_event is not None:
        documents['selected_event'] = selected_event
    loaded = []
    seen = set()
    for source in coverage_sources:
        sid = source['source_id']
        if sid in seen:
            raise ContractError('MRA_DUPLICATE_COVERAGE_SOURCE')
        seen.add(sid)
        text = source['text']
        digest = source.get('source_sha256', source.get('sha256'))
        if digest != text_sha256(text):
            raise ContractError('MRA_COVERAGE_SOURCE_HASH_MISMATCH')
        loaded.append(dict(source_id=sid, text=text, sha256=digest,
                           title=source.get('title', ''), kind=source.get('kind', ''),
                           clocks=deepcopy(source.get('clocks', {})),
                           clock_basis=source.get('clock_basis', 'unknown')))
    result = dict(format=FORMAT, assignment_id=assignment_id, researcher_only=True,
                  doctor_packet_created=False,
                  documents={name: canonical_json(value) for name, value in documents.items()},
                  bindings={name: canonical_sha256(value) for name, value in documents.items()},
                  coverage_sources_json=canonical_json(loaded),
                  coverage_sources_sha256=canonical_sha256(loaded),
                  source_fhir_sha256=source_fhir_sha256)
    result['assignment_sha256'] = canonical_sha256(result)
    validate_assignment(result)
    return result


def validate_assignment(assignment):
    """Verify the assignment before rendering or processing a response."""
    fields = {'format', 'assignment_id', 'researcher_only', 'doctor_packet_created',
              'documents', 'bindings', 'coverage_sources_json', 'coverage_sources_sha256',
              'source_fhir_sha256', 'assignment_sha256'}
    if not isinstance(assignment, dict) or set(assignment) != fields:
        raise ContractError('MRA_ASSIGNMENT_FIELDS_MISMATCH')
    if (assignment['format'] != FORMAT or assignment['researcher_only'] is not True
            or assignment['doctor_packet_created'] is not False):
        raise ContractError('MRA_RESEARCHER_ASSIGNMENT_REQUIRED')
    _nonempty(assignment['assignment_id'], 'MRA_ASSIGNMENT_ID_REQUIRED')
    if canonical_sha256({k: v for k, v in assignment.items() if k != 'assignment_sha256'}) != assignment['assignment_sha256']:
        raise ContractError('MRA_ASSIGNMENT_HASH_MISMATCH')
    document_names = set(assignment['documents'])
    if not REQUIRED_DOCUMENTS <= document_names or document_names - REQUIRED_DOCUMENTS - {'selected_event'}:
        raise ContractError('MRA_REQUIRED_DOCUMENTS_MISMATCH')
    if set(assignment['bindings']) != document_names:
        raise ContractError('MRA_INPUT_BINDINGS_MISMATCH')
    documents = {}
    for name, raw in assignment['documents'].items():
        if text_sha256(raw) != assignment['bindings'][name]:
            raise ContractError('MRA_DOCUMENT_HASH_MISMATCH')
        documents[name] = parse_json_unique(raw)
        if canonical_json(documents[name]) != raw:
            raise ContractError('MRA_CANONICAL_DOCUMENT_REQUIRED')
    compiled, manifest, draft, preparation = (documents[k] for k in ('compiled', 'manifest', 'draft', 'preparation_audit'))
    source_map = _verified_sources(manifest)
    validate_semantic_audit(compiled, draft)
    if set(source_map) != {source['source_id'] for source in compiled['sources']}:
        raise ContractError('MRA_MANIFEST_SOURCE_SET_MISMATCH')
    for source in compiled['sources']:
        original = source_map[source['source_id']]
        if (source['source_sha256'] != original['sha256'] or source['source_version'] != original['source_version']
                or ''.join(span['text'] for span in source['segments']) != original['text']):
            raise ContractError('MRA_MANIFEST_COMPILED_BINDING_MISMATCH')
    if preparation.get('compiled_sha256') != compiled['compiled_sha256']:
        raise ContractError('MRA_PREPARATION_COMPILED_MISMATCH')
    routing = preparation.get('source_routing', [])
    included = {item['source_id'] for item in routing if item['disposition'] == 'INCLUDED_FULL_ORIGINAL_TEXT'}
    excluded = {item['source_id'] for item in routing if item['disposition'] == 'EXCLUDED_OUTSIDE_EVENT_SCOPE'}
    if included != set(source_map) or excluded != set(draft['review_scope']['excluded_source_ids']):
        raise ContractError('MRA_PREPARATION_SCOPE_MISMATCH')
    if 'selected_event' in documents and preparation.get('event_sha256') != canonical_sha256(documents['selected_event']):
        raise ContractError('MRA_SELECTED_EVENT_BINDING_MISMATCH')
    raw = assignment['coverage_sources_json']
    if text_sha256(raw) != assignment['coverage_sources_sha256']:
        raise ContractError('MRA_COVERAGE_SOURCE_SET_HASH_MISMATCH')
    coverage = parse_json_unique(raw)
    if not isinstance(coverage, list) or canonical_json(coverage) != raw:
        raise ContractError('MRA_CANONICAL_COVERAGE_SOURCES_REQUIRED')
    loaded = {}
    for source in coverage:
        sid = source['source_id']
        if sid in loaded or sid not in included | excluded:
            raise ContractError('MRA_COVERAGE_SOURCE_SCOPE_MISMATCH')
        if source['sha256'] != text_sha256(source['text']):
            raise ContractError('MRA_COVERAGE_SOURCE_HASH_MISMATCH')
        if sid in source_map and source['text'] != source_map[sid]['text']:
            raise ContractError('MRA_COVERAGE_MANIFEST_TEXT_MISMATCH')
        loaded[sid] = source
    # Included manifest text is always available; outside-event directory IDs
    # become readable sources only when their original text was explicitly loaded.
    all_sources = dict(loaded)
    all_sources.update({sid: dict(source) for sid, source in source_map.items()})
    return dict(documents=documents, loaded_sources=all_sources,
                unloaded_source_ids=sorted((included | excluded) - set(all_sources)))


def build_researcher_html(assignment):
    validate_assignment(assignment)
    embedded = canonical_json(assignment)
    for char, escaped in [('&', '\\u0026'), ('<', '\\u003c'), ('>', '\\u003e'), ('\u2028', '\\u2028'), ('\u2029', '\\u2029')]:
        embedded = embedded.replace(char, escaped)
    html = (ASSETS / 'reviewer.html').read_text(encoding='utf-8')
    html = html.replace('/*__MRA_CSS__*/', (ASSETS / 'reviewer.css').read_text(encoding='utf-8'))
    html = html.replace('/*__MRA_JS__*/', (ASSETS / 'reviewer.js').read_text(encoding='utf-8'))
    return html.replace('__MRA_ASSIGNED_DATA__', embedded)


def private_mkdir(path, *, parents=False, exist_ok=False):
    """Create owner-only directories, including ancestors on relocated storage."""
    path = Path(path)
    if parents and not path.exists():
        private_mkdir(path.parent, parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=exist_ok)
    if path.stat().st_mode & 0o077:
        raise ContractError('MRA_PRIVATE_DIRECTORY_PERMISSIONS_TOO_BROAD')


def private_open(path, mode):
    """Exclusively create owner-only artifacts, independent of process umask."""
    path = Path(path)
    if 'x' not in mode:
        raise ValueError('PRIVATE_ARTIFACT_REQUIRES_EXCLUSIVE_CREATE')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, mode, encoding=None if 'b' in mode else 'utf-8',
                     newline='' if 'b' not in mode else None)


def require_private_output(path, *, private_root=None):
    path = Path(path).resolve()
    # Callers with a project-managed historical symlink explicitly bind the
    # physical migrated parent. Do not infer trust from a path named private.
    root = PRIVATE_ROOT if private_root is None else Path(private_root)
    if not root.is_dir() or not root.resolve().is_dir():
        raise ContractError('MRA_REAL_ASSIGNMENT_OUTPUT_MUST_BE_PRIVATE')
    # The private root may predate this patch; new directories and files have
    # separate owner-only enforcement in private_mkdir/private_open.
    if not path.is_relative_to(root.resolve()):
        raise ContractError('MRA_REAL_ASSIGNMENT_OUTPUT_MUST_BE_PRIVATE')
    return path


def build_files(assignment_dir, *, output, assignment_id, source_fhir=None):
    """Read an existing task and optionally load verified excluded originals.

    The explicit FHIR file must match the preparation input receipt byte hash.
    Outputs use exclusive creation inside a new private directory.
    """
    directory = Path(assignment_dir)
    names = dict(compiled='compiled_partition.json', manifest='source_manifest.json',
                 draft='semantic-audit.draft.json', preparation_audit='researcher_audit.json')
    documents = {name: parse_json_unique((directory / filename).read_text(encoding='utf-8'))
                 for name, filename in names.items()}
    selected_event_path = directory / 'selected_reviewed_event.json'
    if selected_event_path.exists():
        documents['selected_event'] = parse_json_unique(selected_event_path.read_text(encoding='utf-8'))
    loaded, fhir_hash = [], None
    if source_fhir is not None:
        from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources
        receipt = parse_json_unique((directory / 'input_receipt.json').read_text(encoding='utf-8'))
        raw = Path(source_fhir).read_bytes()
        fhir_hash = hashlib.sha256(raw).hexdigest()
        if fhir_hash != receipt.get('source_fhir_input_sha256'):
            raise ContractError('MRA_SOURCE_FHIR_INPUT_HASH_MISMATCH')
        loaded, _ = import_sources(parse_json_unique(raw.decode('utf-8')))
    assignment = build_assignment(**documents, assignment_id=assignment_id,
                                  coverage_sources=loaded, source_fhir_sha256=fhir_hash)
    html = build_researcher_html(assignment)
    output = require_private_output(output)
    private_mkdir(output, parents=True)
    with private_open(output / 'researcher-assignment.json', 'x') as stream:
        stream.write(json.dumps(assignment, ensure_ascii=False, indent=2) + '\n')
    with private_open(output / 'researcher-review.html', 'x') as stream:
        stream.write(html)
    verified = validate_assignment(assignment)
    result = dict(status='CREATED_RESEARCHER_ASSIGNMENT_PENDING_HUMAN_REVIEW',
                  assignment_id=assignment_id, assignment_sha256=assignment['assignment_sha256'],
                  output=str(output), compiled_segment_count=len(documents['draft']['segment_assessments']),
                  loaded_source_count=len(verified['loaded_sources']),
                  unloaded_source_count=len(verified['unloaded_source_ids']),
                  researcher_only=True, doctor_packet_created=False, clinical_reference_created=False,
                  model_api_calls=0, independence_authenticated=False,
                  input_sha256={filename: hashlib.sha256((directory / filename).read_bytes()).hexdigest()
                                for filename in names.values()}, source_fhir_sha256=fhir_hash)
    with private_open(output / 'build-receipt.json', 'x') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assignment-dir', type=Path, required=True)
    parser.add_argument('--assignment-id', required=True)
    parser.add_argument('--source-fhir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_files(args.assignment_dir, output=args.output,
                         assignment_id=args.assignment_id, source_fhir=args.source_fhir)
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    main()
