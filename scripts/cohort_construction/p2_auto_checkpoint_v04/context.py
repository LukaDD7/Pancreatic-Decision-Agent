"""Whole-document discovery and source-bound temporal evidence contexts.

Document types are routing priors, never semantic event labels. Full source text
is not sentence-split, rewritten or truncated. Every omission has a ledger entry.
Recorded/exam time bounds retrieval but does not certify clinical availability.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from scripts.cohort_construction.p2_pathway_replay.core import (
    clock as parse_clock, available_source_clock, _structured_clock, relation_to_cutoff,
)
from .source_time import source_record_time


_OPERATION = re.compile(r'手术时间[：:]\s*(20\d\d-\d\d-\d\d\s+\d\d:\d\d)\s*-\s*(20\d\d-\d\d-\d\d\s+\d\d:\d\d)')
# Only explicit administrative/support types are removed from primary discovery.
_SUPPORT_TITLE = re.compile(r'护理|健康教育|知情同意|告知书|协议书|使用物品|物品清单|交接核查|交接核对|Apache|APACHE|评分|入院介绍|血气及酸碱分析报告粘贴')
_TYPE_PRIORS = {
    'operative': '探查后可能确认推进、调整或终止；仍需明确所见和选择，执行步骤不自动成为决策。',
    'preoperative_summary': '常汇总既定方案；整篇核对此前安排，允许新增条件、延期或真正再评估。',
    'preoperative_discussion': '多人员讨论可能承诺、确认或重述；不要按发言和术式字段重复建事件。',
    'round': '同次查房先组织完整状态和管理问题，再判断新增安排、再评估或延续。',
    'admission_or_first_course': '区分既往历史与当前初始检查/治疗安排。',
    'postoperative_course': '可能回述术中选择，也可能有当前状态驱动处置；不能将记录时间当术中选择时间。',
    'transfer': '区分转入转出本身、补记与其后管理；记录时间不是必然选择时间。',
    'clinical_request_or_assessment': '申请、会诊、拔管、输血等可有明确选择，无须再次出现肿瘤关键词。',
    'discharge_summary': '通常汇总已发生过程，区分当前出院安排与先前选择回述。',
    'clinical_other': '未知临床类型仍阅读全文，不以词规则未命中判断没有选择。',
    'support': '保留来源和上下文，不作为主要决策发现入口。',
    'performed_test_report': '报告是检查后证据；此前检查选择的具体时间和理由可能未知，不能用本报告解释选择它。',
}
_SUPPORT_DISCOVERY_PRIOR = '仅为文书类型提示，不是研究相关性或管理判断存在性的门槛；阅读全文识别患者特异的管理判断，也允许返回零事件。'


def _clock(value):
    return _structured_clock(value)


def time_order(value, cutoff):
    """Exact, precision-aware relation shared with source retrieval/assessment."""
    relation = relation_to_cutoff(value, cutoff)
    return 'UNKNOWN' if relation == 'UNKNOWN_TIME' else relation


def source_clock(source):
    return source_record_time(source)['clock']


def document_profile(source):
    title, text = source.get('title') or '', source.get('text') or ''
    if _SUPPORT_TITLE.search(title):
        return 'support'
    if not text.strip():
        return 'support'
    if re.search(r'术后.*(?:病程|记录)|术后首|术后第', title):
        return 'postoperative_course'
    if '出院小结' in title:
        return 'discharge_summary'
    if re.search(r'转入|转出|入室|出室', title):
        return 'transfer'
    if re.search(r'手\s*术\s*记\s*录', text[:100]) or '手术记录' in title:
        return 'operative'
    if '术前小结' in title:
        return 'preoperative_summary'
    if '术前讨论' in title:
        return 'preoperative_discussion'
    if re.search(r'查房', title):
        return 'round'
    if re.search(r'首次病程|入院记录', title):
        # Truly blank exported templates remain source context. A real initial
        # plan is retained even if a history field happens to be a placeholder.
        if '[现病史]' in text and '[辅助检查]' in text and not re.search(r'拟行|决定|暂缓|检查安排[：:]\s*[^２2\s]|治疗计划[：:]\s*[^。\s]', text):
            return 'support'
        return 'admission_or_first_course'
    if re.search(r'申请|会诊|拔除|拔管|输血|评估', title):
        return 'clinical_request_or_assessment'
    if title.strip() in ('胰腺肿瘤手术', '手术患者交接核查表NEW'):
        return 'support'
    return 'clinical_other'


def _primary_document(source):
    return source.get('kind') == 'document' and document_profile(source) != 'support'


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _source_hash(source):
    return source.get('source_sha256') or _sha(source.get('text', ''))


def _raw(source):
    timing = source_record_time(source)
    raw = dict(source_id=source['source_id'], source_sha256=_source_hash(source),
                text=source.get('text', ''), title=source.get('title') or '',
                kind=source.get('kind'), clock=timing['clock'],
                clock_basis=timing['clock_basis'],
                document_profile=document_profile(source) if source.get('kind') == 'document' else source.get('kind'),
                availability_certified=False)
    if timing['status'] not in ('STRUCTURED_CLOCK_PRESERVED', 'NOT_APPLICABLE'):
        raw['record_time_provenance'] = timing
    return raw


def _catalog(source, relation=None):
    item = {key: value for key, value in _raw(source).items() if key != 'text'}
    item['text_characters'] = len(source.get('text', ''))
    if relation is not None:
        item['relation_to_cutoff'] = relation
    return item


def _exam_number(source):
    value = (source.get('provenance') or {}).get('exam_no_raw')
    if value is None:
        value = (source.get('original_fields') or {}).get('检查号')
    return str(value).strip() if value is not None and str(value).strip().lower() not in ('', 'nan', 'none', 'null') else None


def _unit_hints(source):
    hints = []
    op = _OPERATION.search(source.get('text', ''))
    if op:
        hints.append(dict(kind='operation_interval_not_choice_time', start=op[1], end=op[2],
                          choice_minute_known=False))
    if '术前讨论' in (source.get('title') or ''):
        match = re.search(r'讨论日期[：:]\s*(20\d\d)年(\d{1,2})月(\d{1,2})日', source.get('text', ''))
        if match and source_clock(source)['value']:
            day = f'{int(match[1]):04}-{int(match[2]):02}-{int(match[3]):02}'
            if day != source_clock(source)['value'][:10]:
                hints.append(dict(kind='conflicting_record_and_discussion_date', body_discussion_date=day,
                                  record_clock=source_clock(source), resolved=False))
    return hints


def build_units(sources, config=None, *, routing_audit=None):
    """Return whole primary document units and same-test report variant groups.

    Support forms remain context-only by default, preserving the frozen v0.5
    routing. ``include_support_documents=True`` explicitly adds every nonempty
    support document as a whole discovery unit. Optional ``routing_audit`` is a
    caller-owned list recording per-source routing, including empty documents.
    No topic/action keyword threshold is applied.
    """
    include_support = (config or {}).get('include_support_documents', False)
    if not isinstance(include_support, bool):
        raise ValueError('include_support_documents must be a boolean')
    if routing_audit is not None and not isinstance(routing_audit, list):
        raise TypeError('routing_audit must be a list or None')
    routing_start = len(routing_audit) if routing_audit is not None else 0
    units, imaging_groups = [], {}
    for source in sources:
        document = source.get('kind') == 'document'
        nonempty = bool((source.get('text') or '').strip())
        profile = document_profile(source) if document else source.get('kind')
        route = dict(source_id=source['source_id'], source_kind=source.get('kind'),
                     document_profile=profile, source_characters=len(source.get('text') or ''))
        if document and not nonempty:
            route.update(disposition='EMPTY_DOCUMENT_NOT_DISCOVERED', unit_id=None,
                         reason='EMPTY_OR_WHITESPACE_SOURCE_TEXT')
        elif _primary_document(source) or (document and nonempty and include_support):
            profile = document_profile(source)
            unit_id = 'DU'+_sha(source['source_id'])[:20]
            units.append(dict(unit_id=unit_id, source_ids=[source['source_id']],
                              kind='document', document_profile=profile,
                              type_prior=_SUPPORT_DISCOVERY_PRIOR if profile == 'support' else _TYPE_PRIORS[profile],
                              clock=source_clock(source), clock_basis=_raw(source)['clock_basis'],
                              original_texts=[_raw(source)], hints=_unit_hints(source)))
            route.update(disposition='SUPPORT_DOCUMENT_INCLUDED' if profile == 'support' else 'PRIMARY_DOCUMENT_INCLUDED',
                         unit_id=unit_id, reason='EXPLICIT_INCLUDE_SUPPORT_DOCUMENTS' if profile == 'support' else 'WHOLE_CLINICAL_DOCUMENT')
        elif source.get('kind') == 'imaging':
            exam = _exam_number(source)
            exam_clock = _clock((source.get('clocks') or {}).get('exam_time') or source.get('clock'))
            key = (exam, exam_clock['value'], source.get('title')) if exam else ('unknown_exam_number', source['source_id'])
            imaging_groups.setdefault(key, []).append(source)
            route.update(disposition='IMAGING_CANDIDATE_GROUP_MEMBER', unit_id=None,
                         reason='EXACT_EXAM_NUMBER_TIME_TITLE_GROUPING')
        else:
            route.update(disposition='SUPPORT_DOCUMENT_CONTEXT_ONLY' if document else 'NON_DISCOVERY_SOURCE',
                         unit_id=None, reason='SUPPORT_DOCUMENTS_DISABLED' if document else 'SOURCE_KIND_NOT_DISCOVERY_UNIT')
        if routing_audit is not None:
            routing_audit.append(route)
    for key, group in imaging_groups.items():
        source = group[0]
        upper = _clock((source.get('clocks') or {}).get('exam_time') or source.get('clock'))
        ids = sorted(s['source_id'] for s in group)
        units.append(dict(unit_id='TU'+_sha(json.dumps(ids))[:20], source_ids=ids,
                          same_test_source_ids=ids, kind='performed_test_report',
                          document_profile='performed_test_report', type_prior=_TYPE_PRIORS['performed_test_report'],
                          clock=upper, clock_basis=_raw(source)['clock_basis'],
                          choice_time=None, time_upper_bound=upper,
                          original_texts=[_raw(s) for s in group],
                          hints=[dict(kind='report_supports_earlier_test_occurrence_not_choice_time',
                                      exam_number=_exam_number(source), selection_time_known=False,
                                      own_results_forbidden_as_prechoice=True)]))
    if routing_audit is not None:
        source_units = {sid: unit['unit_id'] for unit in units for sid in unit['source_ids']}
        for route in routing_audit[routing_start:]:
            route['unit_id'] = source_units.get(route['source_id'])
    return sorted(units, key=lambda u: (u['clock'].get('value') or '9999', u['unit_id']))


def _compress_lab_catalog(catalog, index_path):
    # Count by exact report/source time + title, then store titles together under
    # their shared time. Per-cell provenance and repeated lookup boilerplate
    # would otherwise dwarf the complete clinical documents in discovery.
    nonlab, times = [], {}
    for item in catalog:
        if item.get('kind') != 'lab':
            nonlab.append(item)
            continue
        key = (item['clock'].get('value'), item.get('relation_to_cutoff'))
        times.setdefault(key, []).append(item)
    title_groups = 0
    original_count = 0
    for key, members in times.items():
        counts = {}
        for item in members:
            title = item.get('title') or ''
            counts[title] = counts.get(title, 0) + 1
        title_groups += len(counts)
        original_count += len(members)
        nonlab.append(dict(kind='lab_catalog_group', group_id='LC'+_sha(json.dumps(key, ensure_ascii=False))[:16],
                           clock=members[0]['clock'], relation_to_cutoff=key[1], source_count=len(members),
                           title_counts=counts, results_included=False, source_id_index_path=index_path,
                           availability_certified=False,
                           lookup_selector=dict(source_kind='lab', source_clock_value=key[0], titles=list(counts))))
    return nonlab, dict(original_lab_source_count=original_count,
                        compressed_lab_group_count=title_groups, serialized_time_groups=len(times),
                        grouping='exact source/report clock + title counted; titles stored under shared clock',
                        results_rewritten=False, source_id_index_path=index_path)


def unit_comparison_boundary(unit):
    """Conservative retrieval boundary; never an invented choice timestamp.

    A late-written operation record can follow ICU or postoperative records.
    For exploration-phase reconstruction, use the original operation start as
    the external-context boundary. Sources during that interval are unordered.
    When an interval is absent, only an earlier day can safely be offered; the
    record's minute must not certify a within-day clinical order.
    """
    if unit.get('document_profile') != 'operative':
        return _clock(unit.get('clock')), 'source_clock_proxy'
    intervals = [h for h in unit.get('hints', [])
                 if h.get('kind') == 'operation_interval_not_choice_time']
    starts = [h.get('start') for h in intervals if _clock(h.get('start'))['value']]
    if not starts:
        for source in unit.get('original_texts', []):
            match = _OPERATION.search(source.get('text', ''))
            if match:
                starts.append(match[1])
    parsed = {_clock(value)['value']: _clock(value) for value in starts}
    if len(parsed) == 1:
        return next(iter(parsed.values())), 'operation_start_external_boundary_not_choice_time'
    recorded = _clock(unit.get('clock'))
    day = _clock(recorded['value'][:10]) if recorded['value'] else _clock(None)
    return day, ('conflicting_operation_intervals_day_only' if len(parsed) > 1
                 else 'operation_interval_missing_record_day_only')


def context_for_discovery(unit, sources, budget=80000):
    """Current complete raw unit + recent full notes + earlier source catalog.

    Earlier admission/operation/transition notes supply broader surgical context
    without requiring a particular treatment keyword. Unknown/same-day times are
    cataloged for review, not asserted to be earlier or exposed as prior text.
    """
    cutoff, cutoff_basis = unit_comparison_boundary(unit)
    current_ids = set(unit['source_ids'])
    previous, catalog, review_catalog, excluded = [], [], [], []
    excluded_labs = 0
    index_path = unit.get('source_id_index_path', 'source_fhir_r4_bundle.json#/entry')
    for source in sources:
        if source['source_id'] in current_ids:
            continue
        visible_clock, _ = _available_clock(source)
        relation = time_order(visible_clock, cutoff)
        if relation == 'BEFORE':
            catalog.append(_catalog(source, relation))
            if _primary_document(source):
                previous.append(source)
        elif relation == 'AFTER':
            if source.get('kind') == 'lab':
                excluded_labs += 1
            else:
                excluded.append(dict(source_id=source['source_id'], disposition='EXCLUDE_FUTURE_METADATA_ONLY'))
        else:
            review_catalog.append(_catalog(source, relation))
    previous.sort(key=lambda s: (source_clock(s)['value'], s['source_id']), reverse=True)
    recent = previous[:4]
    recent_ids = {s['source_id'] for s in recent}
    background = [s for s in previous if s['source_id'] not in recent_ids and
                  document_profile(s) in ('operative', 'admission_or_first_course', 'transfer')]
    used = sum(len(x['text']) for x in unit['original_texts'])
    kept_recent, kept_background, omissions = [], [], []
    for category, selected in [('recent_clinical', recent), ('background_clinical', background)]:
        for source in selected:
            n = len(source.get('text', ''))
            if used + n > budget:
                omissions.append(dict(source_id=source['source_id'], disposition='omitted_budget',
                                      category=category, source_characters=n, full_source_not_truncated=True))
            else:
                (kept_recent if category == 'recent_clinical' else kept_background).append(_raw(source))
                used += n
    catalog, compression = _compress_lab_catalog(catalog, index_path)
    review_catalog, review_compression = _compress_lab_catalog(review_catalog, index_path)
    if excluded_labs:
        excluded.append(dict(kind='lab_catalog_group', disposition='EXCLUDE_FUTURE_METADATA_ONLY',
                             source_count=excluded_labs, source_id_index_path=index_path))
    current_over_budget = sum(len(x['text']) for x in unit['original_texts']) > budget
    if current_over_budget:
        omissions.append(dict(disposition='CURRENT_UNIT_OVER_BUDGET_RETAINED_NOT_TRUNCATED',
                              category='current_unit', source_ids=list(unit['source_ids']),
                              source_characters=sum(len(x['text']) for x in unit['original_texts']),
                              full_source_not_truncated=True, requires_explicit_budget_waiver=True))
    return dict(**({'comparison_boundary_basis': cutoff_basis} if unit.get('document_profile') == 'operative' else {}),
                current_unit=deepcopy(unit), prior_clinical_documents=kept_recent,
                background_clinical_documents=kept_background, prior_source_catalog=catalog,
                uncertain_time_source_catalog=review_catalog, source_ledger=excluded + omissions,
                catalog_compression=dict(prior=compression, uncertain=review_compression,
                                         index_lookup='source-clock-and-provenance extension source_id; use group lookup_selector'),
                cutoff=cutoff, availability_certified=False,
                budget=dict(limit=budget, text_characters=used, current_unit_over_budget=used > budget,
                            omitted_source_count=len(omissions), complete_sources_not_truncated=True,
                            current_unit_budget_waiver_required=current_over_budget),
                uncertainty=dict(prechoice_state_certified=False, source_time_is_availability_proxy=True,
                                 context_coverage_complete=not omissions,
                                 current_unit_over_budget=current_over_budget,
                                 prior_note_clock_is_only_before_test_bound=unit.get('kind') == 'performed_test_report'))


def _available_clock(source):
    _, value, basis, _ = available_source_clock(
        source, document_record_time=source_record_time)
    return value, basis


def _citation_entries(event, field):
    value = event.get(field) or {}
    return value.get('valid', []) if isinstance(value, dict) else value


def _range_valid(citation, indexed):
    source = indexed.get(citation.get('source_id'))
    start, end = citation.get('start'), citation.get('end')
    if source is None or not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
        return False
    text = source.get('text', '')
    return (0 <= start < end <= len(text) and text[start:end] == citation.get('quote') and
            (not citation.get('source_sha256') or citation['source_sha256'] == _sha(text)))


def choice_boundary_for_source(event, source, indexed=None):
    """Use source-supported semantic choice clauses separately from node proof.

    An action-support quote may contain both the reason and the choice.  Its
    start is therefore only a conservative fallback.  Explicit choice clauses
    may be disjoint: preserve the original prefix before the last clause that is
    contained in a valid node proof, subtracting *every* exact choice clause.
    Additional clauses outside the node proof can only remove text, never
    expand the prefix; a bad secondary proof does not erase valid boundaries.
    This retains an intervening state
    (for example a special blood group) without exposing an earlier surgery
    choice.  Exact offsets validate source support, not the model's semantics;
    ``certified`` here never certifies a blind replay packet.
    """
    indexed = indexed or {source['source_id']: source}
    sid = source['source_id']
    actions = [q for q in _citation_entries(event, 'action_citations')
               if q.get('source_id') == sid and _range_valid(q, indexed)]
    fallback = min((q['start'] for q in actions), default=None)
    result = dict(start=fallback, exclusions=[], citations=[], warnings=[], certified=False,
                  basis='conservative_action_support_start',
                  reason='MISSING_SEMANTIC_CHOICE_BOUNDARY')
    supplied = event.get('choice_boundary_citations') or {}

    def belongs(entry):
        original = entry.get('citation') or {}
        return entry.get('source_id') == sid or original.get('source_id') == sid

    if isinstance(supplied, dict):
        if any(belongs(q) for q in supplied.get('ambiguous', [])):
            result['reason'] = 'AMBIGUOUS_SEMANTIC_CHOICE_BOUNDARY'
            return result
        if any(belongs(q) for q in supplied.get('invalid', [])):
            result['reason'] = 'INVALID_SEMANTIC_CHOICE_BOUNDARY'
            return result
    boundaries = [q for q in _citation_entries(event, 'choice_boundary_citations')
                  if q.get('source_id') == sid]
    if not boundaries:
        return result
    if any(not _range_valid(q, indexed) for q in boundaries):
        result['reason'] = 'INVALID_SEMANTIC_CHOICE_BOUNDARY'
        return result
    unique = {(q['start'], q['end']): q for q in boundaries}
    boundaries = [unique[key] for key in sorted(unique)]
    if any(left['end'] > right['start'] for left, right in zip(boundaries, boundaries[1:])):
        result['reason'] = 'OVERLAPPING_SEMANTIC_CHOICE_BOUNDARIES'
        return result
    contained = [q for q in boundaries if any(
        a['start'] <= q['start'] and q['end'] <= a['end'] for a in actions)]
    uncontained = [q for q in boundaries if q not in contained]
    exclusions = [dict(start=q['start'], end=q['end']) for q in boundaries]
    warnings = [dict(source_id=sid, start=q['start'], end=q['end'],
                     reason='CHOICE_CLAUSE_OUTSIDE_NODE_PROOF_EXCLUDED_ONLY',
                     can_expand_context=False) for q in uncontained]
    if not contained:
        result.update(exclusions=exclusions, citations=deepcopy(boundaries), warnings=warnings,
                      reason='CHOICE_BOUNDARY_NOT_CONTAINED_IN_VALID_NODE_PROOF')
        return result
    return dict(start=max(q['start'] for q in contained), exclusions=exclusions,
                citations=deepcopy(boundaries), warnings=warnings, certified=True,
                basis='source_supported_disjoint_semantic_choice_clauses',
                reason='SEMANTIC_ROLE_NOT_INDEPENDENTLY_CERTIFIED')


def _block(source, start, end, interval, basis, priority):
    text = source.get('text', '')
    anchor = dict(source_id=source['source_id'], source_sha256=_source_hash(source),
                  start=start, end=end, quote=text[start:end])
    return dict(alias=source['source_id'], source_id=source['source_id'], kind=source.get('kind'),
                title=source.get('title') or '', clock=source_clock(source), clock_basis=basis,
                interval=interval, text=anchor['quote'], quote=anchor['quote'], start=start, end=end, anchor=anchor,
                availability_certified=False,
                availability_status=('CURRENT_SEMANTIC_PRECHOICE_SPAN_UNCERTIFIED'
                    if interval.startswith('current_prechoice') else
                    'BEFORE_TEST_UPPER_BOUND_ONLY_UNCERTIFIED'
                    if interval == 'before_test_upper_bound_not_certified_prechoice' else
                    'VERIFIED_VISIBILITY_CLOCK_UNCERTIFIED' if basis == 'verified_time_proxy' else
                    'REPORT_TIME_PROXY_UNCERTIFIED' if basis == 'report_time_proxy' else
                    'SOURCE_TIME_PROXY_UNCERTIFIED'),
                admissibility_role='candidate_prechoice_source_not_certified_observation',
                priority=priority)


def is_operative_phase(event):
    """Recognize an intraoperative phase without matching pre/postoperative.

    The model's explicit time basis takes precedence. Free-text phase labels
    still need token boundaries: preoperative/postoperative/perioperative are
    not evidence that a current choice occurred during exploration.
    """
    if event.get('time_basis') == 'operative_phase':
        return True
    phase = re.sub(r'[_-]+', ' ', str(event.get('phase') or event.get('clinical_phase') or '').lower())
    if re.search(r'\b(?:preoperative|postoperative|perioperative|preop|postop|pre operative|post operative|peri operative)\b|术前|术后|围手术期', phase):
        return False
    return bool(re.search(r'\b(?:intraoperative|operative|exploration)\b|术中|探查后|探查阶段', phase))


def build_evidence_context(event, sources, prior_events=None, budget=150000, preserve_current_context=False):
    """Retrieve earlier full originals, adding only verified current prechoice facts.

    This is reconstruction context, not an automatically certified blind packet.
    Unknown-time performed tests use an exam upper bound and exclude every own
    result variant. Operative facts are phase-located; start time is not asserted
    to be the exact decision minute. A semantic time upper bound beats note time.
    """
    indexed = {s['source_id']: s for s in sources}
    source_id = event.get('source_id')
    primary = indexed.get(source_id)
    source_ids = set(event.get('source_ids') or [source_id])
    source_ids.discard(None)
    actions = _citation_entries(event, 'action_citations')
    prechoice = _citation_entries(event, 'prechoice_citations')
    postchoice = _citation_entries(event, 'postchoice_citations')
    basis = event.get('time_basis') or 'unknown'
    phase = event.get('phase') or event.get('clinical_phase') or ''
    choice = _clock(event.get('choice_time') or event.get('decision_clock'))
    provided_upper = _clock(event.get('time_upper_bound') or event.get('decision_time_upper_bound'))
    performed = (event.get('kind') == 'performed_test_report' or event.get('document_profile') == 'performed_test_report'
                 or event.get('selection_basis') == 'performed_test_report')
    op = _OPERATION.search(primary.get('text', '')) if primary is not None else None
    operative_phase = is_operative_phase(event)
    supplied_operative_proxy_ignored = operative_phase and basis != 'explicit_documented_choice' and bool(choice['value'])
    if supplied_operative_proxy_ignored:
        choice = _clock(None)
    op_start = _clock(event.get('operation_start') or (op[1] if op else None))
    op_end = _clock(event.get('operation_end') or (op[2] if op else None))
    if provided_upper['value']:
        cutoff, cutoff_basis = provided_upper, 'semantic_choice_time_upper_bound'
    elif operative_phase:
        cutoff, cutoff_basis = op_start, 'operation_start_conservative_external_source_boundary_not_choice_time'
    elif choice['value']:
        cutoff, cutoff_basis = choice, basis
    elif performed and primary:
        cutoff = _clock((primary.get('clocks') or {}).get('exam_time') or primary.get('clock'))
        cutoff_basis = 'performed_test_time_upper_bound_not_choice_time'
    elif basis == 'record_time_proxy' and primary:
        cutoff, cutoff_basis = source_clock(primary), 'record_time_proxy_not_exact_choice'
    else:
        cutoff, cutoff_basis = _clock(None), 'unknown_choice_time_no_prior_state_certification'
    unknown_test_choice = performed and not choice['value']
    own_results = set(event.get('same_test_source_ids') or [])
    if performed:
        own_results |= source_ids
        # Extend the exclusion through the same deterministic exam grouping,
        # even if the caller accidentally supplied only one report variant.
        if primary:
            for unit in build_units([s for s in sources if s.get('kind') == 'imaging']):
                if primary['source_id'] in unit['source_ids']:
                    own_results.update(unit['source_ids'])
    candidates, ledger, current_valid, boundary_records = [], [], [], []
    source_boundaries = {sid: choice_boundary_for_source(event, indexed[sid], indexed)
                         for sid in source_ids if sid in indexed}
    for citation in prechoice:
        if not _range_valid(citation, indexed):
            ledger.append(dict(source_id=citation.get('source_id'), disposition='REVIEW_INVALID_PRECHOICE_SPAN'))
            continue
        sid = citation['source_id']
        if sid not in source_ids:
            continue  # Earlier whole originals are handled by temporal retrieval.
        if sid in own_results:
            ledger.append(dict(source_id=sid, disposition='EXCLUDE_OWN_TEST_PRECHOICE_CITATION'))
            continue
        overlaps_action = any(a.get('source_id') == sid and _range_valid(a, indexed) and
                              max(a['start'], citation['start']) < min(a['end'], citation['end']) for a in actions)
        contains_complete_action = any(a.get('source_id') == sid and _range_valid(a,indexed) and
            citation['start'] <= a['start'] and a['end'] <= citation['end'] for a in actions)
        overlaps_postchoice = any(a.get('source_id') == sid and _range_valid(a,indexed) and
            max(a['start'],citation['start']) < min(a['end'],citation['end']) for a in postchoice)
        boundary = source_boundaries.get(sid) or {}
        overlaps_choice_clause = any(max(a['start'], citation['start']) < min(a['end'], citation['end'])
                                    for a in boundary.get('exclusions', []))
        if overlaps_postchoice or overlaps_choice_clause or (overlaps_action and (not preserve_current_context or contains_complete_action)):
            ledger.append(dict(source_id=sid, disposition='REVIEW_PRECHOICE_OVERLAPS_CURRENT_ACTION',
                               start=citation['start'], end=citation['end'],
                               reason='EXPLICIT_CHOICE_CLAUSE' if overlaps_choice_clause else
                                      'POSTCHOICE_OR_COMPLETE_ACTION_SUPPORT'))
            continue
        if overlaps_action:
            ledger.append(dict(source_id=sid,disposition='SEMANTIC_FACT_WITHIN_BROAD_ACTION_SUPPORT_QUOTE',
                               start=citation['start'],end=citation['end'],
                               reason='A node-support quote is not automatically an exact decision-mask boundary'))
        current_valid.append(citation)
        candidates.append(_block(indexed[sid], citation['start'], citation['end'], 'current_prechoice_fact',
                                 'semantic_prechoice_span_operative_phase' if operative_phase else 'semantic_prechoice_span', 0))
    if preserve_current_context and not performed and basis in ('record_time_proxy','explicit_documented_choice','operative_phase'):
        for sid in source_ids:
            if sid not in indexed:
                ledger.append(dict(source_id=sid, disposition='REVIEW_CURRENT_SOURCE_MISSING'))
                continue
            source=indexed[sid];text=source.get('text','')
            boundary=source_boundaries[sid]
            boundary_records.append(dict(source_id=sid, **deepcopy(boundary)))
            for warning in boundary['warnings']:
                ledger.append(dict(disposition='SEMANTIC_CHOICE_CLAUSE_EXCLUDED_WITHOUT_CONTEXT_EXPANSION',
                                   **deepcopy(warning)))
            if boundary['start'] is None:
                ledger.append(dict(source_id=sid,disposition='REVIEW_NO_UNIQUE_ACTION_CONTEXT_BOUNDARY'))
                continue
            end=boundary['start'];start=0
            ledger.append(dict(source_id=sid,
                disposition='SOURCE_SUPPORTED_SEMANTIC_CHOICE_BOUNDARIES' if boundary['certified'] else
                            'REVIEW_CONSERVATIVE_ACTION_SUPPORT_BOUNDARY',
                reason=boundary['reason'], start=end,
                excluded_choice_clauses=deepcopy(boundary['exclusions']),
                clinical_semantics_certified=False))
            if operative_phase:
                # A complete exploration region is a source text region, not a
                # summary of chosen highlights. Header procedure/outcome fields
                # and subsequent diagnosis/choice/execution remain excluded.
                exploration=re.search(r'(?:^|[\n。])\s*探查[：:]?',text[:end])
                if not exploration:
                    ledger.append(dict(source_id=sid,disposition='REVIEW_NO_STRUCTURAL_EXPLORATION_BOUNDARY'))
                    continue
                start=text.find('探查',exploration.start(),exploration.end())
                diagnoses=list(re.finditer(r'术中诊断\s*[：:]',text[start:end]))
                if diagnoses:end=start+diagnoses[0].start()
            regions=[(start,end)] if start<end else []
            # Explicitly identified current downstream facts remain prohibited
            # even when a nonchronological note puts them before its action.
            excluded=[a for a in postchoice if a.get('source_id')==sid and _range_valid(a,indexed)]
            excluded += boundary['exclusions']
            for a in excluded:
                next_regions=[]
                for low,high in regions:
                    if a['end']<=low or a['start']>=high:next_regions.append((low,high))
                    else:
                        if low<a['start']:next_regions.append((low,a['start']))
                        if a['end']<high:next_regions.append((a['end'],high))
                regions=next_regions
            for low,high in regions:
                candidates.append(_block(source,low,high,'current_prechoice_complete_region',
                    'operative_exploration_region_before_current_assessment' if operative_phase else
                    'complete_current_document_regions_excluding_semantic_choice_clauses' if boundary['certified'] else
                    'complete_current_document_prefix_before_action_support',0))
                ledger.append(dict(source_id=sid,disposition='INCLUDE_COMPLETE_PRECHOICE_REGION',
                    start=low,end=high,role='source_context_not_summarized_clinical_fact'))
    available = []
    for source in sources:
        sid = source['source_id']
        if sid in own_results:
            ledger.append(dict(source_id=sid, disposition='EXCLUDE_OWN_TEST_RESULT', reason='same_test_variant'))
            continue
        if sid in source_ids:
            ledger.append(dict(source_id=sid, disposition='CURRENT_VALID_PRECHOICE_SPANS_ONLY',
                               full_document_excluded=True, spans=[dict(start=q['start'], end=q['end']) for q in current_valid if q['source_id'] == sid]))
            continue
        available_clock, source_basis = _available_clock(source)
        relation = time_order(available_clock, cutoff)
        if source.get('kind') == 'quarantine':
            ledger.append(dict(source_id=sid, disposition='REVIEW', reason='SOURCE_QUARANTINE_NOT_CLEAN_FACT'))
        elif source.get('kind') == 'pathology' and not available_clock['value']:
            ledger.append(dict(source_id=sid, disposition='REVIEW', reason='PATHOLOGY_AVAILABILITY_UNKNOWN'))
        elif operative_phase and basis != 'explicit_documented_choice' and op_start['value'] and op_end['value'] and time_order(available_clock, op_start) in ('AFTER', 'SAME_TIME_UNORDERED', 'SAME_MINUTE_UNORDERED') and time_order(available_clock, op_end) in ('BEFORE', 'SAME_TIME_UNORDERED', 'SAME_MINUTE_UNORDERED'):
            ledger.append(dict(source_id=sid, disposition='REVIEW', reason='WITHIN_OPERATION_EXACT_CHOICE_TIME_UNKNOWN'))
        elif relation == 'BEFORE':
            available.append((source, available_clock, source_basis))
        elif relation == 'AFTER':
            ledger.append(dict(source_id=sid, disposition='EXCLUDE_FUTURE', reason='SOURCE_AFTER_CHOICE_BOUNDARY'))
        else:
            ledger.append(dict(source_id=sid, disposition='REVIEW', reason=relation))
    earlier_clinical = sorted([s for s, _, _ in available if _primary_document(s)],
                              key=lambda s: (source_clock(s)['value'] or '', s['source_id']), reverse=True)
    recent_ids = {s['source_id'] for s in earlier_clinical[:4]}
    for source, available_clock, source_basis in available:
        kind, sid = source.get('kind'), source['source_id']
        # Older clinical notes can already contain a later-signed lab value.
        # Preserve their complete baseline before filling the budget with
        # hundreds of later report rows; "recent four" is not the whole state.
        priority = 1 if _primary_document(source) else 2 if kind == 'imaging' else 3 if kind == 'lab' else 5
        interval = 'before_test_upper_bound_not_certified_prechoice' if unknown_test_choice else 'cumulative_prior_state'
        block = _block(source, 0, len(source.get('text', '')), interval, source_basis, priority)
        from .evidence import report_objects
        if source.get('kind') in ('imaging', 'lab', 'pathology'):
            anchor, evidence = report_objects(source)
            block['test_execution_anchor_id'] = anchor['anchor_id']
            block['evidence_object_id'] = evidence['evidence_object_id']
            block['evidence_object_format'] = evidence['format']
            block['time_provenance'] = deepcopy(source.get('time_provenance', {}))
            block['execution_time'] = deepcopy(anchor['execution_time'])
            block['publication_time'] = deepcopy(evidence['publication_time'])
            block['publication_status'] = evidence['publication_status']
            block['clock'] = deepcopy(available_clock)
            block['execution_clock'] = source_clock(source)
            block['publication_before_choice_certified'] = False
            block['reading_before_choice_certified'] = False
        block['available_clock_proxy'] = available_clock
        block['source_text_complete'] = True
        candidates.append(block)
    # Highest-priority complete originals first; within a class use the newest
    # source. A large source is skipped with an explicit receipt, never sliced.
    candidates.sort(key=lambda b: (b['clock'].get('value') or '', b['source_id']), reverse=True)
    candidates.sort(key=lambda b: b['priority'])
    blocks, used, omitted = [], 0, 0
    for block in candidates:
        n = len(block['text'])
        if used + n > budget:
            ledger.append(dict(source_id=block['source_id'], disposition='omitted_budget',
                               start=block['anchor']['start'], end=block['anchor']['end'],
                               source_characters=n, priority=block['priority'], full_source_not_truncated=True))
            omitted += 1
        else:
            blocks.append(block); used += n
            ledger.append(dict(source_id=block['source_id'], disposition='INCLUDE', interval=block['interval'],
                               start=block['anchor']['start'], end=block['anchor']['end'],
                               availability_certified=False, priority=block['priority']))
    allowed = {}
    for block in blocks:
        allowed.setdefault(block['source_id'], []).append(dict(start=block['anchor']['start'], end=block['anchor']['end']))
    exact_choice = bool(choice['value']) and basis == 'explicit_documented_choice' and not operative_phase
    ambiguous_prechoice = (event.get('prechoice_citations') or {}).get('ambiguous', []) if isinstance(event.get('prechoice_citations'), dict) else []
    uncertainty = dict(availability_certified=False, prechoice_state_certified=False,
                       choice_time_exact=exact_choice, choice_time_unknown=not bool(choice['value']),
                       operative_choice_minute_unknown=bool(operative_phase and basis != 'explicit_documented_choice'),
                       supplied_operative_proxy_ignored=bool(supplied_operative_proxy_ignored),
                       performed_test_prechoice_state_uncertified=unknown_test_choice,
                       prechoice_ambiguous_count=len(ambiguous_prechoice), omitted_budget_count=omitted,
                       source_coverage_complete=not bool(omitted),
                       current_document_full_text_not_used_as_evidence=True)
    uncertainty['current_prechoice_context_preserved']=preserve_current_context
    uncertainty['current_choice_boundary_missing_or_ambiguous_count']=sum(
        not q['certified'] for q in boundary_records)
    uncertainty['current_choice_boundary_semantics_certified']=False
    return dict(blocks=blocks, ledger=ledger, source_ledger=ledger, uncertainty=uncertainty,
                current_choice_boundaries=boundary_records,
                cutoff=cutoff, cutoff_basis=cutoff_basis, choice_time=choice,
                cutoff_is_exact_choice_time=exact_choice and cutoff_basis == basis,
                cutoff_is_choice_time_upper_bound=provided_upper['value'] is not None or unknown_test_choice,
                operation_interval=dict(start=op_start, end=op_end) if operative_phase else None,
                same_test_source_ids=sorted(own_results), allowed_spans=allowed,
                budget=dict(limit=budget, text_characters=used, omitted_source_count=omitted,
                            complete_sources_not_truncated=True), availability_certified=False,
                blind_replay_ready=False)
