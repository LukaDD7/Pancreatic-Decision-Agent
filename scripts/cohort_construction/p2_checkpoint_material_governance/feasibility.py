"""Private, source-linked feasibility review of a candidate Episode.

This is deliberately a different product from ``feedback.py``: it asks whether
an Episode and its candidate decision points are clinically intelligible and
well partitioned. It does not create a blind packet, acceptable modes, answers,
a SemanticAudit or a clinical Reference. Explanations retain their upstream
proposal provenance; no new model call or hidden chain-of-thought is exposed.
"""
from copy import deepcopy
from datetime import datetime
import html
import json
import re

from scripts.cohort_construction.p2_checkpoint_material_governance.episode import (
    validate_episode_material_set,
)
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256, text_sha256
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.chain import (
    validate_graph_audit_chain,
)

FORMAT = 'episode-feasibility-review-0.2.0'
FORM_VERSION = 'episode-decision-feasibility-feedback-0.1.0'
VIEWER_VERSION = 'episode-feasibility-viewer-0.2.0'

# These schemas cross a JSON persistence boundary; use JSON-native arrays so
# an in-memory rebuild and its saved representation remain directly comparable.
EPISODE_FIELDS = [
    ['episode_overall', '这段 Episode 的还原是否可行？', ['FEASIBLE', 'FEASIBLE_WITH_REVISION', 'NOT_FEASIBLE', 'NOT_ASSESSED']],
    ['boundary_assessment', 'Episode 的起止范围', ['APPROPRIATE', 'TOO_BROAD', 'TOO_NARROW', 'UNABLE_TO_JUDGE', 'NOT_ASSESSED']],
    ['omissions', '遗漏的关键过程或决策点'],
    ['duplicates', '重复或不应拆分的内容'],
    ['general_comments', '整体修改建议'],
]
EVENT_FIELDS = [
    ['necessity', '这里是否构成值得评价的决策点？', ['VALID_DECISION_POINT', 'NOT_A_DECISION_POINT', 'UNCERTAIN', 'NOT_ASSESSED']],
    ['granularity', '当前划分粒度', ['APPROPRIATE', 'SHOULD_MERGE', 'SHOULD_SPLIT', 'UNCERTAIN', 'NOT_ASSESSED']],
    ['timing', '时点和先后关系', ['SUPPORTED', 'NEEDS_CORRECTION', 'UNKNOWN', 'NOT_ASSESSED']],
    ['source_support', '原文是否支持这个解释？', ['SUPPORTED', 'PARTIALLY_SUPPORTED', 'NOT_SUPPORTED', 'NOT_ASSESSED']],
    ['suggested_correction', '修改建议（合并、拆分、时点、遗漏等）'],
]
GOVERNANCE_FALSE = (
    'formal_blind_packet_created', 'semantic_audit_created', 'clinical_questionnaire_frozen',
    'acceptable_mode_set_created', 'clinical_reference_created', 'doctor_answer_created',
    'doctor_assignment_created', 'clinical_release_permitted',
)


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def _proposal_index(graph_chain, graph):
    """Match upstream discovery proposals by immutable event ID, never text."""
    if graph_chain is None:
        return {}, 'NOT_AVAILABLE'
    validate_graph_audit_chain(graph_chain)
    request = graph_chain['initial_request']
    if request.get('candidate_graph') != graph:
        raise ValueError('FEASIBILITY_GRAPH_AUDIT_BINDING_MISMATCH')
    if graph_chain['graph_sha256'] != canonical_sha256(graph):
        raise ValueError('FEASIBILITY_GRAPH_AUDIT_HASH_MISMATCH')
    proposals = {}
    for unit in request.get('discovery_llm_proposals', []):
        for proposal in unit.get('events', []):
            event_id = proposal.get('event_id')
            if event_id in graph_chain['initial_request']['response_binding'].get('target_event_ids', []) \
                    and event_id in proposals:
                raise ValueError('FEASIBILITY_DUPLICATE_DISCOVERY_PROPOSAL')
            if event_id is not None:
                if event_id in proposals:
                    raise ValueError('FEASIBILITY_DUPLICATE_DISCOVERY_PROPOSAL')
                proposals[event_id] = proposal
    return proposals, 'UPSTREAM_DISCOVERY_LLM_PROPOSAL_NOT_CLINICAL_CERTIFICATION'


def _phase_order(phase):
    if not isinstance(phase, str):
        return 90
    # Coarse display proxies only. A spanning phase cannot prove a clinical order.
    for prefix, order in (
        ('入院', 10), ('术前', 20), ('麻醉', 25), ('术中探查', 30),
        ('术中', 35), ('手术执行', 40), ('术后', 50), ('出院', 60), ('随访', 70),
    ):
        if phase.startswith(prefix):
            return order
    return 80


def _clock(event, proposal, original_index):
    """Return an explicit, typed sort proxy, never a reconstructed minute."""
    choice = event.get('choice_time')
    validation = event.get('time_validation') if isinstance(event.get('time_validation'), dict) else {}
    if _text(choice) and validation.get('choice_time_verified') is True:
        return dict(kind='EXPLICIT_CHOICE_TIME', value=choice, display=choice,
                    order=(0, choice, original_index),
                    explanation='程序已接受的选择时间候选；仍非临床认证。')
    source_clock = proposal.get('source_document_clock') or {}
    if _text(source_clock.get('value')) and source_clock.get('precision') not in (None, 'unknown'):
        value = source_clock['value']
        return dict(kind='RECORD_TIME_PROXY', value=value, display='记录时间代理：' + value,
                    order=(1, value, original_index),
                    explanation='原始记录时间代理，不等于实际选择发生时刻。')
    phase = _text(event.get('phase')) or _text(proposal.get('phase'))
    if phase:
        return dict(kind='PHASE_PROXY', value=phase, display='阶段代理：' + phase,
                    order=(2, _phase_order(phase), original_index),
                    explanation='精确选择时刻未证实；仅按病历阶段粗排，不据此推断真实分钟或因果。')
    return dict(kind='CANDIDATE_ORDER_ONLY', value=None, display='时点未知；保留候选记录顺序',
                order=(3, original_index),
                explanation='没有可用于排序的已证实选择时刻、记录时间或阶段。')


def _citations(event):
    for key, role in (('action_citations', '动作／执行原文'), ('choice_boundary_citations', '实际选择答案边界'),
                      ('prechoice_citations', '选择前证据'), ('postchoice_citations', '选择后记录')):
        for index, citation in enumerate(event.get(key, {}).get('valid', [])):
            if not isinstance(citation, dict) or citation.get('span_verified') is not True \
                    or citation.get('raw_quote_verified') is not True \
                    or citation.get('eligible_as_source_support') is not True:
                continue
            if not isinstance(citation.get('start'), int) or not isinstance(citation.get('end'), int):
                continue
            yield dict(source_id=citation['source_id'], start=citation['start'], end=citation['end'],
                       quote=citation['quote'], role=role, citation_id=key + '-' + str(index))


def _review_proposals(graph_chain):
    """Index structured review proposals; these are LLM judgments, not facts."""
    if graph_chain is None:
        return {}
    proposals = {}
    for proposal in graph_chain['initial_request'].get('review_llm_proposals', []):
        event_id = proposal.get('event_id')
        if event_id is not None:
            if event_id in proposals:
                raise ValueError('FEASIBILITY_DUPLICATE_REVIEW_PROPOSAL')
            proposals[event_id] = proposal
    return proposals


def _uncertainty_judgments(proposal):
    """Expose saved explanations and quotes without deriving a clinical answer."""
    result = []
    for name, value in (proposal.get('uncertainty_dimensions') or {}).items():
        if not isinstance(value, dict):
            continue
        result.append(dict(dimension=name, status=value.get('status'),
            reason_category=value.get('reason_category'),
            explanation=_text(value.get('explanation')),
            source_quotes=deepcopy(value.get('source_quotes') or [])))
    return result


def _material_summary(material):
    """Keep preparation failures distinct from a valid doctor-view projection."""
    chain = material['auto_review_chain']
    failure = chain.get('failure')
    return dict(state=material['state'], stop_reason=chain.get('stop_reason'),
        review_round_count=len(chain['rounds']),
        failure=None if failure is None else dict(stage=failure.get('stage'),
            reason_code=failure.get('reason_code'), error_type=failure.get('error_type')),
        clinical_release_permitted=False)


def _material_review_judgment(material):
    """Return saved model output and receipts, never fill missing rounds."""
    chain = material['auto_review_chain']
    if not chain['rounds']:
        return dict(available=False, unavailable_reason='材料复核尚无通过响应核验的结构化判断；'
            '现有失败不能替换成模型审核意见。', rounds=[])
    rounds = []
    for item in chain['rounds']:
        review = item['review']
        rounds.append(dict(round_index=review['review_round']['round_index'],
            status=review['status'], dimensions=deepcopy(review['dimensions']),
            findings=deepcopy(review['findings']), required_actions=deepcopy(review['required_actions']),
            operation_count=len(review['operations']), input_compiled_sha256=item['input_compiled_sha256'],
            compiled_sha256=item['compiled']['compiled_sha256'],
            doctor_view_sha256=item['request']['doctor_view']['view_sha256'],
            ledger=deepcopy(item['ledger']), diff=deepcopy(item['diff']),
            application_receipt=deepcopy(item['application_receipt'])))
    return dict(available=True, unavailable_reason=None, rounds=rounds,
        provenance='LLM_MATERIAL_REVIEW_AND_DETERMINISTIC_PROGRAM_RECEIPTS_NOT_CLINICAL_CERTIFICATION')


def _checkpoint_context(material, documents):
    """Build the private, inspectable pre-choice context and visible projection.

    ``prechoice_citations`` are LLM-proposed, program-verified spans, not a
    complete historical chart. The projected doctor view is a separate saved
    algorithmic output and must never be called a clinician-approved packet.
    """
    event = next(event for event in material['auto_review_chain']['task_context']
                 ['candidate_graph_snapshot']['events']
                 if event['event_id'] == material['compiled']['target']['checkpoint_id'])
    context = event.get('evidence_context') or {}
    prechoice = [citation for citation in _citations(event) if citation['role'] == '选择前证据']
    linked_ids = {citation['source_id'] for citation in event.get('action_citations', {}).get('valid', [])}
    linked_ids.update(event.get('source_ids') or [])
    linked_ids.update(citation['source_id'] for citation in prechoice)
    linked_ids.update(citation['source_id'] for citation in _citations(event))
    visible, visible_ids = [], set()
    for source in material['doctor_view']['materials']:
        if text_sha256(source['text']) != source['sha256']:
            raise ValueError('FEASIBILITY_DOCTOR_VIEW_HASH_MISMATCH')
        if not source['text']:
            continue
        visible.append(dict(source_id=source['source_id'], source_version=source['source_version'],
            text=source['text'], sha256=source['sha256']))
        visible_ids.add(source['source_id'])
    original_ids = [source['source_id'] for source in material['manifest']['sources']]
    partitions = []
    for source in material['compiled']['sources']:
        original = documents.get(source['source_id'])
        if original is None or original['sha256'] != source['source_sha256']:
            raise ValueError('FEASIBILITY_PARTITION_ORIGINAL_MISMATCH')
        spans, cursor = [], 0
        for span in source['segments']:
            if span['start'] != cursor or span['end'] <= cursor \
                    or original['text'][span['start']:span['end']] != span['text']:
                raise ValueError('FEASIBILITY_PARTITION_SPAN_MISMATCH')
            spans.append(dict(start=span['start'], end=span['end'],
                disposition=span['disposition'], text=span['text']))
            cursor = span['end']
        if cursor != len(original['text']):
            raise ValueError('FEASIBILITY_PARTITION_INCOMPLETE')
        partitions.append(dict(source_id=source['source_id'],
            source_version=source['source_version'], segments=spans))
    cutoff = dict(value=context.get('cutoff'), basis=context.get('cutoff_basis'),
        exact_choice_time=context.get('cutoff_is_exact_choice_time'),
        choice_time_upper_bound=context.get('cutoff_is_choice_time_upper_bound'),
        availability_certified=context.get('availability_certified'))
    return dict(checkpoint_id=material['compiled']['target']['checkpoint_id'],
        event_id=event['event_id'], target_phase=event.get('phase'),
        target_record_clock=None, cutoff=cutoff,
        cutoff_limit='程序证据范围截点不等于已证明的临床可见时间；仅供本私有可行性页解释拆分。',
        prechoice_citations=prechoice,
        prechoice_coverage_note='只列出候选事件已保存且通过程序字符核验的“选择前证据”引文；不是全部既往病历，缺失不等于没有资料。',
        proposed_evidence_source_ids=sorted(linked_ids),
        included_original_source_ids=original_ids,
        material_partitions=partitions,
        projected_visible_materials=visible,
        projected_visible_source_ids=sorted(visible_ids),
        doctor_view_sha256=material['doctor_view']['view_sha256'],
        projection_status='SAVED_ALGORITHMIC_PROJECTION_NOT_CLINICIAN_APPROVED',
        projection_note='这是按当前材料分区生成的可见文本投影，不是已审定的正式盲评材料；HOLD、失败或空文本均保留实际状态。',
        material_summary=_material_summary(material))


def _event(material_set, event, proposal, checkpoint, provenance, original_index, review_proposal=None):
    event_id = event['event_id']
    if checkpoint is not None:
        target = checkpoint['compiled']['target']
        record_function = event.get('record_function')
    else:
        target, record_function = None, event.get('record_function')
    return dict(
        event_id=event_id,
        checkpoint_id=target['checkpoint_id'] if target else None,
        is_episode_checkpoint=checkpoint is not None,
        checkpoint_status=event.get('checkpoint_status'),
        management_question=_text(event.get('management_question')) or _text(proposal.get('management_question')),
        candidate_action=_text(event.get('action')) or _text(proposal.get('action')),
        record_function=record_function,
        selection_rationale=_text(event.get('rationale')) or _text(proposal.get('rationale')),
        rationale_provenance=provenance if not _text(event.get('rationale')) and _text(proposal.get('rationale'))
            else 'FROZEN_CANDIDATE_EVENT_FIELD' if _text(event.get('rationale')) else 'NOT_AVAILABLE',
        phase=_text(event.get('phase')) or _text(proposal.get('phase')),
        time_basis=event.get('time_basis'),
        choice_time=event.get('choice_time'),
        ordering=_clock(event, proposal, original_index),
        time_validation=deepcopy(event.get('time_validation')),
        original_time_proposal=deepcopy((event.get('time_validation') or {}).get('original_proposal')
            if isinstance(event.get('time_validation'), dict) else proposal.get('model_time_proposal')),
        uncertainty=deepcopy(event.get('uncertainty') or proposal.get('uncertainty') or []),
        uncertainty_dimensions=deepcopy(event.get('uncertainty_dimensions') or proposal.get('uncertainty_dimensions') or {}),
        structured_llm_judgments=_uncertainty_judgments(review_proposal or proposal),
        source_id=event.get('source_id'),
        citations=list(_citations(event)),
        automatic_material_state=checkpoint['state'] if checkpoint else 'NOT_A_SELECTED_CHECKPOINT',
        automatic_state_is_not_clinical_acceptance=True,
        human_or_clinical_status='NOT_ASSESSED',
    )


def build_episode_feasibility_review(material_set, sources, graph_audit_chain=None, *, case_id=None):
    """Build a local, full-original view; callers must keep it in private output."""
    validate_episode_material_set(material_set)
    if not isinstance(sources, list) or not sources:
        raise ValueError('FEASIBILITY_COMPLETE_ORIGINALS_REQUIRED')
    documents = {}
    for source in sources:
        source_id, text = source.get('source_id'), source.get('text')
        digest = text_sha256(text) if isinstance(text, str) else None
        if not _text(source_id) or not isinstance(text, str) or not text or source_id in documents:
            raise ValueError('FEASIBILITY_ORIGINAL_INVALID_OR_DUPLICATE')
        if digest not in (source.get('sha256'), source.get('source_sha256')):
            raise ValueError('FEASIBILITY_ORIGINAL_HASH_MISMATCH')
        documents[source_id] = dict(source_id=source_id, source_version=source.get('source_version'),
            text=text, sha256=text_sha256(text))
    graph = material_set['episode_reference']['graph_snapshot']
    episode = next(item for item in graph['episodes']
                   if item['episode_id'] == material_set['episode_reference']['episode_id'])
    checkpoints = {item['event_reference']['event_id'] if isinstance(item['event_reference'], dict)
                   else item['event_reference']: item for item in material_set['checkpoint_materials']}
    if set(checkpoints) != set(episode['checkpoint_ids']):
        raise ValueError('FEASIBILITY_EPISODE_CHECKPOINT_COVERAGE_MISMATCH')
    proposals, provenance = _proposal_index(graph_audit_chain, graph)
    review_proposals = _review_proposals(graph_audit_chain)
    events_by_id = {event['event_id']: event for event in graph['events']}
    checkpoint_by_id = {item['compiled']['target']['checkpoint_id']: item
                        for item in material_set['checkpoint_materials']}
    events = []
    for index, event_id in enumerate(episode['event_ids']):
        checkpoint = checkpoints.get(event_id)
        if checkpoint is not None and checkpoint['compiled']['target']['checkpoint_id'] != event_id:
            raise ValueError('FEASIBILITY_CHECKPOINT_IDENTITY_MISMATCH')
        events.append(_event(material_set, events_by_id[event_id], proposals.get(event_id, {}),
                     checkpoint, provenance, index, review_proposals.get(event_id)))
    for event in events:
        for citation in event['citations']:
            source = documents.get(citation['source_id'])
            if source is None or source['text'][citation['start']:citation['end']] != citation['quote']:
                raise ValueError('FEASIBILITY_CITATION_SPAN_MISMATCH')
        if event['is_episode_checkpoint']:
            event['checkpoint_context'] = _checkpoint_context(
                checkpoint_by_id[event['event_id']], documents)
            for citation in event['checkpoint_context']['prechoice_citations']:
                source = documents.get(citation['source_id'])
                if source is None or source['text'][citation['start']:citation['end']] != citation['quote']:
                    raise ValueError('FEASIBILITY_PRECHOICE_CITATION_SPAN_MISMATCH')
            event['material_review_judgment'] = _material_review_judgment(
                checkpoint_by_id[event['event_id']])
        else:
            event['checkpoint_context'] = None
            event['material_review_judgment'] = dict(available=False,
                unavailable_reason='该 Episode 内候选事件没有单独的 Checkpoint 材料链。', rounds=[])
    events.sort(key=lambda event: event['ordering']['order'])
    for index, event in enumerate(events, 1):
        event['display_sequence'] = index
        del event['ordering']['order']
    if any(event['ordering']['kind'] != 'EXPLICIT_CHOICE_TIME' for event in events):
        ordering_note = '本页顺序含记录时间／阶段／候选顺序代理；不是已证实的临床时间顺序。'
    else:
        ordering_note = '本页按程序接受的选择时间候选排序；仍需医生核对。'
    result = dict(format=FORMAT, viewer_version=VIEWER_VERSION,
        episode_id=episode['episode_id'], case_id=case_id,
        candidate_graph_sha256=canonical_sha256(graph), material_set_sha256=material_set['set_sha256'],
        candidate_episode_version=graph['graph_version'],
        episode_management_questions=deepcopy(episode.get('management_questions', [])),
        events=events, source_documents=list(documents.values()),
        feedback_form_version=FORM_VERSION, episode_feedback_fields=deepcopy(EPISODE_FIELDS),
        event_feedback_fields=deepcopy(EVENT_FIELDS), ordering_note=ordering_note,
        limitations=[
            '本页供私人研究性可行性评价：看Episode与决策点还原是否合理，不是正式盲评。',
            '开放完整原文仅供当前结构评价；不得把这份页面复用为未来信息隔离的盲评材料。',
            '管理问题、候选动作和选择理由来自冻结候选事件或上游LLM发现提案，不是独立临床认证。',
            '精确选择时刻缺失时使用有标记的代理排序；不补写或猜测真实时点。',
            '自动材料状态与结构合理性无关，不构成临床接受、医生答案或Reference。',
            '每个Checkpoint前单独展示已保存的选择前证据引文和材料投影；这些不是完整既往病历，也不是经医生审定的可见范围。',
            '结构判断和材料审核意见均标明其上游LLM来源；程序回执与模型判断分开呈现，不将失败或空投影补写成模型意见。',
        ],
        clinical_content_is_private=True, model_call_performed=False,
        **{key: False for key in GOVERNANCE_FALSE})
    result['review_sha256'] = canonical_sha256(result)
    validate_episode_feasibility_review(result)
    return result


def validate_episode_feasibility_review(review):
    if not isinstance(review, dict) or review.get('format') != FORMAT \
            or review.get('viewer_version') != VIEWER_VERSION:
        raise ValueError('FEASIBILITY_REVIEW_FORMAT_INVALID')
    if canonical_sha256({key: value for key, value in review.items() if key != 'review_sha256'}) \
            != review.get('review_sha256'):
        raise ValueError('FEASIBILITY_REVIEW_HASH_INVALID')
    if review.get('clinical_content_is_private') is not True or review.get('model_call_performed') is not False \
            or any(review.get(key) is not False for key in GOVERNANCE_FALSE):
        raise ValueError('FEASIBILITY_GOVERNANCE_CHANGED')
    if not review.get('events') or not review.get('source_documents'):
        raise ValueError('FEASIBILITY_CONTENT_REQUIRED')
    documents = {source['source_id']: source for source in review['source_documents']}
    if len(documents) != len(review['source_documents']):
        raise ValueError('FEASIBILITY_DUPLICATE_SOURCE')
    for source in documents.values():
        if text_sha256(source['text']) != source['sha256']:
            raise ValueError('FEASIBILITY_SOURCE_HASH_CHANGED')
    event_ids = set()
    for index, event in enumerate(review['events'], 1):
        if event['event_id'] in event_ids or event['display_sequence'] != index \
                or event.get('human_or_clinical_status') != 'NOT_ASSESSED' \
                or event['ordering']['kind'] not in ('EXPLICIT_CHOICE_TIME', 'RECORD_TIME_PROXY', 'PHASE_PROXY', 'CANDIDATE_ORDER_ONLY'):
            raise ValueError('FEASIBILITY_EVENT_SEQUENCE_INVALID')
        event_ids.add(event['event_id'])
        if event['ordering']['kind'] != 'EXPLICIT_CHOICE_TIME' and '代理' not in event['ordering']['display'] \
                and '未知' not in event['ordering']['display']:
            raise ValueError('FEASIBILITY_PROXY_NOT_LABELLED')
        if event.get('management_question') is None or event.get('candidate_action') is None:
            raise ValueError('FEASIBILITY_DECISION_EXPLANATION_INCOMPLETE')
        for citation in event['citations']:
            if documents[citation['source_id']]['text'][citation['start']:citation['end']] != citation['quote']:
                raise ValueError('FEASIBILITY_CITATION_CHANGED')
        if event['is_episode_checkpoint']:
            context = event.get('checkpoint_context')
            if not isinstance(context, dict) or context['event_id'] != event['event_id'] \
                    or context['projection_status'] != 'SAVED_ALGORITHMIC_PROJECTION_NOT_CLINICIAN_APPROVED' \
                    or context['prechoice_coverage_note'] != '只列出候选事件已保存且通过程序字符核验的“选择前证据”引文；不是全部既往病历，缺失不等于没有资料。' \
                    or context['material_summary']['clinical_release_permitted'] is not False:
                raise ValueError('FEASIBILITY_CHECKPOINT_CONTEXT_INVALID')
            for citation in context['prechoice_citations']:
                if documents[citation['source_id']]['text'][citation['start']:citation['end']] != citation['quote']:
                    raise ValueError('FEASIBILITY_PRECHOICE_CITATION_CHANGED')
            for source in context['material_partitions']:
                original = documents.get(source['source_id'])
                if original is None:
                    raise ValueError('FEASIBILITY_PARTITION_SOURCE_CHANGED')
                cursor = 0
                for span in source['segments']:
                    if span['start'] != cursor or original['text'][span['start']:span['end']] != span['text'] \
                            or span['disposition'] not in ('KEEP', 'MASK', 'REVIEW'):
                        raise ValueError('FEASIBILITY_PARTITION_CHANGED')
                    cursor = span['end']
                if cursor != len(original['text']):
                    raise ValueError('FEASIBILITY_PARTITION_INCOMPLETE')
            for source in context['projected_visible_materials']:
                if text_sha256(source['text']) != source['sha256'] or not source['text']:
                    raise ValueError('FEASIBILITY_PROJECTED_MATERIAL_CHANGED')
        elif event.get('checkpoint_context') is not None:
            raise ValueError('FEASIBILITY_NONCHECKPOINT_CONTEXT_INVALID')
    return True


def replay_episode_feasibility_review(review, saved_review, saved_html):
    """Compare a saved private viewer byte-for-byte; never mutate its inputs."""
    validate_episode_feasibility_review(review)
    if review != saved_review:
        raise ValueError('FEASIBILITY_REPLAY_REVIEW_MISMATCH')
    validate_episode_feasibility_review(saved_review)
    if render_episode_feasibility_html(saved_review) != saved_html:
        raise ValueError('FEASIBILITY_REPLAY_HTML_MISMATCH')
    return dict(review_sha256=review['review_sha256'],
        material_set_sha256=review['material_set_sha256'],
        candidate_graph_sha256=review['candidate_graph_sha256'],
        replay_verified=True, model_call_performed=False,
        review_json_reconstructed=True, html_reconstructed_byte_for_byte=True,
        formal_blind_packet_created=False, clinical_reference_created=False,
        doctor_answer_created=False, clinical_release_permitted=False)


def _json_script(value, element_id):
    return '<script type="application/json" id="' + element_id + '">' + json.dumps(value,
        ensure_ascii=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e') + '</script>'


def render_episode_feasibility_html(review):
    validate_episode_feasibility_review(review)
    esc = html.escape
    parts = ['''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Episode 可行性评价</title><style>
:root{--bg:#f3f5f4;--paper:#fff;--ink:#20312e;--muted:#64736d;--line:#dce3df;--teal:#14695c;--teal-soft:#e6f1ed;--amber:#8a570b;--amber-soft:#fff4d9;--highlight:#ffe49a}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#15211e;--paper:#1e2c28;--ink:#e5eee8;--muted:#a7b8af;--line:#3c4c44;--teal:#80c9b4;--teal-soft:#254c41;--amber:#e4bd78;--amber-soft:#493c24;--highlight:#76602b;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#15211e;--paper:#1e2c28;--ink:#e5eee8;--muted:#a7b8af;--line:#3c4c44;--teal:#80c9b4;--teal-soft:#254c41;--amber:#e4bd78;--amber-soft:#493c24;--highlight:#76602b;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;padding:0 max(16px,env(safe-area-inset-right,0px)) 48px max(16px,env(safe-area-inset-left,0px))}button,input,textarea,select{font:inherit}button{cursor:pointer}button:focus-visible,a:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid var(--teal);outline-offset:3px}.wrap{max-width:1600px;margin:auto}header{padding:28px 0 20px;border-bottom:1px solid var(--line)}.eyebrow{font-size:12px;letter-spacing:.1em;color:var(--teal);font-weight:700}h1{font-size:30px;line-height:1.35;margin:7px 0 12px}h2{font-size:21px;line-height:1.4}h3{font-size:17px;line-height:1.5}p{margin:8px 0}.subtitle{color:var(--muted);max-width:850px}.warning{padding:15px 18px;background:var(--amber-soft);border-left:4px solid var(--amber);margin:18px 0;border-radius:0 8px 8px 0}.notice{color:var(--muted);font-size:13px}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:18px 0}.primary{background:var(--teal);color:var(--paper);border:0;border-radius:7px;padding:11px 18px;font-weight:650}.secondary{background:var(--paper);color:var(--teal);border:1px solid var(--line);border-radius:7px;padding:9px 13px}.workspace{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr);gap:20px;align-items:start}.workspace>section{min-width:0}.timeline{position:relative;padding-left:23px;min-width:0}.timeline:before{content:"";position:absolute;left:6px;top:13px;bottom:20px;width:2px;background:var(--line)}.event{background:var(--paper);border:1px solid var(--line);border-radius:10px;margin-bottom:18px;position:relative;overflow:hidden}.event:before{content:"";position:absolute;left:-22px;top:28px;width:10px;height:10px;border:3px solid var(--teal);border-radius:50%;background:var(--paper)}.event-head{padding:18px 20px 12px}.event-top{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.seq{color:var(--teal);font-weight:750;font-size:20px}.tag{display:inline-block;border:1px solid var(--line);border-radius:5px;padding:2px 8px;color:var(--muted);font-size:12px}.tag.proxy{color:var(--amber);background:var(--amber-soft);border-color:transparent}.event h3{margin:12px 0 6px}.action{font-size:16px;font-weight:600}.explanation{padding:14px 20px;background:var(--teal-soft);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.label{font-size:12px;font-weight:700;color:var(--muted);letter-spacing:.035em}.meta{display:grid;gap:5px;padding:13px 20px;color:var(--muted);font-size:13px}.quote-list{padding:0 20px 10px}.quote{border-left:3px solid var(--teal);padding:7px 10px;margin:9px 0;background:var(--bg)}.quote p{font-size:14px}.feedback{border-top:1px solid var(--line);padding:12px 20px 18px}.feedback summary{cursor:pointer;font-weight:650;color:var(--teal)}.feedback-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}.field{min-width:0}.field.full{grid-column:1/-1}label{display:block;font-size:13px;font-weight:600;margin-bottom:5px}select,textarea{width:100%;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:9px}textarea{min-height:100px;resize:vertical}.source-panel{background:var(--paper);border:1px solid var(--line);border-radius:10px;position:sticky;top:16px;max-height:calc(100vh - 32px);display:flex;flex-direction:column;min-width:0}.source-head{padding:16px 18px;border-bottom:1px solid var(--line)}.source-head h2{margin:0 0 8px}.source-body{overflow:auto;padding:20px;white-space:pre-wrap;overflow-wrap:anywhere;font:14px/2 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;min-height:220px;flex:1}.source-body mark{background:var(--highlight);color:inherit;border-radius:2px;padding:2px 0}.source-body mark.current{outline:3px solid var(--teal)}.source-empty{color:var(--muted)}.context-tabs{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}.context-tabs button.active{background:var(--teal);color:var(--paper);border-color:var(--teal)}.context-nav{display:grid;gap:8px;margin:10px 0}.context-nav label{margin:0}.process-strip{display:grid;gap:8px;margin:16px 0}.process-step{display:grid;grid-template-columns:105px minmax(0,1fr);gap:12px;align-items:center;padding:10px 13px;border:1px solid var(--line);border-radius:8px;background:var(--paper);text-align:left;color:var(--ink);min-width:0}.process-step.current{border-color:var(--teal);background:var(--teal-soft)}.process-step .step-time{color:var(--muted);font-size:12px}.process-step strong,.process-step small{display:block;overflow-wrap:anywhere}.process-step small{color:var(--muted)}.checkpoint-context{border:1px solid var(--line);border-radius:10px;background:var(--paper);padding:15px 20px;margin:0 0 18px 23px;min-width:0;overflow-wrap:anywhere}.checkpoint-context h3{margin:0 0 8px}.context-status{color:var(--amber);font-size:13px}.llm-output{padding:14px 20px;border-top:1px solid var(--line)}.llm-output summary{cursor:pointer;color:var(--teal);font-weight:650}.dimension{padding:9px 0;border-bottom:1px solid var(--line)}.dimension:last-child{border-bottom:0}.dimension p{margin:3px 0}.material-judgment{margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}.judgment-round{margin-top:10px;padding:12px;background:var(--bg);border-radius:6px}.diff-list{padding-left:20px;color:var(--muted);font-size:13px}.source-foot{padding:10px 18px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}.source-foot p{margin:2px 0}.episode-review{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:20px;margin:20px 0}.episode-fields{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.limitations{margin:18px 0}.limitations li{margin:5px 0;color:var(--muted)}code{font-size:12px;overflow-wrap:anywhere}.export-output{white-space:pre-wrap;overflow-wrap:anywhere;max-height:400px;overflow:auto;padding:15px;background:var(--paper);border:1px solid var(--line);margin-top:14px;display:none}.status{font-size:13px;color:var(--teal)}.mobile-source{display:none}
@media(max-width:900px){.workspace{grid-template-columns:1fr}.source-panel{position:relative;top:0;max-height:75vh}.feedback-grid,.episode-fields{grid-template-columns:1fr}h1{font-size:25px}.source-panel{order:-1}.mobile-source{display:inline-block}.timeline{padding-left:17px}.event:before{left:-16px}.checkpoint-context{margin-left:17px}.process-step{grid-template-columns:1fr}}
@media print{body{background:white;color:black}.source-panel{position:relative;max-height:none}.feedback{display:none}.workspace{grid-template-columns:1fr}button{display:none}}
</style></head><body><div class="wrap"><header><div class="eyebrow">RESEARCH FEASIBILITY · 非正式盲评</div><h1>Episode 与决策点还原可行性评价</h1><p class="subtitle">请判断：这段诊疗过程是否被合理划分？哪里是真正的决策点？时点、粒度和原文解释是否需要修改？</p><p><strong>研究病例：</strong>''', esc(review.get('case_id') or '未提供'), '　<strong>Episode：</strong><code>', esc(review['episode_id']), '</code></p></header>',
    '<div class="warning"><strong>这不是正式盲评，也不生成治疗答案。</strong>右侧有意提供完整原始文件，便于核对候选结构；含选择后信息，不能作为未来信息隔离材料。自动状态不代表临床认可。</div>',
    '<p class="notice"><strong>阅读顺序：</strong>先看下方过程总览，再逐个阅读“本 Checkpoint 之前” → “当前候选决策点” → “LLM 判断与程序回执”。右侧可切换完整原文和该时点的可见材料投影。</p>',
    '<div class="warning"><strong>时间说明：</strong>', esc(review['ordering_note']), '</div>',
    '<section class="episode-review"><h2>这段 Episode 的过程总览</h2><p class="notice">每个节点都可定位到“该点之前”的资料。顺序含代理，不能据此补造缺失时点。</p><div class="process-strip">']
    for event in review['events']:
        parts.append('<button type="button" class="process-step" data-process-event="' + esc(event['event_id']) + '"><span class="step-time">' + str(event['display_sequence']) + ' · ' + esc(event['ordering']['display']) + '</span><span><strong>' + esc(event['management_question']) + '</strong><small>查看此点前的资料与当前候选动作</small></span></button>')
    parts.extend(['</div></section>',
    '<div class="toolbar"><button type="button" class="secondary" id="toggle-forms">展开／收起全部评价表</button><span class="notice">本页不联网；反馈先显示在本页底部，由研究者本机回收。</span></div>',
    '<main class="workspace"><section aria-label="逐Checkpoint之前资料与候选决策过程"><h2>逐点看：之前有什么，当前发生了什么</h2><p class="notice">“之前”按已保存的选择前引文及材料投影呈现，不声称覆盖全部既往病历。</p><div class="timeline">'])
    for event in review['events']:
        eid = esc(event['event_id'])
        if event['checkpoint_context'] is not None:
            context = event['checkpoint_context']
            summary = context['material_summary']
            parts.append('<section class="checkpoint-context" id="before-' + eid + '" data-context-event="' + eid + '"><div class="label">过程 ' + str(event['display_sequence']) + ' · 当前 Checkpoint 之前</div><h3>这个决策点前，已经保存了哪些资料？</h3>')
            parts.append('<p class="notice">' + esc(context['prechoice_coverage_note']) + '</p>')
            parts.append('<p class="context-status">资料范围截点：' + esc(str(context['cutoff']['value'] or '未确定')) + '；依据：' + esc(str(context['cutoff']['basis'] or '未提供')) + '。' + esc(context['cutoff_limit']) + '</p>')
            parts.append('<button type="button" class="secondary show-projection" data-event="' + eid + '">查看本点材料投影 ↗</button>')
            if not context['prechoice_citations']:
                parts.append('<p class="warning"><strong>没有已保存且通过字符核验的“选择前证据”引文。</strong>这不等于此前没有病历；请在右侧完整原文和材料投影中核对覆盖缺口。</p>')
            for citation in context['prechoice_citations']:
                index = event['citations'].index(citation)
                parts.append('<div class="quote"><div class="label">选择前证据 · ' + esc(citation['source_id']) + '</div><p>“' + esc(citation['quote']) + '”</p><button type="button" class="secondary locate" data-event="' + eid + '" data-citation="' + str(index) + '">查看完整原文并定位 ↗</button></div>')
            parts.append('<p class="notice">本点材料：' + esc(str(summary['state'])) + ' / ' + esc(str(summary['stop_reason'])) + '；已保存审核轮次：' + str(summary['review_round_count']) + '。' + esc(context['projection_note']) + '</p></section>')
        parts.append('<article class="event" id="event-' + eid + '" data-event="' + eid + '"><div class="event-head"><div class="event-top"><span class="seq">' + str(event['display_sequence']) + '</span>')
        parts.append('<span class="tag proxy">' + esc(event['ordering']['display']) + '</span>')
        parts.append('<span class="tag">' + ('候选 Checkpoint' if event['is_episode_checkpoint'] else 'Episode 内候选事件') + '</span></div>')
        parts.append('<div class="label" style="margin-top:12px">这个决策点要回答什么？</div><h3>' + esc(event['management_question']) + '</h3>')
        parts.append('<div class="label">病历记载的候选动作／记录内容（不是医生建议）</div><p class="action">' + esc(event['candidate_action']) + '</p>')
        parts.append('<p class="notice">记录功能：' + esc(event.get('record_function') or '未提供') + '；阶段：' + esc(event.get('phase') or '未提供') + '</p></div>')
        parts.append('<div class="explanation"><div class="label">为什么被还原为候选决策点？</div>')
        parts.append('<p>' + esc(event['selection_rationale'] or '上游未提供可展示的选择理由；请结合原文判断，不能把空缺解释为临床合理。') + '</p>')
        parts.append('<p class="notice">理由来源：' + ('上游 LLM 发现提案，非独立临床认证' if event['rationale_provenance'].startswith('UPSTREAM') else '冻结候选事件字段，非独立临床认证' if event['rationale_provenance'].startswith('FROZEN') else '未提供') + '</p></div>')
        parts.append('<div class="meta"><span>' + esc(event['ordering']['explanation']) + '</span><span>精确选择时刻：' + esc(event.get('choice_time') or '未证实') + '；time_basis：' + esc(event.get('time_basis') or '未提供') + '</span>')
        if event.get('original_time_proposal'):
            parts.append('<span>原始时间提议（未被程序接受为选择时刻）：<code>' + esc(json.dumps(event['original_time_proposal'], ensure_ascii=False)) + '</code></span>')
        if event.get('uncertainty'):
            parts.append('<span>不确定性：' + esc('；'.join(str(item) for item in event['uncertainty'])) + '</span>')
        parts.append('<span>自动材料状态：' + esc(event['automatic_material_state']) + '（与结构临床合理性无关）</span></div>')
        parts.append('<details class="llm-output"><summary>查看 LLM 的结构判断、材料复核与程序回执</summary><div class="label" style="margin-top:12px">发现／复核阶段 LLM 的结构判断</div><p class="notice">这是模型输出，不是已确认临床事实；引文支持仍须阅读右侧原文。</p>')
        if not event['structured_llm_judgments']:
            parts.append('<p class="notice">该事件没有可展示的结构化不确定性判断。</p>')
        for dimension in event['structured_llm_judgments']:
            parts.append('<div class="dimension"><strong>' + esc(dimension['dimension']) + ' · ' + esc(str(dimension['status'] or '未提供')) + '</strong><p>' + esc(dimension['explanation'] or '未提供解释') + '</p><span class="notice">原因类别：' + esc(str(dimension['reason_category'] or '未提供')) + '</span></div>')
        judgment = event['material_review_judgment']
        parts.append('<div class="material-judgment"><div class="label">材料复核 LLM 与程序执行</div>')
        if not judgment['available']:
            parts.append('<p class="notice">' + esc(judgment['unavailable_reason']) + '</p>')
        for round_ in judgment['rounds']:
            parts.append('<div class="judgment-round"><strong>第 ' + str(round_['round_index']) + ' 轮 · ' + esc(round_['status']) + '</strong>')
            for dimension in round_['dimensions']:
                parts.append('<div class="dimension"><strong>' + esc(dimension['dimension']) + ' · ' + esc(dimension['status']) + '</strong><p>' + esc(dimension['reason']) + '</p></div>')
            if round_['required_actions']:
                parts.append('<p>未决动作：' + esc('；'.join(round_['required_actions'])) + '</p>')
            parts.append('<p class="notice">模型提出操作 ' + str(round_['operation_count']) + ' 项；程序逐操作回执见下，不能把模型状态当作已执行结果。</p><ul class="diff-list">')
            for item in round_['ledger']['operations']:
                operation = item.get('operation', {})
                parts.append('<li>' + esc(str(operation.get('operation') or '未提供')) + ' → 程序：' + esc(str(item.get('status') or '未提供')) + ('；' + esc(str(item.get('reason'))) if item.get('reason') else '') + '</li>')
            if not round_['ledger']['operations']:
                parts.append('<li>本轮没有材料操作。</li>')
            parts.append('</ul><p class="notice">输入 compiled：<code>' + esc(round_['input_compiled_sha256']) + '</code><br>输出 compiled：<code>' + esc(round_['compiled_sha256']) + '</code><br>doctor view：<code>' + esc(round_['doctor_view_sha256']) + '</code></p></div>')
        parts.append('</div></details>')
        parts.append('<div class="quote-list"><div class="label">当前事件的全部已核验引文（含选择前、选择边界及选择后）</div>')
        if not event['citations']:
            parts.append('<p class="notice">没有通过程序校验的定位引文。请使用来源选择器核查完整原文。</p>')
        for index, citation in enumerate(event['citations']):
            parts.append('<div class="quote"><div class="label">' + esc(citation['role']) + '</div><p>“' + esc(citation['quote']) + '”</p><button type="button" class="secondary locate" data-event="' + eid + '" data-citation="' + str(index) + '">在原文中定位 ↗</button></div>')
        parts.append('</div><details class="feedback"><summary>评价这个决策点</summary><div class="feedback-grid">')
        for field in EVENT_FIELDS:
            name, label = field[0], field[1]
            full = ' full' if len(field) == 2 else ''
            parts.append('<div class="field' + full + '"><label for="' + name + '-' + eid + '">' + esc(label) + '</label>')
            if len(field) == 3:
                parts.append('<select data-field="' + name + '" id="' + name + '-' + eid + '">' + ''.join('<option>' + esc(option) + '</option>' for option in reversed(field[2])) + '</select>')
            else:
                parts.append('<textarea data-field="' + name + '" id="' + name + '-' + eid + '"></textarea>')
            parts.append('</div>')
        parts.append('</div></details></article>')
    parts.append('</div></section><aside class="source-panel" aria-label="Checkpoint前资料与原始文件对照"><div class="source-head"><h2>本点资料与原始文件</h2><p class="notice">先选择左侧 Checkpoint，再切换“之前可见投影”或“完整原文”。投影不是正式盲评包。</p><div class="context-nav"><label for="event-select">查看哪个 Checkpoint 之前？</label><select id="event-select">')
    for event in review['events']:
        if event['checkpoint_context'] is not None:
            parts.append('<option value="' + esc(event['event_id']) + '">' + str(event['display_sequence']) + ' · ' + esc(event['management_question']) + '</option>')
    parts.append('</select></div><div class="context-tabs"><button type="button" class="secondary" id="show-original">完整原文</button><button type="button" class="secondary active" id="show-visible">之前可见投影</button></div><label for="source-select">选择来源文件</label><select id="source-select">')
    all_sources = {source['source_id']: source for source in review['source_documents']}
    for event in review['events']:
        if event['checkpoint_context'] is not None:
            for source in event['checkpoint_context']['projected_visible_materials']:
                all_sources[source['source_id']] = source
    for source_id in sorted(all_sources):
        source = all_sources[source_id]
        parts.append('<option value="' + esc(source_id) + '">' + esc(source_id) + ' · ' + esc(source.get('source_version') or '未标版本') + '</option>')
    parts.append('</select></div><div id="source-body" class="source-body"><span class="source-empty">请选择左侧“本 Checkpoint 之前”或当前事件的引文。</span></div><div class="source-foot"><p id="context-caption">尚未选择 Checkpoint。</p><p id="context-warning">本私有页同时保留完整原文与程序投影；二者不可混称。</p></div></aside></main>')
    parts.append('<section class="episode-review"><h2>评价整个 Episode</h2><p class="notice">这里评价结构本身，不填写正确治疗方案。</p><div class="episode-fields">')
    for field in EPISODE_FIELDS:
        name, label = field[0], field[1]
        full = ' full' if len(field) == 2 else ''
        parts.append('<div class="field' + full + '"><label for="episode-' + name + '">' + esc(label) + '</label>')
        if len(field) == 3:
            parts.append('<select id="episode-' + name + '">' + ''.join('<option>' + esc(option) + '</option>' for option in reversed(field[2])) + '</select>')
        else:
            parts.append('<textarea id="episode-' + name + '"></textarea>')
        parts.append('</div>')
    parts.append('</div></section><section><h2>解释边界</h2><ul class="limitations">')
    parts.extend('<li>' + esc(limitation) + '</li>' for limitation in review['limitations'])
    parts.append('</ul><label for="reviewer-declaration">实际反馈人声明（由反馈人填写，程序不预填）</label><textarea id="reviewer-declaration"></textarea><p style="margin-top:16px"><button class="primary" type="button" id="export">生成研究性反馈 JSON</button> <span id="export-status" class="status" role="status"></span></p><p class="notice">按钮只在本页生成文本，不上传、不声称已提交。请复制下方 JSON 交回研究者。</p><pre tabindex="0" id="export-output" class="export-output"></pre></section><footer class="notice"><p>Viewer：<code>' + esc(VIEWER_VERSION) + '</code><br>Candidate graph：<code>' + esc(review['candidate_graph_sha256']) + '</code><br>Review binding：<code>' + esc(review['review_sha256']) + '</code></p></footer></div>')
    parts.append(_json_script(review, 'review-data'))
    parts.append(r'''<script>
(()=>{
 const review=JSON.parse(document.getElementById('review-data').textContent);
 const sources=new Map(review.source_documents.map(s=>[s.source_id,s]));
 const events=new Map(review.events.map(e=>[e.event_id,e]));
 const contexts=new Map(review.events.filter(e=>e.checkpoint_context).map(e=>[e.event_id,e.checkpoint_context]));
 let activeCitation=null, activeEvent=contexts.keys().next().value||null, viewMode='projection', activeSource=null;
 const body=document.getElementById('source-body');
 const select=document.getElementById('source-select');
 const eventSelect=document.getElementById('event-select');
 const caption=document.getElementById('context-caption');
 const warning=document.getElementById('context-warning');
 function updateSourceOptions(){
  const context=contexts.get(activeEvent),originals=new Map(review.source_documents.map(s=>[s.source_id,s]));
  let options=viewMode==='projection'&&context?context.projected_visible_materials:[...originals.values()];
  select.replaceChildren();options.forEach(s=>{const option=document.createElement('option');option.value=s.source_id;option.textContent=s.source_id+' · '+(s.source_version||'未标版本');select.append(option);});
  activeSource=options.length?options[0].source_id:null;
  if(options.length)select.value=activeSource;
  select.disabled=!options.length;
 }
 function setMode(mode){viewMode=mode;document.getElementById('show-original').classList.toggle('active',mode==='original');document.getElementById('show-visible').classList.toggle('active',mode==='projection');updateSourceOptions();renderActiveSource();}
 function setEvent(eventId,mode='projection'){
  if(!contexts.has(eventId))return;activeEvent=eventId;eventSelect.value=eventId;
  document.querySelectorAll('.process-step').forEach(x=>x.classList.toggle('current',x.dataset.processEvent===eventId));
  setMode(mode);
 }
 function renderActiveSource(){
  const context=contexts.get(activeEvent),original=sources.get(activeSource),projection=context&&context.projected_visible_materials.find(s=>s.source_id===activeSource);
  const source=viewMode==='original'?original:projection;
  body.textContent=source?source.text:'当前没有可显示的非空材料投影。此空缺不代表原始病历不存在；请切换完整原文或查看本点材料失败原因。';
  activeCitation=null;
  if(!context){caption.textContent='该候选事件没有单独Checkpoint材料链。';warning.textContent='完整原文仅供结构可行性评价。';return;}
  caption.textContent='过程 '+events.get(activeEvent).display_sequence+' · '+events.get(activeEvent).management_question;
  warning.textContent=viewMode==='original'?'完整原文：可能包含当前选择及选择后信息，不是该时点可见材料。':context.projection_note+' doctor_view：'+context.doctor_view_sha256;
 }
 function renderSource(sourceId,citation){
  viewMode='original';document.getElementById('show-original').classList.add('active');document.getElementById('show-visible').classList.remove('active');
  const source=sources.get(sourceId);if(!source)return;
  activeSource=sourceId;updateSourceOptions();select.value=sourceId;
  body.textContent=source.text.slice(0,citation.start);const mark=document.createElement('mark');mark.textContent=citation.quote;mark.className='current';body.append(mark,document.createTextNode(source.text.slice(citation.end)));activeCitation=citation;
  const context=contexts.get(activeEvent);caption.textContent='完整原文 · '+(context?'过程 '+events.get(activeEvent).display_sequence:'候选事件');
  warning.textContent='当前为完整原文，可能包含当前选择及选择后信息；不能当作该时点可见投影。';
  requestAnimationFrame(()=>{const current=body.querySelector('mark');if(current)body.scrollTop=Math.max(0,current.offsetTop-body.clientHeight/3);});
 }
 document.querySelectorAll('.locate').forEach(button=>button.addEventListener('click',()=>{
  const citation=events.get(button.dataset.event).citations[Number(button.dataset.citation)];
  renderSource(citation.source_id,citation);
  if(window.innerWidth<=900)document.querySelector('.source-panel').scrollIntoView({behavior:'smooth',block:'start'});
 }));
 select.addEventListener('change',()=>{activeSource=select.value;renderActiveSource();});
 document.getElementById('show-original').addEventListener('click',()=>setMode('original'));
 document.getElementById('show-visible').addEventListener('click',()=>setMode('projection'));
 eventSelect.addEventListener('change',()=>setEvent(eventSelect.value));
 document.querySelectorAll('.show-projection').forEach(button=>button.addEventListener('click',()=>{setEvent(button.dataset.event);if(window.innerWidth<=900)document.querySelector('.source-panel').scrollIntoView({behavior:'smooth',block:'start'});}));
 document.querySelectorAll('[data-process-event]').forEach(button=>button.addEventListener('click',()=>{setEvent(button.dataset.processEvent);document.getElementById('before-'+button.dataset.processEvent).scrollIntoView({behavior:'smooth',block:'start'});}));
 if(activeEvent)setEvent(activeEvent);else setMode('original');
 document.getElementById('toggle-forms').addEventListener('click',()=>{const forms=[...document.querySelectorAll('details.feedback')];const open=forms.some(x=>!x.open);forms.forEach(x=>x.open=open);});
 document.getElementById('export').addEventListener('click',()=>{
  const episode={};review.episode_feedback_fields.forEach(f=>episode[f[0]]=document.getElementById('episode-'+f[0]).value);
  const events=review.events.map(e=>{const response={event_id:e.event_id,checkpoint_id:e.checkpoint_id};review.event_feedback_fields.forEach(f=>response[f[0]]=document.querySelector('[data-event="'+e.event_id+'"] [data-field="'+f[0]+'"]').value);return response;});
  const result={format:'episode-feasibility-feedback-response-0.1.0',review_sha256:review.review_sha256,material_set_sha256:review.material_set_sha256,candidate_graph_sha256:review.candidate_graph_sha256,feedback_form_version:review.feedback_form_version,episode_id:review.episode_id,episode_feedback:episode,event_feedback:events,reviewer_declaration:document.getElementById('reviewer-declaration').value,formal_blind_packet_created:false,clinical_reference_created:false,doctor_answer_created:false};
  const output=document.getElementById('export-output');output.textContent=JSON.stringify(result,null,2);output.style.display='block';document.getElementById('export-status').textContent='已在本页生成，尚未上传或提交。';output.scrollIntoView({behavior:'smooth',block:'start'});
 });
})();
</script></body></html>''')
    return ''.join(parts)
