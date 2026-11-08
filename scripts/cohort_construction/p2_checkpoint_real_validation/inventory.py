"""Independent batch inventory and settled-call coverage, never clinical labels."""
from collections import Counter
from copy import deepcopy
import re

from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import read

FORMAT = 'checkpoint-real-execution-inventory-0.1.0'
ELIGIBLE = frozenset({'READY_FOR_AUTHORIZED_DEVELOPMENT_EXECUTION',
    'READY_GRAPH_AUDIT_WITH_MATERIAL_PREPARATION_HOLD'})


def _ids(rows):
    if not isinstance(rows, list):
        raise ValueError('REAL_BATCH_CASE_INVENTORY_INVALID')
    ids = [row.get('case_id') if isinstance(row, dict) else None for row in rows]
    if (any(not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', sid)
            or sid in ('.', '..') for sid in ids) or len(ids) != len(set(ids))):
        raise ValueError('REAL_BATCH_CASE_IDENTITY_INVALID')
    return ids


def freeze_inventory(preflight, batch_contract, authorization, initial_budget_state=None):
    ids = _ids(preflight['cases'])
    if not ids:
        raise ValueError('REAL_BATCH_EMPTY_FROZEN_INVENTORY')
    subset = authorization.get('execution_subset')
    authorized = ids if subset is None else _ids([dict(case_id=sid) for sid in subset])
    if not authorized or not set(authorized).issubset(ids):
        raise ValueError('REAL_BATCH_AUTHORIZED_CASE_INVENTORY_INVALID')
    if subset is not None and not set(authorized).issubset(
            row['case_id'] for row in preflight['cases'] if row['status'] in ELIGIBLE):
        raise ValueError('REAL_BATCH_AUTHORIZED_CASE_INVENTORY_INVALID')
    inherited = (initial_budget_state or {}).get('records', [])
    _settled_calls(inherited)
    result = dict(format=FORMAT, batch_id=batch_contract['batch_id'],
        manifest_sha256=batch_contract['manifest_sha256'],
        preflight_sha256=canonical_sha256(preflight),
        batch_contract_sha256=canonical_sha256(batch_contract),
        authorization_sha256=canonical_sha256(authorization),
        cases=deepcopy(preflight['cases']), authorized_case_ids=authorized,
        inherited_record_count=len(inherited), inherited_records_sha256=canonical_sha256(inherited))
    result['inventory_sha256'] = canonical_sha256(result)
    return result


def _settled_calls(records):
    """Pair every recorded provider plan with one settlement, including failures."""
    pending = Counter()
    called = set()
    for row in records:
        if not row.get('provider_request_performed'):
            continue
        sid = row.get('context', {}).get('case_id')
        _ids([dict(case_id=sid)])
        called.add(sid)
        key = canonical_sha256({name: row.get(name) for name in
            ('context', 'stage', 'task', 'request_sha256', 'target', 'precondition', 'expected_gain')})
        if row.get('outcome') == 'PROVIDER_IO_STARTED_OR_UNSETTLED':
            pending[key] += 1
        elif row.get('outcome', '').startswith(('EXECUTION_FAILED:', 'RESPONSE_RETURNED_')):
            if pending[key] <= 0:
                raise ValueError('REAL_BATCH_LEDGER_SETTLEMENT_WITHOUT_PLAN')
            pending[key] -= 1
        else:
            raise ValueError('REAL_BATCH_LEDGER_OUTCOME_INVALID')
    if any(pending.values()):
        raise ValueError('REAL_BATCH_LEDGER_UNSETTLED_CALLS')
    return called


def validate_inventory(output, progress, ledger):
    ids = _ids(progress.get('cases'))
    called = _settled_calls(ledger['records'])
    authorization = read(output / 'effective-batch-authorization.json')
    if progress.get('batch_id') != authorization.get('batch_id'):
        raise ValueError('REAL_BATCH_PROGRESS_BATCH_BINDING_MISMATCH')
    path = output / 'batch-execution-inventory.json'
    inventory = read(path) if path.is_file() else None
    if authorization.get('execution_inventory_version') is not None:
        if authorization['execution_inventory_version'] != FORMAT or inventory is None:
            raise ValueError('REAL_BATCH_EXECUTION_INVENTORY_REQUIRED')
    if inventory is not None:
        if (inventory.get('format') != FORMAT or inventory.get('inventory_sha256') !=
                canonical_sha256({k: v for k, v in inventory.items() if k != 'inventory_sha256'})
                or inventory['authorization_sha256'] != canonical_sha256(authorization)
                or inventory['batch_id'] != progress['batch_id']
                or inventory['manifest_sha256'] != authorization['manifest_sha256']):
            raise ValueError('REAL_BATCH_EXECUTION_INVENTORY_BINDING_INVALID')
        expected = _ids(inventory['cases'])
        authorized = inventory['authorized_case_ids']
        if authorized != authorization.get('execution_subset', expected):
            raise ValueError('REAL_BATCH_EXECUTION_AUTHORIZED_INVENTORY_CHANGED')
        if ids != expected:
            raise ValueError('REAL_BATCH_PROGRESS_CASE_INVENTORY_MISMATCH')
        inherited_count = inventory['inherited_record_count']
        if (type(inherited_count) is not int or inherited_count < 0
                or inherited_count > len(ledger['records'])
                or canonical_sha256(ledger['records'][:inherited_count]) != inventory['inherited_records_sha256']):
            raise ValueError('REAL_BATCH_INHERITED_LEDGER_BINDING_CHANGED')
        scope = 'FROZEN_PREFLIGHT_AND_AUTHORIZATION'
    else:
        # Legacy runs did not freeze an execution inventory. Do not create one
        # retrospectively or infer a full cohort denominator from progress rows.
        authorized = authorization.get('execution_subset', ids)
        if not set(authorized).issubset(ids) or not ids:
            raise ValueError('REAL_BATCH_PROGRESS_AUTHORIZED_CASE_MISSING')
        expected = ids
        scope = 'LEGACY_AUTHORIZATION_LEDGER_AND_ARTIFACTS_ONLY'
    if not called.issubset(ids):
        raise ValueError('REAL_BATCH_LEDGER_CASE_MISSING_FROM_PROGRESS')
    if not called.issubset(authorized):
        raise ValueError('REAL_BATCH_LEDGER_CASE_OUTSIDE_AUTHORIZATION')
    artifacts = set()
    # Either artifact root may be absent in a legitimate non-reconstruction run.
    for name in ('episodes', 'automatic-revisions'):
        if (output / name).is_dir():
            artifacts.update(p.name for p in (output / name).iterdir() if p.is_dir())
    if not artifacts.issubset(ids):
        raise ValueError('REAL_BATCH_ARTIFACT_CASE_MISSING_FROM_PROGRESS')
    if not artifacts.issubset(authorized):
        raise ValueError('REAL_BATCH_ARTIFACT_CASE_OUTSIDE_AUTHORIZATION')
    rows = {row['case_id']: row for row in progress['cases']}
    # CE-REAL022: a settled provider call can be followed by an orchestration
    # failure before run.json exists. Keep that exact saved state replayable,
    # without manufacturing a run identity. Any run claim, including on a HOLD,
    # still requires the independently replayable episode/run evidence below.
    for sid in called:
        row = rows[sid]
        episode_id = row.get('episode_id')
        run_path = output / 'episodes' / sid / episode_id / 'run.json' if episode_id else None
        run_claimed = bool(row.get('run_sha256')) or (run_path is not None and run_path.is_file())
        if run_claimed:
            # A hash claim needs the persisted run; an unexpected run also needs
            # a progress-bound hash. replay_batch then validates and binds both.
            if not episode_id or not row.get('run_sha256') or not run_path.is_file():
                raise ValueError('REAL_BATCH_CALLED_CASE_RUN_EVIDENCE_REQUIRED')
        elif row.get('state') == 'ORCHESTRATION_HOLD':
            if not episode_id or not row.get('reason_code'):
                raise ValueError('REAL_BATCH_CALLED_CASE_HOLD_EVIDENCE_REQUIRED')
        else:
            raise ValueError('REAL_BATCH_CALLED_CASE_RUN_EVIDENCE_REQUIRED')
        if row['state'].startswith(('NOT_ATTEMPTED', 'PREFLIGHT_HOLD')):
            raise ValueError('REAL_BATCH_CALLED_CASE_MARKED_UNATTEMPTED')
    for sid in set(expected) - set(authorized):
        if rows[sid].get('state') != 'NOT_ATTEMPTED_OUTSIDE_AUTHORIZED_SUBSET':
            raise ValueError('REAL_BATCH_OUTSIDE_SUBSET_STATE_INVALID')
    return dict(scope=scope, frozen_case_count=len(expected) if inventory else None,
        authorized_case_count=len(authorized), attempted_case_count=len(called),
        case_result_count=len(ids), inventory_sha256=inventory['inventory_sha256'] if inventory else None,
        outside_authorized_subset_count=len(set(expected) - set(authorized)),
        recorded_calls_settled=True)
