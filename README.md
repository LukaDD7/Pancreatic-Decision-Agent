# Pancreatic Decision Agent

研究计划根治性胰腺手术路径中的证据充分性与序贯决策。

当前阶段：单病例 D0 证据审阅与状态机初版。已验证非临床 API 连通性，尚未向远程模型发送真实病例。

## 项目目录

- `contracts/v0.1/`：原始 contract pack，按原文保存。
- `data/100例/`：本地临床数据包，未纳入 Git。
- `data/copy_verification.json`：本地复制校验清单。
- `private/`：本地病例时间线、来源映射和病例分析，未纳入 Git。
- `docs/`：不含患者资料的研究设计文档。
- `data_pipeline/`：Stage 5原始来源解析、Stage 6主键连接与病理接入、Stage 7时间轴构建代码。
- `scripts/cohort_construction/`：特殊病例、100/751/851/180队列及Agent资料包构建代码。
- `pancreatic_agent/`：状态、严格输出校验、日志匹配、显式状态机与可选 LLM adapter。
- `configs/`：版本化动作/事件映射。
- `tests/`：只含虚构数据的控制器测试。
- `outputs/`：本地模型输入预览、manifest、trace 和结果，未纳入 Git。

## 当前工作

先将一个病例按事件发生时间、文书时间与信息可用时间展开，再定义关键决策点。每个节点给模型当时可见的病历、完整影像报告及全部检验结果；不输入研究者提取的病灶特征、问题域或关键指标子集。详见 [临床资料输入](docs/clinical-input.md)。

患者级整合包包含 T0 后信息，只用于受控审阅，不能整体作为 T0 policy 输入。病理、手术发现和随访必须按各决策时点的信息边界使用。

D0 在被评价的医生选择之前：不输入医生拟术式、推荐或结局，避免模型复述真实选择。详见 [状态机设计](docs/state-machine.md) 和 [episode 设计](docs/episode-design.md)。实际病例输入需要单独完成来源与时间审阅，未审阅时程序在 API 调用前停止。

## 本地运行

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m pancreatic_agent preview --baseline private/case-001/baseline.json
python3 scripts/maas_smoke_test.py
```

最后一条仅发送固定的“你是谁”。远程调用统一使用 `LLM_API_KEY`、`LLM_API_URL` 和 `LLM_MODEL`。本机令牌由项目之外的用户配置提供，源码中无令牌值。环境建立与真实模型调用参数见 [本机 API 说明](docs/local-api-check.md)。队列来源、字段字典、运行顺序和时间语义见 [队列数据来源与复现说明](docs/cohort-data-provenance.md)。

## 版本管理

Git 跟踪研究规范和非患者级项目文件。临床数据与病例衍生文件保存在本地忽略目录。

`scripts/check_repo_safety.py --staged` 检查待提交文件；`--history` 额外检查可达 Git 历史。它检测受保护路径、已知令牌模式、当前平台 key 和本地患者标识；不能替代对全部待提交内容的人工审阅。本机已安装相应 Git hook，hook 位于 `.git/`，克隆仓库后需重新配置。
