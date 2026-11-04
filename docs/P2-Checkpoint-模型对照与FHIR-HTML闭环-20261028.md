# Checkpoint / Episode：固定模型验证与FHIR／HTML接口闭环

2026-10-28，承接v0.6工程修复后的下一阶段。本轮已补齐DeepSeek技术运行，将实际患者重建对象接入标准原文分割待核接口，并以虚构资料跑通医生FHIR包、单文件HTML、多选意见导出及Python/FHIR回收。**没有形成或发放真实患者的正式盲评包，没有建立医生Reference。**

## 1. 已完成什么，证据到哪一层

|工作|已完成结果|不能由此推导的结论|
|---|---|---|
|固定DeepSeek技术补全|SCALE-03/04/10补齐，固定十例10/10技术完成；原模型、温度0、原始来源及实际请求hash均留档|十例为既有开发抽样，不能代表全部P0或最终研究队列；技术完成不证明语义正确|
|同模型一次报错反馈|预登记2个请求组、3个事件；证据池3/3不变，修订后均保留不确定，新增候选Checkpoint为0|撤回错误提案也是合法结果，不能把引文报错消失等同于临床判断正确|
|实际重建对象进入分割接口|3个真实研究者示例生成SourceManifest、SplitProposal、CompiledPartition及逐来源审计；全部机械区间完整互斥|仍有343段REVIEW，实际KEEP许可为0，独立材料语义审核尚未完成|
|实际研究图进入FHIR接口|Qwen、DeepSeek各一份SCALE-09研究图转换为Basic及明确的本地Reference；官方R4 JSON Schema与引用核验通过|这些资源表达程序研究对象及关系，不是临床已确认的Episode或医生Reference|
|医生阅读与意见回收|许可包→医生FHIR Bundle→单文件HTML→实际下载的ReviewSubmission／QuestionnaireResponse→Python问卷校验→最小响应FHIR Bundle往返通过|界面使用虚构资料与演示选项；真实临床Mode／动作字典没有被代码擅自冻结|

整合回归为 **651项通过，423项子测试通过**；18份问题回归清单的367个必需测试引用均可找到。基线保护的1690份旧产物hash全部未改变，测试运行期间代码hash稳定。未修改P1代码、解析规则、来源核对或纳入条件。

## 2. 固定模型实验的具体发现

复制并按实际请求身份复用既有588个成功缓存，先做零网络预检，再补齐缺失请求。发现阶段补齐后，复核请求中的既往事件目录也会改变，所以新请求数不能只按最初25个缓存缺口估算。

本轮实际共 **71次新的逻辑模型调用、79次传输尝试**：技术补全69次调用／77次尝试，反馈对照2次调用／2次尝试。69次调用成功、2次失败；失败回执均保留。成功返回的模型标识均为`deepseek-v4.1-flash`。这是服务端返回的标识记录，不另行证明服务端内部权重版本。

当前固定十例主开发结果为 **162个候选Checkpoint、87个候选Episode**。旧七例保留LLM语义提案，仅执行新增最终依赖审计；新三例补齐技术缺失。主结果未混入反馈实验，也没有启用支持文书扩展或零发现复查的准确性对照，不能据候选数量变化宣称新版准确性提高。

两组反馈的情况更具体地说明了接口的作用：

- SCALE-01的一事件，当前信息比较证明从0个有效／2条无效校验记录，变为3个有效／0条无效；LLM同时把`new_evidence`改为`uncertain`、把信息演化改为`repeated`。
- SCALE-02的两个`execution`对象撤回与之冲突的`first_anchor`提案，改为`uncertain`并撤去相应观察引文。它们通过撤回提案消除了矛盾，不能写成“引用全部被正确修复”。

另发现并修复CE-EXP-003：旧七例派生汇总遗漏了最终状态分布重算，SCALE-06/09的候选总数正确但状态直方图过期。当前主入口为 `private/p2_checkpoint_next_20261028/model_validation/fixed_ten_development_release_v2/release_receipt.json`。v2逐例从最终事件、Episode和关系重算并核对全部可派生计数，162／87不变；v1及冻结回执完整保留，修正没有新增模型调用。

完整模型实验说明位于 `private/p2_checkpoint_next_20261028/model_validation/固定模型补全与反馈对照报告-20261028.md`，v2差异说明位于其`fixed_ten_development_release_v2/发布说明-20261028.md`。

## 3. 患者重建到医生意见的标准接口

|结点与目的|处理对象|谁处理|上游→下游|标准接口／当前状态|
|---|---|---|---|---|
|准备材料审阅，保持来源可回溯|已复核事件、事件文书及实际模型证据池中的原始来源|程序；不追加LLM调用|event＋sources→来源manifest、分割建议、研究者审计|`p2_checkpoint_materials.prepare_material_review`；已实现|
|执行原文分割并机械核验|整份不变原文及目标Checkpoint绑定的区段提案|程序|SourceManifest＋SplitProposal→CompiledPartition|hash／逐字定位／Unicode code point区间；空洞、冲突、未知保留REVIEW；已实现|
|审定当时可见性与遮蔽语义|MASK／KEEP候选、跨文书决策回述、来源覆盖|独立材料审核者|特定compiled hash→SemanticAudit|schema已实现；真实示例仍`NOT_ASSESSED`，不能自动生成PASS|
|冻结当前医生许可内容|审定分区及外部冻结表单|程序核对绑定；研究协调者负责真实审核与分配|CompiledPartition＋SemanticAudit＋FrozenQuestionnaire→FrozenCheckpointPacket|机械PASS、无REVIEW、语义审核PASS和固定表单均需满足|
|转换为可交换FHIR资源|当前许可正文、Checkpoint及表单|程序|FrozenCheckpointPacket↔医生FHIR collection Bundle|一个当前Checkpoint Basic、许可DocumentReference、Questionnaire；已实现并核验|
|医生阅读和独立作答|当前许可原文及冻结问卷|医生；HTML负责呈现和本地校验|医生FHIR Bundle→单文件HTML→ReviewSubmission内的QuestionnaireResponse|多选、条件题、必填、类型、代码和版本绑定；虚构浏览器实测通过|
|回收结构化意见|实际答案及评审分配|程序；协调者核对身份、分配及接触史|ReviewSubmission→Python语义校验↔最小响应FHIR Bundle→台账|已实现回收接口与往返；未冒充完成真实专家意见收集|

原始事实始终来自原文。提取字段、程序分类或LLM判断不替换原文；医生接口不预填模型动作，也不把程序候选当作专家可接受动作集合。

三个真实待核示例中，DeepSeek和Qwen对同一术中文书分别提出99和792字符的MASK建议；这项差异尚未审定，不能按建议长短判断哪个模型更准确。观察引文即使满足原程序的“显式此前已知”条件，也只登记为KEEP候选提示，实际分区仍是REVIEW。材料审阅者需要解决343段REVIEW，并核对纳入范围之外的来源是否包含必要资料或同次决策回述。

研究者待核包保存整份原文，留在`private/p2_checkpoint_next_20261028/material_proposals/`；它不能直接进入医生HTML。当前材料范围只包含事件文书和原实际模型证据来源的并集，不宣称覆盖了全患者所有可用资料。

## 4. FHIR资源和HTML究竟实现到了什么程度

实际研究图采用研究专用Basic及typed Reference，保留`same_event`、`same_management_process`等原始关系类型及状态；没有因`previous_event_id`存在就凭空制造已支持的比较关系。研究者图与医生资料入口隔离，医生解码器会拒绝研究图。

医生包中只放当前Checkpoint及许可正文，DocumentReference附件内联UTF-8文字；不携带原文外链、被遮内容、未来Checkpoint或研究者全量对象。HTML生成器先经Python合同与完整R4 JSON Schema预验证，再嵌入已分配的许可包。页面按文本呈现原文中的HTML样文字，条件关闭时清空旧答案，无预填或静默上传。实际Chrome验收验证了多选、条件必填、恢复未作答、移动端排版、下载以及零HTTP请求；下载意见再经独立Python接收器和响应FHIR往返验证。

Python／JavaScript另外完成325个固定虚构差分案例，78个接受、247个拒绝，接受／拒绝结果一致。测试修复了BOM原文解码、整数JSON词法、Unicode空白、空URI后缀和时间偏移的具体互操作问题。

FHIR接口附带24个`StructureDefinition(status=draft)`及本地合同。当前已验证实际FHIR JSON结构、引用与本地规则；尚未完成HL7 profile snapshot、完整FHIRPath及术语验证，不能称为已发布的FHIR研究规范。hash保证内容与版本绑定，不认证医生身份或独立审核的真实性。

规范依据采用固定R4版本：[Bundle](https://hl7.org/fhir/R4/bundle.html)、[Basic](https://hl7.org/fhir/R4/basic.html)、[DocumentReference](https://hl7.org/fhir/R4/documentreference.html)、[Questionnaire](https://hl7.org/fhir/R4/questionnaire.html)与[QuestionnaireResponse](https://hl7.org/fhir/R4/questionnaireresponse.html)。项目支持的是明确受限的问卷子集；未支持的特性会拒绝，不能静默忽略。

## 5. 可以现在查看的产物与下一道验收

- 可操作的单文件演示：`private/p2_checkpoint_next_20261028/app_validation/reviewer-demo.html`。资料及Mode／动作选项均为虚构，页面有明确演示标识。
- 浏览器和回收证据：同目录`browser_receipt.json`、`browser_receiver_receipt.json`、实际下载的`browser-submission.json`及`browser-response-bundle.synthetic.json`。
- 实际研究图FHIR及字段投影：`private/p2_checkpoint_next_20261028/fhir_graph_validation/`；只含研究结构索引，未复制原文。
- 完整回归、代码diff与旧产物保护：`private/p2_checkpoint_next_20261028/validation_final/`。

下一道研究验收是：明确并冻结临床评审任务、可接受Mode／动作字典和资料可见规则；对上述待核原文区段及跨文书回述完成独立审核；随后生成一批真正审定、分配到医生的许可包，做小规模试评并验证任务可理解性、选项覆盖及回收流程。支持文书扩展、零发现复查的独立召回／误报验证仍未完成。现有代码不会替代这几项研究判断，也不会据候选数或自动机械PASS发放患者材料。
