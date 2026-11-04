# 研究者原文材料审核应用 0.1.0

此应用处理含未遮原文的研究者任务，**禁止作为医生盲评文件发放**。它不生成医生许可包，不冻结临床方案，不认证审核者实际身份、独立性或临床正确性。

## 已实现的流程

1. 从既有任务读取 manifest、compiled、AI 准备草稿和研究者准备回执，逐项绑定规范 JSON hash、Checkpoint target 和每个原文区间；候选事件存在时另行绑定其 hash。
2. 可选从用户明确指定的原始 FHIR 文件补入事件范围外的已交付来源原文。程序先核对任务 `input_receipt.json` 中的整文件字节 hash，再使用既有 source importer 核对患者绑定和来源原文 hash。只导入 source resources，忽略 FHIR 研究 Basic 注释。
3. 生成仅含本次分配内容的单文件离线研究者 HTML。界面按当前区间、当前来源、当前模型字段或数组单项显示文字，提供来源检索、未决筛选和进度计数。全部原文通过 `textContent` 呈现，无外部网络、上传或浏览器自动保存。
4. 实际审核者填写自己的标识、身份类型、协议版本和真实审核时间。新响应初始 PENDING；独立性及六项检查均不勾选；每段与来源覆盖均为 UNRESOLVED；原 AI 准备草稿只读可追溯。程序生成新的记录编号，不沿用 AI 草稿编号。
5. 审核者可按 SemanticAudit 0.2.0 填写逐段处置、来源覆盖、时间证据、问题及支持引文、后续行动；导出新响应以保留进度，之后可导入同一绑定任务恢复。导入已有响应时，其身份和声明仅来自导入文件，程序仍不认证人员。
6. Python 回收端核对任务、输入和响应 hash，调用 `validate_semantic_audit` 独立核验，排他另存新的 SemanticAudit 文件。原任务、原 compiled 和 AI 草稿均不覆盖。

界面加载原文不等于审核完成。任务未载入原文的来源不能在当前响应中声称已审核，覆盖必须保持 UNRESOLVED；应创建补齐原文的新任务。这里的“未载入”是 HTML 任务内容不足，不等于医院无原报告或交付资料缺失。实际来源缺口可记录在问题、理由及依据中；对于已有可阅读来源中引用的原报告缺口，仍支持 REQUIRED_MISSING / DELIVERY_NOT_FOUND。

ACCEPT 仅用于已有 KEEP/MASK；REVIEW 必须明确提议 REVISE 或继续 UNRESOLVED。PENDING/FAIL 可记录修订；PASS 必须接受当前分区、无修订和未决、完整范围、明确独立性声明和六项检查、无开放阻断问题。审核者若将纳入来源改为 REVIEWED_EXCLUDED，或将范围外来源改为 REVIEWED_INCLUDED，可在 PENDING/FAIL 保存提案；PASS 须先建立新 manifest、分区并重新审核。REVISE 仍需在既有 `revise_partition` 流程另存、重编并重新审核；本应用不自动执行重编或放行。

## 文件命令

在项目根目录运行；所有真实任务 HTML、分配 JSON 和响应必须保存在**本项目** `private/` 下。构建目录和接收文件均不得已经存在。

```sh
python -m scripts.cohort_construction.p2_checkpoint_material_review_app.build \
  --assignment-dir private/原研究者任务目录 \
  --assignment-id 实际任务编号 \
  --source-fhir private/原FHIR来源文件.json \
  --output private/新研究者审核交付目录
```

`--source-fhir` 可省略，届时事件范围外来源仅显示目录，覆盖保持 UNRESOLVED。指定时必须匹配原任务输入回执的 hash。生成 `researcher-review.html`、`researcher-assignment.json`、`build-receipt.json`，原任务文件不变。

```sh
python -m scripts.cohort_construction.p2_checkpoint_material_review_app.receive \
  --assignment private/新研究者审核交付目录/researcher-assignment.json \
  --response private/实际审核者导出的新响应.json \
  --output private/新回收目录/semantic-audit.human-v1.json
```

接收成功仅说明记录满足当前程序合同。即使状态为 PASS，也不表示程序已证实人员独立性、临床正确性、来源完整性或临床表单冻结；不会生成医生包。

## Python 接口

- `build_assignment(compiled, manifest, draft, preparation_audit, *, assignment_id, selected_event=None, coverage_sources=(), source_fhir_sha256=None)`：创建独立绑定对象。
- `validate_assignment(assignment)`：返回已核对 documents、loaded_sources 和 unloaded_source_ids。
- `build_researcher_html(assignment)`：返回单文件 HTML 字符串；调用者仍须遵循 private 内容保存要求。
- `build_files(assignment_dir, *, output, assignment_id, source_fhir=None)`：受 private 路径和排他创建保护的文件桥接。
- `receive_submission(assignment, response)`：权威核验并返回新的 SemanticAudit；不修改输入。
- `receive_files(assignment_path, response_path, *, output)`：权威核验并另存新文件。

各输入使用规范 JSON 字符串作为绑定对象，浏览器直接对原字符串计算 SHA-256，避免历史 LLM 浮点数在 JavaScript 重序列化时改变 hash。响应的原文坐标是 Unicode code point、零起点、半开区间；CRLF 不归一化。

## 回归

`regression_issue_map.json` 登记 CE-MRA-001～007 及必需反例/正对照。全部测试仅使用虚构材料。

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \
  scripts/cohort_construction/p2_checkpoint_material_review_app/tests
```

实际 Chrome 用例验证空身份拒绝、REVIEW不能ACCEPT、修订不自动PASS、来源覆盖不自动完成、实际下载→Python回收、绑定恢复、错hash拒绝、原文HTML不执行、桌面/移动布局和零HTTP请求，并在测试临时目录保留两张截图及浏览器回执。
