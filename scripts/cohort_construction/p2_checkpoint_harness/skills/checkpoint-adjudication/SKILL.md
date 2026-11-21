---
name: checkpoint-adjudication
description: 用最小按需检索裁决候选CP的证据演化、同轮支持与时间不确定性；适用于全病例checkpoint_adjudication阶段。
---

# 候选CP证据演化裁决

## 职责边界

你审核程序生成的候选CP，不重新发现候选，不修改原文，不创建临床Reference。程序负责comparator、时钟、报告身份、引文和span核验；你负责判断当前文书是同轮支持、真正出现新患者证据，还是关系待核。

## 默认路径：最少必要读取

1. 逐页 `list_candidate_checkpoints`，诊断CP不裁决，管理CP按clock从早到晚处理。
2. 每个管理CP调用 `read_checkpoint_adjudication_context`。程序返回当前CP和唯一最近保留comparator，不需要选择或填写comparator ID。
3. 阅读上下文中的双方clock、kind、candidate_actions、member spans和 `diagnostic_target_source_ids`，再 `read_source(source_id,start=0)` 完整读取至少一份当前CP来源至文末（end省略），不得只读candidate附近片段就声称全篇没有新证据。
4. 当前文书只是旧报告复述、既定计划、转科、执行或更具体安排时，直接 `SUPPORT_ONLY`，不需要引文，也不要求读取诊断comparator的报告正文。若文书记载的是comparator之后新发生的外院检查、治疗经过或疗效变化，则不属于旧复述，按证据演化继续判断。
5. 准备声称 `RETAIN_CP+NEW_EVIDENCE_ROUND` 时，诊断comparator的目标报告必须在当前上下文中读取；此前其他CP的读取不替代本次读取。新证据关系不清时可选检索、读取相关原始证据，不要求为了提交REVIEW而补读诊断报告。
6. 证据演化与来源核验分开。外院或来源不明的原报告未交付，不妨碍当前文书记录构成新证据：引用当前文书中的逐字复述，设置 `evidence_source_status=EXTERNAL_RESTATEMENT` 或 `UNATTRIBUTED_RESTATEMENT`，程序自动添加来源与底层时间待核标签。原报告可找到且证明是comparator旧报告时，仍须引用原报告，不得重新定时。
7. 只有证据是否新增、先后关系或同刻顺序确实无法判断时才用 `REVIEW_REQUIRED+UNCERTAIN`。该状态保留独立候选CP，不自动归并；`review_tags` 用于说明具体待核事项。

## 按需检索：先限定范围，再读原文

`search_checkpoint_sources` 默认只检索当前CP与程序comparator之间的资料：

- 只传 `checkpoint_id`：返回区间资料索引和可继续检索的标题词，不展开正文。
- 加 `query`：在选定范围内做字面检索，返回命中来源、时间、字符偏移和局部上下文。
- `scope=INTERVAL`：区间资料，默认。
- `scope=CURRENT`：当前CP来源。
- `scope=COMPARATOR`：程序comparator来源，用于核对目标报告身份。
- `scope` 或索引已经能确定关系时，不再调用全病例 `search_sources`；确需跨范围检索才使用通用工具。

检查名称只是检索线索，不等于证据演化。优先使用上下文已经给出的报告标题、检查日期和来源ID，不需要机械尝试CT、MR、EUS等关键词清单。命中后用 `read_source(source_id,start,end)` 精读必要区间；若 `truncated=true`，只补读判断所需部分。

`list_checkpoint_interval_sources` 是等价的纯索引工具，同样自动继承comparator；不需要传comparator ID，不要求逐项处置全部区间资料。

## 提交裁决

- `FIRST_VISIBLE`：全病例没有更早或同刻未决候选状态的首见锚点。
- `NEW_EVIDENCE_ROUND`：当前CP相对comparator出现新的或发生变化的患者信息，附逐字连续quote。来源可以是原始报告，也可以是明确记载新检查或治疗变化的当前文书；后者必须标记外院或未明来源复述。当前或其他CP的管理动作句不能作为患者证据。
- comparator已有报告及其副本、后续文书中的旧复述均不是新证据。未来报告即使因身份核对被读取，也不代表当前可见。外院原报告未交付不等于证据不存在，也不等于复述一定是旧证据。
- `SUPPORT_ONLY`：使用 `SAME_ROUND_SUPPORT`、`REPEATED_PLAN` 或 `EXECUTION`，不提供证据引文。
- `evidence_source_status`：原始/本院证据用 `ORIGINAL_OR_LOCAL_SOURCE`；外院复述用 `EXTERNAL_RESTATEMENT`；来源不明复述用 `UNATTRIBUTED_RESTATEMENT`。来源待核标签由程序生成，其他具体待核事项用 `review_tags`。
- 每次只处理一个CP，取得所需读取结果后在后续工具批次单独提交裁决，不能把读取和裁决放在同一批；提交成功后立即继续，不反复列出全部CP，全部完成后 `finish`。

程序拒绝时先读错误所指的对象，不要只更换一段相似文字重试。程序接受仍只是研究候选通过结构核验，不是专家临床确认。
