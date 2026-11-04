# P2 Checkpoint v0.6.1：自动化材料治理实施回执

2026-10-29。状态：**A—C 工程实现及虚构整合回归已完成；真实开发运行、人工抽样验收和医生盲评均未完成。**

## 已实现的独立层

新模块：`scripts/cohort_construction/p2_checkpoint_material_governance/`。它读取既有 `SourceManifest／CompiledPartition`，不改写 v0.6.0 候选事件、关系或 Episode。

- `review.py`：以同一 `compiled_sha256` 提供研究者完整原文、候选／排除来源及医生视图；`MaterialReview` 含七维结论、发现、操作、明确状态和后续动作。`SET_DISPOSITION` 由逐字引文定位，程序分离条件、否定等边界，支持 `SPLIT_SPAN` 的执行语义。来源变化生成新 manifest 和分区，新纳入原文默认 REVIEW。支持有界复审、无实质修订拒绝、内容回摆和预算停止。
- `compiler.py` 新增共用 `project_doctor_materials()`：MASK 和 REVIEW 均以中性占位隐藏，自动预览与最终许可包使用同一投影；真实许可仍要求独立人工 `SemanticAudit`，自动 PASS 不可绕过。
- `episode.py`：按候选 Episode 绑定独立 Checkpoint 版本、审核链、医生视图及跨 Checkpoint 检查项；`FLAG_EVENT_GRAPH_ISSUE` 通过重建交接对象返回，不能在材料层修改图。
- `workbench.py`：自动审核意见、操作历史和新旧版本对照作为独立文件及研究者 HTML 中的只读面板展示；Python 和浏览器均核验任务及编译对象绑定。人工草稿仍 PENDING、无独立性声明，不替人点击通过。
- `sampling.py`：预注册分层抽样、整数 seed、明确样本数与缺陷上限、实际回收分母及 Wilson 区间；缺回收不当作成功，也不自动生成临床许可。
- `run.py`：默认只生成私有请求；只有显式 `--execute` 且配置 `remote_execution_authorized=true` 才允许调用。材料审核请求通过独立 system message 发送，不复用发现／复核提示。可传入已核验的排除来源及含时点不确定性的任务上下文；每轮均重建请求并绑定 hash。默认配置不授权，未运行真实患者模型。

## A—C 工程验收范围

- **A**：独立材料审核、修订计划、Episode 材料集和医生视图 schema；稳定问题编号 `CE-AMG-001—007`；旧产物保护。
- **B**：完整研究者原文＋实际医生视图、逐字引文修订、来源重编、版本绑定、复审预算、HOLD 和人工升级；自动状态与人工／临床许可分离。
- **C**：候选 Episode／Checkpoint 材料集绑定和图问题交接；同一来源跨 Checkpoint 的逐项复核义务，不把来源重叠直接判定为未来泄漏；研究者 HTML 的自动意见、操作账本和新旧对照；虚构 FHIR／响应回收整合。医生许可仍沿用既有独立人工 `SemanticAudit` 门槛。

这里的“跨 Checkpoint 检查”是自动材料审核输入中的语义复核及程序生成的重叠来源复核项，**不是**已由程序认证的临床时间顺序或零泄漏结论。真实病例的批量 Episode 组织及自动运行仍属下一阶段。

## 已验证的虚构问题

`CE-AMG-001—007` 登记完整视图／REVIEW 隐藏、边界与否定保留、重复引文、来源越权和旧审核复用、自动与人工状态混淆、无变化／回摆／预算停止、图问题回路、独立 system message、跨 Checkpoint 来源重叠、抽样及浏览器人工独立性。

最终整合回归结果：**755 项测试、449 项子测试通过**；21 份问题清单、486 个必需测试引用可解析；1,690 份历史保护产物 hash 不变；运行期间源文件 hash 未变化。回执：`private/p2_checkpoint_next_20261028/validation_material_governance_v061_final_r2_20261029/validation_receipt.json`，同目录保存代码 diff 和测试日志。

其中包含虚构材料审核模型请求的 mock transport 验证、真实 Chrome 自动意见展示及独立人工导出、Python 人工回收、FHIR 医生包／评审响应整合。浏览器中的自动意见不预填人员身份、独立性声明或审核结论；FHIR 测试使用既有虚构独立人工审核正对照，不是自动 PASS 直接放行。前一次 754 项测试通过的回执因运行中源文件变化未记为 PASS，保留不覆盖。

默认工程配置 `max_material_review_rounds=3` 只是有界工程上限，**不是**经开发实验确定的最佳轮数或统计合格率。抽样阈值须由研究团队预注册，当前代码没有擅自给出合格率门槛。

## 尚未完成

1. 真实小批开发运行及固定模型／提示／协议／代码 hash 的操作分布和成本；
2. 独立人员对最终完整 Checkpoint 材料包的分层抽样评价；尚未回收，质量指标为 `not_measured`；
3. 真实病例批量 Episode 组织的实际运行及临床时点／未来泄漏的语义验收；现有自动检查不能代替临床可见性判定；
4. D01—D11 临床方案、表单、分配、试评与 Reference。

本次没有调用临床模型、处理新患者资料、发放医生包、改变 P1 或提交／推送 Git。旧输出、模型提案和人工任务继续保留。整合回归中的模型调用为 mock，未访问外部服务；`p1_files_modified=false`。

## v0.6.1 装配收尾补充回执（2026-10-29）

本章节只补充既有回执，不改写上文已冻结的整合回归结论及历史回执计数。

### 真实候选图结构的虚构装配验证

A—C 工程及**真实候选图结构**的装配已通过虚构整合测试：测试使用结构符合既有候选图输出的虚构事件、关系、Episode、发现单元和完整来源，验证全部目标装配、证据预算遗漏来源保留、缺源局部 HOLD、零发现及未入选单位覆盖、多轮审核、工作台和局部失败续存。这里的“真实候选图结构”仅指接口和图结构来自实际实现约定，**不是**已运行真实患者资料，也不是临床时点、Episode 划分或材料语义已经验收。

补充登记稳定问题 `CE-AMG-021—024`，分别覆盖严格共享未缓存模型调用预算、只处理剩余目标的 sibling 断点续跑、父链权威 replay／合同拒绝、逐轮 journal 与输入／实现快照。各问题均绑定当前已存在的测试节点；本次未新增或修改测试及实现。

### 本次完整模块测试回执

在仓库根目录执行：

```bash
python3 -m pytest scripts/cohort_construction/p2_checkpoint_material_governance/tests -q
```

实际结果：**96 passed in 6.82s**。该结果是 `p2_checkpoint_material_governance/tests` 当前完整套件结果，包含装配、断点续跑、浏览器和 remediation 测试；不是上文历史全仓整合回归的替代回执。

另以 unittest 发现方式核对兼容性：

```bash
python3 -m unittest discover -s scripts/cohort_construction/p2_checkpoint_material_governance/tests -v
```

实际结果：**Ran 16 tests in 0.049s，OK**。unittest 只收集其支持的类测试，不作为本模块完整测试总数；完整结果以上述 pytest 为准。测试均使用虚构材料和受控／mock 适配器，不代表真实患者运行。

### 收尾边界

- **已完成**：A—C 工程实现及上述真实候选图结构的虚构整合装配验证；本次限定模块回归通过。
- **未完成**：D/E 后续工程与验收；真实患者资料上的开发或批量运行；独立人员抽样、统计验收和人工语义验收；医生盲评、Reference 建立及任何临床发放。
- 自动材料状态、测试通过和候选 Episode 材料集均不构成临床许可。本次没有新增真实模型调用、患者结局分析或医生包，不报告患者人数或临床合格率；相关真实质量指标仍为 `not_measured`。
