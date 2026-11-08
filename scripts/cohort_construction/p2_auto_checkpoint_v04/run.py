"""Run the unchanged fixed-ten source cohort with v0.4 document semantics.

This module performs offline preflight by default only with ``--dry-run``.
Live source discovery/review requires an exact external authorization. Resume
re-verifies the frozen contract and per-case evidence; directory existence is
never completion evidence. No function in this module authorizes a provider.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path

from scripts.cohort_construction.p2_auto_checkpoint.extract import read, write, digest
from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources, build, validate
from .context import build_units, context_for_discovery
from .engine import process_sources
from .review_feedback import validate_feedback_config

ROOT = Path(__file__).resolve().parents[3]
FIXED_CASE_IDS = ['SCALE-%02d' % number for number in range(1, 11)]
EXECUTION_SCOPE = 'SOURCE_DISCOVERY_REVIEW_FEEDBACK_FINAL_GRAPH_EPISODES_AND_FHIR'
AUTHORIZATION_FORMAT = 'p2-complete-fixed-ten-execution-authorization-0.1.0'
CONTRACT_FORMAT = 'p2-complete-fixed-ten-run-contract-0.1.0'
CASE_RECEIPT_FORMAT = 'p2-complete-fixed-ten-case-completion-0.1.0'
SUMMARY_FORMAT = 'complete-fixed-ten-execution-summary-0.1.0'
RECEIPT_NAME = 'case_completion_receipt.json'
AUTH_REQUIRED_FIELDS = {
    'format': AUTHORIZATION_FORMAT,
    'remote_execution_authorized': True,
    'execution_scope': EXECUTION_SCOPE,
    'source_fhir_discovery_and_review_context_authorized': True,
    'doctor_packet_release': False,
    'real_doctor_assignment_creation': False,
    'clinical_release_permitted': False,
}
AUTH_BINDING_FIELDS = (
    'manifest_sha256', 'config_sha256', 'schema_sha256',
    'source_input_sha256', 'code_sha256',
)
CASE_OUTPUTS = (
    'source_import_receipt.json', 'source_fhir_r4_bundle.json',
    'decision_events.json', 'episodes.json', 'relations.json', 'failures.json',
    'model_receipts.json', 'research_fhir_r4_bundle.json', 'summary.json',
)


def _private_mkdir(path, *, parents=False, exist_ok=False):
    """Create owner-only directories; never replace a prior case/run."""
    path = Path(path)
    if parents and not path.exists():
        _private_mkdir(path.parent, parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=exist_ok)
    if path.stat().st_mode & 0o077:
        raise ValueError('PRIVATE_DIRECTORY_PERMISSIONS_TOO_BROAD:' + str(path))


def _private_write_new(path, value):
    """Exclusively persist JSON with owner-only permissions."""
    path = Path(path)
    _private_mkdir(path.parent, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _write_new_json(path, value, *, private=True):
    if private:
        return _private_write_new(path, value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _atomic_write_json(path, value, *, private=False):
    """Replace only this process's known mutable summary, atomically."""
    path = Path(path)
    if private:
        _private_mkdir(path.parent, parents=True, exist_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp-%d' % os.getpid())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600 if private else 0o666)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


@contextmanager
def _private_writes(enabled):
    """Keep legacy pipeline writers compatible while protecting new output."""
    if not enabled:
        yield
        return
    old_umask = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old_umask)


def _verified_file(path, expected_sha256, label):
    path = Path(path)
    if not path.is_file():
        raise ValueError('CASE_EVIDENCE_MISSING:' + label)
    actual = file_hash(path)
    if expected_sha256 != actual:
        raise ValueError('CASE_EVIDENCE_HASH_MISMATCH:' + label)
    return actual


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _count(value, field):
    number = value.get(field, 0)
    if type(number) is not int or number < 0:
        raise ValueError('CASE_SUMMARY_COUNT_INVALID:' + field)
    return number


def _validate_complete_summary(summary, case_id, config):
    if not isinstance(summary, dict) or summary.get('case_id') != case_id:
        raise ValueError('CASE_SUMMARY_IDENTITY_INVALID:' + case_id)
    if summary.get('status') == 'FAILED' or summary.get('failures') != 0:
        raise ValueError('CASE_SUMMARY_NOT_COMPLETE:' + case_id)
    if summary.get('version') != config.get('version'):
        raise ValueError('CASE_SUMMARY_CONTRACT_MISMATCH:' + case_id)
    if summary.get('fhir_validation', {}).get('official_r4_json_schema') is not True:
        raise ValueError('CASE_FHIR_VALIDATION_NOT_COMPLETE:' + case_id)
    counts = {field: _count(summary, field) for field in (
        'discovered_events', 'reviewed_events', 'candidate_checkpoints',
        'candidate_episodes', 'process_links')}
    return counts


def _load_receipts(cache):
    """Measure saved provider receipts, including failures and partial runs."""
    cache = Path(cache)
    receipts = []
    for path in sorted(cache.glob('invocations/*.json')):
        try:
            value = read(path)
        except Exception as exc:
            raise ValueError('INVOCATION_RECEIPT_INVALID:' + path.name) from exc
        if not isinstance(value, dict):
            raise ValueError('INVOCATION_RECEIPT_INVALID:' + path.name)
        receipts.append(value)
    return receipts


def _receipt_totals(receipts):
    fresh = 0
    attempts = 0
    statuses = Counter()
    actual = 0
    verified = 0
    for receipt in receipts:
        cached = receipt.get('cached')
        if type(cached) is not bool:
            raise ValueError('INVOCATION_CACHE_FLAG_INVALID')
        fresh += not cached
        rows = receipt.get('attempts', [])
        if not isinstance(rows, list):
            raise ValueError('INVOCATION_ATTEMPTS_INVALID')
        attempts += len(rows)
        status = receipt.get('status')
        statuses[str(status)] += 1
        if receipt.get('provider_invocation_occurred') is True:
            actual += 1
        if receipt.get('provider_transport_verified') is True:
            verified += 1
    return dict(invocation_count=len(receipts), fresh_invocation_count=fresh,
                http_attempt_count=attempts, invocation_statuses=dict(sorted(statuses.items())),
                actual_provider_invocations=actual,
                verified_provider_transports=verified)


def execution_summary(run_output, cache, *, model_execution_requested=None,
                      resumed_execution=None):
    """Derive the final measured summary from saved case/output/receipt files.

    Missing and failed cases remain explicit. Any malformed saved evidence
    fails closed instead of producing a KeyError or a technically complete run.
    Optional request-mode values describe this invocation, not provider calls;
    they never alter the persisted frozen contract.
    """
    run_output, cache = Path(run_output), Path(cache)
    contract_path = run_output / 'run_contract.json'
    contract = read(contract_path) if contract_path.is_file() else {}
    if not isinstance(contract, dict):
        raise ValueError('RUN_CONTRACT_INVALID')
    case_ids = contract.get('case_ids', [])
    if not isinstance(case_ids, list) or not case_ids \
            or len(set(case_ids)) != len(case_ids) \
            or any(not isinstance(item, str) for item in case_ids):
        raise ValueError('RUN_CONTRACT_CASE_IDS_INVALID')
    config = contract.get('config', {})
    totals = Counter()
    complete_receipts = []
    failures = []
    run_failure_path = run_output / 'failure.json'
    if run_failure_path.is_file():
        failures.append(read(run_failure_path))
    for case_id in sorted(case_ids):
        case_dir = run_output / case_id
        failure_path = case_dir / 'failure.json'
        receipt_path = case_dir / RECEIPT_NAME
        if failure_path.is_file():
            failures.append(read(failure_path))
        if not receipt_path.is_file():
            continue
        receipt = read(receipt_path)
        if not isinstance(receipt, dict) or receipt.get('format') != CASE_RECEIPT_FORMAT:
            raise ValueError('CASE_COMPLETION_RECEIPT_INVALID:' + case_id)
        if receipt.get('case_id') != case_id:
            raise ValueError('CASE_COMPLETION_RECEIPT_IDENTITY_INVALID:' + case_id)
        bindings = receipt.get('output_sha256')
        if not isinstance(bindings, dict) or set(bindings) != set(CASE_OUTPUTS):
            raise ValueError('CASE_COMPLETION_OUTPUT_BINDINGS_INVALID:' + case_id)
        for name, expected in bindings.items():
            _verified_file(case_dir / name, expected, case_id + '/' + name)
        summary = read(case_dir / 'summary.json')
        counts = _validate_complete_summary(summary, case_id, config)
        case_contract = read(case_dir / 'case_contract.json')
        if receipt.get('case_contract_sha256') != digest(case_contract) \
                or case_contract.get('case_id') != case_id \
                or case_contract.get('run_contract_sha256') != file_hash(contract_path):
            raise ValueError('CASE_COMPLETION_CONTRACT_BINDING_MISMATCH:' + case_id)
        totals.update(counts)
        complete_receipts.append(case_id)
    result = dict(format=SUMMARY_FORMAT, selected_case_ids=sorted(case_ids),
        selected_case_count=len(case_ids), fixed_ten_case_ids=FIXED_CASE_IDS,
        all_fixed_ten_selected=set(case_ids) == set(FIXED_CASE_IDS),
        case_count=len(complete_receipts), case_ids=sorted(complete_receipts),
        complete_case_receipt_ids=sorted(complete_receipts),
        incomplete_case_ids=sorted(set(case_ids) - set(complete_receipts)),
        failures=failures, failure_count=len(failures),
        model_execution_requested=(contract.get('model_execution_requested',
            contract.get('execution_mode') in ('LIVE', 'RESUME'))
            if model_execution_requested is None else model_execution_requested),
        resumed_execution=(contract.get('execution_mode') == 'RESUME'
            if resumed_execution is None else resumed_execution),)
    for field in ('discovered_events', 'reviewed_events', 'candidate_checkpoints',
                  'candidate_episodes', 'process_links'):
        result[field] = totals[field]
    result['technically_complete'] = (set(case_ids) == set(FIXED_CASE_IDS)
        and set(complete_receipts) == set(FIXED_CASE_IDS) and not failures)
    result['selected_subset_technically_complete'] = (
        set(complete_receipts) == set(case_ids) and not failures)
    receipt_totals = _receipt_totals(_load_receipts(cache))
    if receipt_totals['http_attempt_count'] \
            > contract.get('offline_preflight', {}).get('maximum_http_attempts', -1) \
            or receipt_totals['fresh_invocation_count'] \
            > contract.get('offline_preflight', {}).get('maximum_logical_provider_requests', -1):
        raise ValueError('MEASURED_PROVIDER_BUDGET_EXCEEDS_FROZEN_MAXIMUM')
    result.update(receipt_totals)
    result['cached_result_file_count'] = len(list(cache.glob('*.json')))
    failures.extend({'cache_receipt': path.name, 'status': 'FAILED'}
        for path in sorted(cache.glob('invocations/*.json'))
        if read(path).get('status') != 'SUCCESS')
    result['failure_count'] = len(failures)
    if failures:
        result['technically_complete'] = False
        result['selected_subset_technically_complete'] = False
    result.update(clinical_reference_created=False, clinical_accuracy_measured=False,
        reference_read_by_pipeline=False, doctor_assignment_created=False,
        doctor_packet_release=False, clinical_release_permitted=False)
    return result


def _ensure_case_outputs(case_dir, names):
    missing = [name for name in names if not (case_dir / name).is_file()]
    if missing:
        raise ValueError('CASE_OUTPUT_MISSING:' + ','.join(missing))


def run_case(case_id, args, config):
    """Run one case and finish with an exclusive, hash-bound completion receipt."""
    path = args.sources / case_id / 'source_fhir_r4_bundle.json'
    if not path.is_file():
        raise ValueError('FIXED_CASE_INPUT_REQUIRED:' + case_id)
    source_input = read(path)
    sources, receipt = import_sources(source_input)
    out = args.output / case_id
    _private_mkdir(out, parents=True, exist_ok=False) if args.private_root \
        else out.mkdir(parents=True, exist_ok=False)
    with _private_writes(args.private_root is not None):
        write(out / 'source_import_receipt.json', dict(input_path=str(path),
            sha256=file_hash(path), **receipt))
        # Source FHIR Basic research annotations are never semantic input.
        source_bundle = build(sources, case_id, [], [])
        source_validation = validate(source_bundle, args.schema)
        write(out / 'source_fhir_r4_bundle.json', source_bundle)
        events, episodes, summary = process_sources(case_id, sources, config, out,
            args.cache, args.unit_workers)
        _ensure_case_outputs(out, CASE_OUTPUTS[:-2])
        annotations = []
        for event in events:
            small = {key: value for key, value in event.items()
                     if key not in ('evidence_context', 'actual_model_evidence')}
            small['evidence_context_audit_file'] = str(out / 'decision_events.json')
            small['semantic'] = dict(supports=event['action_citations']['valid'])
            small['evolution'] = dict(
                supports=event.get('validated_observation_citations', {}).get('valid', []),
                connection_supports=event.get('validated_connection_citations', {}).get('valid', []))
            annotations.append(small)
        result = build(sources, case_id, annotations, episodes)
        summary['fhir_validation'] = validate(result, args.schema)
        summary['source_fhir_validation'] = source_validation
        write(out / 'research_fhir_r4_bundle.json', result)
        _validate_complete_summary(summary, case_id, config)
        write(out / 'summary.json', summary)
    case_contract = dict(
        run_contract_sha256=file_hash(args.output / 'run_contract.json'),
        source_input_sha256=file_hash(path), config_sha256=file_hash(args.config),
        schema_sha256=file_hash(args.schema), case_id=case_id,
        execution_batch_id=config.get('execution_batch_id'),
        semantic_model=config.get('semantic_model'))
    _write_new_json(out / 'case_contract.json', case_contract,
                    private=args.private_root is not None)
    output_hashes = {name: file_hash(out / name) for name in CASE_OUTPUTS}
    completion = dict(format=CASE_RECEIPT_FORMAT, case_id=case_id,
        case_contract_sha256=digest(case_contract), output_sha256=output_hashes,
        technically_complete=False, case_technically_complete=True,
        clinical_reference_created=False, doctor_packet_release=False,
        clinical_release_permitted=False)
    _write_new_json(out / RECEIPT_NAME, completion,
                    private=args.private_root is not None)
    return summary


def _validate_manifest(manifest, requested_cases):
    selected = manifest.get('selected') if isinstance(manifest, dict) else None
    if manifest.get('seed') != 20261023 or not isinstance(selected, list) or len(selected) != 10:
        raise ValueError('FIXED_TEN_MANIFEST_REQUIRED')
    selected_ids = [item.get('case_id') if isinstance(item, dict) else None for item in selected]
    if sorted(selected_ids) != FIXED_CASE_IDS or len(set(selected_ids)) != 10:
        raise ValueError('FIXED_TEN_IDS_CHANGED')
    case_ids = [case_id for case_id in selected_ids
                if not requested_cases or case_id in requested_cases]
    if not case_ids or len(set(case_ids)) != len(case_ids) \
            or requested_cases and set(requested_cases) - set(selected_ids):
        raise ValueError('FIXED_TEN_CASE_SUBSET_INVALID')
    return case_ids


def _code_hashes():
    module_dir = Path(__file__).parent
    dependencies = [module_dir.parent / 'p2_auto_checkpoint_v05_refine' / 'refine.py',
        module_dir.parent / 'p2_auto_checkpoint' / 'fhir_io.py',
        module_dir.parent / 'p2_auto_checkpoint' / 'extract.py',
        module_dir.parent / 'p2_auto_checkpoint' / 'semantic.py']
    paths = sorted(module_dir.glob('*.py')) + dependencies
    for path in paths:
        if not path.is_file():
            raise ValueError('FROZEN_CODE_DEPENDENCY_MISSING:' + str(path))
    return {str(path.relative_to(ROOT)): file_hash(path) for path in paths}


def _preflight(args, config, case_ids):
    """Read all source text for whole-unit budgeting; never make a Client."""
    validate_feedback_config(config)
    attempts = config.get('max_http_attempts_per_request', 3)
    if type(attempts) is not int or not 1 <= attempts <= 100:
        raise ValueError('MODEL_HTTP_ATTEMPTS_INVALID')
    for field in ('discovery_context_characters', 'review_context_characters',
                  'max_review_payload_characters', 'max_output_tokens'):
        if type(config.get(field)) is not int or config[field] <= 0:
            raise ValueError('COMPLETE_CHAIN_CONFIG_INVALID:' + field)
    if not isinstance(config.get('semantic_model'), str) or not config['semantic_model']:
        raise ValueError('SEMANTIC_MODEL_REQUIRED')
    if not isinstance(config.get('execution_batch_id'), str) or not config['execution_batch_id'] \
            or len(config['execution_batch_id']) > 200:
        raise ValueError('EXECUTION_BATCH_ID_REQUIRED')
    if config.get('clinical_reference') is not False \
            or config.get('reference_files_permitted_as_pipeline_input') is not False:
        raise ValueError('NO_REFERENCE_CONFIGURATION_REQUIRED')
    for field in ('doctor_packet_release', 'real_doctor_assignment_creation',
                  'clinical_release_permitted'):
        if config.get(field, False) is not False:
            raise ValueError('NO_CLINICAL_RELEASE_CONFIGURATION_REQUIRED')
    units = {}
    source_hashes = {}
    for case_id in case_ids:
        path = args.sources / case_id / 'source_fhir_r4_bundle.json'
        if not path.is_file():
            raise ValueError('FIXED_CASE_INPUT_REQUIRED:' + case_id)
        source_hashes[case_id] = file_hash(path)
        sources, _ = import_sources(read(path))
        routing = [] if config.get('include_support_documents', False) else None
        case_units = build_units(sources, config, routing_audit=routing) \
            if routing is not None else build_units(sources, config)
        contexts = [context_for_discovery(unit, sources, config['discovery_context_characters'])
                    for unit in case_units]
        overbudget = [unit['unit_id'] for unit, context in zip(case_units, contexts)
                      if context['budget']['current_unit_over_budget']]
        if overbudget and not config.get('allow_overbudget_current_unit', False):
            raise ValueError('CURRENT_UNIT_BUDGET_PREFLIGHT_HOLD:' + case_id)
        feedback_discovery = case_units if config.get('discovery_feedback_max_rounds') else []
        feedback_review = case_units if config.get('review_feedback_max_rounds') else []
        units[case_id] = dict(unit_count=len(case_units),
            unit_ids=[unit['unit_id'] for unit in case_units],
            current_unit_over_budget_ids=overbudget, source_count=len(sources),
            source_text_characters=sum(len(source['text']) for source in sources),
            max_discovery_requests=len(case_units),
            max_conditional_discovery_feedback_requests=len(feedback_discovery),
            max_review_requests=len(case_units),
            max_conditional_review_feedback_requests=len(feedback_review),
            provider_payload_projection_is_bound_ceiling_not_case_replacement=True,
            complete_documents_or_same_examination_groups=True,
            silent_source_truncation=False)
    request_keys = ('max_discovery_requests', 'max_conditional_discovery_feedback_requests',
                    'max_review_requests', 'max_conditional_review_feedback_requests')
    maximum_requests = sum(sum(value[key] for key in request_keys) for value in units.values())
    if maximum_requests < 1:
        raise ValueError('COMPLETE_CHAIN_NONZERO_BUDGET_REQUIRED')
    maximum_attempts = maximum_requests * attempts
    if any(key in config for key in ('max_total_model_requests', 'max_total_http_attempts')):
        if config.get('max_total_model_requests') != maximum_requests \
                or config.get('max_total_http_attempts') != maximum_attempts:
            raise ValueError('COMPLETE_CHAIN_BUDGET_MUST_BIND_DETERMINISTIC_MAXIMUM')
    return dict(cases=units, maximum_logical_provider_requests=maximum_requests,
        maximum_http_attempts=maximum_attempts, max_http_attempts_per_request=attempts,
        conditional_feedback_actually_called_only_on_mechanical_findings=True,
        no_case_replacement=True, output_and_cache_required_new=True,
        source_input_sha256=source_hashes)


def _authorization_and_execution_config(config, preflight):
    return dict(config, max_total_model_requests=preflight['maximum_logical_provider_requests'],
        max_total_http_attempts=preflight['maximum_http_attempts'])


def _frozen_contract(args, config, case_ids, preflight):
    return dict(format=CONTRACT_FORMAT, version=config['version'], config=config,
        case_ids=case_ids, seed=20261023, manifest_sha256=file_hash(args.manifest),
        config_sha256=file_hash(args.config), schema_sha256=file_hash(args.schema),
        source_input_sha256=preflight['source_input_sha256'], code_sha256=_code_hashes(),
        semantic_model=config['semantic_model'], execution_batch_id=config['execution_batch_id'],
        execution_scope=EXECUTION_SCOPE, full_chain_scope=EXECUTION_SCOPE,
        annotation_references_read=False, prior_algorithm_outputs_read=False,
        source_fhir_annotations_ignored=True, clinical_validation=False,
        clinical_reference_created=False, reference_read_by_pipeline=False,
        model_execution_requested=not args.dry_run,
        execution_mode='RESUME' if args.resume else ('DRY_RUN' if args.dry_run else 'LIVE'),
        provider_client_constructed=False,
        frozen_graph_downstream_batch=False, doctor_packet_release=False,
        real_doctor_assignment_creation=False, clinical_release_permitted=False,
        offline_preflight=preflight)


def _validate_authorization(authorization, contract, config, preflight):
    if not isinstance(authorization, dict):
        raise ValueError('EXECUTION_AUTHORIZATION_INVALID')
    for field, expected in AUTH_REQUIRED_FIELDS.items():
        if authorization.get(field) is not expected and authorization.get(field) != expected:
            raise ValueError('EXECUTION_AUTHORIZATION_GOVERNANCE_MISMATCH:' + field)
        if type(authorization.get(field)) is not type(expected):
            raise ValueError('EXECUTION_AUTHORIZATION_TYPE_MISMATCH:' + field)
    for field in AUTH_BINDING_FIELDS:
        if authorization.get(field) != contract[field]:
            raise ValueError('EXECUTION_AUTHORIZATION_BINDING_MISMATCH:' + field)
    for field, expected in (
            ('execution_batch_id', config['execution_batch_id']),
            ('semantic_model', config['semantic_model']),
            ('case_ids', contract['case_ids']),
            ('max_total_model_requests', preflight['maximum_logical_provider_requests']),
            ('max_total_http_attempts', preflight['maximum_http_attempts']),
            ('max_http_attempts_per_request', config.get('max_http_attempts_per_request', 3))):
        if authorization.get(field) != expected:
            raise ValueError('EXECUTION_AUTHORIZATION_LIMIT_MISMATCH:' + field)


def _validate_resume(args, frozen, case_ids, preflight):
    if args.execution_authorization:
        raise ValueError('RESUME_AND_NEW_AUTHORIZATION_ARE_EXCLUSIVE')
    if not args.output.is_dir() or not (args.output / 'run_contract.json').is_file():
        raise ValueError('RESUME_EXISTING_CONTRACT_REQUIRED')
    if not args.cache.is_dir():
        raise ValueError('RESUME_EXISTING_CACHE_REQUIRED')
    saved = read(args.output / 'run_contract.json')
    if not isinstance(saved, dict):
        raise ValueError('RESUME_CONTRACT_FORMAT_INVALID')
    if saved.get('format') != CONTRACT_FORMAT:
        raise ValueError('RESUME_CONTRACT_FORMAT_INVALID')
    for field in ('config_sha256', 'schema_sha256', 'manifest_sha256',
                  'source_input_sha256', 'code_sha256', 'offline_preflight',
                  'semantic_model', 'execution_batch_id', 'execution_scope'):
        if saved.get(field) != frozen[field]:
            raise ValueError('RESUME_FROZEN_CONTRACT_MISMATCH:' + field)
    if saved.get('case_ids') != case_ids or saved.get('config') != frozen['config']:
        raise ValueError('RESUME_SELECTED_CASE_OR_CONFIG_MISMATCH')
    if saved.get('seed') != 20261023 or saved.get('version') != frozen['version']:
        raise ValueError('RESUME_SEED_OR_VERSION_MISMATCH')
    for field in ('doctor_packet_release', 'real_doctor_assignment_creation',
                  'clinical_release_permitted'):
        if saved.get(field) is not False:
            raise ValueError('RESUME_GOVERNANCE_FLAG_MISMATCH:' + field)
    if saved.get('annotation_references_read') is not False \
            or saved.get('prior_algorithm_outputs_read') is not False \
            or saved.get('source_fhir_annotations_ignored') is not True \
            or saved.get('clinical_reference_created') is not False:
        raise ValueError('RESUME_NO_REFERENCE_CONTRACT_MISMATCH')
    if saved.get('semantic_model') != config_model(frozen) \
            or saved.get('execution_batch_id') != frozen['execution_batch_id']:
        raise ValueError('RESUME_MODEL_OR_BATCH_MISMATCH')
    completed = []
    pending = []
    failures = []
    for case_id in case_ids:
        case_dir = args.output / case_id
        receipt_path = case_dir / RECEIPT_NAME
        summary_path = case_dir / 'summary.json'
        failure_path = case_dir / 'failure.json'
        if failure_path.is_file():
            failures.append(read(failure_path))
        if not case_dir.exists():
            pending.append(case_id)
            continue
        if not case_dir.is_dir() or failure_path.exists() or not summary_path.is_file() \
                or not receipt_path.is_file():
            raise ValueError('RESUME_INCOMPLETE_CASE_DIRECTORY:' + case_id
                + '; choose a new sibling output and cache; prior data was not overwritten')
        receipt = read(receipt_path)
        if not isinstance(receipt, dict) \
                or receipt.get('format') != CASE_RECEIPT_FORMAT \
                or receipt.get('case_id') != case_id \
                or receipt.get('case_technically_complete') is not True:
            raise ValueError('RESUME_CASE_RECEIPT_INVALID:' + case_id)
        case_contract = read(case_dir / 'case_contract.json')
        if digest(case_contract) != receipt.get('case_contract_sha256') \
                or case_contract.get('case_id') != case_id \
                or case_contract.get('source_input_sha256') != frozen['source_input_sha256'][case_id] \
                or case_contract.get('config_sha256') != frozen['config_sha256'] \
                or case_contract.get('schema_sha256') != frozen['schema_sha256'] \
                or case_contract.get('run_contract_sha256') != file_hash(args.output / 'run_contract.json'):
            raise ValueError('RESUME_CASE_CONTRACT_BINDING_INVALID:' + case_id)
        bindings = receipt.get('output_sha256')
        if not isinstance(bindings, dict) or set(bindings) != set(CASE_OUTPUTS):
            raise ValueError('RESUME_CASE_OUTPUT_BINDINGS_INVALID:' + case_id)
        for name, expected in bindings.items():
            _verified_file(case_dir / name, expected, case_id + '/' + name)
        _validate_complete_summary(read(summary_path), case_id, frozen['config'])
        completed.append(case_id)
    cache_receipt_paths = sorted((args.cache / 'invocations').glob('*.json'))
    for receipt_path_in_cache in cache_receipt_paths:
        cached_receipt = read(receipt_path_in_cache)
        if not isinstance(cached_receipt, dict) \
                or type(cached_receipt.get('cached')) is not bool \
                or not isinstance(cached_receipt.get('attempts'), list):
            raise ValueError('RESUME_INVOCATION_EVIDENCE_INVALID:' + receipt_path_in_cache.name)
        if cached_receipt.get('status') != 'SUCCESS':
            raise ValueError('RESUME_FAILURE_EVIDENCE_REQUIRES_NEW_SIBLING:'
                + receipt_path_in_cache.name)
        request_sha256 = cached_receipt.get('request_sha256')
        if not isinstance(request_sha256, str) or len(request_sha256) != 64:
            raise ValueError('RESUME_INVOCATION_REQUEST_IDENTITY_INVALID:'
                + receipt_path_in_cache.name)
        if not (args.cache / (request_sha256 + '.json')).is_file():
            raise ValueError('RESUME_SUCCESS_CACHE_MISSING:' + receipt_path_in_cache.name)
        if not cached_receipt.get('cached') \
                and cached_receipt.get('provider_invocation_occurred') is not True:
            raise ValueError('RESUME_ORIGINAL_PROVIDER_PROVENANCE_INVALID:'
                + receipt_path_in_cache.name)
        actual_attempts = len(cached_receipt['attempts'])
        maximum = frozen['config'].get('max_http_attempts_per_request', 3)
        if not 1 <= actual_attempts <= maximum:
            raise ValueError('RESUME_INVOCATION_ATTEMPTS_INVALID:' + receipt_path_in_cache.name)
    receipt_totals = _receipt_totals(_load_receipts(args.cache))
    if receipt_totals['fresh_invocation_count'] > preflight['maximum_logical_provider_requests'] \
            or receipt_totals['http_attempt_count'] > preflight['maximum_http_attempts']:
        raise ValueError('RESUME_PROVIDER_BUDGET_EXCEEDED')
    if (args.output / 'failure.json').exists():
        failures.append(read(args.output / 'failure.json'))
    if failures:
        raise ValueError('RESUME_FAILURE_EVIDENCE_REQUIRES_NEW_SIBLING:'
            + json.dumps(failures, ensure_ascii=False)[:1000])
    return completed, pending


def config_model(contract):
    return contract['config'].get('semantic_model')


def _record_failure(args, case_id, exc):
    """Persist bounded failure evidence without deleting partial case output."""
    failure = dict(case_id=case_id, status='FAILED', error=type(exc).__name__,
                   detail=str(exc)[:1000])
    case_dir = args.output / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    failure_path = case_dir / 'failure.json'
    if failure_path.exists():
        raise ValueError('CASE_FAILURE_EVIDENCE_ALREADY_EXISTS:' + case_id) from exc
    _write_new_json(failure_path, failure, private=args.private_root is not None)
    return failure


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/p2_auto_checkpoint_v05.json')
    parser.add_argument('--schema', type=Path,
        default=ROOT / 'private/p2_auto_checkpoint_v0.1_20261024/standards/fhir.schema.json')
    parser.add_argument('--cases', nargs='*')
    parser.add_argument('--case-workers', type=int, default=1)
    parser.add_argument('--unit-workers', type=int, default=4)
    parser.add_argument('--private-root', type=Path,
        help='Physical owner-only parent for a relocated private output/cache')
    parser.add_argument('--dry-run', action='store_true',
        help='Freeze unit/request-budget preflight without constructing a provider Client')
    parser.add_argument('--execution-authorization', type=Path,
        help='Read-only exact run authorization; required for a new live run')
    parser.add_argument('--resume', action='store_true',
        help='Re-enter only the same frozen contract with verified case receipts')
    args = parser.parse_args(argv)
    if args.dry_run and (args.execution_authorization or args.resume):
        raise ValueError('DRY_RUN_IS_EXCLUSIVE_WITH_LIVE_OR_RESUME')
    if type(args.case_workers) is not int or args.case_workers < 1 \
            or type(args.unit_workers) is not int or args.unit_workers < 1:
        raise ValueError('WORKER_COUNTS_MUST_BE_POSITIVE_INTEGERS')
    if not args.schema.is_file():
        raise ValueError('FHIR_SCHEMA_REQUIRED')
    config = read(args.config)
    manifest = read(args.manifest)
    case_ids = _validate_manifest(manifest, args.cases)
    # Restore the lost early configuration gate before any execution or Client.
    validate_feedback_config(config)
    if args.private_root is not None:
        from scripts.cohort_construction.p2_checkpoint_material_review_app.build import require_private_output
        private_root = args.private_root.resolve()
        if not private_root.is_dir() or private_root.stat().st_mode & 0o077:
            raise ValueError('PRIVATE_ROOT_OWNER_ONLY_REQUIRED')
        for path in (args.output, args.cache):
            require_private_output(path, private_root=private_root)
    if not args.resume:
        if args.output.exists() or args.cache.exists():
            raise ValueError('NEW_OUTPUT_AND_CACHE_REQUIRED; choose a new sibling')
    preflight = _preflight(args, config, case_ids)
    execution_config = _authorization_and_execution_config(config, preflight)
    frozen = _frozen_contract(args, execution_config, case_ids, preflight)
    if args.dry_run:
        # Neither dry-run output nor cache may replace any existing path.
        if args.private_root:
            _private_mkdir(args.output, parents=True, exist_ok=False)
        else:
            args.output.mkdir(parents=True, exist_ok=False)
        _write_new_json(args.output / 'run_contract.json', frozen,
                        private=args.private_root is not None)
        print(json.dumps(dict(dry_run=True, case_count=len(case_ids),
            case_ids=case_ids, maximum_logical_provider_requests=preflight['maximum_logical_provider_requests'],
            maximum_http_attempts=preflight['maximum_http_attempts'], model_calls=0,
            provider_client_constructed=False), ensure_ascii=False, indent=2))
        return 0
    model_execution_requested = not args.dry_run
    if args.resume:
        completed, pending = _validate_resume(args, frozen, case_ids, preflight)
        # The persisted frozen contract remains byte-identical. Only this
        # invocation's measured summary distinguishes a resume from new live work.
        args.resume_completed = completed
    else:
        if args.execution_authorization is None:
            raise ValueError('EXECUTION_AUTHORIZATION_REQUIRED_BEFORE_PROVIDER_CLIENT')
        authorization = read(args.execution_authorization)  # strictly read-only
        _validate_authorization(authorization, frozen, execution_config, preflight)
        if args.private_root:
            _private_mkdir(args.output, parents=True, exist_ok=False)
            _private_mkdir(args.cache, parents=True, exist_ok=False)
        else:
            args.output.mkdir(parents=True, exist_ok=False)
            args.cache.mkdir(parents=True, exist_ok=False)
        _write_new_json(args.output / 'run_contract.json', frozen,
                        private=args.private_root is not None)
        args.resume_completed = []
        pending = list(case_ids)
    summaries = []
    failed = False
    try:
        with ThreadPoolExecutor(max_workers=args.case_workers) as pool:
            futures = {pool.submit(run_case, case_id, args, execution_config): case_id
                       for case_id in pending}
            for future in as_completed(futures):
                case_id = futures[future]
                try:
                    summary = future.result()
                except Exception as exc:
                    failed = True
                    summary = _record_failure(args, case_id, exc)
                summaries.append(summary)
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                # Known mutable run-local progress, never a completion claim.
                _atomic_write_json(args.output / 'summary.json',
                    sorted(summaries, key=lambda row: row['case_id']),
                    private=args.private_root is not None)
    except BaseException as exc:
        # A crash may precede future.result(); keep any already saved cases and
        # produce an honest final measured summary rather than silently PASS.
        failed = True
        if not (args.output / 'failure.json').exists():
            _write_new_json(args.output / 'failure.json',
                dict(status='FAILED', error=type(exc).__name__, detail=str(exc)[:1000]),
                private=args.private_root is not None)
        raise
    finally:
        if not args.dry_run:
            try:
                measured = execution_summary(args.output, args.cache,
                    model_execution_requested=model_execution_requested,
                    resumed_execution=args.resume)
                if failed:
                    measured['technically_complete'] = False
                    measured['selected_subset_technically_complete'] = False
                _atomic_write_json(args.output / 'execution_summary.json', measured,
                    private=args.private_root is not None)
            except Exception as summary_error:
                _atomic_write_json(args.output / 'execution_summary.json', dict(
                    format=SUMMARY_FORMAT, technically_complete=False,
                    selected_subset_technically_complete=False,
                    summary_error=type(summary_error).__name__,
                    summary_error_detail=str(summary_error)[:1000],
                    clinical_reference_created=False, clinical_release_permitted=False),
                    private=args.private_root is not None)
    if failed:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
