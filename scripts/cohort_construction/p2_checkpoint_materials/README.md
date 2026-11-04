# 患者重建结果 → 原文分割待核接口

`prepare_material_review(event, sources, *, decision_task_id, visibility_policy_version)` 返回四个对象：标准 `SourceManifest`、`SplitProposal`、`CompiledPartition`，以及独立的研究者审计。它读取已完成的发现/复核结果，不调用 LLM，不生成语义 PASS、临床 Reference 或医生许可包。

程序只纳入 `event.source_ids` 与 `event.actual_model_evidence[].source_id` 的并集。模型引文不能自行扩张来源范围。审计列明每个已交付来源的纳入/排除以及请求来源缺口；这只是当前事件材料范围，不代表全患者资料完整覆盖。缺少所需来源、原文或原始 hash 时拒绝输出。

每个纳入来源以**整份原文原字符串**进入 manifest，使用 UTF-8 hash、Unicode code-point 的零起点左闭右开坐标；不改换行、不拼接文书、不改写临床事实。来源未提供版本字段时，以 `content-sha256:<hash>` 作为内容身份，不能解释为医院报告的初稿/修订版本。

分割建议采用保守规则：

- `choice_boundary_citations.valid` 和 `postchoice_citations.valid` 必须同时满足来源 hash、坐标、逐字原文以及已有程序验证标记，程序才给出 MASK 建议。它仍然是 LLM 提案，不能证明临床时间或遮蔽方案正确。
- 观察引文只有在原程序声明 `explicitly_known_before`、`is_declared_observation=true`、`eligible_as_known_prechoice=true`，且观察和依据均可在本次来源中逐字定位时，才登记为 KEEP **候选提示**。实际字符分区仍是 REVIEW，等待独立审核。
- KEEP 候选提示与 MASK 提示重叠时，该原文区间为 REVIEW。坐标、hash 或引文错误的 MASK 提示使该来源全部进入 REVIEW；程序不猜测边界、不机械裁剪引文。
- 其余每一个字符均显式 REVIEW，包括 unknown、null、缺失的可见性判断及没有临床决策子句定位的来源。程序可以输出完整、互斥且机械有效的分区，同时仍有大量 REVIEW；机械 PASS 不代表材料获准提供给医生。

审计保留原引文、事件/源文件 hash、规则版本、每段字符的处理理由、算法/提案问题以及 `semantic_audit_status=NOT_ASSESSED`。错误均归于具体处理或提案，不被改写成患者资料质量差。所有输出含研究者可见原文，必须留在 private；禁止直接嵌入医生 HTML。

命令行从现有事件 JSON 和 source FHIR 导入：

```sh
/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_checkpoint_materials \
  --reviewed-events /absolute/private/decision_events.json \
  --event-id EVENT_ID \
  --source-fhir /absolute/private/source_fhir_r4_bundle.json \
  --decision-task-id EXPLICIT_TASK_ID \
  --visibility-policy-version DRAFT_POLICY_VERSION \
  --output /absolute/private/new_material_review_directory
```

输出目录必须是新目录。`input_receipt.json` 绑定输入文件、选定事件、实现代码和标准对象的 hash，并记录 0 次模型调用。

## 审核工作草稿：不是独立审核结果

`review.create_review_draft(...)`将已有compiled中的每个字符区间转换为`SemanticAudit 0.2.0`待核记录，并按准备审计列明来源范围。它只制作阅读任务：所有来源覆盖仍为UNRESOLVED，所有范围和时间检查均未完成，`reviewer_type=research_preparation`、`status=PENDING`、不作独立性声明。

可附加研究者对既有MASK段的保留提案，但这仍不是材料许可。草稿可用于独立审核者逐项填写；AI草稿自身不能改成PASS或声明独立性。CLI不调用模型、不创建socket，输出独占创建：

```sh
python3 -m scripts.cohort_construction.p2_checkpoint_materials.review \
  --compiled /absolute/private/material_proposal/compiled_partition.json \
  --manifest /absolute/private/material_proposal/source_manifest.json \
  --preparation-audit /absolute/private/material_proposal/researcher_audit.json \
  --output /absolute/private/new_review_assignment/semantic-audit.draft.json
```

`--focus-source-id`仅记录优先阅读位置；`--proposal-json`可提供`proposed_mask_spans`、精确绑定的`issue_records`和未决的`decision_time_boundary`。所有含原文的输出均留在研究者private目录。独立审核者实际完成审核后，才可按标准编译器重编译、重新审核并评估是否生成许可包。冻结临床表单与临床Reference不由本工具产生。
