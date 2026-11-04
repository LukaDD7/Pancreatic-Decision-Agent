"""Write inspectable cause, source/input/point diff and final run receipt."""
import collections
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
from .core import cite
from .prepare_scale10 import ROOT,BASE,OUT

def save(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def main():
    cases=json.loads((OUT/'case_summary.json').read_text())
    points=json.loads((OUT/'checkpoint_diff.json').read_text())
    inputs=json.loads((OUT/'input_diff.json').read_text())
    integrity=json.loads((OUT/'integration_checks.json').read_text())
    packets=json.loads((OUT/'packet_manifest.json').read_text())
    stats=dict(patients=10,old_episodes=10,new_episodes=sum(c['episodes'] for c in cases),old_candidates=28,new_candidates=sum(c['new_checkpoints'] for c in cases),
               old_candidates_aligned=sum(d['old_id'] is not None for d in points),added_assessment_rounds=sum(d['old_id'] is None for d in points),
               old_matched_views=len(inputs),new_input_packets=len(packets),old_independent_original_imaging_rows=0,
               new_independent_original_imaging_rows=sum(c['selected_original_source_rows'].get('imaging',0) for c in cases),
               new_image_exam_groups=sum(c['selected_image_exam_groups'] for c in cases),old_independent_lab_rows=0,
               new_independent_lab_rows=sum(c['selected_original_source_rows'].get('lab',0) for c in cases),
               source_characters_removed_from_matched_views=sum(d['removed_source_characters'] for d in inputs),
               original_source_inventory=integrity['source_inventory_totals'],unit_tests=18,integration_checks=integrity['check_count'])
    summary=dict(status='COMPLETED_DEVELOPMENT_RETEST',date='2026-10-24',core_principles_unchanged=True,stats=stats,
                 change_layers=dict(source_processing='FIXED: document-only projection, source-family coverage guard, clock semantics and source-exact packet assembly',
                                    checkpoint_operation='REVISED: explicit preop condition-assessment rounds supported by newly retrieved prior evidence; same-round copies grouped',
                                    episode_membership='Same ten main management tasks; no automatic split by admission/report count',
                                    clinical_reference='Not generated; no treatment correctness assessment'),
                 interpretation='Eight additions are candidate operational reassessments, not eight proven algorithm misses or a measured accuracy improvement. All source/model/clinical uncertainty is not collapsed into a single counter.',
                 semantic_role='Same Codex assistant semantic reinspection with explicit source spans; no independent second model or physician adjudication.')
    save('scale_summary.json',summary)

    rootcause=dict(issue_id='P2SRC-001',case_id='SCALE-06',classification='SOURCE_FAMILY_NOT_INGESTED',
       upstream_source='imaging_all; all-original:A092.csv.record_clusters.jsonl:6496; exam_no_raw 5607657',
       exact_omission_site=dict(path=str(BASE/'prepare_originals.py'),line=21,code="rows=json.loads(payload).get('documents_all',[])",
                               effect='Only document_text rows entered sources.json; every independent imaging/lab report was outside the source universe.'),
       scope_origin=dict(path=str(BASE/'trial_protocol.json'),line=5,explanation='The assistant chose a document-only minimal trial and explicitly deferred imaging/lab audit. That shortcut did not satisfy the clinical-state source coverage needed here.'),
       downstream=dict(path=str(BASE/'compile_and_check.py'),line=35,explanation='The compiler received only sources(case) and selected reviewed source spans; no missing-family guard followed a planned CTA into its original result.'),
       not_demonstrated_causes=['No CTA keyword exclusion was applied to this report.','No pancreatic-only clinical relevance rejection was applied.','No timestamp or mask stage evaluated then removed this CTA; it never reached those stages.'],
       detection_gap='Old checks validated hashes/coverage only within the already projected document universe, not whether original source families had been omitted.',
       regression='CTA imported and present in 28 September later assessment; excluded from the 27 September request; adverse troponin and echo retained.')
    save('CTA_root_cause.json',rootcause)

    source_issues=[]
    def discrepancy(cid,sid,quotes,description):
        ss=json.loads((OUT/cid/'sources.json').read_text())['sources'];s=next(x for x in ss if x['source_id']==sid)
        source_issues.append(dict(case_id=cid,status='PRESERVED_LOCAL_SOURCE_UNCERTAINTY',description=description,evidence=[cite(s,q) for q in quotes]))
    discrepancy('SCALE-03','84db0da16f1432197f972d5a',['1.与肠系膜上动脉无明显接触；1.与肠系膜上动脉接触面。'],'原始所见内有未完成/并列措辞，不补成阳性或阴性；不同交付文本保留。')
    discrepancy('SCALE-08','9d7b31c10ac8928a6a911770',['1.与肠系膜上动脉无明显接触。','2.与腹腔干动脉接触＞180°。','与血管接触：肠系膜上动脉≤180°。'],'同一报告所见与结论的血管对象/角度表述需核，不由实际手术改写。')
    source_issues.extend([
       dict(case_id='SCALE-02',status='SPECIFIC_CITATION_NOT_FULLY_RESOLVED',description='病程引用9月7日的20×19mm检查，已交付相似CT原文日期为9月6日；存在原报告不等于特定引用已匹配。两种来源/日期并存。'),
       dict(case_id='SCALE-03',status='SPECIFIC_CITATION_NOT_FULLY_RESOLVED',description='病程写10月31日CT，当前交付23×22mm CT原文为10月29日；保留候选对应与日期差异。'),
       dict(case_id='SCALE-04/06/10',status='ORIGINAL_LAB_TIME_ORDER_REVIEW',description='124行源隔离记录原因为INVALID_SPECIMEN_REPORT_ORDER，逐条保留REVIEW；不是患者未做检验，也未在本模块擅自修P1。'),
       dict(case_id='ALL',status='EXACT_AVAILABILITY_NOT_CERTIFIED',description='报告存在、检查时间、创建时间与医生实际阅读分别记录；本轮为回顾性阶段重建。'),
       dict(case_id='ALL',status='GENERALIZATION_SCOPE_UNCHANGED',description='相同十例都有切除记录；未手术/长期暂缓/反复PTCD仍未在这次复测覆盖。')])
    save('remaining_issue_log.json',source_issues)

    table='\n'.join(f"| {c['case_id']} | {c['old_checkpoints']} → {c['new_checkpoints']} | {c['selected_image_exam_groups']} / {c['selected_original_source_rows'].get('imaging',0)} | {c['selected_original_source_rows'].get('lab',0)} |" for c in cases)
    headline=f'''# 同一10例优化复测与diff v0.2

2026-10-24。固定种子20261023、原10例、主管理任务及原始容器不变。旧版目录、两份核心原则、Manual及A/B/C保持原样。新版代码在`scripts/cohort_construction/p2_pathway_replay/`；此处是同一AI审阅者的开发复测，不是专家gold或临床验证。

## CTA为何没有进入旧输入

已定位为**导入范围缺口**。旧`prepare_originals.py:21`只读`documents_all`，旧协议也将来源限定为文书。CTA实际位于`imaging_all`，从未进入旧`sources.json`，后续编译器无法取到它。它没有经历“CTA不相关”“时间太晚”或mask判断后再被排除。

这是我当时把最小试验收窄成只读文书造成的处理问题。旧检查只验证已导入来源内部的hash/坐标，没有检查漏掉了整个来源类别，因而45项通过未能发现它。可审查链条见[CTA_root_cause.json]({OUT/'CTA_root_cause.json'})。

新版导入5类原始来源并逐来源留理由，CTA原文进入后续术前/术中state；在前一天“拟查CTA”的切点仍被挡住。原来的肌钙蛋白2.224/2.016及心超表述同时保留，没有因CTA结论而认定风险消除。

## 已实施的变化

| 层级 | 已实施 | 没有据此宣称 |
|---|---|---|
| 来源处理修复 | 文书、影像原文、原始检验单元格、病理、隔离源统一登记；每份输入对每个来源给纳入/REVIEW/排除理由 | 病程中已有转述等于全部原文；新增报告等于新增决策 |
| 时间处理修复 | 创建、正文、检查、采样、报告时钟与精度分开；同日无序不补午夜；术中按已核对阶段处理 | 创建时间等于完整内容可见；精确签署/医生阅读时刻已认证 |
| mask与组包修复 | 保留原句及全部支持性原始结果；按当前问题隔离答案/副本和下游；不计独立点的旧计划仍能进入后次state | 原文分区通过即已完成独立语义盲审或隔离工具权限 |
| 候选点操作标准调整 | 同一问题出现新相关资料，且原文有当前条件评估与安排，保留为候选回合；同轮查房/小结归组 | 8个新增均为已证实漏检，或模型准确率提高 |
| episode组织 | 仍为10条主任务链，跨住院承接保留；扫描术中后段，无额外有充分依据的新点 | 报告/手术步骤/住院数就是episode或checkpoint数 |

核心两原则不变；候选保留的操作展开已经变动，必须单独记为v0.2差异，不能仅称“补了CTA”。

## 数量diff

| 项目 | v0.1 | v0.2 |
|---|---:|---:|
| 同批患者/主episode | 10 / 10 | 10 / 10 |
| 候选checkpoint | 28 | 36 |
| 旧候选点按source+逐字跨度保留 | — | 28/28 |
| 新候选术前条件评估回合 | — | 8 |
| 术中探查候选点 | 10 | 10 |
| 独立导入并用于输入的影像原文行 | 0 | 63（31个按检查号/时间/方法组织的检查组） |
| 独立导入并用于输入的原始检验行 | 0 | 1772 |
| 匹配旧可见视图重跑 | 22 | 22/22 |
| 全部候选点对应输入 | 未逐点封装 | 36份，每点1份 |

“旧0”仅指独立影像/检验来源没有导入，旧病程中一直有二手转述。63是交付文本行数，含同检查不同文本，不能说成63次检查；不择最新、最大或看似合理的版本当真值。

| 病例 | 候选点变化 | 影像检查组/原文行 | 原始检验行（各切点并集） |
|---|---:|---:|---:|
{table}

8个新增均在既有管理任务内：01、03、04、05、07、08、09、10各增加一次。不是新发现8份文书：旧版已经把相应术前小结用于mask试验，只是未把这些回合列为独立候选。现在补取原始资料，并统一要求“有当前条件评估、有在前的新相关证据”，逐项给出处，详见[checkpoint_diff.md]({OUT/'checkpoint_diff.md'})。

SCALE-02特殊血型条件与随后小结合为同轮，点数仍3；SCALE-06原CP03补入CTA/MR后支持增强，仍4点。这里的context_supported表示支持候选回放，不是医生confirmed，也不证明医生主观因果。

## 可直接查看的输入变化

- [SCALE-06术前输入]({OUT/'SCALE-06/visible_inputs/VIEW-06-01.json'})：CTA结果、异常肌钙蛋白、心超与其它原始检验同时可见；当前安排隐藏。
- [SCALE-10术前输入]({OUT/'SCALE-10/visible_inputs/VIEW-10-01.json'})：15:45 CT可见，16:57 MRI晚于16:32正文切点，未提前。该MRI进入[后续术中输入]({OUT/'SCALE-10/visible_inputs/VIEW-10-02.json'})。
- [SCALE-07术前输入]({OUT/'SCALE-07/visible_inputs/VIEW-07-01.json'})：25日下午CT/MR未被倒放到上午正文切点。
- [SCALE-05取消判断输入]({OUT/'SCALE-05/visible_inputs/VIEW-05-03.json'})：保留此前真实手术计划与新动脉所见，隐藏本次取消答案。

对旧22份视图比较的是原文source+字符覆盖，不只比较JSON格式或hash。22份均补入原始来源，旧版已显示的源文字符没有被删除；新增源文字符量逐份见[input_diff.json]({OUT/'input_diff.json'})。这不证明所有新增材料在临床实际当时都已被阅读，时钟代理和局部未解状态仍保留。

## 检查及仍未解决的边界

18项有正反例的回归测试通过，{integrity['check_count']}项整合断言通过，迁移了基线的原文、既往计划、MRI回收、受侵保留和当前答案隔离等保护，并加入未来报告/采样与报告时间/创建时间/缺来源类别等反例。旧结果及原始容器hash未变，见[integration_checks.json]({OUT/'integration_checks.json'})。

本轮仍保留：SCALE-02/03特定影像引用日期与交付原文日期的对应待核；SCALE-03/08原文内部未完成或所见/结论差异；124行隔离检验的源采样/报告时间顺序问题；未认证的真实报告可见时间；以及仅覆盖有切除记录病例的外推限制。具体出处见[remaining_issue_log.json]({OUT/'remaining_issue_log.json'})。没有通过后续手术或结局消除这些未知。

每个点的输入文件与生成来源审计分别保存；医生与Agent指向相同字节文件。当前是有来源审计的开发重建材料，不是独立临床Reference。P1、提取标签和既有标注手册未改，候选语义仍由同一AI审阅者提出；本次没有独立复标或测量临床准确率。
'''
    (OUT/'diff_report.md').write_text(headline)
    lines=['# 候选点逐项diff','按source+原文逐字跨度对齐，不按可能移位的CP序号。新增为候选操作标准及来源覆盖共同变化，不能称专家gold。']
    for p in points:
        lines += [f"## {p['case_id']} / {p.get('old_id')} → {p['new_id']} / {p['change']}"]
        if p['change']=='added_assessment_round':
            lines += [p['reason'],'新增依据及当前条件判断/安排：']
            for a in p['evidence']+p['documented_decision']:
                lines += [f"`{a['source_id']}` Unicode [{a['start']},{a['end']}) / SHA256 `{a['source_sha256']}`",'```text\n'+a['quote']+'\n```']
        else:lines += [f"{p['old_status']} → {p['new_status']}；旧判断原文未删除。"]
    (OUT/'checkpoint_diff.md').write_text('\n\n'.join(lines)+'\n')
    oldfile=BASE/'prepare_originals.py';lns=oldfile.read_text().splitlines()
    assert "get('documents_all',[])" in lns[20]
    (OUT/'CTA_root_cause.md').write_text(f'''# P2SRC-001 CTA遗漏根因

旧第21行原句：

```python
{lns[20]}
```

CTA在患者容器的imaging_all；该行只形成documents_all投影，CTA没有来源条目，后续只能使用已投影文书。因此本例不是读了CTA再按临床相关性/时间/mask排除，而是导入范围缩窄造成整类漏取。

旧试验协议第5行明确只投影文书；这是当时AI试验设计的简化，责任在处理流程。旧检查只在该来源宇宙内校验，没有来源类别覆盖守卫。新版对5类来源登记，并逐输入逐来源给出理由，回归验证CTA既补入后续state又不提前进入此前请求。原文、旧实验输出保持冻结。

详细链条与源码位置：[CTA_root_cause.json]({OUT/'CTA_root_cause.json'})。
''')
    # Keep source clock and source family evidence readable without an LLM summary.
    for c in cases:
        cid=c['case_id'];r=json.loads((OUT/cid/'trial_result.json').read_text())
        review=['# '+cid+' v0.2复测','制作者审计含实际判断，不能整页作为盲评输入。',r['task']]
        for cp in r['checkpoints']:
            review += ['## '+cp['checkpoint_id']+' '+cp['label'],cp['evolution']]
            for a in cp['evidence']+cp['documented_decision']:review += [f"来源 `{a['source_id']}` [{a['start']},{a['end']})",'```text\n'+a['quote']+'\n```']
        (OUT/cid/'case_review.md').write_text('\n\n'.join(review)+'\n')
    save('change_record.json',dict(date='2026-10-24',source_processing_repairs=['P2SRC-001','P2SRC-002','P2SRC-003','P2SRC-005','P2SRC-006','P2SRC-007','P2SRC-008'],
         checkpoint_operation_revision='P2SRC-004: current condition-assessment round plus before-cutoff new related original evidence; same-round copies grouped',
         patient_or_P1_gate_changed=False,core_principle_files_changed=False,clinical_reference_generated=False,
         old_artifacts_immutable=True,tests=dict(unit=18,integration=integrity['check_count']),
         implementation_findings_fixed=['Current echo source must not disappear when isolating the current action.','Earlier supporting plans remain context even if not independent checkpoints.','Lab retrieval starts at the source encounter, not the first identified checkpoint.','Within-operation ancillary results stay REVIEW if stage order is unknown.']))
    print(json.dumps(stats,ensure_ascii=False))

if __name__=='__main__':main()
