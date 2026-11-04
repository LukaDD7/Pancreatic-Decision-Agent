# Checkpoint材料审核接口升级与独立审核交接回执

2026-10-29。本轮承接v0.6与FHIR／HTML闭环，解决“机械分区已生成，但无法逐段执行343段REVIEW的独立材料审核”的接口缺口。**没有发放真实患者医生盲评包，没有完成独立人工材料审核，没有冻结临床表单，也没有形成临床Reference。**

## 1. 已实现：SemanticAudit 0.2.0

- 每个审核结论绑定compiled hash、来源hash、逐字原文及Unicode code point半开坐标；每个compiled区间必须且只能评估一次。
- `ACCEPT`只接受已有KEEP／MASK；`REVISE`明确KEEP／MASK；`UNRESOLVED`保持REVIEW，不提供材料许可。
- 记录审核范围、决策时间证据与不确定性、来源覆盖、跨文书回述检查、具体引文支持的问题归因。
- PASS必须同时具有独立性声明、完整审核范围、六项检查、全部区间明确处置和无开放阻断问题。程序只核验这些记录，不能证明实际独立性或临床正确性。
- `revise_partition`生成新的机械分区，不修改旧manifest、提案或compiled；新分区仍为`NOT_ASSESSED`，必须重新审核。
- `research_preparation`用于AI／研究准备草稿，必须为PENDING且不作独立性声明，不能改写成独立审核PASS。
- `CE-PKT-007`及六项回归测试已登记；旧审核fixture和合成FHIR／HTML闭环已迁移到0.2.0。

## 2. 已生成：真实材料的研究者审核任务，不是审核结论

`private/p2_checkpoint_next_20261028/independent_review_assignments_v02/`保存三份完整研究者任务、逐段待核JSON、阅读说明、绑定清单和验证回执。所有原文仍在private目录，未嵌入HTML，未发送给模型或外部服务。

|任务|compiled区间|原有MASK|原有REVIEW|纳入来源|事件范围外已交付来源|
|---|---:|---:|---:|---:|---:|
|DeepSeek SCALE-01术中|174|3|171|164|524|
|Qwen SCALE-01同一来源|174|5|169|164|524|
|DeepSeek SCALE-04单来源|5|2|3|1|1260|
|合计|353|10|343|—|—|

**353是所有compiled区间，343是原有REVIEW区间。**草稿对353段均保持未决，没有擅自把MASK或观察文本提升为医生许可。所有来源覆盖也仍为UNRESOLVED；“排除”只表示当前事件准备范围，不代表已经逐份排除必要性，更不等于患者资料缺失。

优先审核点：

1. 两个SCALE-01任务先读同次手术文书：区分探查前观察、实际选择回述和选择后操作；材料边界修订不自动修复模型的`UNCERTAIN_CONTRACT_OR_SOURCE`。
2. SCALE-04保留实际选择时间、复合文书和既往安排的未决状态；不能按文书顺序推断当时可见性。
3. 实际独立审核者须阅读完整相关原文、逐段填写理由，并记录实际范围、时间证据、覆盖缺口和独立性。AI准备身份不得沿用为独立人工审核。

原始material proposals的18个文件逐项hash未变；新任务的验证使用本地`validate_semantic_audit`和独立Draft202012Validator。`validation-receipt.json`明确标记`PENDING_INDEPENDENT_REVIEW`。

## 3. 表单状态：仍是草案

`P2-Checkpoint-材料审核与表单冻结草案-v0.2-20261028.md`整理了任务有效性、遗漏资料、多选Acceptable Mode Set、逐动作评价、首选／不合理动作、Severity、Confidence和更新条件等条目。

但原方案manuscript未在当前目录找到，Q1–Q5原文字、Severity／Confidence锚点、评审人数、分歧处理、指南年代及术中动作空间尚未冻结。因此本轮没有把草案做成真实临床Questionnaire，也没有把历史Whipple或模型首选动作预填为专家答案。

## 4. 回归与版本保留

- 浏览器超时用例单独重跑通过；其后完整回归中的实际浏览器闭环亦通过。
- 修复审核fixture迁移遗漏，以及回归清单对`test_file.py::test_function`名称解析错误；`CE-APP-005`保留正反例，18份清单的405个必需测试引用均能找到对应测试。
- 最终回执：`private/p2_checkpoint_next_20261028/validation_semantic_audit_v02_release/`。
- 该回执覆盖继承P2与新增接口，调用临床模型0次；1,690份既有保护产物hash不变。历史`validation_final`及本批较早失败／中间回执均保留，未覆盖。
- 未修改P1代码、提取规则、来源核对或队列纳入输出，因此未触发P1专项回归。

## 5. 仍待实际人员完成

1. 独立材料审核者完成353段的逐项审核及来源范围核对，而不是接受AI草稿。
2. 对需要修订的材料另存新版、重新编译、重新独立审核。
3. 临床团队冻结任务、Mode／action字典、评分锚点、评审人数与分歧程序。
4. 只有真实语义PASS、完整范围、无REVIEW、冻结表单均具备时，才创建真实医生许可包。
5. 小规模医生试评后，才讨论正式盲评、Acceptable Mode Set及临床Reference；目前不能声称临床准确性改善。
