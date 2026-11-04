# Pancreatic Decision Agent

研究计划根治性胰腺手术路径中的证据充分性与序贯决策。

2026-10-23 当前按 [P2验证与医生盲评衔接流程 v0.3](docs/P2-重建验证与医生盲评衔接流程-v0.3-20261023.md) 推进：小规模独立原文重建标注，加全量医生checkpoint有效性检查与Clinical Reference；结构问题修订后重新盲评。已修复事件评估和FP分母，提供独立调用与追加式评审台账。算法保留 [Decision Graph v0.2](scripts/cohort_construction/p2_relation_graph/README.md) 和run07候选检索。临床数据和结果均保存在本地忽略目录，人工标注及正式医生盲评尚待实际执行。

后续语义模型调用与自动化默认复用 [博约 / kimi-k3 配置](docs/boyu-semantic-api.md)。密钥沿用本机配置，不进入代码。模型提议不构成医生 Reference。

Phase A 交接入口为 `python -m pancreatic_agent.phase_a`：冻结四问输入与来源复核记录、离线校验、按指定模型独立复跑。克隆后的虚构示例、真实样本交接包使用方式、历史 Codex 复核执行过程与数据流转见 [Phase A 交接指南](docs/phase-a-handoff.md)。真实样本另以本地包交接，不进入公开仓库。

## P2 Checkpoint 版本登记

P2 版本、实现状态和证据入口统一登记于 [`config/p2_checkpoint_versions.json`](config/p2_checkpoint_versions.json)。

- **v0.6.0**：当前已实现的候选重建基线；独立语义验证、真实材料审核和临床 Reference 尚未完成。
- **v0.6.2**：独立候选图/完整交付双审计及 Ready Resource 接口；仅为虚构工程验证，未形成临床 Reference。
- **v0.6.3**：持久化编排、版本化回修、无模型重放和统计执行接口；真实质量、人工回收及临床放行均未完成。
- **2026-11-01 存储修复**：迁移后新运行统一受实际 `private/` 根目录门禁约束；固定十例适配器显式绑定同一历史符号链接的物理私有父目录，不放宽任意路径。目录 0700、文件 0600、独占创建且不覆盖父运行；图回修从绑定证据恢复请求。最新 P2 完整守卫回执为 `private/p2_checkpoint_next_20261028/validation_storage_migration_v063_r4_20261101/validation_receipt.json`。这些修复不改变上述算法版本或临床状态。
- **真实开发验证适配器**：`p2_checkpoint_real_validation` 已登记；真实 fixed-ten 离线 preflight 已完成，10 例中 6 例 READY、4 例材料准备 HOLD，0 次模型调用。一次另行明确授权的远程开发执行已完成：10 请求、18 HTTP attempts，十例均停在候选图审计，未进入材料审核或临床发布。随后已修复重试配置、重复引文提示及 `REVISE + required_actions` 状态机缺陷；最新完整守卫为 `private/p2_checkpoint_next_20261028/validation_retry10_graph_revision_v063_20261101/validation_receipt.json`，884 项测试及 449 项 subtests 通过，回归中 0 次临床模型调用。随后按用户要求在新目录重跑修复版固定十例：`private/p2_checkpoint_next_20261028/ready_pipeline_real_validation_retry10_graphfix_r1_20261101/execution/batch-execution-receipt.json`。11 请求、48 HTTP attempts；4 例持续 HTTP 400、5 例图重建 handoff、1 例图审计 PASS 后材料 HOLD。该结果仍不是临床性能评价或放行。固定十例最新开发覆盖为：SCALE-01/05/08 在 DeepSeek 与 Kimi 下均持续 HTTP 400；SCALE-06 仅在明确指定的 Kimi 子集重跑后形成重建 handoff；SCALE-02/04/07/09/10 保持 DeepSeek 重建 handoff；SCALE-03 图审计 PASS 后停在材料响应 `DISPOSITION_QUOTE_NOT_UNIQUE`，不是材料语义审核 PASS 后 HOLD。不同模型结果分目录保存，未合并估计性能；没有材料全套 PASS、交付门禁审核、医生分配或临床发布。独立审核、统计验收和医生 Reference 均未完成。

## P1 存储迁移

P1 v0.3.2 保留 v0.3.1 的提取模块与纳入规则，迁移到当前 `private/` 存储并实施私有权限、字节 hash 核验和版本化续跑。冻结 v0.3.1 输出不覆盖。合成守卫为 55 项 active 加 51 项冻结 legacy。本地无模型重跑保留 2,707 名 P0 患者，2,690 例 `INCLUDE_V3`、17 例 `REVIEW_V3`；四个逐行输出与 v0.3.1 字节一致，仅 summary 的版本和耗时不同。这是存储与顺序复现证据，不是临床验证。P1 状态仍不是 P2 的绝对入口门槛。

## 项目目录

- `contracts/v0.1/`：原始 contract pack，按原文保存。
- `contracts/v0.2/`：旧版住院/窗口提取 contract，保留用于对照。
- `contracts/v0.3/`：历史时间线/节选与回述重建 contract，保留用于对照。
- `contracts/v0.4/`：当前 HEWV 本院诊疗 Episode 与完整记录可见性契约。
- `data/100例/`：本地临床数据包，未纳入 Git。
- `data/copy_verification.json`：本地复制校验清单。
- `private/`：本地病例时间线、来源映射和病例分析，未纳入 Git。
- `docs/`：不含患者资料的研究设计文档。
- `pancreatic_agent/`：状态、严格输出校验、日志匹配、显式状态机与可选 LLM adapter。
- `configs/`：版本化动作/事件映射。
- `tests/`：只含虚构数据的控制器测试。
- `outputs/`：本地模型输入预览、manifest、trace 和结果，未纳入 Git。

## 既有四阶段实现（保留）

既有四阶段实现采用 [HEWV v0.4](contracts/v0.4/01_algorithm_contract.md)：按本院诊疗过程定义 episode，区分真正首诊与最早可观测记录；每个问题累计开放整份已可见记录，不截取文书前缀、不将后来外院回述提前。四阶段是问题框架，节点可不同、共享或未定位。独立决策与既有计划复核明确区分。报告返回后用 `<=`，目标决策文书前用 `<`，不使用 `create_time`。这是保留的历史实现，不能据此定义新 P2 的 episode；新 P2 遵循 AGENTS.md，episode 规则尚未确定。

配置与运行方式见 [提取规范](docs/hewv-extraction.md) 和 [v0.3 → v0.4 semantic diff](docs/semantic-diff-v0.3-v0.4.md)。当前入口为 `python3 -m pancreatic_agent.episode_extract --help`；`timeline_extract` 与 `four_stage` 分别保留 v0.3/v0.2 行为。均不调用模型，患者输出仅在 `private/` 或 `outputs/`。

新增 `python -m pancreatic_agent.evaluation`，消费四份固定context，按[决策接口v0.1](contracts/v0.4/05_decision_interface.md)输出PAUSE/EXIT/CONTINUE、一项首选动作与严格JSON。默认只生成请求预览，`--execute`才调用；无前问记忆和动态检查回放。代码结构、运行、重复测试与结果说明见[测试指南](docs/checkpoint-evaluation.md)。新入口凭证由显式`--key-env`指定，默认BOYU_API_KEY；端点与模型独立设置。

以下为保留的序贯状态机设计背景，并非当前四阶段提取器的运行要求：

先将一个病例按事件发生时间、文书时间与信息可用时间展开，再定义关键决策点。每个节点给模型当时可见的病历、完整影像报告及全部检验结果；不输入研究者提取的病灶特征、问题域或关键指标子集。详见 [临床资料输入](docs/clinical-input.md)。

患者级整合包包含 T0 后信息，只用于受控审阅，不能整体作为 T0 policy 输入。病理、手术发现和随访必须按各决策时点的信息边界使用。

D0 在被评价的医生选择之前：不输入医生拟术式、推荐或结局，避免模型复述真实选择。详见 [状态机设计](docs/state-machine.md) 和 [episode 设计](docs/episode-design.md)。实际病例输入需要单独完成来源与时间审阅，未审阅时程序在 API 调用前停止。

## 本地运行

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m pancreatic_agent preview --baseline private/case-001/baseline.json
python3 scripts/maas_smoke_test.py
```

最后一条仅发送固定的“你是谁”。历史状态机和连通性脚本使用 `LLM_API_KEY`、`LLM_API_URL` 和 `LLM_MODEL`；新独立测试入口使用显式 `--key-env`，见[测试指南](docs/checkpoint-evaluation.md)。本机令牌由用户环境配置提供，源码中无令牌值。本机 API 基础说明见 [API 说明](docs/local-api-check.md)。队列来源、字段字典、运行顺序和时间语义见 [队列数据来源与复现说明](docs/cohort-data-provenance.md)。

## 版本管理

Git 跟踪研究规范和非患者级项目文件。临床数据与病例衍生文件保存在本地忽略目录。

`scripts/check_repo_safety.py --staged` 检查待提交文件；`--history` 额外检查可达 Git 历史。它检测受保护路径、已知令牌模式、当前平台 key 和本地患者标识；不能替代对全部待提交内容的人工审阅。本机已安装相应 Git hook，hook 位于 `.git/`，克隆仓库后需重新配置。
