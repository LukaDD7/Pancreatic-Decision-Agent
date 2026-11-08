"""Explicit offline preparation or separately authorized review execution.

Original input bytes and exact local code/prompt/config snapshots are archived
before execution. Every round is persisted exclusively; failures retain prior
rounds and produce a HOLD receipt. Nothing is imported as an authorization.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import platform
from importlib.metadata import version as package_version
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from scripts.cohort_construction.p2_checkpoint_material_governance.prompts import (
    MATERIAL_REVIEW_INSTRUCTIONS, material_review_client,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.review import (
    AUTO_PROTOCOL_VERSION, build_review_request, run_review_chain,
)
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import (
    parse_json_unique, private_mkdir, private_open, require_private_output,
)
from scripts.cohort_construction.p2_auto_checkpoint_v04.client import Client


def provider_transport_verified(rounds):
    """No completed fresh round means no transport verification claim."""
    return bool(rounds) and all(round_.get('receipt', {}).get(
        'provider_transport_verified') is True for round_ in rounds)


def load(path):
    return parse_json_unique(Path(path).read_bytes().decode('utf-8'))


def _json_exclusive(path, value):
    with private_open(path, 'x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def _dependency_paths():
    """Explicit local implementation dependency closure; never include credentials."""
    root = Path(__file__).resolve().parents[3]
    names = [
        'scripts/cohort_construction/p2_checkpoint_material_governance/review.py',
        'scripts/cohort_construction/p2_checkpoint_material_governance/run.py',
        'scripts/cohort_construction/p2_checkpoint_material_governance/prompts.py',
        'scripts/cohort_construction/p2_checkpoint_material_governance/schema.py',
        'scripts/cohort_construction/p2_checkpoint_packet/compiler.py',
        'scripts/cohort_construction/p2_checkpoint_packet/semantic_audit.py',
        'scripts/cohort_construction/p2_auto_checkpoint_v04/client.py',
        'scripts/cohort_construction/p2_auto_checkpoint_v04/prompts.py',
        'scripts/cohort_construction/model_api.py',
        'scripts/cohort_construction/p2_auto_checkpoint/extract.py',
        'scripts/cohort_construction/p2_pathway_replay/core.py',
        'scripts/cohort_construction/p2_checkpoint_material_review_app/build.py',
    ]
    paths = [root / name for name in names]
    for directory in ('p2_checkpoint_material_governance', 'p2_checkpoint_packet'):
        paths.extend(sorted((root / 'scripts/cohort_construction' / directory).glob('*.py')))
    paths = sorted(set(paths))
    for directory in ('p2_checkpoint_packet/schemas', 'p2_checkpoint_material_governance/schemas'):
        paths.extend(sorted((root / 'scripts/cohort_construction' / directory).glob('*.json')))
    return root, paths


def _assert_nonsecret_config(config):
    # Execution credentials are loaded by Client; accepting them in a snapshot
    # config would persist authentication secrets. Reject rather than redact
    # input bytes or silently change the supplied configuration.
    sensitive = {'api_key', 'api-key', 'authorization', 'password', 'access_token',
                 'refresh_token', 'client_secret', 'secret', 'credentials'}
    def walk(value):
        if isinstance(value, dict):
            if any(str(key).lower() in sensitive for key in value):
                raise ValueError('CONFIG_CREDENTIAL_FIELDS_MUST_NOT_BE_ARCHIVED')
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(config)


def _execution_parameters(client, config, effective_budget):
    settings = getattr(client, 'settings', None)
    endpoint = getattr(settings, 'chat_completions_url', None)
    endpoint_record = None
    if endpoint:
        parsed = urlsplit(endpoint)
        # No userinfo, query or fragment is written to the run archive.
        host = parsed.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        if parsed.port:
            host += ':' + str(parsed.port)
        endpoint_record = urlunsplit((parsed.scheme, host, parsed.path, '', ''))
    return dict(format='material-review-execution-settings-0.2.0',
        model=getattr(settings, 'model', config.get('semantic_model')),
        thinking=getattr(settings, 'thinking', None), endpoint=endpoint_record,
        endpoint_sensitive_components_omitted=bool(endpoint and (parsed.username or parsed.query or parsed.fragment)),
        temperature=config.get('model_temperature'), max_tokens=config.get('max_output_tokens'),
        response_format='json_object', max_material_review_rounds=effective_budget,
        max_concurrent_model_calls=max(1, int(config.get('max_concurrent_model_calls', 2))),
        system_instructions_sha256=hashlib.sha256(MATERIAL_REVIEW_INSTRUCTIONS.encode('utf-8')).hexdigest(),
        authentication_secret_included=False)


def _freeze_inputs(output, paths, exact_bytes, *, effective_budget, execution_requested):
    snapshot = output / 'input-snapshot'
    private_mkdir(snapshot)
    records = []
    for name, path in paths.items():
        if path is None:
            continue
        data = exact_bytes[name]
        target = snapshot / (name + '.json')
        with private_open(target, 'xb') as stream:
            stream.write(data)
        records.append(dict(name=name, original_path=str(Path(path).resolve()),
                            archived_path=str(target.relative_to(output)),
                            byte_length=len(data), bytes_sha256=hashlib.sha256(data).hexdigest()))
    code = []
    root, dependencies = _dependency_paths()
    for source in dependencies:
        data = source.read_bytes()
        relative = source.relative_to(root)
        target = snapshot / 'implementation' / relative
        private_mkdir(target.parent, parents=True, exist_ok=True)
        with private_open(target, 'xb') as stream:
            stream.write(data)
        code.append(dict(path=str(relative), archived_path=str(target.relative_to(output)),
                         bytes_sha256=hashlib.sha256(data).hexdigest()))
    receipt = dict(format='material-review-run-inputs-0.2.0', protocol_version=AUTO_PROTOCOL_VERSION,
                   inputs=records, code=code, dependency_list_policy='declared-local-implementation-closure-0.2.0',
                   effective_max_material_review_rounds=effective_budget,
                   execution_requested=execution_requested,
                   runtime_environment=dict(python=platform.python_version(),
                       jsonschema=package_version('jsonschema'), requests=package_version('requests')),
                   prompt_sha256=hashlib.sha256(MATERIAL_REVIEW_INSTRUCTIONS.encode('utf-8')).hexdigest())
    receipt['snapshot_sha256'] = canonical_sha256(receipt)
    _json_exclusive(output / 'input-snapshot-receipt.json', receipt)
    return receipt


def _round_journal(output):
    rounds = output / 'rounds'
    rounds.mkdir(mode=0o700, exist_ok=False)
    def journal(stage, index, record):
        folder = rounds / f'{index:04d}'
        if stage == 'REQUEST':
            folder.mkdir(mode=0o700, exist_ok=False)
            _json_exclusive(folder / 'input.json', record)
        elif stage == 'ROUND':
            _json_exclusive(folder / 'completed-round.json', record)
        elif stage == 'FAILURE':
            _json_exclusive(folder / 'failure-hold.json', record)
        else:
            raise ValueError('UNKNOWN_REVIEW_JOURNAL_STAGE')
    return journal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--compiled', type=Path, required=True)
    parser.add_argument('--candidate-sources', type=Path,
                        help='Private JSON list of complete candidate sources explicitly supplied for review')
    parser.add_argument('--excluded-sources', type=Path,
                        help='Private JSON list of complete, explicitly excluded sources available for review')
    parser.add_argument('--task-context', type=Path,
                        help='Private JSON object with time/visibility uncertainties; never a clinical permission')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--execute', action='store_true',
                        help='Explicitly authorize the configured remote model for this run')
    parser.add_argument('--max-rounds', type=int)
    args = parser.parse_args(argv)
    # Parse exactly the bytes that will be archived, avoiding reread races.
    paths = dict(manifest=args.manifest, compiled=args.compiled,
                 candidate_sources=args.candidate_sources, excluded_sources=args.excluded_sources,
                 task_context=args.task_context, config=args.config)
    exact = {name: Path(path).read_bytes() for name, path in paths.items() if path is not None}
    values = {name: parse_json_unique(data.decode('utf-8')) for name, data in exact.items()}
    if 'config' in values:
        _assert_nonsecret_config(values['config'])
    manifest, compiled = values['manifest'], values['compiled']
    candidates, excluded = values.get('candidate_sources', []), values.get('excluded_sources', [])
    task_context = values.get('task_context')
    # Overlapping supplied scopes fail explicitly rather than silently filtering.
    request = build_review_request(manifest, compiled, candidate_sources=candidates,
                                   excluded_sources=excluded, task_context=task_context)
    output = require_private_output(args.output)
    private_mkdir(output, parents=True)
    effective_budget = args.max_rounds if args.max_rounds is not None else values.get('config', {}).get('max_material_review_rounds')
    snapshot = _freeze_inputs(output, paths, exact, effective_budget=effective_budget, execution_requested=args.execute)
    _json_exclusive(output / 'material-review-request.json', request)
    binding = dict(request_sha256=canonical_sha256(request),
                   material_payload_sha256=request['response_binding']['request_sha256'],
                   input_snapshot_sha256=snapshot['snapshot_sha256'])
    if not args.execute:
        result = dict(status='REQUEST_PREPARED_NOT_EXECUTED', remote_model_calls=0, **binding,
                      doctor_packet_created=False, clinical_reference_created=False)
        _json_exclusive(output / 'preparation-receipt.json', result)
        print(json.dumps(result, ensure_ascii=False))
        return result
    client, settings_sha256, chain, validation_sha256 = None, None, None, None
    try:
        if not args.config:
            raise ValueError('REMOTE_EXECUTION_REQUIRES_EXPLICIT_CONFIG')
        config = values['config']
        if config.get('protocol_version') != AUTO_PROTOCOL_VERSION:
            raise ValueError('MATERIAL_PROTOCOL_CONFIG_MISMATCH')
        if config.get('remote_execution_authorized') is not True:
            raise ValueError('REMOTE_EXECUTION_NOT_AUTHORIZED_BY_CONFIG')
        client = Client(output / 'model-cache', config)
        settings = _execution_parameters(client, config, effective_budget)
        settings_sha256 = canonical_sha256(settings)
        _json_exclusive(output / 'execution-settings.json', settings)
        chain = run_review_chain(manifest, compiled, material_review_client(client),
            candidate_sources=candidates, excluded_sources=excluded, task_context=task_context,
            max_rounds=effective_budget,
            journal=_round_journal(output))
        _json_exclusive(output / 'review-chain.json', chain)
        # The exact completed chain is durable before replay; a replay failure
        # cannot erase completed rounds or trigger an additional model request.
        from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import validate_review_chain
        try:
            validation = validate_review_chain(chain)
            validation['status'] = 'DETERMINISTIC_REPLAY_VERIFIED'
            validation['remote_provider_authenticity_certified'] = False
            validation['provider_transport_verified'] = provider_transport_verified(chain['rounds'])
        except Exception as exc:
            validation = dict(format='material-review-chain-validation-failure-0.2.0',
                status='REPLAY_FAILED', chain_sha256=canonical_sha256(chain),
                error_type=type(exc).__name__, clinical_release_permitted=False)
            validation_sha256 = canonical_sha256(validation)
            _json_exclusive(output / 'chain-validation-receipt.json', validation)
            raise
        validation_sha256 = canonical_sha256(validation)
        _json_exclusive(output / 'chain-validation-receipt.json', validation)
        result = dict(status=chain['state'], stop_reason=chain['stop_reason'], rounds=len(chain['rounds']),
                      model_receipts=deepcopy(client.receipts), **binding,
                      clinical_release_permitted=False, doctor_packet_created=False,
                      clinical_reference_created=False)
        if 'failure' in chain:
            result['failure'] = {k: chain['failure'][k] for k in
                                 ('stage', 'error_type', 'reason_code', 'round_index')}
    except Exception as exc:
        # This path covers local config, model setup, and persistence errors.
        safe_codes = {'REMOTE_EXECUTION_REQUIRES_EXPLICIT_CONFIG', 'MATERIAL_PROTOCOL_CONFIG_MISMATCH',
                      'REMOTE_EXECUTION_NOT_AUTHORIZED_BY_CONFIG', 'MATERIAL_REVIEW_BUDGET_OUT_OF_RANGE'}
        reason = str(exc) if str(exc) in safe_codes else type(exc).__name__
        result = dict(status='AUTO_REVIEW_HOLD', stop_reason='EXECUTION_SETUP_OR_PERSISTENCE_FAILED',
                      reason_code=reason, error_type=type(exc).__name__, model_receipts=deepcopy(client.receipts) if client else [],
                      rounds=len(chain['rounds']) if chain else 0,
                      **binding, clinical_release_permitted=False,
                      doctor_packet_created=False, clinical_reference_created=False)
    result['execution_settings_sha256'] = settings_sha256
    result['review_chain_sha256'] = canonical_sha256(chain) if chain else None
    result['chain_validation_sha256'] = validation_sha256
    _json_exclusive(output / 'execution-receipt.json', result)
    print(json.dumps({k: result[k] for k in ('status', 'stop_reason')}, ensure_ascii=False))
    return result


if __name__ == '__main__':
    main()
