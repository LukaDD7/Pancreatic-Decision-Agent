# v0.6.1 自动材料治理（2026-10-29 审计回修）

本模块组织研究者侧的原始材料、材料审核 LLM 的意见及程序修订记录。自动通过不形成人工 SemanticAudit、医生许可或临床 Reference。当前默认配置禁止真实远程执行。

## 离线准备

在仓库根目录运行，所有路径均使用本次任务的新目录。命令中的 `python3` 须指向已安装 `jsonschema`、`requests`、`pytest` 的解释器；本机整合验证使用 `/opt/anaconda3/bin/python3`，系统默认 Python 3.14 缺少 `jsonschema`，不能据其不完整收集声称通过。

```bash
python3 -m scripts.cohort_construction.p2_checkpoint_material_governance.run \
  --manifest /absolute/private/source-manifest.json \
  --compiled /absolute/private/compiled-partition.json \
  --candidate-sources /absolute/private/candidate-sources.json \
  --excluded-sources /absolute/private/excluded-sources.json \
  --task-context /absolute/private/task-context.json \
  --config config/p2_checkpoint_material_governance_v061.json \
  --output private/NEW_RUN_DIRECTORY
```

三个来源／上下文参数可省略。来源参数是包含完整 `source_id`、`source_version`、`text` 的 JSON 列表，可附原文 `sha256`。纳入、候选、排除三个初始范围必须互斥。程序逐源核对 manifest 与 compiled 的版本、hash、长度及原文完全重建；不会静默过滤重叠来源。

不加 `--execute` 时，仅生成请求、输入快照和准备回执，不创建客户端或调用模型。输入快照保存实际读入的字节、提示、声明的本地实现依赖及 schema；不读取 `.env`，配置中不得包含凭证。已有输出目录拒绝覆盖。

远程执行还要求单独获准的配置、明确的模型及 `remote_execution_authorized=true`，并显式传入 `--execute`。仓库默认配置不能授予该授权。`--max-rounds` 可覆盖配置的工程预算；有效预算、实际模型参数及不含鉴权信息的端点另存并绑定回执。真实开发运行及临床方案的授权仍须单独记录。

## 0.2 绑定与历史

- `AUTO_PROTOCOL_VERSION` 为 `checkpoint-automated-material-review-0.2.0`。请求的 `response_binding` 已给出应逐字回显的四个字段；LLM 不计算 SHA256。
- 响应的 `request_sha256` 是**排除 `response_binding` 后的材料请求**的 canonical hash。链中 `request_sha256` 是**包含信封的完整材料请求**的 canonical hash，`request_binding_sha256` 记录前者。两者均不同于提供者 transport 请求的摘要。
- 版本化来源注册表保留本次提供的全部原文。已纳入后又排除的来源仍进入后续审核的排除视图；来源范围改变不改写原文或候选图。
- 每轮记录输入、原始结构化响应、程序操作账本、输出版本和应用回执。修订先成为链尾，再处理 HOLD；失败保留已有轮次和失败轮请求。
- `required_actions` 在 0.2 中表示阻断待办。非空时不能 PASS；REVISE 可保留已执行修订后停止于 HOLD。仅要求程序对新版继续复审时，使用 `AUTO_REVIEW_REVISE` 和空待办列表。
- 0.1 的材料审核、审核链、抽样方案及旧报告作为历史产物保留，不自动视为 0.2 下已验收结果。既有人工 SemanticAudit 的独立版本含义不变。

## 候选图、工作台与回收

`chain_validation.validate_review_chain` 从初版 manifest／compiled 和不可变来源注册表重建每个请求，确定性重放每轮操作，并核对版本连续性、停止原因及最终医生视图。摘要只证明内部一致性，不认证人员身份或临床时点。

候选 Episode 要求绑定 `episode_reference.graph_snapshot`、图版本和快照摘要。材料链的 `task_context` 通过 `bind_candidate_graph` 绑定该快照、Checkpoint 和目标 `event_id`；程序另外核对目标事件确属该候选 Episode。候选图成员核验不等于医生确认了 Episode 或时间顺序。

`graph_revision_handoff` 向独立重建层提出具体目标及重建要求。重建层提供新图版本和重新准备的材料后，`restart_material_review_after_graph_revision` 生成新请求并重新执行材料审核，不转移旧图的自动 PASS。该入口不自行认定图中的临床事实，也不代替重建层的最终关系审计。

`workbench.build_auto_review_assignment` 接受有效审核链、原始准备审计及 PENDING 草稿。实际修订后，它派生新准备审计和全新未决人工草稿，同时保留原始审计、引文、来源路由及未决内容；新材料范围单列记录。`write_workbench_package` 输出研究者 HTML；被遮原文仍只属于研究者文件。

浏览器核验整链身份、摘要、操作及版本连续性；Python 在 `receive_auto_review_submission` 时执行权威整链重放和响应绑定核验。人工身份、逐项判断和独立性声明须由实际人员填写，自动意见不会预填为人工通过。医生 FHIR／HTML 的既有许可门槛继续独立生效。

## 人工抽样

`freeze_sampling_plan(specification, packets)` 同时冻结完整总体清单（包 ID、hash、层别）、分层配额及按种子生成的有序分配。`select_packets` 核对完整总体；`summarize_human_sample` 拒绝替换、改层、重排和任务绑定错误的回收。

人工响应包含包 ID／hash、方案及分配清单摘要、实际审核者声明和带时区的审核时间。汇总逐层报告总体 N、分配 n、实际回收、缺回收、缺陷数与有缺陷的包数。Wilson 区间仅是已回收包的未校正二项诊断区间；当前协议未定义总体估计设计、权重或患者聚类，故即使全回收，`statistical_validation_complete`、总体推断及自动发布也保持 false。临床团队仍须冻结正式抽样、缺陷标准及验收方案。

## 回归

```bash
python3 -m scripts.cohort_construction.p2_checkpoint_review_app.verify \
  --output private/NEW_VALIDATION_DIRECTORY
```

守卫核对历史产物、pytest 实际收集节点及 JUnit 中实际通过的全部必需参数变体。错模块、错类、未收集、未执行、跳过或失败均不能计为覆盖。稳定问题及对照见本目录 `regression_issue_map.json`；本轮对应 `V061-AUD-001` 至 `V061-AUD-008` 和 `GAP-01` 至 `GAP-03`。
