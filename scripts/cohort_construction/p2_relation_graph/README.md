# Decision Graph v0.2：mention、潜在事件与关系提议

2026-10-23更新：算法骨架保留v0.2，验证基础设施升级v0.3。当前评估、独立重复、标注材料和评审台账使用 [VALIDATION.md](VALIDATION.md)；下文原评估段落属于初版说明，不能覆盖v0.3对source_windows、事件对齐和FP的要求。旧release仍是冻结v0.2记录，新版本回执另存。

本模块接在冻结 Decision-IR v0.1/run07 之后，不扩展其词法、不改变P0/P1、不生成正式episode或医生Reference。run07是冻结候选生成器，其高召回尚未被验证。

## 执行链

1. 核对每个原文单元的来源键、全文哈希及Unicode坐标。
2. Kimi-3 在给定单元内提议 Decision/Evidence/Action/Feedback Mention。每个signature字段保存 stated / not_mentioned / uncertain、值及逐字引用。管理问题可以规范化，但它仍只是索引；原文另外保留。
3. 程序把唯一逐字引用转换为来源坐标；不让模型计算字符数。不存在、重复而无法唯一定位、源外的引用拒绝进入图。模型响应不合格时登记失败单元。
4. 同患者候选按管理问题的字符相似度、主题和时间排序，取Top-K。这里没有embedding，也未证明该排序的召回。未知管理问题不会因字段缺失而被排除。相同/不明时间只用于共指复核，不产生有向时间关系。
5. 首先判断same_event / different_event / unrelated / uncertain。相同动作、复制措辞、时间接近或未写新证据均不足以自动合并；same_event要求模型指出对同一判断的正面引用依据。共指成分仅形成 `DecisionEventHypothesis`，所有mention均保留。
6. 对不同事件假设提议reaffirm / revise / supersede，另对Evidence→Decision、Decision→Action、Action→Feedback、Feedback→Decision提议带类型的关系。事件候选用代表mention排序，分类上下文包含全部成员及两端之间的文书，包括零命中文书。
7. 核对关系所需证据、类型、时间及动作兼容；处理共指传递性冲突、合并后事件内修订和有向环。冲突转uncertain/REVIEW，不输出替代“真值”。

`events`保存决策事件假设，`auxiliary_events`保存Evidence/Action/Feedback事件假设，`event_endpoint_map`让每条事件边都有明确节点。辅助事件当前为逐mention单例，尚未解决其跨文书共指。多判断单元和词法执行/反馈线索未获得对应类型节点的情况另记review_flags；这些是待核覆盖问题，不直接认定临床或源资料错误。

`future_plan`表示行动在未来，不自动表示当前判断不存在；`historical`记录的事件时间不能直接用文书代理时间排序。有向关系目前要求严格更早的代理时间，同文书/同时间关系会进入时间待核范围，尚未恢复文内事件时序。

模型看到的材料是研究者关系重建上下文，可能含当次选择；不是临床盲评或pre-decision state。未知时间文书、超过上下文预算的文书及Top-K过滤分别记录。核心端点片段优先保留，因此上下文预算是软限额。

## 本地运行

在项目根目录运行，Python 3.12、requests、jsonschema。输出目录必须全新，并位于被Git忽略的 `private/` 或 `outputs/`。

```sh
python -m scripts.cohort_construction.p2_relation_graph.run \
  --demo --output private/graph_demo_prepare

python -m scripts.cohort_construction.p2_relation_graph.run \
  --demo --execute --top-k 1 --max-pairs 12 \
  --output private/graph_demo_model

python -m scripts.cohort_construction.p2_relation_graph.run \
  --units private/p2_decision_ir_v0.1_20261022_run07/nodes.jsonl \
  --sources private/p2_dynamic_pathway_probe_v0.1_20261021/review_patient_context.jsonl \
  --output private/graph_development_prepare
```

不加 `--execute` 仅生成词法包装、候选清单和待核队列，signature均未知；不得解释为模型已判读。`--max-units`按输入顺序截取，只是运行预算，不是随机抽样；`--max-pairs`是三个阶段合计的模型关系请求上限，超过预算的关系保留NOT_RUN。模型调用失败时写出可审计结果并返回非零状态。

来源原文、节点、提议、逐单元状态、逐候选上下文覆盖与模型请求回执在 `graph.json`；汇总在 `summary.json`；版本/输入/代码/输出哈希在 `manifest.json`。缓存只能在忽略目录，由CLI约束。直接调用Python适配器的使用者也必须遵守此目录约定。

## 验证与校准

`tests/test_graph.py` 的RG编号登记在 `regression_catalog.json`。回归执行必须包含原95项测试，不只运行新增测试；`test_receipt.json`保存本次完整回执。

`evaluate.py`接受人工独立裁定、范围完整、已对齐mention身份的gold，计算成对共指P/R/F1、关系macro-F1及混淆、候选边召回、正确类型边召回、指定范围图完整性、review率和coverage–precision描述曲线。未提议的gold边仍计入分母。对齐需要人工审核；本版不把字符串模糊匹配当成event gold。无预测/无gold的分母返回null。

gold文件包含 `annotation_status=ADJUDICATED`、`origin=independent_human`、`complete_within_declared_scope=true`、`scope`、`annotator_ids`、`mention_to_event`和`relations`。relations每项含stage/from/to/relation；同一对按阶段区分。完整性与标注来源声明需要项目审计，不能仅靠JSON字段证明。测试允许 `synthetic_fixture`，但不能用于临床校准。

尚无独立人工gold，所以 `auto_accept_enabled=false`，全部关系保持REVIEW。score只表示支持证据特征数，**不是置信概率**。没有训练校准器、选择部署阈值或证明“80%自动化”。`evaluate`曲线不做总体推断、不校正抽样权重；开发校准与锁定验证须分开，分层抽样和患者内相关性另按方案处理。

## 当前边界

- 冻结检索遗漏的独立判断仍可能漏掉；上下文引入零命中文书不等于完成节点召回验证。
- 精确引用验证能发现伪造或错位文字，不能证明模型对引用的解释正确。
- 同动作/近时间/同文本不会自动归为同事件，代价是共指自动覆盖可能偏低；需gold测量。
- 稳定ID包括规范化signature；跨算法版本评估必须先独立对齐，不能默认ID不变。
- 独立问题域、ProblemThread、正式episode、历史可见性和医生Acceptable Mode Set均未自动确定。
- API实际使用 `kimi-k3`；没有embedding模型或第二模型投票，不声称已实现这些建议。
