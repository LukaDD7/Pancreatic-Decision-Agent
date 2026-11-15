---
name: candidate-cp-organization
description: 只为程序生成的候选Checkpoint记录有逐字原文支持的CP级关系。
---

# 候选Checkpoint关系记录

## 节点与职责

端点是 `checkpoint_id`，不是旧流程的 `event_id + span`。管理CP可能已经合并同一来源的多个 `member_event_ids`，不得拆回这些事件另建关系。诊断双节点可以没有管理 `masked_spans`，不得因此伪造span。

你只记录CP之间的 `supported` 边。程序负责诊断pair边和Episode组装，并保留全部未连接CP；不要预览、指定或修改容器。

## 工作方法

先用 `list_candidate_checkpoints(limit=200)` 比较各CP的实际 `masked_spans`、来源、类型与时钟。只有一条具体关系缺少必要上下文时，才用 `read_candidate_checkpoint`、`read_source` 或 `search_sources` 查局部原文。发现明确关系立即 `record_relation`，不确定就不记录。

每条边必须附两端引文。每条引文包含 `checkpoint_id`、`source_id` 和逐字连续的 `quote`；可选 `start/end` 必须同时给出且准确。引文须属于该CP的 `source_ids`，来自实际 `masked_spans` 或该来源原文，不能拼接、改写或用第三份文书替代。诊断CP可引用其检查报告原文，不要求管理span。

## 关系语义

- `same_management_process`：原文支持两个CP服务同一管理目标，并填写 `shared_management_goal`。只有该关系会由Agent边触发Episode合并；同日、同次查房、同标题或时间相近不足。
- `same_event`：严格重复身份，不是组合/组成关系，不单独合并Episode。
- `contains`：组合CP指向组成CP；`overlap`：部分内容重叠。两者均不合并Episode。
- `evidence_used`：方向只能是 `evidence_arrival_reassessment` → `management_decision`。原文必须明确支持后续管理判断使用了该报告证据；检查时间接近、内容相关或报告可见本身均不构成证据使用。该边不合并Episode。
- 检查执行前 → 报告可见后的 `evidence_arrival` 由程序生成。证据到达不等于证据使用，不要重复创建或改写成 `evidence_used`。

程序核验端点、方向与引文，不把这种核验当作专家临床确认。记录完有依据的边后调用 `finish`。
