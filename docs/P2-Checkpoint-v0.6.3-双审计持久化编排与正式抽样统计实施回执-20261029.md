# P2 Checkpoint v0.6.3：双审计持久化编排与正式抽样统计实施回执

2026-10-29。状态：**工程实现及虚构端到端回归完成；真实患者开发运行、正式人工抽样、统计合格、临床协议冻结和真实医生发放未完成。**

本回执区分代码已实现、虚构反例已验证、真实资料待检验和临床未放行。它不是患者队列结果、临床质量结论或发布授权。

## 1. 已实现的持久化 Episode 管线

新增 `scripts/cohort_construction/p2_checkpoint_ready_pipeline/`：

- `persistence.py` 冻结输入合同，使用顺序共享模型预算，并保存仅含相关源代码／schema／配置 hash 的实现快照；快照不含私有临床输入，也不授权远程执行。
- `orchestration.py` 执行图审计→材料装配→逐 Checkpoint 材料审核→Episode 材料集→实际 staging packet/FHIR/HTML→交付审计→Ready Resource。
- 图 PASS、全部目标材料 AUTO_REVIEW_PASS、交付 AUTO_DELIVERY_AUDIT_PASS 是顺序门控。缺目标、HOLD、未执行或预算不足不能产生 Ready PASS。
- 每阶段保存请求、链、journal、预算、材料集、实际交付及 replay receipt。续跑创建新目录，父目录不修改；已完成原始材料链经 `validate_review_chain()` 重放后继承，不要求链对象额外携带不存在的 `chain_sha256`。
- `resume_episode_pipeline()` 累计父运行未缓存调用数；若父运行尚未形成实际交付，调用者须再次提供与冻结合同对应的独立 SemanticAudit factory，不能以自动材料 PASS 代替。
- `delivery.py` 复用生产 packet 编译、FHIR Bundle 与 HTML renderer，只建立 staging 对象；`doctor_assignment_created=false`、`external_release_authorized=false`。
- `cli.py` 只提供离线 `prepare` 与无模型 `replay` 命令，没有远程模型或医生分配命令。虚构 CLI 冒烟结果：`PREPARED_NOT_EXECUTED`、`model_call_performed=false`、`replay_verified=true`。

## 2. 版本化回修与无模型重放

- 图 REVISE 产生 handoff；`consume_graph_revision()` 只接受版本和 hash 均改变的独立新图。新运行重新执行全部图、材料、交付审计。
- 材料图问题从实际轮次的 `review` 和 `ledger` 定位 handoff，不再读取不存在的 `response` 字段。材料层不修改事件图。
- 交付 REVISE 只接受调用者提供的新实际交付对象，要求输入 hash 改变并重审。无显式 revision cycle 时，REVISE 交付链不得被继承为 PASS。
- 子运行保存 `superseded_parent_run`；`replay.py` 用 REVISE 父运行重放 revision receipt，不再把当前 PASS 子运行误作父运行。
- 对继承运行核验 continuation 与父 replay receipt 的绑定。继承实际交付时也保存并核验 packet、Bundle 和 HTML 文件。
- 新实际交付经 revision receipt 绑定，不错误要求与旧 assignment 下的确定性 staging 交付完全相同。

## 3. 可重放正式抽样统计

`p2_checkpoint_delivery_audit/sampling.py` 已执行而非仅声明：

- 冻结单／多 Episode 总体、层权重、聚类单位、阈值、缺失政策和扩容触发；
- 由整数 seed 重现 assignment，并绑定人员响应、声明和带时区时间；
- 按层权重计算缺陷比例点估计；
- 按 patient／episode／checkpoint 计算聚类线性化方差及正态单侧稳健区间；
- 缺回收、未决、REVISE、零方差和自由度不足分别失败关闭或按冻结缺陷政策处理；
- 扩容只生成新计划请求，不改写原 assignment。

`ready.py` 重放 design、plan、responses 与 assessment，不再接受裸 `SAMPLING_QUALIFIED` 或自报结果 hash。多 Episode assessment 不直接驱动单 Episode Ready。当前统计区间不是材料抽样模块的描述性 Wilson 区间。

这些计算及“合格”fixture 均只用虚构对象验证；没有正式人工回收，也没有任何现实统计合格结论。

## 4. 已验证的反例与正对照

新包 9 项整合测试覆盖：

1. 完整 staging 管线、实际文件、journal、实现快照、无模型重放及不发布；
2. 图 REVISE 不创建材料／交付；
3. 共享预算阻断后续交付与 Ready；
4. 部分 Checkpoint 链持久化及新目录续跑，累计预算正确；
5. 新图重启全部审计，旧 PASS 不迁移；
6. 材料图问题形成新图 handoff；
7. 新实际交付版本重审，相同 hash 拒绝，旧 REVISE 不继承；
8. 父合同篡改拒绝；
9. 离线准备零模型调用。

相关包级联合回归：**155 passed in 42.23s**，范围为新管线、双审计、材料治理和 review app。新管线独立回归：**9 passed in 16.32s**。

稳定问题登记于新包 `regression_issue_map.json`：`CE-PIPE-001—004`；统计问题保留 `CE-RDY-003—004`。每项含失败反例、正对照与实际测试引用。

## 5. 完整 P2 守卫回执

执行：

```bash
python3 -m scripts.cohort_construction.p2_checkpoint_review_app.verify \
  --output private/p2_checkpoint_next_20261028/validation_ready_pipeline_v063_final_20261029
```

结果：**PASS；874 项测试、449 项子测试通过**。24 份问题清单、589 个必需测试引用全部实际收集并执行；1,690 份历史保护产物运行前后 hash 不变；运行期间源文件未变化；临床模型调用 0，`clinical_accuracy_measured=false`，`p1_files_modified=false`。

最终回执：

- `private/p2_checkpoint_next_20261028/validation_ready_pipeline_v063_final_20261029/validation_receipt.json`
- SHA-256：`bf4d9941e5ed0d439f283a0f7ba15a611858d50d6ad16da97d0afaff74bacd6c`

早期 `validation_ready_pipeline_v063_20261029` 因交付回修测试引用未解析而在 PRECHECK 失败；该目录和失败回执保留，未覆盖。随后恢复独立测试函数并在新目录通过完整守卫。

## 6. 注册与尚未完成

- 新增 `config/p2_checkpoint_dual_audit_v063.json`，默认 `remote_execution_authorized=false`，医生分配与临床发布均为 false。
- `config/p2_checkpoint_versions.json` 保留 v0.6.0—v0.6.2，新增 v0.6.3 指针。
- `p2_checkpoint_review_app.verify` 已纳入新包的 source hashing、测试收集及 issue-map 必需测试执行。
- 未启动真实患者远程模型、正式人工抽样或真实医生分配；未替临床团队填写 D01—D11；未修改 P1；未提交或推送 Git。

因此可以宣称 **v0.6.3 持久化编排、版本化回修和可重放统计工程已通过虚构整合验证**。真实模型质量、真实人员回收、统计合格、临床 Reference 与真实发放仍须独立后续工作和明确授权。
