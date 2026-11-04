"""Validate the completed automatic run and compare to the old development list."""
import argparse
from collections import Counter
import hashlib
from pathlib import Path

from .extract import digest, load_sources, read, time_order, write
from .run import ROOT, locate_bundle


def create(run, output, manifest):
    rows=read(manifest)['selected']; summaries=[]; checks=[]; alignment=[]; all_events=[]
    for row in rows:
        cid=row['case_id']; p=run/cid
        summary=read(p/'summary.json'); summaries.append(summary)
        if not summary['fhir_validation']['official_r4_json_schema'] or not summary['source_fhir_validation']['official_r4_json_schema']:
            raise ValueError('FHIR_NOT_VALIDATED')
        sources,_=load_sources(locate_bundle(row['patient_uid']),row['patient_uid']); byid={s['source_id']:s for s in sources}
        events=read(p/'decision_events.json'); rules=read(p/'rule_candidates.json')
        for e in events:
            all_events.append(dict(case_id=cid,**e))
            for a in e['semantic']['supports']+e['evolution'].get('supports',[])+e['evolution'].get('connection_supports',[]):
                s=byid[a['source_id']]
                if s['source_sha256']!=a['source_sha256'] or s['text'][a['start']:a['end']]!=a['quote']:
                    raise ValueError('QUOTE_OR_HASH_MISMATCH')
            for page in e['evolution'].get('paging',{}).get('page_results',[]):
                for a in page['selections']:
                    s=byid[a['source_id']]
                    if s['source_sha256']!=a['source_sha256'] or s['text'][a['start']:a['end']]!=a['quote']:
                        raise ValueError('PAGED_QUOTE_OR_HASH_MISMATCH')
            win=read(p/'window_audits'/f'{e["candidate_id"]}.json')
            for b in win['blocks']:
                a=b['anchor'];s=byid[a['source_id']]
                if s['text'][a['start']:a['end']]!=a['quote']:raise ValueError('WINDOW_QUOTE_MISMATCH')
                if b['interval']=='current_prechoice_prefix':
                    if a['end']>e.get('choice_start',e['anchor']['start']):raise ValueError('CURRENT_CHOICE_IN_PRIOR_PREFIX')
                elif time_order(b['clock'],win['cutoff'])!='BEFORE':
                    raise ValueError('UNORDERED_OR_FUTURE_SOURCE_IN_WINDOW')
        checks.append(dict(case_id=cid,source_and_annotation_fhir_schema=True,all_quotes_and_hashes_match=True,
                           windows_use_strict_prior_clocks_or_current_prior_phase=True,
                           complete_clinical_state_certified=False))
        # Post-run comparison only. The old manually selected list never enters
        # the extraction or model prompts, and is not treated as gold.
        old=read(ROOT/'private/p2_pathway_checkpoint_scale10_v0.2_20261024'/cid/'trial_result.json')
        for c in old['checkpoints']:
            matched=[]
            for a in c['documented_decision']:
                matched += [r['candidate_id'] for r in rules if r['source_id']==a['source_id'] and r['anchor']['start']<a['end'] and r['anchor']['end']>a['start']]
            matched=sorted(set(matched)); outcomes=[e for e in events if e['candidate_id'] in matched]
            alignment.append(dict(case_id=cid,old_checkpoint_id=c['checkpoint_id'],
                                  rule_candidate_ids=matched, rule_anchor_found=bool(matched),
                                  automatic_statuses=[e['checkpoint_status'] for e in outcomes],
                                  automatic_candidate_retained=any(e['checkpoint_status'].startswith('CANDIDATE_') for e in outcomes)))
    count=Counter(e['checkpoint_status'] for e in all_events)
    total=dict(cases=len(summaries),rule_candidates=sum(s['rule_candidates'] for s in summaries),
               original_imaging_rows=sum(s['report_rows'] for s in summaries),
               source_inventory=dict(sum((Counter(s['source_inventory']) for s in summaries),Counter())),
               normalized=sum(s['normalized'] for s in summaries),normalization_failures=sum(s['normalization_failures'] for s in summaries),
               semantic_decision_candidates=len(all_events),candidate_checkpoints=sum(s['candidate_checkpoints'] for s in summaries),
               candidate_episodes=sum(s['candidate_episodes'] for s in summaries),checkpoint_statuses=dict(count),
               paged_evidence_windows=sum(s.get('paged_evidence_windows',0) for s in summaries),
               evidence_pages=sum(s.get('evidence_pages',0) for s in summaries),
               old_development_points=len(alignment),old_anchors_found_by_rule=sum(a['rule_anchor_found'] for a in alignment),
               old_points_with_retained_automatic_candidate=sum(a['automatic_candidate_retained'] for a in alignment),
               accuracy_or_recall_measured=False,old_list_is_not_gold=True,
               evidence_stage_statuses=dict(Counter(e['evolution']['status'] for e in all_events)),
               semantic_technical_failures=sum(e['evolution']['status']=='FAILED_OR_INVALID_QUOTES' for e in all_events),
               input_prerequisite_uncertain=sum(e['evolution']['status']=='NOT_EVALUATED_PREREQUISITE_UNCERTAIN' for e in all_events),
               context_budget_deferred=sum(e['evolution']['status']=='DEFERRED_CONTEXT_BUDGET' for e in all_events))
    output.mkdir(parents=True,exist_ok=True)
    write(output/'summary.json',total);write(output/'integration_checks.json',checks)
    write(output/'old36_alignment.json',alignment);write(output/'all_automatic_decision_events.json',all_events)
    text=['# 自动候选与证据演化开发试跑','', '这是程序提候选、真实LLM归类和判断证据演化的开发运行；不是人工选点，不是独立gold或临床验证。固定种子20261023的同10例，未用P1/实际手术标签筛选。原样保留此前开发结果。','',
          '算法契约：docs/P2-自动候选与证据演化-algorithm-contract-v0.1-20261024.md。实际代码：scripts/cohort_construction/p2_auto_checkpoint/。模型沿用项目配置kimi-k3，第三方OpenAI兼容接口；每个候选隔离调用。缓存仅按完整请求hash复用，真实调用回执和返回内容可回溯。','',
          '## 自动流程','', '原始来源 → FHIR R4来源Bundle → 回读原文与时钟并核对hash → 文书类型和原文动作/主题词产生候选 → LLM判角色及根治路径关联 → 选择前累计资料及两轮间新增资料 → LLM判证据演化 → 候选CP、候选管理过程和FHIR研究注释。','',
          '## 实际数量','', '|项目|数量|','|---|---:|']
    for key,label in [('cases','病例'),('rule_candidates','规则候选（含重复/模板，非CP）'),('original_imaging_rows','独立影像来源行（含版本，非独立检查数）'),('normalized','通过角色归类引用校验'),('normalization_failures','归类失败/引用未通过'),('semantic_decision_candidates','进入证据演化环节的相关或待核候选'),('candidate_checkpoints','按本版两原则保留的候选CP'),('candidate_episodes','候选管理过程片段（非确认episode）')]:text.append(f'|{label}|{total[key]}|')
    text+=['','|病例|规则候选|归类失败|语义候选|候选CP|过程片段|','|---|---:|---:|---:|---:|---:|']
    for s in summaries:text.append(f"|{s['case_id']}|{s['rule_candidates']}|{s['normalization_failures']}|{s['semantic_decision_candidates']}|{s['candidate_checkpoints']}|{s['candidate_episodes']}|")
    text+=['','## 候选状态','']+[f'- {k}：{v}' for k,v in count.items()]
    text+=['','## 对旧36点的定位比较','',f"旧36点中，{total['old_anchors_found_by_rule']}个的实际选择原文与自动规则候选相交；{total['old_points_with_retained_automatic_candidate']}个对应至少一个自动保留候选。该比较仅用于开发审计，不能称召回率或F1：旧36点本身不完整、不是独立gold，事件粒度也不同。详细未匹配和状态在old36_alignment.json。",'',
           '## SCALE-06：PET-MRI与CTA','']
    for e in all_events:
        if e['case_id']=='SCALE-06' and ('PET_MRI' in e['topics'] or 'CTA' in e['topics']):
            text += [f"- {e['candidate_id']}：{e['semantic']['action']}；状态 {e['checkpoint_status']}；时间 {e['decision_clock']['value']}。",'']
    text+=['## 实际新增的管理线索示例','']
    for e in all_events:
        if e['case_id']=='SCALE-02' and ('甲亢' in e['anchor']['quote'] or '暂缓' in e['anchor']['quote']):
            text += [f"- {e['candidate_id']}：{e['semantic']['action']}；{e['checkpoint_status']}。",'']
    text+=['## 验证和限制','', '所有10例来源及研究注释Bundle通过官方FHIR R4 JSON Schema；来源回读的原文hash一致，所有模型引用逐字匹配，窗口中每条普通来源严格早于当前切点，当前同文只留选择前片段。验证不等于完整FHIRPath/术语/机构Profile认证，也不认证医生真实阅读时刻。','',
           '本次没有生成正式盲评包。重建语义模块知道当前实际选择；不能直接把其输入给医生或待测Agent。临床状态完整性、语义准确性、候选召回和最有价值CP排序尚未验证。','',
           '已观察到的边界：逐句候选可能把同轮多项安排拆开；前一轮采用最近保留判断，面对并行管理问题可能不合适；部分全文/历史检查时间不可定位；真实缺口、模型引用失败、API失败和超预算分别保留。部分术后支持处理是否属于本研究范围仍需独立评估。旧样本恰有切除记录，不能把试跑结果外推到未手术或长期暂缓患者。','',
           '本版运行过程中修复了FHIR研究JSON/显示标题中全角空白的编码问题；原始文字与原始标题仍无损保存。具体反例和测试登记在regression_catalog.json。早期中断运行保留作开发日志，不作为最终结果。','']
    (output/'report.md').write_text('\n'.join(text))
    inputs=[ROOT/'config/p2_auto_checkpoint_v01.json',Path(manifest)]+list((ROOT/'scripts/cohort_construction/p2_auto_checkpoint').glob('*.py'))
    write(output/'receipt.json',dict(code_and_input_sha256={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
          source_run=str(run),checks=checks,model='configured kimi-k3; API responses cached by complete request hash',
          clinical_validation=False,manual_checkpoint_list_used_for_generation=False))
    return total


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--run',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--manifest',type=Path,required=True)
    a=ap.parse_args();print(create(a.run,a.output,a.manifest))
