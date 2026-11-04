"""Exclusive private artifacts and a single sequential model-call budget."""
from copy import deepcopy
import json
from pathlib import Path

from scripts.cohort_construction.p2_checkpoint_material_governance.orchestrate import SharedModelBudget
from scripts.cohort_construction.p2_checkpoint_material_governance.run import _json_exclusive
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import (
    private_mkdir, private_open, require_private_output,
)
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256

FORMAT = 'checkpoint-ready-pipeline-input-contract-0.1.0'
RUN_FORMAT = 'checkpoint-ready-pipeline-run-0.1.0'
IMPLEMENTATION_FORMAT = 'checkpoint-ready-pipeline-implementation-snapshot-0.1.0'


from .delivery_gate import freeze_delivery_gate


def implementation_snapshot():
    """Hash only source, schema and frozen configuration; never private inputs."""
    import hashlib
    root = Path(__file__).resolve().parents[3]
    packages = ('p2_checkpoint_packet', 'p2_checkpoint_fhir', 'p2_checkpoint_review_app',
        'p2_checkpoint_material_review_app', 'p2_checkpoint_material_governance',
        'p2_checkpoint_reconstruction_audit', 'p2_checkpoint_delivery_audit',
        'p2_checkpoint_ready_pipeline', 'p2_checkpoint_real_validation')
    paths = [root / 'config/p2_checkpoint_dual_audit_v063.json']
    for package in packages:
        paths.extend(path for path in (root / 'scripts/cohort_construction' / package).rglob('*')
            if path.is_file() and path.suffix in ('.py', '.json', '.js', '.cjs', '.css', '.html')
            and '__pycache__' not in path.parts and 'tests' not in path.relative_to(root).parts)
    files = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)}
    result = dict(format=IMPLEMENTATION_FORMAT, source_packages=list(packages),
        configuration_files=[str(paths[0].relative_to(root))], files=files,
        source_sha256=canonical_sha256(files),
        private_inputs_included=False, remote_execution_authorized_by_snapshot=False)
    result['implementation_sha256'] = canonical_sha256(result)
    return result


def json_value(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def new_output(path, *, private_root=None):
    output = require_private_output(path, private_root=private_root)
    if output.exists():
        raise ValueError('READY_PIPELINE_NEW_OUTPUT_REQUIRED')
    private_mkdir(output, parents=True)
    return output


def write(path, value):
    _json_exclusive(path, value)


def write_text(path, value):
    with private_open(path, 'x') as stream:
        stream.write(value)


def read(path):
    from scripts.cohort_construction.p2_checkpoint_material_review_app.build import parse_json_unique
    return parse_json_unique(Path(path).read_bytes().decode('utf-8'))


def self_hash(value, key):
    if not isinstance(value, dict) or not isinstance(value.get(key), str):
        return False
    return canonical_sha256({k: v for k, v in value.items() if k != key}) == value[key]


def freeze_contract(*, graph, sources, graph_audit_sources=None, episode_id, semantic_audits, questionnaire,
                    graph_audit_context, decision_task_id, visibility_policy_version,
                    max_rounds, max_model_calls, execute, config, semantic_audit_factory,
                    delivery_gate=None):
    from scripts.cohort_construction.p2_checkpoint_delivery_audit.audit import PROTOCOL_VERSION as DELIVERY_PROTOCOL
    from scripts.cohort_construction.p2_checkpoint_material_governance.review import AUTO_PROTOCOL_VERSION
    from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.audit import PROTOCOL_VERSION as GRAPH_PROTOCOL
    if not isinstance(config, dict):
        raise ValueError('READY_PIPELINE_CONFIG_REQUIRED')
    if not isinstance(semantic_audits, dict) and semantic_audits != 'LAZY':
        raise ValueError('READY_PIPELINE_INDEPENDENT_SEMANTIC_AUDITS_REQUIRED')
    if semantic_audits == 'LAZY':
        if not callable(semantic_audit_factory):
            raise ValueError('READY_PIPELINE_SEMANTIC_AUDIT_FACTORY_REQUIRED')
        semantic_audits = dict(mode='AFTER_FINAL_MATERIAL_COMPILATION',
            factory_sha256=canonical_sha256(getattr(semantic_audit_factory, '__module__', '')
                + ':' + getattr(semantic_audit_factory, '__qualname__', '')),
            automatic_material_pass_is_not_semantic_audit=True)
    if config.get('remote_execution_authorized') is not True and execute:
        raise ValueError('READY_PIPELINE_REMOTE_EXECUTION_NOT_AUTHORIZED')
    graph_audit_context = json.loads(json.dumps(graph_audit_context or {},
        ensure_ascii=False, allow_nan=False))
    return dict(format=FORMAT, graph=graph, sources=sources,
        graph_audit_sources=json_value(sources if graph_audit_sources is None else graph_audit_sources),
        episode_id=episode_id,
        semantic_audits=semantic_audits, questionnaire=questionnaire,
        graph_audit_context=graph_audit_context, decision_task_id=decision_task_id,
        visibility_policy_version=visibility_policy_version, max_rounds=max_rounds,
        max_model_invocations=max_model_calls, remote_execution_requested=execute,
        material_protocol_version=AUTO_PROTOCOL_VERSION,
        graph_audit_protocol_version=GRAPH_PROTOCOL,
        delivery_audit_protocol_version=DELIVERY_PROTOCOL,
        code_contract_version='p2-checkpoint-0.6.3', config=config,
        delivery_gate=freeze_delivery_gate(delivery_gate),
        doctor_assignment_created=False, clinical_release_permitted=False)


def validate_contract(contract):
    # Frozen v0.1.0 contracts used one source list for both scopes. Their
    # historical replay retains that explicit interpretation; new contracts
    # always persist the two independently reconstructed source scopes.
    if isinstance(contract, dict) and 'graph_audit_sources' not in contract \
            and isinstance(contract.get('sources'), list):
        contract = deepcopy(contract)
        contract['graph_audit_sources'] = deepcopy(contract['sources'])
    if not isinstance(contract, dict) or contract.get('format') != FORMAT:
        raise ValueError('READY_PIPELINE_INPUT_CONTRACT_INVALID')
    if (contract.get('doctor_assignment_created') is not False
            or contract.get('clinical_release_permitted') is not False
            or contract.get('code_contract_version') != 'p2-checkpoint-0.6.3'):
        raise ValueError('READY_PIPELINE_GOVERNANCE_CONTRACT_CHANGED')
    if not isinstance(contract.get('graph_audit_sources'), list):
        raise ValueError('READY_PIPELINE_GRAPH_AUDIT_SOURCES_REQUIRED')
    graph = contract.get('graph', {})
    if freeze_delivery_gate(contract.get('delivery_gate')) != contract.get('delivery_gate'):
        raise ValueError('READY_PIPELINE_DELIVERY_GATE_INVALID')
    if graph.get('format') != 'candidate-reconstruction-graph-snapshot-0.1.0':
        raise ValueError('READY_PIPELINE_FROZEN_GRAPH_REQUIRED')
    if graph.get('graph_sha256') != canonical_sha256({k: v for k, v in graph.items() if k != 'graph_sha256'}):
        raise ValueError('READY_PIPELINE_GRAPH_HASH_INVALID')
    if contract.get('episode_id') not in {item.get('episode_id') for item in graph.get('episodes', [])}:
        raise ValueError('READY_PIPELINE_EPISODE_NOT_IN_GRAPH')
    if (type(contract.get('max_rounds')) is not int or not 1 <= contract['max_rounds'] <= 20
            or type(contract.get('max_model_invocations')) is not int
            or not 1 <= contract['max_model_invocations'] <= 1000):
        raise ValueError('READY_PIPELINE_EXECUTION_BUDGET_INVALID')
    if contract.get('remote_execution_requested') is True and contract['config'].get('remote_execution_authorized') is not True:
        raise ValueError('READY_PIPELINE_REMOTE_EXECUTION_NOT_AUTHORIZED')
    return contract


def validate_inheritance(inherited, contract):
    """Return only rebindable parent values; never inherit a PASS as permission."""
    from .replay import validate_pipeline_run
    parent = inherited.get('parent_run')
    if not isinstance(parent, dict):
        raise ValueError('READY_PIPELINE_INHERITANCE_PARENT_REQUIRED')
    receipt = validate_pipeline_run(parent)
    if parent['input_contract'] != contract or not receipt['inherited_stages_replayable']:
        raise ValueError('READY_PIPELINE_INHERITANCE_CONTRACT_OR_REPLAY_CHANGED')
    if parent.get('delivery_revision_cycle') is not None:
        if parent['delivery_revision_cycle']['parent_run_sha256'] != parent['run_sha256']:
            raise ValueError('READY_PIPELINE_INHERITANCE_REVISION_PARENT_MISMATCH')
    return dict(graph_audit_chain=parent.get('graph_audit_chain'),
        material_chains=deepcopy(parent.get('material_chains', {})),
        material_set_rebuildable=receipt['material_set_rebuildable'],
        resolved_semantic_audits=deepcopy(parent.get('resolved_semantic_audits')),
        deliveries_rebuildable=receipt['deliveries_rebuildable'],
        deliveries=deepcopy(parent.get('actual_deliveries')),
        delivery_audit_chain=deepcopy(parent.get('delivery_audit_chain')),
        delivery_revision_cycle=parent.get('delivery_revision_cycle'),
        receipt=receipt)


class PipelineBudget(SharedModelBudget):
    """Adapters are selected by the orchestrator, never by the provider payload."""
    def __init__(self, limit, adapters, initial=None):
        super().__init__(limit, initial)
        self.adapters = adapters
        self.calls = []
        if initial is not None:
            saved = initial.get('stage_call_counts', {})
            if not isinstance(saved, dict) or any(type(v) is not int or v < 0 for v in saved.values()):
                raise ValueError('READY_PIPELINE_INITIAL_STAGE_USAGE_INVALID')
            self.stage_call_counts = dict(saved)
        else:
            self.stage_call_counts = {}

    def ask_for(self, stage, task, request, schema):
        if stage not in self.adapters:
            raise ValueError('READY_PIPELINE_MODEL_STAGE_UNKNOWN')
        self.adapter = self.adapters[stage]
        try:
            return self.ask(task, request, schema)
        finally:
            self.stage_call_counts[stage] = self.stage_call_counts.get(stage, 0) + 1

    def snapshot(self):
        return dict(super().snapshot(), stage_call_counts=dict(self.stage_call_counts),
            accounting_scope='graph_material_and_delivery_uncached_invocations')
