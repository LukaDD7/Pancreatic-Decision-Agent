"""Offline assembly or explicitly authorized, resumable candidate material review.

A continuation always has a new output directory. The parent remains read-only;
its graph, exact input bytes, completed chains and shared uncached-invocation
budget are rebound before any remaining Checkpoint is executed.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from scripts.cohort_construction.p2_auto_checkpoint_v04.client import Client
from scripts.cohort_construction.p2_auto_checkpoint.extract import read
from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import (
    assembly_receipt, freeze_candidate_graph, material_item, normalize_sources,
    pending_workbench, prepare_candidate_materials,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import validate_review_chain
from scripts.cohort_construction.p2_checkpoint_material_governance.episode import build_episode_material_set
from scripts.cohort_construction.p2_checkpoint_material_governance.prompts import material_review_client
from scripts.cohort_construction.p2_checkpoint_material_governance.review import AUTO_PROTOCOL_VERSION, run_review_chain
from scripts.cohort_construction.p2_checkpoint_material_governance.run import (
    _assert_nonsecret_config, _freeze_inputs, _json_exclusive, _round_journal,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.workbench import write_workbench_package
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import require_private_output
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256


_SAFE_COMPONENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')


def _write_new(path, value):
    _json_exclusive(path, value)


def _checkpoint_directory(output, checkpoint_id):
    if not isinstance(checkpoint_id, str) or not _SAFE_COMPONENT.fullmatch(checkpoint_id):
        raise ValueError('CHECKPOINT_ID_NOT_SAFE_OUTPUT_COMPONENT')
    return output / 'checkpoints' / checkpoint_id


def _self_hash_valid(value, key):
    if not isinstance(value, dict) or not isinstance(value.get(key), str):
        return False
    body = dict(value)
    body.pop(key)
    return canonical_sha256(body) == value[key]


class LocalModelBudgetHold(RuntimeError):
    """No provider invocation occurred; the chain records a local request HOLD."""
    def __init__(self):
        self.receipt = dict(cached=False, status='LOCAL_BUDGET_DENIED', provider_invocation_occurred=False)
        super().__init__('ASSEMBLY_MODEL_CALL_BUDGET_REACHED')


class SharedModelBudget:
    """Serialize the existing sequential orchestration's invocation accounting.

    A cache hit is not an uncached provider invocation. A synthetic adapter with
    no receipt is conservatively counted as uncached. Denials never call it.
    """
    def __init__(self, limit, initial=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('ASSEMBLY_MODEL_CALL_BUDGET_INVALID')
        self.limit = limit
        self.uncached_succeeded = 0
        self.uncached_failed = 0
        self.cache_hits = 0
        self.denied = 0
        if initial is not None:
            for key in ('uncached_succeeded', 'uncached_failed', 'cache_hits', 'denied'):
                value = initial.get(key)
                if type(value) is not int or value < 0:
                    raise ValueError('RESUME_MODEL_USAGE_INVALID')
                setattr(self, key, value)

    @property
    def uncached(self):
        return self.uncached_succeeded + self.uncached_failed

    def snapshot(self):
        return dict(uncached_succeeded=self.uncached_succeeded,
            uncached_failed=self.uncached_failed, cache_hits=self.cache_hits,
            denied=self.denied, actual_uncached_model_invocations=self.uncached,
            max_model_invocations=self.limit)

    def ask(self, task, request, schema):
        if self.uncached >= self.limit:
            self.denied += 1
            raise LocalModelBudgetHold()
        # ``adapter`` is bound only for the duration of this case execution.
        try:
            answer = self.adapter(task, request, schema)
        except Exception as exc:
            receipt = getattr(exc, 'receipt', {})
            if receipt.get('cached') is not True:
                self.uncached_failed += 1
            else:
                self.cache_hits += 1
            raise
        receipt = answer[1] if isinstance(answer, tuple) and len(answer) == 2 and isinstance(answer[1], dict) else {}
        if receipt.get('cached') is True:
            self.cache_hits += 1
        else:
            self.uncached_succeeded += 1
        return answer


def _load_graph(candidate_directory, graph_version):
    events = read(candidate_directory / 'decision_events.json')
    if isinstance(events, dict):
        events = events.get('events')
    return freeze_candidate_graph(events, read(candidate_directory / 'relations.json'),
        read(candidate_directory / 'episodes.json'), graph_version=graph_version,
        discovery_units=read(candidate_directory / 'discovery.json').get('units', [])
            if (candidate_directory / 'discovery.json').is_file() else [],
        unit_inventory=read(candidate_directory / 'unit_inventory.json')
            if (candidate_directory / 'unit_inventory.json').is_file() else None,
        final_graph_audit=read(candidate_directory / 'final_graph_audit.json')
            if (candidate_directory / 'final_graph_audit.json').is_file() else None)


def _execution_contract(graph, sources, decision_task_id, visibility_policy_version,
                        max_rounds, max_model_calls, execute):
    return dict(graph_sha256=canonical_sha256(graph), graph_version=graph['graph_version'],
        supplied_sources_sha256=canonical_sha256(sources), decision_task_id=decision_task_id,
        visibility_policy_version=visibility_policy_version, max_rounds=max_rounds,
        max_model_invocations=max_model_calls, remote_execution_requested=execute,
        protocol_version=AUTO_PROTOCOL_VERSION)


def _completed_parent_results(previous_output, previous, tasks):
    """Replay inherited chains against the identically reassembled task inputs."""
    by_id = {task['compiled']['target']['checkpoint_id']: task for task in tasks}
    completed, chains = [], {}
    for result in previous.get('results', []):
        checkpoint_id = result.get('checkpoint_id')
        if 'chain_sha256' not in result:
            continue
        if checkpoint_id not in by_id:
            raise ValueError('RESUME_PARENT_RESULT_UNKNOWN_CHECKPOINT')
        directory = _checkpoint_directory(previous_output, checkpoint_id)
        saved_task, chain = read(directory / 'prepared-task.json'), read(directory / 'review-chain.json')
        task = by_id[checkpoint_id]
        if saved_task != task or canonical_sha256(chain) != result['chain_sha256']:
            raise ValueError('RESUME_PARENT_TASK_OR_CHAIN_CHANGED')
        validate_review_chain(chain, expected_graph_context=task['task_context'])
        receipt = read(directory / 'execution-receipt.json')
        if receipt != result:
            raise ValueError('RESUME_PARENT_RECEIPT_CHANGED')
        completed.append(deepcopy(result))
        chains[checkpoint_id] = chain
    if len({result['checkpoint_id'] for result in completed}) != len(completed):
        raise ValueError('RESUME_DUPLICATE_PARENT_RESULT')
    return completed, chains


def _freeze_direct_case_inputs(output, candidate_directory, sources, config,
                               max_rounds, execute):
    """Library callers get the same immutable archive as the CLI, once per case."""
    if output.name != 'case' or (output.parent / 'input-snapshot-receipt.json').is_file():
        return
    if (output.parent / 'input-snapshot').exists():
        raise ValueError('ASSEMBLY_INCOMPLETE_INPUT_SNAPSHOT')
    paths = dict(events=candidate_directory / 'decision_events.json',
        relations=candidate_directory / 'relations.json', episodes=candidate_directory / 'episodes.json')
    for name, filename in (('discovery', 'discovery.json'), ('unit_inventory', 'unit_inventory.json'),
                           ('final_graph_audit', 'final_graph_audit.json')):
        if (candidate_directory / filename).is_file():
            paths[name] = candidate_directory / filename
    paths['sources'] = output.parent / 'direct-sources.json'
    paths['config'] = output.parent / 'direct-config.json'
    exact = {name: path.read_bytes() for name, path in paths.items()
             if name not in ('sources', 'config')}
    for name, value in (('sources', sources), ('config', config)):
        if value is not None:
            exact[name] = (json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        else:
            paths.pop(name)
    output.parent.mkdir(parents=True, exist_ok=False)
    _freeze_inputs(output.parent, paths, exact, effective_budget=max_rounds, execution_requested=execute)


def assemble_candidate_case(*, candidate_directory, sources, output, graph_version,
                            decision_task_id, visibility_policy_version,
                            ask=None, config=None, execute=False, max_checkpoints=None,
                            max_rounds=3, max_model_calls=20, selected_checkpoint_ids=None,
                            continuation=None, prior_results=(), inherited_chains=None,
                            initial_model_usage=None, frozen_parent=False):
    """Assemble all targets and optionally execute a deterministic selected subset.

    ``continuation`` and inherited objects are private receipts from
    :func:`resume_candidate_case`; they never transfer clinical or human status.
    """
    if not isinstance(candidate_directory, Path):
        candidate_directory = Path(candidate_directory)
    if frozen_parent:
        _parent_case_integrity(Path(continuation['parent_case_directory']))
    output = require_private_output(output)
    if output.exists():
        raise ValueError('NEW_CASE_OUTPUT')
    _freeze_direct_case_inputs(output, candidate_directory, sources, config, max_rounds, execute)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'checkpoints').mkdir()
    (output / 'episodes').mkdir()
    graph = _load_graph(candidate_directory, graph_version)
    normalized_sources = normalize_sources(sources)
    tasks, failures = prepare_candidate_materials(graph, normalized_sources,
        decision_task_id=decision_task_id, visibility_policy_version=visibility_policy_version)
    if max_checkpoints is not None:
        if selected_checkpoint_ids is not None or type(max_checkpoints) is not int or not 0 <= max_checkpoints <= len(tasks):
            raise ValueError('ASSEMBLY_CHECKPOINT_BUDGET_INVALID')
        selected = tasks[:max_checkpoints]
    elif selected_checkpoint_ids is not None:
        if (not isinstance(selected_checkpoint_ids, list)
                or len(selected_checkpoint_ids) != len(set(selected_checkpoint_ids))):
            raise ValueError('ASSEMBLY_SELECTED_CHECKPOINTS_INVALID')
        wanted = set(selected_checkpoint_ids)
        selected = [task for task in tasks if task['compiled']['target']['checkpoint_id'] in wanted]
        if len(selected) != len(wanted):
            raise ValueError('ASSEMBLY_SELECTED_CHECKPOINT_UNKNOWN')
    else:
        selected = tasks
    effective_config = dict(config or {})
    if ask is not None and not execute:
        raise ValueError('ASSEMBLY_MODEL_ADAPTER_REQUIRES_EXPLICIT_EXECUTE')
    if execute:
        if effective_config.get('protocol_version') != AUTO_PROTOCOL_VERSION:
            raise ValueError('MATERIAL_PROTOCOL_CONFIG_MISMATCH')
        if effective_config.get('remote_execution_authorized') is not True:
            raise ValueError('REMOTE_EXECUTION_NOT_AUTHORIZED_BY_CONFIG')
    budget = SharedModelBudget(max_model_calls, initial_model_usage)
    if ask is not None:
        budget.adapter = ask
    _write_new(output / 'candidate-graph-snapshot.json', graph)
    _write_new(output / 'source-registry-input.json', normalized_sources)
    _write_new(output / 'assembly-receipt.json', assembly_receipt(graph, normalized_sources, tasks, failures))
    results = []
    executed_chains = dict(inherited_chains or {})
    for task in selected:
        checkpoint_id = task['compiled']['target']['checkpoint_id']
        directory = _checkpoint_directory(output, checkpoint_id)
        directory.mkdir()
        _write_new(directory / 'prepared-task.json', task)
        journal = None
        if ask is not None:
            try:
                journal = _round_journal(directory)
            except Exception as exc:
                result = dict(checkpoint_id=checkpoint_id, status='ORCHESTRATION_HOLD',
                    reason_code=type(exc).__name__, clinical_release_permitted=False)
                _write_new(directory / 'execution-receipt.json', result)
                results.append(result)
                continue
        if ask is None:
            result = dict(checkpoint_id=checkpoint_id, status='REQUEST_PREPARED_NOT_EXECUTED', remote_model_calls=0)
            _write_new(directory / 'preparation-receipt.json', result)
            results.append(result)
            continue
        before = budget.snapshot()
        if budget.uncached >= budget.limit:
            # No chain is fabricated for a target whose first request was never made.
            result = dict(checkpoint_id=checkpoint_id, status='MODEL_BUDGET_HOLD',
                reason_code='ASSEMBLY_MODEL_CALL_BUDGET_REACHED', clinical_release_permitted=False)
            _write_new(directory / 'execution-receipt.json', result)
            results.append(result)
            continue
        try:
            chain = run_review_chain(task['manifest'], task['compiled'], budget.ask,
                candidate_sources=task['candidate_sources'], excluded_sources=task['excluded_sources'],
                task_context=task['task_context'], max_rounds=max_rounds, journal=journal)
            replay = validate_review_chain(chain, expected_graph_context=task['task_context'])
            _write_new(directory / 'review-chain.json', chain)
            _write_new(directory / 'chain-validation-receipt.json', replay)
            item = material_item(task, chain)
            _write_new(directory / 'checkpoint-material.json', item)
            executed_chains[checkpoint_id] = chain
            result = dict(checkpoint_id=checkpoint_id, status=chain['state'],
                chain_sha256=canonical_sha256(chain), stop_reason=chain['stop_reason'])
            try:
                package = pending_workbench(task, chain, assignment_id='ASSEMBLY-' + checkpoint_id)
                result['workbench_receipt'] = write_workbench_package(package, directory / 'researcher-workbench')
            except Exception as exc:
                result.update(status='WORKBENCH_HOLD', automatic_state=chain['state'],
                    reason_code=type(exc).__name__, clinical_release_permitted=False)
        except Exception as exc:
            # Local program/persistence failures remain visible and do not become
            # an automated material judgment or invalidate completed siblings.
            result = dict(checkpoint_id=checkpoint_id, status='ORCHESTRATION_HOLD',
                reason_code=type(exc).__name__, clinical_release_permitted=False)
        _write_new(directory / 'execution-receipt.json', result)
        _write_new(directory / 'model-budget-receipt.json', dict(before=before, after=budget.snapshot()))
        results.append(result)
    all_results = list(prior_results) + results
    current_ids = {task['compiled']['target']['checkpoint_id']: task for task in tasks}
    by_episode = {episode['episode_id']: [] for episode in graph['episodes']}
    for checkpoint_id, chain in executed_chains.items():
        task = current_ids.get(checkpoint_id)
        if task is not None:
            by_episode[task['episode_id']].append(material_item(task, chain))
    episode_receipts = []
    for episode in graph['episodes']:
        items = by_episode[episode['episode_id']]
        expected = set(episode['checkpoint_ids'])
        covered = {item['compiled']['target']['checkpoint_id'] for item in items}
        status = 'COMPLETE_MATERIAL_SET_PENDING_INDEPENDENT_AUDIT' if covered == expected else 'INCOMPLETE_OR_UNEXECUTED_CHECKPOINTS'
        value = dict(episode_id=episode['episode_id'], status=status,
            expected_checkpoint_ids=sorted(expected), completed_checkpoint_ids=sorted(covered),
            automatic_material_states={item['compiled']['target']['checkpoint_id']: item['state'] for item in items},
            material_set_sha256=None, clinical_release_permitted=False)
        if covered == expected:
            reference = dict(episode_id=episode['episode_id'], candidate_version=graph['graph_version'],
                input_sha256=canonical_sha256(graph), graph_snapshot=graph)
            material_set = build_episode_material_set(reference, items)
            value['material_set_sha256'] = material_set['set_sha256']
            _write_new(output / 'episodes' / (episode['episode_id'] + '.json'), material_set)
        _write_new(output / 'episodes' / (episode['episode_id'] + '-receipt.json'), value)
        episode_receipts.append(value)
    usage = budget.snapshot()
    summary = dict(format='candidate-material-orchestration-summary-0.2.0',
        graph_sha256=canonical_sha256(graph), graph_version=graph['graph_version'],
        prepared_checkpoints=len(tasks), assembly_failures=failures, results=all_results,
        executed_checkpoints=sum('chain_sha256' in result for result in all_results),
        automatic_pass_checkpoints=sum(result.get('status') == 'AUTO_REVIEW_PASS' for result in all_results),
        hold_checkpoints=sum(result.get('status') in ('AUTO_REVIEW_HOLD', 'WORKBENCH_HOLD', 'MODEL_BUDGET_HOLD', 'ORCHESTRATION_HOLD') for result in all_results),
        unexecuted_checkpoints=len(tasks) - sum('chain_sha256' in result for result in all_results),
        episodes=episode_receipts, model_usage=usage,
        max_model_calls=max_model_calls,
        actual_uncached_model_calls=usage['actual_uncached_model_invocations'],
        remote_execution_requested=execute, graph_clinical_semantics_certified=False,
        human_sample_status='NOT_ASSESSED', doctor_packet_created=False,
        clinical_reference_created=False, clinical_release_permitted=False)
    summary['execution_contract'] = _execution_contract(graph, normalized_sources,
        decision_task_id, visibility_policy_version, max_rounds, max_model_calls, execute)
    if continuation is not None:
        summary['continuation'] = deepcopy(continuation)
    summary['summary_sha256'] = canonical_sha256(summary)
    _write_new(output / 'summary.json', summary)
    return summary


def _parent_case_integrity(previous_output):
    if previous_output.is_symlink() or previous_output.name != 'case':
        raise ValueError('RESUME_PARENT_CASE_IDENTITY_INVALID')
    if not (previous_output.parent / 'input-snapshot-receipt.json').is_file():
        raise ValueError('RESUME_PARENT_INPUT_SNAPSHOT_REQUIRED')
    root = previous_output.parent.resolve()
    if previous_output.resolve() != root / 'case':
        raise ValueError('RESUME_PARENT_CASE_IDENTITY_INVALID')
    expected = read(previous_output.parent / 'input-snapshot-receipt.json')
    if not _self_hash_valid(expected, 'snapshot_sha256'):
        raise ValueError('RESUME_INPUT_SNAPSHOT_HASH_INVALID')
    for record in expected['code']:
        path = Path(record['archived_path'])
        if path.is_absolute() or '..' in path.parts or str(path) != record['archived_path']:
            raise ValueError('RESUME_ARCHIVE_PATH_INVALID')
        archived = previous_output.parent / path
        if (archived.is_symlink() or not archived.is_file()
                or hashlib.sha256(archived.read_bytes()).hexdigest() != record['bytes_sha256']):
            raise ValueError('RESUME_FROZEN_IMPLEMENTATION_CHANGED')
    for record in expected['inputs']:
        path = Path(record['archived_path'])
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('RESUME_ARCHIVE_PATH_INVALID')
        archived = previous_output.parent / path
        if (archived.is_symlink() or not archived.is_file()
                or hashlib.sha256(archived.read_bytes()).hexdigest() != record['bytes_sha256']):
            raise ValueError('RESUME_FROZEN_INPUT_CHANGED')
    return root, expected


def resume_candidate_case(*, previous_output, output, candidate_directory, sources,
                          graph_version, decision_task_id, visibility_policy_version,
                          ask, config, execute=True, max_rounds=3, max_model_calls=20):
    """Continue only unexecuted targets, preserving the parent's aggregate budget."""
    previous_output, output = Path(previous_output).resolve(), Path(output).resolve()
    normalized_sources = normalize_sources(sources)
    if not (previous_output.parent / 'input-snapshot-receipt.json').is_file():
        raise ValueError('RESUME_PARENT_INPUT_SNAPSHOT_REQUIRED')
    _parent_case_integrity(previous_output)
    expected_inputs = {record['name']: record for record in read(previous_output.parent / 'input-snapshot-receipt.json')['inputs']}
    filenames = dict(events='decision_events.json', relations='relations.json', episodes='episodes.json',
        discovery='discovery.json', unit_inventory='unit_inventory.json', final_graph_audit='final_graph_audit.json')
    for name, record in expected_inputs.items():
        if name == 'sources':
            if normalize_sources(read(previous_output.parent / record['archived_path'])) != normalized_sources:
                raise ValueError('RESUME_FROZEN_INPUT_CHANGED')
            continue
        if name in filenames:
            path = Path(candidate_directory) / filenames[name]
            if (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record['bytes_sha256']):
                raise ValueError('RESUME_FROZEN_INPUT_CHANGED')
    previous = read(previous_output / 'summary.json')
    if not _self_hash_valid(previous, 'summary_sha256'):
        raise ValueError('RESUME_PARENT_SUMMARY_HASH_INVALID')
    if previous.get('format') != 'candidate-material-orchestration-summary-0.2.0':
        raise ValueError('RESUME_REQUIRES_V02_ORCHESTRATION_SUMMARY')
    if previous.get('continuation') is not None:
        raise ValueError('RESUME_REQUIRES_ORIGINAL_CASE_NOT_CONTINUATION')
    if (output.parent.resolve() != previous_output.parent.parent.resolve()
            or not output.name.startswith(previous_output.parent.name + '-resume-')
            or output.exists()):
        raise ValueError('RESUME_REQUIRES_NEW_SIBLING_CASE_ROOT')
    graph = _load_graph(Path(candidate_directory), graph_version)
    normalized_sources = normalize_sources(sources)
    expected = _execution_contract(graph, normalized_sources, decision_task_id,
        visibility_policy_version, max_rounds, max_model_calls, execute)
    if previous.get('execution_contract') != expected:
        raise ValueError('RESUME_CONTRACT_CHANGED')
    if read(previous_output / 'candidate-graph-snapshot.json') != graph:
        raise ValueError('RESUME_GRAPH_CHANGED')
    if read(previous_output / 'source-registry-input.json') != normalized_sources:
        raise ValueError('RESUME_SOURCES_CHANGED')
    tasks, failures = prepare_candidate_materials(graph, normalized_sources,
        decision_task_id=decision_task_id, visibility_policy_version=visibility_policy_version)
    if (len(tasks) != previous.get('prepared_checkpoints')
            or failures != previous.get('assembly_failures')):
        raise ValueError('RESUME_ASSEMBLY_CHANGED')
    completed, chains = _completed_parent_results(previous_output, previous, tasks)
    done = {result['checkpoint_id'] for result in completed}
    remaining = [task['compiled']['target']['checkpoint_id'] for task in tasks
                 if task['compiled']['target']['checkpoint_id'] not in done]
    if not remaining:
        raise ValueError('RESUME_NO_UNFINISHED_TARGETS')
    continuation = dict(parent_case_directory=str(previous_output.resolve()),
        parent_summary_sha256=previous['summary_sha256'],
        inherited_completed_checkpoint_ids=sorted(done),
        parent_results_replayed_without_model_calls=True,
        clinical_or_human_status_inherited=False)
    return assemble_candidate_case(candidate_directory=candidate_directory, sources=sources,
        output=output / 'case', graph_version=graph_version, decision_task_id=decision_task_id,
        visibility_policy_version=visibility_policy_version, ask=ask, config=config,
        execute=execute, max_rounds=max_rounds, max_model_calls=max_model_calls,
        selected_checkpoint_ids=remaining, continuation=continuation,
        prior_results=completed, inherited_chains=chains,
        initial_model_usage=previous.get('model_usage'), frozen_parent=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-directory', type=Path, required=True)
    parser.add_argument('--sources', type=Path, required=True, help='Private JSON list of exact complete originals')
    parser.add_argument('--output', type=Path, required=True, help='New case root, or the existing parent root with --resume')
    parser.add_argument('--graph-version', required=True)
    parser.add_argument('--decision-task-id', required=True)
    parser.add_argument('--visibility-policy-version', required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--max-checkpoints', type=int)
    parser.add_argument('--max-rounds', type=int, default=3)
    parser.add_argument('--max-model-calls', type=int, default=20)
    parser.add_argument('--resume', action='store_true', help='Continue an incomplete frozen case in a new sibling directory')
    args = parser.parse_args(argv)
    if not args.execute and (args.max_checkpoints is not None or args.config is not None or args.resume):
        raise ValueError('OFFLINE_ASSEMBLY_DOES_NOT_ACCEPT_EXECUTION_OR_RESUME_OPTIONS')
    if args.resume and args.max_checkpoints is not None:
        raise ValueError('RESUME_SELECTS_ONLY_UNFINISHED_CHECKPOINTS')
    paths = dict(events=args.candidate_directory / 'decision_events.json',
        relations=args.candidate_directory / 'relations.json', episodes=args.candidate_directory / 'episodes.json',
        sources=args.sources, config=args.config)
    for name, filename in (('discovery', 'discovery.json'),
                           ('unit_inventory', 'unit_inventory.json'),
                           ('final_graph_audit', 'final_graph_audit.json')):
        path = args.candidate_directory / filename
        if path.is_file():
            paths[name] = path
    exact = {name: path.read_bytes() for name, path in paths.items() if path is not None}
    config = json.loads(exact['config']) if 'config' in exact else None
    if config is not None:
        _assert_nonsecret_config(config)
    supplied_output = require_private_output(args.output)
    if args.resume:
        if (not supplied_output.is_dir()
                or not (supplied_output / 'case' / 'summary.json').is_file()
                or not (supplied_output / 'input-snapshot-receipt.json').is_file()):
            raise ValueError('RESUME_REQUIRES_INCOMPLETE_FROZEN_CASE')
        previous = read(supplied_output / 'input-snapshot-receipt.json')
        if not _self_hash_valid(previous, 'snapshot_sha256'):
            raise ValueError('RESUME_INPUT_SNAPSHOT_HASH_INVALID')
        for record in previous['inputs']:
            if record['name'] not in exact or hashlib.sha256(exact[record['name']]).hexdigest() != record['bytes_sha256']:
                raise ValueError('RESUME_INPUT_CONTRACT_CHANGED')
        parent_summary = read(supplied_output / 'case' / 'summary.json')
        output = supplied_output.parent / (supplied_output.name + '-resume-' + parent_summary['summary_sha256'][:12])
        output = require_private_output(output)
        output.mkdir(parents=True, exist_ok=False)
        _freeze_inputs(output, paths, exact, effective_budget=args.max_rounds, execution_requested=args.execute)
        case_output, previous_output = output / 'case', supplied_output / 'case'
    else:
        output = supplied_output
        output.mkdir(parents=True, exist_ok=False)
        _freeze_inputs(output, paths, exact, effective_budget=args.max_rounds, execution_requested=args.execute)
        case_output, previous_output = output / 'case', None
    try:
        function = resume_candidate_case if args.resume else assemble_candidate_case
        common = dict(candidate_directory=args.candidate_directory, sources=json.loads(exact['sources']),
            output=case_output, graph_version=args.graph_version, decision_task_id=args.decision_task_id,
            visibility_policy_version=args.visibility_policy_version,
            ask=material_review_client(Client(output / 'model-cache', config)) if args.execute else None,
            config=config, execute=args.execute, max_rounds=args.max_rounds,
            max_model_calls=args.max_model_calls)
        if args.resume:
            common['previous_output'] = previous_output
        else:
            common['max_checkpoints'] = args.max_checkpoints
        result = function(**common)
    except Exception as exc:
        _write_new(output / 'orchestration-failure.json', dict(status='ASSEMBLY_HOLD',
            error_type=type(exc).__name__, remote_model_execution_authorized=args.execute,
            clinical_release_permitted=False))
        raise
    print(json.dumps({key: result[key] for key in ('prepared_checkpoints', 'executed_checkpoints', 'hold_checkpoints')}, ensure_ascii=False))
    return result


if __name__ == '__main__':
    main()
