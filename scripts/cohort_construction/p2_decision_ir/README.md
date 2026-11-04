# Decision-IR v0.1：来源定位与关系复核

这是 P2 开发工具。输入原始文书及来源字段，输出连续原文中的候选判断、执行提及、反馈提及、指导语句和待核关系。既不改变 P0/P1，也不按旧 P1 PASS 过滤，不生成正式 episode、专家 Reference 或临床错误标签。

## 文件职责

- `extract.py`：完整标点单元、旧检索标签加扩展措辞、语境优先级、来源坐标及时间、候选邻接关系。新增正则集中在 ADDED，动作与主题分别在 ACTION_PATTERNS、TOPICS。
- `node.schema.json`：本地研究结构约束；不是 HL7 FHIR 资源或临床本体。
- `run.py`：校验冻结 49 人开发池哈希，逐文书处理，验证源坐标，分层抽样，保存全量索引与五例复核卡。
- `fhir_links.py`：可选的原文版本交叉引用；要求显式患者别名映射、来源编号、全文哈希和跨度一致。
- `compare.py`：首轮与最终轮的逐节点差异、原固定抽样的当前结果、N01 FHIR 连接与哈希回执。
- `tests/test_ir.py`：IR001–IR032 合成反例及对照。用例名即稳定问题登记，完整编号也见 `regression_catalog.json`。
- `release.json`：基线、正式开发运行、代码/输入/产物哈希及测试回执。

## 核心约定

一个节点是一段连续原文（句号、分号、换行界定），可能同时含状态、几项动作及条件。不能把节点数称为临床决策点数。`DECISION_CANDIDATE` 是有判断/安排措辞的候选；`CURRENT_STATEMENT_CANDIDATE` 只是语境优先级，不保证针对根治性切除。`surgical_pathway_relevance=UNADJUDICATED` 对所有节点适用。单独营养医嘱额外标记 `SUPPORTIVE_CARE_PATHWAY_LINK_REQUIRED`，保留供后续联系上下文。

原文不被改写；偏移为 Python Unicode 字符坐标 [start,end)，不是字节位置。source_sha256 对完整源文书编码 UTF-8 后计算。动作字段叫 action_mentions：提到 PTCD 并不意味着本次决定 PTCD。执行/反馈线索可以与判断共存；主 role 只用于复核队列，不表示一句话只有一个语义。

当前性按具体语句、文书类型及语境提示判断。POSTOP 也可能出现新的减黄安排，不能整体排除；住院/出院叙述、知情同意、历史与模板优先留待核。否定判断仍是判断；疑问不能成为肯定适应证。日期和麻醉措辞受拟/定于/计划等未来算子约束。分句中的既往证据不会抹掉另一分句明确的当前结论。

正文时间、创建时间分别保留；排序使用正文时间，缺失时才用创建时间，二者均为代理时钟。无时区按本院 +08:00；日期精度不能补成零点。同时间不指定先后；相邻只是提议关系，不是因果证明或真实首次决策时间。

每个同患者、同具体主题节点提议两类前项：最近更早的主题节点；若本节点为判断候选，再找最近更早的判断候选。泛称“手术”不单独连边。不同就诊可连边但明确标记；visit_id 仅为源就诊索引，不是 episode。完全相同措辞也分别保留，不按 24/48 小时合并。不得把跨次相邻关系直接宣称为同一治疗过程。

输出含原文、当次选择和后续内容，是研究者复核材料，不是 Agent/医生盲评输入。盲评 state 仍需核对具体动作边界与可见时间。专家可接受动作评价不要求与历史动作一致；历史未提供某行动结果不阻止本模块检索，也不会生成 action unacceptable。专家 confidence 保留在方案；这里的 confidence=null 仅表示程序没有校准置信度，不替代或删除专家问项。

## 复现

在独立 Git 仓库根目录，使用 Python 3.12 + jsonschema；本地冻结输入需已存在。运行目录必须全新，程序拒绝覆盖旧结果。

```bash
python -m unittest discover -s scripts/cohort_construction/p2_decision_ir/tests -v
python -m scripts.cohort_construction.p2_decision_ir.run --repo "$PWD" --output "$PWD/local_artifacts/decision_ir_new_run"
python -m scripts.cohort_construction.p2_decision_ir.compare --repo "$PWD" --baseline "$PWD/local_artifacts/p2_decision_ir_v0.1_20261022" --current "$PWD/local_artifacts/decision_ir_new_run" --output "$PWD/local_artifacts/decision_ir_new_audit"
```

`compare` 的 N01 适配检查需要既有 `n01_fhir_replay_v0.1_20261022/researcher_event_envelopes.jsonl`；`run` 提取本身不要求 FHIR 转换、服务器、网络或额外反事实结果。当前复用旧 FHIR 模块的纯时间解析函数，并在提取完成后读取人工 N01 锚点作覆盖检查，不以锚点控制检索。

固定抽样：种子 20261022，当前判断、待核判断、执行、反馈、零命中文书五层，每层随机选四名不同患者，再每人随机选一条；允许跨层同患者。49 人来自此前目的性检索/开发，不是 P0 的随机验证集。修改后的新队列保持 PENDING；只有本轮逐项查看的首轮固定 20 条写入分析者复核账，不伪装为医生裁定或独立准确率。

## 已知边界

- 词法规则仍可能漏掉间接判断、纯检查建议、跨句条件及操作对象；零命中保留索引，不能排除患者。
- 叙述时态、医疗措施所指对象、常规支持与手术路径的联系仍需看原文；局部提示不是完整语义解析。
- 主题邻接尚未判定 responds_to / implements / supersedes；重复、再次确认和计划变更必须复核。
- 本轮不自动生成正式 episode，也不报告独立决策点中位数。FHIR 交叉引用只证明相同文本，不证明历史时点可见或临床正确。
