# 四阶段资料提取规范 v0.3：时间线优先

当前单病例试验使用 [contract v0.3](../contracts/v0.3/01_algorithm_contract.md)，与旧规则的含义差异见 [semantic diff](semantic-diff-v0.2-v0.3.md)。v0.2 实现和既有输出保留。

## 处理步骤

1. 读取患者全部原始来源数组，建立完整时间线，不先限制 30 天或同一次住院。文书用正文完成/记录时间，检验开放用报告时间；create_time 不参与临床排序和可见性。
2. 从正文拟术式定位目标计划，确认本次 episode；从完整来源找最早相关影像/外院报告引用候选。候选最早扫描不自动认证为首次发现。
3. 通过本地 review 固定发现锚点、分期报告轮次和目标计划。回述片段绑定源全文 hash、Unicode 坐标、原文事件日期及适用阶段。没有外院原始报告，不补造报告。
4. 报告返回后 checkpoint 使用 `<=`，纳入当前报告及此前已可见资料；拟术式形成前使用 `<`，且目标计划整篇隔离。
5. 生成四问独立 context/messages。第三/四问证据一致。生成每份本院相关影像返回后的备选包，供讨论第一个起点。
6. 对比旧包的边界、资料集合及正文载荷，保存病例级 semantic diff 和全部来源/排除理由。

## 时间与原文规则

- 影像显式报告时间优先于检查时间；缺报告时间时只有 review 明确接受才启用检查时间代理，不声称已证实真实发布时刻。
- 秒级 `<=` 包含等时刻结果；分钟/日期级按不确定区间处理。日期级锚点自身纳入，不自动开放其他同日事件。没有默认次日零点的日汇集。
- 检验结果不能因采样在前就开放；病理日期午夜占位不能当实际报告钟点。
- 文书结合标题/正文识别，排除空白模板。显式现病史、首次病程第 2 段和诊断依据中的历史治疗不因“手术指征”等关键词被截断；本次计划章节和混合诊断 footer 单列隔离。
- 未核实的未来叙述仍需段落 review；不整篇回填后来的入院记录、查体或本次治疗方案。既往段落原文已在可见文书中时不重复加入。
- 影像提供现有原文，不拆病灶特征；检验保留全部符合边界的项目/原值/单位/采样与报告时间；缺失不补造。不同内容冲突保留，不静默选择。

## Review 配置

病例配置以源行别名为键，包含：

- `source_bundle_sha256`、`reviewer`：防止源文件变动后沿用旧坐标。
- `plan_source_id`：目标计划，可固定一个已定位的正文来源。
- `discovery_source_id`、`discovery_reason`：影像或接受的历史事件来源。历史片段编号为原来源加 `-H1` 等后缀。
- `staging_source_ids`、`staging_reason`：分期报告集，须位于计划边界之前；累计已可见资料，不只开放这些报告。
- `allow_imaging_exam_proxy`、`imaging_proxy_reason`：明确的报告已返回约定。
- `evidence_spans`：源 ID、全文 hash、起止位置、原文事件时间、接受状态、阶段及理由。不接受不存在于原文的事件日期。

这些配置确认资料组织，不填写转移状态、可切除性或标准治疗答案。不提供配置时，函数可以生成候选并标明缺乏审阅，CLI 单病例试验要求配置存在。

## 运行

```sh
python3 -m pancreatic_agent.timeline_extract \
  --input data/本地数据包/structured/patient_source_bundles_100.jsonl \
  --alias S001 \
  --reviews private/本地审阅配置.json \
  --output private/时间线试验 \
  --compare private/旧输出/S001/packets.json
```

`--compare` 可重复传入，对比旧自动版和旧审阅版。病例源 hash 必须一致。CLI 不调用模型，患者输出只能在项目 private/ 或 outputs/。旧命令 `python3 -m pancreatic_agent.four_stage` 仍执行 v0.2，不能将其输出称为 v0.3。

## 输出和阅读顺序

1. README.md：四问计数、锚点、不等式、备选影像包及待审假设。
2. 01_context.md 至 04_context.md、对应 messages 与 packets.json：实际模型输入。
3. after_*_context.md：逐份本院影像返回后的备选起点。
4. timeline.md：完整时间索引；timeline.json 保留每个来源原文、时间依据、坐标和来源键。**含拟术式和后续信息，只供审阅，不是模型输入。**
5. audit.json、review_manifest.json：规则执行和审阅依据。
6. SEMANTIC_DIFF.md、semantic_diff.json：新旧边界、数量、具体来源集合和正文变化。

全部有效包仍为 DRAFT_REVIEW_REQUIRED。没有可用锚点则 UNRESOLVED，不生成有效 messages。运行成功不等于临床信息完整、实际发布时刻准确或 episode 已核准。
