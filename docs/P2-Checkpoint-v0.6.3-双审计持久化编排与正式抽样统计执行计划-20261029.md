# P2 Checkpoint v0.6.3：双审计持久化编排与正式抽样统计执行计划

日期：2026-10-29  
状态：已批准的工程执行计划；不是临床方案冻结或真实运行授权。

## 1. 范围与不可变边界

在 v0.6.2 的独立合同基础上，将以下关系实现为可持久化、可续跑、无模型重放的 Episode 工程管线：

```text
候选图 → 图审计 → 材料治理 → Episode 材料集
       → 实际 packet/FHIR/HTML → 交付审计 → Ready Resource
```

同时把 Ready Resource 的统计合格判断从自报状态或 hash 改为对冻结 design、plan、实际人员响应和 assessment 的确定性重放。

- 原始临床文字仍是事实来源；程序或 LLM 标签不替代原文、不生成临床 Reference。
- 图审计不直接改图；材料层不直接改图；交付审计不直接改 packet/FHIR/HTML。
- 自动材料审核不冒充独立人工 SemanticAudit。
- 不覆盖 v0.6.0—v0.6.2 输出、历史模型提案、人工任务或回执。
- 默认配置禁止真实患者远程模型、真实医生分配和临床发布；本计划只授权虚构资料和 mock 的开发回归。
- 不修改 P1。若另行触及 P1，须执行 P1 专项回归守卫。

## 2. Episode 持久化编排

新增 `scripts/cohort_construction/p2_checkpoint_ready_pipeline/`：

1. **冻结合同**：绑定图、来源、Episode、问卷、图审计上下文、材料与审计协议、可见性政策、执行预算和治理配置。
2. **默认离线准备**：无显式执行参数与调用者提供的适配器时不调用 provider；CLI 只提供离线和重放命令。
3. **共享预算**：图、逐 Checkpoint 材料、交付审核共用未缓存调用预算；provider 前检查预算；缓存命中不重复计入；无 receipt 保守计为未缓存。
4. **阶段门控**：图 PASS 后才装配材料；该 Episode 所有目标材料 AUTO_REVIEW_PASS 后才建立 staging 实际交付；交付 PASS 后才形成 `READY_FOR_HUMAN_SAMPLING`。
5. **独立语义输入**：packet 编译使用调用者提供且绑定 compiled 的 SemanticAudit。虚构 fixture 不能成为真实人工独立性的证明。
6. **持久化重放**：保存每阶段请求、响应、链、journal、材料集、实际交付、失败、预算与实现快照；`validate_pipeline_run()` 不接受裸 PASS。
7. **新目录续跑**：只继承可重放阶段，父目录不修改；合同变化拒绝继承。已保存链是原始 review chain，不以额外 receipt hash 取代链级重放。

## 3. 版本化回修

- **图 REVISE**：保留 handoff，只接受调用者提供的独立重建图；版本和 hash 均须变化，新运行重新执行图、材料、交付全部审计。
- **材料图问题**：从实际审核轮次的 `review` 和 `ledger` 形成 handoff；材料编译器不修改事件图。
- **交付 REVISE**：只接受调用者提供的新实际交付对象；交付输入 hash 必须变化，并重新执行交付审计。
- **历史父运行**：保存 `superseded_parent_run` 与 revision receipt。重放以 REVISE 父运行验证修订，而不是把当前 PASS 子运行误当父运行。
- 相同图或交付、缺失下游新对象、无显式 revision cycle 均失败关闭。

## 4. 正式抽样统计计算

在 `p2_checkpoint_delivery_audit/sampling.py` 实现并冻结：

- 单 Episode 与多 Episode 总体及各自绑定；
- 互斥层、层权重、完整总体注册表、整数 seed 和有序 assignment；
- 响应对 assignment、design、plan、人员声明、独立性和带时区时间的绑定；
- 按层权重的缺陷比例点估计；
- 患者、Episode、Checkpoint 聚类线性化方差与正态单侧聚类稳健区间；
- 缺回收、未决、REVISE、零方差、自由度不足分别处理；
- 扩容／全量复核只产生新计划请求，不改写原 assignment。

`ready.py` 重放 design、plan、responses 和 assessment；不再接受裸 `SAMPLING_QUALIFIED` 或自报结果 hash。多 Episode assessment 不驱动单 Episode Ready。`CLINICAL_PACKET_ELIGIBLE` 即使出现在虚构测试中也不创建真实医生任务或发布。

## 5. 注册与验证

- 新增 `config/p2_checkpoint_dual_audit_v063.json`，保留 v0.6.0—v0.6.2 配置。
- 将新包加入 `p2_checkpoint_review_app.verify` 的 source hashing、pytest collection 和 issue map 执行范围。
- 使用 `CE-PIPE-*` 稳定问题编号登记失败反例、正对照和必需测试。
- 新运行输出进入 `private/` 新目录；不包含密钥，不启动外部服务。
- 完成前执行新增模块、双审计、材料治理及完整 P2 守卫，并保存独立回执。

## 6. 完成口径

v0.6.3 的“工程完成”仅指持久化编排、版本化回修及统计计算通过虚构端到端与反例回归。真实开发运行、实际人员回收、统计合格、D01—D11 临床冻结、试评和真实医生发放均为独立后续工作。
