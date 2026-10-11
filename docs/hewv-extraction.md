# HEWV v0.4：配置、处理与复用

当前契约：[算法contract](../contracts/v0.4/01_algorithm_contract.md)、[episode定义](../contracts/v0.4/02_episode_definition.md)、[不变量](../contracts/v0.4/03_invariants.md)、[配置schema](../contracts/v0.4/04_review.schema.json)。历史变化见 [semantic diff](semantic-diff-v0.3-v0.4.md)。

## 从原始来源开始

输入为一个患者的完整来源数组，不能使用v0.3的context、state_pre_T0或节选片段代替。所有源实例保留，不去重，不抽取新的医学特征。原生检验行的表格呈现不等于研究者选择重要指标。

本模块处理已关联的患者包，未补齐医院XML/CSV到中间索引的上游ETL。来源包是否无损、患者关联是否正确，需要结合上游程序另核对。

## 审阅配置

真实配置位于 `private/`，以 `S001` 等输入行别名为外层键。内部对象由JSON Schema验证，不接受旧版的evidence_spans等字段。以下为结构示意，hash必须用实际源bundle对象计算后替换：

```json
{
  "S001": {
    "contract_version": "hewv-v0.4",
    "source_bundle_sha256": "<sha(bundle)>把这里替换为64位hash",
    "reviewer": "本地来源审阅者",
    "source_scope": {
      "default_origin": "hospital",
      "reason": "已核对本患者来源包来自本院系统",
      "origins": {}
    },
    "episode": {
      "episode_id": "E001",
      "clinical_context": "initial_evaluation",
      "entry_source_id": "IMG0001",
      "entry_basis": "FIRST_OBSERVED_HOSPITAL_RECORD",
      "reason": "最早可观测的本院相关记录，完整首次visit尚未覆盖"
    },
    "checkpoints": [
      {"stage": 1, "source_ids": ["IMG0001"], "position": "after_records", "reason": "本院病变观察后"},
      {"stage": 2, "source_ids": ["IMG0002"], "position": "after_records", "reason": "选定影像轮次返回后"},
      {"stage": 3, "source_ids": ["LAB0001"], "position": "after_records", "reason": "一次证据更新，不预设评估完成"},
      {"stage": 4, "source_ids": ["DOC0001"], "position": "before_record", "task_mode": "independent_decision", "reason": "最早可定位的本次手术建议之前"}
    ]
  }
}
```

- `sha(bundle)` 是项目 `four_stage.sha` 对对象规范化计算的hash，不是整份JSONL文件hash。
- 当前来源编号按各数组原始行序生成，审核后任何源内容/顺序变化均需重新绑定bundle hash。
- `source_scope` 是来源归属声明，不由正文是否提到外院来推断。独立外院来源在origins中标为external，并须有原生 `hospital_received_time`；不可用review填造接收日期。
- 若有可信原生首次就诊时间，可用 `REVIEWED_FIRST_HOSPITAL_ENCOUNTER` 并指定 `encounter_time_field` 为 `visit_time`、`encounter_start_time` 或 `admission_time`，解释首次性与诊疗范围。
- `after_records` 以所选记录可见时间最后一个节点为 `<=` 边界；`before_record` 为一份文书的 `<` 边界。
- 无可靠阶段锚点时填 `source_ids: []` 并解释原因。四问仍保留，该问不生成有效messages。
- 复核任务设 `task_mode: plan_review`，完整保留更早建议。它与独立决策的评价分开。

检查时间代理需额外明确声明：

```json
"imaging_exam_proxy": {
  "accepted": true,
  "reason": "初步探查约定所选结果已返回；检查时间仅用于排序，未确认真实签发钟点"
}
```

文书晚日期只是版本候选。经独立核对确认整篇当时已可见，可添加全文版本review；它不修复缺失/冲突的完成时间，不生成新正文：

```json
"document_version_reviews": {
  "DOC0003": {
    "body_sha256": "<sha(完整正文)>替换为实际64位hash",
    "whole_record_available": true,
    "reason": "填写真实核对依据，例如未来日期实际是当时已有的预约计划"
  }
}
```

配置只组织来源与实验边界，不填写转移状态、可切除性或期望模型答案。自动候选识别与独立临床审阅尚未自动化。

## 运行

先运行完全虚构的示例，输出也仅保存在本地：

```sh
.venv/bin/python -m pancreatic_agent.episode_extract \
  --input examples/hewv_fictional_bundle.json --alias S001 \
  --reviews examples/hewv_fictional_review.json --output private/hewv_fictional_trial
```

该示例的bundle文件用单行JSON保存，兼容逐行读取。真实患者包使用JSONL：

```sh
.venv/bin/python -m pancreatic_agent.episode_extract \
  --input data/本地数据包/structured/patient_source_bundles_100.jsonl \
  --alias S001 \
  --reviews private/hewv_review.json \
  --output private/hewv_trial \
  --compare private/旧输出/S001/packets.json
```

`--compare`可省略，比较时核对别名及旧audit中的源bundle hash。旧配置不能直接改版本号使用。该命令无远程调用参数。

## 输出与阅读

- `packets.json`：episode、四个问题、各自checkpoint、完整资料、状态、context/messages hash。
- `01_context.md`至`04_context.md`：当前累计原文草稿。UNRESOLVED时仍供审阅，不是已接受模型输入。
- `01_messages.json`等：仅有效草稿生成，两条消息、无前问回答。当前采用[独立决策接口](../contracts/v0.4/05_decision_interface.md)的版本化system prompt、三种mode、一项首选动作及JSON；复核任务的问题措辞明确要求复核已有建议。
- `timeline.json`：全部来源及原字段、原文、未知时间、版本问题。含后续资料，只供审阅。
- `audit.json`：整份记录判定及理由、计划候选、代理与提示词/代码hash、零调用记录。
- `review_manifest.json`：该次来源配置。
- `semantic_diff.json`：可选，比较边界与真实正文载荷。

`DRAFT_REVIEW_REQUIRED`表示可用于继续核对的输入草稿，不等于完整临床快照。`UNRESOLVED`表示锚点/任务边界未定位或有效记录缺失。任何未知来源保留且标记，不把未知当阴性。

## 复用步骤

先在少量病例逐项核对本院范围、entry、目标建议、原文版本与时间依据，再批量执行相同规则。来源hash、配置和代码版本固定后可复现。新增医院先做字段/时间适配与患者关联核对，再运行同一可见性逻辑；不能直接将另一院的create_time当作签发字段。
