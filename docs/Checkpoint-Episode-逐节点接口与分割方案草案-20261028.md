# Checkpoint–Episode：处理职责、逐节点接口与分割方案草案

同日后续澄清：交流术语、当前关系集合、节点03—05字段以及面向HTML盲评应用的要求，以[《明确术语、事件接口与HTML盲评应用要求》](Checkpoint-Episode-术语-事件接口与HTML盲评应用-20261028.md)为准。本文件保留先前接口草案及演进记录。

2026-10-28。当前实现说明与建议接口分开记录。本文不改变源资料、研究范围、纳入规则、episode规则、模型运行或正式盲评方案；新对象和操作名称均为本研究草案，不是已发布的 FHIR 标准。

## 1. 两个“轮”不是一回事

**模型处理轮次**：第一轮 discovery 找事件；第二轮 review 核验事件及关系。

**临床评估轮次**：此前某次管理判断与当前某次判断。例如昨天拟手术，今天有新情况而暂缓。`previous_event_id` 指这类此前临床事件，不是上一次模型调用。

第一原则判断该管理是否关联 PDAC 可切除性/根治路径；第二原则判断相对合适的此前管理判断，是否有决策相关的信息演化。首个可见安排可保留为起始锚点，不虚构两轮比较；第二原则未定也不等于患者无关。

## 2. Discovery 到“逐事件组织资料”实际发生什么

职责是 **LLM 提议语义角色与边界 → 程序定位并组织候选资料 → review LLM 选择和比较证据 → 程序核验**。

1. 程序建立 `unit`：普通临床文书整篇一个单位；同次影像检查的报告版本可合为一个单位。
2. Discovery LLM 阅读完整单位，输出零到多个 `events[]`，包括动作、研究范围、事件功能、选择前/后引文、完整选择子句、时间与阶段提案。
3. 程序为每个事件生成稳定 ID；将逐字引文定位到冻结原文，获得 `source_id/source_sha256/start/end/quote`。动作、选择前事实、选择后事实、时间、选择边界分别保存。
4. 程序根据该事件的实际时间证据、记录代理或术中阶段，遍历患者来源，组织累计原始资料。当前混合文书按已核对的选择子句和后续事实做区间运算；术中还保留栏目规则。它输出 `blocks`、`allowed_spans`、`ledger`、`cutoff/cutoff_basis` 及预算遗漏。
5. Review LLM 再判断哪些允许材料是当前选择前观察、哪些相对前轮是新信息、是否与当前判断有明确连接。它填写 `observation_quotes/evidence_comparison/observation_availability/evidence_connection`。
6. 程序核验来源、范围、时序声明与结构一致性。它不能单凭精确匹配证明临床语义正确。

重要：第4步**不是先定前轮，再只取两轮之间的资料**。`build_evidence_context` 虽有 `prior_events` 参数，目前函数内未使用；它先按当前事件的边界组织累计资料。前轮由第二轮 LLM 随后选择，程序再按选定前轮检查 `previous_known_quotes` 的允许来源。

代码定位：`p2_auto_checkpoint_v04/context.py:156–186,433–653`；`engine.py:53–98,217–229,285–381`。

## 3. “同篇”以及三种关系

同篇联合 review 是 **同一 `unit_id` 的 discovery 事件一起复核**。不是整个 episode 一起复核，也不是把同篇自动认作一个事件。

同一篇查房可识别 E1“暂缓手术”和 E2“调整营养支持”，同一次请求含两个事件、当前完整文书和共享来源池；每个事件有自己的 `block_ids/cutoff/eligible_previous_event_ids`。共享传输正文不等于共享使用许可。

| 字段 | 大白话 | 谁确定 |
|---|---|---|
| `previous_event_id` | 判断本轮有无演化，该与哪个此前判断比较？ | 程序筛出可用候选，review LLM 选一个或 null；不是自动选最近一条 |
| `same_process_previous_id` | 与哪个此前事件接成同一持续管理过程？ | review LLM 选择，程序核验连接；可与比较前轮不同 |
| `same_event_with[]` | 哪些记录其实是在重复记同一件事？ | review LLM 提议身份归并，可指同篇其他事件或此前事件 |

例子：术前安排手术与术中探查后确定术式可属于同一管理过程；术后首程再次回述那次术中选择，是同事件提及，不能自动新增临床一轮。当前 `previous_event_id` 本身不直接合并 episode；组装主要使用同过程与同事件连接。

同篇两个事件只有明确的实际选择时序可核验时，才能互为临床前轮；没有具体时刻仍可能判断两段是同事件。程序未发现的事件不能在当前 review 中新建 ID，这也是补召回缺口。

代码定位：`engine.py:175–205,285–314,404–427,470–476`；`prompts.py:37–49,85`。

## 4. 逐节点接口清单

下表每行对应流程图一个主节点及其右侧说明。已有“内部对象/schema”与已发布的互操作接口不是同一成熟度。

| 节点 | 目的 | 处理对象 | 谁处理 | 上游数据 → 下游输出 | 接口现状 |
|---|---|---|---|---|---|
| 来源导入 | 冻结原文与来源身份 | 文书/影像/检验等来源 | 程序 | source-only FHIR + manifest → `sources[]`、导入回执 | 已有 R4 资源及本研究扩展；未发布研究 profile |
| 建立阅读单位 | 指定每次模型看哪份完整资料 | 普通文书/同次影像版本 | 程序 | `sources[]` → `unit[]` | 已有内部字典，无独立版本化schema |
| Discovery | 发现管理事件并标语义角色 | 完整 unit | LLM提案，程序核引文/时间 | unit + 既往背景 → 事件、前后事实、选择子句 | 已有 `DISCOVERY_SCHEMA`，内嵌Python |
| 逐事件候选资料池 | 组织时间上可能可用的原始资料 | 每个事件及全患者来源 | 程序，依赖LLM语义边界提案 | event + sources → `blocks/allowed_spans/ledger/cutoff` | 已有内部对象和范围运算，尚非盲评包 |
| 联合 Review | 判断两原则、前轮、演化和关系 | 同unit多个事件，各自独立许可 | LLM决定语义，程序限定候选 | 全文+ledger+每事件资料池 → `items[]` | 已有 `REVIEW_SCHEMA`；全文仍披露真实选择 |
| 核验和图组装 | 检查证明并组织候选过程 | review提案、来源及事件关系 | 程序 | items + sources → CP、episode、关系、失败/FHIR | 部分约束已实现；最终前轮/全图回扫不完整 |
| 完整呈现分割提案 | 明确每一来源片段对本CP应如何呈现 | 特定CP、任务与来源版本 | LLM提案，可扩展review输出 | reconstruction proposal → `SplitProposal` | **待统一**；已有边界引文不等于完整呈现计划 |
| 分割编译和双层审计 | 忠实执行提案并检查盲法材料 | 提案、原文、策略 | 确定性程序 + 独立语义审核 | proposal → partition、diff、receipt、准入状态 | 有模板原型，通用版本化契约待补 |
| FHIR派生包和呈现 | 冻结医生实际收到的原文 | 已审核片段和中性问题 | 程序 | approved partitions → reviewer bundle + renderer | 原始FHIR适配已有；医生专用视图未接通 |
| 专家有效性与Reference | 判断可评价性，形成独立临床依据 | 冻结原文包与冻结表单 | 医生；程序记录版本/接触史 | packet → validity/Reference/修订需求 | 台账可复用；真实正式流程待执行 |

## 5. 分割 Gap 的准确位置

上一轮“分割执行和审计缺失”应收窄为：**没有接成通用、独立、版本化的提案—编译—审计—FHIR材料—医生呈现接口链**。已有三类部件应复用：

- `validation.py:84–92,121–235`：严格逐字引用解析、hash/坐标/allowlist验证。
- `context.py:534–571`：按选择子句、后续事实做区间相减，保存保留/排除 ledger；术中另有“探查→首次术中诊断”的栏目正则。
- `p2_pathway_replay/core.py:114–136,183–197`：特定模板的 KEEP/MASK/REVIEW 连续分区校验、原样切片和禁止引文字面检查。

当前主要分割的是 **review可引用为选择前事实的范围**。review仍收到整篇原文，以重建身份；因此这不是医生实际可见的盲评包。

已有分区原型不处理通用跨文书答案回述、元数据/附件绕回、完整模型提案及标准医生视图；其中断言也宜在正式接口改为可记录的显式错误。本文未修改它。

## 6. 字符数组、逐字引文还是工具

建议 **LLM判语义，工具返回原文锚点，程序计算并执行坐标**。不依赖LLM人工数字符，不用宽松正则把模型改写“修成”原句。

最小工具接口草案（本研究定义）：

```text
get_source(source_id, source_version)
    → 原始全文、来源身份、text_sha256、offset_unit

resolve_anchor(source_id, source_text_sha256, exact_quote,
               optional_prefix, optional_suffix)
    → 零/一/多个精确候选、程序生成的anchor_id和坐标

submit_split_plan(checkpoint_id, task_mode, visibility_mode,
                  source_manifest_hash, anchor_roles, unresolved)
    → 已保存提案，不代表已审核

compile_split(plan_id, compiler_version, policy_version)
    → partition、完整diff、机械回执；失败不静默修复

review_partition(partition_id, independent_review)
    → 通过/需修订/未定；保留责任人与原文依据
```

工具不是另一个临床判断者：它负责查找、计数、切片和验证；歧义由模型/审核者结合全文解决，无法解决即待核。它可以作为函数调用工具，也可先实现为文件级纯函数，无需先部署 FHIR 服务。分割提案可由第二轮扩展输出或独立工具步骤产生，不必机械规定增加第三次模型请求。

### 定位规则

原文必须先冻结精确文本表示和来源版本。`source_text_sha256` 对该文本的 UTF-8 字节计算；坐标固定为 **Unicode code point、零起点、左闭右开 `[start,end)`**。哈希不证明医院原生版本或真实发布时间。

当前实现使用 `text.find` 逐字匹配，不是正则/语义近似，不统一引号、空格、换行和Unicode。修改一个引号也可能 `QUOTE_NOT_IN_SOURCE`。重复出现时保留所有位置，不能任取第一处做切点；显式坐标错也不回退模糊搜索。

浏览器常规字符串索引按 UTF-16 code unit，Python 当前按 code point，UTF-8字节又不同。正式renderer必须使用明确转换；emoji、扩展汉字、组合字符及CRLF应有跨端对照。原始数据若有格式投影，需保存 original artifact hash 与精确文本投影hash，不能把投影坐标当PDF页坐标。

### 两个分开的标准对象

**`ReconstructionProposal`**：研究问题、两原则判断、事件身份/前轮/过程关系、时间依据、来源引文和七维未定；保留模型原输出。它是研究重建提案，不作为医生答案。

**`SplitProposal` / `CompiledPartition`**：针对特定checkpoint、任务模式、可见性模式和源版本，指定每段语义角色与呈现处置。同一原文对于不同checkpoint可能有不同资格，不能设全患者永久mask。

下面是纯虚构的**程序编译后**对象节选，坐标由程序计算，不是要求模型填写的数值：

```json
{
  "schema_version": "research-compiled-partition-draft-v0.1",
  "checkpoint_id": "CP-fictional",
  "source_id": "source-fictional",
  "source_version": "local-content-snapshot-1",
  "source_text_sha256": "a4e38e7ffed901f5e656c2b21f566252d250209cb5a5e8d1a38886627d6c861b",
  "offset_unit": "unicode_code_point",
  "interval": "half_open",
  "task_mode": "independent_decision",
  "visibility_mode": "retrospective_phase_reconstruction",
  "segments": [
    {"id":"s1","start":0,"end":11,"quote":"探查见未发现远处转移。","role":"prechoice_observation","disposition":"KEEP"},
    {"id":"s2","start":11,"end":20,"quote":"随后决定实施切除。","role":"current_decision","disposition":"MASK"},
    {"id":"s3","start":20,"end":32,"quote":"完成切除后生命体征平稳。","role":"postchoice_result","disposition":"MASK"}
  ],
  "mechanical_status": "PASS",
  "semantic_audit_status": "NOT_REVIEWED",
  "blind_packet_status": "NOT_FROZEN"
}
```

该例只证明可以按提案忠实切片，不证明真实病例语义/可见性已通过。阶段重建模式也不伪称医院当时实际EHR快照。

分区处置可为 KEEP、MASK、REVIEW；语义支持引用可以重叠，**最终呈现分区必须互斥、无洞并覆盖全文**。未知或未标区域不得默认开放。每份来源都要有纳入/排除/待核记录，范围作用于来源或包，不否决患者其他可用资料。

机械审核检查版本hash、逐字一致、坐标、完整覆盖、无冲突、切片未改字、输出映射及输入/输出/编译器hash。独立语义审核另查该切点是否正确、当前答案是否完整遮蔽、合法依据是否误删、既往真实计划是否保留、跨文书回述与时间资格。一个 `PASS` 不能同时代表两者。

## 7. FHIR适配层应承载什么

建议沿用项目的 **FHIR R4 4.0.1**，定义本研究的 profile/extension、CodeSystem/ValueSet、OperationDefinition及非FHIR的分割/包manifest schema。符合FHIR基础JSON结构，不等于研究契约、临床语义与盲法审核已通过。

| 信息 | 建议表示 | 需明确的自定义部分 |
|---|---|---|
| 原始来源 | `DocumentReference` / `DiagnosticReport` / `Observation`，必要时附件/`Binary` | 精确原文表示、来源时钟、hash和本地来源版本；原件不改写 |
| 实际交付的派生原文视图 | 独立 `DocumentReference` + 派生附件；对应文书可用 `relatesTo.code=transforms` | 派生标记、split plan、坐标映射、内容hash、可见性模式 |
| 转换及审核来源链 | `Provenance`，派生物为target，来源为entity | 模型/规则/编译器/审核身份及hash；完整链归研究者侧，不提供绕回全文的医生取回通道 |
| 研究Episode/Checkpoint | 明确研究用途的 `Basic` profile + 类型化extensions；充分评估其他资源后使用 | 本研究事件、身份簇、前轮/过程关系、部分顺序、packet引用及版本；不冒称原生临床医嘱 |
| 审核表 | 冻结的 `Questionnaire` / `QuestionnaireResponse` | validity、AMS、动作、Severity、Confidence仍由正式研究方案定义 |
| 交换包 | MVP可用 `Bundle.type=collection` | 研究者包与医生可见包分别生成；临床内容hash和renderer映射 |

`EpisodeOfCare` 表示患者与承担照护责任的机构/提供者的关联，不能因名称相似直接当作本研究决策episode。FHIR的 **Slicing** 指profile中重复元素的约束划分，也不是切病历原文。

当前 `Basic` 主要把本地事件JSON放进自定义字符串extension，缺少已发布的字段级研究profile。应定义类型化引用、必填字段、关系、状态和不变量；不能只把任意JSON装进去就宣称标准互操作。

FHIR提供原件、派生件、关系和来源链的表达，不自带本研究的语义分割/遮蔽编译器。自定义操作可用 OperationDefinition 描述，例如本研究 `$compile-checkpoint`；这个名字不是HL7预定义标准操作，也不要求MVP先搭服务。

R4还有一个实现细节：`Attachment.hash` 定义为base64编码的 **SHA-1**，不能把本研究SHA-256直接塞入该字段；可为SHA-256定义明确扩展。需对原始附件hash和文本投影hash分别说明。

官方依据：[DocumentReference](https://hl7.org/fhir/R4/documentreference.html)、[Provenance](https://hl7.org/fhir/R4/provenance.html)、[Basic](https://hl7.org/fhir/R4/basic.html)、[EpisodeOfCare](https://hl7.org/fhir/R4/episodeofcare.html)、[Profiles](https://hl7.org/fhir/R4/profiling.html)、[OperationDefinition](https://hl7.org/fhir/R4/operationdefinition.html)、[Attachment.hash](https://hl7.org/fhir/R4/datatypes-definitions.html#Attachment.hash)。

### 两个交付视图

研究者视图看原文、模型提案、保留/遮蔽/待核diff、理由和完整审计。医生视图只看经方案审核的中性问题及原始临床内容，不附模型两原则标签、预设动作、被遮原文或可绕回的附件。若已有计划复核是任务，须显式换 `task_mode=plan_review`，不能与独立决策模式混用。

派生视图保留来源分隔和中性省略标记，不能把非连续片段改写、拼接为新的临床事实。标题、元数据、链接、附件与页面源数据也需纳入审查。最终输出应是**版本化Episode对象 → checkpoint对象 → 冻结packet引用 → FHIR派生材料与实际呈现**，而非只有文字总结。

建议状态分开：`PROPOSED → MECHANICALLY_VALIDATED → SEMANTIC_AUDITED → CONTENT_FROZEN → VALIDITY_GATE_PASSED → REFERENCE_COLLECTED`。任一步未定保留具体原因；不自动判患者无关，也不把结构通过当医生Reference。

## 8. “证明引用失败”具体是怎么回事

它表示“模型给出的这条证明没有满足该字段的来源/位置/范围/时间要求”，不等于病历没写，也不等于临床结论错误。

**真实开发输出A，匿名简化转述**：当前术中记录包含探查事实，随后写具体选择。模型在 `observation_quotes` 引了 `[312,662)`，把后面的实际选择 `[625,662)` 一起吞入；允许观察仅 `[312,610)`。原文逐字存在，程序仍报 `QUOTE_OUTSIDE_ALLOWED_SPANS`。唯一观察及对应可见性声明无效，候选降为 `UNCERTAIN_CONTRACT_OR_SOURCE`。此前临床计划的引用、当前较短比较事实 `[500,609)` 均有效；这不是缺前轮资料。

**真实开发输出B，匿名简化转述**：炎症较重的事实和化验均已找到，但一个比较引文将事实与“给予治疗、控制后手术”的选择合引，越范围；另一个引文改了弯引号，严格查找失败。事实存在而证明写坏，与只按报告日期猜测医生已知，是两个不同缺口。

**真实开发输出C，匿名简化转述**：观察、前后比较和可见性全部合格，模型却一边标“只是执行”，另一边标“新证据checkpoint”。这是 `MODEL_EVENT_FUNCTION_CHECKPOINT_CONFLICT`，不是引用失败。

`NO_VALID_PRIOR_OBSERVATION` 是容易误解的原因码。`select_status` 实际检查**当前选择之前的观察引文**是否合格，不是说“前轮没有资料”。`connection_quotes` 则可以含“事实，因此决定……”整句来证明连接，不能把 observation 字段的禁用范围推广到所有字段。

实际依据见 `private/p2_auto_checkpoint_v0.5_20261027/cp_alignment_regression_audit.md`；定位逻辑见 `validation.py:84–92,192–235`、`engine.py:129–138,147–172`。

## 9. 另外几个问题，用具体故事说清楚

**最终前轮没有复查**：第一轮以为E0是患者管理决定；E1后来把E0当比较前轮。各篇review结束后，E0被认定为一般模板、不是患者实际判断，E1却仍“相对E0有新信息”，过程边也可能supported。缺的是所有review结束后重新核对身份、前轮角色及图成员。不应简单要求前轮必须也是被选CP；重复记录可能承载真实既往决定，应挂回正确事件。

**分割/盲评不足**：程序可能已规定“只能引用探查段作为观察”，但仍把含“决定切除”的全文放在 `current_full_document` 给重建LLM。重建模型可据此判断身份；如果把同一请求直接给盲评医生或Agent，就把真实答案一起交出去了。需要独立编译的医生视图。

**没有完整补发现**：标题为“评分表”的原文里可能真的写了“发现新问题，原安排暂缓”；它被路由为support，没进主发现。另一篇虽被读了，但discovery返回零事件，或只找到E1漏了E2，review都无法增加新ID。需要专项召回核验，不把所有零事件计为错误。

**并发格式恢复取错回执**：A文书返回JSON格式错误，B文书刚好成功，最后共享回执变成B。A准备一次格式提醒时读取最后回执，发现不是A，于是直接失败。已复现的是漏掉格式恢复，不是把B的临床答案错装给A；尚未定位真实运行哪些失败由此造成。

本次仅核对实现并提出接口草案。上一轮的稳定问题编号 CE-AUD-001至006继续保留；工程修复需迁入正式回归及issue map，不以本草案宣称已修复或专家已审。
