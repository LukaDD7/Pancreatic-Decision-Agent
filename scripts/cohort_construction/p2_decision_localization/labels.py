"""Phrase matching with explicit scope, section and temporal-role diagnostics.

Every hit survives in the audit output. A history, question, different operation
or unknown context goes to review rather than excluding the patient. No use is
made of subsequent pathology, operation or outcome to certify earlier choices.
"""
import re


class Labeler:
    def __init__(self, config):
        self.config = config
        self.patterns = {k: re.compile(v, re.I) for k, v in config.items() if k.endswith('_pattern')}

    def section(self, text, position):
        """Use the most recent explicit heading, preserving its start offset."""
        # A hit can START at a heading (“拟施手术名称…”). Include that
        # heading instead of assigning the preceding “手术指征” section.
        preceding = [m for m in self.patterns['section_pattern'].finditer(text, 0, min(len(text),position+45)) if m.start()<=position]
        if not preceding:
            return 'UNKNOWN', 0
        last = preceding[-1]
        return last.group('section'), last.end()

    def role(self, note, start, end, label):
        """Resolve obvious scope errors; uncertainty is an explicit queue."""
        text = note['text']; section, section_start = self.section(text, start)
        left = max(text.rfind(c, 0, start) for c in '。；;\n') + 1
        prefix = text[max(left, start-35):start]
        after = text[end:end+28]
        if label == 'INDICATION_YES' and re.search(r'是否|有无|评估.*(?:是否|有无)', prefix):
            return 'QUESTION', section
        if label == 'INDICATION_YES' and re.search(r'不(?:存?在)?$|未见$|否认$', prefix):
            return 'NEGATED_SCOPE', section
        if re.search(r'当时|既往|此前|曾经', prefix):
            return 'EXPLICIT_HISTORY', section
        # Do not let “考虑诊断” reach across a comma to an already performed
        # operation; a bare diagnostic “考虑” is not a PLAN_RESECTION intent.
        if label == 'PLAN_RESECTION' and (re.match(r'后|术后', after) or re.search(r'已(?:于|行)|曾(?:于|行)|实施了|完成了', prefix)):
            return 'EXECUTED_OR_HISTORY', section
        # A radiology quotation can contain “其它诊断:” and fool the most
        # recent-heading parser. A subsequent LOCAL physician conclusion in a
        # physician record outranks that heading, while true history sections
        # remain unresolved. This precedence was found by reverse source review.
        paragraph = text[max(left, start-160):end+60]
        cue = re.search(r'查房后.{0,8}(?:指示|指出)|查看(?:病人|患者)后(?:示|指示|指出)|外科评估后|根据患者目前.{0,35}(?:检查|影像)', paragraph)
        if cue and note['family'] in self.config['candidate_families'] and section not in {'现病史','既往史','病史特点','诊疗经过'}:
            return 'CURRENT_DOCUMENT_OPINION', section
        if section in self.config['template_sections']:
            return 'TEMPLATE_CONTEXT', section
        if section in self.config['narrative_sections']:
            return 'NARRATIVE_UNRESOLVED', section
        if section in self.config['current_sections']:
            return 'CURRENT_DOCUMENT_OPINION', section
        # An explicit “查房后指示/外科评估后” is stronger than a section label
        # missing from a locally formatted note, but is still source evidence.
        if note['family'] in {'PREOP_BRIEF', 'PREOP_DISCUSSION'} and label == 'PLAN_RESECTION':
            return 'CURRENT_DOCUMENT_OPINION', section
        return 'CONTEXT_UNRESOLVED', section

    def target(self, note, label, start, end):
        """Specific named resection beats generic procedure-context heuristics.

        Negative operability decisions can coexist with a planned biopsy. Never
        discard INDICATION_NO simply because EUS/FNA occurs elsewhere.
        """
        if label == 'PLAN_RESECTION':
            return 'NAMED_PANCREAS_RESECTION'
        if label in {'INDICATION_YES', 'GENERIC_SURGERY_PLAN'}:
            text = note['text']; section, ss = self.section(text, start)
            heading = self.patterns['section_pattern'].search(text, end)
            stop = min(len(text), end+220, heading.start() if heading else len(text))
            context = text[max(ss, start-80):stop]
            # Generic affirmative indication often refers to the procedure in
            # a later “治疗:” section. Use a bounded forward context too.
            context += text[end:end+160]
            if self.patterns['nonresection_procedure_pattern'].search(context) and not self.patterns['operation_pattern'].search(context):
                return 'OTHER_PROCEDURE'
        return 'PANCREAS_CONTEXT_UNSPECIFIED' if re.search(r'胰|PDAC|pancrea', note['text'], re.I) else 'TARGET_UNRESOLVED'

    def match(self, note):
        """Return all spans, plus explicit eligibility reasons for each one."""
        text = note['text']; spans = []
        for operation in self.patterns['operation_pattern'].finditer(text):
            # Clause-bounded and explicit intent, unlike v0.2's diagnostic 考虑.
            left = max(text.rfind(c, 0, operation.start()) for c in '。；;\n，,') + 1
            before = text[max(left, operation.start()-self.config['intent_lookback_chars']):operation.start()]
            intents = list(self.patterns['intent_pattern'].finditer(before))
            if intents:
                intent = intents[-1]
                spans.append(('PLAN_RESECTION', operation.start()-len(before)+intent.start(), operation.end()))
        mapping = {'yes_pattern': 'INDICATION_YES', 'no_pattern': 'INDICATION_NO',
                   'pending_pattern': 'ASSESSMENT_PENDING', 'nat_pattern': 'NAT_PLAN',
                   'generic_pattern': 'GENERIC_SURGERY_PLAN', 'fitness_pattern': 'FITNESS_CONTEXT'}
        for key, label in mapping.items():
            for match in self.patterns[key].finditer(text):
                if label == 'NAT_PLAN':
                    # “门诊拟‘胰腺肿瘤新辅助化疗后’收入院” names an
                    # admission diagnosis, unlike “建议新辅助治疗后手术”.
                    before = text[max(0,match.start()-8):match.start()]
                    if ('门诊' in before and match.group().startswith('拟')) or re.match(r'拟[“"「]',match.group()):
                        continue
                spans.append((label, match.start(), match.end()))
        hits = []
        for label, start, end in sorted(set(spans), key=lambda x: (x[1], x[2], x[0])):
            role, section = self.role(note, start, end, label)
            target = self.target(note, label, start, end)
            reasons = []
            if label not in self.config['candidate_labels']: reasons.append('SUPPORT_LABEL_ONLY')
            if role != 'CURRENT_DOCUMENT_OPINION': reasons.append(role)
            if target in {'OTHER_PROCEDURE','TARGET_UNRESOLVED'}: reasons.append(target)
            if note['family'] not in self.config['candidate_families']: reasons.append('DOCUMENT_FAMILY_REVIEW')
            eligible = not reasons
            left = max(text.rfind(c, 0, start) for c in '。；;\n') + 1
            stops = [text.find(c, end) for c in '。；;\n']; stops = [s for s in stops if s >= 0]
            sentence = text[left:min(stops) if stops else len(text)]
            hits.append({'record_key': note['record_key'], 'patient_uid': note['patient_uid'], 'label': label,
                         'start': start, 'end': end, 'match': text[start:end], 'section': section,
                         'role': role, 'target': target, 'candidate_eligible': eligible, 'review_reasons':reasons,
                         'sentence': sentence, 'context': text[max(0,start-80):min(len(text),end+130)]})
        for hit in hits:
            if hit['label'] == 'NAT_PLAN' and hit['candidate_eligible']:
                # A chemotherapy cycle plan is not automatically a new
                # surgical-candidacy judgment. Require an explicit current
                # operability/reevaluation statement in this source record.
                linked = any(h['candidate_eligible'] and h['label'] in {'INDICATION_YES','INDICATION_NO','ASSESSMENT_PENDING'} for h in hits)
                if not linked:
                    hit['candidate_eligible'] = False
                    hit['eligibility_note'] = 'NAT_WITHOUT_EXPLICIT_CURRENT_SURGERY_ASSESSMENT_REVIEW'
                    hit['review_reasons'].append(hit['eligibility_note'])
        return hits
