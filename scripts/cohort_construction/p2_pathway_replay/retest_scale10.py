"""Re-run the fixed ten, with explicit same-assistant semantic review proposals.

Programs validate and compile the reviewed cases. They do not declare expert
gold or infer treatment correctness. Old artifacts are read-only.
"""
import collections
import copy
import csv
import hashlib
import json
import re
from pathlib import Path
from .core import (sha,cite,operative_partition,validate_partition,resource_selection,
                   compile_view,relation_to_cutoff,validate_assessment_round)
from .prepare_scale10 import ROOT,BASE,OUT

# Each added point needs a pre-choice source AND a documented current condition
# assessment/choice. These are explicit semantic review inputs, not a rule that
# every preop summary or every new laboratory row is a checkpoint.
READY_EVIDENCE={
 'SCALE-01':['9b83845b186dcde9fdb09b7a'],
 'SCALE-03':['1eada45166c0af7bd28334a6'],
 'SCALE-04':['0b5bdf0bab2240a79f258457'],
 'SCALE-05':['72ca6caa28b62f0ee035ddeb','dcf5cd289ad721dfa17ac1a1'],
 'SCALE-07':['51839078bf0a2ad659a68987'],
 'SCALE-08':['6fc2780ade8f23c1b3dccbba'],
 'SCALE-09':['158b59c6109618351e85367b'],
 'SCALE-10':['70df86949167b87bd6949180'],
}
ROUND_NOTES={
 'SCALE-01':'12月14日新CT/MR已补入；当前记录无禁忌评估并安排手术。不是因日期或签字本身增点。',
 'SCALE-03':'11月5—6日术前检验与6日当前适应性评估形成新一轮条件判断；保留白细胞低值，未判风险解除。',
 'SCALE-04':'9月3日明确待炎症控制，4日取得脂肪酶复查并有当前术前评估/安排；不把数值变化当临床控制完成。',
 'SCALE-05':'4月16日重新进入条件性路径，17日新增血常规/心肌检查及当前术前条件评估；10:14切点后回报的部分检验不提前。',
 'SCALE-07':'25日当前心肺耐受评估与此前待排禁忌不同；8:41已有心肌检查。25日下午CT/MR晚于正文切点，不借它们支持上午判断。',
 'SCALE-08':'11月1日新心肌等术前检验及当前准备/无禁忌评估，作为条件性计划之后的一轮候选判断；不是每次正常检验都建点。',
 'SCALE-09':'28日新胆红素等检查、29日当前条件评估及具体范围安排；不把胆红素增高改写成改善。',
 'SCALE-10':'29日15:45新CT及当前术前评估/安排；16:57 MRI在16:32切点之后，仅用于后续术中输入。',
}
LATE_REVIEW={
 'SCALE-01':'血管切除长度/重建及阻断时间为后段操作信息；未记录另一次新发现后的选择，保留过程，不前移或另计点。',
 'SCALE-02':'后段联合左肾上腺处理已在当前选择中包含，未另记独立新判断。',
 'SCALE-03':'肝病灶切除后的术中超声不能前移；未记后续新管理选择，不另计点。',
 'SCALE-04':'门静脉侧壁处理已在当前决定内；未记独立后续判断。',
 'SCALE-05':'后段无脏器侵犯与前段十二指肠侵犯描述差异保留；不能据此自动造新判断或覆盖前段。',
 'SCALE-06':'左肾上腺切除理由未充分交代，已含在当前选择；不据实施补造理由。',
 'SCALE-07':'后段为已定术式实施，未见额外决策相关新发现与选择。',
 'SCALE-08':'静脉长度、重建和阻断细节为当前选择的后续；不提前。',
 'SCALE-09':'后段静脉侧壁处理，较早术前计划已写侧壁切除重建；保留过程，不凭操作增加点。',
 'SCALE-10':'未行血管切除紧邻楔形切除的局部差异保留，不当成策略修改或改写所见。',
}

def load_case(cid):
    return json.loads((OUT/cid/'sources.json').read_text())['sources']

def sentence(t,marker):
    start=t.index(marker);end=t.index('。',start)+1
    return t[start:end]

def old_anchor(a,byid):
    return cite(byid[a['source_id']],a['quote'],a['start'])

def review_case(cid,sources):
    old=json.loads((BASE/cid/'trial_result.json').read_text());byid={s['source_id']:s for s in sources}
    r=copy.deepcopy(old);r['origin']='AI_semantic_reinspection_with_multisource_retrieval_v0.2'
    preop=next(m for m in r['mixed_document_masks'] if m['mask_id'].startswith('PREOP'))
    op=next(m for m in r['mixed_document_masks'] if m['mask_id'].startswith('INTRAOP'))
    ps=byid[preop['source_id']];pt=ps['clocks']['body_record_time']['value']
    plan=cite(ps,sentence(ps['text'],'经与家属谈话并签字同意定于'))
    assessment_marker='术前心肺功能评估可耐受手术及麻醉。' if cid=='SCALE-07' else '心肺检查未见明显异常，术前检查未见明确手术禁忌症。'
    assess=cite(ps,assessment_marker)
    changes=[];cps=[]
    for c in r['checkpoints']:
        c['baseline_checkpoint_id']=c['checkpoint_id']
        c['assessment_round_support']='baseline_reinspected'
        c['uncertainty_dimensions']=dict(subjective_rationale_not_claimed=True,source_availability_certified=False)
        if cid=='SCALE-06' and c['baseline_checkpoint_id'].endswith('CP03'):
            for sid in ['98a469ee50635c9c67093741','a886d9234d646afb00f1775a']:
                s=byid[sid];a=cite(s,s['original_fields']['original_impression'])
                assert relation_to_cutoff(s['clocks']['exam_time'],c['documented_decision'][0]['body_time'])=='BEFORE'
                c['evidence'].append(a)
            c['status']='candidate_context_supported'
            c['label']='补取CTA及胰腺MR后当前继续推进的候选判断'
            c['evolution']='9月27日肌钙蛋白异常及拟冠脉CTA；28日原始CTA/MR在当前评估之前，且当前记录继续推进。支持候选回放，不证明医生主观因果或全部心脏风险解除。'
            c['assessment_round_support']='new_report_and_current_judgment_context_supported'
            c['documented_decision'].append(plan)
            changes.append(dict(type='support_strengthened_not_new_point',baseline=c['baseline_checkpoint_id'],reason='CTA was omitted by the document-only projection, not rejected by relevance/time rules.'))
            preop['target_round_baseline_id']=c['baseline_checkpoint_id']
        if cid=='SCALE-02' and c['baseline_checkpoint_id'].endswith('CP02'):
            c['documented_decision'].append(plan)
            c['assessment_round_support']='special_blood_condition_and_arrangement_grouped_same_round'
            c['current_evidence_to_isolate']=[a for a in c['evidence'] if '需血库充分备血' in a['quote']]
            preop['target_round_baseline_id']=c['baseline_checkpoint_id']
            changes.append(dict(type='same_round_mentions_grouped',baseline=c['baseline_checkpoint_id'],reason='Special blood safety condition and ensuing summary share the current assessment; no extra point or proof blood preparation completed.'))
        cps.append(c)
    if cid in READY_EVIDENCE:
        evidence=[]
        for sid in READY_EVIDENCE[cid]:
            s=byid[sid];q=s['original_fields']['original_impression'] if s['kind']=='imaging' else s['text']
            evidence.append(cite(s,q))
        previous=cps[-2]['documented_decision'][0]
        supported=validate_assessment_round(previous,assess,plan,evidence,sources,pt)
        assert supported['eligible'],(cid,supported)
        added=dict(label='补取新证据后当前术前条件评估与安排',status='candidate_context_supported',
                   evidence=evidence+[assess],documented_decision=[plan],evolution=ROUND_NOTES[cid],
                   time_note=f'{pt}为正文切点代理；创建时间不等于实际判断时刻。',
                   assessment_round_support='new_original_evidence_plus_documented_current_condition_assessment',
                   current_evidence_to_isolate=[assess],
                   prior_unresolved_condition=previous,baseline_checkpoint_id=None,
                   uncertainty_dimensions=dict(actual_choice_minute_not_certified=True,subjective_rationale_not_claimed=True,clinical_risk_resolution_not_assessed=True))
        cps.insert(-1,added)
        changes.append(dict(type='added_assessment_round',reason=ROUND_NOTES[cid],new_evidence=evidence))
    for i,c in enumerate(cps,1):c['checkpoint_id']=f'{cid}-CP{i:02}'
    r['checkpoints']=cps;r['episodes'][0]['checkpoint_ids']=[c['checkpoint_id'] for c in cps]
    r['episodes'][0]['baseline_boundary_notes']=r['episodes'][0]['boundary_notes']
    r['episodes'][0]['boundary_notes']=['与基线同一根治管理任务及起止范围；不按住院、文书或检验条目拆episode。',
        '有补取的新证据和当前条件评估时识别新的候选判断回合；同轮多份记录归组，真实未知仍局部保留。']
    preop_cp=next(c for c in cps if any(a['source_id']==plan['source_id'] and a['start']==plan['start'] for a in c['documented_decision']))
    preop['target_checkpoint_id']=preop_cp['checkpoint_id']
    for m in r['mixed_document_masks']:
        if m['mask_id'].startswith('INTRAOP'):
            proposed=operative_partition(byid[m['source_id']]);assert proposed['status']=='STAGE_PATTERN_PROPOSAL'
            m['segments']=proposed['segments'];m['mode']='retrospective_phase_reconstruction'
            m['clinical_phase']=proposed['clinical_phase'];m['target_checkpoint_id']=cps[-1]['checkpoint_id']
            m['late_content_review']=dict(span=proposed['late_content_review_span'],assessment=LATE_REVIEW[cid],additional_checkpoint_identified=False)
        elif m['mask_id']=='CHANGE-D019':m['target_checkpoint_id']=next(c['checkpoint_id'] for c in cps if c.get('baseline_checkpoint_id')=='SCALE-05-CP02')
        elif m['mask_id']=='WORKUP-D014':m['target_checkpoint_id']=next(c['checkpoint_id'] for c in cps if c.get('baseline_checkpoint_id')=='SCALE-09-CP01')
        validate_partition(byid[m['source_id']],m['segments'])
    r['changes_from_baseline']=changes
    r['baseline_issue_log_reference']=str(BASE/cid/'case_review.md')
    reassessed=[]
    for issue in r.pop('issues'):
        category=issue['category']
        status='PRESERVED_LOCAL_SOURCE_OBSERVATION_NOT_AUTOMATIC_EXCLUSION'
        if category=='duplicate_vs_renewed_choice':status='REASSESSED_USING_NEW_ORIGINAL_EVIDENCE_AND_CURRENT_CONDITION_ASSESSMENT'
        if category=='retrospective_timing':status='SOURCE_CLOCK_MEANINGS_SEPARATED; CLEAR_STAGE_RECONSTRUCTION_RETAINED'
        if cid=='SCALE-06' and category=='planned_test_result_not_established':status='CTA_REPORT_FOUND; OLD_RETRIEVAL_GAP_FIXED; CLINICAL_RISK_RESOLUTION_NOT_CLAIMED'
        reassessed.append(dict(baseline_issue_id=issue['issue_id'],category=category,current_status=status,baseline_support=issue['support']))
    r['issue_reassessment']=reassessed
    r['known_issue_updates']=dict(CTA_retrieval_gap='FIXED' if cid=='SCALE-06' else 'not_case_specific',
          time_field_semantics='source_create_time_and_body_record_time_separate; neither equals verified availability',
          operative_retrospective_source='usable_stage_reconstruction_when_semantics_support_it; not automatically an uncertain point',
          original_source_discrepancies='preserved; not adjudicated by final operation',
          old_issue_log='baseline historical audit retained in old directory; superseded descriptions are not new data-missing claims')
    r['coverage']=dict(source_family_inventory=json.loads((OUT/cid/'sources.json').read_text())['source_family_inventory'],
          baseline_clinical_full_read=old['coverage']['reviewed_full'],new_imaging_original_text_and_lab_trajectories_reviewed=True,
          all_lab_original_rows_projected_and_time_screened=True,all_patient_decisions_exhaustive=False,
          semantic_reviewer='same Codex assistant; not independent adjudication')
    return r

def audit_quote_occurrences(sources,quotes):
    result=[]
    for q in quotes:
        for s in sources:
            if s['kind']!='document':continue
            for m in re.finditer(re.escape(q),s['text']):result.append(dict(source_id=s['source_id'],start=m.start(),end=m.end(),quote=q))
    return result

def source_spans_for_current(cp,sources):
    byid={s['source_id']:s for s in sources}
    current=cp['documented_decision'][0];s=byid[current['source_id']];t=s['text']
    if '诊疗计划：' in t:
        end=t.index('诊疗计划：')
    elif '拟完善冠脉CTA检查。' in t:end=t.index('拟完善冠脉CTA检查。')
    elif '根据影像学检查及病情发展分析仍诊断' in t:end=t.index('根据影像学检查及病情发展分析仍诊断')
    else:end=current['start']
    assert end>0
    return {s['source_id']:[cite(s,t[:end],0)]}

def compile_packet(cid,r,sources,cp,view_id,mask=None):
    byid={s['source_id']:s for s in sources};ids=[c['checkpoint_id'] for c in r['checkpoints']];cp_i=ids.index(cp['checkpoint_id'])
    is_op=bool(mask and mask['mask_id'].startswith('INTRAOP'))
    operation_end=None
    if mask:
        s=byid[mask['source_id']]
        cutoff=re.search(r'手术时间：(\d{4}-\d\d-\d\d \d\d:\d\d)',s['text']).group(1)+':00' if is_op else s['clocks']['body_record_time']['value']
        if is_op:
            operation_end=re.search(r'手术时间：\d{4}-\d\d-\d\d \d\d:\d\d-(\d{4}-\d\d-\d\d \d\d:\d\d)',s['text']).group(1)+':00'
        spans={s['source_id']:[cite(s,seg['quote'],seg['start']) for seg in mask['segments'] if seg['disposition']=='KEEP']}
    else:
        s=byid[cp['documented_decision'][0]['source_id']]
        cutoff=s['clocks']['body_record_time']['value'];spans=source_spans_for_current(cp,sources)
    interval=s['clocks']['encounter_admission_time']['value']
    assert cutoff and interval,(cid,view_id)
    # Older true judgments remain visible for this later round; the current
    # round's other document copies are never reintroduced as prior decisions.
    for previous in r['checkpoints'][:cp_i]:
        for a in previous['evidence']+previous['documented_decision']:
            ps=byid[a['source_id']]
            if ps['kind']!='document':continue
            time=ps['clocks']['body_record_time']['value']
            if time and time<cutoff:
                spans.setdefault(ps['source_id'],[]).append(old_anchor(a,byid))
    if cid=='SCALE-02' and mask and mask['mask_id'].startswith('PREOP'):
        blood_source=next(byid[a['source_id']] for a in cp['evidence'] if '血型' in a['quote'])
        spans.setdefault(blood_source['source_id'],[]).append(cite(blood_source,'患者血型极为特殊'))
    if cid=='SCALE-05' and mask and mask['mask_id']=='CHANGE-D019':
        # A supporting plan remains historical context even when it was not
        # counted as an independent checkpoint. Do not equate episode with CPs.
        planned=next(x for x in sources if x.get('source_document_index')=='D015')
        q=sentence(planned['text'],'经与家属谈话并签字同意定于')
        spans.setdefault(planned['source_id'],[]).append(cite(planned,q))
    evidence_visibility=[]
    isolated={(a['source_id'],a['start'],a['end']) for a in cp.get('current_evidence_to_isolate',[])}
    for a in cp['evidence']:
        rs=byid[a['source_id']]
        if (a['source_id'],a['start'],a['end']) in isolated:
            evidence_visibility.append(dict(source_id=a['source_id'],quote=a['quote'],disposition='MASK',reason='CURRENT_SYNTHETIC_CONDITION_JUDGMENT_OR_CHOICE_MIXTURE; underlying source evidence retained separately'))
        elif rs['kind']=='document':
            existing=spans.get(rs['source_id'],[])
            if not any(x['start']<=a['start'] and x['end']>=a['end'] for x in existing):
                spans.setdefault(rs['source_id'],[]).append(old_anchor(a,byid))
            evidence_visibility.append(dict(source_id=a['source_id'],quote=a['quote'],disposition='KEEP',reason='CURRENT_PRECHOICE_ORIGINAL_OBSERVATION_NOT_TARGET_ACTION'))
    # Restore previously reviewed preop context for intraoperative replay.
    if is_op:
        pm=next(m for m in r['mixed_document_masks'] if m['mask_id'].startswith('PREOP'))
        ps=byid[pm['source_id']]
        for seg in pm['segments']:
            if seg['disposition']=='KEEP':spans.setdefault(ps['source_id'],[]).append(cite(ps,seg['quote'],seg['start']))
    ledger=[]
    for rs in sources:
        a=resource_selection(rs,cutoff,interval)
        if is_op and rs['kind'] in ['imaging','lab','pathology'] and a['disposition']=='EXCLUDE' and a['reason']=='AFTER_TARGET_PHASE':
            basis='exam_time' if rs['kind']=='imaging' else 'report_time'
            value=rs['clocks'][basis]['value']
            if value and cutoff<=value<=operation_end:
                a.update(disposition='REVIEW',reason='WITHIN_OPERATION_CLOCK_STAGE_UNRESOLVED; operation start is only ancillary-data proxy')
        if rs['kind']=='document':
            if rs['source_id'] in spans:a.update(disposition='INCLUDE_PARTIAL',reason='SOURCE_EXACT_SEMANTICALLY_REVIEWED_CURRENT_OR_PRIOR_PHASE_SPANS')
            else:
                bc=rs['clocks']['body_record_time'];state=relation_to_cutoff(bc,cutoff)
                a.update(disposition='EXCLUDE' if state=='AFTER' else 'REVIEW',reason='LATER_BODY_PHASE_NOT_USED' if state=='AFTER' else 'DOCUMENT_OUTSIDE_SELECTED_CLINICAL_SPANS_NOT_A_CLAIM_NO_INFORMATION')
        # Historical imaging in the same cancer workup remains relevant. The
        # older unrelated 2017 cervical episode in SCALE-06 is not promoted.
        elif rs['kind']=='imaging' and a['reason']=='EARLIER_CONTEXT_NOT_SILENTLY_DISCARDED':
            stamp=rs['clocks']['exam_time']['value']
            if not (cid=='SCALE-06' and stamp[:4]=='2017'):
                a.update(disposition='INCLUDE',reason='SEMANTIC_REVIEW_SAME_PATIENT_CANCER_WORKUP_HISTORICAL_REPORT_DATE_RETAINED')
            else:a.update(disposition='EXCLUDE',reason='SEMANTIC_REVIEW_PRIOR_DISTINCT_CERVICAL_EPISODE')
        if a['disposition']=='INCLUDE':
            spans[rs['source_id']]=[cite(rs,rs['text'],0)]
        ledger.append(a)
    # Remove duplicate selected spans only, retaining all distinct raw versions.
    for sid,aa in list(spans.items()):
        unique={(a['start'],a['end']):a for a in aa};spans[sid]=sorted(unique.values(),key=lambda a:a['start'])
    forbidden=[a['quote'] for a in cp['documented_decision']]
    blocks,provenance=compile_view(sources,spans,forbidden)
    for b,p in zip(blocks,provenance):
        rs=byid[p['source_id']]
        if rs['kind']=='imaging':
            number=rs['provenance'].get('exam_no_raw')
            group=sha(str(number)+'|'+str(rs['clocks']['exam_time']['value'])+'|'+str(rs['title']))[:16] if number else rs['source_id']
            b.update(source_exam_time=rs['clocks']['exam_time']['raw'],source_method=rs['title'],same_exam_group=group,
                     source_note='同检查可能有多个交付文本；原文保留，不按文本数当独立检查。检查时间不等于已认证报告可见时间。')
        if rs['kind']=='lab':b['source_report_time']=rs['clocks']['report_time']['value']
    phase='after_exploration_before_current_choice' if is_op else 'before_current_assessment_choice'
    view=dict(packet_id=view_id,question=mask['target_question'] if mask else '根据这些当阶段资料，下一步有哪些可接受的根治路径管理方向、还需补充什么？',
              clinical_phase=phase,sources=blocks,
              coverage_note='按本轮问题与阶段提供交付原始资料及既往安排；缺口不等于阴性，计划不等于已执行。',
              timing_note='回顾性阶段重建；源创建、检查及报告时间分别保留其含义，未认证医生实际阅读时刻。')
    output=OUT/cid/'visible_inputs';output.mkdir(exist_ok=True)
    path=output/(view_id+'.json');data=(json.dumps(view,ensure_ascii=False,indent=2)+'\n').encode();path.write_bytes(data)
    occurrences=audit_quote_occurrences(sources,forbidden)
    for row in ledger:
        if row['source_id'] in spans and row['kind']!='document':assert row['disposition']=='INCLUDE'
    selected_ids=set(spans)
    # Imaging/lab clocks are screened independently of source creation time.
    for sid in selected_ids:
        rs=byid[sid]
        if rs['kind'] in ['imaging','lab','pathology']:
            basis='exam_time' if rs['kind']=='imaging' else 'report_time'
            assert relation_to_cutoff(rs['clocks'][basis],cutoff)=='BEFORE'
    audit=dict(packet_id=view_id,checkpoint_id=cp['checkpoint_id'],baseline_view_id=view_id if view_id.startswith('VIEW-') else None,
               source_selection=ledger,source_provenance=provenance,current_choice_occurrences_in_all_original_documents=occurrences,
               current_evidence_visibility=evidence_visibility,
               cutoff_proxy=cutoff,clinical_phase=phase,current_document_source_id=s['source_id'],source_admission_context_start=interval,
               doctor_view_file=str(path),agent_view_file=str(path),clinical_view_sha256=hashlib.sha256(data).hexdigest(),
               source_family_selected=dict(collections.Counter(byid[sid]['kind'] for sid in selected_ids)),
               source_retrieval_coverage_complete=True,complete_clinical_state_certified=False,
               release_status='DEVELOPMENT_RECONSTRUCTION_NOT_EXPERT_REFERENCE')
    return audit

def main():
    manifest=json.loads((BASE/'sampling_manifest.json').read_text());summaries=[];all_packets=[];point_diffs=[]
    for chosen in manifest['selected']:
        cid=chosen['case_id'];sources=load_case(cid);byid={s['source_id']:s for s in sources}
        r=review_case(cid,sources);packets=[];covered=set()
        for j,m in enumerate(r['mixed_document_masks'],1):
            cp=next(c for c in r['checkpoints'] if c['checkpoint_id']==m['target_checkpoint_id'])
            packets.append(compile_packet(cid,r,sources,cp,f'VIEW-{cid[-2:]}-{j:02}',m));covered.add(cp['checkpoint_id'])
        for c in r['checkpoints']:
            if c['checkpoint_id'] not in covered:packets.append(compile_packet(cid,r,sources,c,'CPVIEW-'+c['checkpoint_id']))
        # Point alignment uses source + exact span/quote, not sequential IDs.
        old=json.loads((BASE/cid/'trial_result.json').read_text())
        for oc in old['checkpoints']:
            key={(a['source_id'],a['start'],a['end'],a['quote']) for a in oc['documented_decision']}
            matches=[c for c in r['checkpoints'] if key<={(a['source_id'],a['start'],a['end'],a['quote']) for a in c['documented_decision']}]
            assert len(matches)==1
            nc=matches[0]
            point_diffs.append(dict(case_id=cid,old_id=oc['checkpoint_id'],new_id=nc['checkpoint_id'],alignment='source_and_verbatim_choice_span',
                                    change='support_updated' if nc['evidence']!=oc['evidence'] or nc['status']!=oc['status'] or nc['documented_decision']!=oc['documented_decision'] else 'same_candidate',
                                    old_status=oc['status'],new_status=nc['status']))
        for c in r['checkpoints']:
            if c.get('baseline_checkpoint_id') is None:point_diffs.append(dict(case_id=cid,old_id=None,new_id=c['checkpoint_id'],change='added_assessment_round',reason=c['evolution'],evidence=c['evidence'],documented_decision=c['documented_decision']))
        r['input_packets']=[p['packet_id'] for p in packets]
        (OUT/cid/'trial_result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
        (OUT/cid/'packet_audit.json').write_text(json.dumps(packets,ensure_ascii=False,indent=2)+'\n')
        (OUT/cid/'episode_replay.json').write_text(json.dumps(dict(episode=r['episodes'][0],ordered_checkpoints=r['checkpoints'],late_operative_review=next(m['late_content_review'] for m in r['mixed_document_masks'] if m['mask_id'].startswith('INTRAOP')),scope='same main target task; not exhaustive all-patient episode inventory'),ensure_ascii=False,indent=2)+'\n')
        selected=set(a['source_id'] for p in packets for a in p['source_provenance'])
        summaries.append(dict(case_id=cid,old_checkpoints=len(old['checkpoints']),new_checkpoints=len(r['checkpoints']),episodes=len(r['episodes']),
                              input_packets=len(packets),baseline_matched_views=sum(p['baseline_view_id'] is not None for p in packets),
                              selected_original_source_rows=dict(collections.Counter(byid[s]['kind'] for s in selected)),
                              selected_image_exam_groups=len({(byid[s]['provenance'].get('exam_no_raw'),byid[s]['clocks']['exam_time']['value'],byid[s]['title']) for s in selected if byid[s]['kind']=='imaging'})))
        all_packets.extend(dict(case_id=cid,**{k:v for k,v in p.items() if k not in ['source_selection','source_provenance','current_choice_occurrences_in_all_original_documents']}) for p in packets)
        print(json.dumps(summaries[-1],ensure_ascii=False),flush=True)
    (OUT/'case_summary.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2)+'\n')
    (OUT/'packet_manifest.json').write_text(json.dumps(all_packets,ensure_ascii=False,indent=2)+'\n')
    (OUT/'checkpoint_diff.json').write_text(json.dumps(point_diffs,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':main()
