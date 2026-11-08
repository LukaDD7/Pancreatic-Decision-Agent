"""Bound Ready Resource state composition; this function never releases a packet.

Automatic evidence, actual human sample statistics and external governance are
separate inputs. The caller must execute the separately approved release process;
this manifest is a state receipt, not a signature or doctor assignment.
"""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_material_governance.episode import validate_episode_material_set
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from .chain import validate_delivery_audit_chain

FORMAT = 'ready-resource-manifest-0.1.0'
GOVERNANCE_FORMAT = 'ready-resource-external-governance-authorization-0.1.0'
RESOURCE_STATES = ('CANDIDATE_NOT_READY', 'READY_FOR_HUMAN_SAMPLING',
    'SAMPLING_VALIDATION_PENDING', 'SAMPLING_QUALIFIED', 'SAMPLING_FAILED',
    'READY_FOR_CLINICAL_PILOT', 'CLINICAL_PACKET_ELIGIBLE')


def _delivery_binding(delivery):
    binding = delivery['initial_request']['response_binding']
    return dict(episode_material_set_sha256=binding['episode_material_set_sha256'],
        delivery_input_sha256=binding['delivery_input_sha256'],
        chain_sha256=delivery['chain_sha256'])


def _verified_delivery(delivery, material_set):
    replay = validate_delivery_audit_chain(delivery)
    if delivery['state'] != 'AUTO_DELIVERY_AUDIT_PASS':
        raise ValueError('READY_RESOURCE_DELIVERY_AUDIT_NOT_PASS')
    if delivery['initial_request']['episode_material_set']['set_sha256'] != material_set['set_sha256']:
        raise ValueError('READY_RESOURCE_DELIVERY_MATERIAL_SET_MISMATCH')
    return replay


def _actual_sample(sample, design, plan):
    if not isinstance(sample, dict) or not isinstance(design, dict) or not isinstance(plan, dict):
        return None
    try:
        from .sampling import validate_human_sample_assessment
        return validate_human_sample_assessment(sample, design=design, plan=plan)
    except (ValueError, KeyError, TypeError):
        return None


def _protocol(protocol, material_set, deliveries):
    if not isinstance(protocol, dict) or not all(protocol.get(key) is True for key in
            ('frozen', 'D01_D11_frozen', 'questionnaire_frozen', 'assignment_and_blinding_frozen')):
        return False
    if not isinstance(protocol.get('protocol_sha256'), str) or not protocol['protocol_sha256']:
        return False
    if protocol.get('episode_material_set_sha256') != material_set['set_sha256']:
        return False
    actual = {item['packet']['questionnaire']['sha256'] for item in deliveries}
    actual.update(item['packet']['questionnaire']['canonical'] + '|' + item['packet']['questionnaire']['version']
                  for item in deliveries)
    bindings = protocol.get('questionnaire_bindings')
    if not isinstance(bindings, list) or not bindings:
        return False
    for item in bindings:
        if item.get('sha256') not in actual or item.get('canonical') + '|' + item.get('version') not in actual:
            return False
    return True


def _authorization(authorization, *, material_set, delivery, sample, design, protocol):
    if not isinstance(authorization, dict) or authorization.get('format') != GOVERNANCE_FORMAT:
        return False
    if not all(authorization.get(key) is True for key in ('active', 'release_approval',
            'pipeline_scope_approved', 'population_scope_approved', 'actual_statistics_reviewed',
            'clinical_protocol_approved', 'automatic_sampling_release_authorized')):
        return False
    if (authorization.get('episode_material_set_sha256') != material_set['set_sha256']
            or authorization.get('delivery_input_sha256') != delivery['delivery_input_sha256']
            or authorization.get('design_sha256') != design['design_sha256']
            or authorization.get('human_result_sha256') != sample['human_result_sha256']
            or authorization.get('protocol_sha256') != protocol['protocol_sha256']):
        return False
    if authorization.get('pipeline_binding_sha256') != sample['pipeline_binding_sha256']:
        return False
    if authorization.get('population_manifest_sha256') != sample['population_manifest_sha256']:
        return False
    return isinstance(authorization.get('authorization_id'), str) and bool(authorization['authorization_id'])


def build_ready_resource_manifest(*, resource_id, graph_audit, material_set,
                                  delivery_audit=None, staging_delivery=None,
                                  sampling_design=None, sampling_plan=None, human_sample_result=None,
                                  clinical_protocol=None, external_authorization=None,
                                  strict_human_semantic_audit=False):
    """Compose independently verified evidence; no LLM status creates human proof."""
    if not isinstance(resource_id, str) or not resource_id:
        raise ValueError('READY_RESOURCE_ID_REQUIRED')
    if not isinstance(material_set, dict):
        raise ValueError('READY_RESOURCE_MATERIAL_SET_REQUIRED')
    if not isinstance(graph_audit, dict) or graph_audit.get('format') not in (
            'candidate-reconstruction-audit-chain-0.1.0', 'candidate-reconstruction-audit-chain-0.2.0',
            'candidate-reconstruction-audit-chain-0.3.0'):
        raise ValueError('READY_RESOURCE_REPLAYABLE_GRAPH_CHAIN_REQUIRED')
    if not isinstance(delivery_audit, dict) or delivery_audit.get('format') != 'episode-delivery-audit-chain-0.1.0':
        raise ValueError('READY_RESOURCE_REPLAYABLE_DELIVERY_CHAIN_REQUIRED')
    validate_episode_material_set(material_set)
    graph_replay = None
    if isinstance(graph_audit, dict) and graph_audit.get('format') in (
            'candidate-reconstruction-audit-chain-0.1.0', 'candidate-reconstruction-audit-chain-0.2.0',
            'candidate-reconstruction-audit-chain-0.3.0'):
        from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.chain import validate_graph_audit_chain
        graph_replay = validate_graph_audit_chain(graph_audit)
        graph = graph_audit['initial_request']['candidate_graph']
        if material_set['episode_reference'].get('graph_snapshot') != graph:
            raise ValueError('READY_RESOURCE_GRAPH_MATERIAL_MISMATCH')
    delivery = delivery_audit if isinstance(delivery_audit, dict) and delivery_audit.get('format') == 'episode-delivery-audit-chain-0.1.0' else None
    delivery_replay = _verified_delivery(delivery, material_set) if delivery is not None else None
    automatic_complete = (graph_replay is not None and graph_audit['state'] == 'AUTO_GRAPH_AUDIT_PASS'
        and bool(material_set['checkpoint_materials'])
        and all(item['state'] == 'AUTO_REVIEW_PASS' for item in material_set['checkpoint_materials'])
        and delivery_replay is not None and isinstance(staging_delivery, dict)
        and staging_delivery.get('doctor_assignment_created') is False
        and staging_delivery.get('clinical_release_permitted') is False)
    # An automatic chain never supplies or substitutes for per-packet SemanticAudit.
    automatic_complete = automatic_complete and all(
        item['packet']['provenance']['semantic_audit_id'] and item['packet']['provenance']['compiled_partition_sha256']
        for item in delivery['initial_request']['actual_deliveries']) if delivery is not None else False
    sample = _actual_sample(human_sample_result, sampling_design, sampling_plan)
    sample_bound = sample is not None and automatic_complete and all(
        sample.get(key) == value for key, value in (
            ('episode_material_set_sha256', material_set['set_sha256']),
            ('delivery_input_sha256', delivery['delivery_input_sha256'])))
    if sample is not None and sample.get('population_scope') != 'SINGLE_EPISODE':
        # This manifest is one Episode resource, not a cohort-level release receipt.
        sample_bound = False
    human_status = 'NOT_ASSESSED'
    sampling_status = 'SAMPLING_VALIDATION_PENDING'
    if sample is not None:
        human_status = sample.get('human_sample_status', 'NOT_ASSESSED')
    if sample_bound and sample['status'] == 'SAMPLING_QUALIFIED':
        sampling_status = 'SAMPLING_QUALIFIED'
    elif sample is not None and sample['status'] in ('SAMPLING_FAILED', 'HUMAN_SAMPLE_REVISE', 'HUMAN_SAMPLE_UNRESOLVED'):
        sampling_status = 'SAMPLING_FAILED'
    protocol_ready = _protocol(clinical_protocol, material_set,
        delivery['initial_request']['actual_deliveries']) if delivery is not None else False
    authorized = (sample_bound and sampling_status == 'SAMPLING_QUALIFIED' and protocol_ready
        and _authorization(external_authorization, material_set=material_set, delivery=delivery,
            sample=sample, design=sampling_design, protocol=clinical_protocol))
    strict_bound = strict_human_semantic_audit is True and protocol_ready and automatic_complete
    state = 'CANDIDATE_NOT_READY'
    if automatic_complete:
        state = 'READY_FOR_HUMAN_SAMPLING'
        if sampling_status == 'SAMPLING_QUALIFIED' and protocol_ready:
            state = 'READY_FOR_CLINICAL_PILOT'
            if strict_bound or authorized:
                state = 'CLINICAL_PACKET_ELIGIBLE'
    result = dict(format=FORMAT, resource_id=resource_id,
        candidate_graph_audit_status=graph_audit.get('state', graph_audit.get('status')) if isinstance(graph_audit, dict) else 'NOT_ASSESSED',
        graph_audit_chain_sha256=graph_audit.get('chain_sha256') if graph_replay is not None else None,
        episode_material_set_sha256=material_set['set_sha256'],
        automatic_delivery_audit_status=delivery['state'] if delivery is not None else 'NOT_ASSESSED',
        delivery_audit_binding=_delivery_binding(delivery) if delivery_replay is not None else None,
        actual_fhir_html_replay_verified=delivery_replay is not None,
        human_sample_status=human_status, sampling_validation_status=sampling_status,
        sampling_design_sha256=sampling_design.get('design_sha256') if sample is not None else None,
        frozen_sampling_plan_sha256=sample['frozen_plan_sha256'] if sample is not None else None,
        statistical_assessment_replayed=sample is not None,
        population_estimation_executed=bool(sample and sample['population_estimation_executed']),
        human_result_sha256=sample['human_result_sha256'] if sample is not None else None,
        clinical_protocol_ready=protocol_ready,
        strict_independent_human_semantic_audit=strict_bound,
        external_governance_authorization_supplied=authorized,
        resource_state=state, doctor_assignment_created=False,
        clinical_reference_created=False, expert_status='NOT_ASSESSED',
        automatic_sampling_release_authorized=authorized,
        strict_human_release_authorized=strict_bound,
        clinical_release_permitted=False, authorization_status='NOT_CLINICALLY_RELEASED',
        actual_release_requires_separate_execution=True)
    if state == 'CLINICAL_PACKET_ELIGIBLE':
        result['release_route'] = ('STRICT_INDEPENDENT_HUMAN_SEMANTIC_AUDIT_WITH_PROTOCOL'
            if strict_bound else 'EXPLICIT_EXTERNAL_AUTOMATIC_SAMPLING_GOVERNANCE_WITH_PROTOCOL')
    else:
        result['release_route'] = None
    result['manifest_sha256'] = canonical_sha256(result)
    return result
