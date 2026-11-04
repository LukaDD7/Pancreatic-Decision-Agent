# 目标Checkpoint绑定的原文分割编译器 v0.1.0／语义审核合同 v0.2.0

2026-10-28。此包新增独立、纯离线的接口实现，不修改现有发现阶段/复核阶段LLM提示词、Checkpoint资格、候选Episode组装或患者成员。全部测试使用虚构文字。没有重跑临床模型，也没有将开发测试结果解释为临床性能改善。

程序负责逐字定位、字符区间运算、不可变来源核对、分割执行及医生材料投影。LLM或人工仍负责提议各原文区段应保留、遮蔽或待核；独立语义审计仍负责判断提议是否符合冻结研究方案。**机械审计PASS不证明原文中的目标临床决策已正确遮蔽。**

## 已实现的数据接口

| 契约 | 处理对象及用途 | 可见范围 |
|---|---|---|
| `SourceManifest` | 冻结原文、来源版本、原文UTF-8 hash、字符数；不规范化换行或Unicode | 研究者 |
| `SourceAnchor` | 定位工具返回的具体原文位置；绑定来源版本及hash | 研究者/重建LLM |
| `SplitProposal` | 针对一个Checkpoint、决策任务和资料可见规则版本的KEEP/MASK/REVIEW提案 | 研究者/重建LLM |
| `CompiledPartition` | 覆盖每份来源全部字符的互斥分区、原文切片和机械报错 | 研究者 |
| `SemanticAudit` | 独立审阅者对特定分割产物的语义审计、独立性声明及结果 | 研究者 |
| `FrozenQuestionnaire` | 外部提供的固定版本FHIR Questionnaire及hash；不由模型动作生成 | 医生许可表单 |
| `FrozenCheckpointPacket` | 独立构造的医生许可材料、表单和hash绑定；不复制研究者对象 | 医生 |
| `ReviewSubmission` | 医生实际QuestionnaireResponse及Checkpoint/资料包/表单版本绑定 | 评审回收 |

`schemas/*.schema.json` 是 Draft 2020-12 JSON Schema。`validate_contract` 仅实现这些本地文件所使用的有限关键词，不是通用JSON Schema/FHIR验证器；可用独立`jsonschema`检查这些文件。

`target`必须给出`checkpoint_id`、`decision_task_id`、`visibility_policy_version`。同一段原文对不同Checkpoint可能有不同呈现资格；没有全患者永久mask，也不从之后的决策结果推改较早原文。

## SemanticAudit 0.2.0：独立材料审核，不是模型结论

`semantic_audit.validate_semantic_audit(compiled, audit)`核验每个审核判断都精确绑定当前compiled hash、来源hash、Unicode code point半开坐标及逐字原文。审核者须记录实际来源范围、临床决策时间证据和不确定性、跨文书同次决策回述、来源覆盖和具体问题归因；不能用完整模板命中替代实际审核。

- `ACCEPT`只接受已有的KEEP或MASK，不能把REVIEW直接变成许可；
- `REVISE`必须明确KEEP或MASK，但整个审核仍可为PENDING或FAIL；
- `UNRESOLVED`不产生处置许可，经`revise_partition`重编译后仍为REVIEW；
- `research_preparation`表示AI/研究准备草稿，必须PENDING且不作独立性声明，不能作为独立人工审核；
- PASS要求独立性声明、完整审核范围、六项检查完成、每段明确处置且无开放阻断问题。程序核对这些记录，不能证明审核者的实际独立性或临床判断正确。

`revise_partition(manifest, proposal, audit, compiled=...)`返回新的机械分区，旧manifest、提案和compiled不覆盖，新分区仍为`semantic_audit_status=NOT_ASSESSED`，须重新独立审核。`write_assessment`使用独占创建保存研究者审核记录，不提升状态。

## 定位与分割规则

1. LLM返回`source_id`和`quote_locator.exact_quote`，必要时给逐字相邻`prefix/suffix`；无需填写字符坐标。程序返回全部匹配位置，重复引文不会自动采用第一处。
2. 程序或定位工具也可返回`SourceAnchor.anchor_id`。提案引用该ID时，必须同时交入工具anchor目录；编译器重新核验原文、坐标和hash。
3. 已有程序解析的坐标可通过`resolved_span`承接，但必须同时提供完全等于`source[start:end]`的`exact_quote`。这不是要求LLM数坐标。
4. 坐标统一为Unicode code point、零起点、半开区间`[start,end)`。Python字符串切片直接符合；JavaScript普通`string.length/slice`是UTF-16，不能直接充当本接口坐标；JS端须按code point转换。CRLF保留两个字符，emoji按code point计数，组合字符不作NFC/NFKC。
5. 每份原文每个字符必须由唯一提案段覆盖。未覆盖字符生成`REVIEW/UNASSIGNED_GAP`；重叠部分生成`REVIEW/OVERLAPPING_PROPOSALS`。即使两个重叠提案都为KEEP，也要待核。零匹配、未知anchor、错误坐标不会被修回或默认KEEP。
6. 来源hash、版本、来源集合或manifest ID不符时直接拒绝编译。引文未定位、空洞和重叠则输出覆盖完整原文的研究者分区，同时机械审计FAIL。
7. 显式REVIEW段可通过结构性机械核验，但阻断医生包冻结。修改提案后应增加版本，保存旧输入、旧分割产物和新旧diff；CLI使用独占创建，不覆盖历史文件。

原始内容始终保存在研究者产物中，切片拼接须能重构同hash原文。`freeze_checkpoint_packet`不会修改该产物；只把KEEP文字复制入新的医生对象，连续MASK合并成一个中性`[资料按方案遮蔽]`占位符，不带被遮文字、被遮字数、原文坐标、分割理由、模型分类或研究者原文附件。

## 调用示例

```python
from scripts.cohort_construction.p2_checkpoint_packet import (
    build_source_manifest, compile_partition, locate_exact_quote,
)

manifest = build_source_manifest("M-fictional", [{
    "source_id": "S-fictional", "source_version": "1",
    "text": "观察甲。决定乙。实施丙。",  # 纯虚构
}])

anchors = locate_exact_quote(manifest, "S-fictional", "决定乙。")
# anchors.status: UNIQUE / AMBIGUOUS / NOT_FOUND；提供全部候选坐标和anchor_id

proposal = {
    "schema_version": "0.1.0", "proposal_id": "P-fictional", "proposal_version": "1",
    "manifest_id": "M-fictional",
    "target": {"checkpoint_id": "CP-fictional", "decision_task_id": "fictional-task",
               "visibility_policy_version": "synthetic-only"},
    "source_bindings": [{key: manifest["sources"][0][key]
                         for key in ("source_id", "source_version", "sha256")}],
    "segments": [
        {"segment_id": "p1", "source_id": "S-fictional", "disposition": "KEEP",
         "quote_locator": {"exact_quote": "观察甲。"}},
        {"segment_id": "p2", "source_id": "S-fictional", "disposition": "MASK",
         "quote_locator": {"exact_quote": "决定乙。实施丙。"}},
    ],
}
compiled = compile_partition(manifest, proposal)
# compiled.mechanical_audit.status == PASS
# compiled.semantic_audit_status == NOT_ASSESSED
# 此时不产生医生包，也不自动产生语义审计PASS。
```

`freeze_checkpoint_packet(compiled, semantic_audit, frozen_questionnaire, packet_id=..., packet_version=...)`要求：

- `compiled_sha256`和原文重构hash核对通过，机械审计PASS，且无REVIEW段；
- 外部语义审计精确绑定相同`compiled_sha256`及`target`，结果PASS，声明独立性，并完成分割语义、许可信息范围、跨文书同次决策回述、决策时间边界、必需来源覆盖及逐段评估；
- 表单资源是`Questionnaire`，`status=active`，canonical/version/hash相符，调用方明确提供`frozen=true`。

调用方必须独立管理审阅者身份、实际独立性、方案冻结和医生发放。程序只能核对这些声明和绑定，不能证明声明真实。SHA-256用于一致性与版本绑定，不是数字签名，也不是访问控制。没有虚构出真实临床Mode/action代码或专家方案。

`make_review_submission(packet, questionnaire_response, submission_id=..., reviewer_assignment_id=..., submitted_at=...)`保留医生实际多选答案；不预填模型答案。`validate_review_submission`只核验结构和资料包/固定表单绑定，**尚不验证全部FHIR约束、每题必填、条件显示或临床代码合法性**，这些须由未来HTML应用和评审回收校验器按冻结Questionnaire实现。

## 离线CLI

在仓库根目录运行，输入输出均为本地JSON：

```bash
python3 -m scripts.cohort_construction.p2_checkpoint_packet compile \
  --manifest source-manifest.json --proposal split-proposal.json \
  --output compiled-partition.json

python3 -m scripts.cohort_construction.p2_checkpoint_packet freeze \
  --compiled compiled-partition.json --semantic-audit semantic-audit.json \
  --questionnaire frozen-questionnaire.json --packet-id PK-1 --packet-version 1 \
  --output doctor-packet.json
```

机械审计FAIL时，`compile`仍保存研究者审计产物并返回退出码3；结构/绑定错误返回2，不生成包。已有输出文件一律不覆盖。`locate`和`validate-response`的参数见`--help`。若从原始文本文件构造manifest，应用`open(..., newline="")`保留CRLF，避免在hash之前已被Python默认换行读取改写。

## FHIR和HTML对接边界

此包实现的医生许可包是JSON接口。当前适配由`p2_checkpoint_fhir`实现R4 Bundle，`p2_checkpoint_review_app`实现单文件离线HTML和回复导出；此处不另定义FHIR profile。`fhir_version=4.0.1`声明适配目标。来源文书派生视图可映射为FHIR R4 `DocumentReference`的许可`text/plain`附件，来源转换另以研究者可见`Provenance`保留；实际Checkpoint、Episode、关系和评审分配仍须定义资源约束、扩展及验证规则。

HTML应用只读取`FrozenCheckpointPacket.materials`和固定`Questionnaire`，按文本渲染，验证`packet_sha256`、`clinical_content_sha256`、每份派生正文hash及表单hash。不得读入`SourceManifest`或`CompiledPartition`再靠CSS隐藏；不得给医生端添加可回溯到研究者全文的附件。HTML中也不能内嵌未来Checkpoint内容。离线导出的QuestionnaireResponse应回装为`ReviewSubmission`，由协调者核验分配及接触史后进入台账。

`p2_checkpoint_materials`已接入实际候选事件，但只准备研究者可见待核分区；当前没有真实患者的独立语义PASS、冻结临床表单或医生许可包。上述FHIR/HTML验证使用虚构材料，不构成临床质量结论。

## 回归与已知问题登记

```bash
python3 -m unittest discover -s scripts/cohort_construction/p2_checkpoint_packet/tests -v
```

`regression_issue_map.json`登记稳定问题ID和必需正负例。当前反例覆盖emoji、CRLF、hash冲突、重复引文、错误坐标、来源/目标/版本错误、空洞/重叠、显式REVIEW、机械PASS但漏遮临床决策、医生包未携带被遮文字及评审表单绑定。无`jsonschema`时仅跳过独立标准验证两项；其余纯stdlib测试全部执行。不存在任何网络调用。
