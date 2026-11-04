# CE-AMG-027 / CE-GRA-012：多轮 Agent 状态连续性修复与版本差异

日期：2026-11-04。范围：P2 材料复核、候选图重建后的独立审核及其持久化回放。

## 问题与归因边界

材料续轮原先虽会收到新分区和 hash，但没有收到同一审核任务前一轮的完整输出、逐项程序执行回执及实际前后差异；指令也近似首轮审核。重建后的新图审核同样缺少父图审核、独立重建提案和确定性执行回执。该缺口属于 harness 的状态组织问题，不能仅据审核意见回摆推断 LLM 能力不足。

本修复只改变研究请求的历史表达、确定性核验和程序对无效续轮的拒绝，不认定源资料质量、临床决策正确性或模型性能；不改变患者成员、候选 Episode 的临床身份，也不创建医生 Reference。

## 材料：0.2.0 → 0.3.0

- 续轮请求新增 `review_round` 和 `previous_round`：包含前次完整审核、程序执行回执、逐项操作回执、实际分区/来源范围/医生可见文本差异，以及当前 manifest、compiled 和 doctor-view hash。
- 系统指令及请求要求明确：当前状态是前一轮建议经确定性程序执行后的结果；本次是续轮而非首轮。审核者须用相同 `finding_id` 交代 OPEN finding 的当前状态，不得把“再次审核”理解成“必须重新发现问题”。
- `MASK`、`REVIEW` 都不开放医生原文。`AUTO_REVIEW_REVISE` 必须改变实际医生可见材料或声明来源范围；同一来源范围内纯内部 `MASK ↔ REVIEW` churn 不得继续消耗自动修订轮次。
- `INCLUDE_SOURCE` / `EXCLUDE_SOURCE` 仍是合法来源范围修订。新增来源初始全部为 `REVIEW` 时，允许下一轮显式处理，或以具体待办 `HOLD` 停止；不能为了制造可见变化而无依据开放原文。
- 新增全遮蔽来源的占位条目不再被误算成医生可见临床文本变化。
- 人工请求、来源待核、图问题和具体待办不能被续轮自行清除。新 hash 必须重新审核；自动 PASS 仍不是人工或临床许可。
- Python 权威 replay 和浏览器研究者镜像同时核验正常轮及失败请求的历史。`0.2.0` 历史链按旧协议重放，不注入新字段。

## Graph：0.1.0 → 0.2.0

- 新图审核通过 `reconstruction_lineage` 获得完整父审核链、独立重建链、前次审核输出、重建响应、确定性执行回执和实际图差异。
- lineage 从持久化链重放构造；diff 使用 `discovery_units[*].events`、按有序事件对识别的 relations、`episode_id` 及图元数据，不虚构顶层事件或 `relation_id`。
- 重建后的审核仍是全新独立审核：旧 REVISE、重建任务完成或旧材料 PASS 均不转移为当前 PASS。前次发现须以相同 ID 说明当前状态及原文依据。
- 重放重算完整 lineage，而非仅核对尾 hash；篡改历史、替换图和向旧协议注入 lineage 均拒绝。
- lineage 只进入 Graph 审核请求，不改写冻结 input contract。旧 `0.1.0` 请求和链不升级，不新增历史字段。

## 稳定问题与回归

- `CE-AMG-027`：续轮自身历史及执行回执、纯内部 churn、来源范围和新来源待解决状态；包括 Python、浏览器 replay 和篡改反例。
- `CE-GRA-012`：新图 lineage、真实图结构 diff、历史篡改拒绝和旧链兼容。
- 两项均登记在相应 `regression_issue_map.json`，含失败反例、正/负对照及必需测试。定向回归覆盖材料、Graph、ready-pipeline 和 real-validation 全部测试，共 **159 项通过**；该回归使用合成或 mock 路径，零真实模型调用。
- 历史十例以 `verify_implementation=False`、`persist=False` 完成无模型 replay：**10/10 可重放**；终态分布为 `AUTO_GRAPH_AUDIT_HOLD` 1、`AUTO_GRAPH_AUDIT_REVISE` 4、`HOLD` 5。该结果只证明历史链与旧协议仍可重放，不证明当前实现字节与旧实现一致，不证明临床语义正确或自动审核通过。
- 完整 P2 守卫回执：`private/p2_checkpoint_next_20261028/validation_agent_state_continuity_v063_r1_20261104/validation_receipt.json`，**928 项测试、449 项 subtests 通过**；25 份问题清单的必需测试核验通过，历史保护产物前后 hash 不变，测试期间源码不变，回归中 0 次真实模型调用。回执 SHA-256：`c0b45e1febe00d88d6360a9dd58ef030097222872a4faba944704061c9c11a08`。
- 真实小范围模型复验将另存独立回执；未完成项不得写成已验证。P1 文件未修改，未执行或伪称 P1 专项验收。

## 历史结果保护

基线提交 `bd420c0` 及其历史输出不覆盖。本修复形成独立版本；真实重试使用新私有输出目录、fresh cache 和仅限所选 case 的授权，不继承原全十例执行范围。任何新终态都只是该模型和 harness 版本下的研究运行结果，不是临床放行。
