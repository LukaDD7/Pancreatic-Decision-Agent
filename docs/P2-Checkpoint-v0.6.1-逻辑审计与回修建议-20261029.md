# v0.6.1 自动化材料治理：逻辑审计与回修建议

2026-10-29。按用户要求审计当前计划、实现和最新回执。**方向可以继续，但目前不能按“A—C 已完整工程验收”进入真实患者开发运行。** 本轮登记8组已复现缺陷，以及3项应单独说明的衔接/统计设计缺口；没有修改生产源码、调用临床模型、提交真实人工意见或发放医生包。

`V061-AUD-*` 是本次稳定审计编号，均为开放待回修项，不冒充已实现的生产回归编号。修复时须将相应反例与正对照迁移到当前整合回归，不能只运行本审计旧快照。

## 1. 审计对象及证据边界

- 计划：`docs/P2-Checkpoint-v0.6.1-自动化材料审核与抽样验证实施计划-20261029.md`。
- 实施回执：`docs/P2-Checkpoint-v0.6.1-自动化材料治理实施回执-20261029.md`。
- 当前源码与`validation_material_governance_v061_final_r2_20261029/validation_receipt.json`的源码hash一致；该回执确实记载755项测试、449项子测试、21份清单及486个可解析必需引用。
- **上述数字不等于5项运行整合测试实际执行。** 默认pytest未收集该测试类；强制收集后4失败、1通过。四个失败包含一个真实CLI缺陷和三处测试自身的接口/fixture问题，不解释成四个独立产品缺陷。
- 全部失败反例使用虚构原文和虚构人工标识。其他Agent完成的是只读工程复核，不是独立人工材料审核或临床Reference。

代码定位对应本次冻结hash，复现脚本及结果保存在：

`private/p2_checkpoint_next_20261028/audit_material_governance_v061_20261029/`。

## 2. 已复现缺陷

P1/P2表示工程回修优先级，不表示临床错误严重度或患者人数。

### V061-AUD-001 · P1 · 默认CLI入口无法运行

定位：`scripts/cohort_construction/p2_checkpoint_material_governance/run.py:31–46`。

`argparse`没有登记`--candidate-sources`，但第46行无条件访问`args.candidate_sources`。使用合法虚构manifest/compiled调用默认准备入口，得到`AttributeError: 'Namespace' object has no attribute 'candidate_sources'`，请求输出目录尚未创建。带`--execute`也要先经过同一错误。

回修：补齐参数及输入契约；默认离线准备、候选来源、排除来源和显式执行各自做入口测试，不能只调用内部函数。

证据：`governance/governance_audit_results.json`的`default_cli_missing_candidate_sources`；脚本`cli_missing_argument()`。

### V061-AUD-002 · P1 · 必需整合测试未收集，类路径校验仍可误通过

定位：`p2_checkpoint_material_governance/tests/test_runtime_integration.py:29`；`p2_checkpoint_review_app/verify.py:43–58`。

普通类`MaterialRuntimeTests`既不以`Test`开头，也不继承`unittest.TestCase`，回执使用的pytest配置不收集其5个方法。

```text
默认运行该测试文件：no tests ran，exit 5
强制 python_classes=MaterialRuntimeTests：4 failed，1 passed
```

强制执行暴露：CLI缺参数；任务上下文fixture的请求hash不匹配；工作台fixture试图patch不存在的`PRIVATE_ROOT`；FHIR响应fixture漏传packet参数。因此现有回执不能证明这些整合行为已验证。

此外，必需引用解析器找到模块后仅查最后的方法名，忽略中间类路径。临时fixture仅有`ActualClass.test_required`，清单写成不存在的`MissingClass.test_required`仍被判PASS。AST存在不等于pytest实际收集或执行。

回修：用pytest实际收集的node ID核验模块、类和方法；缺失收集必须失败。修好fixture后，确认5项均进入完整整合运行，保留错误类名和未收集类的反例。

证据：`governance/runtime_default_collection.log`、`runtime_forced_collection.log`；`sampling/sampling_audit_results.json`中的`AUDIT-V061-REGISTRY-001`。

### V061-AUD-003 · P1 · 模型被要求填写未提供给它的请求摘要

定位：`p2_checkpoint_material_governance/review.py:120–133`、`:390–406`；`prompts.py:14–18`；`p2_auto_checkpoint_v04/client.py:71–75`。

响应必须满足`request_sha256 == canonical_sha256(request)`，但请求正文及发送包装均未提供这个预期摘要。捕获实际发送结构的虚构测试确认摘要不在模型输入中。现有`review_for()`测试助手在Python中计算正确摘要，掩盖了真实LLM需要自行完成规范JSON序列化及SHA256运算的接口问题。

回修：由程序生成可复制、无自引用的绑定信封，或由程序将模型的语义响应绑定到本次请求。须区分材料payload摘要、实际传输请求摘要和响应摘要；不要依赖模型计算hash。测试应从实际发送包装构造响应，而非从隐藏的Python对象生成正确字段。

这是接口缺陷的静态/发送结构证据，不是一次真实患者模型失败率测量。

证据：`governance/governance_audit_results.json`的`model_input_does_not_supply_required_payload_hash`。

### V061-AUD-004 · P1 · 两份审核视图可使用不同原文版本并自动PASS

定位：`p2_checkpoint_material_governance/review.py:72–77`、`:113–116`。

请求只核验`manifest_id`和source ID集合，不逐来源核验version/hash/正文与compiled一致。反例：同一manifest ID和source ID，manifest放v2原文、compiled仍为v1；研究者看到v2，医生投影来自v1，构建请求和`AUTO_REVIEW_PASS`均成功，`changed=false`。

回修：在构建任何请求前逐来源核验版本、原文hash、完整切片重构和manifest/compiled绑定。不能只验证两对象各自内部有效。

证据：`governance/governance_audit_results.json`的`mismatched_original_and_doctor_source_accepted`。

### V061-AUD-005 · P2 · 刚被排除的原始来源从下一轮审核消失

定位：`p2_checkpoint_material_governance/review.py:335–354`。

初始纳入doc1/doc2，第一轮排除doc2；第二轮只有doc1，`candidate_sources`和`excluded_sources`均为空，仍可自动PASS。程序只过滤初始外部集合，没有将从原manifest排除的来源保留到链级来源注册表。下一版LLM因此无法重新核对被删资料是否包含必要证据。

回修：保留贯穿整条链的完整来源注册表、原文身份和每版范围处置；新排除的原始来源进入下一轮排除视图。来源排除不是临床不相关或患者不可用的认定。

证据：`governance/governance_audit_results.json`的`excluded_original_disappears_next_review`。

### V061-AUD-006 · P2 · HOLD终止时链尾与已执行账本不一致

定位：`p2_checkpoint_material_governance/review.py:360–371`。

同轮有`SET_DISPOSITION`和`REQUEST_HUMAN_REVIEW`时，程序已生成新版、将操作记为APPLIED，却在更新`current_manifest/current`前break。反例中末轮`changed=true`，最终chain的manifest/compiled/doctor_view仍是初版，与末轮输出不同。

回修：明确“采用新版后HOLD”或“旧版仍为当前，修订未提交”的语义。终止对象、操作状态、版本对照和工作台正文必须一致，不能把未采用结果记录为已采用。

证据：`episode/episode_audit_results.json`的`AUDIT-EP-001`。

### V061-AUD-007 · P2 · Episode和工作台未验证整条审核链

定位：`p2_checkpoint_material_governance/episode.py:26–34`；`workbench.py:58–65`；`workbench.js:8–12`。

两种独立反例均通过：

- 仅构造`state=AUTO_REVIEW_PASS`、空rounds和匹配的链尾hash，缺少chain.manifest，Episode仍接受含REVIEW区间的材料为自动PASS。
- 保持最终manifest/compiled不变，改写历史轮次输入hash、审核目标为其他Checkpoint并清空账本，工作台核验仍返回True；浏览器采用同样的链尾核验范围。

回修：建立可复用的审核链验证器，逐轮核验输入→请求→审核→操作→重编输出、相邻版本连续性、终止状态和最后PASS。Episode与Python/JavaScript工作台共用该规则，并将链/账本hash绑定到可信构建回执，而非只比较链尾。

这些反例破坏研究侧自动状态和审计证据，**没有直接绕过医生人工许可门禁**。

证据：`episode/episode_audit_results.json`的`AUDIT-EP-002/003`。

### V061-AUD-008 · P2 · 回收可违背冻结分层配额却标记验收完成

定位：`p2_checkpoint_material_governance/sampling.py:83–95`、`:116–128`。

冻结计划规定术中1份、支持来源1份；调用汇总时改为术中2份、支持来源0份。两份虚构人工响应均ACCEPT，汇总仍返回`meets_frozen_count_thresholds=true`、`statistical_validation_complete=true`。代码只校总数及层名称，不校每层配额，也未绑定封存的随机分配清单。

回修：冻结实际总体及抽样分配的ID、版本、hash、层别和抽样配置；汇总必须验证逐包、逐层与该清单一致。完成回收和按设计完成验证须分开，不能仅以总数相等代替。

证据：`sampling/sampling_audit_results.json`的`AUDIT-V061-SAMPLING-001`。

## 3. 应与确定缺陷分开的三项验收缺口

### GAP-01 · 实际修订到新版工作台的适配尚未完成

`workbench.py:27–29`需要新版manifest、compiled、草稿和准备回执。合法材料修订后，虽然生成了新草稿，沿用旧准备回执仍正确报`MRA_PREPARATION_COMPILED_MISMATCH`。当前没有从版本化来源/操作账本派生新版准备回执的受支持适配器，浏览器测试只使用未改变材料的HOLD。

拒绝旧回执是正确行为；缺口是“实际修订→新版准备回执/范围→新草稿→工作台→人工回收”未被贯通和验收。不能只把旧回执的hash改成新值。

证据：`episode/episode_audit_results.json`的`AUDIT-EP-GAP-001`。

### GAP-02 · 改图后不得沿用旧PASS主要依赖调用者约定

`episode.py:13–15`仅接收非空版本字符串，不加载既有候选图；`event_reference`可缺省。同一合法材料PASS可以重新挂到不同Episode/事件引用。`graph_revision_handoff()`给出required steps清单，没有程序执行和重启绑定。

回修时应将目标事件、候选图版本、成员关系及任务时点上下文纳入请求/审核身份，并定义重建修订回到材料层的执行入口。不能把这项工程约束缺失解释成已观察到临床时间判断错误。

证据：`episode/episode_audit_results.json`的`AUDIT-EP-GAP-002`。

### GAP-03 · 合并Wilson区间不能直接支持不等比例分层总体外推

`sampling.py:111–126`只计算合并样本的`accepted/n`，没有各层总体量、入样概率、层内回收/缺陷分母、评价单位或患者/Episode聚类处理。

虚构总体：常见层9900份全ACCEPT、罕见层100份全REVISE，各抽50份。总体接受率为99%，当前函数给出的配额样本Wilson区间约为40.38%—59.62%。该区间可描述人为配额样本，不能直接解释为总体接受率区间。

正式D前须冻结估计目标、评价单位、抽样设计、各层分母及设计一致的区间；关键泄漏/遗漏的缺陷率与区间也需单独定义。若只做挑战样本诊断，应明确禁止总体外推。`statistical_validation_complete=true`不能代替这些统计条件。

证据：`sampling/sampling_audit_results.json`的`AUDIT-V061-STATISTICAL-DESIGN-001`。

## 4. 回修次序及最低重新验收

1. 先修CLI和请求绑定，修复并实际收集5项运行测试；必需测试守卫改为验证真实pytest node ID。
2. 加入权威的manifest/compiled双向校验、链级完整来源注册和审核链重放核验，统一HOLD对象与账本语义。
3. 用至少一次真实结构修订的虚构实例跑完整过程：准备请求→审核修订→新manifest/partition→下一版复审→新版准备回执→工作台→人工导出/Python回收。FHIR部分须使用该新版的独立人工fixture，不借用旧包证明新版链已贯通。
4. 冻结实际抽样分配清单并校验逐层配额；在D前明确统计估计对象和区间适用范围。
5. 再对真实开发运行补齐输入精确字节、代码/配置/提示/协议hash和逐轮持久化；模型/引文/操作错误要留下可回溯停止回执，不能只保留成功结束的chain。

每个问题修复须登记对应生产问题编号、失败反例及必要正/负对照，并纳入当前整合回归。审计脚本证明现有缺陷成立，不等于已完成修复或已通过新的验收。

## 5. 仍成立的保护及当前完成口径

- 材料审核确实有独立system prompt，未直接复用发现/复核提示。
- 自动对象仍与独立人工SemanticAudit分离；现有医生冻结入口未被自动PASS直接绕过。
- 共用医生投影的方向正确；其前提是两份输入必须绑定同一原文版本。
- 来源重叠复核项没有被宣称为临床时间顺序或零泄漏证明，这个限制应继续保留。
- 本轮未发现真实患者包误发证据；不得把虚构反例数量改写为临床错误人数。

建议当前状态表述：**v0.6.1的A—C已有实现和部分虚构验证，但存在已复现工程阻断及整合缺口，尚不满足完整工程验收；D/E仍未开展。先回修并重新验收，再另行开展获授权的小批真实开发运行。**

本次保留原计划、原实施回执和755项历史测试结果，未回写或覆盖其冻结状态。
