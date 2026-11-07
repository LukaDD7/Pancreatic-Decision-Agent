"""Bounded material-review transport; full research context remains in the chain.

This is a declared task-semantics projection, not lossless JSON compression and
not certification that omitted graph facts are clinically irrelevant. The full
request, graph, source registry and execution receipts remain replayable. Only
the provider payload omits the duplicate whole-graph snapshot. The selected
event and explicitly available target descriptors remain verbatim. All included,
candidate and excluded original source strings remain present in their existing
research scopes; this projection does not change doctor visibility.
"""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_packet.compiler import (
    ContractError, canonical_sha256,
)

FORMAT = 'checkpoint-material-review-task-projection-0.1.0'
TARGET_FIELDS = (
    'management_question', 'record_function', 'time_validation',
    'choice_boundary_citations', 'postchoice_citations', 'prechoice_citations',
    'availability_validation', 'evidence_relation_ledger',
    'evidence_relation_validation_reasons',
)
TARGET_FIELD_LABELS = {
    'management_question': '上游提议的管理问题；不是程序补写或已审定临床问题',
    'record_function': '发现阶段LLM的文书表述功能分类，不是已确认临床事实',
    'time_validation': '候选目标的时间核验状态，不是已认证实际临床选择时刻',
    'choice_boundary_citations': '上游提议的目标临床决策子句；默认MASK，不是选择前证据',
    'postchoice_citations': '上游提议的目标决策后执行/结局引文；默认MASK',
    'prechoice_citations': '上游提议的目标临床决策前信息引文，仍需材料审核',
    'availability_validation': '上游时点可用性核验记录，不等于医生披露许可',
    'evidence_relation_ledger': '上游来源绑定的EvidenceObject关系及局部未决原因，不证明阅读、采用或因果',
    'evidence_relation_validation_reasons': '上游EvidenceObject关系的兼容核验原因，不是临床真值或错误人数',
}


def _target_descriptor(event):
    supplied = {key: deepcopy(event[key]) for key in TARGET_FIELDS if key in event}
    ledger = event.get('evidence_relation_ledger')
    if isinstance(ledger, dict) and 'evidence_relation_validation_reasons' not in supplied:
        supplied['evidence_relation_validation_reasons'] = deepcopy(ledger.get('validation_reasons', []))
    if isinstance(ledger, dict) and 'evidence_relation_validation_reasons' in supplied \
            and supplied['evidence_relation_validation_reasons'] != ledger.get('validation_reasons', []):
        raise ContractError('MATERIAL_TRANSPORT_EVIDENCE_REASONS_CONTRADICT_LEDGER')
    return dict(
        event_id=event['event_id'],
        supplied_fields=supplied,
        not_supplied_fields=[key for key in TARGET_FIELDS if key not in event],
        field_semantics=deepcopy(TARGET_FIELD_LABELS),
        absent_field_is_not_original_text_omission=True,
        upstream_proposal_not_clinical_reference=True,
    )


def compact_material_review_request(request):
    """Project the provider task without mutating its persisted full request."""
    context = request.get('task_context') or {}
    snapshot = context.get('candidate_graph_snapshot')
    binding = context.get('candidate_graph_binding')
    if snapshot is None and binding is None:
        return deepcopy(request)
    if not isinstance(snapshot, dict) or not isinstance(binding, dict):
        raise ContractError('MATERIAL_TRANSPORT_GRAPH_BINDING_REQUIRED')
    if canonical_sha256(snapshot) != binding['graph_sha256'] \
            or ('graph_version' in snapshot
                and snapshot['graph_version'] != binding.get('graph_version')):
        raise ContractError('MATERIAL_TRANSPORT_GRAPH_HASH_MISMATCH')
    matches = [event for event in snapshot.get('events', [])
               if event.get('event_id') == binding['target_event_id']]
    if len(matches) != 1 or binding['checkpoint_id'] != request['target']['checkpoint_id']:
        raise ContractError('MATERIAL_TRANSPORT_TARGET_NOT_UNIQUE')
    projected_context = {key: deepcopy(value) for key, value in context.items()
                         if key != 'candidate_graph_snapshot'}
    projected_context.update(
        target_event_research_context=_target_descriptor(matches[0]),
        candidate_episode=next((deepcopy(episode) for episode in snapshot.get('episodes', [])
            if binding['target_event_id'] in episode.get('checkpoint_ids', [])), None),
        candidate_relations_for_target=[deepcopy(relation)
            for relation in snapshot.get('relations', [])
            if binding['target_event_id'] in (
                relation.get('source_event_id'), relation.get('target_event_id'))],
    )
    omitted = dict(
        field='task_context.candidate_graph_snapshot',
        graph_sha256=binding['graph_sha256'],
        graph_version=binding['graph_version'],
        reason='FULL_RESEARCH_GRAPH_ALREADY_HASH_BOUND;NOT_A_DUPLICATE_PROVIDER_INPUT',
        persisted_response_binding_hash=request['response_binding']['request_sha256'],
        clinical_fields_removed_from_included_or_candidate_or_excluded_sources=False,
        source_scope_changed=False,
        doctor_view_changed=False,
        clinical_irrelevance_certified=False,
        target_time_visibility_certified=False,
    )
    result = deepcopy(request)
    result['task_context'] = projected_context
    result['material_transport_projection'] = dict(
        format=FORMAT,
        semantics='TARGET_TASK_ONLY_FULL_SOURCE_SCOPES_RETAINED',
        omitted_whole_graph=omitted,
        replay_authority='PERSISTED_FULL_REQUEST_AND_SOURCE_REGISTRY',
        not_lossless_json_compression=True,
        not_a_new_clinical_fact=True,
    )
    # The echo hash deliberately remains that of the full persisted request.
    if result['response_binding'] != request['response_binding']:
        raise ContractError('MATERIAL_TRANSPORT_RESPONSE_BINDING_CHANGED')
    return result


def expand_material_review_request(payload, full_request):
    """Verify the declared projection against the actual persisted full request."""
    expected = compact_material_review_request(full_request)
    if payload != expected:
        raise ContractError('MATERIAL_TRANSPORT_PROJECTION_REPLAY_MISMATCH')
    return deepcopy(full_request)
