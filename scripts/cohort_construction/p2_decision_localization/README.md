# P2决策记录定位 v0.3

说明修订（2026-10-21，v0.3.2）：报告按“判断步骤→原因短注释→具体分支→代码操作”呈现，逐项对应现行函数、配置键与局部范围；少量实际代码摘录仅说明关键操作，不替代流程伪代码。“双向”仍仅指从标签看上下文、从少量原文检查标签的两个查看角度。程序、规则及统计仍为v0.3.0。

这个目录把原先按日期命名的探索脚本整理成可复跑的流程。输入仍是冻结P1 v0.2中的同一批100/300人，不修改P1、不认证临床参考标准或正式episode。

## 从哪里开始

先读 `docs/P2决策记录定位-定义规则与双向样本-v0.3-20261020.md`，再看本文件及配置。不要把旧v0.2报告中的“候选点”直接与本版“候选链”相减：读取上下文范围、字典和压缩方法均有调整。

| 文件 | 职责 |
|---|---|
| `configs/rules-v0.3.json` | 字典、标签定义、段落标题、候选文书类型组、研究期及24/48h/7天窗口；正则的唯一完整配置入口 |
| `documents.py` | 校验原始缓存哈希及冻结P1源记录数；解码正文；保留原始文书名、类型组、VISIT_ID、两套时钟和命名日期字段 |
| `labels.py` | 提取文字标签及偏移；判断疑问/否定作用域、段落位置、时间角色、操作对象；保留逐命中的待核原因 |
| `timeline.py` | 按文字状态连接邻近记录，保留状态差异和意见冲突；标记跨visit重复句，不自动认定为临床更新 |
| `run.py` | 唯一批处理入口；重放固定抽样，运行上述模块，输出患者私有明细和不含患者标识的聚合统计 |
| `review.py` | 用人工源文书注释制作双向样本卡；验证引文偏移，显示规则结果及不按标签筛选的跨visit邻域 |
| `tests/test_rules.py` | 合成回归病例；覆盖已观察到的误配、漏检、时间和压缩风险，不包含患者原文 |

## 输入、环境与运行

在独立本地版本库根目录运行，使用Python 3.12及pyarrow。原始数据不随Git分发。需要：

- `local_artifacts/p1_quality_v0.2/results/patient_quality.jsonl`：冻结P1结果；
- `local_artifacts/source_cache_20261020/*_cache_manifest.json`及其指向的缓存源文件：265份临床parquet和300名患者影像原文；
- `local_artifacts/p2_scale_20261020/selection.json`：v0.2冻结的随机抽样，程序从2,004名PASS重放种子20261020核对；
- v0.2的 `clinical_notes.jsonl`：只用于核算本版新增上下文，不作为本版唯一源数据；
- 人工复核另需私有 `semantic_annotations.json`，含源记录键、原文偏移、引文和分析者解释；不是自动生成的真值。

```bash
python -m unittest discover -s scripts/cohort_construction/p2_decision_localization/tests -v
python -m scripts.cohort_construction.p2_decision_localization.run --repo "$PWD"
python -m scripts.cohort_construction.p2_decision_localization.review --input local_artifacts/p2_localization_v0.3_20261020
```

`run.py`可指定 `--output`；`review.py`的输入须指向同一目录。缓存清单中的绝对路径用于定位文件，SHA-256才用于验证内容，迁移机器时需更新缓存路径。源文件数或哈希不一致、抽样不一致、临床源记录覆盖不一致或命中偏移不能重放时，程序直接失败，不能悄悄跳过。

## 数据单位与字段

`patient_uid`是患者键；`VISIT_ID`是源系统一次就诊的分组键。本版的患者—visit是二元组 `(patient_uid, VISIT_ID)`，仅用于附近记录检索及压缩，不等于研究episode。缺失VISIT_ID时每条文书单独成组，防止把所有未知就诊混在一起。

`document_type`保留原始文书名称；`family`是代码归并的类型组。常用对应：

| family | 中文含义 | 文书名归并依据 |
|---|---|---|
| FIRST_COURSE | 首次病程 | 名称含首次病程，但转入/术后等更具体组优先 |
| FIRST_SENIOR_ROUND | 首次上级查房 | 名称含首次查房或首次…查房；可含主治、主诊、主任等不同职称 |
| WARD_ROUND | 其他查房 | 名称含查房，但不满足更具体组 |
| PREOP_BRIEF / PREOP_DISCUSSION | 术前小结/讨论 | 名称直接含相应字词 |
| CONSULT / MDT | 会诊/多学科记录 | 名称含会诊或MDT/多学科；MDT优先 |
| TRANSFER_IN / TRANSFER_OUT | 转入/转出 | 名称含相应字词；转入首次病程归TRANSFER_IN |
| ADMISSION / OUTPATIENT | 入院/门诊 | 名称含入院或门诊/就诊记录 |

`POSTOP`、`DISCHARGE`、`OPERATION`、`OTHER`保留上下文，不直接进入主候选文书类型组；其文字命中仍保留待核。类型归并优先级见 `document_family()`，不是对医院原始文书类别的官方重新编码。

`label`表示说了什么；`section`表示写在哪里；`role`表示该表述在文书中的角色；`target`表示所指操作。它们不能相互替代。

| role | 操作含义 |
|---|---|
| CURRENT_DOCUMENT_OPINION | 位于明示的分析/治疗计划等段落，或有查房指示/查看患者后示等连接；只是当次意见候选 |
| QUESTION / NEGATED_SCOPE | 字面肯定子串实际处在疑问/否定作用域 |
| EXPLICIT_HISTORY / EXECUTED_OR_HISTORY | 近邻有当时/既往等提示，或拟手术误连到已执行/术后表述 |
| NARRATIVE_UNRESOLVED | 位于现病史、诊断依据等叙述段；不直接认作新决策，也不认定它必为历史 |
| TEMPLATE_CONTEXT / CONTEXT_UNRESOLVED | 注意事项等模板语句，或上下文暂无法确定；保留队列 |

主候选需同时满足：决策类标签、当次意见候选角色、目标不是明确其他操作/完全未明、类型组在配置名单。NAT意向还须同文书有明确的当次手术指征/否定/重评语句；单纯化疗周期计划不自动算新手术评估。泛手术意向和无禁忌只能辅助召回。

## 时间与链的规则

`body_time`只解析正文开头含日期及小时分钟的记录头；日期缺少时分不补午夜。`system_time`优先源create_time，缺失时才使用admission_time，另存origin。两者均不是已验证的临床发生或可见时间。

`explicit_dates.discussion_date`单列讨论日期；`scheduled_operation_date`单列计划手术日，两者不替换正文/系统时钟，也不证明手术已执行。已实现正文/系统日期不一致、讨论日期/正文日期不一致标记。正文中检查时间晚于记录头等复杂问题仍需源文书复核，未由日期差自动修正。

候选链按患者—visit、观察时钟排序。相同文字状态在距**首条见证**24/48/168小时内可合并；状态改变则分开，正/负相反时加“意见冲突或可能更新”标记。这里的状态是检索分组（手术路径、不推进手术、待评估、新辅助），不是Agent Mode或专家Reference Standard。跨visit相同判断句仅加重复标记，不静默认作新判断或合并为一个episode。

局限：同结论的新重评在窗口内可能被合并；相同旧计划跨窗口可能被拆开；判断冲突可能是模板错误而非真实更新。候选链数用于安排复核，不能直接用作独立决策点或episode数。

## 私有输出与公开输出

私有目录默认 `local_artifacts/p2_localization_v0.3_20261020/`，被Git忽略：

- `notes.jsonl`、`hits.json`：全文、来源键、精确偏移、标签和角色/对象；
- `review_queue.json`：未直接进入主候选的决策类命中，逐条说明原因；
- `chains.json`、`pairs.json`、`cross_visit_repeats.json`：压缩链、配对及重复明细；
- `input_hashes.json`、`selection.json`：输入与抽样溯源；
- `semantic_annotations.json`、`semantic_review_ledger.json`及私有样本Markdown：分析者源文书复核和完整邻域；
- `aggregate.json`、`semantic_review_aggregate.json`：可单独检查后复制到docs的聚合输出。

禁止把私有目录、患者源文本或逐人名单加入Git。公开报告只保留定义、规则、聚合统计和抽样方法。合成回归测试不等于临床准确性验证；本轮12份诊断性目的抽样参与了规则修正，不能报告其为独立验证集或宣称8/12为准确率。

## 相对v0.2的差异

旧7类标签原模块未改；本版使用明确的新字典及角色字段，历史回述成为独立属性，不再用一个PAST标签强行概括。具体切除计划涵盖不带根治、不同词序、次全切、切除手术及不带术字的名称。疑问作用域、诊断考虑误连、其他操作、NAT病名/周期、文书段落、医生指示句及时间字段有各自处理。

输入从“正文含胰等字词”扩为同批患者经名称筛选的全部上下文文书；仍保留完整源记录计数核验。计数从“每visit每标签第一次”改为“保留状态差异的邻近候选链”。两个版本都保留，不能将总数变化归因于某一条修补。
