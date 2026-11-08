"""Independent, no-model reconstruction of persisted graph/material/delivery journals."""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256

from .persistence import read


INPUT_KEYS = {
    'material': ('input_manifest', 'input_compiled', 'request', 'request_sha256',
                 'request_binding_sha256', 'source_registry_sha256'),
    'graph': ('request', 'request_sha256', 'graph_sha256'),
    'delivery': ('request', 'request_sha256', 'episode_material_set_sha256',
                 'delivery_input_sha256'),
}


def _journal_input(chain, record, kind, *, failure=False):
    """Rebuild the exact REQUEST projection, including a failed material round.

    Failure receipts deliberately contain only the request, not the potentially
    large input manifest and compiled partition. Their durable input comes from
    the last validated output (or the initial version), never from a receipt.
    """
    if not isinstance(record, dict) or not isinstance(record.get('request'), dict):
        raise ValueError('READY_PIPELINE_JOURNAL_REQUEST_INVALID')
    if record.get('request_sha256') != canonical_sha256(record['request']):
        raise ValueError('READY_PIPELINE_JOURNAL_REQUEST_HASH_INVALID')
    request = {key: deepcopy(record[key]) for key in INPUT_KEYS[kind]
               if key in record}
    if kind == 'material':
        if failure:
            previous = chain['rounds'][-1] if chain['rounds'] else None
            request.update(
                input_manifest=deepcopy(previous['manifest'] if previous else chain['initial_manifest']),
                input_compiled=deepcopy(previous['compiled'] if previous else chain['initial_compiled']))
        request.update(
            request_binding_sha256=record['request']['response_binding']['request_sha256'],
            source_registry_sha256=chain['source_registry_sha256'])
    else:
        key = 'graph_sha256' if kind == 'graph' else 'episode_material_set_sha256'
        request[key] = chain[key]
        if kind == 'delivery':
            request['delivery_input_sha256'] = chain['delivery_input_sha256']
    if any(key not in request for key in INPUT_KEYS[kind]):
        raise ValueError('READY_PIPELINE_JOURNAL_INPUT_INCOMPLETE')
    return request


def expected_journal(chain, kind):
    """Derive records from a validated chain, not from an editable receipt."""
    if kind not in INPUT_KEYS or not isinstance(chain, dict) \
            or not isinstance(chain.get('rounds'), list):
        raise ValueError('READY_PIPELINE_JOURNAL_CHAIN_INVALID')
    records = []
    for index, round_ in enumerate(chain['rounds'], 1):
        request = _journal_input(chain, round_, kind)
        records.append(dict(index=index, request=request, completed=deepcopy(round_)))
    if 'failure' in chain:
        failure = chain['failure']
        request = _journal_input(chain, failure, kind, failure=True)
        records.append(dict(index=len(chain['rounds']) + 1, request=request,
                            failure=deepcopy(failure)))
    return dict(chain_sha256=canonical_sha256(chain), records=records)


def restore_journal(directory, chain, kind):
    """Recreate a replayed inherited journal without making a model request."""
    from .persistence import private_mkdir, write
    private_mkdir(directory / 'rounds', exist_ok=False)
    for record in expected_journal(chain, kind)['records']:
        folder = directory / 'rounds' / f"{record['index']:04d}"
        private_mkdir(folder, exist_ok=False)
        write(folder / 'input.json', record['request'])
        write(folder / ('completed-round.json' if 'completed' in record else 'failure-hold.json'),
              record.get('completed', record.get('failure')))


def journal_receipt(chain, kind):
    return dict(verified=True, journal_sha256=canonical_sha256(expected_journal(chain, kind)),
                chain_sha256=canonical_sha256(chain))


def validate_journal(output, directory, chain, kind):
    """Compare every saved round and the receipt; reject unaccounted extra files."""
    from .replay import _saved
    expected = expected_journal(chain, kind)
    prefix = str(directory.relative_to(output))
    for record in expected['records']:
        folder = prefix + '/rounds/' + f"{record['index']:04d}"
        if 'completed' in record:
            _saved(output, folder + '/input.json', record['request'])
            _saved(output, folder + '/completed-round.json', record['completed'])
            if (output / folder / 'failure-hold.json').exists():
                raise ValueError('READY_PIPELINE_JOURNAL_UNEXPECTED_FILE:' + folder)
        else:
            _saved(output, folder + '/input.json', record['request'])
            _saved(output, folder + '/failure-hold.json', record['failure'])
            if (output / folder / 'completed-round.json').exists():
                raise ValueError('READY_PIPELINE_JOURNAL_UNEXPECTED_FILE:' + folder)
    expected_paths = {prefix + '/journal-receipt.json'}
    for record in expected['records']:
        folder = prefix + '/rounds/' + f"{record['index']:04d}"
        expected_paths.add(folder + ('/completed-round.json' if 'completed' in record else '/failure-hold.json'))
        expected_paths.add(folder + '/input.json')
    rounds = output / prefix / 'rounds'
    actual_paths = {str(path.relative_to(output)) for path in rounds.rglob('*') if path.is_file()}
    if actual_paths != expected_paths - {prefix + '/journal-receipt.json'}:
        raise ValueError('READY_PIPELINE_JOURNAL_FILE_SET_MISMATCH:' + prefix)
    _saved(output, prefix + '/journal-receipt.json', journal_receipt(chain, kind))
