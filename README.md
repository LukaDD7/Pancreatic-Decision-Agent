# Pancreatic Decision Agent

研究计划根治性胰腺手术路径中的证据充分性与序贯决策。

当前阶段：四阶段固定临床 context 的规则提取、资料审阅与独立LLM测试。临床数据和测试结果均保存在本地忽略目录。

Phase A 交接入口为 `python -m pancreatic_agent.phase_a`：冻结四问输入与来源复核记录、离线校验、按指定模型独立复跑。克隆后的虚构示例、真实样本交接包使用方式、历史 Codex 复核执行过程与数据流转见 [Phase A 交接指南](docs/phase-a-handoff.md)。真实样本另以本地包交接，不进入公开仓库。

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

## 当前工作

当前采用 [HEWV v0.4](contracts/v0.4/01_algorithm_contract.md)：按本院诊疗过程定义 episode，区分真正首诊与最早可观测记录；每个问题累计开放整份已可见记录，不截取文书前缀、不将后来外院回述提前。四阶段是问题框架，节点可不同、共享或未定位。独立决策与既有计划复核明确区分。报告返回后用 `<=`，目标决策文书前用 `<`，不使用 `create_time`。

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
