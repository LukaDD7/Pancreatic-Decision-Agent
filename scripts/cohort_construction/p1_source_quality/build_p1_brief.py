"""Build an aggregate P1 brief from frozen runs; no patient rows are exported."""
from pathlib import Path
from collections import Counter, defaultdict
import argparse
import csv
import hashlib
import json
import statistics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--runs-root', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    v1 = args.runs_root / 'p1_quality_v0.1/results/summary.json'
    base = args.runs_root / 'p1_quality_v0.2'
    audit_path = args.runs_root / 'p1_pass_audit_20261019/summary.json'
    rule_path = base / 'quality_rules.json'
    index_path = base / 'inputs/P0_index_snapshot.csv'
    patient_path = base / 'results/patient_quality.jsonl'
    report_path = base / 'results/report_quality.jsonl'
    summary_path = base / 'results/summary.json'
    summary = json.loads(summary_path.read_text())
    audit = json.loads(audit_path.read_text())
    size_path = audit_path.with_name('size_consistency_summary.json')
    cross_path = audit_path.with_name('cross_report_consistency.json')
    size_audit = json.loads(size_path.read_text())
    cross_audit = json.loads(cross_path.read_text())
    assert size_audit['patients_with_different_size'] == 73
    assert size_audit['patients_with_large_difference'] == 21
    assert cross_audit['same_exam_no_opposite_relation_groups'] == 1
    assert cross_audit['patients_with_same_day_CT_MR_opposite_relation'] == 121
    with index_path.open(encoding='utf-8-sig') as f:
        index = list(csv.DictReader(f))
    patients = [json.loads(s) for s in patient_path.read_text().splitlines() if s.strip()]
    passed = {p['patient_uid']: p for p in patients if p['state'] == 'PASS_V2'}
    assert len(index) == len(patients) == 2707
    assert len(passed) == audit['passed_patients'] == 2004
    modalities = defaultdict(set)
    vascular = defaultdict(set)
    distant_positive = set()
    template_count = report_count = 0
    with report_path.open() as f:
        for line in f:
            r = json.loads(line)
            uid = r['patient_uid']
            if uid not in passed or r['quality_state'] != 'PASS':
                continue
            assert r['quality_report_id'] in passed[uid]['qualifying_report_ids']
            report_count += 1
            p = r['parsed']
            modalities[uid].add(p['modality'])
            template_count += bool(p['template_detected'])
            for name, vessel in p['vessels'].items():
                if vessel['state'] == 'CONTACT_OR_INVASION':
                    vascular[name].add(uid)
            if p['metastatic_domain'].get('distant', {}).get('state') == 'METASTATIC_STATEMENT':
                distant_positive.add(uid)
    assert report_count == audit['qualified_content_versions_in_passed_patients'] == 7794
    modality_counts = Counter('+'.join(sorted(v)) for v in modalities.values())
    assert sum(modality_counts.values()) == 2004
    diagnoses = {
        'P0': dict(Counter(p['诊断依据层'] for p in index)),
        'P1': dict(Counter(p['诊断依据层'] for p in index if p['patient_uid'] in passed)),
    }
    vascular_any = len(set().union(*vascular.values()))
    inputs = [v1, rule_path, index_path, patient_path, report_path, summary_path, audit_path, size_path, cross_path]
    result = {
        'date': '2026-10-19',
        'algorithm': 'p1-source-quality-v0.2',
        'P0_n': 2707,
        'states': summary['patient_states'],
        'diagnosis_evidence': diagnoses,
        'qualified_report_versions_in_passed_patients': report_count,
        'modality_patient_counts': dict(modality_counts),
        'structured_template_versions': template_count,
        'median_qualified_clinical_notes': statistics.median(p['qualified_clinical_notes'] for p in passed.values()),
        'any_history_vascular_contact_or_invasion_statement_patients': vascular_any,
        'any_history_vascular_contact_or_invasion_by_vessel': {k: len(v) for k, v in vascular.items()},
        'any_history_distant_metastatic_statement_patients': len(distant_positive),
        'passed_patient_resource_audit': audit,
        'size_audit': size_audit,
        'cross_report_audit_counts': {k: v for k, v in cross_audit.items() if isinstance(v, int)},
        'source_sha256': {str(p.relative_to(args.runs_root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
        'contains_patient_level_data': False,
        'baseline_stage_or_resectability_estimated': False,
    }
    def pct(n, d=2004):
        return f'{n:,}（{100*n/d:.1f}%）'
    states = summary['patient_states']
    pathology0 = diagnoses['P0']['病理明确PDAC']
    pathology1 = diagnoses['P1']['病理明确PDAC']
    text = f'''# P1 纳入规则、队列概况与 P2 衔接简报

日期：2026-10-19｜算法：P1 source-quality v0.2｜固定 P0：2,707 人

## 1. P1 的定位

P1 是**患者级资料表征质量筛选**：从已交付的 PDAC 人群中，识别至少有一次可追溯、包含胰腺病变及关键解剖表征的影像记录，并有相关临床文书支撑的患者，供后续识别决策 episode。

这里的“资料质量”指源文可核验、关键事实得到表征；**不要求已经足够作出手术决策，也不等于可以手术**。不以实际手术、最终切除性、是否姑息治疗作为 P1 纳入条件。

本说明反映本轮已执行规则。方案正文仍保留早期“是否进入手术评估”的 P1 表述，尚需另行同步；本次未改动方案正文。

## 2. 当前可执行的纳入规则

患者纳入条件为：

```text
P1(i) = 属于固定 P0
        AND 身份索引通过核对
        AND 至少一条可定位的胰腺相关临床文书
        AND 至少一份合格影像报告
        AND 无患者级影像读取错误
```

| 层级 | v0.2 的具体检查 |
|---|---|
| 临床上下文 | 医生相关文书类型命中预定义规则，排除护理、同意书等；正文非空、含胰腺相关表述，并有日期与来源记录标识 |
| 原文与时间 | 报告可按患者、来源键、日期和原文回溯；检查日期可解析；仅有二次摘录不能独立充当原文核验 |
| 检查与病变 | 胰腺/相关腹部增强 CT 或 MR；存在目标胰腺病变上下文；病变部位、尺寸有表征，尺寸也可明确记为无法测量或病灶不再可辨 |
| 血管关系 | SMA、CA、CHA、PV、SMV 五条血管分别有明确阴性、明确接触/侵犯或明确无法判断的表述 |
| 转移相关信息 | 远处转移字段有实质取值，或肝脏与腹膜/网膜字段均有实质取值；不要求结果阴性 |
| 上下文与一致性 | 当前规则识别的胰腺切除术后/复发语境先待核；单报告无已检出的阻断冲突，同报告标识对应不同内容时待核；其他合格报告仍可支持该患者纳入 |

处理原则：已有有效血管取值时，重复空栏不覆盖有效值；明确接触但角度未量化可以通过并标记；仅写“通畅”、空角度或未提及，不推断为无接触。模板范围内的简写“无”可以识别；空肠分支与 SMV 主干分别处理。

**五血管逐项有表征是 v0.2 尚未放宽的自动通过门槛，不是临床决策必须满足的普遍标准。** 未命中、未识别或仅部分表征的报告进入待核；不能将其解释为临床不可决策或永久排除。

P1 检查全部已交付历史，不限于研究期内；P2 再限定研究期内 episode。本次冻结窗口标记为 2021-01-01 至 2026-10-18，后者是本次快照截至日。

## 3. 筛选前后样本情况

| 人群/状态 | 人数 | 占固定 P0 | 含义 |
|---|---:|---:|---|
| 筛选前 P0 | 2,707 | 100.0% | 师弟交付的 PDAC 患者索引，不在本轮重新定义 P0 |
| P1 自动通过 | {states['PASS_V2']:,} | {100*states['PASS_V2']/2707:.1f}% | 进入后续 P2 候选 episode 提取的患者池 |
| 待核 | {states['REVIEW_V2']:,} | {100*states['REVIEW_V2']/2707:.1f}% | 保留复核机会，本轮不视为确定排除 |
| 当前资料不满足 | {states['NOT_MET_V2']:,} | {100*states['NOT_MET_V2']/2707:.1f}% | 本次交付资料未满足算法门槛，可随补充资料再评估 |

v0.1 → v0.2：通过 1,941 → 2,004，待核 672 → 620，当前不满足 94 → 83。新增通过 63 人：60 人修复重复空栏覆盖，1 人修复分支与主干关系混淆，2 人识别模板内简写“无”；另有 11 人因临床上下文缺口转入待核。所有原通过患者保留。

筛选覆盖 38,561 条影像来源行，归并为 31,236 个内容版本；全部 P0 中有 7,870 个报告版本通过报告级规则，其中 **7,794 个属于最终通过的 2,004 人**，另 76 个属于患者级待核者。来源行、内容版本、独立检查不能混用。

## 4. P1 关键人群特征

| 指标 | 结果 | 统计口径 |
|---|---:|---|
| 病理明确 PDAC | {pct(pathology1)} | P0 对应为 {pct(pathology0,2707)}；沿用原索引的诊断依据层 |
| 临床原文明确 PDAC、未见明确病理总表证据 | {pct(2004-pathology1)} | 不等于已经证明没有病理 |
| 同时有合格 CT 与 MR | {pct(modality_counts['CT+MR'])} | 任一历史时点有相应合格报告 |
| 仅有合格 CT / 仅有合格 MR | {modality_counts['CT']:,} / {modality_counts['MR']:,} | 不代表患者从未做过另一模态 |
| 合格报告内容版本 / 检查组近似数 | 7,794 / 4,442 | 检查组为去重代理，尚未完全核实独立检查 |
| 每人合格内容版本 / 检查组中位数 | 4 / 2 | 有纵向资料基础，不能直接解释为已有 2 个 episode |
| 只有一个检查组 | {pct(565)} | 仍可产生单次决策 episode，未保证支持序贯更新评价 |
| 每人相关临床文书中位数 | 14 | 为类型、关键词、日期和来源规则命中的文书数 |
| 至少一次有血管接触/侵犯表述 | {pct(vascular_any)} | 任一五血管、任一合格历史报告；不是不可切除人数 |
| 至少一次有远处转移表述 | {pct(len(distant_positive))} | 全病程报告字段的程序化信号，未作专家确认，不能作为基线 M1 比例 |
| 仅有研究期前合格影像 | {pct(5)} | 可保留在 P1；是否存在研究期内候选 episode 另核 |

以上影像特征按“全历史至少一次”统计，可能来自不同时间和不同 episode，**不能拼成同一时点的临床分期**。年龄、性别、基线分期和实际治疗路径本轮尚未形成经核验的 P1 统计，本简报不作推断。

通过人群的质量审计还发现：73 人存在同报告两处尺寸差异候选（21 人差异较大），1 人存在同检查跨来源血管取值冲突，121 人有同日 CT/MR 血管描述分歧，10 人没有相距 30 天以内的合格影像—临床文书记录对。前两类需核实具体字段或报告版本；跨模态分歧保留为临床不确定性。30 天仅为审阅提示，不是 episode 纳入阈值。这些发现均未改变本轮 P1 成员。

## 5. 如何衔接 P2

**P2 的单位从“患者”转为“候选决策 episode”。** 从 P1 患者池中，结合研究期内临床文书与证据时间线，定位涉及是否推进根治性切除路径的一次临床评估及其相关证据、管理决定和后续更新。

| P2 候选场景 | 需要提取的内容 |
|---|---|
| 初始评估 | 已获得初步分期相关信息后的管理判断，包括手术、NAT、系统治疗、补充检查或暂不宜手术 |
| NAT 后或治疗中的重新评估 | 重新讨论手术机会、继续治疗、换方案、进一步取证或不再推进手术路径 |
| 否定或暂缓手术 | 只要有相关评估证据和管理判断，同样可成为候选；不能因最终未手术而自动排除 |

同一患者可以有多个候选 episode；一次 CT、一次住院或重复复制的计划不能机械各算一个。具体 episode 的起止与拆分仍需形成独立规则，本简报不预设最终 episode 数量。

后续在 P3 核查：目标病变与本次决策是否匹配、研究期与节点时点是否合格、证据当时是否可见、检查是否去重、冲突版本和有问题字段如何使用。**患者曾有一份合格影像，不能代替本次 episode 的证据；未来影像、手术结果或病理不能回填早期 Agent 输入。** P1 通过只提供候选患者池，不保证每人均有可纳入的 episode 或可评价的序贯 transition。

## 6. 版本与复现

本文根据已冻结运行汇总，不更改规则或患者成员。既有代码标签：v0.1 `41c68ac`，v0.2 `abbbad1`，通过者审计 `9ae1fe8`，均属于独立本地队列版本库。

文档生成程序：`scripts/cohort_construction/p1_source_quality/build_p1_brief.py`。同名 `.aggregate.json` 保存聚合统计及本次输入文件 SHA-256；运行目录为本地忽略的 `local_artifacts/`。本简报未输出患者级标识或病例原文。
'''
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding='utf-8')
    args.output.with_suffix('.aggregate.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'document': str(args.output), 'patients': len(passed), 'qualified_report_versions': report_count, 'contains_patient_level_data': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
