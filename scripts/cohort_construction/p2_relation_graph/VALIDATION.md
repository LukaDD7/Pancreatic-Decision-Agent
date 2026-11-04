# 验证与评审工作流 v0.3

本版落实2026-10-23流程调整。算法和关系类别仍为v0.2；变更是评估口径、独立调用、独立标注材料及医生有效性/临床评价的登记隔离。v0.2的release、测试回执及历史运行保留；新结果使用新文件和目录。

## 入口

| 文件 | 职责 |
|---|---|
| evaluate.py | 完整scope下节点/共指、事件一对一匹配、分类型关系及FP/曲线分母 |
| model.py | 四种cache模式、每次新请求/响应独立保存、hash与replicate绑定 |
| repeatability.py | 固定规范化任务及固定节点关系任务的独立重复实验；不是临床准确率 |
| annotation_pilot.py | 开发集分层选择局部范围，输出双人独立完整原文与空白标注表 |
| annotation_compile.py | 将人工逐字引用编译为坐标，不产生标签或裁定 |
| inter_annotator.py | 在裁定前比较两份独立完成的标注，不指定任何一方为gold |
| review_workflow.py | 追加式有效性、结构裁定、修订、重盲评与Clinical Reference登记 |
| run_validation_checks.py | 检查稳定问题编号及必需测试，运行继承95项与当前全部测试 |

## 评估器

真实gold必须有 `origin=independent_human`、双人独立与裁定记录、`annotation_status=ADJUDICATED`、`complete_within_declared_scope=true`，以及可执行source_windows范围。编译器检查双人及裁定人声明，评估器检查结构和scope；任何字段声明本身都不是实际完成标注的证明。合成测试允许aligned_mentions范围，真实gold不允许用候选ID清单代替全文范围。

先按同患者/源ID/全文hash/精确跨度/类型对齐mention；不直接用包含signature的预测ID。人工跨度对齐须单独记录责任人，只能在同一来源版本的相交范围。随后按mention集合Jaccard作正重叠的一对一全局最大匹配；关系标签不参与。并列按排序ID固定处理，低重叠匹配仍可能发生，须结合共指和节点指标解释，不能把matched_events当临床事件检出正确率。

关系按对齐后的事件端点、任务与label去重；方向相反、额外边、错误label和自环会产生FP/FN。scope内完整标注后缺失的正边按无关系处理；明确uncertain的pair单列unassessed，曲线显示precision上下界，不把它们当阴性。scope外预测单独计数，不用于scope内指标。组合指标保留兼容字段，但Decision–Decision与process-link必须分别报告。

`candidate_edge_recall`目前通过同一预测事件对齐后的候选对计数，是端到端条件下的候选覆盖；若要单独评价检索器，使用人工节点/event构成的oracle输入，另记 `evaluation_mode=oracle_nodes`。只改mode字符串不会自动形成oracle实验，必须实际替换输入并保存来源。B³仅针对共同对齐的Decision mentions，同时报告漏检/增检，不能据此忽略前面的节点遗漏。

```sh
python -m scripts.cohort_construction.p2_relation_graph.annotation_compile \
  --sources private/pilot/annotator_A/LOCAL-01/sources.json \
  --annotation private/adjudicated_annotation.json --output private/compiled_gold.json
python -m scripts.cohort_construction.p2_relation_graph.evaluate \
  --graph private/frozen_graph.json --gold private/compiled_gold.json --output private/event_metrics.json
```

跨度变化与规范化变动仍需审核对齐；本版没有自动计算自由文本signature的医学语义正确率，也未做统计加权或可靠性校准。

裁定前的两份标注使用 `annotation_status=INDEPENDENT_COMPLETE`，两人的annotator_ids必须不同，scope必须相同。先分别用编译器定位原文引用，再运行：

```sh
python -m scripts.cohort_construction.p2_relation_graph.inter_annotator \
  --a private/compiled_A.json --b private/compiled_B.json --output private/pre_adjudication_agreement.json
```

该入口报告原文跨度/类型一致、共同mention上的共指B³、事件对齐后的正关系一致及字段字面一致；两人角色对称，不将独立标注伪标成ADJUDICATED。未定义负pair总体，因此不自动输出kappa。分歧须另行裁定，真实IAA目前仍未产生。

## 独立调用

- read_write：命中则读；新响应另存后更新cache。
- read_only：只读已有cache，缺失立即失败，零外部请求。
- refresh：不读cache；每次新请求和旧响应均保留，再更新cache指向。
- bypass：不读、不写公共cache入口；每次新响应单独留存。

replicate_id与invocation_id只在本地记录，不进prompt。request_semantic_hash相同用于确认请求一致；每个HTTP 200响应的原始回答（包括不合格输出）、schema结果、usage、response hash和状态分别保存。发生中断时保留IN_FLIGHT尝试。重复性实验禁用传输重试，失败不补抽直到成功。

```sh
python -m scripts.cohort_construction.p2_relation_graph.repeatability \
  --demo --repeats 3 --execute --output private/fresh_semantic_trial
```

生产参数保持kimi-k3、temperature=1、thinking disabled。本轮没有为提高一致率调整温度。输出同时保留失败数量、mention集合Jaccard、共享mention上字段字面一致率及关系label一致率。它们不是对人工gold的准确率，缺少有效重复时返回null。

## 标注准备

```sh
python -m scripts.cohort_construction.p2_relation_graph.annotation_pilot \
  --sources private/p2_dynamic_pathway_probe_v0.1_20261021/review_patient_context.jsonl \
  --units private/p2_decision_ir_v0.1_20261022_run07/nodes.jsonl --output private/independent_pilot
```

只向每人提供其annotator_A/B目录与标注手册；researcher_only清单含开发分层，不能一并分发。病例均来自已使用的49人开发材料，不属于锁定验证。固定范围为anchor前后3天代理时钟内全部交付文书，另包含该患者精确时间未明文书；anchor无精确时间时提供该患者全部交付文书。原文不摘要、不高亮候选，未知时间不填零点，不定义episode。需要额外范围时由研究者同步更新两套材料与scope版本。

## 评审台账

每条输入是 `{"kind":"PACKET|GATE|ADJUDICATION|EXPOSURE|REFERENCE","payload":{...}}`。先注册已独立核验的packet元数据，再登记医生gate，达到建账时预设人数且无人标记问题后，才能登记该版临床评分。临床form schema/hash必须与packet预注册一致。台账不制造盲评原文，不验证人员身份或文件访问权限，不替代正式临床protocol。

```sh
python -m scripts.cohort_construction.p2_relation_graph.review_workflow \
  --ledger private/study/review.jsonl --event private/study/one_event.json \
  --required-valid-reviewers 2 --summary private/study/status_001.json
```

数据只追加，版本、评审响应和Reference不覆盖；新版本须保留父版本与原proposal根。结构裁定会自动记录该患者对裁定者的暴露；修订版禁止原评审者或接触过结构/未来资料者临床评分。合并可以包含正确父包与被标记父包；confirmed_error须明确具体错误根，不能把所有受影响父包都计作错误。被结构裁定的父版本及后来被标记问题的Reference保留并隔离。

SHA链和文件锁检查意外改动、串行并发写入；它不是对恶意本机管理员的防篡改安全系统。实际盲法仍需要材料分发、人员分工和接触史登记。当前提供本地工具和模板，尚无医生线上UI，也没有实际医生评分。
