# 独立 Checkpoint 决策接口 v0.1

稳定标识：`independent-checkpoint-eval-v0.1`。配套 HEWV v0.4 输入契约，单独版本化动作、输出 schema 与 system prompt；不改变 episode 或整份记录的可见性规则。

这是四问独立的初步测试。每个问题只包含当前固定 context；没有 patient state 注入、前问回答、memory、RSI、环境回放或临床动作执行。模型输出结构化结论不等于研究者事先构造状态。

## 1. 三种临床 Mode

评价边界始终是**当前根治性胰腺切除路径是否应继续推进**，不是“要不要继续任何诊疗”。早期发现阶段仍回答这个问题，但不预设患者已经进入手术路径。

| Mode | 操作性定义 | 不能解释为 |
|---|---|---|
| CONTINUE | 现有证据足够继续所评价的根治路径，无需为当前关键问题再追加分期取证 | 只是继续诊断流程；自动执行手术；任何不确定性都已消失 |
| PAUSE | 存在可能改变路径的关键未知、矛盾或可纠正条件，先选一项针对性动作，再复评 | 已证实不可切；暂停全部诊疗；把所有缺项补齐 |
| EXIT | 现有证据已足以支持改变当前根治切除路径，转其他管理 | 单纯“资料不够”；终止全部治疗；永远排除未来转化/手术 |

三者是模型需要作出的临床建议，没有固定的置信度阈值或“缺检查必 PAUSE”规则。资料中的可疑与确证须由模型区分。对其他胰腺肿瘤不能机械套用 PDAC 的转移处理；病种未明时保留不确定性。

参考对话将 DEFER_TO_EXPERT 与临床 mode 区分。当前初版遵循用户要求仅用三个临床 mode，专家复核可作为 PAUSE 下的明确动作。后续如增加系统可靠性 gate，另开版本；本版不自动以重复一致性决定专家转交。技术失败独立记录，不生成临床 mode。

## 2. 首选动作空间

唯一机器来源：[actions-v0.1.json](../../pancreatic_agent/evaluation/resources/actions-v0.1.json)。定义 code、允许的 mode 和含义，共20项。相同动作表用于所有阶段，不能根据患者后来实际检查过什么动态删选，否则泄漏未来动作可得性。

| Mode | 动作代码 |
|---|---|
| PAUSE：影像 | PANCREAS_PROTOCOL_CT、LIVER_MRI、PANCREAS_MRI_MRCP、CHEST_CT、PET_CT |
| PAUSE：组织/腹腔取证 | EUS_WITH_TISSUE_SAMPLING、TARGETED_BIOPSY、STAGING_LAPAROSCOPY |
| PAUSE：复核已有来源 | EXPERT_RADIOLOGY_REVIEW、RETRIEVE_EXISTING_RECORDS |
| PAUSE：其他针对性评估 | TARGETED_LABS、FITNESS_ASSESSMENT、MDT_REVIEW、OPTIMIZE_CLINICAL_CONDITION、OTHER_TARGETED_EVIDENCE |
| CONTINUE | CONTINUE_NO_NEW_STAGING |
| EXIT | REDIRECT_ONCOLOGY_PATHWAY、REDIRECT_SUPPORTIVE_CARE、REDIRECT_OTHER_DISEASE_PATHWAY、RESPECT_DECLINED_SURGERY |

每问恰好一个 `primary_action`。目标、问题、处置分支必须写清。CT code 可用于初次完善或重评估，在 purpose 说明；不新增“重评估”臆造事实。MRI 与 MRCP 的范围在 target/purpose 说明。一个腹腔镜动作可以包含其取材目的，但不能把 MRI、PET 和腹腔镜套餐装进同一个动作。

`OTHER_TARGETED_EVIDENCE` 是可审阅的扩展口，必须写明一项具体取证，不自动执行。既有资料足以解决问题时，不因动作表有该检查就再申请。CONTINUE/EXIT 不涉及待返回检查时，两个处置分支可以明确写“不适用”并说明改变路径的条件。

动作表是**研究者设计的建议分类**，不宣称指南规定了这些代码或三种 mode。临床适用性需专家审阅，软件只能验证代码和 mode 配对。

## 3. 指南依据及适用范围

- [2025 泛亚洲适配 ESMO 指南，doi:10.1016/j.esmoop.2025.105826](https://pmc.ncbi.nlm.nih.gov/articles/PMC12546840/)：多期增强CT作为初始影像；MRI用于CT不能完成/不确定等情形；PET-CT为选择性分期，非原发诊断常规。可切除性兼顾解剖、生物学与患者条件；边界可切除可考虑诱导路径。
- [NICE NG85，1.1、1.2、1.3](https://www.nice.org.uk/guidance/ng85/chapter/Recommendations)：胰腺协议CT、需要时EUS取材、专科MDT；额外分期依具体问题选择肝MRI、EUS或腹腔镜，后者针对可能影响切除的小量腹膜/肝病灶。

这两份指南用于设计动作覆盖范围，未把整篇指南或额外检索结果注入患者 context，也未据此自动制定患者答案。NICE 较早期治疗部分不作为当前药物/诱导治疗方案来源；本接口不输出药物剂量。对于历史病例，本次试验采用现代建议框架，不能据此直接裁定当年的医生错误；正式临床评分需固定适用的指南版本。

## 4. 输出格式

唯一 schema：[output-v0.1.schema.json](../../pancreatic_agent/evaluation/resources/output-v0.1.schema.json)。当前基础提示词：[system-v0.1.1.txt](../../pancreatic_agent/evaluation/resources/system-v0.1.1.txt)。运行时将按mode分组的通用动作表与 schema 加入同一 system message。旧提示词v0.1保留，用于复现最初测试。

必填字段：`schema_version`、`mode`、`clinical_assessment`、`resectability`、`primary_action`、`evidence`、`uncertainties`、`decision_change_conditions`、`confidence`。

可直接阅读[完全虚构的输出示例](../../examples/checkpoint_decision_fictional.json)。它仅帮助研究者理解字段，不加入模型提示词，不作为任何真实患者参考答案。

- clinical_assessment 回答当次阶段问题；resectability 是模型输出判断，不是预注入标签。
- primary_action 包含 code、target、purpose、management_if_supporting、management_if_not_supporting、source_ids。
- evidence 为来源编号与简短解释。所有引用必须在本问可见来源集合内；编号存在不证明解释医学上正确。
- PAUSE 须说明条件如何改变决策。confidence 是自报数值，不当作已校准可靠性。
- 严格一个 JSON 对象；禁止额外字段、Markdown围栏、重复键、NaN/Infinity。无效输出原样留存，不删围栏、不补JSON、不请求另一模型修复、不自动重采直到成功。

## 5. 测试与评价契约

输入只读取 `packets.json`，不读取含未来事件的 timeline、源 bundle 或结局。核对整份 body hash、context hash及完整重渲染结果，拒绝未定位节点。`DRAFT_REVIEW_REQUIRED` 需显式探索性运行标记，原问题和报告代理/版本缺口进入本地 manifest，不被提升为真实临床快照。

预览默认零调用。执行时每问每次重复均为两条新消息；不根据 PAUSE 请求释放报告。无历史检查结果时也不模拟，其建议留待动作合理性审阅。重复数、模型、参数、提示词/schema/动作表、代码hash和输入hash留痕；改变参数创建新run，不能覆盖旧run。

技术状态：VALID、INVALID_OUTPUT、INCOMPLETE_RESPONSE、EMPTY_RESPONSE、REFUSED、TRANSPORT_FAILURE、PREVIEW_ONLY；请求进行中/中断另记IN_FLIGHT、INTERRUPTED，启动即记录尝试。VALID仅指格式、mode-action和可见来源引用通过，**不是临床正确**。

首版自动统计格式通过率、技术失败、各问 mode/action 分布、至少两次有效重复的一致比例；失败样本数同时报告，不把失败映射为PAUSE。四个问题不混成四条独立患者轨迹。临床合理性及事后可拦截性为独立人工评价，默认空，不以实际结局自动判对错。

## 6. 与上一输入版本的语义关系

HEWV v0.4 的 episode、来源集合和 checkpoint 规则未改。本次新增 `independent-checkpoint-eval-v0.1`：自由文本回答变为三个mode、一项首选动作与严格JSON，原来的多选建议不再作为机器首选输出。新增通用动作/格式指令会改变提示词条件，因此旧自由文本结果与新结果不能当作相同实验直接比较。

提示词v0.1.1只澄清格式：mode仅位于JSON顶层，primary_action仅允许六个schema字段；动作表改为分组文字，避免把动作元数据误复制进回答。临床mode、动作代码与schema未改。不同提示词的测试分开保存，不删模型多余字段来补成有效答案。

v0.3/v0.2 的提取器及早期序贯状态机保持历史行为。本次不把新输出直接传给旧控制器，也不宣称已经实现新的序贯回放。
