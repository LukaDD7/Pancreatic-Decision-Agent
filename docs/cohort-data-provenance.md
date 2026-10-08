# 队列数据来源、时间语义与复现说明

本文对应 Issue #1，说明本仓库当前能够复现的边界。

**本目录保存上游队列生成逻辑，供来源审计和历史复现；不等于当前四阶段实验的已验证可见性规则。当前输入分包使用 `pancreatic_agent.timeline_extract` 和 contract v0.3。旧脚本的 `pre_T0`、`temporal_bucket`、规范答案均不能直接作为独立评估的真实可见性或临床金标准。**
仓库不包含真实病例、索引、输出或人工审阅表；本地数据仍由 `PANCREATIC_DATA_ROOT` 指向。当前代码从已建立的 Stage 5/6/7 索引开始消费数据，原始医院 XML、CSV 到这些索引的全部早期解析器尚未随本 PR 提交，不能把本说明理解为原始数据库的完整 ETL。

## 运行入口

安装依赖：

```powershell
python -m pip install -e ".[test,cohort]"
$env:PANCREATIC_DATA_ROOT = "D:\path\to\local-data"
```

按依赖关系运行：

```powershell
# 输入契约、来源盘点、分层抽样和校验；不执行批量医学事实抽取
python -m scripts.cohort_construction.agent_packaging_and_audit.shared.stage8a run-all

# 特殊病例池
python -m scripts.cohort_construction.upstream_special_cases.step01_find_restage_candidates
python -m scripts.cohort_construction.upstream_special_cases.step02_adjudicate_candidates
python -m scripts.cohort_construction.upstream_special_cases.step03_expand_five_class_ledger

# 原100例
python -m scripts.cohort_construction.cohort_100.step01_build_pre_t0_signal_pool
python -m scripts.cohort_construction.cohort_100.step02_build_closed_decision_chains
python -m scripts.cohort_construction.cohort_100.step03_build_four_pathway_cohort_100

# 751、851和术前180例
python -m scripts.cohort_construction.cohort_751_851_180.step01_audit_remaining_751
python -m scripts.cohort_construction.cohort_751_851_180.step02_build_continuous_decision_851
python -m scripts.cohort_construction.cohort_751_851_180.step03_build_preop_decision_180

# 100例患者状态和Agent包
python -m scripts.cohort_construction.agent_packaging_and_audit.cohort_100.step01_build_patient_states_100
python -m scripts.cohort_construction.agent_packaging_and_audit.cohort_100.step02_build_t1_candidate_audit
python -m scripts.cohort_construction.agent_packaging_and_audit.cohort_100.step03_build_agent_eval_100_package
```

仅特殊病例候选的模型复核需要远程模型。运行时使用 `LLM_API_KEY`、`LLM_API_URL`、`LLM_MODEL`；`LLM_API_URL` 按 SDK 约定配置为基础地址（如 `https://example.invalid/v1/`），队列 HTTP 调用自动追加 `chat/completions`，同时兼容旧完整请求地址；提示词和输出校验在对应脚本中，密钥不进入文件。其他步骤是确定性规则或本地人工审阅结果的消费。

## 原始来源与字段映射

| 类型 | 当前消费层 | 主要输入字段 | 关联键 | 包内字段与用途 |
| --- | --- | --- | --- | --- |
| 文书 | Stage 5 `document_l1`；Stage 7 `document_day_detail` | `source_file`、`source_row`、`source_record_id`、`PATIENT_ID`、`VISIT_ID`、`文书名称`、`文书内容`、`create_time` | `patient_uid`；就诊层用 `encounter_uid`/`VISIT_ID`；记录层用 `source_record_key` | 文书类型、正文、决策前可见时间、来源定位 |
| 检验 | Stage 7 所有 `lab_result_detail/*.parquet` 分片 | `item_name`、各结果字段、单位、`sample_time`、`report_time`、`available_time`、异常标记 | `patient_uid`、`source_record_key` | 检验项目、原值/数值/定性值、单位和时间 |
| 影像报告 | 胰腺影像报告索引及 Stage 7 `imaging_event` | `index_report_uid`、`source_record_key`、`patient_id`、`patient_uid`、`exam_datetime`、检查方法、描述、诊断、全文 | 患者号先映射到唯一 `patient_uid`；报告用 `index_report_uid` | 描述和诊断均保留，不只保留结论；当前缺少完整签发时间 |
| 病理 | Stage 6 `pathology_total_v2/pathology_record` 所有 Parquet 分片；Stage 7 `pathology_event` | 标本、临床诊断、病理诊断、镜下所见、取材/收到/报告日期、`pathology_record_uid` | `patient_uid`、`source_record_key`、`pathology_record_uid` | 病理正文、标本信息和报告可见时间 |
| 随访 | 本地专病随访表 | 患者号或规范化病理号、结局字段 | 患者号优先；病理号仅作受审计的辅助映射 | 与原始临床事实分开存放，不倒灌到术前输入 |

`patient_id` 是业务患者号，`patient_uid` 是跨来源主键，`VISIT_ID`/`encounter_uid` 是就诊层键，影像号和病理号都不能直接当患者主键。患者号到 `patient_uid` 的映射必须唯一且完整，否则构建中止。

输出证据保存 `source_record_key`，并在来源可得时保存 `source_file`、`source_sheet`、`source_row`、`source_record_id`。同一患者、同一事件、同一来源类型和相同正文哈希才合并报告实例；所有原始来源仍进入 `source_map`。跨患者、跨事件或肯定/否定含义不同的记录不合并。

## 时间字段语义

| 字段 | 含义 | 能否直接表示当时可见 |
| --- | --- | --- |
| `create_time` | 本来源的住院建档/归档时间 | 不能作为文书完成或可见时间；旧队列脚本仍按此字段截断，仅保留作历史复现 |
| 住院建档/入院时间 | 就诊开始时间 | 不能据此认为本次住院全部文书已经可见 |
| 正文首时间或明确记录时间 | 整理方确认的文书完成时间 | 当前实验依据；混合版本和正文冲突仍需审阅，不要求与建档时间一致 |
| `exam_datetime` | 影像检查实施时间 | 当前因签发时间不完整而作为弱代理，并标记为 proxy；不能声称报告当时已经签发 |
| `sample_time` | 标本采样时间 | 不是检验结果可见时间 |
| `report_time` | 检验或病理报告时间 | 无更直接的 `available_time` 时使用 |
| `available_time` | 索引推定结果可读取时间 | 决策截断首选字段，但仍须保留来源和推定规则 |
| 取材/收到日期 | 病理流程中的临床或接收日期 | 不是病理诊断可见时间 |
| 病理报告日期 | 病理结论形成日期 | 可作日级代理；同日且没有时分时不能证明在决策前可见 |

以下为**历史队列构建规则**：检验按 `available_time > report_time > event_time_used`，病理按 `report_time > available_time > event_time_used`，文书按 `create_time > event_time_used`。这段描述不认可建档时间作为当前实验的可见性依据。共享示例函数已改为只接受调用方从正文核对的 `document_completion_time`；缺失则排除，不回退到 `create_time`。只有日期或 `00:00` 的记录标为低精度；病理同日低精度记录不进入决策前状态。原100例若信号只有日期，会把 T0 暂置为当日 `23:59:59`，字段同时标记 `date_only_end_of_day_proxy`；这只能用于候选构建，不能解释成医生在全天任何时刻都已见到材料。

事后补录、出院总结和回顾性病史可能描述更早事件。建档时间不能决定其最早可见边界。正文完成时间限制整篇文书的纳入；正文引用的既往事件只能经来源绑定的片段审阅回建，不能把后来整篇文书倒推为当时已知。时间冲突不自动选择“更合理”的一个，需保留冲突或从决策输入排除。

## 文书、检验与泄漏控制

- 文书标题只用于候选筛选，医学事实来自正文；标题和正文矛盾时保留来源并进入人工核对，不用标题覆盖正文。
- 一份文件中的多条记录按 `source_record_id`/`source_row` 分开。无法定位到记录级的旧文件不得伪造行号。
- 诊断、计划和实际术式必须分开。术前180例会删除含拟术式、实施术式或结局的句子；不能可靠删除时，整段不进入 Agent 输入。
- 检验按项目行保存，不把采样时间当结果可见时间。所有选中 Stage 7 Parquet 分片都会读取，不能只取首个分片。
- 病理同样读取全部 Parquet 分片。本 PR 已移除固定 `part-00001.parquet` 的旧实现。
- 文书正文解码优先使用结构化 Parquet；遗留字节按 `gb18030` 兼容解码。报告结构化证据片段最多 1,200 字符，原始受限副本不因此删除；文书读取时去标识化上限为 50,000 字符，术前安全状态片段另有 1,800 字符限制。
- 影像描述、诊断和可用全文均参与规则，不是只保留影像结论。
- “转移信号”“窗口类型”和规范答案是派生标签。`diagnosis_deidentified` 中的“阳性/阴性”当前被原样透传；因早期影像索引生成器未提交，不能凭本 PR 判定其来自原始报告还是后处理标签，实验输入应单独审阅。

## 队列关系与筛选

| 版本 | 关系 | 主要纳入逻辑 | 明确排除或限制 |
| --- | --- | --- | --- |
| 原100例 | 从特殊信号和闭合决策链候选中分层抽取 | 有胰腺相关信号、可识别 T0、后续实际路径可核对，按四路径平衡 | 计划与实际动作混淆、缺关键时间、证据链不闭合 |
| 剩余751例 | 与原100例患者不重叠的候选余集 | 审计五类特殊病例、纵向和 Agent 轨迹可用性 | 重复患者、与100例重叠 |
| 851例 | `751 + 100` 的患者级并集 | 同日事件聚合为窗口，生成状态—可选动作—实际动作—反馈 | 孤立检验不单独建窗；U 表示索引未见，不改写为 N |
| 正式补建251例 | 851例中的第二决策窗口扩展子集 | 至少有可复核的后续决策候选 | 只观察到结果而无决策证据者不强行标决策 |
| 术前180例 | 从851例重新按术前信息边界选择 | 唯一患者、决策锚点早于实际动作、锚点前180天有影像；含部分非手术动作 | 已泄漏拟术式/实施术式、锚点在动作后、影像缺失；原100例仅术前影像已见转移信号者可作为例外纳入 |

队列脚本硬校验 100、751、851、正式补建251和目标180的患者关系。最近一次本地851审计产生 6,992 个窗口和 6,141 个相邻转换；这两个数量是数据快照结果，不是固定常量，重跑后必须以新 `audit.json` 为准。候选数、排除原因、动作分布和缺失资料数量均由脚本写入各输出目录的审计文件，不在代码中推测补齐。

所谓 rebuild 版本主要是补齐来源、检验、病理和 Agent 包的重建，不自动改变队列成员。若人工替换候选，必须保存替换前后患者键、理由、操作者和时间；当前仓库只有替换建议和审计输出，没有完整通用人工修改日志，这是已知缺陷。

## 自动规则、人工修订与模型使用

- 自动规则：主键连接、时间截断、正则候选、分层选择、去重、字段提取、来源哈希、计数与硬校验。
- 人工步骤：疑难记录判读、标题正文冲突、时间冲突、候选替换、规范答案终审。人工结果必须与原始事实分表保存。
- LLM：只用于特殊病例候选复核等明确脚本；提示词固定在源码，模型名、提示词版本、成功/失败数写审计文件。模型不能生成缺失事实，也不参与原始主键连接。当前代码没有把任何密钥写入仓库。

## 完全虚构的最小示例

```powershell
python -m scripts.cohort_construction.example_fictional_bundle examples/fictional_raw_records.json
```

预期输出包含 2 条决策前可见报告和 2 条排除记录：影像检查时间作为 proxy 纳入，检验按 `available_time` 纳入；决策后完成的入院记录排除，同日仅有日期精度的病理报告也排除。输出保留来源文件、工作表/行号、记录 ID 和 `source_record_key`，`labels` 为空，说明原始证据与分析标签没有混在一起。

## 已知缺陷

1. 原始医院 XML/CSV 到 Stage 5/6/7 的完整上游解析器尚未全部进入仓库；现阶段复现起点是已建立的索引。
2. 影像报告签发时间覆盖不足，`exam_datetime` 只能作为弱代理。
3. 旧文书的 `00:00` 可能是真实午夜，也可能是占位；未获得更精确来源前只能标低精度。
4. 随访表的患者号/病理号匹配存在人工核对环节，不能自动当作术前事实。
5. 通用人工修改日志尚未完整实现；发布数据集前需补齐逐条变更记录。
6. 历史队列仍含以建档时间截断文书的实现；不能直接用于当前四阶段效果评估，需经过正文时间与原文片段重建。
7. 原始影像描述、诊断和“阳性/阴性”的列语义需要早期索引生成器确认，本 PR 的字段透传无法独立证明其是报告原文或派生标签。
