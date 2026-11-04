# Checkpoint–Episode：明确术语、事件接口与 HTML 盲评应用要求

2026-10-28。本文补充并澄清同日《逐节点接口与分割方案草案》。当前实现、已复现问题、接口建议分别说明；未修改模型提示词、解析规则、事件/Checkpoint资格或临床数据。术语要求已写入 AGENTS.md 第8节。

## 1. 术语及代码字段的对应关系

| 本文用语 | 代码字段/对象 | 不能混称为 |
|---|---|---|
| 发现阶段 LLM 提议的患者管理记录对象 | discovery `events[]` | 已确认的独立临床决策或候选Checkpoint |
| 复核阶段 LLM 对管理记录的判断 | review `items[]` | 医生Reference |
| LLM 指定的既往临床管理判断比较对象 | `previous_event_id` | 上一次模型调用、程序上一批结果 |
| LLM 指定的同一持续管理过程关联对象 | `same_process_previous_id` | 必然等同患者信息演化比较对象 |
| LLM 判断为同一实际事件的其他记录 | `same_event_with[]` | 新增临床决策 |
| 文书表述功能分类 | `record_function` | 没有分类维度的“角色” |
| LLM 标记的目标临床决策子句引文 | `choice_boundary_quotes` | 程序自主理解医学含义的结果 |
| LLM 提议的目标临床决策前患者信息引文 | `prechoice_facts` | 已由专家认证当时可见的事实 |
| 程序核验后允许复核LLM引用的原文区间 | `allowed_spans` | 已制作好的医生盲评材料 |
| 程序记录的来源处理明细 | `ledger/source_ledger` | 原始病历、完整历史正文 |
| 程序判定的文书类型标签 | `document_profile` | FHIR profile |

`record_function` 的六种值分别为：`new_decision`（LLM认为文书记录新的患者管理安排/判断）、`reassessment`（LLM认为文书明确记载重新评估）、`repeat`（LLM认为文书重述既定管理安排）、`execution`（LLM认为文书主要记载已实施措施）、`generic`（LLM未识别到针对当前患者的具体管理判断）、`uncertain`（LLM仍不能确定文书表述功能）。这些都是模型分类，不是新的临床事实。

## 2. 边界从哪里来，能否扩大处理规模

以纯虚构正文为例：

> 探查见未发现远处转移。随后决定实施切除。完成切除后生命体征平稳。

发现阶段LLM提交：

```json
{
  "action_quotes": [{"source_id":"D","quote":"随后决定实施切除。"}],
  "choice_boundary_quotes": [{"source_id":"D","quote":"随后决定实施切除。"}],
  "prechoice_facts": [{"source_id":"D","quote":"探查见未发现远处转移。"}],
  "postchoice_facts": [{"source_id":"D","quote":"完成切除后生命体征平稳。"}]
}
```

程序没有独立判断第二句话是临床决策。程序接受LLM提出的临床语义分类，再执行逐字查找：三段坐标为 `[0,11)`、`[11,20)`、`[20,32)`。程序据此把第一段放入临床决策前观察引用范围，将后两段排除。

复核阶段LLM如果把 `[0,20)` 填入 `observation_quotes`，程序检查整条引用是否包含于 `[0,11)`，结果为否。这个错误表示“LLM的观察引文含有已标出的目标临床决策文字”，不证明原文缺失。

真实术中开发案例还有程序栏目规则：发现阶段LLM标出临床决策子句 `[625,662)`；程序寻找“探查”栏目，并在首个“术中诊断”之前停止，实际保留 `[312,610)`；复核阶段LLM却引用 `[312,662)`，因此程序判定观察引文越范围。两种限制来源必须分别记录，不能把全部范围都归功于LLM。

程序逐字定位、区间运算和审计日志能批量运行，无需人手填坐标；但这不证明LLM标注临床决策子句的准确性已能泛化。需验证：LLM漏标临床决策子句、将患者事实误标为临床决策、重复原句定位歧义、无“探查”栏目、复合决策、回述与事件漏发现。工具返回原文锚点能减少模型人工数坐标错误，不能补救模型误判医学语义。当前每单位最多10个事件，文本预算也需继续报告。

## 3. 当前关系集合是什么，用什么实现

“当前关系集合”由Python字典、列表和并查集实现，不是FHIR引擎，也不是只连接Checkpoint的图：

- `decision_events.json` 保存全部患者管理记录对象，含重复、执行、不确定及被复核LLM判为 `generic` 的记录。
- `relations.json` 保存两类关系及原文支持；每条包含起点ID、终点ID、关系类型、`supported/uncertain` 与连接引文。
- `episodes.json` 保存程序按已通过机械核验的关系进行连通分组的结果；分组至少包含一个候选Checkpoint。

| 关系类型 | 临床记录层面的含义 | LLM提议字段 | 程序处理 |
|---|---|---|---|
| `same_management_process` | 两个管理判断属于同一持续管理过程 | `same_process_previous_id` | 来源/ID等核验通过后参与并查集合并 |
| `same_event` | 两段或多段原文实际记载同一次事件 | `same_event_with[]` | 身份合并并去除重复候选Checkpoint |

关系文件使用起点/终点形式；`same_event` 语义是身份相同，不表示临床时间先后。并查集按连通性分组，不保留方向作为因果链证明。

`previous_event_id` 目前没有另存为一类比较关系边；患者信息与管理判断的连接也没有独立事实节点/关系边。当前模型与代码能表达的关系比“完整临床决策图”少。

当前FHIR导出只接收事件和候选Episode，没有将 `relations.json` 独立导成关系资源。大部分研究字段位于 `Basic` 的私有JSON字符串扩展。`Provenance`和`supporting-source`用于回溯原文来源，不自动证明临床因果关系或Checkpoint正确性。

### 已复现的问题应怎样描述

发现阶段LLM先提议E0管理记录对象；此时它尚不是合格Checkpoint。复核E1的LLM把E0指定为患者信息演化的临床比较对象。同一批复核结束后，复核E0的LLM把 `record_function` 改为 `generic`。E0仍是同一个程序对象，只是分类和资格改变，没有变成另一种已确认“普通临床事件”。

程序组装候选Episode时排除generic E0，却没有重新核验依赖E0的E1候选Checkpoint和关系。合成例中出现：Episode成员只有E1，关系文件仍存在已标supported的E0→E1管理过程关系。

因此问题名应为 **“全部LLM复核完成后，程序缺少最终事件分类与依赖关系一致性核验”**。不能简单把E1改为起始锚点，也不能要求比较对象本身必须是被纳入的Checkpoint；需要核对E0是否指代另一个真实既往判断、E1是否有其他有效比较对象。稳定问题编号仍为CE-AUD-001。

代码：`engine.py:392–454,470–490`；FHIR导出：`run.py:28–39`、`p2_auto_checkpoint/fhir_io.py:51–68`。

## 4. FHIR profile是什么意思，当前缺少什么

FHIR标准提供通用资源。研究profile是在资源上说明“本研究必须提供哪些字段、哪些扩展和代码、如何关联”的机器可读约束，通常用 `StructureDefinition` 表达；扩展和代码表等可组成固定版本规范包。

例如，本研究要规定一个Checkpoint对象必须关联哪个冻结原文包、采用哪种临床决策任务、绑定哪版表单、记录何种时间证据，以及医生评审响应如何回指Checkpoint。仅把私有JSON放进Basic还不能使另一套HTML程序自动理解这些约定。

前文“未发布”更准确应写成：**本项目尚未形成并分发可由生成程序与消费程序共同校验的完整版本化FHIR研究规范包**。当前有扩展URL和JSON约定，未发现对应的完整 `StructureDefinition`/IG定义包；校验函数也明确未完成profile/FHIRPath/术语验证。

研究规范可以私有分发，不要求先获得HL7批准，也不要求公开患者数据。`document_profile='operative'` 只是一项文书类型分类，与FHIR资源约束规范不是同一概念。

官方依据：[FHIR Profiles](https://hl7.org/fhir/R4/profiling.html)。

## 5. “同次检查候选报告组”的准确含义

当前程序按 **检查号、检查时间、报告标题完全一致** 分组，缺检查号时按各自source ID单列。所有报告原文均保留；不按内容相似性合并，不自动用最新报告覆盖旧报告。

这可能聚合：同次检查的重复导出、不同字段记录，也可能是真正初稿与复核稿。但当前分组本身不证明原生修订关系，因此不应直接称为已确认“报告版本链”。

需要进一步区分三个对象：一次检查发生、每份独立报告原文、报告之间的关系依据。医院原生报告ID、修订状态、发布时间或审定依据不足时，保留同检查候选分组及未知，不推断哪份是最终真值。

同次检查后出的报告也不能自动进入较早临床决策的医生原文包。程序还将同组报告排除于“证明此前为何安排这次检查”的观察资料，防止用检查自身结果解释检查安排。

代码：`context.py:133–137,169–185,475–484`。当前Qwen十例产物含57个影像候选组：27组各1份、17组各2份、11组各3份、2组各4份。这些是分组规模，不是已确认的修订次数。

## 6. 节点03：LLM怎样定义多个事件，如何交给节点04

节点03允许发现阶段LLM返回 `events[]`，每个元素是一条模型提出的患者管理记录对象。LLM依据管理问题、文书是否记载新的判断、再评估、重复安排或措施执行来提议对象数量。没有“每句话一个事件”“每个血管一个事件”“每个阶段一个事件”的程序定义。

例如，同篇原文分别记载“呼吸改善，拟拔管”和“营养摄入不足，调整支持”，LLM可提出两条管理记录对象；多个术前评估字段共同支持同一手术安排时，LLM也可提出一条。数量和独立性尚需复核，不能把数组元素数当临床独立决策数。

节点03与04已有代码接口：

```text
发现LLM输出events[]（原文引文，不填字符坐标）
  → discover_unit逐条核引文、生成event_id、补来源字段、核时间
  → build_evidence_context(event, sources, …)
  → 每个event一个evidence_context
```

引用字段对接：`action_quotes→action_citations`、`prechoice_facts→prechoice_citations`、`postchoice_facts→postchoice_citations`、`choice_boundary_quotes→choice_boundary_citations`。程序解析后增加source hash和 `[start,end)`。

当前event ID基于unit ID和动作引文内容生成；引文长度、顺序改变可能导致ID改变，它不是永久临床事件身份证。重复提案先保留并标待核，后续依靠身份复核合并。

接口是内部Python对象配合内嵌JSON Schema，尚非独立发布的完整研究接口规范。最多10个数组元素；schema也不证明多个元素确实对应独立临床判断。

## 7. 节点04：程序怎样按术中阶段处理来源

这里没有完整的新辅助治疗/术前/术后治疗阶段状态机。当前特殊处理主要围绕 **目标临床决策发生于术中探查后、具体操作前** 的情形。

1. 发现LLM返回 `time_basis='operative_phase'`、`choice_time=null`，或在 `phase` 中描述术中临床决策阶段。
2. 程序据该字段或术中词匹配启用术中处理；它明确避免把“术前/术后/围手术期”字符串误识别为术中。
3. 对外部来源，程序通常用手术开始时间作保守检索界限，不把手术开始等同术者作出具体临床决策的分钟。手术期间返回的检验，若无法证明在该决策前已知，则待核。
4. 对当前手术记录正文，程序定位“探查”栏目，截至目标临床决策子句或首个“术中诊断”之前，再扣除LLM标出的决策后措施/结果文字。

例如手术08:00–12:00，病历15:00写完，某化验10:00出报告，但术者作出具体术式决策的分钟未知。程序不能因15:00写病历就把10:00化验当作术者当时已知；它把该外部报告待核，同时依据手术记录内的阶段描述提议探查观察区段。

`ledger`应称为**程序来源处理明细**：记录每条来源或原文区段被纳入、排除、待核或因预算省略的理由。一份来源可能有多条处理记录。

| 日志字段值 | 程序动作 |
|---|---|
| `INCLUDE` | 原文区段实际进入该事件资料块 |
| `EXCLUDE_FUTURE` | 来源时间晚于该事件检索界限，未纳入观察资料 |
| `REVIEW` | 时间、隔离状态或术中决策分钟等不足，保留待核 |
| `EXCLUDE_OWN_TEST_RESULT` | 未将某检查自身结果用于解释此前的检查安排 |
| `INCLUDE_COMPLETE_PRECHOICE_REGION` | 程序纳入扣除目标临床决策/决策后内容的连续原文区段 |
| `omitted_budget` | 候选原文区段超预算，整块未发送 |
| `omitted_provider_payload_budget` | 序列化模型请求再次超预算，程序移除整块并同步引用许可 |

日志不证明医生当时实际读过资料；“纳入原文候选资料”与“确认临床决策前已知”分别记录。代码：`context.py:418–653`。

## 8. 节点05：复核LLM具体能看到什么

一次请求对应当前一个unit，不是全患者所有原始病历。请求在姓名替换后发送，包含下列主体对象：

| 实际字段 | 模型实际收到的内容 |
|---|---|
| `current_full_document` | 当前unit的 `original_texts[]`：当前文书完整正文；若是候选影像报告组则含组内各报告完整正文。临床决策、措施执行和结果可能均在其中。 |
| `earlier_event_ledger` | 发现LLM提出的管理记录目录：ID、模型填写的管理问题/动作、分类、时间元数据及部分已核原句。不是全部历史病历。 |
| `events[]` | 当前unit各待复核管理记录对象，以及各自 `block_ids/cutoff/eligible_previous_event_ids/uncertainty/budget`。 |
| `evidence_source_pool` | 实际纳入并合并传输的原文块：某些历史文书完整正文、当前文书允许区段，以及被纳入的报告/检验正文。每块有source ID、坐标、正文、来源类型及时间。 |
| `instruction/document_profile/type_prior` | 任务规则、文书类型标签及审阅提示。还附 `reconstruction_only_actual_choices_disclosed=true`，声明目标临床决策文字已向重建模型披露。 |

共享目录中的每一条并非对每个目标事件都已确认更早；程序另给每个事件允许引用的历史事件ID。共享原文池物理上全部交给该次模型调用，程序通过各事件的block许可及返回引文核验限制用途，不是把模型视觉内容按事件硬隔离。

节点04完整来源处理明细没有全部发送给复核LLM；程序在本地保存明细和允许区间，用于核验模型返回的观察引文。历史来源未实际进入块池的正文也不应被报告为“模型已读”。外层消息还含 `task='document_checkpoint_review'` 与 `REVIEW_SCHEMA`。

代码：`engine.py:285–347`、`client.py:33–37`。

## 9. 面向单文件HTML盲评应用的FHIR接口要求

应按“生成研究材料→HTML显示医生许可信息→医生填写→导出响应→协调者导入台账”的闭环设计。

```text
医生专用FHIR Bundle＋packet manifest＋固定版本Questionnaire
    → 通用单文件HTML应用读取、校验并呈现
    → 医生浏览许可原文，填写validity、acceptable modes/actions等
    → QuestionnaireResponse＋ReviewSubmission
    → 协调者核对包/表单版本、评审分配与接触史后入账
```

- 可以采用一个通用HTML程序导入不同医生专用FHIR包；若要求每个包单文件分发，HTML只内嵌该分配允许的临床内容和表单。不得把未来Checkpoint、被遮原文或研究者全量Bundle藏在脚本、JSON、base64、注释或CSS隐藏元素中。
- manifest固定 `packet_id/version/checkpoint_id`、许可临床内容hash、表单canonical/version/hash、FHIR版本、研究规范包版本及renderer版本；HTML对不支持的版本或hash错误给出明确错误。
- 资源内容用文本和白名单渲染。附件只指向许可派生内容。医生可以正确浏览每条许可来源，看到按方案需要的来源等级、原始值及时间限制；不能点击回研究者隐藏原件。
- acceptable mode集合、acceptable action评价及理由分别作为表单条目，不因模型现有接口只输出一个首选动作而把专家答案限制为一个。可多选、无法判断和必填条件均由冻结Questionnaire/研究方案规定，不预填模型答案。
- `Questionnaire`定义题目、代码、必填和重复规则；`QuestionnaireResponse`保存实际回答。本研究Checkpoint和packet hash通过正式扩展或外层 `ReviewSubmission` 绑定，不挤占语义不符的FHIR字段。
- 离线HTML可保存/导出本地答案文件，不要求LLM、API密钥或远程脚本。实际专家身份、是否看过后续资料、其他专家是否已通过有效性门槛，由协调者和台账管理，单文件不能自行证明。
- 若研究方案要求先完成多人有效性检查再填Clinical Reference，宜分阶段发放表单和许可包；本地UI通过某项检查不等于全流程正式准入。

官方依据：[Questionnaire](https://hl7.org/fhir/R4/questionnaire.html)、[QuestionnaireResponse](https://hl7.org/fhir/R4/questionnaireresponse.html)。具体AMS/Mode/action定义仍属本研究方案，FHIR不预设临床答案。

本次更新仅涉及交流术语、说明文档和流程演示，未把HTML应用或FHIR研究规范包描述成已实现功能。
