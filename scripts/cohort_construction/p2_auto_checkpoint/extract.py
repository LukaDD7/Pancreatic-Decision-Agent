"""Deterministic source mapping and broad candidate extraction, no case fixtures."""
import copy
import hashlib
import json
import re
from pathlib import Path

from scripts.cohort_construction.p2_pathway_replay.core import project_sources, clock


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def body_clock(text):
    # Labeled record time anywhere, or a dated narrative heading. Never the
    # first arbitrary date in a history, consent compilation or operation.
    labeled = re.findall(r'记录时间[：:]\s*(20\d\d-\d\d-\d\d \d\d:\d\d(?::\d\d)?)', text)
    if len(set(labeled)) == 1:
        return clock(labeled[0])
    if len(set(labeled)) > 1:
        return clock(None)
    leading = re.match(r'\s*(20\d\d-\d\d-\d\d \d\d:\d\d(?::\d\d)?)', text)
    return clock(leading[1] if leading else None)


def load_sources(bundle_path, uid):
    bundle = read(bundle_path)
    sources, inventory = project_sources(bundle, uid)
    for s in sources:
        if s['kind'] == 'document':
            s['clocks']['body_record_time'] = body_clock(s['text'])
        s['clock'] = (s['clocks'].get('body_record_time') if s['kind'] == 'document' else
                      s['clocks'].get('exam_time') if s['kind'] == 'imaging' else
                      s['clocks'].get('report_time')) or clock(None)
        s['clock_basis'] = ('body_record_time' if s['kind'] == 'document' else
                            'exam_time_proxy_release_unknown' if s['kind'] == 'imaging' else 'report_time_proxy')
    return sources, inventory


def document_class(title, config):
    # Order matters: a postoperative note is not an operative report merely
    # because it mentions a procedure in the body.
    if re.search(r'术后.*病程|术后首次', title or ''):
        return 'clinical'
    for kind, pattern in config['document_types'].items():
        if re.search(pattern, title or ''):
            return kind
    return 'other'


def topics(text, config):
    result = [k for k, p in config['topics'].items() if re.search(p, text, re.I)]
    if any(k in result for k in ['PET_MRI', 'PET_CT']):
        result = [k for k in result if k not in ['MRI', 'CT']]
    if 'CTA' in result:
        result = [k for k in result if k != 'CT']
    return result


def quote(s, start, end):
    return dict(source_id=s['source_id'], source_sha256=s['source_sha256'],
                start=start, end=end, quote=s['text'][start:end])


def historical_clock(text, span_start, span_end, source_clock):
    clause = text[span_start:span_end]
    # Actual historical performance, not a future planned operating date.
    hist = bool(re.search(r'予以.{0,18}(?:检查|治疗)|遂行|曾.{0,20}(?:行|化疗)|行.{0,18}检查提示|在.{0,40}医院.{0,8}(?:行|予以)', clause))
    if hist:
        dates = re.findall(r'(20\d\d[-年/]\d{1,2}[-月/]\d{1,2})', clause)
        if dates:
            vals = re.findall(r'\d+', dates[-1])
            d = f'{int(vals[0]):04}-{int(vals[1]):02}-{int(vals[2]):02}'
            if source_clock['value'] and d < source_clock['value'][:10]:
                return clock(d), 'historical_date_from_same_sentence', True
        if re.search(r'现病史|遂行|曾|既往|患者于.{0,12}前', text[max(0, span_start-200):span_end]):
            return clock(None), 'historical_time_unresolved', True
    return copy.deepcopy(source_clock), 'source_body_proxy', False


def comparison_clock(candidate):
    """An examination time bounds an unknown earlier choice; it is not that choice's time."""
    return (candidate.get('decision_time_upper_bound') if not candidate['decision_clock']['value']
            else candidate['decision_clock']) or clock(None)


def test_report_candidate(group, variants, config):
    s = variants[0]  # Preserve every variant; this is a carrier, not a chosen truth.
    upper = s['clocks'].get('exam_time') or clock(None)
    if not upper['value']:
        upper = s['clocks'].get('report_time') or clock(None)
    return dict(candidate_id='TC'+digest([group])[:18], source_id=s['source_id'],
                anchor=quote(s, 0, len(s['text'])), document_type='diagnostic_report',
                topics=topics(s['title'] or '', config), decision_clock=clock(None),
                decision_time_upper_bound=upper, execution_clock=s['clocks'].get('exam_time', clock(None)),
                source_document_clock=s['clock'], time_basis='choice_before_test_exact_time_unknown',
                selection_basis='performed_test_report', historical_hint=True,
                choice_start=0, choice_boundary_basis='report_is_not_prechoice_state',
                clinical_phase='choice_before_documented_test', operation_end=None,
                current_note_prefix=quote(s, 0, 0), possible_repeat_key=group,
                exam_group=group, same_test_source_ids=[v['source_id'] for v in variants],
                occurrence_supported_by='report_metadata_and_text_not_an_order',
                prechoice_state_certified=False)


def extract(sources, config):
    candidates, reports, ledger = [], [], []
    report_groups = {}
    for s in sources:
        if s['kind'] != 'document':
            if s['kind'] == 'imaging':
                number = s['provenance'].get('exam_no_raw')
                group = digest([number, s['clock']['value'], s['title']])[:20] if number else s['source_id']
                reports.append(dict(report_event_id='R'+digest([s['source_id']])[:18], exam_group=group,
                                    topics=topics(s['title'], config), source_id=s['source_id'],
                                    clock=s['clock'], role='evidence_only_not_a_decision'))
                if config.get('diagnostic_decision_candidates'):
                    report_groups.setdefault(group, []).append(s)
            continue
        dtype = document_class(s['title'], config)
        op = re.search(r'手术时间[：:]\s*(20\d\d-\d\d-\d\d \d\d:\d\d)(?:\s*)-(20\d\d-\d\d-\d\d \d\d:\d\d)', s['text'])
        exploration = s['text'].find('探查') if op else -1
        count = 0
        # Include unknown document types when action+topic occurs; the fallback
        # is explicit in the ledger instead of silently losing a source family.
        if dtype == 'support':
            ledger.append(dict(source_id=s['source_id'], document_type=dtype, candidate_count=0,
                               reason='SUPPORT_FORM_RETAINED_FOR_CONTEXT_NOT_PRIMARY_ANCHOR'))
            continue
        for m in re.finditer(r'[^。；;\n]+[。；;]?', s['text']):
            raw = m.group(); hit_topics = topics(raw, config)
            if not hit_topics or not re.search(config['action_pattern'], raw):
                continue
            start, end = m.start(), m.end()
            t, basis, hist = historical_clock(s['text'], start, end, s['clock'])
            marker=re.search(r'遂决定|予取消|拟|建议|决定|评估|判断|考虑|给予|予以|予|限期|择期|暂缓|取消|继续|需',raw)
            choice_start=start+marker.start() if marker else start
            choice_basis='explicit_action_marker' if marker else 'sentence_start_unresolved'
            phase, op_end = 'documented_assessment', None
            prefix_start = max(0, start-config['context_characters'])
            if op:
                op_end = clock(op[2])
                decision_in_clause=re.search(r'遂决定|决定|改为|终止|取消|放弃',raw)
                if exploration >= 0 and decision_in_clause and start+decision_in_clause.start()>exploration:
                    # The narrative locates a clinical phase, not an exact
                    # minute. Ancillary reports during the operation stay REVIEW.
                    t, basis, hist = clock(op[1]), 'operative_start_proxy_actual_choice_phase_not_minute', False
                    phase = 'after_documented_exploration_before_current_choice'
                    prefix_start = exploration
                    decision_marker=re.search(r'遂决定|决定|改为|终止|取消|放弃',raw)
                    choice_start=start+decision_marker.start()
                    diagnosis=s['text'].rfind('术中诊断：',exploration,choice_start)
                    if diagnosis>=exploration:choice_start=diagnosis
                    choice_basis='operative_current_assessment_or_choice_marker'
                else:
                    t, basis = clock(None), 'operative_header_or_execution_time_not_decision_time'
                    phase = 'operative_source_without_unique_choice_phase'
            elif re.search(r'术后|出院小结|转入记录|出室记录',s['title'] or '') and re.search(r'遂决定|术中诊断.{0,30}决定',raw):
                # A postoperative summary can quote an intraoperative choice.
                # It is not another decision merely because it was written later.
                hist=True; t=clock(s['clock']['value'][:10] if s['clock']['value'] else None)
                basis='secondary_operative_restatement_date_proxy_not_new_judgment'
                phase='historical_operative_restatement'
            anchor = quote(s, start, end)
            # Exact repetitions are grouped only as mentions, not automatically
            # as the same clinical decision. A new assessment survives.
            cid = 'C'+digest([s['source_id'], start, end])[:18]
            candidates.append(dict(candidate_id=cid, source_id=s['source_id'], anchor=anchor,
                                   document_type=dtype, topics=hit_topics, decision_clock=t,
                                   time_basis=basis, historical_hint=hist,
                                   choice_start=choice_start,choice_boundary_basis=choice_basis,
                                   clinical_phase=phase, operation_end=op_end,
                                   current_note_prefix=quote(s, prefix_start, end),
                                   possible_repeat_key=digest([hit_topics, t['value'], re.sub(r'\s+', '', raw)])[:20]))
            if config.get('diagnostic_decision_candidates'):
                c = candidates[-1]
                c['source_document_clock'] = s['clock']
                c['selection_basis'] = 'documented_statement'
                diagnostic = any(k in hit_topics for k in ['PET_MRI', 'PET_CT', 'MRI', 'CT', 'CTA', 'EUS_BIOPSY'])
                if diagnostic and (hist or re.search(r'(?:检查|MRI|MR|CT|CTA).{0,6}提示', raw)):
                    # A retrospective test is a choice candidate, not a decision at
                    # the examination timestamp or the later note timestamp.
                    dates = re.findall(r'(20\d\d)[-年/](\d{1,2})[-月/](\d{1,2})', raw)
                    dated = clock('-'.join([dates[-1][0], dates[-1][1].zfill(2), dates[-1][2].zfill(2)])) if dates else clock(None)
                    execution = t if hist and basis == 'historical_date_from_same_sentence' else dated
                    c.update(selection_basis='historical_test_trace', historical_hint=True,
                             decision_clock=clock(None), decision_time_upper_bound=execution if execution['value'] else s['clock'],
                             execution_clock=execution, time_basis='historical_choice_before_test_exact_time_unknown',
                             prechoice_state_certified=False)
                    test_start = re.search(r'(?:予以|遂行|行)?(?:PET[-－ /]?(?:MRI|MR|CT)|MRI|MRCP|CTA|CT|MR|B超|超声)', raw, re.I)
                    if test_start:
                        c['choice_start'] = start + test_start.start()
                    # Only the local historical sequence, not later admission
                    # demographics/state or the current test result, can be replayed.
                    history_start = max(s['text'].rfind('现病史：', 0, c['choice_start']),
                                        s['text'].rfind('简要病情：', 0, c['choice_start']))
                    c['retrospective_prefix_start'] = history_start if history_start >= 0 else max(0, start-config['context_characters'])
                    c['possible_repeat_key'] = digest([hit_topics, execution['value'], re.sub(r'\s+', '', raw)])[:20]
            count += 1
        ledger.append(dict(source_id=s['source_id'], document_type=dtype, candidate_count=count,
                           reason='RULE_CANDIDATES' if count else 'NO_RULE_MATCH_NOT_PROOF_NO_DECISION'))
    if config.get('diagnostic_decision_candidates'):
        candidates.extend(test_report_candidate(g, vv, config) for g, vv in report_groups.items())
        candidates.sort(key=lambda c: (comparison_clock(c)['value'] or '9999',
                                       c.get('source_document_clock', clock(None))['value'] or '9999',
                                       c['source_id'], c['anchor']['start']))
    else:
        candidates.sort(key=lambda c: (c['decision_clock']['value'] or '9999', c['source_id'], c['anchor']['start']))
    if config.get('document_aware_flow'):
        from .document_flow import organize
        candidates = organize(candidates, sources)
    return candidates, reports, ledger


def time_order(c, cutoff):
    if not c['value'] or not cutoff['value']:
        return 'UNKNOWN'
    if c['value'][:10] != cutoff['value'][:10]:
        return 'BEFORE' if c['value'][:10] < cutoff['value'][:10] else 'AFTER'
    if c['precision'] == 'date' or cutoff['precision'] == 'date':
        return 'SAME_DAY_UNORDERED'
    if (c['precision']=='minute' or cutoff['precision']=='minute') and c['value'][:16]==cutoff['value'][:16]:
        return 'SAME_MINUTE_UNORDERED'
    return 'BEFORE' if c['value'] < cutoff['value'] else 'AFTER' if c['value'] > cutoff['value'] else 'SAME_TIME_UNORDERED'


def window(sources, candidate, previous=None):
    """Cumulative prior raw state plus interval delta, never downstream text."""
    blocks, ledger = [], []
    upper = comparison_clock(candidate)
    lower = comparison_clock(previous) if previous else None
    if previous and previous.get('comparison_basis')=='same_source_narrative_order_only_not_exact_timed_interval':
        lower = clock(None)
    for s in sources:
        source_id = s['source_id']
        state = time_order(s['clock'], upper)
        if s['kind'] == 'quarantine':
            state = 'QUARANTINE_REVIEW'
        if source_id in candidate.get('same_test_source_ids', []):
            ledger.append(dict(source_id=source_id, disposition='EXCLUDE_CURRENT_TEST_RESULT_FROM_PRECHOICE'))
            continue
        retrospective = candidate.get('selection_basis') == 'historical_test_trace'
        if source_id == candidate['source_id'] and (not candidate['historical_hint'] or retrospective):
            end = candidate.get('choice_start',candidate['anchor']['start'])
            prefix_start = candidate['current_note_prefix']['start'] if candidate.get('operation_end') else 0
            if retrospective:
                prefix_start = candidate['retrospective_prefix_start']
            if end > prefix_start:
                blocks.append(dict(alias=source_id, kind=s['kind'], title=s['title'],
                                   clock=s['clock'], clock_basis='same_note_prior_narrative_stage',
                                   interval='retrospective_pretest_narrative_unverified' if retrospective else 'current_prechoice_prefix',
                                   anchor=quote(s, prefix_start, end)))
            ledger.append(dict(source_id=source_id, disposition='PARTIAL_CURRENT_PREFIX', end=end))
        elif state == 'BEFORE':
            interval = 'baseline_before_previous' if lower and time_order(s['clock'], lower) == 'BEFORE' else 'between_previous_and_current'
            # Retain exact original lab cells without repeating export labels
            # and report provenance in the model context. Audit retains source.
            a = quote(s, 0, len(s['text']))
            blocks.append(dict(alias=source_id, kind=s['kind'], title=s['title'], clock=s['clock'],
                               clock_basis=s['clock_basis'], interval=interval, anchor=a))
            ledger.append(dict(source_id=source_id, disposition='INCLUDE_PROXY_PRIOR', interval=interval))
        else:
            within_op = (candidate.get('operation_end') and state == 'AFTER' and
                         time_order(s['clock'], candidate['operation_end']) in ['BEFORE', 'SAME_TIME_UNORDERED'])
            ledger.append(dict(source_id=source_id, disposition='REVIEW' if within_op else 'EXCLUDE_FUTURE' if state == 'AFTER' else 'REVIEW',
                               reason='WITHIN_OPERATION_STAGE_UNRESOLVED' if within_op else state))
    return dict(cutoff=upper, lower=lower, blocks=blocks, source_ledger=ledger,
                current_decision_clock=candidate['decision_clock'],
                cutoff_is_choice_time_upper_bound=not bool(candidate['decision_clock']['value']),
                availability_certified=False, historical_prechoice_state_complete=False if candidate['historical_hint'] else None)
