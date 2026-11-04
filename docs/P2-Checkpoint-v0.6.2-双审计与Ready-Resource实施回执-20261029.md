# P2 Checkpoint v0.6.2：双审计与 Ready Resource 实施回执

2026-10-29。状态：**工程实现及虚构端到端回归完成；真实开发运行、独立人工统计验收、临床协议冻结和医生发放未完成。**

本回执记录代码已实现、虚构反例已验证、真实数据待检验和临床未放行四种状态。它不是患者队列结果、临床质量报告或发布授权。

## 1. 已实现的独立候选重建审计

新包：`scripts/cohort_construction/p2_checkpoint_reconstruction_audit/`。

- `audit.py` 实现 `checkpoint-candidate-reconstruction-audit-0.1.0` 请求、七维响应核验和重建 handoff。请求绑定冻结图、声明范围完整原文、处理单位、零发现／被拒单位、发现／复核原提案、反馈历史、来源路由和最终依赖审计。
- 审计维度覆盖来源覆盖、根治路径相关性、事件身份、既往比较与证据演化、关系与 Episode、程序组装保真、时间与可见性。`whole_patient_chart_certified=false`，不把声明范围写成全病历。
- 发现及修订请求的原文引文必须在指定来源中唯一定位；缺覆盖、未解决维度、开放阻断发现或修订请求不能形成 PASS。
- `chain.py` 实现可重放的单图审核链。`REVISE` 形成 handoff 并停止，审计层不直接改图；新图必须新建请求，旧材料及交付 PASS 不迁移。
- 执行失败和零完成轮次只形成可重放 HOLD。篡改输入、响应、链绑定或停止状态会在离线重放中拒绝。

这里的 handoff 是明确的新图重建合同，不是已自动调用真实上游重建 Agent 的远程编排。

## 2. 已实现的实际 Episode／FHIR／HTML 交付审计

新包：`scripts/cohort_construction/p2_checkpoint_delivery_audit/`。

- `audit.py` 实现 `checkpoint-episode-delivery-audit-0.1.0`，审核 Episode 成员、跨点边界、证据保留、当前选择和未来泄漏、来源不确定性、实际 FHIR／HTML 保真、响应绑定及可评价性。
- 实际对象核验从 Bundle 恢复 packet，并以外部 packet hash 核验；核对 target、compiled hash、可见材料和 Episode ID。`doctor_view` 比较只排除 packet 派生的 `material_id`，不放宽来源、版本、正文或 hash。
- 有 assignment ID 时，使用同一 Bundle 和 trusted packet hash 调用生产 `build_reviewer_html()`，与实际 HTML 逐字节比较。
- 确定性初检覆盖 FHIR 扩展和附件、base64、链接、HTML 注释、隐藏内容及部分外部资源。正常应用的 hidden UI 控件不被误作隐藏临床内容；实际隐藏临床内容形成阻断发现。
- 阻断发现进入独立模型请求，不能由模型自报清除。交付 `REVISE` 由下游材料或 renderer 生成新版本，审计层不修改实际交付对象。
- `chain.py` 保存真实交付输入、响应和停止原因，支持无模型重放；失败零轮不能改名为 PASS。

生产 packet 门禁未被削弱。虚构整合测试仍通过 `freeze_checkpoint_packet()`，以显式标记的虚构独立 SemanticAudit fixture 构造 staging packet；没有从自动材料 PASS 自动制造人工独立性声明。

## 3. 实际 Chrome 下载与 Python／FHIR 回收已验证

新增：

- `tests/test_browser_delivery.py`；
- `tests/browser_delivery_checks.cjs`。

浏览器脚本从冻结 Questionnaire 读取真实答案标签，从 `assigned-data` 读取精确 `assignment_id`，不再使用不存在的 `.choice-input` 或把 UI 标签文本当作 ID。

实际 Chrome 对虚构 staging HTML 验证了：无预填、目标／当前／未来选择不在 DOM、真实答案填写、下载、零网络请求、无页面脚本错误及移动布局无横向溢出。下载的 submission 随后经：

1. `validate_submission()`；
2. `response_bundle_from_submission()`；
3. `submission_from_response_bundle()`；

按冻结 packet 和 assignment ID 完成 Python／FHIR 权威回收。另一个虚构反例验证被遮哨兵不进入实际医生 HTML。

该模块浏览器测试结果：**3 passed in 5.22s**。这是虚构交付接口测试，不认证真实人员、临床时间、临床语义或发布权限。

## 4. Ready Resource 与统计治理边界

- `ready.py` 的 `build_ready_resource_manifest()` 强制重放图审计链和实际交付审计链，并绑定同一 Episode 材料集；裸状态、裸响应或单 hash 不能替代链。
- 自动完成仅表达 `READY_FOR_HUMAN_SAMPLING`。实际人工统计结果、冻结临床协议及外部治理授权分别核验。
- `sampling.py` 冻结外部给定的总体绑定、层权重、聚类单位、严重性门槛、缺回收／未决政策和扩容／全量复核触发规则，不自行选择阈值。
- 自动＋抽样替代路径需要显式版本化外部授权，逐项绑定材料集、交付输入、pipeline、适用总体、抽样设计、实际人工结果和临床协议；严格独立人工 SemanticAudit 路径保留。
- 虚构完整绑定链可表达 `CLINICAL_PACKET_ELIGIBLE`，但始终 `doctor_assignment_created=false`、`clinical_release_permitted=false`、`actual_release_requires_separate_execution=true`。

当前人工统计对象是外部实际结果的**绑定核验接口**，没有在本模块内重新计算真实加权总体估计、聚类区间或验证实际人员身份。真实统计验收仍未完成。

## 5. 问题登记与回归

新增稳定问题：

- `CE-GRA-001—003`：完整审计范围、原文证据、漏事件／错误 Episode 重建、新图重启和图链防篡改；
- `CE-DLV-001—004`：实际对象身份、隐藏内容、实际 Chrome 下载／回收和交付链防篡改；
- `CE-RDY-001—002`：Ready Resource 强绑定、实际人工结果与外部授权错配。

每项均登记失败反例、必要正对照、控制和真实测试引用，位于两个新包的 `regression_issue_map.json`。`p2_checkpoint_review_app.verify` 已纳入两个新包和实际测试收集。

完整守卫执行：

```bash
python3 -m scripts.cohort_construction.p2_checkpoint_review_app.verify \
  --output private/p2_checkpoint_next_20261028/validation_dual_audit_v062_20261029
```

结果：**PASS；860 项测试、449 项子测试通过**。23 份问题清单、573 个必需测试引用全部实际收集并执行；1,690 份历史保护产物运行前后 hash 不变；运行期间源文件未变化；临床模型调用 0，`clinical_accuracy_measured=false`，`p1_files_modified=false`。

回执：`private/p2_checkpoint_next_20261028/validation_dual_audit_v062_20261029/validation_receipt.json`。同目录保存收集／执行回执、JUnit XML、测试日志和代码 diff。回执 SHA-256：`b6c5bab3e37945fb7da2de22b1705e435810f520bbb0b584d3618eaac1a15ef0`。

此前 v0.6.1 装配收尾的包级回归为 **96 passed**；本次 860 项是纳入 v0.6.2 后的完整 P2 守卫结果，不覆盖或改写历史回执。

## 6. 尚未完成与禁止推断

1. 真实患者小批开发、固定模型／提示／协议／代码 hash 的 PASS／HOLD 分布、成本和操作质量尚未测量。
2. 独立人工分层抽样、实际回收、加权总体估计、聚类区间和预注册门槛验收未开展，质量指标为 `not_measured`。
3. D01—D11、实际临床表单、分配／盲法、医生试评、专家 Reference 和外部发放批准均未完成。
4. 图与交付响应 schema 当前为 Python 内联 Draft 2020-12；接口可执行不代表真实模型提示质量已验证。
5. 没有启动真实患者远程模型、真实人工正式抽样或真实医生分配，没有修改 P1，没有提交或推送 Git。

因此可以宣称 **v0.6.2 工程闭环已通过虚构整合验证**，不能宣称真实 ready-resource 已完成统计验收、临床放行或大范围盲评。
