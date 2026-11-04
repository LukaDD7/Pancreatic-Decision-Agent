# v0.6 独立原文验证与材料审核工作台：实施回执

2026-10-29，承接当天进度核对与用户“继续推进”的指令。本轮将待执行的原文验证和材料审核落实为离线工具及真实开发材料任务，未代替实际人员作出审核结论。

## 1. 本轮交付

|交付|已实现并执行|当前状态|
|---|---|---|
|研究者材料审核应用|单文件HTML逐段填写、逐来源检索、覆盖/时间证据/六项检查/问题记录、导出与恢复；Python独立回收校验|三份真实研究者任务已生成；353区间仍未决|
|原FHIR补入来源正文|先核对原准备回执中的FHIR文件字节hash，再导入并核验原文；不把正文载入等同于完成阅读|三任务分别载入688、688、1261份来源，未载正文均为0|
|独立原文验证接口|prepare→submit→reveal→compare→summarize，首轮原文重建先封存，之后才另行开放模型提案|四类开发抽查各2项，共8项，实际人工回收0|
|临床冻结清单|核对既有三Mode/20动作及历史Q1—Q5转述，列出D01—D11的具体待决事项和影响接口|全部待临床团队审定，未自行冻结|
|来源范围一致性修复|CE-PKT-010：PASS不能要求与当前纳入/排除范围相反的处置后继续冻结旧分区|Python、JavaScript均实现；PENDING/FAIL保留修订提案|

代码位于：

- `scripts/cohort_construction/p2_checkpoint_material_review_app/`。
- `scripts/cohort_construction/p2_auto_checkpoint_v06/independent_validation/`。
- `scripts/cohort_construction/p2_checkpoint_packet/semantic_audit.py`及对应测试/问题清单。
- `scripts/cohort_construction/p2_checkpoint_review_app/verify.py`接入新模块并继承既有回归。

## 2. 实际人员可以使用的材料审核入口

协调者入口：`private/p2_checkpoint_next_20261028/researcher_material_review_v01_20261029/index.html`。

|任务目录|原compiled区间|原manifest纳入来源|界面已载正文|未载正文|
|---|---:|---:|---:|---:|
|deepseek_SCALE-01_operative|174|164|688|0|
|qwen_SCALE-01_same_source|174|164|688|0|
|deepseek_SCALE-04_small_scope|5|1|1261|0|

两份术中任务指向同一患者来源，不是两个独立患者样本。每份新目录包含`researcher-review.html`、`researcher-assignment.json`、`build-receipt.json`。补入正文是为审核者实际查看原目录中排除来源提供条件，**并未扩大当前compiled纳入范围，未把来源改成已审状态，也未改变患者或候选Episode成员**。

界面初始身份留空、独立性不勾选、检查项不勾选、状态PENDING，所有区间和来源覆盖保持UNRESOLVED。实际审核者逐项填写后导出响应；导出文件含原文，须保留在项目private。浏览器不静默保存或上传，关闭前须主动导出。

协调者回收示例（替换实际响应和新的输出文件名）：

```sh
/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_checkpoint_material_review_app.receive \
  --assignment private/p2_checkpoint_next_20261028/researcher_material_review_v01_20261029/deepseek_SCALE-01_operative/researcher-assignment.json \
  --response private/实际审核者导出的响应.json \
  --output private/p2_checkpoint_next_20261028/human_material_audits/新的审核记录.json
```

回收端核对任务、来源、compiled、原草稿和响应hash，拒绝沿用AI草稿身份或记录编号，不覆盖旧产物。程序验证记录，不能认证实际人员身份、接触史或独立性。

“正文未载入”特指目录中已有交付来源的正文未进入该HTML，不等于医院没有记录或原报告未交付。未载正文不能被目录替代为已审；具体引用原报告缺失等问题须另记问题、依据和来源，不能以软件状态一律排除患者。

本应用含模型提案、实际临床管理措施及未来资料，**不能用于医生临床动作盲评，也不能在独立原文审核者封存首轮判断前交给该审核者**。

## 3. 首批8项独立原文验证任务

批次：`private/p2_checkpoint_next_20261028/independent_validation_v01_20261029/`。

本次在采样前保存开发采样意向、修订理由及整数seed映射，seed为20261029，每层2项。支持来源路由由当前代码重算，十例均与冻结库存的单位成员相符；清单明确标记`reconstructed_routing`，不冒称原运行保存的回执。

|开发抽查层|现有总体规模|选取数|
|---|---:|---:|
|有发现的原文单位|269|2|
|零发现的原文单位|78|2|
|含被程序拒绝提案的原文单位|347|2|
|未进入发现的支持来源|175|2|

这些层的适用单位有重叠，程序按预定顺序去重后取样，再以与分层无关的hash重新排列任务编号。不能把四层总体规模相加作为患者/事件总数，也不能把本批挑战样本汇总成P0准确率。

本次显式采用当前完整单位、代理时钟前后3日及无法解析时钟的来源作为局部检索范围；默认API仍仅当前单位。代理时钟范围不是临床决策时间证据、Episode边界、当时可见性证明或全患者病历完整性。历史比较资料不足时保留UNRESOLVED并申请同范围扩展，不用后续结果改写较早信息。

协调者只向独立原文审核者分发`phase_a_delivery.zip`，不要分发整个批次或`researcher_only/`。ZIP含8个任务的原文/空白表、填写说明和hash清单，不含模型提案、程序状态、抽样分层及另一人结果。8份空白模板均为`origin=ai_preparation`、PENDING、无独立性声明；须由实际人员另行填写。

具体顺序及命令见新模块README：

1. 实际人员从指定范围原文重建管理判断、证据、事件归组和时间依据，使用逐字引文及Unicode code point坐标；协调者用`submit`封存。
2. 已读完整但无法判定目标管理判断者，可提供明确的未决理由/原文依据并完成阅读记录，不被强迫改成阳性或阴性；汇总单列UNRESOLVED。
3. 同批同患者的全部已分配首轮应先封存，再开放任一模型对照；本地已登记模型暴露后，程序拒绝后来声称未暴露的首轮提交。批次外接触史仍需真实分配台账核对。
4. `reveal`只接受封存的实际人员首轮，AI准备不能触发；开放后分别呈现发现阶段LLM输出、复核阶段LLM输出和程序结果，不覆盖首轮。
5. `compare`回收六个维度的明确评价、理由与原文依据；`summarize`报告人工提交分母及未决/范围外结论，不自动裁定或输出准确率。

初始汇总为8项待审、实际独立原文回收0、模型对照回收0；语义准确性及总体召回均`not_measured`。该批为既有开发十例中的开发验证准备，不是新锁定的独立外部验证队列。

## 4. CE-PKT-010的修复范围

旧实现允许审核PASS同时把当前排除来源标成`REVIEWED_INCLUDED`，或把compiled纳入来源标成`REVIEWED_EXCLUDED`，而冻结函数仍按旧范围生成正文。两种错误已用虚构反例复现。

新版在PASS时拒绝这两类明确相反的范围处置；改范围的判断可保留在PENDING/FAIL，但须生成新manifest/分区并审核新hash。它不自动改变模型证据池、候选Episode或患者成员，也不统一拒绝`REQUIRED_MISSING`/`DELIVERY_NOT_FOUND`。测试覆盖失败反例、非PASS保存、缺口对照及显式新范围重新审核的合法路径；浏览器使用同一拒绝码。

## 5. 验证和版本保留

- 完整继承回归：**738项测试、449项子测试通过**。
- **20份问题清单的468个必需测试引用全部可解析**，新模块已有覆盖纳入整合运行。
- **1,690份历史保护产物hash不变**；额外核对本轮基线中的72个既有材料/任务/回执文件，hash不变。
- 新增21个源码/资产/测试/说明文件；既有源码仅修改packet审核器、其测试、问题清单及整合验证入口，共4个文件。精确diff、修改原因与旧实现失败反例均保留。
- 三份真实研究者assignment经Python/JavaScript内容绑定核验；8项Phase A仅含规定原文和空白表。其他Agent的只读工程复核不计为独立人工材料审核或研究验证。
- 实际Chrome使用虚构资料完成导出、Python回收、错误hash拒绝、未载正文、范围冲突、原文HTML样文字不执行和桌面/移动端检查；0 HTTP请求。真实生成阶段另以socket禁止机制核对，socket调用0、临床模型调用0。
- 未修改P1、患者纳入门槛、冻结模型提案或既有结果，未提交或推送Git。

回执入口：

- `private/p2_checkpoint_next_20261028/validation_workflow_release_20261029/validation_receipt.json`：完整测试、代码hash及历史保护。
- `private/p2_checkpoint_next_20261028/workflow_implementation_20261029/implementation_receipt.json`：本轮改动分类和追加保护。
- 同目录`generation_receipt.json`、`delivery_packaging_receipt.json`、`workflow_changes.diff`和`scope_gate_fix_evidence/`。
- `private/p2_checkpoint_next_20261028/material_review_app_synthetic_browser_release_20261029/`：仅虚构资料的浏览器回执和截图。

## 6. 仍需完成的验收

实际人员仍需完成独立原文验证、353区间及来源覆盖的材料审核；临床团队仍需审定`docs/P2-Checkpoint-v0.6-临床冻结决策清单-20261029.md`的D01—D11。随后依真实审核结论修订、重编和重审，满足冻结表单与许可条件后才创建真实医生包，开展试评及正式盲评。

本轮完成的是可执行工具和待填写材料；未产生真实人工审核PASS、正式医生许可包、Acceptable Mode Set或临床Reference，尚不能声称v0.6整体研究验收完成或临床性能改善。
