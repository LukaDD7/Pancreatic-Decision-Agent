# 研究 Checkpoint 的本地 FHIR R4 交换接口

版本 `0.1.0-draft`。程序把已冻结医生许可包转换为真正的 FHIR R4 `Bundle` JSON，并支持医生意见返回及研究者图引用。`https://checkpoint-research.invalid/fhir` 是明确不可发布的占位 canonical。这里提供的是本地草案契约，尚未发布为 HL7 profile 或通过术语服务器/FHIRPath/临床语义认证。`contracts/local-extension-definitions.bundle.json` 包含 24 个实际 `StructureDefinition(status=draft)`，明确扩展上下文、值类型及复合绑定字段；没有生成 snapshot，尚未用 HL7 validator 执行 differential/profile 验证。

本包不提取临床事实，不判断患者可切除性，不冻结临床 Mode 或动作字典，也不把程序候选 Episode 当作临床事实。实际表单必须由研究方案提供已冻结、版本化的 `Questionnaire`；合成示例的代码仅供软件验证。

## 医生专用入口

```python
from scripts.cohort_construction.p2_checkpoint_fhir import (
    doctor_bundle_from_packet, packet_from_doctor_bundle,
)
bundle = doctor_bundle_from_packet(frozen_packet)  # 可选 episode_id 必须已有获准标识
restored = packet_from_doctor_bundle(bundle, expected_packet_sha256=trusted_assignment_packet_hash)
assert restored == frozen_packet
```

医生 `Bundle.type=collection` 仅包含一个当前 Checkpoint 的 `Basic`、逐材料 `DocumentReference` 和原样冻结 `Questionnaire`。`Basic` 使用自定义 typed extension 保存 Checkpoint、资料包版本、许可策略、内容 hash、表单 hash 及资源引用。资料正文仅以内联 base64 UTF-8 文本出现；遮蔽文本、后续 Checkpoint、研究者全图、附件外部 URL、`contained` 资源和未知扩展均不准进入医生包。

`DocumentReference` 不是原文全量文书；它明确承接**本次许可视图**。材料中的通用占位符不保留被遮蔽文字。空许可文本用 `Attachment.size=0` 且省略 `data` 表示，以避免 FHIR 不允许的空 primitive。`Attachment.hash` 在 R4 表示 base64 SHA-1，所以本包不使用它保存 SHA-256；SHA-256 使用本地 `content-sha256.valueString` 扩展。

解码器先解析本地引用，重建医生许可包，校验所有来源视图、临床内容、资料包和表单 hash，调用问卷支持子集验证器，再重新编码并比较整个 JSON 对象。未知字段、未知资源、冗余附件、额外脚本数据、重复引用、外部引用和对象篡改在显示前被拒绝。CLI 的原始 JSON 读取器也拒绝重复对象键和非有限数；以字典调用 API 的上游读取器须执行同样检查，避免常规 JSON 解析静默丢弃重复键中隐藏的原文。条目和扩展顺序也是本地契约的一部分。

`questionnaire-canonical-json` 是同一许可 Questionnaire 的精确序列化辅助字段，处理 Python `1.0` 与 JavaScript `1`、极小小数表示差异。浏览器核验该串的 UTF-8 hash、解析后与可见 Questionnaire 作语义比较，并在重建包 hash 时插入原串。它不得携带第二份表单或其他数据；Python 接收器拒绝重复键、非有限数及与可见资源不等价的内容。材料和包 hash 延续既有编译器算法，不静默变更。

hash 证明内容绑定和传输完整性；攻击者若重新生成所有 hash，自洽包仍需与独立可信分配台账比较。`expected_packet_sha256` 用于这个外部绑定。可选 `episode_id` 只是额外许可的研究标识，不包含其他 Checkpoint，也不证明临床 Episode 已审定。

## 医生意见返回

```python
from scripts.cohort_construction.p2_checkpoint_fhir import (
    response_bundle_from_submission, submission_from_response_bundle,
)
response_bundle = response_bundle_from_submission(submission, trusted_packet)
restored = submission_from_response_bundle(
    response_bundle, trusted_packet,
    expected_reviewer_assignment_id=trusted_assignment_id,
)
```

返回包只含 `Basic(code=research-review-submission)` 和 `QuestionnaireResponse`，不重复回传原文或整份表单。接收程序校验资料包、表单、目标 Checkpoint 绑定，调用 `p2_checkpoint_review.validate_submission` 核验答题语义，输出既有 `ReviewSubmission` 结构供台账使用。未完成状态、未知题目/代码、条件禁用题目答案和版本不符由问卷验证器拒绝。

`response-canonical-json` 同样只包含这份评审响应；浏览器或 Python 序列化均可，`response-content-sha256` 核验其精确 UTF-8 字节。解码器拒绝重复 JSON 键、隐藏数据和与 QR 不等价的辅助串。可选可信 assignment ID 比较不替代评审者身份认证或伦理/研究分配管理。

## 研究者专用图入口

```python
from scripts.cohort_construction.p2_checkpoint_fhir import researcher_bundle_from_graph
research_bundle = researcher_bundle_from_graph({
    "graph_id": "a-versioned-graph", "graph_version": "1",
    "events": events, "episodes": episodes, "relations": relations,
})
```

研究包使用 `Basic` 承接 `research-graph`、`research-management-record`、`research-candidate-episode` 和 `research-graph-relation`。每项都标记 `audience=researcher-only` 和 `assertion-status=algorithm-candidate-not-clinical-reference`；原研究 JSON 与 hash 保留在研究专用扩展中。成员、候选 Checkpoint 和关系两端使用实际本地 `Reference`。不生成临床 `EpisodeOfCare`，也不把 LLM 分类变为医生 Reference。

`same_management_process` 表示复核阶段 LLM 提议两条管理记录属于同一连续管理过程，程序核验证据并决定候选 Episode 是否连通；`same_event` 表示 LLM 提议两条表述指向同一具体临床管理判断，程序核验来源及身份；`evidence_comparison_previous` 仅指定当前记录用于患者信息变化判断的既往比较对象，单独存在不证明同一 Episode。后者仅在输入 `relations` 显式提供时导出，程序不根据模糊文字制造关系。完整字典见 `researcher.RELATION_DEFINITIONS`。

研究包不能被医生入口导入。它可能包含研究者全部原文、标签、LLM 输出和后续内容，必须留在研究端。

## CLI 与验证

```sh
python -m scripts.cohort_construction.p2_checkpoint_fhir doctor-export --input frozen-packet.json --output new-doctor-bundle.json
python -m scripts.cohort_construction.p2_checkpoint_fhir doctor-import --input new-doctor-bundle.json --output roundtrip-packet.json --expected-packet-sha256 TRUSTED_HASH
python -m scripts.cohort_construction.p2_checkpoint_fhir response-export --input review-submission.json --packet frozen-packet.json --output new-response-bundle.json
python -m scripts.cohort_construction.p2_checkpoint_fhir response-import --input new-response-bundle.json --packet frozen-packet.json --output accepted-submission.json --expected-reviewer-assignment-id ASSIGNMENT_ID
python -m scripts.cohort_construction.p2_checkpoint_fhir research-export --input researcher-graph.json --output new-researcher-bundle.json
```

输出以独占创建模式写入，拒绝覆盖已有文件。所有 API/CLI 均离线，不调用临床 LLM。正式临床资料还需获准的独立材料审核与可信分配台账。

`validate_fhir_r4` 使用仓库既有完整 R4 JSON Schema：`private/p2_auto_checkpoint_v0.1_20261024/standards/fhir.schema.json`。测试同时执行包级 allowlist、完整 JSON Schema、篡改及跨语言对照。需要 Python `jsonschema`，测试另外需要 `pytest`。机械通过不证明临床资料范围正确或专家意见正确。

规范依据：[FHIR R4 Basic](https://hl7.org/fhir/R4/basic.html)、[Bundle](https://hl7.org/fhir/R4/bundle.html)、[DocumentReference](https://hl7.org/fhir/R4/documentreference.html)、[Questionnaire](https://hl7.org/fhir/R4/questionnaire.html)、[QuestionnaireResponse](https://hl7.org/fhir/R4/questionnaireresponse.html)、[Extensibility](https://hl7.org/fhir/R4/extensibility.html)、[Attachment.hash 定义](https://hl7.org/fhir/R4/datatypes-definitions.html#Attachment.hash)。
