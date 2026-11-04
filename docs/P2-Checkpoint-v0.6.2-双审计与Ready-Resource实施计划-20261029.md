# P2 Checkpoint v0.6.2：候选重建与实际交付双审计、Ready Resource 实施计划

2026-10-29。状态：**工程实现与虚构整合回归完成；真实开发运行、独立人工统计验收、临床方案冻结和医生发放未完成。**

本版本承接 v0.6.1 的自动材料治理，补上两个独立审计对象：程序组织后的候选 Episode／Checkpoint 图，以及实际派生的 FrozenCheckpointPacket、FHIR Bundle 和医生 HTML。工程状态不等于临床正确性、人工验收或发布许可。详细实施依据和验证范围见 [双审计与 Ready Resource 实施回执](P2-Checkpoint-v0.6.2-双审计与Ready-Resource实施回执-20261029.md)。

## 1. 要闭合的问题

即使发现阶段 LLM、联合复核 LLM 和文书分割均正确，程序仍可能丢失事件、错接既往比较对象、错误合并 Episode、错误排除目标 Checkpoint，或在 compiled→packet→FHIR→HTML 的派生过程中引入目标错配和隐藏泄漏。v0.6.1 的逐 Checkpoint 材料审核不能替代对这两个对象的独立审计。

v0.6.2 保持两组“两原则”分离：

1. 候选定位：是否关联 PDAC 可切除性／根治路径；相对合适的既往管理判断是否存在决策相关证据演化。首个可见安排可作起始锚点，不虚构两轮演化。
2. 材料隔离：目标时点可见；当前选择隔离且决策依据保留。另查同义回述、执行、结局、标题、元数据、FHIR 附件／扩展和 HTML DOM／脚本／样式泄漏。

原始事实以对应时点原文为准；未提供资料保持未知。程序或 LLM 的审计标签不构成 Clinical Reference，也不以实际手术或结局决定患者相关性。

## 2. 候选重建独立语义审计

实现包：`scripts/cohort_construction/p2_checkpoint_reconstruction_audit/`。

### 2.1 审计输入与七维判断

版本化请求绑定冻结图、声明范围内完整原文、处理单位、零发现／未入选／被拒单位、发现阶段 LLM 原提案、复核阶段 LLM 原提案、反馈历史、来源路由和最终依赖审计。审计者逐项回答：

- `source_coverage`：声明范围的原文与单位覆盖，不把范围外资料写成已读；
- `radical_pathway_relevance`：根治路径关联、推进／暂缓／补证据／不推进及漏事件；
- `event_identity`：事件误拆、误并、重复、执行与回述；
- `comparison_and_evolution`：起始锚点与证据演化、既往比较对象及新证据；
- `relations_and_episode`：关系、同管理任务、Episode 成员和边界；
- `program_assembly_fidelity`：上游合理提案是否被程序丢失、错接、错排或错误排除；
- `time_and_visibility`：时间证据、阶段描述和来源可见性是否过度声称。

“完整原文”只指请求逐项列明的处理单位和来源，不宣称全患者病历已读。证据引文必须在指定原文中唯一定位。

### 2.2 有界回修与新图重启

状态为 `AUTO_GRAPH_AUDIT_PASS／REVISE／HOLD`。`REVISE` 产生带原文证据的重建 handoff 并停止；审计层不直接修改图，也不在旧图上无限反馈。需要人工或未决来源的请求保持 HOLD。新图必须新建审计请求，并重新执行材料准备、图审计、材料审计和交付审计；旧 PASS 不可迁移。

每条链保存输入、响应、发现、handoff、停止原因及 hash，可无模型权威重放。零完成轮次的失败只能是 HOLD，不得改写为 PASS。

## 3. 完整 Episode 与实际交付审计

实现包：`scripts/cohort_construction/p2_checkpoint_delivery_audit/`。

### 3.1 审核对象

在 v0.6.1 逐点材料审核之外，独立审核完整 Episode 的成员、跨 Checkpoint 边界、证据保留、当前选择和未来泄漏、来源不确定性、实际 FHIR／HTML 保真及响应绑定与可评价性。

确定性核验不以预览代替实际对象：

- 从 Bundle 恢复 packet，并用外部提供的 packet hash 核验；
- 核对 compiled target、packet target、Episode ID、可见材料及分配 ID；
- 有完整材料上下文时，以同一 Bundle、assignment 和 trusted hash 重建 HTML 并逐字节比较；
- 检查 FHIR 扩展、附件、base64、链接及 HTML 注释、隐藏内容、外部资源和 DOM；
- 正常应用的 hidden UI 控件与隐藏临床内容分开处理。

自动交付状态为 `AUTO_DELIVERY_AUDIT_PASS／REVISE／HOLD`。确定性阻断发现进入模型请求，模型自报不能清除。审计层不直接修改 packet／FHIR／HTML；修复由下游材料或 renderer 层生成新版本后重审。

不得为自动审计绕过生产 `freeze_checkpoint_packet()` 的独立 SemanticAudit 门禁。虚构测试 fixture 不是实际人员声明。

### 3.2 浏览器与响应回收

实际 Chrome 对虚构 staging HTML 执行读取、无预填、DOM 无隐藏选择、真实答案填写、下载和移动布局检查。下载文件再由 Python `validate_submission()`、FHIR 响应 Bundle 构造和反向恢复，按冻结 packet 与精确 assignment ID 权威回收。浏览器结果是镜像核验，不认证人员、临床时间或发布权限。

## 4. Ready Resource 与统计治理

`ReadyResourceManifest` 分离：

- `CANDIDATE_NOT_READY`；
- `READY_FOR_HUMAN_SAMPLING`；
- `SAMPLING_VALIDATION_PENDING／QUALIFIED／FAILED`；
- `READY_FOR_CLINICAL_PILOT`；
- `CLINICAL_PACKET_ELIGIBLE`。

自动路径必须提供可重放的图审计链、材料集和实际交付审计链，拒绝裸状态或裸 LLM 响应。自动＋抽样的替代放行路径必须绑定冻结抽样设计、适用总体、实际人工结果、临床协议和显式版本化外部治理授权。严格独立人工 SemanticAudit 路径继续保留，不能暗中绕过。

抽样设计接口只记录外部既定政策：总体、层权重、聚类单位、严重性门槛、缺回收／未决、扩容及全量复核触发规则。它不擅自选择阈值，也不替代正式加权总体估计或聚类区间计算。

即使组合状态为 `CLINICAL_PACKET_ELIGIBLE`，`doctor_assignment_created=false`、`clinical_release_permitted=false`。实际发放须另行执行获批流程；医生仍可因泄漏、资料不足或任务不可评价而 EXPERT_HOLD。

## 5. 验证、问题登记与完成边界

稳定问题编号：

- `CE-GRA-001—003`：图审计范围与原文证据、漏事件／错误 Episode 回修、新图重启和审计链防篡改；
- `CE-DLV-001—004`：实际对象身份、隐藏内容、Chrome 下载／FHIR 回收和交付链防篡改；
- `CE-RDY-001—002`：Ready Resource 强绑定、实际人工结果和外部治理授权。

必需反例包括程序丢事件、错误 Episode 合并、旧图 PASS 迁移、不可定位引文、空 PASS、目标／packet／HTML 替换、隐藏扩展／DOM、失败链改名和错授权绑定。正对照包括完整声明来源、合法既往材料、正常 hidden UI 控件及实际下载回收。

完整 P2 守卫须实际收集并执行新增包测试，核验全部问题清单引用及历史保护文件。完成口径仅为“工程实现与虚构整合回归完成”；真实质量指标仍为 `not_measured`，不报告患者人数、临床合格率或医生发放结果。
