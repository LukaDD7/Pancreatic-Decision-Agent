"""Development extractor: retain source spans and distinguish semantic roles.

One candidate = one contiguous sentence/semicolon unit with potentially several
actions. It is NOT a confirmed atomic clinical decision or episode. Old labels
are retrieval evidence only; neither their eligibility nor time-window chains
are used as a gate. No mode/reference or counterfactual-result requirement.
"""
import hashlib
import re
from datetime import timezone
from scripts.cohort_construction.p2_decision_localization.labels import Labeler
from scripts.cohort_construction.p2_dynamic_pathway_probe.review_context import local_context_flags
from scripts.cohort_construction.p2_fhir_replay.core import instant


ADDED = {
    'OPTIMIZATION_PLAN': r'(?:今予|拟(?:行)?|计划(?:行)?|先行|择期再行|再行|予以)[^。；;\n]{0,15}?(?:PTCD|ERCP|减黄|胆汁引流|营养(?:支持|补充))',
    'CONDITIONAL_SURGERY': r'待[^。；;\n]{0,65}?(?:后|再)[^。；;\n]{0,25}?(?:手术|切除)',
    'INDICATION_PRESENT_EXT': r'具备(?:明确的?)?手术(?:指征|条件)',
    'NAMED_RESECTION_EXT': r'(?:限期行|择期行|再行|需行|需|行)(?:根治性)?(?:胰十二指肠切除(?:术)?|胰体尾脾切除(?:术)?|Whipple(?:术)?)',
    'SCHEDULE_REASSESSMENT': r'(?:拟|计划|建议|待|再|重新)[^。；;\n]{0,30}?(?:评估|评价)[^。；;\n]{0,20}?(?:手术|切除)',
    'SCHEDULED_RESECTION': r'(?:定于|拟于|拟|计划于)[^。；;\n]{0,55}?(?:胰十二指肠切除|胰体尾脾切除|胰[头体尾腺]癌根治|Whipple)',
    'SCHEDULED_PROCEDURE': r'(?:定于|拟于|计划于)[^。；;\n]{0,55}?(?:下行|行)[^。；;\n]{0,40}?(?:术|治疗)',
}
ACTION_PATTERNS = {
    'PTCD': r'PTCD', 'ERCP': r'ERCP', 'DRAINAGE_UNSPECIFIED': r'减黄|胆汁引流',
    'NUTRITION': r'营养(?:支持|补充|改善)',
    'PANCREAS_RESECTION': r'胰(?:十二指肠|体尾|体|尾|头|腺)[^。；;\n，,]{0,16}(?:切除|根治|次全切)|全胰切除|Whipple',
    'SURGERY_UNSPECIFIED': r'手术(?:治疗)?', 'BIOPSY': r'活检|穿刺|FNA|FNB',
    'MRI': r'MRI|磁共振', 'CT': r'(?<![A-Z])CT(?![A-Z])',
    'NEOADJUVANT': r'新辅助(?:治疗|化疗)|转化治疗',
}
TOPICS = {'BILIARY': r'胆红素|黄疸|减黄|胆汁|PTCD|ERCP',
          'NUTRITION': r'营养|白蛋白', 'CARDIOPULMONARY': r'心功能|心衰|肺部感染|BNP',
          'STAGING': r'转移|分期|病理|活检|穿刺', 'RESECTION': r'手术|切除|Whipple'}
EXECUTION = r'已(?:经)?行|完成(?:了)?[^。；;\n]{0,20}(?:手术|PTCD|ERCP)|(?:全麻|局麻|麻醉)[^。；;\n]{0,8}下行|于\d{4}[年-][^。；;\n]{0,32}(?:行|进行)'
FEEDBACK = r'(?:管|管道|引流管)[^。；;\n]{0,8}脱出|引流不畅|引流量|白蛋白未有明显升高|病灶较前缩小|疾病进展'
INTENT = r'今予|拟(?:行|施行)?|定于|计划|建议|择期|限期|先行|再行|暂不|不宜|不适合|无手术指征|待|需'
HISTORY = r'既往|当时|曾(?:行|于|经)|此前|外院建议|\d+[年月]前'
CURRENT_FAMILIES = {'FIRST_COURSE', 'FIRST_SENIOR_ROUND', 'WARD_ROUND', 'CONSULT', 'MDT',
                    'OUTPATIENT', 'PREOP_BRIEF', 'PREOP_DISCUSSION', 'TRANSFER_IN', 'TRANSFER_OUT', 'POSTOP'}


def execution_mentions(quote, offset):
    """A future operator scopes anaesthesia/date wording within its comma clause.

    Preserve explicit 已行/完成 and execution in a separate earlier clause;
    planned anaesthesia alone is not evidence that an operation happened.
    """
    result = []
    for m in matches(EXECUTION, quote, offset):
        local_start = m['start']-offset
        prefix = re.split(r'[，,：:]', quote[:local_start])[-1]
        explicit_done = re.match(r'已|完成', m['text'])
        if not explicit_done and re.search(r'拟|定于|计划|将于|预计|择期', prefix + quote[local_start:local_start+1]):
            continue
        result.append(m)
    return result


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def spans(text):
    """Full punctuation units keep negation; decimal dots never split numbers."""
    return [(m.start(), m.end()) for m in re.finditer(r'[^。；;\n]+[。；;]?', text) if m.group().strip()]


def matches(pattern, text, offset=0):
    return [{'start': offset+m.start(), 'end': offset+m.end(), 'text': m.group()}
            for m in re.finditer(pattern, text, re.I)]


class Extractor:
    def __init__(self, legacy_config):
        self.labeler = Labeler(legacy_config)

    def extract(self, note):
        text = note['text']; legacy = self.labeler.match(note); nodes = []
        for start, end in spans(text):
            quote = text[start:end]
            signals = []
            for h in legacy:
                if start <= h['start'] and h['end'] <= end:
                    signals.append({'label': h['label'], 'span': [h['start'], h['end']],
                                    'quote': text[h['start']:h['end']], 'legacy_role': h['role'],
                                    'legacy_target': h['target']})
            for label, pattern in ADDED.items():
                for m in matches(pattern, quote, start):
                    signals.append({'label': label, 'span': [m['start'], m['end']], 'quote': m['text']})
            # Positive substring inside "不可切除/不具备..." must not become an
            # affirmative assessment. Keep it in audit with explicit suppression.
            negative = [s for s in signals if s['label'] == 'INDICATION_NO']
            for s in signals:
                if s['label'] == 'NAMED_RESECTION_EXT' and s['quote'].startswith('行'):
                    prefix = text[max(start, s['span'][0]-15):s['span'][0]]
                    if re.search(r'下$|已$|曾$', prefix):
                        s['suppressed_as_intent'] = True
                if s['label'] in {'INDICATION_YES', 'INDICATION_PRESENT_EXT'}:
                    prefix = text[max(start, s['span'][0]-12):s['span'][0]]
                    if any(n['span'][0] <= s['span'][0] < n['span'][1] for n in negative) or re.search(r'是否|有无|不$|未见$|否认$', prefix):
                        s['suppressed_as_affirmative'] = True
            active = [s for s in signals if s['label'] != 'FITNESS_CONTEXT'
                      and not s.get('suppressed_as_affirmative') and not s.get('suppressed_as_intent')]
            execution = execution_mentions(quote, start)
            feedback = matches(FEEDBACK, quote, start)
            if not (signals or execution or feedback):
                continue
            flags = local_context_flags(note, quote)
            if re.search(r'同意|教育|介绍表', note['document_type']): flags.append('FORM_OR_CONSENT')
            if re.search(r'手术可能存在|可能出现的(?:意外|风险)|并发症包括|临床研究|临床试验', quote): flags.append('RISK_OR_TRIAL_CONTEXT')
            if any(s.get('legacy_role') == 'TEMPLATE_CONTEXT' for s in active): flags.append('TEMPLATE_CONTEXT')
            instruction = bool(re.search(r'出院(?:指导|医嘱)|注意事项|出院后[^。；;\n]{0,30}(?:每隔|每天|应|注意)', quote) or
                               (note['family']=='DISCHARGE' and re.search(r'如连续|每天记|每隔\d|若.*(?:可|应|需)', quote)))
            if instruction: flags.append('INSTRUCTION_NOT_OBSERVED_FEEDBACK')
            # Current supportive-care wording is not proof of a resection-path
            # decision. Retain it for linking; do not exclude its patient.
            if active and all(s['label']=='OPTIMIZATION_PLAN' for s in active) and re.search(r'营养', quote) and not re.search(r'手术|切除|PTCD|ERCP|胆汁引流|减黄', quote):
                flags.append('SUPPORTIVE_CARE_PATHWAY_LINK_REQUIRED')
            if re.search(HISTORY, quote): flags.append('HISTORY_CUE')
            current_clause = False
            for s in active:
                # A reference to earlier evidence must not erase an explicit
                # current conclusion in another comma clause of the same source.
                pos = s['span'][0]-start
                left = max(quote.rfind('，',0,pos),quote.rfind(',',0,pos))+1
                ends = [x for x in [quote.find('，',pos),quote.find(',',pos)] if x>=0]
                clause = quote[left:min(ends) if ends else len(quote)]
                if re.search(r'目前|当前|今予|今日|现(?:已|拟|有|无)',clause) and not re.search(HISTORY,clause):
                    current_clause = True
            if 'HISTORY_CUE' in flags and current_clause: flags.append('CURRENT_JUDGMENT_WITH_HISTORICAL_CONTEXT')
            if re.search(r'是否|能否|有无|[？?]', quote): flags.append('QUESTION_CUE')
            if execution: flags.append('EXECUTION_OR_RETROSPECTIVE_CUE')
            if note['family'] in {'ADMISSION', 'DISCHARGE'}: flags.append('NARRATIVE_DOCUMENT_TIME_REVIEW')
            if any(s.get('legacy_role') in {'EXPLICIT_HISTORY', 'NARRATIVE_UNRESOLVED', 'EXECUTED_OR_HISTORY'} for s in active):
                flags.append('LEGACY_TEMPORAL_REVIEW')
            if re.search(r'首先考虑[：:]?.*(?:新辅助治疗|化疗)后', quote) and not re.search(r'拟先|计划先|建议先', quote):
                flags.append('DIAGNOSIS_NOT_TREATMENT_SEQUENCE')
            target_other = 'DYN005_OTHER_TARGET_REVIEW' in flags or bool(re.search(r'输尿管|ESWL|脊髓|鞘内|颈椎|腰椎', quote))
            if target_other: flags.append('OTHER_TARGET_REVIEW')
            body = instant(note.get('body_time')); recorded = instant(note.get('system_time'))
            if not body: flags.append('NO_PRECISE_BODY_TIME')
            if body and recorded and body.date() != recorded.date(): flags.append('BODY_RECORD_DATE_DIFFER')
            blocker = {'FORM_OR_CONSENT','RISK_OR_TRIAL_CONTEXT','HISTORY_CUE','NARRATIVE_DOCUMENT_TIME_REVIEW',
                       'LEGACY_TEMPORAL_REVIEW','OTHER_TARGET_REVIEW','TEMPLATE_CONTEXT'} & set(flags)
            if current_clause:
                blocker.discard('HISTORY_CUE')
                if not any(s.get('legacy_role') in {'NARRATIVE_UNRESOLVED','EXECUTED_OR_HISTORY'} for s in active):
                    blocker.discard('LEGACY_TEMPORAL_REVIEW')
            if instruction:
                role, status = 'INSTRUCTION_MENTION', 'CONTEXT_REVIEW'
            elif active:
                role = 'DECISION_CANDIDATE'
                current = note['family'] in CURRENT_FAMILIES and not blocker
                # Execution wording cannot silently establish a present plan.
                if execution and not re.search(INTENT, quote): current = False
                if 'QUESTION_CUE' in flags and not re.search(r'拟|待|计划|建议|再评估|重新评估', quote): current = False
                status = 'CURRENT_STATEMENT_CANDIDATE' if current else 'CONTEXT_REVIEW'
            elif execution:
                role, status = 'EXECUTION_MENTION', 'CONTEXT_REVIEW'
            elif feedback:
                role, status = 'FEEDBACK_MENTION', 'CONTEXT_REVIEW'
            else:
                role, status = 'SUPPORT_MENTION', 'CONTEXT_REVIEW'
            actions = [{'concept': label, **m} for label, pattern in ACTION_PATTERNS.items() for m in matches(pattern, quote, start)]
            topics = [label for label, pattern in TOPICS.items() if re.search(pattern, quote, re.I)]
            nid = sha('|'.join([note['patient_uid'], note['record_key'], sha(text), str(start), str(end)]))[:24]
            nodes.append({'id': nid, 'patient_uid': note['patient_uid'], 'visit_id': note.get('visit_id'),
                          'source_id': note['record_key'], 'source_sha256': sha(text), 'span': [start, end],
                          'quote': quote, 'document_type': note['document_type'], 'document_family': note['family'],
                          'body_time': note.get('body_time'), 'recorded_time': note.get('system_time'),
                          'ordering_time': (body or recorded).astimezone(timezone.utc).isoformat() if body or recorded else None,
                          'time_basis': 'BODY_PROXY' if body else 'CREATION_PROXY' if recorded else 'UNKNOWN',
                          'role': role, 'status': status, 'flags': sorted(set(flags)), 'signals': signals,
                          'action_mentions': actions, 'execution_cues': execution, 'feedback_cues': feedback,
                          'topic_candidates': topics, 'atomicity': 'CONTIGUOUS_STATEMENT_GROUP_MAY_CONTAIN_MULTIPLE_ACTIONS',
                          'surgical_pathway_relevance': 'UNADJUDICATED',
                          'extractor_version': 'decision-ir-0.1', 'confidence': None, 'expert_reference': None})
        return nodes


def relate(nodes):
    """Propose review links, never merge records or claim causal/clinical links.

    Compare nearest strictly earlier statement on each specific shared topic.
    Generic surgery alone is too weak for a problem-thread claim. Multiple
    parallel topics are kept. Missing/same-time sources do not receive ordering.
    """
    links = []
    ordered = sorted(nodes, key=lambda n: (n['ordering_time'] or '', n['id']))
    prior = {}
    for n in ordered:
        if not n['ordering_time']: continue
        for topic in sorted(set(n['topic_candidates']) - {'RESECTION'}):
            key = (n['patient_uid'], topic)
            candidates = [p for p in prior.get(key, []) if p['source_id'] != n['source_id'] and p['ordering_time'] < n['ordering_time']]
            pools = [('previous_topic_statement', candidates)]
            if n['role'] == 'DECISION_CANDIDATE':
                pools.append(('previous_topic_decision', [p for p in candidates if p['role']=='DECISION_CANDIDATE']))
            for selector, pool in pools:
                if not pool: continue
                p = max(pool, key=lambda x: (x['ordering_time'], x['id']))
                precise_actions = lambda x: {a['concept'] for a in x['action_mentions']} - {'SURGERY_UNSPECIFIED', 'DRAINAGE_UNSPECIFIED'}
                pa, na = precise_actions(p), precise_actions(n)
                if n['quote'].strip() == p['quote'].strip(): hint = 'IDENTICAL_WORDING_NOT_MERGED'
                elif pa and pa == na: hint = 'SAME_ACTION_REASSESSMENT_OR_REPEAT_REVIEW'
                elif pa and na and pa != na: hint = 'ACTION_MENTION_DIFFERENCE_REVIEW'
                else: hint = 'SAME_TOPIC_NEIGHBOUR_REVIEW'
                links.append({'from': p['id'], 'to': n['id'], 'topic': topic, 'hint': hint,
                              'selector': selector,
                              'relation_status': 'UNADJUDICATED_TEMPORAL_TOPIC_LINK',
                              'basis': 'shared lexical topic + strictly earlier proxy time; not proof of responds_to/implements',
                              'cross_visit': p.get('visit_id') != n.get('visit_id'),
                              'episode_id': None})
            prior.setdefault(key, []).append(n)
    return links
