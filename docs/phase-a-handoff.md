# Phase A 交接：四个 checkpoint、来源复核与独立 LLM 测试

此交接对应 HEWV v0.4 的固定 context 测试。公开 Git 仅含代码、规范与完全虚构的示例；真实来源、截面、复核和回答通过本地交接包传递，继续保存在 `private/`、`outputs/`。

## 本机历史运行到底怎么做

2026-10-08 至 10-09 的流程分三步。第一步由 Codex 在交互式会话中逐原文审阅本院来源归属、episode 入口、四问锚点、文书时间和全文版本，把判断写入 `review_manifest.json`。历史配置明确写着“Codex来源核对/逐原文探索审阅，未经独立临床专家核准”。这一步不是提取器调用的 API，也没有独立专家金标准。原会话、精确模型身份和复核时间没有作为数据文件保存；本次交接的 `source_review_record.json` 从已有配置恢复可证事实，这些缺失字段留为 null，不能声称还原了当时完整会话。

第二步由 `episode_extract.py` 读取 source bundle 和已复核配置，零模型调用，按时间边界累计开放**整份来源记录**。第三步由 `evaluation` 为每问重新建立 system + user 两条消息，调用 OpenAI-compatible 接口。历史完整 pilot 请求参数为模型别名 `kimi-k3`、temperature 1、max_tokens 16000、timeout 600 秒、retries 0、prompt_only；端点见本地历史 manifest。模型别名不认证底层身份。模型复核病历中既有计划的 `plan_review` 与构建输入时的“来源复核”是两件事。

原来的 `private/case-pilot-20261008/prepare.py`、`private/full-pilot-20261009/prepare.py` 等包含具体病例配置和正文解读，属于病例衍生数据，保留在真实交接包的 `history/`。公开仓库的新入口通用化这些操作，不依赖上述本机目录、硬编码患者锚点或绝对路径。

完整 pilot 为 15 个 episode、60 个问题槽位，6 个 UNRESOLVED，实际调用 54 次，49 个技术有效回答。技术有效仅说明 JSON/schema/动作一致性及引用 ID 校验通过。历史离线回答审阅还保留 1 个输入可用性冲突；输入冻结成功不消除该冲突，不代表临床正确。这些数值是旧 pilot 的既有记录，新运行单独统计。

## 四个 checkpoint 怎样截取

| 问题 | 含义 | 选点方式 |
|---|---|---|
| Q1 | 发现病变，下一步检查或评估 | 最早可定位本院观察；首次 visit 未覆盖时明确标记 |
| Q2 | 诊断与分期 | 所选诊断/分期证据返回后 |
| Q3 | 根治切除可行性与治疗承受条件 | 所选证据更新后，不假定此时已评估完成 |
| Q4 | 治疗或处置选择 | 目标决策文书之前，或最后安全证据更新后复核已有计划 |

四问是临床问题框架，时间点可以共享，并非每个病例一定有四个独立节点。`after_records` 用 `<=`；`before_record` 用 `<` 并整篇排除目标文书。不按正文前缀截断，不使用 `create_time`，不将后来回述的外院事件提前。影像检查时间代理必须声明；未来日期的全文版本例外必须绑定正文 hash 并解释。无法定位的节点保留 UNRESOLVED 和理由，测试时跳过。详见 [提取规范](hewv-extraction.md)。

## 数据怎样流转成测试样本

```text
本地已关联的完整 source_bundle
    ↓ 交互式原文复核（或明确记录的其他来源审阅）
review_manifest + source_review_record
    ↓ 校验 bundle hash、review hash、审阅者、来源/锚点/版本
HEWV 规则提取：零 API 调用
    ↓
source + review + audit + timeline + packets + 四问 context/messages
    ↓ 每个 episode × Q1..Q4 均写入 samples.json，含 UNRESOLVED
dataset.json：相对路径、文件 hash、样本来源链、代码版本
    ↓ 全数据集 hash 校验 + 从 source/review 重建比对
独立 LLM 测试：每问/每次重复 fresh system + user
    ↓
实际 request → raw_output → 严格校验后的 decision 或技术失败
    ↓
逐次 result、manifest、summary、sample_lineage、batch_manifest
```

`samples.json` 记录 sample_id、问题/全文 hash、可见 source_id、checkpoint、选点理由、质量 issues 和来源复核记录 hash。`dataset.json` 使用相对路径，可解包换机器。原始 source/timeline 可能含术后资料，只供本地来源审阅；run 只向模型传 packet 的问题与当时可见 context，不读取术后标签、临床评分或前问回答。

默认预览不需要凭证，不调用模型。真实执行加 `--execute`。PAUSE 不触发新检查回放，重复之间无记忆。原始回答、解析/传输/截断失败均保存；不自动修 JSON，不把技术失败改成 PAUSE，不补采一个有效答案替换失败。新 batch 永远创建新目录；中断后先查 manifest，未实现自动恢复，重新执行会重新调用所选问题。完整请求格式、退出码和校验细节见 [测试指南](checkpoint-evaluation.md)。

## 师弟克隆后先跑虚构流程

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q

.venv/bin/python -m pancreatic_agent.phase_a prepare \
  --source examples/hewv_fictional_bundle.json \
  --review examples/hewv_fictional_review.json \
  --review-record examples/phase_a_fictional_source_review.json \
  --alias S001 --output private/phase-a-fictional/dataset

.venv/bin/python -m pancreatic_agent.phase_a verify \
  --dataset private/phase-a-fictional/dataset

.venv/bin/python -m pancreatic_agent.phase_a run \
  --dataset private/phase-a-fictional/dataset \
  --output outputs/phase-a-fictional \
  --base-url https://example.invalid/v1/ --model example-model
```

prepare 输出目录必须是新目录，避免覆盖已经冻结的样本；每次运行使用新的输出批次。上述地址仅用于离线预览。将地址与模型替换成实际配置，通过 `--key-env` 指定密钥环境变量名，并添加 `--execute` 才发送。密钥只通过环境变量传入，不写入源码、命令参数或交接包。

## 收到真实交接包后继续跑

将 `.tar.gz` 解包至克隆仓库的 `private/phase-a-handoff/`。交接包自带 `handoff_inventory.json`（逐文件字节 hash）和 `READ_ME.txt`，`dataset/` 为冻结输入，`history/` 保留旧脚本、复核、结果、失败和统计。历史文件中的本机绝对路径只供留档，新入口无需它们。

```sh
.venv/bin/python -m pancreatic_agent.phase_a verify \
  --dataset private/phase-a-handoff/dataset

# 先预览全部可运行问题
.venv/bin/python -m pancreatic_agent.phase_a run \
  --dataset private/phase-a-handoff/dataset --output outputs/phase-a-next \
  --base-url https://example.invalid/v1/ --model example-model

# 确定端点和密钥环境后，先选一个 episode 真实测试
.venv/bin/python -m pancreatic_agent.phase_a run \
  --dataset private/phase-a-handoff/dataset --output outputs/phase-a-next \
  --base-url "$LLM_API_URL" --model "$LLM_MODEL" --key-env BOYU_API_KEY \
  --cases S033_E001 --repeats 1 --execute
```

不指定 `--cases` 会测试所有可运行 checkpoint。`--repeats 5` 会对每问独立采样五次；按 54 个可运行问题计是 270 个计划调用，显式开启 retries 会额外增加尝试。对照模型时使用同一个冻结 dataset，新目录分别保存，不将旧批次混入新统计。

## 新增病例怎样复核和保存

先准备一个完整来源 JSON；上游患者关联和医院导出 ETL 见 [来源说明](cohort-data-provenance.md)。审阅者读取完整来源与时间线，按 [review schema](../contracts/v0.4/04_review.schema.json) 编写四问配置，逐个解释锚点、来源归属、时间代理和全文版本。不要写临床期望答案或把术后结局用于选点。来源 ID 依原数组行序产生，数组或正文变化后重新复核并绑定 hash。

随后保存与示例同格式的 `source_review_record.json`：记录审阅者类型（Codex交互式/human/fictional/api_llm）、执行方法、checks、局限、实际可知的模型身份/审阅时间，以及规范化 source/review hash。`sha` 是 `pancreatic_agent.four_stage.sha` 的对象规范化 hash，文件字节 hash 用途不同。API型还必须记录模型，将 `request_artifact`、`response_artifact` 各保存为 `{"payload": 实际请求或最终响应, "sha256": sha(payload)}`；请求正文仅含消息及非密钥参数，最终响应不含隐藏推理或认证头。这样原件随复核记录冻结，不依赖本机文件引用。当前入口消费已有复核记录，**没有自动调用 LLM 选择 checkpoint 的功能**。不要把这一步与待测试模型的 decision 输出混在一起。

如果让 Codex 执行新病例审阅，应明确要求“读取完整来源，输出 review_manifest 与 source_review_record，逐项记录来源/时间/版本/四问锚点依据，不利用后见结局，不填期望答案”；这是可以再次执行的审阅操作说明，历史日志没有保存该提示词，不能称为历史原始 prompt。新审阅将实际使用的提示词和结论留在本地记录里，再运行 prepare/verify/run。所有当前输入仍标 DRAFT_REVIEW_REQUIRED，仅用于探索测试，不会自动升级为临床核准。

已存在同结构历史目录时可重新冻结（从旧配置恢复 provenance，不重新声称做了审阅）：

```sh
.venv/bin/python -m pancreatic_agent.phase_a import-pilot \
  --input-root private/full-pilot-20261009/inputs \
  --output private/phase-a-new-freeze/dataset

.venv/bin/python -m scripts.package_phase_a_handoff \
  --dataset private/phase-a-new-freeze/dataset \
  --history private/full-pilot-20261009 \
  --history outputs/full_pilot_20261009 \
  --output outputs/phase-a-handoff.tar.gz
```

历史导入会重建并核对每问可见记录/问题/时间边界，差异时停止，不静默改变旧样本。交接包仅本地生成；打包器检查已配置 API_KEY 的字面值与常见令牌模式，并逐文件校验归档字节。它不是病历脱敏工具，不能把真实包推到公开 Git。公开仓库继续使用 `scripts/check_repo_safety.py` 与忽略规则保护本地数据。
