# v0.6 离线独立原文验证执行接口 0.1.0

本模块准备研究者开发抽查任务，核验、封存实际人员填写的原文重建与模型对照。代码不判断医学语义是否正确，不产生医生许可包、Acceptable Mode Set 或临床 Reference。当前真实任务若无人回收，必须报告独立人工完成数为 0、语义性能未测。

## 范围、抽样与来源

`prepare` 读取冻结开发输出中的原文 FHIR Bundle、原始 `unit_inventory.json`、`discovery.json`、`decision_events.json`、候选 Episode 与程序关系。旧迁移病例可从 `summary.original_input_directory` 寻找原运行库存；找不到库存、库存原文与 Bundle 版本不符、发现事件的两份副本内容不一致或单位来源绑定错误时拒绝准备。

四层依次为：有发现提案的完整单位、成功请求但零发现的完整单位、包含被发现阶段 LLM 拒绝提案的完整单位、未进入发现的支持来源。后两层不表示临床团队认为不相关，技术失败也不计零发现。程序在预登记顺序内按 seed 和单位身份确定性抽样，以完整来源集合去除跨层重复；清单记录各层总体、去重后可抽数量、实际抽取数及缺口。选完后再次按不含层标签的身份 hash 排序再生成任务编号，避免编号连续段暗示分层。

若旧运行没有路由回执，程序用当前 `build_units` 分别重算关闭／开启支持文书发现的来源路由。只有单位 ID 与来源成员能重现冻结库存时，才使用其中可靠识别的支持来源层；输出明确标为 `reconstructed_routing`、`original_run_receipt=false`，记录路由代码 hash。无法重现或路由多义时，记录该层缺口，不把重算结果说成旧运行回执。

默认 `context_days=0`，每项仅提供当前完整单位或完整支持来源。显式 `--context-days 3` 可预登记开发检索范围：当前来源加代理时钟前后 3 日内全部交付来源及精确时间未明来源。锚点时间不明时仅保留当前来源并登记缺口，不默认开放全患者病历。日期不补零点；无时区但精确到时分的代理时钟使用项目既有 `+08:00` 检索约定并在 scope 声明。该范围不证明临床当时可见，不定义 Episode，不代表全患者完整病历。程序不按 LLM 指定的历史比较对象决定首轮附加资料。

这批材料是开发／挑战样本，层间可能重叠且抽样去重，不是 P0 代表性验证集；不从规则命中数或候选变化计算准确率。

## 目录及实际交接顺序

准备与回收的解析后目录必须有项目 `private/` 分量；CLI 进一步限定在本项目 `private/`。macOS 系统临时根 `/private/var` 不能代替研究资料目录。指向公开目录的软链接也会被拒绝。所有结果另存，已有目录、提交、开放记录和汇总文件不覆盖。每个实际输入文件保留精确字节快照及 hash，另存代码快照、阶段操作记录；提交的原始 JSON 字节与规范化绑定对象同时保存。

1. 协调者在新私有目录执行 `prepare`，核对研究者清单中的范围、来源、分层与缺口。
2. 只向实际独立人员分发所分配的 `phase_a/IV-xxxx/` 目录及操作说明。不能分发整个批次，不能把 `researcher_only/`、分层或模型提案一并交付。多位人员可填写同一份空白表，但使用各自独立的化名 ID；程序不会编造第二位人员。
3. 独立人员阅读全文、填写原文重建表，返回另存的 JSON。协调者执行 `submit`；程序逐字核验引文、原文 hash 与 Unicode codepoint 坐标，并封存首轮记录。
4. 同患者有多项任务时，该人员应先封存拟独立阅读的各项首轮记录，再开放该患者任一模型对照。程序仅按本批已登记的 `reveal` 阻止同一人员在已接触该患者模型对照后，将后来提交说成独立原文重建。先封存的首轮不被后来的正常开放反向否决；本地操作锁串行化封存与开放。该约束不要求患者一定有多项任务，也不要求全患者病历完整。
5. 协调者执行 `reveal`，将另存的 `researcher_only/revealed/IV-xxxx/<reviewer>/` 对照目录交给已完成首轮的同一人员。该文件分别保留发现阶段 LLM 输出、复核阶段 LLM 输出字段及程序结果；可含后续／范围外上下文，不能转成医生盲评包。
6. 人员填写 `comparison_template.json`，协调者执行 `compare` 封存，再用 `summarize` 回收每个维度的人工判断分布、完成数和待评分母。对照不会覆盖原文重建。

文件权限、目录隔离、hash 和人员声明不认证真实身份或实际独立性，也不能发现批次外接触史或协调者直接打开其他本地文件。实际材料分发与人员组织仍由研究团队负责。

## 原文重建表如何填写

自动生成的空白表为 `origin=ai_preparation`、`status=PENDING`；它不声称人工已参与。实际人员完成自己的重建时，显式改为 `origin=independent_human`，填写化名 `reviewer_id`、确认真实独立性并按实际情况改状态。AI 准备或合成测试只能保持 PENDING，不能触发开放或进入人工完成分母。

|字段|含义|
|---|---|
|`task_id/task_sha256/source_bundle_sha256`|保持任务原有绑定值，不能复制其他版本的值|
|`reviewer_id`|人员化名，字母／数字开头，随后可含字母、数字、点、下划线、连字符，最多 80 字符|
|`independence_attested`|人员声明自己在该次首轮前未接触相应模型提案；不是程序代为确认|
|`read_source_ids`|实际已阅读全文的来源 ID；完成时必须覆盖本任务全部声明来源|
|`complete_within_declared_scope`|仅声明本次给定原文范围已读完并完成重建；不声明全患者资料完整|
|`no_target_management_judgment_found`|`true`：人员在该范围未识别目标管理判断；`false`：人员识别到并提供 `management_judgment` 原文引文；`null`：人员仍无法确定，不能转为阴性|
|`findings`|人员提议的管理判断、证据、时间依据或未定表述；不是程序确认的临床事实。各项需稳定 `finding_id`、`kind`、说明及至少一个逐字原文引文，可用 `event_group_id` 记录人员提出的事件归组|
|`unresolved_issues`|具体未决理由及申请补充的来源 ID。未知来源可保留请求线索，不伪造引用原文|
|`status`|`INDEPENDENT_COMPLETE` 指人员已读完所给范围并完成首轮，包括明确保留未定的情形；不是确定语义正确的标志|

引文固定为 `source_id/source_sha256/start/end/exact_quote`；坐标从 0 开始，右端不含，按 Unicode codepoint 计数。例如虚构文字 `某😀原文` 中 emoji 的区间为 `[1,2)`。程序不模糊匹配、不搜索替换无效显式坐标、不用脱敏显示 hash 代替原文 hash。

已读完整但目标判断仍未定，可保留 `no_target_management_judgment_found=null`，同时填写带逐字引文的 `kind=uncertain` finding，或填写具体来源缺口／未决理由；之后可以开放对照。没有理由的空白表不能声明完成。存在缺历史资料等未决问题不自动否决当前可用原文。

## 对照表与统计边界

人员逐项填写 `event_detection`（管理判断定位与遗漏）、`event_granularity`（事件粒度）、`previous_comparison_object`（LLM 指定的既往临床管理判断比较对象）、`evidence_evolution`（信息演化）、`source_availability`（当时可见性）、`decision_time_boundary`（目标临床决策时间证据）。这些是研究核对维度，不是冻结临床 Mode/action 字典。

每项明确填写 `SUPPORTED/REVISE/UNRESOLVED/OUT_OF_SCOPE` 与理由。SUPPORTED 需当前原文引文或关联已封存原文 finding；REVISE 需当前原文引文。`event_ids` 只能关联该任务实际模型事件，`finding_ids` 只能关联该人员已封存首轮 finding。未取得历史原文时，用 UNRESOLVED 或 OUT_OF_SCOPE 并申请范围扩展，不强行填写历史比较对象，也不能用模型提案本身证明临床语义。

范围扩展须由协调者另存新批次／新版本，同步提供相同原文给独立人员；保留旧任务、首轮及对照。已看过原任务对照的人员不能被重新声称为仍盲的独立原文审核者。当前模块不自动裁定、合并人工分歧或制作 gold。

汇总分别报告：准备任务数、独立完成阅读／重建的提交数与去重任务数、目标发现／未发现／未定声明数、独立对照提交数、待评任务数、草稿数，以及各维度四种判断的计数和实际人工提交分母。UNRESOLVED、OUT_OF_SCOPE 不计作阴性或已确认错误。未回收人员意见时全部性能字段为 `not_measured`。双人意见未裁定，不自动当作确定真值；`adjudication_status` 始终为 `NOT_PERFORMED`。

## CLI

在项目根目录运行；以下路径是操作示例，准备输出目录必须尚不存在：

```sh
/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_auto_checkpoint_v06.independent_validation prepare \
  --release private/p2_checkpoint_next_20261028/model_validation/fixed_ten_development_release_v2 \
  --output private/p2_checkpoint_next_20261028/independent_validation_v01_NEW_BATCH \
  --seed 20261029 --per-stratum 2 --context-days 0

/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_auto_checkpoint_v06.independent_validation submit \
  --batch private/p2_checkpoint_next_20261028/independent_validation_v01_NEW_BATCH \
  --source-review private/returned/person-A_IV-0001_source.json

/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_auto_checkpoint_v06.independent_validation reveal \
  --batch private/p2_checkpoint_next_20261028/independent_validation_v01_NEW_BATCH \
  --task-id IV-0001 --reviewer-id person-A

/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_auto_checkpoint_v06.independent_validation compare \
  --batch private/p2_checkpoint_next_20261028/independent_validation_v01_NEW_BATCH \
  --comparison private/returned/person-A_IV-0001_comparison.json

/opt/anaconda3/bin/python3 -m scripts.cohort_construction.p2_auto_checkpoint_v06.independent_validation summarize \
  --batch private/p2_checkpoint_next_20261028/independent_validation_v01_NEW_BATCH \
  --output private/p2_checkpoint_next_20261028/independent_validation_v01_summary_NEW.json
```

CLI 仅打印聚合状态、错误代码和 hash，不输出患者原文或人员答案。原文 FHIR 适配沿用项目既有 `jsonschema` 依赖；上述 Anaconda Python 已具备。代码没有模型请求入口。

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /opt/anaconda3/bin/python3 -m pytest \
  scripts/cohort_construction/p2_auto_checkpoint_v06/independent_validation/tests -q
```

稳定契约问题与必需反例／阳性对照登记在 `independent_validation_issue_map.json`。测试全部使用虚构原文、模型提案和人员声明；测试通过不表示真实人工验证已经开展。
