"""Document/paragraph routing and source-backed support lookup, never clinical gold."""
import copy
import re
from collections import defaultdict


def compact(text):
    return re.sub(r'\s+', '', text or '')


def profile(source):
    title=source.get('title') or ''; head=compact(source.get('text','')[:100])
    if source['kind']!='document':
        return source['kind']
    if '手术记录' in head:
        return 'operative_report'
    for kind,pattern in [
        ('postoperative_note',r'术后.*病程|术后首次'),
        ('preoperative_summary',r'术前小结'),
        ('preoperative_discussion',r'术前讨论'),
        ('consent',r'知情同意'),
        ('support_form',r'护理|健康教育|交接|评估记录单'),
        ('discharge_summary',r'出院小结'),
        ('first_course',r'首次病程'),
        ('consultation',r'会诊'),
        ('senior_round',r'主诊医师|主任医师.*查房'),
        ('attending_round',r'主治医师.*查房'),
        ('admission',r'入院记录'),
        ('transfer_note',r'转入记录|出室记录')]:
        if re.search(pattern,title) or (kind in ['admission','first_course'] and re.search(pattern,head)):
            return kind
    return 'clinical_other'


SECTION_PATTERN=re.compile(
    r'现病史[：:]|病例特点[：:]|简要病情[：:]|诊断依据[：:]|鉴别诊断[：:]|'
    r'诊疗计划[：:]|治疗计划[：:]|讨论内容[：:]|主任医师总结[：:]|'
    r'术前准备情况[：:]|术前诊断[：:]|手术指征[：:]|拟施手术名称和手术方式[：:]|'
    r'拟实施[：:]|注意事项[：:]|防范措施[：:]|术前[^\n：:]{0,25}主刀医师查看患者及病情评估[：:]|'
    r'探查[：:]|切除[：:]|重建[：:]|手术经过[：:]')


def paragraph_role(source,start):
    headings=[m for m in SECTION_PATTERN.finditer(source['text']) if m.start()<=start or
              (m.start()<=start+80 and not source['text'][start:m.start()].strip(' \t\n0123456789.、'))]
    heading=headings[-1].group() if headings else ''
    for role,p in [
        ('history',r'现病史|病例特点|简要病情'),
        ('differential_options',r'鉴别诊断'),
        ('diagnostic_assessment',r'诊断依据'),
        ('current_plan',r'诊疗计划|治疗计划'),
        ('preparation_status',r'术前准备'),
        ('readiness_assessment',r'主刀医师查看'),
        ('generic_precautions',r'注意事项|防范措施'),
        ('operative_findings',r'探查'),
        ('execution',r'切除|重建'),
        ('discussion',r'讨论内容|主任医师总结'),
        ('planned_procedure_field',r'拟施|拟实施'),
        ('indication',r'手术指征'),
        ('diagnostic_label',r'术前诊断')]:
        if re.search(p,heading):return role
    return 'narrative'


def generic_support(text,document_type,section):
    """Narrow observed form phrases; patient-specific changes always survive."""
    t=compact(text)
    if re.match(r'\d*[.、]?特殊手术报告[：:]',t):return 'administrative_form_field'
    if re.match(r'\d*[.、]?手术知情同意书[：:]已签署',t):return 'consent_completion_record'
    if document_type not in ['preoperative_summary','preoperative_discussion']:
        return None
    specific=bool(re.search(r'因.{1,30}(?:异常|升高|下降|发热|胸痛|梗阻)|新发|再次|复评|重新|改为|改用|取消|暂缓|若.{1,30}则|如果.{1,30}则|\d+(?:\.\d+)?(?:μg|mg|mmol|℃)',t))
    if specific:return None
    if re.fullmatch(r'术中根据探查情况决定具体手术方式[，,]注意仔细操作解剖[，,]严密止血[，,]避免副损伤[。]?',t):
        return 'generic_future_exploration_statement'
    # Readiness statements can contain a real reassessment. Keep them for the
    # model even when their wording resembles the observed form template.
    return None


def procedure(text):
    if re.search(r'胰十二指肠切除|Whipple',text,re.I):return 'pancreaticoduodenectomy'
    if re.search(r'全胰切除',text):return 'total_pancreatectomy'
    if re.search(r'胰腺根治性次全切除|胰体尾切除|远端胰腺切除|胰腺次全切除',text):return 'distal_or_subtotal_pancreatectomy'
    return None


def planned_date(text):
    m=re.search(r'(?:定于|拟于|拟定于|手术时间[：:])\s*(20\d\d)[年/-](\d{1,2})[月/-](\d{1,2})',text)
    return f'{m[1]}-{int(m[2]):02}-{int(m[3]):02}' if m else None


def management_track(c):
    t=c['anchor']['quote'];topics=c['topics']
    if 'CTA' in topics and re.search('冠脉|冠状|心脏|肌钙',t):return 'cardiac_safety'
    if any(k in topics for k in ['PET_MRI','PET_CT','MRI','CT','EUS_BIOPSY']) and not procedure(t):return 'diagnostic_workup'
    if 'BILIARY' in topics and not procedure(t):return 'biliary_or_drain_management'
    if 'SYSTEMIC' in topics and not procedure(t):return 'systemic_therapy'
    if procedure(t) or 'SURGERY' in topics:return 'operative_pathway'
    if re.search('营养|会诊',t):return 'nutrition_or_support'
    return 'other_management'


def organize(candidates,sources):
    """Keep every mention, bundle compatible same-document planned procedures.

    A bundle is a provisional input unit, not an adjudicated same_event edge.
    Cross-document similarity only adds prior-source context, never forced merge.
    """
    from .extract import comparison_clock, quote, time_order
    byid={s['source_id']:s for s in sources};out=copy.deepcopy(candidates)
    groups=defaultdict(list)
    for c in out:
        s=byid[c['source_id']];p=profile(s)
        c.update(document_profile=p,paragraph_role=paragraph_role(s,c['anchor']['start']),
                 flow_route='primary',management_track=management_track(c))
        created=s['clocks'].get('source_create_time',{})
        c['documentation_time_status']=('body_and_creation_differ' if s['kind']=='document' and
            created.get('value') and s['clock']['value'] and created['value'][:16]!=s['clock']['value'][:16] else 'no_difference_established')
        reason=generic_support(c['anchor']['quote'],p,c['paragraph_role'])
        if reason:
            c.update(flow_route='support_only',flow_reason=reason)
        if p in ['preoperative_summary','preoperative_discussion'] and not c['historical_hint'] and c['flow_route']=='primary':
            proc=procedure(c['anchor']['quote'])
            # Cancellation/change and patient-specific branches stay separate.
            changed=re.search(r'取消|暂缓|改为|改用|若|如果|不再|再次|重新|考虑|建议|可能|或者|或',c['anchor']['quote'])
            if proc and not changed:
                groups[(s['source_id'],proc,planned_date(c['anchor']['quote']))].append(c)
    # Date-less fields can support the unique specific date in the same document.
    by_procedure=defaultdict(list)
    for (sid,proc,date),members in groups.items():by_procedure[(sid,proc)].append((date,members))
    for (sid,proc),dated in by_procedure.items():
        dates={d for d,_ in dated if d}
        sets=[sum((v for _,v in dated),[])] if len(dates)<=1 else [v for _,v in dated]
        for members in sets:
            if len(members)<2:continue
            primary=max(members,key=lambda c:(bool(planned_date(c['anchor']['quote'])),
                         '术前各项检查未见' in c['anchor']['quote'],len(c['anchor']['quote'])))
            primary['member_mentions']=[dict(candidate_id=c['candidate_id'],anchor=c['anchor'],paragraph_role=c['paragraph_role']) for c in sorted(members,key=lambda c:c['anchor']['start'])]
            primary['bundle_grouping']='provisional_same_document_intended_procedure_not_confirmed_event_identity'
            primary['choice_start']=min(c['choice_start'] for c in members)
            end=max(c['current_note_prefix']['end'] for c in members)
            primary['current_note_prefix']=quote(byid[sid],max(0,min(c['current_note_prefix']['start'] for c in members)),end)
            for c in members:
                if c is not primary:c.update(flow_route='support_only',flow_reason='same_document_plan_mention',bundle_primary_id=primary['candidate_id'])
    for c in out:
        c['intended_procedure']=procedure(c['anchor']['quote'])
        c['intended_date']=planned_date(c['anchor']['quote'])
        if c['flow_route']!='primary':continue
        prior=[]
        for p in out:
            if p['source_id']==c['source_id'] or p['flow_route']!='primary' or p['historical_hint']:continue
            ps=byid[p['source_id']]
            if time_order(ps['clock'],comparison_clock(c))!='BEFORE':continue
            same_proc=c['intended_procedure'] and procedure(p['anchor']['quote'])==c['intended_procedure']
            if same_proc and p['management_track']==c['management_track']:
                prior.append(p)
        prior.sort(key=lambda p:comparison_clock(p)['value'] or '',reverse=True)
        c['prior_plan_mentions']=[dict(candidate_id=p['candidate_id'],anchor=p['anchor'],
             source_record_time=byid[p['source_id']]['clock'],document_profile=p['document_profile'],
             intended_date=planned_date(p['anchor']['quote']),relation='possible_prior_plan_not_confirmed_coreference') for p in prior[:4]]
        if c['clinical_phase']=='historical_operative_restatement':
            matches=[p for p in out if p['clinical_phase']=='after_documented_exploration_before_current_choice'
                and p['source_id']!=c['source_id'] and p['decision_clock']['value']
                and c['decision_clock']['value'] and p['decision_clock']['value'][:10]==c['decision_clock']['value'][:10]
                and compact(c['anchor']['quote'])==compact(p['anchor']['quote'])]
            if len(matches)==1:
                c.update(flow_route='support_only',flow_reason='exact_secondary_operative_restatement',
                         supports_candidate_id=matches[0]['candidate_id'])
    return out


def prior_occurrences(anchor,previous,sources):
    """Exact repeated wording in earlier documents, not a claim of same measurement."""
    from .extract import comparison_clock, quote, time_order
    if previous is None:return []
    text=anchor['quote'];units=[]
    score=re.search(r'评估分数[：:]\s*\d+(?:\.\d+)?[。；;]?\s*级别[：:]\s*[^。；;\n]+',text)
    if score:units.append(score.group())
    units += [m.group().strip() for m in re.finditer(r'[^。；;\n]+[。；;]?',text) if len(compact(m.group()))>=18]
    hits=[];seen=set()
    for s in sources:
        if s['kind']!='document' or s['source_id']==anchor['source_id'] or time_order(s['clock'],comparison_clock(previous))!='BEFORE':continue
        for unit in units:
            for m in re.finditer(re.escape(unit),s['text']):
                key=(s['source_id'],m.start(),m.end())
                if key in seen:continue
                seen.add(key);hits.append(dict(anchor=quote(s,m.start(),m.end()),
                    current_quote=unit,interpretation='same_wording_previously_recorded_not_proof_same_measurement'))
    return hits[:20]


def evidence_guard(candidate,previous,result,sources):
    """No new clinical evidence merely from form/plan text; retain ambiguity."""
    from .extract import comparison_clock,time_order
    if result.get('evidence_evolution')!='new_relevant':return None
    if result.get('evidence_character') in ['plan_or_form_only','repeated_documentation_only']:
        return 'NO_NEW_CLINICAL_EVIDENCE'
    byid={s['source_id']:s for s in sources};valid=[];repeated=[];repeat_ambiguous=False
    for a in result.get('supports',[]):
        if a['source_id'] in candidate.get('same_test_source_ids',[]):continue
        if a['source_id']==candidate['source_id'] and a['end']>candidate['choice_start']:continue
        s=byid[a['source_id']]
        section=paragraph_role(s,a['start'])
        # A source saying "术中根据探查情况" before the word 决定 is still a plan.
        if re.fullmatch(r'\s*术中根据探查情况\s*',a['quote']):continue
        if generic_support(a['quote'],profile(s),section):continue
        if previous and previous['decision_clock']['value'] and time_order(s['clock'],comparison_clock(previous))=='BEFORE':
            # An old source alone does not establish a new interpretation now.
            # That reassessment must also be quoted from the current interval.
            continue
        hits=prior_occurrences(a,previous,sources)
        repeated.extend(hits)
        q=compact(a['quote'])
        whole_repeat=any(compact(h['current_quote'])==q for h in hits)
        reassessed=bool(re.search(r'重新评估|再次评估|复评|复查|新发|改变|升高|降低',a['quote']))
        if whole_repeat and profile(s) in ['preoperative_summary','preoperative_discussion'] and not reassessed:
            repeat_ambiguous=True
            continue
        valid.append(a)
    result['prior_wording_matches']=repeated
    return None if valid else ('REPEATED_WORDING_IDENTITY_UNCERTAIN' if repeat_ambiguous else 'NEW_EVIDENCE_ONLY_PLAN_OR_PREVIOUSLY_RECORDED')
