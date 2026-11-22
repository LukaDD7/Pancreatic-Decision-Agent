---
name: checkpoint-adjudication
description: 用最小按需检索裁决候选CP的证据演化、同轮支持与时间不确定性；适用于全病例checkpoint_adjudication阶段。
---

# 候选CP证据演化裁决

## 职责边界

你审核程序生成的候选CP，不重新发现候选，不修改原文，不创建临床Reference。程序负责comparator、时钟、报告身份、引文和span核验；你负责复审检查执行—报告时间代理—手术边界与候选任务是否冲突，并区分无新增的同轮支持、新证据后的实质管理再评价、有新增证据但无独立管理再评价，以及关系待核。

## 先处理时间与任务冲突

1. 逐页 `list_candidate_checkpoints`，找出 `review_flags` 含 `ACTION_TASK_MISMATCH_REVIEW_REQUIRED` 的CP。先调用 `read_checkpoint_action_review_context`，阅读当前CP、可选候选动作、`acquisition_clocks`、phase和手术边界；字典动作仅为可选参考。
2. `read_source(source_id,start=0)` 完整读取至少一份当前来源及至少一份 `operation_source_ids` 中的手术记录，均省略end。若结果被spill替换为头尾预览，必须用 `read_spilled` 对该locator分页读完，预览不算全文。不能只凭 `ACTION_TASK_MISMATCH_REVIEW_REQUIRED` 标签或动作名称下结论。
3. 每个标记CP调用一次 `review_checkpoint_action`。report time只是正式报告时间代理，不是医生实际阅片或知晓结果的时刻；医生可能提前阅片。但手术开始前已完成、报告时间代理落在手术后的检查不再承担独立的术前评价任务时，用 `REMOVE_INDEPENDENT_CP` 撤销独立诊断CP；报告原文仍由程序保留为上下文。若仍有独立术后管理任务，用 `RETAIN_CP_WITH_ACTION` 保留CP，动作参考可全部留空，也可用 `OTHER_SPECIFIED` 自由描述字典外动作，不做动作阶段或完整性校验；是否留空不影响本复审。
4. 管理CP不能用这个动作工具删除。动作复审完成后才进入管理CP证据演化裁决；被撤销的诊断CP立即退出比较状态，程序自动确定下一个最近保留comparator，不需要Agent选择。

## 默认路径：最少必要读取

1. 逐页 `list_candidate_checkpoints`，完成上述动作复审后，管理CP按clock从早到晚处理；其他诊断CP不做证据演化裁决。
2. 每个管理CP调用 `read_checkpoint_adjudication_context`。程序返回当前CP、唯一最近保留或待核comparator和 `same_time_candidate_states` 同刻候选索引，不需要选择或填写comparator ID。待核CP若最终保留为独立候选，也进入后续实际比较全景。
3. 阅读上下文中的双方clock、kind、candidate_actions、member spans和 `diagnostic_target_source_ids`，再 `read_source(source_id,start=0)` 完整读取至少一份当前CP来源和至少一份管理comparator来源至文末（end省略），不得只读candidate附近片段或结构化详情就声称全篇没有新证据。同刻候选不能严格定序，但不妨碍语义判断：横向阅读原文后，可以判同轮支持、重复计划或执行；只有语义关系也不明时才待核。
4. 若上下文有 `timed_interval_report_groups`，逐组 `read_source` 其 `representative_source_id`。这些是程序按报告时钟确定落在当前区间的原始检验、影像、病理或高风险检查报告；区间内检验合并为一个批次，只读一个最新异常代表项核对是否有新检验，不需要逐项阅读，也不能声称已读所有项目。裁决理由必须处理这类新报告，不得再写“无新检查”。程序确认存在这些新报告时不得提交无新增的 `SUPPORT_ONLY`；引用新报告后按是否形成实质再评价选择 `RETAIN_CP` 或 `CONTEXT_ONLY`，只有关系确实不明时才提交 `REVIEW_REQUIRED+UNCERTAIN`。
5. 当前文书只是旧报告复述、既定计划、转科、执行或更具体安排，且没有新区间证据时，直接 `SUPPORT_ONLY`，不需要引文，也不要求读取诊断comparator的报告正文。若文书记载的是comparator之后新发生的外院检查、治疗经过或疗效变化，则不属于旧复述，按证据演化继续判断。
6. 准备声称 `RETAIN_CP` 或 `CONTEXT_ONLY` 的 `NEW_EVIDENCE_ROUND` 时，诊断comparator的目标报告必须在当前上下文中读取；此前其他CP的读取不替代本次读取。新证据关系不清时可选检索、读取相关原始证据，不要求为了提交REVIEW而补读诊断报告。
7. 证据演化与来源核验分开。外院或来源不明的原报告未交付，不妨碍当前文书记录构成新证据：引用当前文书中的逐字复述，设置 `evidence_source_status=EXTERNAL_RESTATEMENT` 或 `UNATTRIBUTED_RESTATEMENT`，程序自动添加来源与底层时间待核标签。原报告可找到且证明是comparator旧报告时，仍须引用原报告，不得重新定时。
8. 只有证据是否新增、是否形成实质再评价或语义关系确实无法判断时才用 `REVIEW_REQUIRED+UNCERTAIN`，不能仅因同刻就待核。该状态保留独立候选CP并进入后续comparator全景，不自动归并；`review_tags` 用于说明具体待核事项。

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

- `FIRST_VISIBLE`：全病例没有更早候选且尚未形成同刻保留或待核状态的首见锚点。首见依据可以是当前文书中已有的外院检查复述；此时可附该依据并标 `EXTERNAL_RESTATEMENT`，程序保留原报告与底层时间待核标签，但它不是 `NEW_EVIDENCE_ROUND`。若交付中已有匹配原报告，仍须改用原报告来源；其他管理 CP 的动作句不能冒充首见患者证据。
- `NEW_EVIDENCE_ROUND`：当前CP相对comparator出现新的或发生变化的患者信息，附逐字连续quote。来源可以是原始报告，也可以是明确记载新检查或治疗变化的当前文书；后者必须标记外院或未明来源复述。当前或其他CP的管理动作句不能作为患者证据。
- **证据演化的底线是新增**：不要求结果异常、阳性、具有专科特异性或实际改变管理方向。comparator之后新回报的常规检验、术前监测、正常或阴性结果同样构成证据演化，不得以此为无新增的 `SUPPORT_ONLY` 理由。
- **独立管理CP还需要实质再评价**：同一未决管理问题下，证据可以连续积累，但只保留最能体现“新证据后的实质再评价”的代表CP；动作是否相似不是判断标准。保留理由必须说明新增证据推进了哪个具体未决问题，以及当前文书如何围绕该变化进行实质再评价。关键证据解决此前不确定时，动作不变仍可保留。
- `CONTEXT_ONLY+NEW_EVIDENCE_ROUND`：报告确实新增，但当前文书只是复述结果、重复既定计划或写下执行安排，没有推进未决问题的实质再评价。必须引用新增证据；不形成独立评价CP，但推进后续comparator的证据时间线，防止同一证据被下一CP重复当成新增。原文按原时钟保留，不回填为较早CP的当时可见信息。
- 连续上下文之后若出现真正的实质再评价，后续 `RETAIN_CP` 可引用 `pending_context_evidence` 中的原 `source_id`，携带已积累证据成为代表CP。`CONTEXT_ONLY` 不能重复消费同一证据；相同报告的副本不能换 `source_id` 绕过。
- comparator已有报告及其副本、后续文书中的旧复述均不是新证据。未来报告即使因身份核对被读取，也不代表当前可见。外院原报告未交付不等于证据不存在，也不等于复述一定是旧证据。
- `SUPPORT_ONLY`：仅在没有新增患者证据时使用 `SAME_ROUND_SUPPORT`、`REPEATED_PLAN` 或 `EXECUTION`，不提供证据引文。程序确认有 `timed_interval_report_groups` 时会拒绝该裁决。
- `evidence_source_status`：原始/本院证据用 `ORIGINAL_OR_LOCAL_SOURCE`；外院复述用 `EXTERNAL_RESTATEMENT`；来源不明复述用 `UNATTRIBUTED_RESTATEMENT`。来源待核标签由程序生成，其他具体待核事项用 `review_tags`。
- 每次只处理一个CP，取得所需读取结果后在后续工具批次单独提交裁决，不能把读取和裁决放在同一批；提交成功后立即继续，不反复列出全部CP，全部完成后 `finish`。

程序拒绝时先读错误所指的对象，不要只更换一段相似文字重试。若收到 `SECONDARY_RESTATEMENT_REQUIRES_ORIGINAL_SOURCE`，下一步必须 `read_source` 错误列出的匹配候选，用原始报告的 `source_id` 和逐字 `quote` 替换复述引文，不得原样重复提交；原报告不能证明严格晚于comparator或无法支持证据演化时改 `SUPPORT_ONLY`，来源或关系确实不明时改 `REVIEW_REQUIRED+UNCERTAIN`。若收到 `EVIDENCE_ORIGINAL_REPORT_AFTER_CHECKPOINT`，删除对应未来检验引文，不得标为来源未明；仅可另引当前时点已发生的新生理状态或治疗经过，否则改判。若收到 `CONTEXT_EVIDENCE_ALREADY_CONSUMED`，停止替换已消费引文；它证明该证据已消费，不等于关系不明，不得仅据此改 `REVIEW_REQUIRED`。若收到 `EVIDENCE_NOT_NEW_AFTER_COMPARATOR`，按错误中的实际关系处理：明确早于comparator时不得改 `REVIEW_REQUIRED`；同刻或时间未知只表示不能证明严格晚于，可阅读后判同轮 `SUPPORT_ONLY`，确实因此无法判断新增关系时才 `REVIEW_REQUIRED+UNCERTAIN`。有pending且当前形成实质再评价时，用pending原 `source_id` 提交 `RETAIN_CP`；没有pending或其他真正新增证据且关系明确时，即使出现新动作、手术或转科计划，也改 `SUPPORT_ONLY`，然后继续后续CP。程序接受仍只是研究候选通过结构核验，不是专家临床确认。
